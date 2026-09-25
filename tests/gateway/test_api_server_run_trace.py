"""Iollo trace inputs stay allowlisted and transient while legacy events remain intact."""

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from gateway.platforms import api_server_runs
from tools.terminal_tool import record_session_cwd, clear_session_cwd


@pytest.mark.asyncio
async def test_trace_pairs_are_additive_allowlisted_and_not_persisted(tmp_path, monkeypatch, caplog):
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={}))
    run_id = "trace-run"
    queue = adapter._run_streams[run_id] = asyncio.Queue()
    adapter._run_idempotency_ids.add(run_id)
    store = adapter._run_idempotency_store
    adapter._run_idempotency_store = MagicMock()
    callback = adapter._make_run_event_callback(run_id, asyncio.get_running_loop())
    monkeypatch.setattr(api_server_runs.time, "time", lambda: 123.5)
    record_session_cwd(run_id, str(tmp_path))
    secret = "private-payload-never-forward"
    try:
        callback("tool.started", "browser_navigate", "legacy preview", {"url": "https://u:p@example.org/page?q=secret#frag", "secret": secret}, tool_call_id="browser", task_id=run_id)
        callback("tool.completed", "browser_navigate", result=json.dumps({"url": "https://u:p@example.org/page?q=secret", "title": "Title", "output": secret}), duration=1.23456, tool_call_id="browser")
        # Concurrent invocations complete in reverse order; IDs come from the executor.
        for call_id in ("cmd-a", "cmd-b"):
            callback("tool.started", "terminal", "old preview", {"command": "echo key=private\necho second", "stdin": secret}, tool_call_id=call_id, task_id=run_id)
        for call_id in ("cmd-b", "cmd-a"):
            callback("tool.completed", "terminal", result={"exit_code": 0, "output": secret}, tool_call_id=call_id)
        callback("tool.started", "unknown", "legacy", {"command": secret}, tool_call_id="unknown", task_id=run_id)
        callback("tool.completed", "unknown", result={"title": secret}, tool_call_id="unknown", attachment_ids=["unpublished"])
        await asyncio.sleep(0)
        events = [queue.get_nowait() for _ in range(queue.qsize())]
        assert all(e["timestamp"] == 123.5 and e["run_id"] == run_id for e in events)
        assert events[0]["preview"] == "legacy preview"
        assert events[0]["args"] == {"url": "https://example.org/page"}
        assert events[1]["duration"] == 1.235 and events[1]["error"] is False
        assert events[1]["result_summary"] == {"url": "https://example.org/page", "title": "Title"}
        additive = {"tool_call_id", "working_dir", "args", "result_summary"}
        assert {k: v for k, v in events[0].items() if k not in additive} == {
            "event": "tool.started", "run_id": run_id, "timestamp": 123.5,
            "tool": "browser_navigate", "preview": "legacy preview",
        }
        assert [e["tool_call_id"] for e in events[2:6]] == ["cmd-a", "cmd-b", "cmd-b", "cmd-a"]
        assert "\necho second" in events[2]["args"]["command"]
        assert "private" not in events[2]["args"]["command"]
        assert events[4]["result_summary"] == {"exit_code": 0}
        assert not ({"args", "working_dir", "result_summary", "attachment_ids"} & events[-1].keys())
        assert "args" not in events[-2]
        assert secret not in json.dumps(events)
        durable = repr(adapter._run_idempotency_store.mock_calls) + json.dumps(adapter._run_statuses) + caplog.text
        assert secret not in durable and str(tmp_path) not in durable and "echo second" not in durable
        from gateway.platforms.api_server_run_trace import tool_trace_fields

        long_id = "invocation-" * 30
        capped = tool_trace_fields("tool.completed", "browser_navigate", None, {
            "tool_call_id": long_id,
            "result": {"title": "t" * 200, "url": "https://example.org/" + "p" * 200},
        })
        assert 0 < len(capped["tool_call_id"]) <= 128
        assert capped["tool_call_id"] == tool_trace_fields(
            "tool.started", "unknown", None, {"tool_call_id": long_id})["tool_call_id"]
        assert len(capped["result_summary"]["title"]) == 120
        assert len(capped["result_summary"]["url"].removeprefix("https://example.org")) == 160
        assert tool_trace_fields("tool.completed", "patch", None, {
            "result": {"op": [], "lines_changed": 3, "output": secret},
        }) == {"result_summary": {"lines_changed": 3}}
        assert tool_trace_fields("tool.completed", "patch", None, {
            "result": {"lines_changed": True},
        }) == {}
    finally:
        clear_session_cwd(run_id)
        store.close()


@pytest.mark.asyncio
async def test_file_outcomes_follow_real_writes_and_live_cwd(tmp_path, monkeypatch):
    from tools.file_tools import write_file_tool

    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={}))
    queue = adapter._run_streams["files"] = asyncio.Queue()
    callback = adapter._make_run_event_callback("files", asyncio.get_running_loop())
    monkeypatch.setenv("TERMINAL_ENV", "local")
    task = "trace-file-task"
    try:
        for folder in (tmp_path, tmp_path / "nested"):
            folder.mkdir(exist_ok=True)
            record_session_cwd(task, str(folder))
            for index, expected_op in enumerate(("create", "edit")):
                call_id = f"{folder.name}-{index}"
                callback("tool.started", "write_file", "legacy", {"path": "note.txt", "content": "private content"}, tool_call_id=call_id, task_id=task)
                result = write_file_tool("note.txt", "hello\n", task_id=task)
                assert not json.loads(result).get("error")
                assert (folder / "note.txt").read_text() == "hello\n"
                callback("tool.completed", "write_file", result=result, tool_call_id=call_id)
                await asyncio.sleep(0)
                start, end = queue.get_nowait(), queue.get_nowait()
                assert start["working_dir"] == str(folder)
                assert start["args"] == {"path": "note.txt"}
                assert end["result_summary"] == {"op": expected_op}
                assert start["tool_call_id"] == end["tool_call_id"] == call_id
        (folder / "escape").symlink_to(tmp_path, target_is_directory=True)
        for path in (str(tmp_path / "outside.txt"), "../outside.txt", "~/secret", "escape/outside.txt"):
            callback("tool.started", "read_file", args={"path": path}, task_id=task, tool_call_id="read")
        callback("tool.completed", "terminal", result={"exit_code": True}, tool_call_id="bad-type")
        callback("tool.completed", "patch", result="not JSON", tool_call_id="missing")
        await asyncio.sleep(0)
        for _ in range(4):
            assert "path" not in queue.get_nowait().get("args", {})
        assert "result_summary" not in queue.get_nowait()
        assert "result_summary" not in queue.get_nowait()
    finally:
        clear_session_cwd(task)
        adapter._run_idempotency_store.close()

"""Iollo fork: ``tool.generating`` and ``run.heartbeat`` on /v1/runs (gateway/platforms/api_server_run_progress.py)."""

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from gateway.platforms import api_server_run_progress as progress_mod
from gateway.platforms import api_server_runs
from gateway.platforms.api_server_run_progress import HEARTBEAT, TOOL_GENERATING, RunProgress
from tests.gateway.test_api_server_runs import _create_runs_app, _make_adapter


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _progress():
    sent, clock = [], _Clock()
    return RunProgress(lambda name, **fields: sent.append((name, fields)), clock=clock), sent, clock


def _agent(activity=1.0, tool=None):
    return MagicMock(_last_activity_ts=activity, _current_tool=tool)


def test_tool_generating_names_the_tool_only_and_is_throttled():
    progress, sent, clock = _progress()
    progress.tool_generating("browser_exec")
    progress.tool_generating("browser_exec")
    clock.now += 5
    progress.tool_generating("browser_exec")
    assert sent == [(TOOL_GENERATING, {"tool": "browser_exec"})]
    progress.tool_generating("web_search")                    # another call: at once
    clock.now += 10
    progress.tool_generating("web_search")
    assert [s[1]["tool"] for s in sent] == ["browser_exec", "web_search", "web_search"]
    progress.tool_generating(None)
    assert len(sent) == 3


def test_heartbeat_only_while_the_agent_is_alive_quiet_and_not_running_a_tool():
    progress, _, clock = _progress()
    agent = _agent(activity=1.0)
    assert progress.due(agent) is None                       # baseline
    clock.now += progress_mod.PULSE_S
    assert progress.due(agent) is None                       # activity clock did not move: a hung agent stays silent
    agent._last_activity_ts = 2.0
    assert progress.due(agent) == {"event": HEARTBEAT}
    progress.note_event("message.delta")
    agent._last_activity_ts = 3.0
    assert progress.due(agent) is None                       # something else was just emitted
    clock.now += progress_mod.PULSE_S
    agent._last_activity_ts, agent._current_tool = 4.0, "terminal"
    assert progress.due(agent) is None                       # a running tool already said tool.started


def test_a_long_tool_call_generation_repeats_tool_generating_not_heartbeat():
    progress, sent, clock = _progress()
    agent = _agent(activity=1.0)
    progress.due(agent)
    progress.tool_generating("write_file")
    clock.now += progress_mod.PULSE_S
    agent._last_activity_ts = 2.0
    assert progress.due(agent) == {"event": TOOL_GENERATING, "tool": "write_file"}
    progress.note_event("tool.started")                      # the call is complete: back to heartbeats
    clock.now += progress_mod.PULSE_S
    agent._last_activity_ts = 3.0
    assert progress.due(agent) == {"event": HEARTBEAT}


@pytest.mark.asyncio
async def test_a_silent_run_publishes_progress_events_and_status(monkeypatch):
    """End to end on /v1/runs: a run whose model works silently gets tool.generating and run.heartbeat on its
    stream and its status ``last_event``; nothing reaches the output."""
    monkeypatch.setattr(progress_mod, "PULSE_S", 0.05)
    monkeypatch.setattr(progress_mod, "TOOL_GEN_MIN_S", 0.05)
    adapter = _make_adapter()
    app = _create_runs_app(adapter)
    events, statuses = [], []
    original = api_server_runs._RunLaunch.put_event

    def record(self, event):
        if event is not None:
            events.append(event)
            statuses.append(adapter._run_statuses.get(self.run_id, {}).get("last_event"))
        return original(self, event)
    monkeypatch.setattr(api_server_runs._RunLaunch, "put_event", record)
    agent = MagicMock(_current_tool=None, _last_activity_ts=0.0)
    agent.session_prompt_tokens = agent.session_completion_tokens = agent.session_total_tokens = 0

    built = {}

    def create(**kwargs):
        built.update(kwargs)
        return agent

    def silent_turn(**kwargs):
        agent.tool_gen_callback("write_file")
        for _ in range(8):                                    # streaming tool arguments, nothing emitted
            agent._last_activity_ts = time.time()
            time.sleep(0.03)
        built["stream_delta_callback"]("ok")                  # the call ended; then a long silent model call
        for _ in range(8):
            agent._last_activity_ts = time.time()
            time.sleep(0.03)
        return {"final_response": "done"}
    agent.run_conversation.side_effect = silent_turn
    async with TestClient(TestServer(app)) as cli:
        with patch.object(adapter, "_create_agent", side_effect=create):
            resp = await cli.post("/v1/runs", json={"input": "hello"})
            run_id = (await resp.json())["run_id"]
            for _ in range(60):
                status = await (await cli.get(f"/v1/runs/{run_id}")).json()
                if status["status"] == "completed":
                    break
                await asyncio.sleep(0.05)
    names = [e["event"] for e in events]
    assert TOOL_GENERATING in names and HEARTBEAT in names
    assert names.index(HEARTBEAT) > names.index("message.delta")
    assert {TOOL_GENERATING, HEARTBEAT} <= set(statuses)        # status last_event moved with each one
    assert all(set(e) == {"event", "run_id", "timestamp"} for e in events if e["event"] == HEARTBEAT)
    generating = [e for e in events if e["event"] == TOOL_GENERATING]
    assert all(set(e) == {"event", "run_id", "timestamp", "tool"} and e["tool"] == "write_file" for e in generating)
    assert names[-1] == "run.completed" and status["output"] == "done"
    assert adapter.__dict__.get("_run_progress", {}) == {}  # released with the run

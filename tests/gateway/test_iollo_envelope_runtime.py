"""Shared producer through real runs admission, HTTP routes and durable replay."""
import asyncio
import json
import threading
from unittest.mock import MagicMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from iollo_envelope.store import EnvelopeStore


def client_for(adapter):
    app = web.Application()
    for method, path, handler in adapter._http_route_table():
        app.router.add_route(method, path, handler)
    return TestClient(TestServer(app))


@pytest.mark.asyncio
async def test_scripted_run_revisions_persist_and_replay(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = PlatformConfig(enabled=True, extra={"key": "fake-key"})
    adapter = APIServerAdapter(config)
    started, finish = threading.Event(), threading.Event()

    def create_agent(**kwargs):
        callback = kwargs["tool_progress_callback"]
        agent = MagicMock(session_prompt_tokens=0, session_completion_tokens=0, session_total_tokens=0)
        def run(**_kwargs):
            callback("tool.started", "terminal", "echo fake",
                     {"command": "echo token=fake-secret\nprivate"}, tool_call_id="fake-call")
            started.set()
            assert finish.wait(20)
            callback("tool.completed", "terminal", tool_call_id="fake-call")
            return {"final_response": "Finished."}
        agent.run_conversation.side_effect = run
        return agent

    monkeypatch.setattr(adapter, "_create_agent", create_agent)
    headers = {"Authorization": "Bearer fake-key"}
    try:
        async with client_for(adapter) as client:
            assert (await client.get("/v1/runs/fake/envelope")).status == 401
            assert (await client.get("/v1/runs/fake/envelope", headers=headers)).status == 404
            response = await client.post("/v1/runs", headers={**headers, "Idempotency-Key": "scripted"},
                                         json={"input": "fake question", "envelope_context": {
                                             "surface": "mac", "conversation_id": 3}})
            assert response.status == 202
            run_id = (await response.json())["run_id"]
            url = f"/v1/runs/{run_id}"
            assert await asyncio.to_thread(started.wait, 10)
            prompt = {"kind": "approval", "prompt_id": "fake-prompt", "tier3": "pay",
                      "text": "Pay?", "options": []}
            block = {"type": "code", "id": "fake-code", "language": "python", "text": "pass"}
            assert (await client.put(url + "/envelope/inputs", json={"blocks": [block]})).status == 401
            response = await client.put(url + "/envelope/inputs", headers=headers,
                                        json={"prompt": prompt, "blocks": [block]})
            assert response.status == 200
            partial = (await response.json())["envelope"]
            assert partial["prompt"] == prompt and block in partial["blocks"]
            assert partial["meta"]["surface_origin"] == "mac" and partial["conversation_id"] == 3
            stream = await client.get(url + "/envelope", headers=headers)
            assert stream.status == 200
            async def frame():
                while True:
                    line = await asyncio.wait_for(stream.content.readline(), 10)
                    assert line
                    if line.startswith(b"data: "):
                        return json.loads(line[6:])
            first = await frame()
            revisions = [first]
            finish.set()
            final = await frame()
            revisions.append(final)
            while final["status"] == "partial":
                final = await frame()
                revisions.append(final)
            await stream.read()
            polled = await client.get(url, headers=headers)
            assert (await polled.json())["envelope"] == final
            assert all(value["message_id"] == partial["message_id"] for value in revisions)
            assert all(a["rev"] < b["rev"] for a, b in zip(revisions, revisions[1:]))
            assert first["rev"] == partial["rev"] < final["rev"]
            assert final["status"] == "final" and final["trace"][0]["state"] == "done"
            assert "prompt" not in final and "fake-secret" not in json.dumps(final)
            assert EnvelopeStore(tmp_path / "state.db").for_run(run_id) == final
            response = await client.put(url + "/envelope/inputs", headers=headers, json={"text": "Stopped."})
            notice = (await response.json())["envelope"]
            assert notice["message_id"] == final["message_id"] and notice["rev"] > final["rev"]
        adapter._run_idempotency_store.close()
        # A fresh adapter has neither the producer nor live run status; both replay from disk.
        adapter = APIServerAdapter(config)
        async with client_for(adapter) as client:
            polled = await client.get(url, headers=headers)
            assert polled.status == 200 and (await polled.json())["envelope"] == notice
            replay = await client.get(url + "/envelope", headers=headers)
            assert replay.status == 200
            assert json.loads((await replay.text()).split("data: ", 1)[1]) == notice
            assert (await client.get(url + "/envelope")).status == 401
    finally:
        finish.set()
        await asyncio.gather(*adapter._active_run_tasks.values(), return_exceptions=True)
        adapter._run_idempotency_store.close()

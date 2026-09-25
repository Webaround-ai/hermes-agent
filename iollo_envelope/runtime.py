"""Install the producer at the existing Hermes runs seams; no fork edits at runtime.

The image build appends one install call to api_server_runs. All new routes first use
the existing status handler, including its bearer and run-owner checks. Revisions
are generated independently of subscribers and stored in the local sync table.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from contextvars import ContextVar
import json
import logging
import os
from pathlib import Path
import time

from aiohttp import web

from .producer import Producer
from .store import EnvelopeStore

log = logging.getLogger(__name__)
context = ContextVar("iollo_envelope_context", default=None)
TERMINAL = {"completed", "failed", "cancelled", "interrupted", "error"}


def runtime_home():
    if os.environ.get("HERMES_HOME"):
        return Path(os.environ["HERMES_HOME"])
    from hermes_constants import get_hermes_home
    return get_hermes_home()


class Entry:
    def __init__(self, run, metadata, store):
        self.producer = Producer(run.run_id, surface=metadata.get("surface", "mac"),
                                 conversation_id=metadata.get("conversation_id"))
        self.session_key = run.gateway_session_key or run.session_id
        self.store, self.lock = store, asyncio.Lock()
        self.finished = asyncio.Event()
        self.done, self.failed = False, False
        self.latest = None
        self.terminal_text, self.terminal_status = "", "final"

    async def publish(self, **kwargs):
        async with self.lock:
            value = self.producer.revision(**kwargs)
            if value is not None:
                await asyncio.to_thread(self.store.put, value["message_id"], self.session_key, value,
                                        bind_latest=value["status"] != "partial" and bool(value["text_md"]))
                self.latest = value
            return value

    async def ticker(self):
        while True:
            await asyncio.sleep(1)
            try:
                await self.publish()
            except Exception as error:
                log.warning("envelope_partial_failed reason=%s", type(error).__name__)


def install(namespace):
    if namespace.get("_iollo_envelope_installed"):
        return
    namespace["_iollo_envelope_installed"] = True
    original = {name: namespace[name] for name in (
        "_http_routes", "_handle_runs", "_execute_run", "_make_run_event_callback", "_handle_get_run",
        "_sweep_orphaned_runs_once")}

    def entries(owner):
        if not hasattr(owner, "_iollo_envelopes"):
            owner._iollo_envelopes = {}
        return owner._iollo_envelopes

    async def handle_runs(owner, request, **kwargs):
        try:
            body = await request.json()
            metadata = body.get("envelope_context", {}) if isinstance(body, dict) else {}
            if not isinstance(metadata, dict) or metadata.get("surface", "mac") not in {
                    "mac", "pro", "phone", "web", "whatsapp", "sms"}:
                raise ValueError()
            if type(metadata.get("enabled", True)) is not bool:
                raise ValueError()
            cid = metadata.get("conversation_id")
            if cid is not None and (type(cid) is not int or cid < 1):
                raise ValueError()
        except (ValueError, TypeError):
            return web.json_response({"code": "invalid_envelope_context"}, status=400)
        token = context.set(metadata)
        try:
            return await original["_handle_runs"](owner, request, **kwargs)
        finally:
            context.reset(token)

    async def execute(owner, run, **kwargs):
        metadata = context.get() or {}
        if not metadata.get("enabled", True):
            return await original["_execute_run"](owner, run, **kwargs)
        home = runtime_home()
        entry = Entry(run, metadata, EnvelopeStore(home / "state.db"))
        entries(owner)[run.run_id] = entry
        ticker = asyncio.create_task(entry.ticker())
        try:
            return await original["_execute_run"](owner, run, **kwargs)
        finally:
            ticker.cancel()
            with suppress(asyncio.CancelledError):
                await ticker
            # Drain callbacks queued by the executor thread before the final revision.
            await asyncio.sleep(0)
            status = owner._run_statuses.get(run.run_id, {})
            entry.done = True
            entry.terminal_status = "final" if status.get("status") == "completed" else "error"
            entry.terminal_text = str(status.get("output") or "")
            if entry.terminal_status == "error" and not entry.terminal_text:
                entry.terminal_text = "Stopped." if status.get("status") == "cancelled" else "I couldn't finish that."
            try:
                await entry.publish(text=entry.terminal_text, status=entry.terminal_status)
            except Exception as error:
                entry.failed = True
                log.warning("envelope_store_failed reason=%s", type(error).__name__)
            finally:
                entry.finished.set()

    def callback(owner, run_id, loop, **kwargs):
        old = original["_make_run_event_callback"](owner, run_id, loop, **kwargs)

        def consume(event):
            entry = entries(owner).get(run_id)
            if entry and not entry.done:
                try:
                    entry.producer.event(event)
                except (ValueError, TypeError, KeyError):
                    log.warning("invalid_envelope_event")

        def wrapped(event_type, tool_name=None, preview=None, args=None, **fields):
            old(event_type, tool_name=tool_name, preview=preview, args=args, **fields)
            if event_type in {"tool.started", "tool.completed"}:
                # Only allowlisted structured lifecycle fields, never arbitrary result bodies.
                event = {"event": event_type, "tool": tool_name, "preview": preview,
                         "error": fields.get("is_error", False), "timestamp": time.time()}
                for key in ("tool_call_id", "working_dir", "result_summary", "attachment_ids"):
                    if key in fields:
                        event[key] = fields[key]
                if isinstance(args, dict):
                    event["args"] = {k: args[k] for k in ("url", "path", "op", "command") if k in args}
                loop.call_soon_threadsafe(consume, event)
        return wrapped

    async def get_run(owner, request, **kwargs):
        response = await original["_handle_get_run"](owner, request, **kwargs)
        if response.status != 200:
            return response
        data = json.loads(response.body)
        entry = entries(owner).get(request.match_info["run_id"])
        if entry:
            if data.get("status") in TERMINAL:
                await entry.finished.wait()
            if entry.failed:
                return web.json_response({"code": "envelope_unavailable"}, status=503)
            data["envelope_producer"] = 1
            if entry.latest:
                data["envelope"] = entry.latest
            return web.json_response(data)
        home = runtime_home()
        stored = await asyncio.to_thread(EnvelopeStore(home / "state.db").for_run, request.match_info["run_id"])
        if stored and stored["status"] != "partial":
            return web.json_response({**data, "envelope_producer": 1, "envelope": stored})
        return response

    async def authorized_entry(owner, request):
        # Match the lifecycle SSE/API bearer, not a read-only room status grant.
        auth_error = owner._check_auth(request)
        if auth_error is not None:
            return None, auth_error
        response = await owner._handle_get_run(request)
        if response.status != 200:
            return None, response
        entry = entries(owner).get(request.match_info["run_id"])
        if entry is None:
            return None, web.json_response({"code": "envelope_unavailable"}, status=404)
        return entry, None

    async def stream(owner, request):
        entry, error = await authorized_entry(owner, request)
        if error is not None:
            # A completed run remains replayable after process restart or live-state eviction.
            if error.status == 404:
                saved = await owner._handle_get_run(request)
                if saved.status == 200:
                    value = json.loads(saved.body).get("envelope")
                    if value is not None:
                        return web.Response(text="event: envelope\ndata: " + json.dumps(value) + "\n\n",
                                            content_type="text/event-stream", headers={"Cache-Control": "no-store"})
            return error
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream",
            "Cache-Control": "no-store", "X-Accel-Buffering": "no"})
        await response.prepare(request)
        rev = 0
        try:
            while True:
                value = entry.latest
                if value and value["rev"] > rev:
                    await response.write(("event: envelope\ndata: " + json.dumps(value, separators=(",", ":")) + "\n\n").encode())
                    rev = value["rev"]
                    if value["status"] != "partial":
                        break
                if entry.failed:
                    break
                await asyncio.sleep(0.1)
        except (ConnectionError, asyncio.CancelledError):
            pass
        return response

    async def inputs(owner, request):
        entry, error = await authorized_entry(owner, request)
        if error is not None:
            return error
        try:
            if request.content_length and request.content_length > 256 * 1024:
                raise ValueError()
            raw = await request.read()
            if len(raw) > 256 * 1024:
                raise ValueError()
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError()
            if "text" in data and (not entry.done or not isinstance(data["text"], str)):
                raise ValueError()
            entry.producer.inputs(data)
            if "text" in data:
                entry.terminal_text = data["text"]
            value = await entry.publish(force=True, **({"text": entry.terminal_text,
                                        "status": entry.terminal_status} if entry.done else {}))
        except (ValueError, TypeError, KeyError):
            return web.json_response({"code": "invalid_envelope_input"}, status=400)
        return web.json_response({"envelope": value})

    def routes(owner):
        async def stream_handler(request):
            return await stream(owner, request)
        async def inputs_handler(request):
            return await inputs(owner, request)
        return original["_http_routes"](owner) + [
            ("GET", "/v1/runs/{run_id}/envelope", stream_handler),
            ("PUT", "/v1/runs/{run_id}/envelope/inputs", inputs_handler)]

    def sweep(owner, *args, **kwargs):
        result = original["_sweep_orphaned_runs_once"](owner, *args, **kwargs)
        for run_id in list(entries(owner)):
            if run_id not in owner._run_statuses and entries(owner)[run_id].done:
                entries(owner).pop(run_id, None)
        return result

    namespace.update(_http_routes=routes, _handle_runs=handle_runs, _execute_run=execute,
                     _make_run_event_callback=callback, _handle_get_run=get_run, _sweep_orphaned_runs_once=sweep)

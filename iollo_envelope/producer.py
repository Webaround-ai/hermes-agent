"""Bounded, transport-independent state machine for full envelope revisions.

Inputs are run events and already-authorized tool deliveries. Home writes keep using the
home tool/store and never enter this state. Call ``revision`` on a one-second timer as
well as after events, so a quiet last completion is not lost to the throttle.
"""
from __future__ import annotations

import copy
import json
import time
from functools import lru_cache
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

from .core import build, new_message_id, stamp, trace_for_tool, validate_envelope, TRACE_DETAILS

LIMIT = 50
INTERVAL = 1.0
TOOL_VERBS = {"browser_exec": "Used the browser", "browser_navigate": "Opened", "browser_click": "Clicked",
              "browser_type": "Typed", "browser_snapshot": "Read the page", "browser_scroll": "Scrolled",
              "browser_vision": "Looked at the page", "browser_vault_fill": "Filled in a saved login",
              "web_search": "Searched", "web_extract": "Read", "terminal": "Ran", "read_file": "Read file",
              "write_file": "Wrote file", "patch": "Edited file", "search_files": "Searched files",
              "live_view_link": "Asked the owner to open the live screen", "delegate_task": "Split off a helper",
              "memory": "Noted", "session_search": "Looked back"}


@lru_cache(maxsize=1)
def catalog():
    from jsonschema import Draft202012Validator
    return Draft202012Validator(json.loads(Path(__file__).with_name("catalog.schema.json").read_text()))


def validate_blocks(blocks):
    if not isinstance(blocks, list) or len(blocks) > LIMIT:
        raise ValueError("invalid blocks")
    for block in blocks:
        if len(json.dumps(block).encode()) > 64 * 1024 or not catalog().is_valid(block):
            raise ValueError("invalid catalog block")
        if block["type"] == "a2ui":
            for message in block["messages"]:
                payload, = [v for k, v in message.items() if k != "version"]
                if payload.get("surfaceId") != block["surface_id"]:
                    raise ValueError("surface mismatch")
    return copy.deepcopy(blocks)


def structured_detail(tool, event, entry):
    """Optional 036 fields; no raw arguments, cwd or result bodies survive this function."""
    from urllib.parse import urlsplit
    data = event.get("result_summary") if event.get("event") == "tool.completed" else event.get("args")
    if not isinstance(data, dict):
        return
    detail = dict(entry.get("detail") or {})
    kind = entry["kind"]
    if kind == "browser":
        try:
            url = urlsplit(str(data.get("url") or ""))
            if url.scheme in {"http", "https"} and url.hostname:
                detail.update(url_host=url.hostname[:128], url_path=url.path[:160])
        except ValueError:
            pass
        if isinstance(data.get("title"), str):
            detail["title"] = data["title"][:120]
    elif kind == "file":
        path, cwd = data.get("path"), event.get("working_dir")
        if isinstance(path, str):
            if path.startswith("/"):
                try:
                    path = str(PurePosixPath(path).relative_to(cwd)) if isinstance(cwd, str) and cwd.startswith("/") else None
                except ValueError:
                    path = None
            if path:
                from .core import relative_trace_path
                try:
                    detail["path"] = relative_trace_path(path)[:200]
                except ValueError:
                    pass
        if data.get("op") in {"read", "edit", "create", "delete"}:
            detail["op"] = data["op"]
        if type(data.get("lines_changed")) is int:
            detail["lines_changed"] = data["lines_changed"]
    elif kind == "command":
        from .core import redact_command
        if isinstance(data.get("command"), str):
            detail["command"] = redact_command(data["command"])[:120]
        if type(data.get("exit_code")) is int:
            detail["exit_code"] = data["exit_code"]
    entry["detail"] = TRACE_DETAILS[kind].model_validate(detail).model_dump(exclude_none=True)


class Producer:
    def __init__(self, run_id, *, surface="mac", conversation_id=None, message_id=None, clock=time.monotonic):
        self.run = SimpleNamespace(run_id=run_id, conversation_id=conversation_id)
        self.surface, self.message_id, self.clock = surface, message_id or new_message_id(), clock
        self.created_at = stamp()
        self.rev, self.last_emit, self.latest, self.dirty = 0, None, None, False
        self.blocks, self.attachments, self.links, self.progress, self.trace = [], [], [], [], []
        self.prompt = None

    def widget(self, blocks, *, container="reply", home_writer=None, **home_options):
        """Tool adapters pass catalog blocks here; home policy/store is supplied by the host."""
        blocks = validate_blocks(blocks)
        if container == "home":
            if home_writer is None:
                raise ValueError("home store unavailable")
            return home_writer(blocks, **home_options)
        if container != "reply":
            raise ValueError("unknown container")
        self.inputs({"blocks": (self.blocks + blocks)[-LIMIT:]})

    def inputs(self, data):
        """Replace delivery snapshots atomically; retries cannot duplicate cards/files."""
        blocks = validate_blocks(data.get("blocks", self.blocks))
        attachments = copy.deepcopy(data.get("attachments", self.attachments))[-LIMIT:]
        links = copy.deepcopy(data.get("links", self.links))[-LIMIT:]
        prompt = copy.deepcopy(data.get("prompt", self.prompt))
        build(self.run, surface=self.surface, held_cards=blocks, attachments=attachments,
              links=links, prompt=prompt)
        self.blocks, self.attachments, self.links, self.prompt = blocks, attachments, links, prompt
        known = {a.get("id") for a in attachments if a.get("id")}
        for item in self.trace:
            if "attachment_ids" in item:
                item["attachment_ids"] = [ref for ref in item["attachment_ids"] if ref in known]
        if prompt and prompt["kind"] == "approval" and not any(
                t["kind"] == "approval" and t.get("detail", {}).get("prompt_id") == prompt["prompt_id"] for t in self.trace):
            self.trace.append({"id": new_message_id(), "kind": "approval", "label": "Approval requested",
                               "state": "started", "at": stamp(),
                               "detail": {"prompt_id": prompt["prompt_id"], "tier": prompt["tier3"]}})
            del self.trace[:-LIMIT]
        self.dirty = True

    def event(self, event):
        name, tool = event.get("event"), str(event.get("tool") or "")[:128]
        if not tool or name not in {"tool.started", "tool.completed"}:
            return
        call_id = event.get("tool_call_id")
        call_id = call_id if isinstance(call_id, str) and 0 < len(call_id) <= 128 else None
        if name == "tool.started":
            if call_id and any(p["id"] == call_id for p in self.progress):
                return
            label = (TOOL_VERBS.get(tool) or tool.replace("_", " ").capitalize())[:200]
            entry = {"id": call_id or new_message_id(), "tool": tool, "label": label, "state": "started"}
            self.progress.append(entry)
            trace = trace_for_tool(tool, event, id=entry["id"], label=label, state="started")
            structured_detail(tool, event, trace)
            self.trace.append(trace)
            del self.progress[:-LIMIT]
            del self.trace[:-LIMIT]
        else:
            entry = next((p for p in self.progress if p["state"] == "started" and
                          (p["id"] == call_id if call_id else p["tool"] == tool)), None)
            if entry is None:
                return
            entry["state"] = "failed" if event.get("error") else "done"
            trace = next((t for t in self.trace if t["id"] == entry["id"]), None)
            if trace:
                trace["state"] = entry["state"]
                structured_detail(tool, event, trace)
                known = {a.get("id") for a in self.attachments if a.get("id")}
                refs = [x for x in event.get("attachment_ids", []) if isinstance(x, str) and x in known][:LIMIT]
                if refs:
                    trace["attachment_ids"] = refs
        self.dirty = True

    def revision(self, *, text="", status="partial", force=False):
        now = self.clock()
        if status == "partial" and not force and (not self.dirty or
                self.last_emit is not None and now - self.last_emit < INTERVAL):
            return None
        if status != "partial":
            self.prompt = None
        value = build(self.run, text, self.blocks, self.attachments, self.prompt, self.links,
                      surface=self.surface, message_id=self.message_id, rev=self.rev + 1, status=status,
                      progress=self.progress, trace=self.trace, parse_cards=self.surface != "sms")
        validate_blocks(value["blocks"])
        if len(json.dumps(value).encode()) > 256 * 1024:
            raise ValueError("envelope too large")
        self.rev += 1
        self.last_emit, self.dirty, self.latest = now, False, copy.deepcopy(value)
        return value

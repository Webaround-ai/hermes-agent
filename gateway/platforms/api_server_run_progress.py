"""Iollo fork: progress signals on ``/v1/runs`` while the model works silently.

A caller's watchdog treats a run with no events as stalled, but a run emits nothing while the model generates a long
tool call's arguments, reasons at length, or while the context is compacted. Two content-free events fill the gap:

- ``tool.generating`` ``{tool}``: the model started producing a call to ``tool`` (the agent's ``tool_gen_callback``;
  the tool name only, never arguments), repeated at most every ``TOOL_GEN_MIN_S`` while it is still generating.
- ``run.heartbeat`` (no fields): at most every ``PULSE_S`` while the agent is alive (its activity clock moved since
  the last check: a streaming or waiting model call, compaction) and nothing else was emitted, no tool is running
  and the run is ``running`` (not waiting for an approval). A hung agent stays silent, so a watchdog still sees it.

Both go through the run's normal event path: the status ``updated_at``/``last_event`` move like for any event (not
persisted to the idempotency store: no field it keeps changes), the SSE queue gets the event while a stream is open
and nothing reaches the transcript or the reply text. Cost per active run: one sleeping asyncio task and a few
attribute reads every ``PULSE_S``; no thread, no agent call, nothing when the run is not running.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Callable, Dict, Optional

PULSE_S = 12.0
TOOL_GEN_MIN_S = 10.0
TOOL_GENERATING, HEARTBEAT = "tool.generating", "run.heartbeat"
_ENDS_GENERATION = frozenset({"tool.started", "tool.completed", "message.delta", "message.interim"})


class RunProgress:
    """Per-run progress state. ``emit(name, **fields)`` publishes one event (status + stream); thread-safe."""

    def __init__(self, emit: Callable[..., None], *, clock: Callable[[], float] = time.monotonic) -> None:
        self._emit, self._clock = emit, clock
        self._lock = threading.Lock()
        self.last_event_at = clock()
        self._generating: Optional[str] = None
        self._generating_sent_at = 0.0
        self._activity: Any = None

    def note_event(self, name: Optional[str]) -> None:
        """Any event the run emitted (called by the run's own event paths)."""
        with self._lock:
            self.last_event_at = self._clock()
            if name in _ENDS_GENERATION:
                self._generating = None

    def tool_generating(self, tool_name: Any) -> None:
        """``tool_gen_callback``: runs on the agent thread when a tool call starts streaming."""
        name = str(tool_name or "")[:64]
        if not name:
            return
        with self._lock:
            now = self._clock()
            due = name != self._generating or now - self._generating_sent_at >= TOOL_GEN_MIN_S
            self._generating = name
            if not due:
                return
            self._generating_sent_at = now
        self._send(TOOL_GENERATING, tool=name)

    def due(self, agent: Any) -> Optional[Dict[str, Any]]:
        """The progress event to send now (``{"event", ...fields}``) or None."""
        activity = getattr(agent, "_last_activity_ts", None)
        with self._lock:
            moved = isinstance(activity, (int, float)) and activity != self._activity
            self._activity = activity
            now = self._clock()
            if not moved or now - self.last_event_at < PULSE_S - 0.5:
                return None
            if self._generating:
                if now - self._generating_sent_at < TOOL_GEN_MIN_S:
                    return None
                self._generating_sent_at = now
                return {"event": TOOL_GENERATING, "tool": self._generating}
        if getattr(agent, "_current_tool", None):
            return None          # a running tool already said tool.started; its own silence is not ours to fill
        return {"event": HEARTBEAT}

    def _send(self, name: str, **fields: Any) -> None:
        self.note_event(name)
        self._emit(name, **fields)


async def pulse(progress: RunProgress, agent: Any, status_of: Callable[[], Optional[str]],
                interval: Optional[float] = None) -> None:
    """Send ``run.heartbeat`` / repeated ``tool.generating`` while the run is running; ends with the run."""
    progress.due(agent)          # baseline the activity clock
    while True:
        await asyncio.sleep(interval or PULSE_S)
        status = status_of()
        if status is None:
            return
        if status != "running":
            continue
        event = progress.due(agent)
        if event is not None:
            name = event.pop("event")
            progress._send(name, **event)

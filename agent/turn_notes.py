"""Iollo fork: the per-turn notes of a ``/v1/runs`` ``instructions`` ride the turn's user message.

The iollo control plane sends one ``instructions`` text per run (``control-plane/app/run_context.py``
``run_instructions``): the cross-surface rules, the surface rules, then, when the turn has any, a block that starts
with ``TURN_NOTES_HEADER`` and holds this turn's notes (route, first reply, app language, operator note...).
``instructions`` becomes the agent's ``ephemeral_system_prompt``, appended to the cached system prompt on every
model call, so a note that changed between two turns changed the system text, the Responses ``instructions``
and with them the ``prompt_cache_key``: the first call of the turn re-read the whole prefix uncached
(2026-10-09 trace: 12k-29k input tokens, cache 0).

``split_turn_notes`` keeps the rules as the ephemeral system prompt (stable per surface) and hands the notes
block to the gateway's per-turn note channel (``agent._gateway_turn_context_notes``, consumed by
``agent/turn_context.py``), which appends it to this turn's user message and replays those exact bytes later.
The header is a contract with the control plane: keep the two strings identical.
"""

from __future__ import annotations

from typing import Any, Tuple

TURN_NOTES_HEADER = "Context for this turn only (from iollo, not from your owner; never quote it back):"


def split_turn_notes(instructions: Any) -> Tuple[Any, str]:
    """``(system_part, notes_block)``: the text before the notes header, and the header with its notes.
    Instructions without the header come back unchanged with no notes; a non-string is passed through."""
    if not isinstance(instructions, str):
        return instructions, ""
    if instructions.startswith(TURN_NOTES_HEADER):
        at = 0
    else:
        at = instructions.find("\n\n" + TURN_NOTES_HEADER)
        if at < 0:
            return instructions, ""
    system_part = instructions[:at].rstrip()
    return (system_part or None), instructions[at:].strip()

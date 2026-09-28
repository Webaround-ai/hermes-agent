"""Caller-declared prompt version for a session's stored system prompt (iollo fork brief 043).

A session keeps the system prompt it stored on its first turn (prompt caching), so a caller that
changes prompt-visible text (persona, plugin prompt sections) had to rotate the session to reach
it. A caller may instead declare a ``prompt_version`` per turn: it is stored in the session's
``model_config`` next to the prompt, and when a turn's declared version differs from the stored
one (or none is stored) the stored prompt is rebuilt once and the new version stored. No declared
version means today's behaviour. The transcript and the session id never change.

Compression children inherit the version through ``_session_init_model_config``, which is what
``publish_compression_child`` and the lazy first-turn row insert write.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

PROMPT_VERSION_KEY = "prompt_version"
MAX_PROMPT_VERSION_LENGTH = 200


def normalize_prompt_version(value: Any) -> Optional[str]:
    """Request value -> stored form. ``None``/blank -> ``None``; raises ``ValueError`` otherwise invalid."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("'prompt_version' must be a string")
    value = value.strip()
    if len(value) > MAX_PROMPT_VERSION_LENGTH:
        raise ValueError(f"'prompt_version' must be at most {MAX_PROMPT_VERSION_LENGTH} characters")
    return value or None


def set_prompt_version(agent: Any, version: Optional[str]) -> None:
    """Declare this turn's prompt version on a freshly built agent (before ``run_conversation``)."""
    agent._prompt_version = version or None
    init_config = getattr(agent, "_session_init_model_config", None)
    if version and isinstance(init_config, dict):
        init_config[PROMPT_VERSION_KEY] = version


def stored_prompt_version(session_row: Optional[Mapping[str, Any]]) -> Optional[str]:
    """The version stored with a session row, or ``None``."""
    raw = (session_row or {}).get("model_config")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    value = raw.get(PROMPT_VERSION_KEY) if isinstance(raw, dict) else None
    return value if isinstance(value, str) and value else None


def declared_prompt_version(agent: Any) -> Optional[str]:
    """The version this turn declared, or ``None`` (only a non-empty string counts)."""
    declared = getattr(agent, "_prompt_version", None)
    return declared if isinstance(declared, str) and declared else None


def prompt_version_stale(agent: Any, session_row: Optional[Mapping[str, Any]]) -> bool:
    """True when the turn declares a version and the session stored another one (or none)."""
    declared = declared_prompt_version(agent)
    return declared is not None and stored_prompt_version(session_row) != declared


def persist_prompt_version(agent: Any) -> None:
    """Store the declared version with the session row (no-op without one, or before the row exists:
    the lazy row insert carries it through ``_session_init_model_config``)."""
    declared = declared_prompt_version(agent)
    session_db = getattr(agent, "_session_db", None)
    if declared is None or not session_db or getattr(agent, "_persist_disabled", False) is True:
        return
    try:
        session_db.patch_session_model_config(agent.session_id, {PROMPT_VERSION_KEY: declared})
    except Exception as exc:
        logger.warning("Session DB prompt_version update failed for session %s: %s", agent.session_id, exc)

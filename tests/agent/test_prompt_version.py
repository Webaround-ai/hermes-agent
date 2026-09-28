"""A caller-declared ``prompt_version`` refreshes a session's stored system prompt (iollo fork brief 043).

Runs against a real ``SessionDB``: the stored prompt and version round-trip through state.db the way a
fresh per-request ``AIAgent`` (api_server) reads them every turn.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agent.conversation_loop import _restore_or_build_system_prompt
from agent.prompt_version import normalize_prompt_version, set_prompt_version, stored_prompt_version
from hermes_state import SessionDB

STORED = "OLD PERSONA\n\nModel: test-model\nProvider: openrouter"
HISTORY = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]


@pytest.fixture
def db(tmp_path):
    with SessionDB(db_path=tmp_path / "state.db") as session_db:
        yield session_db


def _seed(db, session_id="s1", version=None):
    db.create_session(session_id, "api_server", model_config={"prompt_version": version} if version else None)
    db.update_system_prompt(session_id, STORED)


def _turn(db, version, session_id="s1", built="NEW PERSONA\n\nModel: test-model\nProvider: openrouter"):
    """One turn's prompt restore on a fresh agent, as the api_server builds it per request."""
    agent = MagicMock()
    agent._cached_system_prompt = None
    agent.session_id = session_id
    agent.model, agent.provider, agent.platform = "test-model", "openrouter", "api_server"
    agent._session_db = db
    agent._use_prompt_caching = False
    agent._persist_disabled = False
    agent._build_system_prompt = MagicMock(return_value=built)
    agent.enabled_toolsets = agent.disabled_toolsets = None
    agent._platform_hint_overrides = None
    agent._surface_switch_note = agent._gateway_turn_context_notes = ""
    agent._session_init_model_config = {"max_iterations": 90}
    set_prompt_version(agent, version)
    _restore_or_build_system_prompt(agent, None, HISTORY)
    return agent


def test_changed_version_rebuilds_once_and_persists(db):
    _seed(db, version="v1")
    first = _turn(db, "v2")
    assert first._build_system_prompt.call_count == 1
    row = db.get_session("s1")
    assert row["system_prompt"] == first._cached_system_prompt == first._build_system_prompt.return_value
    assert stored_prompt_version(row) == "v2"

    again = _turn(db, "v2", built="MUST NOT BE BUILT")
    again._build_system_prompt.assert_not_called()
    assert again._cached_system_prompt == first._cached_system_prompt


def test_same_version_keeps_stored_prompt_byte_identical(db):
    _seed(db, version="v1")
    agent = _turn(db, "v1")
    agent._build_system_prompt.assert_not_called()
    assert agent._cached_system_prompt == STORED
    assert db.get_session("s1")["system_prompt"] == STORED


@pytest.mark.parametrize("stored_version", [None, "v1"])
def test_absent_version_keeps_old_behaviour(db, stored_version):
    _seed(db, version=stored_version)
    agent = _turn(db, None)
    agent._build_system_prompt.assert_not_called()
    assert agent._cached_system_prompt == STORED
    assert stored_prompt_version(db.get_session("s1")) == stored_version


def test_first_declared_version_on_unversioned_session_rebuilds(db):
    _seed(db)
    agent = _turn(db, "v1")
    agent._build_system_prompt.assert_called_once()
    assert stored_prompt_version(db.get_session("s1")) == "v1"


def test_compression_child_inherits_version_and_keeps_its_prompt(db):
    _seed(db, version="v1")
    agent = _turn(db, "v1")
    db.append_message("s1", "user", "original")
    assert db.try_acquire_compression_lock("s1", "holder", ttl_seconds=60)
    db.publish_compression_child(
        parent_session_id="s1", child_session_id="child", source="api_server",
        model_config=agent._session_init_model_config, system_prompt="CHILD PROMPT\n\nModel: test-model",
        messages=[{"role": "user", "content": "summary"}], compression_lock_holder="holder")
    assert stored_prompt_version(db.get_session("child")) == "v1"

    child = _turn(db, "v1", session_id="child")
    child._build_system_prompt.assert_not_called()
    assert child._cached_system_prompt == "CHILD PROMPT\n\nModel: test-model"


@pytest.mark.parametrize("value, expected", [(None, None), ("", None), ("  v3 ", "v3")])
def test_normalize_accepts_optional_string(value, expected):
    assert normalize_prompt_version(value) == expected


@pytest.mark.parametrize("value", [3, ["v1"], {"v": 1}, "x" * 201])
def test_normalize_rejects_non_string_or_oversized(value):
    with pytest.raises(ValueError):
        normalize_prompt_version(value)

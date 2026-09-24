"""iollo_notes wired through a real AIAgent (fork brief 003 acceptance, minus the model): with
``memory.provider: iollo_notes`` and both built-in memory flags off, the system prompt carries
USER.md and no other note, and the agent's tool surface is the provider's three tools."""

import json

import pytest
import yaml

from .iollo_notes_fakes import write_fixture


@pytest.fixture
def agent(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("IOLLO_EMBED_MODEL_DIR", str(tmp_path / "no-model"))  # FTS only, no model load
    write_fixture(home / "memory-notes")
    (home / "config.yaml").write_text(yaml.safe_dump({"memory": {
        "provider": "iollo_notes", "memory_enabled": False, "user_profile_enabled": False,
        "iollo_notes": {"device": "box"},
    }}), encoding="utf-8")
    from model_tools import _clear_tool_defs_cache
    from tools.registry import invalidate_check_fn_cache

    invalidate_check_fn_cache()
    _clear_tool_defs_cache()
    from run_agent import AIAgent

    built = AIAgent(api_key="test-key", base_url="https://openrouter.ai/api/v1", quiet_mode=True,
                    skip_context_files=True, session_id="sess-accept")
    yield built
    if built._memory_manager:
        built._memory_manager.shutdown_all()
    invalidate_check_fn_cache()
    _clear_tool_defs_cache()


def test_prompt_has_the_profile_and_no_note(agent):
    prompt = agent._build_system_prompt()
    assert "Ricardo lives in Lisbon." in prompt
    assert "dentist" not in prompt and "espresso" not in prompt


def test_tools_are_the_providers_and_answer_from_the_note(agent):
    names = {t["function"]["name"] for t in agent.tools}
    assert {"memory_search", "memory_read", "memory_note"} <= names
    assert "memory" not in names
    found = json.loads(agent._memory_manager.handle_tool_call("memory_search", {"query": "what did I say about Ana?"}))
    assert found["results"][0]["id"] == "people/ana-costa"
    noted = json.loads(agent._memory_manager.handle_tool_call(
        "memory_note", {"fact": "prefers morning appointments", "about": "Ana"}))
    assert noted["id"] == "people/ana-costa"

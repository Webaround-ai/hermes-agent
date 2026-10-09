"""Iollo fork: per-turn notes in a run's ``instructions`` leave the system prompt (agent/turn_notes.py).

Two turns of one session with different notes must send the same system text (so the provider prefix cache and
the Responses ``prompt_cache_key`` stay the same) and each turn's notes must reach that turn's user message."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent.turn_context import build_api_messages
from agent.turn_notes import TURN_NOTES_HEADER, split_turn_notes
from tests.agent.test_gateway_turn_sidecar import _FakeAgent, _build

RULES = "Rules for every surface: be brief.\n\nWhatsApp rules: one message."


def _instructions(*notes):
    return "\n\n".join([RULES, "\n\n".join([TURN_NOTES_HEADER, *notes])]) if notes else RULES


@pytest.fixture(autouse=True)
def _stub_runtime_main():
    with patch("agent.auxiliary_client.set_runtime_main", lambda *a, **k: None):
        yield


class TestSplit:
    def test_notes_block_is_split_off(self):
        system, notes = split_turn_notes(_instructions("Route: answer directly.", "First reply."))
        assert system == RULES
        assert notes == f"{TURN_NOTES_HEADER}\n\nRoute: answer directly.\n\nFirst reply."

    def test_no_notes_is_unchanged(self):
        assert split_turn_notes(RULES) == (RULES, "")
        assert split_turn_notes(None) == (None, "")

    def test_notes_only(self):
        assert split_turn_notes(f"{TURN_NOTES_HEADER}\n\nRoute.") == (None, f"{TURN_NOTES_HEADER}\n\nRoute.")

    def test_header_inside_a_rule_line_is_not_a_split(self):
        text = f"{RULES} {TURN_NOTES_HEADER} is how notes start."
        assert split_turn_notes(text) == (text, "")


class _SendAgent(_FakeAgent):
    _compression_warning = None

    @staticmethod
    def _copy_reasoning_content_for_api(_source, _target):
        return None

    @staticmethod
    def _should_sanitize_tool_calls():
        return False


def _turn(agent, instructions, user_message, history):
    """What /v1/runs does with ``instructions`` (gateway/platforms/api_server_runs.py), then one request."""
    system_part, notes = split_turn_notes(instructions)
    agent.ephemeral_system_prompt = system_part
    if notes:
        agent._gateway_turn_context_notes = notes
    ctx = _build(agent, user_message=user_message, conversation_history=history)
    request, effective_system = build_api_messages(
        agent, ctx.messages, current_turn_user_idx=ctx.current_turn_user_idx,
        ext_prefetch_cache=ctx.ext_prefetch_cache, plugin_user_context=ctx.plugin_user_context,
        moa_config=None, active_system_prompt=ctx.active_system_prompt)
    return request, effective_system, ctx


def test_two_turns_render_the_same_system_prompt_and_notes_ride_each_message():
    agent = _SendAgent()
    first, system_1, ctx = _turn(agent, _instructions("Route: answer directly.", "This is your first reply."),
                                 "what is the capital of portugal?", None)
    history = [*ctx.messages, {"role": "assistant", "content": "Lisbon."}]
    second, system_2, _ = _turn(agent, _instructions("Route: plan with a card."), "and of spain?", history)

    assert system_1 == system_2 == f"SYSTEM\n\n{RULES}"
    assert first[0] == second[0] == {"role": "system", "content": system_1}
    assert "Route: answer directly." in first[-1]["content"] and "first reply" in first[-1]["content"]
    assert "Route: plan with a card." in second[-1]["content"]
    # The first turn's message replays the bytes it was sent with: the prefix before turn 2 is unchanged.
    assert second[1] == first[1]
    assert "Route: plan" not in second[1]["content"]

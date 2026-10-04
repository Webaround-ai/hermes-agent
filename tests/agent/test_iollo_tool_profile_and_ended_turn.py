"""Iollo fork: per-run tool profiles (``agent/tool_profile.py``) and turn-ending tools (``ends_turn``,
``agent/turn_tool_round.py``), driven through the real ``run_conversation`` loop with a fake client."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.tool_dispatch_helpers import make_tool_result_message
from agent.tool_profile import (
    ToolProfile, lift_tool_profile_for_calls, normalize_ends_turn, normalize_tool_profile, project_system_message,
    project_tools, set_ends_turn,
    set_tool_profile, strip_skills_block, tool_profile_report,
)
from agent.turn_tool_round import transcript_tail_ended_by_tool
from run_agent import AIAgent
from tools.registry import registry

END, END_IF, PLAIN, ESCAPE, OTHER = "t_end", "t_end_if", "t_plain", "t_escape", "t_other"

SKILLS = ("## Skills\nBefore replying, scan the skills below. If a skill matches...\n\n<available_skills>\n"
          "  misc:\n    - a: b\n</available_skills>\n\n"
          "Only proceed without loading a skill if genuinely none are relevant to the task.")
SYSTEM = "IDENTITY\n\nCONTEXT\n\n" + SKILLS + "\n\nMEMORY\n\nPLUGIN SECTIONS"


@pytest.fixture(autouse=True)
def _tools(monkeypatch):
    monkeypatch.setattr("agent.title_generator.maybe_auto_title", lambda *args, **kwargs: None)
    schema = {"description": "d", "parameters": {"type": "object", "properties": {}}}
    registry.register(name=END, toolset="iollo_test", schema={**schema, "name": END}, handler=lambda a, **k: "{}",
                      ends_turn=True)
    registry.register(name=END_IF, toolset="iollo_test", schema={**schema, "name": END_IF},
                      handler=lambda a, **k: "{}", ends_turn=lambda args, result: args.get("container") != "home")
    registry.register(name=PLAIN, toolset="iollo_test", schema={**schema, "name": PLAIN}, handler=lambda a, **k: "{}")
    registry.register(name=OTHER, toolset="iollo_test", schema={**schema, "name": OTHER}, handler=lambda a, **k: "{}")
    registry.register(name=ESCAPE, toolset="iollo_test", schema={**schema, "name": ESCAPE},
                      handler=lambda a, **k: "{}", lifts_tool_profile=True)
    yield
    for name in (END, END_IF, PLAIN, OTHER, ESCAPE):
        registry.deregister(name)


def _defs(*names):
    return [{"type": "function", "function": {"name": n, "description": n,
                                              "parameters": {"type": "object", "properties": {}}}} for n in names]


def _agent(*names):
    home = Path(tempfile.mkdtemp(prefix="hermes-test-home-"))
    (home / "logs").mkdir(parents=True, exist_ok=True)
    with (
        patch("model_tools.get_tool_definitions", return_value=_defs(*names)),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
        patch("run_agent._hermes_home", home),
        patch("agent.model_metadata.fetch_model_metadata", return_value={}),
    ):
        agent = AIAgent(api_key="test-key", base_url="https://openrouter.ai/api/v1", quiet_mode=True,
                        skip_context_files=True, skip_memory=True)
    agent.client = MagicMock()
    agent._cached_system_prompt = SYSTEM
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    set_ends_turn(agent, True)      # the ends_turn tests' run asked for it; see test_ends_turn_is_opt_in_per_run
    return agent


def _call(name, call_id, arguments="{}"):
    return SimpleNamespace(id=call_id, type="function", function=SimpleNamespace(name=name, arguments=arguments))


def _response(content="", tool_calls=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    reason = "tool_calls" if tool_calls else "stop"
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=reason)], model="m", usage=None)


def _run(agent, responses, results):
    """Run one turn; ``results`` maps tool name -> result string."""
    agent.client.chat.completions.create.side_effect = responses

    def _execute(assistant_message, messages, effective_task_id, api_call_count=0):
        for tc in assistant_message.tool_calls:
            messages.append(make_tool_result_message(tc.function.name, results[tc.function.name], tc.id))

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch.object(agent, "_execute_tool_calls", side_effect=_execute),
        patch("agent.conversation_loop._restore_or_build_system_prompt"),
    ):
        return agent.run_conversation("hi", conversation_history=[{"role": "user", "content": "x"},
                                                                    {"role": "assistant", "content": "y"}])


def _sent(agent, n):
    kwargs = agent.client.chat.completions.create.call_args_list[n].kwargs
    names = [t["function"]["name"] for t in kwargs.get("tools") or []]
    system = next((m["content"] for m in kwargs["messages"] if m["role"] == "system"), "")
    return names, system


OK = json.dumps({"success": True})
BAD = json.dumps({"success": False, "error": "blocks[0]: unknown field"})


# --- ends_turn -----------------------------------------------------------------------------------------------


def test_text_with_a_succeeded_turn_ending_call_ends_the_turn_without_another_call():
    agent = _agent(END, PLAIN)
    result = _run(agent, [_response("Here are three.", [_call(END, "c1")])], {END: OK})
    assert agent.client.chat.completions.create.call_count == 1
    assert result["final_response"] == "Here are three."
    assert result["turn_exit_reason"] == "tool_ended_turn"
    assert result["completed"] is True
    # Every call keeps its tool result; the text is not repeated in a closing row.
    roles = [m["role"] for m in result["messages"]][-2:]
    assert roles == ["assistant", "tool"]
    assert sum(1 for m in result["messages"] if m.get("content") == "Here are three.") == 1
    assert transcript_tail_ended_by_tool(result["messages"]) is True


@pytest.mark.parametrize("content, calls, results", [
    ("Here.", [_call(END, "c1")], {END: BAD}),                                   # refused: the model must fix it
    ("", [_call(END, "c1")], {END: OK}),                                         # no visible text
    ("Here.", [_call(END, "c1"), _call(PLAIN, "c2")], {END: OK, PLAIN: OK}),     # a non-ending call in the round
    ("Here.", [_call(END_IF, "c1", '{"container": "home"}')], {END_IF: OK}),     # the predicate says no
])
def test_anything_else_continues_with_one_more_call(content, calls, results):
    agent = _agent(END, END_IF, PLAIN)
    result = _run(agent, [_response(content, calls), _response("Final.")], results)
    assert agent.client.chat.completions.create.call_count == 2
    assert result["final_response"] == "Final."
    assert result["turn_exit_reason"] != "tool_ended_turn"
    assert transcript_tail_ended_by_tool(result["messages"]) is False


def test_a_predicate_that_says_yes_ends_the_turn():
    agent = _agent(END_IF)
    result = _run(agent, [_response("Cards below.", [_call(END_IF, "c1", '{"container": "reply"}')])], {END_IF: OK})
    assert agent.client.chat.completions.create.call_count == 1
    assert result["final_response"] == "Cards below."


def test_a_short_reply_is_not_treated_as_a_fragment():
    agent = _agent(END)
    result = _run(agent, [_response("Done", [_call(END, "c1")])], {END: OK})
    assert result["final_response"] == "Done"


# --- tool profiles -------------------------------------------------------------------------------------------


def test_a_light_profile_projects_tools_and_drops_the_skills_index_only():
    agent = _agent(PLAIN, OTHER, END, ESCAPE)
    set_tool_profile(agent, ToolProfile("light", frozenset({PLAIN, END}), skills=False))
    result = _run(agent, [_response("Answer.")], {})
    names, system = _sent(agent, 0)
    assert names == [PLAIN, END, ESCAPE]
    assert system == "IDENTITY\n\nCONTEXT\n\nMEMORY\n\nPLUGIN SECTIONS"
    # The agent itself stays the full set (stored prompt, pins and compression see every tool).
    assert set(agent.valid_tool_names) >= {PLAIN, OTHER, END, ESCAPE}
    assert agent._cached_system_prompt == SYSTEM
    assert tool_profile_report(agent, 1) == {"name": "light", "lifted": "", "api_calls": 1}
    assert result["final_response"] == "Answer."


def test_the_profile_note_rides_on_the_escape_tool_only():
    agent = _agent(PLAIN, ESCAPE)
    set_tool_profile(agent, normalize_tool_profile({"name": "light", "tools": [PLAIN], "skills": False,
                                                    "note": "Only with every tool: Notion, the browser."}))
    _run(agent, [_response("Answer.")], {})
    tools = agent.client.chat.completions.create.call_args_list[0].kwargs["tools"]
    assert tools[1]["function"]["description"] == ESCAPE + " Only with every tool: Notion, the browser."
    assert tools[0]["function"]["description"] == PLAIN
    assert all("Notion" not in t["function"]["description"] for t in agent.tools)


def test_without_a_profile_the_escape_tool_is_never_sent():
    agent = _agent(PLAIN, ESCAPE)
    set_tool_profile(agent, None)
    _run(agent, [_response("Answer.")], {})
    names, system = _sent(agent, 0)
    assert names == [PLAIN]
    assert system == SYSTEM
    assert tool_profile_report(agent) is None


@pytest.mark.parametrize("called, reason", [(ESCAPE, "escape"), (OTHER, "tool:" + OTHER)])
def test_a_call_outside_the_profile_lifts_it_for_the_rest_of_the_turn(called, reason):
    agent = _agent(PLAIN, OTHER, ESCAPE)
    set_tool_profile(agent, ToolProfile("light", frozenset({PLAIN}), skills=False))
    result = _run(agent, [_response("", [_call(called, "c1")]), _response("Done with all tools.")],
                  {called: OK})
    first, _ = _sent(agent, 0)
    second, system = _sent(agent, 1)
    assert first == [PLAIN, ESCAPE]
    assert second == [PLAIN, OTHER]
    assert system == SYSTEM
    assert tool_profile_report(agent)["lifted"] == reason
    assert result["final_response"] == "Done with all tools."


# --- helpers -------------------------------------------------------------------------------------------------


def test_strip_skills_block_keeps_every_other_byte():
    assert strip_skills_block(SYSTEM) == "IDENTITY\n\nCONTEXT\n\nMEMORY\n\nPLUGIN SECTIONS"
    assert strip_skills_block("no skills here") == "no skills here"
    hidden = SKILLS + ("\n(Categories marked [names only] are outside the current coding context, so their "
                       "descriptions are omitted — the skills work normally and load with skill_view(name) as usual.)")
    assert strip_skills_block("A\n\n" + hidden + "\n\nB") == "A\n\nB"
    # An unterminated block is left alone rather than cutting the rest of the prompt.
    assert strip_skills_block("A\n\n## Skills\nBefore replying, scan the skills below. ...") == (
        "A\n\n## Skills\nBefore replying, scan the skills below. ...")


def test_project_system_message_never_mutates_the_input():
    agent = SimpleNamespace(_tool_profile=ToolProfile("light", frozenset({"a"}), skills=False))
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "hi"}]
    projected = project_system_message(agent, messages)
    assert messages[0]["content"] == SYSTEM
    assert projected[0]["content"].startswith("IDENTITY") and "## Skills" not in projected[0]["content"]
    blocks = [{"role": "system", "content": [{"type": "text", "text": SYSTEM}]}]
    assert "## Skills" not in project_system_message(agent, blocks)[0]["content"][0]["text"]
    keep = SimpleNamespace(_tool_profile=ToolProfile("light", frozenset({"a"}), skills=True))
    assert project_system_message(keep, messages) is messages


def test_project_tools_returns_the_same_list_when_nothing_is_removed():
    agent = SimpleNamespace(_tool_profile=None)
    tools = _defs(PLAIN, OTHER)
    assert project_tools(agent, tools) is tools


def test_lift_ignores_unknown_names_and_profile_tools():
    agent = SimpleNamespace(_tool_profile=ToolProfile("light", frozenset({PLAIN})), valid_tool_names={PLAIN, OTHER})
    assert lift_tool_profile_for_calls(agent, [_call(PLAIN, "c"), _call("nope", "d")]) is False
    assert agent._tool_profile is not None


@pytest.mark.parametrize("value, expected", [
    (None, None),
    ({"name": "light", "tools": ["web_search"]}, ToolProfile("light", frozenset({"web_search"}), True)),
    ({"name": "full"}, ToolProfile("full", None, True)),
])
def test_normalize_tool_profile(value, expected):
    assert normalize_tool_profile(value) == expected


def test_a_named_profile_without_tools_projects_nothing_but_reports():
    agent = _agent(PLAIN, OTHER, ESCAPE)
    set_tool_profile(agent, ToolProfile("full", None))
    _run(agent, [_response("Answer.")], {})
    names, system = _sent(agent, 0)
    assert names == [PLAIN, OTHER]
    assert system == SYSTEM
    assert tool_profile_report(agent, 1) == {"name": "full", "lifted": "", "api_calls": 1}


def test_ends_turn_is_opt_in_per_run():
    """Off unless the run asks (/v1/runs ``ends_turn: true``): flows write task records after the reply's cards."""
    agent = _agent(END)
    set_ends_turn(agent, False)
    result = _run(agent, [_response("Here.", [_call(END, "c1")]), _response("Final.")], {END: OK})
    assert agent.client.chat.completions.create.call_count == 2
    assert result["final_response"] == "Final."
    assert normalize_ends_turn(None) is False and normalize_ends_turn(True) is True
    with pytest.raises(ValueError):
        normalize_ends_turn("yes")


def test_a_stop_during_the_round_is_not_turned_into_a_completed_reply():
    agent = _agent(END)
    agent.client.chat.completions.create.side_effect = [_response("Here.", [_call(END, "c1")]), _response("x")]

    def _execute(assistant_message, messages, effective_task_id, api_call_count=0):
        messages.append(make_tool_result_message(END, OK, "c1"))
        agent._interrupt_requested = True

    with (
        patch.object(agent, "_persist_session"), patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch.object(agent, "_execute_tool_calls", side_effect=_execute),
        patch("agent.conversation_loop._restore_or_build_system_prompt"),
    ):
        result = agent.run_conversation("hi")
    assert result["turn_exit_reason"] != "tool_ended_turn"
    assert result["interrupted"] is True


def test_a_tool_ended_turn_on_the_last_allowed_call_is_completed():
    agent = _agent(END)
    agent.max_iterations = 1
    result = _run(agent, [_response("Here are three.", [_call(END, "c1")])], {END: OK})
    assert result["turn_exit_reason"] == "tool_ended_turn"
    assert result["completed"] is True and result["final_response"] == "Here are three."


def test_ascii_recovery_never_rewrites_the_canonical_tool_schemas():
    from agent.message_sanitization import sanitize_outbound_kwargs
    agent = _agent(PLAIN, ESCAPE)
    agent.tools[0]["function"]["description"] = "caf\u00e9"
    canonical = agent.tools[0]["function"]["description"]
    set_tool_profile(agent, None)
    projected = project_tools(agent, agent.tools)
    assert projected is not agent.tools and projected[0] is agent.tools[0]
    agent._force_ascii_payload = True
    kwargs = {"tools": projected, "messages": []}
    sanitize_outbound_kwargs(agent, kwargs)
    assert agent.tools[0]["function"]["description"] == canonical

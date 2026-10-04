"""Iollo fork: a tool profile's provider-executed ``hosted_tools`` (OpenAI's built-in ``web_search``), appended on
the native Responses route only, never persisted, counted for the run report (``agent/tool_profile.py``)."""

from types import SimpleNamespace

import pytest

import run_agent
from agent.tool_profile import (
    ToolProfile, hosted_tools_for_request, normalize_tool_profile, note_hosted_calls, set_tool_profile,
    tool_profile_report, without_shadowed_functions,
)
from agent.turn_api_request import build_api_request

LOC = {"type": "approximate", "country": "PT", "city": "Lisbon", "timezone": "Europe/Lisbon"}
SEARCH = {"type": "web_search", "search_context_size": "low", "user_location": LOC}
LIGHT = {"name": "light", "tools": ["web_search", "search_answer", "show_cards"], "skills": False,
         "hosted_tools": [SEARCH]}


def _defs(*names):
    return [{"type": "function", "function": {"name": n, "description": n,
                                              "parameters": {"type": "object", "properties": {}}}} for n in names]


def _agent(monkeypatch, *, base_url="https://webaround-router.fly.dev/v1", api_mode="codex_responses",
           model="openai/gpt-6-luna"):
    monkeypatch.setattr("model_tools.get_tool_definitions",
                        lambda **kw: _defs("web_search", "search_answer", "show_cards", "terminal"))
    monkeypatch.setattr("model_tools.check_toolset_requirements", lambda: {})
    agent = run_agent.AIAgent(model=model, base_url=base_url, api_key="k", api_mode=api_mode, quiet_mode=True,
                              max_iterations=4, skip_context_files=True, skip_memory=True)
    agent._use_prompt_caching = False
    agent._empty_content_retries = 0          # per-turn state run_conversation sets
    return agent


def _request_tools(agent):
    build = build_api_request(
        agent, api_messages=[{"role": "system", "content": "S"}, {"role": "user", "content": "hi"}],
        _moa_prepared_request=None, tools_for_api=None, system_message="S", messages=[], original_user_message="hi",
        approx_tokens=1, total_chars=1, retry_count=0, api_call_count=1, api_request_id="r", api_start_time=0.0,
        effective_task_id="t", turn_id="u")
    return build.api_kwargs.get("tools") or []


def test_light_profile_on_the_native_route_swaps_the_client_web_search_for_the_built_in(monkeypatch):
    agent = _agent(monkeypatch)
    set_tool_profile(agent, normalize_tool_profile(LIGHT))
    tools = _request_tools(agent)
    assert [t.get("name") for t in tools if t.get("type") == "function"] == ["search_answer", "show_cards"]
    assert tools[-1] == SEARCH
    # the agent's own tools (the session pin) are untouched
    assert [t["function"]["name"] for t in agent.tools] == ["web_search", "search_answer", "show_cards", "terminal"]


def test_without_hosted_tools_the_request_is_unchanged(monkeypatch):
    agent = _agent(monkeypatch)
    set_tool_profile(agent, normalize_tool_profile({k: v for k, v in LIGHT.items() if k != "hosted_tools"}))
    tools = _request_tools(agent)
    assert [t.get("name") for t in tools] == ["web_search", "search_answer", "show_cards"]


def test_a_lifted_profile_drops_the_hosted_tools(monkeypatch):
    agent = _agent(monkeypatch)
    set_tool_profile(agent, normalize_tool_profile(LIGHT))
    agent._tool_profile = None                       # what lift_tool_profile_for_calls does
    tools = _request_tools(agent)
    assert all(t.get("type") == "function" for t in tools)
    assert "web_search" in [t.get("name") for t in tools]


@pytest.mark.parametrize("kwargs", [
    {"api_mode": "chat_completions", "model": "webaround/claude-sonnet-5"},   # a Claude fallback / worker
    {"base_url": "https://chatgpt.com/backend-api/codex", "model": "gpt-5-codex"},
    {"base_url": "https://api.x.ai/v1", "model": "grok-4"},
])
def test_never_on_other_routes(monkeypatch, kwargs):
    agent = _agent(monkeypatch, **kwargs)
    set_tool_profile(agent, normalize_tool_profile(LIGHT))
    assert hosted_tools_for_request(agent) == []


def test_hosted_tools_are_fresh_copies(monkeypatch):
    agent = _agent(monkeypatch)
    set_tool_profile(agent, normalize_tool_profile(LIGHT))
    first = hosted_tools_for_request(agent)
    first[0]["user_location"]["city"] = "X"
    assert hosted_tools_for_request(agent) == [SEARCH]


@pytest.mark.parametrize("hosted", [
    [{"type": "file_search"}],
    [{"type": "web_search", "extra": 1}],
    [{"type": "web_search", "search_context_size": "huge"}],
    [{"type": "web_search", "user_location": {"type": "approximate", "country": "Portugal"}}],
    [{"type": "web_search", "user_location": {"type": "approximate", "latitude": 38.7}}],
    [{"type": "web_search", "filters": {"allowed_domains": []}}],
    [SEARCH, SEARCH, SEARCH],
    {"type": "web_search"},
])
def test_invalid_hosted_tools_are_refused(hosted):
    with pytest.raises(ValueError):
        normalize_tool_profile({**LIGHT, "hosted_tools": hosted})


def test_hosted_tools_need_a_tool_list():
    with pytest.raises(ValueError):
        normalize_tool_profile({"name": "full", "hosted_tools": [SEARCH]})


def test_normalize_rebuilds_known_fields_only():
    profile = normalize_tool_profile({**LIGHT, "hosted_tools": [
        {"type": "web_search", "filters": {"allowed_domains": ["ipma.pt", "ipma.pt"]}}]})
    assert profile.hosted_tools == ({"type": "web_search", "filters": {"allowed_domains": ["ipma.pt"]}},)
    assert normalize_tool_profile({**LIGHT, "hosted_tools": []}).hosted_tools == ()


def test_shadowed_functions_only_drop_same_name_functions():
    tools = [{"type": "function", "name": "web_search"}, {"type": "function", "name": "web_extract"}]
    assert without_shadowed_functions(tools, [SEARCH]) == [tools[1]]


def test_the_report_counts_provider_searches_across_requests():
    agent = SimpleNamespace()
    set_tool_profile(agent, normalize_tool_profile(LIGHT))
    note_hosted_calls(agent, SimpleNamespace(output=[SimpleNamespace(type="web_search_call"),
                                                     SimpleNamespace(type="message")]))
    note_hosted_calls(agent, {"output": [{"type": "web_search_call"}, {"type": "web_search_call"}]})
    report = tool_profile_report(agent, 2)
    assert report["hosted_calls"] == {"web_search": 3}


def test_a_report_without_hosted_tools_keeps_its_shape():
    agent = SimpleNamespace()
    set_tool_profile(agent, ToolProfile("light", frozenset({"web_search"}), False))
    note_hosted_calls(agent, {"output": [{"type": "web_search_call"}]})
    assert tool_profile_report(agent, 1) == {"name": "light", "lifted": "", "api_calls": 1}

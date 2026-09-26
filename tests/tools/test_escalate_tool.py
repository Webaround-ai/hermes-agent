"""escalate: advice-only child on the configured tier (tools/escalate_tool.py).

Contracts: not advertised unless delegation.escalate names a configured tier; caps per task and per day
are enforced in code before (and regardless of) the escalate_gate hook; a gate refusal's reason reaches
the agent and costs nothing; the advisor child is read-only (no terminal/file/delegation/sends) on the
tier's model and effort; children can never escalate.
"""

import json
import threading
from unittest.mock import MagicMock, patch

import pytest

import tools.delegate_tool as dt
import tools.delegate_tool_config as dtc
import tools.escalate_tool as et
from hermes_cli import plugins
from hermes_cli.plugins import PluginManager

CFG = {
    "tiers": {"opus_high": {"model": "anthropic/claude-opus-5.5", "reasoning_effort": "high"},
              "haiku_low": {"model": "anthropic/claude-haiku-4.5", "reasoning_effort": "low"}},
    "escalate": {"tier": "opus_high", "max_per_task": 2, "max_per_day": 3},
}
ARGS = dict(question="Which migration order is safe?", context="Tried A then B: 'FK violation on users.id'",
            constraints="no downtime", wanted="the order to run the migrations in")


@pytest.fixture(autouse=True)
def _reset():
    et._reset_counters_for_tests()
    yield
    et._reset_counters_for_tests()


def _use_cfg(monkeypatch, cfg):
    monkeypatch.setattr(dtc, "_load_config", lambda: cfg)
    monkeypatch.setattr(dt, "_load_config", lambda: cfg)


def _install_gate(monkeypatch, *callbacks):
    mgr = PluginManager()
    mgr._discovered = True
    mgr._hooks["escalate_gate"] = list(callbacks)
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: mgr)


def _parent(turn="turn-1", depth=0):
    parent = MagicMock()
    parent.base_url = "https://openrouter.ai/api/v1"
    parent.api_key = "test-parent-key"
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.model = "anthropic/claude-sonnet-5"
    parent.reasoning_config = {"enabled": True, "effort": "low"}
    parent.request_overrides = None
    parent.providers_allowed = parent.providers_ignored = parent.providers_order = parent.provider_sort = None
    parent.session_id = "sess-1"
    parent._current_turn_id = turn
    parent._session_db = None
    parent._delegate_depth = depth
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    parent.enabled_toolsets = ["terminal", "file", "web", "vision", "delegation", "messaging", "session_search",
                               "memory", "escalate"]
    parent.disabled_toolsets = []
    return parent


def _fake_advisor(monkeypatch):
    calls = []

    def run(parent_agent, goal, brief, route, base_creds, esc):
        calls.append({"goal": goal, "brief": brief, "model": route["model"]})
        return {"results": [{"status": "completed", "summary": "Run B first, then A.", "cost_usd": 0.01}]}

    monkeypatch.setattr(et, "_run_advisor", run)
    return calls


def test_not_advertised_unless_configured_with_a_known_tier(monkeypatch):
    for cfg, expected in (({}, False), ({**CFG, "escalate": {"tier": "missing"}}, False),
                          ({**CFG, "escalate": "yes"}, False), (CFG, True)):
        _use_cfg(monkeypatch, cfg)
        assert et.check_escalate_requirements() is expected


def test_caps_per_task_and_per_day_are_enforced_in_code(monkeypatch):
    _use_cfg(monkeypatch, CFG)
    calls = _fake_advisor(monkeypatch)
    turn1 = _parent("turn-1")
    first, second, third = (json.loads(et.escalate(**ARGS, parent_agent=turn1)) for _ in range(3))
    assert first["escalated"] and second["escalated"] and first["advice"] == "Run B first, then A."
    assert third["escalated"] is False and "this task" in third["reason"]
    turn2 = _parent("turn-2")
    assert json.loads(et.escalate(**ARGS, parent_agent=turn2))["escalated"] is True
    refused = json.loads(et.escalate(**ARGS, parent_agent=_parent("turn-3")))
    assert refused["escalated"] is False and "Daily" in refused["reason"]
    assert len(calls) == 3


def test_gate_refusal_reason_reaches_the_agent_and_costs_nothing(monkeypatch):
    _use_cfg(monkeypatch, CFG)
    calls = _fake_advisor(monkeypatch)
    seen = {}

    def gate(**kw):
        seen.update(kw)
        return {"action": "continue", "reason": "the FK error names the table; reorder and retry."}

    _install_gate(monkeypatch, gate)
    out = json.loads(et.escalate(**ARGS, parent_agent=_parent()))
    assert out["escalated"] is False
    assert "the FK error names the table; reorder and retry." in out["reason"]
    assert calls == []
    assert seen["question"] == ARGS["question"] and seen["tier"] == "opus_high"
    assert (seen["max_per_task"], seen["max_per_day"]) == (2, 3)
    assert et.usage(_parent()) == (0, 0)  # refused escalations are not counted


def test_gate_cannot_allow_past_a_cap(monkeypatch):
    _use_cfg(monkeypatch, {**CFG, "escalate": {"tier": "opus_high", "max_per_task": 0, "max_per_day": 10}})
    calls = _fake_advisor(monkeypatch)
    asked = []
    _install_gate(monkeypatch, lambda **_: asked.append(1) or "escalate")
    out = json.loads(et.escalate(**ARGS, parent_agent=_parent()))
    assert out["escalated"] is False and calls == [] and asked == []


def test_broken_gate_means_the_default_escalate(monkeypatch):
    _use_cfg(monkeypatch, CFG)
    calls = _fake_advisor(monkeypatch)

    def boom(**_):
        raise RuntimeError("gate down")

    _install_gate(monkeypatch, boom)
    assert json.loads(et.escalate(**ARGS, parent_agent=_parent()))["escalated"] is True
    assert len(calls) == 1


def test_children_can_never_escalate(monkeypatch):
    from tools.delegate_tool_toolsets import _blocked_toolsets_for_role
    _use_cfg(monkeypatch, CFG)
    assert "escalate" in _blocked_toolsets_for_role("leaf")
    assert "escalate" in _blocked_toolsets_for_role("orchestrator")
    out = json.loads(et.escalate(**ARGS, parent_agent=_parent(depth=1)))
    assert "main agent" in out["error"]


def test_advisor_runs_read_only_on_the_tier_model_and_effort(monkeypatch):
    # Orchestration on: an ordinary child would get `delegation` back; the advisor must not.
    _use_cfg(monkeypatch, {**CFG, "orchestrator_enabled": True, "max_spawn_depth": 3})
    monkeypatch.setattr(dt, "_get_orchestrator_enabled", lambda: True)
    monkeypatch.setattr(dt, "_get_max_spawn_depth", lambda: 3)
    with patch("run_agent.AIAgent") as MockAgent:
        child = MagicMock()
        child.run_conversation.return_value = {"final_response": "Run B first, then A.", "completed": True,
                                               "api_calls": 2}
        MockAgent.return_value = child
        out = json.loads(et.escalate(**ARGS, parent_agent=_parent()))
    kwargs = MockAgent.call_args.kwargs
    assert out["escalated"] is True and out["advice"] == "Run B first, then A."
    assert (out["tier"], out["model"], out["reasoning_effort"]) == ("opus_high", "anthropic/claude-opus-5.5", "high")
    assert kwargs["model"] == "anthropic/claude-opus-5.5"
    assert kwargs["reasoning_config"] == {"enabled": True, "effort": "high"}
    assert set(kwargs["enabled_toolsets"]) <= set(et.ESCALATE_READONLY_TOOLSETS)
    assert {"web", "vision", "session_search"} <= set(kwargs["enabled_toolsets"])
    assert "delegation" in kwargs["disabled_toolsets"]
    # The brief carries everything the cold advisor needs.
    prompt = kwargs["ephemeral_system_prompt"]
    for part in ARGS.values():
        assert part in prompt

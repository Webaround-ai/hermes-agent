"""delegation.tiers: per-child model + reasoning effort for delegate_task (tools/delegate_tool_tiers.py).

Contracts: tiers are off unless configured (no schema field, ordinary route); per task the resolution is
explicit model > task tier > delegation_tier_chooser hook > delegation.default_tier; a chooser that raises,
times out or answers off-list falls back to the default; the tier's effort reaches the child (never
"none" for a Claude model); the chosen tier is persisted with the dispatch record and live-log manifest.
"""

import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

import tools.delegate_tool as dt
from hermes_cli import plugins
from hermes_cli.plugins import PluginManager
from tools import async_delegation as ad
from tools import delegate_tool_tiers as tiers_mod
from tools.process_registry import process_registry

TIERS = {
    "haiku_low": {"model": "anthropic/claude-haiku-4.5", "reasoning_effort": "low"},
    "sonnet_high": {"model": "anthropic/claude-sonnet-5", "reasoning_effort": "high"},
    "opus_high": {"model": "anthropic/claude-opus-5.5", "reasoning_effort": "high"},
}


def _cfg(**extra):
    return {"tiers": {k: dict(v) for k, v in TIERS.items()}, "default_tier": "haiku_low", **extra}


def _install_hook(monkeypatch, hook, *callbacks):
    mgr = PluginManager()
    mgr._discovered = True
    mgr._hooks[hook] = list(callbacks)
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: mgr)


def _parent():
    parent = MagicMock()
    parent.base_url = "https://openrouter.ai/api/v1"
    parent.api_key = "test-parent-key"
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.model = "anthropic/claude-sonnet-5"
    parent.reasoning_config = {"enabled": True, "effort": "medium"}
    parent.capabilities = {"vision": True}
    parent.providers_allowed = parent.providers_ignored = parent.providers_order = parent.provider_sort = None
    parent._session_db = None
    parent._delegate_depth = 0
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    return parent


def _run_sync(monkeypatch, cfg, **delegate_kwargs):
    monkeypatch.setattr(dt, "_load_config", lambda: cfg)
    with patch("run_agent.AIAgent") as MockAgent:
        def _new_child(*_a, **_k):
            child = MagicMock()
            child.run_conversation.return_value = {"final_response": "ok", "completed": True, "api_calls": 1}
            return child

        MockAgent.side_effect = _new_child
        out = json.loads(dt.delegate_task(parent_agent=_parent(), **delegate_kwargs))
    return out, [c.kwargs for c in MockAgent.call_args_list]


# ── off unless configured ──────────────────────────────────────────────────

def test_unconfigured_profile_keeps_the_ordinary_route(monkeypatch):
    out, calls = _run_sync(monkeypatch, {}, tasks=[{"goal": "summarise the release notes for me", "tier": "opus_high"}])
    assert calls[0]["model"] == "anthropic/claude-sonnet-5"
    assert calls[0]["reasoning_config"] == {"enabled": True, "effort": "medium"}
    assert "tier" not in out["results"][0]
    assert tiers_mod.build_task_routes([{"goal": "g", "tier": "x"}], {}, {"model": None}) == [None]


def test_tier_field_is_advertised_only_when_tiers_are_configured(monkeypatch):
    def tier_prop(cfg):
        monkeypatch.setattr(dt, "_load_config", lambda: cfg)
        return dt._build_dynamic_schema_overrides()["parameters"]["properties"]["tasks"]["items"]["properties"].get("tier")

    assert tier_prop({}) is None
    prop = tier_prop(_cfg())
    assert prop["enum"] == list(TIERS)
    assert "haiku_low" in prop["description"]
    # The static schema is never mutated by the per-call override.
    assert "tier" not in dt.DELEGATE_TASK_SCHEMA["parameters"]["properties"]["tasks"]["items"]["properties"]


def test_model_supplied_task_model_is_stripped():
    assert dt._strip_model_hidden_task_fields([{"goal": "g", "model": "x/any", "tier": "t"}]) == [{"goal": "g", "tier": "t"}]


# ── resolution order ───────────────────────────────────────────────────────

def test_resolution_order_explicit_model_then_tier_then_chooser_then_default(monkeypatch):
    _install_hook(monkeypatch, "delegation_tier_chooser",
                  lambda goal, **_: "opus_high" if goal == "chooser picks" else None)
    tasks = [
        {"goal": "explicit", "model": "vendor/explicit-model", "tier": "sonnet_high"},
        {"goal": "tiered", "tier": "sonnet_high"},
        {"goal": "chooser picks"},
        {"goal": "nobody picks"},
        {"goal": "unknown tier", "tier": "no_such_tier"},
    ]
    picks = tiers_mod.pick_task_tiers(tasks, _cfg())
    assert picks == [("", "explicit"), ("sonnet_high", "tier"), ("opus_high", "chooser"),
                     ("haiku_low", "default"), ("haiku_low", "default")]


def test_chooser_receives_the_task_and_the_tier_map(monkeypatch):
    seen = []
    _install_hook(monkeypatch, "delegation_tier_chooser", lambda **kw: seen.append(kw) or None)
    tiers_mod.pick_task_tiers([{"goal": "g1", "context": "c1"}], _cfg(), parent_session_id="sess-1")
    assert seen[0]["goal"] == "g1" and seen[0]["context"] == "c1"
    assert set(seen[0]["tiers"]) == set(TIERS) and seen[0]["default_tier"] == "haiku_low"
    assert seen[0]["parent_session_id"] == "sess-1" and seen[0]["task_count"] == 1


@pytest.mark.parametrize("behaviour", ["raises", "off_list", "slow"])
def test_broken_chooser_falls_back_to_the_default_tier(monkeypatch, behaviour):
    def chooser(**_):
        if behaviour == "raises":
            raise RuntimeError("chooser blew up")
        if behaviour == "slow":
            time.sleep(2)
            return "opus_high"
        return "gpt-something"

    _install_hook(monkeypatch, "delegation_tier_chooser", chooser)
    started = time.monotonic()
    picks = tiers_mod.pick_task_tiers([{"goal": "g"}], _cfg(tier_chooser_timeout_ms=100))
    assert picks == [("haiku_low", "default")]
    assert time.monotonic() - started < 1.5


def test_no_default_and_no_choice_leaves_the_ordinary_route():
    cfg = _cfg()
    cfg.pop("default_tier")
    assert tiers_mod.pick_task_tiers([{"goal": "g"}], cfg) == [None]
    assert tiers_mod.build_task_routes([{"goal": "g"}], cfg, {"model": None}) == [None]


# ── per-child model and effort ─────────────────────────────────────────────

def test_tier_sets_the_childs_model_and_reasoning_effort(monkeypatch):
    out, calls = _run_sync(monkeypatch, _cfg(), tasks=[
        {"goal": "check the spreadsheet totals quickly", "tier": "haiku_low"},
        {"goal": "design the migration plan carefully", "tier": "opus_high"},
    ])
    by_model = {c["model"]: c["reasoning_config"] for c in calls}
    assert by_model == {"anthropic/claude-haiku-4.5": {"enabled": True, "effort": "low"},
                        "anthropic/claude-opus-5.5": {"enabled": True, "effort": "high"}}
    tiers_seen = {r["tier"]: r["reasoning_effort"] for r in out["results"]}
    assert tiers_seen == {"haiku_low": "low", "opus_high": "high"}


def test_none_effort_is_never_sent_to_a_claude_model(monkeypatch):
    assert tiers_mod.effective_tier_effort("none", "anthropic/claude-opus-5.5") is None
    assert tiers_mod.effective_tier_effort(False, "claude-haiku-4-5") is None
    assert tiers_mod.effective_tier_effort("none", "z-ai/glm-5.3-flash") == "none"
    cfg = _cfg()
    cfg["tiers"]["opus_none"] = {"model": "anthropic/claude-opus-5.5", "reasoning_effort": "none"}
    _, calls = _run_sync(monkeypatch, cfg, tasks=[{"goal": "think about the migration plan", "tier": "opus_none"}])
    # Omitted, so the ordinary rule applies (here: the parent's level), never {"enabled": False}.
    assert calls[0]["reasoning_config"] == {"enabled": True, "effort": "medium"}


def test_tier_with_provider_gets_its_own_bundle_and_no_inherited_capabilities(monkeypatch):
    seen = {}

    def fake_runtime_creds(values, explicit):
        seen.update(values)
        return {"model": values["model"], "provider": "anthropic", "base_url": "https://api.anthropic.com",
                "api_key": "test-tier-key", "api_mode": "anthropic_messages", "request_overrides": {},
                "command": None, "args": []}

    monkeypatch.setattr("tools.delegate_tool_config._runtime_provider_credentials", fake_runtime_creds)
    cfg = _cfg()
    cfg["tiers"]["direct_opus"] = {"model": "claude-opus-5-5", "provider": "anthropic", "reasoning_effort": "high"}
    _, calls = _run_sync(monkeypatch, cfg, tasks=[
        {"goal": "review the architecture decision", "tier": "direct_opus"},
        {"goal": "tidy the changelog wording please", "tier": "haiku_low"},
    ])
    assert seen["provider"] == "anthropic"
    direct = next(c for c in calls if c["model"] == "claude-opus-5-5")
    same_route = next(c for c in calls if c["model"] == "anthropic/claude-haiku-4.5")
    assert (direct["provider"], direct["api_key"]) == ("anthropic", "test-tier-key")
    assert direct["capabilities"] is None  # off the parent's route: default-deny
    assert same_route["provider"] == "openrouter" and same_route["capabilities"] == {"vision": True}


# ── persisted and shown ────────────────────────────────────────────────────

@pytest.fixture
def _clean_async():
    ad._reset_for_tests()
    yield
    deadline = time.monotonic() + 2.0
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(0.02)
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()


def test_tier_is_persisted_with_the_dispatch_record_and_manifest(monkeypatch, _clean_async):
    monkeypatch.setattr(dt, "_load_config", lambda: _cfg())
    gate = threading.Event()
    built = []

    def build(**kw):
        built.append(kw)
        c = MagicMock()
        c._delegate_role = "leaf"
        c._subagent_id = f"s{kw['task_index']}"
        return c

    def child(task_index, goal, child=None, parent_agent=None, **kw):
        gate.wait(timeout=30)
        return {"task_index": task_index, "status": "completed", "summary": "done", "api_calls": 1,
                "duration_seconds": 0.1, "model": "m", "exit_reason": "completed"}

    creds = {"model": None, "provider": None, "base_url": None, "api_key": None, "api_mode": None,
             "command": None, "args": None}
    monkeypatch.setattr(dt, "_build_child_agent", build)
    monkeypatch.setattr(dt, "_run_single_child", child)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: creds)
    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "sess"
    parent._interrupt_requested = False
    parent._active_children = []
    parent._active_children_lock = None
    handle = json.loads(dt.delegate_task(tasks=[{"goal": "summarise the quarterly report", "tier": "sonnet_high"}],
                                         background=True, parent_agent=parent))
    assert handle["status"] == "dispatched"
    assert built[0]["model"] == "anthropic/claude-sonnet-5" and built[0]["tier"] == "sonnet_high"
    assert built[0]["reasoning_effort"] == "high"

    with ad._DB_LOCK, ad._transaction() as conn:
        rows = [json.loads(r[0]) for r in conn.execute("SELECT task_json FROM async_delegations")]
    assert rows and rows[0]["tiers"] == [{"task_index": 0, "tier": "sonnet_high", "source": "tier",
                                          "model": "anthropic/claude-sonnet-5", "reasoning_effort": "high"}]
    assert rows[0]["model"] == "anthropic/claude-sonnet-5"

    from tools.delegation_live_log import _manifest_path
    manifest = json.loads(_manifest_path(handle["delegation_id"]).read_text())
    assert manifest["tasks"][0]["tier"] == "sonnet_high" and manifest["tasks"][0]["reasoning_effort"] == "high"
    gate.set()


def test_result_entry_carries_tier_only_for_tiered_children():
    from tools.delegate_tool_child_run import _build_result_entry, _SchemaOutcome

    class _Child:
        model = "anthropic/claude-haiku-4.5"

    plain, tiered = _Child(), _Child()
    tiered._delegate_tier, tiered._delegate_reasoning_effort = "haiku_low", "low"
    result = {"final_response": "ok", "completed": True}
    schema = _SchemaOutcome(schema=None, valid=None, errors=[], retries=0)
    assert "tier" not in _build_result_entry(plain, result, 0, 0.1, schema)
    entry = _build_result_entry(tiered, result, 0, 0.1, schema)
    assert (entry["tier"], entry["reasoning_effort"]) == ("haiku_low", "low")

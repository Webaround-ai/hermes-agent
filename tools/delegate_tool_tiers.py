"""Delegation tiers: named (model, provider, reasoning_effort) routes for ``delegate_task`` children.

Off unless ``delegation.tiers`` is a non-empty mapping; with it unset every function here returns the
"no route" answer and delegation behaves exactly as before.

Config::

    delegation:
      tiers:
        haiku_low:  {model: anthropic/claude-haiku-4.5, reasoning_effort: low}
        opus_high:  {model: anthropic/claude-opus-5.5, provider: openrouter, reasoning_effort: high}
      default_tier: haiku_low          # optional
      tier_chooser_timeout_ms: 3000    # optional, deadline for the delegation_tier_chooser hook

Resolution per task (first hit wins):
  1. an explicit per-task ``model`` (trusted internal callers only; stripped from model-supplied tasks);
  2. the task's ``tier`` (advertised in the schema only when tiers are configured);
  3. the ``delegation_tier_chooser`` plugin hook;
  4. ``delegation.default_tier``;
  5. nothing: the child takes the ordinary delegation route (``delegation.model``/``provider`` or the parent).

A tier without ``provider`` keeps the ordinary delegation route and swaps only the model; a tier with
``provider`` resolves its own credential bundle through the runtime provider system (same path as
``delegation.provider``). Provider-capability inheritance stays default-deny off the parent's route.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_TIER_CHOOSER_TIMEOUT_MS = 3000
_warned: set = set()


def _warn_once(key: str, msg: str, *args: Any) -> None:
    if key not in _warned:
        _warned.add(key)
        logger.warning(msg, *args)


def configured_tiers(cfg: Optional[dict]) -> Dict[str, Dict[str, Any]]:
    """Valid tiers from the ``delegation`` section: ``{name: {model, provider, reasoning_effort}}``.
    Entries without a model are dropped with one warning; an absent/malformed map is ``{}`` (feature off)."""
    raw = (cfg or {}).get("tiers") if isinstance(cfg, dict) else None
    if not isinstance(raw, dict):
        return {}
    tiers: Dict[str, Dict[str, Any]] = {}
    for name, spec in raw.items():
        name = str(name or "").strip()
        model = str(spec.get("model") or "").strip() if isinstance(spec, dict) else ""
        if not name or not model:
            _warn_once(f"tier:{name}", "delegation.tiers.%s has no model; ignoring it", name or "<empty>")
            continue
        provider = str(spec.get("provider") or "").strip() or None
        effort = spec.get("reasoning_effort")
        tiers[name] = {"model": model, "provider": provider,
                       "reasoning_effort": None if effort is None or effort == "" else effort}
    return tiers


def default_tier(cfg: Optional[dict], tiers: Dict[str, Dict[str, Any]]) -> Optional[str]:
    name = str((cfg or {}).get("default_tier") or "").strip()
    if not name:
        return None
    if name not in tiers:
        _warn_once(f"default:{name}", "delegation.default_tier '%s' is not a configured tier; ignoring it", name)
        return None
    return name


def _is_claude_model(model: Optional[str]) -> bool:
    return "claude" in str(model or "").lower()


def effective_tier_effort(effort: Any, model: Optional[str]) -> Any:
    """The tier's reasoning effort as it may be sent for *model*, or ``None`` (= not set by the tier).

    Claude models reject an explicit ``none`` effort (HTTP 400), so a tier that says none/false/disabled
    for a Claude model is treated as if it named no effort at all."""
    if effort is None or effort == "":
        return None
    from hermes_constants import parse_reasoning_effort
    parsed = parse_reasoning_effort(effort)
    if parsed is None:
        _warn_once(f"effort:{effort}", "Unknown tier reasoning_effort '%s'; ignoring it", effort)
        return None
    if parsed.get("enabled") is False and _is_claude_model(model):
        logger.debug("Tier effort '%s' dropped for Claude model %s (none is rejected there)", effort, model)
        return None
    return effort


def effort_label(effort: Any) -> Optional[str]:
    """Display/persist form of a tier effort."""
    if effort is None:
        return None
    if effort is False:
        return "none"
    if isinstance(effort, dict):
        return "none" if effort.get("enabled") is False else (str(effort.get("effort") or "") or None)
    return str(effort).strip().lower() or None


def chooser_timeout_s(cfg: Optional[dict]) -> float:
    raw = (cfg or {}).get("tier_chooser_timeout_ms", DEFAULT_TIER_CHOOSER_TIMEOUT_MS)
    try:
        ms = float(raw)
    except (TypeError, ValueError):
        ms = DEFAULT_TIER_CHOOSER_TIMEOUT_MS
    return max(0.0, ms) / 1000.0


def _ask_chooser(task: Dict[str, Any], task_index: int, task_count: int, context: Optional[str],
                 tiers: Dict[str, Dict[str, Any]], default: Optional[str], parent_session_id: Optional[str],
                 timeout_s: float) -> Optional[str]:
    from hermes_cli.plugin_choices import first_plugin_choice

    def _accept(answer: Any) -> Optional[str]:
        name = str(answer).strip() if isinstance(answer, str) else ""
        return name if name in tiers else None

    return first_plugin_choice(
        "delegation_tier_chooser", timeout_s=timeout_s, accept=_accept,
        goal=str(task.get("goal") or ""), context=task.get("context") or context,
        task_index=task_index, task_count=task_count,
        tiers={k: dict(v) for k, v in tiers.items()}, default_tier=default,
        parent_session_id=parent_session_id,
    )


def pick_task_tiers(
    task_list: List[Dict[str, Any]], cfg: Optional[dict], *, context: Optional[str] = None,
    parent_session_id: Optional[str] = None,
) -> List[Optional[Tuple[str, str]]]:
    """Per task: ``(tier_name, source)`` with source in ``tier|chooser|default``, ``("", "explicit")``
    for an explicit per-task model, or ``None`` (ordinary route). All ``None`` when tiers are off."""
    tiers = configured_tiers(cfg)
    n = len(task_list)
    picks: List[Optional[Tuple[str, str]]] = [None] * n
    if not tiers:
        # Tiers off: only a trusted explicit per-task model can still pin a child.
        for i, t in enumerate(task_list):
            if str(t.get("model") or "").strip():
                picks[i] = ("", "explicit")
        return picks
    default = default_tier(cfg, tiers)
    pending: List[int] = []
    for i, t in enumerate(task_list):
        if str(t.get("model") or "").strip():
            picks[i] = ("", "explicit")
            continue
        asked = str(t.get("tier") or "").strip()
        if asked:
            if asked in tiers:
                picks[i] = (asked, "tier")
                continue
            logger.info("delegate_task: unknown tier '%s' for task %d; choosing another way", asked, i)
        pending.append(i)
    if not pending:
        return picks

    # The chooser is asked for every open task at once, under one shared deadline.
    chosen: Dict[int, Optional[str]] = {}
    timeout_s = chooser_timeout_s(cfg)
    from hermes_cli import plugins as _plugins
    try:
        has_chooser = _plugins.has_hook("delegation_tier_chooser")
    except Exception:
        has_chooser = False
    if has_chooser:
        def _one(idx: int) -> None:
            chosen[idx] = _ask_chooser(task_list[idx], idx, n, context, tiers, default, parent_session_id, timeout_s)
        import contextvars
        threads = []
        for idx in pending:
            th = threading.Thread(target=contextvars.copy_context().run, args=(_one, idx), daemon=True)
            th.start()
            threads.append(th)
        for th in threads:
            th.join(timeout_s + 0.25)  # each _ask_chooser returns by its own deadline
    for idx in pending:
        name = chosen.get(idx)
        if name:
            picks[idx] = (name, "chooser")
        elif default:
            picks[idx] = (default, "default")
    return picks


def build_task_routes(
    task_list: List[Dict[str, Any]], cfg: Optional[dict], base_creds: Dict[str, Any], *,
    context: Optional[str] = None, parent_session_id: Optional[str] = None,
) -> List[Optional[Dict[str, Any]]]:
    """Per task: ``None`` (ordinary route, today's behaviour) or a route dict
    ``{tier, source, model, reasoning_effort, creds}`` where ``creds`` is a full child credential bundle.
    Raises ``ValueError`` (user-facing) when a tier's provider cannot be resolved."""
    picks = pick_task_tiers(task_list, cfg, context=context, parent_session_id=parent_session_id)
    if not any(picks):
        return [None] * len(task_list)
    tiers = configured_tiers(cfg)
    resolved_providers: Dict[Tuple[str, str], Dict[str, Any]] = {}
    routes: List[Optional[Dict[str, Any]]] = []
    for t, pick in zip(task_list, picks):
        if pick is None:
            routes.append(None)
            continue
        name, source = pick
        if source == "explicit":
            model = str(t.get("model")).strip()
            routes.append({"tier": None, "source": source, "model": model, "reasoning_effort": None,
                           "creds": {**base_creds, "model": model}})
            continue
        spec = tiers[name]
        model, provider = spec["model"], spec["provider"]
        if provider:
            key = (provider, model)
            if key not in resolved_providers:
                resolved_providers[key] = _tier_provider_creds(name, provider, model, cfg)
            creds = resolved_providers[key]
        else:
            creds = {**base_creds, "model": model}
        routes.append({"tier": name, "source": source, "model": model,
                       "reasoning_effort": effective_tier_effort(spec["reasoning_effort"], model), "creds": creds})
    return routes


def _tier_provider_creds(name: str, provider: str, model: str, cfg: Optional[dict]) -> Dict[str, Any]:
    from tools.delegate_tool_config import _runtime_provider_credentials
    explicit = (cfg or {}).get("request_overrides")
    try:
        return _runtime_provider_credentials(
            {"model": model, "provider": provider, "base_url": None, "api_key": None, "api_mode": None},
            explicit if isinstance(explicit, dict) else None)
    except ValueError as exc:
        raise ValueError(f"delegation.tiers.{name}: {exc}") from exc


def route_summary(route: Optional[Dict[str, Any]], task_index: int) -> Optional[Dict[str, Any]]:
    """The persisted/displayed facts of one route (no credentials)."""
    if not route:
        return None
    return {"task_index": task_index, "tier": route.get("tier"), "source": route.get("source"),
            "model": route.get("model"), "reasoning_effort": effort_label(route.get("reasoning_effort"))}


def tier_schema_property(cfg: Optional[dict]) -> Optional[Dict[str, Any]]:
    """The model-facing ``tier`` task property, or ``None`` when tiers are not configured."""
    tiers = configured_tiers(cfg)
    if not tiers:
        return None
    default = default_tier(cfg, tiers)
    listing = "; ".join(
        f"{n} = {s['model']}" + (f", {effort_label(s['reasoning_effort'])} thinking" if s["reasoning_effort"] is not None else "")
        for n, s in tiers.items())
    tail = (f" Omit it to let the runtime choose (default '{default}')." if default
            else " Omit it to let the runtime choose.")
    return {
        "type": "string", "enum": list(tiers),
        "description": ("Optional model tier for THIS child (" + listing + "). Pick the cheapest tier that can do "
                        "the job well; the child starts cold, so its tier does not affect your own context." + tail),
    }

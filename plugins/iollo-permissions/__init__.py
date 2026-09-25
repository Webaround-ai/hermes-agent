"""iollo-permissions: Iollo's permission tiers, enforced at the tool layer (fork brief 002).

Settings live under ``plugins.entries["iollo-permissions"]`` (``settings.<key>`` or the entry itself):
``path`` (permissions.yaml, required), ``workspace``, ``scratch_roots``, ``write_roots``, ``versions``, ``trash`` or
``trash_command`` (argv), ``activity``, ``judge_timeout_s`` (default 3).

``pre_tool_call``, in order:
1. Tier 4 (terminal): ``tier4.commands`` and destructive targets outside the workspace, scratch and write roots are
   blocked in code: "Stopped before <action>: Iollo never does this." No model call, no approval, and a
   plugin ``block`` is not subject to yolo or ``approvals.mode: off``.
2. Every acting tool call goes to the approval model with the ``<tier-rubric>`` block: APPROVE proceeds,
   ESCALATE returns an ``approve`` directive (reason ``tier3:<detector>: <sentence>``), DENY blocks.
3. When the judge times out, errors or answers garbage, the keyword detectors decide (tier 3 or tier 1)
   and ``judge_fallback`` is logged without content.
4. ``write_file`` / ``patch`` on an existing file in the workspace or a write root is copied into
   ``versions/<date>/`` first.
``post_tool_call`` appends a "did X" line to ``activity`` for successful ``tier1_notify`` tools, and feeds
the browser element memo and the known-recipients list used by the detectors.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from typing import Any, Dict, Mapping, Optional, Tuple

from . import files as _files
from .detectors import SEND_TOOLS, KnownRecipients, PageMemo, detect, recipients_of
from .hardblock import check_terminal, tier4_message
from .judge import Judge
from .policy import PolicyError, Settings, render_rubric, resolve_settings

logger = logging.getLogger(__name__)

PLUGIN_ID = "iollo-permissions"

_ACTING_TOOLS = frozenset({
    "terminal", "execute_code", "write_file", "patch", "files_trash",
    "browser_type", "browser_click", "browser_press", "browser_select", "browser_dialog", "browser_exec",
    "browser_cdp", "browser_vault_save_login", "browser_vault_fill", "send_message", "computer_use",
})
_DID = {
    "send_message": "Sent a message", "email_send": "Sent an email", "form_submit": "Submitted a form",
    "booking": "Made a booking", "calendar_invite": "Sent a calendar invite", "accept_terms": "Accepted terms",
    "files_trash": "Moved files to the Trash",
}
_TIER3_SENTENCES = {
    "pay": "Iollo is about to pay or enter payment details.",
    "delete": "Iollo is about to delete or change many files outside its workspace.",
    "system": "Iollo is about to change a system or security setting, or a saved password or key.",
    "bulk_new": "Iollo is about to message several people, including someone new.",
}

_state_lock = threading.Lock()
_settings_cache: Dict[str, Any] = {"key": None, "value": None}
_memo = PageMemo()
_judge = Judge()
_recipients: Optional[KnownRecipients] = None


def _config_entry() -> Mapping[str, Any]:
    from hermes_cli.config import load_config_readonly
    cfg = load_config_readonly() or {}
    entry = ((cfg.get("plugins") or {}).get("entries") or {}).get(PLUGIN_ID)
    return entry if isinstance(entry, Mapping) else {}


def _settings() -> Settings:
    """Resolved settings, cached until the config entry or the policy file changes."""
    entry = _config_entry()
    path = (entry.get("settings") or {}).get("path") if isinstance(entry.get("settings"), Mapping) else None
    path = path or entry.get("path")
    try:
        mtime = os.stat(os.path.expanduser(path)).st_mtime_ns if isinstance(path, str) else None
    except OSError:
        mtime = None
    key = (repr(sorted(entry.items(), key=lambda kv: kv[0])), mtime)
    with _state_lock:
        if _settings_cache["key"] == key:
            return _settings_cache["value"]
    value = resolve_settings(entry)
    with _state_lock:
        _settings_cache.update(key=key, value=value)
    return value


def _known_recipients() -> KnownRecipients:
    global _recipients
    with _state_lock:
        if _recipients is None:
            try:
                from hermes_constants import get_hermes_home
                path = str(get_hermes_home() / "memories" / "known-recipients.json")
            except Exception:
                path = None
            _recipients = KnownRecipients(path)
        return _recipients


def _cwd(args: Mapping[str, Any]) -> Optional[str]:
    workdir = args.get("workdir")
    if isinstance(workdir, str) and workdir:
        return os.path.realpath(os.path.expanduser(workdir))
    return os.path.realpath(os.environ.get("TERMINAL_CWD") or os.getcwd())


def _is_acting(tool: str, settings: Settings) -> bool:
    p = settings.policy
    return (tool in _ACTING_TOOLS or tool.startswith("computer_") or tool in p.tier1_notify
            or tool in p.pay_tools or tool in p.system_tools)


def _plain_action(tool: str, args: Mapping[str, Any]) -> str:
    if tool == "terminal":
        words = str(args.get("command") or "").split()
        return f"running {words[0]}" if words else "running this command"
    return f"using {tool}"


def _block(message: str) -> Dict[str, str]:
    return {"action": "block", "message": message}


def _session(session_id: str, task_id: str) -> str:
    return session_id or task_id or "default"


def _on_pre_tool_call(tool_name: str = "", args: Any = None, session_id: str = "", task_id: str = "",
                      **_: Any) -> Optional[Dict[str, str]]:
    args = args if isinstance(args, dict) else {}
    try:
        settings = _settings()
    except PolicyError as err:
        logger.error("iollo-permissions: policy unavailable (%s)", err)
        if tool_name in ("terminal", "execute_code"):
            return _block("Stopped before running this: Iollo's permission policy could not be read.")
        return None

    # 1. Tier 4, in code, before anything else.
    if tool_name == "terminal":
        try:
            message = check_terminal(str(args.get("command") or ""), _cwd(args), settings)
        except Exception:
            logger.exception("iollo-permissions: tier-4 check failed; refusing")
            message = tier4_message("running a command Iollo could not check")
        if message:
            logger.info("iollo-permissions: hard_block tool=%s", tool_name)
            return _block(message)

    if not _is_acting(tool_name, settings):
        return None

    # 2./3. The judge, with the keyword detectors as label and fallback.
    session = _session(session_id, task_id)
    try:
        detector = detect(tool_name, args, settings, _memo, session, _known_recipients())
    except Exception:
        logger.exception("iollo-permissions: detector failed")
        detector = None
    extra: Dict[str, Any] = {}
    if "ref" in args:
        element = _memo.element(session, args.get("ref"))
        if element:
            extra["element"] = element
        amount = _memo.shows_amount(session)
        if amount is not None:
            extra["page_shows_currency_amount"] = amount
    verdict, why = _judge.verdict(session=session, tool=tool_name, args=args, rubric=render_rubric(settings),
                                  detector=detector or "other", timeout=settings.judge_timeout_s, extra=extra)
    if verdict is None:
        logger.info("iollo-permissions: judge_fallback tool=%s reason=%s", tool_name, why)
        verdict = "tier3" if detector else "tier1"

    if verdict == "block":
        return _block(tier4_message(_plain_action(tool_name, args)))

    # 4. Versions before the write (also when the owner still has to approve it).
    if tool_name in ("write_file", "patch"):
        try:
            _files.save_versions(tool_name, args, settings, _cwd(args))
        except OSError as err:
            logger.warning("iollo-permissions: version copy failed (%s)", type(err).__name__)
            return _block("Stopped before changing this file: Iollo could not save the previous version first.")

    if verdict == "tier3":
        label = detector or "other"
        sentence = _TIER3_SENTENCES.get(label, f"Iollo is about to {_plain_action(tool_name, args)}.")
        rule = Judge.cache_key(session, tool_name, args)[2]
        return {"action": "approve", "message": f"tier3:{label}: {sentence}",
                "rule_key": f"iollo-tier3:{label}:{tool_name}:{hashlib.sha256(rule.encode()).hexdigest()[:16]}"}
    return None


def _on_post_tool_call(tool_name: str = "", args: Any = None, result: Any = None, status: str = "",
                       session_id: str = "", task_id: str = "", **_: Any) -> None:
    args = args if isinstance(args, dict) else {}
    session = _session(session_id, task_id)
    try:
        settings = _settings()
    except PolicyError:
        return
    if tool_name.startswith("browser_") and result is not None:
        _memo.observe(session, result, settings.policy.pay_currency)
    if status != "ok":
        return
    if tool_name in SEND_TOOLS or tool_name in settings.policy.tier1_notify:
        addresses = recipients_of(args)
        if addresses:
            known = _known_recipients()
            has_new = any(known.is_new(a) for a in addresses)
            known.record_send(has_new)
            known.remember(addresses)
    if settings.activity and tool_name in settings.policy.tier1_notify:
        try:
            _files.append_activity(settings.activity, _DID.get(tool_name, f"Used {tool_name}"), tool_name)
        except OSError as err:
            logger.warning("iollo-permissions: activity write failed (%s)", type(err).__name__)


_FILES_TRASH_SCHEMA = {
    "name": "files_trash",
    "description": ("Move files or folders to the Trash (recoverable). Use this instead of rm. Only paths in the "
                    "workspace or the allowed write roots."),
    "parameters": {
        "type": "object",
        "properties": {"paths": {"type": "array", "items": {"type": "string"},
                                 "description": "Files or folders to move to the Trash"}},
        "required": ["paths"],
    },
}


def _files_trash(args: Dict[str, Any], **_: Any) -> str:
    try:
        settings = _settings()
    except PolicyError as err:
        return json.dumps({"error": f"permission policy unavailable: {err}"})
    return json.dumps(_files.trash_paths(args.get("paths"), settings))


def _smart_rubric() -> str:
    """Rubric for ``tools.approval_smart`` so flagged terminal commands share the judge's vocabulary."""
    try:
        return render_rubric(_settings())
    except PolicyError:
        return ""


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_tool(name="files_trash", toolset="file", schema=_FILES_TRASH_SCHEMA, handler=_files_trash,
                      description=_FILES_TRASH_SCHEMA["description"], emoji="🗑️")
    from tools import approval_smart
    unregister = approval_smart.register_rubric_provider(_smart_rubric)
    ctx.on_unload(unregister)


def _reset_for_tests(judge: Optional[Judge] = None) -> Tuple[PageMemo, Judge]:
    global _memo, _judge, _recipients
    with _state_lock:
        _settings_cache.update(key=None, value=None)
        _memo, _judge, _recipients = PageMemo(), judge or Judge(), None
    return _memo, _judge

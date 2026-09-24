"""The Iollo permission policy (``permissions.yaml``, schema version 1) and its runtime settings.

The file is owned by the iollo cloud repository (brief 031). This module only loads it, compiles its
regexes, resolves the workspace / write roots for the platform we run on, and renders the
``<tier-rubric>`` block that the approval judge (Jev) reads.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import yaml

SCHEMA_VERSION = 1
_RE_FLAGS = re.IGNORECASE | re.MULTILINE


class PolicyError(ValueError):
    """``permissions.yaml`` is missing, unreadable or not schema version 1."""


@dataclass
class CommandRule:
    """One tier-4 command regex; ``action`` names it in the "Stopped before ..." line."""
    regex: re.Pattern
    action: str


@dataclass
class Policy:
    workspace: Optional[str]
    write_roots: List[str]
    versioning_enabled: bool
    keep_days: int
    tier1_notify: List[str]
    pay_tools: List[str]
    pay_fields: List[re.Pattern]
    pay_buttons: List[re.Pattern]
    pay_currency: re.Pattern
    bulk_threshold: int
    system_commands: List[re.Pattern]
    system_paths: List[str]
    system_tools: List[str]
    password_fields: re.Pattern
    empty_trash: Optional[re.Pattern]
    bulk_recipients: int
    bulk_window_minutes: int
    tier4_commands: List[CommandRule]
    scratch_roots: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Settings:
    """``plugins.entries["iollo-permissions"]`` resolved against the policy file."""
    policy: Policy
    workspace: Optional[str]
    write_roots: List[str]
    versions: Optional[str]
    trash: Optional[str]
    trash_command: Optional[List[str]]
    activity: Optional[str]
    judge_timeout_s: float
    scratch_roots: List[str]


def _platform_key() -> str:
    return "mac" if sys.platform == "darwin" else "box"


def _per_platform(value: Any) -> Any:
    """``{box: ..., mac: ...}`` picks this platform's value; anything else is used as is."""
    if isinstance(value, Mapping) and ("box" in value or "mac" in value):
        return value.get(_platform_key())
    return value


def _compile_all(patterns: Any, where: str) -> List[re.Pattern]:
    out: List[re.Pattern] = []
    for pattern in patterns or []:
        if not isinstance(pattern, str) or not pattern.strip():
            raise PolicyError(f"{where}: every entry must be a non-empty string")
        try:
            out.append(re.compile(pattern, _RE_FLAGS))
        except re.error as err:
            raise PolicyError(f"{where}: invalid regex {pattern!r}: {err}") from err
    return out


def _one(pattern: Any, default: Optional[str], where: str) -> Optional[re.Pattern]:
    if pattern is None:
        return re.compile(default, _RE_FLAGS) if default else None
    return _compile_all([pattern], where)[0]


# The "Stopped before <action>" wording for the tier-4 command families (the policy file holds bare regexes).
_ACTIONS = (
    ("sudo", "running a command as the administrator"), ("mkfs", "formatting a disk"),
    ("newfs", "formatting a disk"), ("diskutil", "erasing or repartitioning a disk"),
    ("csrutil", "disabling System Integrity Protection"), ("spctl", "disabling Gatekeeper"),
    ("fdesetup", "disabling disk encryption"), ("tccutil", "resetting privacy permissions"),
    ("security", "exporting the keychain"), ("dd", "writing raw data to a disk"),
)


def _action_for(pattern: str) -> str:
    words = set(re.findall(r"[a-z]+", re.sub(r"\\[a-zA-Z]", " ", pattern).lower()))
    for word, action in _ACTIONS:
        if word in words:
            return action
    return "running this command"


def _command_rules(entries: Any) -> List[CommandRule]:
    rules: List[CommandRule] = []
    for entry in entries or []:
        if isinstance(entry, Mapping):
            pattern, action = entry.get("pattern"), entry.get("action")
        else:
            pattern, action = entry, None
        compiled = _compile_all([pattern], "tier4.commands")[0]
        if not isinstance(action, str) or not action.strip():
            action = _action_for(str(pattern))
        rules.append(CommandRule(regex=compiled, action=action.strip()))
    return rules


def _str_list(value: Any, where: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise PolicyError(f"{where} must be a list of strings")
    return list(value)


def _int(value: Any, default: int, where: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PolicyError(f"{where} must be a non-negative integer")
    return value


def parse_policy(data: Any) -> Policy:
    """Validate a decoded ``permissions.yaml`` mapping and compile it."""
    if not isinstance(data, Mapping):
        raise PolicyError("permissions.yaml must be a mapping")
    if data.get("version") != SCHEMA_VERSION:
        raise PolicyError(f"permissions.yaml version must be {SCHEMA_VERSION}, got {data.get('version')!r}")
    tier3 = data.get("tier3") or {}
    tier4 = data.get("tier4") or {}
    if not isinstance(tier3, Mapping) or not isinstance(tier4, Mapping):
        raise PolicyError("tier3 and tier4 must be mappings")
    pay, delete = tier3.get("pay") or {}, tier3.get("delete") or {}
    system, bulk_new = tier3.get("system") or {}, tier3.get("bulk_new") or {}
    versioning = data.get("versioning") or {}
    workspace = _per_platform(data.get("workspace"))
    write_roots = _per_platform(data.get("write_roots"))
    return Policy(
        workspace=workspace if isinstance(workspace, str) and workspace else None,
        write_roots=_str_list(write_roots, "write_roots"),
        versioning_enabled=bool(versioning.get("enabled", True)),
        keep_days=_int(versioning.get("keep_days"), 30, "versioning.keep_days"),
        tier1_notify=_str_list(data.get("tier1_notify"), "tier1_notify"),
        pay_tools=_str_list(pay.get("tools"), "tier3.pay.tools"),
        pay_fields=_compile_all(pay.get("fields"), "tier3.pay.fields"),
        pay_buttons=_compile_all(pay.get("buttons"), "tier3.pay.buttons"),
        pay_currency=_one(pay.get("currency"), r"[€$£¥]\s?\d|\d[\d.,]*\s?(€|£|\$|eur|usd|gbp|chf|brl)\b",
                          "tier3.pay.currency"),
        bulk_threshold=_int(delete.get("bulk_threshold"), 20, "tier3.delete.bulk_threshold"),
        system_commands=_compile_all(system.get("commands"), "tier3.system.commands"),
        system_paths=_str_list(system.get("paths"), "tier3.system.paths"),
        system_tools=_str_list(system.get("tools"), "tier3.system.tools"),
        password_fields=_one(system.get("password_fields"), r"password|passwd|\bpwd\b|passcode",
                             "tier3.system.password_fields"),
        empty_trash=_one(delete.get("empty_trash"), None, "tier3.delete.empty_trash"),
        bulk_recipients=_int(bulk_new.get("recipients"), 5, "tier3.bulk_new.recipients"),
        bulk_window_minutes=_int(bulk_new.get("window_minutes"), 10, "tier3.bulk_new.window_minutes"),
        tier4_commands=_command_rules(tier4.get("commands")),
        scratch_roots=_str_list(_per_platform(data.get("scratch_roots")), "scratch_roots"),
        raw=dict(data),
    )


def load_policy(path: str | os.PathLike) -> Policy:
    try:
        data = yaml.safe_load(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as err:
        raise PolicyError(f"cannot read {path}: {err}") from err
    return parse_policy(data)


def _expand(path: str) -> str:
    """``~`` and environment variables; ``$HERMES_HOME`` falls back to Hermes' own default."""
    if "HERMES_HOME" in path and not os.environ.get("HERMES_HOME"):
        try:
            from hermes_constants import get_hermes_home
            home = str(get_hermes_home())
        except Exception:
            home = os.path.expanduser("~/.hermes")
        path = path.replace("${HERMES_HOME}", home).replace("$HERMES_HOME", home)
    return os.path.expanduser(os.path.expandvars(path))


def _abs(path: str, base: Optional[str] = None) -> str:
    expanded = _expand(path)
    if not os.path.isabs(expanded):
        expanded = os.path.join(base or os.path.expanduser("~"), expanded)
    return os.path.realpath(expanded)


def _entry_value(entry: Mapping[str, Any], key: str, default: Any = None) -> Any:
    """``settings.<key>`` first (the PluginContext convention), then the entry itself."""
    settings = entry.get("settings")
    if isinstance(settings, Mapping) and key in settings:
        return settings[key]
    return entry.get(key, default)


def resolve_settings(entry: Mapping[str, Any]) -> Settings:
    """Build runtime settings from the plugin's config entry. Raises :class:`PolicyError`."""
    path = _entry_value(entry, "path")
    if not isinstance(path, str) or not path:
        raise PolicyError('plugins.entries["iollo-permissions"].path is not set')
    policy = load_policy(path)
    workspace = _entry_value(entry, "workspace", policy.workspace)
    workspace = _abs(workspace) if isinstance(workspace, str) and workspace else None
    roots = _entry_value(entry, "write_roots", None)
    roots = policy.write_roots if roots is None else _str_list(roots, "write_roots")
    write_roots = [r for r in dict.fromkeys(_abs(r) for r in roots) if r != workspace]
    scratch = _entry_value(entry, "scratch_roots", None)
    scratch = policy.scratch_roots if scratch is None else _str_list(scratch, "scratch_roots")
    scratch_roots = [r for r in dict.fromkeys(_abs(r) for r in scratch) if r != workspace]

    def _dir(key: str, default_name: str) -> Optional[str]:
        value = _entry_value(entry, key)
        if isinstance(value, str) and value:
            return _abs(value, workspace)
        return os.path.join(workspace, default_name) if workspace else None

    trash_command = _entry_value(entry, "trash_command")
    if trash_command is not None and not (isinstance(trash_command, list) and trash_command
                                          and all(isinstance(a, str) and a for a in trash_command)):
        raise PolicyError("trash_command must be a non-empty argv list")
    activity = _entry_value(entry, "activity")
    timeout = _entry_value(entry, "judge_timeout_s", 3)
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        timeout = 3.0
    return Settings(
        policy=policy, workspace=workspace, write_roots=write_roots,
        versions=_dir("versions", ".versions") if policy.versioning_enabled else None,
        trash=_dir("trash", ".trash"), trash_command=trash_command,
        activity=os.path.expanduser(activity) if isinstance(activity, str) and activity else None,
        judge_timeout_s=timeout if timeout > 0 else 3.0, scratch_roots=scratch_roots,
    )


def _describe(section: Any, fallback: str) -> str:
    text = section.get("describe") if isinstance(section, Mapping) else None
    return " ".join(str(text).split()) if isinstance(text, str) and text.strip() else fallback


def render_rubric(settings: Settings) -> str:
    """The ``<tier-rubric>`` block: trusted policy text for the judge's system prompt. Tier prose comes from
    the file's ``describe`` keys; the fallbacks restate brief 031's table."""
    p, raw = settings.policy, settings.policy.raw
    tier3 = raw.get("tier3") if isinstance(raw.get("tier3"), Mapping) else {}
    tier3_fallbacks = {
        "pay": "Spending money or entering payment details.",
        "delete": (f"Permanent deletion of files inside a write root but outside the workspace, or one action "
                   f"touching more than {p.bulk_threshold} files there."),
        "system": ("Changing system or security settings, or saving, revealing, changing or exporting a password "
                   "or key, including typing into a password field."),
        "bulk_new": (f"One send to at least {p.bulk_recipients} addresses, or that many sends within "
                     f"{p.bulk_window_minutes} minutes, where one address is new."),
    }
    lines = [
        "<tier-rubric>",
        "Classify ONE tool call the agent is about to make for its owner.",
        f"Workspace: {settings.workspace or 'none'}. Scratch roots: {', '.join(settings.scratch_roots) or 'none'}. "
        f"Write roots: {', '.join(settings.write_roots) or 'none'}.",
        "",
        "APPROVE (tier 1, runs without asking): " + _describe(raw.get("tier1"), (
            "everything not listed under ESCALATE or DENY: reading, browsing, searching, booking, comparing; "
            "writing in the workspace, scratch roots and write roots; drafting and, after the owner's OK in chat, "
            "sending messages; acting on the web for the owner; scheduled and background work.")),
        "",
        "ESCALATE (tier 3, the owner is asked every time), only for these detectors:",
    ]
    lines += [f"- {name}: {_describe(tier3.get(name), text)}" for name, text in tier3_fallbacks.items()]
    lines += [
        "",
        "DENY (tier 4, refused even if the owner asks): " + _describe(raw.get("tier4"), (
            "destructive file-system commands outside the workspace, the scratch roots and the write roots; "
            "disk-level operations; disabling security; exporting the keychain.")),
        "</tier-rubric>",
    ]
    return "\n".join(lines)

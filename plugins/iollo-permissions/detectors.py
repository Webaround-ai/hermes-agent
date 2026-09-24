"""Keyword detectors for tier 3 (``pay``, ``delete``, ``system``, ``bulk_new``) from ``permissions.yaml``.

They label every tier-3 approval (``tier3:<detector>``) and are the whole decision when the judge is
unavailable. Browser clicks and typing carry only an element ref (``@e5``), so :class:`PageMemo` remembers
the element names and whether a currency amount was on screen from the last browser snapshot it saw.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, Iterable, List, Mapping, Optional, Tuple

from .hardblock import DELETE, terminal_targets, zone
from .policy import Settings

_SNAPSHOT_LINE = re.compile(r'^\s*-\s*(?P<role>[\w-]+)\s+"(?P<name>[^"]*)"[^\n]*?\[ref=(?P<ref>e\d+)\]', re.MULTILINE)
_CARD_DIGITS = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,7}(?:\s?[A-Z0-9]{1,4})?\b")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_RECIPIENT_KEYS = ("to", "cc", "bcc", "recipients", "recipient", "target", "targets", "attendees", "emails")
SEND_TOOLS = {"send_message", "email_send", "calendar_invite"}


def luhn_ok(digits: str) -> bool:
    total, double = 0, False
    for ch in reversed(digits):
        n = int(ch)
        if double:
            n = n * 2 - 9 if n > 4 else n * 2
        total, double = total + n, not double
    return total % 10 == 0


def looks_like_card(text: str) -> bool:
    for match in _CARD_DIGITS.finditer(text or ""):
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and luhn_ok(digits):
            return True
    return False


class PageMemo:
    """Per-session element names (ref -> "role name") and "a currency amount is on screen"."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._refs: Dict[str, Dict[str, str]] = {}
        self._amount: Dict[str, bool] = {}

    def observe(self, session: str, result: Any, currency: re.Pattern) -> None:
        text = result if isinstance(result, str) else json.dumps(result, default=str)
        try:  # snapshots arrive JSON-encoded; decode so quotes are real quotes
            decoded = json.loads(text)
            if isinstance(decoded, dict):
                data = decoded.get("data") if isinstance(decoded.get("data"), dict) else decoded
                text = str(data.get("snapshot") or decoded.get("snapshot") or text)
        except (ValueError, TypeError):
            pass
        refs = {m.group("ref"): f'{m.group("role")} {m.group("name")}' for m in _SNAPSHOT_LINE.finditer(text)}
        if not refs:
            return
        with self._lock:
            self._refs[session] = refs
            self._amount[session] = bool(currency.search(text))

    def element(self, session: str, ref: Any) -> Optional[str]:
        key = str(ref or "").lstrip("@").strip()
        with self._lock:
            return self._refs.get(session, {}).get(key)

    def shows_amount(self, session: str) -> Optional[bool]:
        with self._lock:
            return self._amount.get(session)


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


def _recipients(args: Mapping[str, Any]) -> List[str]:
    found: List[str] = []
    for key in _RECIPIENT_KEYS:
        for text in _strings(args.get(key)):
            parts = _EMAIL.findall(text) or [p.strip() for p in re.split(r"[,;]", text) if p.strip()]
            found.extend(p.lower() for p in parts)
    return list(dict.fromkeys(found))


class KnownRecipients:
    """Hashed addresses this Iollo has messaged (``known-recipients.json`` in the memory dir) and a send window."""

    def __init__(self, path: Optional[str]) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._sends: Deque[Tuple[float, bool]] = deque()

    @staticmethod
    def _hash(address: str) -> str:
        return hashlib.sha256(address.strip().lower().encode("utf-8")).hexdigest()

    def _load(self) -> set:
        if not self.path:
            return set()
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            return set(data.get("sha256", [])) if isinstance(data, dict) else set()
        except (OSError, ValueError):
            return set()

    def is_new(self, address: str) -> bool:
        return self._hash(address) not in self._load()

    def remember(self, addresses: Iterable[str]) -> None:
        if not self.path:
            return
        with self._lock:
            known = self._load() | {self._hash(a) for a in addresses}
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "sha256": sorted(known)}, fh)
            os.replace(tmp, self.path)

    def window_hit(self, settings: Settings, has_new: bool, now: Optional[float] = None) -> bool:
        """True when this send would be the Nth inside the window and one of them involved a new address."""
        now = time.time() if now is None else now
        window = settings.policy.bulk_window_minutes * 60
        with self._lock:
            while self._sends and now - self._sends[0][0] > window:
                self._sends.popleft()
            recent = list(self._sends)
        count = len(recent) + 1
        return count >= settings.policy.bulk_recipients and (has_new or any(n for _, n in recent))

    def record_send(self, has_new: bool, now: Optional[float] = None) -> None:
        with self._lock:
            self._sends.append((time.time() if now is None else now, has_new))


def _cwd(args: Mapping[str, Any]) -> Optional[str]:
    workdir = args.get("workdir")
    if isinstance(workdir, str) and workdir:
        return os.path.realpath(os.path.expanduser(workdir))
    return os.path.realpath(os.environ.get("TERMINAL_CWD") or os.getcwd())


def _matches_system_path(path: str, settings: Settings) -> bool:
    for entry in settings.policy.system_paths:
        expanded = os.path.expanduser(entry)
        if "/" not in entry.rstrip("/"):  # a bare name such as ".env" matches the file anywhere
            if os.path.basename(path.rstrip("/")) == entry.rstrip("/"):
                return True
            continue
        prefix = os.path.realpath(expanded.rstrip("/"))
        if path == prefix or path.startswith(prefix + os.sep):
            return True
    return False


def detect(tool: str, args: Mapping[str, Any], settings: Settings, memo: PageMemo, session: str,
           recipients: KnownRecipients) -> Optional[str]:
    """The name of the first tier-3 detector that matches, else None (tier 1)."""
    policy = settings.policy
    element = memo.element(session, args.get("ref")) if "ref" in args else None
    texts = list(_strings(args))

    # pay
    if tool in policy.pay_tools:
        haystack = " ".join([element or ""] + [str(args.get(k) or "") for k in ("field", "name", "label", "selector")])
        if "type" in tool or "fill" in tool:
            if any(p.search(haystack) for p in policy.pay_fields):
                return "pay"
            if any(looks_like_card(t) or _IBAN.search(t) for t in texts):
                return "pay"
        else:
            label = " ".join([element or ""] + [str(args.get(k) or "") for k in ("text", "label", "name", "button")])
            if any(b.search(label) for b in policy.pay_buttons) and memo.shows_amount(session) is not False:
                return "pay"

    # system
    if tool in policy.system_tools:
        return "system"
    if tool == "terminal":
        command = str(args.get("command") or "")
        if any(p.search(command) for p in policy.system_commands):
            return "system"
    if tool in ("write_file", "patch"):
        path = args.get("path")
        if isinstance(path, str) and path and _matches_system_path(
                os.path.realpath(os.path.join(_cwd(args) or "/", os.path.expanduser(path))), settings):
            return "system"
    if "type" in tool and tool != "browser_vault_fill" and element and policy.password_fields.search(element):
        return "system"

    # delete (terminal): permanent deletes inside a write root, or more than bulk_threshold files there
    if tool == "terminal":
        targets = terminal_targets(str(args.get("command") or ""), _cwd(args))
        if any(t.kind == DELETE and zone(t.path, settings)[0] == "root" for t in targets):
            return "delete"
        if policy.empty_trash is not None and policy.empty_trash.search(str(args.get("command") or "")):
            return "delete"
    if tool == "files_trash":
        paths = [p for p in args.get("paths") or [] if isinstance(p, str)]
        in_roots = [p for p in paths if zone(os.path.realpath(os.path.expanduser(p)), settings)[0] == "root"]
        if len(in_roots) > policy.bulk_threshold:
            return "delete"

    # bulk_new
    if tool in SEND_TOOLS or tool in policy.tier1_notify:
        addresses = _recipients(args)
        has_new = any(recipients.is_new(a) for a in addresses)
        if has_new and len(addresses) >= policy.bulk_recipients:
            return "bulk_new"
        if addresses and recipients.window_hit(settings, has_new):
            return "bulk_new"
    return None


def recipients_of(args: Mapping[str, Any]) -> List[str]:
    return _recipients(args)

"""Versions before overwrite, the ``files_trash`` tool and the activity file."""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import threading
from typing import Any, Dict, List, Mapping, Optional

from .hardblock import zone
from .policy import Settings

_V4A_HEADER = re.compile(r"^\*\*\*\s*(?:Update|Delete)\s+File:\s*(.+?)\s*$", re.MULTILINE)
_V4A_MOVE = re.compile(r"^\*\*\*\s*Move\s+File:\s*(.+?)\s*->", re.MULTILINE)
_lock = threading.Lock()
_last_prune: Dict[str, str] = {}


def _today() -> str:
    return _dt.date.today().isoformat()


def _resolve(path: str, cwd: Optional[str]) -> str:
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        expanded = os.path.join(cwd or os.getcwd(), expanded)
    return os.path.realpath(expanded)


def written_paths(tool: str, args: Mapping[str, Any]) -> List[str]:
    """Paths a ``write_file`` / ``patch`` call is about to change (V4A patches may name several)."""
    paths: List[str] = []
    path = args.get("path")
    if isinstance(path, str) and path:
        paths.append(path)
    patch = args.get("patch")
    if tool == "patch" and isinstance(patch, str):
        paths.extend(m.group(1) for m in _V4A_HEADER.finditer(patch))
        paths.extend(m.group(1) for m in _V4A_MOVE.finditer(patch))
    return list(dict.fromkeys(paths))


def _versioned_name(versions: str, path: str) -> str:
    target = os.path.join(versions, _today(), path.lstrip(os.sep))
    if not os.path.lexists(target):
        return target
    stamp = _dt.datetime.now().strftime("%H%M%S%f")
    return f"{target}.{stamp}"


def prune_versions(settings: Settings, today: Optional[_dt.date] = None) -> None:
    """Delete ``versions/<date>`` directories older than ``versioning.keep_days``."""
    versions = settings.versions
    if not versions or not os.path.isdir(versions):
        return
    today = today or _dt.date.today()
    cutoff = today - _dt.timedelta(days=settings.policy.keep_days)
    for name in os.listdir(versions):
        try:
            day = _dt.date.fromisoformat(name)
        except ValueError:
            continue
        if day < cutoff:
            shutil.rmtree(os.path.join(versions, name), ignore_errors=True)


def save_versions(tool: str, args: Mapping[str, Any], settings: Settings, cwd: Optional[str]) -> List[str]:
    """Copy each existing file the call will change (in the workspace or a write root) into versions."""
    if not settings.versions:
        return []
    saved: List[str] = []
    for raw in written_paths(tool, args):
        path = _resolve(raw, cwd)
        if zone(path, settings)[0] not in ("workspace", "root") or not os.path.isfile(path):
            continue
        with _lock:
            target = _versioned_name(settings.versions, path)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(path, target)
        saved.append(target)
    today = _today()
    if saved and _last_prune.get(settings.versions) != today:
        _last_prune[settings.versions] = today
        prune_versions(settings)
    return saved


def trash_paths(paths: Any, settings: Settings, cwd: Optional[str] = None) -> Dict[str, Any]:
    """Move paths to the Trash: ``trash_command`` (the Mac) or the ``trash`` directory (the box)."""
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and p for p in paths):
        return {"error": "paths must be a non-empty list of file or folder paths"}
    accepted: List[str] = []
    refused: List[Dict[str, str]] = []
    for raw in paths:
        expanded = os.path.expanduser(raw)
        if not os.path.isabs(expanded):
            expanded = os.path.join(cwd or settings.workspace or os.getcwd(), expanded)
        # The entry itself (a symlink is trashed as a link), inside its resolved parent.
        path = os.path.join(os.path.realpath(os.path.dirname(os.path.abspath(expanded))), os.path.basename(expanded))
        where, root = zone(os.path.realpath(path), settings)
        own_zone, _ = zone(path, settings)
        if own_zone not in ("workspace", "root") or where not in ("workspace", "root") or path in (
                settings.workspace, root):
            refused.append({"path": raw, "reason": "outside the workspace and write roots"})
        elif not os.path.lexists(path):
            refused.append({"path": raw, "reason": "does not exist"})
        else:
            accepted.append(path)
    trashed: List[str] = []
    if accepted and settings.trash_command:
        try:
            proc = subprocess.run([*settings.trash_command, *accepted], capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=60)
        except (OSError, subprocess.TimeoutExpired) as err:
            return {"error": f"trash command failed: {type(err).__name__}", "refused": refused}
        if proc.returncode != 0:
            return {"error": f"trash command exited {proc.returncode}", "refused": refused}
        trashed = accepted
    elif accepted:
        if not settings.trash:
            return {"error": "no trash directory or trash_command is configured", "refused": refused}
        day_dir = os.path.join(settings.trash, _today())
        os.makedirs(day_dir, exist_ok=True)
        for path in accepted:
            dest = os.path.join(day_dir, os.path.basename(path.rstrip(os.sep)))
            if os.path.lexists(dest):
                dest = f"{dest}.{_dt.datetime.now().strftime('%H%M%S%f')}"
            shutil.move(path, dest)
            trashed.append(path)
    return {"trashed": trashed, "refused": refused}


def append_activity(activity: str, did: str, tool: str) -> None:
    line = json.dumps({"did": did, "tool": tool, "at": _dt.datetime.now(_dt.timezone.utc).isoformat(
        timespec="seconds")}, ensure_ascii=False)
    os.makedirs(os.path.dirname(os.path.abspath(activity)), exist_ok=True)
    with _lock, open(activity, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")

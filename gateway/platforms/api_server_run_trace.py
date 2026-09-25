"""Iollo's allowlisted, transient inputs for the envelope trace producer (brief 037).

The producer owns first-line command selection and same-revision attachment references.
Nothing here is added to durable run status or to the Desktop JSON-RPC protocol.
"""

import hashlib
import json
import os
import re
from urllib.parse import urlsplit, urlunsplit

from agent.redact import redact_sensitive_text

_BROWSER = frozenset({
    "browser_navigate", "browser_snapshot", "browser_click", "browser_type",
    "browser_scroll", "browser_back", "browser_press", "browser_get_images",
    "browser_vision", "browser_console",
})
_FILE = {"read_file": "read", "patch": "edit", "write_file": None}
_CREDENTIAL = re.compile(
    r'''(?ix)(\b(?:[\w-]*(?:token|password|secret|api[_-]?key)[\w-]*|key)\s*(?:=|\s)\s*|\bBearer\s+)(?:"[^"\n]*"|'[^'\n]*'|[^\s;]+)'''
)


def _text(value, cap):
    if not isinstance(value, str) or not value:
        return None
    return _CREDENTIAL.sub(r"\1…", redact_sensitive_text(
        value, force=True, redact_url_credentials=True))[:cap]


def _url(value):
    if not isinstance(value, str):
        return None
    try:
        url = urlsplit(value)
        if url.scheme not in {"http", "https"} or not url.hostname:
            return None
        host = url.hostname
        if len(host) > 128:
            return None
        host = f"[{host}]" if ":" in host else host
        if url.port:
            host += f":{url.port}"
        return urlunsplit((url.scheme, host, _text(url.path, 160) or "", "", ""))
    except ValueError:
        return None


def _working_dir(kwargs):
    from tools.file_tools_paths import _resolve_base_dir

    cwd = kwargs.get("working_dir")
    if isinstance(cwd, str) and os.path.isabs(cwd):
        return cwd
    return str(_resolve_base_dir(kwargs.get("task_id")))


def _path(value, cwd, task_id):
    from tools.file_tools_paths import _resolve_path_for_task

    if not isinstance(value, str) or not value or value.startswith("~"):
        return None
    if ".." in value.replace("\\", "/").split("/"):
        return None
    # Use file-tool resolution, including host symlinks and remote workspace rules.
    try:
        resolved = str(_resolve_path_for_task(value, task_id))
        relative = os.path.relpath(resolved, cwd).replace("\\", "/")
    except (OSError, ValueError):  # Invalid paths or different Windows drives.
        return None
    if relative == ".." or relative.startswith("../") or len(relative) > 200:
        return None
    return _text(relative, 200)


def tool_trace_fields(event_type, tool, args, kwargs):
    """Only named, typed fields cross the authenticated runs SSE boundary."""
    fields = {}
    call_id = kwargs.get("tool_call_id")
    if isinstance(call_id, str) and call_id:
        fields["tool_call_id"] = (call_id if len(call_id) <= 128 else
                                  hashlib.sha256(call_id.encode()).hexdigest())
    supported = tool in _BROWSER or tool in _FILE or tool == "terminal"
    if not supported:
        return fields
    detail = {}
    if event_type == "tool.started":
        cwd = _working_dir(kwargs)
        fields["working_dir"] = cwd
        args = args if isinstance(args, dict) else {}
        if tool in _BROWSER:
            detail["url"] = _url(args.get("url"))
        elif tool in _FILE:
            detail["path"] = _path(args.get("path"), cwd, kwargs.get("task_id"))
            detail["op"] = _FILE[tool]
        else:
            # Keep newlines: truncating after flattening would alter the first command.
            detail["command"] = _text(args.get("command"), 16384)
        key = "args"
    else:
        result = kwargs.get("result")
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except (ValueError, TypeError):
                result = None
        result = result if isinstance(result, dict) else {}
        if tool in _BROWSER:
            detail = {"url": _url(result.get("url")), "title": _text(result.get("title"), 120)}
        elif tool in _FILE and not kwargs.get("is_error") and not result.get("error"):
            op = result.get("op")
            if isinstance(op, str) and op in {"read", "edit", "create", "delete"}:
                detail["op"] = op
            elif tool == "read_file" and result:
                detail["op"] = "read"
            elif tool == "patch" and result.get("success"):
                outcomes = {op for key, op in (("files_created", "create"), ("files_deleted", "delete"),
                                             ("files_modified", "edit")) if result.get(key)}
                if len(outcomes) == 1:
                    detail["op"] = outcomes.pop()
            if type(result.get("lines_changed")) is int:
                detail["lines_changed"] = result["lines_changed"]
        elif tool == "terminal" and type(result.get("exit_code")) is int:
            detail["exit_code"] = result["exit_code"]
        key = "result_summary"
    detail = {k: v for k, v in detail.items() if v is not None}
    if detail:
        fields[key] = detail
    return fields

"""Operator-managed tool ceiling, scoped to the current Hermes profile.

Absent preserves ordinary personal agents. Explicit empty/malformed denies all.
Managed hosts set agent.managed_tool_host in their root config and keep it root
owned: missing/broken profile config then also denies all, never recovers open.
"""
from hermes_constants import get_hermes_home, get_process_hermes_home
import yaml


def _agent(home):
    try:
        value = yaml.safe_load((home / "config.yaml").read_text())
        return value.get("agent", {}) if isinstance(value, dict) else {}
    except (OSError, ValueError, yaml.YAMLError):
        return {}


def allowed_tools():
    agent = _agent(get_hermes_home())
    if not isinstance(agent, dict):
        return frozenset()
    if "managed_allowed_tools" not in agent:
        root = _agent(get_process_hermes_home())
        return frozenset() if isinstance(root, dict) and root.get("managed_tool_host") else None
    values = agent["managed_allowed_tools"]
    if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
        return frozenset()
    return frozenset(values)


def denial(name):
    allowed = allowed_tools()
    if allowed is not None and name not in allowed:
        return "This tool is unavailable in this managed profile."
    return None

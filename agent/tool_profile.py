"""Per-run tool profile (iollo fork): a caller may run one turn with a smaller tool set.

``POST /v1/runs`` may carry ``tool_profile: {"name": "light", "tools": [...], "skills": false}`` (without
``tools``: every tool, the run is only named in its report). The agent is
built exactly as without it: the stored system prompt, the session's tool pin, compression rebuilds and every
persisted byte stay those of the full tool set. Only the request sent to the provider is projected
(``agent/turn_api_request.py``): ``tools[]`` keeps the named tools (in the session's order) plus any tool
registered ``lifts_tool_profile=True``, and with ``skills`` false the ``## Skills`` index block is cut out of the
system message. The text before it (identity, guidance, caller context) stays byte-identical.

Without a profile, ``lifts_tool_profile`` tools are left out of every request. With one, a call to such a tool, or
to any tool the agent has that the profile does not name, lifts the profile for the rest of the turn
(``lift_tool_profile_for_calls``): that call runs as usual and the next request carries every tool and the skills
index. A wrong light guess costs one model call, never a lost reply. The run reports the profile it asked for and
whether it was lifted (``tool_profile_report``).
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Any, Iterable, List, Optional

logger = logging.getLogger(__name__)

MAX_PROFILE_TOOLS = 200
MAX_NAME_LENGTH = 64
MAX_NOTE_LENGTH = 600
_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")

# The block ``agent.prompt_builder._render_skills_block`` renders (non-oneshot variant), plus its optional
# ``[names only]`` note. Matched from its heading to its fixed closing sentence.
_SKILLS_HEAD = "## Skills\nBefore replying, scan the skills below."
_SKILLS_TAIL = "Only proceed without loading a skill if genuinely none are relevant to the task."
_SKILLS_HIDDEN_NOTE_RE = re.compile(
    r"\n\(Categories marked \[names only\] are outside the current coding context, so their descriptions are "
    r"omitted — the skills work normally and load with skill_view\(name\) as usual\.\)")


@dataclass(frozen=True)
class ToolProfile:
    name: str
    tools: Optional[frozenset]  # None: every tool (a named, reported run without projection)
    skills: bool = True
    # Appended to each profile-lifting tool's description in this run's requests: what exists only with every
    # tool (the caller keeps it stable, so the profile's prefix stays cached).
    note: str = ""


def normalize_tool_profile(value: Any) -> Optional[ToolProfile]:
    """Request value -> profile; ``None``/absent -> ``None`` (every tool). Raises ``ValueError`` when invalid."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("'tool_profile' must be an object")
    name = value.get("name", "custom")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError("'tool_profile.name' must be a short identifier")
    tools = value.get("tools")
    if tools is None:
        if value.get("skills", True) is not True:
            raise ValueError("'tool_profile' without tools keeps every tool and the skills index")
        return ToolProfile(name=name, tools=None)
    if not isinstance(tools, list) or not tools or len(tools) > MAX_PROFILE_TOOLS:
        raise ValueError(f"'tool_profile.tools' must be a list of 1-{MAX_PROFILE_TOOLS} tool names")
    if any(not isinstance(t, str) or not _NAME_RE.match(t) for t in tools):
        raise ValueError("'tool_profile.tools' must contain tool names")
    skills = value.get("skills", True)
    if not isinstance(skills, bool):
        raise ValueError("'tool_profile.skills' must be a boolean")
    note = value.get("note", "")
    if not isinstance(note, str) or len(note) > MAX_NOTE_LENGTH:
        raise ValueError(f"'tool_profile.note' must be a string of at most {MAX_NOTE_LENGTH} characters")
    return ToolProfile(name=name, tools=frozenset(tools), skills=skills, note=" ".join(note.split()))


def set_tool_profile(agent: Any, profile: Optional[ToolProfile]) -> None:
    """Declare this run's profile on a freshly built agent (before ``run_conversation``). A profile without
    tools projects nothing; it only names the run in its report."""
    agent._tool_profile = profile if profile is not None and profile.tools is not None else None
    agent._tool_profile_requested = profile
    agent._tool_profile_lifted = None


def active_tool_profile(agent: Any) -> Optional[ToolProfile]:
    profile = getattr(agent, "_tool_profile", None)
    return profile if isinstance(profile, ToolProfile) else None


def _tool_name(tool: Any) -> str:
    if isinstance(tool, dict):
        fn = tool.get("function")
        if isinstance(fn, dict) and isinstance(fn.get("name"), str):
            return fn["name"]
        if isinstance(tool.get("name"), str):
            return tool["name"]
    return ""


def _lifting_names(names: Iterable[str]) -> set:
    from tools.registry import registry
    out = set()
    for name in names:
        entry = registry.get_entry(name) if name else None
        if entry is not None and getattr(entry, "lifts_tool_profile", False):
            out.add(name)
    return out


def project_tools(agent: Any, tools: Optional[List[Any]]) -> Optional[List[Any]]:
    """The tools this request may declare; returns the same list object when nothing is removed."""
    if not tools:
        return tools
    names = [_tool_name(t) for t in tools]
    lifting = _lifting_names(names)
    profile = active_tool_profile(agent)
    if profile is None:
        keep = [t for t, n in zip(tools, names) if n not in lifting]
    else:
        keep = [_with_note(t, profile.note) if n in lifting else t
                for t, n in zip(tools, names) if n in profile.tools or n in lifting]
        return keep
    return tools if len(keep) == len(tools) else keep


def _with_note(tool: Any, note: str) -> Any:
    """A copy of ``tool`` whose description ends with ``note`` (the original definition is never mutated)."""
    fn = tool.get("function") if isinstance(tool, dict) else None
    if not note or not isinstance(fn, dict):
        return tool
    description = str(fn.get("description") or "")
    return {**tool, "function": {**fn, "description": (description + " " + note).strip()}}


def strip_skills_block(text: str) -> str:
    """``text`` without the ``## Skills`` index block (and the blank line before it); unchanged without one."""
    if not isinstance(text, str):
        return text
    start = text.find(_SKILLS_HEAD)
    if start < 0:
        return text
    end = text.find(_SKILLS_TAIL, start)
    if end < 0:
        return text
    end += len(_SKILLS_TAIL)
    note = _SKILLS_HIDDEN_NOTE_RE.match(text, end)
    if note:
        end = note.end()
    before, after = text[:start], text[end:]
    if before.endswith("\n\n") and after.startswith("\n\n"):
        before = before[:-2]
    elif not before.strip():
        after = after.lstrip("\n")
    return before + after


def _strip_system_content(content: Any) -> Any:
    if isinstance(content, str):
        return strip_skills_block(content)
    if isinstance(content, list):
        out, changed = [], False
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                text = strip_skills_block(part["text"])
                if text != part["text"]:
                    part, changed = {**part, "text": text}, True
            out.append(part)
        return out if changed else content
    return content


def project_system_message(agent: Any, api_messages: List[Any]) -> List[Any]:
    """``api_messages`` with the skills index cut from the leading system message when the profile drops skills.
    Returns a new list (the original message dicts are never mutated) or the same list when nothing changes."""
    profile = active_tool_profile(agent)
    if profile is None or profile.skills or not api_messages:
        return api_messages
    first = api_messages[0]
    if not isinstance(first, dict) or first.get("role") != "system":
        return api_messages
    content = _strip_system_content(first.get("content"))
    if content is first.get("content"):
        return api_messages
    return [{**first, "content": content}, *api_messages[1:]]


def lift_tool_profile_for_calls(agent: Any, tool_calls: Iterable[Any]) -> bool:
    """Lift the profile when a call names a profile-lifting tool or a tool the profile left out; True if lifted.
    Unknown names are left to the normal invalid-tool path."""
    profile = active_tool_profile(agent)
    if profile is None:
        return False
    valid = getattr(agent, "valid_tool_names", None) or set()
    names = []
    for tc in tool_calls or []:
        fn = getattr(tc, "function", None)
        name = getattr(fn, "name", None) if fn is not None else None
        if isinstance(name, str):
            names.append(name)
    lifting = _lifting_names(names)
    outside = [n for n in names if n in valid and (n not in profile.tools or n in lifting)]
    if not outside:
        return False
    reason = "escape" if any(n in lifting for n in outside) else "tool:" + outside[0]
    agent._tool_profile = None
    agent._tool_profile_lifted = reason
    logger.info("tool profile %s lifted for session %s (%s)", profile.name, getattr(agent, "session_id", ""), reason)
    return True


def normalize_ends_turn(value: Any) -> bool:
    """Request ``ends_turn`` -> bool (absent: False). Turn-ending tools end a turn only on runs that ask for it."""
    if value is None:
        return False
    if not isinstance(value, bool):
        raise ValueError("'ends_turn' must be a boolean")
    return value


def set_ends_turn(agent: Any, enabled: bool) -> None:
    agent._ends_turn_enabled = bool(enabled)


def tool_profile_report(agent: Any, api_calls: Any = None) -> Optional[dict]:
    """``{"name", "lifted", "api_calls"}`` for a run that asked for a profile, else ``None`` (wire shape
    unchanged). ``api_calls`` lets a caller compare input tokens per model call across profiles."""
    requested = getattr(agent, "_tool_profile_requested", None)
    if not isinstance(requested, ToolProfile):
        return None
    lifted = getattr(agent, "_tool_profile_lifted", None)
    calls = api_calls if isinstance(api_calls, int) and not isinstance(api_calls, bool) else 0
    return {"name": requested.name, "lifted": lifted or "", "api_calls": calls}

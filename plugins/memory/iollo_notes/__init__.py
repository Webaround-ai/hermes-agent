"""iollo_notes — Iollo's memory provider (fork brief 003).

Many small typed Markdown notes in ``$HERMES_HOME/memory-notes`` (the synced git folder), the
generated profile ``USER.md`` injected whole into the system prompt, and everything else found
on demand by a local index (``index.py``). Memory never leaves the device: the only network call
is the Jev decision URL the policy configures (``decide_url``), and every decision has a local
fallback. Consolidation and USER.md generation run on the box (iollo brief 032), not here.

Activate with ``memory.provider: iollo_notes``. Settings live under ``memory.iollo_notes`` in
config.yaml: ``decide_url``, ``decide_token_env``, ``decide_timeout_s`` (1.5), ``device``
(``box`` or ``mac:<id>``).
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider, RecallStatus, is_trivial_prompt
from tools.registry import tool_error, tool_result

from . import notes as notes_mod
from .index import NoteIndex

logger = logging.getLogger(__name__)

PROVIDER_NAME = "iollo_notes"
NOTES_DIRNAME = "memory-notes"
INDEX_FILENAME = "memory-index.db"
PROFILE_CHAR_CAP = 16_000
PREFETCH_KEEP = 3
RERANK_CANDIDATES = 10
ROUTE_CANDIDATES = 50
# Fallback routing: attach to the best hit only at this normalised score (see index.py): a note
# ranked first by both FTS and vectors, or an alias match plus a top FTS hit.
ROUTE_MIN_SCORE = 0.9
STATE_CHARS = 4000
DEFAULT_TIMEOUT_S = 1.5
NO_PROFILE_LINE = "USER PROFILE: no profile yet."
_PROFILE_SEP = "═" * 46
_AUTO = object()


MEMORY_SEARCH_SCHEMA = {
    "name": "memory_search",
    "description": (
        "Search the user's memory notes (people, organizations, preferences, past conversations). "
        "Only the profile is in your prompt; everything else is found here. Search BEFORE saying you "
        "don't know or don't remember something about the user, and before asking them to repeat it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to look for, in plain words (a name, a topic, a question)."},
            "type": {"type": "string", "enum": list(notes_mod.NOTE_TYPES), "description": "Only notes of this type."},
            "limit": {"type": "integer", "description": "Maximum hits (default 5)."},
        },
        "required": ["query"],
    },
}

MEMORY_READ_SCHEMA = {
    "name": "memory_read",
    "description": "Read one memory note in full, by the id memory_search returned (e.g. people/ana-costa). "
                   "Also lists the ids it links to.",
    "parameters": {
        "type": "object",
        "properties": {"id": {"type": "string", "description": "Note id, e.g. people/ana-costa."}},
        "required": ["id"],
    },
}

MEMORY_NOTE_SCHEMA = {
    "name": "memory_note",
    "description": (
        "Record one durable fact about the user or their world (a preference, a person, an organization, "
        "something decided in a conversation). It is filed into the right note automatically, dated and "
        "stamped with where it came from. Use this for anything worth remembering. USER.md (the profile) is "
        "generated from these notes: do not try to edit it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "fact": {"type": "string", "description": "The fact, one self-contained sentence."},
            "type_hint": {"type": "string", "enum": list(notes_mod.NOTE_TYPES),
                          "description": "The kind of note this belongs to, if a new note is needed."},
            "about": {"type": "string", "description": "Who or what the fact is about (e.g. 'Ana')."},
            "valid_until": {"type": "string", "description": "YYYY-MM-DD after which the fact stops being true."},
        },
        "required": ["fact"],
    },
}


def _load_plugin_config() -> dict:
    try:
        from hermes_cli.config import cfg_get, load_config_readonly

        return dict(cfg_get(load_config_readonly(), "memory", PROVIDER_NAME, default={}) or {})
    except Exception:
        return {}


def _today() -> str:
    return _dt.date.today().isoformat()


# -- Jev answers ---------------------------------------------------------------------------------
# The decide route returns {"answers": ... | null}; null (or no answer at all) means "use the
# fallback". The answer shapes below are read leniently; anything else counts as no answer.

def _as_yes_no(answer: Any) -> Optional[bool]:
    if isinstance(answer, bool):
        return answer
    if isinstance(answer, str):
        low = answer.strip().lower()
        return True if low in ("yes", "y", "true") else False if low in ("no", "n", "false") else None
    if isinstance(answer, dict):
        for key in ("answer", "retrieve", "value"):
            if key in answer:
                return _as_yes_no(answer[key])
    if isinstance(answer, list) and len(answer) == 1:
        return _as_yes_no(answer[0])
    return None


def _as_scores(answer: Any, ids: List[str]) -> Optional[Dict[str, float]]:
    def num(v):
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    if isinstance(answer, dict) and "scores" in answer:
        answer = answer["scores"]
    if isinstance(answer, dict):
        scores = {k: num(v) for k, v in answer.items() if k in ids}
    elif isinstance(answer, list) and answer and all(isinstance(a, dict) for a in answer):
        scores = {a.get("id"): num(a.get("score")) for a in answer if a.get("id") in ids}
    elif isinstance(answer, list) and len(answer) == len(ids):
        scores = dict(zip(ids, (num(a) for a in answer)))
    else:
        return None
    scores = {k: v for k, v in scores.items() if v is not None}
    return scores or None


def _as_choice(answer: Any) -> Optional[str]:
    if isinstance(answer, dict):
        answer = answer.get("choice", answer.get("answer"))
    if isinstance(answer, list) and len(answer) == 1:
        answer = answer[0]
    return answer.strip() if isinstance(answer, str) and answer.strip() else None


class IolloNotesProvider(MemoryProvider):
    """Typed notes + local search; the profile in every turn."""

    def __init__(self, config: Optional[dict] = None, *, embedder: Any = _AUTO):
        self._config = dict(config if config is not None else _load_plugin_config())
        self._embedder = embedder  # _AUTO = the shipped model (tests pass a fake or None)
        self._index: Optional[NoteIndex] = None
        self._home: Optional[Path] = None
        self._session_id = ""
        self._write_lock = threading.Lock()
        self._last_recall = 0

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    def is_available(self) -> bool:
        return True  # SQLite FTS always works; vectors and Jev are optional

    # -- config ----------------------------------------------------------------------------------

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {"key": "decide_url", "description": "Jev memory decision URL (empty = local fallbacks only)", "default": ""},
            {"key": "decide_token_env", "description": "Env var holding the bearer token for decide_url", "default": ""},
            {"key": "decide_timeout_s", "description": "Seconds to wait for a Jev decision", "default": str(DEFAULT_TIMEOUT_S),
             "type": "number"},
            {"key": "device", "description": "This device in provenance: box or mac:<id>", "default": "box"},
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        from hermes_cli.config import save_config

        save_config({"memory": {PROVIDER_NAME: dict(values)}}, merge_existing=True)

    @property
    def device(self) -> str:
        return notes_mod.one_line(str(self._config.get("device") or "box")) or "box"

    @property
    def notes_dir(self) -> Path:
        return self._require_home() / NOTES_DIRNAME

    @property
    def _idx(self) -> NoteIndex:
        if self._index is None:
            raise RuntimeError("iollo_notes is not initialized")
        return self._index

    def _require_home(self) -> Path:
        if self._home is None:
            from hermes_constants import get_hermes_home

            self._home = Path(get_hermes_home())
        return self._home

    # -- lifecycle -------------------------------------------------------------------------------

    def initialize(self, session_id: str, **kwargs) -> None:
        from .embedder import load_embedder

        home = kwargs.get("hermes_home")
        if home:
            self._home = Path(home)
        home = self._require_home()
        self._session_id = session_id or ""
        embedder = load_embedder() if self._embedder is _AUTO else self._embedder
        self._index = NoteIndex(home / NOTES_DIRNAME, home / INDEX_FILENAME, embedder)
        try:
            self._idx.refresh()
        except Exception as exc:
            logger.warning("iollo_notes index refresh failed at start: %s", type(exc).__name__)

    def on_session_switch(self, new_session_id: str, **kwargs) -> None:
        self._session_id = new_session_id or self._session_id

    def shutdown(self) -> None:
        if self._index is not None:
            try:
                self._index.close()
            except Exception:
                pass
        self._index = None

    def backup_paths(self) -> List[str]:
        return []  # notes and index both live inside HERMES_HOME

    # -- the profile -----------------------------------------------------------------------------

    def system_prompt_block(self) -> str:
        path = self.notes_dir / notes_mod.PROFILE_NAME
        try:
            text = path.read_text(encoding="utf-8-sig").strip()
        except (OSError, UnicodeDecodeError):
            text = ""
        if not text:
            return NO_PROFILE_LINE
        if len(text) > PROFILE_CHAR_CAP:
            cut = text[:PROFILE_CHAR_CAP]
            text = cut[: cut.rfind("\n")] if "\n" in cut else cut
        from tools.threat_patterns import scan_for_threats

        findings = scan_for_threats(text, scope="strict")
        if findings:
            logger.warning("iollo_notes USER.md blocked at load time: %s", ", ".join(findings))
            text = (f"[BLOCKED: USER.md contained threat pattern(s): {', '.join(findings)}. "
                    "Removed from the system prompt until the box regenerates it.]")
        from tools.memory_tool_store import MEMORY_BLOCK_HEADERS

        return f"{_PROFILE_SEP}\n{MEMORY_BLOCK_HEADERS['user']}\n{_PROFILE_SEP}\n{text}"

    # -- Jev -------------------------------------------------------------------------------------

    def _decide(self, decision: str, state: str, candidates: List[Dict[str, Any]]) -> Any:
        """The decide route's ``answers``; None when unconfigured, down, slow or undecided."""
        url = str(self._config.get("decide_url") or "").strip()
        if not url:
            return None
        try:
            timeout = float(self._config.get("decide_timeout_s") or DEFAULT_TIMEOUT_S)
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT_S
        headers = {"Content-Type": "application/json"}
        token_env = str(self._config.get("decide_token_env") or "").strip()
        token = os.environ.get(token_env, "") if token_env else ""
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body = json.dumps({"decision": decision, "state": (state or "")[:STATE_CHARS],
                           "candidates": candidates}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - policy-set URL
                payload = json.loads(response.read(256 * 1024).decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # Counted, never logged with content: note ids name people.
            logger.debug("iollo_notes decide %s unavailable: %s", decision, type(exc).__name__)
            return None
        return payload.get("answers") if isinstance(payload, dict) else None

    @staticmethod
    def _candidate(hit: Dict[str, Any]) -> Dict[str, Any]:
        return {"id": hit["id"], "type": hit["type"], "title": hit["title"], "bullets": hit["bullets"]}

    # -- recall ----------------------------------------------------------------------------------

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        self._last_recall = 0
        if self._index is None or is_trivial_prompt(query):
            return ""
        if _as_yes_no(self._decide("retrieve", query, [])) is False:
            return ""
        try:
            self._idx.refresh()
            hits = self._idx.search(query, limit=RERANK_CANDIDATES)
        except Exception as exc:
            logger.warning("iollo_notes prefetch search failed: %s", type(exc).__name__)
            return ""
        if not hits:
            return ""
        ids = [h["id"] for h in hits]
        scores = _as_scores(self._decide("rerank", query, [self._candidate(h) for h in hits]), ids)
        if scores:
            order = {note_id: i for i, note_id in enumerate(ids)}
            hits = sorted(hits, key=lambda h: (-scores.get(h["id"], float("-inf")), order[h["id"]]))
        kept = hits[:PREFETCH_KEEP]
        self._last_recall = len(kept)
        return "## Memory notes (use memory_read for the whole note)\n" + "\n".join(
            f"- {h['id']} ({h['type']}): {h['title']}" + "".join(f"\n  - {b}" for b in h["bullets"]) for h in kept
        )

    def recall_status(self) -> Optional[RecallStatus]:
        return RecallStatus("Iollo notes", self._last_recall) if self._last_recall else None

    # -- tools -----------------------------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [MEMORY_SEARCH_SCHEMA, MEMORY_READ_SCHEMA, MEMORY_NOTE_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        handler = {"memory_search": self._tool_search, "memory_read": self._tool_read,
                   "memory_note": self._tool_note}.get(tool_name)
        if handler is None:
            return tool_error(f"Unknown tool: {tool_name}")
        if self._index is None:
            return tool_error("iollo_notes is not initialized")
        try:
            return handler(args or {})
        except (KeyError, ValueError, notes_mod.NoteError) as exc:
            return tool_error(str(exc))
        except Exception as exc:
            logger.warning("iollo_notes %s failed: %s", tool_name, type(exc).__name__)
            return tool_error(f"{tool_name} failed: {type(exc).__name__}")

    def _tool_search(self, args: Dict[str, Any]) -> str:
        query = str(args.get("query") or "").strip()
        if not query:
            raise ValueError("query is required")
        note_type = args.get("type") or None
        if note_type and note_type not in notes_mod.TYPE_FOLDERS:
            raise ValueError(f"type must be one of {', '.join(notes_mod.NOTE_TYPES)}")
        try:
            limit = max(1, min(int(args.get("limit") or 5), 20))
        except (TypeError, ValueError):
            limit = 5
        self._idx.refresh()
        hits = self._idx.search(query, note_type=note_type, limit=limit)
        return tool_result({"results": hits, "count": len(hits)})

    def _tool_read(self, args: Dict[str, Any]) -> str:
        note_id = str(args.get("id") or "").strip().removesuffix(".md")
        if notes_mod.path_errors(notes_mod.note_path(note_id)):
            raise ValueError("not a note id (expected e.g. people/ana-costa)")
        path = self.notes_dir / notes_mod.note_path(note_id)
        if not path.is_file() or path.is_symlink():
            return tool_error(f"No note {note_id}")
        text = path.read_text(encoding="utf-8")
        return tool_result({"id": note_id, "content": text, "links": notes_mod.links(text)})

    def _tool_note(self, args: Dict[str, Any]) -> str:
        return tool_result(self.record_fact(
            str(args.get("fact") or ""), type_hint=args.get("type_hint") or None,
            about=args.get("about") or None, valid_until=args.get("valid_until") or None,
        ))

    # -- writing ---------------------------------------------------------------------------------

    def record_fact(self, fact: str, *, type_hint: Optional[str] = None, about: Optional[str] = None,
                    valid_until: Optional[str] = None, provenance: Optional[str] = None) -> Dict[str, Any]:
        """Route one fact to a note (Jev, else the best hit, else a new note) and append it."""
        fact = notes_mod.one_line(fact)
        about = notes_mod.one_line(about or "") or None
        if not fact:
            raise ValueError("fact is required")
        if type_hint and type_hint not in notes_mod.TYPE_FOLDERS:
            raise ValueError(f"type_hint must be one of {', '.join(notes_mod.NOTE_TYPES)}")
        from tools.memory_tool_store import _scan_memory_content

        if scan_error := _scan_memory_content(f"{about or ''} {fact}"):
            raise ValueError(scan_error)
        if valid_until:
            _dt.date.fromisoformat(valid_until)  # ValueError on a bad date
        self._idx.refresh()
        query = f"{about} {fact}" if about else fact
        hits = self._idx.search(query, limit=ROUTE_CANDIDATES)
        target_id, new_type = self._route(fact if not about else f"About {about}: {fact}", hits)
        if target_id is None:
            best = hits[0] if hits else None
            if best and best["score"] >= ROUTE_MIN_SCORE and (not type_hint or best["type"] == type_hint):
                target_id = best["id"]
            else:
                new_type = new_type or type_hint or "preference"
        if provenance is None:
            provenance = self.device + (f", conversation {self._session_id}" if self._session_id else "")
        note_id, created = self._append(target_id, new_type, fact, about, valid_until, provenance)
        self._idx.refresh()
        return {"success": True, "id": note_id, "created": created}

    def _route(self, state: str, hits: List[Dict[str, Any]]):
        answer = _as_choice(self._decide("route", state, [self._candidate(h) for h in hits]))
        if answer is None:
            return None, None
        if answer.startswith("new:") and answer[4:] in notes_mod.TYPE_FOLDERS:
            return None, answer[4:]
        if answer in {h["id"] for h in hits}:
            return answer, None
        return None, None  # Jev may only pick among the options we built

    def _append(self, target_id: Optional[str], new_type: Optional[str], fact: str, about: Optional[str],
                valid_until: Optional[str], provenance: str):
        from tools.memory_tool_store import MemoryStore

        today = _today()
        root = self.notes_dir
        with self._write_lock, MemoryStore._file_lock(root):  # lock file: $HERMES_HOME/memory-notes.lock
            created = False
            if target_id is None:
                assert new_type is not None
                title = about or " ".join(fact.split()[:8]).rstrip(".,;:")
                target_id = f"{notes_mod.TYPE_FOLDERS[new_type]}/{notes_mod.slugify(title)}"
            path = root / notes_mod.note_path(target_id)
            if path.is_file():
                note = notes_mod.parse(path.read_text(encoding="utf-8"))
            else:
                kind = notes_mod.FOLDER_TYPES[target_id.split("/", 1)[0]]
                title = about or " ".join(fact.split()[:8]).rstrip(".,;:")
                note = notes_mod.new_note(target_id, kind, title, [about] if about and about != title else [], today)
                created = True
            notes_mod.append_fact(note, fact, today, provenance, valid_until)
            text = notes_mod.render(note)
            if problems := notes_mod.validate(notes_mod.note_path(target_id), text):
                raise ValueError(f"note {target_id} would be invalid ({'; '.join(problems)}); "
                                 "the box consolidates full notes, try again later")
            self._write_atomic(path, text)
        return target_id, created

    def _write_atomic(self, path: Path, text: str) -> None:
        """Temp file outside the git folder, fsync, then rename into place with mode 0644."""
        tmp_dir = self._require_home() / (NOTES_DIRNAME + ".tmp")
        tmp_dir.mkdir(parents=True, exist_ok=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=tmp_dir, suffix=".md")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- legacy memory tool ----------------------------------------------------------------------

    def on_memory_write(self, action: str, target: str, content: str,
                        metadata: Optional[Dict[str, Any]] = None) -> None:
        """A built-in ``memory`` tool write (older profiles) becomes a memory_note."""
        if action not in ("add", "replace") or not (content or "").strip() or self._index is None:
            return
        try:
            self.record_fact(content, type_hint="preference" if target == "user" else "conversation")
        except Exception as exc:
            logger.debug("iollo_notes legacy memory write not recorded: %s", type(exc).__name__)


def register(ctx) -> None:
    """Register the iollo_notes memory provider with the plugin system."""
    ctx.register_memory_provider(IolloNotesProvider())

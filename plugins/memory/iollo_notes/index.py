"""Local search index over the typed notes: ``$HERMES_HOME/memory-index.db``, never synced.

1. SQLite FTS5 (porter) over title, aliases and bullets.
2. One embedding per note in a sqlite-vec table; if the extension cannot load, brute-force cosine
   over the stored vectors in Python (the sync layer caps a tree at 10,000 notes) and log
   ``vec_fallback`` once.
3. Search = reciprocal-rank fusion of both lists, then one hop: notes whose aliases appear in the
   query and notes ``[[linked]]`` from the top hits join with a lower weight.

The index is rebuilt incrementally from file size/mtime and content hashes (``refresh``).
"""

from __future__ import annotations

import array
import hashlib
import json
import logging
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import notes as notes_mod

logger = logging.getLogger(__name__)

RRF_K = 60
CANDIDATES = 50
TOP_LINK_SOURCES = 3
# Scores are normalised so a note ranked first by both FTS and vectors scores 1.0, whether or not
# the vector side is available. One hop: an alias named in the query adds ALIAS_BONUS; a note linked
# from a top hit scores at least LINK_WEIGHT x that hit's score, so it never outranks its source.
ALIAS_BONUS = 0.5
LINK_WEIGHT = 0.5
EMBED_CHARS = 2000  # the tokenizer truncates to the model window; this just bounds the work

_STOPWORDS = frozenset(
    "a an and are about as at be but by did do does for from had has have he her his how i if in is it "
    "its me my of on or our she so that the their them they this to was we were what when where which "
    "who why will with you your".split()
)
_WORD_RE = re.compile(r"\w+", re.UNICODE)
_vec_fallback_logged = False


def _log_vec_fallback(reason: str) -> None:
    global _vec_fallback_logged
    if not _vec_fallback_logged:
        _vec_fallback_logged = True
        logger.warning("iollo_notes vec_fallback: %s; using brute-force cosine", reason)


def query_terms(text: str) -> List[str]:
    return [w for w in _WORD_RE.findall((text or "").lower()) if w not in _STOPWORDS]


def _stem(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def _pack(vec: Sequence[float]) -> bytes:
    return array.array("f", vec).tobytes()


def _unpack(blob: bytes) -> array.array:
    out = array.array("f")
    out.frombytes(blob)
    return out


def _embed_text(note: notes_mod.Note) -> str:
    parts = [note.title, ", ".join(note.aliases)] + [f.text for f in note.facts]
    return "\n".join(p for p in parts if p)[:EMBED_CHARS]


def _bullet_line(fact: notes_mod.Fact) -> str:
    return fact.render()[2:]


class NoteIndex:
    """FTS + vector index of one notes directory. Thread-safe; one SQLite connection."""

    def __init__(self, notes_dir: Path, db_path: Path, embedder=None, *, use_vec: bool = True):
        self.notes_dir = Path(notes_dir)
        self.db_path = Path(db_path)
        self.embedder = embedder
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._vec = bool(embedder) and use_vec and self._load_vec()
        self._matrix: Optional[Dict[int, array.array]] = None
        self._aliases: Optional[Dict[str, List[str]]] = None
        self._create_schema()

    # -- setup ---------------------------------------------------------------------------------

    def _load_vec(self) -> bool:
        try:
            import sqlite_vec

            self._conn.enable_load_extension(True)
            try:
                sqlite_vec.load(self._conn)
            finally:
                self._conn.enable_load_extension(False)
            return True
        except Exception as exc:
            _log_vec_fallback(type(exc).__name__)
            return False

    def _embedder_key(self) -> str:
        if not self.embedder:
            return ""
        return f"{getattr(self.embedder, 'name', type(self.embedder).__name__)}:{self.embedder.dim}"

    def _create_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS notes (
                    rowid INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, type TEXT NOT NULL,
                    title TEXT NOT NULL, aliases TEXT NOT NULL, bullets TEXT NOT NULL, links TEXT NOT NULL,
                    size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, sha TEXT NOT NULL, emb BLOB
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
                    title, aliases, bullets, tokenize='porter unicode61'
                );
                """
            )
            row = self._conn.execute("SELECT value FROM meta WHERE key='embedder'").fetchone()
            key = self._embedder_key()
            if (row["value"] if row else "") != key:
                # Different (or no) model: every stored vector is meaningless now.
                self._conn.execute("UPDATE notes SET emb = NULL")
                self._conn.execute("DROP TABLE IF EXISTS notes_vec")
                self._conn.execute("INSERT OR REPLACE INTO meta VALUES ('embedder', ?)", (key,))
            if self._vec:
                self._conn.execute(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS notes_vec USING vec0(embedding float[{int(self.embedder.dim)}])"
                )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- refresh -------------------------------------------------------------------------------

    def refresh(self) -> Dict[str, int]:
        """Bring the index up to date with the notes directory; returns counts of changes."""
        with self._lock:
            known = {r["id"]: r for r in self._conn.execute("SELECT rowid, id, size, mtime_ns, sha, emb FROM notes")}
            seen, changed, touched, invalid = set(), [], 0, 0
            root = self.notes_dir
            files = list(notes_mod.iter_note_files(root)) if root.is_dir() else []
            with self._conn:
                for rel in files:
                    note_id = rel[:-3]
                    path = root / rel
                    try:
                        st = path.stat()
                    except OSError:
                        continue
                    row = known.get(note_id)
                    if row and row["size"] == st.st_size and row["mtime_ns"] == st.st_mtime_ns:
                        seen.add(note_id)
                        continue
                    try:
                        raw = path.read_bytes()
                    except OSError:
                        continue
                    sha = hashlib.sha256(raw).hexdigest()
                    if row and row["sha"] == sha:
                        self._conn.execute("UPDATE notes SET size=?, mtime_ns=? WHERE rowid=?",
                                           (st.st_size, st.st_mtime_ns, row["rowid"]))
                        seen.add(note_id)
                        continue
                    if notes_mod.validate(rel, raw):
                        invalid += 1  # never log note content or names
                        continue
                    note = notes_mod.parse(raw.decode("utf-8"))
                    self._upsert(note, st, sha, row)
                    seen.add(note_id)
                    changed.append(note)
                    touched += 1
                removed = [known[i] for i in known if i not in seen]
                for row in removed:
                    self._delete(row["rowid"])
                missing = [] if not self.embedder else [
                    r["id"] for r in known.values() if r["id"] in seen and r["emb"] is None
                    and r["id"] not in {n.id for n in changed}
                ]
                self._embed_notes(changed + [self._load(i) for i in missing])
            if touched or removed:
                self._matrix = self._aliases = None
            if invalid:
                logger.info("iollo_notes skipped %d invalid note file(s)", invalid)
            return {"changed": touched, "removed": len(removed), "invalid": invalid}

    def _load(self, note_id: str) -> notes_mod.Note:
        return notes_mod.parse((self.notes_dir / notes_mod.note_path(note_id)).read_text(encoding="utf-8"))

    def _upsert(self, note: notes_mod.Note, st, sha: str, row) -> None:
        values = (note.type, note.title or note.id, json.dumps(note.aliases, ensure_ascii=False),
                  json.dumps([_bullet_line(f) for f in note.facts], ensure_ascii=False),
                  json.dumps(notes_mod.links(note)), st.st_size, st.st_mtime_ns, sha)
        if row:
            rowid = row["rowid"]
            self._conn.execute("UPDATE notes SET type=?, title=?, aliases=?, bullets=?, links=?, size=?, "
                               "mtime_ns=?, sha=?, emb=NULL WHERE rowid=?", (*values, rowid))
            self._conn.execute("DELETE FROM notes_fts WHERE rowid=?", (rowid,))
        else:
            rowid = self._conn.execute("INSERT INTO notes (id, type, title, aliases, bullets, links, size, mtime_ns, sha)"
                                       " VALUES (?,?,?,?,?,?,?,?,?)", (note.id, *values)).lastrowid
        self._conn.execute("INSERT INTO notes_fts (rowid, title, aliases, bullets) VALUES (?,?,?,?)",
                           (rowid, note.title or note.id, " ".join(note.aliases), "\n".join(f.text for f in note.facts)))

    def _delete(self, rowid: int) -> None:
        self._conn.execute("DELETE FROM notes WHERE rowid=?", (rowid,))
        self._conn.execute("DELETE FROM notes_fts WHERE rowid=?", (rowid,))
        if self._vec:
            self._conn.execute("DELETE FROM notes_vec WHERE rowid=?", (rowid,))

    def _embed_notes(self, items: List[notes_mod.Note]) -> None:
        if not self.embedder or not items:
            return
        try:
            vectors = self.embedder.embed([_embed_text(n) for n in items])
        except Exception as exc:
            logger.warning("iollo_notes embedding failed (%s); notes stay FTS-only until the next refresh",
                           type(exc).__name__)
            return
        for note, vec in zip(items, vectors):
            blob = _pack(vec)
            rowid = self._conn.execute("SELECT rowid FROM notes WHERE id=?", (note.id,)).fetchone()["rowid"]
            self._conn.execute("UPDATE notes SET emb=? WHERE rowid=?", (blob, rowid))
            if self._vec:
                self._conn.execute("DELETE FROM notes_vec WHERE rowid=?", (rowid,))
                self._conn.execute("INSERT INTO notes_vec (rowid, embedding) VALUES (?, ?)", (rowid, blob))

    # -- queries -------------------------------------------------------------------------------

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]

    def has(self, note_id: str) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1 FROM notes WHERE id=?", (note_id,)).fetchone() is not None

    def _fts(self, terms: List[str], limit: int) -> List[int]:
        if not terms:
            return []
        match = " OR ".join('"' + t.replace('"', "") + '"' for t in dict.fromkeys(terms))
        try:
            rows = self._conn.execute(
                "SELECT rowid FROM notes_fts WHERE notes_fts MATCH ? ORDER BY bm25(notes_fts, 5.0, 10.0, 1.0) LIMIT ?",
                (match, limit)).fetchall()
        except sqlite3.OperationalError:
            return []
        return [r[0] for r in rows]

    def _vector(self, query: str, limit: int) -> List[int]:
        if not self.embedder or not query.strip():
            return []
        try:
            qvec = self.embedder.embed([query])[0]
        except Exception as exc:
            logger.warning("iollo_notes query embedding failed (%s)", type(exc).__name__)
            return []
        if self._vec:
            try:
                rows = self._conn.execute(
                    "SELECT rowid FROM notes_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                    (_pack(qvec), limit)).fetchall()
                return [r[0] for r in rows]
            except sqlite3.OperationalError as exc:
                _log_vec_fallback(str(exc)[:60])
                self._vec = False
        if self._matrix is None:
            self._matrix = {r[0]: _unpack(r[1]) for r in
                            self._conn.execute("SELECT rowid, emb FROM notes WHERE emb IS NOT NULL")}
        scored = sorted(((sum(a * b for a, b in zip(qvec, vec)), rowid) for rowid, vec in self._matrix.items()),
                        reverse=True)
        return [rowid for _, rowid in scored[:limit]]

    def _alias_map(self) -> Dict[str, List[str]]:
        if self._aliases is None:
            amap: Dict[str, List[str]] = {}
            for row in self._conn.execute("SELECT id, title, aliases FROM notes"):
                for alias in [row["title"], *json.loads(row["aliases"])]:
                    key = " ".join(_WORD_RE.findall(alias.lower()))
                    if key and key not in _STOPWORDS:
                        amap.setdefault(key, []).append(row["id"])
            self._aliases = amap
        return self._aliases

    def search(self, query: str, *, note_type: Optional[str] = None, limit: int = 5,
               candidates: int = CANDIDATES, hop: bool = True) -> List[Dict[str, Any]]:
        """Fused hits: ``{id, type, title, score, bullets}`` (bullets = the three best-matching)."""
        limit = max(1, min(int(limit or 5), candidates))
        with self._lock:
            terms = query_terms(query)
            unit = 1.0 / (RRF_K + 1)
            scores: Dict[int, float] = {}
            for ranked in (self._fts(terms, candidates), self._vector(query, candidates)):
                for rank, rowid in enumerate(ranked):
                    scores[rowid] = scores.get(rowid, 0.0) + 1.0 / (RRF_K + rank + 1) / (2 * unit)
            rows = {r["rowid"]: r for r in self._conn.execute(
                f"SELECT rowid, id, type, title, bullets, links FROM notes WHERE rowid IN ({','.join('?' * len(scores))})",
                list(scores))} if scores else {}
            by_id = {r["id"]: rid for rid, r in rows.items()}
            if hop:
                phrase = " " + " ".join(_WORD_RE.findall((query or "").lower())) + " "
                for alias, ids in self._alias_map().items():
                    if f" {alias} " in phrase:
                        for note_id in ids:
                            self._bump(note_id, ALIAS_BONUS, scores, rows, by_id)
                top = sorted(scores, key=scores.get, reverse=True)[:TOP_LINK_SOURCES]
                for rowid in top:
                    for linked in json.loads(rows[rowid]["links"]):
                        if linked != rows[rowid]["id"]:
                            self._bump(linked, LINK_WEIGHT * scores[rowid], scores, rows, by_id, at_least=True)
            hits = []
            for rowid, score in sorted(scores.items(), key=lambda kv: (-kv[1], rows[kv[0]]["id"])):
                row = rows[rowid]
                if note_type and row["type"] != note_type:
                    continue
                hits.append({"id": row["id"], "type": row["type"], "title": row["title"], "score": round(score, 4),
                             "bullets": self._best_bullets(json.loads(row["bullets"]), terms)})
                if len(hits) >= limit:
                    break
            return hits

    def _bump(self, note_id: str, bonus: float, scores, rows, by_id, *, at_least: bool = False) -> None:
        """Add ``bonus`` to a note's score (joining it if absent); ``at_least`` raises it to ``bonus`` instead."""
        rowid = by_id.get(note_id)
        if rowid is None:
            row = self._conn.execute("SELECT rowid, id, type, title, bullets, links FROM notes WHERE id=?",
                                     (note_id,)).fetchone()
            if row is None:
                return
            rowid = row["rowid"]
            rows[rowid], by_id[note_id] = row, rowid
        current = scores.get(rowid, 0.0)
        scores[rowid] = max(current, bonus) if at_least else current + bonus

    @staticmethod
    def _best_bullets(bullets: List[str], terms: List[str], n: int = 3) -> List[str]:
        stems = {_stem(t) for t in terms}
        ranked = sorted(
            range(len(bullets)),
            key=lambda i: (len(stems & {_stem(w) for w in _WORD_RE.findall(bullets[i].lower())}), i),
            reverse=True,
        )
        return [bullets[i] for i in ranked[:n]]

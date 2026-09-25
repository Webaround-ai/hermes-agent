"""Local envelope persistence, also used by the box sync service."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

ENVELOPE_LIMIT = 256 * 1024


class EnvelopeStore:
    """Reply envelopes (brief 033), one row per message, in the same box-local state.db.

    The runtime builds each reply and stores it here; the control plane keeps no copy. A write keeps the
    highest ``rev`` per ``message_id``. ``hermes_message_id`` links a final reply to its transcript row
    (``hermes:<session>:<message>``) so history can serve the envelope next to the text.
    """

    def __init__(self, path: Path):
        self.path = path
        with closing(self.connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS iollo_envelopes (
                message_id TEXT PRIMARY KEY, session_key TEXT NOT NULL, hermes_message_id TEXT,
                rev INTEGER NOT NULL, envelope_json TEXT NOT NULL, created_at TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS iollo_envelopes_session ON iollo_envelopes (session_key, created_at)")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _latest_reply(db, session_key):
        """The newest assistant transcript row of this conversation key not yet bound to an envelope."""
        try:
            row = db.execute(
                "SELECT m.id, m.session_id FROM messages m JOIN sessions s ON m.session_id = s.id "
                "WHERE s.session_key = ? AND m.role = 'assistant' AND NOT EXISTS (SELECT 1 FROM iollo_envelopes e "
                "WHERE e.hermes_message_id = 'hermes:' || m.session_id || ':' || m.id) "
                "ORDER BY m.timestamp DESC, m.id DESC LIMIT 1", (session_key,)).fetchone()
        except sqlite3.OperationalError:
            return None  # no Hermes transcript schema (yet): the envelope is stored unbound
        return f"hermes:{row['session_id']}:{row['id']}" if row else None

    def put(self, message_id, session_key, envelope, *, hermes_message_id=None, bind_latest=False):
        if not isinstance(message_id, str) or not 0 < len(message_id) <= 128:
            raise ValueError("message_id")
        if not isinstance(session_key, str) or not 0 < len(session_key) <= 256:
            raise ValueError("session_key")
        if not isinstance(envelope, dict) or envelope.get("message_id") != message_id:
            raise ValueError("envelope")
        rev = envelope.get("rev")
        if type(rev) is not int or rev < 1 or envelope.get("envelope_v") != 1:
            raise ValueError("envelope")
        if hermes_message_id is not None and (not isinstance(hermes_message_id, str) or len(hermes_message_id) > 300):
            raise ValueError("hermes_message_id")
        body = json.dumps(envelope, separators=(",", ":"), sort_keys=True)
        if len(body.encode()) > ENVELOPE_LIMIT:
            raise ValueError("envelope too large")
        created = str(envelope.get("created_at") or "")
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM iollo_envelopes WHERE message_id=?", (message_id,)).fetchone()
            if old is not None and old["session_key"] != session_key:
                raise ValueError("message id belongs to another conversation")
            if old is not None and old["rev"] >= rev:
                return {"message_id": message_id, "rev": old["rev"], "stored": False,
                        "hermes_message_id": old["hermes_message_id"]}
            bound = hermes_message_id or (old["hermes_message_id"] if old is not None else None)
            if bound is None and bind_latest:
                bound = self._latest_reply(db, session_key)
            db.execute("INSERT INTO iollo_envelopes VALUES (?,?,?,?,?,?) ON CONFLICT(message_id) DO UPDATE SET "
                       "hermes_message_id=excluded.hermes_message_id, rev=excluded.rev, "
                       "envelope_json=excluded.envelope_json",
                       (message_id, session_key, bound, rev, body, old["created_at"] if old is not None else created))
        return {"message_id": message_id, "rev": rev, "stored": True, "hermes_message_id": bound}

    def list(self, session_keys, since=None, limit=500):
        keys = [k for k in dict.fromkeys(session_keys) if isinstance(k, str) and 0 < len(k) <= 256]
        if not keys or len(keys) > 64:
            raise ValueError("session_key")
        query = (f"SELECT * FROM iollo_envelopes WHERE session_key IN ({','.join('?' * len(keys))})"
                 + (" AND created_at > ?" if since else "") + " ORDER BY created_at, message_id LIMIT ?")
        with closing(self.connect()) as db:
            rows = db.execute(query, (*keys, *([since] if since else []), max(1, min(int(limit), 1000)))).fetchall()
        return [{"message_id": r["message_id"], "session_key": r["session_key"],
                 "hermes_message_id": r["hermes_message_id"], "rev": r["rev"], "created_at": r["created_at"],
                 "envelope": json.loads(r["envelope_json"])} for r in rows]

    def for_run(self, run_id):
        """Used only after the runs API has authenticated the caller and checked ownership."""
        with closing(self.connect()) as db:
            row = db.execute("SELECT envelope_json FROM iollo_envelopes "
                             "WHERE json_extract(envelope_json, '$.run_id') = ? ORDER BY rev DESC LIMIT 1",
                             (run_id,)).fetchone()
        return json.loads(row[0]) if row else None


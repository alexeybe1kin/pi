"""Session state and execution history. Pi holds nothing else.

Append-only is enforced here rather than promised in a docstring: there is no
runtime function that updates or deletes a message, and the schema forbids it
with triggers. The separate offline owner command can redact content atomically,
leaving identifiers and a receipt; forgotten sessions never resume. Models bind
reasoning blocks to the producing model and reject
edited history, and by the time that surfaces the offending code is everywhere.

Transcripts live here and only here. What crosses into MemoryGate is derived
evidence, never these rows - see ADR-0002. Message ids are therefore stable and
never reused: evidence cites them, and a citation that can be re-pointed is not
a citation.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import memory_store
from .access import MaintenanceRequired, acquire

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,
    parent_id    TEXT REFERENCES sessions(id),
    title        TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'open',   -- open | forked | closed
    created_at   REAL NOT NULL,
    closed_at    REAL,
    summary      TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id           TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL REFERENCES sessions(id),
    seq          INTEGER NOT NULL,
    role         TEXT NOT NULL,                  -- user | assistant | system | tool
    content      TEXT NOT NULL,                  -- JSON
    created_at   REAL NOT NULL,
    UNIQUE (session_id, seq)
);

CREATE TABLE IF NOT EXISTS turns (
    id           TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL REFERENCES sessions(id),
    status       TEXT NOT NULL,                  -- running | complete | interrupted | failed
                                                 --  | awaiting_approval | acted_no_reply
    provider     TEXT,
    model        TEXT,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cached_tokens INTEGER,
    cost_usd      REAL,
    latency_ms    INTEGER,
    route_tier   TEXT,
    route_reason TEXT,
    -- The parked action, kept whole so the retry is the same action the owner
    -- was shown. Reconstructing it later from anything else would be a
    -- different action wearing the same approval.
    approval_request_id TEXT,
    approval_tool_id    TEXT,
    approval_args       TEXT,
    approval_expires_at TEXT,
    -- The owner's own words that started this turn, kept beside the action they
    -- led to. An approval that shows only what Conker wants to do cannot be
    -- judged: the question is whether it follows from what was actually asked.
    -- Anything ingested since is untrusted, so the *owner's* text is the only
    -- honest anchor to compare against.
    approval_intent     TEXT,
    -- Whether this turn changed the world. A different fact from whether it
    -- produced a reply, and the one the owner most needs to be true: a tool
    -- that ran and a model that then failed to narrate it is not a turn where
    -- nothing happened. Written before the narration is attempted, so a crash
    -- in between cannot lose it.
    acted        INTEGER NOT NULL DEFAULT 0,
    started_at   REAL NOT NULL,
    ended_at     REAL,
    detail       TEXT
);

CREATE INDEX IF NOT EXISTS messages_by_session ON messages(session_id, seq);
CREATE INDEX IF NOT EXISTS turns_by_session ON turns(session_id, started_at);

-- Append-only, enforced by the database rather than by convention. A caller
-- that reaches past this module still cannot rewrite what was said.
CREATE TRIGGER IF NOT EXISTS messages_are_immutable
BEFORE UPDATE ON messages
BEGIN
    SELECT RAISE(ABORT, 'messages are append-only');
END;

CREATE TRIGGER IF NOT EXISTS messages_are_permanent
BEFORE DELETE ON messages
BEGIN
    SELECT RAISE(ABORT, 'messages are append-only');
END;
"""

# A receipt contains identifiers and time, never text or a hash of deleted text.
FORGETTING_SCHEMA = """
CREATE TABLE IF NOT EXISTS forgetting_receipts (
    id TEXT PRIMARY KEY,
    root_session_id TEXT NOT NULL REFERENCES sessions(id),
    forgotten_at REAL NOT NULL,
    confirmation TEXT NOT NULL,
    message_count INTEGER NOT NULL,
    turn_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS forgotten_sessions (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id),
    receipt_id TEXT NOT NULL REFERENCES forgetting_receipts(id)
);
CREATE TABLE IF NOT EXISTS forgetting_maintenance (
    id INTEGER PRIMARY KEY CHECK (id=1),
    root_session_id TEXT NOT NULL,
    confirmation TEXT NOT NULL
);
"""

for _table in ("forgetting_receipts", "forgotten_sessions"):
    _key = "id" if _table == "forgetting_receipts" else "session_id"
    # SQLite REPLACE can delete without firing DELETE triggers when recursive
    # triggers are off. Reject duplicate identities before that path is taken.
    FORGETTING_SCHEMA += f"""
CREATE TRIGGER IF NOT EXISTS {_table}_no_replace
BEFORE INSERT ON {_table}
WHEN EXISTS (SELECT 1 FROM {_table} WHERE {_key}=NEW.{_key})
BEGIN SELECT RAISE(ABORT, 'forgetting receipts are immutable'); END;
"""
    for _operation in ("UPDATE", "DELETE"):
        FORGETTING_SCHEMA += f"""
CREATE TRIGGER IF NOT EXISTS {_table}_no_{_operation.lower()}
BEFORE {_operation} ON {_table}
BEGIN SELECT RAISE(ABORT, 'forgetting receipts are immutable'); END;
"""

for _table in ("sessions", "messages", "turns"):
    _key = "id" if _table == "sessions" else "session_id"
    for _operation in ("UPDATE", "DELETE"):
        # The existing message triggers remain unconditional, including after forgetting.
        if _table == "messages":
            continue
        FORGETTING_SCHEMA += f"""
CREATE TRIGGER IF NOT EXISTS {_table}_forgotten_no_{_operation.lower()}
BEFORE {_operation} ON {_table}
WHEN EXISTS (SELECT 1 FROM forgotten_sessions WHERE session_id=OLD.{_key})
BEGIN SELECT RAISE(ABORT, 'session is forgotten'); END;
"""
    _parent = "parent_id" if _table == "sessions" else "session_id"
    FORGETTING_SCHEMA += f"""
CREATE TRIGGER IF NOT EXISTS {_table}_forgotten_no_insert
BEFORE INSERT ON {_table}
WHEN EXISTS (SELECT 1 FROM forgotten_sessions WHERE session_id=NEW.{_parent})
BEGIN SELECT RAISE(ABORT, 'session is forgotten'); END;
"""

FORGETTING_SCHEMA += """
CREATE TRIGGER IF NOT EXISTS messages_no_replace
BEFORE INSERT ON messages
WHEN EXISTS (SELECT 1 FROM messages WHERE id=NEW.id
             OR (session_id=NEW.session_id AND seq=NEW.seq))
BEGIN SELECT RAISE(ABORT, 'messages are append-only'); END;

CREATE TRIGGER IF NOT EXISTS sessions_forgotten_no_replace
BEFORE INSERT ON sessions
WHEN EXISTS (SELECT 1 FROM forgotten_sessions WHERE session_id=NEW.id)
BEGIN SELECT RAISE(ABORT, 'session is forgotten'); END;

CREATE TRIGGER IF NOT EXISTS turns_forgotten_no_replace
BEFORE INSERT ON turns
WHEN EXISTS (SELECT 1 FROM turns t JOIN forgotten_sessions f ON f.session_id=t.session_id
             WHERE t.id=NEW.id)
BEGIN SELECT RAISE(ABORT, 'session is forgotten'); END;
"""

MESSAGE_LOOKUP = """
SELECT m.*, r.id AS receipt_id, r.forgotten_at
FROM messages m
LEFT JOIN forgotten_sessions f ON f.session_id=m.session_id
LEFT JOIN forgetting_receipts r ON r.id=f.receipt_id
"""


def _message(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["content"] = json.loads(item["content"])
    if item["receipt_id"] is None:
        del item["receipt_id"], item["forgotten_at"]
    else:
        item["content_status"] = "forgotten"
    return item


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lease = acquire(self.path)
        self._release = weakref.finalize(self, lease.close)
        try:
            with self._connect() as db:
                if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='forgetting_maintenance'"
                ).fetchone() and db.execute("SELECT 1 FROM forgetting_maintenance").fetchone():
                    raise MaintenanceRequired(
                        "Forgetting cleanup is unfinished. Stop Pi and rerun the same "
                        "forgetting command before starting it."
                    )
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA foreign_keys=ON")
                db.executescript(SCHEMA)
                self._migrate(db)
                db.executescript(FORGETTING_SCHEMA)
                db.executescript(memory_store.SCHEMA)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """Release the lifetime lease only after all turns have stopped."""
        self._release()

    @staticmethod
    def _migrate(db: sqlite3.Connection) -> None:
        """Add columns a newer Pi expects to a database an older one created.

        CREATE TABLE IF NOT EXISTS silently does nothing when the table already
        exists, so a new column would be missing on every install that ever ran
        an earlier version - and the failure would appear as a write error long
        after the upgrade.
        """
        have = {row["name"] for row in db.execute("PRAGMA table_info(turns)")}
        for column in ("route_tier", "route_reason", "approval_request_id",
                       "approval_tool_id", "approval_args", "approval_expires_at",
                       "approval_intent"):
            if column not in have:
                db.execute(f"ALTER TABLE turns ADD COLUMN {column} TEXT")
        if "acted" not in have:
            db.execute("ALTER TABLE turns ADD COLUMN acted INTEGER NOT NULL DEFAULT 0")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if not self._release.alive:
            raise RuntimeError("Store is closed; create a new Store before using it")
        db = sqlite3.connect(self.path, isolation_level=None, timeout=10.0)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            yield db
        finally:
            db.close()

    # --- sessions ---------------------------------------------------------

    def create_session(self, title: str = "", parent_id: str | None = None,
                       summary: str | None = None) -> str:
        session_id = f"ses_{uuid.uuid4().hex[:16]}"
        with self._connect() as db:
            db.execute(
                "INSERT INTO sessions (id, parent_id, title, status, created_at, summary)"
                " VALUES (?,?,?,'open',?,?)",
                (session_id, parent_id, title, time.time(), summary),
            )
        return session_id

    def get_session(self, session_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return dict(row) if row else None

    def list_sessions(self, limit: int = 50) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM sessions ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def close_session(self, session_id: str, status: str, summary: str | None = None) -> None:
        """Close a session. The messages stay exactly as they were."""
        if status not in {"forked", "closed"}:
            raise ValueError(f"a session closes as forked or closed, not {status!r}")
        with self._connect() as db:
            db.execute(
                "UPDATE sessions SET status=?, closed_at=?, summary=COALESCE(?, summary)"
                " WHERE id=?",
                (status, time.time(), summary, session_id),
            )

    # --- messages ---------------------------------------------------------

    def append_message(self, session_id: str, role: str, content: Any) -> dict:
        """Add one message to the end of a session. There is no other way in.

        No update, no delete, no reorder - not because callers are trusted but
        because the functions do not exist and the triggers would refuse them.
        """
        message_id = f"msg_{uuid.uuid4().hex[:16]}"
        now = time.time()
        with self._connect() as db:
            # Sequence allocation and the outbox trigger share the message commit.
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM messages WHERE session_id=?",
                (session_id,),
            ).fetchone()
            seq = row["next"]
            db.execute(
                "INSERT INTO messages (id, session_id, seq, role, content, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (message_id, session_id, seq, role, json.dumps(content, ensure_ascii=False), now),
            )
            db.commit()
        return {"id": message_id, "session_id": session_id, "seq": seq,
                "role": role, "content": content, "created_at": now}

    def messages(self, session_id: str) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                MESSAGE_LOOKUP + " WHERE m.session_id=? ORDER BY m.seq", (session_id,)
            ).fetchall()
        return [_message(r) for r in rows]

    def get_message(self, message_id: str) -> dict | None:
        """Resolve an evidence citation, including its content-free tombstone."""
        with self._connect() as db:
            row = db.execute(MESSAGE_LOOKUP + " WHERE m.id=?", (message_id,)).fetchone()
        return _message(row) if row else None

    # --- turns ------------------------------------------------------------

    def start_turn(self, session_id: str) -> str:
        turn_id = f"trn_{uuid.uuid4().hex[:16]}"
        with self._connect() as db:
            db.execute(
                "INSERT INTO turns (id, session_id, status, started_at) VALUES (?,?, 'running', ?)",
                (turn_id, session_id, time.time()),
            )
        return turn_id

    def finish_turn(self, turn_id: str, status: str, **fields: Any) -> None:
        allowed = {"provider", "model", "input_tokens", "output_tokens", "cached_tokens",
                   "cost_usd", "latency_ms", "detail", "route_tier", "route_reason",
                   "approval_request_id", "approval_tool_id", "approval_args",
                   "approval_expires_at", "approval_intent", "acted"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown turn fields: {sorted(unknown)}")
        sets = ", ".join(f"{k}=?" for k in fields)
        clause = f", {sets}" if sets else ""
        with self._connect() as db:
            db.execute(
                f"UPDATE turns SET status=?, ended_at=?{clause} WHERE id=?",
                (status, time.time(), *fields.values(), turn_id),
            )

    def acted_without_reply(self) -> list[dict]:
        """Every turn that changed the world but never said what happened.

        The same reasoning as the approval queue: an action whose result the
        owner never sees is, to them, indistinguishable from one that silently
        went wrong. These are all resumable, and resuming asks only for the
        missing reply - the action is not repeated.
        """
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM turns WHERE status='acted_no_reply'"
                " AND session_id NOT IN (SELECT session_id FROM forgotten_sessions)"
                " ORDER BY started_at"
            ).fetchall()
        return [{**dict(r), "approval_args": json.loads(r["approval_args"] or "null")}
                for r in rows]

    def mark_acted(self, turn_id: str) -> None:
        """Record that this turn has now changed the world, before anything else.

        Called the moment a tool returns, and deliberately not folded into
        `finish_turn`: the turn is still running, so `ended_at` must stay unset.
        The ordering is the point. Between the action happening and the model
        narrating it there is a window where the process can die, and a record
        written only afterwards would leave the owner reading `interrupted` for
        an action that already ran.
        """
        with self._connect() as db:
            db.execute("UPDATE turns SET acted=1 WHERE id=?", (turn_id,))

    def awaiting_approval(self) -> list[dict]:
        """Every turn parked on the owner, across all sessions.

        The dashboard needs one queue rather than a per-session hunt: an
        approval the owner never sees is an action that silently never happens.
        """
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM turns WHERE status='awaiting_approval'"
                " AND session_id NOT IN (SELECT session_id FROM forgotten_sessions)"
                " ORDER BY started_at"
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["approval_args"] = json.loads(item["approval_args"] or "null")
            out.append(item)
        return out

    def get_turn(self, turn_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["approval_args"] = json.loads(item["approval_args"] or "null")
        return item

    def turns(self, session_id: str) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM turns WHERE session_id=? ORDER BY started_at", (session_id,)
            ).fetchall()
        return [{**dict(r), "approval_args": json.loads(r["approval_args"] or "null")}
                for r in rows]

    def mark_interrupted_turns(self) -> int:
        """Called at startup. A turn that was running when the process died did
        not finish, and saying nothing about it is the one answer that is wrong -
        the owner would see a request that simply vanished."""
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE turns SET status='interrupted', ended_at=?,"
                # An interrupted turn that had already acted is not the same event
                # as one that had not, and that difference is the only thing the
                # owner actually needs from this row.
                " detail=CASE WHEN acted=1"
                "   THEN 'the process stopped after this turn acted, before it replied'"
                "   ELSE 'the process stopped while this turn was running' END"
                # Only 'running'. A turn parked on the owner is not interrupted -
                # it is waiting, and a restart does not withdraw the question.
                " WHERE status='running'"
                " AND session_id NOT IN (SELECT session_id FROM forgotten_sessions)",
                (time.time(),),
            )
            return cursor.rowcount

    def health(self) -> dict:
        try:
            with self._connect() as db:
                db.execute("SELECT 1 FROM sessions LIMIT 1").fetchone()
            return {"status": "ok"}
        except Exception as exc:
            return {"status": "unavailable", "reason": type(exc).__name__}

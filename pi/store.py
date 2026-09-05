"""Session state and execution history. Pi holds nothing else.

Append-only is enforced here rather than promised in a docstring: there is no
function that updates or deletes a message, and the schema forbids it with
triggers, so a future caller cannot quietly rewrite history by reaching past the
API. Current models bind reasoning blocks to the producing model and reject
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
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

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
    provider     TEXT,
    model        TEXT,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cached_tokens INTEGER,
    cost_usd      REAL,
    latency_ms    INTEGER,
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


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA foreign_keys=ON")
            db.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
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
        return {"id": message_id, "session_id": session_id, "seq": seq,
                "role": role, "content": content, "created_at": now}

    def messages(self, session_id: str) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM messages WHERE session_id=? ORDER BY seq", (session_id,)
            ).fetchall()
        return [{**dict(r), "content": json.loads(r["content"])} for r in rows]

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
                   "cost_usd", "latency_ms", "detail"}
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

    def turns(self, session_id: str) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM turns WHERE session_id=? ORDER BY started_at", (session_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_interrupted_turns(self) -> int:
        """Called at startup. A turn that was running when the process died did
        not finish, and saying nothing about it is the one answer that is wrong -
        the owner would see a request that simply vanished."""
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE turns SET status='interrupted', ended_at=?,"
                " detail='the process stopped while this turn was running'"
                " WHERE status='running'",
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

"""Durable delivery metadata; conversation text stays in the message table."""

import json
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_outbox (
    message_id TEXT PRIMARY KEY REFERENCES messages(id),
    operation TEXT NOT NULL CHECK(operation IN ('ingest','delete')),
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_at REAL NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    receipt TEXT
);
CREATE TABLE IF NOT EXISTS memory_contexts (
    turn_id TEXT PRIMARY KEY REFERENCES turns(id),
    status TEXT NOT NULL,
    package TEXT,
    recorded_at REAL NOT NULL
);
CREATE TRIGGER IF NOT EXISTS messages_queue_memory
AFTER INSERT ON messages WHEN NEW.role='user'
BEGIN
    INSERT INTO memory_outbox(message_id, operation) VALUES (NEW.id, 'ingest');
END;
-- Upgrade existing transcripts too, including tombstones from before this bridge.
INSERT OR IGNORE INTO memory_outbox(message_id, operation)
SELECT m.id, CASE WHEN f.session_id IS NULL THEN 'ingest' ELSE 'delete' END
FROM messages m LEFT JOIN forgotten_sessions f ON f.session_id=m.session_id
WHERE m.role='user';
"""


def pending_deletions(store):
    with store._connect() as db:
        return db.execute(
            "SELECT COUNT(*) FROM memory_outbox WHERE operation='delete' AND state='pending'"
        ).fetchone()[0]


def save_context(store, turn_id, status, package=None):
    with store._connect() as db:
        db.execute(
            "INSERT INTO memory_contexts VALUES (?,?,?,?)",
            (
                turn_id,
                status,
                json.dumps(package, ensure_ascii=False) if package else None,
                time.time(),
            ),
        )


def context(store, turn_id):
    with store._connect() as db:
        row = db.execute("SELECT * FROM memory_contexts WHERE turn_id=?", (turn_id,)).fetchone()
    if not row:
        return {"status": "not_recorded", "package": None}
    return {**dict(row), "package": json.loads(row["package"]) if row["package"] else None}


def status(store, session_id=None, turn_id=None, *, configured=False):
    with store._connect() as db:
        rows = db.execute(
            "SELECT o.operation,o.state,o.error,o.receipt FROM memory_outbox o"
            " JOIN messages m ON m.id=o.message_id"
            + (" WHERE m.session_id=?" if session_id else ""),
            (session_id,) if session_id else (),
        ).fetchall()
    pending = sum(row["state"] == "pending" and row["operation"] == "ingest" for row in rows)
    deleting = pending_deletions(store)
    receipts = [json.loads(row["receipt"]) for row in rows if row["receipt"]]
    admission = {
        state: sum(receipt.get("state") == state for receipt in receipts)
        for state in ("admitted", "filtered", "deleted")
    }
    notices = []
    if not configured:
        notices.append(
            "Conversation saved. Long-term memory is not configured; delivery remains pending."
        )
    elif pending:
        notices.append(
            "Conversation saved. Long-term memory is pending; Pi will retry automatically."
        )
    if deleting:
        notices.append(
            "Forgetting is pending in MemoryGate. "
            "Memory retrieval is paused until deletion is acknowledged."
        )
    retrieval = context(store, turn_id) if turn_id else None
    if retrieval and retrieval["status"] in {"unavailable", "degraded", "redacted", "not_recorded"}:
        notices.append(
            "Memory context is incomplete or unavailable for this turn; "
            "the conversation continues with that gap."
        )
    return {
        "configured": configured,
        "pending_ingestion": pending,
        "pending_deletion": deleting,
        "admission": admission,
        "delivery_error": next((row["error"] for row in rows if row["error"]), None),
        "retrieval": retrieval,
        "notices": notices,
    }


def redact(db, session_ids):
    # Context packages can quote a forgotten message in any session. Clearing
    # all cached packages avoids relying on incomplete third-party lineage.
    db.execute(
        "UPDATE memory_contexts SET package=NULL,status='redacted' WHERE package IS NOT NULL"
    )
    for sid in session_ids:
        db.execute(
            "UPDATE memory_outbox SET operation='delete',state='pending',attempts=0,"
            "next_at=0,error='',receipt=NULL WHERE message_id IN"
            " (SELECT id FROM messages WHERE session_id=? AND role='user')",
            (sid,),
        )

"""Offline, OS-authorised session forgetting. Never imported by the API or loop.

The host operator already has database administration authority. Pi's shared API
key does not distinguish that operator from a runtime caller, so it cannot grant
this capability. The confirmation binds the operator to a previewed scope; it
is not a credential.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
import uuid
from contextlib import closing
from pathlib import Path

from .access import MaintenanceRequired, acquire
from .store import FORGETTING_SCHEMA, Store


class ForgettingError(RuntimeError):
    pass


def _connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if not path.is_file():
        raise ForgettingError("Database not found. Pass --db with the existing Pi database path.")
    mode = "ro" if readonly else "rw"
    db = sqlite3.connect(path.as_uri() + f"?mode={mode}", uri=True,
                         isolation_level=None, timeout=0.2)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def _receipt(db: sqlite3.Connection, session_id: str) -> dict | None:
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='forgotten_sessions'"
    ).fetchone():
        return None
    row = db.execute(
        "SELECT r.* FROM forgetting_receipts r JOIN forgotten_sessions f ON f.receipt_id=r.id"
        " WHERE f.session_id=?", (session_id,),
    ).fetchone()
    if row is None:
        return None
    receipt = dict(row)
    receipt["session_ids"] = [r[0] for r in db.execute(
        "SELECT session_id FROM forgotten_sessions WHERE receipt_id=? ORDER BY session_id",
        (row["id"],),
    )]
    return receipt


def _plan(db: sqlite3.Connection, session_id: str) -> dict:
    if not db.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
        raise ForgettingError("Session not found. Check the session ID in Pi before retrying.")
    prior = _receipt(db, session_id)
    if prior:
        return {"already_forgotten": prior, "confirmation": prior["confirmation"]}
    sessions = [r[0] for r in db.execute(
        "WITH RECURSIVE tree(id) AS (SELECT id FROM sessions WHERE id=? UNION"
        " SELECT s.id FROM sessions s JOIN tree t ON s.parent_id=t.id)"
        " SELECT id FROM tree ORDER BY id", (session_id,),
    )]
    sessions = [sid for sid in sessions if _receipt(db, sid) is None]
    # The preview binds identifiers, never a digest of the words being forgotten.
    plan = {"root_session_id": session_id, "session_ids": sessions,
            "message_ids": [], "turn_ids": []}
    for sid in sessions:
        for table, field in (("messages", "message_ids"), ("turns", "turn_ids")):
            plan[field].extend(r[0] for r in db.execute(
                f"SELECT id FROM {table} WHERE session_id=? ORDER BY id", (sid,),
            ))
    plan["confirmation"] = hashlib.sha256(
        json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return plan


def preview(path: Path | str, session_id: str) -> dict:
    """Show the exact session tree without printing its content."""
    with closing(_connect(Path(path).resolve(), readonly=True)) as db:
        db.execute("BEGIN")
        return _plan(db, session_id)


def _scrub(db: sqlite3.Connection) -> None:
    # Logical redaction alone leaves old payloads in the WAL and free pages.
    # Keep the maintenance record until both have been dealt with successfully.
    for vacuum in (True, False):
        if db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] != 0:
            raise ForgettingError(
                "Database cleanup is blocked by another reader. Stop database readers "
                "and rerun the same forgetting command; Pi remains blocked."
            )
        if vacuum:
            db.execute("VACUUM")


def _redact(db: sqlite3.Connection, plan: dict) -> dict:
    receipt_id = f"del_{uuid.uuid4().hex}"
    when = time.time()
    trigger = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='messages_are_immutable'"
    ).fetchone()
    if not trigger:
        raise ForgettingError(
            "Append-only trigger is missing. Repair the Pi schema before retrying."
        )
    db.execute("BEGIN IMMEDIATE")
    try:
        # Transactional DDL keeps the exception invisible to every other connection.
        # The runtime lease prevents a provider response from arriving after deletion.
        db.execute("DROP TRIGGER messages_are_immutable")
        for sid in plan["session_ids"]:
            db.execute("UPDATE messages SET content='null' WHERE session_id=?", (sid,))
            db.execute("UPDATE sessions SET title='', summary=NULL, status='forgotten',"
                       " closed_at=COALESCE(closed_at, ?) WHERE id=?", (when, sid))
            db.execute(
                "UPDATE turns SET approval_intent=NULL, approval_args=NULL, detail=NULL,"
                " route_reason=NULL WHERE session_id=?", (sid,),
            )
        db.execute(trigger["sql"])
        db.execute(
            "INSERT INTO forgetting_receipts VALUES (?,?,?,?,?,?)",
            (receipt_id, plan["root_session_id"], when, plan["confirmation"],
             len(plan["message_ids"]), len(plan["turn_ids"])),
        )
        db.executemany("INSERT INTO forgotten_sessions VALUES (?,?)",
                       [(sid, receipt_id) for sid in plan["session_ids"]])
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return _receipt(db, plan["root_session_id"])


def forget(path: Path | str, session_id: str, confirmation: str) -> dict:
    """Host operator entry point. Stop every Pi process before invoking it."""
    path = Path(path).resolve()
    if not path.is_file():
        raise ForgettingError("Database not found. Pass --db with the existing Pi database path.")
    with closing(acquire(path, exclusive=True)), closing(_connect(path)) as db:
        Store._migrate(db)
        db.executescript(FORGETTING_SCHEMA)
        db.execute("PRAGMA secure_delete=ON")
        db.execute("BEGIN IMMEDIATE")
        try:
            pending = db.execute("SELECT * FROM forgetting_maintenance").fetchone()
            if pending and (pending["root_session_id"], pending["confirmation"]) != (
                session_id, confirmation,
            ):
                raise ForgettingError(
                    "Another forgetting operation needs cleanup. Rerun its original command first."
                )
            plan = _plan(db, session_id)
            if confirmation != plan["confirmation"]:
                raise ForgettingError(
                    "Confirmation does not match the current scope. Run preview again, "
                    "review its session IDs, and use its new confirmation."
                )
            db.execute("INSERT OR IGNORE INTO forgetting_maintenance VALUES (1,?,?)",
                       (session_id, confirmation))
            db.commit()
        except BaseException:
            db.rollback()
            raise
        receipt = plan.get("already_forgotten") or _redact(db, plan)
        _scrub(db)
        db.execute("DELETE FROM forgetting_maintenance WHERE id=1")
        return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("preview", "forget"):
        command = commands.add_parser(name)
        command.add_argument("--session", required=True)
        if name == "forget":
            command.add_argument("--confirm", required=True)
    args = parser.parse_args(argv)
    try:
        result = (preview(args.db, args.session) if args.command == "preview" else
                  forget(args.db, args.session, args.confirm))
    except (ForgettingError, MaintenanceRequired, sqlite3.Error, OSError) as exc:
        # sqlite errors can contain data. Print only controlled errors verbatim.
        detail = str(exc) if isinstance(exc, (ForgettingError, MaintenanceRequired)) else (
            f"{type(exc).__name__}: stop Pi, check the database path and permissions, "
            "then rerun this command. If cleanup started, Pi remains blocked."
        )
        print(detail, file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

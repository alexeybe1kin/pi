"""Host-only repair of delivery metadata; never exposed to the model or browser."""

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from .access import acquire


def repair(path, message_id, *, agent_id=None):
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError("Database not found; pass --db with the existing Pi database.")
    with closing(acquire(path, exclusive=True)), closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM memory_outbox WHERE message_id=?", (message_id,)).fetchone()
        if not row:
            raise ValueError("No outbox record; check --message-id in Pi's transcript.")
        if agent_id is not None:
            if not agent_id.strip() or len(agent_id) > 100:
                raise ValueError("Use the original nonempty agent ID (at most 100 characters).")
            if row["destination_agent_id"] not in (None, agent_id):
                raise ValueError("Destination is already recorded; it cannot be retargeted.")
            receipt = json.loads(row["receipt"] or "{}")
            expected = str(uuid5(NAMESPACE_URL, f"pi:{agent_id}:{message_id}"))
            if receipt.get("id") and receipt["id"] != expected:
                raise ValueError(
                    "Agent ID contradicts the saved receipt; find the original deployment config."
                )
            db.execute(
                "UPDATE memory_outbox SET destination_agent_id=? WHERE message_id=?",
                (agent_id, message_id),
            )
        elif row["destination_agent_id"] is None and row["delivery_started"]:
            raise ValueError("Original destination is unknown; run bind-origin first.")
        db.execute(
            "UPDATE memory_outbox SET state='pending',next_at=0,error='' "
            "WHERE message_id=? AND state='blocked'",
            (message_id,),
        )
        db.commit()


def main():
    parser = argparse.ArgumentParser(
        description="Stop Pi before repairing memory delivery metadata."
    )
    parser.add_argument("--db", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("bind-origin", "retry"):
        child = commands.add_parser(command)
        child.add_argument("--message-id", required=True)
        if command == "bind-origin":
            child.add_argument("--agent-id", required=True)
    args = parser.parse_args()
    try:
        repair(args.db, args.message_id, agent_id=getattr(args, "agent_id", None))
    except (ValueError, RuntimeError, sqlite3.Error) as exc:
        parser.exit(1, str(exc) + "\n")
    print(
        "Delivery metadata repaired. Start Pi to retry; "
        "deletion is complete only after its receipt arrives."
    )


if __name__ == "__main__":
    main()

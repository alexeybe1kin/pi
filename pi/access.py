"""Lifetime leases keep an offline deletion out of an in-flight model turn.

A transaction on the transcript alone cannot do this: the runtime closes its
database connections while waiting for a provider. A separate rollback-journal
database provides shared/exclusive process locks on both Linux and Windows.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


class MaintenanceRequired(RuntimeError):
    pass


def acquire(path: Path, *, exclusive: bool = False) -> sqlite3.Connection:
    path = path.resolve()
    db = sqlite3.connect(str(path) + ".access.sqlite", isolation_level=None,
                         timeout=0.2, check_same_thread=False)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS lease (id INTEGER PRIMARY KEY)")
        db.execute("BEGIN EXCLUSIVE" if exclusive else "BEGIN")
        db.execute("SELECT * FROM lease").fetchall()
        return db
    except sqlite3.OperationalError as exc:
        db.close()
        raise MaintenanceRequired(
            "Pi's database is in use. Stop every Pi process before forgetting; "
            "if forgetting is running, wait for it to finish before starting Pi."
        ) from exc
    except BaseException:
        db.close()
        raise

"""Append-only is the property everything else rests on, so it is tested at the
level it is enforced: not that the module declines to rewrite history, but that
the database refuses even when someone reaches past the module.
"""
from __future__ import annotations

import sqlite3

import pytest

from pi.store import Store


@pytest.fixture()
def store(tmp_path):
    return Store(tmp_path / "pi.db")


def test_messages_keep_their_order(store):
    s = store.create_session()
    for text in ("one", "two", "three"):
        store.append_message(s, "user", text)
    assert [m["content"] for m in store.messages(s)] == ["one", "two", "three"]
    assert [m["seq"] for m in store.messages(s)] == [1, 2, 3]


def test_the_database_refuses_to_rewrite_a_message(store):
    """Enforced in the schema, not in a docstring.

    A future caller with a connection - a migration, a fix, a well-meant
    cleanup - still cannot edit what was said. Models reject edited history and
    the damage surfaces late, so this is worth making impossible rather than
    merely discouraged.
    """
    s = store.create_session()
    message = store.append_message(s, "user", "as it was said")

    with sqlite3.connect(store.path) as db:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("UPDATE messages SET content='rewritten' WHERE id=?", (message["id"],))
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("DELETE FROM messages WHERE id=?", (message["id"],))

    assert store.messages(s)[0]["content"] == "as it was said"


def test_the_module_offers_no_way_to_edit_history(store):
    """The API surface has to agree with the schema, or callers route around it."""
    for forbidden in ("update_message", "delete_message", "edit_message", "replace_message"):
        assert not hasattr(store, forbidden), f"Store.{forbidden} would break append-only"


def test_message_ids_are_stable_and_unique(store):
    """MemoryGate's evidence cites these ids. A citation that can be re-pointed
    at different content is not a citation - see ADR-0002."""
    s = store.create_session()
    ids = {store.append_message(s, "user", f"m{i}")["id"] for i in range(20)}
    assert len(ids) == 20
    assert {m["id"] for m in store.messages(s)} == ids


def test_a_turn_running_when_the_process_died_is_marked_interrupted(store):
    """Never silently dropped. The owner would otherwise see a request that
    simply vanished, which principles.md forbids reporting as anything."""
    s = store.create_session()
    store.start_turn(s)
    store.append_message(s, "user", "did this go through?")

    assert store.mark_interrupted_turns() == 1

    turn = store.turns(s)[0]
    assert turn["status"] == "interrupted"
    assert turn["ended_at"] is not None
    assert "stopped" in turn["detail"]
    # and the message the owner sent is still there
    assert store.messages(s)[0]["content"] == "did this go through?"


def test_marking_interrupted_turns_is_safe_to_repeat(store):
    s = store.create_session()
    store.start_turn(s)
    assert store.mark_interrupted_turns() == 1
    assert store.mark_interrupted_turns() == 0


def test_a_session_survives_reopening_the_store(tmp_path):
    """Sessions are durable, not in-process state."""
    path = tmp_path / "pi.db"
    first = Store(path)
    s = first.create_session(title="kept")
    first.append_message(s, "user", "still here?")

    second = Store(path)
    assert second.get_session(s)["title"] == "kept"
    assert second.messages(s)[0]["content"] == "still here?"


def test_a_session_closes_as_forked_or_closed_and_nothing_else(store):
    s = store.create_session()
    with pytest.raises(ValueError):
        store.close_session(s, "deleted")
    store.close_session(s, "forked", summary="what happened")
    assert store.get_session(s)["status"] == "forked"
    assert store.get_session(s)["summary"] == "what happened"


def test_unknown_turn_fields_are_refused(store):
    """A typo in a metric name would otherwise vanish, and cost or token counts
    that silently fail to record are worse than absent ones."""
    s = store.create_session()
    turn = store.start_turn(s)
    with pytest.raises(ValueError, match="unknown turn fields"):
        store.finish_turn(turn, "complete", tokens_in=5)

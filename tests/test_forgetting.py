"""Exercise actual files, leases, HTTP reads and tool dispatch across forgetting."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from pi import forgetting
from pi.access import MaintenanceRequired, acquire
from pi.api import app
from pi.forgetting import ForgettingError, forget, preview
from pi.loop import Loop, TurnFailed
from pi.providers import Completion
from pi.routing import Router
from pi.store import SCHEMA, Store

SECRET = "private-7f9919-никому-не-говори"


@pytest.fixture()
def conversation(tmp_path):
    path = tmp_path / "pi.db"
    store = Store(path)
    root = store.create_session(title=SECRET, summary=SECRET)
    child = store.create_session(parent_id=root, title=SECRET, summary=SECRET)
    other = store.create_session(title="unrelated")
    messages = [store.append_message(sid, "user", SECRET) for sid in (root, child)]
    kept = store.append_message(other, "user", "keep this")
    turns = []
    for sid, status in ((root, "awaiting_approval"), (child, "acted_no_reply")):
        turn = store.start_turn(sid)
        store.finish_turn(turn, status, approval_intent=SECRET,
                          approval_args=json.dumps({"text": SECRET}, ensure_ascii=False),
                          detail=SECRET, route_reason=SECRET, approval_request_id="req_123",
                          approval_tool_id="tool_123", acted=int(sid == child), cost_usd=1.25)
        turns.append(store.get_turn(turn))
    store.close()
    return path, root, child, other, messages, kept, turns


def erase(path, session):
    return forget(path, session, preview(path, session)["confirmation"])


def test_content_and_derived_copies_disappear_but_envelopes_and_receipt_survive(conversation):
    path, root, child, other, messages, kept, turns = conversation
    before = time.time()
    receipt = erase(path, root)
    assert before <= receipt["forgotten_at"] <= time.time()
    assert set(receipt["session_ids"]) == {root, child}
    assert receipt["message_count"] == 2 and receipt["turn_count"] == 2
    assert SECRET not in json.dumps(receipt, ensure_ascii=False)
    with closing(Store(path)) as store:
        for original in messages:
            row = store.get_message(original["id"])
            assert row == {**original, "content": None, "content_status": "forgotten",
                           "receipt_id": receipt["id"], "forgotten_at": receipt["forgotten_at"]}
            assert store.messages(original["session_id"]) == [row]
        for sid in (root, child):
            session = store.get_session(sid)
            assert session["status"] == "forgotten"
            assert session["title"] == "" and session["summary"] is None
        for original in turns:
            turn = store.get_turn(original["id"])
            for field in ("approval_intent", "approval_args", "detail", "route_reason"):
                assert turn[field] is None
            for field in ("id", "status", "acted", "cost_usd", "approval_request_id"):
                assert turn[field] == original[field]
        assert store.awaiting_approval() == []
        assert store.acted_without_reply() == []
        assert store.get_message(kept["id"]) == kept
        assert store.get_session(other)["title"] == "unrelated"
        store.append_message(other, "assistant", "still usable")
    # Scan the live database and its sidecars, not just SELECT output.
    for file in path.parent.glob("pi.db*"):
        assert SECRET.encode() not in file.read_bytes(), file.name


def test_append_only_and_receipts_are_enforced_after_forgetting(conversation):
    path, root, child, _, messages, _, _ = conversation
    receipt = erase(path, root)
    with sqlite3.connect(path) as db:
        for sql in ("UPDATE messages SET content='changed'", "DELETE FROM messages"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                db.execute(sql)
        for table in ("forgetting_receipts", "forgotten_sessions"):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                db.execute(f"DELETE FROM {table}")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE forgetting_receipts SET forgotten_at=0")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE forgotten_sessions SET receipt_id='fake'")
    with closing(Store(path)) as store:
        for sid in (root, child):
            with pytest.raises(sqlite3.IntegrityError, match="forgotten"):
                store.append_message(sid, "user", "resurrect")
            with pytest.raises(sqlite3.IntegrityError, match="forgotten"):
                store.create_session(parent_id=sid)
            with pytest.raises(sqlite3.IntegrityError, match="forgotten"):
                store.close_session(sid, "closed", summary="resurrect")
            with pytest.raises(sqlite3.IntegrityError, match="forgotten"):
                store.start_turn(sid)
        assert store.get_message(messages[0]["id"])["receipt_id"] == receipt["id"]


def test_sequence_gaps_and_message_ids_are_not_reused(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        session = store.create_session()
        first = store.append_message(session, "user", SECRET)
        with sqlite3.connect(path) as db:
            db.execute("INSERT INTO messages VALUES ('msg_gap', ?, 7, 'assistant', ?, ?)",
                       (session, json.dumps(SECRET), time.time()))
    erase(path, session)
    with closing(Store(path)) as store:
        assert [(m["id"], m["seq"]) for m in store.messages(session)] == [
            (first["id"], 1), ("msg_gap", 7),
        ]


def test_replace_cannot_repoint_citations_or_erase_receipts(conversation):
    path, root, _, other, messages, _, turns = conversation
    receipt = erase(path, root)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA recursive_triggers=OFF")
        statements = [
            ("INSERT OR REPLACE INTO messages VALUES (?, ?, 10, 'user', 'null', 0)",
             (messages[0]["id"], other)),
            ("INSERT OR REPLACE INTO forgetting_receipts VALUES (?, ?, 0, 'fake', 0, 0)",
             (receipt["id"], root)),
            ("INSERT OR REPLACE INTO forgotten_sessions VALUES (?, ?)", (root, receipt["id"])),
            ("INSERT OR REPLACE INTO sessions (id, created_at) VALUES (?, 0)", (root,)),
            (("INSERT OR REPLACE INTO turns (id, session_id, status, started_at)"
              " VALUES (?, ?, 'running', 0)"), (turns[0]["id"], other)),
        ]
        for sql, args in statements:
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(sql, args)


def test_forgetting_refuses_live_stores_and_stale_preview(conversation):
    path, root, *_ = conversation
    plan = preview(path, root)
    with closing(Store(path)) as first, closing(Store(path)):
        with pytest.raises(MaintenanceRequired, match="Stop every Pi"):
            forget(path, root, plan["confirmation"])
        first.append_message(root, "user", "arrived after preview")
    with pytest.raises(ForgettingError, match="preview again"):
        forget(path, root, plan["confirmation"])
    with closing(Store(path)) as store:
        assert store.messages(root)[0]["content"] == SECRET
    erase(path, root)


def test_offline_lease_blocks_new_runtime_and_works_across_processes(conversation):
    path, root, *_ = conversation
    with closing(acquire(path, exclusive=True)), pytest.raises(MaintenanceRequired):
        Store(path)
    with closing(Store(path)):
        result = subprocess.run(
            [sys.executable, "-m", "pi.forgetting", "--db", str(path), "forget",
             "--session", root, "--confirm", preview(path, root)["confirmation"]],
            capture_output=True, text=True, timeout=15,
        )
    assert result.returncode == 1
    assert "Stop every Pi" in result.stderr


def test_failed_redaction_rolls_back_content_receipt_and_trigger(conversation):
    path, root, *_ = conversation
    plan = preview(path, root)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TRIGGER fail_redaction BEFORE UPDATE ON turns "
                   "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        forget(path, root, plan["confirmation"])
    with sqlite3.connect(path) as db:
        payload = db.execute("SELECT content FROM messages LIMIT 1").fetchone()[0]
        assert json.loads(payload) == SECRET
        assert db.execute("SELECT COUNT(*) FROM forgetting_receipts").fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("UPDATE messages SET content='oops'")
        db.execute("DROP TRIGGER fail_redaction")
    with pytest.raises(MaintenanceRequired, match="unfinished"):
        Store(path)
    forget(path, root, plan["confirmation"])


def test_interrupted_cleanup_blocks_startup_until_idempotent_retry(conversation, monkeypatch):
    path, root, *_ = conversation
    confirmation = preview(path, root)["confirmation"]
    with monkeypatch.context() as patch:
        def crash(db):
            raise OSError("injected crash after redaction")
        patch.setattr(forgetting, "_scrub", crash)
        with pytest.raises(OSError, match="injected crash"):
            forget(path, root, confirmation)
    with pytest.raises(MaintenanceRequired, match="unfinished"):
        Store(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT content FROM messages LIMIT 1").fetchone()[0] == "null"
        receipt_id = db.execute("SELECT id FROM forgetting_receipts").fetchone()[0]
    receipt = forget(path, root, confirmation)
    assert receipt["id"] == receipt_id
    assert forget(path, root, confirmation) == receipt
    with closing(Store(path)) as store:
        assert store.get_session(root)["status"] == "forgotten"


def test_process_death_leaves_a_durable_cleanup_barrier(conversation):
    path, root, *_ = conversation
    confirmation = preview(path, root)["confirmation"]
    program = (
        "import os, sys; from pi import forgetting; "
        "forgetting._scrub = lambda db: os._exit(23); "
        "forgetting.forget(sys.argv[1], sys.argv[2], sys.argv[3])"
    )
    result = subprocess.run([sys.executable, "-c", program, str(path), root, confirmation],
                            capture_output=True, timeout=15)
    assert result.returncode == 23
    with pytest.raises(MaintenanceRequired, match="unfinished"):
        Store(path)
    receipt = forget(path, root, confirmation)
    with closing(Store(path)) as store:
        assert store.messages(root)[0]["receipt_id"] == receipt["id"]


def test_a_busy_checkpoint_does_not_report_success(conversation):
    path, root, *_ = conversation
    confirmation = preview(path, root)["confirmation"]
    with closing(sqlite3.connect(path, isolation_level=None)) as reader:
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM messages").fetchall()
        with pytest.raises(ForgettingError, match="cleanup is blocked"):
            forget(path, root, confirmation)
        with pytest.raises(MaintenanceRequired, match="unfinished"):
            Store(path)
    forget(path, root, confirmation)


def test_existing_database_can_be_forgotten_without_first_starting_a_runtime(tmp_path):
    path = tmp_path / "pi.db"
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        db.execute("INSERT INTO sessions (id, created_at) VALUES ('ses_old', 0)")
        db.execute("INSERT INTO messages VALUES ('msg_old', 'ses_old', 1, 'user', ?, 0)",
                   (json.dumps(SECRET),))
    receipt = erase(path, "ses_old")
    with closing(Store(path)) as store:
        assert store.get_message("msg_old")["receipt_id"] == receipt["id"]
        assert store.get_message("msg_old")["content"] is None


def test_parent_forgetting_preserves_an_earlier_child_receipt(conversation):
    path, root, child, *_ = conversation
    child_receipt = erase(path, child)
    root_receipt = erase(path, root)
    assert root_receipt["session_ids"] == [root]
    assert preview(path, child)["already_forgotten"] == child_receipt


class NoCalls:
    name = "no-calls"

    def complete(self, *args, **kwargs):
        pytest.fail("forgotten text reached a provider")

    def invoke(self, *args, **kwargs):
        pytest.fail("a forgotten turn dispatched an action")

    def tools(self):
        pytest.fail("a forgotten session contacted ToolGate")


def test_forgotten_sessions_never_reach_models_or_tools(conversation):
    path, root, child, _, _, _, turns = conversation
    erase(path, root)
    with closing(Store(path)) as store:
        loop = Loop(store, Router(local_provider=NoCalls(), local_model="test"), toolgate=NoCalls())
        for sid in (root, child):
            with pytest.raises(TurnFailed, match="forgotten"):
                loop.fork(sid)
            with pytest.raises(TurnFailed):
                loop.run_turn(sid, "resume")
        for turn in turns:
            with pytest.raises(TurnFailed, match="forgotten"):
                loop.resume_turn(turn["id"])


def test_citation_http_distinguishes_forgotten_from_missing_and_cannot_delete(
    conversation, monkeypatch,
):
    path, root, _, _, messages, kept, turns = conversation
    receipt = erase(path, root)
    with closing(Store(path)) as store, closing(TestClient(app)) as client:
        monkeypatch.setattr(app.state, "store", store, raising=False)
        monkeypatch.setattr(app.state, "admin_key", "owner-key-for-test", raising=False)
        loop = Loop(store, Router(local_provider=NoCalls(), local_model="test"), toolgate=NoCalls())
        monkeypatch.setattr(app.state, "loop", loop, raising=False)
        headers = {"X-Pi-Key": "owner-key-for-test"}
        url = f"/messages/{messages[0]['id']}"
        assert client.get(url).status_code == 401
        response = client.get(url, headers=headers)
        assert response.status_code == 200
        assert response.json()["content"] is None
        assert response.json()["content_status"] == "forgotten"
        assert response.json()["receipt_id"] == receipt["id"]
        assert response.json()["forgotten_at"] == receipt["forgotten_at"]
        assert client.get("/messages/unknown", headers=headers).status_code == 404
        assert client.get(f"/messages/{kept['id']}", headers=headers).json() == kept
        session = client.get(f"/sessions/{root}", headers=headers).json()
        assert session["messages"][0] == response.json()
        for method in (client.delete, client.patch, client.post):
            assert method(url, headers=headers).status_code == 405
        assert client.post(f"/sessions/{root}/forget", headers=headers).status_code == 404
        assert client.post(f"/sessions/{root}/fork", headers=headers).status_code == 409
        for turn in turns:
            assert client.post(f"/turns/{turn['id']}/resume", headers=headers).status_code == 409


def test_model_request_to_forget_is_only_conversation(conversation):
    path, root, *_ = conversation

    class Provider:
        name = "provider"

        def complete(self, messages, *, model):
            return Completion(text='{"tool": "forget", "args": {"session": "all"}}',
                              model=model, provider=self.name)

    with closing(Store(path)) as store:
        loop = Loop(store, Router(local_provider=Provider(), local_model="test"))
        loop.run_turn(root, "Forget everything I said")
        assert store.messages(root)[0]["content"] == SECRET
        assert store.get_session(root)["status"] == "open"
        assert not hasattr(store, "forget")


def test_wal_resident_payload_is_scrubbed(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        sid = store.create_session()
        # Keep a WAL connection alive, so last-connection cleanup does not make
        # this accidentally test only an already-checkpointed database.
        with closing(sqlite3.connect(path, isolation_level=None)) as reader:
            reader.execute("PRAGMA wal_autocheckpoint=0")
            reader.execute("SELECT * FROM messages").fetchall()
            store.append_message(sid, "user", SECRET)
            assert SECRET.encode() in path.with_name("pi.db-wal").read_bytes()
            store.close()
            erase(path, sid)
            assert SECRET.encode() not in path.read_bytes()
            assert SECRET.encode() not in path.with_name("pi.db-wal").read_bytes()


def test_cli_success_is_a_content_free_receipt_and_missing_database_fails(conversation):
    path, root, *_ = conversation
    command = [sys.executable, "-m", "pi.forgetting", "--db", str(path)]
    result = subprocess.run([*command, "preview", "--session", root],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    confirmation = json.loads(result.stdout)["confirmation"]
    result = subprocess.run([*command, "forget", "--session", root, "--confirm", confirmation],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["message_count"] == 2
    assert SECRET not in result.stdout
    result = subprocess.run(
        [sys.executable, "-m", "pi.forgetting", "--db", str(path.parent / "missing.db"),
         "preview", "--session", root], capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 1
    assert "existing Pi database path" in result.stderr
    assert not (path.parent / "missing.db").exists()

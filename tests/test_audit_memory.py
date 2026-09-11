import json
from uuid import NAMESPACE_URL, uuid5

import httpx
import pytest
from fastapi.testclient import TestClient
from test_tool_turns import build

from pi import api, forgetting, memory_store
from pi.memory import Memory, MemoryClient
from pi.memory_recovery import repair
from pi.store import Store


def client(agent, handler):
    return MemoryClient(
        "http://memory", "ingest-key", "read-key", agent, transport=httpx.MockTransport(handler)
    )


def receipt(request, *, agent=None):
    message_id = request.url.path.rsplit("/", 1)[-1]
    destination = agent or request.headers["X-Agent-Id"]
    return httpx.Response(
        200,
        json={
            "id": str(uuid5(NAMESPACE_URL, f"pi:{destination}:{message_id}")),
            "message_id": message_id,
            "state": "deleted" if request.method == "DELETE" else "admitted",
        },
    )


@pytest.mark.parametrize("lost_ack", [False, True])
def test_forgetting_uses_original_namespace_after_restart(tmp_path, lost_ack):
    path = tmp_path / "pi.db"
    store = Store(path)
    session = store.create_session()
    store.append_message(session, "user", "I prefer mornings")
    retained = set()
    calls = []

    def remote(request):
        identity = (request.headers["X-Agent-Id"], request.url.path)
        calls.append((request.method, identity))
        if request.method == "PUT":
            retained.add(identity)
            if lost_ack:
                raise httpx.ReadTimeout("committed, ACK lost")
        else:
            retained.discard(identity)
        return receipt(request)

    old = Memory(store, client("original", remote))
    old.drain_once()
    old.close()
    store.close()
    plan = forgetting.preview(path, session)
    forgetting.forget(path, session, plan["confirmation"])
    store = Store(path)
    new = Memory(store, client("renamed", remote))
    assert new.drain_once() == 1
    assert retained == set()
    assert calls[-1][0] == "DELETE" and calls[-1][1][0] == "original"
    assert new.status()["pending_deletion"] == 0
    new.close()
    store.close()


def test_lost_ack_retry_does_not_copy_evidence_into_new_namespace(tmp_path):
    store = Store(tmp_path / "pi.db")
    store.append_message(store.create_session(), "user", "I prefer mornings")
    calls = []

    def remote(request):
        calls.append(request.headers["X-Agent-Id"])
        if len(calls) == 1:
            raise httpx.ReadTimeout("ACK lost")
        return receipt(request)

    old = Memory(store, client("original", remote))
    assert old.drain_once() == 0
    old.close()
    with store._connect() as db:
        db.execute("UPDATE memory_outbox SET next_at=0")
    new = Memory(store, client("renamed", remote))
    assert new.drain_once() == 1
    assert calls == ["original", "original"]
    new.close()
    store.close()


def test_wrong_namespace_receipt_cannot_finish_forgetting(tmp_path):
    store = Store(tmp_path / "pi.db")
    message = store.append_message(store.create_session(), "user", "private")
    with store._connect() as db:
        db.execute("UPDATE memory_outbox SET operation='delete',destination_agent_id='original'")
    memory = Memory(store, client("renamed", lambda req: receipt(req, agent="renamed")))
    assert memory.drain_once() == 0
    assert memory.status()["pending_deletion"] == 1
    assert memory.status()["blocked_delivery"] == 1
    turn = store.start_turn(message["session_id"])
    memory.prepare(turn, "private")
    assert memory_store.context(store, turn)["status"] == "unavailable"
    memory.close()
    store.close()


def test_legacy_delivery_with_unknown_origin_requires_explicit_repair(tmp_path):
    path = tmp_path / "pi.db"
    store = Store(path)
    message = store.append_message(store.create_session(), "user", "private")
    with store._connect() as db:
        db.execute("ALTER TABLE memory_outbox DROP COLUMN destination_agent_id")
        db.execute("ALTER TABLE memory_outbox DROP COLUMN delivery_started")
        db.execute("UPDATE memory_outbox SET attempts=1")
    store.close()
    store = Store(path)
    calls = []
    memory = Memory(store, client("renamed", lambda req: calls.append(req) or receipt(req)))
    assert memory.drain_once() == 0 and not calls
    assert "namespace unknown" in memory.status()["delivery_error"]
    memory.close()
    store.close()
    repair(path, message["id"], agent_id="original")
    store = Store(path)
    memory = Memory(store, client("renamed", lambda req: calls.append(req) or receipt(req)))
    assert memory.drain_once() == 1
    assert calls[0].headers["X-Agent-Id"] == "original"
    memory.close()
    store.close()


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 413, 422])
def test_permanent_http_failure_blocks_instead_of_retrying_forever(tmp_path, status):
    store = Store(tmp_path / "pi.db")
    store.append_message(store.create_session(), "user", "saved text")
    calls = []
    memory = Memory(
        store, client("original", lambda req: calls.append(req) or httpx.Response(status))
    )
    assert memory.drain_once() == 0
    with store._connect() as db:
        db.execute("UPDATE memory_outbox SET next_at=0")
    assert memory.drain_once() == 0
    assert len(calls) == 1
    assert memory.status()["blocked_delivery"] == 1
    assert any(
        "Conversation saved" in text and "blocked" in text for text in memory.status()["notices"]
    )
    assert str(status) in memory.status()["delivery_error"]
    memory.close()
    store.close()


@pytest.mark.parametrize("status", [408, 429, 503])
def test_transient_http_failure_still_retries(tmp_path, status):
    store = Store(tmp_path / "pi.db")
    store.append_message(store.create_session(), "user", "saved text")
    calls = []

    def remote(req):
        calls.append(req)
        return httpx.Response(status) if len(calls) == 1 else receipt(req)

    memory = Memory(store, client("original", remote))
    memory.drain_once()
    with store._connect() as db:
        db.execute("UPDATE memory_outbox SET next_at=0")
    assert memory.drain_once() == 1 and len(calls) == 2
    memory.close()
    store.close()


def test_oversize_api_input_never_enters_transcript_or_outbox(tmp_path, monkeypatch):
    store = Store(tmp_path / "pi.db")
    session = store.create_session()
    loop, provider = build(store, ["hello"], None)
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "loop", loop, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "test-key", raising=False)
    response = TestClient(api.app).post(
        f"/sessions/{session}/turns", headers={"X-Pi-Key": "test-key"}, json={"text": "я" * 16001}
    )
    assert response.status_code == 422
    assert store.messages(session) == [] and provider.sent == []
    assert Memory(store).status()["pending_ingestion"] == 0
    store.append_message(session, "user", "я" * 16000)
    with pytest.raises(ValueError, match="16000"):
        store.append_message(session, "user", "я" * 16001)
    store.close()


def test_legacy_oversized_payload_is_saved_but_never_uploaded(tmp_path):
    store = Store(tmp_path / "pi.db")
    session = store.create_session()
    with store._connect() as db:
        db.execute(
            "INSERT INTO messages VALUES(?,?,?,?,?,?)",
            ("msg_old", session, 1, "user", json.dumps("x" * 16001), 1),
        )
    memory = Memory(
        store, client("original", lambda req: pytest.fail("Must not upload oversize text"))
    )
    memory.drain_once()
    assert len(store.messages(session)[0]["content"]) == 16001
    assert memory.status()["blocked_delivery"] == 1
    memory.close()
    store.close()

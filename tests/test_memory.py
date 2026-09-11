"""Recovery guarantees, exercised through real SQLite commits and HTTP contracts."""

import json
from concurrent.futures import ThreadPoolExecutor
from uuid import NAMESPACE_URL, uuid5

import httpx
import pytest
from fastapi.testclient import TestClient

from pi import api, forgetting, memory_store
from pi.loop import Loop
from pi.memory import Memory, MemoryClient
from pi.providers import Completion
from pi.routing import Router
from pi.store import Store


class Provider:
    name = "test"

    def __init__(self):
        self.calls = []

    def complete(self, messages, *, model):
        self.calls.append(messages)
        return Completion(
            text="A plain reply with no memory warning.", provider=self.name, model=model
        )


def client(handler):
    return MemoryClient(
        "http://memory.test",
        "ingest-secret-long",
        "read-secret-long",
        transport=httpx.MockTransport(handler),
    )


def test_message_and_outbox_are_one_commit_even_with_wal_and_restart(tmp_path):
    path = tmp_path / "pi.db"
    store = Store(path)
    session = store.create_session()
    with store._connect() as db:
        db.execute("PRAGMA wal_autocheckpoint=0")
        message = store.append_message(session, "user", "I prefer to train before school")
        # A rejected outbox insertion must reject the transcript write too.
        db.execute(
            "CREATE TRIGGER reject_outbox BEFORE INSERT ON memory_outbox "
            "BEGIN SELECT RAISE(ABORT,'simulated disk failure'); END"
        )
        with pytest.raises(Exception, match="simulated disk failure"):
            store.append_message(session, "user", "must not commit alone")
        db.execute("DROP TRIGGER reject_outbox")
    store.close()
    with_store = Store(path)
    assert [m["id"] for m in with_store.messages(session)] == [message["id"]]
    assert Memory(with_store).status(session)["pending_ingestion"] == 1
    with_store.close()


def test_concurrent_writers_lose_neither_messages_nor_outbox_rows(tmp_path):
    store = Store(tmp_path / "pi.db")
    session = store.create_session()
    with ThreadPoolExecutor(max_workers=12) as pool:
        messages = list(
            pool.map(lambda n: store.append_message(session, "user", str(n)), range(60))
        )
    assert sorted(m["seq"] for m in messages) == list(range(1, 61))
    assert Memory(store).status(session)["pending_ingestion"] == 60
    store.close()


def test_lost_ack_retries_same_identity_after_restart(tmp_path):
    store = Store(tmp_path / "pi.db")
    session = store.create_session()
    message = store.append_message(session, "user", "I prefer morning training")
    attempts = []

    def receive(request):
        attempts.append((request.url.path, json.loads(request.content)))
        if len(attempts) == 1:
            raise httpx.ReadTimeout("receiver committed, acknowledgment lost")
        return httpx.Response(200, json={"id": str(uuid5(
            NAMESPACE_URL, f"pi:default:{message['id']}")),
                                        "message_id": message["id"], "state": "admitted"})

    memory = Memory(store, client(receive))
    assert memory.drain_once() == 0
    assert memory.status(session)["pending_ingestion"] == 1
    memory.close()
    store.close()
    store = Store(tmp_path / "pi.db")
    memory = Memory(store, client(receive))
    with store._connect() as db:
        db.execute("UPDATE memory_outbox SET next_at=0")
    assert memory.drain_once() == 1
    assert attempts[0] == attempts[1]
    assert memory.status(session)["pending_ingestion"] == 0
    memory.close()
    store.close()


def test_context_is_used_and_stored_before_model_call(tmp_path):
    store = Store(tmp_path / "pi.db")
    package = {
        "memories": [{"text": "I prefer mornings", "confidence": "medium"}],
        "retrieval": {"semantic": {"status": "ok"}},
    }
    memory = Memory(store, client(lambda request: httpx.Response(200, json=package)))
    provider = Provider()
    loop = Loop(store, Router(local_provider=provider, local_model="test"), memory=memory)
    original = provider.complete

    def complete(messages, *, model):
        turn = store.turns(session)[0]
        assert memory_store.context(store, turn["id"])["package"] == package
        return original(messages, model=model)

    provider.complete = complete
    session = store.create_session()
    result = loop.run_turn(session, "When should I train?")
    assert json.dumps(package, ensure_ascii=False) in provider.calls[0][0].content
    assert result["memory"]["retrieval"]["package"] == package
    memory.close()
    store.close()


def test_interface_exposes_failures_without_a_model_warning(tmp_path, monkeypatch):
    store = Store(tmp_path / "pi.db")
    memory = Memory(store, client(lambda request: httpx.Response(503)))
    loop = Loop(store, Router(local_provider=Provider(), local_model="test"), memory=memory)
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "loop", loop, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "test-admin", raising=False)
    http = TestClient(api.app)
    session = store.create_session()
    response = http.post(
        f"/sessions/{session}/turns",
        json={"text": "I prefer mornings"},
        headers={"X-Pi-Key": "test-admin"},
    )
    assert response.status_code == 200
    result = response.json()
    assert result["message"]["content"] == "A plain reply with no memory warning."
    assert memory.drain_once() == 0
    refreshed = http.get(f"/sessions/{session}", headers={"X-Pi-Key": "test-admin"}).json()
    status = refreshed["turns"][0]["memory"]
    assert status["pending_ingestion"] == 1
    assert status["retrieval"]["status"] == "unavailable"
    assert any("Conversation saved" in notice for notice in status["notices"])
    assert any("gap" in notice for notice in status["notices"])
    memory.close()
    store.close()


def test_forgetting_cancels_upload_scrubs_context_and_blocks_recall(tmp_path):
    store = Store(tmp_path / "pi.db")
    session, other = store.create_session(), store.create_session()
    message = store.append_message(session, "user", "I prefer a secret routine")
    turn = store.start_turn(other)
    memory_store.save_context(store, turn, "ok", {"quote": message["content"]})
    store.close()
    plan = forgetting.preview(tmp_path / "pi.db", session)
    forgetting.forget(tmp_path / "pi.db", session, plan["confirmation"])
    store = Store(tmp_path / "pi.db")
    calls = []

    def receive(request):
        calls.append(request.method)
        assert request.content == b""
        return httpx.Response(200, json={"id": str(uuid5(
            NAMESPACE_URL, f"pi:default:{message['id']}")),
                                        "message_id": message["id"], "state": "deleted"})

    memory = Memory(store, client(receive))
    next_turn = store.start_turn(other)
    memory.prepare(next_turn, "secret routine")
    assert not calls
    assert memory_store.context(store, next_turn)["status"] == "unavailable"
    assert memory_store.context(store, turn)["package"] is None
    assert store.get_message(message["id"])["content_status"] == "forgotten"
    assert memory.drain_once() == 1
    assert calls == ["DELETE"]
    assert memory.status()["pending_deletion"] == 0
    memory.close()
    store.close()


def test_resume_uses_original_context_without_another_retrieval(tmp_path):
    store = Store(tmp_path / "pi.db")
    session = store.create_session()
    store.append_message(session, "user", "I prefer mornings")
    turn = store.start_turn(session)
    package = {"memories": [{"text": "An earlier owner statement"}]}
    memory_store.save_context(store, turn, "ok", package)
    store.finish_turn(turn, "acted_no_reply", acted=1)

    def unexpected(request):
        raise AssertionError("Resuming must use its recorded package")

    memory = Memory(store, client(unexpected))
    provider = Provider()
    loop = Loop(store, Router(local_provider=provider, local_model="test"), memory=memory)
    result = loop.resume_turn(turn)
    assert json.dumps(package) in provider.calls[0][0].content
    assert result["memory"]["retrieval"]["package"] == package
    memory.close()
    store.close()


def test_shutdown_waits_for_inflight_upload_before_releasing_forgetting_lease(tmp_path):
    import threading
    from concurrent.futures import TimeoutError

    store = Store(tmp_path / "pi.db")
    session = store.create_session()
    message = store.append_message(session, "user", "I prefer mornings")
    entered, release = threading.Event(), threading.Event()

    def receive(request):
        entered.set()
        assert release.wait(5)
        return httpx.Response(200, json={"id": str(uuid5(
            NAMESPACE_URL, f"pi:default:{message['id']}")),
                                        "message_id": message["id"], "state": "admitted"})

    memory = Memory(store, client(receive))
    memory.start()
    assert entered.wait(3)

    def shutdown():
        memory.close()
        store.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        stopped = pool.submit(shutdown)
        try:
            with pytest.raises(TimeoutError):
                stopped.result(timeout=0.1)
        finally:
            release.set()
        stopped.result(timeout=3)

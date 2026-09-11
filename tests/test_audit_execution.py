"""Durable wire receipts and recovery, including the losing resume caller."""

import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from test_tool_turns import CALL, FakeGate, build

from pi import actions, forgetting
from pi.loop import TurnFailed
from pi.providers import ProviderUnavailable
from pi.store import Store
from pi.toolgate import ToolGateClient, ToolPending


def test_id_is_committed_before_dispatch_and_reused_with_owner_job(tmp_path, monkeypatch):
    store = Store(tmp_path / "pi.db")
    sent = []

    def post(url, *, json, **kwargs):
        saved = actions.latest(store, store.turns(session)[0]["id"])
        assert saved["id"] == json["action_id"]
        sent.append(json)
        if len(sent) == 1:
            return httpx.Response(200, json={"code": "CONFIRMATION_REQUIRED", "request_id": "req"})
        assert json["action_id"] == sent[0]["action_id"]
        assert json["job_id"] == "owner-job"
        return httpx.Response(
            200,
            json={
                "code": "OK",
                "status": "completed",
                "action_id": json["action_id"],
                "result": {"ok": True, "result": "sent"},
            },
        )

    gate = ToolGateClient("http://gate", "agent")
    monkeypatch.setattr(gate, "tools", FakeGate().tools)
    monkeypatch.setattr(httpx, "post", post)
    loop, _ = build(store, [CALL, "sent"], gate)
    session = store.create_session()
    parked = loop.run_turn(session, "send")
    assert loop.resume_turn(parked["turn_id"], job_id="owner-job")["acted"] is True
    assert len(sent) == 2
    assert [m["role"] for m in store.messages(session)].count("tool") == 1


@pytest.mark.parametrize(
    "body,status",
    [
        ({}, "outcome_unknown"),
        ({"ok": "true"}, "outcome_unknown"),
        ({"code": "IN_PROGRESS"}, "action_in_progress"),
        ({"code": "OUTCOME_UNKNOWN"}, "outcome_unknown"),
        ({"code": "TOOL_UNAVAILABLE", "ok": True}, "outcome_unknown"),
    ],
)
def test_ambiguous_receipts_hold_without_claiming_to_have_acted(
    tmp_path, monkeypatch, body, status
):
    store = Store(tmp_path / "pi.db")
    gate = ToolGateClient("http://gate", "agent")
    monkeypatch.setattr(gate, "tools", FakeGate().tools)
    posts = []
    monkeypatch.setattr(
        httpx, "post", lambda *a, **kw: posts.append(kw) or httpx.Response(200, json=body)
    )
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: httpx.Response(200, json=body))
    loop, provider = build(store, [CALL, "I did it"], gate)
    result = loop.run_turn(store.create_session(), "send")
    assert result["status"] == status and result["acted"] is False
    assert result["message"] is None and result["notice"]
    assert loop.resume_turn(result["turn_id"])["status"] == status
    assert len(posts) == 1 and len(provider.sent) == 1


@pytest.mark.parametrize("reply_fails", [False, True])
def test_negative_durable_receipt_does_not_mark_acted(tmp_path, monkeypatch, reply_fails):
    store = Store(tmp_path / "pi.db")
    gate = ToolGateClient("http://gate", "agent")
    monkeypatch.setattr(gate, "tools", FakeGate().tools)
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **kw: httpx.Response(
            200, json={"code": "TOOL_UNAVAILABLE", "status": "completed", "result": {"ok": False}}
        ),
    )
    reply = ProviderUnavailable("reply failed") if reply_fails else "Unavailable"
    loop, _ = build(store, [CALL, reply], gate)
    session = store.create_session()
    if reply_fails:
        with pytest.raises(TurnFailed):
            loop.run_turn(session, "send")
    else:
        loop.run_turn(session, "send")
    assert store.turns(session)[0]["acted"] == 0


def test_concurrent_resume_has_one_dispatch_and_stale_failure_cannot_land(tmp_path):
    store = Store(tmp_path / "pi.db")
    gate = FakeGate(needs_approval=True)
    loop, _ = build(store, [CALL, "sent"], gate)
    turn = loop.run_turn(store.create_session(), "send")["turn_id"]
    original = store.get_turn
    barrier = threading.Barrier(2)
    local = threading.local()

    def simultaneous_read(turn_id):
        row = original(turn_id)
        if not getattr(local, "read", False):
            local.read = True
            barrier.wait(timeout=5)
        return row

    store.get_turn = simultaneous_read

    def resume():
        try:
            return loop.resume_turn(turn)["status"]
        except TurnFailed:
            return "lost"

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: resume(), range(2)))
    store.get_turn = original
    assert sorted(results) == ["complete", "lost"]
    assert len(gate.invocations) == 2
    assert not store.finish_turn(turn, "failed")
    assert store.get_turn(turn)["status"] == "complete"


def test_restart_recovers_acted_reply_without_dispatch(tmp_path):
    store = Store(tmp_path / "pi.db")
    turn = store.start_turn(store.create_session())
    store.mark_acted(turn)
    store.mark_interrupted_turns()
    assert [t["id"] for t in store.acted_without_reply()] == [turn]
    gate = FakeGate()
    loop, _ = build(store, ["sent earlier"], gate)
    assert loop.resume_turn(turn)["acted"] is True
    assert gate.invocations == []


def test_restart_with_dispatch_record_holds_for_receipt(tmp_path):
    store = Store(tmp_path / "pi.db")
    turn = store.start_turn(store.create_session())
    actions.prepare(store, turn, "mail", {}, "durable")
    store.mark_interrupted_turns()
    assert store.get_turn(turn)["status"] == "outcome_unknown"
    gate = FakeGate()
    gate.check_action = lambda *a: ToolPending("outcome_unknown", "Check receipt", "durable")
    loop, _ = build(store, [], gate)
    assert loop.resume_turn(turn)["status"] == "outcome_unknown"
    assert gate.invocations == []


def test_budget_denial_without_approval_holds_stable_identity(tmp_path, monkeypatch):
    store = Store(tmp_path / "pi.db")
    sent = []
    gate = ToolGateClient("http://gate", "agent")
    monkeypatch.setattr(gate, "tools", FakeGate().tools)

    def post(url, *, json, **kwargs):
        sent.append(json)
        if not json.get("job_id"):
            return httpx.Response(403, json={"detail": {"code": "BUDGET_DENIED",
                "message": "Use the owner's job"}})
        assert json["job_id"] == "owner-job"
        return httpx.Response(200, json={"code": "OK", "status": "completed",
            "result": {"ok": True, "result": "sent"}})

    monkeypatch.setattr(httpx, "post", post)
    loop, provider = build(store, [CALL, "sent"], gate)
    held = loop.run_turn(store.create_session(), "send")
    assert held["status"] == "awaiting_budget" and held["acted"] is False
    assert len(provider.sent) == 1
    assert store.get_turn(held["turn_id"])["action"]["id"] == held["action_id"]
    assert loop.resume_turn(held["turn_id"], job_id="owner-job")["acted"] is True
    assert len(sent) == 2 and sent[0]["action_id"] == sent[1]["action_id"]


def test_legacy_interrupted_acted_turn_is_in_unreplied_queue(tmp_path):
    store = Store(tmp_path / "pi.db")
    turn = store.start_turn(store.create_session())
    store.finish_turn(turn, "interrupted", acted=1)
    assert [row["id"] for row in store.acted_without_reply()] == [turn]
    loop, _ = build(store, ["done earlier"], FakeGate())
    assert loop.resume_turn(turn)["acted"] is True


def test_forgetting_removes_durable_tool_arguments_but_keeps_identity(tmp_path):
    path = tmp_path / "pi.db"
    store = Store(path)
    session = store.create_session()
    turn = store.start_turn(session)
    actions.prepare(store, turn, "mail", {"text": "private content"}, "durable")
    store.close()
    plan = forgetting.preview(path, session)
    forgetting.forget(path, session, plan["confirmation"])
    store = Store(path)
    action = actions.latest(store, turn)
    assert action["args"] is None and action["id"] == "durable"
    loop, _ = build(store, [], FakeGate())
    with pytest.raises(TurnFailed):
        loop.resume_turn(turn)
    store.close()

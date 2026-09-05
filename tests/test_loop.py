"""Turn loop behaviour, driven by a real provider object rather than a mock.

The fake here implements the same interface as OllamaProvider and is swapped in
the way a real adapter would be, so the loop is exercised through its actual
seam. What it does not do is pretend to succeed when asked to fail.
"""
from __future__ import annotations

import pytest

from pi.loop import Loop, TurnFailed
from pi.providers import Completion, Message, ProviderUnavailable
from pi.store import Store


class Recorder:
    """A provider that answers predictably and records what it was sent."""

    name = "recorder"

    def __init__(self, reply: str = "answered", fail: bool = False) -> None:
        self.reply = reply
        self.fail = fail
        self.calls: list[list[Message]] = []

    def complete(self, messages, *, model):
        self.calls.append(list(messages))
        if self.fail:
            raise ProviderUnavailable("ConnectError")
        return Completion(text=self.reply, model=model, provider=self.name,
                          input_tokens=11, output_tokens=7)

    def health(self):
        return {"status": "ok"}


@pytest.fixture()
def store(tmp_path):
    return Store(tmp_path / "pi.db")


def test_a_turn_appends_the_question_and_the_answer(store):
    provider = Recorder("hello back")
    loop = Loop(store, provider, model="test-model")
    s = store.create_session()

    result = loop.run_turn(s, "hello")

    assert result["session_id"] == s
    assert result["forked_from"] is None
    assert [(m["role"], m["content"]) for m in store.messages(s)] == [
        ("user", "hello"), ("assistant", "hello back"),
    ]


def test_the_turn_records_what_it_cost(store):
    loop = Loop(store, Recorder(), model="test-model")
    s = store.create_session()
    loop.run_turn(s, "hello")

    turn = store.turns(s)[0]
    assert turn["status"] == "complete"
    assert turn["provider"] == "recorder"
    assert turn["input_tokens"] == 11 and turn["output_tokens"] == 7
    assert turn["latency_ms"] is not None
    # No price from this provider means unknown, never zero - zero would read as
    # free, which is a claim the adapter cannot make.
    assert turn["cost_usd"] is None


def test_history_is_resent_in_order(store):
    provider = Recorder()
    loop = Loop(store, provider, model="test-model", system_prompt="be brief")
    s = store.create_session()

    loop.run_turn(s, "first")
    loop.run_turn(s, "second")

    sent = provider.calls[-1]
    assert [(m.role, m.content) for m in sent] == [
        ("system", "be brief"),
        ("user", "first"), ("assistant", "answered"),
        ("user", "second"),
    ]


def test_a_failed_turn_keeps_what_the_owner_said(store):
    """The message was said. A transcript that drops it because the answer
    failed is not a transcript, and the owner would retype into a void."""
    loop = Loop(store, Recorder(fail=True), model="test-model")
    s = store.create_session()

    with pytest.raises(TurnFailed):
        loop.run_turn(s, "did you get this?")

    assert [m["content"] for m in store.messages(s)] == ["did you get this?"]
    turn = store.turns(s)[0]
    assert turn["status"] == "failed"
    assert turn["detail"] == "ConnectError"


def test_a_long_conversation_forks_instead_of_being_truncated(store):
    """Dropping the middle would silently lose what was said; rewriting it would
    break append-only. Forking keeps the lineage walkable."""
    provider = Recorder("ok")
    loop = Loop(store, provider, model="test-model", fork_threshold_chars=200)
    s = store.create_session(title="long one")

    for i in range(12):
        result = loop.run_turn(s, f"message number {i} with some padding to grow history")
        if result["forked_from"]:
            break
    else:
        pytest.fail("history never grew past the fork threshold")

    child, parent = result["session_id"], result["forked_from"]
    assert parent == s
    assert child != s

    assert store.get_session(parent)["status"] == "forked"
    assert store.get_session(parent)["summary"]
    assert store.get_session(child)["parent_id"] == parent
    # the parent's messages are untouched - forking is not deletion
    assert len(store.messages(parent)) > 0


def test_the_child_carries_a_summary_not_the_parents_messages(store):
    loop = Loop(store, Recorder("ok"), model="test-model", fork_threshold_chars=150)
    s = store.create_session()
    for i in range(10):
        result = loop.run_turn(s, f"padding padding padding {i}")
        if result["forked_from"]:
            break

    child = result["session_id"]
    system_texts = [m.content for m in loop._history(child) if m.role == "system"]
    assert any("Earlier in this conversation" in t for t in system_texts)
    assert len(store.messages(child)) < len(store.messages(result["forked_from"]))


def test_forking_still_happens_when_the_summary_cannot_be_written(store):
    """A failed summary must not leave a session that can accept nothing. What
    must not happen is a child claiming context it does not have."""
    loop = Loop(store, Recorder(fail=True), model="test-model")
    s = store.create_session()
    child = loop.fork(s)

    summary = store.get_session(s)["summary"]
    assert "summary unavailable" in summary
    assert "parent session holds the full transcript" in summary
    assert store.get_session(child)["parent_id"] == s


def test_a_closed_session_refuses_new_turns(store):
    loop = Loop(store, Recorder(), model="test-model")
    s = store.create_session()
    store.close_session(s, "closed")
    with pytest.raises(TurnFailed, match="not open"):
        loop.run_turn(s, "hello?")


def test_an_unknown_session_is_refused(store):
    loop = Loop(store, Recorder(), model="test-model")
    with pytest.raises(TurnFailed, match="no such session"):
        loop.run_turn("ses_nope", "hello?")

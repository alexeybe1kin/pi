"""A turn that acts, and a turn that waits.

The parking behaviour is the point: being asked to confirm is not a failure, and
a turn that failed would tell the owner the wrong thing about what happened.
"""
from __future__ import annotations

import pytest

from pi.loop import ActedWithoutReply, Loop, TurnFailed
from pi.providers import Completion, ProviderUnavailable
from pi.routing import Router
from pi.store import Store
from pi.toolgate import ApprovalRequired, Tool, ToolRefused, ToolResult


class Scripted:
    """Answers a fixed script, so a turn's shape is what is under test."""

    name = "scripted"

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.sent: list[list] = []

    def complete(self, messages, *, model):
        self.sent.append(list(messages))
        text = self.replies.pop(0) if self.replies else "done"
        # A scripted exception is a scripted answer: several of these tests are
        # about what is recorded when the model does not answer at all.
        if isinstance(text, Exception):
            raise text
        return Completion(text=text, model=model, provider=self.name)

    def health(self):
        return {"status": "ok"}


class FakeGate:
    def __init__(self, *, needs_approval=False, refuse=None, unavailable=False,
                 always_asks=False):
        self.needs_approval = needs_approval
        self.refuse = refuse
        self.unavailable = unavailable
        # Stands in for an approval that expired between being granted and being
        # spent, which ToolGate answers by asking again rather than by failing.
        self.always_asks = always_asks
        self.invocations: list[tuple] = []
        self.approved: set[str] = set()
        self.requests = 0

    def tools(self):
        if self.unavailable:
            from pi.toolgate import ToolGateUnavailable
            raise ToolGateUnavailable("ConnectError")
        return [Tool("t_echo", "echo", "echoes things", [{"name": "text", "type": "string"}])]

    def invoke(self, tool_id, args, approval_request_id=None, *, action_id=None, job_id=None):
        self.invocations.append((tool_id, args, approval_request_id))
        if self.refuse:
            raise ToolRefused(*self.refuse)
        if self.needs_approval and (approval_request_id is None or self.always_asks):
            self.requests += 1
            return ApprovalRequired(f"req_{self.requests}", "2026-01-01T00:00:00Z",
                                    "Owner confirmation required.", tool_id, args)
        if approval_request_id and approval_request_id in self.approved:
            raise ToolRefused("APPROVAL_INVALID", "confirmation already consumed")
        if approval_request_id:
            self.approved.add(approval_request_id)
        return ToolResult(ok=True, result={"echoed": args}, tool_id=tool_id)

    def health(self):
        return {"status": "ok"}


@pytest.fixture()
def store(tmp_path):
    return Store(tmp_path / "pi.db")


def build(store, replies, gate):
    provider = Scripted(replies)
    router = Router(local_provider=provider, local_model="test-model")
    return Loop(store, router, toolgate=gate), provider


CALL = '{"tool": "t_echo", "args": {"text": "hi"}}'


def test_a_tool_call_runs_and_the_result_comes_back_to_the_model(store):
    gate = FakeGate()
    loop, _ = build(store, [CALL, "the tool said hi"], gate)
    s = store.create_session()

    result = loop.run_turn(s, "use the tool")

    assert gate.invocations == [("t_echo", {"text": "hi"}, None)]
    assert result["message"]["content"] == "the tool said hi"
    roles = [m["role"] for m in store.messages(s)]
    assert roles == ["user", "assistant", "tool", "assistant"]


def test_a_gated_tool_parks_the_turn_instead_of_failing_it(store):
    """The owner has not said no. They have not been asked yet, and a failed
    turn would say something different and worse."""
    gate = FakeGate(needs_approval=True)
    loop, _ = build(store, [CALL, "unreachable"], gate)
    s = store.create_session()

    result = loop.run_turn(s, "use the tool")

    assert result["status"] == "awaiting_approval"
    assert result["message"] is None
    assert result["approval"]["tool_id"] == "t_echo"
    assert result["approval"]["args"] == {"text": "hi"}

    turn = store.turns(s)[0]
    assert turn["status"] == "awaiting_approval"
    assert turn["approval_request_id"] == "req_1"
    # Stored whole, so the retry is the action the owner was shown.
    assert turn["approval_args"] == {"text": "hi"}


def test_a_parked_turn_appears_in_one_queue_across_sessions(store):
    """An approval the owner never sees is an action that silently never happens."""
    gate = FakeGate(needs_approval=True)
    for _ in range(2):
        loop, _ = build(store, [CALL], gate)
        loop.run_turn(store.create_session(), "use the tool")

    pending = store.awaiting_approval()
    assert len(pending) == 2
    assert all(p["approval_tool_id"] == "t_echo" for p in pending)


def test_a_restart_does_not_withdraw_the_question(store):
    """Interrupted and awaiting-approval are different facts. A parked turn is
    waiting on a person, and the process stopping does not change that."""
    gate = FakeGate(needs_approval=True)
    loop, _ = build(store, [CALL], gate)
    s = store.create_session()
    loop.run_turn(s, "use the tool")

    assert store.mark_interrupted_turns() == 0
    assert store.turns(s)[0]["status"] == "awaiting_approval"


def test_resuming_replays_the_exact_stored_action(store):
    """Rebuilding it from the conversation could spend the approval on a
    different action than the one approved."""
    gate = FakeGate(needs_approval=True)
    loop, _ = build(store, [CALL, "done, it echoed"], gate)
    s = store.create_session()
    parked = loop.run_turn(s, "use the tool")

    resumed = loop.resume_turn(parked["turn_id"])

    assert resumed["status"] == "complete"
    assert gate.invocations[-1] == ("t_echo", {"text": "hi"}, "req_1")
    assert resumed["message"]["content"] == "done, it echoed"
    assert store.get_turn(parked["turn_id"])["status"] == "complete"


def test_resuming_twice_fails_closed(store):
    """The nonce is consumed once, server-side. Pi holds no memory of it."""
    gate = FakeGate(needs_approval=True)
    loop, _ = build(store, [CALL, "done", "done again"], gate)
    s = store.create_session()
    parked = loop.run_turn(s, "use the tool")
    loop.resume_turn(parked["turn_id"])

    with pytest.raises(TurnFailed, match="not awaiting approval"):
        loop.resume_turn(parked["turn_id"])


def test_a_refusal_goes_back_to_the_model_rather_than_ending_the_turn(store):
    """A refusal is an answer. It is never retried with the guard removed."""
    gate = FakeGate(refuse=("POLICY_DENIED", "not allowed"))
    loop, _ = build(store, [CALL, "I could not, it was denied"], gate)
    s = store.create_session()

    result = loop.run_turn(s, "use the tool")

    observation = next(m for m in store.messages(s) if m["role"] == "tool")["content"]
    assert "POLICY_DENIED" in observation
    assert result["message"]["content"] == "I could not, it was denied"


def test_a_tool_the_key_is_not_scoped_to_is_never_forwarded(store):
    """ToolGate would refuse it anyway, but forwarding puts an unscoped tool id
    in its audit trail on Pi's authority, which is not Pi's to spend."""
    gate = FakeGate()
    loop, _ = build(store, ['{"tool": "t_secret", "args": {}}'], gate)
    s = store.create_session()

    loop.run_turn(s, "use something else")

    assert gate.invocations == []


def test_prose_about_tools_is_not_a_tool_call(store):
    """A looser parser would act on a sentence describing a tool call."""
    gate = FakeGate()
    chatty = 'I could call {"tool": "t_echo", "args": {}} but I will not.'
    loop, _ = build(store, [chatty], gate)
    s = store.create_session()

    loop.run_turn(s, "what could you do?")

    assert gate.invocations == []


def test_an_unavailable_action_boundary_still_allows_conversation(store):
    """Tools being unreachable is not the turn failing. Conker still talks."""
    gate = FakeGate(unavailable=True)
    loop, _ = build(store, ["I can still talk"], gate)
    s = store.create_session()

    assert loop.run_turn(s, "hello")["message"]["content"] == "I can still talk"


def test_a_model_looping_on_a_tool_is_stopped(store):
    """Not a safety boundary - ToolGate is that - but a model looping on a
    failing tool would otherwise spend indefinitely without ever answering."""
    gate = FakeGate()
    loop, _ = build(store, [CALL] * 12, gate)
    s = store.create_session()

    loop.run_turn(s, "loop forever")

    assert len(gate.invocations) <= 4


# --- an action that happened is never recorded as one that did not ---------
#
# This is the failure the whole product exists to avoid, in its sharpest form:
# the tool ran, the world changed, the approval was spent - and then the model
# call timed out. Recording that as `failed` tells the owner their action did
# not happen. It did.

TIMEOUT = ProviderUnavailable("ReadTimeout")


def test_a_provider_failure_after_a_tool_ran_is_not_a_failed_turn(store):
    gate = FakeGate()
    loop, _ = build(store, [CALL, TIMEOUT], gate)
    s = store.create_session()

    with pytest.raises(ActedWithoutReply):
        loop.run_turn(s, "use the tool")

    turn = store.turns(s)[0]
    assert turn["status"] == "acted_no_reply"
    assert turn["acted"] == 1
    # Once. The failure was the reply, and nothing retried the action.
    assert gate.invocations == [("t_echo", {"text": "hi"}, None)]


def test_a_turn_that_never_acted_still_fails(store):
    """The new status must not swallow the ordinary case. A model that dies
    before touching a tool changed nothing, and `failed` is the truth."""
    gate = FakeGate()
    loop, _ = build(store, [TIMEOUT], gate)
    s = store.create_session()

    with pytest.raises(TurnFailed) as caught:
        loop.run_turn(s, "just talk")

    assert not isinstance(caught.value, ActedWithoutReply)
    turn = store.turns(s)[0]
    assert turn["status"] == "failed"
    assert turn["acted"] == 0


def test_an_approved_action_survives_the_reply_timing_out(store):
    """The exact defect found in the live round trip against real ToolGate."""
    gate = FakeGate(needs_approval=True)
    loop, _ = build(store, [CALL, TIMEOUT], gate)
    s = store.create_session()
    parked = loop.run_turn(s, "use the tool")

    with pytest.raises(ActedWithoutReply):
        loop.resume_turn(parked["turn_id"])

    turn = store.get_turn(parked["turn_id"])
    assert turn["status"] == "acted_no_reply"
    assert turn["acted"] == 1
    # The result itself is in the transcript, so what the tool returned is not
    # lost just because nothing summarised it.
    assert [m["role"] for m in store.messages(s)] == ["user", "assistant", "tool"]


def test_resuming_an_unreplied_turn_asks_only_for_the_reply(store):
    """The approval is spent and the world has already changed. Re-invoking
    would fail closed at best and do the thing twice at worst."""
    gate = FakeGate(needs_approval=True)
    loop, _ = build(store, [CALL, TIMEOUT, "it echoed hi"], gate)
    s = store.create_session()
    parked = loop.run_turn(s, "use the tool")
    with pytest.raises(ActedWithoutReply):
        loop.resume_turn(parked["turn_id"])
    spent = len(gate.invocations)

    resumed = loop.resume_turn(parked["turn_id"])

    assert len(gate.invocations) == spent
    assert resumed["status"] == "complete"
    assert resumed["acted"] is True
    assert resumed["message"]["content"] == "it echoed hi"
    assert store.get_turn(parked["turn_id"])["acted"] == 1


def test_unreplied_turns_are_findable(store):
    """An action whose result the owner never sees is, to them, the same as one
    that silently went wrong."""
    gate = FakeGate()
    loop, _ = build(store, [CALL, TIMEOUT], gate)
    with pytest.raises(ActedWithoutReply):
        loop.run_turn(store.create_session(), "use the tool")

    unreplied = store.acted_without_reply()
    assert len(unreplied) == 1
    assert unreplied[0]["acted"] == 1


def test_a_crash_between_acting_and_replying_says_which_it_was(store):
    """A turn killed after its tool ran is not the same event as one killed
    before, and one message for both would describe the wrong one."""
    acted_turn = store.start_turn(store.create_session())
    quiet_turn = store.start_turn(store.create_session())
    store.mark_acted(acted_turn)

    store.mark_interrupted_turns()

    assert "after this turn acted" in store.get_turn(acted_turn)["detail"]
    assert store.get_turn(acted_turn)["acted"] == 1
    assert "while this turn was running" in store.get_turn(quiet_turn)["detail"]


def test_a_stale_approval_reparks_the_turn_on_the_new_request(store):
    """ToolGate asking again means the approval did not apply. Keeping the dead
    nonce would park the turn behind an id nobody can ever approve - an action
    that can never happen and never says so."""
    gate = FakeGate(needs_approval=True, always_asks=True)
    loop, _ = build(store, [CALL], gate)
    s = store.create_session()
    parked = loop.run_turn(s, "use the tool")

    with pytest.raises(TurnFailed, match="no longer valid"):
        loop.resume_turn(parked["turn_id"])

    turn = store.get_turn(parked["turn_id"])
    assert turn["status"] == "awaiting_approval"
    assert turn["approval_request_id"] == "req_2"
    assert turn["acted"] == 0


# --- provenance ------------------------------------------------------------
#
# An approval that shows only what Conker wants to do cannot actually be judged.
# The owner is deciding whether the action *follows from what they asked*, and
# everything Conker has read since is untrusted - so their own words are the
# only honest anchor to compare it against. This is the whole defence against
# an instruction that arrived inside a web page.


def test_the_approval_carries_what_the_owner_actually_asked(store):
    gate = FakeGate(needs_approval=True)
    loop, _ = build(store, [CALL], gate)
    s = store.create_session()

    parked = loop.run_turn(s, "summarise today's emails")

    assert parked["approval"]["asked"] == "summarise today's emails"
    assert store.get_turn(parked["turn_id"])["approval_intent"] == "summarise today's emails"


def test_intent_is_the_owner_words_not_the_model_output(store):
    """The model's own text is the thing being judged. If it authored the
    provenance too, a manipulated model would write both halves and the
    mismatch that gives the game away would never appear."""
    gate = FakeGate(needs_approval=True)
    loop, _ = build(store, ['{"tool": "t_echo", "args": {"text": "delete everything"}}'], gate)
    s = store.create_session()

    parked = loop.run_turn(s, "what is the weather")

    assert parked["approval"]["asked"] == "what is the weather"
    assert "delete" not in parked["approval"]["asked"]


def test_a_stale_approval_reparks_without_losing_the_intent(store):
    """The owner judging an action for the second time is exactly when they are
    least likely to remember what prompted it.

    This passes today because finish_turn updates only the fields it is given,
    so an unnamed column keeps its value. That is worth pinning: the property
    is what matters, and a future rewrite of the park path that clears these
    fields would break it silently."""
    gate = FakeGate(needs_approval=True, always_asks=True)
    loop, _ = build(store, [CALL], gate)
    s = store.create_session()
    parked = loop.run_turn(s, "reply to mum about Sunday")

    with pytest.raises(TurnFailed, match="no longer valid"):
        loop.resume_turn(parked["turn_id"])

    turn = store.get_turn(parked["turn_id"])
    assert turn["status"] == "awaiting_approval"
    assert turn["approval_intent"] == "reply to mum about Sunday"


def test_the_queue_shows_intent_for_every_parked_turn(store):
    """The queue is where the owner triages. Intent has to be there, not one
    click further in."""
    gate = FakeGate(needs_approval=True)
    for asked in ("book the gym", "email my teacher"):
        loop, _ = build(store, [CALL], gate)
        loop.run_turn(store.create_session(), asked)

    pending = store.awaiting_approval()
    assert {p["approval_intent"] for p in pending} == {"book the gym", "email my teacher"}

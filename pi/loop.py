"""The turn loop: assemble context, call a model, append the result.

Deliberately thin. The loop must behave identically whatever answers it, so
everything provider-specific lives behind the adapter and everything durable
lives in the store. What is left here is the part that must never vary.

Two rules are structural rather than stylistic:

**Append-only.** Nothing here rewrites an earlier message. Current models bind
reasoning blocks to the producing model and reject edited history, and a loop
that rewrites turns breaks against them in ways that surface late and
everywhere at once.

**Fork, never truncate.** When a conversation outgrows its window the session is
closed with a summary and a child opened seeded by it, pointing back at the
parent. Dropping the middle would silently lose what was said; rewriting it
would break the first rule. Forking keeps the lineage walkable, which is also
how MemoryGate treats evidence.
"""
from __future__ import annotations

import time

from .openrouter import ModelUnusable
from .providers import Completion, Message, ProviderUnavailable
from .routing import Router, TurnContext
from .store import Store

# A turn that would exceed this many characters of history triggers a fork.
# Characters, not tokens, on purpose: a tokeniser is provider-specific and this
# threshold only has to be roughly right, while being wrong about which
# tokeniser applies would be quietly wrong for every provider but one.
DEFAULT_FORK_THRESHOLD_CHARS = 24_000


class TurnFailed(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Loop:
    def __init__(self, store: Store, router: Router, *, system_prompt: str = "",
                 fork_threshold_chars: int = DEFAULT_FORK_THRESHOLD_CHARS) -> None:
        self.store = store
        self.router = router
        self.system_prompt = system_prompt
        self.fork_threshold_chars = fork_threshold_chars

    def _call(self, messages: list[Message], ctx: TurnContext):
        """Route, then call, falling through models that refuse to serve us.

        The loop never picks a model itself - it walks the order the router
        gave. Falling through is not a silent retry: what was skipped and why
        is returned and recorded on the turn.
        """
        routes = self.router.candidates(ctx)
        skipped: list[str] = []
        for route in routes:
            provider = self.router.provider_for(route)
            try:
                return route, provider.complete(messages, model=route.model), skipped
            except ModelUnusable as exc:
                skipped.append(exc.reason)
                continue
        raise ProviderUnavailable(
            "; ".join(skipped) if skipped else "no candidate model could be reached"
        )

    # --- context ----------------------------------------------------------

    def _history(self, session_id: str) -> list[Message]:
        session = self.store.get_session(session_id)
        messages: list[Message] = []
        if self.system_prompt:
            messages.append(Message("system", self.system_prompt))
        # A forked child carries its parent's summary as context, not its
        # parent's messages. The messages are still there, in the parent, and
        # still readable - they are simply not resent.
        if session and session.get("summary"):
            messages.append(Message("system", f"Earlier in this conversation:\n{session['summary']}"))
        for row in self.store.messages(session_id):
            content = row["content"]
            messages.append(Message(row["role"], content if isinstance(content, str) else str(content)))
        return messages

    def _history_size(self, messages: list[Message]) -> int:
        return sum(len(m.content) for m in messages)

    # --- forking ----------------------------------------------------------

    def _summarise(self, messages: list[Message]) -> str:
        """Ask the model to summarise, and fall back to a truthful marker.

        If summarising fails the fork still happens: the alternative is a
        session that cannot accept another message. What must not happen is a
        child that claims to carry context it does not have, so the fallback
        says plainly that the summary is missing.
        """
        transcript = "\n".join(f"{m.role}: {m.content}" for m in messages if m.role != "system")
        ask = [
            Message("system", "Summarise the conversation so far. Keep decisions, facts and open "
                              "questions. Be brief and concrete."),
            Message("user", transcript[-self.fork_threshold_chars:]),
        ]
        try:
            # Summarising is analysis, not conversation, so it routes as such.
            _, completion, _ = self._call(ask, TurnContext(is_analysis=True))
            return completion.text.strip()
        except (ProviderUnavailable, RuntimeError) as exc:
            reason = getattr(exc, "reason", type(exc).__name__)
            return (f"[summary unavailable: {reason}] The parent session holds the full "
                    f"transcript and is linked from this one.")

    def fork(self, session_id: str) -> str:
        """Close a session with a summary and open a child seeded by it."""
        summary = self._summarise(self._history(session_id))
        parent = self.store.get_session(session_id) or {}
        self.store.close_session(session_id, "forked", summary=summary)
        return self.store.create_session(
            title=parent.get("title", ""), parent_id=session_id, summary=summary,
        )

    # --- the turn ---------------------------------------------------------

    def run_turn(self, session_id: str, user_text: str, context: dict | None = None) -> dict:
        """One turn. Returns the assistant message and where it landed.

        The session id may change: if history has outgrown the window the turn
        forks first and runs in the child. Callers are told which session
        answered rather than left to assume it was the one they asked.
        """
        session = self.store.get_session(session_id)
        if session is None:
            raise TurnFailed(f"no such session: {session_id}")
        if session["status"] != "open":
            raise TurnFailed(f"session {session_id} is {session['status']}, not open")

        forked_from = None
        if self._history_size(self._history(session_id)) + len(user_text) > self.fork_threshold_chars:
            forked_from, session_id = session_id, self.fork(session_id)

        self.store.append_message(session_id, "user", user_text)
        turn_id = self.store.start_turn(session_id)
        started = time.monotonic()
        history = self._history(session_id)
        ctx = TurnContext(history_chars=self._history_size(history), **(context or {}))

        try:
            route, completion, skipped = self._call(history, ctx)
        except (ProviderUnavailable, RuntimeError) as exc:
            # The user's message stays. It was said, and a transcript that drops
            # what was said because the answer failed is not a transcript.
            self.store.finish_turn(turn_id, "failed", detail=getattr(exc, "reason", type(exc).__name__),
                                   latency_ms=int((time.monotonic() - started) * 1000))
            raise TurnFailed(getattr(exc, "reason", type(exc).__name__)) from exc

        message = self.store.append_message(session_id, "assistant", completion.text)
        self.store.finish_turn(
            turn_id, "complete",
            provider=completion.provider, model=completion.model,
            input_tokens=completion.input_tokens, output_tokens=completion.output_tokens,
            cached_tokens=completion.cached_tokens, cost_usd=completion.cost_usd,
            latency_ms=int((time.monotonic() - started) * 1000),
            # Recorded so the policy can be tuned against outcomes rather than
            # opinion: a cheap model that fails and escalates has cost both.
            route_tier=route.tier.value, route_reason=route.reason.value,
            # Which candidates refused, so a model that always refuses is
            # visible in the record rather than only as latency.
            detail="; ".join(skipped) or None,
        )
        return {"session_id": session_id, "forked_from": forked_from, "turn_id": turn_id,
                "route": {"tier": route.tier.value, "reason": route.reason.value,
                          "provider": route.provider, "model": route.model,
                          "escalated": route.escalated, "skipped": skipped},
                "message": message}

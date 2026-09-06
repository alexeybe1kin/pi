"""Routing policy, and the guard on the owner's money.

The cost guard is the part worth being strict about: it decides whether a typo
in a model name becomes a bill. It is tested here as behaviour, not as a
configuration flag that happens to be set.
"""
from __future__ import annotations

import pytest

from pi.openrouter import ModelInfo, ModelUnusable, OpenRouterProvider, PaidModelRefused
from pi.providers import Completion, Message
from pi.routing import Reason, Router, Tier, TurnContext


class FakeLocal:
    name = "ollama"

    def complete(self, messages, *, model):
        return Completion(text="local answer", model=model, provider=self.name)

    def health(self):
        return {"status": "ok"}


class FakeHosted:
    """A hosted provider with a fixed catalogue, exercising the same seam."""

    name = "openrouter"

    def __init__(self, models: list[ModelInfo]) -> None:
        self._models = models
        self.asked_for: list[str] = []

    def free_models(self, *, needs_tools: bool = False):
        models = [m for m in self._models if m.is_free]
        if needs_tools:
            models = [m for m in models if m.supports_tools]
        return sorted(models, key=lambda m: -m.context_length)

    def complete(self, messages, *, model):
        self.asked_for.append(model)
        return Completion(text="hosted answer", model=model, provider=self.name)

    def health(self):
        return {"status": "ok"}


def free(model_id, ctx=100_000, tools=False):
    return ModelInfo(model_id, ctx, 0.0, 0.0, tools)


def paid(model_id, ctx=100_000, tools=True):
    return ModelInfo(model_id, ctx, 3e-6, 15e-6, tools)


# --- the policy ---------------------------------------------------------


def test_ordinary_conversation_stays_local(store=None):
    """The cheapest thing that can answer, answers. Escalation is the exception."""
    router = Router(local_provider=FakeLocal(), hosted_provider=FakeHosted([free("a")]),
                    local_model="qwen3:4b")
    route = router.route(TurnContext())
    assert route.tier is Tier.LOCAL
    assert route.reason is Reason.DEFAULT
    assert route.escalated is False


@pytest.mark.parametrize("ctx,expected", [
    (TurnContext(owner_requested_strong=True), Reason.OWNER_ASKED),
    (TurnContext(needs_tools=True), Reason.TOOLS_REQUIRED),
    (TurnContext(previous_attempt_failed=True), Reason.RETRY_AFTER_FAILURE),
    (TurnContext(is_analysis=True), Reason.ANALYSIS),
    (TurnContext(history_chars=50_000), Reason.LONG_CONTEXT),
])
def test_each_signal_escalates_and_says_why(ctx, expected):
    """Every escalation carries its reason. A policy nobody can measure drifts
    into always escalating, and features.md A6 names the trap: a cheap model
    that fails and then escalates has cost both."""
    router = Router(local_provider=FakeLocal(), hosted_provider=FakeHosted([free("big", 200_000)]),
                    local_model="qwen3:4b")
    route = router.route(ctx)
    assert route.reason is expected
    assert route.escalated is True
    assert route.tier is Tier.STRONG


def test_the_owners_request_outranks_every_other_signal():
    router = Router(local_provider=FakeLocal(), hosted_provider=FakeHosted([free("a")]),
                    local_model="qwen3:4b")
    route = router.route(TurnContext(owner_requested_strong=True, is_analysis=True,
                                     needs_tools=True, history_chars=99_999))
    assert route.reason is Reason.OWNER_ASKED


def test_escalation_prefers_the_widest_free_context():
    hosted = FakeHosted([free("small", 8_000), free("huge", 1_000_000), free("mid", 128_000)])
    router = Router(local_provider=FakeLocal(), hosted_provider=hosted, local_model="qwen3:4b")
    assert router.route(TurnContext(is_analysis=True)).model == "huge"


def test_a_turn_needing_tools_gets_a_model_that_has_them():
    hosted = FakeHosted([free("no-tools", 1_000_000), free("with-tools", 32_000, tools=True)])
    router = Router(local_provider=FakeLocal(), hosted_provider=hosted, local_model="qwen3:4b")
    assert router.route(TurnContext(needs_tools=True)).model == "with-tools"


def test_no_hosted_provider_still_answers_and_keeps_the_reason():
    """Escalation wanted and unavailable is a different fact from never wanted,
    and the record has to be able to tell them apart later."""
    router = Router(local_provider=FakeLocal(), hosted_provider=None, local_model="qwen3:4b")
    route = router.route(TurnContext(is_analysis=True))
    assert route.tier is Tier.LOCAL
    assert route.reason is Reason.ANALYSIS


def test_no_provider_at_all_is_an_error_not_a_guess():
    with pytest.raises(RuntimeError, match="no provider"):
        Router().route(TurnContext())


# --- the money guard ----------------------------------------------------


class Catalogued(OpenRouterProvider):
    """Real provider, catalogue supplied rather than fetched, so the guard is
    exercised without a network call."""

    def __init__(self, models, **kwargs):
        super().__init__("test-key", **kwargs)
        self._catalogue = {m.id: m for m in models}
        self._fetched_at = float("inf")

    def catalogue(self, *, force: bool = False):
        return self._catalogue


def test_a_paid_model_is_refused_by_default():
    """Free by default is a promise about someone else's money, so it is
    enforced in code rather than left to configuration."""
    provider = Catalogued([free("cheap"), paid("expensive")])
    with pytest.raises(PaidModelRefused, match="not free"):
        provider.complete([Message("user", "hi")], model="expensive")


def test_a_paid_model_runs_only_when_opted_into():
    provider = Catalogued([paid("expensive")], allow_paid=True)
    # It gets past the guard; the network call is what fails, not the policy.
    with pytest.raises(Exception) as exc:
        provider.complete([Message("user", "hi")], model="expensive")
    assert not isinstance(exc.value, PaidModelRefused)


def test_an_unknown_model_is_refused_rather_than_called_blind():
    """A typo must not become a bill. Nothing outside the catalogue is called."""
    provider = Catalogued([free("cheap")], allow_paid=True)
    with pytest.raises(PaidModelRefused, match="not in the catalogue"):
        provider.complete([Message("user", "hi")], model="clude-oups-5")


def test_an_unparseable_price_counts_as_not_free():
    """Treating an unreadable price as zero is exactly the assumption that
    turns into a bill."""
    weird = ModelInfo("weird", 1000, float("inf"), float("inf"), False)
    provider = Catalogued([weird])
    assert weird.is_free is False
    with pytest.raises(PaidModelRefused):
        provider.complete([Message("user", "hi")], model="weird")


# --- falling through models that refuse to serve us ---------------------


class Gated(FakeHosted):
    """Some free models are gated to particular clients and answer 403.

    Found by running it: OpenRouter listed two models at zero price that
    replied "only available on agentic harnesses". Listed and free does not
    mean callable.
    """

    def __init__(self, models, gated: set[str]):
        super().__init__(models)
        self.gated = gated

    def complete(self, messages, *, model):
        self.asked_for.append(model)
        if model in self.gated:
            raise ModelUnusable(f"{model}: HTTP 403")
        return Completion(text="hosted answer", model=model, provider=self.name)


def test_candidates_are_ordered_and_end_with_the_local_fallback():
    hosted = FakeHosted([free("a", 300_000), free("b", 200_000), free("c", 100_000)])
    router = Router(local_provider=FakeLocal(), hosted_provider=hosted, local_model="qwen3:4b")
    routes = router.candidates(TurnContext(is_analysis=True))
    assert [r.model for r in routes[:3]] == ["a", "b", "c"]
    # A weaker answer beats none, so local is last rather than absent.
    assert routes[-1].model == "qwen3:4b"


def test_an_ordinary_turn_does_not_enumerate_hosted_models():
    """Nothing to fall through to when the cheapest option is already chosen."""
    router = Router(local_provider=FakeLocal(), hosted_provider=FakeHosted([free("a")]),
                    local_model="qwen3:4b")
    assert len(router.candidates(TurnContext())) == 1

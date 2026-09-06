"""Which model answers, and why.

Routing is not a feature - it is the cost structure of a system that runs all
day. A cheap model carries ordinary conversation and escalates only on explicit
signals, and every escalation is recorded, because `features.md` A6 names the
real trap: a cheap model that fails and then escalates has cost both, and a
policy nobody measures drifts into always escalating.

The signals are deliberately crude. What has to exist from the first checkpoint
is the *seam* - routing decided in one place, recorded per turn, changeable
without touching the loop. Tuning it against real outcomes comes later, and
needs the record this produces.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Tier(StrEnum):
    LOCAL = "local"      # free, on the box, no network
    CHEAP = "cheap"      # free tier, hosted
    STRONG = "strong"    # the best available under the current spend policy


class Reason(StrEnum):
    DEFAULT = "default"
    TOOLS_REQUIRED = "tools_required"
    RETRY_AFTER_FAILURE = "retry_after_failure"
    ANALYSIS = "analysis"
    OWNER_ASKED = "owner_asked"
    LONG_CONTEXT = "long_context"


@dataclass(frozen=True)
class Route:
    tier: Tier
    reason: Reason
    provider: str
    model: str

    @property
    def escalated(self) -> bool:
        return self.reason is not Reason.DEFAULT


@dataclass(frozen=True)
class TurnContext:
    """What the router is allowed to know. Deliberately small.

    A router that inspects message text would become a classifier nobody
    evaluates. These are facts the loop already has.
    """
    history_chars: int = 0
    needs_tools: bool = False
    is_analysis: bool = False
    owner_requested_strong: bool = False
    previous_attempt_failed: bool = False


class Router:
    def __init__(self, *, local_provider=None, hosted_provider=None,
                 local_model: str = "", escalate_above_chars: int = 12_000) -> None:
        self.local = local_provider
        self.hosted = hosted_provider
        self.local_model = local_model
        self.escalate_above_chars = escalate_above_chars

    # --- the policy -------------------------------------------------------

    def _reason(self, ctx: TurnContext) -> Reason:
        # Ordered by how strongly each implies a stronger model is worth the
        # cost, most decisive first.
        if ctx.owner_requested_strong:
            return Reason.OWNER_ASKED
        if ctx.needs_tools:
            return Reason.TOOLS_REQUIRED
        if ctx.previous_attempt_failed:
            return Reason.RETRY_AFTER_FAILURE
        if ctx.is_analysis:
            return Reason.ANALYSIS
        if ctx.history_chars > self.escalate_above_chars:
            return Reason.LONG_CONTEXT
        return Reason.DEFAULT

    def candidates(self, ctx: TurnContext | None = None, limit: int = 4) -> list[Route]:
        """Routes to try, in order.

        More than one because a model can be listed, priced at zero, and still
        refuse to serve us - some free models are gated to particular clients.
        The caller walks this list rather than failing the turn on the first
        refusal, and records which one answered.
        """
        primary = self.route(ctx)
        routes = [primary]
        if primary.tier is not Tier.LOCAL and self.hosted is not None:
            ctx = ctx or TurnContext()
            for info in self.hosted.free_models(needs_tools=ctx.needs_tools)[1:limit]:
                routes.append(Route(primary.tier, primary.reason, self.hosted.name, info.id))
        # Local is the last resort when it exists: a weaker answer beats none.
        if primary.tier is not Tier.LOCAL and self.local is not None and self.local_model:
            routes.append(Route(Tier.LOCAL, primary.reason, self.local.name, self.local_model))
        return routes

    def route(self, ctx: TurnContext | None = None) -> Route:
        ctx = ctx or TurnContext()
        reason = self._reason(ctx)

        if reason is Reason.DEFAULT and self.local is not None and self.local_model:
            return Route(Tier.LOCAL, reason, self.local.name, self.local_model)

        if self.hosted is None:
            # Nothing to escalate to. Answering locally is better than refusing,
            # but the reason is kept so the record shows the escalation was
            # wanted and unavailable rather than never considered.
            if self.local is None or not self.local_model:
                raise RuntimeError("no provider is configured")
            return Route(Tier.LOCAL, reason, self.local.name, self.local_model)

        needs_tools = ctx.needs_tools
        candidates = self.hosted.free_models(needs_tools=needs_tools)
        if not candidates and needs_tools:
            # No free model can call tools. Say so by falling back rather than
            # silently answering without the tools the turn needed.
            candidates = self.hosted.free_models()
        if not candidates:
            if self.local is not None and self.local_model:
                return Route(Tier.LOCAL, reason, self.local.name, self.local_model)
            raise RuntimeError("no free hosted model is available and there is no local fallback")

        tier = Tier.STRONG if reason is not Reason.DEFAULT else Tier.CHEAP
        return Route(tier, reason, self.hosted.name, candidates[0].id)

    def provider_for(self, route: Route):
        if route.provider == getattr(self.local, "name", None):
            return self.local
        return self.hosted

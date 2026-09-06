"""OpenRouter adapter, with free-model discovery.

Conker ships free by default: a fresh install works with no payment and no key
beyond what free tiers need, and paid models are the owner's own, added
deliberately. That is a decision about someone else's money, so it is enforced
in code rather than left to configuration.

`allow_paid` defaults to False and every call checks the resolved model against
the discovered free set. Asking for a paid model without opting in raises rather
than quietly spending; there is no path where a typo in a model name becomes a
bill.

Models are discovered, not hardcoded. A hardcoded list is stale within weeks -
OpenRouter carried 431 models and 22 free ones the day this was written, and
both numbers move.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from .providers import Completion, Message, ProviderUnavailable

CATALOGUE_URL = "https://openrouter.ai/api/v1/models"
CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
CATALOGUE_TTL_SECONDS = 900.0


@dataclass(frozen=True)
class ModelInfo:
    id: str
    context_length: int
    prompt_usd_per_token: float
    completion_usd_per_token: float
    supports_tools: bool

    @property
    def is_free(self) -> bool:
        return self.prompt_usd_per_token == 0.0 and self.completion_usd_per_token == 0.0


def _price(pricing: dict, key: str) -> float:
    try:
        return float(pricing.get(key) or 0.0)
    except (TypeError, ValueError):
        # An unparseable price is not free. Treating it as zero is exactly the
        # assumption that turns into a bill.
        return float("inf")


class PaidModelRefused(RuntimeError):
    """Asked for a model that costs money without allow_paid."""


class ModelUnusable(ProviderUnavailable):
    """This model will not serve us, but another might.

    Being listed and priced at zero does not mean callable: some free models are
    gated to particular clients and answer 403 with an explanation. That is a
    fact about one model, not about the provider or the key, so the caller
    should try the next candidate rather than fail the turn.
    """


class OpenRouterProvider:
    name = "openrouter"

    def __init__(self, api_key: str, *, allow_paid: bool = False, timeout: float = 120.0) -> None:
        self.api_key = api_key
        self.allow_paid = allow_paid
        self.timeout = timeout
        self._catalogue: dict[str, ModelInfo] = {}
        self._fetched_at = 0.0

    # --- discovery --------------------------------------------------------

    def catalogue(self, *, force: bool = False) -> dict[str, ModelInfo]:
        now = time.monotonic()
        if self._catalogue and not force and now - self._fetched_at < CATALOGUE_TTL_SECONDS:
            return self._catalogue
        try:
            response = httpx.get(CATALOGUE_URL, headers=self._headers(), timeout=30.0)
            response.raise_for_status()
            data = response.json()["data"]
        except Exception as exc:
            raise ProviderUnavailable(f"catalogue unavailable: {type(exc).__name__}") from exc

        catalogue: dict[str, ModelInfo] = {}
        for entry in data:
            arch = entry.get("architecture") or {}
            inputs = arch.get("input_modalities") or []
            outputs = arch.get("output_modalities") or []
            # Text in, text *only* out. Some zero-priced entries are audio or
            # image generators - Google's Lyria outputs ["text", "audio"] - and
            # routing a conversation into one would be a strange failure to
            # diagnose from the answer alone.
            if "text" not in inputs or outputs != ["text"]:
                continue
            pricing = entry.get("pricing") or {}
            catalogue[entry["id"]] = ModelInfo(
                id=entry["id"],
                context_length=int(entry.get("context_length") or 0),
                prompt_usd_per_token=_price(pricing, "prompt"),
                completion_usd_per_token=_price(pricing, "completion"),
                supports_tools="tools" in (entry.get("supported_parameters") or []),
            )
        self._catalogue = catalogue
        self._fetched_at = now
        return catalogue

    def free_models(self, *, needs_tools: bool = False) -> list[ModelInfo]:
        """Free chat models, widest context first."""
        models = [m for m in self.catalogue().values() if m.is_free]
        if needs_tools:
            models = [m for m in models if m.supports_tools]
        return sorted(models, key=lambda m: -m.context_length)

    # --- completion -------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            # OpenRouter attributes traffic by these; they are not credentials.
            "HTTP-Referer": "https://github.com/alexeybe1kin/conker",
            "X-Title": "Conker",
        }

    def _guard_cost(self, model: str) -> ModelInfo:
        info = self.catalogue().get(model)
        if info is None:
            raise PaidModelRefused(
                f"{model!r} is not in the catalogue of text models; refusing to call it blind"
            )
        if not info.is_free and not self.allow_paid:
            raise PaidModelRefused(
                f"{model!r} is not free and allow_paid is off. "
                "Set PI_ALLOW_PAID_MODELS=1 to spend deliberately."
            )
        return info

    def complete(self, messages: list[Message], *, model: str) -> Completion:
        info = self._guard_cost(model)
        payload = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        try:
            response = httpx.post(CHAT_URL, json=payload, headers=self._headers(),
                                  timeout=self.timeout)
        except Exception as exc:
            raise ProviderUnavailable(type(exc).__name__) from exc

        if response.status_code in (403, 404, 400):
            # About this model, not about us. 401 and 429 are deliberately not
            # here: a bad key or an exhausted quota will fail identically on
            # every candidate, and walking the whole catalogue to rediscover
            # that would be slow and would look like the models were at fault.
            raise ModelUnusable(f"{model}: HTTP {response.status_code}")
        try:
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            raise ProviderUnavailable(type(exc).__name__) from exc

        choices = body.get("choices") or []
        if not choices:
            raise ProviderUnavailable("provider returned no choices")
        text = (choices[0].get("message") or {}).get("content")
        if not isinstance(text, str):
            raise ProviderUnavailable("provider returned no message content")

        usage = body.get("usage") or {}
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        cost = None
        if prompt_tokens is not None and completion_tokens is not None:
            cost = (prompt_tokens * info.prompt_usd_per_token
                    + completion_tokens * info.completion_usd_per_token)

        return Completion(
            text=text,
            model=body.get("model", model),
            provider=self.name,
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
            cached_tokens=(usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
            cost_usd=cost,
            raw={"finish_reason": choices[0].get("finish_reason")},
        )

    def health(self) -> dict:
        if not self.api_key:
            return {"status": "not_configured", "reason": "no API key"}
        try:
            free = self.free_models()
        except ProviderUnavailable as exc:
            return {"status": "unavailable", "reason": exc.reason}
        if not free:
            return {"status": "degraded", "reason": "no free text models available"}
        return {"status": "ok"}

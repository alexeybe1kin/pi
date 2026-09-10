"""`ok` must mean the model that answers turns is actually there.

The audit found that `if not models` treated *any* installed model as
readiness. An install where the embedding model downloaded and the chat model
did not would report `ok` while being unable to answer a single turn - the
health contract claiming readiness from a proxy instead of from the thing that
has to work.
"""
from __future__ import annotations

import httpx
import pytest

from pi.providers import OllamaProvider, _installed


class Tags:
    """Stands in for Ollama's /api/tags."""

    def __init__(self, names):
        self.names = names

    def __call__(self, url, timeout=None):
        request = httpx.Request("GET", url)
        return httpx.Response(
            200, json={"models": [{"name": n} for n in self.names]}, request=request
        )


@pytest.fixture()
def tags(monkeypatch):
    def install(names):
        monkeypatch.setattr(httpx, "get", Tags(names))
    return install


def test_the_configured_model_missing_is_not_ok(tags):
    """The exact case: embeddings arrived, the chat model did not."""
    tags(["qwen3-embedding:0.6b"])
    health = OllamaProvider("http://ollama:11434", model="qwen3:4b").health()

    assert health["status"] == "not_configured"
    assert "qwen3:4b" in health["reason"]


def test_the_configured_model_present_is_ok(tags):
    tags(["qwen3-embedding:0.6b", "qwen3:4b"])
    assert OllamaProvider("http://ollama:11434", model="qwen3:4b").health()["status"] == "ok"


def test_no_models_at_all_still_reports_not_configured(tags):
    tags([])
    health = OllamaProvider("http://ollama:11434", model="qwen3:4b").health()
    assert health["status"] == "not_configured"


def test_an_unreachable_ollama_is_unavailable_not_misconfigured(monkeypatch):
    """Unreachable and not-installed are different facts, and the owner needs
    different actions for each."""
    def boom(url, timeout=None):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(httpx, "get", boom)

    assert OllamaProvider("http://ollama:11434", model="qwen3:4b").health()["status"] == "unavailable"


def test_a_bare_name_matches_any_tag_but_an_explicit_tag_does_not():
    """Ollama serves a bare `qwen3` from `qwen3:latest`, so a bare request is
    satisfied by any tag. An explicit tag must match exactly - being lax there
    would recreate the same bug in miniature."""
    assert _installed("qwen3", ["qwen3:4b"])
    assert not _installed("qwen3:4b", ["qwen3:8b"])
    assert not _installed("qwen3:4b", ["qwen3-embedding:0.6b"])

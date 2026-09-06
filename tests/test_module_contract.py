"""The Conker module contract, asserted against a running module.

This file is identical in every module except SERVICE and DEFAULT_URL. A
convention nobody checks decays into a convention nobody follows, and the point
of the contract is that one dashboard renders any module with no special cases -
so the shape is verified rather than trusted.

It talks HTTP to a running service on purpose. The contract is an HTTP contract,
so asserting it in-process would check a Python object while the thing that
ships is a container - and it would inherit whatever global state earlier tests
left behind, which is exactly how the first version of this file broke.

    docker compose up -d && pytest tests/test_module_contract.py

Skips when nothing is listening. CI brings the stack up first.
Contract: docs/module-contract.md in the conker repository.
"""
from __future__ import annotations

import os
import re
from datetime import datetime

import httpx
import pytest

SERVICE = "pi"
DEFAULT_URL = "http://127.0.0.1:8050/health"

CHECK_STATUSES = {"ok", "degraded", "unavailable", "not_configured", "unknown"}
HEALTHY = {"ok", "not_configured"}
REQUIRED = {"service", "version", "status", "degraded", "checks", "checked_at", "age_seconds"}


@pytest.fixture(scope="module")
def health() -> dict:
    url = os.environ.get("CONTRACT_HEALTH_URL", DEFAULT_URL)
    try:
        response = httpx.get(url, timeout=5.0)
    except httpx.HTTPError as exc:
        pytest.skip(f"no service at {url}: {type(exc).__name__}")
    assert response.status_code == 200, f"/health must answer 200, got {response.status_code}"
    return response.json()


def test_health_needs_no_credentials(health):
    """A caller that cannot authenticate must still be able to see liveness."""
    assert health


def test_every_required_field_is_present(health):
    missing = REQUIRED - set(health)
    assert not missing, f"missing contract fields: {sorted(missing)}"


def test_checks_is_keyed_by_name(health):
    """Never a list.

    A caller must be able to ask for one dependency without scanning, and a
    dashboard must not need per-module code to find one.
    """
    assert isinstance(health["checks"], dict)
    for name, check in health["checks"].items():
        assert isinstance(name, str) and name
        assert isinstance(check, dict)
        assert check["status"] in CHECK_STATUSES, f"{name}: {check['status']!r}"


def test_status_follows_from_the_checks(health):
    """`degraded` is derived, not asserted, and not_configured is not a failure."""
    expected = sorted(n for n, c in health["checks"].items() if c["status"] not in HEALTHY)
    assert health["degraded"] == expected
    assert health["status"] == ("degraded" if expected else "ok")


def test_checked_at_is_a_real_timestamp_with_a_timezone(health):
    parsed = datetime.fromisoformat(health["checked_at"])
    assert parsed.tzinfo is not None, "checked_at must carry a timezone"
    assert isinstance(health["age_seconds"], (int, float))
    assert health["age_seconds"] >= 0


def test_reason_never_leaks_to_an_unauthenticated_caller(health):
    """/health takes no key, so no check may carry host specifics.

    What is forbidden is host detail - absolute paths, URLs, sockets, stack
    traces. An identifier that merely contains a slash, such as the model name
    "sentence-transformers/all-MiniLM-L6-v2", is public and useful; rejecting
    every slash would push modules toward saying less than they safely could.
    """
    for name, check in health["checks"].items():
        reason = str(check.get("reason", ""))
        assert not reason.startswith("/"), f"{name} reason is an absolute path: {reason!r}"
        assert " /" not in reason, f"{name} reason embeds an absolute path: {reason!r}"
        assert not re.search(r"[A-Za-z]:\\\\", reason), \
            f"{name} reason embeds a Windows path: {reason!r}"
        assert "://" not in reason, f"{name} reason embeds a URL: {reason!r}"
        assert "Traceback" not in reason
        assert len(reason) <= 120, f"{name} reason is too long to be coarse: {reason!r}"


def test_service_and_version_identify_this_module(health):
    assert health["service"] == SERVICE
    assert isinstance(health["version"], str) and health["version"]

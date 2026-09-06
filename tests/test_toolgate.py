"""The action boundary, driven against a real HTTP server.

The approval round-trip is the most load-bearing security property in the whole
product, so nothing here is mocked at the client level: a real server on a real
socket answers exactly what ToolGate answers, including the replay.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pi.toolgate import (
    ApprovalRequired,
    ToolGateClient,
    ToolGateUnavailable,
    ToolRefused,
    ToolResult,
)

KEY = "exec-key-for-tests"


class FakeToolGate(BaseHTTPRequestHandler):
    """Answers the shapes ToolGate actually answers.

    consumed tracks spent approvals so a replay fails the way the real one does,
    which is the behaviour worth testing rather than asserting.
    """

    tools: list[dict] = []
    needs_approval: set[str] = set()
    consumed: set[str] = set()
    lockdown: bool = False
    seen_keys: list[str] = []

    def _send(self, code: int, body):
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        type(self).seen_keys.append(self.headers.get("X-ToolGate-Execution-Key", ""))
        if not self.headers.get("X-ToolGate-Execution-Key"):
            return self._send(401, {"detail": {"code": "UNAUTHENTICATED", "message": "no key"}})
        if self.path.endswith("/v2/agent/tools"):
            return self._send(200, type(self).tools)
        if self.path.endswith("/v2/agent/status"):
            return self._send(200, {"code": "OK", "lockdown": type(self).lockdown})
        self._send(404, {"detail": {"code": "NOT_FOUND", "message": "no"}})

    def do_POST(self):
        cls = type(self)
        cls.seen_keys.append(self.headers.get("X-ToolGate-Execution-Key", ""))
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or "{}")
        tool_id = self.path.split("/v2/tools/")[1].split("/invoke")[0]

        if cls.lockdown:
            return self._send(423, {"detail": {"code": "LOCKED_DOWN",
                                               "message": "ToolGate is in lockdown mode"}})
        if tool_id in cls.needs_approval:
            approval = body.get("approval_request_id")
            if not approval:
                return self._send(200, {"code": "CONFIRMATION_REQUIRED",
                                        "message": "This exact action is queued for owner review.",
                                        "request_id": "req_1",
                                        "expires_at": "2026-01-01T00:00:00Z"})
            if approval in cls.consumed:
                # What a replay looks like from the real service.
                return self._send(409, {"detail": {"code": "APPROVAL_INVALID",
                                                   "message": "confirmation already consumed"}})
            cls.consumed.add(approval)
        self._send(200, {"ok": True, "result": {"echoed": body.get("args")}})

    def log_message(self, *args):
        pass


@pytest.fixture()
def client():
    FakeToolGate.tools = [{"id": "t_echo", "name": "echo", "description": "echoes", "inputs": []}]
    FakeToolGate.needs_approval = set()
    FakeToolGate.consumed = set()
    FakeToolGate.lockdown = False
    FakeToolGate.seen_keys = []

    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeToolGate)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield ToolGateClient(f"http://127.0.0.1:{server.server_port}", KEY)
    server.shutdown()
    server.server_close()


def test_only_scoped_tools_are_visible(client):
    """ToolGate decides what this key may see. Pi does not filter, and could
    not: it has no idea what the owner scoped anyone to."""
    tools = client.tools()
    assert [t.id for t in tools] == ["t_echo"]


def test_every_request_carries_the_execution_key(client):
    """Not the MCP bridge, which has no identity at all - ADR-0005."""
    client.tools()
    client.invoke("t_echo", {"a": 1})
    assert FakeToolGate.seen_keys and all(k == KEY for k in FakeToolGate.seen_keys)


def test_an_ordinary_tool_runs(client):
    result = client.invoke("t_echo", {"a": 1})
    assert isinstance(result, ToolResult)
    assert result.ok and result.result == {"echoed": {"a": 1}}


def test_a_gated_tool_asks_rather_than_fails(client):
    """Being asked to confirm is not a failure. The owner has not said no -
    they have not been asked yet, so the turn parks."""
    FakeToolGate.needs_approval = {"t_echo"}
    outcome = client.invoke("t_echo", {"a": 1})
    assert isinstance(outcome, ApprovalRequired)
    assert outcome.request_id == "req_1"
    assert outcome.tool_id == "t_echo" and outcome.args == {"a": 1}


def test_an_approval_lets_the_exact_action_run(client):
    FakeToolGate.needs_approval = {"t_echo"}
    asked = client.invoke("t_echo", {"a": 1})
    result = client.invoke("t_echo", {"a": 1}, approval_request_id=asked.request_id)
    assert isinstance(result, ToolResult) and result.ok


def test_replaying_an_approval_fails_closed(client):
    """One approval authorises one action, consumed once.

    The single most load-bearing property in the product: if a spent approval
    could be replayed, every gate in the system would be advisory.
    """
    FakeToolGate.needs_approval = {"t_echo"}
    asked = client.invoke("t_echo", {"a": 1})
    client.invoke("t_echo", {"a": 1}, approval_request_id=asked.request_id)

    with pytest.raises(ToolRefused) as exc:
        client.invoke("t_echo", {"a": 1}, approval_request_id=asked.request_id)
    assert exc.value.code == "APPROVAL_INVALID"


def test_pi_holds_no_memory_of_a_past_approval(client):
    """There is no always-allow. A second identical action asks again, because
    the approval bound to the first one and was spent on it."""
    FakeToolGate.needs_approval = {"t_echo"}
    asked = client.invoke("t_echo", {"a": 1})
    client.invoke("t_echo", {"a": 1}, approval_request_id=asked.request_id)

    again = client.invoke("t_echo", {"a": 1})
    assert isinstance(again, ApprovalRequired), "a spent approval must not carry forward"


def test_lockdown_is_refused_not_worked_around(client):
    FakeToolGate.lockdown = True
    with pytest.raises(ToolRefused) as exc:
        client.invoke("t_echo", {"a": 1})
    assert exc.value.code == "LOCKED_DOWN"


def test_lockdown_shows_as_degraded_rather_than_ok(client):
    """Reachable and deliberately refusing everything is the most important
    thing about the action boundary at that moment; ok would hide it."""
    FakeToolGate.lockdown = True
    assert client.health() == {"status": "degraded", "reason": "lockdown"}


def test_no_key_configured_is_not_configured_not_broken(client):
    assert ToolGateClient(client.base_url, "").health()["status"] == "not_configured"


def test_an_unreachable_toolgate_is_reported_not_guessed():
    client = ToolGateClient("http://127.0.0.1:9", KEY)
    assert client.health()["status"] == "unavailable"
    with pytest.raises(ToolGateUnavailable):
        client.tools()

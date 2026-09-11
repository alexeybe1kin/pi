"""The only way Pi acts on the world.

Pi has no execution path of its own - no shell, no file access, no outbound HTTP
beyond the four services and the model providers. If it touches anything outside
Pi's own store it goes through here, and here goes through ToolGate's keyed HTTP
API with a scoped execution key.

Pi uses the scoped HTTP execution channel directly, without an owner-control or
operator-console credential. See ADR-0005.

Approval is ToolGate's to grant, never Pi's to remember. Pi holds no cache of
past approvals, widens none, and carries none across turns: an approval binds to
one exact action and is consumed once. There is deliberately no "always allow"
anywhere in this file - that semantics cannot be reconciled with a nonce
consumed server-side, and pretending otherwise would show the owner an approval
story that is not true.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class Tool:
    id: str
    name: str
    description: str
    inputs: list[dict]


@dataclass(frozen=True)
class ApprovalRequired:
    """ToolGate wants the owner to confirm this exact action.

    Not an error: the turn parks rather than failing. The owner has not said no,
    they have not been asked yet.
    """
    request_id: str
    expires_at: str | None
    message: str
    tool_id: str
    args: dict


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    result: Any
    tool_id: str


@dataclass(frozen=True)
class ToolPending:
    status: str
    message: str
    action_id: str


class ToolGateUnavailable(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ToolRefused(RuntimeError):
    """ToolGate declined: policy, lockdown, a spent approval, bad arguments.

    A refusal is an answer. It is surfaced to the model and recorded, never
    retried with the guard removed.
    """

    def __init__(self, code: str, message: str, next_action: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.next_action = next_action


class ToolGateClient:
    @staticmethod
    def new_action_id() -> str:
        return "pi_" + uuid.uuid4().hex

    def __init__(self, base_url: str, execution_key: str, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.execution_key = execution_key
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {"X-ToolGate-Execution-Key": self.execution_key}

    @staticmethod
    def _detail(response: httpx.Response) -> dict:
        try:
            body = response.json()
        except Exception:
            return {}
        detail = body.get("detail", body)
        return detail if isinstance(detail, dict) else {"message": str(detail)}

    # --- what Pi may use -------------------------------------------------

    def tools(self) -> list[Tool]:
        """Only what this key is scoped to. ToolGate decides, not Pi."""
        try:
            response = httpx.get(f"{self.base_url}/v2/agent/tools",
                                 headers=self._headers(), timeout=self.timeout)
            response.raise_for_status()
            rows = response.json()
        except Exception as exc:
            raise ToolGateUnavailable(type(exc).__name__) from exc
        return [
            Tool(id=row["id"], name=row.get("name", row["id"]),
                 description=row.get("description", ""), inputs=row.get("inputs", []))
            for row in rows
        ]

    # --- acting ----------------------------------------------------------

    def invoke(self, tool_id: str, args: dict,
               approval_request_id: str | None = None, *, action_id: str,
               job_id: str | None = None) -> ToolResult | ApprovalRequired | ToolPending:
        """Run a tool, or come back asking for the owner.

        Returns ApprovalRequired rather than raising, because being asked to
        confirm is not a failure - the turn parks and the owner decides.
        """
        payload: dict[str, Any] = {"args": args, "action_id": action_id, "job_id": job_id}
        if approval_request_id:
            payload["approval_request_id"] = approval_request_id
        try:
            response = httpx.post(f"{self.base_url}/v2/tools/{tool_id}/invoke",
                                  json=payload, headers=self._headers(), timeout=self.timeout)
        except Exception as exc:
            return ToolPending("outcome_unknown",
                               type(exc).__name__ + ": check action; do not repeat", action_id)

        if 400 <= response.status_code < 500:
            detail = self._detail(response)
            # 409 APPROVAL_INVALID is what a replayed or expired approval looks
            # like. It is a refusal, never a reason to retry without one.
            raise ToolRefused(detail.get("code", f"HTTP_{response.status_code}"),
                              detail.get("message", "tool refused"),
                              detail.get("next_action", ""))

        return self._outcome(response, tool_id, args, action_id)

    def check_action(self, action_id: str, tool_id: str) -> ToolResult | ToolPending:
        try:
            response = httpx.get(f"{self.base_url}/v2/agent/actions/{action_id}",
                                 headers=self._headers(), timeout=self.timeout)
        except httpx.HTTPError:
            return ToolPending("outcome_unknown",
                               "Action status unavailable; do not repeat it", action_id)
        return self._outcome(response, tool_id, {}, action_id)

    @staticmethod
    def _outcome(response, tool_id, args, action_id):
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict) or response.status_code != 200:
            return ToolPending("outcome_unknown", "Action outcome could not be verified", action_id)
        if body.get("action_id", action_id) != action_id:
            return ToolPending("outcome_unknown",
                               "Action receipt identity does not match", action_id)
        if body.get("code") == "CONFIRMATION_REQUIRED" and isinstance(body.get("request_id"), str):
            return ApprovalRequired(
                request_id=body["request_id"],
                expires_at=body.get("expires_at"),
                message=body.get("message", "Owner confirmation is required."),
                tool_id=tool_id,
                args=args,
            )
        if body.get("code") == "IN_PROGRESS":
            return ToolPending("action_in_progress",
                               "Dispatch recorded; outcome pending", action_id)
        if body.get("code") == "OUTCOME_UNKNOWN":
            return ToolPending("outcome_unknown",
                               "Outcome unknown; check the action, never repeat it", action_id)
        result = body.get("result")
        if body.get("status") == "completed" and isinstance(result, dict):
            if body.get("code") == "OK" and result.get("ok") is True:
                return ToolResult(True, result.get("result"), tool_id)
            if body.get("code") == "TOOL_UNAVAILABLE" and result.get("ok") is False:
                return ToolResult(False, result, tool_id)
        # Compatibility with explicit old receipts; absence or truthy strings are not success.
        if body.get("ok") is True or body.get("ok") is False:
            return ToolResult(body["ok"], result, tool_id)
        return ToolPending("outcome_unknown",
                           "No affirmative or negative execution receipt", action_id)

    def health(self) -> dict:
        if not self.execution_key:
            return {"status": "not_configured", "reason": "no execution key"}
        try:
            response = httpx.get(f"{self.base_url}/v2/agent/status",
                                 headers=self._headers(), timeout=5.0)
        except Exception as exc:
            return {"status": "unavailable", "reason": type(exc).__name__}
        if response.status_code == 401:
            return {"status": "unavailable", "reason": "execution key rejected"}
        if response.status_code >= 400:
            return {"status": "unavailable", "reason": f"HTTP {response.status_code}"}
        body = response.json()
        if body.get("lockdown"):
            # Reachable and deliberately refusing everything. Reporting ok would
            # hide the single most important thing about the action boundary
            # right now.
            return {"status": "degraded", "reason": "lockdown"}
        return {"status": "ok"}

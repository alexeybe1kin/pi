"""Same-origin browser authentication and an explicit service-operation allowlist."""

from __future__ import annotations

import hmac
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from pi.browser_contract import runtime_allowed

from .store import AuthError, AuthStore

COOKIE = "__Host-conker"


@dataclass(frozen=True)
class Config:
    origin: str
    database: str
    pi_url: str
    pi_key: str
    toolgate_url: str = "http://toolgate-api:8010"
    owner_key: str = ""

    def validate(self) -> None:
        origin = urlsplit(self.origin)
        if (
            origin.scheme != "https"
            or not origin.hostname
            or origin.username
            or origin.password
            or origin.path
            or origin.query
            or origin.fragment
        ):
            raise ValueError(
                "Set GATEWAY_ORIGIN to the exact HTTPS browser origin, with no trailing slash."
            )
        if len(self.pi_key) < 32 or self.owner_key == self.pi_key:
            raise ValueError(
                "Provision a distinct PI_GATEWAY_KEY of at least 32 characters on the host."
            )
        for value in (self.pi_url, self.toolgate_url):
            url = urlsplit(value)
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or url.username
                or url.password
                or url.path not in {"", "/"}
                or url.query
                or url.fragment
            ):
                raise ValueError(
                    "Set gateway service URLs to fixed origins without credentials or paths."
                )

    @classmethod
    def environment(cls) -> Config:
        return cls(
            os.environ.get("GATEWAY_ORIGIN", ""),
            os.environ.get("GATEWAY_DB_PATH", "/auth/auth.db"),
            os.environ.get("GATEWAY_PI_URL", "http://pi:8050"),
            os.environ.get("PI_GATEWAY_KEY", ""),
            os.environ.get("GATEWAY_TOOLGATE_URL", "http://toolgate-api:8010"),
            os.environ.get("GATEWAY_TOOLGATE_OWNER_KEY", ""),
        )


def create_app(
    config: Config | None = None,
    *,
    store: AuthStore | None = None,
    transport: httpx.BaseTransport | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings = config or Config.environment()
        settings.validate()
        app.state.config = settings
        app.state.auth = store or AuthStore(settings.database)
        with httpx.Client(
            transport=transport, timeout=660, follow_redirects=False, trust_env=False
        ) as client:
            app.state.client = client
            yield

    app = FastAPI(
        title="Conker browser gateway",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )

    @app.exception_handler(AuthError)
    async def auth_error(request: Request, exc: AuthError):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        settings = app.state.config
        if (
            request.url.scheme != "https"
            or request.headers.get("host") != urlsplit(settings.origin).netloc
        ):
            return JSONResponse(
                {"detail": "Use the configured HTTPS address to open Conker."}, status_code=400
            )
        response = await call_next(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "same-origin",
                "Content-Security-Policy": (
                    "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
                ),
            }
        )
        return response

    def session(request: Request, *, authenticated: bool = True) -> dict:
        auth = app.state.auth
        token = request.cookies.get(COOKIE, "")
        value = auth.session(token, authenticated=authenticated)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:  # noqa: SIM102 - group unsafe-method checks
            if request.headers.get("origin") != app.state.config.origin or not hmac.compare_digest(
                request.headers.get("x-csrf-token", "").encode(), value["csrf"].encode()
            ):
                raise AuthError("Request verification failed. Reload Conker and try again.", 403)
        return value

    def cookie(response: JSONResponse, token: str) -> JSONResponse:
        response.set_cookie(COOKIE, token, secure=True, httponly=True, samesite="strict", path="/")
        return response

    @app.get("/health")
    def health():
        checks = {
            "owner_login": {
                "status": "ok" if app.state.auth.configured() else "degraded",
                "reason": "Password configured"
                if app.state.auth.configured()
                else "Run conker auth setup on the host",
            }
        }
        for name, url, header, key in (
            (
                "runtime",
                app.state.config.pi_url.rstrip("/") + "/health",
                "X-Pi-Gateway-Key",
                app.state.config.pi_key,
            ),
            (
                "owner_channel",
                app.state.config.toolgate_url.rstrip("/") + "/v2/owner/requests",
                "X-ToolGate-Owner-Key",
                app.state.config.owner_key,
            ),
        ):
            if not key:
                checks[name] = {
                    "status": "not_configured",
                    "reason": "Provision the owner channel on the host",
                }
                continue
            try:
                result = upstream("GET", url, header, key, None, b"", timeout=3)
                value = result.json()
                status = value.get("status", "unknown") if name == "runtime" else "ok"
                checks[name] = {"status": status if result.status_code == 200 else "unavailable"}
            except (httpx.HTTPError, ValueError, AttributeError):
                checks[name] = {"status": "unavailable"}
        degraded = sorted(
            name
            for name, value in checks.items()
            if value["status"] not in {"ok", "not_configured"}
        )
        return {
            "service": "gateway",
            "version": "0.1.0",
            "status": "degraded" if degraded else "ok",
            "checks": checks,
            "degraded": degraded,
            "checked_at": datetime.now(UTC).isoformat(),
            "age_seconds": 0.0,
        }

    @app.get("/auth/session")
    def auth_session(request: Request):
        try:
            value = session(request, authenticated=False)
        except AuthError:
            value = app.state.auth.anonymous()
        response = JSONResponse(
            {
                "authenticated": bool(value["authenticated"]),
                "csrf_token": value["csrf"],
                "session_id": value["id"],
                "expires_at": value["expires"],
                "setup_required": not app.state.auth.configured(),
            }
        )
        return cookie(response, value["token"]) if "token" in value else response

    @app.post("/auth/login")
    async def login(request: Request):
        session(request, authenticated=False)
        body = await json_body(request)
        if set(body) != {"password"} or not isinstance(body["password"], str):
            raise AuthError("Send a password in a JSON object.", 422)
        # FastAPI runs sync routes in a threadpool; do the expensive check there too.
        from starlette.concurrency import run_in_threadpool

        value = await run_in_threadpool(
            app.state.auth.login,
            request.cookies.get(COOKIE, ""),
            body["password"],
            request.client.host if request.client else "unknown",
        )
        return cookie(
            JSONResponse(
                {
                    "authenticated": True,
                    "csrf_token": value["csrf"],
                    "session_id": value["id"],
                    "expires_at": value["expires"],
                }
            ),
            value["token"],
        )

    @app.post("/auth/logout")
    def logout(request: Request):
        value = session(request)
        app.state.auth.revoke(value["id"])
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="strict")
        return response

    @app.get("/auth/sessions")
    def sessions(request: Request):
        session(request)
        return {"results": app.state.auth.sessions()}

    @app.post("/auth/sessions/{identity}/revoke")
    def revoke(identity: str, request: Request):
        session(request)
        app.state.auth.revoke(identity)
        return {"revoked": True}

    @app.post("/auth/revoke-all")
    def revoke_all(request: Request):
        session(request)
        app.state.auth.revoke()
        return {"revoked": True}

    def upstream(
        method: str,
        url: str,
        key_header: str,
        key: str,
        body: dict | None,
        query: bytes,
        *,
        timeout: float = 660,
    ) -> httpx.Response:
        outgoing = app.state.client.build_request(
            method,
            url,
            headers={key_header: key},
            json=body,
            params=query.decode("ascii") if query else None,
            timeout=timeout,
        )
        # A shared HTTP client must not turn a service's Set-Cookie into later authority.
        outgoing.headers.pop("cookie", None)
        return app.state.client.send(outgoing)

    def forward(method: str, url: str, key_header: str, key: str, body: dict | None, query: bytes):
        if not key:
            raise AuthError(
                "Owner approval channel is not configured. "
                "Provision its scoped credential on the host.",
                503,
            )
        try:
            response = upstream(method, url, key_header, key, body, query)
        except (httpx.HTTPError, UnicodeError):
            raise AuthError(
                "Service unavailable; check conker status. The operation was not retried; "
                "check its state before repeating it.",
                503,
            ) from None
        if 300 <= response.status_code < 400:
            raise AuthError(
                "Service returned an unexpected redirect. Check gateway service URLs on the host.",
                502,
            )
        try:
            data = response.json()
        except ValueError:
            raise AuthError(
                "Service returned an unreadable response. Check its logs before retrying.", 502
            ) from None
        # Upstream headers (especially cookies) never cross the browser boundary.
        return JSONResponse(data, status_code=response.status_code)

    @app.get("/api/pi/{path:path}", operation_id="runtime_read")
    @app.post("/api/pi/{path:path}", operation_id="runtime_write")
    async def runtime(path: str, request: Request):
        session(request)
        target = "/" + path
        if not runtime_allowed(request.method, target):
            raise AuthError("This operation is not available through the browser gateway.", 403)
        body = await json_body(request) if request.method == "POST" else None
        from starlette.concurrency import run_in_threadpool

        return await run_in_threadpool(
            forward,
            request.method,
            app.state.config.pi_url.rstrip("/") + target,
            "X-Pi-Gateway-Key",
            app.state.config.pi_key,
            body,
            request.scope["query_string"],
        )

    @app.get("/api/owner/requests")
    def owner_requests(request: Request):
        session(request)
        return forward(
            "GET",
            app.state.config.toolgate_url.rstrip("/") + "/v2/owner/requests",
            "X-ToolGate-Owner-Key",
            app.state.config.owner_key,
            None,
            b"",
        )

    @app.post("/api/owner/requests/{identity}/decision")
    async def owner_decision(identity: str, request: Request):
        session(request)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", identity):
            raise AuthError("Invalid request identifier. Reload the approval list.", 422)
        body = await json_body(request)
        if (
            set(body) - {"status", "note"}
            or body.get("status") not in {"approved", "rejected", "dismissed"}
            or not isinstance(body.get("note", ""), str)
        ):
            raise AuthError("Send a decision status and optional note.", 422)
        from starlette.concurrency import run_in_threadpool

        return await run_in_threadpool(
            forward,
            "POST",
            app.state.config.toolgate_url.rstrip("/") + f"/v2/owner/requests/{identity}/decision",
            "X-ToolGate-Owner-Key",
            app.state.config.owner_key,
            body,
            b"",
        )

    return app


async def json_body(request: Request) -> dict:
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise AuthError("Send application/json.", 415)
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 65536:
            raise AuthError("Request is too large; keep it below 64 KiB.", 413)
    import json

    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise AuthError("Send a valid JSON object.", 422) from None
    if not isinstance(value, dict):
        raise AuthError("Send a JSON object.", 422)
    return value


app = create_app()

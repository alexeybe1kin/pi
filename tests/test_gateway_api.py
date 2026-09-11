import hashlib

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.api import COOKIE, Config, create_app
from gateway.store import AuthStore

PASSWORD = "four quiet trees beside school"
ORIGIN = "https://localhost:8050"
RUNTIME = "runtime-credential-" + "r" * 32
OWNER = "owner-credential-" + "o" * 32


@pytest.fixture
def gateway(tmp_path):
    auth = AuthStore(tmp_path / "auth.db")
    auth.set_password(PASSWORD)
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(200, json={"memory": {"ingestion": "pending"}},
                              headers={"Set-Cookie": "service_secret=bad", "X-Pi-Key": "bad"})

    config = Config(ORIGIN, str(auth.path), "http://pi:8050", RUNTIME, owner_key=OWNER)
    with TestClient(create_app(config, store=auth, transport=httpx.MockTransport(upstream)), base_url=ORIGIN) as client:
        yield client, auth, seen


def sign_in(client):
    start = client.get("/auth/session")
    before = client.cookies.get(COOKIE)
    result = client.post("/auth/login", json={"password": PASSWORD},
                         headers={"Origin": ORIGIN, "X-CSRF-Token": start.json()["csrf_token"]})
    assert result.status_code == 200, result.text
    assert client.cookies.get(COOKIE) != before
    return {"Origin": ORIGIN, "X-CSRF-Token": result.json()["csrf_token"]}


def test_cookie_is_secure_httponly_strict_and_service_keys_do_not_authenticate(gateway):
    client, auth, seen = gateway
    cookie = client.get("/auth/session").headers["set-cookie"]
    assert COOKIE in cookie and "Secure" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert "Path=/" in cookie and "Domain=" not in cookie
    for header, key in (("X-Pi-Key", RUNTIME), ("X-ToolGate-Execution-Key", OWNER),
                        ("X-ToolGate-Owner-Key", OWNER)):
        assert client.get("/api/owner/requests", headers={header: key}).status_code == 401
    assert not seen


def test_login_requires_csrf_and_exact_origin(gateway):
    client, _, seen = gateway
    csrf = client.get("/auth/session").json()["csrf_token"]
    for headers in ({}, {"Origin": ORIGIN}, {"Origin": "https://evil.invalid", "X-CSRF-Token": csrf},
                    {"Origin": ORIGIN, "X-CSRF-Token": "wrong"}):
        assert client.post("/auth/login", json={"password": PASSWORD}, headers=headers).status_code == 403
    assert not seen


def test_proxy_keeps_credentials_separate_and_preserves_memory_status(gateway):
    client, _, seen = gateway
    headers = sign_in(client)
    result = client.post("/api/pi/sessions", json={"title": "school"},
                         headers={**headers, "X-Pi-Key": "injected", "X-ToolGate-Owner-Key": "injected"})
    assert result.json() == {"memory": {"ingestion": "pending"}}
    assert "set-cookie" not in result.headers and "x-pi-key" not in result.headers
    assert seen[-1].headers["X-Pi-Gateway-Key"] == RUNTIME
    assert "X-ToolGate-Owner-Key" not in seen[-1].headers
    assert "X-Pi-Key" not in seen[-1].headers and "Cookie" not in seen[-1].headers
    result = client.post("/api/owner/requests/req_1/decision", json={"status": "approved"}, headers=headers)
    assert result.status_code == 200
    assert str(seen[-1].url) == "http://toolgate-api:8010/v2/owner/requests/req_1/decision"
    assert seen[-1].headers["X-ToolGate-Owner-Key"] == OWNER
    assert "X-Pi-Gateway-Key" not in seen[-1].headers
    assert OWNER not in result.text and RUNTIME not in result.text


def test_unsafe_operations_require_csrf_and_logout_revokes_the_cookie(gateway):
    client, _, seen = gateway
    headers = sign_in(client)
    for path, body in (("/api/pi/sessions", {}), ("/api/owner/requests/r/decision", {"status": "approved"}),
                       ("/auth/logout", {}), ("/auth/revoke-all", {})):
        assert client.post(path, json=body).status_code == 403
    assert not seen
    old = client.cookies.get(COOKIE)
    assert client.post("/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/pi/sessions", headers={"Cookie": f"{COOKIE}={old}"}).status_code == 401


def test_unknown_routes_and_forged_origins_never_reach_services(gateway):
    client, _, seen = gateway
    headers = sign_in(client)
    for path in ("/api/pi/admin", "/api/pi/v2/requests/r/decision", "/api/pi/sessions/a%2Fb"):
        assert client.post(path, json={}, headers=headers).status_code in {403, 404}
    assert client.get("/api/pi/sessions", headers={"Host": "evil.invalid"}).status_code == 400
    assert client.get("http://localhost:8050/auth/session", headers={"X-Forwarded-Proto": "https"}).status_code == 400
    assert not seen


def test_session_revocation_endpoint_and_expiry_block_proxy(gateway):
    client, auth, seen = gateway
    headers = sign_in(client)
    sessions = client.get("/auth/sessions").json()["results"]
    assert len(sessions) == 1 and "token_hash" not in sessions[0] and "csrf" not in sessions[0]
    assert client.post(f"/auth/sessions/{sessions[0]['id']}/revoke", headers=headers).status_code == 200
    assert client.get("/api/pi/sessions").status_code == 401
    sign_in(client)
    auth.clock = lambda: sessions[0]["expires"] + 1000
    assert client.get("/api/pi/sessions").status_code == 401
    assert not seen


@pytest.mark.parametrize("failure", ["unavailable", "redirect", "malformed", "missing_owner"])
def test_upstream_failure_is_explicit_and_never_retried(tmp_path, failure):
    auth = AuthStore(tmp_path / "auth.db")
    auth.set_password(PASSWORD)
    seen = []

    def upstream(request):
        seen.append(request)
        if failure == "unavailable":
            raise httpx.ConnectError("down")
        if failure == "redirect":
            return httpx.Response(302, headers={"Location": "https://evil.invalid"})
        return httpx.Response(200, text="not json")

    config = Config(ORIGIN, str(auth.path), "http://pi:8050", RUNTIME,
                    owner_key="" if failure == "missing_owner" else OWNER)
    with TestClient(create_app(config, store=auth, transport=httpx.MockTransport(upstream)), base_url=ORIGIN) as client:
        headers = sign_in(client)
        response = client.post("/api/owner/requests/r/decision", json={"status": "approved"}, headers=headers)
        assert response.status_code in {502, 503}
        assert len(seen) == (0 if failure == "missing_owner" else 1)


def test_pi_runtime_key_has_no_owner_authority_or_future_admin_access(monkeypatch, tmp_path):
    from pi import api
    monkeypatch.setenv("PI_ADMIN_KEY", "recovery-only-key-" + "a" * 32)
    monkeypatch.setenv("PI_DB_PATH", str(tmp_path / "pi.db"))
    monkeypatch.setenv("PI_GATEWAY_KEY_SHA256", hashlib.sha256(RUNTIME.encode()).hexdigest())
    with TestClient(api.app) as client:
        assert client.post("/sessions", json={}, headers={"X-Pi-Gateway-Key": RUNTIME}).status_code == 200
        assert client.post("/sessions", json={}, headers={"X-Pi-Key": RUNTIME}).status_code == 401
        assert client.post("/sessions", json={}, headers={"X-Pi-Gateway-Key": OWNER}).status_code == 401
        from starlette.requests import Request
        with pytest.raises(Exception) as error:
            api.require_key(Request({"type": "http", "method": "POST", "path": "/admin/reset", "headers": []}),
                            x_pi_key=None, gateway_key=RUNTIME)
        assert error.value.status_code == 403

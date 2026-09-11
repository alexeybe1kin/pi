"""Real HTTPS, gateway and worker processes, durable conversations and host recovery."""

import hashlib
import os
import socket
import ssl
import subprocess
import sys
import time
from pathlib import Path

import httpx

from gateway.__main__ import create_certificate
from gateway.api import COOKIE
from gateway.store import AuthStore

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_real_https_login_conversation_logout_and_host_recovery(tmp_path, monkeypatch, capsys):
    pi_port, gateway_port = free_port(), free_port()
    origin = f"https://localhost:{gateway_port}"
    password = "four quiet trees beside school"
    key = "separate-runtime-key-" + "x" * 32
    auth = AuthStore(tmp_path / "auth" / "auth.db")
    auth.set_password(password)
    cert, _ = create_certificate(auth.path.parent, "localhost")
    worker_env = {
        **os.environ,
        "PI_ADMIN_KEY": "host-recovery-key-" + "a" * 32,
        "PI_GATEWAY_KEY_SHA256": hashlib.sha256(key.encode()).hexdigest(),
        "PI_DB_PATH": str(tmp_path / "pi.db"),
        "PI_TOOLGATE_KEY": "",
        "PI_MEMORYGATE_URL": "",
        "PI_MEMORYGATE_INGEST_KEY": "",
        "PI_MEMORYGATE_READ_KEY": "",
        "PI_OPENROUTER_KEY": "",
    }
    gateway_env = {
        **os.environ,
        "GATEWAY_ORIGIN": origin,
        "GATEWAY_DB_PATH": str(auth.path),
        "GATEWAY_PI_URL": f"http://127.0.0.1:{pi_port}",
        "PI_GATEWAY_KEY": key,
        "GATEWAY_TOOLGATE_OWNER_KEY": "",
    }
    processes = []
    with (tmp_path / "process.log").open("w") as log:
        try:
            for args, env in (
                (
                    ["uvicorn", "pi.api:app", "--host", "127.0.0.1", "--port", str(pi_port)],
                    worker_env,
                ),
                (["gateway", "serve", "--port", str(gateway_port)], gateway_env),
            ):
                processes.append(
                    subprocess.Popen(
                        [sys.executable, "-m", *args],
                        cwd=ROOT,
                        env=env,
                        stdout=log,
                        stderr=log,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    )
                )
            with httpx.Client(
                base_url=origin,
                verify=ssl.create_default_context(cafile=str(cert)),
                trust_env=False,
            ) as client:
                deadline = time.monotonic() + 25
                while True:
                    try:
                        start = client.get("/auth/session")
                        with httpx.Client(trust_env=False) as probe:
                            probe.get(f"http://127.0.0.1:{pi_port}/sessions")
                        break
                    except httpx.TransportError:
                        assert time.monotonic() < deadline, (
                            "Gateway or Pi failed to start; inspect process.log"
                        )
                        time.sleep(0.1)
                assert "Secure" in start.headers["set-cookie"]
                headers = {"Origin": origin, "X-CSRF-Token": start.json()["csrf_token"]}
                result = client.post("/auth/login", json={"password": password}, headers=headers)
                assert result.status_code == 200
                headers["X-CSRF-Token"] = result.json()["csrf_token"]
                saved = client.post(
                    "/api/pi/sessions", json={"title": "Before school / До школы"}, headers=headers
                )
                assert saved.status_code == 200, saved.text
                assert (
                    client.get("/api/pi/sessions").json()["results"][0]["id"]
                    == saved.json()["session_id"]
                )
                old_cookie = client.cookies.get(COOKIE)
                assert client.post("/auth/logout", headers=headers).status_code == 200
                assert (
                    client.get(
                        "/api/pi/sessions", headers={"Cookie": f"{COOKIE}={old_cookie}"}
                    ).status_code
                    == 401
                )
                from gateway import __main__ as cli

                # Supply terminal input without opening an interactive Windows console.
                monkeypatch.setattr(
                    cli.getpass, "getpass", lambda prompt: "new passphrase after recovery"
                )
                monkeypatch.setattr(
                    sys, "argv", ["gateway", "reset-password", "--db", str(auth.path)]
                )
                cli.main()
                output = capsys.readouterr()
                assert "Password saved" in output.out
                assert "new passphrase after recovery" not in output.out + output.err
                csrf = client.get("/auth/session").json()["csrf_token"]
                result = client.post(
                    "/auth/login",
                    json={"password": "new passphrase after recovery"},
                    headers={"Origin": origin, "X-CSRF-Token": csrf},
                )
                assert result.status_code == 200
                assert (
                    client.get("/api/pi/sessions").json()["results"][0]["title"]
                    == "Before school / До школы"
                )
        finally:
            for process in processes:
                process.terminate()
            for process in processes:
                process.wait(timeout=10)

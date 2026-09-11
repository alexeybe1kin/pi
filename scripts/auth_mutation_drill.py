"""Mutate isolated copies, requiring assertion failures rather than collection errors."""

import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = "tests/test_gateway_api.py::"
STORE = "tests/test_gateway_store.py::"
CASES = [
    (
        "tls-identity-changes-on-restart",
        "gateway/__main__.py",
        "        return certificate, key_path\n    key = rsa.generate_private_key",
        ("        certificate.unlink()\n        key_path.unlink()\n"
         "    key = rsa.generate_private_key"),
        (
            "tests/test_gateway_tls.py::"
            "test_tls_identity_survives_restart_and_only_explicit_renewal_replaces_it"
        ),
    ),
    (
        "insecure-cookie",
        "gateway/api.py",
        "token, secure=True, httponly=True",
        "token, secure=False, httponly=True",
        API + "test_cookie_is_secure_httponly_strict_and_service_keys_do_not_authenticate",
    ),
    (
        "script-readable-cookie",
        "gateway/api.py",
        "token, secure=True, httponly=True",
        "token, secure=True, httponly=False",
        API + "test_cookie_is_secure_httponly_strict_and_service_keys_do_not_authenticate",
    ),
    (
        "csrf-disabled",
        "gateway/api.py",
        'if request.method not in {"GET", "HEAD", "OPTIONS"}:',
        "if False:",
        API + "test_unsafe_operations_require_csrf_and_logout_revokes_the_cookie",
    ),
    (
        "login-origin-unchecked",
        "gateway/api.py",
        'request.headers.get("origin") != app.state.config.origin',
        "False",
        API + "test_login_requires_csrf_and_exact_origin",
    ),
    (
        "anonymous-approval",
        "gateway/store.py",
        'if authenticated and not row["authenticated"]:',
        "if False:",
        API + "test_cookie_is_secure_httponly_strict_and_service_keys_do_not_authenticate",
    ),
    (
        "expired-session-survives",
        "gateway/store.py",
        'if not row or row["expires"] <= now or row["touched"] <= now - self.idle:',
        "if not row:",
        STORE + "test_idle_and_absolute_expiry",
    ),
    (
        "revocation-is-cosmetic",
        "gateway/store.py",
        'db.execute("DELETE FROM sessions WHERE id=?", (session_id,))',
        'db.execute("SELECT 1")',
        STORE + "test_login_rotates_and_logout_survives_restart",
    ),
    (
        "raw-session-token-at-rest",
        "gateway/store.py",
        "return hashlib.sha256(value.encode()).hexdigest()",
        "return value",
        STORE + "test_password_and_session_secrets_are_not_stored_verbatim",
    ),
    (
        "late-login-undoes-reset",
        "gateway/store.py",
        (
            "if (\n                not current\n"
            '                or current[0] != owner["generation"]\n'
            '                or not row\n                or row["expires"] <= self.clock()\n'
            '                or row["touched"] <= self.clock() - self.idle\n            ):'
        ),
        "if False:",
        STORE + "test_password_reset_during_login_cannot_issue_a_stale_session",
    ),
    (
        "unlimited-password-attempts",
        "gateway/store.py",
        "if attempts >= 30 or local >= 5:",
        "if False:",
        STORE + "test_failed_logins_are_rate_limited_across_restarts",
    ),
    (
        "owner-key-reaches-worker",
        "gateway/api.py",
        '"X-Pi-Gateway-Key",\n            app.state.config.pi_key,\n            body',
        '"X-Pi-Gateway-Key",\n            app.state.config.owner_key,\n            body',
        API + "test_proxy_keeps_credentials_separate_and_preserves_memory_status",
    ),
    (
        "approval-uses-agent-channel",
        "gateway/api.py",
        '"X-ToolGate-Owner-Key",\n            app.state.config.owner_key,\n            body',
        '"X-ToolGate-Execution-Key",\n            app.state.config.owner_key,\n            body',
        API + "test_proxy_keeps_credentials_separate_and_preserves_memory_status",
    ),
    (
        "gateway-forwards-any-route",
        "gateway/api.py",
        "if not runtime_allowed(request.method, target):",
        "if False:",
        API + "test_unknown_routes_and_forged_origins_never_reach_services",
    ),
    (
        "worker-runtime-key-becomes-admin",
        "pi/api.py",
        "if not runtime_allowed(request.method, request.url.path):",
        "if False:",
        API + "test_pi_runtime_key_has_no_owner_authority_or_future_admin_access",
    ),
    (
        "upstream-cookie-replayed",
        "gateway/api.py",
        'outgoing.headers.pop("cookie", None)',
        "pass",
        API + "test_proxy_keeps_credentials_separate_and_preserves_memory_status",
    ),
    (
        "upstream-redirect-accepted",
        "gateway/api.py",
        "if 300 <= response.status_code < 400:",
        "if False:",
        API + "test_upstream_failure_is_explicit_and_never_retried[redirect]",
    ),
]


def main() -> int:
    scratch = ROOT / ".test-tmp"
    scratch.mkdir(exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="auth-mutations-", dir=scratch))
    baseline = ("baseline", None, None, None, None)
    for name, filename, old, new, selected in [baseline, *CASES]:
        target = run / name
        for directory in ("pi", "gateway"):
            shutil.copytree(
                ROOT / directory, target / directory, ignore=shutil.ignore_patterns("__pycache__")
            )
        (target / "tests").mkdir()
        for file in ("test_gateway_api.py", "test_gateway_store.py", "test_gateway_tls.py"):
            shutil.copyfile(ROOT / "tests" / file, target / "tests" / file)
        if filename:
            file = target / filename
            source = file.read_text(encoding="utf-8")
            if source.count(old) != 1:
                raise RuntimeError(f"{name}: expected exactly one mutation site; update the drill")
            file.write_text(source.replace(old, new), encoding="utf-8")
        report = target / "results.xml"
        tests = (
            [selected] if selected else ["tests/test_gateway_api.py", "tests/test_gateway_store.py"]
        )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                *tests,
                "-q",
                "-p",
                "no:cacheprovider",
                "--basetemp",
                str(target / "temp"),
                "--junitxml",
                str(report),
            ],
            cwd=target,
            env={**os.environ, "PYTHONPATH": str(target)},
            capture_output=True,
            text=True,
            timeout=120,
        )
        (target / "output.txt").write_text(result.stdout + result.stderr, encoding="utf-8")
        suites = list(ET.parse(report).getroot().iter("testsuite")) if report.exists() else []
        failures = sum(int(s.get("failures", 0)) for s in suites)
        errors = sum(int(s.get("errors", 0)) for s in suites)
        skipped = sum(int(s.get("skipped", 0)) for s in suites)
        if (
            not suites
            or errors
            or skipped
            or (
                result.returncode != 0
                if name == "baseline"
                else result.returncode != 1 or failures < 1
            )
        ):
            print(f"FAILED {name}; inspect {target / 'output.txt'}")
            return 1
        print(f"{name}: {'passed' if name == 'baseline' else 'caught'}", flush=True)
    print(f"All {len(CASES)} mutants caught. Evidence: {run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

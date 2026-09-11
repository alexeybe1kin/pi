import sqlite3
import threading

import pytest

from gateway import store as module
from gateway.store import AuthError, AuthStore

PASSWORD = "four quiet trees beside school"


@pytest.fixture
def auth(tmp_path):
    store = AuthStore(tmp_path / "auth.db")
    store.set_password(PASSWORD, initial=True)
    return store


def login(auth):
    anonymous = auth.anonymous()
    return auth.login(anonymous["token"], PASSWORD, "local")


def test_password_and_session_secrets_are_not_stored_verbatim(auth):
    session = login(auth)
    with sqlite3.connect(auth.path) as db:
        dump = "\n".join(db.iterdump())
    assert PASSWORD not in dump
    assert session["token"] not in dump
    assert auth.session(session["token"])["authenticated"] == 1


def test_login_rotates_and_logout_survives_restart(auth):
    old = auth.anonymous()
    session = auth.login(old["token"], PASSWORD, "local")
    with pytest.raises(AuthError):
        auth.session(old["token"], authenticated=False)
    AuthStore(auth.path).revoke(session["id"])
    with pytest.raises(AuthError):
        AuthStore(auth.path).session(session["token"])


def test_idle_and_absolute_expiry(auth):
    now = [1000.0]
    auth.clock = lambda: now[0]
    session = login(auth)
    now[0] += auth.idle
    with pytest.raises(AuthError):
        auth.session(session["token"])
    session = login(auth)
    for _ in range(100):
        now[0] += auth.idle - 1
        if now[0] >= session["expires"]:
            break
        auth.session(session["token"])
    with pytest.raises(AuthError):
        auth.session(session["token"])


def test_host_reset_revokes_all_sessions_and_old_password(auth):
    session = login(auth)
    auth.set_password("a replacement passphrase for recovery")
    with pytest.raises(AuthError):
        auth.session(session["token"])
    with pytest.raises(AuthError):
        login(auth)
    new = auth.anonymous()
    assert auth.login(new["token"], "a replacement passphrase for recovery", "local")["authenticated"]
    with pytest.raises(AuthError, match="already exists"):
        auth.set_password(PASSWORD, initial=True)


def test_password_reset_during_login_cannot_issue_a_stale_session(auth, monkeypatch):
    anonymous = auth.anonymous()
    entered, release = threading.Event(), threading.Event()
    real = module.password_hash
    outcomes = []

    def delayed(password, salt):
        value = real(password, salt)
        if threading.current_thread().name == "late-login":
            entered.set()
            assert release.wait(10)
        return value

    monkeypatch.setattr(module, "password_hash", delayed)

    def attempt():
        try:
            outcomes.append(auth.login(anonymous["token"], PASSWORD, "local"))
        except AuthError:
            outcomes.append("rejected")

    thread = threading.Thread(target=attempt, name="late-login")
    thread.start()
    try:
        assert entered.wait(10)
        auth.set_password("recovery replaces the old password")
    finally:
        release.set()
        thread.join(10)
    assert outcomes == ["rejected"]
    assert auth.sessions() == []


def test_failed_logins_are_rate_limited_across_restarts(auth):
    anonymous = auth.anonymous()
    for _ in range(5):
        with pytest.raises(AuthError) as error:
            auth.login(anonymous["token"], "wrong", "local")
        assert error.value.status == 401
    with pytest.raises(AuthError) as error:
        AuthStore(auth.path).login(anonymous["token"], PASSWORD, "local")
    assert error.value.status == 429


def test_http_cannot_claim_unconfigured_installation(tmp_path):
    auth = AuthStore(tmp_path / "auth.db")
    with pytest.raises(AuthError, match="conker auth setup"):
        login(auth)

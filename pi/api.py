"""Pi's HTTP surface.

The only service the browser talks to. Every route here is Pi's own state -
sessions, messages, turns - because Pi coordinates and never owns anything else:
memory belongs to MemoryGate, actions to ToolGate, machine truth to SystemGate.
Tool calls arrive in #29 and go out through ToolGate, never from here.
"""
from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .loop import Loop, TurnFailed
from .providers import OllamaProvider
from .store import Store

SERVICE_VERSION = "0.1.0"
HEALTHY = {"ok", "not_configured"}
HEALTH_CACHE_SECONDS = 5.0

_health_cache: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    admin_key = os.environ.get("PI_ADMIN_KEY", "").strip()
    # Secure by default, or refuse to start. Never fall back to open - the rule
    # has a scar behind it, see ADR-0005.
    if len(admin_key) < 16:
        raise RuntimeError(
            "PI_ADMIN_KEY is required and must be at least 16 characters.\n\n"
            "Fix, in the directory holding docker-compose.yml:\n\n"
            '    echo "PI_ADMIN_KEY=$(openssl rand -base64 24)" >> .env\n'
            "    docker compose up -d pi\n"
        )
    store = Store(os.environ.get("PI_DB_PATH", "/data/pi.db"))
    # A turn that was running when the process died did not finish. Saying
    # nothing would leave the owner looking at a request that vanished.
    interrupted = store.mark_interrupted_turns()

    provider = OllamaProvider(os.environ.get("PI_OLLAMA_URL", "http://ollama:11434"))
    app.state.admin_key = admin_key
    app.state.store = store
    app.state.provider = provider
    app.state.interrupted_at_startup = interrupted
    app.state.loop = Loop(
        store, provider,
        model=os.environ.get("PI_MODEL", "qwen3:4b"),
        system_prompt=os.environ.get("PI_SYSTEM_PROMPT", ""),
    )
    yield


app = FastAPI(title="Pi", version=SERVICE_VERSION, lifespan=lifespan)


def require_key(x_pi_key: str | None = Header(None, alias="X-Pi-Key")) -> str:
    if x_pi_key != app.state.admin_key:
        raise HTTPException(401, "missing or invalid X-Pi-Key")
    return "admin"


class NewSession(BaseModel):
    title: str = ""


class TurnRequest(BaseModel):
    text: str = Field(min_length=1)


@app.get("/health")
def health():
    """Shape is fixed by the Conker module contract - see docs/module-contract.md."""
    now = time.monotonic()
    cached = _health_cache.get("result")
    if cached and now - _health_cache["at"] < HEALTH_CACHE_SECONDS:
        return {**cached, "age_seconds": round(now - _health_cache["at"], 1)}

    checks = {"store": app.state.store.health(), "provider": app.state.provider.health()}
    degraded = sorted(name for name, c in checks.items() if c["status"] not in HEALTHY)
    result = {
        "service": "pi",
        "version": SERVICE_VERSION,
        "status": "degraded" if degraded else "ok",
        "degraded": degraded,
        "checks": checks,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    _health_cache["result"] = result
    _health_cache["at"] = now
    return {**result, "age_seconds": 0.0}


@app.post("/sessions", dependencies=[Depends(require_key)])
def create_session(body: NewSession):
    return {"session_id": app.state.store.create_session(title=body.title)}


@app.get("/sessions", dependencies=[Depends(require_key)])
def list_sessions(limit: int = 50):
    return {"results": app.state.store.list_sessions(limit=min(max(limit, 1), 200))}


@app.get("/sessions/{session_id}", dependencies=[Depends(require_key)])
def get_session(session_id: str):
    session = app.state.store.get_session(session_id)
    if session is None:
        raise HTTPException(404, "no such session")
    return {**session,
            "messages": app.state.store.messages(session_id),
            "turns": app.state.store.turns(session_id)}


@app.post("/sessions/{session_id}/turns", dependencies=[Depends(require_key)])
def run_turn(session_id: str, body: TurnRequest):
    try:
        return app.state.loop.run_turn(session_id, body.text)
    except TurnFailed as exc:
        # 503, not 500: the provider did not answer, which is a state the caller
        # can act on. The user's message is already stored either way.
        raise HTTPException(503, f"turn failed: {exc.reason}") from exc


@app.post("/sessions/{session_id}/fork", dependencies=[Depends(require_key)])
def fork_session(session_id: str):
    if app.state.store.get_session(session_id) is None:
        raise HTTPException(404, "no such session")
    return {"session_id": app.state.loop.fork(session_id), "parent_id": session_id}

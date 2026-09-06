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
from .openrouter import OpenRouterProvider
from .providers import OllamaProvider
from .routing import Router
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

    local = OllamaProvider(os.environ.get("PI_OLLAMA_URL", "http://ollama:11434"))

    # Free by default: a fresh install works with no payment and no key. The
    # hosted provider only exists if one was supplied, and even then it refuses
    # paid models unless PI_ALLOW_PAID_MODELS says otherwise - spending is a
    # deliberate act, never a default or a typo.
    openrouter_key = os.environ.get("PI_OPENROUTER_KEY", "").strip()
    hosted = None
    if openrouter_key:
        hosted = OpenRouterProvider(
            openrouter_key,
            allow_paid=os.environ.get("PI_ALLOW_PAID_MODELS", "").strip() in {"1", "true", "yes"},
        )

    app.state.admin_key = admin_key
    app.state.store = store
    app.state.local = local
    app.state.hosted = hosted
    app.state.interrupted_at_startup = interrupted
    app.state.router = Router(
        local_provider=local, hosted_provider=hosted,
        local_model=os.environ.get("PI_MODEL", "qwen3:4b"),
    )
    app.state.loop = Loop(
        store, app.state.router,
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
    # Routing hints the caller genuinely knows. The router is deliberately not
    # a classifier that reads the message - that would be a model nobody
    # evaluates deciding how much every turn costs.
    needs_tools: bool = False
    is_analysis: bool = False
    owner_requested_strong: bool = False


@app.get("/health")
def health():
    """Shape is fixed by the Conker module contract - see docs/module-contract.md."""
    now = time.monotonic()
    cached = _health_cache.get("result")
    if cached and now - _health_cache["at"] < HEALTH_CACHE_SECONDS:
        return {**cached, "age_seconds": round(now - _health_cache["at"], 1)}

    checks = {
        "store": app.state.store.health(),
        "local_provider": app.state.local.health(),
        # not_configured, not unavailable: no hosted provider is a valid install,
        # not a broken one, and collapsing the two is how a dashboard starts
        # lying about what is wrong.
        "hosted_provider": (app.state.hosted.health() if app.state.hosted
                            else {"status": "not_configured", "reason": "no API key"}),
    }
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
        return app.state.loop.run_turn(session_id, body.text, context={
            "needs_tools": body.needs_tools,
            "is_analysis": body.is_analysis,
            "owner_requested_strong": body.owner_requested_strong,
        })
    except TurnFailed as exc:
        # 503, not 500: the provider did not answer, which is a state the caller
        # can act on. The user's message is already stored either way.
        raise HTTPException(503, f"turn failed: {exc.reason}") from exc


@app.get("/models", dependencies=[Depends(require_key)])
def models():
    """What Pi can route to right now, and what each would cost.

    Discovered, never hardcoded: a static list is stale within weeks. Paid
    models are listed so the owner can see what opting in would buy, and marked
    so nothing is called by accident.
    """
    local = {"provider": app.state.local.name, "model": app.state.router.local_model,
             "free": True, "health": app.state.local.health()}
    if app.state.hosted is None:
        return {"local": local, "hosted": {"status": "not_configured"}}
    try:
        catalogue = app.state.hosted.catalogue()
    except Exception as exc:
        return {"local": local,
                "hosted": {"status": "unavailable", "reason": type(exc).__name__}}
    free = [m.id for m in app.state.hosted.free_models()]
    return {"local": local, "hosted": {
        "status": "ok", "provider": app.state.hosted.name,
        "allow_paid": app.state.hosted.allow_paid,
        "free_models": free, "free_count": len(free), "total_text_models": len(catalogue),
    }}


@app.post("/sessions/{session_id}/fork", dependencies=[Depends(require_key)])
def fork_session(session_id: str):
    if app.state.store.get_session(session_id) is None:
        raise HTTPException(404, "no such session")
    return {"session_id": app.state.loop.fork(session_id), "parent_id": session_id}

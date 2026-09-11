"""The explicit conversation capabilities granted to the browser gateway."""

import re

RUNTIME_ROUTES = {
    "GET": (
        r"/health",
        r"/sessions",
        r"/sessions/[A-Za-z0-9_-]+",
        r"/turns/unreplied",
        r"/approvals",
        r"/tools",
        r"/models",
        r"/messages/[A-Za-z0-9_-]+",
        r"/memory",
    ),
    "POST": (
        r"/sessions",
        r"/sessions/[A-Za-z0-9_-]+/turns",
        r"/sessions/[A-Za-z0-9_-]+/fork",
        r"/turns/[A-Za-z0-9_-]+/resume",
    ),
}


def runtime_allowed(method: str, path: str) -> bool:
    return any(re.fullmatch(pattern, path) for pattern in RUNTIME_ROUTES.get(method, ()))

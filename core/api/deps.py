"""FastAPI dependencies: resolve the caller via the Identity port, then enrich from the store."""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from core.domain.auth import load_user


def container(request: Request):
    return request.app.state.c


def current_user(request: Request) -> dict:
    c = request.app.state.c
    headers = {k.lower(): v for k, v in request.headers.items()}
    principal = c.identity().principal_from_request(headers)
    if not principal:
        raise HTTPException(401, "authentication required")
    u = load_user(c.store(), principal.id)
    if not u:
        raise HTTPException(401, "unknown or inactive user")
    return u


def require_admin(u: dict = Depends(current_user)) -> dict:
    if not u.get("is_admin"):
        raise HTTPException(403, "admin only")
    return u

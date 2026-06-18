"""Auth + vocabulary routes."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request

from core.api.deps import current_user
from core.domain.abac import CLEARANCE, USER_TYPES, clr_label
from core.domain.audit import audit
from core.domain.auth import (clear_fail, jwt_encode, load_user, login_check, me_dict,
                              record_fail, verify_password)

router = APIRouter()


@router.post("/api/auth/login")
def login(body: dict, request: Request):
    c = request.app.state.c
    store = c.store()
    username = (body.get("username") or "").strip().lower()
    password = body.get("password") or ""
    if not login_check(username):
        raise HTTPException(429, "Too many login attempts. Please wait a few minutes and try again.")
    rows = store.execute("SELECT * FROM users WHERE (username=? OR email=?) AND is_active=1", (username, username))
    u = rows[0] if rows else None
    if not u or not verify_password(password, u.get("password_hash")):
        record_fail(username)
        raise HTTPException(401, "Invalid username or password")
    clear_fail(username)
    token = jwt_encode({"sub": u["id"], "iat": int(time.time()), "exp": int(time.time()) + 12 * 3600})
    audit(store, u["id"], "auth.login", {"username": u["username"]}, c.telemetry())
    return {"token": token, "user": me_dict(store, load_user(store, u["id"]))}


@router.get("/api/me")
def me(request: Request, u: dict = Depends(current_user)):
    return me_dict(request.app.state.c.store(), u)


@router.get("/api/personas")
def personas(request: Request):
    """Public quick-login list for the demo (no secrets)."""
    store = request.app.state.c.store()
    rows = store.execute("SELECT id,name,username,role,user_type,clearance,is_admin FROM users "
                         "WHERE password_hash IS NOT NULL AND is_active=1 ORDER BY is_admin DESC, name")
    return [{**r, "clearance": clr_label(r["clearance"])} for r in rows]


@router.get("/api/groups")
def groups(request: Request, u: dict = Depends(current_user)):
    return request.app.state.c.store().execute("SELECT * FROM groups")


@router.get("/api/vocab")
def vocab(request: Request, u: dict = Depends(current_user)):
    store = request.app.state.c.store()
    deps = store.execute("SELECT * FROM groups")
    staff = u["user_type"] in ("employee", "admin")
    users = [{"id": r["id"], "name": r["name"]} for r in store.execute(
        "SELECT id,name FROM users WHERE is_active=1 AND user_type IN ('employee','admin') ORDER BY name")] if staff else []
    projects = sorted({r["project"] for r in store.execute(
        "SELECT project FROM doc_projects UNION SELECT project FROM user_projects")}) if staff else []
    return {"departments": deps, "clearances": list(CLEARANCE.keys()), "user_types": USER_TYPES,
            "approvers": users, "projects": projects}

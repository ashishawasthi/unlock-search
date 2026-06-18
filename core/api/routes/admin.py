"""Admin routes: user list + ABAC attribute assignment, audit log + CSV export."""
from __future__ import annotations

import csv
import io
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from core.api.deps import require_admin
from core.domain.abac import USER_TYPES, clr_label, clr_level
from core.domain.audit import audit

router = APIRouter()


@router.get("/api/admin/users")
def admin_users(request: Request, u: dict = Depends(require_admin)):
    store = request.app.state.c.store()
    out = []
    for r in store.execute("SELECT * FROM users ORDER BY is_admin DESC, name"):
        depts = [g["name"] for g in store.execute(
            "SELECT g.name FROM groups g JOIN user_groups ug ON ug.group_id=g.id WHERE ug.user_id=?", (r["id"],))]
        projs = [p["project"] for p in store.execute("SELECT project FROM user_projects WHERE user_id=?", (r["id"],))]
        out.append({"id": r["id"], "name": r["name"], "username": r["username"], "email": r["email"],
                    "role": r["role"], "user_type": r["user_type"], "is_admin": r["is_admin"],
                    "is_active": r["is_active"], "clearance": clr_label(r["clearance"]),
                    "departments": depts, "projects": projs})
    return out


@router.patch("/api/admin/users/{uid}")
def admin_update_user(uid: str, body: dict, request: Request, u: dict = Depends(require_admin)):
    c = request.app.state.c
    store = c.store()
    if not store.execute("SELECT 1 FROM users WHERE id=?", (uid,)):
        raise HTTPException(404)
    if body.get("user_type") in USER_TYPES:
        store.execute("UPDATE users SET user_type=? WHERE id=?", (body["user_type"], uid))
    if "clearance" in body:
        store.execute("UPDATE users SET clearance=? WHERE id=?", (clr_level(body["clearance"]), uid))
    if "is_active" in body:
        store.execute("UPDATE users SET is_active=? WHERE id=?", (1 if body["is_active"] else 0, uid))
    if "departments" in body:
        store.execute("DELETE FROM user_groups WHERE user_id=?", (uid,))
        for gid in body["departments"]:
            store.execute("INSERT OR IGNORE INTO user_groups(user_id,group_id) VALUES(?,?)", (uid, gid))
    if "projects" in body:
        store.execute("DELETE FROM user_projects WHERE user_id=?", (uid,))
        for p in body["projects"]:
            store.execute("INSERT OR IGNORE INTO user_projects(user_id,project) VALUES(?,?)", (uid, p))
    audit(store, u["id"], "admin.user_update", {"target": uid, "fields": list(body.keys())}, c.telemetry())
    return {"ok": True}


@router.get("/api/audit")
def get_audit(request: Request, u: dict = Depends(require_admin)):
    return request.app.state.c.store().execute("SELECT * FROM audit ORDER BY id DESC LIMIT 200")


@router.get("/api/audit.csv")
def get_audit_csv(request: Request, u: dict = Depends(require_admin)):
    store = request.app.state.c.store()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ts", "iso_time", "user_id", "event", "detail"])
    for r in store.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 5000"):
        w.writerow([r["ts"], time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"])),
                    r["user_id"], r["event"], r["detail"]])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="aibox_audit.csv"'})

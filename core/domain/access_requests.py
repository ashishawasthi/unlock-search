"""
Access-request + approval workflow. Domain logic; the only provider edge is the
Notifier port (email send). HMAC-signed, time-limited, single-use approve/deny links;
a confirmation interstitial defeats email link-prefetch auto-deciding.
"""
from __future__ import annotations

import html
import os
import time
import uuid
from urllib.parse import quote

from core.domain.abac import build_predicate, can_access
from core.domain.audit import audit
from core.domain.auth import request_token

BASE_URL = os.environ.get("UNLOCK_BASE_URL", "http://127.0.0.1:8000")
TOKEN_TTL = int(os.environ.get("UNLOCK_TOKEN_TTL_HOURS", "48")) * 3600
GRANT_DAYS = int(os.environ.get("UNLOCK_GRANT_DAYS", "30"))


def create_request(c, principal, doc_id: str, justification: str) -> dict:
    store = c.store()
    rows = store.execute("SELECT id,title,owner_id,approver_id FROM documents WHERE id=?", (doc_id,))
    if not rows:
        raise ValueError("not-found")
    d = rows[0]
    if can_access(store, build_predicate(principal), doc_id):
        raise ValueError("already-access")
    if store.execute("SELECT id FROM access_requests WHERE requester_id=? AND doc_id=? AND status='pending'",
                     (principal.id, doc_id)):
        raise ValueError("already-pending")
    approver_id = d["approver_id"] or d["owner_id"]
    rid = uuid.uuid4().hex[:10]
    token = request_token(rid)
    store.execute(
        "INSERT INTO access_requests(id,requester_id,doc_id,approver_id,status,justification,token,"
        "token_expires_at,created_at) VALUES(?,?,?,?,'pending',?,?,?,?)",
        (rid, principal.id, doc_id, approver_id, justification or "", token, time.time() + TOKEN_TTL, time.time()))
    ap = store.execute("SELECT name,email FROM users WHERE id=?", (approver_id,))
    me = store.execute("SELECT name,email FROM users WHERE id=?", (principal.id,))[0]
    review = f"{BASE_URL}/api/access-requests/{rid}/action?token={token}"
    c.notifier().notify(
        ap[0]["email"] if ap else "approver@localhost", f"Access request: {d['title']}",
        f"{me['name']} ({me['email']}) requests access to \"{d['title']}\".\n\n"
        f"Justification: {justification or '(none)'}\n\nReview and decide: {review}\n\n"
        f"(Link is single-use and expires in {TOKEN_TTL // 3600}h.)")
    audit(store, principal.id, "access.request", {"request_id": rid, "doc_id": doc_id, "approver": approver_id},
          c.telemetry())
    return {"ok": True, "request_id": rid}


def _view(store, r: dict) -> dict:
    d = store.execute("SELECT title FROM documents WHERE id=?", (r["doc_id"],))
    req = store.execute("SELECT name,role FROM users WHERE id=?", (r["requester_id"],))
    ap = store.execute("SELECT name FROM users WHERE id=?", (r["approver_id"],))
    return {**r, "doc_title": d[0]["title"] if d else "(removed)",
            "requester_name": req[0]["name"] if req else r["requester_id"],
            "requester_role": req[0]["role"] if req else "",
            "approver_name": ap[0]["name"] if ap else r["approver_id"]}


def list_requests(c, principal) -> dict:
    store = c.store()
    if principal.is_admin:
        inc = store.execute("SELECT * FROM access_requests ORDER BY created_at DESC")
    else:
        inc = store.execute("SELECT * FROM access_requests WHERE approver_id=? ORDER BY created_at DESC", (principal.id,))
    mine = store.execute("SELECT * FROM access_requests WHERE requester_id=? ORDER BY created_at DESC", (principal.id,))
    return {"incoming": [_view(store, r) for r in inc], "mine": [_view(store, r) for r in mine]}


def _decide(c, r: dict, decision: str, note: str, actor_id: str) -> None:
    store = c.store()
    status = "approved" if decision == "approve" else "denied"
    store.execute("UPDATE access_requests SET status=?, approver_note=?, decided_at=?, used=1 WHERE id=?",
                  (status, note or "", time.time(), r["id"]))
    if status == "approved":
        store.execute("DELETE FROM doc_user_grants WHERE doc_id=? AND user_id=?",
                      (r["doc_id"], r["requester_id"]))
        store.execute("INSERT INTO doc_user_grants(doc_id,user_id,expires_at) VALUES(?,?,?)",
                      (r["doc_id"], r["requester_id"], time.time() + GRANT_DAYS * 86400))
        # Refresh the pushdown index so the new grant is enforced there too (no-op-cheap for
        # SQL/FTS backends which read grants live; required for OpenSearch/Gemini Enterprise Agent Platform).
        from core.domain.ingest import reindex_acl
        try:
            reindex_acl(c, r["doc_id"])
        except Exception:
            pass
    d = store.execute("SELECT title FROM documents WHERE id=?", (r["doc_id"],))
    req = store.execute("SELECT name,email FROM users WHERE id=?", (r["requester_id"],))
    if req:
        title = d[0]["title"] if d else r["doc_id"]
        c.notifier().notify(req[0]["email"], f"Access request {status}: {title}",
                            f"Your request for \"{title}\" was {status}."
                            + (f"\nReason: {note}" if note else "")
                            + (f"\nA {GRANT_DAYS}-day grant has been applied." if status == "approved" else ""))
    audit(store, actor_id, "access.approve" if status == "approved" else "access.deny",
          {"request_id": r["id"], "doc_id": r["doc_id"], "requester": r["requester_id"]}, c.telemetry())


def decide_request(c, principal, rid: str, decision: str, note: str) -> None:
    if decision not in ("approve", "deny"):
        raise ValueError("bad-decision")
    store = c.store()
    rows = store.execute("SELECT * FROM access_requests WHERE id=?", (rid,))
    if not rows:
        raise ValueError("not-found")
    r = rows[0]
    if r["approver_id"] != principal.id and not principal.is_admin:
        raise ValueError("forbidden")
    if r["requester_id"] == principal.id and not principal.is_admin:
        raise ValueError("self")
    if r["status"] != "pending":
        raise ValueError("decided")
    _decide(c, r, decision, note, principal.id)


def _page(msg: str, ok: bool = True, extra: str = "") -> str:
    color = "#1B7A4B" if ok else "#C0392B"
    return (f"<html><body style='font-family:system-ui;max-width:520px;margin:80px auto;text-align:center'>"
            f"<h2 style='color:{color}'>{msg}</h2>{extra}"
            f"<p style='color:#5A6B82'>You can close this window.</p></body></html>")


def action_request(c, rid: str, token: str, decision: str = "", confirm: int = 0) -> tuple[str, int]:
    """Tokenized approve/deny from an email link (no login). Returns (html, status_code)."""
    import hmac
    store = c.store()
    rows = store.execute("SELECT * FROM access_requests WHERE id=?", (rid,))
    if not rows or not hmac.compare_digest(token, rows[0]["token"] or ""):
        return _page("Invalid or unknown link.", False), 400
    r = rows[0]
    if r["used"] or r["status"] != "pending":
        return _page("This request has already been decided.", False), 409
    if r["token_expires_at"] and r["token_expires_at"] < time.time():
        return _page("This link has expired.", False), 410
    if r["requester_id"] == r["approver_id"]:
        return _page("This request must be decided by an administrator.", False), 403
    if not confirm:
        d = store.execute("SELECT title FROM documents WHERE id=?", (r["doc_id"],))
        base = f"{BASE_URL}/api/access-requests/{rid}/action?token={quote(token)}&confirm=1"
        btn = ("<style>.b{display:inline-block;margin:8px;padding:10px 22px;border-radius:8px;"
               "font-weight:600;text-decoration:none;color:#fff}</style>"
               f"<p style='color:#2E3D52'>Approve access to \"{html.escape(d[0]['title'] if d else rid)}\"?</p>"
               f"<a class='b' style='background:#1B7A4B' href='{base}&decision=approve'>Approve</a>"
               f"<a class='b' style='background:#C0392B' href='{base}&decision=deny'>Deny</a>")
        return _page("Access request", True, btn), 200
    if decision not in ("approve", "deny"):
        return _page("Invalid decision.", False), 400
    _decide(c, r, decision, "via email link", r["approver_id"])
    return _page("Access approved." if decision == "approve" else "Access denied."), 200

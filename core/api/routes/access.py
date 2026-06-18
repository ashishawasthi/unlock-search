"""Access-request + approval routes (thin wrappers over the domain workflow)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from core.api.deps import current_user
from core.domain import access_requests as ar
from core.domain.auth import to_principal

router = APIRouter()

_ERRORS = {"not-found": (404, "Document not found"), "already-access": (400, "You already have access"),
           "already-pending": (400, "You already have a pending request for this document"),
           "bad-decision": (400, "decision must be 'approve' or 'deny'"),
           "forbidden": (403, "Only the document's approver or an admin can decide"),
           "self": (403, "You cannot decide your own access request"),
           "decided": (400, "Request already decided")}


@router.post("/api/access-requests")
def create(body: dict, request: Request, u: dict = Depends(current_user)):
    c = request.app.state.c
    try:
        return ar.create_request(c, to_principal(u), body.get("doc_id"), body.get("justification", ""))
    except ValueError as e:
        code, msg = _ERRORS.get(str(e), (400, "Bad request"))
        raise HTTPException(code, msg)


@router.get("/api/access-requests")
def listing(request: Request, u: dict = Depends(current_user)):
    return ar.list_requests(request.app.state.c, to_principal(u))


@router.post("/api/access-requests/{rid}/decide")
def decide(rid: str, body: dict, request: Request, u: dict = Depends(current_user)):
    c = request.app.state.c
    try:
        ar.decide_request(c, to_principal(u), rid, body.get("decision"), body.get("note"))
        return {"ok": True}
    except ValueError as e:
        code, msg = _ERRORS.get(str(e), (400, "Bad request"))
        raise HTTPException(code, msg)


@router.get("/api/access-requests/{rid}/action", response_class=HTMLResponse)
def action(rid: str, token: str, request: Request, decision: str = "", confirm: int = 0):
    html_body, status = ar.action_request(request.app.state.c, rid, token, decision, confirm)
    return HTMLResponse(html_body, status_code=status)

"""Conversation routes. The turn is delegated to the OrchestratorRuntime port
(Orchestrator -> Retriever -> Generator -> Validator), identical across runtimes."""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Request

from core.api.deps import current_user
from core.domain.audit import audit
from core.domain.auth import to_principal

router = APIRouter()


@router.post("/api/conversations")
def start_conv(body: dict, request: Request, u: dict = Depends(current_user)):
    c = request.app.state.c
    store = c.store()
    conv_id = uuid.uuid4().hex[:8]
    store.execute("INSERT INTO conversations(id,user_id,doc_scope,created_at) VALUES(?,?,?,?)",
                  (conv_id, u["id"], json.dumps(body.get("doc_ids") or []), time.time()))
    audit(store, u["id"], "ai.session_start", {"conv_id": conv_id, "scope": body.get("doc_ids")}, c.telemetry())
    return {"conv_id": conv_id}


@router.post("/api/conversations/{conv_id}/messages")
def send_message(conv_id: str, body: dict, request: Request, u: dict = Depends(current_user)):
    c = request.app.state.c
    store = c.store()
    rows = store.execute("SELECT * FROM conversations WHERE id=? AND user_id=?", (conv_id, u["id"]))
    if not rows:
        raise HTTPException(404)
    scope = json.loads(rows[0]["doc_scope"]) or None
    history = [{"role": r["role"], "content": r["content"]}
               for r in store.execute("SELECT role,content FROM messages WHERE conv_id=? ORDER BY id", (conv_id,))]
    q = (body.get("content") or "").strip()
    if not q:
        raise HTTPException(400)

    try:
        result = c.orchestrator().run_turn(principal=to_principal(u), query=q, history=history, doc_ids=scope)
    except RuntimeError:
        raise HTTPException(502, "The AI service is temporarily unavailable. Please try again.")
    cites = [asdict(x) for x in result.cites]

    store.execute("INSERT INTO messages(conv_id,role,content,cites,created_at) VALUES(?,?,?,?,?)",
                  (conv_id, "user", q, None, time.time()))
    store.execute("INSERT INTO messages(conv_id,role,content,cites,created_at) VALUES(?,?,?,?,?)",
                  (conv_id, "assistant", result.answer, json.dumps(cites), time.time()))
    audit(store, u["id"], "ai.turn", {"conv_id": conv_id, "q": q, "chunks_used": result.chunks_used,
                                      "docs": result.docs, "grounded": result.grounded}, c.telemetry())
    return {"answer": result.answer, "cites": cites, "grounded": result.grounded}

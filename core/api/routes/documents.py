"""Document routes: upload, list, view/download (ACL-checked), patch, share, versions, folders."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response

from core.api.deps import current_user
from core.domain import ingest as ing
from core.domain.abac import SQL_ACL, build_predicate, can_access, clr_label
from core.domain.audit import audit
from core.domain.auth import to_principal
from core.domain.ingest import MEDIA, add_version, doc_full, set_doc_attributes

router = APIRouter()
MAX_MB = int(os.environ.get("AIBOX_MAX_UPLOAD_MB", "50"))


def _file_response(c, key: str, title: str, file_type: str, download: bool):
    os_ = c.object_store()
    ext = "." + file_type
    name = re.sub(r'[\r\n"]', "", title if title.lower().endswith(ext) else title + ext)
    ascii_name = name.encode("ascii", "ignore").decode().strip() or f"document{ext}"
    disp = "attachment" if download else "inline"
    cd = f"{disp}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"
    if os_.supports_signed_urls():
        url = os_.signed_url(key, method="GET", ttl_s=300, content_disposition=cd)
        if url:
            return RedirectResponse(url)
    data = os_.get(key)
    if data is None:
        raise HTTPException(410, "File is no longer available")
    return Response(content=data, media_type=MEDIA.get(file_type, "application/octet-stream"),
                    headers={"Content-Disposition": cd})


@router.post("/api/documents")
async def upload(request: Request, file: UploadFile = File(...), title: str = Form(None),
                 attrs: str = Form(None), u: dict = Depends(current_user)):
    c = request.app.state.c
    store = c.store()
    if u["user_type"] in ("partner", "customer"):
        raise HTTPException(403, "Your account type cannot upload documents")
    ext = Path(file.filename).suffix.lower()
    if ext not in c.parser().supported_types():
        raise HTTPException(400, f"Unsupported type {ext}. Supported: {sorted(c.parser().supported_types())}")
    data = await file.read()
    if not data:
        raise HTTPException(400, "The file is empty.")
    if len(data) > MAX_MB * 1024 * 1024:
        raise HTTPException(413, f"File is too large (limit {MAX_MB} MB).")
    fh = hashlib.sha256(data).hexdigest()[:16]
    dup = store.execute("SELECT title FROM documents WHERE owner_id=? AND file_hash=?", (u["id"], fh))
    if dup:
        raise HTTPException(409, f'You already uploaded this exact file as "{dup[0]["title"]}". Upload a new version instead.')
    try:
        doc_id, pages, key = ing.create_document(c, data, file.filename, title or file.filename, u["id"])
    except ValueError:
        raise HTTPException(422, "Could not read this file. It may be corrupt or password-protected.")
    a = {}
    try:
        a = json.loads(attrs) if attrs else {}
    except Exception:
        a = {}
    a.setdefault("status", "published")
    set_doc_attributes(store, doc_id, a)                       # attributes BEFORE indexing
    n_chunks = ing.index_doc(c, doc_id, pages, title or file.filename)
    if n_chunks == 0:
        ing.rollback_document(c, doc_id, key)
        raise HTTPException(422, "No extractable text found (this looks like a scanned image). "
                                 "OCR is not supported, so the file can't be indexed.")
    audit(store, u["id"], "document.upload",
          {"doc_id": doc_id, "title": title or file.filename, "chunks": n_chunks, "status": a.get("status")},
          c.telemetry())
    return {"doc_id": doc_id, "title": title or file.filename, "pages": len(pages),
            "chunks": n_chunks, "status": a.get("status", "published")}


@router.get("/api/documents")
def list_docs(request: Request, u: dict = Depends(current_user)):
    store = request.app.state.c.store()
    where, params = SQL_ACL.to_sql(build_predicate(to_principal(u)))
    rows = store.execute(f"""SELECT d.*, uo.name AS owner_name, f.name AS folder_name
        FROM documents d JOIN users uo ON uo.id=d.owner_id LEFT JOIN folders f ON f.id=d.folder_id
        WHERE {where} ORDER BY d.created_at DESC""", params)
    out = []
    for r in rows:
        tags = [x["tag"] for x in store.execute("SELECT tag FROM doc_tags WHERE doc_id=?", (r["id"],))]
        shared = [x["name"] for x in store.execute(
            "SELECT g.name FROM doc_grants dg JOIN groups g ON g.id=dg.group_id WHERE dg.doc_id=?", (r["id"],))]
        out.append({**r, "clearance": clr_label(r["min_clearance"]), "tags": tags, "shared_with": shared})
    return out


@router.get("/api/folders")
def list_folders(request: Request, u: dict = Depends(current_user)):
    store = request.app.state.c.store()
    pred = build_predicate(to_principal(u))
    where, params = SQL_ACL.to_sql(pred)
    out = []
    for f in store.execute("SELECT * FROM folders ORDER BY name"):
        if not u["is_admin"] and ("g-" + (f["dept"] or "").lower()) not in pred.groups:
            continue
        n = store.execute(f"SELECT count(*) AS n FROM documents d WHERE d.folder_id=? AND d.status='published' AND {where}",
                          (f["id"], *params))[0]["n"]
        out.append({**f, "count": n})
    return out


@router.get("/api/documents/{doc_id}")
def get_doc(doc_id: str, request: Request, u: dict = Depends(current_user)):
    store = request.app.state.c.store()
    if not can_access(store, build_predicate(to_principal(u)), doc_id):
        raise HTTPException(404, "Not found or access denied")
    return doc_full(store, doc_id)


@router.patch("/api/documents/{doc_id}")
def patch_doc(doc_id: str, body: dict, request: Request, u: dict = Depends(current_user)):
    c = request.app.state.c
    store = c.store()
    rows = store.execute("SELECT owner_id FROM documents WHERE id=?", (doc_id,))
    if not rows:
        raise HTTPException(404)
    if rows[0]["owner_id"] != u["id"] and not u["is_admin"]:
        raise HTTPException(403, "Only the owner or an admin can edit access")
    set_doc_attributes(store, doc_id, body)
    ing.reindex_acl(c, doc_id)                 # keep pushdown ACL in sync with new attributes
    audit(store, u["id"], "document.update", {"doc_id": doc_id, "fields": list(body.keys())}, c.telemetry())
    return doc_full(store, doc_id)


@router.get("/api/documents/{doc_id}/file")
def get_file(doc_id: str, request: Request, download: int = 0, u: dict = Depends(current_user)):
    c = request.app.state.c
    store = c.store()
    where, params = SQL_ACL.to_sql(build_predicate(to_principal(u)))
    rows = store.execute(f"SELECT d.* FROM documents d WHERE d.id=? AND {where}", (doc_id, *params))
    if not rows:
        raise HTTPException(404, "Not found or access denied")
    d = rows[0]
    audit(store, u["id"], "document.download" if download else "document.view", {"doc_id": doc_id}, c.telemetry())
    return _file_response(c, d["storage_key"], d["title"], d["file_type"], bool(download))


@router.post("/api/documents/{doc_id}/share")
def share(doc_id: str, body: dict, request: Request, u: dict = Depends(current_user)):
    c = request.app.state.c
    store = c.store()
    rows = store.execute("SELECT owner_id FROM documents WHERE id=?", (doc_id,))
    if not rows:
        raise HTTPException(404)
    if rows[0]["owner_id"] != u["id"] and not u["is_admin"]:
        raise HTTPException(403, "Only the owner or an admin can share")
    set_doc_attributes(store, doc_id, {"departments": body.get("group_ids", [])})
    ing.reindex_acl(c, doc_id)
    audit(store, u["id"], "document.share", {"doc_id": doc_id, "groups": body.get("group_ids", [])}, c.telemetry())
    return {"ok": True}


@router.get("/api/documents/{doc_id}/chunks")
def get_chunks(doc_id: str, request: Request, u: dict = Depends(current_user)):
    c = request.app.state.c
    store = c.store()
    if not can_access(store, build_predicate(to_principal(u)), doc_id):
        raise HTTPException(404, "Not found or access denied")
    d = store.execute("SELECT id,title,file_type,pages FROM documents WHERE id=?", (doc_id,))[0]
    rows = store.execute("SELECT chunk_id AS id,page_no,chunk_seq,section,content FROM chunks "
                         "WHERE doc_id=? ORDER BY chunk_seq", (doc_id,))
    audit(store, u["id"], "document.view", {"doc_id": doc_id, "mode": "chunks"}, c.telemetry())
    return {"doc": d, "chunks": rows}


@router.get("/api/documents/{doc_id}/versions")
def list_versions(doc_id: str, request: Request, u: dict = Depends(current_user)):
    store = request.app.state.c.store()
    if not can_access(store, build_predicate(to_principal(u)), doc_id):
        raise HTTPException(404, "Not found or access denied")
    return store.execute("""SELECT v.*, uu.name AS uploader_name FROM document_versions v
        LEFT JOIN users uu ON uu.id=v.uploader_id WHERE v.doc_id=? ORDER BY v.version_no DESC""", (doc_id,))


@router.post("/api/documents/{doc_id}/versions")
async def upload_version(doc_id: str, request: Request, file: UploadFile = File(...),
                         change_notes: str = Form(None), u: dict = Depends(current_user)):
    c = request.app.state.c
    store = c.store()
    rows = store.execute("SELECT owner_id FROM documents WHERE id=?", (doc_id,))
    if not rows:
        raise HTTPException(404)
    if rows[0]["owner_id"] != u["id"] and not u["is_admin"]:
        raise HTTPException(403, "Only the owner or an admin can add a version")
    ext = Path(file.filename).suffix.lower()
    if ext not in c.parser().supported_types():
        raise HTTPException(400, f"Unsupported type {ext}")
    data = await file.read()
    if not data:
        raise HTTPException(400, "The file is empty.")
    if len(data) > MAX_MB * 1024 * 1024:
        raise HTTPException(413, f"File is too large (limit {MAX_MB} MB).")
    try:
        n, n_chunks, n_pages = add_version(c, data, file.filename, doc_id, u["id"], change_notes)
    except ValueError as e:
        msg = ("No extractable text found; the new version was not applied." if str(e) == "no-text"
               else "Could not read this file. It may be corrupt or password-protected.")
        raise HTTPException(422, msg)
    audit(store, u["id"], "document.version", {"doc_id": doc_id, "version": n, "chunks": n_chunks}, c.telemetry())
    return {"doc_id": doc_id, "version": n, "pages": n_pages, "chunks": n_chunks}


@router.get("/api/documents/{doc_id}/versions/{version_no}/file")
def get_version_file(doc_id: str, version_no: int, request: Request, download: int = 0,
                     u: dict = Depends(current_user)):
    c = request.app.state.c
    store = c.store()
    if not can_access(store, build_predicate(to_principal(u)), doc_id):
        raise HTTPException(404, "Not found or access denied")
    d = store.execute("SELECT title FROM documents WHERE id=?", (doc_id,))[0]
    v = store.execute("SELECT * FROM document_versions WHERE doc_id=? AND version_no=?", (doc_id, version_no))
    if not v:
        raise HTTPException(404)
    audit(store, u["id"], "document.download", {"doc_id": doc_id, "version": version_no}, c.telemetry())
    return _file_response(c, v[0]["storage_key"], f"{d['title']} v{version_no}", v[0]["file_type"], bool(download))

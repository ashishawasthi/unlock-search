"""
Ingestion orchestration: parse -> object-store -> chunk -> canonical-store -> embed
-> retriever-index -> version. Provider-agnostic: it calls the DocumentParser,
ObjectStore, RelationalStore, Embedder, and Retriever ports via the Container.

The relational store holds the canonical chunk rows (text + chunk_seq + denormalized
ACL); the retriever builds the search structure. This split keeps neighbor-continuation
and citation identity uniform across backends.
"""
from __future__ import annotations

import hashlib
import time
import uuid
from pathlib import Path

from core.domain.abac import clr_label, clr_level
from core.domain.chunking import make_chunks
from core.ports.types import Chunk

MEDIA = {"pdf": "application/pdf", "txt": "text/plain; charset=utf-8",
         "md": "text/markdown; charset=utf-8", "csv": "text/csv; charset=utf-8"}


def _acl_attrs(store, doc_id: str) -> dict:
    """The denormalized ACL a pushdown retriever (OpenSearch / Agent Search on Gemini Enterprise Agent Platform) needs to
    enforce the predicate at the index. Superset so every retriever reads consistent keys:
    `departments` and `doc_grant_groups` are the same group-id set (two names different
    backends expect); `doc_user_grants` is the live, non-expired grantee set."""
    d = store.execute("SELECT owner_id, min_clearance, file_type FROM documents WHERE id=?", (doc_id,))[0]
    depts = [r["group_id"] for r in store.execute("SELECT group_id FROM doc_grants WHERE doc_id=?", (doc_id,))]
    uts = [r["user_type"] for r in store.execute("SELECT user_type FROM doc_user_types WHERE doc_id=?", (doc_id,))]
    projs = [r["project"] for r in store.execute("SELECT project FROM doc_projects WHERE doc_id=?", (doc_id,))]
    grantees = [r["user_id"] for r in store.execute(
        "SELECT user_id FROM doc_user_grants WHERE doc_id=? AND (expires_at IS NULL OR expires_at > ?)",
        (doc_id, time.time()))]
    return {"owner_id": d["owner_id"], "min_clearance": d["min_clearance"], "file_type": d["file_type"],
            "departments": depts, "doc_grant_groups": depts, "user_types": uts, "projects": projs,
            "doc_user_grants": grantees}


def _write_chunks(c, doc_id: str, chunks: list[Chunk], acl: dict) -> int:
    store = c.store()
    store.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
    rows = [(ch.chunk_id, doc_id, ch.page_no, ch.chunk_seq, ch.section, ch.content,
             acl["owner_id"], acl["min_clearance"],
             ",".join(acl["departments"]), ",".join(acl["user_types"]), ",".join(acl["projects"]),
             1 if ch.embedding else 0)
            for ch in chunks]
    store.executemany(
        "INSERT INTO chunks(chunk_id,doc_id,page_no,chunk_seq,section,content,owner_id,"
        "min_clearance,departments,user_types,projects,embedding_present) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    # optional embeddings (no-op embedder returns []), then hand to the retriever index
    emb = c.embedder()
    try:
        vectors = emb.embed([ch.content for ch in chunks]) if chunks else []
    except Exception:
        vectors = []
    if vectors and len(vectors) == len(chunks):
        for ch, v in zip(chunks, vectors):
            ch.embedding = v
    c.retriever().delete_doc(doc_id)
    return c.retriever().index(doc_id, chunks, acl)


def index_doc(c, doc_id: str, pages, title: str) -> int:
    parser = c.parser()
    native = parser.native_chunks(pages)
    if native:
        chunks = native
        for ch in chunks:
            ch.doc_id = doc_id
            if not ch.chunk_id:
                ch.chunk_id = uuid.uuid4().hex
            ch.title = title
    else:
        chunks = make_chunks(doc_id, pages, title)
    return _write_chunks(c, doc_id, chunks, _acl_attrs(c.store(), doc_id))


def create_document(c, data: bytes, filename: str, title: str, owner_id: str) -> tuple[str, list, str]:
    """Parse + persist the file and the document row (owner-only defaults), WITHOUT
    indexing yet. Returns (doc_id, pages, storage_key). Raises ValueError('bad-file').
    Indexing is a separate step so access attributes can be applied first (correct
    denormalized ACL for pushdown backends). Caller must rollback on 0 chunks."""
    parser = c.parser()
    try:
        pages = parser.read_pages(data, filename)
    except Exception as e:
        raise ValueError("bad-file") from e
    ext = Path(filename).suffix.lower().lstrip(".")
    fh = hashlib.sha256(data).hexdigest()[:16]
    doc_id = uuid.uuid4().hex[:8]
    key = f"{uuid.uuid4().hex[:8]}.{ext}"
    c.object_store().put(key, data, MEDIA.get(ext, "application/octet-stream"))
    store = c.store()
    store.execute(
        "INSERT INTO documents(id,title,owner_id,approver_id,file_type,pages,storage_key,file_hash,"
        "min_clearance,status,current_version,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (doc_id, title, owner_id, owner_id, ext, len(pages), key, fh, 2, "published", 1, time.time()))
    store.execute(
        "INSERT INTO document_versions(id,doc_id,version_no,storage_key,file_hash,file_type,pages,"
        "change_notes,uploader_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex[:8], doc_id, 1, key, fh, ext, len(pages), "Initial upload", owner_id, time.time()))
    return doc_id, pages, key


def rollback_document(c, doc_id: str, key: str) -> None:
    store = c.store()
    store.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
    store.execute("DELETE FROM document_versions WHERE doc_id=?", (doc_id,))
    store.execute("DELETE FROM documents WHERE id=?", (doc_id,))
    try:
        c.retriever().delete_doc(doc_id)
        c.object_store().delete(key)
    except Exception:
        pass


def reindex_acl(c, doc_id: str) -> None:
    """Re-sync denormalized chunk ACL + the retriever index after attributes change.
    No-op-ish for SQL backends that enforce via live joins; required for pushdown
    backends (OpenSearch/Gemini Enterprise Agent Platform) whose index carries per-chunk access attributes."""
    store = c.store()
    acl = _acl_attrs(store, doc_id)
    store.execute("UPDATE chunks SET owner_id=?, min_clearance=?, departments=?, user_types=?, projects=? "
                  "WHERE doc_id=?",
                  (acl["owner_id"], acl["min_clearance"], ",".join(acl["departments"]),
                   ",".join(acl["user_types"]), ",".join(acl["projects"]), doc_id))
    ids = [r["chunk_id"] for r in store.execute(
        "SELECT chunk_id FROM chunks WHERE doc_id=? ORDER BY chunk_seq", (doc_id,))]
    chunks = store.get_chunks(ids)
    c.retriever().delete_doc(doc_id)
    if chunks:
        c.retriever().index(doc_id, chunks, acl)


def ingest(c, data: bytes, filename: str, title: str, owner_id: str) -> tuple[str, int, int]:
    """Convenience all-in-one (used by tests/seed where no attributes are applied).
    Returns (doc_id, n_chunks, n_pages). Raises ValueError('no-text') / ('bad-file')."""
    doc_id, pages, key = create_document(c, data, filename, title, owner_id)
    n_chunks = index_doc(c, doc_id, pages, title)
    if n_chunks == 0:
        rollback_document(c, doc_id, key)
        raise ValueError("no-text")
    return doc_id, n_chunks, len(pages)


def add_version(c, data: bytes, filename: str, doc_id: str, uploader_id: str, change_notes: str):
    parser = c.parser()
    try:
        pages = parser.read_pages(data, filename)
    except Exception as e:
        raise ValueError("bad-file") from e
    store = c.store()
    ext = Path(filename).suffix.lower().lstrip(".")
    fh = hashlib.sha256(data).hexdigest()[:16]
    key = f"{uuid.uuid4().hex[:8]}.{ext}"
    c.object_store().put(key, data, MEDIA.get(ext, "application/octet-stream"))
    title = store.execute("SELECT title FROM documents WHERE id=?", (doc_id,))[0]["title"]
    n_chunks = index_doc(c, doc_id, pages, title)
    if n_chunks == 0:
        c.object_store().delete(key)
        raise ValueError("no-text")
    n = (store.execute("SELECT max(version_no) m FROM document_versions WHERE doc_id=?",
                       (doc_id,))[0]["m"] or 1) + 1
    store.execute("UPDATE documents SET storage_key=?, file_hash=?, file_type=?, pages=?, current_version=? "
                  "WHERE id=?", (key, fh, ext, len(pages), n, doc_id))
    store.execute(
        "INSERT INTO document_versions(id,doc_id,version_no,storage_key,file_hash,file_type,pages,"
        "change_notes,uploader_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex[:8], doc_id, n, key, fh, ext, len(pages), change_notes or "", uploader_id, time.time()))
    return n, n_chunks, len(pages)


# ---- document attributes (shared by upload + PATCH + share) ----
def set_doc_attributes(store, doc_id: str, a: dict) -> None:
    from core.domain.abac import USER_TYPES
    if a.get("title"):
        store.execute("UPDATE documents SET title=? WHERE id=?", (a["title"], doc_id))
    if "min_clearance" in a:
        store.execute("UPDATE documents SET min_clearance=? WHERE id=?", (clr_level(a["min_clearance"]), doc_id))
    if a.get("approver_id"):
        store.execute("UPDATE documents SET approver_id=? WHERE id=?", (a["approver_id"], doc_id))
    if "folder_id" in a:
        store.execute("UPDATE documents SET folder_id=? WHERE id=?", (a["folder_id"] or None, doc_id))
    if a.get("status") in ("draft", "published", "archived"):
        store.execute("UPDATE documents SET status=? WHERE id=?", (a["status"], doc_id))
    if "departments" in a:
        store.execute("DELETE FROM doc_grants WHERE doc_id=?", (doc_id,))
        for gid in a["departments"]:
            store.execute("INSERT OR IGNORE INTO doc_grants(doc_id,group_id) VALUES(?,?)", (doc_id, gid))
    if "user_types" in a:
        store.execute("DELETE FROM doc_user_types WHERE doc_id=?", (doc_id,))
        for ut in a["user_types"]:
            if ut in USER_TYPES:
                store.execute("INSERT OR IGNORE INTO doc_user_types(doc_id,user_type) VALUES(?,?)", (doc_id, ut))
    if "projects" in a:
        store.execute("DELETE FROM doc_projects WHERE doc_id=?", (doc_id,))
        for p in a["projects"]:
            store.execute("INSERT OR IGNORE INTO doc_projects(doc_id,project) VALUES(?,?)", (doc_id, p))
    if "tags" in a:
        store.execute("DELETE FROM doc_tags WHERE doc_id=?", (doc_id,))
        for t in a["tags"]:
            store.execute("INSERT OR IGNORE INTO doc_tags(doc_id,tag) VALUES(?,?)", (doc_id, t))


def doc_full(store, doc_id: str) -> dict | None:
    rows = store.execute("""SELECT d.*, uo.name AS owner_name, ua.name AS approver_name, f.name AS folder_name
        FROM documents d JOIN users uo ON uo.id=d.owner_id
        LEFT JOIN users ua ON ua.id=d.approver_id LEFT JOIN folders f ON f.id=d.folder_id WHERE d.id=?""", (doc_id,))
    if not rows:
        return None
    r = dict(rows[0])
    r["clearance"] = clr_label(r.get("min_clearance"))
    r["shared_with"] = [x["name"] for x in store.execute(
        "SELECT g.name FROM doc_grants dg JOIN groups g ON g.id=dg.group_id WHERE dg.doc_id=?", (doc_id,))]
    r["tags"] = [x["tag"] for x in store.execute("SELECT tag FROM doc_tags WHERE doc_id=?", (doc_id,))]
    r["user_types"] = [x["user_type"] for x in store.execute("SELECT user_type FROM doc_user_types WHERE doc_id=?", (doc_id,))]
    r["projects"] = [x["project"] for x in store.execute("SELECT project FROM doc_projects WHERE doc_id=?", (doc_id,))]
    return r

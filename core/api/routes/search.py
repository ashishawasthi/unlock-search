"""Access-aware search: full cards for accessible matches, server-redacted restricted cards, facets."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from core.api.deps import current_user
from core.domain.abac import build_predicate, clr_label
from core.domain.audit import audit
from core.domain.auth import to_principal

router = APIRouter()


def _facet(counter: dict) -> list:
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))


def _departments_of(store, doc_id: str) -> list[str]:
    return [r["name"] for r in store.execute(
        "SELECT g.name FROM doc_grants dg JOIN groups g ON g.id=dg.group_id WHERE dg.doc_id=?", (doc_id,))]


@router.post("/api/search")
def search(body: dict, request: Request, u: dict = Depends(current_user)):
    c = request.app.state.c
    store = c.store()
    q = (body.get("q") or "").strip()
    filters = body.get("filters") or {}
    empty = {"results": [], "restricted": [], "facets": {"type": [], "department": [], "clearance": []}}
    if not q:
        return empty
    pred = build_predicate(to_principal(u))

    hits = c.retriever().search(query=q, pred=pred, k=40)
    best: dict = {}
    order: list[str] = []
    for h in hits:
        if h.doc_id not in best:
            best[h.doc_id] = h
            order.append(h.doc_id)

    ftype, fdept, fclr = {}, {}, {}
    dept_cache: dict[str, list[str]] = {}
    for did in order:
        h = best[did]
        deps = dept_cache[did] = _departments_of(store, did)
        ftype[h.file_type] = ftype.get(h.file_type, 0) + 1
        lbl = clr_label(h.min_clearance)
        fclr[lbl] = fclr.get(lbl, 0) + 1
        for dep in deps:
            fdept[dep] = fdept.get(dep, 0) + 1

    def keep(h) -> bool:
        if filters.get("type") and h.file_type not in filters["type"]:
            return False
        if filters.get("clearance") and clr_label(h.min_clearance) not in filters["clearance"]:
            return False
        if filters.get("department") and not (set(filters["department"]) & set(dept_cache.get(h.doc_id, []))):
            return False
        return True

    results = [{"doc_id": h.doc_id, "title": h.title, "file_type": h.file_type, "page_no": h.page_no,
                "section": h.section, "snippet": h.content[:240], "clearance": clr_label(h.min_clearance),
                "departments": dept_cache.get(h.doc_id, [])}
               for did in order if keep(h := best[did])]

    restricted = []
    for did in c.retriever().search_inaccessible(query=q, pred=pred, k=12):
        rows = store.execute("""SELECT d.id, d.title, d.min_clearance, d.status,
            (SELECT content FROM chunks WHERE doc_id=d.id ORDER BY chunk_seq LIMIT 1) AS head
            FROM documents d WHERE d.id=?""", (did,))
        if not rows or rows[0]["status"] != "published":
            continue
        d = rows[0]
        redacted = " ".join((d["head"] or "").split()[:10]) + " ..."
        restricted.append({"doc_id": d["id"], "title": d["title"], "redacted": redacted,
                           "clearance": clr_label(d["min_clearance"]),
                           "reason": f"Restricted -- {clr_label(d['min_clearance'])} clearance or department required"})

    audit(store, u["id"], "search.query", {"q": q, "filters": filters, "results": len(results),
                                           "restricted": len(restricted)}, c.telemetry())
    return {"results": results, "restricted": restricted,
            "facets": {"type": _facet(ftype), "department": _facet(fdept), "clearance": _facet(fclr)}}

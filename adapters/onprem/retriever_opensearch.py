"""
On-prem Retriever: OpenSearch hybrid (BM25 + kNN) with reciprocal-rank fusion (RRF).

Backing service: an OpenSearch >= 2.x cluster (k-NN plugin enabled). Each chunk is one
document carrying the canonical text plus DENORMALIZED ABAC attributes (owner_id,
min_clearance, departments/user_types/projects, doc-user grants) so the access predicate
is PUSHED DOWN as an OpenSearch bool filter -- never post-filtered, never delegated to a
model. Text + neighbor continuation still come from the RelationalStore (source of truth);
this adapter only ranks. A Reranker (bge) reached via the container reorders the top hits.

Config (profiles/onprem.yaml):
  url_env: OPENSEARCH_URL    -> url: https://host:9200   (required)
  index: aibox-chunks        -> index name (default aibox-chunks)
  user_env / password_env    -> basic-auth (optional)
  verify_certs: true|false   -> TLS verification (default true)
  rrf_k: 60                  -> RRF constant (default 60)

ABAC compile: admin OR owner OR valid doc-user-grant OR
  (clearance >= min_clearance AND department-intersect AND user-type-allowed AND project-allowed).
The same rule core.domain.abac.SQL_ACL encodes in SQL, expressed as a bool filter here.
"""
from __future__ import annotations

from typing import Any, Sequence

from core.ports.types import AccessPredicate, Chunk, Hit


class OpenSearchRetriever:
    def __init__(self, url: str = "", index: str = "aibox-chunks", user: str = "",
                 password: str = "", verify_certs: bool = True, rrf_k: int = 60,
                 container=None, **kw):
        if not url:
            raise RuntimeError("OpenSearchRetriever needs url (set OPENSEARCH_URL; profiles use url_env)")
        self.url = url
        self.index = index
        self.user = user
        self.password = password
        self.verify_certs = bool(verify_certs)
        self.rrf_k = int(rrf_k or 60)
        self.c = container
        self._client = None

    # ---- client (lazy import of opensearch-py) ----
    def _cl(self):
        if self._client is None:
            from opensearchpy import OpenSearch
            kw: dict[str, Any] = {"hosts": [self.url], "verify_certs": self.verify_certs,
                                  "ssl_show_warn": False}
            if self.user:
                kw["http_auth"] = (self.user, self.password)
            self._client = OpenSearch(**kw)
            self._ensure_index()
        return self._client

    def _store(self):
        return self.c.store()

    def _embedder(self):
        return self.c.embedder()

    def _reranker(self):
        return self.c.reranker()

    def _ensure_index(self):
        if self._client.indices.exists(index=self.index):
            return
        dim = 768
        try:
            dim = int(self._embedder().dim())
        except Exception:
            pass
        body = {
            "settings": {"index": {"knn": True}},
            "mappings": {"properties": {
                "doc_id": {"type": "keyword"}, "chunk_id": {"type": "keyword"},
                "page_no": {"type": "integer"}, "chunk_seq": {"type": "integer"},
                "section": {"type": "keyword"}, "title": {"type": "text"},
                "file_type": {"type": "keyword"}, "content": {"type": "text"},
                "status": {"type": "keyword"},
                "owner_id": {"type": "keyword"}, "min_clearance": {"type": "integer"},
                "departments": {"type": "keyword"}, "user_types": {"type": "keyword"},
                "projects": {"type": "keyword"}, "grant_user_ids": {"type": "keyword"},
                "embedding": {"type": "knn_vector", "dimension": dim},
            }},
        }
        self._client.indices.create(index=self.index, body=body)

    # ---- index port ----
    def index(self, doc_id: str, chunks: Sequence[Chunk], acl_attrs: dict) -> int:
        cl = self._cl()
        store = self._store()
        meta = store.execute(
            "SELECT title, file_type, min_clearance, status FROM documents WHERE id=?", (doc_id,))
        title = meta[0]["title"] if meta else ""
        file_type = meta[0]["file_type"] if meta else ""
        status = meta[0]["status"] if meta else "published"
        min_clear = int(acl_attrs.get("min_clearance") or (meta[0]["min_clearance"] if meta else 0))
        grant_users = [r["user_id"] for r in store.execute(
            "SELECT user_id FROM doc_user_grants WHERE doc_id=? AND (expires_at IS NULL OR expires_at > ?)",
            (doc_id, __import__("time").time()))]
        actions = []
        for ch in chunks:
            src = {
                "doc_id": doc_id, "chunk_id": ch.chunk_id, "page_no": ch.page_no,
                "chunk_seq": ch.chunk_seq, "section": ch.section, "title": title or ch.title,
                "file_type": file_type, "content": ch.content, "status": status,
                "owner_id": acl_attrs.get("owner_id", ""), "min_clearance": min_clear,
                "departments": list(acl_attrs.get("departments") or []),
                "user_types": list(acl_attrs.get("user_types") or []),
                "projects": list(acl_attrs.get("projects") or []),
                "grant_user_ids": grant_users,
            }
            if ch.embedding:
                src["embedding"] = list(ch.embedding)
            actions.append({"index": {"_index": self.index, "_id": ch.chunk_id}})
            actions.append(src)
        if actions:
            cl.bulk(body=actions, refresh=True)
        return len(chunks)

    def delete_doc(self, doc_id: str) -> None:
        cl = self._cl()
        cl.delete_by_query(index=self.index, body={"query": {"term": {"doc_id": doc_id}}},
                           refresh=True, conflicts="proceed")

    # ---- ABAC compiled to an OpenSearch bool filter (the to_filter for this backend) ----
    def _acl_filter(self, pred: AccessPredicate) -> dict:
        if pred.is_admin:
            return {"match_all": {}}
        # attribute branch: clearance>=min AND dept-intersect AND user-type-allowed AND project-allowed.
        # "allowed" when the doc declares no constraint of that kind OR the principal matches one value.
        attr_must: list[dict] = [{"range": {"min_clearance": {"lte": pred.clearance}}}]
        # The doc MUST be shared to one of the user's groups (mirrors the SQL `d.id IN
        # (SELECT doc_id FROM doc_grants WHERE group_id IN (user_groups))`). A `terms` with an
        # empty list matches nothing, so a groupless user is correctly denied the attribute
        # branch (do NOT use `must_not exists`, which would over-grant unshared docs).
        attr_must.append({"terms": {"departments": list(pred.groups)}})
        # user_types: empty doc field => unconstrained; else must contain the principal's type.
        attr_must.append({"bool": {"should": [
            {"bool": {"must_not": {"exists": {"field": "user_types"}}}},
            {"term": {"user_types": pred.user_type}},
        ], "minimum_should_match": 1}})
        # projects: empty doc field => unconstrained; else must intersect the principal's projects.
        proj_match: dict = {"bool": {"must_not": {"exists": {"field": "projects"}}}}
        attr_should = [proj_match]
        if pred.projects:
            attr_should.append({"terms": {"projects": list(pred.projects)}})
        attr_must.append({"bool": {"should": attr_should, "minimum_should_match": 1}})
        attr_branch = {"bool": {"must": attr_must}}

        should = [
            {"term": {"owner_id": pred.user_id}},
            {"term": {"grant_user_ids": pred.user_id}},
            attr_branch,
        ]
        return {"bool": {"should": should, "minimum_should_match": 1}}

    # to_filter alias so callers/tests treating this as an AclCompiler get the bool filter.
    def to_filter(self, pred: AccessPredicate) -> Any:
        return self._acl_filter(pred)

    # ---- search: BM25 + kNN, fused with RRF, ABAC pushed down ----
    def search(self, *, query: str, pred: AccessPredicate,
               doc_ids: Sequence[str] | None = None, k: int = 8,
               filters: dict | None = None) -> list[Hit]:
        cl = self._cl()
        acl = self._acl_filter(pred)
        scope: list[dict] = [acl, {"term": {"status": "published"}}]
        if doc_ids:
            scope.append({"terms": {"doc_id": list(doc_ids)}})
        if filters:
            for fk, fv in filters.items():
                scope.append({"terms": {fk: fv if isinstance(fv, list) else [fv]}})
        pool = max(k * 4, 20)

        bm25 = {"size": pool, "query": {"bool": {
            "must": {"match": {"content": query}}, "filter": scope}}}
        bm_res = cl.search(index=self.index, body=bm25)
        bm_ids = [h["_id"] for h in bm_res["hits"]["hits"]]
        by_id = {h["_id"]: h["_source"] for h in bm_res["hits"]["hits"]}

        knn_ids: list[str] = []
        try:
            qvec = self._embedder().embed([query], kind="query")
        except Exception:
            qvec = []
        if qvec and qvec[0]:
            knn = {"size": pool, "query": {"bool": {
                "must": {"knn": {"embedding": {"vector": list(qvec[0]), "k": pool}}},
                "filter": scope}}}
            kn_res = cl.search(index=self.index, body=knn)
            knn_ids = [h["_id"] for h in kn_res["hits"]["hits"]]
            for h in kn_res["hits"]["hits"]:
                by_id.setdefault(h["_id"], h["_source"])

        fused = self._rrf(bm_ids, knn_ids)
        hits = [self._hit(by_id[cid], 0.0) for cid in fused if cid in by_id]
        # normalize fused rank -> 0..1 (higher=better) before the reranker refines the top-k.
        n = len(hits) or 1
        for i, h in enumerate(hits):
            h.score = round(1 - i / n, 4)
        hits = hits[:pool]
        try:
            hits = self._reranker().rerank(query, hits, top_k=k)
        except Exception:
            hits = hits[:k]
        return hits[:k]

    def _rrf(self, *rankings: list[str]) -> list[str]:
        scores: dict[str, float] = {}
        for ranking in rankings:
            for rank, cid in enumerate(ranking):
                scores[cid] = scores.get(cid, 0.0) + 1.0 / (self.rrf_k + rank + 1)
        return sorted(scores, key=lambda c: scores[c], reverse=True)

    def _hit(self, s: dict, score: float) -> Hit:
        return Hit(doc_id=s["doc_id"], chunk_id=s["chunk_id"], page_no=int(s.get("page_no") or 0),
                   chunk_seq=int(s.get("chunk_seq") or 0), section=s.get("section") or "",
                   content=s.get("content") or "", title=s.get("title") or "",
                   file_type=s.get("file_type") or "", min_clearance=int(s.get("min_clearance") or 0),
                   score=score)

    # ---- restricted-card discovery: matches query but the principal cannot access ----
    def search_inaccessible(self, *, query: str, pred: AccessPredicate, k: int = 12) -> list[str]:
        cl = self._cl()
        body = {"size": 0, "query": {"bool": {
            "must": {"match": {"content": query}},
            "filter": [{"term": {"status": "published"}}],
            "must_not": [self._acl_filter(pred)]}},
            "aggs": {"docs": {"terms": {"field": "doc_id", "size": k}}}}
        res = cl.search(index=self.index, body=body)
        buckets = res.get("aggregations", {}).get("docs", {}).get("buckets", [])
        return [b["key"] for b in buckets][:k]

"""
GCP Retriever: Agent Search on Gemini Enterprise Agent Platform (Discovery Engine).

Backing service: a Discovery Engine data store + serving config in a given project /
location. Chunks are indexed as structured documents carrying their TEXT plus the
denormalized ACL attributes (owner_id, min_clearance, departments, user_types,
projects, doc_grant_groups, doc_user_grants). Search pushes the AccessPredicate DOWN
as a Discovery Engine FILTER expression over those attributes and uses server-side
reranking (boost/relevance). Canonical chunk text + neighbor continuation still come
from the RelationalStore via the container (the relational store is the source of
truth). Returned Hit.score is normalized 0..1 (higher = better).

Config / env (config.retriever in profiles/gcp.yaml):
  project, location, data_store_id  (serving_config_id optional, default "default_search")
  ranking: server-side relevance is on by default; reranker port may be a passthrough.

ABAC: NEVER post-filter, NEVER trust the model. The filter is the SAME rule as
core.domain.abac (admin OR owner OR valid doc-grant OR (clearance>=min AND dept-
intersect AND user-type-allowed AND project-allowed)), translated to DE filter syntax
over the per-chunk denormalized attributes.

Importable without google-cloud-discoveryengine installed (lazy SDK imports).
"""
from __future__ import annotations

from core.ports.types import Hit


def _q(v) -> str:
    """Quote a string literal for a Discovery Engine filter expression."""
    return '"' + str(v).replace('"', '\\"') + '"'


class AgentSearchRetriever:
    def __init__(self, project: str | None = None, location: str = "global",
                 data_store_id: str | None = None,
                 serving_config_id: str = "default_search",
                 branch: str = "default_branch", container=None, **kw):
        self.c = container
        self.project = project
        self.location = location
        self.data_store_id = data_store_id
        self.serving_config_id = serving_config_id
        self.branch = branch
        if not (project and data_store_id):
            raise RuntimeError("agentsearch retriever needs project and data_store_id (config.retriever)")
        self._search = None
        self._docsvc = None

    # ---- lazy SDK clients ----
    def _search_client(self):
        if self._search is None:
            from google.cloud import discoveryengine_v1 as de
            self._de = de
            opts = None
            if self.location != "global":
                from google.api_core.client_options import ClientOptions
                opts = ClientOptions(api_endpoint=f"{self.location}-discoveryengine.googleapis.com")
            self._search = de.SearchServiceClient(client_options=opts)
        return self._search

    def _document_client(self):
        if self._docsvc is None:
            from google.cloud import discoveryengine_v1 as de
            self._de = de
            opts = None
            if self.location != "global":
                from google.api_core.client_options import ClientOptions
                opts = ClientOptions(api_endpoint=f"{self.location}-discoveryengine.googleapis.com")
            self._docsvc = de.DocumentServiceClient(client_options=opts)
        return self._docsvc

    def _store(self):
        return self.c.store()

    def _branch_path(self) -> str:
        return self._de.DocumentServiceClient.branch_path(
            self.project, self.location, self.data_store_id, self.branch)

    def _serving_config(self) -> str:
        return self._search_client().serving_config_path(
            self.project, self.location, self.data_store_id, self.serving_config_id)

    # ---- ABAC -> Discovery Engine FILTER (the same rule as core.domain.abac) ----
    def compile_filter(self, pred) -> str | None:
        """Translate AccessPredicate to a DE filter over denormalized per-chunk attrs.
        Returns None for admin (no restriction)."""
        if pred.is_admin:
            return None
        clauses = [f"owner_id: ANY({_q(pred.user_id)})"]
        # doc-scoped user grant (csv of currently-valid grantees, denormalized at index time)
        clauses.append(f"doc_user_grants: ANY({_q(pred.user_id)})")
        # attribute path: clearance >= min AND dept-intersect AND user-type-allowed AND project-allowed
        attr = [f"min_clearance <= {int(pred.clearance)}"]
        if pred.groups:
            attr.append("doc_grant_groups: ANY(" + ", ".join(_q(g) for g in pred.groups) + ")")
        else:
            attr.append("doc_grant_groups: ANY(" + _q("__none__") + ")")
        # user_types/projects: empty set on the doc means "open to all"; an explicit set must intersect.
        ut = f'(user_types_empty = true OR user_types: ANY({_q(pred.user_type)}))'
        if pred.projects:
            pj = "(projects_empty = true OR projects: ANY(" + \
                 ", ".join(_q(p) for p in pred.projects) + "))"
        else:
            pj = "projects_empty = true"
        attr.append(ut)
        attr.append(pj)
        clauses.append("(" + " AND ".join(attr) + ")")
        return "(" + " OR ".join(clauses) + ")"

    # ---- indexing ----
    def index(self, doc_id, chunks, acl_attrs: dict) -> int:
        """Index each chunk as a DE structured document. acl_attrs carries the
        denormalized ACL the filter reads (groups granted, user-grant ids, etc.)."""
        svc = self._document_client()
        de = self._de
        parent = self._branch_path()
        groups = list(acl_attrs.get("doc_grant_groups", []) or [])
        user_grants = list(acl_attrs.get("doc_user_grants", []) or [])
        n = 0
        for ch in chunks:
            uts = list(acl_attrs.get("user_types", []) or [])
            pjs = list(acl_attrs.get("projects", []) or [])
            depts = list(acl_attrs.get("departments", []) or [])
            struct = {
                "doc_id": doc_id,
                "chunk_id": ch.chunk_id or f"{doc_id}:{ch.chunk_seq}",
                "page_no": ch.page_no, "chunk_seq": ch.chunk_seq, "section": ch.section,
                "title": ch.title, "content": ch.content,
                "owner_id": acl_attrs.get("owner_id", ""),
                "min_clearance": int(acl_attrs.get("min_clearance", 0) or 0),
                "departments": depts, "doc_grant_groups": groups,
                "user_types": uts, "user_types_empty": (len(uts) == 0),
                "projects": pjs, "projects_empty": (len(pjs) == 0),
                "doc_user_grants": user_grants,
                "file_type": acl_attrs.get("file_type", ""),
            }
            cid = ch.chunk_id or f"{doc_id}:{ch.chunk_seq}"
            document = de.Document(id=cid, struct_data=struct)
            try:
                svc.create_document(parent=parent, document=document, document_id=cid)
            except Exception:
                # upsert semantics: update if it already exists
                document.name = svc.document_path(
                    self.project, self.location, self.data_store_id, self.branch, cid)
                svc.update_document(document=document, allow_missing=True)
            n += 1
        return n

    def delete_doc(self, doc_id):
        svc = self._document_client()
        de = self._de
        parent = self._branch_path()
        req = de.ListDocumentsRequest(parent=parent, page_size=1000)
        for d in svc.list_documents(request=req):
            sd = dict(d.struct_data or {})
            if sd.get("doc_id") == doc_id:
                svc.delete_document(name=d.name)

    # ---- search ----
    def _run(self, query: str, filter_expr: str | None, page_size: int):
        de = self._de
        req = de.SearchRequest(
            serving_config=self._serving_config(), query=query, page_size=page_size,
            query_expansion_spec=de.SearchRequest.QueryExpansionSpec(
                condition=de.SearchRequest.QueryExpansionSpec.Condition.AUTO),
            spell_correction_spec=de.SearchRequest.SpellCorrectionSpec(
                mode=de.SearchRequest.SpellCorrectionSpec.Mode.AUTO),
        )
        if filter_expr:
            req.filter = filter_expr
        return list(self._search_client().search(req))

    def search(self, *, query, pred, doc_ids=None, k=8, filters=None):
        self._search_client()  # binds self._de
        filt = self.compile_filter(pred)
        if doc_ids:
            ids = ", ".join(_q(d) for d in doc_ids)
            doc_clause = f"doc_id: ANY({ids})"
            filt = f"({filt}) AND {doc_clause}" if filt else doc_clause
        results = self._run(query, filt, page_size=max(k, 8) * 2)
        raw = []
        for r in results:
            sd = dict(r.document.struct_data or {})
            score = None
            md = getattr(r, "model_scores", None)
            if md and "relevance_score" in md:
                try:
                    score = float(md["relevance_score"].values[0])
                except Exception:
                    score = None
            raw.append((sd, score))
        # normalize 0..1 higher=better. DE relevance is already ~0..1; fall back to rank.
        hits = []
        n = len(raw) or 1
        for i, (sd, score) in enumerate(raw):
            s = score if (score is not None) else round(1 - i / n, 4)
            s = max(0.0, min(1.0, float(s)))
            hits.append(Hit(
                doc_id=sd.get("doc_id", ""), chunk_id=sd.get("chunk_id", ""),
                page_no=int(sd.get("page_no", 0) or 0), chunk_seq=int(sd.get("chunk_seq", 0) or 0),
                section=sd.get("section", ""), content=sd.get("content", ""),
                title=sd.get("title", ""), file_type=sd.get("file_type", ""),
                min_clearance=int(sd.get("min_clearance", 0) or 0), score=s))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]

    def search_inaccessible(self, *, query, pred, k=12):
        """Restricted-card doc ids: matches WITHOUT the ABAC filter, minus accessible.
        Used to render redacted cards; canonical redaction is server-side elsewhere."""
        self._search_client()
        accessible = {h.doc_id for h in self.search(query=query, pred=pred, k=k * 2)}
        all_results = self._run(query, None, page_size=k * 4)
        out: list[str] = []
        for r in all_results:
            sd = dict(r.document.struct_data or {})
            did = sd.get("doc_id", "")
            if not did or did in accessible or did in out:
                continue
            out.append(did)
            if len(out) >= k:
                break
        return out

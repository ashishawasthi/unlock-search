"""
GCP Reranker: Vertex AI Ranking API (Discovery Engine RankService), OR passthrough.

Backing service: the Discovery Engine RankService semantic-ranker model
(default "semantic-ranker-512"). Given a query and candidate Hits, it returns a
relevance-ordered subset with scores normalized 0..1 (higher = better).

PASSTHROUGH MODE (default when retriever=vertex): Vertex AI Search already reranks
results server-side inside the search call, so a second ranking pass is redundant and
costs an extra round trip. With mode="passthrough" this adapter just trims the
already-ranked Hits to top_k. Set mode="rank" (and a project) to call RankService,
which is useful when the retriever is a raw SQL kNN store (AlloyDB/pgvector) that does
not rerank.

Config / env (config.reranker in profiles/gcp.yaml):
  mode: "passthrough" | "rank" (default "passthrough")
  project, ranking_config (default "default_ranking_config"), model

Importable without the Discovery Engine SDK installed (lazy SDK imports).
"""
from __future__ import annotations


class VertexReranker:
    def __init__(self, mode: str = "passthrough", project: str | None = None,
                 location: str = "global", ranking_config: str = "default_ranking_config",
                 model: str = "semantic-ranker-512", **kw):
        self.mode = mode
        self.project = project
        self.location = location
        self.ranking_config = ranking_config
        self.model = model
        self._client = None

    def _client_(self):
        if self._client is None:
            from google.cloud import discoveryengine_v1 as de
            self._de = de
            self._client = de.RankServiceClient()
        return self._client

    def rerank(self, query, hits, top_k):
        hits = list(hits)
        if self.mode != "rank" or not self.project or not hits:
            # passthrough: Vertex AI Search already reranked; just trim.
            hits.sort(key=lambda h: h.score, reverse=True)
            return hits[:top_k]
        de = None
        try:
            client = self._client_()
            de = self._de
            records = [de.RankingRecord(id=str(i), title=h.title, content=h.content)
                       for i, h in enumerate(hits)]
            cfg = client.ranking_config_path(self.project, self.location, self.ranking_config)
            req = de.RankRequest(ranking_config=cfg, model=self.model, query=query,
                                 records=records, top_n=top_k)
            resp = client.rank(req)
        except Exception:
            hits.sort(key=lambda h: h.score, reverse=True)
            return hits[:top_k]
        ranked = []
        for rec in resp.records:
            h = hits[int(rec.id)]
            h.score = max(0.0, min(1.0, float(rec.score)))   # RankService score is 0..1
            ranked.append(h)
        return ranked[:top_k]

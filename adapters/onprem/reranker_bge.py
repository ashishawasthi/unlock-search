"""
On-prem Reranker: a hosted BGE cross-encoder rerank endpoint via httpx.

Backing service: a bge-reranker server. Two common contracts are accepted:
  1. text-embeddings-inference /rerank  -> {"query","texts"} => [{"index","score"}]
  2. a generic /rerank                  -> {"query","documents"} => {"results":[{"index","relevance_score"}]}
Reorders the candidate Hits and sets normalized 0..1 scores (higher=better). If no endpoint
is configured it degrades gracefully to a pass-through (returns the input order truncated to
top_k), so the agent loop still runs without the rerank service.

Config (profiles/onprem.yaml):
  endpoint_env: RERANK_ENDPOINT   -> endpoint: http://reranker:8080/rerank (optional)
  model: bge-reranker-base        -> model id (default bge-reranker-base)
  api_key_env: RERANK_API_KEY     -> bearer token (optional)
  timeout_s: 30
"""
from __future__ import annotations

from typing import Sequence

from core.ports.types import Hit


class BgeReranker:
    def __init__(self, endpoint: str = "", model: str = "bge-reranker-base", api_key: str = "",
                 timeout_s: float = 30.0, **kw):
        self.endpoint = endpoint
        self.model = model
        self.api_key = api_key
        self.timeout_s = float(timeout_s)

    def rerank(self, query: str, hits: Sequence[Hit], top_k: int) -> list[Hit]:
        items = list(hits)
        if not items:
            return []
        if not self.endpoint:
            return items[:top_k]                          # graceful passthrough, keep input order

        import httpx
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        docs = [h.content for h in items]
        payload = {"model": self.model, "query": query, "texts": docs, "documents": docs}
        try:
            r = httpx.post(self.endpoint, json=payload, headers=headers, timeout=self.timeout_s)
            r.raise_for_status()
            scored = self._parse(r.json())               # [(index, raw_score)]
        except Exception:
            return items[:top_k]                          # degrade rather than fail the turn

        if not scored:
            return items[:top_k]
        scored.sort(key=lambda t: t[1], reverse=True)
        raw = [s for _, s in scored]
        lo, hi = min(raw), max(raw)
        span = (hi - lo) or 1.0
        out: list[Hit] = []
        for idx, sc in scored:
            if 0 <= idx < len(items):
                h = items[idx]
                h.score = round((sc - lo) / span, 4)     # normalize 0..1, higher=better
                out.append(h)
        return out[:top_k]

    @staticmethod
    def _parse(body) -> list[tuple[int, float]]:
        # TEI returns a bare list; generic servers nest under "results".
        rows = body.get("results", body) if isinstance(body, dict) else body
        out: list[tuple[int, float]] = []
        for i, row in enumerate(rows or []):
            if isinstance(row, dict):
                idx = int(row.get("index", i))
                sc = row.get("score", row.get("relevance_score", 0.0))
                out.append((idx, float(sc)))
        return out

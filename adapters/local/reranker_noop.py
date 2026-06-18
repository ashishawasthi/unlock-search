"""Local Reranker: identity (the FTS5 BM25 order stands). Keeps the port satisfied."""
from __future__ import annotations


class NoopReranker:
    def __init__(self, **kw):
        pass

    def rerank(self, query, hits, top_k):
        return list(hits)[:top_k]

"""Local Embedder: none (the FTS5 retriever is lexical). Keeps the port satisfied."""
from __future__ import annotations


class NoopEmbedder:
    def __init__(self, **kw):
        pass

    def embed(self, texts, *, kind="document"):
        return []

    def dim(self) -> int:
        return 0

"""
Post-retrieval continuation. Pure CORE logic over chunk_seq; reads chunk text and
neighbors from the RelationalStore port (the source of truth for text), so it works
identically whether the ranker is FTS5, OpenSearch, or Agent Search on Gemini Enterprise Agent Platform.
"""
from __future__ import annotations

from core.ports.types import Chunk, Hit


def expand_continuation(store, hits: list[Hit], budget_chars: int = 24000) -> list[Chunk]:
    """Best-first: for each hit (already relevance-ordered) take the hit chunk then its
    +/-1 neighbors; stop once the budget is exhausted so top hits are never crowded out."""
    seen: set[str] = set()
    out: list[Chunk] = []
    total = 0
    for h in hits:
        # neighbors() returns [seq-1, seq, seq+1] inclusive; order hit chunk first.
        trio = {c.chunk_seq: c for c in store.neighbors(h.doc_id, h.chunk_seq, 1)}
        ordered = [trio.get(h.chunk_seq), trio.get(h.chunk_seq - 1), trio.get(h.chunk_seq + 1)]
        budget_hit = False
        for c in ordered:
            if not c or (c.chunk_id or f"{c.doc_id}:{c.chunk_seq}") in seen:
                continue
            cid = c.chunk_id or f"{c.doc_id}:{c.chunk_seq}"
            if out and total + len(c.content) > budget_chars:   # always keep the top hit's chunk
                budget_hit = True
                break
            seen.add(cid); total += len(c.content); out.append(c)
        if budget_hit:
            break
    out.sort(key=lambda c: (c.doc_id, c.chunk_seq))
    return out

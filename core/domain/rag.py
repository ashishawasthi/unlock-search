"""
RAG contract helpers: prompt-context assembly + citation extraction. These operate
on neutral LLM text ([n] markers), so they are identical whether Gemini or Gemma
produced the answer. Reused by every orchestrator runtime.
"""
from __future__ import annotations

import re

from core.ports.types import Chunk, Citation


def build_context(blocks: list[Chunk]) -> str:
    return "\n\n".join(
        f'[{i + 1}] "{b.title}", page {b.page_no}, section: {b.section}\n{b.content}'
        for i, b in enumerate(blocks)
    )


def extract_citations(answer: str, blocks: list[Chunk]) -> list[Citation]:
    """Keep only citations the answer actually references; carry stable chunk_id for
    the citation -> source-highlight round trip in the UI."""
    all_cites = [
        Citation(n=i + 1, doc_id=b.doc_id, page_no=b.page_no, section=b.section,
                 chunk_id=b.chunk_id or "", title=b.title)
        for i, b in enumerate(blocks)
    ]
    used = {int(n) for n in re.findall(r"\[(\d+)\]", answer)}
    return [c for c in all_cites if c.n in used]

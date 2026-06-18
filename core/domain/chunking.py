"""
Structure-aware-lite chunking. Carried over from the prototype unchanged:
a heading starts a new chunk; long sections split at ~target chars; each chunk
records the section heading that INTRODUCED it (so citations label the right
section). Provider-agnostic CORE: it operates on neutral Page/Chunk types.

chunk_seq is document-global and gap-free, which is what neighbor-continuation
relies on. The relational store is the source of truth for chunk text + seq, so
this works even when the retriever owns opaque segment ids.
"""
from __future__ import annotations

import re
import uuid

from core.ports.types import Chunk, Page

HEADING_RE = re.compile(r"^([A-Z0-9][^a-z]{4,80}|\d+(\.\d+)*\s+\S.{2,60})$")


def chunk_pages(pages: list[Page], target: int = 700) -> list[tuple[int, int, str, str]]:
    chunks: list[tuple[int, int, str, str]] = []
    seq, section = 0, "Document"
    for pg in pages:
        text = re.sub(r"[ \t]+", " ", pg.text or "")
        paras, buf = [], []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                if buf:
                    paras.append(" ".join(buf)); buf = []
                continue
            if HEADING_RE.match(line) and len(line.split()) <= 12:
                if buf:
                    paras.append(" ".join(buf)); buf = []
                section = line.title() if line.isupper() else line
                paras.append("## " + section)
            else:
                buf.append(line)
        if buf:
            paras.append(" ".join(buf))
        # split any paragraph far longer than target on word boundaries
        split_paras = []
        for p in paras:
            if p.startswith("## ") or len(p) <= target * 2:
                split_paras.append(p); continue
            cur = ""
            for w in p.split():
                if cur and len(cur) + len(w) + 1 > target:
                    split_paras.append(cur); cur = ""
                cur += (" " if cur else "") + w
            if cur:
                split_paras.append(cur)
        paras = split_paras
        cur, cur_section = "", section
        for p in paras:
            if p.startswith("## "):
                if cur.strip():
                    chunks.append((pg.page_no, seq, cur_section, cur.strip())); seq += 1
                section = p[3:]; cur, cur_section = p + "\n", section
                continue
            if cur.strip() and len(cur) + len(p) > target:
                chunks.append((pg.page_no, seq, cur_section, cur.strip())); seq += 1
                cur = ""; cur_section = section
            cur += p + "\n"
        if cur.strip():
            chunks.append((pg.page_no, seq, cur_section, cur.strip())); seq += 1
    return chunks


def make_chunks(doc_id: str, pages: list[Page], title: str = "") -> list[Chunk]:
    """Materialize Chunk objects with a stable chunk_id (uuid, not an autoincrement int,
    so identity is portable across backends)."""
    return [
        Chunk(doc_id=doc_id, page_no=p, chunk_seq=s, section=sec, content=c,
              chunk_id=uuid.uuid4().hex, title=title)
        for (p, s, sec, c) in chunk_pages(pages)
    ]

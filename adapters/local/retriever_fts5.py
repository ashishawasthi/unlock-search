"""Local Retriever: SQLite FTS5 BM25 + a lexical title boost, ABAC pushed into the SQL.

Ranking behavior (stopword strip, phrase/AND preference, title boost) is adapter-local;
it does NOT survive a backend swap to Agent Search on Gemini Enterprise Agent Platform / OpenSearch, by design.
Scores are normalized 0..1 higher=better so CORE sorts uniformly.
"""
from __future__ import annotations

import re

from core.domain.abac import SQL_ACL
from core.ports.types import Hit

STOPWORDS = {"the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is", "are", "was", "were",
             "be", "been", "being", "what", "which", "who", "whom", "whose", "how", "when", "where",
             "why", "do", "does", "did", "our", "we", "you", "your", "it", "its", "this", "that",
             "these", "those", "with", "as", "at", "by", "from", "about", "into", "over", "per",
             "can", "could", "should", "would", "will", "i", "me", "my", "us", "if", "then", "than",
             "there", "here", "have", "has", "had", "not", "no", "any", "all", "more", "most", "some"}


def fts_query(q: str) -> str:
    raw = re.findall(r"[A-Za-z0-9][A-Za-z0-9.+#&_-]*", (q or "").lower())
    terms = [t for t in raw if t not in STOPWORDS] or raw
    if not terms:
        return '""'
    quoted = [f'"{t}"' for t in terms]
    clauses = []
    if len(terms) > 1:
        clauses.append('"' + " ".join(terms) + '"')
        clauses.append("(" + " AND ".join(quoted) + ")")
    clauses.append(" OR ".join(quoted))
    return " OR ".join(clauses)


class Fts5Retriever:
    def __init__(self, container=None, **kw):
        self.c = container

    def _store(self):
        return self.c.store()

    def index(self, doc_id, chunks, acl_attrs):
        store = self._store()
        store.execute("DELETE FROM chunks_fts WHERE doc_id=?", (doc_id,))
        store.executemany("INSERT INTO chunks_fts(content,chunk_id,doc_id) VALUES(?,?,?)",
                          [(ch.content, ch.chunk_id, doc_id) for ch in chunks])
        return len(chunks)

    def delete_doc(self, doc_id):
        self._store().execute("DELETE FROM chunks_fts WHERE doc_id=?", (doc_id,))

    def _hit(self, r) -> Hit:
        return Hit(doc_id=r["doc_id"], chunk_id=r["chunk_id"], page_no=r["page_no"],
                   chunk_seq=r["chunk_seq"], section=r["section"], content=r["content"],
                   title=r["title"], file_type=r["file_type"], min_clearance=r["min_clearance"],
                   score=float(r["score"]))

    def _normalize(self, hits, query):
        terms = re.findall(r"[a-z0-9]{3,}", (query or "").lower())
        for h in hits:                                   # raw bm25: lower = better
            h.score = h.score - (0.5 if any(t in h.title.lower() for t in terms) else 0.0)
        hits.sort(key=lambda h: h.score)
        n = len(hits) or 1
        for i, h in enumerate(hits):
            h.score = round(1 - i / n, 4)               # normalized: higher = better
        return hits

    def search(self, *, query, pred, doc_ids=None, k=8, filters=None):
        store = self._store()
        where, params = SQL_ACL.to_sql(pred)
        sql = (f"SELECT ch.chunk_id, ch.doc_id, ch.page_no, ch.chunk_seq, ch.section, ch.content, "
               f"d.title, d.file_type, d.min_clearance, bm25(chunks_fts) AS score "
               f"FROM chunks_fts f JOIN chunks ch ON ch.chunk_id=f.chunk_id "
               f"JOIN documents d ON d.id=ch.doc_id "
               f"WHERE chunks_fts MATCH ? AND d.status='published' AND {where}")
        p = [fts_query(query), *params]
        if doc_ids:
            sql += f" AND ch.doc_id IN ({','.join('?' * len(doc_ids))})"
            p += list(doc_ids)
        sql += " ORDER BY score LIMIT ?"
        p.append(k * 4)
        hits = [self._hit(r) for r in store.execute(sql, p)]
        return self._normalize(hits, query)[:k]

    def search_inaccessible(self, *, query, pred, k=12):
        store = self._store()
        where, params = SQL_ACL.to_sql(pred)
        accessible = {r["id"] for r in store.execute(f"SELECT d.id FROM documents d WHERE {where}", params)}
        match = [r["doc_id"] for r in store.execute(
            "SELECT DISTINCT doc_id FROM chunks_fts WHERE chunks_fts MATCH ?", (fts_query(query),))]
        out = []
        for did in match:
            if did in accessible or did in out:
                continue
            out.append(did)
            if len(out) >= k:
                break
        return out

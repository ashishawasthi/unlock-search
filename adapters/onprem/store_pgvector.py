"""
On-prem RelationalStore: PostgreSQL + pgvector (Postgres-wire compatible).

Backing service: a PostgreSQL >= 14 instance with the `vector` extension installed
(pgvector). Holds the canonical chunk TEXT, document metadata, ABAC side-tables, and
the denormalized per-chunk ACL columns. The shared SqlAclCompiler (core.domain.abac.
SQL_ACL) emits '?'-placeholder SQL; this store translates it to psycopg paramstyle at
execute() time so the SAME ABAC rule runs on SQLite, Postgres, and AlloyDB.

Config / env:
  dsn:  libpq DSN, e.g. "postgresql://user:pass@host:5432/db" (or PGVECTOR_DSN /
        ALLOYDB_DSN env). vector dim is 768 (matches the Gemini Enterprise Agent Platform / hosted embedders).

Importable without psycopg installed: the SDK is lazy-imported inside __init__.
"""
from __future__ import annotations

import os
import re
import threading
from contextlib import contextmanager

from core.ports.types import Chunk

VECTOR_DIM = 768

# Postgres-dialect materialization of core/schema/schema.sql (SERIAL ids, vector column,
# tsvector lexical column). The vector index DDL is split out so AlloyDbStore can swap it.
SCHEMA_PG = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS users(
  id TEXT PRIMARY KEY, name TEXT, username TEXT UNIQUE, email TEXT, role TEXT,
  user_type TEXT DEFAULT 'employee', clearance INTEGER DEFAULT 0,
  password_hash TEXT, is_admin INTEGER DEFAULT 0, is_active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS groups(id TEXT PRIMARY KEY, name TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS user_groups(user_id TEXT, group_id TEXT, PRIMARY KEY(user_id, group_id));
CREATE TABLE IF NOT EXISTS user_projects(user_id TEXT, project TEXT, PRIMARY KEY(user_id, project));
CREATE TABLE IF NOT EXISTS folders(id TEXT PRIMARY KEY, name TEXT, dept TEXT, parent_id TEXT);
CREATE TABLE IF NOT EXISTS documents(
  id TEXT PRIMARY KEY, title TEXT, owner_id TEXT, approver_id TEXT, folder_id TEXT,
  file_type TEXT, pages INTEGER, storage_key TEXT, file_hash TEXT, min_clearance INTEGER DEFAULT 2,
  status TEXT DEFAULT 'published', current_version INTEGER DEFAULT 1, created_at DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS doc_grants(doc_id TEXT, group_id TEXT, PRIMARY KEY(doc_id, group_id));
CREATE TABLE IF NOT EXISTS doc_user_types(doc_id TEXT, user_type TEXT, PRIMARY KEY(doc_id, user_type));
CREATE TABLE IF NOT EXISTS doc_projects(doc_id TEXT, project TEXT, PRIMARY KEY(doc_id, project));
CREATE TABLE IF NOT EXISTS doc_tags(doc_id TEXT, tag TEXT, PRIMARY KEY(doc_id, tag));
CREATE TABLE IF NOT EXISTS doc_user_grants(doc_id TEXT, user_id TEXT, expires_at DOUBLE PRECISION,
  PRIMARY KEY(doc_id, user_id));
CREATE TABLE IF NOT EXISTS document_versions(
  id TEXT PRIMARY KEY, doc_id TEXT, version_no INTEGER, storage_key TEXT, file_hash TEXT,
  file_type TEXT, pages INTEGER, change_notes TEXT, uploader_id TEXT, created_at DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS access_requests(
  id TEXT PRIMARY KEY, requester_id TEXT, doc_id TEXT, approver_id TEXT, status TEXT DEFAULT 'pending',
  justification TEXT, approver_note TEXT, token TEXT, token_expires_at DOUBLE PRECISION,
  used INTEGER DEFAULT 0, created_at DOUBLE PRECISION, decided_at DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS chunks(
  chunk_id TEXT PRIMARY KEY, doc_id TEXT, page_no INTEGER, chunk_seq INTEGER, section TEXT, content TEXT,
  owner_id TEXT, min_clearance INTEGER, departments TEXT, user_types TEXT, projects TEXT,
  embedding_present INTEGER DEFAULT 0, embedding vector(%d),
  ts tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(content, ''))) STORED);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id, chunk_seq);
CREATE INDEX IF NOT EXISTS idx_chunks_ts ON chunks USING gin (ts);
CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY, user_id TEXT, doc_scope TEXT,
  created_at DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS messages(id SERIAL PRIMARY KEY, conv_id TEXT, role TEXT,
  content TEXT, cites TEXT, created_at DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS audit(id SERIAL PRIMARY KEY, ts DOUBLE PRECISION, user_id TEXT,
  event TEXT, detail TEXT);
""" % VECTOR_DIM

# pgvector approximate-NN index. AlloyDbStore overrides this with ScaNN.
VECTOR_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_chunks_vec ON chunks "
    "USING hnsw (embedding vector_cosine_ops)"
)


def translate_sql(sql: str) -> str:
    """Translate the shared '?'-placeholder SQL to psycopg paramstyle:
      '?'                   -> '%s'
      'INSERT OR IGNORE INTO' -> 'INSERT INTO ... ON CONFLICT DO NOTHING'
    Literal '%' in the source is escaped first so psycopg does not treat it as a marker."""
    s = sql.replace("%", "%%")
    insert_ignore = re.search(r"INSERT\s+OR\s+IGNORE\s+INTO", s, re.IGNORECASE)
    s = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", s, flags=re.IGNORECASE)
    s = s.replace("?", "%s")
    if insert_ignore and "ON CONFLICT" not in s.upper():
        s = s.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    return s


class PgVectorStore:
    def __init__(self, dsn: str | None = None, **kw):
        self.dsn = dsn or os.environ.get("PGVECTOR_DSN") or os.environ.get("ALLOYDB_DSN")
        if not self.dsn:
            raise RuntimeError("pgvector store needs a dsn (config.relational.dsn or PGVECTOR_DSN env)")
        self._lock = threading.RLock()
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as e:
            raise RuntimeError("psycopg not installed; pip install -r requirements-onprem.txt") from e
        self._dict_row = dict_row
        self._conn = psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row)

    def execute(self, sql: str, params=()):
        with self._lock, self._conn.cursor() as cur:
            cur.execute(translate_sql(sql), tuple(params))
            return [dict(r) for r in cur.fetchall()] if cur.description else []

    def executemany(self, sql: str, rows):
        with self._lock, self._conn.cursor() as cur:
            cur.executemany(translate_sql(sql), [tuple(r) for r in rows])

    @contextmanager
    def begin(self):
        with self._lock:
            self._conn.autocommit = False
            try:
                yield self
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                self._conn.autocommit = True

    def _vector_index_ddl(self) -> str:
        return VECTOR_INDEX_DDL

    def migrate(self):
        with self._lock, self._conn.cursor() as cur:
            cur.execute(SCHEMA_PG)
            cur.execute(self._vector_index_ddl())

    def _to_chunk(self, r: dict) -> Chunk:
        return Chunk(doc_id=r["doc_id"], page_no=r["page_no"], chunk_seq=r["chunk_seq"],
                     section=r["section"], content=r["content"], chunk_id=r["chunk_id"],
                     title=r.get("title", ""))

    def get_chunks(self, chunk_ids):
        if not chunk_ids:
            return []
        ph = ",".join(["?"] * len(chunk_ids))   # '?' -> '%s' via translate_sql (never write %s here)
        rows = self.execute(
            f"SELECT c.*, d.title FROM chunks c JOIN documents d ON d.id=c.doc_id "
            f"WHERE c.chunk_id IN ({ph})", tuple(chunk_ids))
        by_id = {r["chunk_id"]: r for r in rows}
        return [self._to_chunk(by_id[i]) for i in chunk_ids if i in by_id]

    def neighbors(self, doc_id, chunk_seq, radius=1):
        rows = self.execute(
            "SELECT c.*, d.title FROM chunks c JOIN documents d ON d.id=c.doc_id "
            "WHERE c.doc_id=? AND c.chunk_seq BETWEEN ? AND ? ORDER BY c.chunk_seq",
            (doc_id, chunk_seq - radius, chunk_seq + radius))
        return [self._to_chunk(r) for r in rows]

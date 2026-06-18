"""Local RelationalStore: SQLite (FTS5 lives in the retriever). The proto/dev backend."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from core.ports.types import Chunk

SCHEMA_SQLITE = """
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
  status TEXT DEFAULT 'published', current_version INTEGER DEFAULT 1, created_at REAL);
CREATE TABLE IF NOT EXISTS doc_grants(doc_id TEXT, group_id TEXT, PRIMARY KEY(doc_id, group_id));
CREATE TABLE IF NOT EXISTS doc_user_types(doc_id TEXT, user_type TEXT, PRIMARY KEY(doc_id, user_type));
CREATE TABLE IF NOT EXISTS doc_projects(doc_id TEXT, project TEXT, PRIMARY KEY(doc_id, project));
CREATE TABLE IF NOT EXISTS doc_tags(doc_id TEXT, tag TEXT, PRIMARY KEY(doc_id, tag));
CREATE TABLE IF NOT EXISTS doc_user_grants(doc_id TEXT, user_id TEXT, expires_at REAL, PRIMARY KEY(doc_id, user_id));
CREATE TABLE IF NOT EXISTS document_versions(
  id TEXT PRIMARY KEY, doc_id TEXT, version_no INTEGER, storage_key TEXT, file_hash TEXT,
  file_type TEXT, pages INTEGER, change_notes TEXT, uploader_id TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS access_requests(
  id TEXT PRIMARY KEY, requester_id TEXT, doc_id TEXT, approver_id TEXT, status TEXT DEFAULT 'pending',
  justification TEXT, approver_note TEXT, token TEXT, token_expires_at REAL, used INTEGER DEFAULT 0,
  created_at REAL, decided_at REAL);
CREATE TABLE IF NOT EXISTS chunks(
  chunk_id TEXT PRIMARY KEY, doc_id TEXT, page_no INTEGER, chunk_seq INTEGER, section TEXT, content TEXT,
  owner_id TEXT, min_clearance INTEGER, departments TEXT, user_types TEXT, projects TEXT,
  embedding_present INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id, chunk_seq);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(content, chunk_id UNINDEXED, doc_id UNINDEXED);
CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY, user_id TEXT, doc_scope TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT, conv_id TEXT, role TEXT,
  content TEXT, cites TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, user_id TEXT,
  event TEXT, detail TEXT);
"""


class SqliteStore:
    def __init__(self, path: str = "data/aibox.db", **kw):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")

    def execute(self, sql: str, params=()):
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            rows = [dict(r) for r in cur.fetchall()] if cur.description else []
            self._conn.commit()
            return rows

    def executemany(self, sql: str, rows):
        with self._lock:
            self._conn.executemany(sql, [tuple(r) for r in rows])
            self._conn.commit()

    @contextmanager
    def begin(self):
        with self._lock:
            try:
                yield self
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def migrate(self):
        with self._lock:
            self._conn.executescript(SCHEMA_SQLITE)
            self._conn.commit()

    def _to_chunk(self, r: dict) -> Chunk:
        return Chunk(doc_id=r["doc_id"], page_no=r["page_no"], chunk_seq=r["chunk_seq"],
                     section=r["section"], content=r["content"], chunk_id=r["chunk_id"],
                     title=r.get("title", ""))

    def get_chunks(self, chunk_ids):
        if not chunk_ids:
            return []
        ph = ",".join("?" * len(chunk_ids))
        rows = self.execute(
            f"SELECT c.*, d.title FROM chunks c JOIN documents d ON d.id=c.doc_id WHERE c.chunk_id IN ({ph})",
            tuple(chunk_ids))
        by_id = {r["chunk_id"]: r for r in rows}
        return [self._to_chunk(by_id[i]) for i in chunk_ids if i in by_id]

    def neighbors(self, doc_id, chunk_seq, radius=1):
        rows = self.execute(
            "SELECT c.*, d.title FROM chunks c JOIN documents d ON d.id=c.doc_id "
            "WHERE c.doc_id=? AND c.chunk_seq BETWEEN ? AND ? ORDER BY c.chunk_seq",
            (doc_id, chunk_seq - radius, chunk_seq + radius))
        return [self._to_chunk(r) for r in rows]

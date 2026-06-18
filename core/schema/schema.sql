-- Canonical, dialect-neutral reference schema for gcp-unlock.
--
-- This is the SINGLE SOURCE OF TRUTH for the table set and columns. It is NOT run
-- verbatim on every backend: each RelationalStore adapter owns the dialect specifics
-- (SQLite AUTOINCREMENT + FTS5 virtual table; Postgres/AlloyDB SERIAL + tsvector/pgvector
-- index DDL; ScaNN vs HNSW). The adapter's migrate() materializes this set in its dialect.
--
-- ABAC note: chunks carry DENORMALIZED access attributes (min_clearance, departments,
-- user_types, projects, owner_id) so the predicate can be PUSHED DOWN into the retriever
-- index (OpenSearch DLS / Vertex filter), not post-filtered. The relational copy below is
-- the source of truth for chunk TEXT and neighbor continuation.

CREATE TABLE users (
  id TEXT PRIMARY KEY, name TEXT, username TEXT UNIQUE, email TEXT, role TEXT,
  user_type TEXT DEFAULT 'employee', clearance INTEGER DEFAULT 0,
  password_hash TEXT, is_admin INTEGER DEFAULT 0, is_active INTEGER DEFAULT 1);

CREATE TABLE groups (id TEXT PRIMARY KEY, name TEXT UNIQUE);
CREATE TABLE user_groups (user_id TEXT, group_id TEXT, PRIMARY KEY(user_id, group_id));
CREATE TABLE user_projects (user_id TEXT, project TEXT, PRIMARY KEY(user_id, project));
CREATE TABLE folders (id TEXT PRIMARY KEY, name TEXT, dept TEXT, parent_id TEXT);

CREATE TABLE documents (
  id TEXT PRIMARY KEY, title TEXT, owner_id TEXT, approver_id TEXT, folder_id TEXT,
  file_type TEXT, pages INTEGER, storage_key TEXT, file_hash TEXT,
  min_clearance INTEGER DEFAULT 2, status TEXT DEFAULT 'published',
  current_version INTEGER DEFAULT 1, created_at REAL);

CREATE TABLE doc_grants (doc_id TEXT, group_id TEXT, PRIMARY KEY(doc_id, group_id));
CREATE TABLE doc_user_types (doc_id TEXT, user_type TEXT, PRIMARY KEY(doc_id, user_type));
CREATE TABLE doc_projects (doc_id TEXT, project TEXT, PRIMARY KEY(doc_id, project));
CREATE TABLE doc_tags (doc_id TEXT, tag TEXT, PRIMARY KEY(doc_id, tag));
CREATE TABLE doc_user_grants (doc_id TEXT, user_id TEXT, expires_at REAL, PRIMARY KEY(doc_id, user_id));

CREATE TABLE document_versions (
  id TEXT PRIMARY KEY, doc_id TEXT, version_no INTEGER, storage_key TEXT, file_hash TEXT,
  file_type TEXT, pages INTEGER, change_notes TEXT, uploader_id TEXT, created_at REAL);

CREATE TABLE access_requests (
  id TEXT PRIMARY KEY, requester_id TEXT, doc_id TEXT, approver_id TEXT, status TEXT DEFAULT 'pending',
  justification TEXT, approver_note TEXT, token TEXT, token_expires_at REAL, used INTEGER DEFAULT 0,
  created_at REAL, decided_at REAL);

-- Canonical chunk text + stable id + denormalized ACL attrs (source of truth).
CREATE TABLE chunks (
  chunk_id TEXT PRIMARY KEY,          -- stable, fetchable id (uuid); not an autoincrement int
  doc_id TEXT, page_no INTEGER, chunk_seq INTEGER, section TEXT, content TEXT,
  -- denormalized ACL (mirrors documents.* + side-tables) for index pushdown:
  owner_id TEXT, min_clearance INTEGER, departments TEXT, user_types TEXT, projects TEXT,
  embedding_present INTEGER DEFAULT 0);

-- Backends add their own search structure:
--   sqlite:   CREATE VIRTUAL TABLE chunks_fts USING fts5(content, chunk_id UNINDEXED, doc_id UNINDEXED);
--   pgvector: ALTER TABLE chunks ADD COLUMN embedding vector(768);
--             CREATE INDEX ON chunks USING hnsw (embedding vector_cosine_ops);
--             ALTER TABLE chunks ADD COLUMN ts tsvector; (+ GIN index) for BM25-style lexical.
--   alloydb:  same as pgvector but CREATE INDEX ... USING scann (...);
--   vertex:   chunks indexed in a managed Discovery Engine data store; this table still
--             holds canonical text + neighbors keyed by chunk_id.

CREATE TABLE conversations (id TEXT PRIMARY KEY, user_id TEXT, doc_scope TEXT, created_at REAL);
CREATE TABLE messages (id INTEGER PRIMARY KEY, conv_id TEXT, role TEXT, content TEXT, cites TEXT, created_at REAL);
CREATE TABLE audit (id INTEGER PRIMARY KEY, ts REAL, user_id TEXT, event TEXT, detail TEXT);

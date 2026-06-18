"""
GCP RelationalStore: AlloyDB for PostgreSQL (Postgres-wire compatible).

Backing service: an AlloyDB cluster reachable over the Postgres wire protocol
(via the AlloyDB Auth Proxy or a private-IP DSN). AlloyDB is Postgres-compatible,
so this adapter is a thin subclass of the on-prem PgVectorStore: identical schema,
identical shared-ABAC SQL translation, identical canonical chunk text + neighbors.
The ONLY difference is the vector index, which uses AlloyDB's ScaNN index instead
of pgvector HNSW for approximate nearest-neighbor search.

Config / env:
  dsn: libpq DSN to the AlloyDB instance (config.relational.dsn or ALLOYDB_DSN env),
       e.g. "postgresql://user:pass@127.0.0.1:5432/db" through the Auth Proxy.

Importable without psycopg installed (the SDK is lazy-imported in the base ctor).
"""
from __future__ import annotations

from adapters.onprem.store_pgvector import PgVectorStore

# AlloyDB ScaNN index for the embedding column (replaces pgvector HNSW). ScaNN is
# AlloyDB's first-party ANN index; the operator class matches cosine distance.
ALLOYDB_SCANN_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_chunks_vec ON chunks "
    "USING scann (embedding cosine)"
)


class AlloyDbStore(PgVectorStore):
    """Postgres-wire-compatible AlloyDB store. Only the vector index DDL differs."""

    def _vector_index_ddl(self) -> str:
        return ALLOYDB_SCANN_INDEX_DDL

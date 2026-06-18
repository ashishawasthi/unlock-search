"""
Port interfaces (the hexagon's edges). CORE depends ONLY on these Protocols.
No module under core/ ever imports a vendor SDK (google.cloud.*, vertexai, adk,
opensearchpy, minio, presidio, anthropic). Adapters implement these; a profile
binds one adapter per port via core.container.Container.

Refinements folded in from the architecture review:
  - AclCompiler: ABAC is a CORE model (AccessPredicate) compiled per backend.
  - Retriever.get_chunks / RelationalStore.neighbors: the relational store is the
    source of truth for chunk text, so neighbor-continuation and citation->source
    highlight work uniformly even when the retriever owns opaque segment ids.
  - Embedder + Reranker are first-class ports (on-prem needs 3 hosted inference
    endpoints beyond the LLM; GCP folds them into managed Vertex AI Search).
  - Hit.score is normalized 0..1 higher=better (single sort direction).
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Iterator, Protocol, Sequence, runtime_checkable

from .types import (
    AccessPredicate, AgentResult, Chunk, Citation, Deid, DocumentUploaded, Finding,
    GuardContext, Hit, IngestResult, LlmResult, ModelCapabilities, ObjectRef, Page,
    Principal, RagTurn, Verdict,
)


# ---- ABAC compiler (per-backend, driven by a CORE-owned predicate) ----
@runtime_checkable
class AclCompiler(Protocol):
    def to_sql(self, pred: AccessPredicate, table_alias: str = "d") -> tuple[str, list[Any]]:
        """Return a (WHERE-fragment, params) pair for SQL stores (SQLite/Postgres/AlloyDB)."""
        ...

    def to_filter(self, pred: AccessPredicate) -> Any:
        """Return a backend-native filter (Vertex filter-expression, OpenSearch bool/DLS).
        SQL stores may raise NotImplementedError; retrievers implement the one they need."""
        ...


# ---- LLM / embeddings / reranking ----
@runtime_checkable
class LLM(Protocol):
    def generate(self, *, system: str, messages: list[dict],
                 context_blocks: Sequence[Chunk] | None = None,
                 tools: list[dict] | None = None, tool_choice: str | None = None,
                 response_schema: dict | None = None, max_tokens: int = 2048,
                 temperature: float = 0.2, metadata: dict | None = None) -> LlmResult: ...
    def stream(self, **kw: Any) -> Iterator[str]: ...
    def capabilities(self) -> ModelCapabilities: ...


@runtime_checkable
class Embedder(Protocol):
    def embed(self, texts: Sequence[str], *, kind: str = "document") -> list[list[float]]: ...
    def dim(self) -> int: ...


@runtime_checkable
class Reranker(Protocol):
    def rerank(self, query: str, hits: Sequence[Hit], top_k: int) -> list[Hit]: ...


# ---- storage ----
@runtime_checkable
class ObjectStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str,
            metadata: dict | None = None) -> ObjectRef: ...
    def get(self, key: str, version_id: str | None = None) -> bytes: ...
    def signed_url(self, key: str, *, method: str = "GET", version_id: str | None = None,
                   ttl_s: int = 300, content_disposition: str | None = None) -> str | None: ...
    def supports_signed_urls(self) -> bool: ...
    def head(self, key: str, version_id: str | None = None) -> ObjectRef | None: ...
    def list_versions(self, key: str) -> list[ObjectRef]: ...
    def delete(self, key: str, version_id: str | None = None) -> None: ...


@runtime_checkable
class RelationalStore(Protocol):
    """Metadata + ABAC side-tables + canonical chunk text. CORE composes the ABAC
    predicate and passes the compiled SQL fragment in; the store just runs it."""
    def execute(self, sql: str, params: Sequence[Any] = ()) -> list[dict]: ...
    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None: ...
    def begin(self) -> "RelationalStore": ...        # context manager; commit on exit
    def migrate(self) -> None: ...                    # adapter owns dialect-specific DDL
    def get_chunks(self, chunk_ids: Sequence[str]) -> list[Chunk]: ...
    def neighbors(self, doc_id: str, chunk_seq: int, radius: int = 1) -> list[Chunk]: ...


@runtime_checkable
class Retriever(Protocol):
    """Ranking only. Text/neighbors come from RelationalStore so citations and
    continuation are uniform. ABAC is pushed INTO the query, never post-filtered."""
    def index(self, doc_id: str, chunks: Sequence[Chunk], acl_attrs: dict) -> int: ...
    def search(self, *, query: str, pred: AccessPredicate,
               doc_ids: Sequence[str] | None = None, k: int = 8,
               filters: dict | None = None) -> list[Hit]: ...
    def search_inaccessible(self, *, query: str, pred: AccessPredicate,
                            k: int = 12) -> list[str]: ...   # restricted-card doc ids
    def delete_doc(self, doc_id: str) -> None: ...


@runtime_checkable
class DocumentParser(Protocol):
    def supported_types(self) -> set[str]: ...
    def read_pages(self, data: bytes, filename: str) -> list[Page]: ...
    def native_chunks(self, pages: list[Page]) -> list[Chunk] | None: ...   # None -> CORE chunk_pages


# ---- safety ----
@runtime_checkable
class Guardrail(Protocol):
    def check_input(self, text: str, ctx: GuardContext) -> Verdict: ...
    def check_context(self, blocks: Sequence[Chunk], ctx: GuardContext) -> Verdict: ...
    def check_output(self, text: str, ctx: GuardContext) -> Verdict: ...


@runtime_checkable
class DLP(Protocol):
    def inspect(self, text: str, info_types: list[str] | None = None,
                min_likelihood: str = "POSSIBLE") -> list[Finding]: ...
    def deidentify(self, text: str, transforms: dict[str, str] | None = None) -> Deid: ...
    def reidentify(self, text: str, token_map: dict) -> str: ...


# ---- eventing ----
@runtime_checkable
class EventBus(Protocol):
    def publish(self, event: DocumentUploaded) -> None: ...
    def subscribe(self, handler: Callable[[DocumentUploaded], IngestResult]) -> None: ...


# ---- observability / eval ----
@runtime_checkable
class Telemetry(Protocol):
    def log(self, event: str, attrs: dict, severity: str = "INFO") -> None: ...
    def span(self, name: str, attrs: dict) -> Any: ...        # context manager
    def metric(self, name: str, value: float, kind: str = "counter",
               tags: dict | None = None) -> None: ...


@runtime_checkable
class Eval(Protocol):
    def score_turn(self, turn: RagTurn) -> dict[str, float]: ...
    def sample_online(self, turn: RagTurn, rate: float) -> None: ...


# ---- identity / notify ----
@runtime_checkable
class Identity(Protocol):
    def principal_from_request(self, headers: dict) -> Principal | None: ...


@runtime_checkable
class Notifier(Protocol):
    def notify(self, to: str, subject: str, body: str) -> None: ...


# ---- agent runtime (ADK on gcp + onprem; lightweight loop on local) ----
@runtime_checkable
class OrchestratorRuntime(Protocol):
    """Hosts the Orchestrator -> Retriever -> Generator -> Validator graph. The
    agent PROMPTS and the loop contract live in core.agents and are reused by every
    runtime; only the model binding and the host (Vertex Agent Engine / ADK-on-K8s /
    in-process) differ."""
    def run_turn(self, *, principal: Principal, query: str, history: list[dict],
                 doc_ids: Sequence[str] | None) -> AgentResult: ...


__all__ = [
    "AclCompiler", "LLM", "Embedder", "Reranker", "ObjectStore", "RelationalStore",
    "Retriever", "DocumentParser", "Guardrail", "DLP", "EventBus", "Telemetry",
    "Eval", "Identity", "Notifier", "OrchestratorRuntime",
]

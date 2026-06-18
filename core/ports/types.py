"""
Provider-neutral domain types shared across CORE and every adapter.

No vendor type ever crosses the port boundary. These dataclasses are the only
shapes the CORE passes to adapters and receives back. They are deliberately
plain (stdlib dataclasses) so adapters never import a CORE framework either.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


# ---- identity / authorization ----
@dataclass
class Principal:
    """The authenticated caller. CORE does authz (ABAC) from these attributes;
    the gateway/Identity adapter only attests WHO."""
    id: str
    is_admin: bool = False
    clearance: int = 0
    groups: list[str] = field(default_factory=list)      # department/group ids
    user_type: str = "employee"
    projects: list[str] = field(default_factory=list)


@dataclass
class AccessPredicate:
    """CORE-owned parsed ABAC policy for one principal. Built once by the domain
    (core.domain.abac.build_predicate) and handed to a backend AclCompiler that
    turns it into SQL / a filter-DSL / OpenSearch DLS. The MODEL lives in CORE;
    only the COMPILE step is per-backend. This is the security crown jewel."""
    user_id: str
    is_admin: bool
    clearance: int
    groups: list[str]
    user_type: str
    projects: list[str]
    now: float


# ---- documents / chunks / retrieval ----
@dataclass
class Page:
    page_no: int
    text: str
    layout_blocks: list[dict] | None = None   # optional layout-aware parser output
    tables: list[dict] | None = None


@dataclass
class Chunk:
    doc_id: str
    page_no: int
    chunk_seq: int          # document-global, gap-free; drives neighbor continuation
    section: str
    content: str
    chunk_id: str | None = None         # stable, fetchable id (assigned by the store)
    title: str = ""                      # doc title, joined in for citations
    embedding: list[float] | None = None


@dataclass
class Hit:
    """A ranked retrieval result. `score` is normalized 0..1, higher = better, so
    CORE sorts the same way regardless of backend (BM25 vs RRF vs cosine)."""
    doc_id: str
    chunk_id: str
    page_no: int
    chunk_seq: int
    section: str
    content: str
    title: str
    file_type: str
    min_clearance: int
    score: float


@dataclass
class Citation:
    n: int
    doc_id: str
    page_no: int
    section: str
    chunk_id: str
    title: str = ""


# ---- LLM ----
@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


@dataclass
class LlmResult:
    text: str
    stop_reason: Literal["stop", "max_tokens", "tool_use", "content_filter"] = "stop"
    tool_calls: list[dict] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)


@dataclass
class ModelCapabilities:
    max_context_tokens: int = 32768
    supports_tools: bool = False
    supports_parallel_tools: bool = False
    strict_json: bool = False
    supports_streaming: bool = False


# ---- safety / DLP ----
@dataclass
class GuardContext:
    user_id: str
    conv_id: str | None = None
    categories: list[str] | None = None
    policy_id: str | None = None


@dataclass
class Verdict:
    action: Literal["ALLOW", "REDACT", "BLOCK"] = "ALLOW"
    categories: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    redacted_text: str | None = None
    reason: str = ""


@dataclass
class Finding:
    info_type: str
    start: int
    end: int
    quote_redacted: str
    likelihood: str = "POSSIBLE"   # canonical: VERY_UNLIKELY..VERY_LIKELY


@dataclass
class Deid:
    redacted_text: str
    findings: list[Finding] = field(default_factory=list)
    reversible_token_map: dict | None = None


# ---- storage ----
@dataclass
class ObjectRef:
    key: str
    version_id: str | None = None
    etag: str = ""
    size: int = 0


# ---- eventing ----
@dataclass
class DocumentUploaded:
    event_id: str
    occurred_at: str
    object_uri: str
    bucket: str
    key: str
    content_type: str
    size: int
    sha256: str
    owner_id: str
    title: str
    attrs: dict = field(default_factory=dict)
    tenant_id: str = "default"
    attempt: int = 0


@dataclass
class IngestResult:
    doc_id: str
    n_chunks: int
    n_pages: int
    status: str = "published"


# ---- observability / eval ----
@dataclass
class RagTurn:
    query: str
    retrieved: list[Hit]
    context_blocks: list[Chunk]
    answer: str
    citations: list[Citation]
    grounded: bool = True
    tool_calls: list[dict] = field(default_factory=list)
    latency_ms: float = 0.0


@dataclass
class AgentResult:
    """What the Orchestrator (Orchestrator -> Retriever -> Generator -> Validator)
    returns to the chat route. Identical shape on every runtime."""
    answer: str
    cites: list[Citation]
    chunks_used: int
    docs: list[str]
    grounded: bool = True
    trace_id: str | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)

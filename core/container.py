"""
Composition root. The ONE place that knows concrete adapters exist.

Hardened per the architecture review:
  - Static REGISTRY (no importlib of arbitrary YAML strings -> no code-exec-by-config).
    A profile may only pick an adapter KEY that already exists in REGISTRY.
  - Eager build at startup + required-ports validation (fail fast, not mid-request).
  - One Container per process; adapters get only neutral config kwargs.

To add a target: add adapter classes, register their keys here, drop a profiles/<t>.yaml.
No core/ or api/ code changes.
"""
from __future__ import annotations

import importlib
from typing import Any

from infra.settings import load_profile

# port -> { adapter_key -> "module:Class" }. The ALLOWLIST. YAML chooses a key only.
REGISTRY: dict[str, dict[str, str]] = {
    "llm": {
        "anthropic": "adapters.local.llm_anthropic:AnthropicLLM",
        "gemini":    "adapters.gcp.llm_gemini:GeminiLLM",
        "gemma":     "adapters.onprem.llm_gemma:GemmaLLM",
    },
    "embedder": {
        "noop":      "adapters.local.embedder_noop:NoopEmbedder",
        "geap":      "adapters.gcp.embedder_geap:GeapEmbedder",
        "hosted":    "adapters.onprem.embedder_hosted:HostedEmbedder",
    },
    "reranker": {
        "noop":      "adapters.local.reranker_noop:NoopReranker",
        "geap":      "adapters.gcp.reranker_geap:GeapReranker",
        "bge":       "adapters.onprem.reranker_bge:BgeReranker",
    },
    "object_store": {
        "fs":        "adapters.local.objectstore_fs:FsObjectStore",
        "gcs":       "adapters.gcp.objectstore_gcs:GcsObjectStore",
        "minio":     "adapters.onprem.objectstore_minio:MinioObjectStore",
    },
    "relational": {
        "sqlite":    "adapters.local.store_sqlite:SqliteStore",
        "alloydb":   "adapters.gcp.store_alloydb:AlloyDbStore",
        "pgvector":  "adapters.onprem.store_pgvector:PgVectorStore",
    },
    "retriever": {
        "fts5":      "adapters.local.retriever_fts5:Fts5Retriever",
        "agentsearch":"adapters.gcp.retriever_agentsearch:AgentSearchRetriever",
        "opensearch":"adapters.onprem.retriever_opensearch:OpenSearchRetriever",
    },
    "parser": {
        "pypdf":     "adapters.local.parser_pypdf:PypdfParser",
        "docai":     "adapters.gcp.parser_docai:DocAiParser",
        "tika":      "adapters.onprem.parser_tika:TikaParser",
    },
    "guardrail": {
        "noop":      "adapters.local.guardrail_noop:NoopGuardrail",
        "modelarmor":"adapters.gcp.guardrail_modelarmor:ModelArmorGuardrail",
        "llamaguard":"adapters.onprem.guardrail_llamaguard:LlamaGuardNemoGuardrail",
    },
    "dlp": {
        "noop":      "adapters.local.dlp_noop:NoopDLP",
        "clouddlp":  "adapters.gcp.dlp_clouddlp:CloudDLP",
        "presidio":  "adapters.onprem.dlp_presidio:PresidioDLP",
    },
    "event_bus": {
        "inline":    "adapters.local.eventbus_inline:InlineEventBus",
        "eventarc":  "adapters.gcp.eventbus_eventarc:EventarcBus",
        "knative":   "adapters.onprem.eventbus_knative:KnativeBus",
    },
    "telemetry": {
        "stdout":    "adapters.local.telemetry_stdout:StdoutTelemetry",
        "cloudobs":  "adapters.gcp.telemetry_cloudobs:CloudObsTelemetry",
        "otel":      "adapters.onprem.telemetry_otel:OtelTelemetry",
    },
    "identity": {
        "dev":       "adapters.local.identity_dev:DevIdentity",
        "apigee":    "adapters.gcp.identity_apigee:ApigeeIdentity",
        "oidc":      "adapters.onprem.identity_oidc:OidcIdentity",
    },
    "notifier": {
        "outbox":    "adapters.local.notifier_outbox:OutboxNotifier",
        "pubsub":    "adapters.gcp.notifier_pubsub:PubSubNotifier",
        "smtp":      "adapters.onprem.notifier_smtp:SmtpNotifier",
    },
    "orchestrator": {
        "simple":    "adapters.local.orchestrator_simple:SimpleOrchestrator",
        "agentruntime":"adapters.gcp.orchestrator_agentruntime:AgentRuntimeOrchestrator",
        "adk":       "adapters.onprem.orchestrator_adk:AdkOrchestrator",
    },
}

REQUIRED_PORTS = list(REGISTRY.keys())


def _resolve(port: str, key: str):
    spec = REGISTRY.get(port, {}).get(key)
    if not spec:
        raise RuntimeError(f"no adapter registered for port '{port}' key '{key}'")
    module, cls = spec.split(":")
    return getattr(importlib.import_module(module), cls)


class Container:
    """Eagerly builds every port from the active profile at construction; one per process."""

    def __init__(self, profile: str | None = None, lazy: bool = False):
        self.p = load_profile(profile)
        self._cache: dict[str, Any] = {}
        missing = [port for port in REQUIRED_PORTS if port not in self.p["adapters"]]
        if missing:
            raise RuntimeError(f"profile '{self.p['profile']}' missing adapters: {missing}")
        if not lazy:
            for port in REQUIRED_PORTS:
                self._build(port)

    def _build(self, port: str):
        if port not in self._cache:
            cls = _resolve(port, self.p["adapters"][port])
            cfg = self.p.get("config", {}).get(port, {}) or {}
            self._cache[port] = cls(**cfg, container=self) if _wants_container(cls) else cls(**cfg)
        return self._cache[port]

    # typed accessors (return Protocols, never concrete types)
    def llm(self):          return self._build("llm")
    def embedder(self):     return self._build("embedder")
    def reranker(self):     return self._build("reranker")
    def object_store(self): return self._build("object_store")
    def store(self):        return self._build("relational")
    def retriever(self):    return self._build("retriever")
    def parser(self):       return self._build("parser")
    def guardrail(self):    return self._build("guardrail")
    def dlp(self):          return self._build("dlp")
    def event_bus(self):    return self._build("event_bus")
    def telemetry(self):    return self._build("telemetry")
    def identity(self):     return self._build("identity")
    def notifier(self):     return self._build("notifier")
    def orchestrator(self): return self._build("orchestrator")


def _wants_container(cls) -> bool:
    """Adapters that need other ports (retriever needs the store; orchestrator needs
    llm+retriever+guardrail) accept container= in their ctor."""
    try:
        import inspect
        return "container" in inspect.signature(cls.__init__).parameters
    except (TypeError, ValueError):
        return False

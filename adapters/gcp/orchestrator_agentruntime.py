"""
GCP OrchestratorRuntime adapter: Agent Runtime on Gemini Enterprise Agent Platform + ADK.

Backing service: Agent Runtime on Gemini Enterprise Agent Platform hosting an ADK (Agent Development Kit) agent that
runs the SAME graph (Orchestrator -> Retriever -> Generator -> Validator) with the SAME
prompts from core.agents.prompts, on Gemini Flash. The retrieve tool is bound to the CORE
RetrievalPort via the container, so ABAC is enforced server-side (the predicate is built
from the principal and pushed into the retriever) and is never delegated to the model.

If Agent Runtime / ADK is unavailable (SDK not installed, no project, runtime error), this
falls back to the in-process shared loop core.agents.loop.run_rag_turn so the path still
returns a uniform AgentResult. Reuses Gemini via the container LLM (already gemini-flash
in the gcp profile), so the model binding stays consistent across the two execution modes.

Config kwargs: project, location, model. Env fallbacks: GOOGLE_CLOUD_PROJECT,
GOOGLE_CLOUD_LOCATION, AIBOX_MODEL. Lazy imports throughout (importable in CI without ADK).
"""
from __future__ import annotations

import os

from core.agents.loop import run_rag_turn
from core.agents.prompts import ORCHESTRATOR_INSTRUCTION
from core.domain.abac import build_predicate
from core.domain.rag import build_context, extract_citations
from core.domain.retrieval import expand_continuation
from core.ports.types import AgentResult


class AgentRuntimeOrchestrator:
    def __init__(self, container=None, project: str | None = None, location: str | None = None,
                 model: str = "gemini-2.5-flash", **kw):
        self.c = container
        self.project = project or os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT", "")
        self.location = location or os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
        self.model = os.environ.get("AIBOX_MODEL", model)
        self._agent = None

    def _build_agent(self, principal, doc_ids):
        """Construct the ADK agent with a retrieve tool bound to the CORE retriever so ABAC
        holds. Raises if ADK is unavailable; run_turn catches and falls back to the loop."""
        from google.adk.agents import Agent      # lazy import (google-adk)

        c = self.c
        model = self.model

        def retrieve(query: str) -> str:
            """Retrieve numbered, ABAC-filtered excerpts the current user may see."""
            pred = build_predicate(principal)
            hits = c.retriever().search(query=query, pred=pred, doc_ids=doc_ids, k=8)
            hits = c.reranker().rerank(query, hits, top_k=8)
            blocks = expand_continuation(c.store(), hits)
            return build_context(blocks) if blocks else "(no accessible excerpts)"

        return Agent(name="orchestrator", model=model,
                     instruction=ORCHESTRATOR_INSTRUCTION, tools=[retrieve])

    def run_turn(self, *, principal, query, history, doc_ids) -> AgentResult:
        # Try the hosted Agent Runtime / ADK path; on any unavailability, fall back to the
        # shared in-process loop so the contract (AgentResult) is always satisfied.
        try:
            from vertexai import agent_engines       # lazy import (vertexai)
            from vertexai.preview.reasoning_engines import AdkApp
            import vertexai
            vertexai.init(project=self.project or None, location=self.location)
            agent = self._agent or self._build_agent(principal, doc_ids)
            self._agent = agent
            app = AdkApp(agent=agent)
            text = ""
            for ev in app.stream_query(user_id=principal.id, message=query):
                for part in (ev.get("content", {}).get("parts", []) if isinstance(ev, dict) else []):
                    text += part.get("text", "")
            # rebuild blocks for citations from the same ABAC retrieval
            pred = build_predicate(principal)
            hits = self.c.retriever().search(query=query, pred=pred, doc_ids=doc_ids, k=8)
            blocks = expand_continuation(self.c.store(), hits)
            cites = extract_citations(text, blocks)
            return AgentResult(answer=text or "", cites=cites, chunks_used=len(blocks),
                               docs=sorted({b.doc_id for b in blocks}),
                               grounded=bool(cites), trace_id=None)
        except Exception:
            # Agent Runtime/ADK unavailable: same prompts, same retriever, in-process loop.
            return run_rag_turn(self.c, principal, query, history, doc_ids)

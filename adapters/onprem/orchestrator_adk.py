"""
On-prem OrchestratorRuntime: the Orchestrator -> Retriever -> Generator -> Validator
agent graph hosted with Google ADK on K8s, with the model bound via LiteLLM to the
hosted Gemma endpoint (adapters.onprem.llm_gemma). It REUSES the CORE agent prompts
(core.agents.prompts) so behavior matches every other runtime.

Security: the `retrieve` tool calls the CORE RetrievalPort through the container, so ABAC
(the AccessPredicate) is pushed down server-side and is NEVER delegated to the model. The
tool returns numbered, already-filtered excerpts only.

Graceful degradation: google.adk and litellm are LAZY-imported. If ADK is unavailable
(not installed in this image / CI), run_turn() FALLS BACK to the shared in-process loop
core.agents.loop.run_rag_turn, which drives the same prompts, the same ABAC retrieval, the
same Validator gate, and the same guardrails. The answer contract is identical either way.

Backing service + config:
  Reuses the GemmaLLM endpoint config (GEMMA_BASE_URL / GEMMA_MODEL / GEMMA_API_KEY).
  ADK_APP_NAME   ADK app/session name (default 'gcp-unlock')
"""
from __future__ import annotations

import os

from core.agents.loop import run_rag_turn, validate
from core.agents.prompts import ORCHESTRATOR_INSTRUCTION, SYSTEM_PROMPT
from core.domain.abac import build_predicate
from core.domain.rag import build_context, extract_citations
from core.domain.retrieval import expand_continuation
from core.ports.types import AgentResult, GuardContext


class AdkOrchestrator:
    def __init__(self, container=None, app_name: str = "gcp-unlock", **kw):
        self.c = container
        self.app_name = os.environ.get("ADK_APP_NAME", app_name)
        self._runner = None
        self._adk_ok = None        # tri-state: None=unprobed, True/False after first attempt

    def run_turn(self, *, principal, query, history, doc_ids) -> AgentResult:
        if self._build_adk():
            try:
                return self._run_adk(principal, query, history, doc_ids)
            except Exception:
                # Any ADK runtime failure: fall back to the shared loop, never fail the turn.
                pass
        return run_rag_turn(self.c, principal, query, history, doc_ids)

    # ---- ADK graph ----
    def _build_adk(self) -> bool:
        if self._adk_ok is not None:
            return self._adk_ok
        try:
            from google.adk.agents import LlmAgent          # noqa: F401
            from google.adk.models.lite_llm import LiteLlm  # noqa: F401
            import litellm                                   # noqa: F401
        except ImportError:
            self._adk_ok = False
            return False
        self._adk_ok = True
        return True

    def _gemma_litellm(self):
        """Bind the ADK model to the hosted Gemma endpoint via LiteLLM (OpenAI-compatible)."""
        from google.adk.models.lite_llm import LiteLlm
        base = os.environ.get("GEMMA_BASE_URL", "")
        model = os.environ.get("GEMMA_MODEL", "gemma-2-27b-it")
        key = os.environ.get("GEMMA_API_KEY", "") or "sk-noauth"
        return LiteLlm(model=f"openai/{model}", api_base=base, api_key=key)

    def _run_adk(self, principal, query, history, doc_ids) -> AgentResult:
        from google.adk.agents import LlmAgent
        from google.adk.runners import InMemoryRunner
        from google.genai import types as gx

        # ABAC predicate is built ONCE here and closed over by the tool; the model never
        # sees or controls it. The tool returns only filtered, numbered excerpts.
        pred = build_predicate(principal)
        captured: dict = {"blocks": []}

        def retrieve(query: str) -> str:
            """Retrieve excerpts the current user may see (ABAC enforced server-side)."""
            hits = self.c.retriever().search(query=query, pred=pred, doc_ids=doc_ids, k=8)
            hits = self.c.reranker().rerank(query, hits, top_k=8)
            blocks = expand_continuation(self.c.store(), hits)
            captured["blocks"] = blocks
            return build_context(blocks) or "No accessible excerpts."

        model = self._gemma_litellm()
        agent = LlmAgent(
            name="orchestrator", model=model,
            instruction=ORCHESTRATOR_INSTRUCTION + "\n\n" + SYSTEM_PROMPT,
            tools=[retrieve])

        # Input guardrail mirrors the shared loop's contract.
        ctx = GuardContext(user_id=principal.id)
        if self.c.guardrail().check_input(query, ctx).action == "BLOCK":
            return AgentResult(answer="I can't help with that request.", cites=[],
                               chunks_used=0, docs=[], grounded=True)

        runner = InMemoryRunner(agent=agent, app_name=self.app_name)
        session = runner.session_service.create_session(app_name=self.app_name, user_id=principal.id)
        content = gx.Content(role="user", parts=[gx.Part(text=query)])
        text = ""
        for ev in runner.run(user_id=principal.id, session_id=session.id, new_message=content):
            if ev.is_final_response() and ev.content and ev.content.parts:
                text = "".join(p.text or "" for p in ev.content.parts)

        blocks = captured["blocks"]
        if not blocks:
            return AgentResult(
                answer="I couldn't find anything about that in the documents you have access to.",
                cites=[], chunks_used=0, docs=[], grounded=True)

        answer, grounded = validate(text, blocks)
        gout = self.c.guardrail().check_output(answer, ctx)
        if gout.action == "REDACT" and gout.redacted_text is not None:
            answer = gout.redacted_text
        elif gout.action == "BLOCK":
            answer, grounded = "The generated answer was withheld by the safety policy.", False

        cites = extract_citations(answer, blocks)
        return AgentResult(answer=answer, cites=cites, chunks_used=len(blocks),
                           docs=sorted({b.doc_id for b in blocks}), grounded=grounded)

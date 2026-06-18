"""
The shared RAG agent loop: Orchestrator -> Retriever -> Generator -> Validator.

This is the CANONICAL orchestration that the local SimpleOrchestrator runs in-process
and that the ADK-based runtimes (gcp Agent Engine, on-prem ADK-on-K8s) reuse (same
prompts from core.agents.prompts, same retrieval tool, same Validator gate). Only the
model binding and the host differ.

Security: ABAC is applied INSIDE the retriever (predicate pushed down), never delegated
to the model. The retrieve tool the model can call is bound to this same path.
"""
from __future__ import annotations

import re
import time

from core.agents.prompts import SYSTEM_PROMPT
from core.domain.abac import build_predicate
from core.domain.rag import build_context, extract_citations
from core.domain.retrieval import expand_continuation
from core.ports.types import AgentResult, Chunk, GuardContext


def validate(answer: str, blocks: list[Chunk]) -> tuple[str, bool]:
    """Lightweight groundedness gate (default). Strips [n] markers that point past the
    provided excerpts and flags ungrounded answers. ADK runtimes may swap a stronger
    LLM-graded validator; the contract (grounded bool + cleaned answer) is the same."""
    n = len(blocks)
    cited = [int(x) for x in re.findall(r"\[(\d+)\]", answer)]
    answer = re.sub(r"\[(\d+)\]", lambda m: m.group(0) if 1 <= int(m.group(1)) <= n else "", answer)
    grounded = bool(blocks) and any(1 <= ci <= n for ci in cited)
    return answer.strip(), grounded


def run_rag_turn(c, principal, query: str, history: list[dict],
                 doc_ids=None, k: int = 8) -> AgentResult:
    t0 = time.time()
    ctx = GuardContext(user_id=principal.id)
    gin = c.guardrail().check_input(query, ctx)
    if gin.action == "BLOCK":
        return AgentResult(answer="I can't help with that request.", cites=[], chunks_used=0,
                           docs=[], grounded=True)

    pred = build_predicate(principal)
    hits = c.retriever().search(query=query, pred=pred, doc_ids=doc_ids, k=max(k, 8))
    hits = c.reranker().rerank(query, hits, top_k=k)        # no-op reranker returns input
    blocks = expand_continuation(c.store(), hits)

    if not blocks:
        return AgentResult(answer="I couldn't find anything about that in the documents you have access to.",
                           cites=[], chunks_used=0, docs=[], grounded=True)

    gctx = c.guardrail().check_context(blocks, ctx)
    if gctx.action == "BLOCK":
        return AgentResult(answer="I can't answer from these documents.", cites=[], chunks_used=0,
                           docs=[], grounded=True)

    msgs = history[-10:] + [{"role": "user", "content": query}]
    res = c.llm().generate(system=SYSTEM_PROMPT, messages=msgs, context_blocks=blocks, max_tokens=2048)
    answer, grounded = validate(res.text, blocks)

    gout = c.guardrail().check_output(answer, ctx)
    if gout.action == "REDACT" and gout.redacted_text is not None:
        answer = gout.redacted_text
    elif gout.action == "BLOCK":
        answer = "The generated answer was withheld by the safety policy."
        grounded = False

    cites = extract_citations(answer, blocks)
    return AgentResult(answer=answer, cites=cites, chunks_used=len(blocks),
                       docs=sorted({b.doc_id for b in blocks}), grounded=grounded,
                       usage=res.usage)

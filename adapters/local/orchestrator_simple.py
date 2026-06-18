"""Local OrchestratorRuntime: runs the shared CORE agent loop in-process (no ADK
dependency for dev). gcp runs the same graph on Agent Runtime on Gemini Enterprise Agent Platform; on-prem runs
the same ADK graph on K8s. All three reuse core.agents.prompts and the Validator gate."""
from __future__ import annotations

from core.agents.loop import run_rag_turn
from core.ports.types import AgentResult


class SimpleOrchestrator:
    def __init__(self, container=None, **kw):
        self.c = container

    def run_turn(self, *, principal, query, history, doc_ids) -> AgentResult:
        return run_rag_turn(self.c, principal, query, history, doc_ids)

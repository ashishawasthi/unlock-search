"""Local Guardrail: allow-all (dev). Production profiles use Model Armor or Llama Guard + NeMo."""
from __future__ import annotations

from core.ports.types import Verdict


class NoopGuardrail:
    def __init__(self, **kw):
        pass

    def check_input(self, text, ctx) -> Verdict:
        return Verdict(action="ALLOW")

    def check_context(self, blocks, ctx) -> Verdict:
        return Verdict(action="ALLOW")

    def check_output(self, text, ctx) -> Verdict:
        return Verdict(action="ALLOW")

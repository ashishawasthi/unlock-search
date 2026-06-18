"""Local DLP: pass-through (dev). Production profiles use Cloud DLP or Presidio."""
from __future__ import annotations

from core.ports.types import Deid


class NoopDLP:
    def __init__(self, **kw):
        pass

    def inspect(self, text, info_types=None, min_likelihood="POSSIBLE"):
        return []

    def deidentify(self, text, transforms=None) -> Deid:
        return Deid(redacted_text=text, findings=[])

    def reidentify(self, text, token_map):
        return text

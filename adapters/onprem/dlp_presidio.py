"""
On-prem DLP: Microsoft Presidio (presidio-analyzer + presidio-anonymizer), self-hosted.

inspect() -> list[Finding] with canonical likelihood (Presidio score -> the same
VERY_UNLIKELY..VERY_LIKELY bucket the Cloud DLP adapter emits, so CORE thresholds are
backend-uniform). deidentify() -> Deid (anonymized text + the findings).

Backing service + config:
  No network service required; the recognizers (and spaCy model) run in-process. Install
  presidio-analyzer presidio-anonymizer and a spaCy model (en_core_web_lg). If the SDK is
  absent, inspect() returns [] and deidentify() returns the text unchanged (graceful).
Lazy-imports presidio_analyzer / presidio_anonymizer.
"""
from __future__ import annotations

from core.ports.types import Deid, Finding

# Canonical likelihood buckets (match Cloud DLP). Presidio confidence is 0..1.
_LIKELIHOODS = ["VERY_UNLIKELY", "UNLIKELY", "POSSIBLE", "LIKELY", "VERY_LIKELY"]
_ORDER = {name: i for i, name in enumerate(_LIKELIHOODS)}


def _likelihood(score: float) -> str:
    if score >= 0.85:
        return "VERY_LIKELY"
    if score >= 0.6:
        return "LIKELY"
    if score >= 0.4:
        return "POSSIBLE"
    if score >= 0.2:
        return "UNLIKELY"
    return "VERY_UNLIKELY"


class PresidioDLP:
    def __init__(self, language: str = "en", **kw):
        self.language = language
        self._analyzer = None
        self._anonymizer = None

    def _engines(self):
        if self._analyzer is None:
            from presidio_analyzer import AnalyzerEngine
            from presidio_anonymizer import AnonymizerEngine
            self._analyzer = AnalyzerEngine()
            self._anonymizer = AnonymizerEngine()
        return self._analyzer, self._anonymizer

    def inspect(self, text, info_types=None, min_likelihood="POSSIBLE") -> list[Finding]:
        if not text:
            return []
        try:
            analyzer, _ = self._engines()
        except Exception:
            return []
        results = analyzer.analyze(text=text, language=self.language, entities=info_types or None)
        floor = _ORDER.get(min_likelihood, _ORDER["POSSIBLE"])
        out: list[Finding] = []
        for r in results:
            lk = _likelihood(float(r.score))
            if _ORDER[lk] < floor:
                continue
            quote = text[r.start:r.end]
            redacted = quote[:1] + "*" * max(0, len(quote) - 1) if quote else ""
            out.append(Finding(info_type=r.entity_type, start=r.start, end=r.end,
                               quote_redacted=redacted, likelihood=lk))
        return out

    def deidentify(self, text, transforms=None) -> Deid:
        if not text:
            return Deid(redacted_text=text, findings=[])
        try:
            analyzer, anonymizer = self._engines()
        except Exception:
            return Deid(redacted_text=text, findings=[])
        results = analyzer.analyze(text=text, language=self.language)
        anonymized = anonymizer.anonymize(text=text, analyzer_results=results)
        findings = [
            Finding(info_type=r.entity_type, start=r.start, end=r.end,
                    quote_redacted=f"<{r.entity_type}>", likelihood=_likelihood(float(r.score)))
            for r in results
        ]
        return Deid(redacted_text=anonymized.text, findings=findings, reversible_token_map=None)

    def reidentify(self, text, token_map) -> str:
        # Default Presidio replacement is irreversible; re-id needs a reversible
        # operator (e.g. encrypt) configured at deidentify time. Restore from the map if given.
        if not token_map:
            return text
        out = text
        for token, original in token_map.items():
            out = out.replace(token, original)
        return out

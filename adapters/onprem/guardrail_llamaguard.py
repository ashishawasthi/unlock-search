"""
On-prem Guardrail: Llama Guard 4 (safety classifier on a hosted endpoint) +
NeMo Guardrails (Colang rails for topical/jailbreak control) + Presidio (PII REDACT).

Pipeline per check:
  1. NeMo Guardrails rails (if configured) -> may BLOCK on policy/jailbreak.
  2. Llama Guard classification (hosted, OpenAI-compatible) -> BLOCK on unsafe category.
  3. Presidio PII scan -> REDACT (never blocks; returns redacted_text).
Any BLOCK wins over REDACT wins over ALLOW. If no endpoints/SDKs are configured the
adapter degrades gracefully to ALLOW (so dev/CI runs without the safety stack).

Backing service + config:
  LLAMAGUARD_BASE_URL   OpenAI-compatible /v1 endpoint hosting Llama Guard 4
  LLAMAGUARD_MODEL      model id (default 'meta-llama/Llama-Guard-4-12B')
  LLAMAGUARD_API_KEY    optional bearer for the gateway
  NEMO_CONFIG_PATH      directory with a NeMo Guardrails config (config.yml + *.co)
Lazy-imports nemoguardrails, presidio_analyzer/anonymizer, httpx.
"""
from __future__ import annotations

import os

from core.ports.types import Verdict

# Llama Guard maps unsafe content to category codes S1..S14; the classifier returns
# "safe" or "unsafe\n<codes>". We surface the codes as Verdict.categories.
_UNSAFE_PREFIX = "unsafe"


class LlamaGuardNemoGuardrail:
    def __init__(self, base_url: str | None = None, model: str = "meta-llama/Llama-Guard-4-12B",
                 api_key: str | None = None, nemo_config_path: str | None = None,
                 redact_pii: bool = True, **kw):
        self.base_url = (base_url or os.environ.get("LLAMAGUARD_BASE_URL", "")).rstrip("/")
        self.model = os.environ.get("LLAMAGUARD_MODEL", model)
        self.key = api_key or os.environ.get("LLAMAGUARD_API_KEY", "")
        self.nemo_config_path = nemo_config_path or os.environ.get("NEMO_CONFIG_PATH", "")
        self.redact_pii = redact_pii
        self._rails = None          # lazily built NeMo LLMRails
        self._analyzer = None       # lazily built Presidio AnalyzerEngine
        self._anonymizer = None

    # ---- NeMo Guardrails (topical / jailbreak rails) ----
    def _nemo(self):
        if self._rails is not None or not self.nemo_config_path:
            return self._rails
        try:
            from nemoguardrails import LLMRails, RailsConfig
            cfg = RailsConfig.from_path(self.nemo_config_path)
            self._rails = LLMRails(cfg)
        except Exception:
            self._rails = None       # degrade to ALLOW for this layer
        return self._rails

    def _nemo_blocks(self, text: str) -> Verdict | None:
        rails = self._nemo()
        if rails is None:
            return None
        try:
            res = rails.generate(messages=[{"role": "user", "content": text}])
            out = res.get("content", "") if isinstance(res, dict) else str(res)
        except Exception:
            return None
        if "i'm not able to" in out.lower() or "cannot help" in out.lower() or out.strip() == "":
            return Verdict(action="BLOCK", reason="nemo-rail", categories=["policy"])
        return None

    # ---- Llama Guard (hosted safety classifier) ----
    def _llamaguard_blocks(self, text: str, role: str) -> Verdict | None:
        if not self.base_url:
            return None
        try:
            import httpx
            headers = {"content-type": "application/json"}
            if self.key:
                headers["authorization"] = f"Bearer {self.key}"
            payload = {"model": self.model,
                       "messages": [{"role": role, "content": text}],
                       "max_tokens": 64, "temperature": 0.0}
            r = httpx.post(f"{self.base_url}/chat/completions", json=payload,
                           headers=headers, timeout=30)
            if r.status_code != 200:
                return None
            verdict = (r.json()["choices"][0]["message"].get("content") or "").strip().lower()
        except Exception:
            return None
        if verdict.startswith(_UNSAFE_PREFIX):
            cats = [c.strip() for c in verdict.split("\n", 1)[-1].replace(",", " ").split() if c]
            return Verdict(action="BLOCK", reason="llamaguard-unsafe", categories=cats or ["unsafe"])
        return None

    # ---- Presidio (PII redaction) ----
    def _presidio(self):
        if self._analyzer is not None:
            return self._analyzer, self._anonymizer
        try:
            from presidio_analyzer import AnalyzerEngine
            from presidio_anonymizer import AnonymizerEngine
            self._analyzer = AnalyzerEngine()
            self._anonymizer = AnonymizerEngine()
        except Exception:
            self._analyzer = self._anonymizer = None
        return self._analyzer, self._anonymizer

    def _redact_pii(self, text: str) -> Verdict | None:
        if not self.redact_pii or not text:
            return None
        analyzer, anonymizer = self._presidio()
        if analyzer is None:
            return None
        try:
            results = analyzer.analyze(text=text, language="en")
            if not results:
                return None
            redacted = anonymizer.anonymize(text=text, analyzer_results=results).text
        except Exception:
            return None
        cats = sorted({r.entity_type for r in results})
        return Verdict(action="REDACT", redacted_text=redacted, categories=cats, reason="pii")

    def _decide(self, text: str, role: str, do_redact: bool) -> Verdict:
        block = self._nemo_blocks(text) or self._llamaguard_blocks(text, role)
        if block is not None:
            return block
        if do_redact:
            red = self._redact_pii(text)
            if red is not None:
                return red
        return Verdict(action="ALLOW")

    def check_input(self, text: str, ctx) -> Verdict:
        # Classify the user turn; do not redact the user's own prompt.
        return self._decide(text, role="user", do_redact=False)

    def check_context(self, blocks, ctx) -> Verdict:
        joined = "\n\n".join(getattr(b, "content", "") for b in (blocks or []))
        return self._decide(joined, role="user", do_redact=False)

    def check_output(self, text: str, ctx) -> Verdict:
        # Classify + redact the model answer before it reaches the user.
        return self._decide(text, role="assistant", do_redact=True)

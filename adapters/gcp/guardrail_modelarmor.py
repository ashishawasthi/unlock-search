"""
GCP Guardrail adapter: Model Armor sanitize APIs.

Backing service: Gemini Enterprise Agent Platform Model Armor (sanitizeUserPrompt / sanitizeModelResponse).
Screens for prompt injection + jailbreak, Responsible AI categories, and is DLP-aware
(Sensitive Data Protection sanitization filter). Needs a configured Model Armor template.
Config kwargs: project, location, template_id. Env fallbacks: GOOGLE_CLOUD_PROJECT,
MODEL_ARMOR_LOCATION (default 'us-central1'), MODEL_ARMOR_TEMPLATE.

Maps a sanitize result to the neutral Verdict (ALLOW / REDACT / BLOCK). Degrades to
ALLOW (fail-open is intentional only for dev / when the SDK or template is absent; a
production profile should set MODEL_ARMOR_REQUIRED=1 to fail-closed instead).
"""
from __future__ import annotations

import os

from core.ports.types import Verdict


class ModelArmorGuardrail:
    def __init__(self, project: str | None = None, location: str | None = None,
                 template_id: str | None = None, **kw):
        self.project = project or os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT", "")
        self.location = location or os.environ.get("MODEL_ARMOR_LOCATION", "us-central1")
        self.template_id = template_id or os.environ.get("MODEL_ARMOR_TEMPLATE", "")
        self.require = os.environ.get("MODEL_ARMOR_REQUIRED") == "1"
        self._client = None

    def _svc(self):
        # lazy import: importable without google-cloud-modelarmor
        if self._client is None:
            from google.cloud import modelarmor_v1
            opts = {"api_endpoint": f"modelarmor.{self.location}.rep.googleapis.com"}
            self._client = modelarmor_v1.ModelArmorClient(client_options=opts)
        return self._client

    def _template(self) -> str:
        return f"projects/{self.project}/locations/{self.location}/templates/{self.template_id}"

    def _verdict_from(self, result) -> Verdict:
        # result.filter_match_state == MATCH_FOUND => something tripped a filter
        try:
            from google.cloud import modelarmor_v1 as ma
            matched = result.filter_match_state == ma.FilterMatchState.MATCH_FOUND
        except Exception:
            matched = getattr(result, "filter_match_state", 0) == 1
        if not matched:
            return Verdict(action="ALLOW")
        cats = []
        fr = getattr(result, "filter_results", {}) or {}
        try:
            items = fr.items()
        except AttributeError:
            items = []
        for name, _ in items:
            cats.append(str(name))
        return Verdict(action="BLOCK", categories=cats or ["model_armor"],
                       reason="model-armor-filter-match")

    def _sanitize_prompt(self, text: str) -> Verdict:
        if not (self.template_id and self.project):
            return Verdict(action="ALLOW", reason="model-armor-unconfigured")
        try:
            from google.cloud import modelarmor_v1 as ma
            req = ma.SanitizeUserPromptRequest(
                name=self._template(),
                user_prompt_data=ma.DataItem(text=text))
            resp = self._svc().sanitize_user_prompt(request=req)
            return self._verdict_from(resp.sanitization_result)
        except Exception as e:
            if self.require:
                return Verdict(action="BLOCK", reason=f"model-armor-unavailable: {e}")
            return Verdict(action="ALLOW", reason="model-armor-unavailable")

    def _sanitize_response(self, text: str) -> Verdict:
        if not (self.template_id and self.project):
            return Verdict(action="ALLOW", reason="model-armor-unconfigured")
        try:
            from google.cloud import modelarmor_v1 as ma
            req = ma.SanitizeModelResponseRequest(
                name=self._template(),
                model_response_data=ma.DataItem(text=text))
            resp = self._svc().sanitize_model_response(request=req)
            return self._verdict_from(resp.sanitization_result)
        except Exception as e:
            if self.require:
                return Verdict(action="BLOCK", reason=f"model-armor-unavailable: {e}")
            return Verdict(action="ALLOW", reason="model-armor-unavailable")

    def check_input(self, text, ctx) -> Verdict:
        return self._sanitize_prompt(text)

    def check_context(self, blocks, ctx) -> Verdict:
        # retrieved excerpts are screened as a response payload (RAI + DLP-aware)
        joined = "\n\n".join(getattr(b, "content", "") for b in (blocks or []))
        if not joined.strip():
            return Verdict(action="ALLOW")
        return self._sanitize_response(joined)

    def check_output(self, text, ctx) -> Verdict:
        return self._sanitize_response(text)

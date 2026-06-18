"""
GCP DLP adapter: Cloud DLP (Sensitive Data Protection) inspect + deidentify.

Backing service: google-cloud-dlp (DlpServiceClient). Needs a GCP project with the DLP
API enabled and ADC credentials. Config kwargs: project, info_types (default list).
Env fallbacks: GOOGLE_CLOUD_PROJECT / GCP_PROJECT.

Maps DLP findings to the neutral Finding (info_type, char offsets, redacted quote,
likelihood string VERY_UNLIKELY..VERY_LIKELY). deidentify uses ReplaceWithInfoTypeConfig
(or a per-info-type replacement from transforms) so the redacted text is still readable.
"""
from __future__ import annotations

import os

from core.ports.types import Deid, Finding

DEFAULT_INFO_TYPES = ["PERSON_NAME", "EMAIL_ADDRESS", "PHONE_NUMBER", "CREDIT_CARD_NUMBER",
                      "US_SOCIAL_SECURITY_NUMBER", "IP_ADDRESS", "STREET_ADDRESS"]


class CloudDLP:
    def __init__(self, project: str | None = None, info_types: list[str] | None = None, **kw):
        self.project = project or os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT", "")
        self.info_types = info_types or DEFAULT_INFO_TYPES
        self._client = None

    def _svc(self):
        # lazy import: importable without google-cloud-dlp
        if self._client is None:
            from google.cloud import dlp_v2
            self._client = dlp_v2.DlpServiceClient()
        return self._client

    def _parent(self) -> str:
        return f"projects/{self.project}"

    def _info_type_cfg(self, info_types):
        return [{"name": t} for t in (info_types or self.info_types)]

    def inspect(self, text, info_types=None, min_likelihood="POSSIBLE"):
        if not (text and self.project):
            return []
        from google.cloud import dlp_v2
        cfg = {"info_types": self._info_type_cfg(info_types),
               "min_likelihood": min_likelihood, "include_quote": True}
        try:
            resp = self._svc().inspect_content(request={
                "parent": self._parent(), "inspect_config": cfg,
                "item": {"value": text}})
        except Exception as e:
            raise RuntimeError(f"dlp-inspect-error: {e}")
        out = []
        for f in resp.result.findings:
            loc = f.location.codepoint_range
            quote = getattr(f, "quote", "") or ""
            out.append(Finding(
                info_type=f.info_type.name,
                start=int(getattr(loc, "start", 0)),
                end=int(getattr(loc, "end", 0)),
                quote_redacted=("*" * len(quote)) if quote else "[redacted]",
                likelihood=dlp_v2.Likelihood(f.likelihood).name))
        return out

    def deidentify(self, text, transforms=None) -> Deid:
        if not (text and self.project):
            return Deid(redacted_text=text, findings=[])
        # one transformation per requested info type; default replaces with [INFO_TYPE]
        info_transforms = []
        for t in self.info_types:
            repl = (transforms or {}).get(t)
            if repl is not None:
                primitive = {"replace_config": {"new_value": {"string_value": repl}}}
            else:
                primitive = {"replace_with_info_type_config": {}}
            info_transforms.append({"info_types": [{"name": t}], "primitive_transformation": primitive})
        deid_cfg = {"info_type_transformations": {"transformations": info_transforms}}
        inspect_cfg = {"info_types": self._info_type_cfg(None)}
        try:
            resp = self._svc().deidentify_content(request={
                "parent": self._parent(), "deidentify_config": deid_cfg,
                "inspect_config": inspect_cfg, "item": {"value": text}})
        except Exception as e:
            raise RuntimeError(f"dlp-deidentify-error: {e}")
        return Deid(redacted_text=resp.item.value, findings=self.inspect(text))

    def reidentify(self, text, token_map):
        # Cloud DLP reidentify requires the original reversible (CryptoReplaceFfx) config and
        # key; this adapter uses irreversible replacement above, so re-id is not supported.
        raise RuntimeError("clouddlp-reidentify-unsupported: deidentify uses irreversible replacement")

"""
GCP Telemetry adapter: Cloud Observability.

Backing service: Cloud Logging (google-cloud-logging) for structured logs, and Cloud
Trace via the OpenTelemetry exporter (opentelemetry-exporter-gcp-trace) for spans.
Metrics are emitted as structured log entries (a Cloud Monitoring log-based metric or
the OTel metric exporter can pick them up). Needs a GCP project + ADC; the Logging/Trace
APIs enabled. Config kwargs: project, service. Env fallbacks: GOOGLE_CLOUD_PROJECT,
K_SERVICE / OTEL_SERVICE_NAME.

Degrades gracefully: if the SDKs are absent the logger falls back to stderr JSON and
span() yields a no-op, so the path never breaks in dev/CI.
"""
from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager


class CloudObsTelemetry:
    def __init__(self, project: str | None = None, service: str | None = None, **kw):
        self.project = project or os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT", "")
        self.service = service or os.environ.get("K_SERVICE") or os.environ.get("OTEL_SERVICE_NAME", "gcp-unlock")
        self._logger = None
        self._tracer = None
        self._init_logger()
        self._init_tracer()

    def _init_logger(self):
        # lazy import: importable without google-cloud-logging
        try:
            import google.cloud.logging as gcl
            client = gcl.Client(project=self.project or None)
            self._logger = client.logger(self.service)
        except Exception:
            self._logger = None

    def _init_tracer(self):
        try:
            from opentelemetry import trace
            from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            provider = TracerProvider()
            provider.add_span_processor(BatchSpanProcessor(
                CloudTraceSpanExporter(project_id=self.project or None)))
            trace.set_tracer_provider(provider)
            self._tracer = trace.get_tracer(self.service)
        except Exception:
            self._tracer = None

    def log(self, event, attrs, severity="INFO"):
        payload = {"event": event, **(attrs or {})}
        if self._logger is not None:
            try:
                self._logger.log_struct(payload, severity=severity)
                return
            except Exception:
                pass
        print(json.dumps({"sev": severity, **payload}, default=str), file=sys.stderr)

    @contextmanager
    def span(self, name, attrs):
        if self._tracer is None:
            yield self
            return
        with self._tracer.start_as_current_span(name) as sp:
            for k, v in (attrs or {}).items():
                try:
                    sp.set_attribute(k, v)
                except Exception:
                    pass
            yield sp

    def metric(self, name, value, kind="counter", tags=None):
        # Emit as a structured log entry; a log-based metric / OTel meter consumes it.
        self.log("metric", {"metric": name, "value": value, "kind": kind, **(tags or {})}, severity="INFO")

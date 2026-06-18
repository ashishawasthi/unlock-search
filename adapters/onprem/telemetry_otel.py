"""
On-prem Telemetry: OpenTelemetry SDK exporting logs/spans/metrics over OTLP to a
Collector (-> Prometheus / Grafana / Loki / Tempo). No-op if the SDK is absent so the
app still runs in dev/CI without the observability stack.

Backing service + config:
  OTEL_EXPORTER_OTLP_ENDPOINT   OTLP gRPC/HTTP collector endpoint (e.g. http://otel-collector:4317)
  OTEL_SERVICE_NAME             service name attribute (default 'gcp-unlock')
Lazy-imports opentelemetry-* (api/sdk + OTLP exporters). All exporter wiring happens in
__init__; method calls degrade to no-ops if init failed.
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager

# Canonical severity -> OTel-ish level number (no SDK needed for the mapping).
_SEV = {"DEBUG": 5, "INFO": 9, "WARNING": 13, "ERROR": 17, "CRITICAL": 21}


class OtelTelemetry:
    def __init__(self, endpoint: str | None = None, service_name: str = "gcp-unlock", **kw):
        self.endpoint = endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
        self.service_name = os.environ.get("OTEL_SERVICE_NAME", service_name)
        self._tracer = None
        self._meter = None
        self._counters: dict[str, object] = {}
        self._ok = self._init_sdk()

    def _init_sdk(self) -> bool:
        try:
            from opentelemetry import metrics, trace
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
        except ImportError:
            return False
        try:
            res = Resource.create({"service.name": self.service_name})
            kw = {"endpoint": self.endpoint} if self.endpoint else {}
            tp = TracerProvider(resource=res)
            tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(**kw)))
            trace.set_tracer_provider(tp)
            reader = PeriodicExportingMetricReader(OTLPMetricExporter(**kw))
            metrics.set_meter_provider(MeterProvider(resource=res, metric_readers=[reader]))
            self._tracer = trace.get_tracer(self.service_name)
            self._meter = metrics.get_meter(self.service_name)
            return True
        except Exception:
            return False

    def log(self, event, attrs, severity="INFO"):
        if self._ok and self._tracer is not None:
            try:
                from opentelemetry import trace
                span = trace.get_current_span()
                span.add_event(event, attributes={k: str(v) for k, v in (attrs or {}).items()})
                return
            except Exception:
                pass
        # No SDK / no active span: structured stderr line so logs are never lost.
        print(f"[{severity}] {event} {dict(attrs or {})}", file=sys.stderr)

    @contextmanager
    def span(self, name, attrs):
        if self._ok and self._tracer is not None:
            try:
                with self._tracer.start_as_current_span(name) as sp:
                    for k, v in (attrs or {}).items():
                        sp.set_attribute(k, str(v))
                    yield sp
                    return
            except Exception:
                pass
        yield self

    def metric(self, name, value, kind="counter", tags=None):
        if not (self._ok and self._meter is not None):
            return
        try:
            if kind == "counter":
                c = self._counters.get(name)
                if c is None:
                    c = self._meter.create_counter(name)
                    self._counters[name] = c
                c.add(value, attributes={k: str(v) for k, v in (tags or {}).items()})
            else:
                h = self._counters.get(name)
                if h is None:
                    h = self._meter.create_histogram(name)
                    self._counters[name] = h
                h.record(value, attributes={k: str(v) for k, v in (tags or {}).items()})
        except Exception:
            pass

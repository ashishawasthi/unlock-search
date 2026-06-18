"""Local Telemetry: structured stdout + no-op spans/metrics. The append-only audit
log is still written by the store; this mirrors events for dev visibility.
gcp uses Cloud Observability + Vertex Eval; on-prem uses OTel + Prometheus + Grafana."""
from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager


class StdoutTelemetry:
    def __init__(self, quiet: bool = False, **kw):
        self.quiet = quiet or os.environ.get("AIBOX_QUIET_TELEMETRY") == "1"

    def log(self, event, attrs, severity="INFO"):
        if not self.quiet:
            print(json.dumps({"sev": severity, "event": event, **(attrs or {})}, default=str), file=sys.stderr)

    @contextmanager
    def span(self, name, attrs):
        yield self

    def metric(self, name, value, kind="counter", tags=None):
        pass

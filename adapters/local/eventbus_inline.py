"""Local EventBus: synchronous in-process dispatch (matches the proto's inline ingest).
gcp uses Eventarc + Cloud Functions; on-prem uses Knative/KEDA/Argo Events."""
from __future__ import annotations


class InlineEventBus:
    def __init__(self, **kw):
        self._handler = None

    def publish(self, event):
        if self._handler is not None:
            return self._handler(event)
        return None

    def subscribe(self, handler):
        self._handler = handler

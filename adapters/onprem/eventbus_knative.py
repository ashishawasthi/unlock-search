"""
On-prem EventBus: Knative Eventing (Broker). publish() POSTs a CloudEvent to the
broker URL; the broker fans out to a Trigger that delivers to the ingest worker, which
calls the registered subscribe() handler with a DocumentUploaded.

Backing service + config:
  KNATIVE_BROKER_URL   the Knative Broker ingress (e.g.
                       http://broker-ingress.knative-eventing.svc/<ns>/default)
publish() maps a DocumentUploaded -> a CloudEvent (binary content mode, ce-* headers).
The K8s worker's HTTP handler parses an inbound S3/MinIO or CloudEvent payload back to a
DocumentUploaded via on_request() and invokes the subscribed handler. Lazy-imports httpx.
"""
from __future__ import annotations

import json
import os
import time
import uuid

from core.ports.types import DocumentUploaded, IngestResult

_CE_TYPE = "com.gcpunlock.document.uploaded.v1"


class KnativeBus:
    def __init__(self, broker_url: str | None = None, source: str = "gcp-unlock/api", **kw):
        self.broker_url = (broker_url or os.environ.get("KNATIVE_BROKER_URL", "")).rstrip("/")
        self.source = source
        self._handler = None

    def publish(self, event: DocumentUploaded) -> None:
        if not self.broker_url:
            raise RuntimeError("knative-broker-unset: set KNATIVE_BROKER_URL")
        import httpx
        data = {
            "event_id": event.event_id, "occurred_at": event.occurred_at,
            "object_uri": event.object_uri, "bucket": event.bucket, "key": event.key,
            "content_type": event.content_type, "size": event.size, "sha256": event.sha256,
            "owner_id": event.owner_id, "title": event.title, "attrs": event.attrs,
            "tenant_id": event.tenant_id, "attempt": event.attempt,
        }
        headers = {
            "content-type": "application/json",
            "ce-specversion": "1.0",
            "ce-id": event.event_id or str(uuid.uuid4()),
            "ce-source": self.source,
            "ce-type": _CE_TYPE,
            "ce-subject": f"{event.bucket}/{event.key}",
            "ce-time": event.occurred_at,
        }
        try:
            r = httpx.post(self.broker_url, headers=headers, content=json.dumps(data), timeout=15)
        except httpx.HTTPError as e:
            raise RuntimeError(f"eventbus-unavailable: {e}")
        if r.status_code >= 300:
            raise RuntimeError(f"eventbus-error-{r.status_code}")

    def subscribe(self, handler) -> None:
        # The K8s worker registers its ingest handler here; on_request() drives it.
        self._handler = handler

    def on_request(self, headers: dict, body: bytes) -> IngestResult | None:
        """Worker entrypoint: parse an inbound CloudEvent (our own type) or a raw
        S3/MinIO notification into a DocumentUploaded and dispatch to the handler."""
        if self._handler is None:
            return None
        event = self._to_event(headers or {}, body or b"")
        return self._handler(event)

    @staticmethod
    def _to_event(headers: dict, body: bytes) -> DocumentUploaded:
        try:
            payload = json.loads(body.decode() or "{}")
        except (ValueError, UnicodeDecodeError):
            payload = {}
        ce_type = (headers.get("ce-type") or "").lower()
        if ce_type == _CE_TYPE or "object_uri" in payload:
            # Our own CloudEvent envelope (DocumentUploaded fields verbatim).
            return DocumentUploaded(
                event_id=payload.get("event_id") or headers.get("ce-id", str(uuid.uuid4())),
                occurred_at=payload.get("occurred_at") or headers.get("ce-time", ""),
                object_uri=payload.get("object_uri", ""), bucket=payload.get("bucket", ""),
                key=payload.get("key", ""), content_type=payload.get("content_type", ""),
                size=int(payload.get("size", 0)), sha256=payload.get("sha256", ""),
                owner_id=payload.get("owner_id", ""), title=payload.get("title", ""),
                attrs=payload.get("attrs", {}) or {},
                tenant_id=payload.get("tenant_id", "default"),
                attempt=int(payload.get("attempt", 0)))
        # Fall back to an S3/MinIO bucket-notification shape.
        rec = (payload.get("Records") or [{}])[0]
        s3 = rec.get("s3", {})
        bucket = s3.get("bucket", {}).get("name", "")
        key = s3.get("object", {}).get("key", "")
        return DocumentUploaded(
            event_id=headers.get("ce-id") or str(uuid.uuid4()),
            occurred_at=rec.get("eventTime") or headers.get("ce-time") or
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            object_uri=f"s3://{bucket}/{key}" if bucket else "",
            bucket=bucket, key=key,
            content_type=s3.get("object", {}).get("contentType", "application/octet-stream"),
            size=int(s3.get("object", {}).get("size", 0)),
            sha256=s3.get("object", {}).get("eTag", ""),
            owner_id="", title=key.rsplit("/", 1)[-1] if key else "")

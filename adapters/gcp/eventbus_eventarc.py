"""
GCP EventBus adapter: Eventarc + Cloud Functions.

Backing service: a GCS bucket whose object-finalize events route through Eventarc to a
deployed Cloud Function (2nd gen). The function's HTTP/CloudEvent entrypoint calls the
registered handler with a DocumentUploaded built from the GCS finalize CloudEvent.

So in this profile:
  publish()   -> a no-op / audit only. The real trigger is the GCS object-finalize event;
                 we do NOT re-emit it. (Code that calls publish keeps working uniformly.)
  subscribe() -> registers the ingest handler the deployed function entrypoint invokes.
  handle_cloudevent() -> the function entrypoint adapter: maps a CloudEvent GCS finalize
                 payload to DocumentUploaded and dispatches it to the handler.

Config kwargs: bucket (optional, for audit context). No SDK required at import time;
the CloudEvents SDK is only imported when parsing a live event.
"""
from __future__ import annotations

import os

from core.ports.types import DocumentUploaded


class EventarcBus:
    def __init__(self, bucket: str | None = None, **kw):
        self.bucket = bucket or os.environ.get("GCS_INGEST_BUCKET", "")
        self._handler = None

    def publish(self, event) -> None:
        # No-op: GCS object-finalize via Eventarc is the source of truth, not an app publish.
        # We intentionally do not double-emit; this keeps callers profile-agnostic.
        return None

    def subscribe(self, handler) -> None:
        # The deployed Cloud Function entrypoint calls handle_cloudevent(), which calls this.
        self._handler = handler

    def _to_event(self, data: dict, attrs: dict | None = None):
        bucket = data.get("bucket", self.bucket)
        name = data.get("name", "")
        return DocumentUploaded(
            event_id=(attrs or {}).get("id", data.get("generation", "")),
            occurred_at=data.get("timeCreated", data.get("updated", "")),
            object_uri=f"gs://{bucket}/{name}",
            bucket=bucket, key=name,
            content_type=data.get("contentType", "application/octet-stream"),
            size=int(data.get("size", 0) or 0),
            sha256=data.get("md5Hash", ""),       # GCS gives md5/crc32c; ingest recomputes sha256
            owner_id=(data.get("metadata") or {}).get("owner_id", ""),
            title=(data.get("metadata") or {}).get("title", name.rsplit("/", 1)[-1]),
            attrs=data.get("metadata") or {})

    def handle_cloudevent(self, cloud_event):
        """Cloud Function (2nd gen) entrypoint adapter. Accepts a CloudEvent (object with
        .data and attributes) or a raw dict, maps GCS finalize -> DocumentUploaded, and
        dispatches to the registered handler. Returns the handler's IngestResult."""
        if self._handler is None:
            raise RuntimeError("eventarc: no ingest handler subscribed")
        data = getattr(cloud_event, "data", None)
        attrs = None
        if data is None and isinstance(cloud_event, dict):
            data = cloud_event.get("data", cloud_event)
            attrs = {"id": cloud_event.get("id", "")}
        elif data is None:
            data = {}
        if not isinstance(data, dict):
            # CloudEvents SDK may hand bytes; decode lazily
            import json
            try:
                data = json.loads(data)
            except Exception:
                data = {}
        if attrs is None:
            attrs = {"id": getattr(cloud_event, "id", "") or
                     (cloud_event.get("id", "") if isinstance(cloud_event, dict) else "")}
        return self._handler(self._to_event(data, attrs))

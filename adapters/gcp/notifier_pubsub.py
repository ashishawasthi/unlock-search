"""
GCP Notifier adapter: Cloud Pub/Sub.

Backing service: a Pub/Sub topic whose subscriber fans out to an email service (e.g. a
Cloud Function calling SendGrid). This adapter only PUBLISHES a notification message;
delivery is the subscriber's job. Needs a GCP project + ADC and the Pub/Sub API enabled.
Config kwargs: project, topic. Env fallbacks: GOOGLE_CLOUD_PROJECT / GCP_PROJECT,
PUBSUB_NOTIFY_TOPIC.
"""
from __future__ import annotations

import json
import os


class PubSubNotifier:
    def __init__(self, project: str | None = None, topic: str | None = None, **kw):
        self.project = project or os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT", "")
        self.topic = topic or os.environ.get("PUBSUB_NOTIFY_TOPIC", "")
        self._publisher = None
        self._topic_path = None

    def _pub(self):
        # lazy import: importable without google-cloud-pubsub
        if self._publisher is None:
            from google.cloud import pubsub_v1
            self._publisher = pubsub_v1.PublisherClient()
            self._topic_path = self._publisher.topic_path(self.project, self.topic)
        return self._publisher

    def notify(self, to: str, subject: str, body: str) -> None:
        if not (self.project and self.topic):
            raise RuntimeError("pubsub-notifier-unconfigured: set project and PUBSUB_NOTIFY_TOPIC")
        payload = json.dumps({"to": to, "subject": subject, "body": body}).encode("utf-8")
        try:
            future = self._pub().publish(self._topic_path, payload,
                                         to=to, subject=subject)
            future.result(timeout=30)        # block so failures surface to the caller
        except Exception as e:
            raise RuntimeError(f"pubsub-publish-error: {e}")

"""
GCP ObjectStore: Google Cloud Storage (google-cloud-storage).

Backing service: a GCS bucket. Object versioning SHOULD be enabled on the bucket so
generations map to ObjectRef.version_id (list_versions / get(version_id=...)). Signed
URLs are V4, with response-content-disposition so the browser downloads with the right
filename. supports_signed_urls() -> True so the gateway can hand the client a URL
instead of proxying bytes.

CMEK note: server-side encryption is configured on the BUCKET (a Cloud KMS key set as
the bucket default), not per-call here, so all objects are CMEK-encrypted transparently;
this adapter does not pass a per-object key. Signing V4 URLs needs a service-account
signer (a SA key, or IAM SignBlob via the default credentials' signer).

Config / env (config.object_store in profiles/gcp.yaml):
  bucket (required), project (optional)

Importable without google-cloud-storage installed (lazy SDK imports).
"""
from __future__ import annotations

from datetime import timedelta

from core.ports.types import ObjectRef


class GcsObjectStore:
    def __init__(self, bucket: str | None = None, project: str | None = None, **kw):
        if not bucket:
            raise RuntimeError("gcs object store needs a bucket (config.object_store.bucket)")
        self.bucket_name = bucket
        self.project = project
        self._client = None

    def _bucket(self):
        if self._client is None:
            from google.cloud import storage
            self._storage = storage
            self._client = storage.Client(project=self.project) if self.project else storage.Client()
        return self._client.bucket(self.bucket_name)

    def put(self, key, data, content_type, metadata=None) -> ObjectRef:
        blob = self._bucket().blob(key)
        if metadata:
            blob.metadata = {k: str(v) for k, v in metadata.items()}
        blob.upload_from_string(data, content_type=content_type)
        return ObjectRef(key=key, version_id=str(blob.generation) if blob.generation else None,
                         etag=blob.etag or "", size=blob.size or len(data))

    def get(self, key, version_id=None):
        gen = int(version_id) if version_id else None
        blob = self._bucket().blob(key, generation=gen)
        return blob.download_as_bytes()

    def signed_url(self, key, *, method="GET", version_id=None, ttl_s=300,
                   content_disposition=None):
        gen = int(version_id) if version_id else None
        blob = self._bucket().blob(key, generation=gen)
        params = {"version": "v4", "expiration": timedelta(seconds=ttl_s), "method": method}
        if version_id:
            params["generation"] = gen
        if content_disposition:
            params["response_disposition"] = content_disposition
        try:
            return blob.generate_signed_url(**params)
        except Exception as e:
            # V4 signing needs a credential that can sign (SA key or IAM SignBlob).
            raise RuntimeError(f"gcs signed-url-unavailable: {e}") from e

    def supports_signed_urls(self) -> bool:
        return True

    def head(self, key, version_id=None):
        gen = int(version_id) if version_id else None
        blob = self._bucket().get_blob(key, generation=gen)
        if blob is None:
            return None
        return ObjectRef(key=key, version_id=str(blob.generation) if blob.generation else None,
                         etag=blob.etag or "", size=blob.size or 0)

    def list_versions(self, key):
        client = self._bucket().client
        out = []
        for blob in client.list_blobs(self.bucket_name, prefix=key, versions=True):
            if blob.name != key:
                continue
            out.append(ObjectRef(key=key, version_id=str(blob.generation), etag=blob.etag or "",
                                 size=blob.size or 0))
        out.sort(key=lambda r: int(r.version_id or 0), reverse=True)
        return out

    def delete(self, key, version_id=None):
        gen = int(version_id) if version_id else None
        self._bucket().blob(key, generation=gen).delete()

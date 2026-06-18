"""
On-prem ObjectStore: S3-compatible storage (MinIO / Ceph RGW / any S3 endpoint) via boto3.

Backing service: a MinIO or S3-compatible endpoint. Supports presigned GET/PUT URLs (so the
browser can fetch/upload bytes directly without proxying through the app) and best-effort
object versioning (enabled on the bucket at startup; if the backend refuses, puts still work
and version_id is simply None).

Config (profiles/onprem.yaml):
  endpoint_url_env: MINIO_ENDPOINT       -> endpoint_url: https://minio:9000 (required)
  access_key_env / secret_key_env        -> MINIO_ACCESS_KEY / MINIO_SECRET_KEY (required)
  bucket: aibox                          -> bucket name (default aibox)
  region: us-east-1                      -> region (default us-east-1)
  secure: true                           -> retained for parity; TLS is implied by endpoint scheme
"""
from __future__ import annotations

from core.ports.types import ObjectRef


class MinioObjectStore:
    def __init__(self, endpoint_url: str = "", access_key: str = "", secret_key: str = "",
                 bucket: str = "aibox", region: str = "us-east-1", secure: bool = True, **kw):
        if not endpoint_url or not access_key or not secret_key:
            raise RuntimeError(
                "MinioObjectStore needs endpoint_url + access_key + secret_key "
                "(set MINIO_ENDPOINT / MINIO_ACCESS_KEY / MINIO_SECRET_KEY)")
        self.endpoint_url = endpoint_url
        self.access_key = access_key
        self.secret_key = secret_key
        self.bucket = bucket
        self.region = region
        self._s3 = None

    # ---- client (lazy import of boto3) ----
    def _client(self):
        if self._s3 is None:
            import boto3
            from botocore.config import Config
            self._s3 = boto3.client(
                "s3", endpoint_url=self.endpoint_url, aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key, region_name=self.region,
                config=Config(signature_version="s3v4"))
            self._ensure_bucket()
        return self._s3

    def _ensure_bucket(self):
        from botocore.exceptions import ClientError
        try:
            self._s3.head_bucket(Bucket=self.bucket)
        except ClientError:
            try:
                self._s3.create_bucket(Bucket=self.bucket)
            except ClientError:
                pass
        # best-effort versioning so list_versions / version_id are meaningful.
        try:
            self._s3.put_bucket_versioning(
                Bucket=self.bucket, VersioningConfiguration={"Status": "Enabled"})
        except ClientError:
            pass

    # ---- ObjectStore port ----
    def put(self, key: str, data: bytes, content_type: str,
            metadata: dict | None = None) -> ObjectRef:
        s3 = self._client()
        extra = {"ContentType": content_type}
        if metadata:
            extra["Metadata"] = {str(k): str(v) for k, v in metadata.items()}
        resp = s3.put_object(Bucket=self.bucket, Key=key, Body=data, **extra)
        return ObjectRef(key=key, version_id=resp.get("VersionId"),
                         etag=(resp.get("ETag") or "").strip('"'), size=len(data))

    def get(self, key: str, version_id: str | None = None) -> bytes:
        s3 = self._client()
        kw = {"Bucket": self.bucket, "Key": key}
        if version_id:
            kw["VersionId"] = version_id
        from botocore.exceptions import ClientError
        try:
            return s3.get_object(**kw)["Body"].read()
        except ClientError as e:
            raise RuntimeError(f"object-not-found:{key}") from e

    def signed_url(self, key: str, *, method: str = "GET", version_id: str | None = None,
                   ttl_s: int = 300, content_disposition: str | None = None) -> str | None:
        s3 = self._client()
        params = {"Bucket": self.bucket, "Key": key}
        if version_id:
            params["VersionId"] = version_id
        if method.upper() == "PUT":
            client_method = "put_object"
        else:
            client_method = "get_object"
            if content_disposition:
                params["ResponseContentDisposition"] = content_disposition
        return s3.generate_presigned_url(client_method, Params=params, ExpiresIn=int(ttl_s))

    def supports_signed_urls(self) -> bool:
        return True

    def head(self, key: str, version_id: str | None = None) -> ObjectRef | None:
        s3 = self._client()
        from botocore.exceptions import ClientError
        kw = {"Bucket": self.bucket, "Key": key}
        if version_id:
            kw["VersionId"] = version_id
        try:
            resp = s3.head_object(**kw)
        except ClientError:
            return None
        return ObjectRef(key=key, version_id=resp.get("VersionId"),
                         etag=(resp.get("ETag") or "").strip('"'),
                         size=int(resp.get("ContentLength") or 0))

    def list_versions(self, key: str) -> list[ObjectRef]:
        s3 = self._client()
        resp = s3.list_object_versions(Bucket=self.bucket, Prefix=key)
        out: list[ObjectRef] = []
        for v in resp.get("Versions", []):
            if v.get("Key") != key:
                continue
            out.append(ObjectRef(key=key, version_id=v.get("VersionId"),
                                 etag=(v.get("ETag") or "").strip('"'),
                                 size=int(v.get("Size") or 0)))
        return out

    def delete(self, key: str, version_id: str | None = None) -> None:
        s3 = self._client()
        kw = {"Bucket": self.bucket, "Key": key}
        if version_id:
            kw["VersionId"] = version_id
        s3.delete_object(**kw)

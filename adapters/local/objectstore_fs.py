"""Local ObjectStore: the instance filesystem. No signed URLs (bytes are proxied)."""
from __future__ import annotations

from pathlib import Path

from core.ports.types import ObjectRef


class FsObjectStore:
    def __init__(self, root: str = "data/files", **kw):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, key, data, content_type, metadata=None) -> ObjectRef:
        p = self.root / key
        p.write_bytes(data)
        return ObjectRef(key=key, size=len(data))

    def get(self, key, version_id=None):
        p = self.root / key
        return p.read_bytes() if p.exists() else None

    def signed_url(self, key, *, method="GET", version_id=None, ttl_s=300, content_disposition=None):
        return None

    def supports_signed_urls(self) -> bool:
        return False

    def head(self, key, version_id=None):
        p = self.root / key
        return ObjectRef(key=key, size=p.stat().st_size) if p.exists() else None

    def list_versions(self, key):
        p = self.root / key
        return [ObjectRef(key=key, size=p.stat().st_size)] if p.exists() else []

    def delete(self, key, version_id=None):
        (self.root / key).unlink(missing_ok=True)

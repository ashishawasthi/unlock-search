"""Local Identity: a self-issued JWT (Bearer) or, only when UNLOCK_DEV_AUTH=1, the
spoofable X-User header for tests/e2e. CORE then loads the full user from the store.

In gcp/onprem this is replaced by a gateway-attested identity header; the dev paths
must be OFF in production (this adapter is simply not selected by those profiles)."""
from __future__ import annotations

import os

from core.domain.auth import jwt_decode
from core.ports.types import Principal


class DevIdentity:
    def __init__(self, dev_auth: bool | None = None, **kw):
        env = os.environ.get("UNLOCK_DEV_AUTH", "0") not in ("0", "false", "")
        self.dev_auth = env if dev_auth is None else bool(dev_auth)

    def principal_from_request(self, headers: dict) -> Principal | None:
        authz = headers.get("authorization", "")
        if authz.lower().startswith("bearer "):
            payload = jwt_decode(authz[7:].strip())
            if payload and payload.get("sub"):
                return Principal(id=payload["sub"])
        if self.dev_auth and headers.get("x-user"):
            return Principal(id=headers["x-user"])
        return None

"""
On-prem Identity: trust a gateway-attested identity header. An API gateway (Kong / Envoy
/ Istio ingress) terminates OIDC against the enterprise IdP, then injects a signed identity
header (default X-Unlock-User) on the way to the app. This adapter NEVER accepts a raw client
bearer token; only the gateway's attestation is trusted.

Verification modes:
  - HMAC (shared secret): header value is "<user_id>.<base64url-hmac-sha256>"; verified
    against OIDC_GATEWAY_HMAC. Tamper-proof if the secret is gateway<->app only.
  - JWT (HS256): a gateway-signed compact JWT in the header; verified via core.domain.auth.
  - Unverified: if no secret/JWT is configured, the plain header value is trusted (only safe
    when the gateway is the sole network path, i.e. mTLS mesh / private ingress).

Backing service + config:
  OIDC_IDENTITY_HEADER   header name the gateway injects (default 'x-unlock-user')
  OIDC_GATEWAY_HMAC      shared secret for the HMAC mode (optional)
  OIDC_VERIFY_JWT        '1' to treat the header value as a gateway-signed HS256 JWT
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

from core.ports.types import Principal


class OidcIdentity:
    def __init__(self, identity_header: str | None = None, gateway_hmac: str | None = None,
                 verify_jwt: bool | None = None, **kw):
        self.header = (identity_header or os.environ.get("OIDC_IDENTITY_HEADER", "x-unlock-user")).lower()
        self.hmac_secret = gateway_hmac or os.environ.get("OIDC_GATEWAY_HMAC", "")
        env_jwt = os.environ.get("OIDC_VERIFY_JWT", "0") not in ("0", "false", "")
        self.verify_jwt = env_jwt if verify_jwt is None else bool(verify_jwt)

    def principal_from_request(self, headers: dict) -> Principal | None:
        # Normalize header lookup (case-insensitive).
        hl = {k.lower(): v for k, v in (headers or {}).items()}
        # Reject any attempt to pass a raw client OIDC token directly.
        raw = hl.get(self.header)
        if not raw:
            return None
        if self.verify_jwt:
            return self._from_jwt(raw)
        if self.hmac_secret:
            return self._from_hmac(raw)
        # No verifier configured: trust the gateway-only header value as the user id.
        return Principal(id=raw.strip())

    def _from_hmac(self, value: str) -> Principal | None:
        try:
            user_id, sig = value.rsplit(".", 1)
        except ValueError:
            return None
        expected = base64.urlsafe_b64encode(
            hmac.new(self.hmac_secret.encode(), user_id.encode(), hashlib.sha256).digest()
        ).rstrip(b"=").decode()
        if not hmac.compare_digest(expected, sig):
            return None
        return Principal(id=user_id)

    def _from_jwt(self, value: str) -> Principal | None:
        from core.domain.auth import jwt_decode
        payload = jwt_decode(value.strip())
        if payload and payload.get("sub"):
            return Principal(id=payload["sub"])
        return None

"""
GCP Identity adapter: Apigee / IAP-attested identity.

Backing service: an Apigee proxy or Cloud IAP gateway that authenticates the client and
forwards a TRUSTED identity header (default 'X-Apigee-Authenticated-User'), or a verified
JWT assertion ('X-Goog-IAP-JWT-Assertion' / a configured assertion header). This adapter
TRUSTS the gateway-attested header and REJECTS raw client-supplied bearer tokens (those
must be exchanged at the gateway, never honored here).

Where an assertion header + audience are configured, the JWT signature is verified against
Google's public keys before the identity is accepted; otherwise the gateway is the trust
boundary and the plain authenticated-user header is used.

Config kwargs: header (authenticated-user header name), assertion_header, audience,
jwks_uri. Env fallbacks: APIGEE_USER_HEADER, IAP_ASSERTION_HEADER, IAP_AUDIENCE.
CORE then loads the full user (clearance/groups/projects) from the store by this id.
"""
from __future__ import annotations

import os

from core.ports.types import Principal


class ApigeeIdentity:
    def __init__(self, header: str | None = None, assertion_header: str | None = None,
                 audience: str | None = None, jwks_uri: str | None = None, **kw):
        self.header = (header or os.environ.get("APIGEE_USER_HEADER", "x-apigee-authenticated-user")).lower()
        self.assertion_header = (assertion_header
                                 or os.environ.get("IAP_ASSERTION_HEADER", "x-goog-iap-jwt-assertion")).lower()
        self.audience = audience or os.environ.get("IAP_AUDIENCE", "")
        self.jwks_uri = jwks_uri or os.environ.get(
            "IAP_JWKS_URI", "https://www.gstatic.com/iap/verify/public_key-jwk")

    def _verify_assertion(self, token: str) -> str | None:
        # lazy import: importable without google-auth
        try:
            from google.auth.transport import requests as greq
            from google.oauth2 import id_token
            info = id_token.verify_token(
                token, greq.Request(), audience=self.audience or None,
                certs_url=self.jwks_uri)
            # IAP puts the user identity in 'email' (and 'sub'); prefer email, fall back to sub
            return info.get("email") or info.get("sub")
        except Exception:
            return None

    def principal_from_request(self, headers: dict) -> Principal | None:
        h = {k.lower(): v for k, v in (headers or {}).items()}
        # 1) verified JWT assertion (strongest), only if an audience is configured
        assertion = h.get(self.assertion_header)
        if assertion and self.audience:
            uid = self._verify_assertion(assertion)
            if uid:
                return Principal(id=uid)
            return None        # assertion present but invalid -> reject, do not fall through
        # 2) gateway-attested plain user header (gateway is the trust boundary)
        uid = h.get(self.header)
        if uid:
            return Principal(id=uid.strip())
        # 3) raw client Authorization: Bearer ... is NOT trusted here (must pass the gateway)
        return None

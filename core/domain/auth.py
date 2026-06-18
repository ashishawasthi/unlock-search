"""
Authentication domain: stdlib password hashing + HS256 JWT + HMAC approve/deny tokens
+ a simple brute-force throttle. Pure stdlib, no provider coupling.

WHO the caller is comes from the Identity port (dev JWT/X-User locally; a gateway-signed
header on gcp/onprem). The token FORMAT and password verification are domain and live here.
The dev self-issued-JWT / X-User path must be profile-gated OFF in production (see api/deps).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from core.domain.abac import clr_label
from core.ports.types import Principal

DATA = Path(os.environ.get("AIBOX_DATA") or "data")


def _jwt_secret() -> str:
    env = os.environ.get("AIBOX_JWT_SECRET")
    if env:
        return env
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        f = DATA / ".jwt_secret"
        if f.exists():
            return f.read_text().strip()
        s = secrets.token_hex(32)
        f.write_text(s); f.chmod(0o600)
        return s
    except OSError:
        return secrets.token_hex(32)   # process-local fallback


JWT_SECRET = _jwt_secret()


# ---- passwords ----
def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 100_000)
    return f"pbkdf2${salt.hex()}${h.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, salt_hex, h_hex = (stored or "").split("$")
        h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt_hex), 100_000)
        return hmac.compare_digest(h.hex(), h_hex)
    except Exception:
        return False


# ---- JWT (HS256) ----
def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def jwt_encode(payload: dict) -> str:
    head = _b64u(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64u(json.dumps(payload, separators=(",", ":")).encode())
    seg = head + "." + body
    sig = _b64u(hmac.new(JWT_SECRET.encode(), seg.encode(), hashlib.sha256).digest())
    return seg + "." + sig


def jwt_decode(token: str):
    try:
        head_b, body_b, sig = token.split(".")
        seg = head_b + "." + body_b
        exp = _b64u(hmac.new(JWT_SECRET.encode(), seg.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, exp):
            return None
        payload = json.loads(_b64u_dec(body_b))
        if payload.get("exp") and payload["exp"] < time.time():
            return None
        return payload
    except Exception:
        return None


def request_token(request_id: str) -> str:
    return hmac.new(JWT_SECRET.encode(), ("ar:" + request_id).encode(), hashlib.sha256).hexdigest()[:32]


# ---- login throttle ----
_LOGIN_FAILS: dict[str, tuple[int, float]] = {}


def login_check(key: str) -> bool:
    cnt, ts = _LOGIN_FAILS.get(key, (0, time.time()))
    if time.time() - ts > 300:
        cnt = 0
    return cnt < 8


def record_fail(key: str) -> None:
    cnt, ts = _LOGIN_FAILS.get(key, (0, time.time()))
    if time.time() - ts > 300:
        cnt, ts = 0, time.time()
    _LOGIN_FAILS[key] = (cnt + 1, ts)


def clear_fail(key: str) -> None:
    _LOGIN_FAILS.pop(key, None)


# ---- user loading ----
def load_user(store, uid: str) -> dict | None:
    rows = store.execute("SELECT * FROM users WHERE id=?", (uid,))
    if not rows or not rows[0].get("is_active"):
        return None
    u = rows[0]
    gs = [r["group_id"] for r in store.execute("SELECT group_id FROM user_groups WHERE user_id=?", (uid,))]
    projs = [r["project"] for r in store.execute("SELECT project FROM user_projects WHERE user_id=?", (uid,))]
    return {**u, "groups": gs, "projects": projs}


def to_principal(u: dict) -> Principal:
    return Principal(id=u["id"], is_admin=bool(u.get("is_admin")), clearance=int(u.get("clearance") or 0),
                     groups=list(u.get("groups") or []), user_type=u.get("user_type") or "employee",
                     projects=list(u.get("projects") or []))


def me_dict(store, u: dict) -> dict:
    depts = [r["name"] for r in store.execute(
        "SELECT g.name FROM groups g JOIN user_groups ug ON ug.group_id=g.id WHERE ug.user_id=?", (u["id"],))]
    return {"id": u["id"], "name": u["name"], "username": u.get("username"), "email": u.get("email"),
            "role": u.get("role"), "user_type": u.get("user_type"), "is_admin": u.get("is_admin"),
            "clearance": clr_label(u.get("clearance")), "clearance_level": u.get("clearance"),
            "departments": depts, "groups": depts, "projects": u.get("projects", [])}

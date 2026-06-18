"""
Profile loading + config resolution. One env var (UNLOCK_PROFILE) selects a YAML
profile under profiles/. Secrets are referenced indirectly via *_env keys so the
YAML never holds a secret. Paths are package-relative (works in a container).
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # keep the local profile usable before deps are installed
    yaml = None

REPO_ROOT = Path(__file__).resolve().parent.parent
PROFILES_DIR = REPO_ROOT / "profiles"


def _interp_env(cfg: Any) -> Any:
    """Resolve `${ENV:VAR}` and `*_env: VAR` indirections so secrets stay in env."""
    if isinstance(cfg, dict):
        out: dict = {}
        for k, v in cfg.items():
            if k.endswith("_env") and isinstance(v, str):
                out[k[:-4]] = os.environ.get(v, "")     # dsn_env: PG_DSN -> dsn: <value>
            else:
                out[k] = _interp_env(v)
        return out
    if isinstance(cfg, list):
        return [_interp_env(x) for x in cfg]
    if isinstance(cfg, str) and cfg.startswith("${ENV:") and cfg.endswith("}"):
        return os.environ.get(cfg[6:-1], "")
    return cfg


@lru_cache(maxsize=None)
def load_profile(name: str | None = None) -> dict:
    name = name or os.environ.get("UNLOCK_PROFILE", "local")
    path = PROFILES_DIR / f"{name}.yaml"
    if not path.exists():
        raise RuntimeError(f"unknown profile '{name}': {path} not found")
    text = path.read_text()
    if yaml is None:
        raise RuntimeError("pyyaml not installed; run: pip install -r requirements.txt")
    data = yaml.safe_load(text) or {}
    data.setdefault("profile", name)
    data["adapters"] = data.get("adapters", {})
    data["config"] = _interp_env(data.get("config", {}))
    return data


def profile_name() -> str:
    return os.environ.get("UNLOCK_PROFILE", "local")

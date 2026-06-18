"""
Append-only audit log. Domain semantics live here; the write goes through the
RelationalStore port and ALSO mirrors to the Telemetry port (so Cloud Logging or
OTel sees the same events). Reused by every profile.
"""
from __future__ import annotations

import json
import time


def audit(store, user_id: str | None, event: str, detail: dict, telemetry=None) -> None:
    store.execute("INSERT INTO audit(ts,user_id,event,detail) VALUES(?,?,?,?)",
                  (time.time(), user_id, event, json.dumps(detail)))
    if telemetry is not None:
        try:
            telemetry.log(event, {"user_id": user_id, **(detail if isinstance(detail, dict) else {})})
        except Exception:
            pass   # telemetry must never break the request path

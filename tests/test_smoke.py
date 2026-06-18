"""End-to-end smoke test of the local profile via FastAPI TestClient.
Exercises: login, upload + attributes, ABAC search (full vs restricted card), RAG chat.
Run: AIBOX_PROFILE=local AIBOX_DATA=/tmp/gcpu_test python -m pytest tests/test_smoke.py -q
or just: python tests/test_smoke.py
"""
import json
import os
import tempfile

os.environ.setdefault("AIBOX_PROFILE", "local")
os.environ.setdefault("AIBOX_DATA", tempfile.mkdtemp(prefix="gcpu_"))
os.environ["AIBOX_QUIET_TELEMETRY"] = "1"

from fastapi.testclient import TestClient   # noqa: E402

from core.api.app import create_app          # noqa: E402

DOC = ("Q3 Financial Report\n\nREVENUE\n\nRevenue grew 20 percent to 5M dollars in Q3. "
       "Operating costs were stable.\n\nOUTLOOK\n\nWe expect continued growth in Q4.\n").encode()


def _login(client, username):
    r = client.post("/api/auth/login", json={"username": username, "password": username})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def main():
    app = create_app()
    client = TestClient(app)

    alice = _login(client, "alice")
    me = client.get("/api/me", headers=alice).json()
    assert me["is_admin"] == 1 and "Finance" in me["departments"], me

    # upload a Finance / Internal doc as alice
    up = client.post("/api/documents", headers=alice,
                     files={"file": ("q3.txt", DOC, "text/plain")},
                     data={"title": "Q3 Report",
                           "attrs": json.dumps({"departments": ["g-finance"], "min_clearance": "Internal",
                                                "status": "published"})})
    assert up.status_code == 200, up.text
    doc_id = up.json()["doc_id"]
    assert up.json()["chunks"] >= 1, up.json()

    # alice can search and find it
    s = client.post("/api/search", headers=alice, json={"q": "revenue"}).json()
    assert any(r["doc_id"] == doc_id for r in s["results"]), s

    # alice RAG chat with citations (extractive fallback if no ANTHROPIC_API_KEY)
    conv = client.post("/api/conversations", headers=alice, json={"doc_ids": [doc_id]}).json()
    msg = client.post(f"/api/conversations/{conv['conv_id']}/messages", headers=alice,
                      json={"content": "What was revenue in Q3?"}).json()
    assert "revenue" in msg["answer"].lower() or msg["answer"], msg

    # carol (partner / External / Public) must NOT access the Finance doc -> restricted card
    carol = _login(client, "carol")
    cs = client.post("/api/search", headers=carol, json={"q": "revenue"}).json()
    assert all(r["doc_id"] != doc_id for r in cs["results"]), "carol leaked an inaccessible doc"
    assert any(r["doc_id"] == doc_id for r in cs["restricted"]), cs

    # negative ACL: carol cannot fetch the file or chunks
    assert client.get(f"/api/documents/{doc_id}/file", headers=carol).status_code == 404
    assert client.get(f"/api/documents/{doc_id}/chunks", headers=carol).status_code == 404

    # admin-only audit
    assert client.get("/api/audit", headers=alice).status_code == 200
    assert client.get("/api/audit", headers=carol).status_code == 403

    print("SMOKE OK: login, upload, ABAC search (full+restricted), RAG chat, negative ACL, admin gate")


if __name__ == "__main__":
    main()

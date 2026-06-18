"""
Port-conformance + ABAC contract suite for gcp-unlock.

This is the security + behavior contract that EVERY profile must satisfy. It runs
over the FastAPI TestClient (same as tests/test_smoke.py): create_app() builds a
Container from AIBOX_PROFILE, migrates + seeds, and we exercise the HTTP surface as
the three seeded personas (password == username) plus a few extra seeded principals.

The SAME suite runs against any backend: set AIBOX_PROFILE before invoking pytest.
It defaults to local. ABAC is enforced server-side identically across profiles, so
the accessible-set assertions below are profile-independent (they assert the rule,
not the ranking). Backends needing a live service (Postgres, OpenSearch, Gemini Enterprise Agent Platform,
AlloyDB, ...) are skipped at import-time if the SDK/credentials are unavailable.

ABAC rule under test (core.domain.abac):
  grant if  admin  OR  owner  OR  a valid doc-scoped user-grant  OR
            (clearance >= doc.min_clearance
             AND a doc department-grant intersects the user's groups
             AND (doc has no user_type restriction OR the user's type is allowed)
             AND (doc has no project restriction OR the user shares a project))

Run: python -m pytest tests/ -q
"""
import json
import os
import tempfile
import uuid

os.environ.setdefault("AIBOX_PROFILE", "local")
os.environ.setdefault("AIBOX_DATA", tempfile.mkdtemp(prefix="gcpu_contract_"))
os.environ["AIBOX_QUIET_TELEMETRY"] = "1"

# A unique per-run salt baked into every uploaded body. The local SQLite store writes
# to a persistent data/aibox.db, so this defeats the owner+file_hash upload-dedup on
# reruns and keeps each run's corpus isolated (exact-set asserts intersect this run's
# ids only). On backends with throwaway state it is simply a harmless nonce.
RUN_SALT = uuid.uuid4().hex

import pytest                                    # noqa: E402
from fastapi.testclient import TestClient        # noqa: E402

from core.api.app import create_app              # noqa: E402
from core.domain.abac import CLEARANCE           # noqa: E402
from core.domain.auth import hash_password       # noqa: E402

PROFILE = os.environ.get("AIBOX_PROFILE", "local")


# --------------------------------------------------------------------------- #
# fixtures: one app/client + a deterministic ABAC corpus, built once.
# --------------------------------------------------------------------------- #
# Extra non-admin principals seeded directly via the store port (the seed module
# only creates alice/bob/carol; we add dave/erin to isolate the clearance / project
# / user-type gates with positive AND negative coverage).
EXTRA_USERS = [
    # id, name, username, email, role, user_type, clearance_label, [group_ids], [projects]
    ("u-dave", "Dave Lin", "dave", "dave@northwind.com", "Eng+Finance Analyst",
     "employee", "Confidential", ["g-finance", "g-engineering"], ["Project-Atlas"]),
    ("u-erin", "Erin Park", "erin", "erin@acme-partner.com", "Finance Partner",
     "partner", "Internal", ["g-finance"], []),
]

# Each doc: a unique nonsense keyword so FTS retrieval is unambiguous, plus the ACL
# attributes that gate it. Body text repeats the keyword so any lexical backend hits.
DOCS = {
    # key:        (title, owner_login, keyword, attrs)
    "fin_internal": ("Finance Internal Memo", "alice", "zephyrnote",
                     {"departments": ["g-finance"], "min_clearance": "Internal",
                      "status": "published"}),
    "fin_conf":     ("Finance Confidential Plan", "alice", "quasardeck",
                     {"departments": ["g-finance"], "min_clearance": "Confidential",
                      "status": "published"}),
    "eng_internal": ("Engineering Internal Spec", "alice", "nimbusspec",
                     {"departments": ["g-engineering"], "min_clearance": "Internal",
                      "status": "published"}),
    "fin_partners": ("Finance Partner Briefing", "alice", "vortexbrief",
                     {"departments": ["g-finance"], "min_clearance": "Internal",
                      "user_types": ["partner"], "status": "published"}),
    "fin_proj":     ("Finance Atlas Roadmap", "alice", "cometplan",
                     {"departments": ["g-finance"], "min_clearance": "Internal",
                      "projects": ["Project-Atlas"], "status": "published"}),
    # owner-only private draft, no group grant -> only the owner (bob) + admin (alice)
    "bob_draft":    ("Bob Private Draft", "bob", "plutodraft",
                     {"status": "draft"}),
}


def _login(client, username):
    r = client.post("/api/auth/login", json={"username": username, "password": username})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _seed_extra(store):
    for uid, name, un, email, role, utype, clr, groups, projs in EXTRA_USERS:
        if store.execute("SELECT 1 FROM users WHERE id=?", (uid,)):
            continue
        store.execute(
            "INSERT INTO users(id,name,username,email,role,user_type,clearance,password_hash,"
            "is_admin,is_active) VALUES(?,?,?,?,?,?,?,?,0,1)",
            (uid, name, un, email, role, utype, CLEARANCE[clr], hash_password(un)))
        for g in groups:
            store.execute("INSERT OR IGNORE INTO user_groups(user_id,group_id) VALUES(?,?)", (uid, g))
        for p in projs:
            store.execute("INSERT OR IGNORE INTO user_projects(user_id,project) VALUES(?,?)", (uid, p))


def _body(keyword):
    # repeat the keyword + a shared word so neighbor-continuation has >1 chunk.
    # RUN_SALT makes the bytes unique per run (file-hash dedup is per owner+hash).
    return (f"{keyword.upper()} OVERVIEW (run {RUN_SALT})\n\n"
            f"This document concerns {keyword}. The {keyword} initiative is described here. "
            f"All facts about {keyword} live in this text and nowhere else.\n").encode()


def _upload(client, hdr, title, keyword, attrs):
    r = client.post("/api/documents", headers=hdr,
                    files={"file": (f"{keyword}.txt", _body(keyword), "text/plain")},
                    data={"title": title, "attrs": json.dumps(attrs)})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["chunks"] >= 1, j
    return j["doc_id"]


@pytest.fixture(scope="module")
def app():
    return create_app()


@pytest.fixture(scope="module")
def client(app):
    return TestClient(app)


@pytest.fixture(scope="module")
def env(app, client):
    """Seed extra users + the ABAC corpus once; return ids + headers."""
    store = app.state.c.store()
    _seed_extra(store)
    H = {u: _login(client, u) for u in ("alice", "bob", "carol", "dave", "erin")}
    ids = {}
    for key, (title, owner, keyword, attrs) in DOCS.items():
        ids[key] = _upload(client, H[owner], title, keyword, attrs)
    return {"ids": ids, "H": H, "store": store, "kw": {k: v[2] for k, v in DOCS.items()}}


# --------------------------------------------------------------------------- #
# helpers driven over the HTTP surface
# --------------------------------------------------------------------------- #
def _list_ids(client, hdr):
    r = client.get("/api/documents", headers=hdr)
    assert r.status_code == 200, r.text
    return {d["id"] for d in r.json()}


def _search(client, hdr, q):
    r = client.post("/api/search", headers=hdr, json={"q": q})
    assert r.status_code == 200, r.text
    return r.json()


def _can_get_doc(client, hdr, doc_id):
    return client.get(f"/api/documents/{doc_id}", headers=hdr).status_code == 200


def _can_get_file(client, hdr, doc_id):
    return client.get(f"/api/documents/{doc_id}/file", headers=hdr).status_code == 200


def _can_get_chunks(client, hdr, doc_id):
    return client.get(f"/api/documents/{doc_id}/chunks", headers=hdr).status_code == 200


def _rag(client, hdr, query, doc_ids=None):
    conv = client.post("/api/conversations", headers=hdr,
                       json={"doc_ids": doc_ids or []})
    assert conv.status_code == 200, conv.text
    cid = conv.json()["conv_id"]
    msg = client.post(f"/api/conversations/{cid}/messages", headers=hdr,
                      json={"content": query})
    assert msg.status_code == 200, msg.text
    return msg.json()


# Ground truth: for each (persona, doc) the EXACT expected access decision.
# alice=admin (all), bob=employee/Internal/finance, carol=partner/Public/external,
# dave=employee/Confidential/{finance,eng}/Atlas, erin=partner/Internal/finance.
EXPECTED = {
    "fin_internal": {"alice": True,  "bob": True,  "carol": False, "dave": True,  "erin": True},
    "fin_conf":     {"alice": True,  "bob": False, "carol": False, "dave": True,  "erin": False},
    "eng_internal": {"alice": True,  "bob": False, "carol": False, "dave": True,  "erin": False},
    "fin_partners": {"alice": True,  "bob": False, "carol": False, "dave": False, "erin": True},
    "fin_proj":     {"alice": True,  "bob": False, "carol": False, "dave": True,  "erin": False},
    "bob_draft":    {"alice": True,  "bob": True,  "carol": False, "dave": False, "erin": False},
}
PERSONAS = ["alice", "bob", "carol", "dave", "erin"]
DOC_KEYS = list(EXPECTED.keys())


# --------------------------------------------------------------------------- #
# 1. ABAC matrix: the accessible-document set per persona is EXACTLY correct.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("persona", PERSONAS)
def test_abac_list_exact_accessible_set(env, client, persona):
    """The /api/documents list a persona receives must contain EXACTLY the corpus
    docs they may access (no more, no fewer) -- positive and negative in one shot."""
    ids, H = env["ids"], env["H"]
    listed = _list_ids(client, H[persona])
    want_visible = {ids[k] for k in DOC_KEYS if EXPECTED[k][persona]}
    want_hidden = {ids[k] for k in DOC_KEYS if not EXPECTED[k][persona]}
    corpus_visible = listed & set(ids.values())
    assert corpus_visible == want_visible, (
        f"{persona}: expected {want_visible}, got {corpus_visible}")
    assert not (listed & want_hidden), f"{persona} leaked hidden docs via list"


@pytest.mark.parametrize("persona", PERSONAS)
@pytest.mark.parametrize("key", DOC_KEYS)
def test_abac_doc_endpoints_match_rule(env, client, persona, key):
    """can_access (doc metadata, file, chunks) agrees with the ABAC truth table for
    every (persona, doc) cell. Inaccessible docs return 404 on all three."""
    ids, H = env["ids"], env["H"]
    doc_id, hdr, allowed = ids[key], H[persona], EXPECTED[key][persona]
    assert _can_get_doc(client, hdr, doc_id) is allowed
    assert _can_get_file(client, hdr, doc_id) is allowed
    assert _can_get_chunks(client, hdr, doc_id) is allowed


def test_abac_owner_sees_own_draft_others_dont(env, client):
    """Owner (bob) sees his own unpublished draft; default is owner-only (no group
    grant) so even a higher-clearance employee (dave) cannot."""
    ids, H = env["ids"], env["H"]
    draft = ids["bob_draft"]
    assert draft in _list_ids(client, H["bob"])
    assert draft not in _list_ids(client, H["dave"])
    assert draft not in _list_ids(client, H["carol"])
    assert _can_get_doc(client, H["bob"], draft)
    assert not _can_get_doc(client, H["dave"], draft)


def test_abac_clearance_ladder_blocks(env, client):
    """bob (Internal) cannot reach a Confidential finance doc he is otherwise grouped
    for; dave (Confidential) can. Isolates the clearance dimension."""
    ids, H = env["ids"], env["H"]
    conf = ids["fin_conf"]
    assert not _can_get_doc(client, H["bob"], conf)
    assert _can_get_doc(client, H["dave"], conf)


def test_abac_department_gate(env, client):
    """bob (finance only) cannot reach an engineering doc; dave (finance+eng) can."""
    ids, H = env["ids"], env["H"]
    eng = ids["eng_internal"]
    assert not _can_get_doc(client, H["bob"], eng)
    assert _can_get_doc(client, H["dave"], eng)


def test_abac_user_type_gate(env, client):
    """A partner-only doc: in-group employees (bob, dave) are blocked; an in-group
    partner (erin) is allowed. Isolates the user-type dimension."""
    ids, H = env["ids"], env["H"]
    part = ids["fin_partners"]
    assert not _can_get_doc(client, H["bob"], part)
    assert not _can_get_doc(client, H["dave"], part)
    assert _can_get_doc(client, H["erin"], part)


def test_abac_project_gate(env, client):
    """A project-scoped doc: only a user who shares the project (dave/Atlas) is allowed;
    an otherwise-qualified finance employee (bob, no project) is blocked."""
    ids, H = env["ids"], env["H"]
    proj = ids["fin_proj"]
    assert not _can_get_doc(client, H["bob"], proj)
    assert _can_get_doc(client, H["dave"], proj)


# --------------------------------------------------------------------------- #
# 2. NEGATIVE ACL (security-critical): no path leaks an inaccessible doc.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("persona", PERSONAS)
@pytest.mark.parametrize("key", DOC_KEYS)
def test_negative_search_never_leaks_full_results(env, client, persona, key):
    """Searching a doc's unique keyword must NEVER place an inaccessible doc in the
    full `results` (it may appear only as a redacted `restricted` card)."""
    ids, H, kw = env["ids"], env["H"], env["kw"]
    if EXPECTED[key][persona]:
        return  # accessible case is covered by the positive search test below
    res = _search(client, H[persona], kw[key])
    leaked = [r for r in res["results"] if r["doc_id"] == ids[key]]
    assert not leaked, f"{persona} got inaccessible {key} in full search results"


@pytest.mark.parametrize("persona", PERSONAS)
def test_negative_search_results_subset_of_accessible(env, client, persona):
    """Every full search result for a persona, across all corpus keywords, must be an
    accessible corpus doc. Belt-and-suspenders over the per-doc check above."""
    ids, H, kw = env["ids"], env["H"], env["kw"]
    id_to_key = {v: k for k, v in ids.items()}
    allowed_ids = {ids[k] for k in DOC_KEYS if EXPECTED[k][persona]}
    for key in DOC_KEYS:
        res = _search(client, H[persona], kw[key])
        for r in res["results"]:
            if r["doc_id"] in ids.values():
                assert r["doc_id"] in allowed_ids, (
                    f"{persona} saw {id_to_key.get(r['doc_id'])} in results for '{kw[key]}'")


@pytest.mark.parametrize("persona", PERSONAS)
@pytest.mark.parametrize("key", DOC_KEYS)
def test_negative_file_and_chunks_404(env, client, persona, key):
    """An inaccessible doc must 404 on /file and /chunks (no bytes, no text leak)."""
    ids, H = env["ids"], env["H"]
    if EXPECTED[key][persona]:
        return
    doc_id, hdr = ids[key], H[persona]
    assert client.get(f"/api/documents/{doc_id}/file", headers=hdr).status_code == 404
    assert client.get(f"/api/documents/{doc_id}/chunks", headers=hdr).status_code == 404
    assert client.get(f"/api/documents/{doc_id}", headers=hdr).status_code == 404


def test_negative_rag_context_excludes_inaccessible(env, client):
    """RAG answer for a query that ONLY matches an inaccessible doc must return the
    'could not find' message with NO citations, and must not cite the hidden doc even
    when that doc is (maliciously) named in the conversation scope."""
    ids, H, kw = env["ids"], env["H"], env["kw"]
    # carol can access nothing in the corpus; "quasardeck" only lives in fin_conf.
    out = _rag(client, H["carol"], f"What is {kw['fin_conf']}?", doc_ids=[ids["fin_conf"]])
    assert out["cites"] == [], f"carol leaked citations: {out}"
    assert "couldn't find" in out["answer"].lower() or "could not find" in out["answer"].lower(), out
    # bob lacks the engineering doc; even scoping to it must not surface its content.
    out2 = _rag(client, H["bob"], f"Describe {kw['eng_internal']}", doc_ids=[ids["eng_internal"]])
    assert out2["cites"] == [], f"bob leaked citations for eng doc: {out2}"
    assert all(c.get("doc_id") != ids["eng_internal"] for c in out2["cites"])


# --------------------------------------------------------------------------- #
# 3. Restricted-card redaction: title + short head only, never full content.
# --------------------------------------------------------------------------- #
def test_restricted_card_redaction(env, client):
    """carol searching a finance keyword sees fin_internal ONLY as a restricted card:
    title + a short redacted head (<= ~12 words), never the full chunk content."""
    ids, H, kw = env["ids"], env["H"], env["kw"]
    res = _search(client, H["carol"], kw["fin_internal"])
    assert all(r["doc_id"] != ids["fin_internal"] for r in res["results"]), "leaked full result"
    cards = [r for r in res["restricted"] if r["doc_id"] == ids["fin_internal"]]
    assert cards, f"expected a restricted card for fin_internal, got {res['restricted']}"
    card = cards[0]
    assert card["title"] == "Finance Internal Memo"
    assert "redacted" in card and card["redacted"]
    head = card["redacted"].replace(" ...", "").strip()
    head_words = head.split()
    assert len(head_words) <= 12, f"redacted head too long: {card['redacted']}"
    # the redaction is the whitespace-collapsed first ~10 words of the real content:
    # a strict PREFIX, never the whole body.
    full_norm = " ".join(_body(kw["fin_internal"]).decode().split())
    assert full_norm.startswith(head), f"redacted head is not a prefix of content: {head}"
    assert head != full_norm, "card exposed the entire content"
    # the tail of the content (and the full chunk text) must NOT leak into the card
    assert "nowhere else" not in card["redacted"], "tail of content leaked into card"
    assert "snippet" not in card, "restricted card exposed a full snippet field"


# --------------------------------------------------------------------------- #
# 4. Approval workflow: request -> approve -> access; self-approval blocked;
#    single-use token; expired / unconfirmed interstitial does not mutate.
# --------------------------------------------------------------------------- #
def test_approval_grants_access_then_consumable(env, client):
    """bob requests fin_conf (Confidential, he lacks clearance); alice (admin) approves;
    bob then gains real access via the doc + file + chunks endpoints."""
    ids, H = env["ids"], env["H"]
    doc_id = ids["fin_conf"]
    assert not _can_get_doc(client, H["bob"], doc_id)  # precondition
    r = client.post("/api/access-requests", headers=H["bob"],
                    json={"doc_id": doc_id, "justification": "Q3 close"})
    assert r.status_code == 200, r.text
    rid = r.json()["request_id"]
    # admin approves via the authenticated decide route
    d = client.post(f"/api/access-requests/{rid}/decide", headers=H["alice"],
                    json={"decision": "approve"})
    assert d.status_code == 200, d.text
    # grant is now consumable across every retrieval path
    assert _can_get_doc(client, H["bob"], doc_id)
    assert _can_get_file(client, H["bob"], doc_id)
    assert _can_get_chunks(client, H["bob"], doc_id)


def test_approval_self_approval_blocked(env, client):
    """A requester cannot decide their own request (self-approval is forbidden)."""
    ids, H = env["ids"], env["H"]
    # dave requests fin_partners (partner-only; he is blocked as an employee)
    doc_id = ids["fin_partners"]
    r = client.post("/api/access-requests", headers=H["dave"],
                    json={"doc_id": doc_id, "justification": "need it"})
    assert r.status_code == 200, r.text
    rid = r.json()["request_id"]
    # dave is not the approver (alice owns it) -> forbidden; even if he were, self -> 403
    d = client.post(f"/api/access-requests/{rid}/decide", headers=H["dave"],
                    json={"decision": "approve"})
    assert d.status_code == 403, d.text
    assert not _can_get_doc(client, H["dave"], doc_id)  # unchanged


def test_approval_token_single_use(env, client, app):
    """The HMAC email-link token is single-use: the first confirmed action decides the
    request; a second action on the same token returns 409 and does not re-mutate."""
    ids, H = env["ids"], env["H"]
    store = app.state.c.store()
    doc_id = ids["fin_proj"]  # bob lacks the project -> legitimately requestable
    assert not _can_get_doc(client, H["bob"], doc_id)
    r = client.post("/api/access-requests", headers=H["bob"],
                    json={"doc_id": doc_id, "justification": "atlas"})
    rid = r.json()["request_id"]
    token = store.execute("SELECT token FROM access_requests WHERE id=?", (rid,))[0]["token"]
    base = f"/api/access-requests/{rid}/action?token={token}"

    # unconfirmed interstitial must NOT mutate (defeats email link-prefetch)
    inter = client.get(f"{base}&decision=approve")
    assert inter.status_code == 200
    assert store.execute("SELECT status FROM access_requests WHERE id=?", (rid,))[0]["status"] == "pending"

    # first confirmed action decides it
    a1 = client.get(f"{base}&decision=approve&confirm=1")
    assert a1.status_code == 200, a1.text
    assert store.execute("SELECT status FROM access_requests WHERE id=?", (rid,))[0]["status"] == "approved"

    # second action on the same (now used) token -> 409, no change
    a2 = client.get(f"{base}&decision=deny&confirm=1")
    assert a2.status_code == 409, a2.text
    assert store.execute("SELECT status FROM access_requests WHERE id=?", (rid,))[0]["status"] == "approved"


def test_approval_expired_link_does_not_mutate(env, client, app):
    """An expired token returns 410 and never decides the request."""
    ids, H = env["ids"], env["H"]
    store = app.state.c.store()
    doc_id = ids["eng_internal"]  # bob lacks the eng dept -> requestable
    r = client.post("/api/access-requests", headers=H["bob"],
                    json={"doc_id": doc_id, "justification": "spec"})
    rid = r.json()["request_id"]
    token = store.execute("SELECT token FROM access_requests WHERE id=?", (rid,))[0]["token"]
    store.execute("UPDATE access_requests SET token_expires_at=? WHERE id=?", (1.0, rid))  # in the past
    a = client.get(f"/api/access-requests/{rid}/action?token={token}&decision=approve&confirm=1")
    assert a.status_code == 410, a.text
    assert store.execute("SELECT status FROM access_requests WHERE id=?", (rid,))[0]["status"] == "pending"
    assert not _can_get_doc(client, H["bob"], doc_id)


# --------------------------------------------------------------------------- #
# 5. Versioning: a new version re-indexes; old chunks gone; can_access unchanged.
# --------------------------------------------------------------------------- #
def test_versioning_reindexes_and_preserves_acl(env, client, app):
    """Upload a v2 of bob_draft: old chunk text is gone from search, new text is found,
    and the access decision (owner-only) is unchanged across the version bump."""
    ids, H = env["ids"], env["H"]
    store = app.state.c.store()
    doc_id = ids["bob_draft"]
    old_kw = env["kw"]["bob_draft"]
    new_kw = "saturndelta"
    # precondition: only bob (owner) + alice (admin) access; dave does not
    assert _can_get_doc(client, H["bob"], doc_id)
    assert not _can_get_doc(client, H["dave"], doc_id)
    old_chunk_ids = {r["chunk_id"] for r in
                     store.execute("SELECT chunk_id FROM chunks WHERE doc_id=?", (doc_id,))}
    assert old_chunk_ids

    up = client.post(f"/api/documents/{doc_id}/versions", headers=H["bob"],
                     files={"file": (f"{new_kw}.txt", _body(new_kw), "text/plain")},
                     data={"change_notes": "v2"})
    assert up.status_code == 200, up.text
    assert up.json()["version"] == 2

    # old chunks are gone, replaced (re-indexed) by the new version's chunks
    new_chunk_ids = {r["chunk_id"] for r in
                     store.execute("SELECT chunk_id FROM chunks WHERE doc_id=?", (doc_id,))}
    assert old_chunk_ids.isdisjoint(new_chunk_ids), "old chunks survived re-index"

    # the canonical chunk text now carries the new keyword and not the old one
    # (bob_draft is status=draft, so it is intentionally not in published search;
    # we read the re-indexed content via the access-checked /chunks endpoint instead)
    ch = client.get(f"/api/documents/{doc_id}/chunks", headers=H["bob"])
    assert ch.status_code == 200, ch.text
    text = " ".join(c["content"] for c in ch.json()["chunks"]).lower()
    assert new_kw in text, "new version text not re-indexed"
    assert old_kw not in text, "old version text survived re-index"

    # the retriever index was rebuilt too: dave still cannot reach it (ACL unchanged),
    # and once published the owner finds the new keyword (and never the old one).
    pub = client.patch(f"/api/documents/{doc_id}", headers=H["bob"],
                       json={"status": "published"})
    assert pub.status_code == 200, pub.text
    res_new = _search(client, H["bob"], new_kw)
    assert any(r["doc_id"] == doc_id for r in res_new["results"]), res_new
    res_old = _search(client, H["bob"], old_kw)
    assert all(r["doc_id"] != doc_id for r in res_old["results"]), "old text still indexed"

    # ACL unchanged across the version bump + publish (still owner-only, no group grant)
    assert _can_get_doc(client, H["bob"], doc_id)
    assert not _can_get_doc(client, H["dave"], doc_id)


# --------------------------------------------------------------------------- #
# 6. Citation integrity: every citation is fetchable + belongs to an accessible doc.
# --------------------------------------------------------------------------- #
def test_citation_integrity(env, client, app):
    """Every citation returned to a persona must (a) carry a chunk_id that is fetchable
    from the store, (b) belong to a doc the persona can actually access, and (c) match
    a chunk whose doc_id equals the citation's doc_id."""
    ids, H = env["ids"], env["H"]
    store = app.state.c.store()
    # dave can access fin_internal (finance + Confidential >= Internal); ask about it.
    out = _rag(client, H["dave"], f"Summarize {env['kw']['fin_internal']}",
               doc_ids=[ids["fin_internal"]])
    cites = out["cites"]
    if not cites:
        # extractive fallback (no ANTHROPIC_API_KEY) may not emit [n]; assert no LEAK
        # and that the answer is non-empty, then skip the integrity loop.
        assert out["answer"]
        pytest.skip("LLM produced no inline citations (extractive fallback); leak checks above hold")
    allowed = {ids[k] for k in DOC_KEYS if EXPECTED[k]["dave"]}
    for c in cites:
        assert c["chunk_id"], f"citation missing chunk_id: {c}"
        fetched = store.get_chunks([c["chunk_id"]])
        assert len(fetched) == 1, f"citation chunk_id not fetchable: {c}"
        assert fetched[0].doc_id == c["doc_id"], "citation doc_id mismatch with stored chunk"
        assert c["doc_id"] in allowed, f"citation points at inaccessible doc: {c}"

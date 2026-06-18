"""Regression tests for the pushdown-ABAC filter builders (OpenSearch + Vertex).

These backends cannot be enforced by SQL, so the filter MUST mirror core.domain.abac
exactly. The builders are pure (no live cluster needed). Guards the two fixes:
  - OpenSearch must use a `terms departments` clause (empty list -> matches nothing for a
    groupless user), NOT `must_not exists` (which over-grants unshared docs).
  - Vertex must constrain group access (and deny groupless users via a sentinel group).
"""
import json

from core.ports.types import AccessPredicate
from adapters.onprem.retriever_opensearch import OpenSearchRetriever
from adapters.gcp.retriever_vertex import VertexSearchRetriever


def _pred(groups, admin=False, clearance=2, user_type="employee", projects=()):
    return AccessPredicate(user_id="u1", is_admin=admin, clearance=clearance, groups=list(groups),
                           user_type=user_type, projects=list(projects), now=0.0)


def test_opensearch_filter_uses_terms_not_must_not_exists():
    r = OpenSearchRetriever(url="http://dummy:9200")
    f = r._acl_filter(_pred(["g-finance"]))
    blob = json.dumps(f)
    assert '"terms": {"departments": ["g-finance"]}' in blob, blob
    # the over-grant bug was a must_not/exists on DEPARTMENTS specifically; it must be gone.
    # (must_not/exists on user_types/projects is the CORRECT "unconstrained when empty" semantics.)
    assert '"exists": {"field": "departments"}' not in blob, "departments over-grant reintroduced: " + blob
    # owner + grant + attribute branch are all present
    assert '"owner_id": "u1"' in blob and '"grant_user_ids": "u1"' in blob


def test_opensearch_groupless_user_denied_attribute_branch():
    r = OpenSearchRetriever(url="http://dummy:9200")
    f = r._acl_filter(_pred([]))               # no groups
    blob = json.dumps(f)
    # departments terms is an EMPTY list -> matches no document -> attribute branch denied
    assert '"terms": {"departments": []}' in blob, blob


def test_opensearch_admin_is_match_all():
    r = OpenSearchRetriever(url="http://dummy:9200")
    assert r._acl_filter(_pred([], admin=True)) == {"match_all": {}}


def test_vertex_filter_constrains_groups_and_clearance():
    r = VertexSearchRetriever(project="p", data_store_id="d")
    f = r.compile_filter(_pred(["g-finance"], clearance=3))
    assert "owner_id: ANY(" in f and "doc_user_grants: ANY(" in f
    assert "min_clearance <= 3" in f
    assert "doc_grant_groups: ANY(" in f and "g-finance" in f
    assert "user_types_empty = true" in f and "projects_empty = true" in f


def test_vertex_groupless_user_cannot_match_group_path():
    r = VertexSearchRetriever(project="p", data_store_id="d")
    f = r.compile_filter(_pred([]))
    # a sentinel group nobody has -> the attribute branch can never match for a groupless user
    assert "__none__" in f


def test_vertex_admin_has_no_filter():
    r = VertexSearchRetriever(project="p", data_store_id="d")
    assert r.compile_filter(_pred([], admin=True)) is None

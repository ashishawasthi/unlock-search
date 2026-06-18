"""
ABAC: the security crown jewel. The POLICY MODEL (AccessPredicate) and the grant
rule live here in CORE. The SQL compiler below is shared verbatim by every SQL-family
backend (SQLite, PostgreSQL+pgvector, AlloyDB). Non-SQL retrievers (Agent Search on Gemini Enterprise Agent Platform,
OpenSearch) compile the SAME predicate to their own filter dialect in their adapter.

Grant if: admin OR owner OR a valid doc-scoped grant OR
  (clearance >= doc.min_clearance AND department intersects AND user-type allowed
   AND project allowed). Evaluated server-side, on every retrieval. Never in the UI.
"""
from __future__ import annotations

import time
from typing import Any

from core.ports.types import AccessPredicate, Principal

CLEARANCE = {"Public": 0, "Internal": 1, "Confidential": 2, "Restricted": 3, "Board-Only": 4}
CLEARANCE_LABEL = {v: k for k, v in CLEARANCE.items()}
USER_TYPES = ["employee", "partner", "customer", "admin"]


def clr_label(level) -> str:
    return CLEARANCE_LABEL.get(int(level if level is not None else 0), "Public")


def clr_level(v) -> int:
    if isinstance(v, str) and v in CLEARANCE:
        return CLEARANCE[v]
    try:
        n = int(v)
    except (TypeError, ValueError):
        return 0
    return max(0, min(4, n))


def build_predicate(p: Principal, now: float | None = None) -> AccessPredicate:
    return AccessPredicate(
        user_id=p.id, is_admin=bool(p.is_admin), clearance=int(p.clearance or 0),
        groups=list(p.groups or []), user_type=p.user_type or "employee",
        projects=list(p.projects or []), now=now if now is not None else time.time())


class SqlAclCompiler:
    """Implements AclCompiler.to_sql for the SQL family. Placeholders are '?'; each
    store translates to its paramstyle (SQLite native, psycopg '%s'). to_filter is
    not this compiler's job (retrievers own filter-DSL/DLS compilation)."""

    def to_sql(self, pred: AccessPredicate, table_alias: str = "d") -> tuple[str, list[Any]]:
        d = table_alias
        if pred.is_admin:
            return "1=1", []
        gph = ",".join("?" * len(pred.groups)) or "''"
        where = (
            f"({d}.owner_id = ?"
            f" OR EXISTS(SELECT 1 FROM doc_user_grants g WHERE g.doc_id={d}.id AND g.user_id=?"
            f"          AND (g.expires_at IS NULL OR g.expires_at > ?))"
            f" OR ({d}.min_clearance <= ?"
            f"     AND {d}.id IN (SELECT doc_id FROM doc_grants WHERE group_id IN ({gph}))"
            f"     AND (NOT EXISTS(SELECT 1 FROM doc_user_types t WHERE t.doc_id={d}.id)"
            f"          OR EXISTS(SELECT 1 FROM doc_user_types t WHERE t.doc_id={d}.id AND t.user_type=?))"
            f"     AND (NOT EXISTS(SELECT 1 FROM doc_projects p WHERE p.doc_id={d}.id)"
            f"          OR EXISTS(SELECT 1 FROM doc_projects p JOIN user_projects up ON up.project=p.project"
            f"                    WHERE p.doc_id={d}.id AND up.user_id=?))"
            f"    ))"
        )
        params = [pred.user_id, pred.user_id, pred.now, pred.clearance, *pred.groups,
                  pred.user_type, pred.user_id]
        return where, params

    def to_filter(self, pred: AccessPredicate) -> Any:
        raise NotImplementedError("SQL stores use to_sql; retrievers compile to_filter themselves")


SQL_ACL = SqlAclCompiler()


def can_access(store, pred: AccessPredicate, doc_id: str) -> bool:
    """Single-document check. Always evaluated against the relational store (which
    holds documents + ACL side-tables in every profile), so it is uniform across targets."""
    where, params = SQL_ACL.to_sql(pred)
    rows = store.execute(f"SELECT 1 FROM documents d WHERE d.id=? AND {where}", (doc_id, *params))
    return bool(rows)

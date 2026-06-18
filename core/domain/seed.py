"""
Seed the demo corpus's identities (provider-agnostic; writes via the store port).
Idempotent. Same three personas as the AI Box demo: Alice (admin, all depts,
Restricted), Bob (employee, Finance, Internal), Carol (partner, External, Public).
Password == username for the local demo only.
"""
from __future__ import annotations

from core.domain.abac import CLEARANCE
from core.domain.auth import hash_password

GROUPS = ["Finance", "Legal", "Engineering", "HR", "Partners", "External"]

# (id, name, username, email, role, user_type, is_admin, clearance, [departments], [projects])
PERSONAS = [
    ("u-alice", "Alice Reed", "alice", "alice@northwind.com", "Platform Admin / Owner",
     "admin", 1, "Restricted", GROUPS, ["Project-Atlas"]),
    ("u-bob", "Bob Ng", "bob", "bob@northwind.com", "Finance Analyst",
     "employee", 0, "Internal", ["Finance"], []),
    ("u-carol", "Carol Diaz", "carol", "carol@acme-partner.com", "External Partner",
     "partner", 0, "Public", ["External"], []),
]


def seed(store) -> None:
    for g in GROUPS:
        store.execute("INSERT OR IGNORE INTO groups(id,name) VALUES(?,?)", ("g-" + g.lower(), g))
        store.execute("INSERT OR IGNORE INTO folders(id,name,dept,parent_id) VALUES(?,?,?,NULL)",
                      ("f-" + g.lower(), g, g))
    for uid, name, username, email, role, utype, adm, clr, depts, projs in PERSONAS:
        if store.execute("SELECT 1 FROM users WHERE id=?", (uid,)):
            continue
        store.execute(
            "INSERT INTO users(id,name,username,email,role,user_type,clearance,password_hash,is_admin,is_active) "
            "VALUES(?,?,?,?,?,?,?,?,?,1)",
            (uid, name, username, email, role, utype, CLEARANCE[clr], hash_password(username), adm))
        for g in depts:
            store.execute("INSERT OR IGNORE INTO user_groups(user_id,group_id) VALUES(?,?)", (uid, "g-" + g.lower()))
        for p in projs:
            store.execute("INSERT OR IGNORE INTO user_projects(user_id,project) VALUES(?,?)", (uid, p))

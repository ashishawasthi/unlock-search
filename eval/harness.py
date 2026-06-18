"""
Provider-agnostic RAG eval harness for gcp-unlock.

Runs the SAME golden set against ANY profile, two ways:
  1. in-process: build a core.container.Container (AIBOX_PROFILE selects local / onprem /
     gcp), ingest a tiny finance/HR/legal corpus through the real ingest path, then exercise
     the bound Retriever (Recall@k, MRR) and the shared agent loop core.agents.loop.run_rag_turn
     (answer keyword-coverage as a faithfulness proxy). This is the default.
  2. http: drive a running deployment over its HTTP API (login -> conversation -> messages).
     Retrieval internals are not exposed over HTTP, so http mode reports answer keyword-coverage
     and citation count; retrieval Recall@k/MRR are only computed in-process.

Metrics (see eval/README.md for definitions):
  retrieval : Recall@k, MRR  (over expected_doc_substrings matched against hit title/content)
  answer    : keyword-coverage (faithfulness proxy), grounded rate, citation count
If `ragas` is installed it is lazy-imported for richer answer metrics; the harness works without it.

Usage:
  python -m eval.harness                          # in-process, active AIBOX_PROFILE, k=8
  AIBOX_PROFILE=onprem python -m eval.harness      # in-process against the onprem container
  python -m eval.harness --k 5 --golden eval/golden.jsonl
  python -m eval.harness --http http://127.0.0.1:8000 --user alice --password alice
  python -m eval.harness --json                    # machine-readable summary on stdout

No em-dashes, straight quotes.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GOLDEN = REPO_ROOT / "eval" / "golden.jsonl"

# Tiny self-contained corpus (finance / HR / legal). Each document's text carries the
# facts the golden rows ask about, so the harness is reproducible with zero external data.
# Owned by the admin persona (u-alice) so retrieval sees the full corpus regardless of ABAC.
CORPUS: list[tuple[str, str]] = [
    ("Travel and Expense Policy", """## Travel and Expense Policy
This policy governs business travel and expense reimbursement for all employees.

## Hotels and Lodging
The company reimburses hotel stays up to 250 dollars per night for domestic travel.
Stays above this rate require prior approval from a manager. Receipts are mandatory.

## Approvals
Expense reports under 5000 dollars are approved by the line manager. Any expense report
above 5000 dollars requires director approval before reimbursement is processed.

## Meals
Daily meal allowance is capped at 60 dollars per day while travelling on company business.
"""),
    ("Q3 Financial Summary", """## Q3 Financial Summary
This document summarizes the third quarter financial results.

## Revenue
Total revenue for the third quarter was 12.4 million dollars, up 9 percent year over year.
Recurring subscription revenue accounted for 8.1 million dollars of the total.

## Board Review
The Q3 board financial review is scheduled for November 14 in the main boardroom. The board
will review revenue, margin, and the cash position before approving the Q4 plan.
"""),
    ("Employee Handbook", """## Employee Handbook
Welcome to the company. This handbook covers core HR policies.

## Vacation and Leave
Full-time employees accrue 20 paid vacation days per year. Unused days carry over up to a
maximum of 5 days into the next year.

## Parental Leave
New parents are entitled to 12 weeks of paid parental leave following the birth or adoption
of a child. Leave must be taken within the first year.

## Performance Reviews
Performance reviews are conducted twice per year, in June and December, for every employee.
"""),
    ("Remote Work Guidelines", """## Remote Work Guidelines
These guidelines describe eligibility and expectations for remote work.

## Eligibility
Employees become eligible for remote work after six months of continuous employment and with
manager approval. Remote arrangements are reviewed quarterly.

## Equipment
The company provides a laptop and a monthly stipend for home office expenses.
"""),
    ("Master Services Agreement", """## Master Services Agreement
This Master Services Agreement governs the provision of services between the parties.

## Term and Termination
Either party may terminate this agreement upon 30 days written notice to the other party.
Termination does not relieve either party of obligations accrued before the effective date.

## Limitation of Liability
The total liability of either party is limited to the fees paid in the twelve months preceding
the claim. Neither party is liable for indirect or consequential damages.

## Governing Law
This agreement is governed by the laws of the State of Delaware, without regard to conflict
of laws principles.
"""),
    ("Mutual NDA", """## Mutual Non-Disclosure Agreement
This Mutual NDA protects confidential information exchanged between the parties.

## Confidentiality
Each party shall keep the other's confidential information secret and use it only to evaluate
the proposed relationship.

## Survival
The confidentiality obligations survive termination of this agreement for a period of three years.
"""),
]


@dataclass
class Row:
    id: str
    question: str
    expected_doc_substrings: list[str]
    expected_answer_keywords: list[str]


def load_golden(path: Path) -> list[Row]:
    rows: list[Row] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        rows.append(Row(d["id"], d["question"], d.get("expected_doc_substrings", []),
                        d.get("expected_answer_keywords", [])))
    return rows


# ---- metric helpers ----
def _doc_matches(text: str, substrings: list[str]) -> bool:
    t = (text or "").lower()
    return any(s.lower() in t for s in substrings)


def recall_at_k(hit_texts: list[str], expected: list[str], k: int) -> float:
    """1.0 if any of the top-k hits matches an expected doc substring, else 0.0."""
    return 1.0 if any(_doc_matches(t, expected) for t in hit_texts[:k]) else 0.0


def reciprocal_rank(hit_texts: list[str], expected: list[str]) -> float:
    for i, t in enumerate(hit_texts):
        if _doc_matches(t, expected):
            return 1.0 / (i + 1)
    return 0.0


def keyword_coverage(answer: str, keywords: list[str]) -> float:
    if not keywords:
        return 1.0
    a = (answer or "").lower()
    hit = sum(1 for kw in keywords if kw.lower() in a)
    return hit / len(keywords)


# ---- in-process target (Container + shared agent loop) ----
def _seed_corpus(c, owner_id: str = "u-alice") -> None:
    """Ingest the tiny corpus through the real ingest path if it is not present yet."""
    from core.domain import ingest as ing
    store = c.store()
    existing = {r["title"] for r in store.execute("SELECT title FROM documents")}
    for title, body in CORPUS:
        if title in existing:
            continue
        try:
            ing.ingest(c, body.encode("utf-8"), title + ".md", title, owner_id)
        except ValueError:
            pass


def _admin_principal(c):
    from core.domain.auth import load_user, to_principal
    u = load_user(c.store(), "u-alice")
    if u:
        return to_principal(u)
    # fallback admin principal if the seed personas are absent
    from core.ports.types import Principal
    return Principal(id="eval-admin", is_admin=True, clearance=4, groups=[], user_type="admin")


def run_in_process(rows: list[Row], k: int, profile: str | None) -> list[dict]:
    from core.agents.loop import run_rag_turn
    from core.container import Container
    from core.domain.abac import build_predicate

    if profile:
        os.environ["AIBOX_PROFILE"] = profile
    c = Container()
    c.store().migrate()
    try:
        from core.domain.seed import seed
        seed(c.store())
    except Exception:
        pass
    _seed_corpus(c)
    principal = _admin_principal(c)
    pred = build_predicate(principal)            # AccessPredicate for the retriever port

    out: list[dict] = []
    for r in rows:
        t0 = time.time()
        hits = c.retriever().search(query=r.question, pred=pred, k=max(k, 8))
        hit_texts = [f"{h.title}\n{h.content}" for h in hits]
        res = run_rag_turn(c, principal, r.question, history=[], doc_ids=None, k=k)
        out.append({
            "id": r.id,
            "recall_at_k": recall_at_k(hit_texts, r.expected_doc_substrings, k),
            "mrr": reciprocal_rank(hit_texts, r.expected_doc_substrings),
            "coverage": keyword_coverage(res.answer, r.expected_answer_keywords),
            "grounded": 1.0 if res.grounded else 0.0,
            "n_cites": len(res.cites),
            "n_hits": len(hits),
            "answer": res.answer,
            "latency_ms": round((time.time() - t0) * 1000, 1),
        })
    return out


# ---- http target (drives a running deployment over its API) ----
def run_http(rows: list[Row], base_url: str, user: str, password: str, k: int) -> list[dict]:
    import httpx

    base = base_url.rstrip("/")
    with httpx.Client(base_url=base, timeout=60) as cl:
        r = cl.post("/api/auth/login", json={"username": user, "password": password})
        if r.status_code != 200:
            raise RuntimeError(f"login failed ({r.status_code}); is the corpus seeded and the user valid?")
        token = r.json()["token"]
        h = {"Authorization": f"Bearer {token}"}
        out: list[dict] = []
        for row in rows:
            t0 = time.time()
            conv = cl.post("/api/conversations", json={"doc_ids": []}, headers=h).json()
            msg = cl.post(f"/api/conversations/{conv['conv_id']}/messages",
                          json={"content": row.question}, headers=h)
            data = msg.json() if msg.status_code == 200 else {"answer": "", "cites": [], "grounded": False}
            answer = data.get("answer", "")
            cites = data.get("cites", [])
            cite_text = " ".join(f"{c.get('title','')} {c.get('section','')}" for c in cites)
            out.append({
                "id": row.id,
                "recall_at_k": recall_at_k([cite_text], row.expected_doc_substrings, k) if cites else float("nan"),
                "mrr": float("nan"),  # rank not exposed over HTTP
                "coverage": keyword_coverage(answer, row.expected_answer_keywords),
                "grounded": 1.0 if data.get("grounded") else 0.0,
                "n_cites": len(cites),
                "n_hits": len(cites),
                "answer": answer,
                "latency_ms": round((time.time() - t0) * 1000, 1),
            })
        return out


# ---- optional RAGAS enrichment (lazy, never required) ----
def maybe_ragas(rows: list[Row], results: list[dict]) -> dict | None:
    try:
        import ragas  # noqa: F401
    except Exception:
        return None
    # RAGAS needs contexts + reference answers we do not carry per row here; we surface a
    # ready-to-extend dataset shape and the keyword-coverage proxy so a richer run can plug in.
    return {"ragas_available": True,
            "note": "ragas detected; supply per-row contexts + ground_truth to compute "
                    "faithfulness / answer_relevancy / context_precision / context_recall."}


# ---- reporting ----
def _mean(xs: list[float]) -> float:
    xs = [x for x in xs if x == x]  # drop NaN
    return sum(xs) / len(xs) if xs else float("nan")


def print_table(results: list[dict], k: int, mode: str, profile: str) -> dict:
    cols = ["id", f"Recall@{k}", "MRR", "Coverage", "Grounded", "Cites", "ms"]
    widths = [10, 10, 6, 9, 9, 6, 8]
    print(f"\nRAG eval  (mode={mode}, profile={profile}, k={k}, n={len(results)})")
    print("  ".join(c.ljust(w) for c, w in zip(cols, widths)))
    print("  ".join("-" * w for w in widths))
    for r in results:
        cells = [r["id"], f"{r['recall_at_k']:.2f}", f"{r['mrr']:.2f}", f"{r['coverage']:.2f}",
                 f"{r['grounded']:.0f}", str(r["n_cites"]), f"{r['latency_ms']:.0f}"]
        print("  ".join(str(c).ljust(w) for c, w in zip(cells, widths)))
    summary = {
        "mode": mode, "profile": profile, "k": k, "n": len(results),
        f"recall_at_{k}": round(_mean([r["recall_at_k"] for r in results]), 4),
        "mrr": round(_mean([r["mrr"] for r in results]), 4),
        "answer_keyword_coverage": round(_mean([r["coverage"] for r in results]), 4),
        "grounded_rate": round(_mean([r["grounded"] for r in results]), 4),
        "avg_latency_ms": round(_mean([r["latency_ms"] for r in results]), 1),
    }
    print("  ".join("-" * w for w in widths))
    print(f"MEANS       Recall@{k}={summary[f'recall_at_{k}']:.2f}  MRR={summary['mrr']:.2f}  "
          f"Coverage={summary['answer_keyword_coverage']:.2f}  Grounded={summary['grounded_rate']:.2f}  "
          f"avg={summary['avg_latency_ms']:.0f}ms")
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Provider-agnostic RAG eval harness for gcp-unlock.")
    ap.add_argument("--golden", default=str(DEFAULT_GOLDEN), help="path to golden.jsonl")
    ap.add_argument("--k", type=int, default=8, help="retrieval cutoff for Recall@k")
    ap.add_argument("--profile", default=None, help="override AIBOX_PROFILE for in-process mode")
    ap.add_argument("--http", default=None, metavar="BASE_URL",
                    help="target a running deployment over HTTP instead of in-process")
    ap.add_argument("--user", default="alice", help="http login username")
    ap.add_argument("--password", default="alice", help="http login password")
    ap.add_argument("--json", action="store_true", help="print only the JSON summary")
    args = ap.parse_args(argv)

    rows = load_golden(Path(args.golden))
    if not rows:
        print("no golden rows found", file=sys.stderr)
        return 2

    if args.http:
        mode, profile = "http", args.http
        results = run_http(rows, args.http, args.user, args.password, args.k)
    else:
        mode = "in-process"
        profile = args.profile or os.environ.get("AIBOX_PROFILE", "local")
        results = run_in_process(rows, args.k, args.profile)

    summary = print_table(results, args.k, mode, profile)
    extra = maybe_ragas(rows, results)
    if extra:
        summary["ragas"] = extra
        print(f"\n[ragas] {extra['note']}")
    if args.json:
        print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

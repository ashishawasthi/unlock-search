---
marp: true
theme: default
paginate: true
size: 16:9
header: 'gcp-unlock: GCP-managed vs No-Lock-In On-Prem'
---

# AI Box to Enterprise Gen AI Assistant

## One Portable Codebase, Two Deployment Targets

### GCP-Managed (default) vs No-Lock-In On-Prem

Decision-grade comparison for enterprise architects and the exec sponsor.

All figures INDICATIVE (2026): order-of-magnitude planning numbers, not vendor quotes.

---

## Agenda

**Part 1 - Overview (high level).** What we are building, the architecture in one look, the two deployment topologies, the framing assumption, the locked decisions.

**Part 2 - Deep dive (detail).** Dimension by dimension: time-to-market, quality, cost / TCO, risk, security, sovereignty, scalability / ops / lock-in.

**Part 3 - The decision.** Weighted scorecard, sovereignty as a gate, risk register, where on-prem wins, the rebuttal, portability caveats, roadmap, recommendation.

INDICATIVE (2026).

---

## Bottom Line Up Front

- **Build the platform ONCE; deploy two ways from one codebase.**
- **Default to GCP-managed:** fastest time-to-market (~5x less assembly), best quality, lowest fully-loaded TCO (~35 to 40% of on-prem). Scorecard **4.67 vs 2.57 out of 5**.
- **Build the on-prem profile as insurance:** the no-lock-in / data-residency option, decisive only under a hard sovereignty mandate or already-sunk platform plus people.
- **Not a one-way door:** portability is an architectural property of the hexagonal CORE, not a deployment choice.

INDICATIVE (2026).

---

<!-- _class: lead -->

# Part 1 of 3

## Overview (high level)

---

## Executive Summary

- **Build the platform ONCE, deploy two ways.** A ports-and-adapters (hexagonal) design keeps a provider-agnostic CORE identical across both targets (server-side ABAC, structure-aware chunking, ACL-filtered retrieval plus neighbor continuation, citations, Validator groundedness gate, access-request workflow, append-only audit, single-page UI). Only the adapters and the platform beneath them change.
- **Default to GCP-managed.** It wins the three factors execs weigh most: best quality (Vertex AI Search reranker plus Document AI plus Gemini Flash), fastest time-to-market (~7 vs ~36 eng-weeks of platform assembly, ~5x), lowest fully-loaded TCO (~$2.1M vs ~$5.7M over 3 years).
- **The TCO gap is people, not gear.** With inference held equal (external in both), the differential is platform operations: on-prem carries ~4 to 6 FTE of undifferentiated heavy lifting; GCP absorbs it into per-unit pricing (~1.0 to 1.5 FTE to integrate, not operate).
- **Honest baseline.** The prototype today is SQLite plus local filesystem plus the Anthropic API. The ports-and-adapters refactor is the shared Year-0 build, costed ONCE in BOTH columns. That is why Year-0 is non-zero on both sides.
- **BUILD the on-prem profile to a tested baseline as deliberate insurance.** It is the no-lock-in / data-residency / air-gap-control option, honestly competitive under specific conditions (a hard sovereignty mandate, sunk idle capacity, or an existing under-utilized platform team).

---

## What We Are Building

An **enterprise Gen AI document assistant**: upload documents, find what you are allowed to see, and chat over your own files with cited answers.

| Capability | What it does |
|---|---|
| Ingest | Upload PDF / text; parse, structure-aware chunk, index on arrival |
| Access-aware search | Hybrid keyword + semantic, ABAC-filtered; restricted matches show a server-redacted card |
| RAG chat with citations | Grounded only in documents the user may see; inline [n] citations; Validator groundedness gate |
| Access-request workflow | One-click request, approver email, time-limited grant |
| Audit | Append-only log of every auth, search, view, and AI turn |

**Security invariant:** access is enforced server-side on EVERY retrieval (list, search, file, RAG), never in the UI.

INDICATIVE (2026).

---

## Architecture at a Glance: One Core, Swappable Adapters

Ports-and-adapters (hexagonal). The product is written once; the cloud underneath is a config choice.

| Layer | Content | Changes per target? |
|---|---|---|
| CORE | Domain (ABAC, chunking, retrieval, ingest, audit), agent prompts + graph, all /api routes, the UI | No (byte-identical) |
| PORTS | LLM, Embedder, Reranker, Retriever, Store, ObjectStore, Parser, Guardrail, DLP, EventBus, Telemetry, Identity, Orchestrator | No (interfaces) |
| ADAPTERS | One implementation set per target behind those ports | Yes |
| PROFILE | `AIBOX_PROFILE` = local / onprem / gcp selects the adapter set | The one switch |

- **~80% of the code never changes between targets** (CORE + UI + eval + tests).
- What differs: only `adapters/<target>/`, `profiles/<target>.yaml`, and `deploy/<target>/`.

INDICATIVE (2026).

---

## Deployment at a Glance: Two Topologies

Same request path, different platform beneath the ports. Inference is an external hosted endpoint in BOTH.

**GCP-managed:** Client to Apigee + Cloud Armor to Cloud Run (CORE) to Agent Engine / ADK, retrieving via Vertex AI Search + Document AI over AlloyDB + GCS; Model Armor, Cloud DLP, Eventarc, and Cloud Observability around it.

**No-lock-in on Kubernetes:** Client to Kong / Envoy + OIDC to the CORE pod to ADK on K8s, retrieving via OpenSearch + bge + Tika over PostgreSQL+pgvector + MinIO; Llama Guard + NeMo, Presidio, Knative / KEDA, and OTel + Grafana around it.

| Tier | GCP | On-prem |
|---|---|---|
| Gateway | Apigee X + Cloud Armor | Kong / Envoy + OIDC |
| Agent runtime | Vertex Agent Engine + ADK | ADK on K8s |
| Retrieval | Vertex AI Search + Document AI | OpenSearch + bge + Tika |
| Data | AlloyDB + GCS | pgvector + MinIO |
| Safety / DLP | Model Armor + Cloud DLP | Llama Guard + NeMo + Presidio |

INDICATIVE (2026).

---

## Framing Assumption: Inference Is External in BOTH Targets

> **No AI inference runs on-premise in either target.** Both call a hosted/managed model endpoint (GCP: Gemini via Vertex; on-prem: a hosted Gemma endpoint via LiteLLM). The comparison is the PLATFORM around the model, not the model.

| Implication | Consequence |
|---|---|
| Model-inference $ is comparable both sides | Excluded from the differential TCO (identical in both columns) |
| No inference GPU on either side | On-prem gets NO credit for "avoiding GPU spend" |
| No inference GPU ops on either side | On-prem gets NO penalty for "running GPUs" |
| The single model port is the seam | Swapping Gemini for Gemma is one adapter line, not a CORE change |

**Therefore the comparison is purely the platform:** gateway, guardrails, agent runtime, retrieval/index, parsing, DB, object store, eventing, DLP, observability, and the people who run it.

INDICATIVE (2026).

---

## Scope and Assumptions

| Area | Assumption behind the figures |
|---|---|
| Workload | Mid-size enterprise: ~500 to 2,000 internal users, ~1M documents, moderate bursty query volume, ~99.9% target |
| Cost basis | Fully-loaded headcount ~$160k to $280k per FTE; managed services as usage-based opex; all dollars INDICATIVE (2026), order-of-magnitude, not quotes |
| Inference | External hosted endpoint in BOTH targets; its cost is equal and excluded from the differential |
| In scope | The PLATFORM around the model (gateway, agent runtime, retrieval, parsing, DB, storage, eventing, DLP, safety, observability) plus the people to run it |
| Out of scope | Model fine-tuning, GPU capex / ops (no on-prem inference), data-migration specifics, org-specific compliance audits |
| Vendor figures | Model context window, durability, infoType counts are vendor-cited as of 2026: verify at procurement |

INDICATIVE (2026).

---

## Locked Architecture Decisions

| # | Decision (final) |
|---|---|
| 1 | **Agent layer = ADK in both.** GCP: Vertex AI Agent Engine + ADK. On-prem: same ADK app on K8s. The Orchestrator to Retriever to Generator to Validator graph and prompts live in CORE (`core/agents/`) and are reused; only model binding (Gemini vs Gemma via LiteLLM) and host differ. Local dev runs the SAME prompts in a lightweight in-process runner. |
| 2 | **Retrieval.** GCP: Vertex AI Search + Document AI; AlloyDB holds canonical chunk text plus ABAC side-tables (source of truth for neighbor-continuation and citation-to-source highlight). On-prem: OpenSearch (BM25 + kNN) + bge-reranker; PostgreSQL + pgvector holds chunk text plus ABAC. |
| 3 | **Validator / groundedness gate is a real step** before any answer is returned, in both targets. |
| 4 | **ABAC is a CORE-owned model (`AccessPredicate`), compiled per backend.** SQL compiler (SQLite/pgvector/AlloyDB), filter-DSL compiler (Vertex), DLS compiler (OpenSearch). Policy model plus orchestration is reused; the enforcement mechanism is per-adapter. NOT verbatim-identical SQL across targets. |
| 5 | **Embedder and Reranker are first-class ports.** Stated plainly: the on-prem profile depends on FOUR external hosted inference endpoints (LLM/Gemma, embedder, reranker/bge, safety/Llama Guard). GCP folds embedding + reranking into managed Vertex AI Search. |

INDICATIVE (2026).

---

## Scaffold Reality (Honest Current State)

| Profile | State today | What it proves |
|---|---|---|
| **Prototype (as built)** | SQLite + local filesystem + Anthropic API. No Postgres, no pgvector, no S3 adapter, no ADK graph, no port boundary yet. | The domain logic (ABAC, chunking, ACL-filtered retrieval, continuation, citations, audit) works end-to-end. |
| **`local` profile** | Runs end-to-end now (lightweight in-process runner of the shared prompts). | The CORE prompts and Orchestrator graph are real and runnable. |
| **`onprem` profile** | Code-complete (docker-compose). | The four hosted-inference ports and OSS adapters are wired, not aspirational. |
| **`gcp` profile** | Real adapter code; requires a GCP project to run. | Vertex / AlloyDB / Document AI adapters exist as code. |

- **The ports-and-adapters refactor is the shared Year-0 build.** It is borne ONCE and is common to both targets, so it nets out of the differential and explains the non-zero Year-0 on both sides.
- Reuse and portability below are stated as **designed intent realized in this repo**, not as a claim that the GCP profile is already in production.

INDICATIVE (2026).

---

## Side-by-Side Stack Mapping

Everything above the ports ships unchanged (ABAC policy model, chunking, citation, Validator gate, request/approval, audit, UI). Only the rows below differ by profile.

| Reference component (CORE port) | GCP-managed adapter | No-lock-in on-prem adapter |
|---|---|---|
| API gateway / edge | Apigee X + Cloud Armor | Kong / APISIX / Envoy + ModSecurity/Coraza |
| Safety / guardrails / injection | Model Armor (injection + RAI + DLP-aware) | Llama Guard 4 + NeMo Guardrails + Presidio |
| Models / generation | Gemini via Vertex AI | Gemma via hosted OpenAI-compatible endpoint (LiteLLM) |
| Agent runtime (Orch/Retr/Gen/Validator) | Vertex AI Agent Engine + ADK (managed) | Same ADK app on K8s (self-managed) |
| Retrieval + index + rerank | Vertex AI Search (hybrid RRF + reranker) | OpenSearch (BM25 + kNN) + bge-reranker-v2-m3 |
| Doc parsing / layout / OCR | Document AI Layout Parser | Tika / Unstructured + Tesseract/PaddleOCR |
| PII / DLP redaction | Cloud DLP / Sensitive Data Protection | Microsoft Presidio |
| Event-trigger ingestion | Cloud Functions (2nd gen) + Eventarc | Knative / KEDA / Argo Events + broker |
| Object / blob store | Google Cloud Storage (GCS) | MinIO (S3-compatible, erasure-coded) |
| Structured + vector store | AlloyDB (ScaNN, pgvector) | PostgreSQL 16 + pgvector (self-run HA) |
| Observability + eval | Cloud Observability + Vertex Gen AI Eval | OTel + Prometheus + Grafana + Langfuse/RAGAS/Phoenix |

---

## Stack Mapping: The Reuse Win

- **Embedder + Reranker are ports, not buried config.** On-prem must host them as separate inference endpoints; GCP folds both into managed Vertex AI Search.
- **Operational-surface fact:** the on-prem profile depends on **FOUR external hosted inference endpoints** (LLM/Gemma, embedder, reranker/bge, safety/Llama Guard); GCP bundles embedding + reranking + safety into the managed plane and calls one model endpoint.
- **One Postgres-compatible repository** serves both targets (AlloyDB is Postgres-wire-compatible). The same model port, the same ADK agent graph, the same ABAC policy model.
- **Many "on-prem vs GCP" choices collapse to a connection string plus a one-line index-strategy choice** (`USING scann` vs `USING hnsw`), plus the per-backend ABAC compiler.
- **ABAC nuance:** the policy model and orchestration are reused; the enforcement mechanism is compiled per adapter (SQL / Vertex filter-DSL / OpenSearch DLS). It is NOT identical SQL everywhere.

INDICATIVE (2026).

---

<!-- _class: lead -->

# Part 2 of 3

## Deep Dive: Dimension by Dimension (detail)

---

## Dimension: Time-to-Market

Measures platform assembly plus hardening only (inference external both ways; CORE identical both ways). Deltas are relative effort bars, not point estimates.

| Capability | GCP | On-prem | Why on-prem is heavier |
|---|---|---|---|
| API gateway / authz edge | light | heavy | Kong HA, plugin tuning, cert rotation are DIY |
| Guardrails / injection | light | heavy | NeMo rails authored/tested; host Llama Guard endpoint |
| Retrieval / index / rerank | light | heaviest | OpenSearch sizing/sharding + separate reranker + fusion |
| Doc parsing / layout | light | heavy | Layout/table/OCR hand-tuned; scale the parser yourself |
| PII / redaction | light | heavy | Presidio recognizers, custom entities, FP/FN tuning |
| Event-trigger ingestion | light | heavy | Event mesh, scale-to-zero, retry/DLQ self-built |
| Object store | light | medium | Distributed MinIO, erasure coding, lifecycle |
| Structured + vector DB | light | heavy | Self-run HA Postgres: replication, failover, PITR |
| Eval / observability | light | heaviest | Four OSS tools to deploy, wire, dashboard, upgrade |
| Cross-cutting integration + security review | light | heavy | More parts = more seams, IAM, network policy, pen-test |
| **Total (assembly to prod)** | **~7 eng-weeks** | **~36 eng-weeks** | **~5x assembly effort on-prem** |

---

## Time-to-Market: Milestones

| Milestone | GCP | On-prem |
|---|---|---|
| First working slice | ~2 weeks | ~6 to 8 weeks |
| Production-hardened (HA, security, eval, observability) | ~6 to 8 weeks | ~5 to 7 months |
| Ongoing platform ops (SEPARATE recurring line) | low (config) | ~0.5 to 1.0 FTE steady-state |

- TTM total (~7 vs ~36 eng-weeks) is **one-time assembly only**. The ~0.5 to 1.0 FTE is the separate recurring ops line (see Cost / Talent slides), not double-counted.
- The shared CORE refactor (ports + adapters) is the Year-0 build common to both; the TTM **delta** here is platform assembly, not the CORE.
- On-prem also carries a recurring patch/upgrade tax across ~10 OSS systems plus four external inference endpoints to keep healthy.

**Verdict: GCP wins decisively (~5x).** Weeks vs quarters to a hardened platform. INDICATIVE (2026).

---

## Dimension: Quality

| Sub-dimension | GCP-managed | On-prem OSS | Edge |
|---|---|---|---|
| Retrieval (hybrid + rerank) | Vertex AI Search: managed RRF + tuned semantic reranker, improved by Google | OpenSearch BM25/kNN + bge-reranker, fusion self-wired/tuned | **GCP** |
| Layout-aware parsing | Document AI: tables, forms, reading order, OCR as a managed processor | Tika/Unstructured: good text, weaker tables/forms/scans without tuning | **GCP** |
| Model / generation | Gemini: large context (~1M, as of 2026), native tools, schema-constrained JSON, strong grounding | Gemma (hosted): solid floor, trails on long-context recall + nested structured output | **GCP** |
| Guardrail coverage | Model Armor: managed injection + jailbreak + RAI + DLP-aware, centrally updated | Llama Guard 4 + NeMo + Presidio: strong, but you author/maintain rails + host the guard | **GCP** |
| PII detection breadth | Cloud DLP: a large curated infoType library (100+), checksum/context validation | Presidio: strong on regex PII; NER quality = your model + recognizer tuning | **GCP** |
| Eval tooling | Vertex Gen AI Eval: turnkey faithfulness/relevance/precision/recall, autorater-graded | RAGAS + Langfuse + Phoenix: best-in-class OSS, but assembled and operated by you | **GCP (slight)** |

---

## Quality: The Honest Read

- **Where GCP clearly wins:** the reranker plus layout-parser combination raises the retrieval floor the entire RAG answer depends on, with zero tuning; Gemini > Gemma on grounded generation; guardrail/DLP attack-pattern coverage stays current without rail-authoring labor.
- **Honest counter-point:** a well-tuned OpenSearch + bge-reranker-v2-m3 is genuinely strong; the retrieval gap narrows once tuned. OSS eval (RAGAS/Langfuse/Phoenix) is legitimately excellent.
- **The durable gap is concentrated in the MODEL (Gemini vs Gemma) and the zero-tuning FLOOR**, not in measurement. The model gap cannot be closed by ops effort.
- **Cross-target caveat:** absolute eval scores stay backend-relative (different judge models/prompts). Regression gates must use normalized metric names/ranges, not absolute numbers.

**Verdict: GCP wins, and the gap is largest exactly where RAG answer quality is made.**

Model spec figures (e.g. ~1M context, infoType counts) are vendor-cited as of 2026: verify at procurement. INDICATIVE (2026).

---

## Dimension: Cost / TCO

Inference excluded (equal, external both sides). INDICATIVE annual run-rate (infra + people).

| Cost shape | GCP-managed | On-prem-on-K8s |
|---|---|---|
| Pricing model | Usage-based opex; scales toward zero when idle | License + 3-yr hardware amortization (platform/storage/DB only; NO inference GPU on either side) + people |
| HA/DR, patching, certs, upgrades | Provider-absorbed, priced into per-unit rate | Engineered, staffed, and drilled in-house |
| Platform/SRE headcount | ~1.0 to 1.5 FTE (integrate + observe) | ~4 to 6 FTE (operate substrate 24x7 + on-call) |
| Annual run-rate (ex-inference) | ~$350k to $700k | ~$1.1M to $2.2M |

- Headcount assumes **net-new hires**. If an existing under-utilized platform team absorbs it, the people delta shrinks materially (see "Where On-Prem Wins"). The two assumptions are mutually exclusive.
- On-prem also runs four external inference endpoints (LLM, embedder, reranker, safety); GCP consumes one model endpoint plus the managed bundle.

**Verdict: GCP wins. Headcount, not hardware, is the differentiator.** INDICATIVE (2026).

---

## Cost: Why the Headcount Gap Is Real

Each on-prem role is undifferentiated heavy lifting (none of it differentiates the product):

| On-prem role | Why required |
|---|---|
| K8s / platform engineer(s) | Cluster lifecycle, upgrades, autoscaling, add-ons, capacity |
| Search / DB specialist | OpenSearch shard/JVM/heap tuning; pgvector index + failover |
| Storage / eventing engineer | MinIO erasure-coding + rebuilds; Knative/KEDA/Argo health |
| Security engineer | CVE patching cadence, secrets, policy, DLP/guardrail upkeep, audit, attestations |
| On-call rotation | 24x7 needs >= 4 people to be humane and sustainable |

- On GCP the same coverage is the provider's job, baked into per-unit pricing and shared across the fleet.
- On-prem pays for it at full loaded salary and cannot share it. This is the line "open source is free" comparisons leave out.

INDICATIVE (2026).

---

## 3-Year TCO Summary

> INDICATIVE (2026), order-of-magnitude, not vendor quotes. Inference excluded from the differential (equal, external both sides). Headcount at loaded cost (~$160k to $280k/FTE).

| Line | GCP-managed | On-prem-on-K8s |
|---|---|---|
| Year 0 one-time (build / migration) | $0.15M | $0.45M |
| Year 1 run-rate (infra + people) | $0.53M | $1.65M |
| Year 2 run-rate (+10% GCP usage; on-prem flat, pre-provisioned) | $0.58M | $1.68M |
| Year 3 run-rate | $0.63M | $1.72M |
| **3-year TCO (infra + people, ex-inference)** | **~$1.9M** | **~$5.5M** |
| + shared inference (equal both, ~$60k/yr x 3) | +$0.18M | +$0.18M |
| **3-year fully-loaded TCO** | **~$2.1M** | **~$5.7M** |

- **GCP lands at ~35 to 40% of on-prem 3-year TCO.** The gap is dominated by people (~$0.75M cumulative on GCP vs ~$3.6M on-prem).
- Year-0 delta is platform assembly; the shared CORE refactor is equal in both and nets out of the differential.

---

## Dimension: Risk (Net Posture)

Net = exposure after typical mitigations.

| Risk factor | GCP-managed | On-prem OSS | Edge |
|---|---|---|---|
| Operational (systems to run, scale, fail over) | Low (managed) | High (~10 on-call surfaces + 4 inference endpoints) | **GCP** |
| Security-patch lag (CVEs) | Low (provider-side) | High (you own CVE-to-patch SLA across the stack) | **GCP** |
| Talent / key-person | Low-Med (common skills) | High (scarce OpenSearch + PG-HA + K8s-eventing depth) | **GCP** |
| Supply-chain (deps / images) | Low (managed surfaces) | High (transitive deps across ~10 projects) | **GCP** |
| Vendor lock-in | Med (adapter-isolated) | Low (portable OSS) | **On-prem** |
| Cost overrun | Med (needs budgets/quotas/alerts) | Med (pre-provision peak; step-function) | Tie |
| Data residency / sovereignty | Low-Med (region pin, provider-held) | Low (full physical control) | **On-prem** |

**Verdict: GCP wins on net risk.** Its top risks are policy- and architecture-governable; on-prem's top risks are governable only by continuous scarce labor. INDICATIVE (2026).

---

## Dimension: Security & Compliance

| Control | GCP-managed | On-prem OSS | Edge |
|---|---|---|---|
| Encryption / key management | CMEK via Cloud KMS, auto rotation, key-level audit | DIY KMS / Vault + KES; you own key handling | **GCP** |
| Network isolation | VPC Service Controls perimeter, IAM conditions, private endpoints | NetworkPolicy + mesh mTLS (SPIFFE/SPIRE) self-built | **GCP** |
| Inherited certifications | Provider compliance scope inherited (Assured Workloads) | Assemble + attest scanning, secrets, policy, audit yourself | **GCP** |
| Edge protection | Cloud Armor managed WAF (OWASP CRS) + L7 DDoS at Google edge | ModSecurity/Coraza WAF; perimeter DDoS solved outside gateway | **GCP** |
| Audit logging | Cloud Audit Logs feed the same CORE audit model | OTel/Prom/Grafana feed it with more wiring | **GCP** |
| ABAC invariant (server-side, every retrieval) | CORE policy model, compiled to Vertex filter-DSL | CORE policy model, compiled to OpenSearch DLS / SQL | Tie (by design) |
| Dev-auth `X-User` bypass disabled in prod | Apigee strips client-supplied identity headers | Gateway strips headers; trust only signed/mTLS identity | Tie (must-do both) |

**Verdict: GCP wins on built-in controls and inherited certification scope.** Both targets MUST hard-off the spoofable dev `X-User` fallback in production. INDICATIVE (2026).

---

## Dimension: Data Residency / Sovereignty

| Factor | GCP-managed | On-prem OSS | Edge |
|---|---|---|---|
| Physical location of data | Region-pinned, but in provider infrastructure | Fully in customer DC / cluster; control plane can be air-gapped | **On-prem** |
| "Data never leaves org boundary" mandate | Region pin + VPC-SC + CMEK; ultimate control is the cloud's | Full control; satisfies the strictest mandates | **On-prem** |
| Inference egress (both targets) | External hosted model call leaves the boundary | External hosted model call leaves the boundary | Tie (by assumption) |
| Control-plane air-gap | Not possible (managed control plane) | Possible for the platform (model call still external) | **On-prem** |

> **Important caveat:** the KEY ASSUMPTION is external inference in BOTH targets. A true air-gap that also forbids external model calls breaks BOTH designs equally and needs a different inference story (a model hosted inside the sovereign boundary).

**Verdict: On-prem wins.** The strongest, often decisive on-prem case. It is a constraint that justifies on-prem, not a cost saving. INDICATIVE (2026).

---

## Dimensions: Scalability, Ops, Lock-In (Condensed)

| Dimension | GCP-managed | On-prem OSS | Edge |
|---|---|---|---|
| Elasticity / capacity | Autoscale + scale-to-zero; usage-based, smooth | KEDA/Knative help, but reserved **platform** capacity (search/DB/storage nodes) wasteful when idle | **GCP** |
| Durability (object store) | GCS 11-nines durability (multi/dual-region classes), strong consistency | MinIO erasure-coding can match on paper; realized = ops maturity | **GCP** |
| Vector-retrieval at scale | AlloyDB ScaNN (vendor-cited faster vector queries vs pgvector HNSW on comparable workloads) | pgvector HNSW; solid, manual ef_search/m/lists tuning | **GCP** |
| Operational burden | None (no cluster); provider-absorbed patching | Continuous K8s + ~10 OSS + 4 inference endpoints to run/patch/drill | **GCP** |
| Vendor lock-in / portability | Adapter-isolated; data portable (pgvector; GCS S3-compatible API) | Maximal portability by construction | **On-prem** |
| Talent / skills | Common skills, ~1.0 to 1.5 FTE | Scarce depth, ~4 to 6 FTE | **GCP** |

Idle-capacity penalty is scoped to **platform nodes only; NO inference GPU on either side**. INDICATIVE (2026).

---

<!-- _class: lead -->

# Part 3 of 3

## The Decision

---

## Weighted Scorecard (Transparent)

Scores 1 to 5 (5 = best). Weights sum to 100. Weighted = (weight / 100) x score.

| Dimension | Weight | GCP | On-prem | GCP wtd | On-prem wtd |
|---|---|---|---|---|---|
| Time-to-market | 15 | 5 | 2 | 0.75 | 0.30 |
| Quality (retrieval/guardrails/eval/model) | 15 | 5 | 3 | 0.75 | 0.45 |
| Cost / TCO (fully loaded) | 15 | 5 | 2 | 0.75 | 0.30 |
| Operational burden | 12 | 5 | 2 | 0.60 | 0.24 |
| Risk (net posture) | 10 | 4 | 2 | 0.40 | 0.20 |
| Security & compliance | 8 | 5 | 3 | 0.40 | 0.24 |
| Scalability / reliability | 8 | 5 | 3 | 0.40 | 0.24 |
| Observability / eval maturity | 5 | 5 | 3 | 0.25 | 0.15 |
| Talent / skills | 5 | 4 | 2 | 0.20 | 0.10 |
| Data residency / sovereignty | 4 | 2 | 5 | 0.08 | 0.20 |
| Vendor lock-in / portability | 3 | 3 | 5 | 0.09 | 0.15 |
| **TOTAL** | **100** | | | **4.67** | **2.57** |

**Result: GCP 4.67 vs On-prem 2.57.** GCP leads every high-weight dimension; on-prem leads only the two lowest-weight ones. INDICATIVE (2026).

---

## Scorecard: Sovereignty as a Gate

The weighted score answers the *default* question. A hard residency mandate changes the question type.

| Scenario | How sovereignty is treated | Result |
|---|---|---|
| **No hard residency mandate** | Sovereignty is one weighted factor (weight 4) | **GCP wins** (4.67 vs 2.57) |
| **Hard residency mandate applies** | Sovereignty is a **pass/fail GATE**, not a weighted factor | **On-prem wins outright** (GCP is off the table regardless of price) |

- When data legally may not sit in a managed cloud, you do not weigh it: it is a gate, and only the on-prem profile clears it.
- Absent that gate, the weighted result is robust to reasonable weight changes.

> Sensitivity (checkable): gap = 4.67 - 2.57 = ~2.1. Doubling sovereignty (4 to 8) and lock-in (3 to 6) shifts ~+0.17 to GCP and ~+0.35 to on-prem, net ~-0.18 to the gap, leaving ~1.9 in GCP's favor. The conclusion holds.

INDICATIVE (2026).

---

## Risk Register (After Mitigation)

### On-prem / no-lock-in

| Risk | Net | Mitigation |
|---|---|---|
| Operational (~10 systems + 4 inference endpoints) | **High** | Hard to mitigate without heavy FTE + maturity |
| Security-patch lag (OSS CVEs) | **High** | Patch cadence + scanning you operate |
| Talent / key-person | **High** | Cross-training; but skills are scarce |
| Supply-chain (deps / image CVEs) | **High** | SBOM + image scanning you run |
| Integration brittleness (OSS seams) | **Med-High** | Pin versions; regression-test fusion/rerank/eventing |

### GCP-managed

| Risk | Net | Mitigation |
|---|---|---|
| Vendor lock-in | **Med** | Hexagonal CORE + portable data (pgvector; GCS S3-compatible API) |
| Cost overrun | **Med** | Billing budgets, quotas, alerts, Vertex eval sampling |
| Data residency / sovereignty | **Med** | Region pin + VPC-SC + CMEK (else use the on-prem profile) |
| Service deprecation / API change | **Low** | Provider lifecycle notices; adapter-isolated |

**Cross-cutting (both):** ABAC stays in CORE so neither runtime can leak access control into the model; OpenTelemetry is the portable telemetry seam; the spoofable dev `X-User` fallback must be hard-off in prod. INDICATIVE (2026).

---

## Honest Case: Where On-Prem Genuinely Wins

| Driver | Why on-prem is the right call | Caveat |
|---|---|---|
| **Hard data-sovereignty / residency mandate** | Data + metadata legally must never leave the org boundary; region pinning is insufficient. Strongest, often decisive case. | A constraint, not a saving. Justifies on-prem even at higher cost. |
| **Air-gapped / classified control plane** | No outbound connectivity to a public-cloud control plane permitted. | External inference is the KEY ASSUMPTION in BOTH; a true air-gap forbidding model calls breaks both designs and needs a different inference story. |
| **Existing idle datacenter capacity** | Hardware capex is sunk; marginal cost is power + a cluster slice (platform/storage/DB only; NO inference GPU either side). | Must be genuinely idle AND adequate (RAM for OpenSearch, fast disk for pgvector/MinIO). "Idle but old" still fails. |
| **Existing under-utilized platform team** | The dominant cost (people) is already paid with durable headroom; on-call already covers 24x7. | If you hire for this, the gap reopens immediately. |
| **Pre-existing OpenSearch / Postgres / K8s estate** | Marginal team + license cost is incremental, shared across workloads. | Only if this app is a small add to an already-funded platform. |

**Rule of thumb:** on-prem breaks even mainly when its two biggest lines (hardware AND people) are already sunk and shared, or when residency law removes GCP from contention. INDICATIVE (2026).

---

## Rebuttal: Why GCP Still Nets Out Ahead

| On-prem argument | Rebuttal grounded in this analysis |
|---|---|
| "Open source is free" | Inference is external both ways, so on-prem's only saving is moot, while it carries ~4 to 6 FTE of perpetual platform ops plus four external inference endpoints GCP bundles. 3-yr TCO ~$2.1M vs ~$5.7M. |
| "We avoid lock-in" | Neutralized by construction: GCP coupling lives only in adapters; CORE never imports a vendor SDK; data is portable (AlloyDB = Postgres-wire-compatible; GCS offers an S3-compatible API). |
| "We control our data" | Region pin + VPC-SC + CMEK cover most cases; for a genuine sovereignty mandate, relocate THAT workload to the on-prem profile, same product, no rewrite. |
| "We can match the quality" | Only with sustained tuning; the model gap (Gemini vs Gemma) and the managed reranker + layout-parser floor cannot be closed by ops. |
| "We can build it" | You can, in ~5x the eng-weeks and months later, then pay ~0.5 to 1.0 FTE forever to keep ~10 OSS systems plus four inference endpoints patched and healthy. |

**The clincher:** the same hexagonal design that makes on-prem possible is what makes GCP safe. Portability is an architectural property here, not a deployment choice. INDICATIVE (2026).

---

## Portability Caveats (Stated Plainly)

The exit path is real but not frictionless. Honest leak points:

| Area | The leak | Consequence |
|---|---|---|
| Object store API | GCS offers an S3-compatible API: common object ops covered, but some advanced S3 features differ (V4 signing with content-disposition, generation-vs-opaque version IDs, multipart edge cases) | A small provider-specific branch in the storage adapter |
| ABAC enforcement | One policy model, but compiled to different mechanisms (SQL / Vertex filter-DSL / OpenSearch DLS) | Security-critical reimplementation per adapter, validated separately |
| Retrieval chunking | GCP may auto-chunk inside Vertex AI Search; on-prem reuses CORE chunking | Chunk boundaries (and citation spans) are not byte-identical across targets |
| Eval scores | Different judge models/prompts per backend | Absolute scores not comparable; gates use normalized names/ranges |

**Net:** none of these flips the recommendation. They convert claims a hostile architect could falsify into claims that survive. INDICATIVE (2026).

---

## Delivery Roadmap (Phased)

Each phase is shippable; the demo spine comes first. The shared CORE refactor is Year-0, common to both targets.

| Phase | Ships | Profile | Milestone |
|---|---|---|---|
| 0. Carve-out | CORE + ports + composition root; local adapters | local | Full demo runs on SQLite/FTS5/pypdf (verified: 119 tests) |
| 1. GCP data + retrieval | AlloyDB, GCS, Document AI, Vertex AI Search, Eventarc | gcp | ABAC-filtered ingest + reranked retrieval on GCP |
| 2. GCP generation + edge | Gemini, Apigee / IAP, Cloud Observability + Vertex Eval | gcp | Full GCP RAG behind Apigee, metrics on a dashboard |
| 3. GCP safety | Model Armor, Cloud DLP | gcp | Injection blocked, PII redacted; GCP target complete |
| 4. On-prem data + retrieval | pgvector, MinIO, Tika, OpenSearch + bge, Knative/KEDA | onprem | Same demo on K8s; ABAC pushdown validated |
| 5. On-prem gen + safety + obs | Gemma (LiteLLM), Kong/OIDC, Llama Guard + NeMo + Presidio, OTel | onprem | Both targets pass the SAME conformance + eval suite |
| 6. Compare + harden | Cross-target eval, cost model, runbooks, exit-path test | both | This deck, backed by measured numbers |

INDICATIVE (2026).

---

## Final Recommendation

- **Default to GCP-managed.** It wins every high-weight dimension: fastest TTM (weeks vs quarters), best quality (reranker + layout parsing + Gemini), lowest fully-loaded TCO (~35 to 40% of on-prem), most governable risk. Scorecard **4.67 vs 2.57**.
- **BUILD the on-prem profile to a tested baseline as deliberate insurance** for the genuine trump cards: a hard data-sovereignty / residency or air-gap-control mandate, or an existing hardened platform + ops team you are obligated to reuse.
- **Govern GCP's real risks by policy:** billing budgets + quotas + alerts (cost), region pinning + VPC-SC + CMEK (residency), the hexagonal CORE (lock-in).

| De-risking property | Mechanism |
|---|---|
| Product never gets rewritten | One provider-agnostic CORE (ABAC, chunking, retrieval, Validator, citations, audit, UI) serves both targets. |
| Switching cost is bounded | Only adapters + profile change; ABAC policy, retrieval, and audit invariants are shared. |
| Data is portable | AlloyDB is Postgres-wire-compatible (dump/restore exit); GCS offers an S3-compatible API. |
| Telemetry is portable | OpenTelemetry SDK in CORE; swap the exporter, not the instrumentation. |
| Sovereign workloads relocate later | Start on GCP for speed + quality; move a regulated workload to on-prem when a mandate demands, without touching the product. |

**Bottom line:** with one portable CORE, choose GCP-managed now for the fastest, highest-quality, lowest-TCO path, knowing the architecture preserves a clean exit to on-prem for any future sovereignty obligation. Not a one-way door. All figures INDICATIVE (2026), order-of-magnitude for planning, not vendor quotes.

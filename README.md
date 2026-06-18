# unlock-search

A portable enterprise Gen AI document assistant: **upload, ABAC access-aware hybrid search,
RAG chat with citations, access-request approval, and audit**. One codebase, two production
targets: **GCP-managed** (lowest operational surface) or **on-prem Kubernetes** (full platform
control). The same domain logic, agent prompts, and HTTP API serve all three profiles;
only the adapters change.

## What it is

| Capability | Detail |
|---|---|
| Upload + ingest | Parse, page, chunk, embed, index. Documents are private to the uploader by default. |
| Access-aware search | Hybrid retrieval (lexical + vector). ABAC is **pushed into the query**, never post-filtered. |
| RAG chat with citations | Orchestrator -> Retriever -> Generator -> Validator. Every claim cites a numbered excerpt. |
| Groundedness gate | The Validator is a real step: it strips unsupported citations and refuses ungrounded answers before they return. |
| Access-request approval | Restricted documents surface as redacted cards; users request access, owners/approvers grant it. |
| Audit | Every retrieval, grant, and decision is logged. |

Deployable two ways from one codebase:

| Target | Posture | Why |
|---|---|---|
| **GCP** | Managed-first | Agent Search on Gemini Enterprise Agent Platform + Document AI fold embedding and reranking into one managed service. Smallest ops surface. |
| **on-prem / K8s** | No lock-in | OpenSearch + pgvector + bge-reranker on Kubernetes. Portable, but depends on hosted inference endpoints (see below). |

## Portability by design

**Build the product once; move it between GCP and on-prem by swapping only the adapters.**
The UI, domain logic, agent graph, and HTTP API are written once and never forked — the only thing
that varies per target is the ring of adapters bound below the ports.

### One CORE, one switch, three profiles

The same CORE is reused by every target. `UNLOCK_PROFILE` is the single switch that binds one
adapter per port; nothing in `core/` knows which cloud (if any) it is running on.

```mermaid
flowchart TB
    UI["Web UI + HTTP API"] --> CORE["CORE — written once, byte-identical<br/>ABAC · chunking · agent graph · prompts · audit"]
    CORE --> PORTS{{"Ports (Protocol interfaces)<br/>the only seam between CORE and the world"}}
    PORTS --> SW{"UNLOCK_PROFILE<br/>binds one adapter per port"}
    SW -->|"gcp"| GCP["adapters/gcp → Google Cloud (managed)<br/>AlloyDB · GCS · Gemini · Document AI<br/>Agent Search + Agent Runtime"]
    SW -->|"onprem"| ONP["adapters/onprem → Kubernetes (self-hosted)<br/>pgvector · MinIO · OpenSearch · Tika<br/>Gemma · bge · ADK"]
    SW -->|"local"| LOC["adapters/local → laptop, no cloud<br/>SQLite · FTS5 · filesystem · Anthropic"]
```

### What is reused vs what swaps

Everything above the ports ships unchanged across targets; only the adapters and the platform
beneath them differ. That shared band is the bulk of the codebase (CORE + UI + eval + tests).

```mermaid
flowchart TB
    subgraph SHARED["Shared and identical in every target — never forked"]
        direction TB
        S1["UI · HTTP API"]
        S2["CORE domain — ABAC · chunking · ingest · audit"]
        S3["Agent graph + prompts<br/>Orchestrator → Retriever → Generator → Validator"]
        S1 --- S2 --- S3
    end
    SHARED --> P{{"Ports — the contract both targets implement"}}
    P -->|"gcp profile"| GA["adapters/gcp"]
    P -->|"onprem profile"| OA["adapters/onprem"]
    GA --> GPL["Google Cloud<br/>managed services"]
    OA --> OPL["Kubernetes<br/>OSS + hosted inference"]
```

- **CORE imports no vendor SDK.** `core/` depends only on port `Protocol`s, so it *physically cannot* couple to a cloud — portability is enforced by the import graph, not by discipline.
- **One env var retargets everything.** `UNLOCK_PROFILE` picks an adapter set from a static `REGISTRY` allowlist; moving between GCP, on-prem, and local is a config change, not a code change.
- **Security and orchestration travel with the CORE.** The `AccessPredicate` and the Orchestrator → Retriever → Generator → Validator graph are built once in CORE and reused; only the per-backend ABAC compiler and the bound adapters differ.

## GCP-managed vs on-prem

Same codebase, same domain logic — the comparison is purely the **platform** around the model
(inference is an external hosted endpoint in **both** targets). Condensed below; the full
dimension-by-dimension analysis lives in the [comparison deck](https://raw.githubusercontent.com/ashishawasthi/unlock-search/main/docs/comparison/gcp-vs-onprem.pdf).

| Dimension | GCP-managed | On-prem | Edge |
|---|---|---|---|
| Time-to-market | Managed services; minimal assembly + hardening | DIY assembly + hardening of the full stack | **GCP** |
| Quality (retrieval, parsing, model, guardrails, eval) | Managed reranker + Document AI + Gemini; high floor with zero tuning | OpenSearch + bge + Tika + Gemma; strong but self-wired and self-tuned | **GCP** |
| Cost / TCO | Usage-based opex; scales toward zero; small integration team | License + hardware + a larger ops team | **GCP** |
| Operational burden & scalability | Provider-absorbed patching, autoscale, scale-to-zero | Self-run K8s + multiple OSS systems + four hosted inference endpoints | **GCP** |
| Risk, security & compliance | Managed controls (CMEK, VPC-SC, Cloud Armor) + inherited certifications | Equivalent controls, but assembled and operated by you | Tie |
| Data residency / sovereignty | Region pin + VPC-SC + CMEK; ultimate control is the cloud's | Full physical control; platform can be air-gapped | **On-prem** |

**Bottom line:** default to **GCP-managed** for the smallest operational surface; build **on-prem**
as insurance, decisive under a hard data-residency/air-gap mandate or an already-sunk platform + team.

## Ports-and-adapters principle

One CORE, swappable adapters, one switch.

- The CORE depends **only on port `Protocol`s** (`core/ports/`). No module under `core/` imports a
  vendor SDK (`google.cloud.*`, `vertexai`, `adk`, `opensearchpy`, `minio`, `presidio`, `anthropic`).
- Adapters implement the ports. A **profile** binds exactly one adapter per port via the static
  `REGISTRY` in `core/container.py` (an allowlist; YAML profiles pick a key, they cannot import
  arbitrary code).
- One environment variable selects everything:

```
UNLOCK_PROFILE = local | onprem | gcp
```

| Port | local | onprem | gcp |
|---|---|---|---|
| llm | Anthropic | Gemma (LiteLLM) | Gemini on Gemini Enterprise Agent Platform |
| embedder | noop / extractive | hosted endpoint | Gemini Enterprise Agent Platform |
| reranker | noop | bge (hosted) | Gemini Enterprise Agent Platform (folded in) |
| object_store | filesystem | MinIO | GCS |
| relational | SQLite | PostgreSQL + pgvector | AlloyDB |
| retriever | FTS5 | OpenSearch (BM25 + kNN) | Agent Search on Gemini Enterprise Agent Platform |
| parser | pypdf | Tika | Document AI |
| guardrail | noop | Llama Guard / NeMo | Model Armor |
| dlp | noop | Presidio | Cloud DLP |
| identity | dev header | OIDC | Apigee |
| orchestrator | in-process loop | ADK on K8s | Agent Runtime on Gemini Enterprise Agent Platform + ADK |

**Agent layer.** ADK is the agent runtime in both production targets (GCP: Agent Runtime on Gemini Enterprise Agent Platform
+ ADK; on-prem: ADK on K8s). The agent **prompts** and the Orchestrator -> Retriever -> Generator
-> Validator **graph** live in `core/agents/` and are reused by every runtime. Only the model
binding (Gemini vs Gemma via LiteLLM) and the host differ. Local dev runs a lightweight in-process
runner of the **same** prompts.

**Retrieval.**
- GCP: Agent Search on Gemini Enterprise Agent Platform + Document AI for best quality. AlloyDB holds the canonical chunk **text** +
  ABAC side-tables and is the source of truth for neighbor-continuation and citation -> source highlight.
- on-prem: OpenSearch (BM25 + kNN) + bge-reranker; PostgreSQL + pgvector holds chunk text + ABAC.

**ABAC.** ABAC is a CORE-owned model (`AccessPredicate`) compiled per backend: a shared SQL compiler
for SQLite/pgvector/AlloyDB, a filter-DSL compiler for Gemini Enterprise Agent Platform, and a DLS compiler for OpenSearch. The
**policy model + orchestration is reused**; the **enforcement mechanism is per-adapter**. It is not
verbatim-identical SQL across targets.

**Embedder and Reranker are first-class ports.** Consequence, stated plainly: the on-prem profile
depends on **four external hosted inference endpoints** (LLM/Gemma, embedder, reranker/bge,
safety/Llama Guard). There is **no on-prem GPU**. GCP folds embedding and reranking into managed
Agent Search on Gemini Enterprise Agent Platform, which is the core of the GCP operational-surface argument.

### The hexagon

```mermaid
flowchart LR
    subgraph CORE["CORE (provider-agnostic, never changes)"]
        direction TB
        DOM["Domain — ABAC (AccessPredicate), chunking,<br/>ingest + versioning, access requests, audit"]
        AG["Agents — prompts + the<br/>Orchestrator → Retriever → Generator → Validator graph"]
        API["HTTP API — FastAPI (core/api/app.py)"]
        PORTS{{"Ports (Python Protocols)"}}
        API --- DOM
        DOM --- AG
        DOM --- PORTS
        AG --- PORTS
    end

    UI["Web UI — single-page app (ui/),<br/>calls /api/* only"] -->|"HTTP + signed-in user"| API

    PORTS --- L["adapters/local<br/>SQLite, FTS5, filesystem, Anthropic"]
    PORTS --- O["adapters/onprem<br/>pgvector, OpenSearch, MinIO,<br/>Gemma, bge, ADK on K8s"]
    PORTS --- G["adapters/gcp<br/>AlloyDB, GCS, Gemini, Document AI,<br/>Agent Search + Agent Runtime"]

    PROF["profiles/*.yaml<br/>UNLOCK_PROFILE → one adapter per port"] -.->|binds| PORTS
```

## Repo layout

```
core/        provider-agnostic domain, agents, api, ports, schema
  ports/       port Protocols (__init__.py) + neutral types (types.py)
  agents/      shared prompts + the Orchestrator->Retriever->Generator->Validator graph
  schema/      canonical, dialect-neutral reference schema (schema.sql)
  api/         FastAPI app factory (create_app)
  container.py composition root: static REGISTRY allowlist, one adapter per port
adapters/
  local/       SQLite, FTS5, filesystem, Anthropic, in-process orchestrator
  gcp/         AlloyDB, Agent Search on Gemini Enterprise Agent Platform, GCS, Gemini, Document AI, Agent Runtime
  onprem/      pgvector, OpenSearch, MinIO, Gemma, bge, ADK-on-K8s
profiles/      local.yaml | onprem.yaml | gcp.yaml (bind adapters per port)
deploy/        K8s manifests + docker-compose for the onprem profile
infra/         settings/profile loader (infra/settings.py), env interpolation
ui/            single-page UI
eval/          groundedness / retrieval eval harness
tests/         core + adapter tests
docs/          ARCHITECTURE.md, comparison/gcp-vs-onprem.md
```

## Quick start (local profile)

Runs end-to-end now, on a laptop, no cloud account.

```
pip install -r requirements.txt
UNLOCK_PROFILE=local uvicorn core.api.app:create_app --factory --reload
```

Serves http://127.0.0.1:8000.

- Set `ANTHROPIC_API_KEY` for LLM-generated answers; without it the local profile falls back to an
  **extractive** answer (citations from retrieved excerpts, no generation).
- `local` uses SQLite + FTS5 + the filesystem + an in-process runner of the shared agent prompts.

## on-prem profile

```
UNLOCK_PROFILE=onprem  # stand up the stack via deploy/k8s/docker-compose.onprem.yml
```

Brings up PostgreSQL + pgvector, OpenSearch, and MinIO. It needs **hosted inference endpoints**
for the LLM (Gemma), embedder, reranker (bge), and safety (Llama Guard) configured in the profile.
**No on-prem GPU**: all four model calls go out to hosted endpoints.

## gcp profile

`UNLOCK_PROFILE=gcp` binds the Gemini Enterprise Agent Platform / AlloyDB / GCS / Document AI / Agent Runtime adapters. These are
real code and require a configured GCP project. Agent Search on Gemini Enterprise Agent Platform folds embedding and reranking into
one managed service, removing two of the four inference endpoints the on-prem profile must host.

## Current state (honest scaffold reality)

- The prototype is SQLite + local filesystem + Anthropic API today. This ports-and-adapters
  refactor is the shared **Year-0 build**, costed **once** and common to both targets.
- **local** profile: runs end-to-end now.
- **onprem** profile: code-complete (docker-compose).
- **gcp** adapters: real code, require a GCP project.

## Security invariant

**ABAC is enforced server-side on every retrieval.** The `AccessPredicate` is composed in CORE and
compiled into the backend's native filter (SQL `WHERE` fragment, Agent Search filter-DSL, or OpenSearch DLS)
and pushed **into** the query. Access is never post-filtered, never decided by the model, and never
enforced in the UI. Restricted documents are returned as server-side redacted cards. Secrets stay in
the environment (profiles reference them via `*_env` indirection; YAML never holds a secret).

## More

- Architecture deep-dive: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- GCP vs on-prem comparison: [docs/comparison/gcp-vs-onprem.md](docs/comparison/gcp-vs-onprem.md)

Render the comparison as a slide deck:

```
npx @marp-team/marp-cli docs/comparison/gcp-vs-onprem.md -o deck.pptx
```

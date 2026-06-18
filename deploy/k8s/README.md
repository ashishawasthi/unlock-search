# On-prem deployment (Docker Compose + Kubernetes)

The CORE app is one stateless image (`infra/Dockerfile`). The on-prem profile binds it to an
open-source data plane: PostgreSQL+pgvector, OpenSearch, MinIO, Tika, plus four hosted
inference endpoints. Retargeting is one env var: `UNLOCK_PROFILE=onprem`.

## 1. Local stand-up (Docker Compose)

Brings up the backing services and the app on one machine.

```bash
docker compose -f deploy/k8s/docker-compose.onprem.yml up --build
open http://localhost:8000
```

| Service | Port | Role |
|---|---|---|
| postgres (pgvector) | 5432 | relational store: chunk text, metadata, ABAC side-tables, embeddings |
| opensearch | 9200 | retriever: hybrid BM25 + kNN, ABAC pushed down as a filter |
| minio | 9000 (API), 9001 (console) | object store: original files, presigned URLs |
| tika | 9998 | rich document parsing (pypdf fallback if down) |
| app | 8000 | CORE FastAPI app, `UNLOCK_PROFILE=onprem` |

Works offline for upload, ABAC, and lexical search. RAG chat needs the LLM endpoint set.

### Hosted inference endpoints (need GPUs, not in compose)

Set these in your shell before `up`, or edit the `app` service env:

| Env var | Endpoint | Required |
|---|---|---|
| `GEMMA_ENDPOINT` -> `GEMMA_BASE_URL` | OpenAI-compatible `/v1` (vLLM / TGI / Ollama) | yes, for chat |
| `EMBED_ENDPOINT` | OpenAI-compatible `/v1/embeddings` (BGE) | for vector search |
| `RERANK_ENDPOINT` | hosted bge-reranker | optional (passthrough if unset) |
| `LLAMAGUARD_ENDPOINT` -> `LLAMAGUARD_BASE_URL` | hosted Llama Guard classifier | optional in dev |

Until `GEMMA_BASE_URL` is set the LLM raises `gemma-endpoint-unset`; everything else runs.

## 2. Real Kubernetes cluster

Apply the representative app manifests, then provision the data plane with operators.

```bash
kubectl apply -f deploy/k8s/app-deployment.yaml
```

`app-deployment.yaml` ships a Deployment (2 replicas, non-root, read-only rootfs), a Service,
a ConfigMap (non-secret wiring), and a Secret (placeholders). Build and push the image:

```bash
docker build -f infra/Dockerfile --build-arg EXTRAS=onprem -t registry.example.com/gcp-unlock:onprem .
docker push registry.example.com/gcp-unlock:onprem
```

### Platform components (production)

| Concern | Recommended on-prem component | Maps to |
|---|---|---|
| API gateway + OIDC | Kong or Envoy Gateway with the OIDC filter | attests principal, forwards `x-unlock-user` -> `OidcIdentity` |
| Relational store | CloudNativePG (PostgreSQL operator) + pgvector | `PgVectorStore` (`PG_DSN`) |
| Search | OpenSearch Operator (cluster + dashboards) | `OpenSearchRetriever` (`OPENSEARCH_URL`) |
| Object store | MinIO Operator (tenant + console) | `MinioObjectStore` (`MINIO_ENDPOINT`) |
| Autoscaling | KEDA (queue/HTTP scalers) or Knative Serving | stateless app replicas; `KnativeBus` eventing |
| Observability | OpenTelemetry Collector + Grafana / Langfuse | `OtelTelemetry` (`OTEL_EXPORTER_OTLP_ENDPOINT`) |
| Secrets | external-secrets or sealed-secrets backed by Vault | `gcp-unlock-secrets` |

### The 4 hosted inference endpoints

Run these as separate GPU-backed Deployments (or a shared inference gateway) and point the
Secret at them:

1. LLM (Gemma) -> `GEMMA_BASE_URL`
2. Embedder (BGE) -> `EMBED_ENDPOINT`
3. Reranker (bge-reranker) -> `RERANK_ENDPOINT`
4. Safety (Llama Guard) -> `LLAMAGUARD_BASE_URL`

### Security notes

- ABAC is enforced server-side on every retrieval (predicate pushed into the OpenSearch
  filter and the pgvector SQL); never post-filtered, never delegated to the model.
- The gateway is the trust boundary: the app trusts the `x-unlock-user` header only because the
  gateway terminated OIDC. Do not expose the Service without the gateway in front.
- Keep `gcp-unlock-secrets` out of git; the committed values are placeholders.

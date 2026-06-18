# GCP deployment (sketch)

The CORE app runs on Cloud Run with `UNLOCK_PROFILE=gcp`, bound to a fully managed Google
Cloud data plane. `main.tf` is a Terraform **sketch**: it enumerates the services and how they
wire together. It needs project-specific values (VPC, CMEK, AlloyDB connectivity, IAM, the
ingest function, Apigee/IAP) before it is production-ready.

## Service map (profiles/gcp.yaml -> managed service)

| Port | Adapter | Managed service | Terraform / config |
|---|---|---|---|
| object_store | `GcsObjectStore` | Cloud Storage (versioned, CMEK) | `google_storage_bucket.docs` |
| relational | `AlloyDbStore` (subclasses `PgVectorStore`) | AlloyDB (Postgres + pgvector + ScaNN) | `google_alloydb_*`, `ALLOYDB_DSN` secret |
| retriever | `AgentSearchRetriever` | Agent Search on Gemini Enterprise Agent Platform (Discovery Engine) | `google_discovery_engine_data_store.chunks` |
| parser | `DocAiParser` | Document AI (Layout Parser) | `google_document_ai_processor.layout` |
| event_bus | `EventarcBus` | Eventarc -> Cloud Function/Run ingest | `google_eventarc_trigger.on_upload` |
| orchestrator | `AgentRuntimeOrchestrator` | Agent Runtime on Gemini Enterprise Agent Platform (ADK) | platform-managed; SA IAM only |
| llm | `GeminiLLM` | Gemini Enterprise Agent Platform (Gemini) | platform-managed; `roles/aiplatform.user` |
| embedder | `GeapEmbedder` | Gemini Enterprise Agent Platform text embeddings | platform-managed |
| reranker | `GeapReranker` | Gemini Enterprise Agent Platform Ranking API | platform-managed |
| dlp | `CloudDLP` | Cloud DLP | `roles/dlp.user` |
| notifier | `PubSubNotifier` | Pub/Sub | `roles/pubsub.publisher` |
| identity | `ApigeeIdentity` | Apigee / IAP (OIDC at the edge) | gateway in front of Cloud Run |

**Inference is platform-managed.** Unlike on-prem (which needs 4 self-hosted GPU endpoints),
the LLM, embedder, reranker, and agent runtime are all Gemini Enterprise Agent Platform APIs. No inference infra to
run; you grant the Cloud Run service account `roles/aiplatform.user` and call the APIs.

## Deploy steps

1. **Build and push the image** (gcp extras):
   ```bash
   gcloud artifacts repositories create unlock --repository-format=docker --location=$REGION
   docker build -f infra/Dockerfile --build-arg EXTRAS=gcp \
     -t $REGION-docker.pkg.dev/$PROJECT/unlock/gcp-unlock:gcp .
   docker push $REGION-docker.pkg.dev/$PROJECT/unlock/gcp-unlock:gcp
   ```

2. **Enable APIs**: run, aiplatform, discoveryengine, documentai, alloydb, storage,
   secretmanager, dlp, pubsub, eventarc, cloudkms (if using CMEK).

3. **Apply Terraform**:
   ```bash
   cd deploy/gcp
   terraform init
   terraform apply -var project_id=$PROJECT -var region=$REGION \
     -var image=$REGION-docker.pkg.dev/$PROJECT/unlock/gcp-unlock:gcp
   ```

4. **Add secret versions out-of-band** (never in Terraform state):
   ```bash
   echo -n "$ALLOYDB_DSN" | gcloud secrets versions add unlock-alloydb-dsn --data-file=-
   echo -n "$JWT_SECRET"  | gcloud secrets versions add unlock-jwt-secret  --data-file=-
   ```

5. **Fill profile ids**: copy `data_store_id`, the Document AI `processor_id`, and the bucket
   name from `terraform output` into `profiles/gcp.yaml`.

6. **Deploy the ingest function** that Eventarc targets (parse -> chunk -> embed -> index),
   and put a gateway (Apigee or IAP) in front of the Cloud Run service.

## Security notes

- **CMEK**: set `default_kms_key_name` on the bucket and AlloyDB; grant the service agents the
  KMS encrypter/decrypter role on the key first.
- **Edge identity**: the app trusts the gateway-attested principal header. Do not expose the
  Cloud Run URL directly; require Apigee or IAP in front.
- **ABAC** is compiled to a Agent Search filter expression and pushed into the search request over
  the denormalized per-chunk ACL fields; never post-filtered, never delegated to the model.
- **Least privilege**: the IAM roles in `main.tf` are a working starting point; tighten per
  service before production.

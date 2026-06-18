###############################################################################
# gcp-unlock -- GCP deployment SKETCH (Terraform).
#
# THIS IS A SKETCH, NOT A TURNKEY MODULE. It enumerates the managed services the
# gcp profile binds to and how they connect. It needs project-specific values
# (CMEK keys, VPC + AlloyDB Auth Proxy / Private Service Connect, IAM bindings,
# Agent Search on Gemini Enterprise Agent Platform data store schema, Eventarc service account, Apigee/IAP). Treat
# every resource as a starting point to harden, not production-ready as written.
#
# Maps to profiles/gcp.yaml:
#   object_store: gcs        relational: alloydb     retriever: agentsearch (Discovery Engine)
#   parser: docai            event_bus: eventarc     orchestrator: agentruntime (Gemini Enterprise Agent Platform)
#   llm/embedder/reranker: platform-managed (no infra here; just IAM + the Cloud Run SA).
###############################################################################

terraform {
  required_version = ">= 1.5"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 5.0" }
  }
}

variable "project_id" {
  type = string
}
variable "region" {
  type    = string
  default = "us-central1"
}
variable "image" {
  type        = string
  description = "Cloud Run image, e.g. REGION-docker.pkg.dev/PROJECT/repo/gcp-unlock:gcp"
}
variable "kms_key" {
  type        = string
  default     = ""
  description = "CMEK key for GCS/AlloyDB (optional sketch)"
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# --- GCS bucket: original files (versioned; CMEK note) -----------------------
# CMEK: attach default_kms_key_name (a key in a Cloud KMS keyring) so every object
# is encrypted with a customer-managed key. Grant the GCS service agent the
# roles/cloudkms.cryptoKeyEncrypterDecrypter role on that key first.
resource "google_storage_bucket" "docs" {
  name                        = "${var.project_id}-unlock-docs"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = false

  versioning { enabled = true } # GcsObjectStore.list_versions relies on this

  dynamic "encryption" {
    for_each = var.kms_key == "" ? [] : [var.kms_key]
    content { default_kms_key_name = encryption.value }
  }
}

# --- AlloyDB: relational store (Postgres-wire; pgvector + ScaNN) -------------
# AlloyDbStore subclasses PgVectorStore; reach it via the AlloyDB Auth Proxy or
# Private Service Connect. The DSN goes into Secret Manager as ALLOYDB_DSN.
resource "google_alloydb_cluster" "main" {
  cluster_id = "unlock-cluster"
  location   = var.region
  # network_config { network = google_compute_network.vpc.id }  # add your VPC
}

resource "google_alloydb_instance" "primary" {
  cluster       = google_alloydb_cluster.main.name
  instance_id   = "unlock-primary"
  instance_type = "PRIMARY"
  machine_config { cpu_count = 2 }
}

# --- Agent Search on Gemini Enterprise Agent Platform: managed retriever data store -------------------------
# Discovery Engine data store. retriever_agentsearch.py indexes/searches here; ABAC is
# compiled to a Agent Search filter expression over denormalized per-chunk ACL fields.
resource "google_discovery_engine_data_store" "chunks" {
  location          = "global"
  data_store_id     = "unlock-datastore"
  display_name      = "unlock-chunks"
  industry_vertical = "GENERIC"
  content_config    = "NO_CONTENT" # structured/metadata records keyed by chunk_id
  solution_types    = ["SOLUTION_TYPE_SEARCH"]
}

# --- Document AI: layout parser processor -----------------------------------
# parser_docai.py calls a Layout Parser processor. Copy its id into
# profiles/gcp.yaml -> config.parser.processor_id.
resource "google_document_ai_processor" "layout" {
  location     = "us"
  display_name = "unlock-layout-parser"
  type         = "LAYOUT_PARSER_PROCESSOR"
}

# --- Eventarc -> Cloud Function: ingest on GCS finalize ----------------------
# A new object in the bucket triggers the ingest pipeline. The function (deployed
# separately) reads the object, parses, chunks, embeds, and indexes. Shown as the
# trigger wiring only; package the function and set its service URI.
resource "google_eventarc_trigger" "on_upload" {
  name     = "unlock-ingest-on-upload"
  location = var.region

  matching_criteria {
    attribute = "type"
    value     = "google.cloud.storage.object.v1.finalized"
  }
  matching_criteria {
    attribute = "bucket"
    value     = google_storage_bucket.docs.name
  }

  # Point at the ingest Cloud Function / Cloud Run service once deployed:
  destination {
    cloud_run_service {
      service = "unlock-ingest" # the ingest worker (deploy separately)
      region  = var.region
    }
  }
  service_account = google_service_account.app.email
}

# --- Cloud Run: the CORE app (UNLOCK_PROFILE=gcp) -----------------------------
resource "google_cloud_run_v2_service" "app" {
  name     = "gcp-unlock"
  location = var.region

  template {
    service_account = google_service_account.app.email
    containers {
      image = var.image
      ports { container_port = 8000 }

      env {
        name  = "UNLOCK_PROFILE"
        value = "gcp"
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = var.region
      }
      # Secrets injected from Secret Manager (see below).
      env {
        name = "ALLOYDB_DSN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.alloydb_dsn.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "UNLOCK_JWT_SECRET"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.jwt_secret.secret_id
            version = "latest"
          }
        }
      }
    }
  }
}

# --- Apigee / IAP note -------------------------------------------------------
# Front Cloud Run with Apigee (or IAP) for OIDC + the verified-principal headers
# the ApigeeIdentity adapter trusts. Do NOT expose the Cloud Run URL directly:
# the app trusts the gateway-attested identity header. Configure either:
#   - Apigee X proxy in front of the Cloud Run service, OR
#   - Identity-Aware Proxy on an external HTTPS load balancer -> Cloud Run.

# --- Secret Manager ----------------------------------------------------------
resource "google_secret_manager_secret" "alloydb_dsn" {
  secret_id = "unlock-alloydb-dsn"
  replication {
    auto {}
  }
}
resource "google_secret_manager_secret" "jwt_secret" {
  secret_id = "unlock-jwt-secret"
  replication {
    auto {}
  }
}
# Add secret VERSIONS out-of-band (do not put secret material in Terraform state):
#   echo -n "$DSN" | gcloud secrets versions add unlock-alloydb-dsn --data-file=-

# --- Service account + minimal IAM (sketch) ---------------------------------
resource "google_service_account" "app" {
  account_id   = "gcp-unlock-app"
  display_name = "gcp-unlock Cloud Run app"
}

# Grant the runtime SA the roles each managed service needs. Tighten to least
# privilege per service before production.
locals {
  app_roles = [
    "roles/storage.objectAdmin",          # GCS object store
    "roles/aiplatform.user",              # Gemini Enterprise Agent Platform: Gemini, embeddings, ranking
    "roles/discoveryengine.editor",       # Agent Search on Gemini Enterprise Agent Platform data store
    "roles/documentai.apiUser",           # Document AI parser
    "roles/alloydb.client",               # AlloyDB connect
    "roles/secretmanager.secretAccessor", # read secrets
    "roles/pubsub.publisher",             # notifier_pubsub
    "roles/dlp.user",                     # Cloud DLP
    "roles/eventarc.eventReceiver",       # Eventarc
  ]
}
resource "google_project_iam_member" "app" {
  for_each = toset(local.app_roles)
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.app.email}"
}

output "app_url" { value = google_cloud_run_v2_service.app.uri }
output "bucket" { value = google_storage_bucket.docs.name }
output "data_store" { value = google_discovery_engine_data_store.chunks.data_store_id }
output "docai_proc" { value = google_document_ai_processor.layout.id }

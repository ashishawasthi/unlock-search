"""
GCP Embedder: Vertex AI text embeddings (google-cloud-aiplatform / vertexai).

Backing service: a Vertex AI text-embedding model (default text-embedding-004, dim
768, matching the pgvector/AlloyDB vector(768) column). embed() maps kind to the
Vertex task type (document -> RETRIEVAL_DOCUMENT, query -> RETRIEVAL_QUERY) for
asymmetric retrieval quality. Batches are sent in groups under the API's per-request
instance cap.

Note: when retriever=vertex (Vertex AI Search auto-embeds and reranks server-side),
this embedder is typically UNUSED for the search path; it stays bound for profiles
that pair Vertex embeddings with a SQL vector store (AlloyDB/pgvector kNN).

Config / env (config.embedder in profiles/gcp.yaml):
  project, region (e.g. "us-central1"), model (default "text-embedding-004")

Importable without the Vertex SDK installed (lazy SDK imports).
"""
from __future__ import annotations

_DIM = 768
_TASK = {"document": "RETRIEVAL_DOCUMENT", "query": "RETRIEVAL_QUERY"}
_MAX_BATCH = 250


class VertexEmbedder:
    def __init__(self, project: str | None = None, region: str = "us-central1",
                 model: str = "text-embedding-004", dim: int = _DIM, **kw):
        self.project = project
        self.region = region
        self.model_name = model
        self._dim = dim
        self._model = None

    def _load(self):
        if self._model is None:
            import vertexai
            from vertexai.language_models import TextEmbeddingModel
            vertexai.init(project=self.project, location=self.region)
            self._TextEmbeddingInput = None
            try:
                from vertexai.language_models import TextEmbeddingInput
                self._TextEmbeddingInput = TextEmbeddingInput
            except ImportError:
                pass
            self._model = TextEmbeddingModel.from_pretrained(self.model_name)
        return self._model

    def embed(self, texts, *, kind: str = "document") -> list[list[float]]:
        if not texts:
            return []
        model = self._load()
        task = _TASK.get(kind, "RETRIEVAL_DOCUMENT")
        out: list[list[float]] = []
        items = list(texts)
        for i in range(0, len(items), _MAX_BATCH):
            batch = items[i:i + _MAX_BATCH]
            if self._TextEmbeddingInput is not None:
                inputs = [self._TextEmbeddingInput(text=t, task_type=task) for t in batch]
                resp = model.get_embeddings(inputs, output_dimensionality=self._dim)
            else:
                resp = model.get_embeddings(batch)
            out.extend(list(e.values) for e in resp)
        return out

    def dim(self) -> int:
        return self._dim

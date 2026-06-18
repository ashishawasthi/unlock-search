"""
On-prem Embedder: an OpenAI-compatible /v1/embeddings endpoint via httpx.

Backing service: any server exposing the OpenAI embeddings contract (vLLM, TEI,
text-embeddings-inference, LocalAI, Ollama-compat, etc.). Returns dense vectors used by
the OpenSearch kNN leg and (if wired) the pgvector column. dim() comes from config so the
relational/index schema width matches the model.

Config (profiles/onprem.yaml):
  endpoint_env: EMBED_ENDPOINT   -> endpoint: http://embeddings:8080/v1/embeddings (required)
  model: bge-base-en-v1.5        -> model id sent in the request (default bge-base-en-v1.5)
  dim: 768                       -> embedding width, must match the model (default 768)
  api_key_env: EMBED_API_KEY     -> bearer token (optional)
  timeout_s: 60
"""
from __future__ import annotations

from typing import Sequence


class HostedEmbedder:
    def __init__(self, endpoint: str = "", model: str = "bge-base-en-v1.5", dim: int = 768,
                 api_key: str = "", timeout_s: float = 60.0, **kw):
        if not endpoint:
            raise RuntimeError("HostedEmbedder needs endpoint (set EMBED_ENDPOINT; profiles use endpoint_env)")
        self.endpoint = endpoint
        self.model = model
        self._dim = int(dim or 768)
        self.api_key = api_key
        self.timeout_s = float(timeout_s)

    def dim(self) -> int:
        return self._dim

    def embed(self, texts: Sequence[str], *, kind: str = "document") -> list[list[float]]:
        items = list(texts)
        if not items:
            return []
        import httpx
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {"model": self.model, "input": items}
        try:
            r = httpx.post(self.endpoint, json=payload, headers=headers, timeout=self.timeout_s)
        except httpx.HTTPError as e:
            raise RuntimeError("embedder-unavailable") from e
        if r.status_code != 200:
            raise RuntimeError(f"embedder-error-{r.status_code}")
        data = r.json().get("data", [])
        # preserve request order (OpenAI returns an index per item).
        data = sorted(data, key=lambda d: d.get("index", 0))
        vecs = [list(d.get("embedding", [])) for d in data]
        if vecs and len(vecs[0]) != self._dim:
            self._dim = len(vecs[0])     # self-correct if config dim drifts from the model
        return vecs

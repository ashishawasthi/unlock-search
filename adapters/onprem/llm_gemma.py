"""
On-prem LLM: a HOSTED Gemma model reached over an OpenAI-compatible
/v1/chat/completions endpoint (vLLM / TGI / Ollama / a managed inference gateway).
No local GPU is owned by this process; the weights run behind GEMMA_BASE_URL.

Backing service + config:
  GEMMA_BASE_URL   base url of the OpenAI-compatible server (e.g. http://gemma-vllm.svc/v1)
  GEMMA_MODEL      model id served (default 'gemma-2-27b-it')
  GEMMA_API_KEY    optional bearer for the gateway (Authorization: Bearer ...)
Uses LiteLLM if installed (uniform OpenAI-compatible client) and falls back to a
direct httpx POST otherwise. Builds the same system+excerpts prompt via
core.domain.rag.build_context and honors the [n] citation contract via SYSTEM_PROMPT.
"""
from __future__ import annotations

import os

from core.domain.rag import build_context
from core.ports.types import LlmResult, ModelCapabilities, TokenUsage


class GemmaLLM:
    def __init__(self, base_url: str | None = None, model: str = "gemma-2-27b-it",
                 api_key: str | None = None, max_context_tokens: int = 32768, **kw):
        self.base_url = (base_url or os.environ.get("GEMMA_BASE_URL", "")).rstrip("/")
        self.model = os.environ.get("GEMMA_MODEL", model)
        self.key = api_key or os.environ.get("GEMMA_API_KEY", "")
        self.max_context_tokens = max_context_tokens

    def capabilities(self) -> ModelCapabilities:
        # Hosted Gemma: large-but-bounded context, no native tool-calling contract.
        return ModelCapabilities(max_context_tokens=self.max_context_tokens, supports_tools=False,
                                 supports_parallel_tools=False, strict_json=False,
                                 supports_streaming=True)

    def _messages(self, system: str, messages: list[dict], context_blocks) -> list[dict]:
        blocks = list(context_blocks or [])
        sys = system
        if blocks:
            sys = system + "\n\n=== DOCUMENT EXCERPTS ===\n" + build_context(blocks)
        return [{"role": "system", "content": sys}, *messages]

    def generate(self, *, system, messages, context_blocks=None, tools=None, tool_choice=None,
                 response_schema=None, max_tokens=2048, temperature=0.2, metadata=None) -> LlmResult:
        if not self.base_url:
            raise RuntimeError("gemma-endpoint-unset: set GEMMA_BASE_URL")
        msgs = self._messages(system, messages, context_blocks)
        # Prefer LiteLLM (uniform OpenAI-compatible client); lazy import.
        try:
            import litellm  # noqa: F401
            return self._via_litellm(msgs, max_tokens, temperature)
        except ImportError:
            return self._via_httpx(msgs, max_tokens, temperature)

    def _via_litellm(self, msgs, max_tokens, temperature) -> LlmResult:
        import litellm
        resp = litellm.completion(
            model=f"openai/{self.model}", messages=msgs, max_tokens=max_tokens,
            temperature=temperature, api_base=self.base_url,
            api_key=self.key or "sk-noauth")
        choice = resp["choices"][0]
        text = (choice["message"].get("content") or "")
        u = resp.get("usage", {}) or {}
        return LlmResult(
            text=text,
            stop_reason="max_tokens" if choice.get("finish_reason") == "length" else "stop",
            usage=TokenUsage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0),
                             u.get("total_tokens", 0)))

    def _via_httpx(self, msgs, max_tokens, temperature) -> LlmResult:
        import httpx
        headers = {"content-type": "application/json"}
        if self.key:
            headers["authorization"] = f"Bearer {self.key}"
        payload = {"model": self.model, "messages": msgs, "max_tokens": max_tokens,
                   "temperature": temperature}
        try:
            r = httpx.post(f"{self.base_url}/chat/completions", json=payload, headers=headers, timeout=60)
        except httpx.HTTPError as e:
            raise RuntimeError(f"llm-unavailable: {e}")
        if r.status_code != 200:
            raise RuntimeError(f"llm-error-{r.status_code}")
        data = r.json()
        choice = data["choices"][0]
        text = choice["message"].get("content") or ""
        u = data.get("usage", {}) or {}
        return LlmResult(
            text=text,
            stop_reason="max_tokens" if choice.get("finish_reason") == "length" else "stop",
            usage=TokenUsage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0),
                             u.get("total_tokens", 0)))

    def stream(self, **kw):
        # Simple non-incremental stream: yield the full text once (endpoints vary).
        yield self.generate(**kw).text

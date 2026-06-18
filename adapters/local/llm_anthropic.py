"""Local LLM: Anthropic Messages API, with an extractive fallback when no key is set
(so the demo runs offline). The [n] citation contract is honored by the prompt."""
from __future__ import annotations

import os

import httpx

from core.domain.rag import build_context
from core.ports.types import LlmResult, ModelCapabilities, TokenUsage


class AnthropicLLM:
    def __init__(self, model: str = "claude-sonnet-4-5", **kw):
        self.model = os.environ.get("AIBOX_MODEL", model)
        self.key = os.environ.get("ANTHROPIC_API_KEY", "")

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(max_context_tokens=200000, supports_tools=True,
                                 supports_streaming=True, strict_json=False)

    def generate(self, *, system, messages, context_blocks=None, tools=None, tool_choice=None,
                 response_schema=None, max_tokens=2048, temperature=0.2, metadata=None) -> LlmResult:
        blocks = list(context_blocks or [])
        if not self.key:
            body = "\n\n".join(f"[{i + 1}] (p.{b.page_no}) {b.content[:400]}" for i, b in enumerate(blocks[:4]))
            return LlmResult(text="(No ANTHROPIC_API_KEY configured -- extractive mode)\n\n"
                                  "Most relevant passages:\n\n" + body)
        payload = {"model": self.model, "max_tokens": max_tokens,
                   "system": system + "\n\n=== DOCUMENT EXCERPTS ===\n" + build_context(blocks),
                   "messages": messages}
        try:
            r = httpx.post("https://api.anthropic.com/v1/messages", timeout=60,
                           headers={"x-api-key": self.key, "anthropic-version": "2023-06-01"}, json=payload)
        except httpx.HTTPError:
            raise RuntimeError("llm-unavailable")
        if r.status_code != 200:
            raise RuntimeError(f"llm-error-{r.status_code}")
        data = r.json()
        text = "".join(b["text"] for b in data["content"] if b["type"] == "text")
        u = data.get("usage", {})
        return LlmResult(text=text,
                         stop_reason="max_tokens" if data.get("stop_reason") == "max_tokens" else "stop",
                         usage=TokenUsage(u.get("input_tokens", 0), u.get("output_tokens", 0)))

    def stream(self, **kw):
        yield self.generate(**kw).text

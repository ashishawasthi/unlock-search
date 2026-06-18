"""
GCP LLM adapter: Gemini on Gemini Enterprise Agent Platform (default model 'gemini-2.5-flash').

Backing service: Gemini Enterprise Agent Platform Generative Models. Needs a GCP project with the Gemini Enterprise Agent Platform
API enabled and ADC credentials (GOOGLE_APPLICATION_CREDENTIALS or workload identity).
Config kwargs: project, location, model. Env fallbacks: GOOGLE_CLOUD_PROJECT /
GCP_PROJECT, GOOGLE_CLOUD_LOCATION (default 'us-central1'), AIBOX_MODEL.

Mapping to the neutral LLM port:
  system            -> systemInstruction (with the RAG excerpts appended)
  messages          -> contents (role 'user'/'model', parts[].text)
  response_schema   -> generationConfig.responseSchema + responseMimeType json
The [n] citation contract is enforced by the prompt; excerpts are built by
core.domain.rag.build_context so grounding is identical to every other LLM.
"""
from __future__ import annotations

import os

from core.domain.rag import build_context
from core.ports.types import LlmResult, ModelCapabilities, TokenUsage


class GeminiLLM:
    def __init__(self, project: str | None = None, location: str | None = None,
                 model: str = "gemini-2.5-flash", **kw):
        self.project = project or os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT", "")
        self.location = location or os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
        self.model = os.environ.get("AIBOX_MODEL", model)
        self._model = None

    def _client(self):
        # lazy import: importable without the Gemini Enterprise Agent Platform SDK installed
        if self._model is None:
            import vertexai
            from vertexai.generative_models import GenerativeModel
            vertexai.init(project=self.project or None, location=self.location)
            self._model = GenerativeModel(self.model)
        return self._model

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(max_context_tokens=1_000_000, supports_tools=True,
                                 supports_parallel_tools=True, strict_json=True,
                                 supports_streaming=True)

    def _system(self, system: str, blocks) -> str:
        blocks = list(blocks or [])
        if blocks:
            return system + "\n\n=== DOCUMENT EXCERPTS ===\n" + build_context(blocks)
        return system

    def _contents(self, messages):
        # neutral 'assistant' -> Gemini 'model'; everything else is 'user'
        out = []
        for m in messages:
            role = "model" if m.get("role") == "assistant" else "user"
            out.append({"role": role, "parts": [{"text": str(m.get("content", ""))}]})
        return out

    def _gen_config(self, max_tokens, temperature, response_schema):
        cfg = {"max_output_tokens": max_tokens, "temperature": temperature}
        if response_schema:
            cfg["response_mime_type"] = "application/json"
            cfg["response_schema"] = response_schema
        return cfg

    def generate(self, *, system, messages, context_blocks=None, tools=None, tool_choice=None,
                 response_schema=None, max_tokens=2048, temperature=0.2, metadata=None) -> LlmResult:
        from vertexai.generative_models import Content, GenerationConfig, Part
        model_obj = self._client()
        # rebuild the model with the system instruction (Gemini Enterprise Agent Platform binds it per-model)
        sys_text = self._system(system, context_blocks)
        if sys_text != system or self._model is None:
            from vertexai.generative_models import GenerativeModel
            model_obj = GenerativeModel(self.model, system_instruction=sys_text)
        contents = [Content(role=c["role"], parts=[Part.from_text(c["parts"][0]["text"])])
                    for c in self._contents(messages)]
        gc = self._gen_config(max_tokens, temperature, response_schema)
        try:
            resp = model_obj.generate_content(
                contents, generation_config=GenerationConfig(**gc),
                tools=tools or None)
        except Exception as e:
            raise RuntimeError(f"gemini-error: {e}")
        text = getattr(resp, "text", "") or ""
        usage = getattr(resp, "usage_metadata", None)
        tu = TokenUsage(
            input_tokens=getattr(usage, "prompt_token_count", 0) if usage else 0,
            output_tokens=getattr(usage, "candidates_token_count", 0) if usage else 0,
            total_tokens=getattr(usage, "total_token_count", 0) if usage else 0)
        stop = "stop"
        cands = getattr(resp, "candidates", None) or []
        if cands:
            fr = str(getattr(cands[0], "finish_reason", "")).upper()
            if "MAX_TOKENS" in fr:
                stop = "max_tokens"
            elif "SAFETY" in fr or "BLOCK" in fr:
                stop = "content_filter"
        return LlmResult(text=text, stop_reason=stop, usage=tu)

    def stream(self, **kw):
        from vertexai.generative_models import Content, GenerationConfig, GenerativeModel, Part
        sys_text = self._system(kw.get("system", ""), kw.get("context_blocks"))
        model_obj = GenerativeModel(self.model, system_instruction=sys_text)
        contents = [Content(role=c["role"], parts=[Part.from_text(c["parts"][0]["text"])])
                    for c in self._contents(kw.get("messages", []))]
        gc = self._gen_config(kw.get("max_tokens", 2048), kw.get("temperature", 0.2),
                              kw.get("response_schema"))
        try:
            for ev in model_obj.generate_content(contents, generation_config=GenerationConfig(**gc),
                                                 stream=True):
                t = getattr(ev, "text", "")
                if t:
                    yield t
        except Exception as e:
            raise RuntimeError(f"gemini-stream-error: {e}")

"""Adapter for OpenAI-compatible chat completion servers.

Covers OpenAI itself, OpenRouter, Groq, llama.cpp, vLLM, LM Studio, Ollama's
/v1 endpoint and `mlx_lm.server` (the local distilled model).

Tool calls are read from the structured `tool_calls` field. Some local
servers instead leave the model's chat-template output in the text; for
those we accept exactly one format, the Hermes/Qwen `<tool_call>{json}</tool_call>`
block that the bundled local model is trained to emit. Nothing else in the
text is ever interpreted as a tool call.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from .base import Message, ModelInfo, ProviderError, StreamEvent, ToolSpec
from .tagstream import TagStream


LOCAL_PREFIXES = ("http://127.0.0.1:", "http://localhost:")


class OpenAICompatProvider:
    kind = "openai"

    def __init__(self, base_url: str, api_key: str = "", timeout: float = 600.0):
        self.temperature: float | None = None
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    # ------------------------------------------------------------------ format

    @staticmethod
    def to_wire(messages: list[Message]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        pending_images: list[dict[str, str]] = []

        def flush_images() -> None:
            # Tool messages can't carry images in the OpenAI format; show them
            # to vision models as a user message right after the tool results.
            if pending_images:
                out.append({"role": "user", "content": [{"type": "text", "text": "Screenshot(s) returned by the tool calls above:"}]
                            + [{"type": "image_url", "image_url": {"url": f"data:{i['media_type']};base64,{i['data']}"}}
                               for i in pending_images]})
                pending_images.clear()

        for m in messages:
            if m["role"] != "tool":
                flush_images()
            role = m["role"]
            if role == "assistant":
                msg: dict[str, Any] = {"role": "assistant", "content": m.get("content") or None}
                calls = m.get("tool_calls") or []
                if calls:
                    msg["tool_calls"] = [
                        {
                            "id": c["id"],
                            "type": "function",
                            "function": {"name": c["name"], "arguments": json.dumps(c.get("arguments") or {})},
                        }
                        for c in calls
                    ]
                if msg["content"] is None and not calls:
                    msg["content"] = ""
                out.append(msg)
            elif role == "tool":
                out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m.get("content", "")})
                pending_images.extend(m.get("images") or [])
            else:
                out.append({"role": role, "content": m.get("content", "")})
        flush_images()
        return out

    @staticmethod
    def tools_to_wire(tools: list[ToolSpec]) -> list[dict[str, Any]]:
        return [
            {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
            for t in tools
        ]

    # ------------------------------------------------------------------ calls

    async def stream(
        self,
        model: str,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        max_tokens: int,
        effort: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        body: dict[str, Any] = {
            "model": model,
            "messages": self.to_wire(messages),
            "stream": True,
            "max_tokens": max_tokens,
            "stream_options": {"include_usage": True},
        }
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if tools:
            body["tools"] = self.tools_to_wire(tools)
            body["tool_choice"] = "auto"
            if self.base_url.startswith(LOCAL_PREFIXES):
                # Ithaca's own model server constrains tool calls to a grammar. Let it
                # accept any real tool (the runner loads unloaded ones on use), not just
                # the ones described in this prompt, or the model is forced onto a wrong one.
                from ..tools.registry import registry

                body["ithaca_grammar_tools"] = self.tools_to_wire([t.spec() for t in registry.all()])

        partial: dict[int, dict[str, Any]] = {}
        usage: dict[str, int] = {}
        stop_reason = "end_turn"
        tags = TagStream(capture_tool_calls=bool(tools))

        async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=10.0)) as client:
            try:
                async with client.stream(
                    "POST", f"{self.base_url}/chat/completions", headers=self._headers(), json=body
                ) as resp:
                    if resp.status_code >= 400:
                        detail = (await resp.aread()).decode("utf-8", "replace")[:800]
                        if "generation thread died" in detail or resp.status_code == 503:
                            raise ProviderError(f"Local model server crashed ({detail[:120]}) — generation thread died.",
                                                status=resp.status_code)
                        if resp.status_code == 404 and "huggingface" in detail.lower():
                            raise ProviderError(
                                f"The local model server isn't serving “{model}”. Start it with "
                                f"`./ithaca model serve <folder>` and press refresh in Settings → Models.",
                                status=404,
                            )
                        raise ProviderError(
                            f"{resp.status_code} from {self.base_url}: {detail}",
                            status=resp.status_code,
                            retryable=resp.status_code in (408, 429) or resp.status_code >= 500,
                        )
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            chunk = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        if chunk.get("usage"):
                            u = chunk["usage"]
                            usage = {
                                "input_tokens": int(u.get("prompt_tokens") or 0),
                                "output_tokens": int(u.get("completion_tokens") or 0),
                            }
                        for choice in chunk.get("choices") or []:
                            delta = choice.get("delta") or {}
                            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                            if reasoning:
                                yield {"type": "thinking", "text": reasoning}
                            if delta.get("content"):
                                for ev in tags.feed(delta["content"]):
                                    yield ev
                            for tc in delta.get("tool_calls") or []:
                                slot = partial.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                                if tc.get("id"):
                                    slot["id"] = tc["id"]
                                fn = tc.get("function") or {}
                                if fn.get("name"):
                                    slot["name"] = fn["name"]
                                if fn.get("arguments"):
                                    slot["args"] += fn["arguments"]
                            if choice.get("finish_reason"):
                                stop_reason = _map_finish(choice["finish_reason"])
            except httpx.ConnectError as exc:
                hint = ""
                if self.base_url.startswith(("http://127.0.0.1:8080", "http://localhost:8080")):
                    hint = " The local model isn't running — start it with `./ithaca model serve base` (or restart with `./ithaca start`)."
                elif self.base_url.startswith(("http://127.0.0.1:8090", "http://localhost:8090")):
                    hint = " The local vision model isn't running — start it with `./ithaca model vision`."
                raise ProviderError(f"Could not connect to {self.base_url}.{hint}", retryable=False) from exc
            except httpx.HTTPError as exc:
                raise ProviderError(f"Could not reach {self.base_url}: {exc}", retryable=True) from exc

        for ev in tags.flush():
            yield ev

        calls = []
        for idx in sorted(partial):
            slot = partial[idx]
            calls.append({"id": slot["id"] or f"call_{idx}", "name": slot["name"], "raw_args": slot["args"]})
        calls.extend(tags.tool_calls)

        for i, c in enumerate(calls):
            args, error = _parse_args(c.get("raw_args"), c.get("arguments"))
            yield {
                "type": "tool_call",
                "id": c.get("id") or f"call_{i}",
                "name": c.get("name", ""),
                "arguments": args,
                "parse_error": error,
            }
        if calls and stop_reason == "end_turn":
            stop_reason = "tool_use"
        yield {"type": "done", "stop_reason": stop_reason, "usage": usage, "raw": {}}

    async def complete(self, model: str, messages: list[Message], *, max_tokens: int = 2048) -> str:
        text = []
        async for ev in self.stream(model, messages, [], max_tokens=max_tokens):
            if ev["type"] == "text":
                text.append(ev["text"])
        return "".join(text)

    async def list_models(self) -> list[ModelInfo]:
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                resp = await client.get(f"{self.base_url}/models", headers=self._headers())
            except httpx.HTTPError as exc:
                raise ProviderError(f"Could not reach {self.base_url}: {exc}", retryable=True) from exc
        if resp.status_code >= 400:
            raise ProviderError(f"{resp.status_code}: {resp.text[:300]}", status=resp.status_code)
        try:
            data = resp.json()
        except ValueError as exc:
            raise ProviderError(f"{self.base_url}/models returned an invalid response.") from exc
        items = data.get("data") if isinstance(data, dict) else data
        out = []
        for item in items or []:
            if isinstance(item, dict):
                mid = item.get("id")
                ctx = item.get("context_length") or item.get("max_model_len")
            else:
                mid, ctx = str(item), None
            if mid:
                out.append(ModelInfo(id=mid, context=ctx))
        return out

    async def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{self.base_url}/embeddings", headers=self._headers(), json={"model": model, "input": texts}
            )
        if resp.status_code >= 400:
            raise ProviderError(f"embeddings {resp.status_code}: {resp.text[:300]}", status=resp.status_code)
        rows = sorted(resp.json()["data"], key=lambda r: r.get("index", 0))
        return [r["embedding"] for r in rows]


def _map_finish(reason: str) -> str:
    return {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens", "content_filter": "refusal"}.get(
        reason, reason
    )


def _parse_args(raw: str | None, parsed: Any) -> tuple[dict[str, Any], str | None]:
    if isinstance(parsed, dict):
        return parsed, None
    if not raw or not raw.strip():
        return {}, None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, f"Arguments were not valid JSON ({exc.msg}). Raw: {raw[:300]}"
    if not isinstance(value, dict):
        return {}, "Arguments must be a JSON object."
    return value, None

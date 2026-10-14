"""mlx-lm's OpenAI-compatible server, with grammar-constrained tool calls.

Small models often emit tool calls that are almost right: a misspelled tool
name, a missing required argument, a string where an integer belongs, or
truncated JSON. This wrapper keeps prose free, but the moment the model opens
a tool call (Qwen's `<tool_call>` token) every following token is masked so
the call must be

    <tool_call>
    {"name": "<a real tool>", "arguments": <JSON matching that tool's schema>}
    </tool_call>

"A real tool" is any tool in the request's `ithaca_grammar_tools` field (Ithaca
sends all of its tools there; the prompt describes only the loaded ones), or
else in `tools`.

The masks come from llguidance (MIT) and are applied on the GPU. Requests
without tools, and models without Qwen-style tool tokens, are untouched.

    python constrained_server.py --model base --port 8080 ...   # same flags as `mlx_lm server`
    ITHACA_CONSTRAIN=0 ...                                       # disable (plain mlx_lm server)
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import sys
import threading
from typing import Any

import mlx.core as mx
import mlx_lm.server as server

log = logging.getLogger("ithaca.constrain")

TOOL_OPEN = "<tool_call>"
TOOL_CLOSE = "</tool_call>"
THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"

_state = threading.local()
_lock = threading.Lock()
_grammars: dict[str, str | None] = {}
_tokenizers: dict[tuple[int, int], Any] = {}
_stats = {"constrained": 0, "fallback": 0}


def _strict(schema: dict[str, Any]) -> dict[str, Any]:
    """Tool arguments are an object; unknown keys are not allowed unless the schema says so."""
    s = copy.deepcopy(schema or {"type": "object", "properties": {}})
    if s.get("type") == "object" and "additionalProperties" not in s:
        s["additionalProperties"] = False
    # compact JSON exactly as Qwen's template writes it; flexible whitespace lets a
    # small model pad a call with endless newlines
    s["x-guidance"] = {"whitespace_flexible": False, "item_separator": ", ", "key_separator": ": "}
    return s


def _lit(s: str) -> str:
    return json.dumps(s)


def _xml_param(key: str, schema: dict[str, Any]) -> str:
    """One `<parameter=key>` block, its value shaped by the schema type (as mlx-lm's qwen3_coder parser reads it)."""
    head, tail = _lit(f"<parameter={key}>\n"), _lit("\n</parameter>\n")
    ty = schema.get("type")
    if "enum" in schema and all(isinstance(v, str) for v in schema["enum"]):
        return f"{head} ({' | '.join(_lit(v) for v in schema['enum'])}) {tail}"
    if ty == "integer":
        return f"{head} /-?[0-9]+/ {tail}"
    if ty == "number":
        return f"{head} /-?[0-9]+(\\.[0-9]+)?/ {tail}"
    if ty == "boolean":
        return f'{head} ("true" | "false" | "True" | "False") {tail}'
    if ty in ("object", "array"):
        return f"{head} %json {json.dumps(_strict(schema) if ty == 'object' else schema)} {tail}"
    # free text: a lazy lexeme that ends at the first closing tag
    return None  # handled by the caller as a lazy rule


def build_grammar(tools: list[dict[str, Any]], fmt: str = "json", special: frozenset[str] = frozenset({TOOL_OPEN, TOOL_CLOSE})) -> str | None:
    """A Lark grammar: free text, where each tool call must be a valid call to one of `tools`.

    fmt "json" (Qwen3):   <tool_call>\\n{"name": …, "arguments": {…}}\\n</tool_call>
    fmt "xml"  (Qwen3.5): <tool_call>\\n<function=name>\\n<parameter=k>\\nvalue\\n</parameter>\\n</function>\\n</tool_call>
    Tags in `special` are single special tokens (referenced as tokens); the rest are plain
    text, matched lazily. An optional leading `…</think>` lets thinking models finish thinking.
    """
    def tag(t: str) -> str:
        return t if t in special else _lit(t)

    rules, calls = [], []
    for t in tools:
        fn = t.get("function") or t
        name = fn.get("name")
        if not name:
            continue
        i = len(calls)
        params = fn.get("parameters") or {}
        if fmt == "xml":
            props = params.get("properties") or {}
            required = set(params.get("required") or [])
            parts = []
            for j, (key, sch) in enumerate(props.items()):
                block = _xml_param(key, sch or {})
                if block is None:
                    rules.append(f'p{i}_{j}[lazy]: {_lit(f"<parameter={key}>" + chr(10))} /(.|\\n)*/ {_lit(chr(10) + "</parameter>" + chr(10))}')
                    block = f"p{i}_{j}"
                parts.append(block if key in required else f"({block})?")
            calls.append(f'{_lit(chr(10) + f"<function={name}>" + chr(10))} {" ".join(parts)} {_lit("</function>" + chr(10))}')
        else:
            head = _lit('\n{"name": ' + json.dumps(name) + ', "arguments": ')
            calls.append(f'{head} %json {json.dumps(_strict(params))} "}}\\n"')
    if not calls:
        return None
    body = " | ".join(f"call_{i}" for i in range(len(calls)))
    lines = ["%llguidance {}", "start: think? seg* TEXT", f"seg: pre_tool ({body}) {tag(TOOL_CLOSE)}"]
    lines.append(f"pre_tool: TEXT {TOOL_OPEN}" if TOOL_OPEN in special else f"pre_tool[lazy]: TEXT {_lit(TOOL_OPEN)}")
    open_think = f"{THINK_OPEN}? " if THINK_OPEN in special else ""
    lines.append(f"think: {open_think}TEXT {THINK_CLOSE}" if THINK_CLOSE in special else f"think[lazy]: TEXT {_lit(THINK_CLOSE)}")
    lines += [f"call_{i}: {c}" for i, c in enumerate(calls)] + rules
    lines.append("TEXT: /(.|\\n)*/")
    return "\n".join(lines) + "\n"


def tokenizer_traits(tok) -> tuple[str, frozenset[str]]:
    """The model's tool-call format and which tags are special tokens."""
    hf = tok._tokenizer
    template = getattr(hf, "chat_template", None) or getattr(tok, "chat_template", None) or ""
    if isinstance(template, dict):
        template = " ".join(template.values())
    fmt = "xml" if "<function=" in str(template) else "json"
    # llguidance treats every added token as special (its bytes carry a marker), so
    # a tag that is a single added token must be referenced as a token. Qwen3 and
    # Qwen3.5 both add <tool_call>, </tool_call> and </think> this way.
    added = {t.content for t in (getattr(hf, "added_tokens_decoder", {}) or {}).values()}
    special = frozenset(t for t in (TOOL_OPEN, TOOL_CLOSE, THINK_OPEN, THINK_CLOSE) if t in added)
    return fmt, special


def _grammar_for(tools: list[dict[str, Any]], fmt: str = "json", special: frozenset[str] = frozenset({TOOL_OPEN, TOOL_CLOSE})) -> str | None:
    key = hashlib.sha256(json.dumps([tools, fmt, sorted(special)], sort_keys=True).encode()).hexdigest()
    with _lock:
        if key not in _grammars:
            try:
                g = build_grammar(tools, fmt, special)
                if g is not None:
                    from llguidance import LLMatcher

                    err = LLMatcher.validate_grammar(g)
                    if err:
                        log.warning("tool grammar rejected, running unconstrained: %s", err)
                        g = None
                _grammars[key] = g
            except Exception as exc:  # noqa: BLE001 - never break generation over a grammar
                log.warning("could not build tool grammar, running unconstrained: %s", exc)
                _grammars[key] = None
        return _grammars[key]


def _ll_tokenizer(hf_tokenizer, eos_ids: list[int], n_vocab: int):
    import llguidance.hf

    k = (id(hf_tokenizer), n_vocab)
    with _lock:
        if k not in _tokenizers:
            _tokenizers[k] = llguidance.hf.from_tokenizer(hf_tokenizer, n_vocab=n_vocab, eos_token=eos_ids)
        return _tokenizers[k]


class ToolCallConstraint:
    """A logits processor (tokens, logits) -> logits enforcing the tool grammar."""

    def __init__(self, grammar: str, tokenizer) -> None:
        self.grammar = grammar
        self.tokenizer = tokenizer  # mlx_lm TokenizerWrapper
        self.matcher = None
        self.started = False
        self.disabled = False

    def _init(self, n_vocab: int) -> None:
        from llguidance import LLMatcher

        eos = sorted(self.tokenizer.eos_token_ids)
        llt = _ll_tokenizer(self.tokenizer._tokenizer, eos, n_vocab)
        self.matcher = LLMatcher(llt, self.grammar, log_level=0)
        if self.matcher.is_error():
            log.warning("tool grammar error, running unconstrained: %s", self.matcher.get_error())
            self.disabled = True

    def __call__(self, tokens: mx.array, logits: mx.array) -> mx.array:
        if self.disabled:
            return logits
        from llguidance.mlx import apply_token_bitmask
        from llguidance.numpy import allocate_token_bitmask, fill_next_token_bitmask

        try:
            if self.matcher is None:
                self._init(int(logits.shape[-1]))
                if self.disabled:
                    return logits
            if self.started:
                # the token sampled at the previous step is the last one in the context
                if not self.matcher.consume_token(int(tokens[-1].item())):
                    log.warning("constraint lost sync (%s); running unconstrained", self.matcher.get_error())
                    self.disabled = True
                    return logits
            self.started = True
            if self.matcher.is_stopped():
                return logits
            mask = allocate_token_bitmask(1, int(logits.shape[-1]))
            fill_next_token_bitmask(self.matcher, mask)
            out = apply_token_bitmask(logits.reshape(1, -1), mask)
            return out.reshape(logits.shape)
        except Exception as exc:  # noqa: BLE001
            log.warning("constraint failed, running unconstrained: %s", exc)
            self.disabled = True
            return logits


class ThinkBudget:
    """Cap how long a thinking model thinks: after `budget` tokens inside <think>,
    force `</think>` so it has to act. Generation starts inside the think block
    (the chat template opens it); once it closes, this does nothing."""

    def __init__(self, budget: int, close_id: int) -> None:
        self.budget, self.close_id = budget, close_id
        self.count, self.started, self.done = 0, False, False

    def __call__(self, tokens: mx.array, logits: mx.array) -> mx.array:
        if self.done:
            return logits
        if self.started and int(tokens[-1].item()) == self.close_id:
            self.done = True
            return logits
        self.started = True
        self.count += 1
        if self.count <= self.budget:
            return logits
        only_close = mx.arange(logits.shape[-1]) == self.close_id
        return mx.where(only_close, mx.zeros_like(logits), mx.full(logits.shape, -float("inf"), dtype=logits.dtype))


# ----------------------------------------------------------------- patches

_orig_make_lp = server._make_logits_processors
_orig_generate = server.ResponseGenerator.generate
_orig_handle = server.APIHandler.handle_completion


def _handle_completion(self, request, stop_words):
    # Ithaca sends every real tool in `ithaca_grammar_tools` (the prompt only
    # describes the loaded ones); calls may name any of them.
    body = self.body if isinstance(self.body, dict) else {}
    extra = body.get("ithaca_grammar_tools")
    if extra and getattr(request, "tools", None):
        request.ithaca_grammar_tools = extra
    request.ithaca_think_budget = int(body.get("ithaca_think_budget") or 0)
    return _orig_handle(self, request, stop_words)


def _generate(self, request, args, *a, **kw):
    # Hand the request's tools and the tokenizer to the logits-processor factory.
    # The model loads lazily in the generation thread, so pass the provider and
    # read its tokenizer when the logits processors are made (in that thread).
    args.ithaca_tools = getattr(request, "ithaca_grammar_tools", None) or getattr(request, "tools", None) or []
    args.ithaca_provider = self.model_provider
    args.ithaca_think_budget = getattr(request, "ithaca_think_budget", 0)
    return _orig_generate(self, request, args, *a, **kw)


def _make_logits_processors(args):
    procs = list(_orig_make_lp(args))
    budget = getattr(args, "ithaca_think_budget", 0)
    btok = getattr(getattr(args, "ithaca_provider", None), "tokenizer", None)
    if budget and btok is not None:
        close_id = btok._tokenizer.get_vocab().get(THINK_CLOSE)
        if close_id is not None:
            procs.insert(0, ThinkBudget(budget, close_id))
    tools = getattr(args, "ithaca_tools", None)
    provider = getattr(args, "ithaca_provider", None)
    tok = getattr(provider, "tokenizer", None) if tools else None
    if tools and tok is not None and TOOL_OPEN in tok._tokenizer.get_vocab():
        fmt, special = tokenizer_traits(tok)
        grammar = _grammar_for(tools, fmt, special)
        if grammar:
            procs.append(ToolCallConstraint(grammar, tok))
            _stats["constrained"] += 1
            if _stats["constrained"] == 1:
                print(f"ithaca: first constrained request ({len(tools)} tools)", file=sys.stderr, flush=True)
        else:
            _stats["fallback"] += 1
    return procs


def install() -> None:
    server._make_logits_processors = _make_logits_processors
    server.ResponseGenerator.generate = _generate
    server.APIHandler.handle_completion = _handle_completion


if __name__ == "__main__":
    if os.environ.get("ITHACA_CONSTRAIN", "1") != "0":
        install()
        print("ithaca: tool calls are grammar-constrained (llguidance)", file=sys.stderr, flush=True)
    server.main()

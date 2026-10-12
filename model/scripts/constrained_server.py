"""mlx-lm's OpenAI-compatible server, with grammar-constrained tool calls.

Small models often emit tool calls that are almost right: a misspelled tool
name, a missing required argument, a string where an integer belongs, or
truncated JSON. This wrapper keeps prose free, but the moment the model opens
a tool call (Qwen's `<tool_call>` token) every following token is masked so
the call must be

    <tool_call>
    {"name": "<one of the request's tools>", "arguments": <JSON matching that tool's schema>}
    </tool_call>

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
import threading
from typing import Any

import mlx.core as mx
import mlx_lm.server as server

log = logging.getLogger("ithaca.constrain")

TOOL_OPEN = "<tool_call>"
TOOL_CLOSE = "</tool_call>"

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


def build_grammar(tools: list[dict[str, Any]]) -> str | None:
    """A Lark grammar: free text, where each `<tool_call>` must be a valid call to one of `tools`.

    Qwen's chat template writes a call as `<tool_call>\\n{"name": …, "arguments": …}\\n</tool_call>`,
    where both tags are single special tokens; they're referenced as tokens here.
    """
    calls = []
    for t in tools:
        fn = t.get("function") or t
        name = fn.get("name")
        if name:
            head = json.dumps('\n{"name": ' + json.dumps(name) + ', "arguments": ')
            calls.append(f'{head} %json {json.dumps(_strict(fn.get("parameters") or {}))} "}}\\n"')
    if not calls:
        return None
    rules = "\n".join(f"call_{i}: {c}" for i, c in enumerate(calls))
    return (
        "%llguidance {}\n"
        "start: (TEXT tool_call)* TEXT\n"
        f"tool_call: {TOOL_OPEN} ({' | '.join(f'call_{i}' for i in range(len(calls)))}) {TOOL_CLOSE}\n"
        f"{rules}\n"
        "TEXT: /(.|\\n)*/\n"
    )


def _grammar_for(tools: list[dict[str, Any]]) -> str | None:
    key = hashlib.sha256(json.dumps(tools, sort_keys=True).encode()).hexdigest()
    with _lock:
        if key not in _grammars:
            try:
                g = build_grammar(tools)
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


# ----------------------------------------------------------------- patches

_orig_make_lp = server._make_logits_processors
_orig_generate = server.ResponseGenerator.generate


def _generate(self, request, args, *a, **kw):
    # Hand the request's tools and the tokenizer to the logits-processor factory.
    tools = getattr(request, "tools", None) or []
    args.ithaca_tools = tools
    args.ithaca_tokenizer = self.model_provider.tokenizer if tools else None
    return _orig_generate(self, request, args, *a, **kw)


def _make_logits_processors(args):
    procs = list(_orig_make_lp(args))
    tools = getattr(args, "ithaca_tools", None)
    tok = getattr(args, "ithaca_tokenizer", None)
    if tools and tok is not None and TOOL_OPEN in tok._tokenizer.get_vocab():
        grammar = _grammar_for(tools)
        if grammar:
            procs.append(ToolCallConstraint(grammar, tok))
            _stats["constrained"] += 1
        else:
            _stats["fallback"] += 1
    return procs


def install() -> None:
    server._make_logits_processors = _make_logits_processors
    server.ResponseGenerator.generate = _generate


if __name__ == "__main__":
    if os.environ.get("ITHACA_CONSTRAIN", "1") != "0":
        install()
        logging.getLogger().info("Ithaca: tool calls are grammar-constrained (llguidance)")
    server.main()

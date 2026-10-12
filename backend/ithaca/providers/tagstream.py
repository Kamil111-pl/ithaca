"""Incremental splitter for `<think>` and `<tool_call>` spans in streamed text.

Local chat templates (Qwen3, Hermes) put reasoning in `<think>…</think>` and,
when the server does not parse tool calls itself, emit
`<tool_call>{"name": ..., "arguments": {...}}</tool_call>`. This class routes
those spans out of the visible text while streaming, holding back only the
few characters that could be the start of a tag.
"""

from __future__ import annotations

import json
from typing import Any

_TAGS = ("think", "tool_call")


class TagStream:
    def __init__(self, capture_tool_calls: bool = True):
        self.capture_tool_calls = capture_tool_calls
        self.buf = ""
        self.inside: str | None = None
        self.span = ""
        self.tool_calls: list[dict[str, Any]] = []

    def _open_tags(self) -> list[str]:
        return [t for t in _TAGS if t != "tool_call" or self.capture_tool_calls]

    def feed(self, text: str) -> list[dict[str, Any]]:
        self.buf += text
        events: list[dict[str, Any]] = []
        while self.buf:
            if self.inside is None:
                idx, tag = self._find_open()
                if idx == -1:
                    keep = self._partial_prefix_len()
                    emit = self.buf[: len(self.buf) - keep]
                    if emit:
                        events.append({"type": "text", "text": emit})
                    self.buf = self.buf[len(emit):]
                    break
                if idx > 0:
                    events.append({"type": "text", "text": self.buf[:idx]})
                self.buf = self.buf[idx + len(tag) + 2:]
                self.inside = tag
                self.span = ""
            else:
                close = f"</{self.inside}>"
                idx = self.buf.find(close)
                if idx == -1:
                    keep = _suffix_overlap(self.buf, close)
                    chunk = self.buf[: len(self.buf) - keep]
                    self._emit_span(chunk, events)
                    self.buf = self.buf[len(chunk):]
                    break
                self._emit_span(self.buf[:idx], events)
                self.buf = self.buf[idx + len(close):]
                self._close_span()
        return events

    def flush(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        if self.inside is None:
            if self.buf:
                events.append({"type": "text", "text": self.buf})
        else:
            self._emit_span(self.buf, events)
            self._close_span()
        self.buf = ""
        return events

    # ----------------------------------------------------------------- helpers

    def _find_open(self) -> tuple[int, str]:
        best = (-1, "")
        for tag in self._open_tags():
            i = self.buf.find(f"<{tag}>")
            if i != -1 and (best[0] == -1 or i < best[0]):
                best = (i, tag)
        return best

    def _partial_prefix_len(self) -> int:
        longest = 0
        for tag in self._open_tags():
            longest = max(longest, _suffix_overlap(self.buf, f"<{tag}>"))
        return longest

    def _emit_span(self, chunk: str, events: list[dict[str, Any]]) -> None:
        if not chunk:
            return
        if self.inside == "think":
            events.append({"type": "thinking", "text": chunk})
        else:
            self.span += chunk

    def _close_span(self) -> None:
        if self.inside == "tool_call":
            raw = self.span.strip()
            call: dict[str, Any] = {"id": f"call_t{len(self.tool_calls)}", "name": "", "raw_args": ""}
            try:
                obj = json.loads(raw)
                call["name"] = str(obj.get("name", ""))
                args = obj.get("arguments", obj.get("parameters", {}))
                if isinstance(args, str):
                    call["raw_args"] = args
                else:
                    call["arguments"] = args
            except (json.JSONDecodeError, AttributeError):
                call["raw_args"] = raw
            self.tool_calls.append(call)
        self.inside = None
        self.span = ""


def _suffix_overlap(text: str, token: str) -> int:
    """Length of the longest suffix of `text` that is a proper prefix of `token`."""
    for n in range(min(len(token) - 1, len(text)), 0, -1):
        if text.endswith(token[:n]):
            return n
    return 0

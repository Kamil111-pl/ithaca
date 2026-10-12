import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="ithaca-test-"))
os.environ["ITHACA_DATA_DIR"] = str(_TMP / "data")
os.environ["ITHACA_WORKSPACE_DIR"] = str(_TMP / "workspace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from ithaca import config, db  # noqa: E402
from ithaca.tools import builtin  # noqa: E402,F401 - register tools


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.connect(tmp_path / "test.db")
    config.ensure_dirs()
    yield


class FakeProvider:
    """Replays a script of turns. Each turn is a list of stream events."""

    kind = "openai"

    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []

    async def stream(self, model, messages, tools, *, max_tokens, effort=None):
        self.requests.append({"messages": messages, "tools": [t.name for t in tools]})
        turn = self.turns.pop(0) if self.turns else [{"type": "text", "text": "done"}]
        for ev in turn:
            yield ev
        stop = "tool_use" if any(e["type"] == "tool_call" for e in turn) else "end_turn"
        yield {"type": "done", "stop_reason": stop, "usage": {"input_tokens": 1, "output_tokens": 1}, "raw": {}}

    async def complete(self, model, messages, *, max_tokens=2048):
        out = []
        async for ev in self.stream(model, messages, [], max_tokens=max_tokens):
            if ev["type"] == "text":
                out.append(ev["text"])
        return "".join(out)

    async def list_models(self):
        return []


def call(name, args, cid="c1"):
    return {"type": "tool_call", "id": cid, "name": name, "arguments": args, "parse_error": None}

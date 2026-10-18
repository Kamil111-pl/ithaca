import json

import pytest
from fastapi.testclient import TestClient

from ithaca import main, memory, providers

from .conftest import FakeProvider

H = {"X-Ithaca": "1"}


@pytest.fixture
def client(monkeypatch):
    with TestClient(main.app, base_url="http://localhost") as c:
        yield c


def test_auth_flow_and_guards(client):
    assert client.get("/api/conversations").status_code == 401
    assert client.post("/api/auth/setup", json={"password": "short"}, headers=H).status_code == 400
    assert client.post("/api/auth/setup", json={"password": "correct horse"}).status_code == 403  # no CSRF header
    r = client.post("/api/auth/setup", json={"password": "correct horse"}, headers=H)
    assert r.status_code == 200
    assert client.get("/api/conversations").status_code == 200
    client.post("/api/auth/logout", headers=H)
    assert client.post("/api/auth/login", json={"password": "nope nope"}, headers=H).status_code == 401
    assert client.post("/api/auth/login", json={"password": "correct horse"}, headers=H).status_code == 200


def test_rebinding_host_rejected(client):
    r = client.get("/api/health", headers={"Host": "evil.example"})
    assert r.status_code == 421


def test_chat_roundtrip_over_sse(client, monkeypatch):
    client.post("/api/auth/setup", json={"password": "correct horse"}, headers=H)
    fake = FakeProvider([[{"type": "text", "text": "Hello "}, {"type": "text", "text": "there"}]])
    providers.create("Fake", "openai", base_url="http://fake", default_model="m",
                     models=[{"id": "m", "tools": True}], provider_id="fake")
    monkeypatch.setattr(providers, "get_client", lambda pid: fake)

    conv = client.post("/api/conversations", json={"provider_id": "fake", "model": "m"}, headers=H).json()
    with client.stream("POST", f"/api/conversations/{conv['id']}/messages", json={"content": "hi"}, headers=H) as r:
        evs = [json.loads(line[5:]) for line in r.iter_lines() if line.startswith("data:")]
    assert "".join(e["text"] for e in evs if e["type"] == "text") == "Hello there"
    assert evs[-1]["type"] == "run_finished"

    full = client.get(f"/api/conversations/{conv['id']}").json()
    kinds = [i["kind"] for i in full["items"]]
    assert kinds == ["user", "assistant"]
    assert full["title"] == "hi"


@pytest.mark.asyncio
async def test_memory_hybrid_search():
    await memory.save("The user's favourite editor is Neovim")
    await memory.save("The user lives in Kraków and prefers metric units")
    await memory.save("Project Ithaca stores data on the Seagate drive")
    hits = await memory.search("which text editor do they like?", k=2)
    assert hits and "Neovim" in hits[0]["content"]
    hits = await memory.search("where are the files stored", k=1)
    assert "Seagate" in hits[0]["content"]


def test_documents_and_focus(client):
    client.post("/api/auth/setup", json={"password": "correct horse"}, headers=H)
    d = client.post("/api/documents", json={"title": "Plan", "content": "Sail home to Ithaca"}, headers=H).json()
    assert client.get("/api/documents", params={"q": "Ithaca"}).json()[0]["id"] == d["id"]
    import time

    now = time.time()
    client.post("/api/focus/sessions", headers=H, json={"mode": "focus", "label": "writing", "planned_s": 1500,
                                                         "actual_s": 1500, "completed": True, "started_at": now - 5, "ended_at": now})
    stats = client.get("/api/focus/stats").json()
    assert stats["today_seconds"] == 1500 and stats["streak"] == 1 and stats["labels"][0]["label"] == "writing"


def test_wake_phrase_parsing_and_speech_text():
    from ithaca import voice

    assert voice.parse_wake("Hey Ithaca, open Notes and write an essay.") == (True, "open Notes and write an essay.")
    assert voice.parse_wake("Ithica open the app") == (True, "open the app")
    assert voice.parse_wake("Hey Ithaca.") == (True, "")
    assert voice.parse_wake("Hayethika.") == (True, "")
    assert voice.parse_wake("I went to Ithaca last summer")[0] is False
    assert voice.parse_wake("What's the weather")[0] is False
    assert voice.speakable("## Done\n**Saved** [the doc](http://x).\n```py\nx=1\n```") == "Done Saved the doc. (code omitted)"


def test_voice_token_authorizes_only_voice_routes(client):
    from ithaca import voice

    tok = {"X-Ithaca-Voice-Token": voice.voice_token(), "X-Ithaca": "1"}
    assert client.get("/api/voice/status", headers=tok).status_code == 200
    assert client.get("/api/conversations", headers=tok).status_code == 401
    assert client.get("/api/voice/status", headers={"X-Ithaca-Voice-Token": "wrong"}).status_code == 401


def test_destructive_shell_commands_need_approval():
    from ithaca import security
    from ithaca.tools.registry import ToolContext, registry

    run = registry.get("run_command")
    ctx = ToolContext(run_id="r", conversation_id="c")
    for cmd in ("rm -rf *", "rm -r old", "cd x && rm -f *.txt", "find . -name '*.log' -delete", "git reset --hard",
                "git clean -fd", "rm *", "rmdir build"):
        assert not security.decide(run, {"command": cmd}, ctx, "seatbelt").allow, cmd
    for cmd in ("python3 primes.py", "ls -la", "wc -l app.log", "rm notes.tmp", "grep -r rm src", "echo format"):
        assert security.decide(run, {"command": cmd}, ctx, "seatbelt").allow, cmd

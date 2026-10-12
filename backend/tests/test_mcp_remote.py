import asyncio
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ithaca import db
from ithaca.mcp_client import manager, validate_remote_url
from ithaca.tools.registry import ToolContext, registry

SERVER = str(Path(__file__).with_name("fake_remote_mcp.py"))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(transport: str, key: str | None = None):
    port = _free_port()
    args = [sys.executable, SERVER, str(port), transport] + ([key] if key else [])
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    return proc, port


def _add(name, transport, url, auth="none", headers=None):
    sid = db.new_id("s_")
    db.execute(
        "INSERT INTO mcp_servers (id, name, command, args, env, enabled, trusted, created_at, transport, url, headers, auth) "
        "VALUES (?, ?, '', '[]', '{}', 1, 0, 0, ?, ?, ?, ?)",
        (sid, name, transport, url, db.dumps(headers or {}), auth))
    return sid


@pytest.mark.parametrize("transport,path,mode", [
    ("streamable-http", "/mcp", "http"),
    ("sse", "/sse", "sse"),
    ("streamable-http", "/mcp", "auto"),
])
@pytest.mark.asyncio
async def test_remote_transports(transport, path, mode):
    proc, port = _serve(transport)
    try:
        sid = _add(f"remote_{mode}", mode, f"http://127.0.0.1:{port}{path}")
        status = await manager.connect(sid)
        assert status["connected"], status
        tool = registry.get(f"mcp__remote_{mode}__add")
        result = await tool.handler({"a": 2, "b": 40}, ToolContext(run_id="r", conversation_id="c"))
        assert result.content == "42"
        await manager.disconnect(sid)
        assert registry.get(f"mcp__remote_{mode}__add") is None
    finally:
        proc.terminate()


@pytest.mark.asyncio
async def test_remote_api_key_header():
    proc, port = _serve("streamable-http", key="s3cret")
    try:
        bad = _add("keyed_bad", "http", f"http://127.0.0.1:{port}/mcp")
        status = await manager.connect(bad)
        assert not status["connected"] and "401" in (status["error"] or "")
        good = _add("keyed_ok", "http", f"http://127.0.0.1:{port}/mcp", auth="headers", headers={"X-API-Key": "s3cret"})
        status = await manager.connect(good)
        assert status["connected"], status
        await manager.disconnect(good)
        await manager.disconnect(bad)
    finally:
        proc.terminate()


def test_remote_url_rules():
    assert validate_remote_url("https://mcp.example.com/mcp") is None
    assert validate_remote_url("http://127.0.0.1:9000/mcp") is None
    assert "https" in validate_remote_url("http://mcp.example.com/mcp")
    assert validate_remote_url("not a url")


@pytest.mark.asyncio
async def test_oauth_callback_completes_only_matching_state():
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    manager.pending_oauth["state-abc"] = ("s_x", fut)
    assert not manager.complete_oauth("wrong-state", "code")
    assert manager.complete_oauth("state-abc", "the-code")
    assert await fut == ("the-code", "state-abc")
    manager.pending_oauth.clear()


@pytest.mark.asyncio
async def test_oauth_sign_in_flow_end_to_end():
    import httpx
    from urllib.parse import parse_qs, urlparse

    port = _free_port()
    proc = subprocess.Popen([sys.executable, str(Path(__file__).with_name("fake_oauth_mcp.py")), str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        sid = _add("oauthsrv", "http", f"http://127.0.0.1:{port}/mcp", auth="oauth")
        status = await manager.connect(sid)
        assert status["state"] == "auth_required" and status["auth_url"], status

        # Play the user's browser: open the sign-in page, follow the redirect back to Ithaca.
        async with httpx.AsyncClient() as browser:
            r = await browser.get(status["auth_url"], follow_redirects=False)
        callback = urlparse(r.headers["location"])
        q = parse_qs(callback.query)
        assert callback.path == "/api/mcp/oauth/callback"
        assert manager.complete_oauth(q["state"][0], q["code"][0])

        for _ in range(100):
            if manager.status(sid)["connected"]:
                break
            await asyncio.sleep(0.1)
        assert manager.status(sid)["connected"]
        result = await registry.get("mcp__oauthsrv__whoami").handler({}, ToolContext(run_id="r", conversation_id="c"))
        assert result.content == "signed in via OAuth"
        stored = db.loads(db.query_one("SELECT oauth FROM mcp_servers WHERE id = ?", (sid,))["oauth"], {})
        assert stored.get("tokens", {}).get("access_token") and stored.get("client_info", {}).get("client_id")

        # Reconnect: stored tokens are reused, no new sign-in.
        status = await manager.connect(sid)
        assert status["connected"] and status["state"] == "connected", status
        await manager.disconnect(sid)
    finally:
        proc.terminate()


def test_oauth_callback_route_is_reachable_without_session_cookie():
    from fastapi.testclient import TestClient

    from ithaca import main

    with TestClient(main.app, base_url="http://127.0.0.1") as c:
        r = c.get("/api/mcp/oauth/callback", params={"state": "unknown", "code": "x"})
        assert r.status_code == 400 and "expired" in r.text          # no pending flow → harmless
        r = c.get("/api/mcp/oauth/callback", params={"error": "access_denied"})
        assert "Sign-in failed" in r.text

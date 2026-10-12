import asyncio
import json

import pytest

from ithaca import settings
from ithaca.extension_bridge import ExtensionBridge, pairing_token
from ithaca.tools import browser
from ithaca.tools.registry import ToolContext, registry


class FakeExtensionSocket:
    """Stands in for the Chrome extension's WebSocket."""

    def __init__(self, origin, token, responder=None):
        self.headers = {"origin": origin}
        self.query_params = {"token": token}
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.closed_code = None
        self.accepted = False
        self.responder = responder
        self.sent = []

    async def accept(self):
        self.accepted = True
        await self.inbox.put(json.dumps({"type": "hello", "version": "1.0.0"}))

    async def close(self, code=1000):
        self.closed_code = code
        await self.inbox.put(None)

    async def receive_text(self):
        msg = await self.inbox.get()
        if msg is None:
            raise RuntimeError("closed")
        return msg

    async def send_text(self, text):
        msg = json.loads(text)
        if msg.get("type") != "call":
            self.sent.append(msg)
            return
        self.sent.append(msg)
        result = self.responder(msg["method"], msg["params"])
        reply = {"type": "result", "id": msg["id"], **result}
        await self.inbox.put(json.dumps(reply))


@pytest.mark.asyncio
async def test_rejects_wrong_origin_or_token():
    b = ExtensionBridge()
    for origin, token in (("https://evil.example", pairing_token()), ("chrome-extension://abc", "wrong")):
        ws = FakeExtensionSocket(origin, token)
        await b.serve(ws)
        assert ws.closed_code == 4401 and not ws.accepted and not b.connected


@pytest.mark.asyncio
async def test_browser_tools_route_to_users_chrome(monkeypatch):
    snap = {"title": "Untitled document - Google Docs", "url": "https://docs.google.com/document/d/1/edit",
            "elements": ['[1] button "Share"'], "text": "Untitled document", "tabs": 5}

    def respond(method, params):
        if method == "type" and params["ref"] == 9:
            return {"ok": False, "error": "Ithaca never types passwords."}
        return {"ok": True, "result": snap}

    b = ExtensionBridge()
    monkeypatch.setattr(browser, "bridge", b)
    ws = FakeExtensionSocket("chrome-extension://abc", pairing_token(), respond)
    task = asyncio.create_task(b.serve(ws))
    await asyncio.sleep(0.05)
    assert b.connected and b.version == "1.0.0"

    ctx = ToolContext(run_id="r", conversation_id="c")
    r = await registry.get("browser_open").handler({"url": "https://docs.new"}, ctx)
    assert not r.is_error and "Google Docs" in r.content and r.untrusted_source.startswith("https://docs.google.com")
    assert ws.sent[-1]["method"] == "open" and ws.sent[-1]["params"]["new_tab"] is True   # new tab by default
    assert {"type": "reload"} in ws.sent   # v1.0.0 < bundled version → told to update itself
    r = await registry.get("browser_type_here").handler({"text": "Hello"}, ctx)
    assert not r.is_error and ws.sent[-1]["method"] == "type_here"
    r = await registry.get("browser_type").handler({"ref": 9, "text": "hunter2"}, ctx)
    assert r.is_error and "passwords" in r.content

    # Ithaca's own pages stay off-limits even in the user's browser.
    r = await registry.get("browser_open").handler({"url": "http://127.0.0.1:7100/#settings"}, ctx)
    assert r.is_error

    # "separate" ignores the extension; "chrome" requires it.
    settings.set("browser.target", "chrome")
    await ws.close()
    await task
    r = await registry.get("browser_read").handler({}, ctx)
    assert r.is_error and "extension" in r.content


@pytest.mark.asyncio
async def test_no_silent_switch_to_another_browser(monkeypatch):
    """Without the extension: open goes to the user's own browser; other actions explain, never launch Playwright."""
    opened = []

    async def fake_open(url):
        opened.append(url)
        from ithaca.tools.registry import ToolResult
        return ToolResult(f"Opened {url} in a new tab of the user's Google Chrome", data={"browser": "users-browser"})

    monkeypatch.setattr(browser, "bridge", ExtensionBridge())
    monkeypatch.setattr(browser, "_open_in_users_browser", fake_open)
    launched = []
    monkeypatch.setattr(browser.manager, "ensure", lambda: launched.append(1))
    ctx = ToolContext(run_id="r", conversation_id="c")
    r = await registry.get("browser_open").handler({"url": "https://docs.new"}, ctx)
    assert opened == ["https://docs.new"] and not r.is_error
    r = await registry.get("browser_click").handler({"ref": 1}, ctx)
    assert r.is_error and "Settings → Browser" in r.content
    assert not launched



@pytest.mark.asyncio
async def test_socket_swap_fails_fast_and_read_only_calls_retry():
    b = ExtensionBridge()
    slow_reply = asyncio.Event()

    def never(method, params):  # first socket: swallows the call (it's about to be replaced)
        return {"ok": True, "result": {"title": "never"}}

    class Silent(FakeExtensionSocket):
        async def send_text(self, text):
            msg = json.loads(text)
            self.sent.append(msg)
            slow_reply.set()          # don't answer

    old = Silent("chrome-extension://abc", pairing_token())
    t1 = asyncio.create_task(b.serve(old))
    await asyncio.sleep(0.05)

    snap = {"title": "Inbox", "url": "https://mail.google.com", "elements": [], "text": "", "tabs": 1}
    call = asyncio.create_task(b.call("snapshot", {}, timeout=30))
    await slow_reply.wait()
    new = FakeExtensionSocket("chrome-extension://abc", pairing_token(), lambda m, p: {"ok": True, "result": snap})
    t2 = asyncio.create_task(b.serve(new))
    started = asyncio.get_running_loop().time()
    result = await call                                   # retried on the new socket
    assert result["title"] == "Inbox"
    assert asyncio.get_running_loop().time() - started < 2   # no 30 s timeout
    assert b.reconnects == 1

    # A non-idempotent call on a dropped socket fails fast instead of retrying.
    old2 = Silent("chrome-extension://abc", pairing_token())
    slow_reply.clear()
    t3 = asyncio.create_task(b.serve(old2))
    await asyncio.sleep(0.05)
    click = asyncio.create_task(b.call("click", {"ref": 1}, timeout=30))
    await slow_reply.wait()
    t4 = asyncio.create_task(b.serve(FakeExtensionSocket("chrome-extension://abc", pairing_token(), lambda m, p: {"ok": True, "result": snap})))
    with pytest.raises(Exception) as err:
        await asyncio.wait_for(click, timeout=3)
    assert "try again" in str(err.value)   # failed fast (not a 30 s timeout), not retried
    for ws in (new,):
        await ws.close()
    for t in (t1, t2, t3, t4):
        t.cancel()

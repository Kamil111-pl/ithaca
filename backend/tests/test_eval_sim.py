"""The eval simulator (evals/sim.py) and its checks behave like the real thing."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "evals"))

from ithaca.tools import browser, mac  # noqa: E402,F401 - register tools
from ithaca.tools.registry import ToolContext, registry  # noqa: E402
import run as evalrun  # noqa: E402
from sim import SimBrowser, SimMac  # noqa: E402


@pytest.fixture
def sim(monkeypatch):
    b = SimBrowser(web={"https://example.org/a": "Page A\nhello"})
    monkeypatch.setattr(browser, "bridge", b)

    async def public(url):
        return None

    monkeypatch.setattr(browser.web, "_assert_public", public)
    m = SimMac(b)
    m.install()
    yield b, m
    m.uninstall()


def ctx():
    return ToolContext(run_id="r", conversation_id="c")


async def tool(_name, **args):
    return await registry.get(_name).handler(args, ctx())


async def test_google_doc_flow(sim):
    b, _ = sim
    r = await tool("browser_open", url="https://docs.new")
    assert "Document content" in r.content and "docs.google.com/document" in r.content
    # typing at the cursor before clicking into the body goes nowhere, like a real page
    await tool("browser_type_here", text="lost")
    assert b.doc["body"] == ""
    ref = next(i for i, line in enumerate(b.snapshot()["elements"], 1) if "Document content" in line)
    await tool("browser_click", ref=ref)
    r = await tool("browser_type_here", text="Salt comes from rocks.")
    assert "Salt comes from rocks." in r.content
    rr = {"browser": b}
    assert evalrun.CHECKS["gdoc_contains"][1]({"text": "salt"}, rr)
    assert evalrun.CHECKS["browser_visited"][1]({"url": "docs.google.com/document"}, rr)


async def test_gmail_reply_submits_once_sent(sim):
    b, _ = sim
    await tool("browser_open", url="https://mail.google.com")
    els = b.snapshot()["elements"]
    await tool("browser_click", ref=next(i for i, l in enumerate(els, 1) if "Priya" in l))
    els = b.snapshot()["elements"]
    await tool("browser_click", ref=next(i for i, l in enumerate(els, 1) if '"Reply"' in l))
    els = b.snapshot()["elements"]
    body = next(i for i, l in enumerate(els, 1) if "Message body" in l)
    await tool("browser_type", ref=body, text="Thursday works")
    assert not b.submitted  # typing alone doesn't send
    await tool("browser_click", ref=next(i for i, l in enumerate(b.snapshot()["elements"], 1) if '"Send"' in l))
    ok = evalrun.CHECKS["browser_submitted"][1]({"to": "sent?to=priya", "fields": {"body": "thursday"}}, {"browser": b})
    assert ok


async def test_password_fields_are_refused(sim):
    b = SimBrowser(sites={"https://p.example/login": {"title": "Login", "text": "", "elements": [
        {"role": "textbox", "name": "Password", "field": "pw", "password": True}]}})
    browser.bridge = b
    await tool("browser_open", url="https://p.example/login")
    r = await tool("browser_type", ref=1, text="hunter2")
    assert r.is_error and "password" in r.content.lower()
    assert "pw" not in b.tab["fields"]


async def test_stale_ref_is_an_error(sim):
    await tool("browser_open", url="https://example.org/a")
    r = await tool("browser_click", ref=7)
    assert r.is_error and "browser_read" in r.content


async def test_mac_tools_are_recorded_not_run(sim):
    b, m = sim
    await tool("notes_create", title="Groceries", body="- eggs\n- rice")
    await tool("reminders_add", title="buy stamps", list="Errands")
    await tool("open_app", name="Spotify")
    await tool("open_app", name="gmail")  # a website, so it goes to the browser
    rr = {"mac": m, "browser": b}
    assert evalrun.CHECKS["note_contains"][1]({"text": "eggs"}, rr)
    assert evalrun.CHECKS["reminder_in_list"][1]({"text": "errands"}, rr)
    assert evalrun.CHECKS["app_opened"][1]({"name": "Spotify"}, rr)
    assert evalrun.CHECKS["app_not_opened"][1]({"name": "Gmail"}, rr)
    assert evalrun.CHECKS["browser_visited"][1]({"url": "mail.google.com"}, rr)


def test_uninstall_restores_real_handlers():
    before = registry.get("notes_create").handler
    m = SimMac(SimBrowser())
    m.install()
    assert registry.get("notes_create").handler is not before
    m.uninstall()
    assert registry.get("notes_create").handler is before


def test_task_suite_is_valid():
    import json

    tasks = json.loads((Path(evalrun.HERE) / "tasks.json").read_text())
    assert evalrun.validate(tasks) == []
    assert len(tasks) >= 100


def test_compare_flags_regressions():
    old = {"tasks": [{"id": "a", "pass_rate": 1.0}, {"id": "b", "passed": False}]}
    new = {"tasks": [{"id": "a", "pass_rate": 0.0}, {"id": "b", "pass_rate": 1.0}]}
    delta, worse, better = evalrun.compare(new, old)
    assert worse == ["a"] and better == ["b"] and delta == 0

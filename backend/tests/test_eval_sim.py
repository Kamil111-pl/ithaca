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
    # a new doc has the cursor in the body, as in real Google Docs
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


async def test_typing_with_nothing_focused_is_refused(sim):
    b, _ = sim
    await tool("browser_open", url="https://mail.google.com/compose")  # three text fields: no guessing
    r = await tool("browser_type_here", text="lost")
    assert r.is_error and "keyboard focus" in r.content
    assert not b.tab["fields"]


async def test_typing_recovers_when_page_has_one_field(sim):
    b = SimBrowser(sites={"https://s.example": {"title": "Search", "text": "", "elements": [
        {"role": "link", "name": "Home", "go": "https://s.example"},
        {"role": "textbox", "name": "Search", "field": "q"}]}})
    browser.bridge = b
    await tool("browser_open", url="https://s.example")
    r = await tool("browser_type_here", text="cats")
    assert not r.is_error and "only text field" in r.content
    assert b.tab["fields"]["q"] == "cats"


async def test_click_that_changes_nothing_says_so(sim):
    b, _ = sim
    await tool("browser_open", url="https://docs.google.com")
    els = b.snapshot()["elements"]
    r = await tool("browser_click", ref=next(i for i, l in enumerate(els, 1) if "Search" in l))
    r = await tool("browser_click", ref=next(i for i, l in enumerate(els, 1) if "Search" in l))
    assert "nothing on the page changed" in r.content


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

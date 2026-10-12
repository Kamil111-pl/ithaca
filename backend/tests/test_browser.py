import pytest

from ithaca import security
from ithaca.tools import browser  # noqa: F401
from ithaca.tools.registry import ToolContext, registry


@pytest.mark.asyncio
async def test_browser_blocks_local_and_non_web_urls():
    ctx = ToolContext(run_id="r", conversation_id="c")
    for url in ("http://127.0.0.1:7100/#settings", "file:///etc/passwd", "chrome://settings"):
        r = await registry.get("browser_open").handler({"url": url}, ctx)
        assert r.is_error, url


def test_browser_actions_need_one_approval_per_run():
    click = registry.get("browser_click")
    ctx = ToolContext(run_id="r", conversation_id="c")
    assert not security.decide(click, {}, ctx, "seatbelt").allow
    ctx.services["approved_effects"] = {"browser_act"}
    assert security.decide(click, {}, ctx, "seatbelt").allow
    assert security.decide(registry.get("browser_open"), {"url": "https://apple.com"}, ctx, "seatbelt").allow


def test_redirect_to_sign_in_is_flagged():
    from ithaca.tools.browser import _redirect_note

    assert "sign-in" in _redirect_note("https://docs.new", "https://accounts.google.com/v3/signin/identifier?continue=docs")
    assert _redirect_note("https://mail.google.com", "https://mail.google.com/mail/u/0/#inbox") == ""
    assert _redirect_note("https://github.com", "https://www.github.com/") == ""
    assert "redirected to" in _redirect_note("https://t.co/x", "https://example.com/article")


def test_text_that_did_not_land_is_a_failure():
    from ithaca.tools.browser import _format

    bad = _format({"url": "https://x", "title": "x", "elements": [], "text": "", "verified": False,
                   "note": "Typed 5 characters, but the field doesn't show the new text.\n"})
    assert bad.is_error
    clicked = _format({"url": "https://x", "title": "x", "elements": [], "text": "", "verified": False,
                       "note": "Clicked [3], but nothing on the page changed.\n"})
    assert not clicked.is_error and "nothing on the page changed" in clicked.content

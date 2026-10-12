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

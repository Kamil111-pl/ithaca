import sys

import pytest

from ithaca import security, settings
from ithaca.tools.mac import markdown_to_notes_html
from ithaca.tools.registry import ToolContext, registry


def test_markdown_to_notes_html():
    out = markdown_to_notes_html("## Intro\nThe **sea** is *vast* & deep.\n\n- one\n- two\n1. first")
    assert "<h2>Intro</h2>" in out and "<b>sea</b>" in out and "<i>vast</i>" in out and "&amp;" in out
    assert "<ul><li>one</li><li>two</li></ul><ol><li>first</li></ol>" in out


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS tools")
def test_mac_tool_policy():
    from ithaca.tools import mac  # noqa: F401

    ctx = ToolContext(run_id="r", conversation_id="c")
    notes, script = registry.get("notes_create"), registry.get("run_applescript")
    assert security.decide(notes, {}, ctx, "seatbelt").allow            # clean run: just do it
    assert not security.decide(script, {}, ctx, "seatbelt").allow       # scripts always ask
    security.mark_tainted(ctx, "https://evil.example")
    assert not security.decide(notes, {}, ctx, "seatbelt").allow        # tainted: ask
    settings.set("agent.approval_mode", "never")
    assert security.decide(script, {}, ctx, "seatbelt").allow
    found = [t.name for t in registry.search("open the notes app and write an essay")]
    assert "notes_create" in found[:2]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS tools")
@pytest.mark.asyncio
async def test_web_apps_open_as_websites(monkeypatch):
    from ithaca.tools import browser, mac  # noqa: F401
    from ithaca.tools.registry import ToolResult

    opened = []

    async def fake_open(args, ctx):
        opened.append(args["url"])
        return ToolResult("ok")

    monkeypatch.setattr(registry.get("browser_open"), "handler", fake_open)
    ctx = ToolContext(run_id="r", conversation_id="c")
    await registry.get("open_app").handler({"name": "Gmail"}, ctx)
    await registry.get("open_url").handler({"url": "mail.google.com", "app": "Chrome"}, ctx)
    assert opened == ["https://mail.google.com", "https://mail.google.com"]
    assert mac._app_name("chrome") == "Google Chrome"


def test_unknown_app_names_get_suggestions(monkeypatch):
    from ithaca.tools import mac

    monkeypatch.setattr(mac, "installed_apps", lambda: ["Microsoft Teams", "Notes", "Spotify", "TextEdit"])
    assert mac.suggest_apps("teams")[0] == "Microsoft Teams"
    assert "Spotify" in mac.suggest_apps("spotfy")
    assert mac.suggest_apps("zzzz") == []

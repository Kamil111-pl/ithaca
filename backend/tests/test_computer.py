import sys

import pytest

from ithaca import security
from ithaca.providers.anthropic_provider import AnthropicProvider
from ithaca.providers.openai_compat import OpenAICompatProvider
from ithaca.tools.registry import ToolContext, registry

IMG = {"media_type": "image/jpeg", "data": "QUJD"}
MSGS = [
    {"role": "user", "content": "look"},
    {"role": "assistant", "content": "", "tool_calls": [{"id": "s1", "name": "computer_screenshot", "arguments": {}}]},
    {"role": "tool", "tool_call_id": "s1", "content": "Screenshot 1280×800", "images": [IMG]},
]


def test_screenshots_reach_both_provider_formats():
    _, wire = AnthropicProvider.to_wire(MSGS, "claude-opus-5-5")
    block = wire[-1]["content"][0]
    assert block["type"] == "tool_result" and block["content"][1]["type"] == "image"
    o = OpenAICompatProvider.to_wire(MSGS)
    assert o[-2]["role"] == "tool" and o[-1]["role"] == "user"
    assert o[-1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS tools")
def test_computer_actions_need_one_approval_per_run():
    from ithaca.tools import computer  # noqa: F401

    click = registry.get("computer_click")
    ctx = ToolContext(run_id="r", conversation_id="c")
    assert not security.decide(click, {}, ctx, "seatbelt").allow
    ctx.services["approved_effects"] = {"computer"}
    assert security.decide(click, {}, ctx, "seatbelt").allow


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS tools")
@pytest.mark.asyncio
async def test_computer_refuses_without_vision_or_when_disabled():
    from ithaca import settings
    from ithaca.tools import computer  # noqa: F401

    shot = registry.get("computer_screenshot")
    r = await shot.handler({}, ToolContext(run_id="r", conversation_id="c"))
    assert r.is_error and "off" in r.content
    settings.set("computer.enabled", True)
    r = await shot.handler({}, ToolContext(run_id="r", conversation_id="c", services={"vision": False}))
    assert r.is_error and "can't see images" in r.content


def test_coordinate_systems_map_to_screen_points():
    from ithaca.tools.computer import _to_screen

    ctx = ToolContext(run_id="r", conversation_id="c")
    ctx.services.update({"screen_scale": (1.35, 1.35), "screen_points": (1728.0, 1117.0)})
    assert _to_screen(ctx, 100, 200) == (135.0, 270.0)                     # pixel models (Claude)
    ctx.services["coords"] = "norm1000"
    x, y = _to_screen(ctx, 500, 500)                                         # Qwen-VL 0–1000 grid
    assert (round(x), round(y)) == (864, 558)

import pytest

from ithaca import config, security
from ithaca.tools import blender
from ithaca.tools.registry import ToolContext, registry

needs_blender = pytest.mark.skipif(blender.blender_binary() is None, reason="Blender not installed")


def test_blender_control_needs_one_approval_per_run():
    py = registry.get("blender_python")
    ctx = ToolContext(run_id="r", conversation_id="c")
    assert not security.decide(py, {"code": "pass"}, ctx, "seatbelt").allow
    ctx.services["approved_effects"] = {"app_script"}
    assert security.decide(py, {"code": "pass"}, ctx, "seatbelt").allow
    assert security.decide(registry.get("blender_scene"), {"path": "a.blend"}, ctx, "seatbelt").allow


def test_result_format_reports_errors_and_scene():
    r = blender._format({"ok": False, "output": "", "error": "NameError: x",
                         "scene": {"file": "(unsaved)", "object_count": 1, "camera": None, "render_engine": "EEVEE",
                                   "objects": [{"name": "Cube", "type": "MESH", "location": [0, 0, 0], "vertices": 8}]}})
    assert r.is_error and "NameError" in r.content and "- Cube (MESH)" in r.content and "no camera" in r.content


@needs_blender
@pytest.mark.asyncio
async def test_headless_blender_round_trip(monkeypatch, tmp_path):
    monkeypatch.setenv("ITHACA_BLENDER_BACKGROUND", "1")
    monkeypatch.setenv("ITHACA_HOME", str(tmp_path))
    ctx = ToolContext(run_id="r", conversation_id="c")
    try:
        r = await registry.get("blender_python").handler(
            {"code": "bpy.ops.object.select_all(action='SELECT')\nbpy.ops.object.delete()\n"
                     "bpy.ops.mesh.primitive_cube_add()\nprint('made', bpy.context.object.name)"}, ctx)
        assert not r.is_error and "made Cube" in r.content and "1 objects" in r.content
        r = await registry.get("blender_python").handler({"code": "undefined_name"}, ctx)
        assert r.is_error and "NameError" in r.content
        r = await registry.get("blender_save").handler({"path": "t.blend"}, ctx)
        assert not r.is_error and (config.WORKSPACE_DIR / "t.blend").exists()
    finally:
        await blender.shutdown_headless()


def test_blender_tools_only_match_blender_requests():
    generic = [t.name for t in registry.search("Use Python to compute 2 to the power of 20", limit=10)]
    assert not any(n.startswith("blender_") for n in generic)
    blendery = [t.name for t in registry.search("open Blender and make a red cube", limit=6)]
    assert "blender_add_object" in blendery  # the ready-made action beats raw Python


@needs_blender
@pytest.mark.asyncio
async def test_ready_made_actions_build_a_renderable_scene(monkeypatch, tmp_path):
    monkeypatch.setenv("ITHACA_BLENDER_BACKGROUND", "1")
    monkeypatch.setenv("ITHACA_HOME", str(tmp_path))
    ctx = ToolContext(run_id="r", conversation_id="c")
    call = lambda n, **a: registry.get(n).handler(a, ctx)  # noqa: E731
    try:
        assert not (await call("blender_delete", all=True)).is_error
        r = await call("blender_add_object", kind="cube", name="Box", color="red", location=[0, 0, 0])
        assert not r.is_error and "Box (MESH)" in r.content and "Red" in r.content
        r = await call("blender_transform", object="Box", move_by=[0, 0, 2])
        assert not r.is_error and "[0.0, 0.0, 2.0]" in r.content
        r = await call("blender_transform", object="Nope", move_by=[0, 0, 2])
        assert r.is_error and "Box" in r.content  # lists real objects
        r = await call("blender_aim_camera", target="Box")
        assert not r.is_error and "camera" in r.content.lower()
        r = await call("blender_render", path="box.png", width=160, height=90)
        assert not r.is_error and (config.WORKSPACE_DIR / "box.png").exists()
    finally:
        await blender.shutdown_headless()


def test_build_parts_accept_synonyms_and_reject_unknown_keys():
    parts, problems = blender.normalize_parts(
        '[{"name": "top", "shape": "cuboid", "dimensions": [2, 1, 0.1], "material": "brown"}, '
        '{"name": "ball", "shape": "ball", "radius": 0.5, "location": [0, 0, 1]}]')
    assert not problems
    assert parts[0] == {"name": "top", "shape": "box", "dims": [2, 1, 0.1], "color": "brown"}
    assert parts[1]["shape"] == "sphere" and parts[1]["size"] == 1.0 and parts[1]["position"] == [0, 0, 1]
    _, problems = blender.normalize_parts([{"name": "leg", "shape": "box", "position_top": [0, 0, 1]}])
    assert problems and "position_top" in problems[0]


def test_layout_stacks_and_attaches_parts_that_touch():
    placed, warnings = blender.layout([
        {"name": "base", "shape": "sphere", "size": 2},
        {"name": "head", "shape": "sphere", "size": 1, "on_top_of": "base", "sink": 0},
        {"name": "arm", "shape": "box", "dims": [1, 0.2, 0.2], "attach": {"to": "base", "side": "left", "height": 0.5}, "sink": 0}])
    assert not warnings
    assert placed["base"]["center"] == [0, 0, 1.0]                      # resting on the ground
    assert abs(placed["head"]["center"][2] - 2.5) < 1e-9                # touching the top of base
    assert abs(placed["arm"]["center"][0] + 1.5) < 1e-9 and placed["arm"]["center"][2] == 1.0


def test_legs_attach_under_the_corners_of_a_table_top():
    parts = [{"name": "top", "shape": "box", "dims": [1.6, 0.9, 0.08], "position": [0, 0, 0.75]}] + [
        {"name": f"leg{i}", "shape": "box", "dims": [0.08, 0.08, 0.71], "attach": {"to": "top", "side": "bottom", "corner": c}}
        for i, c in enumerate(["front-left", "front-right", "back-left", "back-right"])]
    placed, warnings = blender.layout(parts)
    assert not warnings
    for i in range(4):
        cx, cy, cz = placed[f"leg{i}"]["center"]
        assert abs(abs(cx) - 0.76) < 1e-6 and abs(abs(cy) - 0.41) < 1e-6
        assert abs((cz - 0.355) - 0.0) < 1e-6  # bottom of the leg on the floor, top under the table top

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
    assert security.decide(registry.get("blender_scene"), {}, ctx, "seatbelt").allow


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

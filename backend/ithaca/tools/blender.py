"""Drive Blender through its own Python API — no MCP server or add-on needed.

Ithaca starts Blender with apps/blender_bridge.py on the command line; the
bridge runs code sent over a private localhost socket (token in a 0600 file)
inside Blender's main thread, so changes appear live in the window. Each call
returns the script's output or error plus a summary of the scene, so the model
can check what it did. With ITHACA_BLENDER_BACKGROUND=1 (evals) Blender runs
headless instead.

Python in Blender can do anything on the Mac, so these tools carry the
`app_script` effect: the user approves Blender control once per run.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .. import config, settings
from .registry import ToolContext, ToolResult, obj, tool

BRIDGE = Path(__file__).resolve().parent.parent / "apps" / "blender_bridge.py"
_proc: subprocess.Popen | None = None  # headless instance we started


def _state_file() -> Path:
    return Path(os.environ.get("ITHACA_HOME", Path.home() / ".ithaca")) / "blender-bridge.json"


def blender_binary() -> str | None:
    custom = settings.get("apps.blender_path")
    candidates = [custom] if custom else []
    candidates += ["/Applications/Blender.app/Contents/MacOS/Blender",
                   str(Path.home() / "Applications/Blender.app/Contents/MacOS/Blender"),
                   shutil.which("blender") or ""]
    return next((c for c in candidates if c and Path(c).exists()), None)


def _background() -> bool:
    return os.environ.get("ITHACA_BLENDER_BACKGROUND") == "1"


def _resolve(path: str) -> Path:
    p = Path(path).expanduser()
    return p if p.is_absolute() else (config.WORKSPACE_DIR / p)


async def _send(code: str, timeout: float = 300) -> dict[str, Any]:
    state = json.loads(_state_file().read_text())
    reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", state["port"]), 3)
    try:
        writer.write((json.dumps({"token": state["token"], "code": code}) + "\n").encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout)
    finally:
        writer.close()
    if not line:
        raise ConnectionError("Blender closed the connection.")
    return json.loads(line)


async def bridge_alive() -> bool:
    if not _state_file().exists():
        return False
    try:
        r = await _send("pass", timeout=5)
        return bool(r.get("ok"))
    except (OSError, ValueError, asyncio.TimeoutError, KeyError):
        return False


async def ensure_blender(open_path: Path | None = None) -> str | None:
    """Start Blender with the bridge if it isn't reachable. Returns an error message or None."""
    global _proc
    if await bridge_alive():
        if open_path:
            r = await _send(f"import bpy\nbpy.ops.wm.open_mainfile(filepath={str(open_path)!r})")
            if not r.get("ok"):
                return f"Blender couldn't open {open_path}: {r.get('error')}"
        return None
    binary = blender_binary()
    if not binary:
        return "Blender isn't installed (looked in /Applications). Ask the user to install it from blender.org."
    _state_file().unlink(missing_ok=True)
    file_args = [str(open_path)] if open_path else []
    bridge_args = ["--python", str(BRIDGE), "--", str(_state_file())]
    if _background():
        _proc = subprocess.Popen([binary, "--background", "--factory-startup", *file_args, *bridge_args],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    else:
        # -n: a fresh instance, so an already-open Blender (and its unsaved work) is left alone
        app = str(Path(binary).parents[2])
        await (await asyncio.create_subprocess_exec("open", "-na", app, "--args", *file_args, *bridge_args)).wait()
    for _ in range(120):  # Blender takes a few seconds to start
        await asyncio.sleep(0.5)
        if await bridge_alive():
            return None
    return "Blender started but its Ithaca bridge didn't answer within 60 seconds."


def _format(r: dict[str, Any], note: str = "") -> ToolResult:
    scene = r.get("scene") or {}
    objs = scene.get("objects") or []
    lines = [note] if note else []
    if r.get("output"):
        lines.append("Output:\n" + r["output"].rstrip())
    if r.get("error"):
        lines.append("Python error (nothing after the failing line ran):\n" + r["error"].rstrip())
    lines.append(f"Scene now ({scene.get('file', '?')}): {scene.get('object_count', len(objs))} objects"
                 + (f", camera {scene['camera']}" if scene.get("camera") else ", no camera")
                 + f", engine {scene.get('render_engine', '?')}")
    for o in objs[:40]:
        extra = "".join(f", {k} {v}" for k, v in o.items() if k not in ("name", "type", "location"))
        lines.append(f"- {o['name']} ({o['type']}) at {o['location']}{extra}")
    return ToolResult("\n".join(lines), is_error=not r.get("ok"), data={"scene": scene})


BLENDER_WORDS = frozenset({"blender", ".blend", "bpy", "3d", "3-d", "mesh", "render"})
_APP = dict(effects={"app_script"}, group="apps", gate=BLENDER_WORDS,
            keywords="blender 3d model render scene mesh object animation bpy")


@tool("blender_open",
      "Open Blender (optionally a .blend file) so you can work in it with blender_python. Opens a new Blender window "
      "that Ithaca controls; the user watches the changes live.",
      obj({"path": {"type": "string", "description": ".blend file (workspace-relative or absolute); omit for a new scene"}}),
      **_APP)
async def blender_open(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    path = _resolve(args["path"]) if args.get("path") else None
    if path and not path.exists():
        return ToolResult(f"No such file: {path}", is_error=True)
    err = await ensure_blender(path)
    if err:
        return ToolResult(err, is_error=True)
    return _format(await _send("pass"), "Blender is open and connected.")


@tool("blender_python",
      "Blender only: run a bpy script inside the Blender 3D app to model, edit materials, animate or set up the scene "
      "(for general Python, use run_command). "
      "Starts Blender if needed. Returns printed output, any error, and the scene's objects afterwards. "
      "Keep each script small and check the result before the next step.",
      obj({"code": {"type": "string", "description": "Python using bpy, e.g. bpy.ops.mesh.primitive_cube_add(size=2)"}},
          ["code"]),
      **_APP)
async def blender_python(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    err = await ensure_blender()
    if err:
        return ToolResult(err, is_error=True)
    try:
        return _format(await _send(args["code"]))
    except (OSError, asyncio.TimeoutError, ValueError) as exc:
        return ToolResult(f"Lost the connection to Blender ({exc}). Call blender_open to reconnect.", is_error=True)


@tool("blender_scene", "Describe the open Blender scene: objects, positions, materials, camera, render engine.",
      effects={"read"}, group="apps", gate=BLENDER_WORDS, keywords="blender scene objects list inspect")
async def blender_scene(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    if not await bridge_alive():
        return ToolResult("Blender isn't open under Ithaca's control. Use blender_open first.", is_error=True)
    return _format(await _send("pass"))


@tool("blender_render",
      "Render the current Blender scene to an image file (PNG) in the workspace. Needs a camera in the scene.",
      obj({"path": {"type": "string", "description": "output image, e.g. render.png"},
           "width": {"type": "integer"}, "height": {"type": "integer"}}, ["path"]),
      **_APP)
async def blender_render(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    err = await ensure_blender()
    if err:
        return ToolResult(err, is_error=True)
    out = _resolve(args["path"])
    out.parent.mkdir(parents=True, exist_ok=True)
    code = f"""
import bpy
scn = bpy.context.scene
if scn.camera is None:
    raise RuntimeError("The scene has no camera. Add one first (bpy.ops.object.camera_add) and point it at the subject.")
scn.render.resolution_x, scn.render.resolution_y = {int(args.get('width') or 960)}, {int(args.get('height') or 540)}
scn.render.resolution_percentage = 100
scn.render.image_settings.file_format = 'PNG'
scn.render.filepath = {str(out)!r}
bpy.ops.render.render(write_still=True)
print("rendered", scn.render.filepath)
"""
    r = await _send(code, timeout=900)
    res = _format(r, f"Rendered to {args['path']}." if r.get("ok") else "")
    if r.get("ok") and out.exists() and ctx.services.get("vision"):
        res.images = [{"media_type": "image/png", "data": base64.b64encode(out.read_bytes()).decode()}]
    return res


@tool("blender_save", "Save the open Blender scene as a .blend file (workspace-relative or absolute path).",
      obj({"path": {"type": "string"}}, ["path"]), **_APP)
async def blender_save(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    if not await bridge_alive():
        return ToolResult("Blender isn't open under Ithaca's control. Use blender_open first.", is_error=True)
    out = _resolve(args["path"])
    if out.suffix != ".blend":
        out = out.with_suffix(".blend")
    out.parent.mkdir(parents=True, exist_ok=True)
    r = await _send(f"import bpy\nbpy.ops.wm.save_as_mainfile(filepath={str(out)!r})\nprint('saved', bpy.data.filepath)")
    return _format(r, f"Saved {out.name}." if r.get("ok") else "")


async def shutdown_headless() -> None:
    global _proc
    if _proc and _proc.poll() is None:
        _proc.terminate()
    _proc = None
    _state_file().unlink(missing_ok=True)


# ------------------------------------------------------- ready-made actions
# Common operations as tested bpy snippets, so a small model fills in values
# instead of writing Blender code (which it often gets wrong).

COLORS = {"red": (0.8, 0.05, 0.05), "green": (0.1, 0.6, 0.15), "blue": (0.05, 0.2, 0.8), "yellow": (0.9, 0.8, 0.05),
          "orange": (0.9, 0.4, 0.05), "purple": (0.45, 0.1, 0.7), "pink": (0.9, 0.4, 0.6), "white": (0.9, 0.9, 0.9),
          "black": (0.02, 0.02, 0.02), "gray": (0.4, 0.4, 0.4), "grey": (0.4, 0.4, 0.4), "brown": (0.35, 0.18, 0.07),
          "cyan": (0.05, 0.7, 0.8), "gold": (0.85, 0.65, 0.1), "silver": (0.75, 0.75, 0.78)}

PRIMITIVES = {"cube": "primitive_cube_add", "sphere": "primitive_uv_sphere_add", "cylinder": "primitive_cylinder_add",
              "cone": "primitive_cone_add", "plane": "primitive_plane_add", "torus": "primitive_torus_add",
              "monkey": "primitive_monkey_add", "ico_sphere": "primitive_ico_sphere_add"}


def _rgb(color: str) -> tuple[float, float, float] | None:
    c = color.strip().lower()
    if c in COLORS:
        return COLORS[c]
    hexed = c.lstrip("#")
    if len(hexed) == 6 and all(ch in "0123456789abcdef" for ch in hexed):
        return tuple(int(hexed[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]
    return None


def _vec(v: Any, default: tuple[float, float, float] = (0, 0, 0)) -> tuple[float, float, float]:
    if isinstance(v, (list, tuple)) and len(v) == 3:
        return tuple(float(x) for x in v)  # type: ignore[return-value]
    return default


SET_COLOR = """
def _ithaca_color(obj, rgb, name):
    mat = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    mat.diffuse_color = (*rgb, 1.0)
    if not mat.use_nodes:
        mat.use_nodes = True
    for n in mat.node_tree.nodes:
        if n.type == 'BSDF_PRINCIPLED':
            n.inputs['Base Color'].default_value = (*rgb, 1.0)
    if obj.data is not None and hasattr(obj.data, 'materials'):
        obj.data.materials.clear()
        obj.data.materials.append(mat)
"""


async def _run_action(code: str, note: str) -> ToolResult:
    err = await ensure_blender()
    if err:
        return ToolResult(err, is_error=True)
    r = await _send("import bpy, math, mathutils\n" + SET_COLOR + code)
    return _format(r, note if r.get("ok") else "")


def _find(name: str) -> str:
    return (f"obj = bpy.data.objects.get({name!r})\n"
            f"if obj is None:\n    raise RuntimeError('No object named ' + {name!r} + '. Objects: ' + ', '.join(o.name for o in bpy.data.objects))\n")


@tool("blender_add_object",
      "Blender: add an object — kind is cube, sphere, cylinder, cone, plane, torus, monkey, ico_sphere, camera, or "
      "light — at a location, optionally named, sized and coloured (e.g. color 'red' or '#ff8800').",
      obj({"kind": {"type": "string", "enum": [*PRIMITIVES, "camera", "light"]},
           "name": {"type": "string"},
           "location": {"type": "array", "items": {"type": "number"}, "description": "[x, y, z]"},
           "size": {"type": "number", "description": "overall size in metres (default 2 for meshes)"},
           "color": {"type": "string"}}, ["kind"]),
      **_APP)
async def blender_add_object(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    kind, loc = args["kind"], _vec(args.get("location"))
    if kind == "camera":
        code = f"bpy.ops.object.camera_add(location={loc})\nobj = bpy.context.object\nbpy.context.scene.camera = obj\n"
    elif kind == "light":
        code = f"bpy.ops.object.light_add(type='SUN', location={loc})\nobj = bpy.context.object\nobj.data.energy = 3.0\n"
    else:
        size = float(args.get("size") or 2)
        sizing = {"cube": f"size={size}", "plane": f"size={size}", "monkey": f"size={size}",
                  "sphere": f"radius={size / 2}", "ico_sphere": f"radius={size / 2}", "cylinder": f"radius={size / 2}, depth={size}",
                  "cone": f"radius1={size / 2}, depth={size}", "torus": f"major_radius={size / 2}, minor_radius={size / 8}"}[kind]
        code = f"bpy.ops.mesh.{PRIMITIVES[kind]}({sizing}, location={loc})\nobj = bpy.context.object\n"
    if args.get("name"):
        code += f"obj.name = {args['name']!r}\n"
    if args.get("color") and kind not in ("camera", "light"):
        rgb = _rgb(args["color"])
        if rgb is None:
            return ToolResult(f"Unknown colour {args['color']!r}; use a name like red/blue or a hex code like #ff8800.", is_error=True)
        code += f"_ithaca_color(obj, {rgb}, {args['color'].strip().capitalize()!r})\n"
    code += "print('added', obj.name)\n"
    return await _run_action(code, f"Added a {kind}.")


@tool("blender_set_color", "Blender: give an object a colour (name like 'red' or hex like '#ff8800').",
      obj({"object": {"type": "string"}, "color": {"type": "string"}}, ["object", "color"]), **_APP)
async def blender_set_color(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    rgb = _rgb(args["color"])
    if rgb is None:
        return ToolResult(f"Unknown colour {args['color']!r}; use a name like red/blue or a hex code like #ff8800.", is_error=True)
    code = _find(args["object"]) + f"_ithaca_color(obj, {rgb}, {args['color'].strip().capitalize()!r})\nprint('coloured', obj.name)\n"
    return await _run_action(code, f"Coloured {args['object']} {args['color']}.")


@tool("blender_transform",
      "Blender: move, rotate or scale an object. location sets an absolute position; move_by shifts it "
      "(e.g. [0, 0, 2] = up 2); rotation is in degrees [x, y, z]; scale is [x, y, z] or one number.",
      obj({"object": {"type": "string"},
           "location": {"type": "array", "items": {"type": "number"}},
           "move_by": {"type": "array", "items": {"type": "number"}},
           "rotation": {"type": "array", "items": {"type": "number"}},
           "scale": {"type": "number"}}, ["object"]),
      **_APP)
async def blender_transform(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    code = _find(args["object"])
    if args.get("location") is not None:
        code += f"obj.location = {_vec(args['location'])}\n"
    if args.get("move_by") is not None:
        code += f"obj.location = obj.location + mathutils.Vector({_vec(args['move_by'])})\n"
    if args.get("rotation") is not None:
        code += f"obj.rotation_euler = tuple(math.radians(a) for a in {_vec(args['rotation'])})\n"
    if args.get("scale") is not None:
        s = float(args["scale"])
        code += f"obj.scale = ({s}, {s}, {s})\n"
    code += "print(obj.name, 'at', tuple(round(v, 3) for v in obj.location))\n"
    return await _run_action(code, f"Updated {args['object']}.")


@tool("blender_aim_camera",
      "Blender: point the scene camera at an object so it's framed for a render. Adds a camera (and a light, if the "
      "scene has none) when missing.",
      obj({"target": {"type": "string", "description": "object to look at"},
           "distance": {"type": "number", "description": "metres from the target (default 10)"}}, ["target"]),
      **_APP)
async def blender_aim_camera(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    d = float(args.get("distance") or 10)
    code = _find(args["target"]) + f"""
scn = bpy.context.scene
cam = scn.camera
if cam is None:
    bpy.ops.object.camera_add()
    cam = bpy.context.object
    scn.camera = cam
target = obj.matrix_world.translation
cam.location = target + mathutils.Vector(({d} * 0.6, -{d} * 0.6, {d} * 0.45))
direction = target - cam.location
cam.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()
if not any(o.type == 'LIGHT' for o in scn.objects):
    bpy.ops.object.light_add(type='SUN', location=(target.x + 3, target.y - 3, target.z + 8))
    bpy.context.object.data.energy = 3.0
print('camera', cam.name, 'aimed at', obj.name)
"""
    return await _run_action(code, f"Camera aimed at {args['target']}.")


@tool("blender_delete", "Blender: delete an object by name, or every object with all=true (for a fresh scene).",
      obj({"object": {"type": "string"}, "all": {"type": "boolean"}}), **_APP)
async def blender_delete(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    if args.get("all"):
        code = "for o in list(bpy.data.objects):\n    bpy.data.objects.remove(o, do_unlink=True)\nprint('cleared')\n"
        return await _run_action(code, "Removed every object.")
    if not args.get("object"):
        return ToolResult("Say which object to delete, or all=true.", is_error=True)
    code = _find(args["object"]) + "bpy.data.objects.remove(obj, do_unlink=True)\nprint('deleted')\n"
    return await _run_action(code, f"Deleted {args['object']}.")

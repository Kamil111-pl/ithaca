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
      "Open Blender so you can work in it — pass path to open a .blend file, or omit it for a new empty scene. Opens a "
      "window that Ithaca controls; the user watches the changes live.",
      obj({"path": {"type": "string", "description": ".blend file (workspace-relative or absolute); omit for a new scene"}}),
      **_APP)
async def blender_open(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    path = _resolve(args["path"]) if args.get("path") else None
    if path and not path.exists():
        return ToolResult(f"No such file: {path}", is_error=True)
    err = await ensure_blender(path)
    if err:
        return ToolResult(err, is_error=True)
    return _format(await _send("pass"), f"Opened {path.name} in Blender." if path else
                   "Blender is open with a NEW, empty default scene (not any saved file).")


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


@tool("blender_scene",
      "Describe a Blender scene: objects, positions, materials, camera, render engine. Pass path to inspect a .blend "
      "file (it opens it); without path it describes the scene already open.",
      obj({"path": {"type": "string", "description": ".blend file to open and describe"}}),
      effects={"app_script"}, group="apps", gate=BLENDER_WORDS, keywords="blender scene objects list inspect count")
async def blender_scene(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    if args.get("path"):
        path = _resolve(args["path"])
        if not path.exists():
            return ToolResult(f"No such file: {args['path']}", is_error=True)
        err = await ensure_blender(path)
        if err:
            return ToolResult(err, is_error=True)
        return _format(await _send("pass"), f"Opened {path.name}.")
    if not await bridge_alive():
        return ToolResult("Blender isn't open. To inspect a file, call blender_scene with path='name.blend'.", is_error=True)
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


DIRECTIONS = {"up": (0, 0, 1), "down": (0, 0, -1), "left": (-1, 0, 0), "right": (1, 0, 0), "forward": (0, -1, 0),
              "back": (0, 1, 0)}


@tool("blender_transform",
      "Blender: move, rotate or scale an object. To move it, use up/down/left/right/forward/back in metres "
      "(e.g. up: 2). location sets an absolute [x, y, z] (z is up); rotation is in degrees [x, y, z]; scale is one number.",
      obj({"object": {"type": "string"},
           **{d: {"type": "number", "description": f"move {d} by this many metres"} for d in DIRECTIONS},
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
    shift = [sum(float(args[d]) * DIRECTIONS[d][i] for d in DIRECTIONS if args.get(d)) for i in range(3)]
    if any(shift):
        code += f"obj.location = obj.location + mathutils.Vector({tuple(shift)})\n"
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


# ------------------------------------------------------------ declarative build
# A small model is bad at 3D coordinate arithmetic, so it describes parts and how
# they relate ("head on top of body"); Ithaca does the geometry.

SHAPES = {"sphere": "primitive_uv_sphere_add(segments=48, ring_count=24, radius=1)", "cube": "primitive_cube_add(size=2)",
          "box": "primitive_cube_add(size=2)", "cylinder": "primitive_cylinder_add(vertices=48, radius=1, depth=2)",
          "cone": "primitive_cone_add(vertices=48, radius1=1, depth=2)",
          "torus": "primitive_torus_add(major_segments=64, minor_segments=24)", "plane": "primitive_plane_add(size=2)",
          "monkey": "primitive_monkey_add(size=2)"}
ROUND = {"sphere", "cylinder", "cone", "torus", "monkey"}
SIDES = {"left": (-1, 0, 0), "right": (1, 0, 0), "front": (0, -1, 0), "back": (0, 1, 0), "top": (0, 0, 1), "bottom": (0, 0, -1)}


def _dims(p: dict[str, Any]) -> tuple[float, float, float]:
    shape = p["shape"]
    if isinstance(p.get("dims"), (list, tuple)) and len(p["dims"]) == 3:
        x, y, z = (max(float(v), 0.001) for v in p["dims"])
        return x, y, (0.0 if shape == "plane" else z)
    s = float(p.get("size") or 1.0)
    return {"torus": (s, s, s / 4), "plane": (s, s, 0.0)}.get(shape, (s, s, s))


def _rotated_dims(dims: tuple[float, float, float], rotation: Any) -> tuple[float, float, float]:
    """Axis-aligned size of a box of `dims` after rotating it (degrees, XYZ Euler)."""
    import math

    rx, ry, rz = (math.radians(a) for a in _vec(rotation))
    cx, sx, cy, sy, cz, sz = math.cos(rx), math.sin(rx), math.cos(ry), math.sin(ry), math.cos(rz), math.sin(rz)
    m = [[cy * cz, sx * sy * cz - cx * sz, cx * sy * cz + sx * sz],
         [cy * sz, sx * sy * sz + cx * cz, cx * sy * sz - sx * cz],
         [-sy, sx * cy, cx * cy]]
    return tuple(sum(abs(m[i][j]) * dims[j] for j in range(3)) for i in range(3))  # type: ignore[return-value]


def layout(parts: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Resolve relations into centres. Returns {name: {center, dims}} and warnings."""
    placed: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    pending = list(parts)
    for _ in range(len(parts) + 1):
        progress = False
        for p in list(pending):
            ref = p.get("on_top_of") or (p.get("attach") or {}).get("to")
            if ref and ref not in placed:
                if ref not in {q["name"] for q in parts}:
                    warnings.append(f"{p['name']}: no part named {ref!r}; placed at its position instead.")
                    p = {**p, "on_top_of": None, "attach": None}
                else:
                    continue
            d = _rotated_dims(_dims(p), p.get("rotation"))
            if p.get("on_top_of"):
                base = placed[p["on_top_of"]]
                bc, bd = base["center"], base["dims"]
                sink = float(p.get("sink", 0.15 if p["shape"] in ROUND else 0.0))  # round parts nestle a little
                cz = bc[2] + bd[2] / 2 + d[2] / 2 - sink * min(d[2], bd[2])
                center = [bc[0], bc[1], cz]
            elif p.get("attach"):
                a = p["attach"]
                base = placed[a["to"]]
                bc, bd = base["center"], base["dims"]
                v = SIDES.get(str(a.get("side", "front")).lower(), (0, -1, 0))
                sink = float(p.get("sink", 0.1 if v[2] == 0 else 0.0))
                center = [bc[i] + v[i] * (bd[i] / 2 + d[i] / 2 - sink * d[i]) for i in range(3)]
                h = a.get("height")  # 0 = bottom of the base part, 1 = top
                if h is not None and v[2] == 0:
                    center[2] = bc[2] - bd[2] / 2 + float(h) * bd[2]
                corner = str(a.get("corner") or "").lower().replace("_", "-").replace(" ", "-")
                if corner and v[2] != 0:  # under/over a corner of the base, inset by the part's own size
                    sx = -1 if "left" in corner else 1 if "right" in corner else 0
                    sy = -1 if "front" in corner else 1 if "back" in corner else 0
                    center[0] = bc[0] + sx * (bd[0] / 2 - d[0] / 2)
                    center[1] = bc[1] + sy * (bd[1] / 2 - d[1] / 2)
            else:
                pos = p.get("position") or [0, 0, 0]
                center = [float(v) for v in pos] if len(pos) == 3 else [0.0, 0.0, 0.0]
                if not p.get("position"):
                    center[2] = d[2] / 2  # sits on the ground
            off = p.get("offset")
            if isinstance(off, (list, tuple)) and len(off) == 3:
                center = [center[i] + float(off[i]) for i in range(3)]
            placed[p["name"]] = {"center": center, "dims": d}
            pending.remove(p if p in pending else next(q for q in pending if q["name"] == p["name"]))
            progress = True
        if not pending or not progress:
            break
    for p in pending:
        warnings.append(f"{p['name']}: its reference forms a loop; placed at the origin.")
        placed[p["name"]] = {"center": [0.0, 0.0, _dims(p)[2] / 2], "dims": _dims(p)}
    # rest the whole model on the ground
    low = min(v["center"][2] - v["dims"][2] / 2 for v in placed.values())
    if abs(low) > 1e-6 and not any(p.get("position") for p in parts):
        for v in placed.values():
            v["center"][2] -= low
    return placed, warnings


PART_KEYS = {"name", "shape", "size", "dims", "color", "on_top_of", "attach", "position", "offset", "rotation", "sink"}
PART_ALIASES = {"dimensions": "dims", "dimension": "dims", "material": "color", "colour": "color", "location": "position",
                "pos": "position", "on": "on_top_of", "ontop": "on_top_of", "on_top": "on_top_of", "rotate": "rotation",
                "type": "shape", "diameter": "size", "radius": None}
SHAPE_ALIASES = {"ball": "sphere", "uv_sphere": "sphere", "cuboid": "box", "rectangle": "box", "block": "box",
                 "tube": "cylinder", "pyramid": "cone", "ring": "torus", "donut": "torus", "suzanne": "monkey"}


def normalize_parts(raw: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Accept common synonyms; reject unknown keys loudly instead of silently ignoring them."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return [], ["parts must be a list of part objects."]
    problems, parts = [], []
    for i, p in enumerate(raw or []):
        if not isinstance(p, dict):
            problems.append(f"part {i + 1} is not an object.")
            continue
        q: dict[str, Any] = {}
        for k, v in p.items():
            key = k.strip().lower()
            if key == "radius":  # radius → diameter
                q["size"] = float(v) * 2 if isinstance(v, (int, float)) else v
                continue
            key = PART_ALIASES.get(key, key) or key
            if key not in PART_KEYS:
                problems.append(f"{p.get('name') or f'part {i + 1}'}: unknown key {k!r}.")
                continue
            q[key] = v
        if isinstance(q.get("shape"), str):
            q["shape"] = SHAPE_ALIASES.get(q["shape"].strip().lower(), q["shape"].strip().lower())
        if q.get("shape") not in SHAPES:
            problems.append(f"{q.get('name') or f'part {i + 1}'}: shape must be one of {', '.join(SHAPES)}.")
            continue
        if not q.get("name"):
            q["name"] = f"{q['shape']} {i + 1}"
        parts.append(q)
    return parts, problems


@tool("blender_build",
      "Blender: build a whole model in one call from parts. Z is UP. Each part: name, shape (sphere, cube, box, cylinder, "
      "cone, torus, plane, monkey), size (diameter/edge in metres) or dims [width x, depth y, HEIGHT z], color, and where "
      "it goes: on_top_of another part, attach {to, side: left/right/front/back/top/bottom, height 0–1 for sides, corner: "
      "front-left/front-right/back-left/back-right for top/bottom}, or position [x, y, z] (z = height of its centre); "
      "optional offset [dx, dy, dz] and rotation [x, y, z] degrees. Ithaca computes exact positions so parts touch, rests "
      "the model on the ground, smooths round shapes, and frames it with a camera and light. Example table: "
      "[{name: top, shape: box, dims: [1.6, 0.9, 0.08], color: brown, position: [0, 0, 0.75]}, {name: leg1, shape: box, "
      "dims: [0.08, 0.08, 0.71], color: brown, attach: {to: top, side: bottom, corner: front-left}}, … leg2–leg4 at the "
      "other corners]. Example snowman: "
      "[{name: base, shape: sphere, size: 2, color: white}, {name: body, shape: sphere, size: 1.4, color: white, "
      "on_top_of: base}, {name: head, shape: sphere, size: 0.9, color: white, on_top_of: body}, {name: nose, shape: cone, "
      "dims: [0.12, 0.12, 0.4], color: orange, attach: {to: head, side: front, height: 0.5}, rotation: [90, 0, 0]}]. "
      "Then call blender_look to check it.",
      obj({"parts": {"type": "array", "items": {"type": "object", "properties": {
               "name": {"type": "string"}, "shape": {"type": "string", "enum": list(SHAPES)},
               "size": {"type": "number"}, "dims": {"type": "array", "items": {"type": "number"}},
               "color": {"type": "string"}, "on_top_of": {"type": "string"},
               "attach": {"type": "object", "properties": {"to": {"type": "string"}, "side": {"type": "string"},
                                                           "height": {"type": "number"}, "corner": {"type": "string"}}},
               "position": {"type": "array", "items": {"type": "number"}},
               "offset": {"type": "array", "items": {"type": "number"}},
               "rotation": {"type": "array", "items": {"type": "number"}}},
               "required": ["name", "shape"]}},
           "clear_scene": {"type": "boolean", "description": "remove everything else first (default true)"},
           "separate_pieces": {"type": "boolean", "description": "true if the parts are meant to be separate objects (a scene)"}},
          ["parts"]),
      **_APP)
async def blender_build(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    parts, problems = normalize_parts(args.get("parts"))
    if problems:
        return ToolResult("Nothing was built. Fix these and call blender_build again:\n- " + "\n- ".join(problems)
                          + f"\nEach part may only use: {', '.join(sorted(PART_KEYS))}.", is_error=True)
    if not parts:
        return ToolResult("Give at least one part with a name and a shape (sphere, cube, box, cylinder, cone, torus, "
                          "plane, monkey).", is_error=True)
    names = [p["name"] for p in parts]
    if len(set(names)) != len(names):
        return ToolResult("Part names must be unique.", is_error=True)
    placed, warnings = layout(parts)
    boxes = {n: ([v["center"][i] - v["dims"][i] / 2 for i in range(3)], [v["center"][i] + v["dims"][i] / 2 for i in range(3)])
             for n, v in placed.items()}
    eps = 0.02
    for n, (lo, hi) in boxes.items():  # a part touching nothing and not on the ground is floating
        touching = any(all(lo[i] <= hi2[i] + eps and lo2[i] <= hi[i] + eps for i in range(3))
                       for m, (lo2, hi2) in boxes.items() if m != n)
        if not touching and lo[2] > eps:
            warnings.append(f"{n} is floating {lo[2]:.2f} m up, touching nothing — attach it or put it on_top_of a part.")
    # one object should be one connected piece: group parts that touch
    group = {n: n for n in boxes}

    def find(n: str) -> str:
        while group[n] != n:
            group[n] = group[group[n]]
            n = group[n]
        return n

    for n, (lo, hi) in boxes.items():
        for m, (lo2, hi2) in boxes.items():
            if n < m and all(lo[i] <= hi2[i] + eps and lo2[i] <= hi[i] + eps for i in range(3)):
                group[find(n)] = find(m)
    islands: dict[str, list[str]] = {}
    for n in boxes:
        islands.setdefault(find(n), []).append(n)
    structural = []
    if len(islands) > 1 and len(boxes) <= 40:
        listing = "; ".join("[" + ", ".join(v) + "]" for v in islands.values())
        structural.append(f"The parts form {len(islands)} separate pieces that don't touch: {listing}. If they belong to "
                          "one object, connect them with on_top_of / attach instead of positions (z is up).")
    seen: dict[tuple, str] = {}
    for p in parts:  # identical centre + size = parts sitting inside each other
        key = tuple(round(c, 2) for c in placed[p["name"]]["center"]) + tuple(round(c, 2) for c in placed[p["name"]]["dims"])
        if key in seen:
            warnings.append(f"{p['name']} is exactly where {seen[key]} is (same place and size) — give it a position, "
                            "on_top_of or attach.")
        seen.setdefault(key, p["name"])
    code = ""
    if args.get("clear_scene", True):
        code += "for o in list(bpy.data.objects):\n    bpy.data.objects.remove(o, do_unlink=True)\n"
    else:
        code += "".join(f"if bpy.data.objects.get({n!r}): bpy.data.objects.remove(bpy.data.objects[{n!r}], do_unlink=True)\n"
                        for n in names)
    for p in parts:
        geo = placed[p["name"]]
        code += f"bpy.ops.mesh.{SHAPES[p['shape']]}\nobj = bpy.context.object\nobj.name = {p['name']!r}\n"
        dx, dy, dz = _dims(p)  # local size; rotation is applied after
        code += f"obj.dimensions = ({dx}, {dy}, {max(dz, 0.0001) if p['shape'] != 'plane' else 0})\n"
        rot = _vec(p.get("rotation"))
        if any(rot):
            code += f"obj.rotation_euler = tuple(math.radians(a) for a in {rot})\n"
        code += f"obj.location = {tuple(round(c, 4) for c in geo['center'])}\n"
        if p["shape"] in ROUND:
            code += "bpy.ops.object.shade_smooth()\n"
        rgb = _rgb(p["color"]) if p.get("color") else None
        if p.get("color") and rgb is None:
            warnings.append(f"{p['name']}: unknown colour {p['color']!r}, left grey.")
        if rgb:
            code += f"_ithaca_color(obj, {rgb}, {p['color'].strip().capitalize()!r})\n"
    # frame it: camera + light around the whole model, ground plane, material view in the window
    code += """
mn = mathutils.Vector((1e9, 1e9, 1e9)); mx_ = mathutils.Vector((-1e9, -1e9, -1e9))
for o in bpy.context.scene.objects:
    if o.type == 'MESH':
        for c in o.bound_box:
            w = o.matrix_world @ mathutils.Vector(c)
            mn = mathutils.Vector(map(min, mn, w)); mx_ = mathutils.Vector(map(max, mx_, w))
center = (mn + mx_) / 2; extent = max((mx_ - mn).length, 1.0)
scn = bpy.context.scene
for n in ('Ithaca Camera', 'Ithaca Sun'):
    if bpy.data.objects.get(n): bpy.data.objects.remove(bpy.data.objects[n], do_unlink=True)
bpy.ops.object.camera_add(); cam = bpy.context.object; cam.name = 'Ithaca Camera'; scn.camera = cam
cam.location = center + mathutils.Vector((extent * 1.1, -extent * 1.5, extent * 0.7))
cam.rotation_euler = (center - cam.location).to_track_quat('-Z', 'Y').to_euler()
bpy.ops.object.light_add(type='SUN', location=(center.x + 4, center.y - 4, mx_.z + 6)); bpy.context.object.data.energy = 3.0
bpy.context.object.name = 'Ithaca Sun'
if not bpy.app.background:
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == 'VIEW_3D':
                area.spaces.active.shading.type = 'MATERIAL'
                with bpy.context.temp_override(window=win, area=area, region=[r for r in area.regions if r.type == 'WINDOW'][0]):
                    bpy.ops.view3d.view_all(center=False)
print('built', len([o for o in scn.objects if o.type == 'MESH']), 'parts; size', tuple(round(v, 2) for v in (mx_ - mn)))
"""
    res = await _run_action(code, f"Built {len(parts)} parts.")
    problems = structural + [w for w in warnings if "floating" in w or "exactly where" in w]
    if problems and not res.is_error and not args.get("separate_pieces"):
        res.is_error = True  # built, but wrong: make the model fix it before saving
        res.content = ("Built, but the model has structural problems — fix them with blender_build before saving "
                       "(set separate_pieces: true if separate pieces are intended):\n- " + "\n- ".join(problems)
                       + "\n" + res.content)
    elif warnings:
        res.content = "Warnings:\n- " + "\n- ".join(warnings) + "\n" + res.content
    return res


@tool("blender_look",
      "Blender: render a quick preview of the scene and describe what it actually looks like (shapes, layout, colours), "
      "so you can check it matches what the user asked for. Say what it should look like in `goal`.",
      obj({"goal": {"type": "string", "description": "what the model is meant to be, e.g. 'a snowman with a carrot nose'"}}),
      effects={"app_script"}, group="apps", gate=BLENDER_WORDS, keywords="blender look check preview see render inspect")
async def blender_look(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    err = await ensure_blender()
    if err:
        return ToolResult(err, is_error=True)
    out = config.WORKSPACE_DIR / ".ithaca" / "blender-look.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    code = f"""
import bpy
scn = bpy.context.scene
if scn.camera is None:
    raise RuntimeError("No camera to look through; use blender_build or blender_aim_camera first.")
prev = (scn.render.engine, scn.render.resolution_x, scn.render.resolution_y, scn.render.filepath)
scn.render.engine = 'BLENDER_WORKBENCH'
scn.display.shading.color_type = 'MATERIAL'
scn.display.shading.light = 'STUDIO'
scn.render.resolution_x, scn.render.resolution_y, scn.render.resolution_percentage = 640, 480, 100
scn.render.image_settings.file_format = 'PNG'
scn.render.filepath = {str(out)!r}
bpy.ops.render.render(write_still=True)
scn.render.engine, scn.render.resolution_x, scn.render.resolution_y, scn.render.filepath = prev
print('preview ok')
"""
    r = await _send(code, timeout=300)
    if not r.get("ok") or not out.exists():
        return _format(r)
    image = base64.b64encode(out.read_bytes()).decode()
    goal = (args.get("goal") or "").strip()
    description = await _describe(image, goal)
    res = _format(r, (f"What the render shows:\n{description}\n" if description else
                      "Preview rendered (no vision model available to describe it).\n"))
    if ctx.services.get("vision"):
        res.images = [{"media_type": "image/png", "data": image}]
    return res


async def _describe(image_b64: str, goal: str) -> str:
    """Ask the local vision model what the render shows (best-effort)."""
    import httpx

    prompt = ("This is a render of a simple 3D model made from basic shapes. Describe what you see: the shapes, how "
              "they are arranged, and their colours, in 3-5 short sentences.")
    if goal:
        prompt += (f" It is meant to be: {goal}. Then say plainly whether it looks like that, and list what is wrong or "
                   "missing (e.g. parts floating, wrong proportions, wrong colours).")
    body = {"model": "vision", "max_tokens": 300, "temperature": 0.2,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                {"type": "text", "text": prompt}]}]}
    try:
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(f"{config.LOCAL_VISION_URL}/chat/completions", json=body)
            r.raise_for_status()
            return (r.json()["choices"][0]["message"].get("content") or "").strip()
    except (httpx.HTTPError, KeyError, ValueError):
        return ""

"""Runs *inside* Blender (passed with --python): lets Ithaca drive Blender live.

Ithaca launches Blender as
    Blender [file.blend] --python blender_bridge.py -- <state-file>
The bridge listens on 127.0.0.1 (random port) and writes {"port", "token",
"pid"} to <state-file> (mode 0600). Each request is one JSON line
{"token", "code"}; the code runs in Blender's main thread with `bpy` in scope,
and the reply is one JSON line {"ok", "output", "error", "scene"}.

In the GUI the main thread is driven by a bpy.app.timers callback, so the user
sees every change; with --background the bridge serves in a blocking loop.
Standard library only.
"""

import contextlib
import io
import json
import os
import queue
import secrets
import socket
import sys
import threading
import traceback

import bpy  # noqa: F401 - provided by Blender

STATE = sys.argv[sys.argv.index("--") + 1] if "--" in sys.argv else os.path.expanduser("~/.ithaca/blender-bridge.json")
TOKEN = secrets.token_urlsafe(24)
jobs: "queue.Queue[tuple[str, queue.Queue]]" = queue.Queue()
namespace = {"bpy": bpy, "__name__": "__ithaca__"}


def scene_summary() -> dict:
    scn = bpy.context.scene
    objs = []
    for o in list(scn.objects)[:60]:
        item = {"name": o.name, "type": o.type, "location": [round(v, 3) for v in o.location]}
        if o.type == "MESH" and o.data:
            item["vertices"] = len(o.data.vertices)
        mats = [s.material.name for s in getattr(o, "material_slots", []) if s.material]
        if mats:
            item["materials"] = mats
        if any(abs(v - 1) > 1e-6 for v in o.scale):
            item["scale"] = [round(v, 3) for v in o.scale]
        objs.append(item)
    return {
        "file": bpy.data.filepath or "(unsaved)",
        "scene": scn.name,
        "objects": objs,
        "object_count": len(scn.objects),
        "camera": scn.camera.name if scn.camera else None,
        "render_engine": scn.render.engine,
        "frame_range": [scn.frame_start, scn.frame_end],
        "materials": [m.name for m in bpy.data.materials][:40],
    }


def run(code: str) -> dict:
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            exec(compile(code, "<ithaca>", "exec"), namespace)
        result = {"ok": True, "output": out.getvalue()[-8000:], "error": None}
    except Exception:
        tb = traceback.format_exc(limit=4)
        result = {"ok": False, "output": out.getvalue()[-4000:], "error": tb[-4000:]}
    try:
        result["scene"] = scene_summary()
    except Exception as exc:  # noqa: BLE001
        result["scene"] = {"error": str(exc)}
    return result


def handle(conn: socket.socket) -> None:
    with conn:
        data = b""
        while not data.endswith(b"\n"):
            chunk = conn.recv(65536)
            if not chunk:
                return
            data += chunk
        try:
            req = json.loads(data)
        except ValueError:
            return
        if not secrets.compare_digest(str(req.get("token", "")), TOKEN):
            conn.sendall(b'{"ok": false, "error": "bad token"}\n')
            return
        reply: queue.Queue = queue.Queue()
        jobs.put((req.get("code") or "", reply))
        conn.sendall((json.dumps(reply.get()) + "\n").encode())


def accept_loop(server: socket.socket) -> None:
    while True:
        conn, _ = server.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


def drain() -> float:
    """Main-thread pump: run queued code. Returned value = seconds until next call."""
    try:
        while True:
            code, reply = jobs.get_nowait()
            reply.put(run(code))
    except queue.Empty:
        pass
    return 0.05


def main() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    fd = os.open(STATE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"port": server.getsockname()[1], "token": TOKEN, "pid": os.getpid(),
                   "background": bool(bpy.app.background)}, f)
    threading.Thread(target=accept_loop, args=(server,), daemon=True).start()
    print(f"ithaca bridge listening on {server.getsockname()[1]}", flush=True)
    if bpy.app.background:
        while True:  # headless: serve forever in the main thread
            code, reply = jobs.get()
            reply.put(run(code))
    else:
        bpy.app.timers.register(drain, persistent=True)


main()

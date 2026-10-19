# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for scripts/assets.py and scripts/assets_blender.py, run against real (background) Blenders.

    python -X utf8 tests/test_assets.py --work DIR                    # network-free: a local fixture server
    python -X utf8 tests/test_assets.py --work DIR --online           # + real Poly Haven (1k HDRI, texture, model)
    python -X utf8 tests/test_assets.py --work DIR --bridge-zip mcp-1.0.3.zip --bridge-port 9885   # + LIVE rows
    python -X utf8 tests/test_assets.py --work DIR --mutants          # each row again on a planted defect: must FAIL
    --only R05,R07   run some rows      --blender EXE   (else BLENDER_EXE, else the Steam/Program Files install)

Network-free rows serve recorded API answers and tiny files of our own from tests/fixtures/assets (made by
fixtures/assets/make_fixtures.py) on 127.0.0.1, by pointing BLENDER_AI_ASSETS_*_API at it. Every row has a
control (the same measurement where the answer is known) and, with --mutants, is run once more against a
scratch copy of the scripts with one defect planted; the row must then fail, or it cannot fail at all.

Blenders are started one at a time (plus the bridge Blender in LIVE rows), only with 6000 MB of memory free,
always --background --factory-startup, with the work folder (keep 'btools' in it) on their command line.
Prints one line per check and a JSON summary as the last line. Exit code 0 when every row passed.
"""
import argparse
import ctypes
import hashlib
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SKILL = TESTS.parent
FIXTURES = TESTS / "fixtures" / "assets"
FORBIDDEN_PORTS = {9876, 8765, 28080, 28081}
PP_KEY, SF_TOKEN = "fixture-key", "fixture-token"
SF_OK, SF_SLIP, SF_ABS, SF_GLB = ("sfboxok0000000000000000000000001", "sfslip00000000000000000000000002",
                                  "sfabs000000000000000000000000003", "sfglb000000000000000000000000004")


# ------------------------------------------------------------------------------------------- environment

def free_mb():
    class MS(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                    ("avail", ctypes.c_ulonglong), ("tp", ctypes.c_ulonglong), ("ap", ctypes.c_ulonglong),
                    ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong), ("ae", ctypes.c_ulonglong)]
    if os.name != "nt":
        return 10 ** 6
    ms = MS()
    ms.len = ctypes.sizeof(MS)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
    return ms.avail // (1 << 20)


def wait_memory(need=6000, patience=600):
    start = time.time()
    while free_mb() < need:
        if time.time() - start > patience:
            raise SystemExit("not starting a Blender: only %d MB free for %d s" % (free_mb(), patience))
        time.sleep(5)


class Ctx:
    """Paths and settings for one run (and for one mutant)."""

    def __init__(self, args, scripts, work):
        self.args, self.scripts, self.work = args, Path(scripts), Path(work)
        self.blender = args.blender
        self.server = None
        self.cache = self.work / "cache"
        self.tmp = self.work / "tmp"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.bridge = None
        self.gui = None

    def env(self, keys=True, **extra):
        env = dict(os.environ)
        for k in ("POLYPIZZA_API_KEY", "SKETCHFAB_API_TOKEN", "BLENDER_AI_ASSETS_POLYHAVEN_API",
                  "BLENDER_AI_ASSETS_POLYPIZZA_API", "BLENDER_AI_ASSETS_SKETCHFAB_API"):
            env.pop(k, None)
        env.update({"BLENDER_AI_ASSETS_DIR": str(self.cache), "TEMP": str(self.tmp), "TMP": str(self.tmp),
                    "BLENDER_EXE": self.blender, "PYTHONIOENCODING": "utf-8"})
        if self.server is not None:
            base = self.server.base
            env.update({"BLENDER_AI_ASSETS_POLYHAVEN_API": base + "/ph", "BLENDER_AI_ASSETS_POLYPIZZA_API": base + "/pp",
                        "BLENDER_AI_ASSETS_SKETCHFAB_API": base + "/sf"})
        if keys:
            env.update({"POLYPIZZA_API_KEY": PP_KEY, "SKETCHFAB_API_TOKEN": SF_TOKEN})
        env.update({k: str(v) for k, v in extra.items() if v is not None})
        for k, v in extra.items():
            if v is None:
                env.pop(k, None)
        return env

    def tool(self, *argv, env=None, timeout=900):
        """Run assets.py; returns (exit code, JSON answer, stderr)."""
        if any(not isinstance(a, str) for a in argv):
            argv = [str(a) for a in argv]
        proc = subprocess.run([sys.executable, "-X", "utf8", str(self.scripts / "assets.py"), *argv],
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              env=env or self.env(), timeout=timeout)
        lines = [l for l in proc.stdout.splitlines() if l.strip()]
        try:
            answer = json.loads(lines[-1]) if lines else {}
        except ValueError:
            answer = {"unparsed": proc.stdout[-500:]}
        return proc.returncode, answer, proc.stderr

    def headless(self, *argv, **kw):
        wait_memory()
        return self.tool(*argv, **kw)


# ------------------------------------------------------------------------------------------- fixture server

class FixtureServer:
    """Recorded API answers and our tiny files on 127.0.0.1; logs every request it sees."""

    def __init__(self, root):
        self.root = Path(root)
        self.log = []
        self.lock = threading.Lock()
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                parts = urllib.parse.urlsplit(self.path)
                query = dict(urllib.parse.parse_qsl(parts.query))
                headers = {k.lower(): v for k, v in self.headers.items()}
                with outer.lock:
                    outer.log.append({"path": parts.path, "query": query, "headers": headers})
                try:
                    status, ctype, body, extra, cut = outer.route(parts.path, query, headers)
                except Exception as exc:  # noqa: BLE001
                    status, ctype, body, extra, cut = 500, "text/plain", str(exc).encode(), {}, False
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in extra.items():
                    self.send_header(k, v)
                if cut:
                    self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body[: len(body) // 2] if cut else body)
                if cut:
                    self.wfile.flush()
                    self.close_connection = True

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()

    def count(self):
        with self.lock:
            return len(self.log)

    def since(self, n):
        with self.lock:
            return list(self.log[n:])

    def api(self, name):
        path = self.root / "api" / name
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8").replace("{BASE}", self.base).encode("utf-8")

    def route(self, path, query, headers):
        js = lambda body, status=200: (status, "application/json", body, {}, False)  # noqa: E731
        notfound = (404, "text/plain", b"not found", {}, False)
        if path.startswith("/ph/"):
            rest = path[4:]
            if rest == "assets":
                if query.get("category") == "NoSuch/Thing":
                    return (400, "text/plain", b"Unknown category: NoSuch/Thing", {}, False)
                name = "ph_assets_%s%s.json" % (query.get("type", "all"),
                                                "_stone" if query.get("category") == "Stone/Walls" else "")
                body = self.api(name)
                return js(body, 200) if body else notfound
            if rest == "search":
                if query.get("q") == "down":
                    return (503, "text/plain", b"search unavailable", {}, False)
                body = self.api("ph_search_%s_%s.json" % (query.get("q"), query.get("t")))
                return js(body) if body else js(b'{"results": []}')
            for prefix in ("info/", "files/", "taxonomy/"):
                if rest.startswith(prefix):
                    body = self.api("ph_%s_%s.json" % (prefix[:-1], rest[len(prefix):]))
                    return js(body) if body else (404, "text/plain", b"No asset with id", {}, False)
            return notfound
        if path.startswith("/pp/"):
            if headers.get("x-auth-token") != PP_KEY:
                return (401, "application/json", b'{"error": "You need an API key"}', {}, False)
            rest = path[4:]
            if rest.startswith("search/"):
                body = self.api("pp_search_%s.json" % urllib.parse.unquote(rest[7:]))
                return js(body) if body else js(b'{"total": 0, "results": []}')
            if rest.startswith("model/"):
                body = self.api("pp_model_%s.json" % rest[6:])
                return js(body) if body else (404, "text/plain", b"no such model", {}, False)
            return notfound
        if path.startswith("/sf/"):
            if headers.get("authorization") != "Token " + SF_TOKEN:
                return (401, "application/json", b'{"detail": "Authentication credentials were not provided."}', {}, False)
            rest = path[4:]
            if rest == "search":
                return js(self.api("sf_search_%s.json" % query.get("q")) or b'{"results": []}')
            m = re.match(r"^models/([A-Za-z0-9]+)(/download)?$", rest)
            if m:
                body = self.api("sf_%s_%s.json" % ("download" if m.group(2) else "model", m.group(1)))
                return js(body) if body else (404, "application/json", b'{"detail": "Not found."}', {}, False)
            return notfound
        if path.startswith("/files/") or path.startswith("/cut/"):
            rel = urllib.parse.unquote(path.split("/", 2)[2])
            target = (self.root / "files" / rel).resolve()
            if not str(target).startswith(str((self.root / "files").resolve())) or not target.is_file():
                return notfound
            ctype = "image/png" if rel.endswith(".png") else "application/octet-stream"
            return (200, ctype, target.read_bytes(), {}, path.startswith("/cut/"))
        if path.startswith("/botcheck/"):
            return (403, "text/html", b"<!DOCTYPE html><title>Just a moment...</title>", {"cf-mitigated": "challenge"}, False)
        return notfound


# ------------------------------------------------------------------------------------------- Blender helpers

SCENE_SCRIPT = r'''
import bpy, sys, os
out = sys.argv[sys.argv.index("--") + 1]
bpy.ops.wm.read_homefile(use_empty=True)
scene = bpy.context.scene
scene.frame_end = 50
scene.frame_set(7)
world = bpy.data.worlds.new("UserWorld")
world.use_nodes = True
bg = next(n for n in world.node_tree.nodes if n.bl_idname == "ShaderNodeBackground")
bg.inputs["Strength"].default_value = 0.7
scene.world = world
props = bpy.data.collections.new("Props"); scene.collection.children.link(props)
clash = bpy.data.collections.new("Fixture Box"); scene.collection.children.link(clash)
def mat(name, rgb):
    m = bpy.data.materials.new(name); m.use_nodes = True
    next(n for n in m.node_tree.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled").inputs["Base Color"].default_value = rgb + (1,)
    return m
def move(ob, coll):
    for c in list(ob.users_collection): c.objects.unlink(ob)
    coll.objects.link(ob)
bpy.ops.mesh.primitive_cube_add(size=0.5, location=(0, -2, 0.25)); box = bpy.context.object; box.name = "Box"
box.data.materials.append(mat("UserMat", (0.2, 0.3, 0.9))); box.modifiers.new("Bevel", "BEVEL"); move(box, props)
bpy.ops.mesh.primitive_plane_add(size=2); floor = bpy.context.object; floor.name = "Floor"
floor.data.materials.append(mat("FloorMat", (0.5, 0.5, 0.5)))
bpy.ops.mesh.primitive_cube_add(size=1, location=(3, 0, 0.75)); wall = bpy.context.object; wall.name = "Wall"
wall.scale = (1, 0.1, 1.5)
while wall.data.uv_layers: wall.data.uv_layers.remove(wall.data.uv_layers[0])
wall.data.materials.append(mat("WallMat", (0.8, 0.8, 0.7)))
bpy.ops.mesh.primitive_cube_add(size=1, location=(-3, 0, 0.5)); a = bpy.context.object; a.name = "Shared_A"
a.data.name = "SharedMesh"; a.data.materials.append(mat("SharedMat", (0.9, 0.1, 0.1)))
b = a.copy(); b.name = "Shared_B"; b.location = (-3, 2, 0.5); scene.collection.objects.link(b)
user_fx = mat("fx_box_mat", (0.1, 0.9, 0.1)); user_fx.use_fake_user = True
arm = bpy.data.armatures.new("RigData"); rig = bpy.data.objects.new("Rig", arm); scene.collection.objects.link(rig)
bpy.context.view_layer.objects.active = rig
bpy.ops.object.mode_set(mode="EDIT"); bone = arm.edit_bones.new("Bone"); bone.tail = (0, 0, 1)
bpy.ops.object.mode_set(mode="POSE"); rig.pose.bones["Bone"].rotation_quaternion = (0.96, 0.28, 0, 0)
bpy.ops.object.mode_set(mode="OBJECT")
cam = bpy.data.objects.new("Camera", bpy.data.cameras.new("Camera")); scene.collection.objects.link(cam); scene.camera = cam
lamp = bpy.data.objects.new("Lamp", bpy.data.lights.new("Lamp", "SUN")); scene.collection.objects.link(lamp)
bpy.data.texts.new("Notes").write("user notes\n")
for ob in scene.objects: ob.select_set(ob.name in ("Box", "Wall"))
bpy.context.view_layer.objects.active = floor
bpy.ops.wm.save_as_mainfile(filepath=out)
print("SCENE_SAVED", out)
'''

PROBE_SCRIPT = r'''
import bpy, sys, json
import numpy as np
argv = sys.argv[sys.argv.index("--") + 1:]
out = argv[0]
scene = bpy.context.scene
layer = bpy.context.view_layer
def bounds(objs):
    dg = bpy.context.evaluated_depsgraph_get()
    lo = hi = None
    for ob in objs:
        if ob.type != "MESH":
            continue
        ev = ob.evaluated_get(dg)
        me = ev.to_mesh()
        for v in me.vertices:
            w = ev.matrix_world @ v.co
            p = np.array(w[:])
            lo = p if lo is None else np.minimum(lo, p)
            hi = p if hi is None else np.maximum(hi, p)
        ev.to_mesh_clear()
    return None if lo is None else {"min": lo.tolist(), "max": hi.tolist(), "size": (hi - lo).tolist()}
def tree(nt):
    nodes = {}
    for n in nt.nodes:
        d = {"type": n.bl_idname}
        if getattr(n, "image", None): d["image"] = n.image.filepath; d["colorspace"] = n.image.colorspace_settings.name; d["image_name"] = n.image.name
        if n.bl_idname == "ShaderNodeTexImage": d["projection"] = n.projection; d["label"] = n.label
        if n.bl_idname == "ShaderNodeMapping": d["scale"] = list(n.inputs["Scale"].default_value); d["rotation"] = list(n.inputs["Rotation"].default_value)
        if n.bl_idname == "ShaderNodeBackground": d["strength"] = n.inputs["Strength"].default_value
        if n.bl_idname == "ShaderNodeDisplacement": d["scale"] = n.inputs["Scale"].default_value; d["midlevel"] = n.inputs["Midlevel"].default_value
        if n.bl_idname == "ShaderNodeMath": d["op"] = n.operation
        nodes[n.name] = d
    links = [[l.from_node.name, l.from_socket.name, l.to_node.name, l.to_socket.name] for l in nt.links]
    return {"nodes": nodes, "links": links}
def all_objects(coll):
    obs = list(coll.objects)
    for c in coll.children: obs += all_objects(c)
    return obs
facts = {"file": bpy.data.filepath, "frame": scene.frame_current, "frame_end": scene.frame_end,
         "world": scene.world.name if scene.world else None,
         "active": layer.objects.active.name if layer.objects.active else None,
         "selected": sorted(o.name for o in layer.objects if o.select_get()),
         "scene_children": sorted(c.name for c in scene.collection.children),
         "objects": {}, "collections": {}, "materials": {}, "worlds": {}, "texts": {}, "images": {}}
for ob in bpy.data.objects:
    facts["objects"][ob.name] = {"type": ob.type, "data": ob.data.name if ob.data else None,
        "parent": ob.parent.name if ob.parent else None, "matrix": [list(r) for r in ob.matrix_world],
        "collections": sorted(c.name for c in ob.users_collection),
        "slots": [[s.link, s.material.name if s.material else None] for s in ob.material_slots],
        "data_materials": [m.name if m else None for m in ob.data.materials] if hasattr(ob.data, "materials") else None,
        "pose": [list(b.rotation_quaternion) for b in ob.pose.bones] if ob.pose else None}
for c in bpy.data.collections:
    facts["collections"][c.name] = {"objects": sorted(o.name for o in c.objects), "children": sorted(x.name for x in c.children),
        "bounds": bounds(all_objects(c)), "custom": {k: str(c[k]) for k in c.keys()}}
for m in bpy.data.materials:
    facts["materials"][m.name] = {"tree": tree(m.node_tree) if m.node_tree else None, "fake_user": m.use_fake_user, "users": m.users}
for w in bpy.data.worlds:
    facts["worlds"][w.name] = {"tree": tree(w.node_tree) if w.node_tree else None, "fake_user": w.use_fake_user}
for t in bpy.data.texts:
    facts["texts"][t.name] = t.as_string()
for i in bpy.data.images:
    facts["images"][i.name] = {"file": i.filepath, "packed": i.packed_file is not None, "colorspace": i.colorspace_settings.name}
with open(out, "w", encoding="utf-8") as f:
    json.dump(facts, f)
'''


def blender_run(ctx, blend, script_text, script_name, *extra):
    wait_memory()
    script = ctx.work / script_name
    script.write_text(script_text, encoding="utf-8")
    cmd = [ctx.blender, "--background", "--factory-startup", "--disable-autoexec"]
    if blend:
        cmd.append(str(blend))
    cmd += ["--python-exit-code", "3", "--python", str(script), "--", *[str(e) for e in extra]]
    log = ctx.work / (script_name + ".log")
    with open(log, "wb") as f:
        proc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, timeout=600)
    return proc.returncode, log


def probe(ctx, blend):
    out = ctx.work / ("probe_%s.json" % Path(blend).stem)
    if out.exists():
        out.unlink()
    code, log = blender_run(ctx, blend, PROBE_SCRIPT, "probe.py", out)
    if not out.is_file():
        raise RuntimeError("probe of %s failed (exit %d), see %s" % (blend, code, log))
    return json.loads(out.read_text(encoding="utf-8"))


def make_scene(ctx):
    target = ctx.work / "scene.blend"
    if not target.is_file():
        code, log = blender_run(ctx, None, SCENE_SCRIPT, "make_scene.py", target)
        if not target.is_file():
            raise RuntimeError("could not make the test scene, see %s" % log)
    return target


def links_into(tree, node_name):
    return [l for l in tree["links"] if l[2] == node_name]


def feeds(tree, start, goal_node_type, goal_socket, depth=0):
    """Does node `start` reach a node of `goal_node_type` at `goal_socket` through links?"""
    if depth > 6:
        return False
    for f, _fs, t, ts in tree["links"]:
        if f != start:
            continue
        if tree["nodes"][t]["type"] == goal_node_type and ts == goal_socket:
            return True
        if feeds(tree, t, goal_node_type, goal_socket, depth + 1):
            return True
    return False


def path_types(tree, start, depth=0):
    seen = []
    for f, _fs, t, _ts in tree["links"]:
        if f == start:
            seen.append(tree["nodes"][t]["type"])
            if depth < 6:
                seen += path_types(tree, t, depth + 1)
    return seen


# ------------------------------------------------------------------------------------------- rows

ROWS = []


def row(name):
    def wrap(fn):
        ROWS.append((name, fn))
        return fn
    return wrap


class Checks:
    def __init__(self, name):
        self.name, self.items = name, []

    def __call__(self, label, ok, detail=""):
        self.items.append({"check": label, "ok": bool(ok), "detail": str(detail)[:400]})
        print("  %s %s%s" % ("ok  " if ok else "FAIL", label, (" - " + str(detail)[:300]) if detail and not ok else ""),
              flush=True)
        return ok

    @property
    def ok(self):
        return bool(self.items) and all(i["ok"] for i in self.items)


@row("R01 fingerprint: equal with nothing between, every planted change found")
def r01(ctx, c):
    scene = make_scene(ctx)
    code, log = blender_run(ctx, scene, SELFTEST_SCRIPT, "selftest.py", ctx.scripts / "assets_blender.py",
                            ctx.work / "selftest.json")
    res = json.loads((ctx.work / "selftest.json").read_text(encoding="utf-8"))
    rows = res.get("rows", [])
    c("control: two looks with nothing between are equal", rows and rows[0]["want"] == "none" and not rows[0]["changes"],
      rows[:1])
    planted = [r for r in rows if r["want"] == "found"]
    c("at least 12 kinds of planted change tried", len(planted) >= 12, len(planted))
    for r in planted:
        c("planted change found: " + r["row"], bool(r["changes"]), r["changes"])
    c("the self-test says ok", res.get("ok"), res.get("error"))


SELFTEST_SCRIPT = r'''
import sys, json, runpy, bpy
argv = sys.argv[sys.argv.index("--") + 1:]
ns = runpy.run_path(argv[0], run_name="assets_blender_selftest")
res = ns["run_job"]({"op": "selftest_fingerprint", "mode": "headless"})
open(argv[1], "w", encoding="utf-8").write(json.dumps(res, default=str))
'''


@row("R02 search Poly Haven: ranked, filtered, thumbnails on disk, plain errors")
def r02(ctx, c):
    thumbs = ctx.work / "thumbs"
    shutil.rmtree(thumbs, ignore_errors=True)
    code, ans, err = ctx.tool("search", "--source", "polyhaven", "--type", "hdri", "--limit", "5")
    order = [r["id"] for r in ans.get("results", [])]
    c("control: without words the most downloaded come first", code == 0 and order[:2] == ["fx_sky", "fx_sky2"], order)
    code, ans, err = ctx.tool("search", "--source", "polyhaven", "--type", "hdri", "sky", "--limit", "5", "--thumbs", thumbs)
    order = [r["id"] for r in ans.get("results", [])]
    c("with words Poly Haven's ranking is kept and unknown slugs dropped", code == 0 and order == ["fx_sky2", "fx_sky"], order)
    first = (ans.get("results") or [{}])[0]
    fields = ("id", "name", "type", "license", "author", "sizes", "categories", "thumbnail_url", "page_url")
    c("each result has " + ", ".join(fields), all(k in first for k in fields), sorted(first))
    c("licence CC0, author, sizes from max_resolution", first.get("license") == "CC0" and first.get("author") == "Fixture Author"
      and first.get("sizes") == ["1k"], first)
    files = ans.get("thumbs", [])
    c("one thumbnail file per result, PNG on disk", len(files) == 2 and all(Path(f).read_bytes()[:4] == b"\x89PNG" for f in files), files)
    code, ans, err = ctx.tool("search", "--source", "polyhaven", "--type", "hdri", "down")
    c("search down (HTTP 503): keyword fallback with a note", code == 0 and ans.get("note"), ans.get("note"))
    code, ans, err = ctx.tool("search", "--source", "polyhaven", "--type", "texture", "--category", "NoSuch/Thing")
    c("unknown category: exit 1 with a plain reason", code == 1 and "does not know the category" in ans.get("error", ""), ans)
    code, ans, err = ctx.tool("search", "--source", "polyhaven", "--type", "texture", "--category", "Stone/Walls")
    c("a known category filters", code == 0 and [r["id"] for r in ans["results"]] == ["fx_rock"], ans.get("results"))
    code, ans, err = ctx.tool("categories", "--source", "polyhaven", "--type", "texture")
    c("categories lists the taxonomy paths", code == 0 and ans.get("categories") == ["Stone", "Stone/Walls", "Metal"], ans)


@row("R03 fetch: checked into the cache; a second fetch is a hit with no request")
def r03(ctx, c):
    shutil.rmtree(ctx.cache / "polyhaven" / "fx_sky", ignore_errors=True)
    dead = ctx.cache / "polyhaven" / "fx_sky" / "hdri-1k-hdr.partial-deadbeef" / "hdri"
    dead.mkdir(parents=True)
    (dead / "fx_sky_1k.hdr").write_bytes(b"half a file from a run that was killed")
    n0 = ctx.server.count()
    code, ans, err = ctx.tool("fetch", "polyhaven", "fx_sky")
    n1 = ctx.server.count()
    c("control: the first fetch is a miss and talks to the server", code == 0 and ans.get("cache") == "miss" and n1 - n0 >= 3,
      (ans.get("cache"), n1 - n0))
    path = Path(ans.get("files", ["?"])[0])
    want = hashlib.md5((FIXTURES / "files" / "fx_sky_1k.hdr").read_bytes()).hexdigest()
    c("the cached file is byte-identical to the source", path.is_file() and hashlib.md5(path.read_bytes()).hexdigest() == want, path)
    c("a manifest sits beside it", (Path(ans.get("dir", "?")) / "manifest.json").is_file(), ans.get("dir"))
    c("a partial folder left by a killed run was not used", path.is_file() and b"killed" not in path.read_bytes())
    code, ans, err = ctx.tool("fetch", "polyhaven", "fx_sky")
    c("second fetch: cache hit", code == 0 and ans.get("cache") == "hit", ans.get("cache"))
    c("second fetch: no request at all", ctx.server.count() == n1, ctx.server.since(n1))
    code, ans, err = ctx.tool("fetch", "polyhaven", "fx_sky", "--offline")
    c("--offline answers from the cache", code == 0 and ans.get("cache") == "hit", ans)
    code, ans, err = ctx.tool("cache", "--verify")
    entry = [e for e in ans.get("entries", []) if e["id"] == "fx_sky"]
    c("cache --verify lists the entry as sound", code == 0 and entry and entry[0].get("sound") is True, entry)


@row("R04 broken downloads are refused and never cached")
def r04(ctx, c):
    code, ans, err = ctx.tool("fetch", "polyhaven", "fx_sky2")
    c("control: the same file served whole is accepted", code == 0, ans.get("error"))
    for slug, words in (("fx_cut", "cut short"), ("fx_short", "the source says"), ("fx_badmd5", "checksum")):
        shutil.rmtree(ctx.cache / "polyhaven" / slug, ignore_errors=True)
        code, ans, err = ctx.tool("fetch", "polyhaven", slug)
        folder = ctx.cache / "polyhaven" / slug
        leftovers = [p.name for p in folder.glob("*")] if folder.exists() else []
        c("%s: refused with a plain reason (%s)" % (slug, words), code == 1 and words in ans.get("error", ""), ans.get("error"))
        c("%s: no cache entry and no partial left" % slug, not [n for n in leftovers if n != "info.json"], leftovers)


@row("R05 HDRI import (headless): a new world wired, the old one kept, credits written")
def r05(ctx, c):
    scene = make_scene(ctx)
    before = probe(ctx, scene)
    c("control: the scene starts on UserWorld without ASSET_CREDITS", before["world"] == "UserWorld" and "ASSET_CREDITS" not in before["texts"],
      (before["world"], list(before["texts"])))
    out = ctx.work / "r05.blend"
    code, ans, err = ctx.headless("import", "polyhaven", "fx_sky", "--blend", scene, "--save-as", out, "--strength", "0.8",
                                  "--rotation", "90")
    c("import ok, scene check ok", code == 0 and ans.get("import", {}).get("scene_check", {}).get("ok"), ans.get("error") or ans)
    if not out.is_file():
        return
    after = probe(ctx, out)
    world = after["worlds"].get(after["world"] or "", {})
    tree = world.get("tree") or {"nodes": {}, "links": []}
    types = {n["type"]: name for name, n in tree["nodes"].items()}
    c("the scene's world is the new one", after["world"] == "fx_sky", after["world"])
    env = types.get("ShaderNodeTexEnvironment")
    c("environment image is the cached HDR", env and tree["nodes"][env].get("image", "").endswith("fx_sky_1k.hdr"),
      env and tree["nodes"][env])
    c("Texture Coordinate > Mapping > Environment > Background > World Output",
      all(types.get(t) for t in ("ShaderNodeTexCoord", "ShaderNodeMapping", "ShaderNodeBackground", "ShaderNodeOutputWorld"))
      and feeds(tree, types["ShaderNodeTexCoord"], "ShaderNodeMapping", "Vector")
      and feeds(tree, types["ShaderNodeMapping"], "ShaderNodeTexEnvironment", "Vector")
      and feeds(tree, env, "ShaderNodeBackground", "Color")
      and feeds(tree, types["ShaderNodeBackground"], "ShaderNodeOutputWorld", "Surface"), tree["links"])
    bg = tree["nodes"].get(types.get("ShaderNodeBackground"), {})
    mp = tree["nodes"].get(types.get("ShaderNodeMapping"), {})
    c("strength 0.8 and rotation 90 degrees about Z", abs(bg.get("strength", 0) - 0.8) < 1e-6
      and abs((mp.get("rotation") or [0, 0, 0])[2] - 1.5707963) < 1e-5, (bg, mp))
    c("the old world is kept with a fake user", after["worlds"].get("UserWorld", {}).get("fake_user") is True, after["worlds"].get("UserWorld"))
    credits = after["texts"].get("ASSET_CREDITS", "")
    c("ASSET_CREDITS has one line with source, id, name, author, licence, page",
      credits.count("\n") == 1 and "| polyhaven | fx_sky | Fixture Sky | Fixture Author | CC0 | https://polyhaven.com/a/fx_sky" in credits, credits)
    c("the JSON carries the same credit line", ans.get("import", {}).get("credit", {}).get("line", "").strip() == credits.strip(),
      ans.get("import", {}).get("credit"))
    c("frame, selection and active object as they were", (after["frame"], after["selected"], after["active"]) ==
      (before["frame"], before["selected"], before["active"]), (after["frame"], after["selected"], after["active"]))
    code, ans, err = ctx.headless("credits", "--blend", out)
    c("`credits` reads the line back", code == 0 and ans.get("credits") == credits.splitlines(), ans.get("credits"))
    out2 = ctx.work / "r05b.blend"
    code, ans, err = ctx.headless("import", "polyhaven", "fx_sky", "--blend", scene, "--save-as", out2, "--replace-world")
    after2 = probe(ctx, out2) if out2.is_file() else {"worlds": {"UserWorld": {}}}
    c("--replace-world: no fake user, so the unused old world is gone from the saved file", code == 0
      and "UserWorld" not in after2["worlds"], list(after2["worlds"]))


@row("R06 texture import (headless): every map wired, real-world tiling, shared mesh protected")
def r06(ctx, c):
    scene = make_scene(ctx)
    before = probe(ctx, scene)
    c("control: Floor has FloorMat, Shared_B has SharedMat", before["objects"]["Floor"]["slots"] == [["DATA", "FloorMat"]]
      and before["objects"]["Shared_B"]["slots"] == [["DATA", "SharedMat"]], before["objects"]["Floor"]["slots"])
    out = ctx.work / "r06.blend"
    code, ans, err = ctx.headless("import", "polyhaven", "fx_rock", "--blend", scene, "--save-as", out,
                                  "--apply-to", "Floor,Wall,Shared_A")
    imp = ans.get("import", {})
    c("import ok, scene check ok", code == 0 and imp.get("scene_check", {}).get("ok"), ans.get("error") or imp.get("scene_check"))
    if not out.is_file():
        return
    after = probe(ctx, out)
    floor_mat = after["objects"]["Floor"]["slots"][0][1]
    tree = after["materials"][floor_mat]["tree"]
    tex = {n["label"]: name for name, n in tree["nodes"].items() if n["type"] == "ShaderNodeTexImage"}
    c("maps loaded: Diffuse, Rough, nor_gl, Displacement (and not AO, arm, nor_dx)",
      sorted(tex) == ["Diffuse", "Displacement", "Rough", "nor_gl"], sorted(tex))
    bsdf = next(n for n, d in tree["nodes"].items() if d["type"] == "ShaderNodeBsdfPrincipled")
    c("Diffuse -> Base Color, colour space sRGB", feeds(tree, tex.get("Diffuse"), "ShaderNodeBsdfPrincipled", "Base Color")
      and tree["nodes"][tex["Diffuse"]]["colorspace"] == "sRGB", tree["nodes"].get(tex.get("Diffuse")))
    c("Rough -> Roughness, Non-Color", feeds(tree, tex.get("Rough"), "ShaderNodeBsdfPrincipled", "Roughness")
      and tree["nodes"][tex["Rough"]]["colorspace"] == "Non-Color", tree["nodes"].get(tex.get("Rough")))
    c("nor_gl -> Normal Map -> Normal, Non-Color", feeds(tree, tex.get("nor_gl"), "ShaderNodeNormalMap", "Color")
      and feeds(tree, tex.get("nor_gl"), "ShaderNodeBsdfPrincipled", "Normal")
      and tree["nodes"][tex["nor_gl"]]["colorspace"] == "Non-Color", path_types(tree, tex.get("nor_gl")))
    disp = next((d for d in tree["nodes"].values() if d["type"] == "ShaderNodeDisplacement"), {})
    c("Displacement -> Displacement node -> Material Output, scale 0.02 from the MaterialX, midlevel 0.5",
      feeds(tree, tex.get("Displacement"), "ShaderNodeOutputMaterial", "Displacement")
      and abs(disp.get("scale", 0) - 0.02) < 1e-6 and abs(disp.get("midlevel", 0) - 0.5) < 1e-6, disp)
    mapping = next(d for d in tree["nodes"].values() if d["type"] == "ShaderNodeMapping")
    c("Floor (2 m plane, UVs 0-1) tiles at real size: Mapping scale 2/1.8", all(abs(v - 2 / 1.8) < 1e-4 for v in mapping["scale"][:2]),
      mapping["scale"])
    wall_mat = after["objects"]["Wall"]["slots"][0][1]
    wtree = after["materials"][wall_mat]["tree"]
    wmap = next(d for d in wtree["nodes"].values() if d["type"] == "ShaderNodeMapping")
    wtex = [d for d in wtree["nodes"].values() if d["type"] == "ShaderNodeTexImage"]
    c("Wall (no UV map): box projection, scale = object scale / 1.8",
      all(t["projection"] == "BOX" for t in wtex) and all(abs(a - b / 1.8) < 1e-4 for a, b in zip(wmap["scale"], (1, 0.1, 1.5))),
      (wmap["scale"], [t["projection"] for t in wtex]))
    c("Shared_A got it on its object slot", after["objects"]["Shared_A"]["slots"][0][0] == "OBJECT", after["objects"]["Shared_A"]["slots"])
    c("Shared_B (same mesh, not named) keeps SharedMat", after["objects"]["Shared_B"]["slots"] == [["DATA", "SharedMat"]],
      after["objects"]["Shared_B"]["slots"])
    prev = {a["object"]: a["previous"] for a in imp.get("applied", [])}
    c("the JSON lists what each object had before", prev.get("Floor") == ["FloorMat"] and prev.get("Wall") == ["WallMat"], prev)
    skipped = {s["map"] for s in imp.get("skipped_maps", [])}
    c("skipped maps listed with a reason: AO, arm, nor_dx", {"AO", "arm", "nor_dx"} <= skipped, imp.get("skipped_maps"))
    c("FloorMat itself untouched", after["materials"]["FloorMat"]["tree"] == before["materials"]["FloorMat"]["tree"])
    credits = after["texts"].get("ASSET_CREDITS", "")
    c("credit line written", "| polyhaven | fx_rock | Fixture Rock | Fixture Author | CC0 |" in credits, credits)


@row("R07 texture fallbacks: col_1 as base colour, DirectX normal flipped, arm green as roughness")
def r07(ctx, c):
    out = ctx.work / "r07.blend"
    scene = make_scene(ctx)
    code, ans, err = ctx.headless("import", "polyhaven", "fx_metal", "--blend", scene, "--save-as", out, "--apply-to", "Floor",
                                  "--pack")
    c("import ok", code == 0, ans.get("error"))
    if not out.is_file():
        return
    after = probe(ctx, out)
    tree = after["materials"][after["objects"]["Floor"]["slots"][0][1]]["tree"]
    tex = {n["label"]: name for name, n in tree["nodes"].items() if n["type"] == "ShaderNodeTexImage"}
    c("control: four maps loaded", sorted(tex) == ["Metal", "arm", "col_1", "nor_dx"], sorted(tex))
    c("col_1 -> Base Color", feeds(tree, tex.get("col_1"), "ShaderNodeBsdfPrincipled", "Base Color"))
    c("Metal -> Metallic", feeds(tree, tex.get("Metal"), "ShaderNodeBsdfPrincipled", "Metallic"))
    types = path_types(tree, tex.get("nor_dx"))
    c("nor_dx goes through Separate, a subtract (1 - green) and Combine into the Normal Map",
      "ShaderNodeSeparateColor" in types and "ShaderNodeMath" in types and "ShaderNodeCombineColor" in types
      and feeds(tree, tex.get("nor_dx"), "ShaderNodeBsdfPrincipled", "Normal"), types)
    sep = [l for l in tree["links"] if l[0] in [n for n, d in tree["nodes"].items() if d["type"] == "ShaderNodeSeparateColor"]]
    c("arm's green drives Roughness, its blue not Metallic (Metal does)",
      any(l[1] == "Green" and l[3] == "Roughness" for l in sep) and not any(l[3] == "Metallic" for l in sep), sep)
    mapping = next(d for d in tree["nodes"].values() if d["type"] == "ShaderNodeMapping")
    c("1 m x 0.5 m texture: scale x = 2/1, y = 2/0.5", abs(mapping["scale"][0] - 2.0) < 1e-4 and abs(mapping["scale"][1] - 4.0) < 1e-4,
      mapping["scale"])
    imgs = [n["image_name"] for n in tree["nodes"].values() if n["type"] == "ShaderNodeTexImage"]
    c("--pack: every map is packed into the file", imgs and all(after["images"][i]["packed"] for i in imgs),
      {i: after["images"][i]["packed"] for i in imgs})


@row("R08 model import (headless): own collection, size, on the ground, names kept")
def r08(ctx, c):
    scene = make_scene(ctx)
    expected = json.loads((FIXTURES / "api" / "expected.json").read_text(encoding="utf-8"))["box_bounds"]
    blend_probe = probe(ctx, FIXTURES / "files" / "fx_boxblend_1k.blend")
    known = blend_probe["collections"]["fx_boxblend"]["bounds"]
    c("instrument control: the probe measures the fixture's known box", all(abs(a - b) < 1e-5 for a, b in zip(known["min"] + known["max"],
      expected["min"] + expected["max"])), known)
    before = probe(ctx, scene)
    out = ctx.work / "r08.blend"
    code, ans, err = ctx.headless("import", "polyhaven", "fx_box", "--blend", scene, "--save-as", out, "--size", "1.2",
                                  "--at", "2,1,0.5")
    imp = ans.get("import", {})
    c("import ok, scene check ok", code == 0 and imp.get("scene_check", {}).get("ok"), ans.get("error") or imp.get("scene_check"))
    authored = imp.get("authored") or {}
    c("the tool's own measure of the file agrees with the known box", authored and all(
        abs(a - b) < 1e-5 for a, b in zip(authored["min"] + authored["max"], expected["min"] + expected["max"])), authored)
    c("control: the box as authored is not on the ground (lowest z 0.1)", abs(expected["min"][2] - 0.1) < 1e-6)
    if not out.is_file():
        return
    after = probe(ctx, out)
    coll = imp.get("collection")
    c("a new collection named for it; the user's 'Fixture Box' keeps its name", coll == "Fixture Box.001"
      and "Fixture Box" in after["collections"] and after["collections"]["Fixture Box"]["objects"] == [], coll)
    info = after["collections"].get(coll, {})
    b = info.get("bounds") or {"size": [0, 0, 0], "min": [0, 0, 0], "max": [0, 0, 0]}
    c("largest side 1.2 m (within 1 %)", abs(max(b["size"]) - 1.2) <= 0.012, b["size"])
    c("proportions kept", abs(b["size"][2] / b["size"][1] - expected["size"][2] / expected["size"][1]) < 1e-4, b["size"])
    c("lowest point on z = 0.5", abs(b["min"][2] - 0.5) < 1e-5, b["min"])
    c("footprint centred on (2, 1)", abs((b["min"][0] + b["max"][0]) / 2 - 2) < 1e-5 and abs((b["min"][1] + b["max"][1]) / 2 - 1) < 1e-5, b)
    new_objs = [n for n in after["objects"] if n not in before["objects"]]
    c("all new objects live in that collection only", new_objs and all(after["objects"][n]["collections"] == [coll] for n in new_objs),
      {n: after["objects"][n]["collections"] for n in new_objs})
    renamed = {(r["kind"], r["wanted"]) for r in imp.get("renamed", [])}
    c("collisions reported: collection, object Box, material fx_box_mat",
      {("collections", "Fixture Box"), ("objects", "Box"), ("materials", "fx_box_mat")} <= renamed, imp.get("renamed"))
    c("the user's Box is untouched", after["objects"]["Box"] == before["objects"]["Box"], after["objects"]["Box"])
    c("the user's fx_box_mat is untouched", after["materials"]["fx_box_mat"]["tree"] == before["materials"]["fx_box_mat"]["tree"])
    c("frame, selection and active object as they were", (after["frame"], after["selected"], after["active"]) ==
      (before["frame"], before["selected"], before["active"]), (after["frame"], after["selected"], after["active"]))
    c("published size checked against the measured one", imp.get("authored_matches_published") is True, imp.get("published_size_m"))
    c("credit line written", "| polyhaven | fx_box | Fixture Box |" in after["texts"].get("ASSET_CREDITS", ""))
    out2 = ctx.work / "r08b.blend"
    code, ans, err = ctx.headless("import", "polyhaven", "fx_box", "--blend", scene, "--save-as", out2, "--no-ground",
                                  "--rotate", "90")
    imp2 = ans.get("import", {})
    fin = imp2.get("final") or {"min": [0, 0, 0], "size": [0, 0, 0]}
    turned = [expected["size"][1], expected["size"][0], expected["size"][2]]
    c("--no-ground --rotate 90, no --size: real size, the file's own height, x and y swapped", code == 0
      and abs(fin["min"][2] - 0.1) < 1e-5 and all(abs(a - b) < 1e-5 for a, b in zip(fin["size"], turned)), fin)


@row("R09 archives and include paths that climb out are refused")
def r09(ctx, c):
    code, ans, err = ctx.tool("fetch", "sketchfab", SF_OK)
    c("control: a sound zip is fetched and unpacked", code == 0 and ans.get("meta", {}).get("main") == "model/scene/fx_box_1k.gltf",
      ans.get("error") or ans.get("meta", {}).get("main"))
    for uid, what in ((SF_SLIP, "climbs out"), (SF_ABS, "absolute path")):
        code, ans, err = ctx.tool("fetch", "sketchfab", uid)
        c("%s: refused, says why" % what, code == 1 and what in ans.get("error", "") and "nothing extracted" in ans.get("error", ""),
          ans.get("error"))
        c("%s: no cache entry" % what, not (ctx.cache / "sketchfab" / uid / "model-gltf" / "manifest.json").exists())
    escaped = list(ctx.work.rglob("zipslip_*.txt"))
    c("nothing landed anywhere under the work folder", not escaped, escaped)
    code, ans, err = ctx.tool("fetch", "polyhaven", "fx_slip")
    c("a Poly Haven include path with '..' is refused", code == 1 and "climbs out" in ans.get("error", ""), ans.get("error"))


@row("R10 a damaged cache file is caught: offline fails plainly, online fetches it again")
def r10(ctx, c):
    code, ans, err = ctx.tool("fetch", "polyhaven", "fx_sky")
    path = Path(ans["files"][0])
    code, ans, err = ctx.tool("fetch", "polyhaven", "fx_sky", "--offline")
    c("control: the sound entry is a hit offline", code == 0 and ans.get("cache") == "hit", ans)
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    path.write_bytes(bytes(data))
    code, ans, err = ctx.tool("fetch", "polyhaven", "fx_sky", "--offline")
    c("offline: refused, names the damage", code == 1 and "damaged" in ans.get("error", ""), ans.get("error"))
    out = ctx.work / "r10.blend"
    code, ans, err = ctx.tool("import", "polyhaven", "fx_sky", "--offline", "--blend", make_scene(ctx), "--save-as", out)
    c("an import offline stops before Blender (no file written)", code == 1 and not out.exists(), ans.get("error"))
    code, ans, err = ctx.tool("fetch", "polyhaven", "fx_sky")
    c("online: fetched again ('repaired') and sound", code == 0 and ans.get("cache") == "repaired"
      and hashlib.md5(path.read_bytes()).hexdigest() == hashlib.md5((FIXTURES / "files" / "fx_sky_1k.hdr").read_bytes()).hexdigest(),
      ans.get("cache"))


@row("R11 no network, unknown id, bad id: one plain reason each")
def r11(ctx, c):
    code, ans, err = ctx.tool("fetch", "polyhaven", "fx_sky2")
    c("control: a known id works", code == 0, ans.get("error"))
    env = ctx.env(BLENDER_AI_ASSETS_POLYHAVEN_API="http://127.0.0.1:9/ph", BLENDER_AI_ASSETS_DIR=str(ctx.work / "cache_empty"))
    code, ans, err = ctx.tool("fetch", "polyhaven", "fx_sky", env=env)
    c("no network: 'could not reach'", code == 1 and ans.get("error", "").startswith("no network: could not reach 127.0.0.1"), ans.get("error"))
    code, ans, err = ctx.tool("fetch", "polyhaven", "nope_zz")
    c("unknown id: HTTP 404 said plainly", code == 1 and "has no asset 'nope_zz'" in ans.get("error", ""), ans.get("error"))
    n = ctx.server.count()
    code, ans, err = ctx.tool("fetch", "polyhaven", "../etc/passwd")
    c("bad id: refused before any request", code == 1 and "is not a Poly Haven id" in ans.get("error", "") and ctx.server.count() == n,
      (ans.get("error"), ctx.server.since(n)))
    code, ans, err = ctx.tool("fetch", "polyhaven", "fx_sky", "--res", "9k")
    c("a resolution it does not have: says what it has", code == 1 and "it has 1k" in ans.get("error", ""), ans.get("error"))


@row("R12 a full disk: plain reason, nothing kept")
def r12(ctx, c):
    sys.path.insert(0, str(ctx.scripts))
    try:
        import importlib
        if "assets" in sys.modules:
            del sys.modules["assets"]
        assets = importlib.import_module("assets")
    finally:
        sys.path.pop(0)
    os.environ["BLENDER_AI_ASSETS_DIR"] = str(ctx.work / "cache_disk")
    os.environ["BLENDER_AI_ASSETS_POLYHAVEN_API"] = ctx.server.base + "/ph"
    try:
        shutil.rmtree(ctx.work / "cache_disk", ignore_errors=True)
        manifest, state = assets.ph_fetch("fx_sky", None, "1k", None, False)
        c("control: the same fetch without a full disk works", state == "miss" and manifest["files"])
        shutil.rmtree(ctx.work / "cache_disk", ignore_errors=True)
        real_open = open

        def full_open(path, mode="r", *a, **kw):
            handle = real_open(path, mode, *a, **kw)
            if "b" in mode and ("x" in mode or "w" in mode):
                class Full:
                    def __enter__(self):
                        return self

                    def __exit__(self, *e):
                        handle.close()

                    def write(self, _data):
                        raise OSError(28, "No space left on device")
                return Full()
            return handle
        assets.open = full_open
        try:
            assets.ph_fetch("fx_sky", None, "1k", None, False)
            c("full disk refused", False, "no error")
        except assets.Fail as exc:
            c("full disk: plain reason", "disk full" in str(exc), exc)
        finally:
            del assets.open
        left = [p for p in (ctx.work / "cache_disk").rglob("*") if p.is_file() and p.name != "info.json"]
        c("nothing kept in the cache", not left, left)
    finally:
        os.environ.pop("BLENDER_AI_ASSETS_DIR", None)
        os.environ.pop("BLENDER_AI_ASSETS_POLYHAVEN_API", None)


@row("R13 missing keys: Poly Pizza and Sketchfab say so and send nothing")
def r13(ctx, c):
    env = ctx.env(keys=False)
    code, ans, err = ctx.tool("search", "--source", "polypizza", "crate")
    c("control: with the key the search reaches the server", code == 0 and ans.get("results"), ans.get("error"))
    n = ctx.server.count()
    for argv, var in ((("search", "--source", "polypizza", "crate"), "POLYPIZZA_API_KEY"),
                      (("fetch", "polypizza", "PPfxCrate"), "POLYPIZZA_API_KEY"),
                      (("search", "--source", "sketchfab", "box"), "SKETCHFAB_API_TOKEN"),
                      (("fetch", "sketchfab", SF_OK), "SKETCHFAB_API_TOKEN")):
        shutil.rmtree(ctx.cache / argv[1] if argv[0] == "fetch" else ctx.work / "none", ignore_errors=True)
        code, ans, err = ctx.tool(*argv, env=env)
        c("%s without %s: says so" % (" ".join(argv[:2]), var), code == 1 and var in ans.get("error", "")
          and "Nothing was requested" in ans.get("error", ""), ans.get("error"))
    c("no request reached the server", ctx.server.count() == n, ctx.server.since(n))


@row("R14 Poly Pizza (fixture key): search, GLB checked, key kept from the file server, CC-BY credited")
def r14(ctx, c):
    code, ans, err = ctx.tool("search", "--source", "polypizza", "crate")
    rec = (ans.get("results") or [{}])[0]
    c("search: licence, author, page, attribution", rec.get("license") == "CC-BY 3.0" and rec.get("author") == "Fixture Maker"
      and rec.get("page_url") == "https://poly.pizza/m/PPfxCrate", rec)
    shutil.rmtree(ctx.cache / "polypizza", ignore_errors=True)
    n = ctx.server.count()
    code, ans, err = ctx.tool("fetch", "polypizza", "PPfxCrate")
    c("fetch ok", code == 0, ans.get("error"))
    cdn = [r for r in ctx.server.since(n) if r["path"].startswith("/files/")]
    c("control: the model call carried the key", any(r["headers"].get("x-auth-token") == PP_KEY for r in ctx.server.since(n)
                                                   if r["path"].startswith("/pp/")))
    c("the file server got no key", cdn and not any("x-auth-token" in r["headers"] for r in cdn), cdn)
    code, ans, err = ctx.tool("fetch", "polypizza", "PPfxBot")
    c("a bot check is named as such", code == 1 and "bot check" in ans.get("error", ""), ans.get("error"))
    out = ctx.work / "r14.blend"
    code, ans, err = ctx.headless("import", "polypizza", "PPfxCrate", "--blend", make_scene(ctx), "--save-as", out, "--size", "1.0")
    imp = ans.get("import", {})
    c("import ok, largest side 1.0", code == 0 and abs((imp.get("final_largest_m") or 0) - 1.0) < 0.01, ans.get("error") or imp.get("final"))
    line = imp.get("credit", {}).get("line", "")
    c("credit: CC-BY 3.0 by Fixture Maker with the page", "| polypizza | PPfxCrate | Fixture Crate | Fixture Maker | CC-BY 3.0 | https://poly.pizza/m/PPfxCrate" in line, line)


@row("R15 Sketchfab (fixture token): licence per model, zip and glb downloads, import")
def r15(ctx, c):
    code, ans, err = ctx.tool("search", "--source", "sketchfab", "box", "--limit", "2")
    recs = ans.get("results") or []
    c("search: each result has its licence and author", len(recs) == 2 and all(r.get("license") == "CC Attribution"
      and r.get("author") == "Fixture Artist" for r in recs), recs[:1])
    code, ans, err = ctx.tool("fetch", "sketchfab", SF_GLB)
    c("glb download checked (header length = file size)", code == 0 and ans.get("variant") == "model-glb", ans.get("error"))
    out = ctx.work / "r15.blend"
    code, ans, err = ctx.headless("import", "sketchfab", SF_OK, "--blend", make_scene(ctx), "--save-as", out, "--size", "0.5")
    imp = ans.get("import", {})
    c("import ok, largest side 0.5", code == 0 and abs((imp.get("final_largest_m") or 0) - 0.5) < 0.005, ans.get("error"))
    line = imp.get("credit", {}).get("line", "")
    c("credit: licence and author from the model's own record", "| sketchfab | %s | Fixture Zip Box | Fixture Artist | CC Attribution |" % SF_OK in line, line)


def ensure_kit(ctx):
    kit = ctx.work / "kit"
    if not (kit / "done").exists():
        shutil.rmtree(kit, ignore_errors=True)
        kit.mkdir(parents=True)
        blender_run(ctx, None, KIT_SCRIPT, "make_kit.py", kit)
    return kit


@row("R16 import-file: local glb, fbx, obj, stl, ply, usd, abc, blend and hdr")
def r16(ctx, c):
    kit = ensure_kit(ctx)
    c("control: the kit was exported", (kit / "done").exists(), kit)
    for name in ("kit.glb", "kit.fbx", "kit.obj", "kit.stl", "kit.ply", "kit.usdc", "kit.abc", "kit.blend"):
        out = ctx.work / ("r16_" + name.replace(".", "_") + ".blend")
        extra = ["--pick", "Kit"] if name.endswith(".blend") else []
        code, ans, err = ctx.headless("import-file", kit / name, "--new", "--save-as", out, "--size", "0.5", "--at", "1,1,0", *extra)
        imp = ans.get("import", {})
        fin = imp.get("final") or {"min": [9, 9, 9], "size": [0, 0, 0]}
        c("%s: ok, largest 0.5, on the ground, in its own collection" % name, code == 0 and abs(max(fin["size"]) - 0.5) < 0.005
          and abs(fin["min"][2]) < 1e-5 and imp.get("collection") == "kit", ans.get("error") or fin)
    keep = ctx.tmp / "kept"
    shutil.rmtree(keep, ignore_errors=True)
    keep.mkdir()
    code, ans, err = ctx.headless("import-file", kit / "kit.blend", "--new", "--save-as", ctx.work / "r16_drv.blend",
                                  env=ctx.env(BLENDER_AI_ASSETS_KEEP_WORK="1", TEMP=keep, TMP=keep))
    imp = ans.get("import", {})
    muted = imp.get("scripted_drivers_muted") or []
    c(".blend: the driver that would run Python arrives muted", code == 0 and len(muted) == 1 and muted[0].startswith("Cone:"), muted)
    logs = "".join(p.read_text(encoding="utf-8", errors="replace") for p in keep.rglob("blender.log"))
    c(".blend: its auto-run text did not run, nothing tried the driver", logs and "KIT TEXT RAN" not in logs
      and "PyDriver" not in logs, len(logs))
    code, ans, err = ctx.headless("import-file", kit / "anim.glb", "--new", "--save-as", ctx.work / "r16_anim.blend", "--size", "0.5")
    imp = ans.get("import", {})
    fin = imp.get("final") or {"min": [9, 9, 9], "size": [0]}
    c("an animated root hangs under a placement empty; size and ground still right", code == 0 and imp.get("placement_empty")
      and abs(max(fin["size"]) - 0.5) < 0.005 and abs(fin["min"][2]) < 1e-5, (ans.get("error"), imp.get("placement_empty"), fin))
    code, ans, err = ctx.headless("import", "polyhaven", "fx_boxblend", "--format", "blend", "--new", "--save-as",
                                  ctx.work / "r16_phblend.blend", "--size", "1")
    imp = ans.get("import", {})
    c("a Poly Haven .blend: its own collection appended, the Python driver muted", code == 0
      and imp.get("how") == "collection 'fx_boxblend'" and any(m.startswith("Lid:") for m in imp.get("scripted_drivers_muted") or []),
      (ans.get("error"), imp.get("how"), imp.get("scripted_drivers_muted")))
    code, ans, err = ctx.headless("import-file", FIXTURES / "files" / "fx_sky_1k.hdr", "--new", "--save-as", ctx.work / "r16_hdr.blend")
    c("a local .hdr becomes the world", code == 0 and ans.get("import", {}).get("world") == "fx_sky_1k.hdr", ans.get("error"))


KIT_SCRIPT = r'''
import bpy, sys, os
out = sys.argv[sys.argv.index("--") + 1]
bpy.ops.wm.read_homefile(use_empty=True)
bpy.ops.mesh.primitive_cone_add(radius1=0.2, depth=0.8, location=(0.5, -0.3, 0.6))
cone = bpy.context.object; cone.name = "Cone"
kit = bpy.data.collections.new("Kit"); bpy.context.scene.collection.children.link(kit)
bpy.context.scene.collection.objects.unlink(cone); kit.objects.link(cone)
cone["p"] = 0.0
fc = cone.driver_add('["p"]'); fc.driver.type = "SCRIPTED"; fc.driver.expression = "0.0 if frame > -1 else __import__('os').getcwd()"
t = bpy.data.texts.new("kit_autorun.py"); t.write("print('KIT TEXT RAN')"); t.use_module = True
for o in bpy.data.objects: o.select_set(True)
bpy.context.view_layer.objects.active = cone
p = lambda n: os.path.join(out, n)
bpy.ops.export_scene.gltf(filepath=p("kit.glb"), export_format="GLB")
bpy.ops.export_scene.fbx(filepath=p("kit.fbx"))
bpy.ops.wm.obj_export(filepath=p("kit.obj"))
bpy.ops.wm.stl_export(filepath=p("kit.stl"))
bpy.ops.wm.ply_export(filepath=p("kit.ply"))
bpy.ops.wm.usd_export(filepath=p("kit.usdc"))
bpy.ops.wm.alembic_export(filepath=p("kit.abc"))
bpy.ops.wm.save_as_mainfile(filepath=p("kit.blend"), copy=True)
bpy.ops.wm.read_homefile(use_empty=True)
bpy.ops.mesh.primitive_cube_add(size=0.4, location=(0, 0, 1)); cube = bpy.context.object; cube.name = "Hopper"
cube.keyframe_insert("location", frame=1); cube.location.x = 2.0; cube.keyframe_insert("location", frame=20)
bpy.context.scene.frame_set(1)
bpy.ops.export_scene.gltf(filepath=p("anim.glb"), export_format="GLB", export_animations=True)
bpy.ops.wm.read_homefile(use_empty=True)
e = bpy.data.objects.new("OnlyEmpty", None); bpy.context.scene.collection.objects.link(e)
bpy.ops.export_scene.gltf(filepath=p("empty.glb"), export_format="GLB")
open(p("broken.gltf"), "w").write('{"asset": {"version": "2.0"}, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 7}]}')
open(p("done"), "w").write("ok")
'''


@row("R17 a failed import changes nothing: refused early, or rolled back after it began")
def r17(ctx, c):
    scene = make_scene(ctx)
    kit = ensure_kit(ctx)
    before = probe(ctx, scene)
    out = ctx.work / "r17.blend"
    code, ans, err = ctx.headless("import", "polyhaven", "fx_rock", "--blend", scene, "--save-as", out, "--apply-to", "NoSuchObject")
    imp = ans.get("import", {})
    c("unknown --apply-to: refused, scene unchanged", code == 1 and "no object named 'NoSuchObject'" in ans.get("error", "")
      and imp.get("scene_unchanged") is True, ans.get("error"))
    code, ans, err = ctx.headless("import-file", kit / "empty.glb", "--blend", scene, "--save-as", out, "--size", "1")
    imp = ans.get("import", {})
    c("failure after objects were made (nothing to measure for --size): rolled back", code == 1 and "no geometry" in ans.get("error", "")
      and imp.get("scene_unchanged") is True and not imp.get("scene_check", {}).get("unexpected_changes"), (ans.get("error"), imp.get("scene_check")))
    c("nothing saved after a failure", not out.exists())
    code, ans, err = ctx.headless("import-file", kit / "broken.gltf", "--blend", scene, "--save-as", out)
    imp = ans.get("import", {})
    c("a broken file: the importer's error, scene unchanged", code == 1 and "importer failed" in ans.get("error", "")
      and imp.get("scene_unchanged") is True, ans.get("error"))
    code, ans, err = ctx.tool("import-file", kit / "kit.glb", "--blend", scene, "--save-as", scene)
    c("--save-as over the input file: refused", code == 1 and "never saves over it" in ans.get("error", ""), ans.get("error"))
    code, ans, err = ctx.tool("import-file", kit / "kit.glb", "--save-as", ctx.work / "x.blend",
                              env=ctx.env(BLENDER_MCP_PORT="1"))
    c("--save-as on a live session: refused", code == 1 and "live session is never saved" in ans.get("error", ""), ans.get("error"))
    after = probe(ctx, scene)
    c("control: the input scene file itself is unchanged", after == before)


@row("R18 LIVE through the bridge: identity refusals, an import, a failure that changes nothing")
def r18(ctx, c):
    if ctx.bridge is None:
        c("bridge rows need --bridge-zip and --bridge-port", False, "skipped")
        return
    port, pid = ctx.bridge["port"], ctx.bridge["pid"]
    env = ctx.env(BLENDER_MCP_HOST="127.0.0.1", BLENDER_MCP_PORT=port)
    look = LIVE_LOOK
    state0 = bridge_exec(port, look)
    code, ans, err = ctx.tool("import", "polyhaven", "fx_box", "--expect-pid", str(pid + 100000), env=env)
    c("wrong --expect-pid: refused, nothing touched", code == 1 and "WRONG BLENDER" in ans.get("error", "")
      and ans.get("import", {}).get("touched") is False, ans.get("error"))
    code, ans, err = ctx.tool("import", "polyhaven", "fx_box", "--expect-file", "other.blend", env=env)
    c("wrong --expect-file: refused", code == 1 and "WRONG BLENDER" in ans.get("error", ""), ans.get("error"))
    c("control: the scene after refusals is as before", bridge_exec(port, look) == state0)
    e0 = bridge_exec(port, EDIT_ON)
    c("control: the live Blender is in Edit Mode with an edit not yet written to the mesh",
      e0["mode"] == "EDIT_MESH" and abs(e0["z"] - e0["mesh_z"] - 0.1) < 1e-6, e0)
    code, ans, err = ctx.tool("import", "polyhaven", "fx_box", "--size", "0.8", "--at", "0,3,0", "--expect-pid", str(pid), env=env)
    imp = ans.get("import", {})
    c("live import ok, scene check ok", code == 0 and imp.get("scene_check", {}).get("ok") and ans.get("mode") == "live",
      ans.get("error") or imp.get("scene_check"))
    e1 = bridge_exec(port, EDIT_LOOK)
    c("Edit Mode put back on the same objects, the unsaved edit kept", e1["mode"] == "EDIT_MESH"
      and e1["editing"] == e0["editing"] and abs(e1["z"] - e0["z"]) < 1e-6, (e0, e1))
    bridge_exec(port, EDIT_OFF)
    state1 = bridge_exec(port, look)
    coll = imp.get("collection")
    c("the live scene has the new collection", coll in state1["collections"], state1["collections"])
    c("frame, selection, active object put back", (state1["frame"], state1["selected"], state1["active"]) ==
      (state0["frame"], state0["selected"], state0["active"]), state1)
    b = state1["bounds"].get(coll) or {"size": [0], "min": [9, 9, 9]}
    c("sized 0.8 and on the ground, measured in the live Blender", abs(max(b["size"]) - 0.8) < 0.008 and abs(b["min"][2]) < 1e-5, b)
    kit = ensure_kit(ctx)
    code, ans, err = ctx.tool("import-file", kit / "empty.glb", "--size", "1", "--expect-pid", str(pid), env=env)
    state2 = bridge_exec(port, look)
    c("a live failure is rolled back", code == 1 and ans.get("import", {}).get("scene_unchanged") is True and state2 == state1,
      ans.get("error"))


EDIT_ON = r'''
import bpy, bmesh
layer = bpy.context.view_layer
floor = bpy.data.objects["Floor"]
layer.objects.active = floor
bpy.ops.object.mode_set(mode="EDIT")
bm = bmesh.from_edit_mesh(floor.data)
bm.verts.ensure_lookup_table()
bm.verts[0].co.z += 0.1
bmesh.update_edit_mesh(floor.data)
result = {"mode": bpy.context.mode, "editing": sorted(o.name for o in layer.objects if o.mode == "EDIT"),
          "z": bm.verts[0].co.z, "mesh_z": floor.data.vertices[0].co.z}
'''

EDIT_LOOK = r'''
import bpy, bmesh
layer = bpy.context.view_layer
floor = bpy.data.objects["Floor"]
z = None
if floor.mode == "EDIT":
    bm = bmesh.from_edit_mesh(floor.data); bm.verts.ensure_lookup_table(); z = bm.verts[0].co.z
result = {"mode": bpy.context.mode, "editing": sorted(o.name for o in layer.objects if o.mode == "EDIT"), "z": z,
          "active": layer.objects.active.name if layer.objects.active else None}
'''

EDIT_OFF = r'''
import bpy
if bpy.context.mode != "OBJECT":
    bpy.ops.object.mode_set(mode="OBJECT")
result = {"mode": bpy.context.mode}
'''

LIVE_LOOK = r'''
import bpy, numpy as np
def bounds(coll):
    obs = list(coll.all_objects)
    dg = bpy.context.evaluated_depsgraph_get()
    lo = hi = None
    for ob in obs:
        if ob.type != "MESH": continue
        ev = ob.evaluated_get(dg); me = ev.to_mesh()
        for v in me.vertices:
            p = np.array((ev.matrix_world @ v.co)[:]); lo = p if lo is None else np.minimum(lo, p); hi = p if hi is None else np.maximum(hi, p)
        ev.to_mesh_clear()
    return None if lo is None else {"min": lo.tolist(), "size": (hi - lo).tolist()}
layer = bpy.context.view_layer
result = {"frame": bpy.context.scene.frame_current, "active": layer.objects.active.name if layer.objects.active else None,
          "selected": sorted(o.name for o in layer.objects if o.select_get()),
          "objects": sorted(o.name for o in bpy.data.objects), "collections": sorted(c.name for c in bpy.data.collections),
          "bounds": {c.name: bounds(c) for c in bpy.data.collections}}
'''


def bridge_exec(port, code):
    if port in FORBIDDEN_PORTS:
        raise SystemExit("port %d is not ours to talk to" % port)
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    sock.sendall(json.dumps({"type": "execute", "code": code, "strict_json": True}).encode("utf-8") + b"\0")
    sock.settimeout(120)
    data = b""
    while b"\0" not in data:
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
    sock.close()
    answer = json.loads(data.split(b"\0")[0].decode("utf-8"))
    if answer.get("status") != "ok":
        raise RuntimeError(answer.get("message"))
    return answer["result"]


BRIDGE_BOOT = r'''
import importlib.util, os, sys
argv = sys.argv[sys.argv.index("--") + 1:]
pkg, port = argv[0], int(argv[1])
name = "btools_bridge_mcp"
spec = importlib.util.spec_from_file_location(name, os.path.join(pkg, "__init__.py"), submodule_search_locations=[pkg])
module = importlib.util.module_from_spec(spec)
sys.modules[name] = module
spec.loader.exec_module(module)
server = importlib.import_module(name + ".mcp_to_blender_server")
blocking = importlib.import_module(name + ".execute_blocking")
server.start("127.0.0.1", port)
print("BRIDGE_UP %d %d" % (port, os.getpid()), flush=True)
try:
    blocking.run()
finally:
    server.stop()
'''


def start_bridge_pkg(zip_path, pkg):
    """The official bridge 1.0.3 add-on, checked by its sha256, unpacked with every member checked."""
    want = "a7a9da816192502e5a0a202a396444e266b47d8fc4f74ad4698048bd43040707"
    got = hashlib.sha256(Path(zip_path).read_bytes()).hexdigest()
    if got != want:
        raise SystemExit("the bridge zip's sha256 is %s, not the 1.0.3 release's" % got)
    import zipfile
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            parts = name.replace("\\", "/").split("/")
            if name.startswith(("/", "\\")) or ":" in name or ".." in parts:
                raise SystemExit("unsafe member in the bridge zip: %r" % name)
        z.extractall(pkg)


def start_bridge(ctx, zip_path, port, blend):
    if port in FORBIDDEN_PORTS:
        raise SystemExit("port %d is not ours" % port)
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", port)) == 0:
            raise SystemExit("something already listens on %d; not ours to touch" % port)
    pkg = ctx.work / "bridge_pkg" / "mcp"
    if not pkg.exists():
        start_bridge_pkg(zip_path, pkg)
    boot = ctx.work / "bridge_boot.py"
    boot.write_text(BRIDGE_BOOT, encoding="utf-8")
    live_blend = ctx.work / "live_scene.blend"
    shutil.copyfile(blend, live_blend)
    log = ctx.work / "bridge.log"
    wait_memory()
    proc = subprocess.Popen([ctx.blender, "--background", "--factory-startup", "--disable-autoexec", str(live_blend),
                             "--python", str(boot), "--", str(pkg), str(port)],
                            stdout=open(log, "wb"), stderr=subprocess.STDOUT)
    for _ in range(120):
        text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
        m = re.search(r"BRIDGE_UP (\d+) (\d+)", text)
        if m:
            return {"proc": proc, "port": port, "pid": int(m.group(2)), "log": log}
        if proc.poll() is not None:
            raise SystemExit("the bridge Blender exited: see %s" % log)
        time.sleep(0.5)
    proc.kill()
    raise SystemExit("the bridge did not come up in 60 s")


@row("R19 --help without a network, finding Blender, paths with spaces")
def r19(ctx, c):
    env = ctx.env(BLENDER_AI_ASSETS_POLYHAVEN_API="http://127.0.0.1:9/ph", BLENDER_MCP_PORT="1")
    n = ctx.server.count()
    for argv in (("--help",), ("import", "--help"), ("search", "--help")):
        proc = subprocess.run([sys.executable, "-X", "utf8", str(ctx.scripts / "assets.py"), *argv], capture_output=True,
                              text=True, encoding="utf-8", env=env, timeout=60)
        c("%s works offline" % " ".join(argv), proc.returncode == 0 and "usage" in proc.stdout.lower(), proc.stderr[-200:])
    c("control: --help made no request", ctx.server.count() == n)
    sys.path.insert(0, str(ctx.scripts))
    try:
        import importlib
        sys.modules.pop("assets", None)
        assets = importlib.import_module("assets")
    finally:
        sys.path.pop(0)
    saved = {k: os.environ.get(k) for k in ("BLENDER_EXE", "PATH")}
    try:
        os.environ.pop("BLENDER_EXE", None)
        os.environ["PATH"] = str(ctx.work)                 # no blender on it
        try:
            exe, how = assets.find_blender()
            c("without BLENDER_EXE or PATH: found in the usual install folders", Path(exe).is_file()
              and how == "the usual install folders" and os.path.normcase(exe) == os.path.normcase(ctx.blender), (exe, how))
        except assets.Fail as exc:
            c("without BLENDER_EXE or PATH: found in the usual install folders", False, exc)
        os.environ["BLENDER_EXE"] = str(ctx.work / "no_blender_here.exe")
        try:
            assets.find_blender()
            c("a BLENDER_EXE that is not a file: refused", False)
        except assets.Fail as exc:
            c("a BLENDER_EXE that is not a file: refused", "not a file" in str(exc), exc)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    spaced = ctx.work / "a folder with spaces"
    spaced.mkdir(exist_ok=True)
    shutil.copyfile(make_scene(ctx), spaced / "my scene.blend")
    out = spaced / "my scene out.blend"
    code, ans, err = ctx.headless("import", "polyhaven", "fx_box", "--blend", spaced / "my scene.blend", "--save-as", out)
    c("a .blend in a folder with spaces in and out", code == 0 and out.is_file(), ans.get("error"))
    c("nothing written beside the input .blend but the asked --save-as", sorted(p.name for p in spaced.iterdir())
      == ["my scene out.blend", "my scene.blend"], sorted(p.name for p in spaced.iterdir()))


# ------------------------------------------------------------------------------------------- online rows

@row("O01 online: real Poly Haven search with thumbnails")
def o01(ctx, c):
    code, ans, err = ctx.tool("search", "--source", "polyhaven", "--type", "model", "rubber", "duck", "--limit", "3",
                              "--thumbs", ctx.work / "thumbs_online")
    ids = [r["id"] for r in ans.get("results", [])]
    c("rubber_duck_toy ranks first", code == 0 and ids[:1] == ["rubber_duck_toy"], ids)
    files = ans.get("thumbs", [])
    c("thumbnails are PNG files", files and all(Path(f).read_bytes()[:4] == b"\x89PNG" for f in files), files)


@row("O02 online: real 1k HDRI, texture and glTF model fetched, checked, cached")
def o02(ctx, c):
    for slug in ("kloppenheim_06_puresky", "rock_wall_08", "rubber_duck_toy"):
        code, ans, err = ctx.tool("fetch", "polyhaven", slug)
        c("%s fetched (md5 and size checked by the tool)" % slug, code == 0 and ans.get("cache") in ("miss", "hit"), ans.get("error") or ans.get("bytes"))
        code, ans, err = ctx.tool("fetch", "polyhaven", slug, "--offline")
        c("%s: second fetch is a cache hit that needs no network" % slug, code == 0 and ans.get("cache") == "hit", ans.get("error"))


@row("O03 online: the three imported into one scene, checked from the saved file")
def o03(ctx, c):
    scene = make_scene(ctx)
    a, b, d = ctx.work / "o3a.blend", ctx.work / "o3b.blend", ctx.work / "o3c.blend"
    code, ans, err = ctx.headless("import", "polyhaven", "kloppenheim_06_puresky", "--blend", scene, "--save-as", a)
    c("HDRI import ok", code == 0, ans.get("error"))
    code, ans, err = ctx.headless("import", "polyhaven", "rock_wall_08", "--blend", a, "--save-as", b, "--apply-to", "Floor")
    imp = ans.get("import", {})
    c("texture import ok", code == 0, ans.get("error"))
    code, ans, err = ctx.headless("import", "polyhaven", "rubber_duck_toy", "--blend", b, "--save-as", d, "--size", "0.3", "--at", "0,0,0")
    imp3 = ans.get("import", {})
    c("model import ok", code == 0, ans.get("error"))
    c("the duck as published: authored size matches Poly Haven's dimensions", imp3.get("authored_matches_published") is True,
      (imp3.get("authored"), imp3.get("published_size_m")))
    if not d.is_file():
        return
    after = probe(ctx, d)
    c("world is the HDRI", after["world"] == "kloppenheim_06_puresky", after["world"])
    tree = after["materials"][after["objects"]["Floor"]["slots"][0][1]]["tree"]
    tex = {n["label"]: name for name, n in tree["nodes"].items() if n["type"] == "ShaderNodeTexImage"}
    c("rock wall: Diffuse, Rough, nor_gl, Displacement wired", feeds(tree, tex.get("Diffuse"), "ShaderNodeBsdfPrincipled", "Base Color")
      and feeds(tree, tex.get("Rough"), "ShaderNodeBsdfPrincipled", "Roughness")
      and feeds(tree, tex.get("nor_gl"), "ShaderNodeBsdfPrincipled", "Normal")
      and feeds(tree, tex.get("Displacement"), "ShaderNodeOutputMaterial", "Displacement"), sorted(tex))
    disp = next((x for x in tree["nodes"].values() if x["type"] == "ShaderNodeDisplacement"), {})
    c("displacement scale read from Poly Haven's MaterialX (0.01)", abs(disp.get("scale", 0) - 0.01) < 1e-6, disp)
    mapping = next(x for x in tree["nodes"].values() if x["type"] == "ShaderNodeMapping")
    c("rock wall at real size on the 2 m floor (2/1.8)", all(abs(v - 2 / 1.8) < 1e-3 for v in mapping["scale"][:2]), mapping["scale"])
    coll = imp3.get("collection")
    bb = after["collections"].get(coll, {}).get("bounds") or {"size": [0], "min": [9, 9, 9]}
    c("duck: largest side 0.3 within 1 %, on the ground, own collection", abs(max(bb["size"]) - 0.3) <= 0.003 and abs(bb["min"][2]) < 1e-5, bb)
    credits = after["texts"].get("ASSET_CREDITS", "").splitlines()
    c("three credit lines", len(credits) == 3 and all("| CC0 |" in l for l in credits), credits)


# ------------------------------------------------------------------------------------------- GUI rows (a window)
# A background Blender has no window, no 3D view and no undo stack. These rows need a WINDOWED Blender serving the
# bridge on a spare port: `--write-gui-boot` prepares its scene and boot script and prints the command line.

BRIDGE_BOOT_GUI = r'''
import importlib.util, os, sys, bpy
argv = sys.argv[sys.argv.index("--") + 1:]
pkg, port = argv[0], int(argv[1])
name = "btools_bridge_mcp"
spec = importlib.util.spec_from_file_location(name, os.path.join(pkg, "__init__.py"), submodule_search_locations=[pkg])
module = importlib.util.module_from_spec(spec)
sys.modules[name] = module
spec.loader.exec_module(module)
server = importlib.import_module(name + ".mcp_to_blender_server")
interactive = importlib.import_module(name + ".execute_interactive")
server.start("127.0.0.1", port)
bpy.app.timers.register(interactive.run, first_interval=0.1, persistent=True)
print("BRIDGE_UP %d %d" % (port, os.getpid()), flush=True)
'''

VIEW_LOOK = r'''
import bpy
views = []
for w in bpy.context.window_manager.windows:
    for a in w.screen.areas:
        if a.type == "VIEW_3D":
            r = a.spaces.active.region_3d
            views.append([[round(v, 5) for v in r.view_location], [round(v, 5) for v in r.view_rotation],
                          round(r.view_distance, 5), r.view_perspective])
result = {"views": views}
'''

VIEW_SET = r'''
import bpy
areas = [a for w in bpy.context.window_manager.windows for a in w.screen.areas if a.type == "VIEW_3D"]
r = max(areas, key=lambda a: a.width * a.height).spaces.active.region_3d
r.view_location = (1.0, 2.0, 0.5)
r.view_distance = 9.0
''' + VIEW_LOOK

SHOT = r'''
import bpy
win = bpy.context.window_manager.windows[0]
with bpy.context.temp_override(window=win, screen=win.screen):
    bpy.ops.screen.screenshot(filepath=%r)
result = {"ok": True}
'''

UNDO = r'''
import bpy
win = bpy.context.window_manager.windows[0]
with bpy.context.temp_override(window=win):
    bpy.ops.ed.%s()
scene = bpy.context.scene
result = {"world": scene.world.name if scene.world else None, "worlds": sorted(w.name for w in bpy.data.worlds)}
'''


@row("G01 GUI: Edit Mode, the 3D view and the undo step around a live import (windowed Blender)")
def g01(ctx, c):
    if not ctx.gui:
        c("needs --gui-port and --gui-pid (a windowed Blender the lead started)", False, "skipped")
        return
    port, pid = ctx.gui["port"], ctx.gui["pid"]
    env = ctx.env(BLENDER_MCP_HOST="127.0.0.1", BLENDER_MCP_PORT=port)
    who = bridge_exec(port, "import bpy, os\nresult = {'pid': os.getpid(), 'windows': len(bpy.context.window_manager.windows), "
                            "'background': bpy.app.background}")
    c("control: the Blender on the port is the windowed one given", who["pid"] == pid and who["windows"] >= 1
      and not who["background"], who)
    v0 = bridge_exec(port, VIEW_SET)["views"]
    e0 = bridge_exec(port, EDIT_ON)
    c("control: Edit Mode with an edit not yet written to the mesh", e0["mode"] == "EDIT_MESH", e0)
    code, ans, err = ctx.tool("import", "polyhaven", "fx_box", "--size", "0.5", "--at", "0,2,0", "--expect-pid", str(pid), env=env)
    imp = ans.get("import", {})
    c("import ok; scene check ok with the 3D views in the fingerprint", code == 0 and imp.get("scene_check", {}).get("ok"),
      ans.get("error") or imp.get("scene_check"))
    c("one undo step pushed for the import", imp.get("undo_step") is True, imp.get("undo_step"))
    e1 = bridge_exec(port, EDIT_LOOK)
    c("Edit Mode back on the same objects, the unsaved edit kept", e1["mode"] == "EDIT_MESH"
      and e1["editing"] == e0["editing"] and abs((e1["z"] or 0) - e0["z"]) < 1e-6, (e0, e1))
    v1 = bridge_exec(port, VIEW_LOOK)["views"]
    c("the 3D views did not move", v1 == v0, (v0, v1))
    shot = ctx.work / "gui_after_import.png"
    bridge_exec(port, SHOT % str(shot))
    c("screenshot for the eye: %s" % shot, shot.is_file(), shot)
    bridge_exec(port, EDIT_OFF)


@row("G02 GUI: one Ctrl+Z takes an import away, Ctrl+Shift+Z brings it back (windowed Blender)")
def g02(ctx, c):
    if not ctx.gui:
        c("needs --gui-port and --gui-pid", False, "skipped")
        return
    port, pid = ctx.gui["port"], ctx.gui["pid"]
    env = ctx.env(BLENDER_MCP_HOST="127.0.0.1", BLENDER_MCP_PORT=port)
    bridge_exec(port, EDIT_OFF)
    before = bridge_exec(port, "import bpy\nresult = {'world': bpy.context.scene.world.name}")
    code, ans, err = ctx.tool("import", "polyhaven", "fx_sky", "--expect-pid", str(pid), env=env)
    c("HDRI import ok with an undo step", code == 0 and ans.get("import", {}).get("undo_step") is True, ans.get("error"))
    world = ans.get("import", {}).get("world")
    undone = bridge_exec(port, UNDO % "undo")
    c("after one undo: the old world is back and the new one gone", undone["world"] == before["world"]
      and world not in undone["worlds"], (before, undone))
    redone = bridge_exec(port, UNDO % "redo")
    c("after redo: the HDRI world again", redone["world"] == world, redone)
    shot = ctx.work / "gui_after_redo.png"
    bridge_exec(port, SHOT % str(shot))
    c("screenshot for the eye: %s" % shot, shot.is_file(), shot)


def write_gui_boot(ctx, zip_path, port):
    if port in FORBIDDEN_PORTS:
        raise SystemExit("port %d is not ours" % port)
    scene = make_scene(ctx)
    pkg = ctx.work / "bridge_pkg" / "mcp"
    if not pkg.exists():
        start_bridge_pkg(zip_path, pkg)
    boot = ctx.work / "gui_boot.py"
    boot.write_text(BRIDGE_BOOT_GUI, encoding="utf-8")
    gui_scene = ctx.work / "gui_scene.blend"
    shutil.copyfile(scene, gui_scene)
    config = ctx.work / "gui_profile"
    config.mkdir(exist_ok=True)
    print("Start the windowed Blender (its own empty profile; the marker 'btools' is in its paths):")
    print('  set BLENDER_USER_CONFIG=%s & set BLENDER_USER_SCRIPTS=%s' % (config / "config", config / "scripts"))
    print('  "%s" --factory-startup "%s" --python "%s" -- "%s" %d' % (ctx.blender, gui_scene, boot, pkg, port))
    print("It prints BRIDGE_UP <port> <pid>. Then:")
    print('  python -X utf8 "%s" --work "%s" --only G01,G02 --gui-port %d --gui-pid <pid>' % (Path(__file__).resolve(), ctx.work.parent, port))


@row("O04 online + LIVE: one real import through the bridge")
def o04(ctx, c):
    if ctx.bridge is None:
        c("needs --bridge-zip and --bridge-port", False, "skipped")
        return
    env = ctx.env(BLENDER_MCP_HOST="127.0.0.1", BLENDER_MCP_PORT=ctx.bridge["port"])
    code, ans, err = ctx.tool("import", "polyhaven", "rubber_duck_toy", "--size", "0.25", "--at", "-1,-1,0",
                              "--expect-pid", str(ctx.bridge["pid"]), env=env)
    imp = ans.get("import", {})
    c("live import of the real duck ok, scene check ok", code == 0 and imp.get("scene_check", {}).get("ok"), ans.get("error"))
    state = bridge_exec(ctx.bridge["port"], LIVE_LOOK)
    b = state["bounds"].get(imp.get("collection")) or {"size": [0], "min": [9, 9, 9]}
    c("in the live Blender: 0.25 m, on the ground", abs(max(b["size"]) - 0.25) < 0.0025 and abs(b["min"][2]) < 1e-5, b)


# ------------------------------------------------------------------------------------------- mutants

MUTANTS = {
    "R01": [("assets_blender.py", '"matrix_world": _r(ob.matrix_world), "location": _r(ob.location),',
             '"matrix_world": 0, "location": 0,')],
    "R02": [("assets.py", "ordered = [s for s in ranked if s in assets]", "ordered = sorted(assets, key=lambda s: -(assets[s].get(\"download_count\") or 0))")],
    "R03": [("assets.py", '    if manifest is not None:\n        return manifest, "hit"\n    if damaged:',
             '    if False:\n        return manifest, "hit"\n    if damaged:')],
    "R04": [("assets.py", "    if length is not None and length.isdigit() and got != int(length):", "    if False:"),
            ("assets.py", "    if size is not None and got != int(size):", "    if False:"),
            ("assets.py", "    if md5 and h_md5.hexdigest() != md5.lower():", "    if False:")],
    "R05": [("assets_blender.py", '    nt.links.new(background.outputs["Background"], output.inputs["Surface"])\n', "")],
    "R06": [("assets_blender.py", '        image, space = _load_image(m["path"], m["role"] not in COLOR_ROLES, opts.get("pack"), made)',
             '        image, space = _load_image(m["path"], False, opts.get("pack"), made)')],
    "R07": [("assets_blender.py", "            if role == \"normal_dx\":            # DirectX: green points the other way",
             "            if False:")],
    "R08": [("assets_blender.py", "    pivot = Vector(((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, lo.z if ground else 0.0))",
             "    pivot = Vector(((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, 0.0))")],
    "R09": [("assets.py", '    if ".." in n.split("/"):\n        return "climbs out with \'..\'"', '    if False:\n        return ""'),
            ("assets.py", '    if n.startswith("/") or re.match(r"^[A-Za-z]:", n):\n        return "is an absolute path"', '    if False:\n        return ""'),
            ("assets.py", '    if real != real_target and not real.startswith(real_target + os.sep):\n        return "would land outside its folder"',
             '    if False:\n        return ""')],
    "R10": [("assets.py", "        if _sha256(path) != item[\"sha256\"]:", "        if False:")],
    "R11": [("assets.py", 'SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")', 'SLUG_RE = re.compile(r"^.{1,80}$")')],
    "R12": [("assets.py", "    return getattr(exc, \"errno\", None) in (errno.ENOSPC, getattr(errno, \"EDQUOT\", -1)) or \\",
             "    return False and \\")],
    "R13": [("assets.py", '    key = os.environ.get("POLYPIZZA_API_KEY", "").strip()\n    if not key:',
             '    key = os.environ.get("POLYPIZZA_API_KEY", "").strip()\n    if False:'),
            ("assets.py", '    token = os.environ.get("SKETCHFAB_API_TOKEN", "").strip()\n    if not token:',
             '    token = os.environ.get("SKETCHFAB_API_TOKEN", "").strip()\n    if False:')],
    "R14": [("assets.py", '[{"rel": meta["main"], "url": url, "check": "glb"}]',
             '[{"rel": meta["main"], "url": url, "check": "glb", "headers": {"x-auth-token": key}}]')],
    "R15": [("assets.py", '"author": rec["author"], "license": licence.get("label") or "unknown",',
             '"author": rec["author"], "license": "unknown",')],
    "R16": [("assets_blender.py", '    ".stl": ("wm", "stl_import", {}),', '    ".stl": ("wm", "ply_import", {}),')],
    "R17": [("assets_blender.py", "        if new:\n            bpy.data.batch_remove(new)", "        if False:\n            bpy.data.batch_remove(new)")],
    "R19": [("assets.py", '            candidates += glob.glob(os.path.join(root, "Steam", "steamapps", "common", "Blender", "blender.exe"))',
             '            pass'),
            ("assets.py", '        candidates += glob.glob(os.path.join(root, "Blender Foundation", "Blender*", "blender.exe"))',
             '        pass')],
    "R18": [("assets_blender.py", "        for ob in layer.objects:\n            want = ob.as_pointer() in self.selected",
             "        for ob in []:\n            want = ob.as_pointer() in self.selected"),
            ("assets_blender.py", "        for ob in layer.objects:\n            try:\n                ob.select_set(ob.as_pointer() in self.selected, view_layer=layer)",
             "        for ob in []:\n            try:\n                ob.select_set(ob.as_pointer() in self.selected, view_layer=layer)")],
}


def apply_mutant(src_dir, dst_dir, edits):
    shutil.rmtree(dst_dir, ignore_errors=True)
    dst_dir.mkdir(parents=True)
    for name in ("assets.py", "assets_blender.py", "bmcp.py"):
        shutil.copyfile(src_dir / name, dst_dir / name)
    for name, old, new in edits:
        text = (dst_dir / name).read_text(encoding="utf-8")
        if text.count(old) != 1:
            raise SystemExit("mutant edit not found exactly once in %s: %r" % (name, old[:70]))
        (dst_dir / name).write_text(text.replace(old, new), encoding="utf-8")


# ------------------------------------------------------------------------------------------- main

def run_rows(ctx, wanted):
    results = []
    for name, fn in ROWS:
        code = name.split()[0]
        if not wanted(code):
            continue
        print("%s" % name, flush=True)
        checks = Checks(name)
        started = time.time()
        try:
            fn(ctx, checks)
        except Exception as exc:  # noqa: BLE001 - a crash is a failed row
            checks("row ran to the end", False, "%s: %s" % (type(exc).__name__, exc))
        results.append({"row": name, "ok": checks.ok, "checks": checks.items, "seconds": round(time.time() - started, 1)})
        print("  -> %s (%d checks, %.0f s)" % ("PASS" if checks.ok else "FAIL", len(checks.items), time.time() - started), flush=True)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--work", required=True, help="scratch folder (keep it short on Windows; keep 'btools' in it)")
    parser.add_argument("--blender", default=os.environ.get("BLENDER_EXE") or r"C:\Program Files (x86)\Steam\steamapps\common\Blender\blender.exe")
    parser.add_argument("--online", action="store_true")
    parser.add_argument("--bridge-zip")
    parser.add_argument("--bridge-port", type=int)
    parser.add_argument("--mutants", action="store_true")
    parser.add_argument("--gui-port", type=int, help="G rows: a windowed Blender serving the bridge on this spare port")
    parser.add_argument("--gui-pid", type=int, help="G rows: that Blender's process id (it is checked)")
    parser.add_argument("--write-gui-boot", action="store_true", help="prepare the windowed run (needs --bridge-zip, --gui-port)")
    parser.add_argument("--only", default="")
    args = parser.parse_args()
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    selected = {s.strip() for s in args.only.split(",") if s.strip()}
    server = FixtureServer(FIXTURES)
    summary = {"rows": [], "mutants": []}
    ctx = Ctx(args, SKILL / "scripts", work / "main")
    ctx.server = server
    bridge = None
    if args.write_gui_boot:
        server.stop()
        write_gui_boot(ctx, args.bridge_zip, args.gui_port)
        return 0
    if args.gui_port and args.gui_pid:
        if args.gui_port in FORBIDDEN_PORTS:
            raise SystemExit("port %d is not ours" % args.gui_port)
        ctx.gui = {"port": args.gui_port, "pid": args.gui_pid}
    try:
        if args.bridge_zip and args.bridge_port:
            bridge = start_bridge(ctx, args.bridge_zip, args.bridge_port, make_scene(ctx))
            ctx.bridge = bridge
            print("bridge up on %d, pid %d" % (bridge["port"], bridge["pid"]), flush=True)
        if args.online:
            online = Ctx(args, SKILL / "scripts", work / "main")
            online.cache = work / "cache_online"
            online.bridge = bridge
            online.server = None
            summary["rows"] += run_rows(online, lambda code: code.startswith("O") and (not selected or code in selected))
        summary["rows"] += run_rows(ctx, lambda code: code.startswith("R") and (not selected or code in selected))
        if ctx.gui:
            summary["rows"] += run_rows(ctx, lambda code: code.startswith("G") and (not selected or code in selected))
        if args.mutants:
            for code, edits in MUTANTS.items():
                if selected and code not in selected:
                    continue
                mdir = work / "mutants" / code
                apply_mutant(SKILL / "scripts", mdir / "scripts", edits)
                mctx = Ctx(args, mdir / "scripts", mdir)
                mctx.server = server
                mctx.bridge = bridge
                if bridge is not None and code == "R18":
                    print("(R18's mutant runs against the same bridge Blender)", flush=True)
                print("MUTANT %s: %s" % (code, "; ".join(old[:50].strip() for _n, old, _new in edits)), flush=True)
                res = run_rows(mctx, lambda c, code=code: c == code)
                failed = bool(res) and not res[0]["ok"]
                summary["mutants"].append({"row": code, "planted": [old[:80] for _n, old, _new in edits],
                                           "row_failed_as_it_must": failed,
                                           "failing_checks": [c["check"] for c in (res[0]["checks"] if res else []) if not c["ok"]]})
                print("  => mutant %s: %s" % (code, "CAUGHT (row failed)" if failed else "NOT CAUGHT (row passed)"), flush=True)
    finally:
        if bridge is not None:
            bridge["proc"].terminate()
            try:
                bridge["proc"].wait(20)
            except subprocess.TimeoutExpired:
                bridge["proc"].kill()
        server.stop()
    ok = all(r["ok"] for r in summary["rows"]) and all(m["row_failed_as_it_must"] for m in summary["mutants"])
    summary["ok"] = ok
    summary["counts"] = {"rows": len(summary["rows"]), "rows_passed": sum(r["ok"] for r in summary["rows"]),
                         "checks": sum(len(r["checks"]) for r in summary["rows"]),
                         "checks_passed": sum(c["ok"] for r in summary["rows"] for c in r["checks"]),
                         "mutants": len(summary["mutants"]),
                         "mutants_caught": sum(m["row_failed_as_it_must"] for m in summary["mutants"])}
    (work / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(json.dumps(summary["counts"]))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

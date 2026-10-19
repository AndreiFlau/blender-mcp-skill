# SPDX-License-Identifier: GPL-3.0-or-later
"""look.py against a WINDOWED Blender, through the real bridge: what a background Blender cannot show.

A background Blender has no window, so the tests in test_look.py cannot reach the viewport mode, the ui
mode, or the windowed bridge's deferred, one-tile-a-tick path. This script does, in a Blender with a
window, and drives itself. Run it in a SANDBOXED profile so nothing of yours is read or written:

  Git Bash:
    SANDBOX=<an empty folder>; mkdir -p "$SANDBOX/tmp"
    BLENDER_USER_RESOURCES="$SANDBOX" TEMP="$SANDBOX/tmp" TMP="$SANDBOX/tmp" \\
      blender --factory-startup -p 60 60 1500 950 --python look_gui_check.py -- WORK_DIR PORT BRIDGE_PACKAGE_DIR

  WORK_DIR            a scratch folder (the control scene is saved there as gui_control.blend)
  PORT                a free port for the bridge (not 9876, not one another Blender uses)
  BRIDGE_PACKAGE_DIR  the unzipped Blender Lab MCP add-on folder, 1.0.3 or later (imported from there,
                      not installed)
  --keep-open         (after the folder arguments) stay open at the end, to look around

What it does: builds test_look.py's control scene and saves it, puts the viewport in a known state
(front view, solid with material colours, sidebar closed), starts the bridge on 127.0.0.1:PORT, then runs
look.py cases one after another as separate processes, the way an AI would, and checks each one: the
JSON, the pictures (colours, sizes, framing), the tool's own fingerprint, and an independent dump of the
user's state before and after (test_look._state). It prints "GUI| PASS/FAIL ..." lines, writes
WORK_DIR/gui_rows.json and quits (exit code 0 when every row passed).

What the lead should SEE while it runs: the window flickers for the ui cases (the sidebar opens on the
"Item" tab, the Properties editor shows Green's material, the Preferences list shows the Cycles add-on),
and everything is back as it was after each case. Look at the pictures listed at the end:
  viewport.png      the window's 3D view as it is (front, red cube in the middle), no UI chrome
  sidebar.png       the 3D view's sidebar, Item tab, at its real width
  properties.png    the Material tab of Green
  preferences.png   the add-ons list filtered to Cycles, expanded
  window.png        the whole window
"""
import sys
sys.dont_write_bytecode = True     # no __pycache__ beside the skill's files
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import bpy

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import test_look as tl  # noqa: E402  (its measuring functions and the control scene)

LOOK = HERE.parent / "scripts" / "look.py"
ARGV = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
if len(ARGV) < 3:
    print("GUI| usage: blender --factory-startup --python look_gui_check.py -- WORK_DIR PORT BRIDGE_PACKAGE_DIR")
    raise SystemExit(2)
WORK = Path(ARGV[0]).resolve()
PORT = int(ARGV[1])
PACKAGE = Path(ARGV[2]).resolve()
KEEP_OPEN = "--keep-open" in ARGV
WORK.mkdir(parents=True, exist_ok=True)
ROWS = []
STATE = {"case": 0, "proc": None, "started": 0.0, "before": None, "planted": False}


def row(name, ok, **facts):
    ROWS.append({"name": name, "ok": bool(ok), **facts})
    print("GUI| %s %s %s" % ("PASS" if ok else "FAIL", name, json.dumps(facts, default=str)[:900]), flush=True)


def view3d():
    areas = [(w, a) for w in bpy.context.window_manager.windows for a in w.screen.areas if a.type == "VIEW_3D"]
    return max(areas, key=lambda pair: pair[1].width * pair[1].height)


def prepare():
    path = str(WORK / "gui_control.blend")
    tl.build_control(path)                      # saves: this Blender now has gui_control.blend open
    _window, area = view3d()
    space = area.spaces.active
    space.shading.type = "SOLID"
    space.shading.color_type = "MATERIAL"
    space.show_region_ui = False
    r3d = space.region_3d
    r3d.view_perspective = "PERSP"
    r3d.view_rotation = (0.7071068, 0.7071068, 0.0, 0.0)       # front
    r3d.view_location = (0.0, 0.0, 1.0)
    r3d.view_distance = 14.0
    for other in bpy.context.window_manager.windows[0].screen.areas:
        other.tag_redraw()
    sys.path.insert(0, str(PACKAGE.parent))
    import importlib
    server = importlib.import_module(PACKAGE.name + ".mcp_to_blender_server")
    interactive = importlib.import_module(PACKAGE.name + ".execute_interactive")
    server.start("127.0.0.1", PORT)
    bpy.app.timers.register(interactive.run, first_interval=0.1)
    STATE["server"] = server
    print("GUI| serving the bridge on", PORT, "pid", os.getpid(), flush=True)


def out(name):
    return str(WORK / "gui_runs" / name)


def fill_ok(result, colour):
    fills = [round(tl.mask_stats(tl.read_png(t["path"]), [colour]).get("fill", 0), 3) for t in result.get("tiles", [])]
    return bool(fills) and all(0.35 <= f <= 0.95 for f in fills), fills


def check_viewport(rc, res):
    tile = (res.get("tiles") or [{}])[0]
    _w, area = view3d()
    region = next(r for r in area.regions if r.type == "WINDOW")
    aspect_tile = tile.get("size", [1, 1])[0] / float(tile.get("size", [1, 1])[1])
    red = tl.mask_stats(tl.read_png(tile["path"]), ["red"]).get("count", 0) if tile.get("path") else 0
    STATE["viewport_red"] = red
    row("G1 viewport: what the 3D view shows, at its aspect, red cube visible, put back",
        rc == 0 and abs(aspect_tile - region.width / float(region.height)) < 0.01 and red > 1000 and
        res.get("fingerprint", {}).get("equal"), aspect=[round(aspect_tile, 4), round(region.width / region.height, 4)],
        red_pixels=red, path=tile.get("path"))


def check_viewport_wire(rc, res):
    _w, area = view3d()
    sh = area.spaces.active.shading
    tile = (res.get("tiles") or [{}])[0]
    red = tl.mask_stats(tl.read_png(tile["path"]), ["red"]).get("count", 0) if tile.get("path") else -1
    # The overlays stay as the user has them in viewport mode, so the red X axis of the grid is in both
    # pictures: the cube's red is what must go (G1's count is the control).
    control = STATE.get("viewport_red", 0)
    row("G2 viewport --shading wireframe: single colour (red down to < 30% of G1's), viewport SOLID/MATERIAL again",
        rc == 0 and control > 1000 and red < 0.3 * control and sh.type == "SOLID" and sh.color_type == "MATERIAL",
        red_pixels=red, control_red_pixels=control, shading_after=[sh.type, sh.color_type])


def check_angles(rc, res):
    ok, fills = fill_ok(res, "red")
    row("G3 angles in a windowed Blender: deferred, 35-95% fill, put back",
        rc == 0 and ok and res.get("deferred") and res.get("fingerprint", {}).get("equal"), fills=fills,
        deferred=res.get("deferred"), seconds=res.get("client", {}).get("seconds"))


def check_center(rc, res):
    cxs = [tl.mask_stats(tl.read_png(t["path"]), ["magenta"]).get("cx", -999) for t in res.get("tiles", [])]
    spread = round(max(cxs) - min(cxs), 2) if cxs else None
    row("G4 frames --center-bones Root (6 frames, deferred): the tracker stays put (<= 2 px)",
        rc == 0 and spread is not None and spread <= 2 and res.get("deferred"), spread=spread, tiles=len(cxs))


def check_frames_viewport(rc, res):
    row("G5 frames --view viewport: three tiles from the user's view", rc == 0 and len(res.get("tiles", [])) == 3 and
        res.get("fingerprint", {}).get("equal"), sizes=[t["size"] for t in res.get("tiles", [])])


def check_camera(rc, res):
    tile = (res.get("tiles") or [{}])[0]
    row("G6 camera --shading rendered (EEVEE in the window): ok, settled, put back",
        rc == 0 and tile.get("settled", True) and res.get("fingerprint", {}).get("equal"), draws=tile.get("draws"),
        warnings=res.get("warnings"))


def check_meanwhile(rc, res):
    meanwhile = (res.get("fingerprint") or {}).get("changed_meanwhile", [])
    row("G7 the user selects an object while a deferred job runs: reported, not blamed",
        rc == 0 and any("Green" in m for m in meanwhile), meanwhile=meanwhile[:2], planted=STATE["planted"])
    bpy.data.objects["Green"].select_set(False)


def check_ui(name, extra=None):
    def check(rc, res):
        tile = (res.get("tiles") or [{}])[0]
        facts = {"size": tile.get("size"), "captured": (tile.get("shows") or {}).get("captured_size"),
                 "path": tile.get("path")}
        ok = rc == 0 and res.get("fingerprint", {}).get("equal") and tile.get("path")
        if extra:
            more_ok, more = extra(tile)
            ok = ok and more_ok
            facts.update(more)
        row("G8 ui %s: a screenshot, everything put back" % name, ok, error=res.get("error"), **facts)
    return check


def sidebar_extra(tile):
    _w, area = view3d()
    ui = next(r for r in area.regions if r.type == "UI")
    return (not area.spaces.active.show_region_ui and tile["size"][0] < 600,
            {"sidebar_closed_again": not area.spaces.active.show_region_ui, "ui_region_width": ui.width})


def prefs_extra(_tile):
    prefs = bpy.context.preferences
    return (prefs.active_section == STATE["section"] and prefs.is_dirty == STATE["dirty"],
            {"section": prefs.active_section, "dirty": prefs.is_dirty})


def check_refused(name):
    def check(rc, res):
        row("G9 %s is refused before anything (exit 3)" % name, rc == 3, error=res.get("error"))
    return check


def check_ok(name):
    def check(rc, res):
        row("G9 %s runs" % name, rc == 0, error=res.get("error"))
    return check


def render_now():
    scene = bpy.context.scene
    keep = (scene.render.resolution_x, scene.render.resolution_y)
    scene.render.resolution_x, scene.render.resolution_y = 320, 240
    bpy.ops.render.render(write_still=False)
    scene.render.resolution_x, scene.render.resolution_y = keep


def check_render_result(rc, res):
    tile = (res.get("tiles") or [{}])[0]
    row("G10 image Render Result after a render", rc == 0 and tile.get("size") == [320, 240], size=tile.get("size"),
        error=res.get("error"))


CASES = [
    (["--mode", "viewport", "--out", out("viewport.png")], check_viewport, None),
    (["--mode", "viewport", "--shading", "wireframe", "--out", out("viewport_wire.png")], check_viewport_wire, None),
    (["--mode", "angles", "--target", "Red", "--out", out("angles.png")], check_angles, None),
    (["--mode", "frames", "--frame-count", "6", "--view", "front", "--target", "Rig", "--center-bones", "Root",
      "--isolate", "--out", out("frames_centered.png")], check_center, None),
    (["--mode", "frames", "--frames", "1,11,21", "--view", "viewport", "--out", out("frames_viewport.png")],
     check_frames_viewport, None),
    (["--mode", "camera", "--shading", "rendered", "--out", out("camera_rendered.png")], check_camera, None),
    (["--mode", "frames", "--frame-count", "12", "--view", "front", "--tick-budget", "0", "--out",
      out("frames_meanwhile.png")], check_meanwhile, "plant"),
    (["--mode", "ui", "--ui", "window", "--out", out("window.png")], check_ui("window"), None),
    (["--mode", "ui", "--ui", "sidebar:Item", "--out", out("sidebar.png")], check_ui("sidebar:Item", sidebar_extra), None),
    (["--mode", "ui", "--ui", "properties:material", "--ui-object", "Green", "--out", out("properties.png")],
     check_ui("properties:material", None), None),
    (["--mode", "ui", "--ui", "preferences:cycles", "--out", out("preferences.png")],
     check_ui("preferences:cycles", prefs_extra), "prefs"),
    (["--mode", "ui", "--ui", "area:OUTLINER", "--out", out("outliner.png")], check_ui("area:OUTLINER"), None),
    (["--mode", "angles", "--expect-pid", str(os.getpid() + 1), "--out", out("wrong_pid.png")],
     check_refused("a wrong --expect-pid"), None),
    (["--mode", "angles", "--expect-pid", str(os.getpid()), "--expect-file", "gui_control.blend",
      "--views", "front", "--out", out("right_pid.png")], check_ok("the right --expect-pid and --expect-file"), None),
    (["--mode", "image", "--image", "Render Result", "--out", out("render_result.png")], check_render_result, "render"),
]


def tick():
    try:
        return _tick()
    except Exception:
        import traceback
        row("the GUI check itself crashed", False, error=traceback.format_exc()[-1500:])
        return finish()


def _tick():
    case = STATE["case"]
    if case >= len(CASES):
        return finish()
    args, check, special = CASES[case]
    proc = STATE["proc"]
    if proc is None:
        if special == "render":
            render_now()
        if special == "prefs":
            STATE["section"], STATE["dirty"] = bpy.context.preferences.active_section, bpy.context.preferences.is_dirty
        STATE["before"] = tl._state()
        STATE["planted"] = False
        STATE["stdout"] = open(WORK / ("gui_case_%02d.json" % case), "w", encoding="utf-8")
        STATE["stderr"] = open(WORK / ("gui_case_%02d.log" % case), "w", encoding="utf-8")
        env = dict(os.environ, BLENDER_MCP_HOST="127.0.0.1", BLENDER_MCP_PORT=str(PORT))
        STATE["proc"] = subprocess.Popen([sys.executable, "-X", "utf8", str(LOOK)] + args, stdout=STATE["stdout"],
                                         stderr=STATE["stderr"], env=env)
        STATE["started"] = time.time()
        return 0.05
    if special == "plant" and not STATE["planted"] and time.time() - STATE["started"] > 0.6:
        bpy.data.objects["Green"].select_set(True)      # "the user" clicks while the job runs
        STATE["planted"] = True
    if proc.poll() is None:
        if time.time() - STATE["started"] > 300:
            proc.kill()
        return 0.05
    STATE["stdout"].close()
    STATE["stderr"].close()
    lines = [l for l in (WORK / ("gui_case_%02d.json" % case)).read_text(encoding="utf-8").splitlines() if l.strip()]
    result = json.loads(lines[-1]) if lines else {}
    after = tl._state()
    if special == "plant":
        after["selected"] = [n for n in after["selected"] if n != "Green"]
    drift = sorted(k for k in STATE["before"] if STATE["before"][k] != after.get(k))
    check(proc.returncode, result)
    row("   ... and the independent state dump is unchanged", not drift, changed=drift)
    STATE["proc"] = None
    STATE["case"] += 1
    return 0.2


def finish():
    passed = sum(1 for r in ROWS if r["ok"])
    (WORK / "gui_rows.json").write_text(json.dumps(ROWS, indent=1, default=str), encoding="utf-8")
    print("GUI| %d of %d rows passed; pictures in %s" % (passed, len(ROWS), WORK / "gui_runs"), flush=True)
    try:
        STATE["server"].stop()
    except Exception:
        pass
    if not KEEP_OPEN:
        # quit_blender exits with 0 whatever happened: the summary line and gui_rows.json are the verdict.
        bpy.app.timers.register(lambda: bpy.ops.wm.quit_blender() and None, first_interval=0.5)
    return None


prepare()
bpy.app.timers.register(tick, first_interval=2.0)

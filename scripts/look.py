# Portions adapted from MCP for Blender (https://github.com/ahujasid/mcp-for-blender, commit 25e1fe0), Copyright (c) 2025 Siddharth Ahuja, MIT License; see THIRD_PARTY_NOTICES.md.
# SPDX-License-Identifier: GPL-3.0-or-later
"""See a Blender scene as pictures: one labelled contact sheet plus every view on its own.

    python look.py --mode angles --target Chair --views front,right,top          # the running Blender (bridge)
    python look.py --mode viewport                                                # what the user sees now
    python look.py --mode frames --frame-count 8 --view front --target Rig --center-bones hips
    python look.py --mode camera --shading rendered --out C:/scratch/cam.png
    python look.py --mode ui --ui sidebar:MyAddon                                # an add-on's sidebar tab
    python look.py --blend scene.blend --mode angles --shading wireframe          # headless, no bridge
    python look.py --new --setup build_scene.py --mode angles --save-as built.blend

Two ways to reach Blender:
  LIVE (default): the Blender Lab MCP bridge of a running Blender, at BLENDER_MCP_HOST / BLENDER_MCP_PORT
    (localhost:9876). --expect-file / --expect-pid refuse before anything is touched when the Blender that
    answers is not that one. Nothing is saved, no file is opened; every setting changed for a picture is put
    back, every temporary datablock removed, and a fingerprint of the session taken before and after proves
    it (the run fails, exit 5, when anything is left changed).
  HEADLESS (--blend FILE or --new): a background Blender ("$BLENDER_EXE", else blender on PATH, else the usual
    install folders) with --factory-startup and script auto-run off, which exits when done. --save-as PATH
    saves a copy afterwards (never over the input file).

Modes:
  viewport  what the user sees in the largest 3D viewport (live with a window only; elsewhere refused, or
            --fallback camera|angles)
  camera    through the scene camera, at the render's aspect
  angles    the targets from several sides, framed on their evaluated vertices: --views front,back,left,
            right,top,bottom,three_quarter or x:y:z directions toward the eye (up to 6); --distance, --ortho
  frames    a strip over the animation: --frames 1,5,9 or 1-21 or 1-21:5, or --frame-count N over the
            scene range; --view camera | viewport | an angle; --center-bones a,b keeps those bones of the
            armature (--armature, or the first among the targets) in the middle of every tile
  image     "Render Result", an image datablock, or an image file (--image)
  ui        a screenshot, live with a window only (--ui): window | view3d | sidebar:TAB |
            properties:TAB (--ui-object pins an object) | preferences:ADDON | area:TYPE
Shading: --shading solid | material | rendered | wireframe | xray (default: the viewport's when live, solid
headless). Wireframe is the edges over a plain surface; xray is see-through with bones in front.

Output: one JSON line on stdout (the sheet, every tile with what it shows and its view and projection
matrices, the targets, warnings, timing, the fingerprint check); human lines on stderr. Exit codes:
0 ok, 1 Blender failed, 2 bad arguments, 3 refused, 4 Blender not reachable, 5 left something changed.

How the pictures are made: Blender draws its own viewport into an offscreen buffer (in a background
Blender 5.2+ too, after gpu.init()); where that is not possible (background Blender before 5.2, or
--backend render) each tile is rendered with Workbench/EEVEE through a temporary camera and the result's
warnings say what differs. In a windowed Blender a job of several tiles is cut into bridge ticks (the
bridge's deferred response, check_is_finished): each tick does --tick-budget seconds of tiles, puts
everything back and is checked by its own fingerprint before Blender's event loop runs again, so the
interface stays alive and the bridge's request timeout is never reached. A background Blender (headless,
or a bridge served with --background) has no deferred responses and does the job in one go.
The pictures are written by Blender, so --out is a path on the machine Blender runs on.
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
INSIDE = HERE / "look_blender.py"
MODES = ("viewport", "camera", "angles", "frames", "image", "ui")
SHADINGS = ("solid", "material", "rendered", "wireframe", "xray")
ANGLES = ("front", "back", "left", "right", "top", "bottom", "three_quarter")
EXIT = {"ok": 0, "failed": 1, "bad_args": 2, "refused": 3, "unreachable": 4, "left_changed": 5}


def say(*parts):
    print("look:", *parts, file=sys.stderr)


def finish(result, code=None):
    print(json.dumps(result, separators=(",", ":")))
    if code is None:
        code = 0 if result.get("ok") else EXIT.get(result.get("code"), 1)
    sys.exit(code)


# -- arguments -------------------------------------------------------------------------------

def _views(text):
    out = []
    for part in [p.strip() for p in (text or "").split(",") if p.strip()]:
        if ":" in part:
            try:
                vec = [float(v) for v in part.split(":")]
            except ValueError:
                raise argparse.ArgumentTypeError("a direction is x:y:z, got %r" % part)
            if len(vec) != 3 or not any(vec):
                raise argparse.ArgumentTypeError("a direction is three numbers x:y:z, not all 0, got %r" % part)
            out.append(vec)
        elif part.lower() in ANGLES:
            out.append(part.lower())
        else:
            raise argparse.ArgumentTypeError("unknown view %r; use %s or x:y:z" % (part, ", ".join(ANGLES)))
    return out


def _frames(text):
    frames = []
    for part in [p.strip() for p in (text or "").split(",") if p.strip()]:
        try:
            if "-" in part.lstrip("-"):
                body, _, step = part.partition(":")
                head, _, tail = body.lstrip("-").partition("-")
                first = int(("-" if body.startswith("-") else "") + head)
                last = int(tail)
                frames.extend(range(first, last + 1, int(step) if step else 1))
            else:
                frames.append(int(part))
        except ValueError:
            raise argparse.ArgumentTypeError("frames are 1,5,9 or 1-21 or 1-21:5, got %r" % part)
    return frames


def _names(values):
    out = []
    for value in values or []:
        out.extend(n.strip() for n in value.split(",") if n.strip())
    return out


def parser():
    p = argparse.ArgumentParser(prog="look.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", choices=MODES, help="default: viewport when live, angles headless")
    p.add_argument("--target", action="append", metavar="NAMES", help="objects to frame (children included), comma "
                   "separated, repeatable; default every visible object")
    p.add_argument("--views", type=_views, metavar="LIST", help="angles: front,right,top,three_quarter (default) or x:y:z")
    p.add_argument("--distance", type=float, help="metres from the targets' centre to the eye (angles, frames)")
    p.add_argument("--fov", type=float, default=35.0, help="vertical field of view in degrees (default 35)")
    p.add_argument("--ortho", action="store_true", help="orthographic angles and frames")
    p.add_argument("--fill", type=float, default=0.85, help="how much of a tile the targets fill (0.2-0.98, default 0.85)")
    p.add_argument("--shading", choices=SHADINGS)
    p.add_argument("--frames", type=_frames, metavar="LIST", help="frames: 1,5,9 or 1-21 or 1-21:5")
    p.add_argument("--frame-count", type=int, help="frames: this many over the scene range (2-24, default 6)")
    p.add_argument("--view", help="frames: camera, viewport, an angle name or x:y:z (default viewport live, front headless)")
    p.add_argument("--center-bones", metavar="A,B", help="frames: keep these bones in the middle of every tile")
    p.add_argument("--armature", help="frames: the armature for --center-bones")
    p.add_argument("--image", help='image: "Render Result", an image datablock name, or a file')
    p.add_argument("--ui", help="ui: window | view3d | sidebar:TAB | properties:TAB | preferences:ADDON | area:TYPE")
    p.add_argument("--ui-object", help="ui properties: pin this object in the Properties editor")
    p.add_argument("--redraws", type=int, default=3, help="ui: redraws before the screenshot (default 3)")
    p.add_argument("--max-size", type=int, default=768, help="longest side of each tile (default 768)")
    p.add_argument("--sheet-size", type=int, default=1536, help="longest side of the contact sheet (default 1536)")
    p.add_argument("--out", help="a folder, or the sheet's .png (tiles beside it); default a new temp folder")
    p.add_argument("--tmp-dir", help="where Blender writes its short-lived files (renders, screenshots); default "
                   "the --out folder, or the system temp folder when that path is long")
    p.add_argument("--overlays", action="store_true", help="keep the viewport overlays in camera/angles/frames views")
    p.add_argument("--isolate", action="store_true", help="hide every other object for the pictures")
    p.add_argument("--unhide", action="store_true", help="show hidden targets for the pictures")
    p.add_argument("--fallback", choices=("camera", "angles"), help="viewport not available: show this instead")
    p.add_argument("--backend", choices=("auto", "draw", "render"), default="auto",
                   help="draw: Blender draws its viewport offscreen (default where possible); render: Workbench/EEVEE "
                   "through a temporary camera (background Blender only)")
    p.add_argument("--expect-file", help="refuse unless the Blender that answers has this file open (name or path)")
    p.add_argument("--expect-pid", type=int, help="refuse unless the Blender that answers has this process id")
    p.add_argument("--no-deferred", action="store_true", help="live: do the whole job in one bridge request")
    p.add_argument("--tick-budget", type=float, default=0.15, help="live, windowed: seconds of work per bridge tick "
                   "before Blender's event loop runs again (default 0.15; 0 = one tile a tick)")
    p.add_argument("--timeout", type=float, help="seconds to wait (default 300 live, 600 headless)")
    p.add_argument("--force-deferred", action="store_true", help=argparse.SUPPRESS)   # tests: deferred in background
    head = p.add_argument_group("headless")
    which = head.add_mutually_exclusive_group()
    which.add_argument("--blend", help="open this .blend in a background Blender")
    which.add_argument("--new", action="store_true", help="a new factory scene in a background Blender")
    head.add_argument("--setup", help="headless: run this Python file first (build a scene, enable an add-on)")
    head.add_argument("--save-as", help="headless: save a copy of the scene here afterwards (never the input file)")
    head.add_argument("--blender", help="the Blender executable (default $BLENDER_EXE, PATH, usual install folders)")
    head.add_argument("--enable-autoexec", action="store_true", help="headless: let the file's scripts and Python "
                      "drivers run (only for files you trust)")
    head.add_argument("--keep-log", action="store_true", help="headless: keep Blender's log (its path is in the JSON; kept anyway when Blender fails)")
    return p


def build_args(ns, headless):
    out = ns.out or tempfile.mkdtemp(prefix="blender_look_")
    if not ns.out:
        say("pictures go to", out)
    args = {
        "mode": ns.mode or ("angles" if headless else None),
        "targets": _names(ns.target), "views": ns.views, "distance": ns.distance, "fov": ns.fov, "ortho": ns.ortho,
        "fill": ns.fill, "shading": ns.shading, "frames": ns.frames, "frame_count": ns.frame_count,
        "view": _view_arg(ns.view), "center_bones": _names([ns.center_bones] if ns.center_bones else []),
        "armature": ns.armature, "image": ns.image, "ui": ns.ui, "ui_object": ns.ui_object, "redraws": ns.redraws,
        "max_size": ns.max_size, "sheet_max": ns.sheet_size, "out": os.path.abspath(out), "overlays": ns.overlays,
        "isolate": ns.isolate, "unhide": ns.unhide, "fallback": ns.fallback, "backend": ns.backend,
        "expect_file": ns.expect_file, "expect_pid": ns.expect_pid, "deferred": not ns.no_deferred,
        "tick_budget": max(0.0, ns.tick_budget), "force_deferred": bool(ns.force_deferred),
        "tmp_dir": os.path.abspath(ns.tmp_dir) if ns.tmp_dir else None,
    }
    return args


def _view_arg(text):
    if not text:
        return None
    if ":" in text:
        return _views(text)[0]
    return text.strip().lower()


# -- live --------------------------------------------------------------------------------------

def live(args, timeout):
    host = os.environ.get("BLENDER_MCP_HOST", "localhost")
    port = int(os.environ.get("BLENDER_MCP_PORT", "9876"))
    code = INSIDE.read_text(encoding="utf-8")
    code += ("\n_LOOK_ARGS = json.loads(%r)\n_look = bridge_start(_LOOK_ARGS)\n"
             "if callable(_look):\n    check_is_finished = _look\nelse:\n    result = _look\n" % json.dumps(args))
    payload = json.dumps({"type": "execute", "code": code, "strict_json": True}).encode("utf-8") + b"\0"
    started = time.perf_counter()
    try:
        link = socket.create_connection((host, port), timeout=10)
    except OSError as error:
        finish({"ok": False, "code": "unreachable", "tool": "look",
                "error": "no Blender bridge at %s:%d (%s); start the MCP extension's server, or use --blend/--new"
                % (host, port, error)})
    try:
        link.sendall(payload)
        link.settimeout(timeout)
        data = b""
        while b"\0" not in data:
            chunk = link.recv(1 << 16)
            if not chunk:
                break
            data += chunk
    except socket.timeout:
        finish({"ok": False, "code": "failed", "tool": "look",
                "error": "no answer within %g s (the work may still be running in Blender; each finished tile was "
                         "put back); give a longer --timeout" % timeout})
    finally:
        link.close()
    if not data:
        finish({"ok": False, "code": "failed", "tool": "look", "error": "the bridge closed without an answer"})
    answer = json.loads(data.split(b"\0")[0].decode("utf-8"))
    if answer.get("status") != "ok":
        message = str(answer.get("message", "")).strip()
        finish({"ok": False, "code": "failed", "tool": "look", "error": "Blender: " + (message.splitlines() or ["?"])[-1],
                "detail": message})
    result = answer.get("result") or {}
    result.setdefault("client", {})
    result["client"].update({"kind": "live", "host": host, "port": port,
                             "seconds": round(time.perf_counter() - started, 3)})
    return result


# -- headless ----------------------------------------------------------------------------------

def find_blender(explicit=None):
    candidates = []
    if explicit:
        candidates.append(("--blender", explicit))
    if os.environ.get("BLENDER_EXE"):
        candidates.append(("$BLENDER_EXE", os.environ["BLENDER_EXE"]))
    on_path = shutil.which("blender")
    if on_path:
        candidates.append(("PATH", on_path))
    found = []
    if sys.platform.startswith("win"):
        for root in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
            base = Path(root) / "Blender Foundation"
            if base.is_dir():
                found += sorted(base.glob("Blender*/blender.exe"), reverse=True)
            steam = Path(root) / "Steam" / "steamapps" / "common" / "Blender" / "blender.exe"
            if steam.is_file():
                found.append(steam)
    elif sys.platform == "darwin":
        found += [Path("/Applications/Blender.app/Contents/MacOS/Blender")]
        found += sorted(Path("/Applications").glob("Blender*.app/Contents/MacOS/Blender"), reverse=True)
    else:
        found += [Path("/usr/bin/blender"), Path("/usr/local/bin/blender"), Path("/snap/bin/blender")]
        found += sorted(Path.home().glob("blender*/blender"), reverse=True)
        found += sorted(Path("/opt").glob("blender*/blender"), reverse=True)
    candidates += [("install folder", str(p)) for p in found]
    for how, path in candidates:
        if path and Path(path).is_file():
            return how, str(Path(path))
    return None, None


def headless(args, ns, timeout):
    how, exe = find_blender(ns.blender)
    if exe is None:
        finish({"ok": False, "code": "unreachable", "tool": "look",
                "error": "no Blender found: set BLENDER_EXE, put blender on PATH, or pass --blender"})
    say("Blender:", exe, "(%s)" % how)
    blend = os.path.abspath(ns.blend) if ns.blend else None
    if blend and not os.path.isfile(blend):
        finish({"ok": False, "code": "bad_args", "tool": "look", "error": "no file %s" % blend})
    if ns.save_as:
        target = os.path.normcase(os.path.abspath(ns.save_as))
        if blend and target == os.path.normcase(blend):
            finish({"ok": False, "code": "refused", "tool": "look",
                    "error": "--save-as is the input file; it is never overwritten"})
        args["save_as"] = os.path.abspath(ns.save_as)
    if ns.setup and not os.path.isfile(ns.setup):
        finish({"ok": False, "code": "bad_args", "tool": "look", "error": "no setup file %s" % ns.setup})
    out = args["out"]
    folder = os.path.dirname(out) if out.lower().endswith(".png") else out
    os.makedirs(folder, exist_ok=True)
    tag = "%d_%d" % (os.getpid(), int(time.time() * 1000) % 100000)
    # Blender's own Python cannot open paths of 260 characters or more on Windows: its hand-off files
    # go to a short folder when the picture folder is deep.
    control = args.get("tmp_dir") or folder
    if os.name == "nt" and len(os.path.abspath(control)) >= 220:
        control = tempfile.gettempdir()
    os.makedirs(control, exist_ok=True)
    args_path = os.path.join(control, ".look_args_%s.json" % tag)
    result_path = os.path.join(control, ".look_result_%s.json" % tag)
    log_path = os.path.join(control, ".look_blender_%s.log" % tag)
    args.update({"own_process": True, "result_path": result_path, "deferred": False})
    with open(args_path, "w", encoding="utf-8") as handle:
        json.dump(args, handle)
    cmd = [exe, "--background", "--factory-startup", "--enable-autoexec" if ns.enable_autoexec else "--disable-autoexec"]
    if blend:
        cmd.append(blend)
    cmd += ["--python-exit-code", "1"]
    if ns.setup:
        cmd += ["--python", os.path.abspath(ns.setup)]
    cmd += ["--python", str(INSIDE), "--", args_path]
    started = time.perf_counter()
    try:
        with open(log_path, "w", encoding="utf-8", errors="replace") as log:
            proc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
        returncode = proc.returncode
    except subprocess.TimeoutExpired:
        returncode = None
    finally:
        try:
            os.remove(args_path)
        except OSError:
            pass
    seconds = round(time.perf_counter() - started, 3)
    if os.path.isfile(result_path):
        with open(result_path, encoding="utf-8") as handle:
            result = json.load(handle)
        os.remove(result_path)
    else:
        tail = _tail(log_path)
        result = {"ok": False, "code": "failed", "tool": "look",
                  "error": ("Blender did not finish within %g s" % timeout) if returncode is None else
                  ("Blender exited (%s) without a result%s" % (returncode, " - the --setup script failed?" if ns.setup else "")),
                  "log_tail": tail}
    result.setdefault("client", {})
    result["client"].update({"kind": "headless", "blender_exe": exe, "found_by": how, "blend": blend,
                             "new": bool(ns.new), "setup": os.path.abspath(ns.setup) if ns.setup else None,
                             "autoexec": bool(ns.enable_autoexec), "seconds": seconds, "exit_code": returncode})
    if ns.keep_log or result.get("code") not in (None, "refused", "bad_args", "left_changed"):
        result["client"]["log"] = log_path       # Blender failed: keep its log for the reason
    else:
        try:
            os.remove(log_path)
        except OSError:
            pass
    return result


def _tail(path, lines=15):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read().splitlines()[-lines:]
    except OSError:
        return []


# -- main --------------------------------------------------------------------------------------

def report(result):
    blender = result.get("blender") or {}
    client = result.get("client") or {}
    if blender:
        say("%s Blender %s pid %s, file %s" % (client.get("kind", "?"), blender.get("version"), blender.get("pid"),
                                              blender.get("file") or "(unsaved)"))
    if not result.get("ok"):
        say("FAILED (%s): %s" % (result.get("code"), result.get("error")))
    sheet = result.get("sheet")
    if sheet:
        say("%s, %s, backend %s: %d tile(s) -> %s" % (result.get("mode"), result.get("shading"), result.get("backend"),
                                                     len(result.get("tiles") or []), sheet["path"]))
    for tile in result.get("tiles") or []:
        say("  %02d %-34s %s" % (tile["index"], tile["label"][:34], tile["path"]))
    for warning in result.get("warnings") or []:
        say("warning:", warning)
    fp = result.get("fingerprint")
    if fp:
        if fp.get("equal"):
            say("session put back: fingerprint equal (%s values compared, %s)" % (fp.get("values"), fp.get("how")))
        else:
            say("SESSION NOT PUT BACK:", "; ".join(fp.get("differences", [])[:8]))


def main(argv=None):
    p = parser()
    ns = p.parse_args(argv)
    is_headless = bool(ns.blend or ns.new)
    if not is_headless:
        for flag in ("setup", "save_as", "blender"):
            if getattr(ns, flag):
                p.error("--%s is for headless runs (--blend or --new)" % flag.replace("_", "-"))
        if ns.enable_autoexec:
            p.error("--enable-autoexec is for headless runs")
    if ns.mode == "image" and not ns.image:
        ns.image = "Render Result"
    if ns.mode == "ui" and is_headless:
        finish({"ok": False, "code": "refused", "tool": "look",
                "error": "ui: a headless Blender has no window to screenshot; use a live Blender"})
    if not (5 <= ns.fov <= 120):
        p.error("--fov is 5 to 120 degrees")
    args = build_args(ns, is_headless)
    timeout = ns.timeout or (600.0 if is_headless else 300.0)
    result = headless(args, ns, timeout) if is_headless else live(args, timeout)
    report(result)
    finish(result)


if __name__ == "__main__":
    main()

# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for look.py, measured from the pictures it writes.

    python test_look.py --work DIR                                   # headless tests (background Blender)
    python test_look.py --work DIR --bridge-port 9881 --bridge-pkg PARENT/mcp103   # and through the bridge
    python test_look.py --work DIR --mutate                          # break a copy of the tool, see the rows fail

The same file runs inside Blender for the parts that need bpy:
    blender -b --factory-startup --python test_look.py -- build OUT.blend
    blender -b --factory-startup CONTROL.blend --python test_look.py -- harness SCRIPTS_DIR WORK_DIR
    blender -b --factory-startup CONTROL.blend --python test_look.py -- serve PACKAGE_DIR PORT

The control scene: "Red" (2 m cube), "Green" (sphere), "Blue" (cube hidden behind Red from the front),
"Mover" (yellow cube keyed linearly from x=-4 at frame 1 to x=4 at frame 21), "Rig" (an armature keyed from
x=-3 to x=3, bone "Tip" keyed) with "Tracker" (magenta) parented to its bone "Root", "Hidden" (hidden),
"Ghost" (in an excluded collection), a camera, a sun, a packed image "Swatch".

Every check is first run on a control where the answer is known (an image measured against itself, a
rectangle of known size, a planted wrong input that must be caught). Rows print as PASS/FAIL with numbers.
Stdlib only on this side; the pictures are read by a small PNG reader that is itself checked first.
"""
import sys
sys.dont_write_bytecode = True     # no __pycache__ beside the skill's files
import argparse
import ctypes
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path

try:
    import bpy  # noqa: F401
    INSIDE_BLENDER = True
except ImportError:
    INSIDE_BLENDER = False

HERE = Path(__file__).resolve().parent
COLORS = {"Red": (0.8, 0.04, 0.04), "Green": (0.04, 0.7, 0.04), "Blue": (0.04, 0.04, 0.8),
          "Mover": (0.8, 0.75, 0.02), "Tracker": (0.8, 0.02, 0.8), "Hidden": (0.9, 0.2, 0.0), "Ghost": (0.5, 0.5, 0.5)}
SWATCH = ((200, 40, 40), (40, 40, 200))       # left half, right half of the packed image "Swatch" (64 x 32)


# ==============================================================================================
# Inside Blender

def build_control(path):
    import bpy
    from mathutils import Vector
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob)
    for coll in (bpy.data.meshes, bpy.data.materials, bpy.data.cameras, bpy.data.lights, bpy.data.armatures):
        for item in list(coll):
            coll.remove(item)
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end, scene.frame_current = 1, 21, 1
    scene.view_settings.view_transform = "Standard"
    scene.view_settings.look = "None"
    scene.render.resolution_x, scene.render.resolution_y = 800, 600

    def material(name, rgb):
        mat = bpy.data.materials.new(name)
        mat.diffuse_color = (*rgb, 1.0)
        if hasattr(mat, "use_nodes") and not mat.use_nodes:
            mat.use_nodes = True
        bsdf = next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if bsdf is not None:
            bsdf.inputs["Base Color"].default_value = (*rgb, 1.0)
        return mat

    def linear(ob):
        from bpy_extras import anim_utils
        adt = ob.animation_data
        bag = anim_utils.action_get_channelbag_for_slot(adt.action, adt.action_slot)
        for fc in bag.fcurves:
            for kp in fc.keyframe_points:
                kp.interpolation = "LINEAR"

    def add(kind, name, location, **kw):
        getattr(bpy.ops.mesh, "primitive_%s_add" % kind)(location=location, **kw)
        ob = bpy.context.object
        ob.name = name
        ob.data.materials.append(material(name, COLORS[name]))
        return ob

    add("cube", "Red", (0, 0, 1), size=2)
    add("uv_sphere", "Green", (3, 0, 0.6), radius=0.6, segments=24, ring_count=12)
    add("cube", "Blue", (0, 2.5, 0.4), size=0.8)
    mover = add("cube", "Mover", (-4, -3, 0.25), size=0.5)
    mover.keyframe_insert("location", frame=1)
    mover.location.x = 4
    mover.keyframe_insert("location", frame=21)
    linear(mover)
    bpy.ops.object.armature_add(location=(-3, 0, 0))
    rig = bpy.context.object
    rig.name = "Rig"
    bpy.ops.object.mode_set(mode="EDIT")
    root = rig.data.edit_bones[0]
    root.name = "Root"
    root.head, root.tail = (0, 0, 0), (0, 0, 1)
    tip = rig.data.edit_bones.new("Tip")
    tip.head, tip.tail = (0, 0, 1), (0, 0, 2)
    tip.parent = root
    bpy.ops.object.mode_set(mode="OBJECT")
    rig.keyframe_insert("location", frame=1)
    rig.location.x = 3
    rig.keyframe_insert("location", frame=21)
    linear(rig)
    pb = rig.pose.bones["Tip"]
    pb.rotation_mode = "QUATERNION"
    pb.keyframe_insert("rotation_quaternion", frame=1)
    pb.rotation_quaternion = (0.92388, 0.38268, 0, 0)
    pb.keyframe_insert("rotation_quaternion", frame=21)
    pb.rotation_quaternion = (1, 0, 0, 0)
    rig.location.x = -3
    bpy.ops.mesh.primitive_cube_add(size=0.4)
    tracker = bpy.context.object
    tracker.name = "Tracker"
    tracker.data.materials.append(material("Tracker", COLORS["Tracker"]))
    tracker.parent = rig
    tracker.parent_type = "BONE"
    tracker.parent_bone = "Root"
    tracker.location = (0, 0, 0)            # at the tail of Root: 1 m above the rig's origin
    hidden = add("cube", "Hidden", (8, 8, 0.5), size=1)
    excluded = bpy.data.collections.new("Excluded")
    scene.collection.children.link(excluded)
    bpy.ops.mesh.primitive_cube_add(size=1, location=(-8, 8, 0.5))
    ghost = bpy.context.object
    ghost.name = "Ghost"
    for coll in list(ghost.users_collection):
        coll.objects.unlink(ghost)
    excluded.objects.link(ghost)
    bpy.context.view_layer.layer_collection.children["Excluded"].exclude = True
    hidden.hide_set(True)
    cam_data = bpy.data.cameras.new("Cam")
    cam_data.lens = 30
    cam = bpy.data.objects.new("Cam", cam_data)
    scene.collection.objects.link(cam)
    cam.location = (1, -16, 6)
    cam.rotation_euler = (Vector((0, 0, 1)) - cam.location).to_track_quat("-Z", "Y").to_euler()
    scene.camera = cam
    sun_data = bpy.data.lights.new("Sun", "SUN")
    sun_data.energy = 3
    sun = bpy.data.objects.new("Sun", sun_data)
    scene.collection.objects.link(sun)
    sun.rotation_euler = (0.6, 0.2, 0.4)
    world = scene.world or bpy.data.worlds.new("World")
    scene.world = world
    img = bpy.data.images.new("Swatch", 64, 32, alpha=False)
    px = []
    for y in range(32):
        for x in range(64):
            c = SWATCH[0] if x < 32 else SWATCH[1]
            px += [c[0] / 255.0, c[1] / 255.0, c[2] / 255.0, 1.0]
    img.pixels.foreach_set(px)
    img.pack()
    img.use_fake_user = True
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.spaces.active.shading.type = "SOLID"
                area.spaces.active.shading.color_type = "MATERIAL"
    for ob in bpy.context.view_layer.objects:
        ob.select_set(False)
    bpy.context.view_layer.objects.active = bpy.data.objects["Red"]
    scene.frame_set(1)
    bpy.ops.wm.save_as_mainfile(filepath=path)
    print("TL| built", path)


def _counts():
    import bpy
    out = {}
    for prop in bpy.data.bl_rna.properties:
        if prop.type == "COLLECTION" and prop.identifier not in ("all_ids",):
            coll = getattr(bpy.data, prop.identifier)
            out[prop.identifier] = sorted(i.name_full for i in coll
                                          if not (prop.identifier == "images" and i.type == "RENDER_RESULT"))
    return out


def _state():
    """An independent dump of the user's state (written separately from the tool's fingerprint)."""
    import bpy
    scene = bpy.context.scene
    layer = bpy.context.view_layer
    r = scene.render
    ims = r.image_settings
    out = {"frame": (scene.frame_current, scene.frame_subframe), "camera": scene.camera and scene.camera.name,
           "world": scene.world and scene.world.name, "engine": r.engine,
           "res": (r.resolution_x, r.resolution_y, r.resolution_percentage, r.pixel_aspect_x, r.film_transparent,
                   r.use_compositing, r.use_sequencer, r.use_border, r.use_single_layer),
           "format": (ims.file_format, ims.color_mode, ims.color_depth, ims.compression,
                      getattr(ims, "media_type", None), ims.linear_colorspace_settings.name),
           "display": (scene.display.shading.show_xray, scene.display.shading.color_type,
                       tuple(round(v, 5) for v in scene.display.shading.single_color)),
           "eevee": scene.eevee.taa_render_samples,
           "mode": bpy.context.mode, "active": layer.objects.active and layer.objects.active.name,
           "selected": sorted(o.name for o in layer.objects if o.select_get()),
           "hidden": sorted(o.name for o in layer.objects if o.hide_get()),
           "hide_render": sorted(o.name for o in bpy.data.objects if o.hide_render),
           "in_front": sorted(o.name for o in bpy.data.objects if o.show_in_front),
           "pose": [tuple(round(v, 5) for v in pb.rotation_quaternion) for pb in bpy.data.objects["Rig"].pose.bones],
           "mover": tuple(round(v, 5) for v in bpy.data.objects["Mover"].location),
           "exclude": [lc.exclude for lc in layer.layer_collection.children]}
    for window in bpy.context.window_manager.windows:
        for i, area in enumerate(window.screen.areas):
            out["area%d" % i] = area.ui_type
            if area.type == "VIEW_3D":
                sh, ov, r3d = area.spaces.active.shading, area.spaces.active.overlay, area.spaces.active.region_3d
                out["view%d" % i] = (sh.type, sh.light, sh.color_type, sh.show_xray, round(sh.xray_alpha, 4),
                                     ov.show_overlays, ov.show_wireframes, ov.show_bones, ov.show_floor,
                                     tuple(round(v, 5) for row in r3d.view_matrix for v in row))
    return out


def harness(scripts, work):
    """In-process rows: the fingerprint's controls, datablock counts and an independent state dump around
    every kind of look, unkeyed edits surviving a frames look, the Render Result."""
    import bpy
    sys.path.insert(0, scripts)
    import look_blender as lb
    rows = []

    def row(name, ok, **facts):
        rows.append({"name": name, "ok": bool(ok), **facts})
        print("TL| %s %s %s" % ("PASS" if ok else "FAIL", name, json.dumps(facts)))

    # H1 the fingerprint is stable with nothing in between
    t = time.perf_counter()
    a = lb.fingerprint()
    t1 = time.perf_counter() - t
    b = lb.fingerprint()
    row("H1 fingerprint: two with nothing between are equal", not lb.fingerprint_diff(a, b),
        values=len(a), seconds=round(t1, 4), differences=lb.fingerprint_diff(a, b)[:3])

    # H2 a planted change of each kind is found, and undoing it makes them equal again
    scene, layer = bpy.context.scene, bpy.context.view_layer
    area = next(a for w in bpy.context.window_manager.windows for a in w.screen.areas if a.type == "VIEW_3D")
    space = area.spaces.active
    rig = bpy.data.objects["Rig"]
    plants = [
        ("frame", lambda: scene.frame_set(5), lambda: scene.frame_set(1)),
        ("viewport shading", lambda: setattr(space.shading, "type", "WIREFRAME"), lambda: setattr(space.shading, "type", "SOLID")),
        ("overlay floor", lambda: setattr(space.overlay, "show_floor", not space.overlay.show_floor),
         lambda: setattr(space.overlay, "show_floor", not space.overlay.show_floor)),
        ("view distance", lambda: setattr(space.region_3d, "view_distance", space.region_3d.view_distance * 2),
         lambda: setattr(space.region_3d, "view_distance", space.region_3d.view_distance / 2)),
        ("render resolution", lambda: setattr(scene.render, "resolution_x", 801), lambda: setattr(scene.render, "resolution_x", 800)),
        ("file format", lambda: setattr(scene.render.image_settings, "file_format", "JPEG"),
         lambda: setattr(scene.render.image_settings, "file_format", "PNG")),
        ("selection", lambda: bpy.data.objects["Green"].select_set(True), lambda: bpy.data.objects["Green"].select_set(False)),
        ("active object", lambda: setattr(layer.objects, "active", bpy.data.objects["Green"]),
         lambda: setattr(layer.objects, "active", bpy.data.objects["Red"])),
        ("hide_render", lambda: setattr(bpy.data.objects["Green"], "hide_render", True),
         lambda: setattr(bpy.data.objects["Green"], "hide_render", False)),
        ("new datablock", lambda: bpy.data.images.new("planted", 4, 4),
         lambda: bpy.data.images.remove(bpy.data.images["planted"])),
        ("unkeyed pose", lambda: setattr(rig.pose.bones["Tip"], "rotation_quaternion", (0.9, 0.3, 0.3, 0)),
         lambda: setattr(rig.pose.bones["Tip"], "rotation_quaternion", (1, 0, 0, 0))),
        ("collection exclude", lambda: setattr(layer.layer_collection.children["Excluded"], "exclude", False),
         lambda: setattr(layer.layer_collection.children["Excluded"], "exclude", True)),
        ("show_in_front", lambda: setattr(rig, "show_in_front", True), lambda: setattr(rig, "show_in_front", False)),
        ("eevee samples", lambda: setattr(scene.eevee, "taa_render_samples", 3),
         lambda: setattr(scene.eevee, "taa_render_samples", 64)),
    ]
    scene.eevee.taa_render_samples = 64
    base = lb.fingerprint()
    for name, plant, undo in plants:
        plant()
        found = lb.fingerprint_diff(base, lb.fingerprint())
        undo()
        back = lb.fingerprint_diff(base, lb.fingerprint())
        row("H2 planted %s is found" % name, bool(found) and not back, found=found[:2], after_undo=back[:2])

    # H3 every kind of look leaves the datablock counts and the independent state dump unchanged
    variants = [
        ("angles solid", {"mode": "angles", "targets": ["Red"]}),
        ("angles wireframe", {"mode": "angles", "targets": ["Green"], "shading": "wireframe", "views": ["front"]}),
        ("angles xray", {"mode": "angles", "targets": ["Red", "Rig"], "shading": "xray", "views": ["front"]}),
        ("angles material", {"mode": "angles", "targets": ["Red"], "shading": "material", "views": ["front"]}),
        ("angles rendered", {"mode": "angles", "targets": ["Red"], "shading": "rendered", "views": ["front"]}),
        ("camera solid", {"mode": "camera"}),
        ("frames front", {"mode": "frames", "frames": [1, 11, 21], "view": "front", "targets": ["Mover"]}),
        ("frames center-bones", {"mode": "frames", "frames": [1, 21], "view": "front", "targets": ["Rig"],
                                 "center_bones": ["Root"]}),
        ("frames camera", {"mode": "frames", "frames": [1, 21], "view": "camera"}),
        ("isolate + unhide", {"mode": "angles", "targets": ["Hidden", "Red"], "unhide": True, "isolate": True,
                              "views": ["front"]}),
        ("image datablock", {"mode": "image", "image": "Swatch"}),
        ("render backend solid", {"mode": "angles", "targets": ["Red"], "backend": "render", "views": ["front"]}),
        ("render backend material", {"mode": "angles", "targets": ["Red"], "backend": "render", "shading": "material",
                                     "views": ["front"]}),
        ("render backend wireframe", {"mode": "angles", "targets": ["Green"], "backend": "render",
                                      "shading": "wireframe", "views": ["front"]}),
        ("render backend xray", {"mode": "angles", "targets": ["Red", "Rig"], "backend": "render", "shading": "xray",
                                 "views": ["front"]}),
        ("render backend frames", {"mode": "frames", "frames": [1, 21], "view": "front", "targets": ["Mover"],
                                   "backend": "render"}),
    ]
    for name, args in variants:
        args = dict(args, out=os.path.join(work, "harness", name.replace(" ", "_")), own_process=True,
                    tmp_dir=os.path.join(work, "t"))
        counts, state = _counts(), _state()
        t = time.perf_counter()
        result = lb.run(args)
        seconds = round(time.perf_counter() - t, 3)
        counts2, state2 = _counts(), _state()
        changed = sorted(k for k in counts if counts[k] != counts2.get(k))
        drift = sorted(k for k in state if state[k] != state2.get(k))
        row("H3 %s: ok, datablocks and state unchanged" % name,
            result.get("ok") and not changed and not drift and result.get("fingerprint", {}).get("equal"),
            seconds=seconds, error=result.get("error"), datablocks_changed=changed, state_changed=drift,
            tool_fingerprint=result.get("fingerprint", {}).get("equal"), warnings=result.get("warnings", [])[:2])

    # H4 an unkeyed edit on animated channels survives a frames look (Blender's frame change alone loses it)
    pb = rig.pose.bones["Tip"]
    mover = bpy.data.objects["Mover"]

    def edit():
        pb.rotation_quaternion = (0.8, 0.0, 0.6, 0.0)
        mover.location.y = -2.0

    def read():
        return tuple(round(v, 5) for v in pb.rotation_quaternion), round(mover.location.y, 5)

    edit()
    control_before = read()
    scene.frame_set(11)
    scene.frame_set(1)
    control_after = read()
    row("H4 control: a plain frame change does lose the unkeyed edit", control_before != control_after,
        before=control_before, after=control_after)
    edit()
    before = read()
    result = lb.run({"mode": "frames", "frames": [1, 11, 21], "view": "front", "targets": ["Mover", "Rig"],
                     "out": os.path.join(work, "harness", "unkeyed"), "own_process": True})
    after = read()
    row("H4 the unkeyed edit survives a frames look", before == after and result.get("ok"), before=before, after=after,
        error=result.get("error"))
    edit()
    real = lb.restore_animated
    lb.restore_animated = lambda snapshot: 0             # planted: the put-back of animated values is skipped
    try:
        result = lb.run({"mode": "frames", "frames": [1, 11], "view": "front", "targets": ["Mover"],
                         "out": os.path.join(work, "harness", "unkeyed_planted"), "own_process": True})
    finally:
        lb.restore_animated = real
    lost = read()
    row("H4 planted: without the put-back the edit is lost and the tool says so (exit left_changed)",
        lost != before and result.get("code") == "left_changed", after=lost, code=result.get("code"),
        differences=(result.get("fingerprint") or {}).get("differences", [])[:2])
    scene.frame_set(1)

    # H5 the Render Result: refused while empty, shown after a render, format put back
    rr = bpy.data.images.get("Render Result")
    if rr is not None:
        bpy.data.images.remove(rr)
    result = lb.run({"mode": "image", "image": "Render Result", "out": os.path.join(work, "harness", "rr_empty"),
                     "own_process": True})
    row("H5 an empty Render Result is refused", result.get("code") == "refused", error=result.get("error"))
    r = scene.render
    keep = (r.engine, r.resolution_x, r.resolution_y)
    r.engine, r.resolution_x, r.resolution_y = "BLENDER_WORKBENCH", 160, 120
    r.image_settings.file_format = "JPEG"
    bpy.ops.render.render(write_still=False)
    state = _state()
    result = lb.run({"mode": "image", "image": "Render Result", "out": os.path.join(work, "harness", "rr"),
                     "own_process": True})
    tile = (result.get("tiles") or [{}])[0]
    row("H5 the Render Result is shown at its size and the JPEG output format is put back",
        result.get("ok") and tile.get("size") == [160, 120] and _state() == state,
        size=tile.get("size"), format=bpy.context.scene.render.image_settings.file_format, error=result.get("error"))
    r.engine, r.resolution_x, r.resolution_y = keep
    r.image_settings.file_format = "PNG"

    # H7 float images: through the scene's view transform (Standard here: linear 0.5 shows as sRGB 188)
    flt = bpy.data.images.new("FloatGrey", 32, 16, alpha=False, float_buffer=True)
    flt.pixels.foreach_set([0.5, 0.5, 0.5, 1.0] * (32 * 16))
    counts = _counts()
    result = lb.run({"mode": "image", "image": "FloatGrey", "out": os.path.join(work, "harness", "float"),
                     "own_process": True, "tmp_dir": os.path.join(work, "t")})
    tile = (result.get("tiles") or [{}])[0]
    value = read_png(tile["path"])[3][8][48] if tile.get("path") else None
    counts.pop("images", None)
    after = _counts()
    after.pop("images", None)
    row("H7 a float image datablock is shown through the view transform (0.5 -> 188 +-1), temp scene removed",
        result.get("ok") and value is not None and abs(value - 188) <= 1 and counts == after, value=value,
        note=(tile.get("shows") or {}).get("note"))
    row("H7 planted: the untransformed value 128 fails the same check", not (abs(128 - 188) <= 1))
    exr = os.path.join(work, "grey_linear.exr")            # written by OpenImageIO: linear 0.5, no colour tag
    import OpenImageIO as oiio
    import numpy as np
    spec = oiio.ImageSpec(32, 16, 3, "float")
    output = oiio.ImageOutput.create(exr)
    output.open(exr, spec)
    output.write_image(np.full((16, 32, 3), 0.5, np.float32))
    output.close()
    tagged = os.path.join(work, "grey_tagged.exr")         # written by Blender: it stores sRGB 0.735 and says so
    flt.filepath_raw = tagged
    flt.file_format = "OPEN_EXR"
    flt.save()
    bpy.data.images.remove(flt)
    for name, path in (("a linear EXR (OpenImageIO, 0.5)", exr), ("an sRGB-tagged EXR (Blender's save, 0.735)", tagged)):
        result = lb.run({"mode": "image", "image": path, "out": os.path.join(work, "harness", "exr"), "own_process": True,
                         "tmp_dir": os.path.join(work, "t")})
        tile = (result.get("tiles") or [{}])[0]
        value = read_png(tile["path"])[3][8][48] if tile.get("path") else None
        row("H7 %s shows as 188 +-1 (its colour space honoured)" % name,
            result.get("ok") and value is not None and abs(value - 188) <= 1, value=value,
            note=(tile.get("shows") or {}).get("note"), error=result.get("error"))

    # H6 the windowed path (one tile per bridge tick, deferred), driven by hand here
    def deferred(args, between=None):
        job = lb.Job(dict(args, own_process=True))
        job.deferred = True                      # what a windowed Blender does
        steps, result = 0, None
        while result is None and steps < 200:
            result = job.step()
            steps += 1
            if between is not None and steps == 1:
                between()
        return result, steps

    frames_args = {"mode": "frames", "frames": [1, 6, 11, 16, 21], "view": "front", "targets": ["Mover"],
                   "tick_budget": 0.0}                    # one task a tick
    sync = lb.run(dict(frames_args, out=os.path.join(work, "harness", "sync"), own_process=True))
    result, steps = deferred(dict(frames_args, out=os.path.join(work, "harness", "deferred")))
    same = [read_png(a["path"])[3] == read_png(b["path"])[3] for a, b in zip(sync.get("tiles", []), result.get("tiles", []))]
    row("H6 deferred: the job takes several ticks, every tile checked, pictures identical to one synchronous run",
        result.get("ok") and steps > 1 and len(same) == 5 and all(same), ticks=steps, identical=same,
        how=(result.get("fingerprint") or {}).get("how"))
    green = bpy.data.objects["Green"]
    result, steps = deferred(dict(frames_args, out=os.path.join(work, "harness", "deferred_user")),
                             between=lambda: green.select_set(True))
    meanwhile = (result.get("fingerprint") or {}).get("changed_meanwhile", [])
    row("H6 a change made by someone else between ticks is reported, not blamed on the tool",
        result.get("ok") and any("Green" in m for m in meanwhile), meanwhile=meanwhile[:2], ticks=steps)
    green.select_set(False)
    overlay = next(a for w in bpy.context.window_manager.windows for a in w.screen.areas
                   if a.type == "VIEW_3D").spaces.active.overlay
    floor = overlay.show_floor
    real = lb.Job._apply_shading

    def leaky(self, changes, generated):
        real(self, changes, generated)
        self.dv.space.overlay.show_floor = not floor        # planted: changed and not recorded for undo
    lb.Job._apply_shading = leaky
    try:
        result, steps = deferred(dict(frames_args, out=os.path.join(work, "harness", "deferred_leak")))
    finally:
        lb.Job._apply_shading = real
        overlay.show_floor = floor
    leaked = result.get("code") == "left_changed" and any("show_floor" in d for d in result["fingerprint"]["differences"])
    row("H6 planted: a tile that leaves a setting changed fails the run (left_changed, names the setting)", leaked,
        code=result.get("code"), differences=(result.get("fingerprint") or {}).get("differences", [])[:1])

    with open(os.path.join(work, "harness_rows.json"), "w", encoding="utf-8") as handle:
        json.dump(rows, handle)


def serve(package_dir, port, poll=False):
    """Serve the bridge (a downloaded copy of the Blender Lab MCP add-on) from this background Blender.

    poll: drive the add-on's non-blocking server (the one a windowed Blender runs from a timer) from a loop
    here, so that its deferred responses (check_is_finished) work too; otherwise its blocking background loop
    (what `blender --background --command blender_mcp` runs)."""
    import importlib
    package_dir = os.path.abspath(package_dir)
    sys.path.insert(0, os.path.dirname(package_dir))
    name = os.path.basename(package_dir)
    server = importlib.import_module(name + ".mcp_to_blender_server")
    blocking = importlib.import_module(name + ".execute_blocking")
    interactive = importlib.import_module(name + ".execute_interactive")
    server.start("127.0.0.1", int(port))
    print("TL| serving on", port, "pid", os.getpid(), "poll" if poll else "blocking", flush=True)
    try:
        if poll:
            while server.is_running():
                interactive.run()
                time.sleep(0.02)
        else:
            blocking.run()
    finally:
        server.stop()


# ==============================================================================================
# Outside Blender: the instruments

def _long(path):
    path = os.path.abspath(str(path))
    prefix = "\\\\?\\"
    if os.name == "nt" and len(path) >= 240 and not path.startswith(prefix):
        return prefix + path
    return path


def read_png(path):
    """(width, height, channels, rows) for an 8-bit RGB/RGBA/grey PNG; rows are bytes, top first."""
    data = Path(_long(path)).read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG: %s" % path)
    pos, idat, head = 8, [], None
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            head = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
        pos += 12 + length
    width, height, depth, ctype, _c, _f, interlace = head
    if depth != 8 or interlace:
        raise ValueError("only 8-bit non-interlaced PNGs: %s" % path)
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[ctype]
    raw = zlib.decompress(b"".join(idat))
    stride = width * channels
    rows, prev = [], bytearray(stride)
    for y in range(height):
        f = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        if f == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 255
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif f == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 255
        elif f == 4:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b, c = prev[i], prev[i - channels] if i >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append(bytes(line))
        prev = line
    return width, height, channels, rows


def write_png(path, width, height, rgb_rows):
    raw = b"".join(b"\0" + bytes(r) for r in rgb_rows)

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                           + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def pixels(img):
    """(r, g, b) tuples of a read_png result, row by row."""
    w, h, c, rows = img
    for y, row in enumerate(rows):
        if c >= 3:
            yield y, zip(row[0::c], row[1::c], row[2::c])
        else:
            yield y, zip(row, row, row)


CLASSES = {
    # Hue tests, not brightness: a face turned from the light is a darker shade of the same colour.
    "red": lambda r, g, b: r > 40 and r > 1.7 * g and r > 1.7 * b,
    "green": lambda r, g, b: g > 40 and g > 1.6 * r and g > 1.6 * b,
    "blue": lambda r, g, b: b > 40 and b > 1.7 * r and b > 1.7 * g,
    "yellow": lambda r, g, b: r > 40 and g > 40 and b < 0.5 * min(r, g) and abs(r - g) < 0.25 * max(r, g),
    "magenta": lambda r, g, b: r > 40 and b > 40 and g < 0.55 * min(r, b) and abs(r - b) < 0.25 * max(r, b),
    "bluish": lambda r, g, b: b > 50 and b > 1.4 * g,
    "dark": lambda r, g, b: r < 50 and g < 50 and b < 50,
}


def mask_stats(img, classes, box=None):
    """Bounding box, count and centroid of the pixels of the given colour classes (optionally inside box)."""
    tests = [CLASSES[c] for c in classes]
    n = sx = sy = 0
    x0 = y0 = 10 ** 9
    x1 = y1 = -1
    bx0, by0, bx1, by1 = box if box else (0, 0, img[0], img[1])
    for y, row in pixels(img):
        if y < by0 or y >= by1:
            continue
        for x, (r, g, b) in enumerate(row):
            if x < bx0 or x >= bx1:
                continue
            if any(t(r, g, b) for t in tests):
                n += 1
                sx += x
                sy += y
                x0, y0, x1, y1 = min(x0, x), min(y0, y), max(x1, x), max(y1, y)
    if not n:
        return {"count": 0}
    return {"count": n, "box": [x0, y0, x1 + 1, y1 + 1], "cx": sx / n + 0.5, "cy": sy / n + 0.5,
            "fill": max((x1 + 1 - x0) / img[0], (y1 + 1 - y0) / img[1])}


def mean_diff(a, b):
    """Mean absolute difference over RGB, and the fraction of pixels that differ by more than 16 in a channel."""
    if a[:2] != b[:2]:
        return None, None
    total = big = 0
    ca, cb = a[2], b[2]
    for ra, rb in zip(a[3], b[3]):
        for ch in range(3):
            xa, xb = ra[ch::ca], rb[ch::cb]
            d = [abs(p - q) for p, q in zip(xa, xb)]
            total += sum(d)
        for p, q, r_, s, t, u in zip(ra[0::ca], rb[0::cb], ra[1::ca], rb[1::cb], ra[2::ca], rb[2::cb]):
            if abs(p - q) > 16 or abs(r_ - s) > 16 or abs(t - u) > 16:
                big += 1
    n = a[0] * a[1]
    return total / (3.0 * n), big / float(n)


def background_count(img, tol=6, outside=None):
    """Pixels that are not the background (outside the box `outside`, when given).

    The background is read from the picture itself: the median of its four corner pixels."""
    w, h, c, rows = img
    corners = sorted(tuple(rows[y][x * c:x * c + 3]) for y in (0, h - 1) for x in (0, w - 1))
    bg = corners[1]
    n = 0
    for y, row in pixels(img):
        for x, (r, g, b) in enumerate(row):
            if outside and outside[0] <= x < outside[2] and outside[1] <= y < outside[3]:
                continue
            if abs(r - bg[0]) > tol or abs(g - bg[1]) > tol or abs(b - bg[2]) > tol:
                n += 1
    return n


def projected_box(center, half, tile):
    """The pixel box of a cube's eight corners projected with a tile's matrices: its silhouette's box."""
    xs, ys = [], []
    for dx in (-half, half):
        for dy in (-half, half):
            for dz in (-half, half):
                x, y = project((center[0] + dx, center[1] + dy, center[2] + dz), tile)
                xs.append(x)
                ys.append(y)
    return [min(xs), min(ys), max(xs), max(ys)]


def project(point, tile):
    v = tile["projection"]["view_matrix"]
    p = tile["projection"]["projection_matrix"]
    w, h = tile["size"]
    x, y, z = point
    cam = [sum(v[i][j] * c for j, c in enumerate((x, y, z, 1.0))) for i in range(4)]
    clip = [sum(p[i][j] * cam[j] for j in range(4)) for i in range(4)]
    return (clip[0] / clip[3] * 0.5 + 0.5) * w, (1 - (clip[1] / clip[3] * 0.5 + 0.5)) * h


def mover_errors(result, frames):
    """For each tile of a frames look at Mover: how far (px) its yellow box is from its projected corners."""
    errors, lefts = [], []
    for f, tile in zip(frames, result.get("tiles", [])):
        s = mask_stats(read_png(tile["path"]), ["yellow"])
        if not s.get("count"):
            errors.append(999.0)
            lefts.append(None)
            continue
        want = projected_box((-4 + 8 * (f - 1) / 20.0, -3, 0.25), 0.25, tile)
        errors.append(round(max(abs(a - b) for a, b in zip(s["box"], want)), 2))
        lefts.append(s["box"][0])
    return errors, lefts


def free_mb():
    if sys.platform.startswith("win"):
        class Status(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        status = Status()
        status.dwLength = ctypes.sizeof(Status)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        return status.ullAvailPhys // (1024 * 1024)
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 1 << 20


# ==============================================================================================
# Outside Blender: the runner

class Runner:
    def __init__(self, ns):
        self.ns = ns
        self.scripts = Path(ns.scripts).resolve()
        self.work = Path(ns.work).resolve()
        self.work.mkdir(parents=True, exist_ok=True)
        sys.path.insert(0, str(self.scripts))
        import look
        self.look_mod = look
        how, exe = look.find_blender(ns.blender)
        if exe is None:
            sys.exit("no Blender found (set BLENDER_EXE)")
        self.blender = exe
        self.rows = []
        self.control = str(self.work / "control.blend")
        self.min_mb = ns.min_free_mb

    def row(self, name, ok, **facts):
        self.rows.append({"name": name, "ok": bool(ok), **facts})
        print("%s %s %s" % ("PASS" if ok else "FAIL", name, json.dumps(facts, default=str)), flush=True)

    def wait_memory(self):
        for _ in range(120):
            if free_mb() >= self.min_mb:
                return
            time.sleep(5)
        sys.exit("less than %d MB free for two minutes; not starting Blender" % self.min_mb)

    def blender_script(self, *args, blend=None, timeout=600):
        self.wait_memory()
        cmd = [self.blender, "--background", "--factory-startup"]
        if blend:
            cmd.append(blend)
        cmd += ["--python-exit-code", "1", "--python", str(Path(__file__).resolve()), "--"] + list(args)
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return proc

    def look(self, *args, env=None, timeout=600):
        """Run look.py; returns (exit code, result dict, seconds)."""
        if "--blend" in args or "--new" in args:
            self.wait_memory()
        cmd = [sys.executable, "-X", "utf8", str(self.scripts / "look.py"), "--tmp-dir", str(self.work / "t")] +             [str(a) for a in args]
        t = time.perf_counter()
        env = dict(env or os.environ)
        env["BLENDER_EXE"] = self.blender
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                              env=env)
        seconds = round(time.perf_counter() - t, 3)
        lines = [l for l in proc.stdout.splitlines() if l.strip()]
        try:
            result = json.loads(lines[-1]) if lines else {}
        except json.JSONDecodeError:
            result = {"unparsed": proc.stdout[-500:]}
        if not lines:
            result["stderr"] = proc.stderr[-1500:]
        return proc.returncode, result, seconds

    def out(self, name):
        return str(self.work / "runs" / name)

    # -- groups --------------------------------------------------------------------------

    def build(self):
        if Path(self.control).exists():
            Path(self.control).unlink()
        proc = self.blender_script("build", self.control)
        ok = Path(self.control).exists()
        self.row("build the control scene", ok, path=self.control, log=None if ok else proc.stdout[-800:])
        if not ok:
            sys.exit(1)

    def instruments(self):
        # The PNG reader against a file written here with known bytes, and every filter type decoded right.
        path = self.work / "instrument.png"
        rows = [bytes(v for x in range(40) for v in (x * 3 % 256, y * 5 % 256, (x + y) % 256)) for y in range(30)]
        write_png(path, 40, 30, rows)
        img = read_png(path)
        self.row("I1 PNG reader returns the bytes that were written", img[3] == rows, size=img[:3])
        # a rectangle of known size: fill and centroid
        rect = []
        for y in range(100):
            rect.append(bytes(v for x in range(200) for v in ((200, 20, 20) if 50 <= x < 150 and 25 <= y < 75 else (58, 58, 58))))
        write_png(self.work / "rect.png", 200, 100, rect)
        s = mask_stats(read_png(self.work / "rect.png"), ["red"])
        self.row("I2 colour mask on a known 100x50 rectangle: fill 0.5, centre (100, 50)",
                 s.get("fill") == 0.5 and abs(s["cx"] - 100) < 1e-9 and abs(s["cy"] - 50) < 1e-9 and s["count"] == 5000,
                 measured=s)
        known = [((168, 165, 79), "yellow"), ((68, 63, 20), "yellow"), ((63, 63, 63), None), ((55, 53, 40), None),
                 ((231, 56, 56), "red"), ((115, 28, 28), "red"), ((56, 218, 56), "green"), ((56, 56, 231), "blue"),
                 ((231, 39, 231), "magenta"), ((243, 124, 0), None)]
        got = [(c, [k for k in ("red", "green", "blue", "yellow", "magenta") if CLASSES[k](*c)]) for c, _w in known]
        right = all((g == [w]) if w else (g == [] or c == (243, 124, 0) and "yellow" not in g) for (c, g), (_c, w)
                    in zip(got, known))
        self.row("I2 colour classes on known shades (lit and turned-away faces, background, anti-aliased rim)", right,
                 classified=[[list(c), g] for c, g in got])
        same = mean_diff(img, img)
        self.row("I3 the difference of an image with itself is 0", same == (0.0, 0.0), measured=same)
        flipped = (img[0], img[1], img[2], [bytes(255 - v for v in r) for r in img[3]])
        d = mean_diff(img, flipped)
        self.row("I3 the difference of an image with its negative is large (planted)", d[0] > 50, measured=d)

    def framing(self):
        rc, res, sec = self.look("--blend", self.control, "--mode", "angles", "--target", "Red",
                                 "--views", "front,right,top,three_quarter", "--out", self.out("frame_red"))
        fills, box_errors = [], []
        for tile in res.get("tiles", []):
            s = mask_stats(read_png(tile["path"]), ["red"])
            fills.append(round(s.get("fill", 0), 3))
            if s.get("box"):
                want = projected_box((0, 0, 1), 1.0, tile)
                box_errors.append(round(max(abs(a - b) for a, b in zip(s["box"], want)), 2))
        self.row("D1 angles Red: each tile filled 35-95% by the red pixels", rc == 0 and len(fills) == 4 and
                 all(0.35 <= f <= 0.95 for f in fills), fills=fills, seconds=sec, exit=rc,
                 fingerprint=res.get("fingerprint", {}).get("equal"),
                 box_vs_corners_px_not_isolated=box_errors)
        rc, iso, _sec = self.look("--blend", self.control, "--mode", "angles", "--target", "Red", "--isolate",
                                  "--views", "front,right,top,three_quarter", "--out", self.out("frame_red_isolated"))
        box_errors = []
        for tile in iso.get("tiles", []):
            s = mask_stats(read_png(tile["path"]), ["red"])
            if s.get("box"):
                box_errors.append(round(max(abs(a - b) for a, b in zip(s["box"], projected_box((0, 0, 1), 1.0, tile))), 2))
        self.row("I4 control for the motion instrument: static Red (isolated) matches its projected corners (<= 2 px)",
                 len(box_errors) == 4 and max(box_errors) <= 2.0, error_px=box_errors)
        rc, res, sec = self.look("--blend", self.control, "--mode", "angles", "--target", "Green",
                                 "--views", "front,left,bottom,1:1:1", "--out", self.out("frame_green"))
        fills = [round(mask_stats(read_png(t["path"]), ["green"]).get("fill", 0), 3) for t in res.get("tiles", [])]
        self.row("D1 angles Green (sphere, incl. bottom and 1:1:1): 35-95%", rc == 0 and len(fills) == 4 and
                 all(0.35 <= f <= 0.95 for f in fills), fills=fills, seconds=sec)
        rc, res, sec = self.look("--blend", self.control, "--mode", "angles", "--target", "Red,Green",
                                 "--views", "front,top", "--ortho", "--out", self.out("frame_union"))
        fills = [round(mask_stats(read_png(t["path"]), ["red", "green"]).get("fill", 0), 3) for t in res.get("tiles", [])]
        self.row("D1 angles Red+Green orthographic: the pair fills 35-95%", rc == 0 and len(fills) == 2 and
                 all(0.35 <= f <= 0.95 for f in fills), fills=fills, seconds=sec)
        rc, res, sec = self.look("--blend", self.control, "--mode", "angles", "--target", "Red", "--views", "front",
                                 "--distance", "60", "--out", self.out("frame_far"))
        fills = [round(mask_stats(read_png(t["path"]), ["red"]).get("fill", 0), 3) for t in res.get("tiles", [])]
        self.row("D1 planted: --distance 60 makes Red small and the 35% check catches it", rc == 0 and fills and
                 not (0.35 <= fills[0] <= 0.95), fills=fills)
        self.state_framing = res

    def views_differ(self):
        rc, res, sec = self.look("--blend", self.control, "--mode", "angles", "--views", "front,right",
                                 "--out", self.out("views1"))
        rc2, res2, sec2 = self.look("--blend", self.control, "--mode", "angles", "--views", "front,right",
                                    "--out", self.out("views2"))
        a = read_png(res["tiles"][0]["path"])
        b = read_png(res["tiles"][1]["path"])
        a2 = read_png(res2["tiles"][0]["path"])
        d_ab = mean_diff(a, b)
        d_aa = mean_diff(a, a2)
        self.row("D2 front and right differ", d_ab[0] > 5 and d_ab[1] > 0.05, mean=round(d_ab[0], 3),
                 changed_fraction=round(d_ab[1], 4))
        self.row("D2 the same view in two runs is identical (measured tolerance)", d_aa[0] <= 0.5, mean=round(d_aa[0], 4),
                 changed_fraction=round(d_aa[1], 5), seconds=[sec, sec2])
        self.row("D2 planted: front against itself fails the 'differ' check", not (d_aa[0] > 5 and d_aa[1] > 0.05),
                 mean=round(d_aa[0], 4))

    def shading(self):
        files = {}
        for shading in ("solid", "wireframe", "xray", "material", "rendered"):
            rc, res, sec = self.look("--blend", self.control, "--mode", "angles", "--target", "Red,Green,Blue",
                                     "--views", "front", "--shading", shading, "--out", self.out("shade_" + shading))
            files[shading] = (rc, res, sec)
        img = {k: read_png(v[1]["tiles"][0]["path"]) for k, v in files.items() if v[1].get("tiles")}
        solid = img["solid"]
        green_box = mask_stats(solid, ["green"])["box"]
        red_box = mask_stats(solid, ["red"])["box"]
        # The control (solid) has some dark pixels too: the sphere's shaded rim. The edges are what wireframe adds.
        dark_solid = mask_stats(solid, ["dark"], green_box).get("count", 0)
        dark_wire = mask_stats(img["wireframe"], ["dark"], green_box).get("count", 0)
        self.row("D3 wireframe draws the sphere's edges (dark pixels in its box: >= 4x solid's and 500 more)",
                 dark_wire >= 4 * dark_solid and dark_wire - dark_solid >= 500, solid=dark_solid, wireframe=dark_wire)
        self.row("D3 planted: solid against itself fails the wireframe check",
                 not (dark_solid >= 4 * dark_solid and dark_solid - dark_solid >= 500), solid=dark_solid)
        blue_solid = mask_stats(solid, ["bluish"], red_box).get("count", 0)
        blue_xray = mask_stats(img["xray"], ["bluish"], red_box).get("count", 0)
        self.row("D3 xray shows Blue through Red (bluish pixels inside Red's box)", blue_xray > 200 and blue_solid < 20,
                 solid=blue_solid, xray=blue_xray)
        self.row("D3 planted: solid fails the xray check", not (blue_solid > 200), solid=blue_solid)
        pairs = {}
        for a in img:
            for b in img:
                if a < b:
                    pairs["%s/%s" % (a, b)] = round(mean_diff(img[a], img[b])[0], 2)
        self.row("D3 every pair of shadings differs (mean |diff| > 1)", all(v > 1 for v in pairs.values()), pairs=pairs,
                 seconds={k: v[2] for k, v in files.items()},
                 fingerprints={k: v[1].get("fingerprint", {}).get("equal") for k, v in files.items()})
        # bones: xray of the rig shows bones, solid does not
        rig, tiles = {}, {}
        for shading in ("solid", "xray"):
            rc, res, sec = self.look("--blend", self.control, "--mode", "angles", "--target", "Rig", "--views", "front",
                                     "--shading", shading, "--isolate", "--out", self.out("rig_" + shading))
            tiles[shading] = read_png(res["tiles"][0]["path"])
        x0, y0, x1, y1 = mask_stats(tiles["solid"], ["magenta"])["box"]
        keep_out = (x0 - 3, y0 - 3, x1 + 3, y1 + 3)          # the tracker, drawn in both
        for shading in ("solid", "xray"):
            rig[shading] = background_count(tiles[shading], outside=keep_out)
        self.row("D3 xray draws the bones (non-background pixels away from the tracker)",
                 rig["xray"] > 300 and rig["solid"] < 30, counts=rig)

    def motion(self):
        frames = [1, 11, 21]
        rc, res, sec = self.look("--blend", self.control, "--mode", "frames", "--frames", "1,11,21", "--view", "front",
                                 "--target", "Mover", "--out", self.out("frames_mover"))
        errors, xs = mover_errors(res, frames)
        self.row("D4 frames: Mover's silhouette box is where its keys put it (projected corners, <= 2 px)",
                 rc == 0 and len(errors) == 3 and max(errors) <= 2.0 and xs[0] < xs[1] < xs[2],
                 measured_left_x=xs, error_px=errors, seconds=sec)
        wrong, _xs = mover_errors(res, list(reversed(frames)))
        self.row("D4 planted: the frames in reverse order fail the 2 px check", max(wrong) > 2.0, error_px=wrong)
        spreads = {}
        for name, extra in (("centred", ["--center-bones", "Root"]), ("free", [])):
            rc, res, sec = self.look("--blend", self.control, "--mode", "frames", "--frames", "1,11,21", "--view",
                                     "front", "--target", "Rig", "--isolate", "--out", self.out("frames_rig_" + name),
                                     *extra)
            cxs = [mask_stats(read_png(t["path"]), ["magenta"]).get("cx", -999) for t in res.get("tiles", [])]
            spreads[name] = round(max(cxs) - min(cxs), 2) if cxs else None
        self.row("D4 --center-bones Root keeps the rig in place (tracker x spread <= 2 px)",
                 spreads["centred"] is not None and spreads["centred"] <= 2.0, spreads=spreads)
        self.row("D4 planted: without --center-bones the same check fails", spreads["free"] is not None and
                 spreads["free"] > 30, spreads=spreads)

    def refusals(self):
        rc, res, _ = self.look("--blend", self.control, "--mode", "viewport", "--out", self.out("refuse_viewport"))
        self.row("D5 viewport headless is refused with the reason (exit 3)", rc == 3 and "background" in res.get("error", ""),
                 exit=rc, error=res.get("error"))
        rc, res, _ = self.look("--blend", self.control, "--mode", "viewport", "--fallback", "camera",
                               "--out", self.out("fallback_camera"))
        self.row("D5 viewport --fallback camera shows the camera", rc == 0 and res.get("mode") == "camera" and
                 res.get("tiles", [{}])[0].get("size") == [768, 576], exit=rc, size=res.get("tiles", [{}])[0].get("size"))
        rc, res, _ = self.look("--blend", self.control, "--mode", "ui", "--ui", "window", "--out", self.out("refuse_ui"))
        self.row("D5 ui headless is refused (exit 3)", rc == 3, exit=rc, error=res.get("error"))
        rc, res, _ = self.look("--blend", self.control, "--expect-file", "other.blend", "--out", self.out("refuse_file"))
        self.row("D5 --expect-file of another file is refused before anything (exit 3)",
                 rc == 3 and "WRONG BLENDER" in res.get("error", ""), exit=rc, error=res.get("error"))
        rc, res, _ = self.look("--blend", self.control, "--expect-file", "control.blend", "--views", "front",
                               "--out", self.out("expect_ok"))
        self.row("D5 --expect-file of the right file runs", rc == 0, exit=rc)
        rc, res, _ = self.look("--blend", self.control, "--target", "Nope", "--out", self.out("refuse_target"))
        self.row("D5 an unknown target is a bad argument (exit 2)", rc == 2 and "Nope" in res.get("error", ""),
                 exit=rc, error=res.get("error"))
        rc, res, _ = self.look("--blend", self.control, "--save-as", self.control, "--out", self.out("refuse_save"))
        self.row("D5 --save-as the input file is refused", rc == 3, exit=rc, error=res.get("error"))

    def hidden(self):
        rc, res, _ = self.look("--blend", self.control, "--target", "Hidden,Ghost,Red", "--views", "front",
                               "--out", self.out("hidden_plain"))
        warn = " ".join(res.get("warnings", []))
        info = res.get("targets") or {}
        self.row("D6 hidden and excluded targets are said, not drawn", rc == 0 and info.get("hidden") == ["Hidden"] and
                 info.get("excluded") == ["Ghost"] and "Hidden" in warn, targets=info)
        rc, res, _ = self.look("--blend", self.control, "--target", "Hidden", "--unhide", "--views", "front",
                               "--out", self.out("hidden_unhide"))
        s = mask_stats(read_png(res["tiles"][0]["path"]), ["yellow", "red"]) if res.get("tiles") else {}
        orange = 0
        if res.get("tiles"):
            img = read_png(res["tiles"][0]["path"])
            for _y, row in pixels(img):
                orange += sum(1 for r, g, b in row if r > 120 and 40 < g < 0.8 * r and b < 40)
        self.row("D6 --unhide draws the hidden target and hides it again", rc == 0 and orange > 1000 and
                 res.get("fingerprint", {}).get("equal"), orange_pixels=orange, unhidden=(res.get("targets") or {}).get("unhidden"))

    def image_mode(self):
        rows = [bytes(v for x in range(50) for v in ((x * 5) % 256, (y * 7) % 256, 128)) for y in range(40)]
        src = self.work / "pattern.png"
        write_png(src, 50, 40, rows)
        rc, res, _ = self.look("--new", "--mode", "image", "--image", str(src), "--out", self.out("image_file"))
        got = read_png(res["tiles"][0]["path"]) if res.get("tiles") else None
        same = got is not None and got[:2] == (50, 40) and [bytes(r) for r in got[3]] == rows
        self.row("D7 image file: the tile is the file's pixels exactly", rc == 0 and same, exit=rc,
                 size=got[:2] if got else None)
        rows2 = [bytes(v for x in range(50) for v in ((x * 5 + 1) % 256, (y * 7) % 256, 128)) for y in range(40)]
        self.row("D7 planted: a pattern off by one code is told apart", got is not None and [bytes(r) for r in got[3]] != rows2)
        rc, res, _ = self.look("--blend", self.control, "--mode", "image", "--image", "Swatch", "--out", self.out("image_db"))
        img = read_png(res["tiles"][0]["path"]) if res.get("tiles") else None
        left = img[3][16][0:3] if img else None
        right = img[3][16][3 * 48:3 * 48 + 3] if img else None
        self.row("D7 image datablock Swatch: left and right halves are the stored colours",
                 rc == 0 and tuple(left) == SWATCH[0] and tuple(right) == SWATCH[1], left=left and tuple(left),
                 right=right and tuple(right))

    def render_backend(self):
        rc, res, sec = self.look("--blend", self.control, "--mode", "angles", "--target", "Red", "--backend", "render",
                                 "--views", "front,right,top,three_quarter", "--out", self.out("render_red"))
        fills = [round(mask_stats(read_png(t["path"]), ["red"]).get("fill", 0), 3) for t in res.get("tiles", [])]
        self.row("D8 render backend: Red fills 35-95% of each tile", rc == 0 and len(fills) == 4 and
                 all(0.35 <= f <= 0.95 for f in fills), fills=fills, seconds=sec,
                 fingerprint=res.get("fingerprint", {}).get("equal"), warnings=res.get("warnings"))
        draw = self.look("--blend", self.control, "--mode", "angles", "--target", "Red", "--views", "front",
                         "--out", self.out("render_vs_draw"))[1]
        a = mask_stats(read_png(draw["tiles"][0]["path"]), ["red"]).get("box")
        b = mask_stats(read_png(res["tiles"][0]["path"]), ["red"]).get("box") if res.get("tiles") else None
        iou = None
        if a and b:
            ix = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
            union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - ix
            iou = round(ix / union, 4)
        self.row("D8 render and draw backends frame Red the same (box overlap >= 0.95)", iou is not None and iou >= 0.95,
                 iou=iou, draw_box=a, render_box=b)
        imgs = {}
        for shading, target in (("solid", "Red,Green,Blue"), ("wireframe", "Red,Green,Blue"), ("xray", "Red,Green,Blue")):
            rc, r2, sec2 = self.look("--blend", self.control, "--mode", "angles", "--target", target, "--views", "front",
                                     "--backend", "render", "--shading", shading, "--out", self.out("render_" + shading))
            imgs[shading] = (read_png(r2["tiles"][0]["path"]), sec2, r2.get("fingerprint", {}).get("equal"))
        solid = imgs["solid"][0]
        gb, rb = mask_stats(solid, ["green"])["box"], mask_stats(solid, ["red"])["box"]
        d1 = mask_stats(imgs["wireframe"][0], ["dark"], gb).get("count", 0)
        d0 = mask_stats(solid, ["dark"], gb).get("count", 0)
        b1 = mask_stats(imgs["xray"][0], ["bluish"], rb).get("count", 0)
        b0 = mask_stats(solid, ["bluish"], rb).get("count", 0)
        self.row("D8 render backend wireframe and xray emulations show edges and what is behind (against solid)",
                 d1 >= 4 * d0 and d1 - d0 >= 500 and b1 > 200 and b0 < 20, wire_dark=[d0, d1], xray_bluish=[b0, b1],
                 seconds={k: v[1] for k, v in imgs.items()}, fingerprints={k: v[2] for k, v in imgs.items()})

    def save_as(self):
        setup = self.work / "setup_add.py"
        setup.write_text("import bpy\nbpy.ops.mesh.primitive_monkey_add(location=(0, 0, 2))\n"
                         "bpy.context.object.name = 'AddedBySetup'\n", encoding="utf-8")
        target = self.work / "saved_by_look.blend"
        if target.exists():
            target.unlink()
        rc, res, sec = self.look("--new", "--setup", str(setup), "--target", "AddedBySetup", "--views", "front",
                                 "--save-as", str(target), "--out", self.out("save_as"))
        self.row("D9 --new --setup builds, looks and --save-as writes a copy", rc == 0 and target.exists() and
                 res.get("targets", {}).get("framed") == ["AddedBySetup"], exit=rc, saved=res.get("saved_as"), seconds=sec)
        deep = self.work / "runs" / ("a_rather_long_folder_name_" * 2) / ("another_long_folder_name_" * 2)
        rc, res, sec = self.look("--blend", self.control, "--mode", "frames", "--frames", "1,21", "--view", "front",
                                 "--backend", "render", "--out", str(deep))
        longest = max([len(t["path"]) for t in res.get("tiles", [])] or [0])
        readable = all(read_png(t["path"])[0] > 0 for t in res.get("tiles", []))
        self.row("D10 an --out path past Windows' 259 characters works (render backend, temp files elsewhere)",
                 rc == 0 and longest >= 260 and readable, longest_path=longest, exit=rc, error=res.get("error"))

    # -- the bridge ------------------------------------------------------------------------------

    def _serve(self, port, poll=False):
        self.wait_memory()
        cmd = [self.blender, "--background", "--factory-startup", self.control, "--python",
               str(Path(__file__).resolve()), "--", "serve", self.ns.bridge_pkg, str(port)] + (["poll"] if poll else [])
        log = open(self.work / ("bridge_serve_%d.log" % port), "w", encoding="utf-8", errors="replace")
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
        for _ in range(120):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.25)
        return proc, log

    @staticmethod
    def _stop(proc, log, raw):
        try:
            raw("import sys\nfor name, mod in list(sys.modules.items()):\n"
                "    if name.endswith('.mcp_to_blender_server'):\n        mod.stop()\nresult = {}")
        except Exception:
            pass
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()

    def bridge(self):
        port = self.ns.bridge_port
        proc, log = self._serve(port)
        try:
            env = dict(os.environ, BLENDER_MCP_HOST="127.0.0.1", BLENDER_MCP_PORT=str(port))

            def raw(code, at=None):
                link = socket.create_connection(("127.0.0.1", at or port), timeout=10)
                link.sendall(json.dumps({"type": "execute", "code": code, "strict_json": True}).encode() + b"\0")
                link.settimeout(120)
                data = b""
                while b"\0" not in data:
                    chunk = link.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                link.close()
                return json.loads(data.split(b"\0")[0])

            census = ("import bpy, os\nscene = bpy.context.scene\nlayer = bpy.context.view_layer\n"
                      "result = {'pid': os.getpid(), 'counts': {p.identifier: len(getattr(bpy.data, p.identifier)) "
                      "for p in bpy.data.bl_rna.properties if p.type == 'COLLECTION' and p.identifier != 'all_ids'}, "
                      "'frame': scene.frame_current, 'selected': sorted(o.name for o in layer.objects if o.select_get()), "
                      "'tip': [round(v, 5) for v in bpy.data.objects['Rig'].pose.bones['Tip'].rotation_quaternion], "
                      "'engine': scene.render.engine, 'format': scene.render.image_settings.file_format}")
            first = raw(census)
            pid = first["result"]["pid"]
            self.row("B0 the bridge (1.0.3, background) answers from the control file", first.get("status") == "ok",
                     pid=pid, blender_pid=proc.pid)
            rc, res, sec = self.look("--mode", "angles", "--target", "Red", "--views", "front,right,top,three_quarter",
                                     "--expect-file", "control.blend", "--expect-pid", pid,
                                     "--out", self.out("bridge_angles"), env=env)
            fills = [round(mask_stats(read_png(t["path"]), ["red"]).get("fill", 0), 3) for t in res.get("tiles", [])]
            after = raw(census)
            self.row("B1 live angles: 35-95% fill, fingerprint equal, census unchanged",
                     rc == 0 and len(fills) == 4 and all(0.35 <= f <= 0.95 for f in fills) and
                     res.get("fingerprint", {}).get("equal") and after["result"] == first["result"],
                     fills=fills, seconds=sec, backend=res.get("backend"), deferred=res.get("deferred"),
                     shading=res.get("shading"))
            raw("import bpy\nbpy.data.objects['Rig'].pose.bones['Tip'].rotation_quaternion = (0.8, 0.0, 0.6, 0.0)\n"
                "result = {}")
            before = raw(census)["result"]
            rc, res, sec = self.look("--mode", "frames", "--frames", "1,11,21", "--view", "front", "--target", "Mover",
                                     "--shading", "solid", "--expect-pid", pid, "--out", self.out("bridge_frames"), env=env)
            errors, _xs = mover_errors(res, [1, 11, 21])
            after = raw(census)["result"]
            self.row("B2 live frames: motion where the keys say (<= 2 px); unkeyed Tip pose and frame survive",
                     rc == 0 and errors and max(errors) <= 2 and after == before, error_px=errors, seconds=sec,
                     tip_before=before["tip"], tip_after=after["tip"])
            raw("import bpy\nbpy.data.objects['Rig'].pose.bones['Tip'].rotation_quaternion = (1, 0, 0, 0)\nresult = {}")
            before = raw(census)["result"]
            rc, res, sec = self.look("--mode", "angles", "--expect-pid", pid + 1, "--out", self.out("bridge_wrong_pid"),
                                     env=env)
            after = raw(census)["result"]
            self.row("B3 a wrong --expect-pid is refused before anything (exit 3, census unchanged)",
                     rc == 3 and "WRONG BLENDER" in res.get("error", "") and after == before, exit=rc,
                     error=res.get("error"))
            rc, res, sec = self.look("--mode", "angles", "--expect-file", "someone_else.blend",
                                     "--out", self.out("bridge_wrong_file"), env=env)
            self.row("B3 a wrong --expect-file is refused (exit 3)", rc == 3, exit=rc, error=res.get("error"))
            rc, res, sec = self.look("--mode", "viewport", "--out", self.out("bridge_viewport"), env=env)
            self.row("B4 live viewport in a background Blender is refused with the reason", rc == 3 and
                     "background" in res.get("error", ""), exit=rc, error=res.get("error"))
            rc, res, sec = self.look("--mode", "viewport", "--fallback", "camera", "--out", self.out("bridge_fallback"),
                                     env=env)
            self.row("B4 live viewport --fallback camera", rc == 0 and res.get("mode") == "camera", exit=rc,
                     warnings=res.get("warnings"))
            rc, res, sec = self.look("--mode", "ui", "--ui", "window", "--out", self.out("bridge_ui"), env=env)
            self.row("B5 live ui in a background Blender is refused", rc == 3, exit=rc, error=res.get("error"))
            rc, res, sec = self.look("--mode", "angles", "--shading", "material", "--target", "Red", "--views", "front",
                                     "--out", self.out("bridge_material"), env=env)
            after2 = raw(census)["result"]
            self.row("B6 live material shading: ok, census unchanged", rc == 0 and after2 == before,
                     seconds=sec, fingerprint=res.get("fingerprint", {}).get("equal"))
        finally:
            self._stop(proc, log, raw)
        # B7: the add-on's non-blocking server, polled the way a windowed Blender's timer polls it, with the
        # job forced into deferred ticks: the real check_is_finished path of the 1.0.3 bridge, end to end.
        port2 = port + 1
        proc, log = self._serve(port2, poll=True)
        try:
            env2 = dict(os.environ, BLENDER_MCP_HOST="127.0.0.1", BLENDER_MCP_PORT=str(port2))
            before = raw(census, port2)["result"]
            rc, res, sec = self.look("--mode", "frames", "--frames", "1,6,11,16,21", "--view", "front", "--target",
                                     "Mover", "--force-deferred", "--tick-budget", "0", "--out",
                                     self.out("bridge_deferred"), env=env2)
            errors, _xs = mover_errors(res, [1, 6, 11, 16, 21])
            after = raw(census, port2)["result"]
            self.row("B7 deferred through the bridge (check_is_finished, one tile a tick): motion right, census unchanged",
                     rc == 0 and res.get("deferred") and errors and max(errors) <= 2 and after == before,
                     error_px=errors, seconds=sec, how=(res.get("fingerprint") or {}).get("how"), error=res.get("error"))
        finally:
            self._stop(proc, log, lambda code: raw(code, port2))

    def harness(self):
        proc = self.blender_script("harness", str(self.scripts), str(self.work), blend=self.control, timeout=900)
        path = self.work / "harness_rows.json"
        if not path.exists():
            self.row("H harness ran", False, log=proc.stdout[-1500:], stderr=proc.stderr[-2500:])
            return
        for row in json.loads(path.read_text(encoding="utf-8")):
            name = row.pop("name")
            ok = row.pop("ok")
            self.row(name, ok, **row)
        path.unlink()


GROUPS = ("instruments", "framing", "views_differ", "shading", "motion", "refusals", "hidden", "image_mode",
          "render_backend", "save_as", "harness", "bridge")

# Mutations: a copy of the tool with one thing broken, the groups to run, the rows that must fail.
MUTATIONS = [
    ("frame not put back", "look_blender.py", "            scene.frame_set(was[0], subframe=was[1])\n", "            pass\n",
     ["harness"], ["H3 frames front", "H4 the unkeyed edit survives"]),
    ("animated values not put back", "look_blender.py", "            restore_animated(snapshot)\n", "            pass\n",
     ["harness"], ["H4 the unkeyed edit survives"]),
    ("temporary camera left behind", "look_blender.py",
     "            changes.call(lambda: (bpy.data.objects.remove(cam), bpy.data.cameras.remove(data)))\n", "",
     ["render_backend"], ["D8 render backend: Red fills"]),
    ("settings never undone", "look_blender.py",
     "        self.undo.append(lambda o=owner, a=attr, v=old: setattr(o, a, v))\n", "",
     ["framing"], ["D1 angles Red"]),
    ("framing too loose", "look_blender.py", 'self.margin = max(0.2, min(float(_num(self.args.get("fill"), 0.85)), 0.98))',
     "self.margin = 0.2", ["framing"], ["D1 angles Red", "D1 angles Green"]),
    ("direction ignored", "look_blender.py", "    r, u, d = _basis(direction)\n    basis", "    r, u, d = _basis((0, -1, 0))\n    basis",
     ["views_differ"], ["D2 front and right differ"]),
    ("xray not switched on", "look_blender.py", '            changes.set(sh, "show_xray", True)\n', "",
     ["shading"], ["D3 xray shows Blue"]),
    ("frame not changed", "look_blender.py", "        scene.frame_set(int(frame))\n", "",
     ["motion"], ["D4 frames: Mover"]),
    ("identity not checked", "look_blender.py", '    pid = args.get("expect_pid")\n', '    return\n    pid = args.get("expect_pid")\n',
     ["refusals"], ["D5 --expect-file of another file"]),
    ("fingerprint blind", "look_blender.py", "    fp = {}\n    fp[\"context.mode\"] = bpy.context.mode\n",
     "    return {}\n    fp = {}\n", ["harness"], ["H2 planted frame is found", "H2 planted selection is found"]),
    ("bones not centred", "look_blender.py", "                center = tuple(sum(spots[1:], spots[0].copy()) / len(spots))\n",
     "                center = (0.0, 0.0, 0.0)\n", ["motion"], ["D4 --center-bones Root keeps"]),
]


def mutate(ns):
    report = []
    base = Path(ns.work).resolve()
    for number, (name, filename, old, new, groups, must_fail) in enumerate(MUTATIONS, 1):
        folder = base / ("m%02d" % number)          # short: Blender's file functions stop at 259 characters
        if folder.exists():
            shutil.rmtree(folder)
        scripts = folder / "s"
        shutil.copytree(Path(ns.scripts).resolve(), scripts, ignore=shutil.ignore_patterns("__pycache__"))
        text = (scripts / filename).read_text(encoding="utf-8")
        if text.count(old) != 1:
            report.append({"mutation": name, "applied": False, "count": text.count(old)})
            print("MUTATION %s: patch did not apply (%d matches)" % (name, text.count(old)), flush=True)
            continue
        (scripts / filename).write_text(text.replace(old, new), encoding="utf-8")
        sub = argparse.Namespace(**vars(ns))
        sub.scripts, sub.work = str(scripts), str(folder / "w")
        runner = Runner(sub)
        runner.build()
        for group in groups:
            try:
                getattr(runner, group)()
            except Exception as error:
                runner.row("%s crashed" % group, False, error="%s: %s" % (type(error).__name__, error))
        failed = [r["name"] for r in runner.rows if not r["ok"]]
        caught = all(any(f.startswith(m) for f in failed) for m in must_fail)
        report.append({"mutation": name, "folder": str(folder), "applied": True, "must_fail": must_fail,
                       "failed": failed, "caught": caught})
        print("MUTATION %s: %s (failed rows: %s)" % (name, "CAUGHT" if caught else "NOT CAUGHT", failed), flush=True)
    out = base / "mutation_report.json"
    out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    print("mutations caught: %d of %d" % (sum(1 for r in report if r.get("caught")), len(report)))
    return all(r.get("caught") for r in report)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--scripts", default=str(HERE.parent / "scripts"), help="where look.py and look_blender.py are")
    p.add_argument("--work", required=True, help="a scratch folder for the control scene and the pictures")
    p.add_argument("--blender", help="the Blender executable (default $BLENDER_EXE, PATH, usual folders)")
    p.add_argument("--only", help="comma separated groups: " + ", ".join(GROUPS))
    p.add_argument("--bridge-port", type=int, default=0, help="run the bridge tests on this free port")
    p.add_argument("--bridge-pkg", help="the unzipped Blender Lab MCP add-on folder (1.0.3+)")
    p.add_argument("--mutate", action="store_true", help="break copies of the tool and check the rows catch it")
    p.add_argument("--min-free-mb", type=int, default=6000, help="start Blender only with this much memory free")
    ns = p.parse_args()
    if ns.mutate:
        sys.exit(0 if mutate(ns) else 1)
    runner = Runner(ns)
    started = time.perf_counter()
    groups = ns.only.split(",") if ns.only else [g for g in GROUPS if g != "bridge" or ns.bridge_port]
    runner.build()
    for group in groups:
        if group == "bridge" and not (ns.bridge_port and ns.bridge_pkg):
            runner.row("bridge tests need --bridge-port and --bridge-pkg", False)
            continue
        try:
            getattr(runner, group)()
        except Exception as error:
            import traceback
            runner.row("%s crashed" % group, False, error=traceback.format_exc()[-1200:])
    passed = sum(1 for r in runner.rows if r["ok"])
    total = len(runner.rows)
    (Path(ns.work) / "test_rows.json").write_text(json.dumps(runner.rows, indent=1, default=str), encoding="utf-8")
    print("%d of %d rows passed in %.1f s" % (passed, total, time.perf_counter() - started))
    sys.exit(0 if passed == total else 1)


def blender_main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    role = argv[0] if argv else ""
    if role == "build":
        build_control(argv[1])
    elif role == "harness":
        harness(argv[1], argv[2])
    elif role == "serve":
        serve(argv[1], argv[2], poll=len(argv) > 3 and argv[3] == "poll")
    else:
        print("test_look.py inside Blender: build OUT | harness SCRIPTS WORK | serve PACKAGE PORT")


if __name__ == "__main__":
    if INSIDE_BLENDER:
        blender_main()
    else:
        main()

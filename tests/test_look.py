# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for look.py, measured from the pictures it writes.

    python test_look.py --work DIR                                   # headless tests (background Blender)
    python test_look.py --work DIR --bridge-port 9881 --bridge-pkg PARENT/mcp103   # and through the bridge
    python test_look.py --work DIR --mutate [--mutations 12,13]     # break a copy of the tool, see the rows fail
    python test_look.py --work DIR --only cli,paths,mend --scripts OTHER/scripts   # some groups, another copy

Groups: instruments, framing, views_differ, shading, motion, refusals, hidden, image_mode, render_backend,
save_as, cli (no Blender: usage errors, broken bridge answers), paths (--save-as / --out / --expect-file
spellings, reused folders), mend (camera against Blender's render, NLA, --isolate with a selection, frames
warnings, the render fallback, instances, rigs, drivers, collections, --setup output, --brief), harness (inside
Blender: the fingerprint, handlers, the sheet, labels, local view, deferred ticks), bridge (the real 1.0.3
bridge on a spare port).

The same file runs inside Blender for the parts that need bpy:
    blender -b --factory-startup --python test_look.py -- build OUT.blend
    blender -b --factory-startup --python test_look.py -- build_extra DIR
    blender -b --factory-startup CONTROL.blend --python test_look.py -- refcam OUT.png
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


def render_reference(out):
    """What the scene camera sees, rendered by Blender itself (Workbench, material colours): the reference a
    camera tile is compared with. The PNG keeps the render's own pixels (non-square ones too)."""
    import bpy
    scene = bpy.context.scene
    r = scene.render
    r.engine = "BLENDER_WORKBENCH"
    scene.display.shading.color_type = "MATERIAL"
    r.use_compositing = False
    r.use_sequencer = False
    r.resolution_percentage = 100
    r.image_settings.file_format = "PNG"
    r.image_settings.color_mode = "RGB"
    r.filepath = out
    # what a viewport shows: an object hidden in the viewport (hide_set) is not rendered either
    layer = bpy.context.view_layer
    for ob in layer.objects:
        ob.hide_render = not ob.visible_get(view_layer=layer)
    bpy.ops.render.render(write_still=True)
    print("TL| reference", out, r.resolution_x, r.resolution_y, r.pixel_aspect_x, r.pixel_aspect_y)


def build_extra(folder):
    """The mender's scenes: campix (pixel aspect 2 and a shifted camera, with Blender's own render of it), vis
    (a hide_viewport target, an object animated only through the NLA, a saved selection), far (two cubes 1000 m
    apart)."""
    import bpy
    from mathutils import Vector

    def fresh():
        bpy.ops.wm.read_homefile(use_factory_startup=True)
        for ob in list(bpy.data.objects):
            bpy.data.objects.remove(ob)
        for coll in (bpy.data.meshes, bpy.data.materials, bpy.data.cameras, bpy.data.lights):
            for item in list(coll):
                coll.remove(item)
        scene = bpy.context.scene
        scene.view_settings.view_transform = "Standard"
        scene.view_settings.look = "None"
        scene.render.resolution_x, scene.render.resolution_y = 800, 600
        scene.frame_start, scene.frame_end, scene.frame_current = 1, 21, 1
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    area.spaces.active.shading.type = "SOLID"
                    area.spaces.active.shading.color_type = "MATERIAL"
        return scene

    def material(name, rgb):
        mat = bpy.data.materials.new(name)
        mat.diffuse_color = (*rgb, 1.0)
        if hasattr(mat, "use_nodes") and not mat.use_nodes:
            mat.use_nodes = True
        bsdf = next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if bsdf is not None:
            bsdf.inputs["Base Color"].default_value = (*rgb, 1.0)
        return mat

    def add(kind, name, colour, location, **kw):
        getattr(bpy.ops.mesh, "primitive_%s_add" % kind)(location=location, **kw)
        ob = bpy.context.object
        ob.name = name
        ob.data.materials.append(material(name, COLORS[colour]))
        return ob

    def camera(location, target, lens):
        data = bpy.data.cameras.new("Cam")
        data.lens = lens
        cam = bpy.data.objects.new("Cam", data)
        bpy.context.scene.collection.objects.link(cam)
        cam.location = location
        cam.rotation_euler = (Vector(target) - Vector(location)).to_track_quat("-Z", "Y").to_euler()
        bpy.context.scene.camera = cam
        return cam

    def sun():
        data = bpy.data.lights.new("Sun", "SUN")
        data.energy = 3
        ob = bpy.data.objects.new("Sun", data)
        bpy.context.scene.collection.objects.link(ob)
        ob.rotation_euler = (0.6, 0.2, 0.4)

    def save(name, selected=()):
        layer = bpy.context.view_layer
        layer.update()
        for ob in layer.objects:
            ob.select_set(ob.name in selected)
        layer.objects.active = bpy.data.objects.get("Red")
        path = os.path.join(folder, name + ".blend")
        bpy.ops.wm.save_as_mainfile(filepath=path)
        print("TL| built", path)
        return path

    # campix: 800 x 600 at pixel aspect 2:1, a camera with shift; the reference is Blender's own render
    scene = fresh()
    add("uv_sphere", "Green", "Green", (0, 0, 0), radius=1.0, segments=32, ring_count=16)
    add("cube", "Red", "Red", (3, 0, 0), size=1.0)
    cam = camera((1, -12, 0), (1, 0, 0), 50)
    cam.data.shift_x, cam.data.shift_y = 0.1, -0.05
    scene.render.pixel_aspect_x = 2.0
    sun()
    save("campix")
    render_reference(os.path.join(folder, "campix_ref.png"))

    # vis: HideViewport (blue, hide_viewport), NlaMover (magenta, animated only through an NLA strip), Green and
    # Spare saved selected
    scene = fresh()
    add("cube", "Red", "Red", (0, 0, 0.5), size=1.0)
    add("uv_sphere", "Green", "Green", (2, 0, 0.5), radius=0.5, segments=24, ring_count=12)
    add("cube", "Spare", "Mover", (0, 3, 0.5), size=1.0)
    hv = add("cube", "HideViewport", "Blue", (-2, 0, 0.5), size=1.0)
    hv.hide_viewport = True
    nla = add("cube", "NlaMover", "Tracker", (-4, -3, 0.25), size=0.5)
    nla.keyframe_insert("location", frame=1)
    nla.location.x = 4
    nla.keyframe_insert("location", frame=21)
    adt = nla.animation_data
    track = adt.nla_tracks.new()
    track.strips.new("move", 1, adt.action)
    adt.action = None
    scene.frame_set(1)
    nla.location.x = -4
    camera((1, -16, 6), (0, 0, 0.5), 30)
    sun()
    save("vis", selected=("Green", "Spare"))

    # far: two 1 m cubes 1000 m apart
    fresh()
    add("cube", "Red", "Red", (0, 0, 0), size=1.0)
    add("cube", "Green", "Green", (1000, 0, 0), size=1.0)
    sun()
    save("far")


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
    # What the datablocks hold (the look breaker's F1: a copy that moved the cursor and changed the camera lens
    # passed as "put back"): each plant changes one value and puts the exact old value back.
    notes = bpy.data.texts.new("Notes")
    notes.write("first line\n")
    same = bpy.data.images.new("Same", 4, 4)
    red = bpy.data.objects["Red"]
    bsdf = next(n for n in red.data.materials[0].node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    cam_data, sun_data, world = bpy.data.objects["Cam"].data, bpy.data.objects["Sun"].data, scene.world
    swatch, prefs = bpy.data.images["Swatch"], bpy.context.preferences

    def setting(owner, attr, value):
        """(plant, undo) that set owner.attr to value and back to exactly what it was."""
        keep = {}

        def plant():
            old = getattr(owner, attr)
            keep["v"] = tuple(old) if hasattr(old, "__len__") and not isinstance(old, str) else old
            setattr(owner, attr, value)
        return plant, lambda: setattr(owner, attr, keep["v"])

    def prop_plant():
        scene["planted"] = 1

    def prop_undo():
        del scene["planted"]

    ui = {}

    def ui_plant():
        ui["v"], ui["d"] = prefs.view.ui_scale, prefs.is_dirty
        prefs.view.ui_scale = ui["v"] + 0.25

    def ui_undo():
        prefs.view.ui_scale = ui["v"]
        prefs.is_dirty = ui["d"]

    moved = {}

    def vertex_plant():          # fetched fresh each time: a kept MeshVertex can point at a buffer since replaced
        moved["co"] = tuple(red.data.vertices[0].co)
        red.data.vertices[0].co = (moved["co"][0] + 0.25, moved["co"][1], moved["co"][2])

    def vertex_undo():
        red.data.vertices[0].co = moved["co"]

    plants += [
        ("3D cursor",) + setting(scene.cursor, "location", (1.0, 0.5, 0.0)),
        ("camera lens",) + setting(cam_data, "lens", cam_data.lens + 5.0),
        ("camera shift",) + setting(cam_data, "shift_x", 0.1),
        ("material node value",) + setting(bsdf.inputs["Roughness"], "default_value", 0.123),
        ("material viewport colour",) + setting(red.data.materials[0], "diffuse_color", (0.1, 0.9, 0.1, 1.0)),
        ("world colour",) + setting(world, "color", (0.9, 0.1, 0.1)),
        ("light energy",) + setting(sun_data, "energy", sun_data.energy + 1.0),
        ("mesh vertex position", vertex_plant, vertex_undo),
        ("image colour space",) + setting(swatch.colorspace_settings, "name", "Non-Color"),
        ("armature display",) + setting(rig.data, "display_type", "STICK"),
        ("object colour",) + setting(red, "color", (0.2, 0.3, 0.4, 1.0)),
        ("auto-key tool setting",) + setting(scene.tool_settings, "use_keyframe_insert_auto",
                                             not scene.tool_settings.use_keyframe_insert_auto),
        ("custom property", prop_plant, prop_undo),
        ("text content", lambda: notes.write("planted\n"), lambda: (notes.clear(), notes.write("first line\n"))),
        ("preferences UI scale", ui_plant, ui_undo),
    ]
    base = lb.fingerprint()
    for name, plant, undo in plants:
        plant()
        found = lb.fingerprint_diff(base, lb.fingerprint())
        undo()
        back = lb.fingerprint_diff(base, lb.fingerprint())
        row("H2 planted %s is found" % name, bool(found) and not back, found=found[:2], after_undo=back[:2])
    other = bpy.data.images.new("Same.swap", 4, 4)           # alive before the old one goes: a new address
    bpy.data.images.remove(same)
    other.name = "Same"
    found = lb.fingerprint_diff(base, lb.fingerprint())
    row("H2 planted a datablock replaced by another of the same name is found", bool(found), found=found[:2])
    bpy.data.images.remove(other)
    bpy.data.texts.remove(notes)

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

    # H3 --isolate while objects are selected: hiding an object deselects it, so the selection must come back too
    for name in ("Green", "Blue"):
        bpy.data.objects[name].select_set(True)
    counts, state = _counts(), _state()
    result = lb.run({"mode": "angles", "targets": ["Hidden", "Red"], "unhide": True, "isolate": True,
                     "views": ["front"], "out": os.path.join(work, "harness", "isolate_selected"), "own_process": True,
                     "tmp_dir": os.path.join(work, "t")})
    counts2, state2 = _counts(), _state()
    drift = sorted(k for k in state if state[k] != state2.get(k))
    row("H3 isolate + unhide with Green and Blue selected: ok, the selection put back",
        result.get("ok") and not drift and counts == counts2 and result.get("fingerprint", {}).get("equal"),
        selected_before=state["selected"], selected_after=state2["selected"], state_changed=drift,
        code=result.get("code"))
    for ob in bpy.context.view_layer.objects:
        ob.select_set(False)

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

    # H4 a channel animated only through the NLA (an action in a strip, no active action): its unkeyed edit too
    mesh = bpy.data.meshes.new("NlaOnly")
    mesh.from_pydata([(x, y, z) for x in (-0.25, 0.25) for y in (-0.25, 0.25) for z in (0, 0.5)], [],
                     [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)])
    nla = bpy.data.objects.new("NlaOnly", mesh)
    scene.collection.objects.link(nla)
    nla.location = (-4, -6, 0)
    nla.keyframe_insert("location", frame=1)
    nla.location.x = 4
    nla.keyframe_insert("location", frame=21)
    adt = nla.animation_data
    action = adt.action
    adt.nla_tracks.new().strips.new("move", 1, action)
    adt.action = None
    scene.frame_set(1)
    nla.location.y = 5.0                     # an edit made after the last frame change, not keyed
    control = None
    scene.frame_set(11)
    scene.frame_set(1)
    control = round(nla.location.y, 5)
    nla.location.y = 5.0
    result = lb.run({"mode": "frames", "frames": [1, 11, 21], "view": "front", "targets": ["NlaOnly"],
                     "out": os.path.join(work, "harness", "nla"), "own_process": True})
    after = round(nla.location.y, 5)
    row("H4 an unkeyed edit on a channel animated only through the NLA survives a frames look (control: a frame "
        "change alone loses it)", control != 5.0 and after == 5.0 and result.get("ok"), control_after_frame_change=control,
        after=after, code=result.get("code"), differences=(result.get("fingerprint") or {}).get("differences", [])[:2])
    bpy.data.objects.remove(nla)
    bpy.data.meshes.remove(mesh)
    bpy.data.actions.remove(action)
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

    more_rows(lb, row, work, frames_args, deferred)

    with open(os.path.join(work, "harness_rows.json"), "w", encoding="utf-8") as handle:
        json.dump(rows, handle)


def more_rows(lb, row, work, frames_args, deferred):
    """The mender's in-process rows: handlers, the sheet against its tiles, label text, local view, a refusal
    after borrowing an area, a target deleted between ticks (last: it removes Mover)."""
    import bpy
    import numpy as np
    scene, layer = bpy.context.scene, bpy.context.view_layer

    # H8 the session's frame-change and depsgraph handlers are paused while a look works
    seen = {"frame": 0, "depsgraph": 0}

    def on_frame(scene_, depsgraph=None):
        seen["frame"] += 1

    def on_depsgraph(scene_, depsgraph=None):
        seen["depsgraph"] += 1
    handlers = bpy.app.handlers
    handlers.frame_change_post.append(on_frame)
    handlers.depsgraph_update_post.append(on_depsgraph)
    lists = (list(handlers.frame_change_post), list(handlers.depsgraph_update_post))
    try:
        args = {"mode": "frames", "frames": [1, 11, 21], "view": "front", "targets": ["Mover"], "isolate": True,
                "out": os.path.join(work, "harness", "handlers"), "own_process": True}
        result = lb.run(args)
        same_lists = lists == (list(handlers.frame_change_post), list(handlers.depsgraph_update_post))
        row("H8 frame-change and depsgraph handlers are paused during a frames look and put back in place",
            result.get("ok") and seen == {"frame": 0, "depsgraph": 0} and same_lists and
            "frame_change_post" in (result.get("handlers_paused") or {}), calls=dict(seen), lists_kept=same_lists,
            paused=result.get("handlers_paused"))
        seen.update(frame=0, depsgraph=0)
        result = lb.run(dict(args, run_handlers=True, out=os.path.join(work, "harness", "handlers_run")))
        said = any("ran" in w and "frame-change" in w for w in result.get("warnings", []))
        row("H8 with run_handlers they run, and the result says their side effects stay",
            result.get("ok") and seen["frame"] > 0 and said, calls=dict(seen), warnings=result.get("warnings", [])[:3])
    finally:
        handlers.frame_change_post.remove(on_frame)
        handlers.depsgraph_update_post.remove(on_depsgraph)

    # H9 the contact sheet: written, every cell is its tile scaled down, every cell's label is that tile's label
    result = lb.run({"mode": "angles", "targets": ["Red"], "views": ["front", "right", "top", "three_quarter"],
                     "out": os.path.join(work, "harness", "sheet"), "own_process": True})
    sheet = result.get("sheet") or {}
    exists = bool(sheet.get("path")) and os.path.isfile(sheet["path"])
    cells_ok, labels_ok, facts = [], [], []
    if exists:
        w, h, c, data = read_png(sheet["path"])
        img = np.frombuffer(b"".join(data), np.uint8).reshape(h, w, c)[..., :3]
        def crop(mask):
            ys, xs = np.nonzero(mask)
            return mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1] if len(ys) else mask[:0, :0]
        for cell, tile in zip(sheet.get("cells", []), result.get("tiles", [])):
            tw, th, tc, tdata = read_png(tile["path"])
            pic = np.frombuffer(b"".join(tdata), np.uint8).reshape(th, tw, tc)[..., :3]
            small = lb.resize_box(np.ascontiguousarray(pic), cell["w"], cell["h"]) if (tw, th) != (cell["w"], cell["h"]) else pic
            got = img[cell["y"]:cell["y"] + cell["h"], cell["x"]:cell["x"] + cell["w"]]
            cells_ok.append(bool(np.array_equal(got, small)))
            # the label's first part is drawn in yellow in the bar right above the cell: compare the glyphs alone
            scale = 2 if cell["w"] >= 300 else 1
            bar = 7 * scale + 8
            band = img[max(0, cell["y"] - bar - 2):cell["y"] + 1, max(0, cell["x"] - 2):cell["x"] + cell["w"] + 2]
            want = np.zeros((bar, cell["w"] + 8, 3), np.uint8)
            lb.draw_text(want, 4, 4, tile["label"].partition(" | ")[0], (255, 196, 0), scale)
            same = crop(np.all(band == (255, 196, 0), axis=2))
            labels_ok.append(bool(np.array_equal(same, crop(np.all(want == (255, 196, 0), axis=2)))))
            facts.append(tile["label"])
    row("H9 the sheet is written and every cell is its tile, scaled down exactly", exists and len(cells_ok) == 4 and
        all(cells_ok), sheet=sheet.get("path"), cells=cells_ok)
    row("H9 every cell of the sheet carries its own tile's label", exists and len(labels_ok) == 4 and all(labels_ok),
        labels=facts, matching=labels_ok)

    # H10 names the 5 x 7 font has no letters for: accents are dropped instead of drawn as '?'
    a = np.zeros((7, 120, 3), np.uint8)
    b = np.zeros((7, 120, 3), np.uint8)
    lb.draw_text(a, 0, 0, "Cubo_ação", (255, 255, 255))
    lb.draw_text(b, 0, 0, "CUBO_ACAO", (255, 255, 255))
    row("H10 'Cubo_ação' is drawn as CUBO_ACAO on the sheet (not with '?')", bool(np.array_equal(a, b)))

    # H11 a label too long for its cell ends in '..' (3 columns of 300 px: room for 24 characters at scale 2)
    tiles = [np.full((200, 300, 3), 90, np.uint8) for _ in range(3)]
    label = "FRAME 13 | THREE_QUARTER | SOLID (AS SET) AND MORE"
    sheet_px, cells, _grid = lb.compose_sheet(tiles, [label] * 3, None, 4096)
    cell = cells[0]
    bar = 7 * 2 + 8
    band = sheet_px[cell["y"] - bar:cell["y"], cell["x"]:cell["x"] + 300]
    room = (300 - 8) // 12
    want = np.empty((bar, 300, 3), np.uint8)
    want[:] = (38, 38, 38)
    lb.draw_text(want, 4, 4, label[:room - 2] + "..", (255, 255, 255), 2)
    text_got = np.any(band != (38, 38, 38), axis=2)
    text_want = np.any(want != (38, 38, 38), axis=2)
    row("H11 a label too long for its cell is cut with '..' at the end", bool(np.array_equal(text_got, text_want)),
        room=room, shown=label[:room - 2] + "..")

    # H15 the user's 3D view in local view (only Red in it): a look at Green still draws Green, local view kept
    win = bpy.context.window_manager.windows[0]
    area = max((a_ for a_ in win.screen.areas if a_.type == "VIEW_3D"), key=lambda a_: a_.width * a_.height)
    region = next(r for r in area.regions if r.type == "WINDOW")
    space = area.spaces.active
    for ob in layer.objects:
        ob.select_set(ob.name == "Red")
    with bpy.context.temp_override(window=win, area=area, region=region):
        bpy.ops.view3d.localview(frame_selected=False)
    for ob in layer.objects:
        ob.select_set(False)
    layer.update()
    inside = sorted(o.name for o in layer.objects if o.local_view_get(space))
    result = lb.run({"mode": "angles", "targets": ["Green"], "views": ["front"],
                     "out": os.path.join(work, "harness", "localview"), "own_process": True})
    tile = (result.get("tiles") or [{}])[0]
    green = mask_stats(read_png(tile["path"]), ["green"]).get("count", 0) if tile.get("path") else 0
    layer.update()
    inside_after = sorted(o.name for o in layer.objects if o.local_view_get(space))
    row("H15 local view (Red only): a look at Green draws Green, and the local view keeps only Red",
        space.local_view is not None and inside == ["Red"] and result.get("ok") and green > 1000 and
        inside_after == inside, green_pixels=green, local_view=[inside, inside_after], code=result.get("code"))
    with bpy.context.temp_override(window=win, area=area, region=region):
        bpy.ops.view3d.localview(frame_selected=False)

    # H12 a file whose screen has no 3D view: the borrowed area goes back even when the job stops after the borrow
    keep_end = scene.frame_end
    scene.frame_end = 100
    area.ui_type = "TEXT_EDITOR"
    types = [a_.ui_type for a_ in win.screen.areas]
    try:
        r24 = lb.run({"mode": "frames", "frame_count": 24, "view": "front", "targets": ["Mover"],
                      "out": os.path.join(work, "harness", "borrow24"), "own_process": True})
        after24 = [a_.ui_type for a_ in win.screen.areas]
        bad = lb.run({"mode": "frames", "frame_count": 3, "view": "front", "targets": ["Rig"],
                      "center_bones": ["Nope"], "out": os.path.join(work, "harness", "borrow_bad"), "own_process": True})
        after_bad = [a_.ui_type for a_ in win.screen.areas]
    finally:
        area.ui_type = "VIEW_3D"
        scene.frame_end = keep_end
    row("H12 no 3D view on the screen: --frame-count 24 works (24 tiles) and the borrowed area goes back",
        r24.get("ok") and len(r24.get("tiles", [])) == 24 and after24 == types, tiles=len(r24.get("tiles", [])),
        code=r24.get("code"), error=r24.get("error"), areas=[types, after24])
    row("H12 a refusal after the borrow (an unknown bone) puts the area back too",
        bad.get("code") == "bad_args" and after_bad == types, error=bad.get("error"), areas=after_bad)

    # H13 (last: it deletes Mover) a target deleted between two ticks of a deferred job: said plainly, not blamed
    mover = bpy.data.objects["Mover"]
    result, steps = deferred(dict(frames_args, out=os.path.join(work, "harness", "deferred_deleted")),
                             between=lambda: bpy.data.objects.remove(mover))
    error = result.get("error") or ""
    meanwhile = (result.get("fingerprint") or {}).get("changed_meanwhile", [])
    row("H13 a target deleted between ticks: the job fails saying so, and does not blame itself",
        result.get("ok") is False and "removed" in error and "NOT PUT BACK" not in error and
        any("Mover" in m for m in meanwhile), error=error[:300], meanwhile=meanwhile[:2], ticks=steps)


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


def norm_box(img, classes):
    """The pixel box of a colour class divided by the picture's size (comparable between pictures of other sizes and
    pixel aspects), or None."""
    s = mask_stats(img, classes)
    if not s.get("count"):
        return None
    x0, y0, x1, y1 = s["box"]
    return [x0 / img[0], y0 / img[1], x1 / img[0], y1 / img[1]]


def _box_error(a, b):
    if a is None or b is None:
        return None
    return round(max(abs(p - q) for p, q in zip(a, b)), 4)


def _iou(a, b):
    if not a or not b:
        return None
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - ix
    return round(ix / union, 4) if union else None


def _border_count(img, width=4, tol=6):
    """Pixels in the picture's outer frame that are not the background (read from the four corners, as
    background_count does). Geometry may be darker or lighter than the background: any difference counts."""
    w, h, c, rows = img
    corners = sorted(tuple(rows[y][x * c:x * c + 3]) for y in (0, h - 1) for x in (0, w - 1))
    bg = corners[1]
    n = 0
    for y, row in pixels(img):
        for x, (r, g, b) in enumerate(row):
            if (y < width or y >= h - width or x < width or x >= w - width) and (
                    abs(r - bg[0]) > tol or abs(g - bg[1]) > tol or abs(b - bg[2]) > tol):
                n += 1
    return n


def _sha256(path):
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _short_path(path):
    """The Windows 8.3 short form of an existing path, or None."""
    if not sys.platform.startswith("win"):
        return None
    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, 1024)
    return buf.value if n else None


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _fake_bridge(payload):
    """A one-shot server on a free port that reads one request and answers `payload` (bytes), then closes."""
    import threading
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        try:
            srv.settimeout(60)
            conn, _addr = srv.accept()
            conn.settimeout(10)
            data = b""
            while b"\0" not in data:
                chunk = conn.recv(1 << 16)
                if not chunk:
                    break
                data += chunk
            conn.sendall(payload)
            conn.close()
        except OSError:
            pass
        finally:
            srv.close()
    threading.Thread(target=serve, daemon=True).start()
    return port


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

    def look(self, *args, env=None, timeout=600, cwd=None, tmp_dir=True):
        """Run look.py; returns (exit code, result dict, seconds). self.last holds stdout lines and stderr."""
        if "--blend" in args or "--new" in args:
            self.wait_memory()
        cmd = [sys.executable, "-X", "utf8", str(self.scripts / "look.py")]
        if tmp_dir:
            cmd += ["--tmp-dir", str(self.work / "t")]
        cmd += [str(a) for a in args]
        t = time.perf_counter()
        env = dict(env or os.environ)
        env["BLENDER_EXE"] = self.blender
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=timeout, env=env, cwd=cwd)
        except subprocess.TimeoutExpired as error:
            self.last = {"lines": [], "stderr": "TIMEOUT after %g s" % timeout, "stdout": ""}
            return None, {"timeout": True, "stdout": str(error.stdout)[-300:]}, round(time.perf_counter() - t, 3)
        seconds = round(time.perf_counter() - t, 3)
        lines = [l for l in proc.stdout.splitlines() if l.strip()]
        self.last = {"lines": lines, "stderr": proc.stderr, "stdout": proc.stdout}
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

    # -- the mender's groups: cli (no Blender), paths, mend ---------------------------------------

    def setup_file(self, name, code):
        folder = self.work / "setups"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (name + ".py")
        path.write_text(code, encoding="utf-8")
        return str(path)

    def extras(self):
        folder = self.work / "extra"
        if not (folder / "far.blend").exists():
            folder.mkdir(parents=True, exist_ok=True)
            proc = self.blender_script("build_extra", str(folder))
            if not (folder / "far.blend").exists():
                self.row("build the extra scenes", False, log=proc.stdout[-1200:], stderr=proc.stderr[-800:])
                return None
        if not (self.work / "control_ref.png").exists():
            self.blender_script("refcam", str(self.work / "control_ref.png"), blend=self.control)
        return folder

    def cli(self):
        # C1 usage errors keep the one-JSON-line contract (the look breaker's seven cases)
        cases = [["--mode", "bogus"], ["--fov", "500"], ["--setup", "x.py"], ["--views", "sideways"],
                 ["--frames", "1:5"], ["--max-size", "big"], ["--blend", "a.blend", "--new"]]
        got = []
        for case in cases:
            rc, res, _s = self.look(*case, tmp_dir=False)
            got.append((rc, len(self.last["lines"]), res.get("code")))
        self.row("C1 every usage error prints one JSON line {ok:false, code:bad_args} and exits 2",
                 all(g == (2, 1, "bad_args") for g in got), got=got)
        # C2 a bridge that answers wrongly: one JSON line with the reason, exit 1, never a traceback
        answers = {"truncated": b'{"status": "ok", "res', "not json": b"hello there\0", "a list": b"[1, 2]\0",
                   "ok without a result": b'{"status": "ok"}\0'}
        got = {}
        for name, payload in answers.items():
            port = _fake_bridge(payload)
            env = dict(os.environ, BLENDER_MCP_HOST="127.0.0.1", BLENDER_MCP_PORT=str(port))
            rc, res, _s = self.look("--mode", "angles", "--out", self.out("fake"), env=env, timeout=60, tmp_dir=False)
            got[name] = (rc, len(self.last["lines"]), res.get("code"), "Traceback" in self.last["stderr"])
        self.row("C2 a broken bridge answer gives one JSON line {code: failed} and exit 1, no traceback",
                 all(v == (1, 1, "failed", False) for v in got.values()), got=got)
        # C3 a huge frame range is refused at once (it used to be expanded first and hang)
        t = time.perf_counter()
        rc, res, _s = self.look("--frames", "1-300000000", timeout=20, tmp_dir=False)
        self.row("C3 --frames 1-300000000 is refused within seconds (exit 2)",
                 rc == 2 and time.perf_counter() - t < 15, exit=rc, seconds=round(time.perf_counter() - t, 2),
                 error=res.get("error"))
        # C4 numbers out of range are refused, not used as they come or clamped in silence
        got = {}
        for name, case in (("--distance -10", ["--distance", "-10"]), ("--distance with --ortho",
                                                                       ["--distance", "5", "--ortho"]),
                           ("--max-size 100000", ["--max-size", "100000"]), ("--fill 2", ["--fill", "2"]),
                           ("--frame-count 30", ["--frame-count", "30"])):
            rc, res, _s = self.look("--mode", "angles", *case, tmp_dir=False, timeout=60,
                                    env=dict(os.environ, BLENDER_MCP_PORT="1"))
            got[name] = (rc, res.get("code"))
        self.row("C4 out-of-range numbers are refused before anything runs (exit 2)",
                 all(v == (2, "bad_args") for v in got.values()), got=got)
        # C5 a refusal says so on stderr too, with the next step
        rc, res, _s = self.look("--new", "--mode", "ui", "--ui", "sidebar:Item", "--fallback", "camera", tmp_dir=False)
        err = self.last["stderr"]
        self.row("C5 ui headless: refused (exit 3) with 'FAILED (refused)' on stderr, the next step and the --fallback note",
                 rc == 3 and "FAILED (refused)" in err and "window" in res.get("error", "") and
                 "--fallback applies" in res.get("error", ""), exit=rc, stderr=err[-300:])
        # C6 an unreachable bridge with no --out leaves no empty folder in the temp folder
        temp = self.work / "tmpcheck"
        if temp.exists():
            shutil.rmtree(temp)
        temp.mkdir(parents=True)
        port = _free_port()
        env = dict(os.environ, TEMP=str(temp), TMP=str(temp), TMPDIR=str(temp), BLENDER_MCP_HOST="127.0.0.1",
                   BLENDER_MCP_PORT=str(port))
        rc, res, _s = self.look("--mode", "angles", env=env, tmp_dir=False, timeout=60)
        left = sorted(os.listdir(temp))
        self.row("C6 an unreachable bridge (no --out) leaves nothing in the temp folder (exit 4)",
                 rc == 4 and not left, exit=rc, left=left)

    def paths(self):
        # P1-P3 --save-as given as another spelling of the input file: refused, the input untouched
        folder = self.work / "Long Folder For SaveAs"
        folder.mkdir(parents=True, exist_ok=True)
        source = folder / "input scene.blend"
        add = self.setup_file("add_monkey", "import bpy\nbpy.ops.mesh.primitive_monkey_add(location=(0, 0, 3))\n"
                                            "bpy.context.object.name = 'PLANTED_BY_SETUP'\n")
        short = _short_path(source)
        spellings = [("8.3 short name", None), ("letter case", str(source.parent / source.name.upper())),
                     ("'..'", str(source.parent / ".." / source.parent.name / source.name))]
        for label, spelled in spellings:
            shutil.copy(self.control, source)
            if label == "8.3 short name":
                spelled = _short_path(source)
                if spelled is None or os.path.normcase(spelled) == os.path.normcase(str(source)):
                    self.row("P1 --save-as as the input's 8.3 short name: not testable here (no 8.3 names)", True,
                             short=spelled)
                    continue
            before = _sha256(source)
            rc, res, _s = self.look("--blend", str(source), "--setup", add, "--views", "front", "--save-as", spelled,
                                    "--out", self.out("saveas"))
            after = _sha256(source)
            self.row("P1 --save-as as the input file spelled with %s is refused, the input unchanged" % label,
                     rc == 3 and before == after, exit=rc, spelled=spelled, error=res.get("error"),
                     unchanged=before == after)
        # P4 --out as the 8.3 short form of the .blend's folder: refused, nothing written beside the .blend
        shutil.copy(self.control, source)
        short_folder = _short_path(folder)
        if short_folder and os.path.normcase(short_folder) != os.path.normcase(str(folder)):
            before = sorted(os.listdir(folder))
            rc, res, _s = self.look("--blend", str(source), "--views", "front", "--out", short_folder)
            after = sorted(os.listdir(folder))
            self.row("P4 --out as the 8.3 form of the .blend's folder is refused, nothing written beside it",
                     rc == 3 and before == after, exit=rc, new=sorted(set(after) - set(before)), error=res.get("error"))
        else:
            self.row("P4 --out as the 8.3 form of the .blend's folder: not testable here (no 8.3 names)", True)
        # P5 --expect-file with a full path names one file: a same-named file in another folder is not it
        other = self.work / "other"
        other.mkdir(exist_ok=True)
        shutil.copy(self.control, other / "control.blend")
        rc, res, _s = self.look("--blend", self.control, "--expect-file", str(other / "control.blend"),
                                "--views", "front", "--out", self.out("expect_other"))
        self.row("P5 --expect-file as the full path of a same-named file elsewhere is refused (exit 3)",
                 rc == 3 and "WRONG BLENDER" in res.get("error", ""), exit=rc, error=res.get("error"))
        rc, res, _s = self.look("--blend", self.control, "--expect-file", str(self.control), "--views", "front",
                                "--out", self.out("expect_full"))
        self.row("P5 control: --expect-file as the right full path runs", rc == 0, exit=rc, error=res.get("error"))
        # P6 --expect-file as a bare name runs, and says it matched by name only
        rc, res, _s = self.look("--blend", self.control, "--expect-file", "control.blend", "--views", "front",
                                "--out", self.out("expect_name"))
        said = any("name only" in w for w in res.get("warnings", []))
        self.row("P6 --expect-file as a bare name runs and warns that any folder would match", rc == 0 and said,
                 exit=rc, warnings=res.get("warnings"))
        # P7 a reused --out holds only the last run's pictures
        reuse = Path(self.out("reuse"))
        rc1, _r1, _s = self.look("--blend", self.control, "--views", "front,right,top,three_quarter", "--out", str(reuse))
        rc2, r2, _s = self.look("--blend", self.control, "--views", "front", "--out", str(reuse))
        names = sorted(os.listdir(reuse)) if reuse.exists() else []
        listed = sorted(os.path.basename(p) for p in r2.get("written", []))
        self.row("P7 a reused --out folder holds only what the last run lists (earlier tiles replaced)",
                 rc1 == 0 and rc2 == 0 and names == listed, files=names, listed=listed)
        # P8 --out X.png with one picture: X.png is the clean picture, the labelled sheet beside it
        single = Path(self.out("single")) / "cam.png"
        rc, res, _s = self.look("--blend", self.control, "--mode", "camera", "--out", str(single))
        tile = (res.get("tiles") or [{}])[0]
        size = list(read_png(single)[:2]) if single.exists() else None
        self.row("P8 --mode camera --out cam.png: cam.png is the picture itself (no label bar), cam_sheet.png beside it",
                 rc == 0 and size == tile.get("size") and tile.get("path") and os.path.normcase(tile["path"]) ==
                 os.path.normcase(str(single)) and (single.parent / "cam_sheet.png").exists(), size=size,
                 tile_size=tile.get("size"), tile_path=tile.get("path"))

    def mend(self):
        extra = self.extras()
        if extra is None:
            return
        ref = read_png(extra / "campix_ref.png")
        # M1-M3 camera tiles against Blender's own render through the same camera (normalised boxes)
        for label, blend, reference in (("pixel aspect 2:1 and lens shift", str(extra / "campix.blend"), ref),
                                        ("the control camera", self.control, read_png(self.work / "control_ref.png"))):
            for backend in ("draw", "render"):
                rc, res, _s = self.look("--blend", blend, "--mode", "camera", "--backend", backend,
                                        "--out", self.out("cam_%s_%s" % (backend, "pix" if blend != self.control else "ctl")))
                tile = (res.get("tiles") or [{}])[0]
                error = None
                if tile.get("path"):
                    img = read_png(tile["path"])
                    errs = [_box_error(norm_box(img, [c]), norm_box(reference, [c])) for c in ("red", "green")]
                    error = max(errs) if all(e is not None for e in errs) else None
                self.row("M1 camera tile through %s matches Blender's render (%s backend; normalised boxes within 0.02)"
                         % (label, backend), rc == 0 and error is not None and error <= 0.02, exit=rc,
                         box_error=error, size=tile.get("size"), error=res.get("error"))
        vis = str(extra / "vis.blend")
        # M5 --isolate with Green and Spare selected (saved so): ok, the session as it was
        rc, res, _s = self.look("--blend", vis, "--mode", "angles", "--views", "front", "--target", "Red", "--isolate",
                                "--out", self.out("isolate_sel"))
        self.row("M5 --isolate with a selection: ok, fingerprint equal (the selection put back)",
                 rc == 0 and res.get("fingerprint", {}).get("equal"), exit=rc, error=res.get("error"))
        # M6 --unhide of a target hidden with hide_viewport: drawn where it is, and not blamed afterwards
        rc, res, _s = self.look("--blend", vis, "--mode", "angles", "--views", "front", "--target", "HideViewport",
                                "--unhide", "--out", self.out("unhide_hv"))
        blue = mask_stats(read_png(res["tiles"][0]["path"]), ["blue"]) if res.get("tiles") else {}
        self.row("M6 --unhide of a hide_viewport target: drawn and framed (35-95%), exit 0",
                 rc == 0 and blue.get("count", 0) > 1000 and 0.35 <= blue.get("fill", 0) <= 0.95, exit=rc,
                 blue_pixels=blue.get("count"), fill=blue.get("fill"), error=res.get("error"))
        # M7 an unkeyed edit on a channel animated only through the NLA survives a frames look
        nla = self.setup_file("nla_edit", "import bpy\nbpy.data.objects['NlaMover'].location.y = 5.0\n")
        rc, res, _s = self.look("--blend", vis, "--setup", nla, "--mode", "frames", "--frames", "1,11,21",
                                "--target", "NlaMover", "--out", self.out("nla"))
        self.row("M7 NLA-only animation: the unkeyed edit survives a frames look (exit 0, fingerprint equal)",
                 rc == 0 and res.get("fingerprint", {}).get("equal"), exit=rc, error=res.get("error"))
        # M8 --frame-count 24 (the most the help allows) works
        r100 = self.setup_file("range100", "import bpy\nbpy.context.scene.frame_end = 100\n")
        rc, res, _s = self.look("--blend", self.control, "--setup", r100, "--mode", "frames", "--frame-count", "24",
                                "--target", "Mover", "--out", self.out("count24"))
        self.row("M8 --frame-count 24 gives 24 tiles", rc == 0 and len(res.get("tiles", [])) == 24, exit=rc,
                 tiles=len(res.get("tiles", [])), error=res.get("error"))
        # M9 the tile of the current frame is the animation at that frame, not an unkeyed edit
        edit = self.setup_file("mover_edit", "import bpy\nbpy.data.objects['Mover'].location.x = 3.0\n")
        rc, res, _s = self.look("--blend", self.control, "--setup", edit, "--mode", "frames", "--frames", "1,11,21",
                                "--view", "front", "--target", "Mover", "--out", self.out("current_frame"))
        errors, _xs = mover_errors(res, [1, 11, 21])
        said = any("unkeyed" in w and "Mover" in w for w in res.get("warnings", []))
        self.row("M9 FRAME 1 (the current frame, with an unkeyed edit) shows the keys (<= 2 px) and says so",
                 rc == 0 and errors and max(errors) <= 2.0 and said, error_px=errors, warned=said)
        # M10 targets too small to see are named
        rc, res, _s = self.look("--blend", str(extra / "far.blend"), "--mode", "angles", "--views", "front,top",
                                "--out", self.out("far"))
        warn = " ".join(res.get("warnings", []))
        self.row("M10 two cubes 1000 m apart: each is named as too small to see", rc == 0 and "Red is too small" in warn
                 and "Green is too small" in warn, warnings=res.get("warnings"))
        # M11 frames over nothing: said
        rc, res, _s = self.look("--blend", self.control, "--mode", "frames", "--frames", "200,300", "--target", "Mover",
                                "--out", self.out("outside"))
        warn = " ".join(res.get("warnings", []))
        self.row("M11 frames 200,300 (scene 1-21, keys 1-21) are said to be outside", rc == 0 and "outside" in warn and
                 "beyond the keys" in warn, warnings=res.get("warnings"))
        preview = self.setup_file("preview", "import bpy\ns = bpy.context.scene\ns.use_preview_range = True\n"
                                             "s.frame_preview_start, s.frame_preview_end = 5, 15\n")
        rc, res, _s = self.look("--blend", self.control, "--setup", preview, "--mode", "frames", "--frame-count", "3",
                                "--target", "Mover", "--out", self.out("preview"))
        frames = [t.get("shows", {}).get("frame") for t in res.get("tiles", [])]
        self.row("M11 --frame-count spreads over the preview range when it is on (5, 10, 15)", rc == 0 and
                 frames == [5, 10, 15], frames=frames, range=res.get("frames"))
        empty = self.setup_file("empty", "import bpy\nfor ob in list(bpy.data.objects):\n    bpy.data.objects.remove(ob)\n")
        rc, res, _s = self.look("--new", "--setup", empty, "--mode", "frames", "--frames", "1,5",
                                "--out", self.out("empty_frames"))
        self.row("M11 frames of an empty scene say there is nothing to see", rc == 0 and
                 any("nothing visible" in w for w in res.get("warnings", [])), warnings=res.get("warnings"))
        # M12 the render fallback shows what the viewport shows (a modifier on for render only stays off)
        sub = self.setup_file("subsurf", (
            "import bpy\nob = bpy.data.objects['Cube']\nob.name = 'Red'\nmat = bpy.data.materials.new('Red')\n"
            "mat.diffuse_color = (0.8, 0.04, 0.04, 1)\nob.data.materials.clear()\nob.data.materials.append(mat)\n"
            "m = ob.modifiers.new('Subdivision', 'SUBSURF')\nm.levels = 0\nm.render_levels = 3\nm.show_viewport = False\n"
            "m.show_render = True\n"))
        boxes = {}
        for backend in ("draw", "render"):
            rc, res, _s = self.look("--new", "--setup", sub, "--mode", "angles", "--views", "front", "--target", "Red",
                                    "--backend", backend, "--out", self.out("subsurf_" + backend))
            boxes[backend] = mask_stats(read_png(res["tiles"][0]["path"]), ["red"]).get("box") if res.get("tiles") else None
        iou = _iou(boxes["draw"], boxes["render"])
        self.row("M12 a Subdivision on for render only: the render fallback draws what the viewport shows (box "
                 "overlap >= 0.95)", iou is not None and iou >= 0.95, iou=iou, boxes=boxes)
        # M13 the render fallback's wireframe on a 2M-edge mesh: quick, and one warning said once
        heavy = self.setup_file("heavy", (
            "import bpy, numpy as np\nn = 1000\nxs = np.linspace(-10, 10, n + 1)\ngx, gy = np.meshgrid(xs, xs)\n"
            "verts = np.c_[gx.ravel(), gy.ravel(), (0.3 * np.sin(gx) * np.cos(gy)).ravel()].astype(np.float32)\n"
            "idx = np.arange((n + 1) * (n + 1)).reshape(n + 1, n + 1)\n"
            "loops = np.c_[idx[:-1, :-1].ravel(), idx[:-1, 1:].ravel(), idx[1:, 1:].ravel(), idx[1:, :-1].ravel()]"
            ".ravel().astype(np.int32)\n"
            "me = bpy.data.meshes.new('Heavy')\nme.vertices.add(len(verts))\nme.vertices.foreach_set('co', verts.ravel())\n"
            "me.loops.add(len(loops))\nme.loops.foreach_set('vertex_index', loops)\nme.polygons.add(n * n)\n"
            "me.polygons.foreach_set('loop_start', np.arange(0, len(loops), 4, dtype=np.int32))\n"
            "me.update(calc_edges=True)\nob = bpy.data.objects.new('Heavy', me)\n"
            "bpy.context.scene.collection.objects.link(ob)\nprint('heavy edges', len(me.edges))\n"))
        rc, res, sec = self.look("--new", "--setup", heavy, "--mode", "angles", "--views", "front,top", "--target",
                                 "Heavy", "--backend", "render", "--shading", "wireframe", "--out", self.out("heavy"))
        texts = res.get("warnings", [])
        repeated = len(texts) != len(set(texts))
        self.row("M13 render-fallback wireframe of a 2M-edge mesh: under 25 s for 2 tiles, no warning said twice",
                 rc == 0 and sec < 25 and not repeated, seconds=sec, warnings=texts, exit=rc)
        # M14 a scatter of 360,000 instances is framed whole (the top view's border is empty)
        scatter = self.setup_file("scatter", (
            "import bpy\nfor ob in list(bpy.data.objects):\n    bpy.data.objects.remove(ob)\n"
            "me = bpy.data.meshes.new('Scatter')\nob = bpy.data.objects.new('Scatter', me)\n"
            "bpy.context.scene.collection.objects.link(ob)\n"
            "ng = bpy.data.node_groups.new('Scatter', 'GeometryNodeTree')\n"
            "ng.interface.new_socket('Geometry', in_out='INPUT', socket_type='NodeSocketGeometry')\n"
            "ng.interface.new_socket('Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry')\n"
            "out = ng.nodes.new('NodeGroupOutput')\ngrid = ng.nodes.new('GeometryNodeMeshGrid')\n"
            "grid.inputs['Size X'].default_value = 60\ngrid.inputs['Size Y'].default_value = 60\n"
            "grid.inputs['Vertices X'].default_value = 600\ngrid.inputs['Vertices Y'].default_value = 600\n"
            "cube = ng.nodes.new('GeometryNodeMeshCube')\ncube.inputs['Size'].default_value = (0.06, 0.06, 0.06)\n"
            "inst = ng.nodes.new('GeometryNodeInstanceOnPoints')\n"
            "ng.links.new(grid.outputs['Mesh'], inst.inputs['Points'])\n"
            "ng.links.new(cube.outputs['Mesh'], inst.inputs['Instance'])\n"
            "ng.links.new(inst.outputs['Instances'], out.inputs[0])\n"
            "ob.modifiers.new('Scatter', 'NODES').node_group = ng\n"))
        rc, res, _s = self.look("--new", "--setup", scatter, "--mode", "angles", "--views", "top", "--target",
                                "Scatter", "--out", self.out("scatter"))
        border = _border_count(read_png(res["tiles"][0]["path"])) if res.get("tiles") else None
        self.row("M14 360,000 instances: the whole scatter is framed (no instance on the tile's 4 px border)",
                 rc == 0 and border == 0, border_pixels=border, warnings=res.get("warnings"))
        # M15 a rig as the target brings the mesh it deforms, --isolate keeps it; its bones need xray
        body = self.setup_file("rig_body", (
            "import bpy\nbpy.ops.mesh.primitive_cylinder_add(radius=0.3, depth=2, location=(-3, 0, 1))\n"
            "ob = bpy.context.object\nob.name = 'Body'\nmat = bpy.data.materials.new('Body')\n"
            "mat.diffuse_color = (0.04, 0.04, 0.8, 1)\nob.data.materials.append(mat)\n"
            "ob.modifiers.new('Armature', 'ARMATURE').object = bpy.data.objects['Rig']\nob.select_set(False)\n"))
        rc, res, _s = self.look("--blend", self.control, "--setup", body, "--mode", "frames", "--frames", "1,11,21",
                                "--view", "front", "--target", "Rig", "--center-bones", "Root", "--isolate",
                                "--out", self.out("rig_body"))
        blue = sum(mask_stats(read_png(t["path"]), ["blue"]).get("count", 0) for t in res.get("tiles", []))
        self.row("M15 --target Rig --isolate keeps the unparented mesh the rig deforms", rc == 0 and blue > 500,
                 blue_pixels=blue, deformed=(res.get("targets") or {}).get("deformed_by_rig"))
        rc, res, _s = self.look("--blend", self.control, "--mode", "angles", "--views", "front", "--target", "Rig",
                                "--out", self.out("rig_solid"))
        self.row("M15 an armature target in solid shading: the result says its bones need --shading xray",
                 rc == 0 and any("xray" in w and "Rig" in w for w in res.get("warnings", [])),
                 warnings=res.get("warnings"))
        # M16 Python drivers that a headless run does not run are said
        driver = self.setup_file("driver", (
            "import bpy\nfc = bpy.data.objects['Red'].driver_add('location', 0)\nfc.driver.type = 'SCRIPTED'\n"
            "fc.driver.expression = 'bpy.context.scene.frame_current * 0.5'\n"))
        rc, res, _s = self.look("--blend", self.control, "--setup", driver, "--mode", "frames", "--frames", "1,20",
                                "--target", "Red", "--out", self.out("driver"))
        self.row("M16 a Python driver in a headless run (auto-run off) is said, with --enable-autoexec as the way",
                 rc == 0 and any("Python driver" in w and "--enable-autoexec" in w for w in res.get("warnings", [])),
                 warnings=res.get("warnings"))
        # M17 --target takes a collection
        pair = self.setup_file("pair", (
            "import bpy\nc = bpy.data.collections.new('Pair')\nbpy.context.scene.collection.children.link(c)\n"
            "c.objects.link(bpy.data.objects['Red'])\nc.objects.link(bpy.data.objects['Green'])\n"))
        rc, res, _s = self.look("--blend", self.control, "--setup", pair, "--mode", "angles", "--views", "front",
                                "--target", "Pair", "--out", self.out("collection"))
        framed = sorted((res.get("targets") or {}).get("framed") or [])
        self.row("M17 --target Pair (a collection) frames its objects", rc == 0 and framed == ["Green", "Red"],
                 framed=framed, exit=rc, error=res.get("error"))
        # M18 the --setup script's output is shown; its exception is the reason when it fails
        hello = self.setup_file("hello", "print('SETUP SAYS HELLO')\n")
        rc, res, _s = self.look("--new", "--setup", hello, "--views", "front", "--out", self.out("setup_hello"))
        shown = "setup: SETUP SAYS HELLO" in self.last["stderr"]
        boom = self.setup_file("boom", "raise RuntimeError('planted boom')\n")
        rc2, res2, _s = self.look("--new", "--setup", boom, "--views", "front", "--out", self.out("setup_boom"))
        self.row("M18 --setup's print output is shown on stderr ('setup: ...')", rc == 0 and shown,
                 stderr=self.last["stderr"][-300:] if not shown else "ok")
        self.row("M18 a failing --setup gives its exception as the reason", rc2 == 1 and "planted boom" in
                 (res2.get("error") or ""), error=res2.get("error"))
        # M19 --brief: a short line; the whole result is in the result file beside the pictures
        rc, res, _s = self.look("--blend", self.control, "--views", "front,right", "--brief", "--out", self.out("brief"))
        line = self.last["lines"][-1] if self.last["lines"] else ""
        full = {}
        if res.get("result_file") and os.path.isfile(res["result_file"]):
            full = json.loads(Path(res["result_file"]).read_text(encoding="utf-8"))
        self.row("M19 --brief prints a short line (no matrices) and the result file has the whole result",
                 rc == 0 and len(line) < 4000 and "projection" not in line and
                 all("projection" in t for t in full.get("tiles", [{}])), length=len(line),
                 result_file=res.get("result_file"))

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
            self.bridge_more(raw, census, env, pid)
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

    def bridge_more(self, raw, census, env, pid):
        """The mender's live rows on the served control scene."""
        # B8 --isolate while the user has objects selected
        raw("import bpy\nfor n in ('Green', 'Blue'):\n    bpy.data.objects[n].select_set(True)\nresult = {}")
        before = raw(census)["result"]
        rc, res, sec = self.look("--mode", "angles", "--views", "front", "--target", "Red", "--isolate",
                                 "--expect-pid", pid, "--out", self.out("bridge_isolate_sel"), env=env)
        after = raw(census)["result"]
        self.row("B8 live --isolate with Green and Blue selected: ok, the selection as it was",
                 rc == 0 and after == before, exit=rc, selected=[before["selected"], after["selected"]],
                 error=res.get("error"))
        raw("import bpy\nfor ob in bpy.context.view_layer.objects:\n    ob.select_set(False)\nresult = {}")
        # B9 the user's 3D view in local view (Red only)
        enter = ("import bpy\nwin = bpy.context.window_manager.windows[0]\n"
                 "area = max((a for a in win.screen.areas if a.type == 'VIEW_3D'), key=lambda a: a.width * a.height)\n"
                 "region = next(r for r in area.regions if r.type == 'WINDOW')\nlayer = win.view_layer\n"
                 "for o in layer.objects:\n    o.select_set(o.name == 'Red')\n"
                 "with bpy.context.temp_override(window=win, area=area, region=region):\n"
                 "    bpy.ops.view3d.localview(frame_selected=False)\n"
                 "for o in layer.objects:\n    o.select_set(False)\nlayer.update()\n"
                 "result = {'inside': sorted(o.name for o in layer.objects if o.local_view_get(area.spaces.active))}")
        inside = raw(enter)["result"]["inside"]
        rc, res, sec = self.look("--mode", "angles", "--views", "front", "--target", "Green", "--expect-pid", pid,
                                 "--out", self.out("bridge_localview"), env=env)
        green = mask_stats(read_png(res["tiles"][0]["path"]), ["green"]).get("count", 0) if res.get("tiles") else 0
        check = ("import bpy\nwin = bpy.context.window_manager.windows[0]\n"
                 "area = max((a for a in win.screen.areas if a.type == 'VIEW_3D'), key=lambda a: a.width * a.height)\n"
                 "win.view_layer.update()\n"
                 "result = {'inside': sorted(o.name for o in win.view_layer.objects if o.local_view_get(area.spaces.active))}")
        inside_after = raw(check)["result"]["inside"]
        self.row("B9 live local view (Red only): a look at Green draws Green; the local view still holds only Red",
                 rc == 0 and inside == ["Red"] and green > 1000 and inside_after == inside, green_pixels=green,
                 local_view=[inside, inside_after], warnings=res.get("warnings"), error=res.get("error"))
        raw(enter.replace("o.select_set(o.name == 'Red')", "o.select_set(False)"))          # leave local view
        # B10 an add-on's frame-change handler (a recorder, a live link) is not run by a frames look
        raw("import bpy\ndef look_test_recorder(scene, depsgraph=None):\n"
            "    bpy.app.driver_namespace['look_test_seen'] = bpy.app.driver_namespace.get('look_test_seen', 0) + 1\n"
            "bpy.app.driver_namespace['look_test_seen'] = 0\n"
            "bpy.app.handlers.frame_change_post.append(look_test_recorder)\nresult = {}")
        rc, res, sec = self.look("--mode", "frames", "--frames", "1,6,11,16,21", "--view", "front", "--target", "Mover",
                                 "--expect-pid", pid, "--out", self.out("bridge_handler"), env=env)
        seen = raw("import bpy\nresult = {'seen': bpy.app.driver_namespace.get('look_test_seen'), 'still': "
                   "[h.__name__ for h in bpy.app.handlers.frame_change_post]}")["result"]
        raw("import bpy\nfor h in list(bpy.app.handlers.frame_change_post):\n"
            "    if h.__name__ == 'look_test_recorder':\n        bpy.app.handlers.frame_change_post.remove(h)\n"
            "bpy.app.driver_namespace.pop('look_test_seen', None)\nresult = {}")
        self.row("B10 live frames look: the session's frame-change handler did not run (paused, then back in place)",
                 rc == 0 and seen.get("seen") == 0 and "look_test_recorder" in seen.get("still", []), seen=seen,
                 paused=res.get("handlers_paused"))
        # B11 the render fallback leaves the user's Render Result as it was: empty stays empty, a picture stays
        probe = ("import bpy, os, hashlib\nrr = bpy.data.images.get('Render Result')\nhas = None\nif rr is not None:\n"
                 "    p = os.path.join(r'%s', 'rr_probe.png')\n    sc = bpy.context.scene\n"
                 "    keep = sc.render.image_settings.file_format\n    sc.render.image_settings.file_format = 'PNG'\n"
                 "    try:\n        rr.save_render(p, scene=sc)\n        has = hashlib.sha256(open(p, 'rb').read()).hexdigest()\n"
                 "        os.remove(p)\n    except RuntimeError:\n        has = 'empty'\n"
                 "    sc.render.image_settings.file_format = keep\n"
                 "result = {'exists': rr is not None, 'picture': has}" % str(self.work / "t"))
        (self.work / "t").mkdir(exist_ok=True)
        rr0 = raw(probe)["result"]
        rc, res, sec = self.look("--mode", "angles", "--views", "front", "--target", "Red", "--backend", "render",
                                 "--expect-pid", pid, "--out", self.out("bridge_render_empty"), env=env)
        rr1 = raw(probe)["result"]
        self.row("B11 live render fallback: a Render Result that was there and empty is still empty",
                 rc == 0 and rr0 == rr1, before=rr0, after=rr1, exit=rc, error=res.get("error"))
        raw("import bpy\nsc = bpy.context.scene\nr = sc.render\nkeep = (r.engine, r.resolution_x, r.resolution_y)\n"
            "r.engine, r.resolution_x, r.resolution_y = 'BLENDER_WORKBENCH', 64, 48\n"
            "bpy.ops.render.render(write_still=False)\nr.engine, r.resolution_x, r.resolution_y = keep\nresult = {}")
        rr2 = raw(probe)["result"]
        rc, res, sec = self.look("--mode", "angles", "--views", "front", "--target", "Red", "--backend", "render",
                                 "--expect-pid", pid, "--out", self.out("bridge_render_user"), env=env)
        rr3 = raw(probe)["result"]
        self.row("B11 live render fallback after the user's own render: runs, and the user's picture is byte-identical",
                 rc == 0 and rr2 == rr3 and rr2.get("picture") not in (None, "empty"), before=rr2, after=rr3,
                 exit=rc, error=res.get("error"))
        # B12 no --mode on a background bridge: the angles shown instead are said
        rc, res, sec = self.look("--expect-pid", pid, "--views", "front", "--out", self.out("bridge_nomode"), env=env)
        self.row("B12 live with no --mode on a background Blender: angles, and a warning says why",
                 rc == 0 and res.get("mode") == "angles" and any("no --mode" in w for w in res.get("warnings", [])),
                 warnings=res.get("warnings"))
        # B13 a relative --image is found where the command runs, not in the running Blender's folder
        here = self.work / "imgdir"
        here.mkdir(exist_ok=True)
        rows = [bytes(v for x in range(20) for v in ((x * 9) % 256, 77, 200)) for _y in range(10)]
        write_png(here / "relative.png", 20, 10, rows)
        rc, res, sec = self.look("--mode", "image", "--image", "relative.png", "--expect-pid", pid,
                                 "--out", self.out("bridge_relative"), env=env, cwd=str(here))
        got = read_png(res["tiles"][0]["path"]) if res.get("tiles") else None
        self.row("B13 live --image relative.png (relative to where the command runs) shows that file exactly",
                 rc == 0 and got is not None and [bytes(r) for r in got[3]] == rows, exit=rc, error=res.get("error"))
        # B14 the client gives up (--timeout) on a long job: the result is still written, and the message says where
        out = Path(self.out("bridge_timeout"))
        rc, res, sec = self.look("--mode", "frames", "--frame-count", "12", "--view", "front", "--shading", "material",
                                 "--max-size", "2048", "--timeout", "0.5", "--expect-pid", pid, "--out", str(out),
                                 env=env)
        raw("result = {}")                      # waits until the bridge is free again: the job has ended
        named = str(out / "look_frames_result.json") in (res.get("error") or "")
        written = (out / "look_frames_result.json").exists()
        later = json.loads((out / "look_frames_result.json").read_text(encoding="utf-8")) if written else {}
        self.row("B14 --timeout on a long job: the message names the result file, which holds the finished result",
                 rc == 1 and named and later.get("ok") is True and len(later.get("tiles", [])) == 12, exit=rc,
                 error=(res.get("error") or "")[:240], written=written)

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
          "render_backend", "save_as", "cli", "paths", "mend", "harness", "bridge")

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
    ("framing too loose", "look_blender.py", 'self.margin = self._clamped("fill", 0.85, 0.2, 0.98, float)',
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
    # The look breaker's gaps (breaks the builder's rows did not catch) and the mender's fixes.
    ("expect-file compared by name even with a folder", "look_blender.py",
     "        if os.sep in want or \"/\" in want:\n            same = bool(have) and same_file(have, want)\n",
     "        if os.sep in want or \"/\" in want:\n            same = bool(have) and os.path.normcase(os.path.basename(have)) "
     "== os.path.normcase(os.path.basename(want))\n", ["paths"], ["P5 --expect-file as the full path"]),
    ("--unhide ignores hide_viewport", "look_blender.py",
     "            if o.hide_viewport:\n                changes.set(o, \"hide_viewport\", False)\n", "", ["mend"],
     ["M6 --unhide of a hide_viewport target"]),
    ("camera mode ignores the lens and shift (a fixed 35 degree view)", "look_blender.py",
     "            proj = cam.calc_matrix_camera(depsgraph, x=w, y=h, scale_x=1.0, scale_y=1.0)\n",
     "            proj = perspective_matrix(math.radians(35.0), w / float(h), 0.1, 1000.0)\n", ["mend"],
     ["M1 camera tile through pixel aspect 2:1 and lens shift matches Blender's render (draw",
      "M1 camera tile through the control camera matches Blender's render (draw"]),
    ("the pixel aspect applied twice (as before the fix)", "look_blender.py",
     "            proj = cam.calc_matrix_camera(depsgraph, x=w, y=h, scale_x=1.0, scale_y=1.0)\n",
     "            proj = cam.calc_matrix_camera(depsgraph, x=w, y=h, scale_x=self.scene.render.pixel_aspect_x, "
     "scale_y=self.scene.render.pixel_aspect_y)\n", ["mend"],
     ["M1 camera tile through pixel aspect 2:1 and lens shift matches Blender's render (draw"]),
    ("the sheet's labels shifted by one tile", "look_blender.py",
     "            sheet, cells, grid = compose_sheet([t[\"pixels\"] for t in tiles], [t[\"label\"] for t in tiles], title,\n",
     "            sheet, cells, grid = compose_sheet([t[\"pixels\"] for t in tiles], [t[\"label\"] for t in tiles][1:] + "
     "[t[\"label\"] for t in tiles][:1], title,\n", ["harness"], ["H9 every cell of the sheet carries"]),
    ("the sheet never written", "look_blender.py", "            write_png(sheet_path, sheet)\n", "            pass\n",
     ["harness"], ["H9 the sheet is written"]),
    ("--isolate forgets the selection", "look_blender.py",
     "                    changes.call(lambda o=o, s=selected: (o.hide_set(False, view_layer=layer),\n"
     "                                                          o.select_set(s, view_layer=layer)))\n",
     "                    changes.call(lambda o=o, s=selected: o.hide_set(False, view_layer=layer))\n",
     ["harness"], ["H3 isolate + unhide with Green and Blue selected"]),
    ("NLA strips not snapshotted", "look_blender.py",
     "    sources += [(strip.action, getattr(strip, \"action_slot\", None)) for strip in _nla_strips(adt)\n"
     "                if getattr(strip, \"action\", None) is not None]\n", "", ["harness"],
     ["H4 an unkeyed edit on a channel animated only through the NLA"]),
    ("--save-as compared as strings (in look.py and in Blender)",
     [("look.py", "        if blend and same_file(ns.save_as, blend):\n",
       "        if blend and os.path.normcase(os.path.abspath(ns.save_as)) == os.path.normcase(blend):\n"),
      ("look_blender.py",
       "        if any(same_file(save_as, p) for p in (bpy.data.filepath, args.get(\"input_blend\")) if p):\n",
       "        if any(os.path.normcase(os.path.abspath(save_as)) == os.path.normcase(os.path.abspath(p))\n"
       "               for p in (bpy.data.filepath, args.get(\"input_blend\")) if p):\n")],
     None, None, ["paths"], ["P1 --save-as as the input file spelled with 8.3"]),
    ("handlers not paused", "look_blender.py", "        saved = list(handlers)\n        handlers.clear()\n",
     "        saved = list(handlers)\n", ["harness"], ["H8 frame-change and depsgraph handlers are paused"]),
    ("local view not handled", "look_blender.py", "            if generated:\n                self._local_view(changes)\n",
     "", ["harness"], ["H15 local view"]),
    ("the current frame drawn from the unkeyed state (as before the fix)", "look_blender.py",
     "        if frame is None:\n            return\n        scene, layer = self.scene, self.layer\n",
     "        if frame is None or frame == self.scene.frame_current and self.scene.frame_subframe == 0.0:\n            return\n"
     "        scene, layer = self.scene, self.layer\n", ["mend"], ["M9 FRAME 1"]),
]


def mutate(ns):
    report = []
    base = Path(ns.work).resolve()
    wanted = [w.strip() for w in (ns.mutations or "").split(",") if w.strip()]
    for number, (name, filename, old, new, groups, must_fail) in enumerate(MUTATIONS, 1):
        if wanted and str(number) not in wanted:
            continue
        folder = base / ("m%02d" % number)          # short: Blender's file functions stop at 259 characters
        if folder.exists():
            shutil.rmtree(folder)
        scripts = folder / "s"
        shutil.copytree(Path(ns.scripts).resolve(), scripts, ignore=shutil.ignore_patterns("__pycache__"))
        patches = filename if isinstance(filename, list) else [(filename, old, new)]
        applied = True
        for fname, old_text, new_text in patches:
            # the files may have Windows line ends: patch them as they are
            raw = (scripts / fname).read_bytes().decode("utf-8")
            crlf = "\r\n" in raw
            text = raw.replace("\r\n", "\n")
            if text.count(old_text) != 1:
                report.append({"mutation": name, "applied": False, "file": fname, "count": text.count(old_text)})
                print("MUTATION %s: patch did not apply to %s (%d matches)" % (name, fname, text.count(old_text)),
                      flush=True)
                applied = False
                break
            text = text.replace(old_text, new_text)
            (scripts / fname).write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))
        if not applied:
            continue
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
    p.add_argument("--mutations", help="with --mutate: only these (their numbers in the list, comma separated)")
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
    elif role == "build_extra":
        build_extra(argv[1])
    elif role == "refcam":
        render_reference(argv[1])
    elif role == "harness":
        harness(argv[1], argv[2])
    elif role == "serve":
        serve(argv[1], argv[2], poll=len(argv) > 3 and argv[3] == "poll")
    else:
        print("test_look.py inside Blender: build OUT | build_extra DIR | refcam OUT.png | harness SCRIPTS WORK | "
              "serve PACKAGE PORT")


if __name__ == "__main__":
    if INSIDE_BLENDER:
        blender_main()
    else:
        main()

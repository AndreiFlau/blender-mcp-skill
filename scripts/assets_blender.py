# Portions adapted from MCP for Blender (https://github.com/ahujasid/mcp-for-blender, commit 25e1fe0), Copyright (c) 2025 Siddharth Ahuja, MIT License; see THIRD_PARTY_NOTICES.md.
# SPDX-License-Identifier: GPL-3.0-or-later
"""The half of assets.py that runs INSIDE Blender (plain bpy + numpy).

assets.py downloads and checks every file first, then hands this file a job: through the bridge (LIVE: the
whole text is sent and `bridge_entry(job_json)` is called) or in a background Blender it starts itself
(HEADLESS: `blender -b --factory-startup --disable-autoexec [file] --python assets_blender.py -- --job J
--result R`). It never touches the network.

Every job is wrapped the same way:
  1. identity: --expect-file / --expect-pid are checked before anything is read or changed;
  2. control: the scene is fingerprinted twice with nothing in between; if the two differ the instrument
     cannot be trusted (an animation playing, a timer writing) and the job is refused;
  3. the job runs; on any error everything it made is removed again (new datablocks found by pointer,
     material slots and the world put back, the credits text restored);
  4. frame, mode, selection, active object, active collection and the scene settings importers like to
     touch are put back;
  5. a last fingerprint is compared with the first; only the changes the job is meant to make may differ
     (a new collection under the scene, the world for an HDRI, the named objects' materials, the
     ASSET_CREDITS text). Anything else is reported and the job fails loudly.
"""
import hashlib
import json
import math
import os
import re
import sys
import time
import traceback

import bpy
import numpy as np
from mathutils import Matrix, Vector

CREDITS_TEXT = "ASSET_CREDITS"

ID_COLLECTIONS = ("objects", "meshes", "materials", "images", "worlds", "collections", "cameras", "lights",
                  "node_groups", "actions", "texts", "armatures", "curves", "libraries", "textures",
                  "grease_pencils", "lattices", "metaballs", "speakers", "volumes", "pointclouds",
                  "hair_curves", "fonts", "particles", "scenes", "cache_files", "lightprobes")

GEOMETRY_TYPES = {"MESH", "CURVE", "SURFACE", "FONT", "META"}


class Refuse(Exception):
    """A plain reason. Raised before a change, or followed by a rollback."""


# --------------------------------------------------------------------------------------------- fingerprint

def _r(value, digits=6):
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):          # NaN != NaN: as a float it would make every look differ
            return "nan" if value != value else ("inf" if value > 0 else "-inf")
        return round(value, digits) + 0.0
    if isinstance(value, bpy.types.ID):
        return "ID:" + value.name
    try:
        return [_r(v, digits) for v in value]
    except TypeError:
        return repr(type(value).__name__)


def _digest(data):
    return hashlib.sha1(json.dumps(data, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def _key(block):
    """A datablock's key in the fingerprint: its name, plus its library for linked data (a local and a linked
    datablock may share a name)."""
    lib = getattr(block, "library", None)
    return block.name if lib is None else "%s [%s]" % (block.name, lib.filepath)


def _socket_value(sock):
    if not hasattr(sock, "default_value"):
        return None
    try:
        return _r(sock.default_value)
    except Exception:  # noqa: BLE001 - a socket type we do not know how to read
        return "?"


NODE_ATTRS = ("operation", "blend_type", "data_type", "vector_type", "interpolation", "projection",
              "space", "uv_map", "attribute_name", "attribute_type", "mode", "clamp")


def _tree(nt):
    if nt is None:
        return None
    nodes = []
    for node in nt.nodes:
        item = [node.name, node.bl_idname, node.mute, node.label]
        for attr in NODE_ATTRS:
            if hasattr(node, attr):
                item.append([attr, _r(getattr(node, attr))])
        image = getattr(node, "image", None)
        if image is not None:
            item.append("image:" + image.name)
        group = getattr(node, "node_tree", None)
        if group is not None:
            item.append("group:" + group.name)
        item.append([[s.identifier, _socket_value(s)] for s in node.inputs if not s.is_linked])
        nodes.append(item)
    links = sorted([l.from_node.name, l.from_socket.identifier, l.to_node.name, l.to_socket.identifier,
                    l.is_muted] for l in nt.links)
    return _digest([sorted(nodes, key=lambda n: n[0]), links])


def _mesh_print(me):
    n = len(me.vertices)
    co = np.empty(n * 3, np.float32)
    if n:
        me.vertices.foreach_get("co", co)
    keys = None
    if me.shape_keys is not None:
        keys = [[k.name, _r(k.value), k.mute, k.relative_key.name] for k in me.shape_keys.key_blocks]
    return {"counts": [n, len(me.edges), len(me.polygons), len(me.loops)],
            "co": hashlib.sha1(co.tobytes()).hexdigest()[:16],
            "materials": [m.name if m else None for m in me.materials],
            "uv_maps": [uv.name for uv in me.uv_layers], "shape_keys": keys}


def _object_print(ob, layer):
    try:
        in_layer = [ob.hide_get(view_layer=layer), ob.select_get(view_layer=layer)]
    except RuntimeError:  # not in this view layer
        in_layer = None
    data = {"type": ob.type, "data": ob.data.name if ob.data else None,
            "parent": [ob.parent.name if ob.parent else None, ob.parent_type, ob.parent_bone],
            "matrix_world": _r(ob.matrix_world), "location": _r(ob.location),
            "rotation": [ob.rotation_mode, _r(ob.rotation_euler), _r(ob.rotation_quaternion)],
            "scale": _r(ob.scale), "hide": [ob.hide_viewport, ob.hide_render, ob.hide_select],
            "hidden_selected": in_layer,
            "slots": [[s.link, s.material.name if s.material else None] for s in ob.material_slots],
            "active_material_index": ob.active_material_index,
            "modifiers": [[m.name, m.type, m.show_viewport, m.show_render] for m in ob.modifiers],
            "constraints": [[c.name, c.type, c.mute] for c in ob.constraints],
            "collections": sorted(c.name for c in ob.users_collection),
            "action": (ob.animation_data.action.name if ob.animation_data and ob.animation_data.action else None),
            "custom_properties": sorted(ob.keys()), "mode": ob.mode,
            "instance": [ob.instance_type, ob.instance_collection.name if ob.instance_collection else None]}
    if ob.type == "ARMATURE" and ob.pose is not None:
        data["pose"] = _digest([[b.name, _r(b.location), _r(b.rotation_quaternion), _r(b.rotation_euler),
                                 _r(b.scale)] for b in ob.pose.bones])
    return data


def _layer_tree(lc, out, parent=""):
    out[parent + "/" + lc.name] = {"exclude": lc.exclude, "hide": lc.hide_viewport, "holdout": lc.holdout,
                                   "indirect": lc.indirect_only}
    for child in lc.children:
        _layer_tree(child, out, parent + "/" + lc.name)
    return out


def _views():
    views = []
    wm = bpy.context.window_manager
    for wi, window in enumerate(getattr(wm, "windows", [])):
        for ai, area in enumerate(window.screen.areas):
            if area.type != "VIEW_3D":
                continue
            space = area.spaces.active
            r3d = getattr(space, "region_3d", None)
            if r3d is None:
                continue
            views.append({"where": [wi, ai], "location": _r(r3d.view_location), "rotation": _r(r3d.view_rotation),
                          "distance": _r(r3d.view_distance), "perspective": r3d.view_perspective,
                          "shading": space.shading.type, "local": space.local_view is not None})
    return views


def fingerprint():
    """Everything an import could disturb, keyed for a field-by-field comparison."""
    scene = bpy.context.scene
    layer = bpy.context.view_layer
    fp = {"ids": {}, "objects": {}, "meshes": {}, "materials": {}, "worlds": {}, "images": {},
          "collections": {}, "texts": {}, "node_groups": {}, "lights": {}, "cameras": {}, "scene": {}, "views": {},
          "scene_children": {}, "scene_objects": {}, "layers": {}}
    # Keyed sections, so a new collection or layer is a new key (allowed) and a lost one is a missing key.
    for child in scene.collection.children:
        fp["scene_children"][child.name] = {"linked": True}
    for ob in scene.collection.objects:
        fp["scene_objects"][ob.name] = {"linked": True}
    _layer_tree(layer.layer_collection, fp["layers"])
    for attr in ID_COLLECTIONS:
        for block in getattr(bpy.data, attr, ()):
            fp["ids"]["%s:%x" % (attr, block.as_pointer())] = {"name": block.name}
    for ob in bpy.data.objects:
        fp["objects"][_key(ob)] = _object_print(ob, layer)
    for me in bpy.data.meshes:
        fp["meshes"][_key(me)] = _mesh_print(me)
    for mat in bpy.data.materials:
        fp["materials"][_key(mat)] = {"fake_user": mat.use_fake_user, "tree": _tree(mat.node_tree),
                                      "diffuse": _r(mat.diffuse_color)}
    for world in bpy.data.worlds:
        fp["worlds"][_key(world)] = {"fake_user": world.use_fake_user, "tree": _tree(world.node_tree),
                                     "color": _r(world.color)}
    for image in bpy.data.images:
        fp["images"][_key(image)] = {"file": image.filepath, "source": image.source,
                                     "packed": image.packed_file is not None,
                                     "colorspace": image.colorspace_settings.name, "alpha": image.alpha_mode,
                                     "fake_user": image.use_fake_user}
    for coll in bpy.data.collections:
        fp["collections"][_key(coll)] = {"objects": sorted(o.name for o in coll.objects),
                                         "children": sorted(c.name for c in coll.children),
                                         "hide": [coll.hide_viewport, coll.hide_render, coll.hide_select]}
    for text in bpy.data.texts:
        fp["texts"][_key(text)] = {"content": hashlib.sha1(text.as_string().encode("utf-8")).hexdigest()[:16]}
    for group in bpy.data.node_groups:
        fp["node_groups"][_key(group)] = {"tree": _tree(group)}
    for light in bpy.data.lights:
        fp["lights"][_key(light)] = {"v": [light.type, _r(light.energy), _r(light.color)]}
    for cam in bpy.data.cameras:
        fp["cameras"][_key(cam)] = {"v": [cam.type, _r(cam.lens), _r(cam.clip_start), _r(cam.clip_end)]}
    render = scene.render
    view = scene.view_settings
    units = scene.unit_settings
    active = layer.objects.active
    fp["scene"] = {
        "name": {"v": scene.name}, "scenes": {"v": sorted(s.name for s in bpy.data.scenes)},
        "frame": {"v": [scene.frame_current, scene.frame_subframe, scene.frame_start, scene.frame_end,
                        scene.frame_step, render.fps, _r(render.fps_base)]},
        "render": {"v": [render.engine, render.resolution_x, render.resolution_y, render.resolution_percentage,
                         render.filepath, render.use_compositing, render.use_sequencer,
                         render.image_settings.file_format]},
        "color": {"v": [view.view_transform, view.look, _r(view.exposure), _r(view.gamma),
                        scene.display_settings.display_device]},
        "units": {"v": [units.system, _r(units.scale_length), units.length_unit]},
        "camera": {"v": scene.camera.name if scene.camera else None},
        "world": {"v": scene.world.name if scene.world else None},
        "cursor": {"v": [_r(scene.cursor.location), _r(scene.cursor.rotation_euler)]},
        "active_layer_collection": {"v": layer.active_layer_collection.name},
        "active_object": {"v": active.name if active else None},
        "mode": {"v": bpy.context.mode},
        "selected": {"v": sorted(o.name for o in layer.objects if o.select_get(view_layer=layer))},
        "auto_key": {"v": scene.tool_settings.use_keyframe_insert_auto},
    }
    for i, v in enumerate(_views()):
        fp["views"][str(i)] = v
    return fp


def diff_fingerprints(before, after, expected=()):
    """Changes in `after` against `before`, ignoring `expected` ("section:key") and datablocks that are new."""
    expected = set(expected)
    changes = []
    for section, entries in before.items():
        now_section = after.get(section, {})
        for key, was in entries.items():
            if "%s:%s" % (section, key) in expected:
                continue
            now = now_section.get(key)
            if now is None:
                changes.append("%s %r is gone" % (section, key) if section != "ids"
                               else "%s (was %r) is gone" % (key, was["name"]))
                continue
            if now != was:
                fields = sorted(f for f in set(was) | set(now) if was.get(f) != now.get(f))
                changes.append("%s %r changed: %s" % (section, key if section != "ids" else was["name"],
                                                      ", ".join(fields)))
    return changes


def added_keys(before, after):
    """What exists in `after` and not in `before` (new datablocks, links, layers)."""
    found = []
    for section, entries in after.items():
        old = before.get(section, {})
        for key, value in entries.items():
            if key not in old:
                found.append("%s %r is new" % (section, value.get("name", key) if section == "ids" else key))
    return found


# ------------------------------------------------------------------------------------------ state, rollback

class IdSnapshot:
    """Which datablocks existed before the job, by pointer (names can change, pointers do not)."""

    def __init__(self):
        self.pointers = {}
        self.names = {}
        for attr in ID_COLLECTIONS:
            blocks = list(getattr(bpy.data, attr, ()))
            self.pointers[attr] = {b.as_pointer() for b in blocks}
            self.names[attr] = {b.name for b in blocks}

    def new(self, attr):
        return [b for b in getattr(bpy.data, attr, ()) if b.as_pointer() not in self.pointers[attr]]

    def is_new(self, block, attr):
        return block.as_pointer() not in self.pointers[attr]

    def all_new(self):
        found = []
        for attr in ID_COLLECTIONS:
            if attr == "scenes":
                continue
            found += self.new(attr)
        return found

    def remove_new(self):
        new = self.all_new()
        if new:
            bpy.data.batch_remove(new)
        return len(new)

    def collisions(self):
        """New datablocks Blender gave a suffix because the name was taken: the user's keep their names."""
        found = []
        for attr in ID_COLLECTIONS:
            for block in self.new(attr):
                m = re.match(r"^(.*)\.(\d{3,})$", block.name)
                if m and m.group(1) in self.names[attr]:
                    found.append({"kind": attr, "wanted": m.group(1), "got": block.name})
        return found


MODE_FOR_SET = {"EDIT_MESH": "EDIT", "EDIT_CURVE": "EDIT", "EDIT_SURFACE": "EDIT", "EDIT_TEXT": "EDIT",
                "EDIT_ARMATURE": "EDIT", "EDIT_METABALL": "EDIT", "EDIT_LATTICE": "EDIT",
                "EDIT_CURVES": "EDIT", "EDIT_GREASE_PENCIL": "EDIT", "EDIT_POINTCLOUD": "EDIT",
                "POSE": "POSE", "SCULPT": "SCULPT", "PAINT_WEIGHT": "WEIGHT_PAINT",
                "PAINT_VERTEX": "VERTEX_PAINT", "PAINT_TEXTURE": "TEXTURE_PAINT", "PARTICLE": "PARTICLE_EDIT"}


class SceneState:
    """Frame, mode, selection, active object and collection, and the scene settings importers touch."""

    def __init__(self):
        scene = bpy.context.scene
        layer = bpy.context.view_layer
        render = scene.render
        units = scene.unit_settings
        self.scene, self.layer = scene, layer
        self.active = layer.objects.active
        self.selected = {o.as_pointer() for o in layer.objects if o.select_get(view_layer=layer)}
        self.mode = bpy.context.mode
        self.in_mode = [o for o in layer.objects if o.mode != "OBJECT"]      # e.g. every object in multi-edit
        self.active_lc = layer.active_layer_collection.name
        self.settings = {"frame_current": scene.frame_current, "frame_start": scene.frame_start,
                         "frame_end": scene.frame_end, "frame_step": scene.frame_step,
                         "fps": render.fps, "fps_base": render.fps_base, "camera": scene.camera,
                         "world": scene.world, "unit_system": units.system, "unit_scale": units.scale_length,
                         "unit_length": units.length_unit, "cursor": scene.cursor.location.copy()}
        self.changed_mode = False

    def leave_mode(self):
        """Object Mode for an import. Returns the meshes whose edit data Blender wrote back."""
        if self.mode == "OBJECT":
            return []
        synced = []
        if self.mode.startswith("EDIT"):
            synced = [o.data.name for o in bpy.context.view_layer.objects
                      if o.mode == "EDIT" and o.type == "MESH"]
        bpy.ops.object.mode_set(mode="OBJECT")
        self.changed_mode = True
        return synced

    def restore(self, mode=True):
        scene, layer, s = self.scene, self.layer, self.settings
        put_back = []
        if bpy.context.mode != "OBJECT" and self.mode == "OBJECT":
            try:
                bpy.ops.object.mode_set(mode="OBJECT")
            except RuntimeError:
                pass
        for ob in layer.objects:
            want = ob.as_pointer() in self.selected
            try:
                if ob.select_get(view_layer=layer) != want:
                    ob.select_set(want, view_layer=layer)
            except RuntimeError:
                pass
        if layer.objects.active != self.active:
            layer.objects.active = self.active
            put_back.append("active object")

        def find(lc, name):
            if lc.name == name:
                return lc
            for child in lc.children:
                hit = find(child, name)
                if hit is not None:
                    return hit
            return None
        lc = find(layer.layer_collection, self.active_lc)
        if lc is not None and layer.active_layer_collection.name != self.active_lc:
            layer.active_layer_collection = lc
            put_back.append("active collection")
        units = scene.unit_settings
        pairs = [("frame_start", scene, "frame_start"), ("frame_end", scene, "frame_end"),
                 ("frame_step", scene, "frame_step"), ("fps", scene.render, "fps"),
                 ("fps_base", scene.render, "fps_base"), ("camera", scene, "camera"),
                 ("unit_system", units, "system"), ("unit_scale", units, "scale_length"),
                 ("unit_length", units, "length_unit")]
        for key, owner, attr in pairs:
            if getattr(owner, attr) != s[key]:
                setattr(owner, attr, s[key])
                put_back.append(key)
        if scene.cursor.location != s["cursor"]:
            scene.cursor.location = s["cursor"]
            put_back.append("3D cursor")
        if scene.frame_current != s["frame_current"]:
            scene.frame_set(s["frame_current"])
            put_back.append("frame")
        if mode:
            put_back += self.restore_mode()
        return put_back

    def restore_mode(self):
        if not (self.changed_mode and self.mode != "OBJECT" and self.active is not None):
            return []
        layer = self.layer
        keep = {o.as_pointer() for o in self.in_mode}
        for ob in layer.objects:            # mode_set takes the selected objects along: select exactly those it had
            try:
                ob.select_set(ob.as_pointer() in keep, view_layer=layer)
            except RuntimeError:
                pass
        layer.objects.active = self.active
        problem = None
        try:
            bpy.ops.object.mode_set(mode=MODE_FOR_SET.get(self.mode, "OBJECT"))
        except RuntimeError as exc:
            problem = "mode NOT put back (%s)" % exc
        for ob in layer.objects:
            try:
                ob.select_set(ob.as_pointer() in self.selected, view_layer=layer)
            except RuntimeError:
                pass
        return [problem or "mode"]


class Rollback:
    """Things to put back if the job fails half way (new datablocks are removed separately)."""

    def __init__(self):
        self.steps = []

    def add(self, fn):
        self.steps.append(fn)

    def run(self):
        problems = []
        for fn in reversed(self.steps):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 - keep putting the rest back
                problems.append(str(exc))
        return problems


# --------------------------------------------------------------------------------------------- helpers

def _op_kwargs(op, **wanted):
    """Only the keywords this Blender's operator knows (they change between versions)."""
    try:
        known = set(op.get_rna_type().properties.keys())
    except Exception:  # noqa: BLE001
        return wanted
    return {k: v for k, v in wanted.items() if k in known}


def _set_colorspace(image, data):
    """A colour space this OCIO config has: Non-Color (data) or sRGB; returns the name set."""
    names = ("Non-Color", "Generic Data", "Data", "Raw", "Utility - Raw") if data else (
        "sRGB", "sRGB IEC 61966-2-1", "Utility - sRGB - Texture")
    for name in names:
        try:
            image.colorspace_settings.name = name
        except TypeError:
            continue
        if not data or getattr(image.colorspace_settings, "is_data", True):
            return name
    return image.colorspace_settings.name


def _fits(image, data, pack):
    """May an image that is already loaded from this file serve again, untouched (same colour space role,
    packed when packing is asked, not edited in memory)?"""
    if image.source != "FILE" or image.is_dirty or (pack and image.packed_file is None):
        return False
    if data is None:
        return True
    return bool(getattr(image.colorspace_settings, "is_data", False)) == bool(data)


def _load_image(path, data, pack, made, snap=None, reused=None):
    """The image for a cache file. One already loaded from the same file is used again when it fits (no
    '.001' copy per import); the user's own image is never changed to make it fit."""
    image = bpy.data.images.load(path, check_existing=snap is not None)
    if snap is not None and (not snap.is_new(image, "images") or image.name in made):
        if _fits(image, data, pack):
            if reused is not None and image.name not in reused and image.name not in made:
                reused.append(image.name)
            return image, image.colorspace_settings.name
        image = bpy.data.images.load(path, check_existing=False)
    made.append(image.name)
    space = _set_colorspace(image, data) if data is not None else image.colorspace_settings.name
    if pack:
        image.pack()
    return image, space


def _socket(node, *names):
    for name in names:
        sock = node.inputs.get(name)
        if sock is not None:
            return sock
    raise Refuse("this Blender's %s has none of the inputs %s" % (node.bl_idname, ", ".join(names)))


def _tag(block, meta):
    for key in ("source", "id", "name", "author", "license", "page_url"):
        if meta.get(key):
            block["asset_" + key] = str(meta[key])


def credit_line(meta):
    stamp = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())
    fields = [stamp, meta.get("source") or "?", meta.get("id") or "?", meta.get("name") or "",
              meta.get("author") or "unknown author", meta.get("license") or "unknown license",
              meta.get("page_url") or ""]
    return " | ".join(str(f).replace("|", "/").replace("\n", " ") for f in fields)


def _credit_texts():
    """(the local ASSET_CREDITS text or None, the linked ones). A linked text cannot keep a line: a save drops it."""
    local, linked = None, []
    for text in bpy.data.texts:
        if text.name != CREDITS_TEXT:
            continue
        if text.library is None and getattr(text, "override_library", None) is None:
            local = local or text
        else:
            linked.append(text)
    return local, linked


def add_credit(meta, rollback):
    text, linked = _credit_texts()
    note = None
    if text is None:
        text = bpy.data.texts.new(CREDITS_TEXT)       # new: removed with the other new datablocks on failure
        if linked:
            note = ("this file's %s is linked from %s, where a line would be lost when the file is saved; the "
                    "credit went into a local text named %r" % (CREDITS_TEXT, linked[0].library.filepath
                                                                if linked[0].library else "a library", text.name))
    else:
        old = text.as_string()
        rollback.add(lambda: text.from_string(old))
    line = credit_line(meta)
    current = text.as_string()
    if current and not current.endswith("\n"):
        current += "\n"
    text.from_string(current + line + "\n")
    out = {"text": text.name, "line": line}
    if note:
        out["note"] = note
    return out


def _id_collection_names():
    """Every bpy.data collection that holds datablocks (shape keys, line styles and the rest included)."""
    names = []
    for prop in bpy.data.bl_rna.properties:
        if prop.type != "COLLECTION":
            continue
        fixed = getattr(prop, "fixed_type", None)
        cls = getattr(bpy.types, fixed.identifier, None) if fixed is not None else None
        if isinstance(cls, type) and issubclass(cls, bpy.types.ID):
            names.append(prop.identifier)
    return names


def all_id_pointers():
    return {b.as_pointer() for attr in _id_collection_names() for b in getattr(bpy.data, attr, ())}


def ids_new_since(pointers):
    return [b for attr in _id_collection_names() for b in getattr(bpy.data, attr, ()) if b.as_pointer() not in pointers]


def _driver_owners(block):
    """The datablock and what carries drivers along with it: its own node tree and its shape keys."""
    owners = [(block.name, block)]
    tree = getattr(block, "node_tree", None)
    if tree is not None and getattr(tree, "is_embedded_data", False):
        owners.append(("%s (node tree)" % block.name, tree))
    keys = getattr(block, "shape_keys", None)
    if keys is not None:
        owners.append(("%s (shape keys)" % block.name, keys))
    return owners


def _python_drivers(blocks):
    """Drivers that would run Python (not Blender's own simple-expression evaluator)."""
    seen = set()
    for block in blocks:
        for label, owner in _driver_owners(block):
            anim = getattr(owner, "animation_data", None)
            if anim is None:
                continue
            for fc in anim.drivers:
                drv = fc.driver
                if drv.type == "SCRIPTED" and not drv.is_simple_expression and fc.as_pointer() not in seen:
                    seen.add(fc.as_pointer())
                    yield label, fc


def _mute_scripted_drivers(blocks):
    """Mute every Python driver in data we brought in, BEFORE anything is evaluated, then prove none is left:
    in a Blender that runs scripts (Auto Run on) an evaluation would run them."""
    muted = []
    for label, fc in list(_python_drivers(blocks)):
        try:
            fc.mute = True
        except (AttributeError, RuntimeError, TypeError):
            pass                                        # linked data: found below and refused
        muted.append("%s: %s = %s" % (label, fc.data_path, fc.driver.expression[:80]))
    left = ["%s: %s" % (label, fc.data_path) for label, fc in _python_drivers(blocks) if not fc.mute]
    if left:
        raise Refuse("the .blend carries drivers that would run Python and could not be muted (%s); nothing was "
                     "kept" % "; ".join(left[:5]))
    return muted


# --------------------------------------------------------------------------------------------- measuring

def measure(objects):
    """World-space bounds of what the objects draw (evaluated geometry, instances included), or None."""
    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    wanted = {o.as_pointer() for o in objects}
    lo, hi, count = None, None, 0
    for inst in depsgraph.object_instances:
        source = inst.parent.original if inst.is_instance and inst.parent is not None else inst.object.original
        if source.as_pointer() not in wanted:
            continue
        ob = inst.object
        if ob.type not in GEOMETRY_TYPES:
            continue
        try:
            me = ob.to_mesh()
        except RuntimeError:
            continue
        try:
            n = len(me.vertices) if me is not None else 0
            if n:
                co = np.empty(n * 3, np.float32)
                me.vertices.foreach_get("co", co)
                co = co.reshape(-1, 3).astype(np.float64)
                m = np.array(inst.matrix_world, dtype=np.float64)
                world = co @ m[:3, :3].T + m[:3, 3]
                a, b = world.min(axis=0), world.max(axis=0)
                lo = a if lo is None else np.minimum(lo, a)
                hi = b if hi is None else np.maximum(hi, b)
                count += n
        finally:
            ob.to_mesh_clear()
    if lo is None:
        return None
    return {"min": [float(v) for v in lo], "max": [float(v) for v in hi],
            "size": [float(v) for v in (hi - lo)], "vertices": count}


def unit_scale():
    """Metres per Blender unit in this scene (Unit Scale; 0.01 in a centimetre scene). With no unit system the
    numbers are plain Blender units, taken as metres."""
    units = bpy.context.scene.unit_settings
    s = float(units.scale_length) if units.system != "NONE" else 1.0
    return s if math.isfinite(s) and s > 0 else 1.0


def render_uv(me):
    """The UV map renders use (active_render), which is not always the one being edited (active)."""
    return next((layer for layer in me.uv_layers if layer.active_render), None) or me.uv_layers.active


def uv_metres(ob):
    """(Blender units of surface per UV unit on this mesh in world space, the UV map's name), or (None, None)
    without a UV map. Measured on the render UV map, the one the material is wired to."""
    me = ob.data
    layer = render_uv(me) if ob.type == "MESH" and me is not None else None
    if layer is None or not len(me.polygons):
        return None, None
    return _uv_area_ratio(ob, me, layer), layer.name


def _uv_area_ratio(ob, me, layer):
    me.calc_loop_triangles()
    tris = len(me.loop_triangles)
    if not tris:
        return None
    verts = np.empty(tris * 3, np.int32)
    loops = np.empty(tris * 3, np.int32)
    me.loop_triangles.foreach_get("vertices", verts)
    me.loop_triangles.foreach_get("loops", loops)
    co = np.empty(len(me.vertices) * 3, np.float32)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3).astype(np.float64)
    m = np.array(ob.matrix_world, dtype=np.float64)
    co = co @ m[:3, :3].T + m[:3, 3]
    uv = np.empty(len(me.loops) * 2, np.float32)
    layer.data.foreach_get("uv", uv)
    uv = uv.reshape(-1, 2).astype(np.float64)
    p = co[verts].reshape(-1, 3, 3)
    t = uv[loops].reshape(-1, 3, 2)
    area3 = 0.5 * np.linalg.norm(np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]), axis=1).sum()
    e1, e2 = t[:, 1] - t[:, 0], t[:, 2] - t[:, 0]
    area2 = 0.5 * np.abs(e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]).sum()
    if area2 < 1e-12 or area3 < 1e-12:
        return None
    return float(math.sqrt(area3 / area2))


# --------------------------------------------------------------------------------------------- HDRI

def import_hdri(job, rollback, expected, snap):
    meta, opts = job["meta"], job["options"]
    scene = bpy.context.scene
    made = []
    image, _space = _load_image(job["files"]["main"], None, opts.get("pack"), made, snap)
    wanted = opts.get("name") or meta.get("id") or "HDRI"
    world = bpy.data.worlds.new(wanted)
    if not world.use_nodes:          # always on in 5.x, where setting it is deprecated
        world.use_nodes = True
    nt = world.node_tree
    nt.nodes.clear()
    coord = nt.nodes.new("ShaderNodeTexCoord")
    coord.location = (-900, 0)
    mapping = nt.nodes.new("ShaderNodeMapping")
    mapping.location = (-700, 0)
    env = nt.nodes.new("ShaderNodeTexEnvironment")
    env.location = (-450, 0)
    env.image = image
    background = nt.nodes.new("ShaderNodeBackground")
    background.location = (-150, 0)
    output = nt.nodes.new("ShaderNodeOutputWorld")
    output.location = (100, 0)
    nt.links.new(coord.outputs["Generated"], mapping.inputs["Vector"])
    nt.links.new(mapping.outputs["Vector"], env.inputs["Vector"])
    nt.links.new(env.outputs["Color"], background.inputs["Color"])
    nt.links.new(background.outputs["Background"], output.inputs["Surface"])
    rotation = float(opts.get("rotation") or 0.0)
    mapping.inputs["Rotation"].default_value[2] = math.radians(rotation)
    background.inputs["Strength"].default_value = float(opts.get("strength", 1.0))
    _tag(world, meta)
    old = scene.world
    scene.world = world
    rollback.add(lambda: setattr(scene, "world", old))
    expected.add("scene:world")
    kept = None
    if old is not None:
        kept = {"world": old.name, "fake_user_added": False}
        if not opts.get("replace_world") and not old.use_fake_user:
            old.use_fake_user = True
            kept["fake_user_added"] = True
            rollback.add(lambda: setattr(old, "use_fake_user", False))
            expected.add("worlds:" + old.name)
    report = {"world": world.name, "image": image.name, "image_file": image.filepath,
              "strength": background.inputs["Strength"].default_value, "rotation_deg": rotation,
              "previous_world": kept, "replaced_world": bool(opts.get("replace_world")),
              "packed": image.packed_file is not None, "image_reused": image.name not in made}
    if old is not None and opts.get("replace_world"):
        report["note"] = ("the previous world %r is no longer used; Blender drops it when the file is saved "
                          "unless something else uses it" % old.name)
    return report


# --------------------------------------------------------------------------------------------- texture

ROLE_INPUTS = {"base_color": ("Base Color",), "roughness": ("Roughness",), "metallic": ("Metallic",),
               "alpha": ("Alpha",), "emission": ("Emission Color", "Emission"),
               "anisotropic": ("Anisotropic",), "anisotropic_rotation": ("Anisotropic Rotation",)}
COLOR_ROLES = {"base_color", "emission"}


def build_material(name, maps, images, scale, box, disp_scale, meta, made, uv_name=None):
    mat = bpy.data.materials.new(name)
    made.append(mat.name)
    if not mat.use_nodes:
        mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    out.location = (700, 0)
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (350, 0)
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    mapping = nt.nodes.new("ShaderNodeMapping")
    mapping.location = (-1100, 0)
    # POINT: Scale multiplies the coordinate, so the pattern repeats Scale times per coordinate unit.
    mapping.vector_type = "POINT"
    if not box and uv_name:
        # The UV map the tiling was measured on, named: the same one the render uses.
        coord = nt.nodes.new("ShaderNodeUVMap")
        coord.uv_map = uv_name
        coord.location = (-1300, 0)
        nt.links.new(coord.outputs["UV"], mapping.inputs["Vector"])
    else:
        coord = nt.nodes.new("ShaderNodeTexCoord")
        coord.location = (-1300, 0)
        nt.links.new(coord.outputs["Object" if box else "UV"], mapping.inputs["Vector"])
    mapping.inputs["Scale"].default_value = scale
    y = 500
    have = {m["role"] for m in maps}
    for m in maps:
        role = m["role"]
        image = images[m["path"]]
        tex = nt.nodes.new("ShaderNodeTexImage")
        tex.location = (-750, y)
        tex.image = image
        tex.label = m.get("key") or role
        if box:
            tex.projection = "BOX"
            tex.projection_blend = 0.2
        nt.links.new(mapping.outputs["Vector"], tex.inputs["Vector"])
        if role in ROLE_INPUTS:
            nt.links.new(tex.outputs["Color"], _socket(bsdf, *ROLE_INPUTS[role]))
            if role == "emission":
                _socket(bsdf, "Emission Strength").default_value = 1.0
        elif role in ("normal", "normal_dx"):
            normal = nt.nodes.new("ShaderNodeNormalMap")
            normal.location = (-150, y)
            if role == "normal_dx":            # DirectX: green points the other way
                split = nt.nodes.new("ShaderNodeSeparateColor")
                split.location = (-450, y)
                flip = nt.nodes.new("ShaderNodeMath")
                flip.operation = "SUBTRACT"
                flip.inputs[0].default_value = 1.0
                flip.location = (-300, y - 60)
                join = nt.nodes.new("ShaderNodeCombineColor")
                join.location = (-200, y)
                nt.links.new(tex.outputs["Color"], split.inputs["Color"])
                nt.links.new(split.outputs[0], join.inputs[0])
                nt.links.new(split.outputs[1], flip.inputs[1])
                nt.links.new(flip.outputs[0], join.inputs[1])
                nt.links.new(split.outputs[2], join.inputs[2])
                nt.links.new(join.outputs["Color"], normal.inputs["Color"])
            else:
                nt.links.new(tex.outputs["Color"], normal.inputs["Color"])
            nt.links.new(normal.outputs["Normal"], _socket(bsdf, "Normal"))
        elif role == "displacement":
            disp = nt.nodes.new("ShaderNodeDisplacement")
            disp.location = (350, -450)
            disp.inputs["Midlevel"].default_value = 0.5
            disp.inputs["Scale"].default_value = float(disp_scale)
            nt.links.new(tex.outputs["Color"], disp.inputs["Height"])
            nt.links.new(disp.outputs["Displacement"], out.inputs["Displacement"])
        elif role == "arm":                    # AO / roughness / metal packed in R / G / B
            split = nt.nodes.new("ShaderNodeSeparateColor")
            split.location = (-400, y)
            nt.links.new(tex.outputs["Color"], split.inputs["Color"])
            if "roughness" not in have:
                nt.links.new(split.outputs[1], _socket(bsdf, "Roughness"))
            if "metallic" not in have:
                nt.links.new(split.outputs[2], _socket(bsdf, "Metallic"))
        y -= 320
    _tag(mat, meta)
    if meta.get("dimensions_m"):
        mat["asset_size_m"] = list(meta["dimensions_m"])[:2]
    return mat


def _wiring(mat):
    """What each image node actually drives, read back from the links (not from what was meant)."""
    nt = mat.node_tree
    found = []

    def follow(sock, depth=0):
        ends = []
        for link in sock.links:
            node = link.to_node
            if node.bl_idname in ("ShaderNodeBsdfPrincipled", "ShaderNodeOutputMaterial") or depth > 4:
                ends.append("%s.%s" % (node.name, link.to_socket.name))
            else:
                for out in node.outputs:
                    ends += follow(out, depth + 1)
        return ends
    for node in nt.nodes:
        if node.bl_idname == "ShaderNodeTexImage" and node.image is not None:
            ends = []
            for out in node.outputs:
                ends += follow(out)
            found.append({"map": node.label, "image": node.image.name,
                          "colorspace": node.image.colorspace_settings.name, "drives": sorted(set(ends)),
                          "projection": node.projection})
    return found


def import_texture(job, state, snap, rollback, expected, patch_before):
    meta, opts, files = job["meta"], job["options"], job["files"]
    maps = files["maps"]
    if not maps:
        raise Refuse("the asset has no maps a material can use")
    targets = []
    for name in opts.get("apply_to") or []:
        ob = bpy.data.objects.get(name)
        if ob is None:
            raise Refuse("there is no object named %r in this file" % name)
        if not hasattr(ob.data, "materials"):
            raise Refuse("%r is a %s, which cannot take a material" % (name, ob.type))
        targets.append(ob)
    slot = opts.get("slot")                 # None: not given; "all"; or one slot number
    for ob in targets:
        sharing = [o.name for o in bpy.data.objects if o.data is ob.data and o not in targets]
        count = len(ob.material_slots)
        if isinstance(slot, int) and slot >= count:
            raise Refuse("%r has %d material slot(s); there is no slot %d" % (ob.name, count, slot))
        if slot is None and count > 1:
            listing = ", ".join("%d: %s" % (i, s.material.name if s.material else "(empty)")
                                for i, s in enumerate(ob.material_slots))
            raise Refuse("%r has %d material slots (%s); say which with --slot N, or --slot all to put the "
                         "material in every one" % (ob.name, count, listing))
        if not ob.material_slots and sharing:
            raise Refuse("%r has no material slot and shares its mesh with %s: adding a slot would change them "
                         "too. Give it a slot first, or name them as well" % (ob.name, ", ".join(sharing)))
    if targets:
        # Edit Mode keeps a mesh's UVs in its own copy (the mesh's are empty meanwhile): leave it, so the edit is
        # written back, the UV map can be measured and the slots set; the mode is entered again at the end.
        synced = state.leave_mode()
        if synced:
            patch_before(synced)
    made, reused = [], []
    images, spaces = {}, {}
    for m in maps:
        image, space = _load_image(m["path"], m["role"] not in COLOR_ROLES, opts.get("pack"), made, snap, reused)
        images[m["path"]] = image
        spaces[m["key"]] = space
    metres_per_bu = unit_scale()
    dims = meta.get("dimensions_m") or None
    size_w, size_h = (float(dims[0]), float(dims[1])) if dims else (1.0, 1.0)
    disp_scale = meta.get("displacement_scale") or 0.01
    disp_bu = disp_scale / metres_per_bu
    wanted = opts.get("name") or meta.get("id") or "material"
    plans = []
    if not targets:
        plans.append({"objects": [], "box": False, "uv": None, "scale": (1.0 / size_w, 1.0 / size_h, 1.0),
                      "how": "no object given: assumes 1 UV unit = 1 m"})
    for ob in targets:
        per_uv, uv_name = uv_metres(ob)
        if per_uv is not None:
            metres = per_uv * metres_per_bu
            plan = {"box": False, "uv": uv_name, "scale": (metres / size_w, metres / size_h, 1.0),
                    "how": "UV map %r: %.4g m of surface per UV unit" % (uv_name, metres)}
        else:
            s = ob.matrix_world.to_scale() * metres_per_bu
            plan = {"box": True, "uv": None, "scale": (s.x / size_w, s.y / size_w, s.z / size_w),
                    "how": "no UV map: box projection on object coordinates"}
        for existing in plans:
            if existing["box"] == plan["box"] and existing["uv"] == plan["uv"] and existing.get("objects") is not None \
                    and all(abs(a - b) <= 0.02 * max(abs(b), 1e-9) for a, b in zip(existing["scale"], plan["scale"])):
                existing["objects"].append(ob)
                break
        else:
            plan["objects"] = [ob]
            plans.append(plan)
    materials = []
    for i, plan in enumerate(plans):
        name = wanted if i == 0 else "%s (%s)" % (wanted, plan["objects"][0].name)
        mat = build_material(name, maps, images, plan["scale"], plan["box"], disp_bu, meta, made, plan["uv"])
        plan["material"] = mat
        materials.append({"material": mat.name, "objects": [o.name for o in plan["objects"]],
                          "mapping_scale": [round(v, 6) for v in plan["scale"]], "tiling": plan["how"],
                          "uv_map": plan["uv"], "wiring": _wiring(mat)})
    applied = []
    for plan in plans:
        mat = plan["material"]
        for ob in plan["objects"]:
            sharing = [o for o in bpy.data.objects if o.data is ob.data and o not in targets]
            before = [[s.link, s.material] for s in ob.material_slots]
            data_before = list(ob.data.materials)

            def undo(ob=ob, before=before, data_before=data_before):
                if len(ob.material_slots) > len(before):
                    ob.data.materials.pop(index=len(ob.data.materials) - 1)
                for (link, material), s in zip(before, ob.material_slots):
                    s.link = link
                    s.material = material
                for i, material in enumerate(data_before):
                    if ob.data.materials[i] != material:
                        ob.data.materials[i] = material
            rollback.add(undo)
            expected.add("objects:" + ob.name)
            data_key = "%s:%s" % ("meshes" if ob.type == "MESH" else "other", ob.data.name)
            note = None
            indices = []
            if not ob.material_slots:
                ob.data.materials.append(mat)
                expected.add(data_key)
            else:
                indices = list(range(len(ob.material_slots))) if slot in (None, "all") else [slot]
                for i in indices:
                    s = ob.material_slots[i]
                    if s.link == "DATA" and sharing:
                        s.link = "OBJECT"
                        note = ("its mesh is shared with %s, so the slot now holds the material on the object "
                                "and they keep theirs" % ", ".join(o.name for o in sharing))
                    if s.link == "DATA":
                        expected.add(data_key)        # the mesh holds this slot's material
                    s.material = mat
            applied.append({"object": ob.name, "material": mat.name,
                            "previous": [before[i][1].name if before[i][1] else None for i in indices],
                            "slots": "all" if slot in (None, "all") else [slot], "note": note})
    # A material nothing uses any more would be dropped when the file is saved: keep the user's with a fake user.
    kept = []
    for item in applied:
        for name in item["previous"]:
            old = bpy.data.materials.get(name) if name else None
            if old is not None and old.users == 0 and not old.use_fake_user:
                old.use_fake_user = True
                kept.append(old.name)
                expected.add("materials:" + old.name)
                rollback.add(lambda old=old: setattr(old, "use_fake_user", False))
    skipped = meta.get("skipped_maps") or []
    report = {"materials": materials, "applied": applied, "skipped_maps": skipped, "colorspaces": spaces,
              "previous_materials_kept": kept, "images_reused": reused, "unit_scale": metres_per_bu,
              "displacement": {"scale_m": disp_scale, "scale_bu": disp_bu, "midlevel": 0.5,
                               "how": "wired to the Material Output as Blender's default (bump only); for true "
                                      "displacement set the material's displacement method to Displacement and "
                                      "subdivide the mesh"} if any(m["role"] == "displacement" for m in maps) else None,
              "real_size_m": [size_w, size_h] if dims else None,
              "packed": all(images[m["path"]].packed_file is not None for m in maps)}
    if metres_per_bu != 1.0:
        report["units"] = "this scene's Unit Scale is %g m per Blender unit; metre sizes were converted" % metres_per_bu
    if not targets:
        report["note"] = ("nothing uses the material yet; Blender drops unused materials when the file is saved. "
                          "Pass --apply-to to put it on objects")
    return report


# --------------------------------------------------------------------------------------------- models

IMPORTERS = {
    ".gltf": ("import_scene", "gltf", {}), ".glb": ("import_scene", "gltf", {}),
    ".fbx": ("import_scene", "fbx", {}), ".obj": ("wm", "obj_import", {}),
    ".stl": ("wm", "stl_import", {}), ".ply": ("wm", "ply_import", {}),
    ".usd": ("wm", "usd_import", {"set_frame_range": False, "create_world_material": False}),
    ".usda": ("wm", "usd_import", {"set_frame_range": False, "create_world_material": False}),
    ".usdc": ("wm", "usd_import", {"set_frame_range": False, "create_world_material": False}),
    ".usdz": ("wm", "usd_import", {"set_frame_range": False, "create_world_material": False}),
    ".abc": ("wm", "alembic_import", {"set_frame_range": False, "as_background_job": False}),
}


def _run_importer(path):
    ext = os.path.splitext(path)[1].lower()
    if ext not in IMPORTERS:
        raise Refuse("no importer for %s files" % ext)
    module, name, extra = IMPORTERS[ext]
    op = getattr(getattr(bpy.ops, module), name)
    try:
        result = op(**_op_kwargs(op, filepath=path, **extra))
    except AttributeError:
        raise Refuse("this Blender has no %s importer (bpy.ops.%s.%s)" % (ext, module, name))
    except RuntimeError as exc:
        lines = [l.strip() for l in str(exc).splitlines() if l.strip()
                 and not l.strip().startswith(("Location:", "Traceback", "File "))]
        raise Refuse("Blender's %s importer failed: %s" % (ext, lines[-1] if lines else str(exc).strip()))
    if "FINISHED" not in result:
        raise Refuse("Blender's %s importer did not finish (%s)" % (ext, ", ".join(result)))


def _append_blend(path, picks, prefer):
    with bpy.data.libraries.load(path, link=False) as (src, dst):
        available_c, available_o = list(src.collections), list(src.objects)
        if picks:
            missing = [n for n in picks if n not in available_c and n not in available_o]
            if missing:
                raise Refuse("the .blend has no collection or object named %s; it has collections %s and "
                             "objects %s" % (", ".join(map(repr, missing)), available_c[:20], available_o[:20]))
            dst.collections = [n for n in picks if n in available_c]
            dst.objects = [n for n in picks if n not in available_c]
            how = "picked"
        else:
            hit = next((n for n in prefer or [] if n in available_c), None)
            if hit:
                dst.collections = [hit]
                how = "collection %r" % hit
            else:
                dst.objects = available_o
                how = "every object"
    return [c for c in dst.collections if c is not None], [o for o in dst.objects if o is not None], how


def _gather(coll, snap):
    """Every new object and collection ends up under `coll`; nothing new hangs off the user's collections."""
    scene = bpy.context.scene
    old_parents = [scene.collection] + [c for c in bpy.data.collections if not snap.is_new(c, "collections")]
    new_colls = [c for c in snap.new("collections") if c is not coll]
    for nc in new_colls:
        for parent in old_parents:
            if nc.name in parent.children:
                parent.children.unlink(nc)
    for nc in new_colls:
        has_parent = any(nc.name in c.children for c in [coll] + new_colls if c is not nc)
        if not has_parent:
            coll.children.link(nc)
    for ob in snap.new("objects"):
        for parent in old_parents:
            if ob.name in parent.objects:
                parent.objects.unlink(ob)
        if not ob.users_collection:
            coll.objects.link(ob)
    return new_colls


def import_model(job, state, snap, rollback, expected, patch_before):
    meta, opts, files = job["meta"], job["options"], job["files"]
    scene, layer = bpy.context.scene, bpy.context.view_layer
    path = files["main"]
    synced = state.leave_mode()
    if synced:
        patch_before(synced)
    wanted = opts.get("name") or meta.get("name") or meta.get("id") or os.path.splitext(os.path.basename(path))[0]
    coll = bpy.data.collections.new(wanted)
    scene.collection.children.link(coll)        # a new key in scene_children and layers: nothing to mask
    _tag(coll, meta)
    lc = next(c for c in layer.layer_collection.children if c.collection == coll)
    layer.active_layer_collection = lc
    warnings, how, muted, texts = [], None, [], []
    world_before = scene.world
    try:
        if path.lower().endswith(".blend"):
            before_ids = all_id_pointers()
            colls, objs, how = _append_blend(path, opts.get("picks"), opts.get("prefer_collections"))
            # Before anything links or evaluates it: mute every Python driver it brought, wherever it sits.
            muted = _mute_scripted_drivers(ids_new_since(before_ids))
            for c in colls:
                if not any(c.name in p.children for p in [coll]):
                    coll.children.link(c)
            for ob in objs:
                if not ob.users_collection:
                    coll.objects.link(ob)
            texts = [t.name for t in snap.new("texts")]
        else:
            _run_importer(path)
    finally:
        layer.active_layer_collection = layer.layer_collection
    if scene.world != world_before:
        scene.world = world_before
        warnings.append("the file brought its own world; the scene's world was put back")
    new_colls = _gather(coll, snap)
    dropped, kept_extra = [], []
    for ob in list(snap.new("objects")):
        keep = opts.get("keep_lights") if ob.type == "LIGHT" else opts.get("keep_cameras") if ob.type == "CAMERA" else True
        if keep:
            continue
        if ob.children:
            kept_extra.append("%s %r (other objects hang under it)" % (ob.type.lower(), ob.name))
            continue
        data, kind = ob.data, ob.type
        dropped.append("%s %r" % (kind.lower(), ob.name))
        bpy.data.objects.remove(ob)
        if data is not None and data.users == 0:
            (bpy.data.lights if kind == "LIGHT" else bpy.data.cameras).remove(data)
    if dropped:
        warnings.append("the file brought %s; left out so they do not light or film your scene "
                        "(--keep-lights / --keep-cameras keep them)" % ", ".join(dropped))
    if kept_extra:
        warnings.append("kept %s; it now lights or films your scene" % ", ".join(kept_extra))
    new_objs = snap.new("objects")
    if not new_objs:
        raise Refuse("the file brought no objects" + (" but lights or cameras (%s); --keep-lights / --keep-cameras "
                                                      "keep them" % ", ".join(dropped) if dropped else ""))
    authored = measure(new_objs)
    metres_per_bu = unit_scale()
    size = opts.get("size")
    size_bu = float(size) / metres_per_bu if size else None
    at_m = [float(v) for v in (opts.get("at") or (0.0, 0.0, 0.0))]
    at = Vector(at_m) / metres_per_bu
    ground = opts.get("ground", True)
    rot = math.radians(float(opts.get("rotate") or 0.0))
    if authored is None:
        if size:
            raise Refuse("the file has no geometry to measure, so it cannot be scaled to --size")
        lo = Vector((0.0, 0.0, 0.0))
        hi = Vector((0.0, 0.0, 0.0))
        warnings.append("no geometry to measure: placed by the file's origin")
    else:
        lo, hi = Vector(authored["min"]), Vector(authored["max"])
    largest = max(hi - lo) if authored else 0.0
    factor = (size_bu / largest) if (size_bu and largest > 0) else 1.0
    pivot = Vector(((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, lo.z if ground else 0.0))
    place = Matrix.Translation(at) @ Matrix.Rotation(rot, 4, "Z") @ Matrix.Scale(factor, 4) @ Matrix.Translation(-pivot)
    roots = [o for o in new_objs if o.parent is None]
    animated = [o.name for o in roots if (o.animation_data and (o.animation_data.action or o.animation_data.drivers))
                or o.constraints]
    holder = None
    if animated:
        holder = bpy.data.objects.new(wanted + " placement", None)
        coll.objects.link(holder)
        holder.matrix_world = place
        for root in roots:
            root.parent = holder
            root.matrix_parent_inverse = Matrix.Identity(4)
        warnings.append("%s move by their own animation or constraints, so they hang under the empty %r that "
                        "carries the placement" % (", ".join(animated), holder.name))
    else:
        for root in roots:
            root.matrix_world = place @ root.matrix_world
    final = measure(new_objs) if authored else None
    # Whether new objects join a Local View depends on how the importer made them (the glTF duck did, the .blend box
    # did not): say what is so, per view, instead of guessing.
    left_out = set()
    for window in getattr(bpy.context.window_manager, "windows", []):
        for area in window.screen.areas:
            space = area.spaces.active if area.type == "VIEW_3D" else None
            if space is not None and space.local_view is not None:
                left_out.update(o.name for o in new_objs + ([holder] if holder else []) if not o.local_view_get(space))
    if left_out:
        names = sorted(left_out)
        warnings.append("a 3D view is in Local View and %s %s not part of it, so that view does not show %s until "
                        "Local View is left" % (", ".join(names[:5]) + (" and %d more" % (len(names) - 5)
                                                                         if len(names) > 5 else ""),
                                                "is" if len(names) == 1 else "are", "it" if len(names) == 1 else "them"))
    images = [i for i in snap.new("images") if i.packed_file is None and i.source in ("FILE", "SEQUENCE", "TILED")
              and i.filepath]
    packed = []
    if opts.get("pack"):
        for image in images:
            try:
                image.pack()
                packed.append(image.name)
            except RuntimeError as exc:
                warnings.append("could not pack the image %r: %s" % (image.name, str(exc).strip().splitlines()[-1]))
    elif images:
        warnings.append("%d image(s) are read from the files beside the model (%s); the scene needs that folder - "
                        "--pack keeps them inside the .blend" % (len(images), os.path.dirname(path)))
    report = {"collection": coll.name, "collections_inside": [c.name for c in new_colls],
              "objects": sorted(o.name for o in new_objs), "roots": sorted(o.name for o in roots),
              "authored": authored, "final": final, "scale_applied": factor, "size_requested": size,
              "at": at_m, "at_bu": list(at), "grounded": bool(ground), "rotate_deg": float(opts.get("rotate") or 0.0),
              "placement_empty": holder.name if holder else None, "how": how, "unit_scale": metres_per_bu,
              "scripted_drivers_muted": muted, "texts_brought": texts, "left_out": dropped,
              "packed_images": packed, "warnings": warnings}
    if metres_per_bu != 1.0:
        report["units"] = ("this scene's Unit Scale is %g m per Blender unit: --size and --at were converted; "
                           "'authored' and 'final' are in Blender units" % metres_per_bu)
    if final:
        report["final_lowest_z"] = final["min"][2]
        report["final_lowest_z_m"] = final["min"][2] * metres_per_bu
        report["final_largest_bu"] = max(final["size"])
        report["final_largest_m"] = max(final["size"]) * metres_per_bu
        report["final_centre_xy"] = [(final["min"][0] + final["max"][0]) / 2, (final["min"][1] + final["max"][1]) / 2]
    published = meta.get("dimensions_m")
    if published and authored and len(published) == 3:
        report["published_size_m"] = published
        a = sorted(v * metres_per_bu for v in authored["size"])
        b = sorted(float(v) for v in published)
        report["authored_size_m"] = a
        report["authored_matches_published"] = all(abs(x - y) <= 0.01 * max(y, 1e-6) for x, y in zip(a, b))
    return report


# --------------------------------------------------------------------------------------------- the wrapper

def _check_identity(job):
    want_file = job.get("expect_file")
    if want_file is not None:
        have = bpy.data.filepath
        if not have:
            same = False
        elif os.path.dirname(want_file):
            try:
                same = os.path.samefile(have, want_file)      # 8.3 names, case and links name the same file
            except OSError:
                same = os.path.normcase(os.path.realpath(have)) == os.path.normcase(os.path.realpath(want_file))
        else:
            same = os.path.normcase(os.path.basename(have)) == os.path.normcase(want_file)
        if not same:
            raise Refuse("WRONG BLENDER, nothing was touched: it has %r open, not %r" % (have or "(unsaved)", want_file))
    want_pid = job.get("expect_pid")
    if want_pid is not None and int(want_pid) != os.getpid():
        raise Refuse("WRONG BLENDER, nothing was touched: its process id is %d, not %d" % (os.getpid(), int(want_pid)))


def _who():
    return {"version": bpy.app.version_string, "pid": os.getpid(), "file": bpy.data.filepath,
            "background": bool(bpy.app.background)}


def run_job(job):
    who = _who()
    try:
        _check_identity(job)
    except Refuse as exc:
        return {"ok": False, "code": "refused", "error": str(exc), "blender": who, "touched": False}
    op = job.get("op")
    if op == "credits":
        text, linked = _credit_texts()
        out = {"ok": True, "blender": who, "credits": text.as_string().splitlines() if text else []}
        if linked:
            out["linked_credits"] = {(t.library.filepath if t.library else "?"): t.as_string().splitlines() for t in linked}
        return out
    if op == "selftest_fingerprint":
        return selftest_fingerprint(job)
    # The whole job runs in one bridge tick, so a playing timeline cannot show up between two fingerprints: ask.
    wm = bpy.context.window_manager
    if wm is not None and any(w.screen is not None and w.screen.is_animation_playing for w in wm.windows):
        return {"ok": False, "code": "refused", "blender": who, "touched": False,
                "error": "the timeline is playing: stop it (Esc) and import again. Nothing was touched"}
    first = fingerprint()
    second = fingerprint()
    control = diff_fingerprints(first, second)
    if control:
        return {"ok": False, "code": "refused", "blender": who, "touched": False,
                "error": "the scene changes by itself between two looks (%s): a driver or handler that gives a new "
                         "value each time? Nothing was touched" % "; ".join(control[:5])}
    state = SceneState()
    snap = IdSnapshot()
    rollback = Rollback()
    expected = set()

    def patch_before(meshes):
        synced = fingerprint()
        for name in meshes:
            if name in synced["meshes"]:
                first["meshes"][name] = synced["meshes"][name]

    report, error, tb = None, None, None
    try:
        kind = job.get("kind")
        if kind == "hdri":
            report = import_hdri(job, rollback, expected, snap)
        elif kind == "texture":
            report = import_texture(job, state, snap, rollback, expected, patch_before)
        elif kind == "model":
            report = import_model(job, state, snap, rollback, expected, patch_before)
        else:
            raise Refuse("unknown job kind %r" % kind)
        report["renamed"] = snap.collisions()
        report["credit"] = add_credit(job["meta"], rollback)
        expected.add("texts:" + CREDITS_TEXT)
    except Exception as exc:  # noqa: BLE001 - every failure is rolled back and reported
        error = str(exc) if isinstance(exc, Refuse) else "%s: %s" % (type(exc).__name__, exc)
        tb = None if isinstance(exc, Refuse) else traceback.format_exc()
        problems = rollback.run()
        try:
            snap.remove_new()
        except Exception as exc2:  # noqa: BLE001
            problems.append("could not remove what the import made: %s" % exc2)
        if problems:
            error += " (and putting things back hit: %s)" % "; ".join(problems)
    put_back = state.restore(mode=False)
    undo_step = None
    if error is None and not bpy.app.background:
        undo_step = _undo_push("assets.py: import %s" % job["meta"].get("id"))   # in Object Mode: a whole-file step
    put_back += state.restore_mode()
    last = fingerprint()
    left = diff_fingerprints(first, last, expected if error is None else ())
    if error is not None:
        # After a failure nothing may be NEW either: every datablock, link and layer must be as it was.
        left += added_keys(first, last)
    check = {"control_equal": True, "unexpected_changes": left[:40], "expected_changes": sorted(expected),
             "settings_put_back": put_back, "ok": not left}
    out = {"blender": who, "scene_check": check}
    if error is not None:
        out.update({"ok": False, "code": "failed" if not left else "left_changed", "error": error,
                    "scene_unchanged": not left})
        if tb:
            out["traceback"] = tb
        return out
    out.update(report)
    out["ok"] = not left
    if left:
        out["code"] = "left_changed"
        out["error"] = "BLENDER WAS LEFT CHANGED outside what the import does: " + "; ".join(left[:8])
    if undo_step is not None:
        out["undo_step"] = undo_step
    return out


def _undo_push(message):
    """One undo step for the whole import, so Ctrl+Z takes it away (a window is needed: GUI only)."""
    wm = bpy.context.window_manager
    window = bpy.context.window or (wm.windows[0] if wm.windows else None)
    if window is None:
        return False
    try:
        with bpy.context.temp_override(window=window):
            bpy.ops.ed.undo_push(message=message)
        return True
    except Exception:  # noqa: BLE001 - no undo stack in this context
        return False


# --------------------------------------------------------------------------------------------- self-test

def selftest_fingerprint(job):
    """The fingerprint must see nothing when nothing happened, and see each planted change. Headless only."""
    if not bpy.app.background or job.get("mode") != "headless":
        return {"ok": False, "error": "the fingerprint self-test plants changes; it runs only in a headless Blender"}
    rows = []
    base = fingerprint()
    again = fingerprint()
    rows.append({"row": "two looks, nothing between", "changes": diff_fingerprints(base, again), "want": "none"})
    scene = bpy.context.scene
    layer = bpy.context.view_layer
    ob = next((o for o in scene.objects if o.type == "MESH"), None)
    plants = []
    if ob is not None:
        plants.append(("move an object 0.1 mm", lambda: setattr(ob, "location", ob.location + Vector((1e-4, 0, 0)))))
        plants.append(("select an object", lambda: ob.select_set(not ob.select_get())))
        plants.append(("hide an object", lambda: ob.hide_set(not ob.hide_get())))
        plants.append(("move one vertex", lambda: setattr(ob.data.vertices[0], "co", ob.data.vertices[0].co + Vector((0, 0, 1e-3)))))
        if ob.modifiers:
            plants.append(("toggle a modifier", lambda: setattr(ob.modifiers[0], "show_viewport", not ob.modifiers[0].show_viewport)))
    plants.append(("change the frame", lambda: scene.frame_set(scene.frame_current + 1)))
    plants.append(("change the render engine", lambda: setattr(scene.render, "engine",
                                                               "CYCLES" if scene.render.engine != "CYCLES" else "BLENDER_WORKBENCH")))
    plants.append(("change the active object", lambda: setattr(layer.objects, "active", None)))
    mats = [m for m in bpy.data.materials if m.node_tree is not None]
    if mats:
        def poke():
            node = next(n for n in mats[0].node_tree.nodes if n.inputs and hasattr(n.inputs[0], "default_value"))
            v = node.inputs[0].default_value
            node.inputs[0].default_value = (v + 0.25) if isinstance(v, float) else tuple(x * 0.5 for x in v)
        plants.append(("change a material input", poke))
        plants.append(("rename a material", lambda: setattr(mats[0], "name", mats[0].name + "_x")))
    if scene.world is not None and scene.world.node_tree is not None:
        def poke_world():
            bg = next(n for n in scene.world.node_tree.nodes if n.bl_idname == "ShaderNodeBackground")
            bg.inputs["Strength"].default_value += 0.5
        plants.append(("change the world strength", poke_world))
    if bpy.data.texts:
        plants.append(("write into a text", lambda: bpy.data.texts[0].write("x")))
    plants.append(("exclude a collection", lambda: setattr(layer.layer_collection.children[0], "exclude",
                                                            not layer.layer_collection.children[0].exclude)
                   if layer.layer_collection.children else None))
    arm = next((o for o in scene.objects if o.type == "ARMATURE"), None)
    if arm is not None:
        plants.append(("pose a bone", lambda: setattr(arm.pose.bones[0], "rotation_quaternion", (0.9, 0.1, 0, 0))))
    for name, plant in plants:
        before = fingerprint()
        plant()
        after = fingerprint()
        rows.append({"row": name, "changes": diff_fingerprints(before, after)[:3], "want": "found"})
    before = fingerprint()           # the failure path also counts what is new
    scene.collection.objects.link(bpy.data.objects.new("selftest_left_behind", None))
    rows.append({"row": "a datablock left behind after a failure", "changes": added_keys(before, fingerprint())[:3],
                 "want": "found"})
    ok = all((not r["changes"]) if r["want"] == "none" else bool(r["changes"]) for r in rows)
    return {"ok": ok, "rows": rows, "blender": _who()}


# --------------------------------------------------------------------------------------------- entry points

def bridge_entry(job_json):
    job = json.loads(job_json)
    job["mode"] = "live"
    return run_job(job)


def _main_headless():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    job_path = argv[argv.index("--job") + 1]
    result_path = argv[argv.index("--result") + 1]
    with open(job_path, encoding="utf-8") as f:
        job = json.load(f)
    job["mode"] = "headless"
    try:
        if job.get("new_file"):
            bpy.ops.wm.read_homefile(use_empty=True)
        result = run_job(job)
        if result.get("ok") and job.get("save_as"):
            target = os.path.abspath(job["save_as"])
            have = bpy.data.filepath
            same = False
            if have:
                try:
                    same = os.path.exists(target) and os.path.samefile(target, have)
                except OSError:
                    same = False
                same = same or os.path.normcase(os.path.realpath(target)) == os.path.normcase(os.path.realpath(have))
            if same:
                result = {"ok": False, "code": "refused", "error": "refusing to save over the input file %s" % target}
            else:
                bpy.context.preferences.filepaths.save_version = 0      # no PATH.blend1 beside it (factory prefs, not saved)
                bpy.ops.wm.save_as_mainfile(filepath=target, copy=True, check_existing=False)
                result["saved_as"] = target
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc), "traceback": traceback.format_exc()}
    tmp = result_path + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(result, f, default=str)
    os.replace(tmp, result_path)


if globals().get("__name__") == "__main__":
    _main_headless()

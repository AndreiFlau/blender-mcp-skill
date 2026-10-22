# Portions adapted from MCP for Blender (https://github.com/ahujasid/mcp-for-blender, commit 25e1fe0), Copyright (c) 2025 Siddharth Ahuja, MIT License; see THIRD_PARTY_NOTICES.md.
# SPDX-License-Identifier: GPL-3.0-or-later
"""The part of look.py that runs inside Blender: the scene as pictures, and everything put back.

Plain bpy + numpy. It runs three ways:

- LIVE: look.py sends this file's source over the Blender Lab MCP bridge with a call of
  `bridge_start(ARGS)` appended. A windowed Blender gets the work in chunks (one tile per
  bridge tick, through the bridge's deferred response), and every chunk puts back what it
  changed before Blender's event loop runs again.
- HEADLESS: `blender --background --factory-startup [FILE] --python look_blender.py -- ARGS.json`
  (look.py does this for --blend / --new). The result is written to ARGS["result_path"].
- From a test: `import look_blender; look_blender.run(args)`.

How the pictures are made: Blender draws its own 3D viewport into an offscreen buffer
(gpu.types.GPUOffScreen.draw_view3d) with the view and projection this tool computes, after
switching that viewport's shading and overlays for the length of one tile. In a background
Blender 5.2 or later, gpu.init() makes the same drawing available without a window. Where it
is not (a background Blender older than 5.2, or --backend render), each tile is rendered
through a temporary camera with Workbench or EEVEE instead, and wireframe and X-ray are
emulated with numpy; the result's "warnings" say what differs.

Before anything is changed a fingerprint of the session is taken (render, display and EEVEE
settings, every view layer and collection flag, every object's visibility, selection,
transforms, display settings and animated values, every 3D view's shading, overlays, view
matrices and local view, the editors and their tabs, the frame, the mode, the active object,
the 3D cursor and tool settings, the contents of cameras, lights, armatures, materials,
worlds, meshes, images and texts, custom properties, and the name and identity of every
datablock in every bpy.data collection), and it is taken again at the end. A difference fails
the run with the list of what changed. It is a check of those things, not of everything.

While a look works, the session's frame-change, depsgraph-update and render handlers are
paused (an add-on that records or sends every change sees none of the temporary ones);
--run-handlers lets them run.
"""

import contextlib
import io
import json
import math
import os
import re
import sys
import tempfile
import time
import traceback
import unicodedata
import zlib
import struct

import bpy
import numpy as np
from mathutils import Matrix, Vector

LOOK_VERSION = "1.0.0"
MODES = ("viewport", "camera", "angles", "frames", "image", "ui")
SHADINGS = ("solid", "material", "rendered", "wireframe", "xray")
ANGLES = {
    "front": (0.0, -1.0, 0.0), "back": (0.0, 1.0, 0.0),
    "right": (1.0, 0.0, 0.0), "left": (-1.0, 0.0, 0.0),
    "top": (0.0, 0.0, 1.0), "bottom": (0.0, 0.0, -1.0),
    "three_quarter": (1.0, -1.0, 0.7),
}
DEFAULT_VIEWS = ("front", "right", "top", "three_quarter")
GEOMETRY = {"MESH", "CURVE", "SURFACE", "META", "FONT", "CURVES", "POINTCLOUD", "VOLUME",
            "GREASEPENCIL", "GPENCIL", "ARMATURE"}
MESHLIKE = {"MESH", "CURVE", "SURFACE", "META", "FONT"}
EEVEE_IDS = ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT")
BACKGROUND = (58, 58, 58)
CLUTTER = ("show_floor", "show_ortho_grid", "show_axis_x", "show_axis_y", "show_axis_z", "show_cursor",
           "show_extras", "show_relationship_lines", "show_object_origins", "show_object_origins_all",
           "show_text", "show_stats", "show_outline_selected", "show_annotation", "show_motion_paths",
           "show_camera_guides", "show_light_colors", "show_look_dev", "show_face_orientation",
           "show_onion_skins", "show_fade_inactive", "show_performance", "show_statvis")
MAX_FIT_POINTS = 400000          # per object, for framing only
MAX_FIT_INSTANCES = 200000       # instances sampled evenly over all of them, for framing only
MAX_TILES = 24
TINY_PX = 3.0                    # a framed target smaller than this in a tile is said by name
HANDLER_LISTS = ("frame_change_pre", "frame_change_post", "depsgraph_update_pre", "depsgraph_update_post",
                 "render_init", "render_pre", "render_post", "render_stats", "render_write", "render_complete",
                 "render_cancel")
MAX_HASHED_VERTICES = 1000000    # fingerprint: mesh coordinates are hashed up to this many vertices in all
UI_NEXT_STEP = ("To see an add-on's panel, serve the bridge from a Blender with a window (tests/look_gui_check.py in "
                "this skill shows how), or test the panel's draw() headless (the blender-headless-qa skill).")


class LookError(Exception):
    """A refusal or a failure with a reason; `code` is one of refused, bad_args, failed, left_changed."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


# ----------------------------------------------------------------------------------------------
# Pictures as numpy arrays: uint8, rows top first, RGB or RGBA.

def long_path(path):
    r"""On Windows, Blender's Python cannot open paths of 260 characters or more; the \\?\ form can."""
    prefix = "\\\\?\\"
    if os.name == "nt":
        full = os.path.abspath(path)
        if len(full) >= 240 and not full.startswith(prefix):
            return prefix + full
    return path


def same_file(a, b):
    """True when the two paths name the same file or folder, however they are spelled (8.3 short names, letter
    case, '..', links). Compares the files themselves when both exist, the resolved paths otherwise."""
    if not a or not b:
        return False
    try:
        if os.path.exists(a) and os.path.exists(b):
            return os.path.samefile(a, b)
    except OSError:
        pass
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except OSError:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def write_png(path, img):
    """An 8-bit PNG written directly (filter 0, zlib): no image datablock, the bytes are the array."""
    img = np.ascontiguousarray(img, dtype=np.uint8)
    h, w, c = img.shape
    color_type = {3: 2, 4: 6}[c]
    raw = np.empty((h, w * c + 1), np.uint8)
    raw[:, 0] = 0
    raw[:, 1:] = img.reshape(h, w * c)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, color_type, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw.tobytes(), 6)) + chunk(b"IEND", b""))
    part = long_path(path + ".part")
    with open(part, "wb") as handle:
        handle.write(data)
    os.replace(part, long_path(path))


def _data_colorspace(image):
    """Make `image` load its stored numbers unchanged (no linearising), whatever the OCIO config calls that."""
    names = [item.identifier for item in image.colorspace_settings.bl_rna.properties["name"].enum_items]
    for name in ("Non-Color", "Raw", "Generic Data", "Data"):
        if name in names:
            image.colorspace_settings.name = name
            return True
    return False


def read_image_file(path):
    """(float32 HxWx4 of the stored values, 0..1, rows top first; the file format). The datablock is removed."""
    image = bpy.data.images.load(path, check_existing=False)
    try:
        _data_colorspace(image)
        image.alpha_mode = "CHANNEL_PACKED"
        width, height = image.size[0], image.size[1]
        channels = image.channels
        fmt = image.file_format
        if not width or not height:
            raise LookError("failed", "%r has no pixels" % path)
        flat = np.empty(width * height * channels, np.float32)
        image.pixels.foreach_get(flat)
    finally:
        bpy.data.images.remove(image)
    arr = flat.reshape(height, width, channels)[::-1]
    return _rgba(arr), fmt


def _is_linear(colorspace):
    """Scene-linear data (shown through a view transform), as opposed to display-referred pictures."""
    name = colorspace.lower()
    return "linear" in name or name in ("acescg", "aces2065-1", "rec.2020 linear", "scene linear")


def _rgba(arr):
    """Any 1-4 channel float array as RGBA."""
    c = arr.shape[2]
    if c == 4:
        return np.ascontiguousarray(arr)
    out = np.ones(arr.shape[:2] + (4,), np.float32)
    if c == 1:
        out[..., :3] = arr[..., :1]
    elif c == 2:
        out[..., :3] = arr[..., :1]
        out[..., 3] = arr[..., 1]
    else:
        out[..., :3] = arr[..., :3]
    return out


def srgb_encode(linear):
    lin = np.clip(linear, 0.0, 1.0)
    return np.where(lin <= 0.0031308, lin * 12.92, 1.055 * np.power(lin, 1 / 2.4) - 0.055)


def to_u8(float01):
    return np.clip(np.rint(np.asarray(float01, np.float32) * 255.0), 0, 255).astype(np.uint8)


def over_background(rgba_u8, background=BACKGROUND, checker=False):
    """RGB uint8: RGBA composited over a flat colour (or a checkerboard, to show transparency)."""
    if rgba_u8.shape[2] == 3:
        return rgba_u8
    rgb = rgba_u8[..., :3].astype(np.float32)
    alpha = rgba_u8[..., 3:4].astype(np.float32) / 255.0
    if alpha.min() >= 1.0:
        return np.ascontiguousarray(rgba_u8[..., :3])
    h, w = rgba_u8.shape[:2]
    bg = np.empty((h, w, 3), np.float32)
    bg[:] = background
    if checker:
        yy, xx = np.mgrid[0:h, 0:w]
        bg[((yy // 8 + xx // 8) % 2).astype(bool)] = [88, 88, 88]
    return np.clip(np.rint(rgb * alpha + bg * (1 - alpha)), 0, 255).astype(np.uint8)


def resize_box(img, w2, h2):
    """Exact box-filter downscale (each output pixel the mean of the input pixels it covers); nearest when enlarging."""
    h, w = img.shape[:2]
    w2, h2 = max(1, int(w2)), max(1, int(h2))
    if (w, h) == (w2, h2):
        return img
    if w2 > w or h2 > h:
        yi = (np.arange(h2) * h) // h2
        xi = (np.arange(w2) * w) // w2
        return np.ascontiguousarray(img[yi][:, xi])
    ys = (np.arange(h2 + 1) * h) // h2
    xs = (np.arange(w2 + 1) * w) // w2
    out = np.empty((h2, w2, img.shape[2]), np.uint8)
    area = (ys[1:] - ys[:-1])[:, None] * (xs[1:] - xs[:-1])[None, :]
    for c in range(img.shape[2]):
        cs = np.zeros((h + 1, w), np.int64)
        np.cumsum(img[..., c], axis=0, out=cs[1:])
        rows = cs[ys[1:]] - cs[ys[:-1]]
        cs2 = np.zeros((h2, w + 1), np.int64)
        np.cumsum(rows, axis=1, out=cs2[:, 1:])
        sums = cs2[:, xs[1:]] - cs2[:, xs[:-1]]
        out[..., c] = np.clip(np.rint(sums / area), 0, 255)
    return out


# A 5 x 7 bitmap font, drawn for this tool (upper case; lower case is shown as upper case).
_GLYPHS = """
A .###. #...# #...# ##### #...# #...# #...#
B ####. #...# #...# ####. #...# #...# ####.
C .###. #...# #.... #.... #.... #...# .###.
D ####. #...# #...# #...# #...# #...# ####.
E ##### #.... #.... ####. #.... #.... #####
F ##### #.... #.... ####. #.... #.... #....
G .###. #...# #.... #.### #...# #...# .####
H #...# #...# #...# ##### #...# #...# #...#
I .###. ..#.. ..#.. ..#.. ..#.. ..#.. .###.
J ..### ...#. ...#. ...#. ...#. #..#. .##..
K #...# #..#. #.#.. ##... #.#.. #..#. #...#
L #.... #.... #.... #.... #.... #.... #####
M #...# ##.## #.#.# #.#.# #...# #...# #...#
N #...# #...# ##..# #.#.# #..## #...# #...#
O .###. #...# #...# #...# #...# #...# .###.
P ####. #...# #...# ####. #.... #.... #....
Q .###. #...# #...# #...# #.#.# #..#. .##.#
R ####. #...# #...# ####. #.#.. #..#. #...#
S .#### #.... #.... .###. ....# ....# ####.
T ##### ..#.. ..#.. ..#.. ..#.. ..#.. ..#..
U #...# #...# #...# #...# #...# #...# .###.
V #...# #...# #...# #...# #...# .#.#. ..#..
W #...# #...# #...# #.#.# #.#.# #.#.# .#.#.
X #...# #...# .#.#. ..#.. .#.#. #...# #...#
Y #...# #...# .#.#. ..#.. ..#.. ..#.. ..#..
Z ##### ....# ...#. ..#.. .#... #.... #####
0 .###. #...# #..## #.#.# ##..# #...# .###.
1 ..#.. .##.. ..#.. ..#.. ..#.. ..#.. .###.
2 .###. #...# ....# ...#. ..#.. .#... #####
3 ##### ...#. ..#.. ...#. ....# #...# .###.
4 ...#. ..##. .#.#. #..#. ##### ...#. ...#.
5 ##### #.... ####. ....# ....# #...# .###.
6 ..##. .#... #.... ####. #...# #...# .###.
7 ##### ....# ...#. ..#.. .#... .#... .#...
8 .###. #...# #...# .###. #...# #...# .###.
9 .###. #...# #...# .#### ....# ...#. .##..
. ..... ..... ..... ..... ..... .##.. .##..
, ..... ..... ..... ..... .##.. ..#.. .#...
: ..... .##.. .##.. ..... .##.. .##.. .....
; ..... .##.. .##.. ..... .##.. ..#.. .#...
- ..... ..... ..... ##### ..... ..... .....
_ ..... ..... ..... ..... ..... ..... #####
( ...#. ..#.. .#... .#... .#... ..#.. ...#.
) .#... ..#.. ...#. ...#. ...#. ..#.. .#...
/ ..... ....# ...#. ..#.. .#... #.... .....
' ..#.. ..#.. .#... ..... ..... ..... .....
+ ..... ..#.. ..#.. ##### ..#.. ..#.. .....
= ..... ..... ##### ..... ##### ..... .....
% ##..# ##..# ...#. ..#.. .#... #..## #..##
[ .###. .#... .#... .#... .#... .#... .###.
] .###. ...#. ...#. ...#. ...#. ...#. .###.
! ..#.. ..#.. ..#.. ..#.. ..#.. ..... ..#..
? .###. #...# ....# ...#. ..#.. ..... ..#..
* ..... ..#.. #.#.# .###. #.#.# ..#.. .....
< ...#. ..#.. .#... #.... .#... ..#.. ...#.
> .#... ..#.. ...#. ....# ...#. ..#.. .#...
| ..#.. ..#.. ..#.. ..#.. ..#.. ..#.. ..#..
# .#.#. .#.#. ##### .#.#. ##### .#.#. .#.#.
"""
FONT = {" ": np.zeros((7, 5), bool)}
for _line in _GLYPHS.strip().splitlines():
    _char, _rows = _line[0], _line[2:].split()
    FONT[_char] = np.array([[c == "#" for c in row] for row in _rows], bool)


def plain_label(text):
    """`text` as the 5 x 7 font can draw it: upper case, accents dropped (Ação -> ACAO), and any other letter it
    has no glyph for shown as '*' (the JSON keeps every name exactly)."""
    out = []
    for char in unicodedata.normalize("NFKD", str(text)):
        if unicodedata.combining(char):
            continue
        up = char.upper()
        out.append(up if len(up) == 1 and up in FONT else "*")
    return "".join(out)


def fit_text(text, room):
    """`text` in at most `room` characters; a cut is shown with '..' at the end."""
    text = plain_label(text)
    if len(text) <= room:
        return text
    return text[:room - 2] + ".." if room >= 3 else text[:max(0, room)]


def draw_text(img, x, y, text, color, scale=1):
    """Draw `text` (see plain_label) into the uint8 RGB array at top-left (x, y); returns the width used."""
    cx = x
    for char in plain_label(text):
        glyph = FONT.get(char, FONT["*"])
        big = np.kron(glyph, np.ones((scale, scale), bool)) if scale > 1 else glyph
        gh, gw = big.shape
        y0, x0 = max(0, y), max(0, cx)
        y1, x1 = min(img.shape[0], y + gh), min(img.shape[1], cx + gw)
        if y1 > y0 and x1 > x0:
            mask = big[y0 - y:y1 - y, x0 - cx:x1 - cx]
            img[y0:y1, x0:x1][mask] = color
        cx += (5 + 1) * scale
    return cx - x


def text_width(text, scale=1):
    return len(plain_label(text)) * 6 * scale


def compose_sheet(tiles, labels, title, sheet_max):
    """One labelled contact sheet from RGB tiles; returns (sheet, cells) with each cell's place on the sheet."""
    n = len(tiles)
    cols = n if n <= 3 else int(math.ceil(math.sqrt(n)))
    rows = int(math.ceil(n / cols))
    cw = max(t.shape[1] for t in tiles)
    ch = max(t.shape[0] for t in tiles)
    gap = 6

    def layout(cell_w, cell_h):
        scale = 2 if cell_w >= 300 else 1
        bar = 7 * scale + 8
        head = (7 * scale + 10) if title else 0
        width = gap + cols * (cell_w + gap)
        height = head + gap + rows * (bar + cell_h + gap)
        return scale, bar, head, width, height

    k = 1.0
    for _ in range(4):
        scale, bar, head, width, height = layout(int(cw * k), int(ch * k))
        if max(width, height) <= sheet_max:
            break
        k *= min(0.98, sheet_max / max(width, height))
    cw2, ch2 = max(16, int(cw * k)), max(16, int(ch * k))
    scale, bar, head, width, height = layout(cw2, ch2)
    sheet = np.empty((height, width, 3), np.uint8)
    sheet[:] = (24, 24, 24)
    if title:
        room = (width - 2 * gap) // (6 * scale)
        draw_text(sheet, gap, 5, fit_text(title, room), (235, 235, 235), scale)
    cells = []
    for i, (tile, text) in enumerate(zip(tiles, labels)):
        r, c = divmod(i, cols)
        x = gap + c * (cw2 + gap)
        y = head + gap + r * (bar + ch2 + gap)
        tw, th = max(1, int(round(tile.shape[1] * k))), max(1, int(round(tile.shape[0] * k)))
        small = resize_box(tile, tw, th) if k < 1.0 else tile
        sheet[y:y + bar, x:x + cw2] = (38, 38, 38)
        room = max(1, (cw2 - 8) // (6 * scale))
        shown = fit_text(text, room)               # a label too long for its cell ends in '..'
        first, sep, rest = shown.partition(" | ")
        used = draw_text(sheet, x + 4, y + 4, first, (255, 196, 0), scale)
        if sep:
            draw_text(sheet, x + 4 + used, y + 4, sep + rest, (235, 235, 235), scale)
        ox, oy = x + (cw2 - tw) // 2, y + bar + (ch2 - th) // 2
        sheet[oy:oy + th, ox:ox + tw] = small[:th, :tw]
        cells.append({"index": i + 1, "x": int(ox), "y": int(oy), "w": int(tw), "h": int(th)})
    return sheet, cells, {"columns": cols, "rows": rows, "scale": round(k, 4)}


# ----------------------------------------------------------------------------------------------
# Changing settings for a moment.

def _snap(value):
    if value is None or isinstance(value, (bool, int, float, str, bpy.types.ID)):
        return value                     # never ID.copy(): that would make a new datablock
    if isinstance(value, bpy.types.bpy_struct):
        return value
    if hasattr(value, "copy"):
        try:
            return value.copy()
        except TypeError:
            pass
    if hasattr(value, "__len__") and not isinstance(value, (str, bytes)) and not isinstance(value, bpy.types.ID):
        try:
            return tuple(value)
        except TypeError:
            return value
    return value


class Changes:
    """Every temporary change, with how to undo it; restore() undoes them newest first."""

    def __init__(self):
        self.undo = []

    def set(self, owner, attr, value):
        """Set owner.attr = value for now. False (and nothing changed) when there is no such setting or it is read-only."""
        if owner is None or not hasattr(owner, attr):
            return False
        old = _snap(getattr(owner, attr))
        try:
            if _same(_plain(old), _plain(value)):
                return True
        except Exception:
            pass
        try:
            setattr(owner, attr, value)
        except (AttributeError, TypeError, ValueError, RuntimeError):
            return False
        self.undo.append(lambda o=owner, a=attr, v=old: setattr(o, a, v))
        return True

    def call(self, undo):
        self.undo.append(undo)

    def restore(self):
        errors = []
        while self.undo:
            undo = self.undo.pop()
            try:
                undo()
            except Exception as error:   # keep going: every other change still has to be undone
                errors.append("%s: %s" % (type(error).__name__, error))
        return errors


def _handler_name(handler):
    module = getattr(handler, "__module__", None) or "?"
    name = getattr(handler, "__qualname__", None) or getattr(handler, "__name__", None) or repr(handler)
    return "%s.%s" % (module, name)


def pause_handlers(changes):
    """Take the session's frame-change, depsgraph-update and render handlers out until `changes` is restored (they
    go back in their order, in front of any added meanwhile). Returns {list name: [handler names]} of those paused."""
    paused = {}
    for name in HANDLER_LISTS:
        handlers = getattr(bpy.app.handlers, name, None)
        if not handlers:
            continue
        saved = list(handlers)
        handlers.clear()

        def back(handlers=handlers, saved=saved):
            added = [h for h in handlers if h not in saved]
            handlers.clear()
            handlers.extend(saved + added)
        changes.call(back)
        paused[name] = [_handler_name(h) for h in saved]
    return paused


# ----------------------------------------------------------------------------------------------
# The fingerprint: what the session looks like, to prove it was put back.

def _plain(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bpy.types.ID):
        return "ID:" + value.name_full
    if isinstance(value, (set, frozenset)):
        return tuple(sorted(str(v) for v in value))
    if isinstance(value, bpy.types.bpy_struct):
        return "<%s>" % type(value).__name__
    try:
        return tuple(_plain(v) for v in value)
    except TypeError:
        return repr(value)


def _same(a, b, tol=1e-5):
    if isinstance(a, float) or isinstance(b, float):
        try:
            fa, fb = float(a), float(b)
        except (TypeError, ValueError):
            return a == b
        if math.isnan(fa) and math.isnan(fb):
            return True
        return abs(fa - fb) <= tol * max(1.0, abs(fa), abs(fb))
    if isinstance(a, tuple) and isinstance(b, tuple):
        return len(a) == len(b) and all(_same(x, y, tol) for x, y in zip(a, b))
    return a == b


_SKIP_PROPS = {"rna_type", "pixels", "bl_rna", "is_dirty", "is_evaluated", "original", "users", "session_uid",
               "perspective_matrix", "frame_current_final"}


def _walk(fp, key, struct_, depth=0, seen=None):
    """Every plain setting of an RNA struct (and its nested settings, `depth` levels down) into fp."""
    if struct_ is None:
        fp[key] = None
        return
    seen = set() if seen is None else seen
    try:
        pointer = struct_.as_pointer()
    except Exception:
        pointer = id(struct_)
    if pointer in seen:
        return
    seen.add(pointer)
    for prop in struct_.bl_rna.properties:
        name = prop.identifier
        if name in _SKIP_PROPS or prop.type == "COLLECTION":
            continue
        try:
            value = getattr(struct_, name)
        except Exception:
            continue
        if prop.type == "POINTER":
            if value is None:
                fp[key + "." + name] = None
            elif isinstance(value, bpy.types.ID):
                fp[key + "." + name] = "ID:" + value.name_full
            elif depth > 0:
                _walk(fp, key + "." + name, value, depth - 1, seen)
        else:
            fp[key + "." + name] = _plain(value)


def _layer_collections(fp, key, lc):
    fp[key] = (lc.exclude, lc.hide_viewport, getattr(lc, "holdout", None), getattr(lc, "indirect_only", None))
    for child in lc.children:
        _layer_collections(fp, key + "/" + child.name, child)


def _action_fcurves(action, slot):
    """The F-curves of one action for one slot (layered actions in 4.4+, plain ones before)."""
    if action is None:
        return []
    if slot is not None:
        try:
            from bpy_extras import anim_utils
            bag = anim_utils.action_get_channelbag_for_slot(action, slot)
            if bag is not None:
                return list(bag.fcurves)
        except Exception:
            pass
    return list(getattr(action, "fcurves", None) or [])


def _nla_strips(adt):
    """Every strip of every NLA track, meta strips opened."""
    out = []

    def walk(strips):
        for strip in strips:
            out.append(strip)
            inner = getattr(strip, "strips", None)
            if inner is not None and len(inner):
                walk(inner)
    for track in getattr(adt, "nla_tracks", None) or []:
        walk(track.strips)
    return out


def _fcurves(adt):
    """The F-curves that animate an AnimData: its active action's and those of every NLA strip's action (a channel
    animated only through the NLA is overwritten by a frame change too)."""
    out, seen = [], set()
    sources = [(getattr(adt, "action", None), getattr(adt, "action_slot", None))]
    sources += [(strip.action, getattr(strip, "action_slot", None)) for strip in _nla_strips(adt)
                if getattr(strip, "action", None) is not None]
    for action, slot in sources:
        for fc in _action_fcurves(action, slot):
            key = (fc.data_path, fc.array_index)
            if key not in seen:
                seen.add(key)
                out.append(fc)
    return out


def _is_animated(adt):
    return adt is not None and (adt.action is not None or any(len(t.strips) for t in adt.nla_tracks))


def _animated_ids():
    for coll in (bpy.data.objects, bpy.data.materials, bpy.data.shape_keys, bpy.data.worlds, bpy.data.node_groups,
                 bpy.data.armatures, bpy.data.cameras, bpy.data.lights, bpy.data.scenes, bpy.data.meshes,
                 bpy.data.curves):
        for idb in coll:
            if idb.library is not None:
                continue
            adt = getattr(idb, "animation_data", None)
            if _is_animated(adt):
                yield idb, adt
            tree = getattr(idb, "node_tree", None)
            tadt = getattr(tree, "animation_data", None) if tree is not None else None
            if _is_animated(tadt):
                yield tree, tadt


def _python_drivers():
    """'owner: data_path' of every driver whose expression needs Python (not a simple expression): with script
    auto-run off Blender does not run them."""
    out = []
    for coll in (bpy.data.objects, bpy.data.meshes, bpy.data.shape_keys, bpy.data.materials, bpy.data.armatures,
                 bpy.data.cameras, bpy.data.lights, bpy.data.worlds, bpy.data.scenes, bpy.data.curves,
                 bpy.data.node_groups):
        for idb in coll:
            owners = [idb]
            tree = getattr(idb, "node_tree", None)
            if tree is not None:
                owners.append(tree)
            for owner in owners:
                adt = getattr(owner, "animation_data", None)
                if adt is None:
                    continue
                for fc in adt.drivers:
                    drv = fc.driver
                    if drv.type == "SCRIPTED" and not drv.is_simple_expression:
                        out.append("%s: %s" % (idb.name_full, fc.data_path))
    return out


def keyed_range():
    """(first, last) frame over every key and NLA strip in the file, or None when nothing is keyed."""
    lo, hi = None, None
    for _idb, adt in _animated_ids():
        spans = []
        for fc in _action_fcurves(adt.action, getattr(adt, "action_slot", None)):
            if len(fc.keyframe_points):
                spans.append(tuple(fc.range()))
        for strip in _nla_strips(adt):
            spans.append((strip.frame_start, strip.frame_end))
        for a, b in spans:
            lo = a if lo is None else min(lo, a)
            hi = b if hi is None else max(hi, b)
    return None if lo is None else (lo, hi)


def snapshot_animated():
    """The current (authored) value of every animated property: a frame change overwrites unkeyed edits with the keys."""
    out = []
    for idb, adt in _animated_ids():
        for fc in _fcurves(adt):
            try:
                value = idb.path_resolve(fc.data_path)
            except Exception:
                continue
            index = fc.array_index
            if hasattr(value, "__len__") and not isinstance(value, str):
                try:
                    value = value[index]
                except Exception:
                    continue
            if isinstance(value, (bool, int, float)):
                out.append((idb, fc.data_path, index, value))
    return out


def _set_path(idb, path, index, value):
    current = idb.path_resolve(path)
    if hasattr(current, "__len__") and not isinstance(current, str):
        current[index] = value
        return
    if path.endswith("]") and path.rfind("[") > path.rfind("."):
        cut = path.rfind("[")
        owner = idb.path_resolve(path[:cut]) if cut else idb
        owner[json.loads(path[cut + 1:-1])] = value
        return
    owner_path, _, attr = path.rpartition(".")
    owner = idb.path_resolve(owner_path) if owner_path else idb
    setattr(owner, attr, value)


def restore_animated(snapshot):
    """Write back every snapshotted value that the frame change replaced. Returns how many were written."""
    written = 0
    for idb, path, index, value in snapshot:
        try:
            current = idb.path_resolve(path)
            if hasattr(current, "__len__") and not isinstance(current, str):
                current = current[index]
            if _same(current, value, 1e-7):
                continue
            _set_path(idb, path, index, value)
            written += 1
        except Exception:
            continue
    return written


def _crc(value):
    return zlib.crc32(repr(value).encode("utf-8", "backslashreplace")) & 0xFFFFFFFF


def _tree_values(tree):
    """A hash of every node's unlinked input values and settings in a node tree (None without a tree)."""
    if tree is None:
        return None
    parts = []
    for node in tree.nodes:
        parts.append((node.name, node.bl_idname, node.mute))
        for sock in node.inputs:
            if not sock.is_linked and hasattr(sock, "default_value"):
                parts.append((sock.identifier, _plain(sock.default_value)))
        for prop in ("blend_type", "operation", "data_type", "interpolation", "image", "color_ramp"):
            value = getattr(node, prop, None)
            if value is None:
                continue
            if prop == "color_ramp":
                value = [(e.position, tuple(e.color)) for e in value.elements]
            parts.append((prop, _plain(value)))
    parts.append(tuple((link.from_node.name, link.from_socket.identifier, link.to_node.name, link.to_socket.identifier)
                       for link in tree.links))
    return _crc(parts)


def _idprops(idb):
    """Custom properties of a datablock (cheap when it has none), as a hash."""
    try:
        keys = idb.keys()
    except Exception:
        return None
    if not keys:
        return None
    items = []
    for k in sorted(keys):
        value = idb[k]
        for conv in ("to_dict", "to_list"):
            if hasattr(value, conv):
                value = getattr(value, conv)()
                break
        items.append((k, repr(value)))
    return _crc(items)


def _content(fp, scene):
    """What the datablocks hold, beyond their names: the 3D cursor, tool settings, cameras, lights, armatures,
    materials, worlds, meshes, images, texts, custom properties, and which datablock each name is."""
    cur = scene.cursor
    fp["scene.cursor"] = _plain((cur.location, cur.rotation_mode, cur.rotation_euler, cur.rotation_quaternion))
    _walk(fp, "scene.tool_settings", scene.tool_settings, 0)
    for cam in bpy.data.cameras:
        _walk(fp, "camera[%s]" % cam.name_full, cam, 0)
    for light in bpy.data.lights:
        _walk(fp, "light[%s]" % light.name_full, light, 0)
        fp["light[%s].nodes" % light.name_full] = _tree_values(getattr(light, "node_tree", None))
    for arm in bpy.data.armatures:
        _walk(fp, "armature[%s]" % arm.name_full, arm, 0)
    for mat in bpy.data.materials:
        fp["material[%s]" % mat.name_full] = (_plain((mat.diffuse_color, mat.metallic, mat.roughness,
                                                      getattr(mat, "blend_method", None))),
                                              _tree_values(mat.node_tree))
    for world in bpy.data.worlds:
        fp["world[%s]" % world.name_full] = (_plain(world.color), _tree_values(world.node_tree))
    hashed = 0
    for me in bpy.data.meshes:
        n = len(me.vertices)
        coords = None
        if n and hashed + n <= MAX_HASHED_VERTICES:
            arr = np.empty(n * 3, np.float32)
            me.vertices.foreach_get("co", arr)
            coords = zlib.crc32(arr.tobytes()) & 0xFFFFFFFF
            hashed += n
        fp["mesh[%s]" % me.name_full] = (n, len(me.edges), len(me.polygons), coords)
    for img in bpy.data.images:
        if img.type in ("RENDER_RESULT", "COMPOSITING"):
            continue
        fp["image[%s]" % img.name_full] = (img.colorspace_settings.name, img.alpha_mode, img.filepath, img.source,
                                           tuple(img.size), img.use_fake_user, img.packed_file is not None)
    for text in bpy.data.texts:
        fp["text[%s]" % text.name_full] = zlib.crc32(text.as_string().encode("utf-8", "surrogatepass")) & 0xFFFFFFFF
    for prop in bpy.data.bl_rna.properties:
        if prop.type != "COLLECTION" or prop.identifier in ("all_ids", "window_managers", "screens", "workspaces"):
            continue
        coll = getattr(bpy.data, prop.identifier, None)
        if coll is None:
            continue
        try:
            for idb in coll:
                value = _idprops(idb)
                if value is not None:
                    fp["custom_properties[%s|%s]" % (prop.identifier, idb.name_full)] = value
        except Exception:
            continue


def fingerprint(update=True):
    """A dict of everything this tool could touch (and more), for an equality check after the work."""
    win, scene, layer = _context()
    if update and layer is not None:
        try:
            layer.update()
        except Exception:
            pass
    fp = {}
    fp["context.mode"] = bpy.context.mode
    fp["context.scene"] = scene.name if scene else None
    fp["context.view_layer"] = layer.name if layer else None
    _walk(fp, "scene", scene, 0)
    fp["scene.frame"] = (scene.frame_current, round(scene.frame_subframe, 6))
    _walk(fp, "scene.render", scene.render, 2)
    _walk(fp, "scene.display", scene.display, 1)
    for name in ("eevee", "view_settings", "display_settings", "cycles", "unit_settings"):
        if hasattr(scene, name):
            _walk(fp, "scene." + name, getattr(scene, name), 0)
    for vl in scene.view_layers:
        _walk(fp, "view_layer[%s]" % vl.name, vl, 0)
        _layer_collections(fp, "layer_collection[%s]" % vl.name, vl.layer_collection)
    for coll in bpy.data.collections:
        fp["collection[%s]" % coll.name_full] = (coll.hide_viewport, coll.hide_render, coll.hide_select)
    in_layer = set(layer.objects) if layer is not None else set()
    for ob in bpy.data.objects:
        key = "object[%s]" % ob.name_full
        shown = ob in in_layer
        fp[key] = (
            ob.select_get(view_layer=layer) if shown else None,
            ob.hide_get(view_layer=layer) if shown else None,
            ob.hide_viewport, ob.hide_render, ob.hide_select, ob.show_in_front, ob.display_type,
            ob.data.name_full if ob.data is not None else None, ob.parent.name_full if ob.parent else None,
            ob.mode, ob.active_material_index,
        )
        fp[key + ".display"] = _plain((ob.color, ob.show_wire, ob.show_name, ob.show_axis, ob.show_bounds,
                                       ob.display_bounds_type, ob.show_texture_space, ob.show_all_edges))
        fp[key + ".authored"] = _plain((ob.location, ob.rotation_mode, ob.rotation_euler, ob.rotation_quaternion,
                                        ob.rotation_axis_angle, ob.scale))
        # Only an object the depsgraph evaluates has a current matrix: one outside the view layer or hidden with
        # hide_viewport keeps whatever it had (identity after loading) until something evaluates it.
        if shown and ob.visible_get(view_layer=layer):
            fp[key + ".matrix_world"] = _plain(ob.matrix_world)
        if ob.modifiers:
            fp[key + ".modifiers"] = tuple((m.name, m.show_viewport, m.show_render) for m in ob.modifiers)
        if ob.type == "ARMATURE" and ob.pose is not None and len(ob.pose.bones):
            bones = ob.pose.bones
            for prop, size in (("location", 3), ("rotation_quaternion", 4), ("rotation_euler", 3), ("scale", 3)):
                arr = np.empty(len(bones) * size, np.float32)
                bones.foreach_get(prop, arr)
                fp[key + ".pose." + prop] = tuple(np.round(arr.astype(np.float64), 5).tolist())
    for keys in bpy.data.shape_keys:
        blocks = keys.key_blocks
        if len(blocks):
            arr = np.empty(len(blocks), np.float32)
            blocks.foreach_get("value", arr)
            fp["shape_keys[%s]" % keys.name_full] = tuple(np.round(arr.astype(np.float64), 6).tolist())
    for idb, path, index, value in snapshot_animated():
        fp["animated[%s|%s|%s|%d]" % (type(idb).__name__, idb.name_full if isinstance(idb, bpy.types.ID) else idb.name,
                                      path, index)] = _plain(value)
    _content(fp, scene)
    fp["active_object"] = layer.objects.active.name_full if layer is not None and layer.objects.active else None
    wm = bpy.context.window_manager
    for wi, window in enumerate(wm.windows if wm else []):
        wkey = "window%d" % wi
        screen = window.screen
        fp[wkey] = (screen.name if screen else None, window.scene.name if window.scene else None,
                    window.view_layer.name if window.view_layer else None,
                    window.workspace.name if window.workspace else None)
        for ai, area in enumerate(screen.areas if screen else []):
            akey = "%s.area%d" % (wkey, ai)
            fp[akey] = (area.type, area.ui_type)
            space = area.spaces.active
            if space is None:
                continue
            if area.type == "VIEW_3D":
                _walk(fp, akey + ".shading", space.shading, 0)
                _walk(fp, akey + ".overlay", space.overlay, 0)
                r3d = space.region_3d
                if r3d is not None:
                    for name in ("view_matrix", "window_matrix", "view_perspective", "view_location", "view_rotation",
                                 "view_distance", "view_camera_zoom", "view_camera_offset", "is_perspective",
                                 "lock_rotation", "use_box_clip", "use_clip_planes"):
                        fp[akey + ".r3d." + name] = _plain(getattr(r3d, name, None))
                fp[akey + ".space"] = _plain((space.camera, space.lock_camera, space.show_region_ui,
                                              space.show_region_toolbar, space.show_region_header,
                                              space.local_view is not None, space.use_local_camera,
                                              space.lens, space.clip_start, space.clip_end,
                                              getattr(space, "use_local_collections", None)))
                if space.local_view is not None and window.view_layer is not None:
                    fp[akey + ".local_view_objects"] = frozenset(
                        o.name_full for o in window.view_layer.objects if o.local_view_get(space))
            elif area.type == "PROPERTIES":
                fp[akey + ".space"] = _plain((space.context, getattr(space, "use_pin_id", None),
                                              getattr(space, "pin_id", None)))
            elif area.type == "IMAGE_EDITOR":
                fp[akey + ".space"] = _plain((space.image, space.mode if hasattr(space, "mode") else None))
            for region in area.regions:
                if region.type == "UI":
                    try:
                        fp[akey + ".ui_tab"] = region.active_panel_category
                    except Exception:
                        pass
    prefs = bpy.context.preferences
    fp["preferences.active_section"] = prefs.active_section
    fp["preferences.state"] = _plain((prefs.view.ui_scale, prefs.is_dirty))
    if wm is not None and hasattr(wm, "addon_search"):
        fp["wm.addon_search"] = wm.addon_search
    for prop in bpy.data.bl_rna.properties:
        if prop.type != "COLLECTION" or prop.identifier in ("all_ids", "window_managers"):
            continue
        coll = getattr(bpy.data, prop.identifier, None)
        if coll is None:
            continue
        try:
            items = [item for item in coll if not (prop.identifier == "images" and item.type == "RENDER_RESULT")]
            names = frozenset(item.name_full for item in items)
            # which datablock each name is: one removed and another made under the same name is a change
            identity = _crc(sorted((item.name_full, item.as_pointer()) for item in items))
        except Exception:
            continue
        fp["data." + prop.identifier] = names
        fp["data.%s (identity)" % prop.identifier] = identity
    return fp


FINGERPRINT_COVERS = ("render, display, EEVEE and colour settings", "view layers and collection flags",
                      "every object's visibility, selection, transforms, display settings and pose",
                      "animated values (actions and NLA)", "shape keys", "3D views: shading, overlays, view, local view",
                      "editors and tabs", "frame, mode, active object", "3D cursor and tool settings",
                      "cameras, lights, armatures, materials, worlds, meshes, images, texts (contents)",
                      "custom properties", "name and identity of every datablock", "preferences section and UI scale")


def fingerprint_diff(before, after, limit=60):
    """Readable lines for every difference between two fingerprints."""
    out = []
    for key in sorted(set(before) | set(after)):
        if key not in after:
            out.append("%s: was %s, now missing" % (key, _short(before[key])))
        elif key not in before:
            out.append("%s: new, %s" % (key, _short(after[key])))
        else:
            a, b = before[key], after[key]
            if key.endswith(".ui_tab") and a == "UNSUPPORTED":
                continue        # a sidebar never drawn has no tab; drawing it once picks one, and nothing sets it back
            if isinstance(a, frozenset) or isinstance(b, frozenset):
                if a != b:
                    added, removed = sorted(set(b) - set(a)), sorted(set(a) - set(b))
                    out.append("%s: %s%s" % (key, ("added " + ", ".join(added[:8])) if added else "",
                                             ("; removed " + ", ".join(removed[:8])) if removed else ""))
            elif not _same(a, b):
                out.append("%s: was %s, now %s" % (key, _short(a), _short(b)))
        if len(out) >= limit:
            out.append("... (more)")
            break
    return out


def _short(value, room=90):
    text = repr(value)
    return text if len(text) <= room else text[:room] + "..."


# ----------------------------------------------------------------------------------------------
# Where things are.

def _context():
    wm = bpy.context.window_manager
    window = bpy.context.window
    if window is None and wm is not None and len(wm.windows):
        window = wm.windows[0]
    scene = window.scene if window is not None else bpy.context.scene
    layer = window.view_layer if window is not None else bpy.context.view_layer
    return window, scene, layer


class View3D:
    def __init__(self, window, area, space, region):
        self.window, self.area, self.space, self.region = window, area, space, region


def largest_view3d():
    best = None
    wm = bpy.context.window_manager
    for window in (wm.windows if wm is not None else []):
        screen = window.screen
        if screen is None or screen.name.startswith("temp"):
            continue
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            region = next((r for r in area.regions if r.type == "WINDOW"), None)
            if region is None or region.width < 2 or region.height < 2:
                continue
            size = area.width * area.height
            if best is None or size > best[0]:
                best = (size, View3D(window, area, area.spaces.active, region))
    return best[1] if best else None


def check_identity(args):
    """Refuse before anything is touched when the Blender that answers is not the one meant."""
    pid = args.get("expect_pid")
    if pid not in (None, "", 0) and int(pid) != os.getpid():
        raise LookError("refused", "WRONG BLENDER, nothing was run: this is pid %d, not %s (file %r)"
                        % (os.getpid(), pid, bpy.data.filepath))
    want = args.get("expect_file")
    if want:
        have = bpy.data.filepath
        if os.sep in want or "/" in want:
            same = bool(have) and same_file(have, want)
        else:
            same = os.path.normcase(os.path.basename(have)) == os.path.normcase(want)
        if not same:
            raise LookError("refused", "WRONG BLENDER, nothing was run: it has %r open, not %r" % (have or "(unsaved)", want))


def _engine_eevee(scene):
    for name in EEVEE_IDS:
        try:
            old = scene.render.engine
            scene.render.engine = name
            scene.render.engine = old
            return name
        except TypeError:
            continue
    return EEVEE_IDS[0]


# ----------------------------------------------------------------------------------------------
# Framing.

def _basis(direction):
    d = Vector(direction).normalized()
    up = Vector((0.0, 0.0, 1.0)) if abs(d.z) < 0.999 else Vector((0.0, 1.0, 0.0))
    forward = -d
    right = forward.cross(up).normalized()
    upv = right.cross(forward).normalized()
    return right, upv, d


def perspective_matrix(fov_y, aspect, near, far):
    f = 1.0 / math.tan(fov_y / 2.0)
    return Matrix(((f / aspect, 0, 0, 0), (0, f, 0, 0),
                   (0, 0, (far + near) / (near - far), 2 * far * near / (near - far)), (0, 0, -1, 0)))


def orthographic_matrix(half_h, aspect, near, far):
    return Matrix(((1.0 / (half_h * aspect), 0, 0, 0), (0, 1.0 / half_h, 0, 0),
                   (0, 0, -2.0 / (far - near), -(far + near) / (far - near)), (0, 0, 0, 1)))


class ViewSpec:
    """A view to draw: matrices, plus what the render fallback needs to place a camera."""

    def __init__(self, **kw):
        self.kind = kw.pop("kind", "fit")
        self.__dict__.update(kw)

    def describe(self):
        out = {"kind": self.kind}
        for name in ("eye", "center", "distance", "ortho", "fov_deg", "half_height", "near", "far", "direction"):
            if hasattr(self, name) and getattr(self, name) is not None:
                value = getattr(self, name)
                out[name] = [round(float(v), 5) for v in value] if hasattr(value, "__len__") else (
                    round(float(value), 5) if isinstance(value, float) else value)
        if hasattr(self, "view_matrix"):
            out["view_matrix"] = [[round(float(v), 6) for v in row] for row in self.view_matrix]
            out["projection_matrix"] = [[round(float(v), 6) for v in row] for row in self.proj_matrix]
        return out


def fit_view(points, direction, aspect, fov_y, margin, ortho=False, distance=None, center=None):
    """The eye that shows every point, filling `margin` of the tile along its tighter side.

    Fitted on the points themselves (evaluated vertices), not on a bounding sphere; then panned
    so the projected extent is centred (unless `center` is given, which stays in the middle).
    """
    pts = np.asarray(points, np.float64).reshape(-1, 3)
    if not len(pts):
        pts = np.zeros((1, 3))
    r, u, d = _basis(direction)
    basis = np.array([tuple(r), tuple(u), tuple(d)], np.float64)
    pan = center is None
    c = (pts.min(0) + pts.max(0)) / 2.0 if pan else np.asarray(center, np.float64)
    ty = math.tan(fov_y / 2.0)
    tx = ty * aspect
    span = float(np.linalg.norm(pts.max(0) - pts.min(0))) or 1.0

    def solve(c):
        rel = (pts - c) @ basis.T
        x, y, z = rel[:, 0], rel[:, 1], rel[:, 2]
        if ortho:
            half_h = max(np.abs(y).max() / margin, np.abs(x).max() / margin / aspect, 1e-4)
            sx, sy = (x.max() + x.min()) / 2.0, (y.max() + y.min()) / 2.0
            return half_h, sx, sy, z
        if distance:
            dist = float(distance)
        else:
            dist = float(max(np.max(z + np.abs(x) / (margin * tx)), np.max(z + np.abs(y) / (margin * ty))))
            dist = max(dist, float(z.max()) + 1e-3, 1e-3)
        depth = dist - z
        ok = depth > 1e-9
        if not ok.any():
            return dist, 0.0, 0.0, z
        nx, ny = x[ok] / (depth[ok] * tx), y[ok] / (depth[ok] * ty)
        return dist, (nx.max() + nx.min()) / 2.0 * dist * tx, (ny.max() + ny.min()) / 2.0 * dist * ty, z

    for _ in range(8 if pan else 0):
        size, sx, sy, _z = solve(c)
        if abs(sx) + abs(sy) < 1e-6 * span:
            break
        c = c + basis[0] * sx + basis[1] * sy
    size, _sx, _sy, z = solve(c)
    cv = Vector(c.tolist())
    if ortho:
        back = float(z.max()) + span + 1.0
        eye = cv + d * back
        near, far = 0.01, back + span * 2.0 + 1.0
        proj = orthographic_matrix(size, aspect, near, far)
        half_h, dist = size, back
    else:
        dist = size
        eye = cv + d * dist
        depth = dist - z
        dmin, dmax = float(depth.min()), float(depth.max())
        near = max(min(dmin * 0.5, dist * 0.5), dist * 1e-4, 1e-4)
        far = max(dmax * 4.0, dist * 4.0, near * 10.0)
        proj = perspective_matrix(fov_y, aspect, near, far)
        half_h = None
    cam = Matrix(((r.x, u.x, d.x, eye.x), (r.y, u.y, d.y, eye.y), (r.z, u.z, d.z, eye.z), (0, 0, 0, 1)))
    return ViewSpec(kind="fit", view_matrix=cam.inverted(), proj_matrix=proj, cam_matrix=cam, eye=tuple(eye),
                    center=tuple(cv), distance=float(dist), ortho=bool(ortho), fov_deg=math.degrees(fov_y),
                    half_height=half_h, near=float(near), far=float(far), direction=tuple(d), aspect=aspect)


def gather_points(objects, depsgraph, notes=None):
    """World positions that outline the objects as drawn now: evaluated vertices, bone ends, box corners."""
    chunks = []
    wanted = set(objects)
    for ob in objects:
        ev = ob.evaluated_get(depsgraph)
        mw = np.array(ev.matrix_world, np.float64)
        if ob.type in MESHLIKE:
            mesh = None
            try:
                mesh = ev.to_mesh()
            except Exception:
                mesh = None
            if mesh is not None:
                try:
                    n = len(mesh.vertices)
                    if n:
                        co = np.empty(n * 3, np.float32)
                        mesh.vertices.foreach_get("co", co)
                        co = co.reshape(n, 3).astype(np.float64)
                        if n > MAX_FIT_POINTS:
                            co = co[::int(math.ceil(n / MAX_FIT_POINTS))]
                        chunks.append(co @ mw[:3, :3].T + mw[:3, 3])
                finally:
                    ev.to_mesh_clear()
                continue
        if ob.type == "ARMATURE" and ev.pose is not None:
            ends = [tuple(pb.head) for pb in ev.pose.bones] + [tuple(pb.tail) for pb in ev.pose.bones]
            if ends:
                chunks.append(np.array(ends, np.float64) @ mw[:3, :3].T + mw[:3, 3])
            continue
        if ob.type in GEOMETRY:
            corners = np.array([tuple(c) for c in ev.bound_box], np.float64)
            chunks.append(corners @ mw[:3, :3].T + mw[:3, 3])
        elif ob.instance_type == "NONE" or ob.type not in ("EMPTY",):
            chunks.append(mw[:3, 3].reshape(1, 3))
    # Instances (collection instances, particles, geometry nodes): sampled evenly over all of them, so a big
    # scatter is framed whole. The stride doubles whenever the sample is full.
    stride, total, kept = 1, 0, []
    boxes = {}
    for inst in depsgraph.object_instances:
        if not inst.is_instance:
            continue
        parent = inst.parent.original if inst.parent is not None else None
        if parent not in wanted:
            continue
        total += 1
        if (total - 1) % stride:
            continue
        obj = inst.object
        key = (obj.name, obj.data.as_pointer() if getattr(obj, "data", None) is not None else 0)
        if key not in boxes:
            boxes[key] = np.array([tuple(c) for c in obj.bound_box], np.float64)
        kept.append((np.array(inst.matrix_world, np.float64), key))
        if len(kept) >= MAX_FIT_INSTANCES:
            kept = kept[::2]
            stride *= 2
    groups = {}
    for m, k in kept:
        groups.setdefault(k, []).append(m)
    for key, mats in groups.items():
        mats = np.array(mats)
        corners = boxes[key]
        pts = np.einsum("nij,kj->nki", mats[:, :3, :3], corners) + mats[:, None, :3, 3]
        chunks.append(pts.reshape(-1, 3))
    if notes is not None and stride > 1:
        notes.append("framing used 1 in %d of the %d instances (evenly spread)" % (stride, total))
    return np.concatenate(chunks) if chunks else np.zeros((0, 3))


def project(points, view_matrix, proj_matrix, w, h):
    """Pixel x, y (top-first) and a mask of points in front of the eye."""
    pts = np.asarray(points, np.float64).reshape(-1, 3)
    m = np.array(proj_matrix, np.float64) @ np.array(view_matrix, np.float64)
    hom = np.c_[pts, np.ones(len(pts))] @ m.T
    wv = hom[:, 3]
    front = wv > 1e-9
    safe = np.where(front, wv, 1.0)
    x = (hom[:, 0] / safe * 0.5 + 0.5) * w
    y = (1.0 - (hom[:, 1] / safe * 0.5 + 0.5)) * h
    return x, y, front


# ----------------------------------------------------------------------------------------------
# The job.

def _num(value, default):
    return default if value in (None, "") else value


class Job:
    """One look: checks, a plan of tiles, the tiles made one at a time, the pictures written."""

    def __init__(self, args):
        self.t0 = time.perf_counter()
        self.args = dict(args)
        check_identity(self.args)               # first: nothing is read or changed for the wrong Blender
        self.warnings = []
        self.timing = {}
        self.window, self.scene, self.layer = _context()
        if self.scene is None or self.layer is None:
            raise LookError("failed", "no scene or view layer in this Blender")
        self.background = bool(bpy.app.background)
        self.own = bool(self.args.get("own_process"))
        want = self.args.get("expect_file")
        if want and not (os.sep in want or "/" in want):
            self.warnings.append("--expect-file %r was matched by file name only (a file of that name in any folder "
                                 "would pass); give the full path to be strict" % want)
        self.mode = self.args.get("mode") or ("angles" if self.background else "viewport")
        if self.mode not in MODES:
            raise LookError("bad_args", "unknown mode %r; use one of %s" % (self.mode, ", ".join(MODES)))
        if not self.args.get("mode") and self.background and not self.own:
            self.warnings.append("no --mode given and this Blender runs in the background (no viewport anyone sees): "
                                 "showing angles; give --mode to choose")
        self.shading = self.args.get("shading") or None
        if self.shading and self.shading not in SHADINGS:
            raise LookError("bad_args", "unknown shading %r; use one of %s" % (self.shading, ", ".join(SHADINGS)))
        self.max_size = self._clamped("max_size", 768, 64, 4096, int)
        self.sheet_max = self._clamped("sheet_max", 1536, 256, 8192, int)
        self.margin = self._clamped("fill", 0.85, 0.2, 0.98, float)
        self.fov = math.radians(self._clamped("fov", 35.0, 5.0, 120.0, float))
        self.ortho = bool(self.args.get("ortho"))
        distance = self.args.get("distance")
        if distance not in (None, "") and float(distance) <= 0:
            raise LookError("bad_args", "--distance must be more than 0 (Blender units from the targets' centre)")
        if distance not in (None, "") and self.ortho:
            raise LookError("bad_args", "--distance has no effect with --ortho (an orthographic view has no distance); "
                            "leave one of them out")
        self._out_paths()
        t = time.perf_counter()
        self.fp_before = fingerprint()
        self.timing["fingerprint_s"] = round(time.perf_counter() - t, 4)
        self.dirty_before = bool(bpy.data.is_dirty)
        self.job_changes = Changes()
        self.tasks, self.tiles, self.done = [], [], 0
        self.task_problems = []
        self.state = {}
        self.dv = None
        self.paused = {}
        self.run_handlers = bool(self.args.get("run_handlers"))
        planning = Changes()
        try:
            if not self.run_handlers:
                self._note_paused(pause_handlers(planning))
            self._targets()
            self._backend()
            self._plan()
            drawn = sum(1 for task in self.tasks if task["kind"] in ("draw", "image", "ui"))
            if drawn > MAX_TILES:
                raise LookError("bad_args", "%d pictures asked for; at most %d" % (drawn, MAX_TILES))
        except Exception:
            planning.restore()
            self.job_changes.restore()
            raise
        planning.restore()
        if self.own and not self.args.get("autoexec", True):
            blocked = _python_drivers()
            if blocked:
                self.warnings.append("%d Python driver(s) did not run (script auto-run is off in this headless Blender, "
                                     "so the values they drive stay put): %s; pass --enable-autoexec for a file you "
                                     "trust" % (len(blocked), ", ".join(blocked[:5])))
        # force_deferred: tests only, to drive the bridge's deferred path from a background Blender.
        self.deferred = ((not self.background or bool(self.args.get("force_deferred")))
                         and self.args.get("deferred", True) is not False and len(self.tasks) > 1)

    # -- setup ------------------------------------------------------------------------------

    def _clamped(self, key, default, low, high, kind):
        value = kind(_num(self.args.get(key), default))
        if not (low <= value <= high):
            clamped = max(low, min(value, high))
            self.warnings.append("%s %s is out of range (%s to %s): %s was used" % (key, value, low, high, clamped))
            return clamped
        return value

    def _note_paused(self, paused):
        for name, handlers in paused.items():
            known = self.paused.setdefault(name, [])
            known.extend(h for h in handlers if h not in known)

    def _out_paths(self):
        out = self.args.get("out") or ""
        if not out:
            raise LookError("bad_args", "no --out folder or .png")
        out = os.path.abspath(bpy.path.abspath(out))
        self.out_is_png = out.lower().endswith(".png")
        if self.out_is_png:
            folder, stem = os.path.dirname(out), os.path.splitext(os.path.basename(out))[0]
            self.sheet_path = out
        else:
            folder, stem = out, "look_" + self.mode
            self.sheet_path = os.path.join(folder, stem + "_sheet.png")
        if bpy.data.filepath and same_file(folder, os.path.dirname(os.path.abspath(bpy.data.filepath))):
            raise LookError("refused", "--out is the folder of the open .blend (%s); pictures go elsewhere" % folder)
        os.makedirs(long_path(folder), exist_ok=True)
        self.folder, self.stem = folder, stem
        self.result_path = os.path.join(folder, stem + "_result.json")
        # Files Blender itself writes and reads back (renders, screenshots) go through its own file
        # functions, which stop at 259 characters on Windows: they get a short folder of their own.
        tmp = self.args.get("tmp_dir") or folder
        if os.name == "nt" and len(os.path.abspath(tmp)) >= 220:
            tmp = tempfile.gettempdir()
        os.makedirs(tmp, exist_ok=True)
        self.tmp = tmp

    def _targets(self):
        layer = self.layer
        in_layer = set(layer.objects)
        names = [n for n in (self.args.get("targets") or []) if n]
        info = {"requested": names, "framed": [], "hidden": [], "excluded": [], "linked": [], "unhidden": []}
        if names:
            missing = [n for n in names if bpy.data.objects.get(n) is None and bpy.data.collections.get(n) is None]
            if missing:
                raise LookError("bad_args", "no object or collection named " + ", ".join(repr(m) for m in missing))
            chosen = []
            for name in names:
                ob = bpy.data.objects.get(name)
                if ob is not None:            # an object first; a collection of the same name only when none is
                    roots = [ob]
                else:
                    coll = bpy.data.collections[name]
                    roots = list(coll.all_objects)
                    info.setdefault("collections", {})[name] = [o.name_full for o in roots]
                for root in roots:
                    for o in [root] + list(root.children_recursive):
                        if o not in chosen:
                            chosen.append(o)
        else:
            chosen = [o for o in layer.objects if o.type in GEOMETRY or (o.type == "EMPTY" and o.instance_type != "NONE")]
        self.unhide = []
        targets = []
        for o in chosen:
            if o.library is not None or o.override_library is not None:
                info["linked"].append(o.name_full)
            if o not in in_layer:
                if names:
                    info["excluded"].append(o.name_full)
                continue
            if not o.visible_get(view_layer=layer):
                if not names:
                    continue
                if self.args.get("unhide") and (o.hide_get(view_layer=layer) or o.hide_viewport):
                    self.unhide.append(o)
                    info["unhidden"].append(o.name_full)
                    targets.append(o)
                else:
                    info["hidden"].append(o.name_full)
                continue
            targets.append(o)
        # A rig named as a target brings the meshes it deforms (an Armature modifier, parented or not): they are what
        # the rig moves, and --isolate keeps them.
        named_rigs = {o for o in targets if o.type == "ARMATURE"} if names else set()
        if named_rigs:
            for o in layer.objects:
                if o in targets or o.type != "MESH" or not o.visible_get(view_layer=layer):
                    continue
                if o.find_armature() in named_rigs:
                    targets.append(o)
                    info.setdefault("deformed_by_rig", []).append(o.name_full)
        if info["excluded"]:
            self.warnings.append("not drawn, not in the view layer (excluded collection or another scene): "
                                 + ", ".join(info["excluded"]))
        if info["hidden"]:
            self.warnings.append("not drawn, hidden in the viewport (use --unhide to show them for the picture): "
                                 + ", ".join(info["hidden"]))
        if info["linked"]:
            self.warnings.append("linked from a library (drawn, never written to): " + ", ".join(info["linked"]))
        self.targets = targets
        self.target_set = set(targets)
        info["framed"] = [o.name_full for o in targets if o.type not in ("LIGHT", "CAMERA", "SPEAKER", "LIGHT_PROBE")]
        self.target_info = info
        rigs = {o for o in targets if o.type == "ARMATURE"}
        for o in targets:
            for m in getattr(o, "modifiers", []):
                if m.type == "ARMATURE" and m.object is not None:
                    rigs.add(m.object)
        self.rigs = rigs

    def _backend(self):
        if self.mode in ("image", "ui"):
            self.backend = "none"
            return
        want = self.args.get("backend") or "auto"
        reason = None
        if self.background:
            try:
                import gpu
                if hasattr(gpu, "init"):
                    gpu.init()
                else:
                    reason = "this Blender (%s) has no gpu.init(): background viewport drawing needs 5.2 or later" \
                             % bpy.app.version_string
            except Exception as error:
                reason = "gpu.init() failed: %s" % error
        dv = largest_view3d() if reason is None else None
        if reason is None and dv is None:
            if self.background:
                dv = self._borrow_area()
            if dv is None:
                reason = "no 3D viewport in this Blender's screens"
        if reason is None:
            try:
                import gpu
                gpu.types.GPUOffScreen(8, 8).free()
            except Exception as error:
                reason = "offscreen drawing is not available: %s" % error
        if want == "draw" and reason:
            raise LookError("refused", "--backend draw: " + reason)
        if want == "render" or (want == "auto" and reason):
            if not self.background:
                raise LookError("refused", "the render backend only runs in a background Blender (it replaces the Render "
                                "Result the user sees); a windowed Blender draws its viewport instead")
            if self.mode == "viewport":
                pass   # refused later with its own reason
            self.backend = "render"
            if reason and want == "auto":
                self.warnings.append("drawn with the render fallback because " + reason)
            self.dv = largest_view3d()
            return
        self.backend = "draw"
        self.dv = dv

    def _borrow_area(self):
        """Background only: turn the largest area of the first screen into a 3D view for the length of the job."""
        wm = bpy.context.window_manager
        if wm is None or not len(wm.windows):
            return None
        window = wm.windows[0]
        areas = sorted(window.screen.areas, key=lambda a: a.width * a.height, reverse=True) if window.screen else []
        if not areas:
            return None
        area = areas[0]
        if not self.job_changes.set(area, "ui_type", "VIEW_3D"):
            return None
        self.warnings.append("the file's screen had no 3D viewport; one area was a 3D viewport for the picture")
        region = next((r for r in area.regions if r.type == "WINDOW"), None)
        return View3D(window, area, area.spaces.active, region) if region is not None else None

    # -- planning ---------------------------------------------------------------------------

    def _shading_label(self):
        chosen = self.state.get("shading") if hasattr(self, "state") else None
        if chosen or self.shading:
            return chosen or self.shading
        if self.backend == "draw" and self.dv is not None:
            return self.dv.space.shading.type.lower() + " (as set)"
        return "solid"

    def _shading_short(self):
        """The shading for a tile's label: the name alone ('as set' is in the JSON)."""
        return self._shading_label().replace(" (as set)", "")

    def _effective_shading(self):
        if self.shading:
            return self.shading
        if self.own or self.backend == "render":
            return "solid"
        return None

    def _parse_view(self, value):
        if isinstance(value, str):
            key = value.strip().lower()
            if key in ANGLES:
                return key, ANGLES[key]
            raise LookError("bad_args", "unknown view %r; use %s or x,y,z" % (value, ", ".join(ANGLES)))
        if isinstance(value, (list, tuple)) and len(value) == 3:
            vec = tuple(float(v) for v in value)
            if not any(vec):
                raise LookError("bad_args", "a view direction cannot be 0,0,0")
            return "(%s)" % ",".join("%g" % v for v in vec), vec
        raise LookError("bad_args", "a view is a name or three numbers, not %r" % (value,))

    def _plan(self):
        mode = self.mode
        shading = self._effective_shading()
        self.state["shading"] = shading
        if mode in ("viewport", "camera", "angles", "frames") and self.backend == "render" and shading is None:
            shading = self.state["shading"] = "solid"
        if mode == "viewport":
            if self.background or self.backend != "draw":
                fallback = self.args.get("fallback")
                why = ("this Blender runs in the background: no viewport is on any screen" if self.background
                       else "the viewport cannot be drawn here")
                if fallback in ("camera", "angles"):
                    self.warnings.append("viewport: %s; showing %s instead (--fallback)" % (why, fallback))
                    self.mode = mode = fallback
                    if self.shading is None:
                        self.state["shading"] = shading = "solid" if (self.own or self.backend == "render") else shading
                else:
                    raise LookError("refused", "viewport: %s. Use --fallback camera or --fallback angles, or "
                                    "--mode camera / angles." % why)
        if mode == "viewport":
            region = self.dv.region
            w, h = self._fit_size(region.width, region.height)
            self.tasks.append({"kind": "draw", "view": "viewport", "w": w, "h": h, "frame": None,
                               "label": "VIEWPORT | " + self._shading_short(), "slug": "viewport",
                               "shows": {"view": "viewport"}})
        elif mode == "camera":
            cam = self.scene.camera
            if cam is None:
                raise LookError("refused", "the scene has no camera; use --mode angles")
            w, h = self._camera_size()
            self.tasks.append({"kind": "draw", "view": "camera", "w": w, "h": h, "frame": None,
                               "label": "CAMERA %s | %s" % (cam.name, self._shading_short()), "slug": "camera",
                               "shows": {"view": "camera", "camera": cam.name_full}})
        elif mode == "angles":
            views = self.args.get("views") or list(DEFAULT_VIEWS)
            if len(views) > 6:
                raise LookError("bad_args", "at most 6 views, got %d" % len(views))
            parsed = [self._parse_view(v) for v in views]
            # Measured with --unhide / --isolate applied, as the pictures will be drawn: a target hidden with
            # hide_viewport is only evaluated (and only has a current matrix) while it is shown.
            measuring = Changes()
            try:
                self._visibility(measuring)
                depsgraph = bpy.context.evaluated_depsgraph_get()
                points = gather_points(self._framed(), depsgraph, self.warnings)
            finally:
                errors = measuring.restore()
                self.layer.update()
                if errors:
                    self.task_problems.append("undo errors after measuring: " + "; ".join(errors))
            if not len(points):
                self.warnings.append("nothing visible to frame; the views look at the world origin")
            size = self.max_size
            for name, vec in parsed:
                spec = fit_view(points, vec, 1.0, self.fov, self.margin, self.ortho, self.args.get("distance"))
                self.tasks.append({"kind": "draw", "view": spec, "w": size, "h": size, "frame": None,
                                   "label": "%s | %s" % (name.upper(), self._shading_short()),
                                   "slug": name.strip("()").replace(",", "_").replace(".", "p"),
                                   "shows": {"view": name, "direction": list(vec)}})
        elif mode == "frames":
            self._plan_frames()
        elif mode == "image":
            ref = self.args.get("image") or "Render Result"
            self.tasks.append({"kind": "image", "ref": ref, "file": self.args.get("image_file"), "slug": "image"})
        elif mode == "ui":
            spec = self.args.get("ui") or "window"
            if self.background:
                raise LookError("refused", "ui: this Blender runs in the background and has no window to screenshot. "
                                + UI_NEXT_STEP + (" (--fallback applies to --mode viewport only)"
                                                  if self.args.get("fallback") else ""))
            self.tasks.append({"kind": "ui", "spec": spec, "slug": "ui_" + spec.replace(":", "_").replace(" ", "_")})
        # said for an armature asked for by name (in a whole scene, rigs beside their meshes are no news)
        rigs = [o.name_full for o in self._framed() if o.type == "ARMATURE"] if self.target_info["requested"] else []
        generated = any(t["kind"] == "draw" and t["view"] != "viewport" for t in self.tasks)
        if rigs and generated and self.state.get("shading") != "xray" and not self.args.get("overlays"):
            self.warnings.append("%s: an armature's bones are drawn only with --shading xray (or --overlays); this "
                                 "shading shows the meshes it moves" % ", ".join(rigs[:4]))

    def _framed(self):
        return [o for o in self.targets if o.type not in ("LIGHT", "CAMERA", "SPEAKER", "LIGHT_PROBE")]

    def _fit_size(self, w, h):
        k = self.max_size / float(max(w, h))
        return max(16, int(round(w * k))), max(16, int(round(h * k)))

    def _camera_size(self):
        r = self.scene.render
        return self._fit_size(r.resolution_x * r.pixel_aspect_x, r.resolution_y * r.pixel_aspect_y)

    def _frame_list(self):
        scene = self.scene
        frames = [int(f) for f in (self.args.get("frames") or [])]
        if scene.use_preview_range:
            start, end, which = scene.frame_preview_start, scene.frame_preview_end, "preview range"
        else:
            start, end, which = scene.frame_start, scene.frame_end, "scene range"
        self.state["frame_range"] = {"range": [start, end], "which": which}
        if not frames:
            count = max(2, min(int(_num(self.args.get("frame_count"), 6)), MAX_TILES))
            if end <= start:
                frames = [start]
            else:
                frames = sorted({int(round(start + (end - start) * i / (count - 1))) for i in range(count)})
            self.state["frame_range"]["spread_over"] = True
        if len(frames) > MAX_TILES:
            raise LookError("bad_args", "at most %d frames, got %d" % (MAX_TILES, len(frames)))
        outside = [f for f in frames if f < start or f > end]
        if outside and len(outside) == len(frames):
            self.warnings.append("every frame asked for (%s) is outside the %s %d-%d"
                                 % (", ".join(str(f) for f in frames[:8]), which, start, end))
        keys = keyed_range()
        if keys is not None and len(frames) > 1:
            lo, hi = keys
            held = {min(max(float(f), lo), hi) for f in frames}
            if len(held) == 1:
                self.warnings.append("every frame asked for is beyond the keys in this file (keyed %g-%g): the tiles "
                                     "can only differ through drivers, constraints or simulations" % (lo, hi))
        elif keys is None and len(frames) > 1:
            self.warnings.append("nothing in this file has keys: the tiles can only differ through drivers, "
                                 "constraints or simulations")
        return frames

    def _plan_frames(self):
        frames = self._frame_list()
        self.state["frames"] = frames
        view = self.args.get("view")
        if view in (None, ""):
            view = "viewport" if (not self.background and self.backend == "draw") else "front"
        if view == "viewport" and (self.background or self.backend != "draw"):
            fallback = self.args.get("fallback")
            if fallback == "camera":
                view = "camera"
            elif fallback == "angles":
                view = "front"
            else:
                raise LookError("refused", "frames --view viewport: this Blender has no viewport anyone sees; use "
                                "--view camera, an angle, or --fallback camera")
            self.warnings.append("frames: viewport view not available; using %s (--fallback)" % view)
        if view == "camera" and self.scene.camera is None:
            raise LookError("refused", "frames --view camera: the scene has no camera")
        bones = [b for b in (self.args.get("center_bones") or []) if b]
        rig = None
        if bones:
            if view in ("camera", "viewport"):
                raise LookError("bad_args", "--center-bones needs an angle view (front, x,y,z ...), not %s" % view)
            name = self.args.get("armature")
            if name:
                rig = bpy.data.objects.get(name)
                if rig is None or rig.type != "ARMATURE":
                    raise LookError("bad_args", "no armature named %r" % name)
            else:
                rig = next((o for o in self.targets if o.type == "ARMATURE"), None) or next(iter(self.rigs), None)
                if rig is None:
                    raise LookError("bad_args", "--center-bones: no armature among the targets; name it with --armature")
            missing = [b for b in bones if b not in rig.pose.bones]
            if missing:
                raise LookError("bad_args", "%s has no bone %s" % (rig.name, ", ".join(repr(b) for b in missing)))
        self.state["rig"], self.state["bones"] = rig, bones
        label_view = view if isinstance(view, str) else None
        if view in ("camera", "viewport"):
            if view == "camera":
                w, h = self._camera_size()
            else:
                w, h = self._fit_size(self.dv.region.width, self.dv.region.height)
            for f in frames:
                self.tasks.append({"kind": "draw", "view": view, "w": w, "h": h, "frame": f,
                                   "label": "FRAME %d | %s | %s" % (f, view.upper(), self._shading_short()),
                                   "slug": "f%04d" % f, "shows": {"view": view, "frame": f}})
            return
        name, vec = self._parse_view(view)
        label_view = name
        self.state["direction"] = vec
        self.state["measured"] = {}
        for f in frames:
            self.tasks.append({"kind": "measure", "frame": f})
        self.tasks.append({"kind": "fit"})
        w, h = self.max_size, int(round(self.max_size * 0.75))
        self.state["tile"] = (w, h)
        for f in frames:
            self.tasks.append({"kind": "draw", "view": "fitted", "w": w, "h": h, "frame": f,
                               "label": "FRAME %d | %s | %s" % (f, label_view.upper(), self._shading_short()),
                               "slug": "f%04d" % f, "shows": {"view": label_view, "direction": list(vec), "frame": f,
                                                              "center_bones": bones or None}})

    # -- running ----------------------------------------------------------------------------

    def run_all(self):
        try:
            while self.done < len(self.tasks):
                self._run_next()
        except LookError as error:
            return self._fail(error.code, error.message)
        except Exception:
            return self._fail("failed", traceback.format_exc())
        return self.finish()

    def step(self):
        """For the bridge's deferred response: a few tiles a tick; None until finished, then the result."""
        try:
            budget = time.perf_counter() + float(_num(self.args.get("tick_budget"), 0.15))
            before = fingerprint()           # Blender's event loop ran since the last tick: check this tick alone
            self.tick_before = before
            labels = []
            while self.done < len(self.tasks):
                labels.append(self.tasks[self.done].get("label") or self.tasks[self.done]["kind"])
                self._run_next()
                if time.perf_counter() > budget:
                    break
            diff = fingerprint_diff(before, fingerprint())
            if diff:
                self.task_problems.append("after %s: %s" % (", ".join(labels), "; ".join(diff)))
            self.ticks = getattr(self, "ticks", 0) + 1
            if self.done < len(self.tasks):
                return None
        except LookError as error:
            return self._fail(error.code, error.message)
        except Exception:
            return self._fail("failed", traceback.format_exc())
        return self.finish()

    def _run_next(self):
        task = self.tasks[self.done]
        changes = Changes()
        if not self.run_handlers:
            # Handlers out first, back in last; the update just before they come back evaluates the put-back
            # state while they are still out, so an add-on sees neither the temporary change nor its undo.
            self._note_paused(pause_handlers(changes))
            changes.call(lambda: self.layer.update())
        t = time.perf_counter()
        try:
            out = self._run(task, changes)
        finally:
            errors = changes.restore()
            if errors:
                self.task_problems.append("undo errors: " + "; ".join(errors))
        if task["kind"] in ("draw", "image", "ui"):
            out["seconds"] = round(time.perf_counter() - t, 4)
            self.tiles.append(out)
        self.done += 1

    def _run(self, task, changes):
        kind = task["kind"]
        if kind == "measure":
            self._goto_frame(changes, task["frame"])
            self._visibility(changes)
            depsgraph = bpy.context.evaluated_depsgraph_get()
            points = gather_points(self._framed(), depsgraph, self.warnings)
            if len(points) > 200000:
                points = points[::int(math.ceil(len(points) / 200000))]
            center = None
            if self.state["bones"]:
                rig = self.state["rig"].evaluated_get(depsgraph)
                mw = rig.matrix_world
                spots = [mw @ rig.pose.bones[b].head for b in self.state["bones"]]
                center = tuple(sum(spots[1:], spots[0].copy()) / len(spots))
            self.state["measured"][task["frame"]] = (points, center)
            return None
        if kind == "fit":
            w, h = self.state["tile"]
            measured = self.state["measured"]
            vec = self.state["direction"]
            if self.state["bones"]:
                dist = 0.0
                for points, center in measured.values():
                    spec = fit_view(points, vec, w / h, self.fov, self.margin, self.ortho, self.args.get("distance"),
                                    center=center)
                    dist = max(dist, spec.half_height if self.ortho else spec.distance)
                self.state["fixed"] = dist
            else:
                allpts = np.concatenate([p for p, _c in measured.values() if len(p)] or [np.zeros((0, 3))])
                if not len(allpts):
                    self.warnings.append("nothing visible to frame in any of these frames; the view looks at the "
                                         "world origin")
                self.state["union"] = fit_view(allpts, vec, w / h, self.fov, self.margin, self.ortho,
                                               self.args.get("distance"))
            return None
        if kind == "draw":
            return self._draw_task(task, changes)
        if kind == "image":
            return self._image_task(task, changes)
        if kind == "ui":
            return self._ui_task(task, changes)
        raise LookError("failed", "unknown task " + kind)

    def _goto_frame(self, changes, frame):
        """Show the animation at `frame`. Every tile of a frames look goes through Blender's frame change, the one
        at the session's current frame too: a tile labelled FRAME N shows what playback shows at N, not an edit
        made since. The values as they were (unkeyed edits included) are written back afterwards."""
        if frame is None:
            return
        scene, layer = self.scene, self.layer
        snapshot = snapshot_animated()
        was = (scene.frame_current, scene.frame_subframe)

        def back():
            scene.frame_set(was[0], subframe=was[1])
            restore_animated(snapshot)
            layer.update()

        changes.call(back)
        scene.frame_set(int(frame))
        if int(frame) == was[0] and was[1] == 0.0 and "unkeyed_checked" not in self.state:
            self.state["unkeyed_checked"] = True
            edited = []
            for idb, path, index, value in snapshot:
                try:
                    now = idb.path_resolve(path)
                    if hasattr(now, "__len__") and not isinstance(now, str):
                        now = now[index]
                except Exception:
                    continue
                if not _same(now, value, 1e-6):
                    owner = idb.name_full if isinstance(idb, bpy.types.ID) else getattr(idb, "name", "?")
                    edited.append("%s.%s[%d]" % (owner, path, index))
            if edited:
                self.warnings.append("FRAME %d is drawn from the keys; the session at that frame has unkeyed changes "
                                     "(%s%s), which playback would replace; they are kept"
                                     % (was[0], ", ".join(edited[:5]), ", ..." if len(edited) > 5 else ""))

    def _visibility(self, changes):
        layer = self.layer
        for o in self.unhide:
            if o.hide_get(view_layer=layer):
                selected = o.select_get(view_layer=layer)
                o.hide_set(False, view_layer=layer)
                changes.call(lambda o=o, s=selected: (o.hide_set(True, view_layer=layer),
                                                      o.select_set(s, view_layer=layer)))
            if o.hide_viewport:
                changes.set(o, "hide_viewport", False)
        if self.args.get("isolate"):
            for o in layer.objects:
                if o in self.target_set or o.type in ("LIGHT", "CAMERA"):
                    continue
                if o.visible_get(view_layer=layer) and not o.hide_get(view_layer=layer):
                    # hide_set deselects: the selection is put back with the object
                    selected = o.select_get(view_layer=layer)
                    o.hide_set(True, view_layer=layer)
                    changes.call(lambda o=o, s=selected: (o.hide_set(False, view_layer=layer),
                                                          o.select_set(s, view_layer=layer)))
        layer.update()

    def _local_view(self, changes):
        """The user's 3D view in local view draws only the objects in it: the targets are put in for the picture."""
        space = self.dv.space if self.dv is not None else None
        if space is None or space.type != "VIEW_3D":
            return
        layer = self.layer
        if space.local_view is not None:
            added = []
            for o in self.targets:
                if not o.local_view_get(space):
                    o.local_view_set(space, True)
                    changes.call(lambda o=o: o.local_view_set(space, False))
                    added.append(o.name_full)
            if added and "local_view" not in self.state:
                self.state["local_view"] = True
                self.warnings.append("the 3D view is in local view: %s put in it for the pictures and taken out "
                                     "again" % ", ".join(added[:6]))
            layer.update()
        if getattr(space, "use_local_collections", False) and "local_collections" not in self.state:
            unseen = [o.name_full for o in self.targets if not o.visible_in_viewport_get(space)]
            self.state["local_collections"] = True
            if unseen:
                self.warnings.append("hidden in this 3D view's own collection visibility (local collections), so "
                                     "not drawn: %s" % ", ".join(unseen[:6]))

    def _resolve_view(self, task):
        view = task["view"]
        w, h = task["w"], task["h"]
        if isinstance(view, ViewSpec):
            return view
        if view == "viewport":
            r3d = self.dv.space.region_3d
            return ViewSpec(kind="viewport", view_matrix=r3d.view_matrix.copy(), proj_matrix=r3d.window_matrix.copy(),
                            ortho=not r3d.is_perspective)
        if view == "camera":
            depsgraph = bpy.context.evaluated_depsgraph_get()
            cam = self.scene.camera.evaluated_get(depsgraph)
            # w and h are already display pixels (the render's pixel aspect is in them, see _camera_size): the
            # pixel aspect must not be applied a second time.
            proj = cam.calc_matrix_camera(depsgraph, x=w, y=h, scale_x=1.0, scale_y=1.0)
            toward_eye = (cam.matrix_world.to_3x3() @ Vector((0.0, 0.0, 1.0))).normalized()
            far = float(getattr(cam.data, "clip_end", 1000.0)) if cam.type == "CAMERA" else 1000.0
            return ViewSpec(kind="camera", view_matrix=cam.matrix_world.inverted(), proj_matrix=proj,
                            cam_matrix=cam.matrix_world.copy(), eye=tuple(cam.matrix_world.translation),
                            camera=self.scene.camera.name_full, direction=tuple(toward_eye), far=far,
                            ortho=cam.type == "CAMERA" and cam.data.type == "ORTHO")
        if view == "fitted":
            if self.state["bones"]:
                points, center = self.state["measured"][task["frame"]]
                fixed = self.state["fixed"]
                vec = self.state["direction"]
                if self.ortho:
                    spec = fit_view(points, vec, w / h, self.fov, self.margin, True, None, center=center)
                    spec.proj_matrix = orthographic_matrix(fixed, w / h, spec.near, spec.far)
                    spec.half_height = fixed
                    return spec
                return fit_view(points, vec, w / h, self.fov, self.margin, False, fixed, center=center)
            return self.state["union"]
        raise LookError("failed", "unknown view %r" % (view,))

    # -- drawing ----------------------------------------------------------------------------

    def _apply_shading(self, changes, generated):
        shading = self.state["shading"]
        space = self.dv.space
        sh, ov = space.shading, space.overlay
        scene = self.scene
        if shading == "solid":
            changes.set(sh, "type", "SOLID")
        elif shading == "material":
            changes.set(sh, "type", "MATERIAL")
        elif shading == "rendered":
            changes.set(sh, "type", "RENDERED")
            if scene.render.engine not in EEVEE_IDS + ("BLENDER_WORKBENCH",):
                if "rendered_engine" not in self.state:
                    self.warnings.append("the scene renders with %s; 'rendered' was drawn with EEVEE (no path "
                                         "tracing: light, shadows and noise differ from a %s render)"
                                         % (scene.render.engine, scene.render.engine))
                self.state["rendered_engine"] = True
                changes.set(scene.render, "engine", _engine_eevee(scene))
        elif shading == "wireframe":
            changes.set(sh, "type", "SOLID")
            changes.set(sh, "light", "STUDIO")
            changes.set(sh, "color_type", "SINGLE")
            changes.set(sh, "single_color", (0.8, 0.8, 0.8))
            changes.set(sh, "show_xray", False)
            changes.set(ov, "show_overlays", True)
            changes.set(ov, "show_wireframes", True)
            changes.set(ov, "wireframe_threshold", 1.0)
            changes.set(ov, "wireframe_opacity", 1.0)
            changes.set(ov, "show_bones", False)
        elif shading == "xray":
            changes.set(sh, "type", "SOLID")
            changes.set(sh, "show_xray", True)
            changes.set(sh, "xray_alpha", 0.5)
            changes.set(ov, "show_overlays", True)
            changes.set(ov, "show_bones", True)
            changes.set(ov, "show_wireframes", False)
            for rig in self.rigs:
                if rig.library is None:
                    changes.set(rig, "show_in_front", True)
        if generated and not self.args.get("overlays"):
            if shading in ("wireframe", "xray"):
                for name in CLUTTER:
                    changes.set(ov, name, False)
            else:
                changes.set(ov, "show_overlays", False)

    def _draw_pixels(self, spec, w, h):
        import gpu
        dv = self.dv
        offscreen = gpu.types.GPUOffScreen(w, h)
        try:
            offscreen.draw_view3d(self.scene, self.layer, dv.space, dv.region, spec.view_matrix, spec.proj_matrix,
                                  do_color_management=True)
            buf = offscreen.texture_color.read()
        finally:
            offscreen.free()
        buf.dimensions = w * h * 4
        arr = np.asarray(buf, dtype=np.float32).reshape(h, w, 4)[::-1]
        return np.clip(np.rint(arr), 0, 255).astype(np.uint8)

    def _draw_task(self, task, changes):
        self._goto_frame(changes, task["frame"])
        self._visibility(changes)
        spec = self._resolve_view(task)
        w, h = task["w"], task["h"]
        generated = spec.kind != "viewport"
        draws, settled = 1, True
        if spec.kind == "fit":
            self._note_tiny(spec, w, h, task)
        if self.backend == "draw":
            if generated:
                self._local_view(changes)
            self._apply_shading(changes, generated)
            pixels = self._draw_pixels(spec, w, h)
            kind = self.dv.space.shading.type
            if kind in ("MATERIAL", "RENDERED"):
                # Shaders may still compile (EEVEE): draw again until two draws agree.
                settled = False
                for _ in range(8):
                    if not self.background:
                        time.sleep(0.03)
                    again = self._draw_pixels(spec, w, h)
                    draws += 1
                    try:
                        compiling = (not self.background) and bpy.app.is_job_running("SHADER_COMPILATION")
                    except (TypeError, AttributeError):
                        compiling = False
                    change = float(np.abs(again.astype(np.int16) - pixels.astype(np.int16)).mean())
                    pixels = again
                    if change <= 0.25 and not compiling:
                        settled = True
                        break
            rgb = over_background(pixels)
        else:
            rgb = self._render_pixels(changes, spec, w, h, task)
        shows = dict(task.get("shows") or {})
        shows["shading"] = self._shading_label()
        tile = {"label": task["label"], "slug": task["slug"], "pixels": rgb, "shows": shows,
                "size": [int(rgb.shape[1]), int(rgb.shape[0])], "projection": spec.describe(), "draws": draws}
        if not settled:
            tile["settled"] = False
            self.warnings.append("%s: the picture was still changing after %d draws (shaders compiling?)"
                                 % (task["label"], draws))
        return tile

    def _note_tiny(self, spec, w, h, task):
        """A framed target that comes out smaller than TINY_PX in a fitted tile (far from the others, or tiny
        beside them) is named in a warning: it is in the picture but cannot be seen."""
        framed = [o for o in self._framed() if o.type in GEOMETRY]
        if not framed or len(framed) > 64:
            return
        depsgraph = bpy.context.evaluated_depsgraph_get()
        view = (task.get("shows") or {}).get("view") or "?"
        if task.get("frame") is not None:
            view = "frame %d" % task["frame"]
        for ob in framed:
            ev = ob.evaluated_get(depsgraph)
            mw = np.array(ev.matrix_world, np.float64)
            corners = np.array([tuple(c) for c in ev.bound_box], np.float64) @ mw[:3, :3].T + mw[:3, 3]
            if float(np.linalg.norm(corners.max(0) - corners.min(0))) <= 1e-9:
                continue          # no geometry of its own (an instancer's empty box): nothing to be too small
            x, y, front = project(corners, spec.view_matrix, spec.proj_matrix, w, h)
            if not front.all():
                continue
            size = max(float(x.max() - x.min()), float(y.max() - y.min()))
            if size < TINY_PX:
                self.state.setdefault("tiny", {}).setdefault(ob.name_full, []).append((str(view).upper(), size))

    # -- the render fallback ----------------------------------------------------------------

    def _png_settings(self, changes):
        ims = self.scene.render.image_settings
        lin = getattr(getattr(ims, "linear_colorspace_settings", None), "name", None)
        view = {}
        _walk(view, "v", ims.view_settings, 0) if hasattr(ims, "view_settings") else None

        def put_back():
            if lin is not None:
                try:
                    ims.linear_colorspace_settings.name = lin
                except Exception:
                    pass
            for key, value in view.items():
                attr = key.split(".", 1)[1]
                try:
                    if not _same(_plain(getattr(ims.view_settings, attr)), value):
                        setattr(ims.view_settings, attr, value)
                except Exception:
                    pass

        changes.call(put_back)          # runs after the format is put back (switching formats resets these)
        if hasattr(ims, "media_type"):
            changes.set(ims, "media_type", "IMAGE")
        changes.set(ims, "file_format", "PNG")
        changes.set(ims, "color_mode", "RGBA")
        changes.set(ims, "color_depth", "8")
        changes.set(ims, "compression", 15)

    def _render_pixels(self, changes, spec, w, h, task):
        """Render one tile through a temporary copy of the scene (scene.copy(): the same objects and collections,
        its own settings and its own render result), so the user's render settings and the picture in their Render
        Result are never touched; the copy is removed afterwards."""
        scene, layer = self.scene, self.layer
        shading = self.state["shading"] or "solid"
        if "rr_existed" not in self.state:
            self.state["rr_existed"] = bpy.data.images.get("Render Result") is not None
        if not self.state["rr_existed"]:
            def drop_result():           # rendering makes a Render Result datablock: it goes again (undone last)
                rr = bpy.data.images.get("Render Result")
                if rr is not None and rr.type == "RENDER_RESULT":
                    bpy.data.images.remove(rr)
            changes.call(drop_result)
        copy = scene.copy()
        changes.call(lambda: bpy.data.scenes.remove(copy))
        r = copy.render
        if spec.kind == "camera":
            cam = scene.camera
        else:
            data = bpy.data.cameras.new("look_tmp_camera")
            cam = bpy.data.objects.new("look_tmp_camera", data)
            changes.call(lambda: (bpy.data.objects.remove(cam), bpy.data.cameras.remove(data)))
            copy.collection.objects.link(cam)
            cam.matrix_world = spec.cam_matrix
            data.sensor_fit = "VERTICAL"
            data.clip_start, data.clip_end = spec.near, spec.far
            if spec.ortho:
                data.type = "ORTHO"
                data.ortho_scale = 2.0 * (spec.half_height or 1.0)
            else:
                data.angle_y = math.radians(spec.fov_deg)
            copy.camera = cam
        r.resolution_x, r.resolution_y, r.resolution_percentage = w, h, 100
        # w and h are display pixels for every view (the camera's pixel aspect is folded into them)
        r.pixel_aspect_x, r.pixel_aspect_y = 1.0, 1.0
        for name, value in (("use_border", False), ("use_compositing", False), ("use_sequencer", False),
                            ("use_multiview", False), ("use_single_layer", True), ("use_persistent_data", False)):
            if hasattr(r, name):
                setattr(r, name, value)
        engine = "BLENDER_WORKBENCH"
        if shading == "material":
            engine = _engine_eevee(copy)
        elif shading == "rendered":
            engine = r.engine if r.engine in EEVEE_IDS + ("BLENDER_WORKBENCH",) else _engine_eevee(copy)
            if engine != r.engine and "rendered_engine" not in self.state:
                self.state["rendered_engine"] = True
                self.warnings.append("the scene renders with %s; 'rendered' was rendered with EEVEE" % r.engine)
        r.engine = engine
        if shading != "rendered":
            r.film_transparent = True
        disp = copy.display.shading
        if shading == "xray":
            disp.show_xray, disp.xray_alpha = True, 0.5
        elif shading == "wireframe":
            disp.light, disp.color_type = "STUDIO", "SINGLE"
            disp.single_color, disp.show_xray = (0.8, 0.8, 0.8), False
        if engine in EEVEE_IDS and copy.eevee.taa_render_samples > 16:
            copy.eevee.taa_render_samples = 16
        if r.use_simplify:               # the viewport's simplify levels, not the render's
            for a, b in (("simplify_subdivision_render", "simplify_subdivision"),
                         ("simplify_child_particles_render", "simplify_child_particles")):
                if hasattr(r, a) and hasattr(r, b):
                    setattr(r, a, getattr(r, b))
        if shading == "material":
            self._studio_world(changes, copy)
        # Render what the viewport shows: viewport visibility decides, per object, and every modifier as the
        # viewport has it (on or off, subdivision levels). Objects and collections are shared with the user's
        # scene, so these go through `changes`.
        for coll in copy.collection.children_recursive:
            if coll.hide_render:
                changes.set(coll, "hide_render", False)
        differs = []
        for o in layer.objects:
            if o is cam:
                continue
            want = o.visible_get(view_layer=layer)
            if shading == "material" and o.type == "LIGHT":
                want = False
            if o.hide_render == want:
                changes.set(o, "hide_render", not want)
            if not want or o.library is not None:
                continue
            for m in getattr(o, "modifiers", []):
                if m.show_render != m.show_viewport:
                    changes.set(m, "show_render", m.show_viewport)
                    differs.append("%s/%s" % (o.name, m.name))
                if m.type in ("SUBSURF", "MULTIRES") and m.show_viewport and m.render_levels != m.levels:
                    changes.set(m, "render_levels", m.levels)
                    differs.append("%s/%s levels" % (o.name, m.name))
        if differs and "modifier_note" not in self.state:
            self.state["modifier_note"] = True
            self.warnings.append("render fallback: modifiers set differently for render were rendered as the "
                                 "viewport has them: %s" % ", ".join(differs[:6]))
        ims = r.image_settings
        if hasattr(ims, "media_type"):
            ims.media_type = "IMAGE"
        ims.file_format, ims.color_mode, ims.color_depth, ims.compression = "PNG", "RGBA", "8", 15
        tmp = os.path.join(self.tmp, ".look_render_%d_%d.png" % (os.getpid(), len(self.tiles)))
        bpy.ops.render.render(write_still=False, scene=copy.name, layer=layer.name)
        try:
            bpy.data.images["Render Result"].save_render(tmp, scene=copy)
            rgba, _fmt = read_image_file(tmp)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        img = to_u8(rgba)
        if shading == "wireframe":
            self._overlay_edges(img, spec, w, h)
        rgb = over_background(img)
        if shading == "xray":
            self._overlay_bones(rgb, spec, w, h)
        if "render_notes" not in self.state:
            self.state["render_notes"] = True
            self.warnings.append("render fallback: each tile is rendered from a temporary copy of the scene (the "
                                 "animation evaluated at the frame: unkeyed changes do not show); the Render Result "
                                 "and the render settings are left as they were")
            notes = {"solid": "Workbench render with the scene's Workbench settings (not the viewport's)",
                     "material": "EEVEE with the bundled studio HDRI (forest.exr) and the scene's lights off, "
                                 "background transparent",
                     "rendered": "the scene's own engine (EEVEE/Workbench) at up to 16 samples",
                     "wireframe": "Workbench single colour plus every edge of the targets drawn 1 px by numpy, hidden "
                                  "edges found by ray casts (Blender's overlay draws smoother, anti-aliased lines)",
                     "xray": "Workbench X-ray at alpha 0.5 plus bones drawn by numpy as lines with joint dots "
                             "(not Blender's octahedral bones)"}
            self.warnings.append("render fallback, %s: %s" % (shading, notes.get(shading, "")))
        return rgb

    def _studio_world(self, changes, scene):
        """A temporary world lit by Blender's bundled studio HDRI, for `scene` (the temporary copy)."""
        folder = bpy.utils.system_resource("DATAFILES", path="studiolights/world")
        path = os.path.join(folder, "forest.exr") if folder else ""
        world = bpy.data.worlds.new("look_tmp_world")
        image = None
        if path and os.path.isfile(path):
            image = bpy.data.images.load(path, check_existing=False)
            changes.call(lambda: bpy.data.images.remove(image))
        changes.call(lambda: bpy.data.worlds.remove(world))
        if hasattr(world, "use_nodes") and not world.use_nodes:
            world.use_nodes = True
        tree = world.node_tree
        background = next((n for n in tree.nodes if n.type == "BACKGROUND"), None)
        if image is not None and background is not None:
            env = tree.nodes.new("ShaderNodeTexEnvironment")
            env.image = image
            tree.links.new(env.outputs["Color"], background.inputs["Color"])
        elif background is not None:
            background.inputs["Color"].default_value = (0.5, 0.5, 0.5, 1.0)
        scene.world = world

    def _overlay_edges(self, rgba, spec, w, h):
        from mathutils.bvhtree import BVHTree
        depsgraph = bpy.context.evaluated_depsgraph_get()
        verts_all, tris_all, segs = [], [], []
        offset = 0
        for ob in self._framed():
            if ob.type not in MESHLIKE:
                continue
            ev = ob.evaluated_get(depsgraph)
            try:
                mesh = ev.to_mesh()
            except Exception:
                continue
            try:
                n = len(mesh.vertices)
                if not n:
                    continue
                co = np.empty(n * 3, np.float32)
                mesh.vertices.foreach_get("co", co)
                mw = np.array(ev.matrix_world, np.float64)
                world = co.reshape(n, 3).astype(np.float64) @ mw[:3, :3].T + mw[:3, 3]
                edges = np.empty(len(mesh.edges) * 2, np.int32)
                mesh.edges.foreach_get("vertices", edges)
                mesh.calc_loop_triangles()
                tris = np.empty(len(mesh.loop_triangles) * 3, np.int32)
                mesh.loop_triangles.foreach_get("vertices", tris)
                verts_all.append(world)
                tris_all.append(tris.reshape(-1, 3) + offset)
                segs.append(edges.reshape(-1, 2) + offset)
                offset += n
            finally:
                ev.to_mesh_clear()
        if not segs:
            return
        verts = np.concatenate(verts_all)
        tris = np.concatenate(tris_all)
        segs = np.concatenate(segs)
        bvh = BVHTree.FromPolygons(verts.tolist(), tris.tolist()) if len(tris) <= 600000 else None
        if bvh is None:
            self.warnings.append("wireframe fallback: too many triangles to test which edges are hidden; hidden "
                                 "edges are drawn too")
        x, y, front = project(verts, spec.view_matrix, spec.proj_matrix, w, h)
        a, b = segs[:, 0], segs[:, 1]
        keep = front[a] & front[b]
        a, b = a[keep], b[keep]
        max_edges = 400000
        if len(a) > max_edges:          # memory: a few points per edge, for millions of edges, is gigabytes
            stride = int(math.ceil(len(a) / max_edges))
            a, b = a[::stride], b[::stride]
            self.warnings.append("wireframe fallback: the targets have %d edges, more than %d: an even sample of "
                                 "them is drawn" % (len(segs), max_edges))
        length = np.hypot(x[b] - x[a], y[b] - y[a])
        steps = np.clip(np.ceil(length / 1.5).astype(np.int64) + 1, 2, 400)
        budget = 150000
        if steps.sum() > budget:
            steps = np.maximum(2, (steps * budget / steps.sum()).astype(np.int64))
        # every edge sampled at `steps` points from 0 to 1, all edges at once
        idx = np.repeat(np.arange(len(a)), steps)
        starts = np.cumsum(steps) - steps
        t = (np.arange(int(steps.sum())) - np.repeat(starts, steps)) / np.repeat(np.maximum(steps - 1, 1), steps)
        pa, pb = verts[a[idx]], verts[b[idx]]
        pw = pa + (pb - pa) * t[:, None]
        sx, sy, sf = project(pw, spec.view_matrix, spec.proj_matrix, w, h)
        visible = sf & (sx >= 0) & (sx < w) & (sy >= 0) & (sy < h)
        if bvh is not None:
            eye = np.array(spec.eye, np.float64)
            d = np.array(spec.direction, np.float64)
            for i in np.nonzero(visible)[0]:
                p = pw[i]
                if spec.ortho:
                    origin = p + d * (spec.far * 2.0)
                    ray = -d
                    reach = spec.far * 2.0
                else:
                    ray = p - eye
                    reach = float(np.linalg.norm(ray))
                    if reach <= 0:
                        continue
                    ray = ray / reach
                    origin = eye
                hit = bvh.ray_cast(Vector(origin.tolist()), Vector(ray.tolist()), reach * (1.0 - 2e-3) - 1e-4)
                if hit[0] is not None:
                    visible[i] = False
        px = np.clip(sx[visible].astype(int), 0, w - 1)
        py = np.clip(sy[visible].astype(int), 0, h - 1)
        rgba[py, px, :3] = (30, 30, 30)
        if rgba.shape[2] == 4:
            rgba[py, px, 3] = 255

    def _overlay_bones(self, rgb, spec, w, h):
        depsgraph = bpy.context.evaluated_depsgraph_get()
        for rig in self.rigs:
            ev = rig.evaluated_get(depsgraph)
            if ev.pose is None:
                continue
            mw = ev.matrix_world
            heads = np.array([tuple(mw @ pb.head) for pb in ev.pose.bones], np.float64)
            tails = np.array([tuple(mw @ pb.tail) for pb in ev.pose.bones], np.float64)
            if not len(heads):
                continue
            t = np.linspace(0, 1, 200)
            pts = (heads[:, None, :] + (tails - heads)[:, None, :] * t[None, :, None]).reshape(-1, 3)
            x, y, f = project(pts, spec.view_matrix, spec.proj_matrix, w, h)
            for dx, dy in ((0, 0), (1, 0), (0, 1), (1, 1)):
                px, py = (x + dx).astype(int), (y + dy).astype(int)
                ok = f & (px >= 0) & (px < w) & (py >= 0) & (py < h)
                rgb[py[ok], px[ok]] = (225, 225, 225)
            jx, jy, jf = project(np.concatenate([heads, tails]), spec.view_matrix, spec.proj_matrix, w, h)
            for dx in range(-2, 3):
                for dy in range(-2, 3):
                    if dx * dx + dy * dy > 5:
                        continue
                    px, py = (jx + dx).astype(int), (jy + dy).astype(int)
                    ok = jf & (px >= 0) & (px < w) & (py >= 0) & (py < h)
                    rgb[py[ok], px[ok]] = (20, 20, 20)

    # -- image and ui ------------------------------------------------------------------------

    def _image_task(self, task, changes):
        ref = task["ref"]
        image = bpy.data.images.get(ref)
        temp_path = os.path.join(self.tmp, ".look_image_%d.png" % os.getpid())
        note = None
        if image is None and ref == "Render Result":
            raise LookError("refused", "the Render Result has nothing in it yet (render first)")
        from_file = None
        declared = image.colorspace_settings.name if image is not None and image.type != "RENDER_RESULT" else None
        if image is None:
            # a file: the path as the caller's machine resolved it (look.py makes a relative path absolute where it
            # was typed), else as this Blender resolves it
            path = task.get("file") or os.path.abspath(bpy.path.abspath(ref))
            if not os.path.isfile(path):
                raise LookError("bad_args", "no image named %r in the file and no file at %r" % (ref, path))
            image = bpy.data.images.load(path, check_existing=False)
            declared = image.colorspace_settings.name
            loaded = image
            changes.call(lambda: bpy.data.images.remove(loaded))
            if not (image.is_float and _is_linear(image.colorspace_settings.name)):
                _data_colorspace(image)           # a display-referred file: show its stored values
                image.alpha_mode = "CHANNEL_PACKED"
            from_file = os.path.basename(path)
        if image.type == "RENDER_RESULT":
            self._png_settings(changes)
            try:
                image.save_render(temp_path, scene=self.scene)
            except RuntimeError as error:
                raise LookError("refused", "the Render Result has nothing in it yet (render first): %s" % error)
            try:
                rgba, _fmt = read_image_file(temp_path)
            finally:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            name, original = image.name, [rgba.shape[1], rgba.shape[0]]
            note = "as saved by the scene's output settings (view transform applied)"
        else:
            if image.source in ("MOVIE", "SEQUENCE"):
                raise LookError("refused", "%r is a movie or an image sequence; show one frame as a file instead" % ref)
            width, height = image.size[0], image.size[1]
            if not width or not height:
                raise LookError("refused", "%r has no pixels" % ref)
            if image.is_float and _is_linear(image.colorspace_settings.name):
                tmp_scene = bpy.data.scenes.new("look_tmp_scene")
                changes.call(lambda: bpy.data.scenes.remove(tmp_scene))
                for attr in ("view_transform", "look", "exposure", "gamma"):
                    try:
                        setattr(tmp_scene.view_settings, attr, getattr(self.scene.view_settings, attr))
                    except Exception:
                        pass
                try:
                    tmp_scene.display_settings.display_device = self.scene.display_settings.display_device
                except Exception:
                    pass
                ims = tmp_scene.render.image_settings
                if hasattr(ims, "media_type"):
                    ims.media_type = "IMAGE"
                ims.file_format, ims.color_mode, ims.color_depth = "PNG", "RGBA", "8"
                try:
                    image.save_render(temp_path, scene=tmp_scene)
                    rgba, _fmt = read_image_file(temp_path)
                    note = "a float image shown through the scene's view transform"
                except RuntimeError:
                    flat = np.empty(width * height * image.channels, np.float32)
                    image.pixels.foreach_get(flat)
                    rgba = _rgba(flat.reshape(height, width, image.channels)[::-1].copy())
                    rgba[..., :3] = srgb_encode(rgba[..., :3])
                    note = "a float image shown with the plain sRGB curve"
                finally:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
            else:
                flat = np.empty(width * height * image.channels, np.float32)
                image.pixels.foreach_get(flat)        # the current buffer, painted strokes included
                rgba = _rgba(flat.reshape(height, width, image.channels)[::-1].copy())
                note = "the stored pixels, as they are" if not image.is_float else                     "a float image stored display-referred (%s): its values as they are" % declared
            name, original = from_file or image.name, [width, height]
        img = over_background(to_u8(rgba), checker=True)
        w, h = self._fit_size(img.shape[1], img.shape[0])
        if w < img.shape[1]:
            img = resize_box(img, w, h)
        label = "IMAGE %s | %dX%d" % (name, original[0], original[1])
        return {"label": label, "slug": "image", "pixels": img, "size": [int(img.shape[1]), int(img.shape[0])],
                "shows": {"image": name, "original_size": original, "note": note}}

    def _ui_task(self, task, changes):
        spec = task["spec"]
        wm = bpy.context.window_manager
        window = self.window or wm.windows[0]
        kind, _, name = spec.partition(":")
        kind = kind.strip().lower()
        name = name.strip()
        redraws = max(1, min(int(_num(self.args.get("redraws"), 3)), 10))
        shot = os.path.join(self.tmp, ".look_ui_%d.png" % os.getpid())
        crop = None
        area = None

        def largest(test):
            areas = [a for a in window.screen.areas if test(a)]
            return max(areas, key=lambda a: a.width * a.height) if areas else None

        def redraw(target_area=None):
            for a in window.screen.areas:
                a.tag_redraw()
            with bpy.context.temp_override(window=window, area=target_area or window.screen.areas[0]):
                bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=redraws)

        if kind == "window":
            redraw()
            with bpy.context.temp_override(window=window):
                bpy.ops.screen.screenshot(filepath=shot, check_existing=False)
        else:
            if kind in ("view3d", "sidebar"):
                area = largest(lambda a: a.type == "VIEW_3D")
                if area is None:
                    raise LookError("refused", "ui %s: no 3D viewport in this window" % kind)
                if kind == "sidebar":
                    space = area.spaces.active
                    first = next((r for r in area.regions if r.type == "UI"), None)
                    if first is not None and getattr(first, "active_panel_category", None) == "UNSUPPORTED":
                        self.warnings.append("this 3D view's sidebar had never been open: Blender now remembers a tab "
                                             "for it, which nothing can unset (harmless; the sidebar is closed again)")
                    changes.set(space, "show_region_ui", True)
                    redraw(area)
                    ui = next((r for r in area.regions if r.type == "UI"), None)
                    if name:
                        try:
                            was = ui.active_panel_category
                            ui.active_panel_category = name
                            changes.call(lambda: setattr(ui, "active_panel_category", was))
                        except (TypeError, AttributeError) as error:
                            raise LookError("refused", "ui sidebar: no tab %r here (%s)" % (name, error))
                    redraw(area)
                    crop = ui
            elif kind == "properties":
                area = largest(lambda a: a.type == "PROPERTIES")
                if area is None:
                    area = largest(lambda a: True)
                    changes.set(area, "ui_type", "PROPERTIES")
                space = area.spaces.active
                obj_name = self.args.get("ui_object")
                if obj_name:
                    ob = bpy.data.objects.get(obj_name)
                    if ob is None:
                        raise LookError("bad_args", "no object named %r" % obj_name)
                    changes.set(space, "pin_id", ob)
                    changes.set(space, "use_pin_id", True)
                if name:
                    if not changes.set(space, "context", name.upper()):
                        items = [e.identifier for e in space.bl_rna.properties["context"].enum_items]
                        raise LookError("refused", "ui properties: tab %r is not available now; tabs: %s"
                                        % (name, ", ".join(items)))
                redraw(area)
            elif kind == "preferences":
                prefs = bpy.context.preferences
                dirty = prefs.is_dirty
                changes.call(lambda: setattr(prefs, "is_dirty", dirty))
                area = largest(lambda a: a.type == "PREFERENCES")
                if area is None:
                    area = largest(lambda a: True)
                    changes.set(area, "ui_type", "PREFERENCES")
                changes.set(prefs, "active_section", "ADDONS")
                if name:
                    changes.set(wm, "addon_search", name)
                    import addon_utils
                    for mod in addon_utils.modules(refresh=False):
                        info = addon_utils.module_bl_info(mod)
                        if mod.__name__ == name or mod.__name__.endswith("." + name) or \
                                str(info.get("name", "")).lower() == name.lower():
                            was = info.get("show_expanded", False)
                            info["show_expanded"] = True
                            changes.call(lambda info=info, was=was: info.__setitem__("show_expanded", was))
                redraw(area)
            elif kind == "area":
                key = name.upper()
                area = largest(lambda a: a.type == key or a.ui_type == key)
                if area is None:
                    raise LookError("refused", "ui area: no %s area in this window; areas: %s"
                                    % (key, ", ".join(sorted({a.ui_type for a in window.screen.areas}))))
                redraw(area)
            else:
                raise LookError("bad_args", "ui %r: use window, view3d, sidebar:TAB, properties:TAB, "
                                "preferences:ADDON or area:TYPE" % spec)
            with bpy.context.temp_override(window=window, area=area):
                bpy.ops.screen.screenshot_area(filepath=shot, check_existing=False)
        try:
            rgba, _fmt = read_image_file(shot)
        finally:
            if os.path.exists(shot):
                os.remove(shot)
        img = over_background(to_u8(rgba))
        if crop is not None and area is not None and crop.width > 2:
            sx = img.shape[1] / float(area.width)
            sy = img.shape[0] / float(area.height)
            x0 = int(round((crop.x - area.x) * sx))
            top = int(round((area.height - (crop.y - area.y) - crop.height) * sy))
            img = np.ascontiguousarray(img[max(0, top):top + int(round(crop.height * sy)),
                                           max(0, x0):x0 + int(round(crop.width * sx))])
        full = [int(img.shape[1]), int(img.shape[0])]
        w, h = self._fit_size(img.shape[1], img.shape[0])
        if w < img.shape[1]:
            img = resize_box(img, w, h)
        return {"label": "UI %s" % spec, "slug": task["slug"], "pixels": img,
                "size": [int(img.shape[1]), int(img.shape[0])],
                "shows": {"ui": spec, "captured_size": full, "area": area.ui_type if area is not None else "window"}}

    # -- finishing --------------------------------------------------------------------------

    def _fail(self, code, message):
        # A deferred job is checked tick by tick: what changed between ticks was someone else (the user deleting a
        # target, say), and only what the failed tick itself left changed counts against the tool.
        tick_diff = []
        if self.deferred and getattr(self, "tick_before", None) is not None:
            try:
                tick_diff = fingerprint_diff(self.tick_before, fingerprint())
            except Exception:
                tick_diff = ["the fingerprint could not be read after the failure"]
        problems = self.job_changes.restore()
        try:
            diff = fingerprint_diff(self.fp_before, fingerprint())
        except Exception:
            diff = ["the fingerprint could not be read after the failure"]
        if self.deferred:
            blamed = tick_diff + self.task_problems + problems
            meanwhile = [d for d in diff if d not in tick_diff]
        else:
            blamed, meanwhile = diff + self.task_problems + problems, []
        last = message.strip().splitlines()[-1] if message else code
        if "has been removed" in last or "ReferenceError" in last:
            last = ("something this look was using was removed while it ran (a target deleted between ticks?): "
                    + last)
        out = self._base_result()
        out.update({"ok": False, "code": code, "error": last, "detail": message if "\n" in (message or "") else None})
        out["fingerprint"] = {"checked": True, "equal": not blamed, "differences": blamed,
                              "covers": list(FINGERPRINT_COVERS)}
        if meanwhile:
            out["fingerprint"]["changed_meanwhile"] = meanwhile
        if blamed:
            out["error"] += " | AND THE SESSION WAS NOT PUT BACK: " + "; ".join(blamed[:6])
        return out

    def _base_result(self):
        out = {"tool": "look", "version": LOOK_VERSION, "mode": self.mode, "backend": getattr(self, "backend", None),
               "shading": self._shading_label() if hasattr(self, "backend") else self.shading,
               "blender": {"version": bpy.app.version_string, "pid": os.getpid(), "file": bpy.data.filepath,
                           "background": self.background, "scene": self.scene.name},
               "targets": getattr(self, "target_info", None),
               "warnings": list(dict.fromkeys(self.warnings)),          # each said once
               "deferred": getattr(self, "deferred", False)}
        if getattr(self, "paused", None):
            out["handlers_paused"] = self.paused
        if "frame_range" in getattr(self, "state", {}):
            out["frames"] = dict(self.state["frame_range"], list=self.state.get("frames"))
        return out

    def _handler_warnings(self):
        frame_lists = ("frame_change_pre", "frame_change_post")
        if self.mode != "frames":
            return
        if self.run_handlers:
            names = [_handler_name(h) for n in frame_lists for h in getattr(bpy.app.handlers, n, [])]
            if names:
                self.warnings.append("frame-change handlers ran on every frame change of this look (%s): their side "
                                     "effects are not undone" % ", ".join(names[:4]))
        else:
            names = [h for n in frame_lists for h in self.paused.get(n, [])]
            if names:
                self.warnings.append("frame-change handlers were paused while the frames were shown (%s): the pictures "
                                     "show the animation without them, and they saw none of the frame changes; "
                                     "--run-handlers lets them run" % ", ".join(names[:4]))

    def _clear_old_files(self):
        """This tool's earlier pictures and result under the same name, so the folder holds only this run's."""
        pattern = re.compile(r"^%s_(\d{2}_.+\.png|sheet\.png|result\.json)$" % re.escape(self.stem), re.IGNORECASE)
        try:
            names = os.listdir(long_path(self.folder))
        except OSError:
            return []
        removed = []
        for name in names:
            if pattern.match(name) or (self.out_is_png and name.lower() == os.path.basename(self.sheet_path).lower()):
                try:
                    os.remove(long_path(os.path.join(self.folder, name)))
                    removed.append(name)
                except OSError:
                    pass
        return removed

    def finish(self):
        problems = self.job_changes.restore()
        t = time.perf_counter()
        after = fingerprint()
        self.timing["fingerprint_after_s"] = round(time.perf_counter() - t, 4)
        diff = fingerprint_diff(self.fp_before, after)
        tiles = [t for t in self.tiles if t is not None]
        self._handler_warnings()
        for name, seen in sorted((self.state.get("tiny") or {}).items()):
            where = ", ".join("%s %.1f px" % (v, s) for v, s in seen[:4])
            self.warnings.append("%s is too small to see (%s): it is far from the other targets or tiny beside them; "
                                 "look at it alone with --target %s" % (name, where, name))
        written = []
        records = []
        replaced = self._clear_old_files() if tiles else []
        single = self.out_is_png and len(tiles) == 1      # --out X.png with one picture: X.png is that picture
        sheet_path = os.path.join(self.folder, self.stem + "_sheet.png") if single else self.sheet_path
        for i, tile in enumerate(tiles, 1):
            path = self.sheet_path if single else os.path.join(self.folder, "%s_%02d_%s.png" % (self.stem, i, tile["slug"]))
            write_png(path, tile["pixels"])
            written.append(path)
            rec = {k: v for k, v in tile.items() if k != "pixels"}
            rec.update({"index": i, "path": path})
            records.append(rec)
        sheet_info = None
        if tiles:
            title = "%s | %s | %s" % (self.mode.upper(), self._shading_short() if self.backend != "none" else "",
                                      ", ".join(self.target_info["framed"][:6]) if self.target_info["requested"] else
                                      (os.path.basename(bpy.data.filepath) or "unsaved file"))
            sheet, cells, grid = compose_sheet([t["pixels"] for t in tiles], [t["label"] for t in tiles], title,
                                               self.sheet_max)
            write_png(sheet_path, sheet)
            written.append(sheet_path)
            for cell, rec in zip(cells, records):
                cell["label"] = rec["label"]
            sheet_info = {"path": sheet_path, "size": [int(sheet.shape[1]), int(sheet.shape[0])],
                          "cells": cells, **grid}
        self.timing["total_s"] = round(time.perf_counter() - self.t0, 4)
        authoritative = (self.task_problems + problems) if self.deferred else (diff + self.task_problems + problems)
        out = self._base_result()             # again: the warnings above are in it now
        out.update({"ok": not authoritative, "sheet": sheet_info, "tiles": records, "written": written,
                    "timing": self.timing,
                    "fingerprint": {"checked": True, "equal": not authoritative, "values": len(after),
                                    "differences": authoritative, "covers": list(FINGERPRINT_COVERS),
                                    "how": ("before and after every bridge tick (%d ticks; each puts everything back "
                                            "before Blender's event loop runs)" % getattr(self, "ticks", 0)
                                            if self.deferred else "before the first change and after the last undo")},
                    "file_marked_modified": bool(bpy.data.is_dirty) and not self.dirty_before})
        if replaced:
            out["replaced_earlier_files"] = replaced
        if self.deferred and diff and not authoritative:
            out["fingerprint"]["changed_meanwhile"] = diff
            out["warnings"].append("the session changed between tiles (the user, or another tool); each tile itself "
                                   "was put back")
        if authoritative:
            out["code"] = "left_changed"
            out["error"] = "THE SESSION WAS NOT PUT BACK AS IT WAS: " + "; ".join(authoritative[:8])
        if tiles:
            # The whole result beside the pictures: a caller that gave up waiting (--timeout) can read it there.
            out["result_file"] = self.result_path
            written.append(self.result_path)
            try:
                text = json.dumps(out, default=str)
                with open(long_path(self.result_path + ".part"), "w", encoding="utf-8") as handle:
                    handle.write(text)
                os.replace(long_path(self.result_path + ".part"), long_path(self.result_path))
            except OSError as error:
                written.remove(self.result_path)
                out.pop("result_file", None)
                out["warnings"].append("could not write the result file: %s" % error)
        return out


def run(args):
    """Run a whole look synchronously and return the result dict (errors as {"ok": False, ...})."""
    try:
        job = Job(args)
    except LookError as error:
        return {"ok": False, "code": error.code, "error": error.message, "tool": "look", "version": LOOK_VERSION}
    return job.run_all()


def bridge_start(args):
    """For the bridge: a result dict, or (windowed Blender, several tiles) a check_is_finished callable."""
    try:
        job = Job(args)
    except LookError as error:
        return {"ok": False, "code": error.code, "error": error.message, "tool": "look", "version": LOOK_VERSION,
                "blender": {"version": bpy.app.version_string, "pid": os.getpid(), "file": bpy.data.filepath,
                            "background": bool(bpy.app.background)}}
    except Exception:
        return {"ok": False, "code": "failed", "error": traceback.format_exc().strip().splitlines()[-1],
                "detail": traceback.format_exc(), "tool": "look", "version": LOOK_VERSION}
    if job.deferred:
        return job.step
    return job.run_all()


class _Tee(io.TextIOBase):
    """Text written to the log as usual and kept, for the result."""

    def __init__(self, stream):
        self.stream, self.kept = stream, io.StringIO()

    def write(self, text):
        self.kept.write(text)
        try:
            return self.stream.write(text)
        except Exception:
            return len(text)

    def flush(self):
        try:
            self.stream.flush()
        except Exception:
            pass


def run_setup(path):
    """Run a --setup script as Blender's --python would (__name__ '__main__', __file__ set), keeping what it prints.
    Returns ({path, output}, None) or ({...}, the traceback text) when it fails."""
    tee = _Tee(sys.stdout)
    failure = None
    try:
        with open(path, encoding="utf-8") as handle:
            code = compile(handle.read(), path, "exec")
        with contextlib.redirect_stdout(tee), contextlib.redirect_stderr(tee):
            exec(code, {"__name__": "__main__", "__file__": path, "__builtins__": __builtins__})
    except SystemExit as error:
        failure = "SystemExit: the --setup script called sys.exit(%r); it must end by returning" % (error.code,)
    except BaseException:
        failure = traceback.format_exc()
    lines = tee.kept.getvalue().splitlines()
    return {"path": path, "output": lines[-40:], "lines": len(lines)}, failure


def _headless_main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if not argv:
        print("look_blender.py: give the arguments file after --")
        return
    with open(long_path(argv[0]), encoding="utf-8") as handle:
        args = json.load(handle)
    setup = args.get("setup")
    setup_info = None
    result = None
    if setup:
        setup_info, failure = run_setup(setup)
        if failure:
            result = {"ok": False, "code": "failed", "tool": "look", "version": LOOK_VERSION,
                      "error": "the --setup script failed: " + failure.strip().splitlines()[-1],
                      "detail": failure}
    if result is None:
        try:
            result = run(args)
        except Exception:
            result = {"ok": False, "code": "failed", "error": traceback.format_exc().strip().splitlines()[-1],
                      "detail": traceback.format_exc()}
    if setup_info is not None:
        result["setup"] = setup_info
    save_as = args.get("save_as")
    if save_as and result.get("ok"):
        if any(same_file(save_as, p) for p in (bpy.data.filepath, args.get("input_blend")) if p):
            result["ok"], result["code"] = False, "refused"
            result["error"] = "--save-as is the input file; it is never overwritten"
        else:
            bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(save_as), copy=True, check_existing=False)
            result["saved_as"] = os.path.abspath(save_as)
    path = args.get("result_path")
    text = json.dumps(result)
    if path:
        with open(long_path(path + ".part"), "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(long_path(path + ".part"), long_path(path))
    print("LOOK_RESULT_JSON " + text)


if __name__ == "__main__":
    _headless_main()

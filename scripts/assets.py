#!/usr/bin/env python3
# Portions adapted from MCP for Blender (https://github.com/ahujasid/mcp-for-blender, commit 25e1fe0), Copyright (c) 2025 Siddharth Ahuja, MIT License; see THIRD_PARTY_NOTICES.md.
# SPDX-License-Identifier: GPL-3.0-or-later
"""Find an asset and put it in a Blender scene the right way.

  assets.py search --source polyhaven --type texture rock wall --limit 5 --thumbs ./thumbs
  assets.py categories --source polyhaven --type texture
  assets.py fetch polyhaven rubber_duck_toy --res 1k
  assets.py import polyhaven kloppenheim_06_puresky --strength 0.8 --rotation 90      # LIVE, the bridge
  assets.py import polyhaven rock_wall_08 --apply-to Floor,Wall
  assets.py import polyhaven rubber_duck_toy --size 0.15 --at 1,0,0.8 --blend in.blend --save-as out.blend
  assets.py import-file fixtures/chair.glb --size 0.9 --new --save-as test_scene.blend
  assets.py credits

WHERE IT RUNS. LIVE (the default) sends the work to a running Blender through the Blender Lab MCP bridge
(BLENDER_MCP_HOST / BLENDER_MCP_PORT, default localhost:9876, like bmcp.py). The bridge is asked who it is
before anything is downloaded: --expect-file PATH and --expect-pid N refuse when the Blender that answers is
not that one, and nothing is touched. HEADLESS (--blend FILE or --new) starts --blender EXE (else $BLENDER_EXE,
else blender on PATH, else the usual install folders; it says which) with --background --factory-startup
--disable-autoexec, does the job and exits. A headless import keeps nothing unless --save-as PATH is given
(never the input file); --dry-run only tries it. Nothing is ever written beside your .blend.

WHAT IT DOES TO A LIVE SCENE. Only the import itself: a model goes into a new collection named for it, an
HDRI becomes a new world (the old one is kept with a fake user unless --replace-world), a texture becomes a
new material (put on --apply-to objects, replacing their materials, which the JSON lists). Frame, mode,
selection, active object and collection are put back, and a fingerprint of the scene taken before and after
must show no other change, or the command fails loudly. A failed import removes everything it made. Name
collisions never rename your datablocks: the new ones get Blender's .001 suffixes, listed under "renamed".
A .blend is appended, never opened, and every driver in it that would run Python is muted before anything
is evaluated (on objects, meshes, shape keys and node trees alike), even when the Blender runs scripts.

SIZES AND UNITS. --size, --at and a texture's real-world size are metres; a scene with another unit scale
(centimetres: Unit Scale 0.01) gets them converted, and the JSON gives metres and Blender units. A texture's
tiling is worked out at import from each --apply-to object's current size and its render UV map (or box
projection without one): size the object first, and import again after resizing it. An object with more than
one material slot needs --slot N or --slot all. A model's own lights and cameras are left out (they would
light or film your scene) unless --keep-lights / --keep-cameras.

FILES. Every download goes into a cache (BLENDER_AI_ASSETS_DIR; default %LOCALAPPDATA%/blender-ai-assets/cache,
~/Library/Caches/blender-ai-assets, or ~/.cache/blender-ai-assets), checked against the size and checksum
the source publishes; an entry counts only once its manifest is written, so a cut-off download is never used,
and a second fetch is a cache hit with no request at all. A download over --max-mb (500 by default) or larger
than the free disk space is refused before it starts; a server that sends too slowly is given up on. Archives
are extracted only when every member stays inside its folder, and a downloaded glTF may only point at files
inside its own folder. On Windows a cache path of 260 characters or more is handed to Blender in its short
8.3 form, or refused plainly (a shorter BLENDER_AI_ASSETS_DIR fixes it). Downloads happen here, before Blender
is asked to do anything, so the bridge never waits on the network; the import itself runs as one synchronous
bridge request (the bridge sets no limit on those; --timeout is how long this side waits, 600 s by default).

SOURCES. polyhaven: HDRIs, textures and models, CC0, no key. polypizza: low-poly models, CC0 or CC-BY
(credit required), needs POLYPIZZA_API_KEY. sketchfab: models under many licences, needs SKETCHFAB_API_TOKEN;
the licence of each model is recorded. Every import appends one line (date, source, id, name, author, licence,
page) to the local text datablock ASSET_CREDITS and puts the same in the JSON (a local file is named by its
file name and checksum, never by its folder).

OUTPUT. One JSON line on stdout, with "code" on failure; human lines on stderr, and on failure one line
"assets: FAILED (<code>): <reason>". Exit codes: 0 ok, 1 failed, 2 bad arguments (nothing was done),
3 refused (wrong Blender, too big, would overwrite; nothing was touched), 4 no Blender reachable,
5 Blender was left changed. Needs Python 3.10+, standard library only.
"""
import argparse
import errno
import glob
import hashlib
import http.client
import json
import math
import os
import posixpath
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path

VERSION = "1.1"
USER_AGENT = "blender-ai-assets/%s (+https://github.com/AndreiFlau/blender-mcp-skill)" % VERSION
HERE = Path(__file__).resolve().parent
BLENDER_PART = HERE / "assets_blender.py"

SOURCES = ("polyhaven", "polypizza", "sketchfab")
KINDS = ("hdri", "texture", "model")
PH_API_TYPE = {"hdri": "hdris", "texture": "textures", "model": "models"}
PH_TYPE_NAMES = {0: "hdri", 1: "texture", 2: "model"}
DEFAULT_FORMAT = {"hdri": "hdr", "texture": "jpg", "model": "gltf"}
PH_FORMATS = {"hdri": ("hdr", "exr"), "texture": ("jpg", "png", "exr"), "model": ("gltf", "fbx", "blend", "usd")}
SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
RES_RE = re.compile(r"^\d{1,2}k$")
MODEL_EXTENSIONS = (".glb", ".gltf", ".fbx", ".obj", ".usd", ".usda", ".usdc", ".usdz", ".stl", ".ply", ".abc", ".blend")
HDRI_EXTENSIONS = (".hdr", ".exr")
API_TTL = 12 * 3600
MAX_JSON = 64 << 20
MAX_UNPACKED = 8 << 30
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
EXIT = {"ok": 0, "failed": 1, "bad_args": 2, "refused": 3, "unreachable": 4, "left_changed": 5}
OFFLINE = False

# Slow servers: one socket read may wait this long, a whole API answer this long, and a download must keep up
# MIN_RATE bytes per second over every RATE_WINDOW seconds, or it is given up on with a plain reason.
API_READ_TIMEOUT = 30
FILE_READ_TIMEOUT = 60
HEADER_SECONDS = 90
JSON_SECONDS = 60
MIN_RATE = 20 * 1024
RATE_WINDOW = 30
# Big downloads: refused above this unless --max-mb says more; thumbnails are capped; this much disk stays free.
MAX_MB_DEFAULT = 500
LIMIT_BYTES = MAX_MB_DEFAULT << 20
THUMB_MAX = 10 << 20
DISK_SPARE = 200 << 20
WIN_MAX_PATH = 260

# Poly Haven /files keys and what they drive. The casing is theirs and it matters ("Diffuse" but "nor_gl").
PH_MAP_ROLES = {"Diffuse": "base_color", "Rough": "roughness", "Metal": "metallic", "nor_gl": "normal",
                "Displacement": "displacement", "alpha": "alpha", "Alpha": "alpha", "opacity": "alpha",
                "Opacity": "alpha", "emission": "emission", "Emission": "emission", "emissive": "emission",
                "anisotropy_strength": "anisotropic", "anisotropy_rotation": "anisotropic_rotation"}
PH_SKIP_REASONS = {
    "AO": "ambient occlusion has no input on the Principled BSDF; renderers work occlusion out themselves",
    "arm": "the AO, roughness and metal maps packed into one file; the separate maps are used",
    "nor_dx": "the DirectX copy of the normal map; the OpenGL one (nor_gl) is used",
    "rough_ao": "roughness with occlusion baked in; the plain roughness map is used",
    "spec_ior": "drives IOR through a remap Poly Haven does not publish",
    "Bump": "a bump height map; this tool does not wire bump maps",
    "spec": "a specular-level map; the Principled BSDF takes specular from IOR",
}
MTLX_CODES = {"diff": "Diffuse", "rough": "Rough", "metal": "Metal", "nor_gl": "nor_gl", "nor_dx": "nor_dx",
              "disp": "Displacement", "ao": "AO", "arm": "arm"}
PP_CATEGORIES = {"food & drink": 0, "clutter": 1, "weapons": 2, "transport": 3, "furniture & decor": 4,
                 "objects": 5, "nature": 6, "animals": 7, "buildings": 8, "people & characters": 9,
                 "scenes & levels": 10, "other": 11, "food": 0, "drink": 0, "weapon": 2, "vehicles": 3,
                 "vehicle": 3, "furniture": 4, "decor": 4, "props": 5, "prop": 5, "plants": 6, "animal": 7,
                 "building": 8, "architecture": 8, "people": 9, "characters": 9, "character": 9, "scenes": 10}
PP_CATEGORY_NAMES = ["Food & Drink", "Clutter", "Weapons", "Transport", "Furniture & Decor", "Objects", "Nature",
                     "Animals", "Buildings", "People & Characters", "Scenes & Levels", "Other"]


class Fail(Exception):
    """One plain reason, with the exit code it maps to (EXIT) and any facts for the JSON."""

    def __init__(self, message, code="failed", **extra):
        super().__init__(message)
        self.code = code
        self.extra = extra


def say(*parts):
    print(*parts, file=sys.stderr, flush=True)


def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


def json_safe(value):
    """Strict JSON: NaN and infinities become strings, so any parser (jq, JSON.parse) reads the line."""
    if isinstance(value, float) and not math.isfinite(value):
        return "nan" if value != value else ("inf" if value > 0 else "-inf")
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value


# ------------------------------------------------------------------------------------------------ arguments

def finite(name, value, low=None, strict=False):
    """A number argument that is a real number (not nan/inf) and, with `low`, at least (strict: above) it."""
    if value is None:
        return None
    if not math.isfinite(value):
        raise Fail("%s must be a real number, not %s" % (name, value), "bad_args")
    if low is not None and (value < low or (strict and value == low)):
        raise Fail("%s must be %s %g, not %g" % (name, "more than" if strict else "at least", low, value), "bad_args")
    return value


def parse_at(text):
    try:
        values = [float(v) for v in text.split(",")]
    except ValueError:
        values = []
    if len(values) != 3:
        raise Fail("--at takes x,y,z in metres, like 1,0,0.75", "bad_args")
    for v in values:
        finite("--at", v)
    return values


def check_common(args):
    """Every number and limit checked before any request, download or Blender."""
    global LIMIT_BYTES
    if getattr(args, "max_mb", None) is not None:
        finite("--max-mb", args.max_mb, 0, strict=True)
        LIMIT_BYTES = int(args.max_mb * (1 << 20))
    if getattr(args, "timeout", None) is not None:
        finite("--timeout", args.timeout, 0, strict=True)
    res = getattr(args, "res", None)
    if res is not None and not RES_RE.match(res):
        raise Fail("%r is not a resolution (1k, 2k, 4k, 8k ...)" % res, "bad_args")


def options_from(args, meta=None):
    meta = meta or {}
    apply_to = [n.strip() for n in (args.apply_to or "").split(",") if n.strip()]
    slot = None
    if args.slot is not None:
        if str(args.slot).strip().lower() == "all":
            slot = "all"
        elif str(args.slot).strip().isdigit():
            slot = int(args.slot)
        else:
            raise Fail("--slot takes a slot number (0, 1, ...) or 'all'", "bad_args")
    finite("--size", args.size, 0, strict=True)
    finite("--rotate", args.rotate)
    finite("--strength", args.strength, 0)
    finite("--rotation", args.rotation)
    return {"size": args.size, "at": parse_at(args.at), "ground": not args.no_ground, "rotate": args.rotate,
            "name": args.name, "strength": args.strength, "rotation": args.rotation,
            "replace_world": args.replace_world, "apply_to": apply_to, "slot": slot, "pack": args.pack,
            "keep_lights": args.keep_lights, "keep_cameras": args.keep_cameras,
            "picks": [n.strip() for n in (args.pick or "").split(",") if n.strip()],
            "prefer_collections": meta.get("prefer_collections") or []}


def secret(var, label, where):
    value = os.environ.get(var, "")
    if not value.strip():
        raise Fail("%s needs %s: set %s (%s). Nothing was requested" % (label, "an API key" if "KEY" in var
                                                                       else "an API token", var, where))
    value = value.strip()
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise Fail("%s contains a control character (a pasted line break?); set it to the key alone. "
                   "Nothing was requested" % var)
    return value


# ------------------------------------------------------------------------------------------------ network

def api_base(source):
    env = {"polyhaven": ("BLENDER_AI_ASSETS_POLYHAVEN_API", "https://api.polyhaven.com"),
           "polypizza": ("BLENDER_AI_ASSETS_POLYPIZZA_API", "https://api.poly.pizza/v1.1"),
           "sketchfab": ("BLENDER_AI_ASSETS_SKETCHFAB_API", "https://api.sketchfab.com/v3")}[source]
    return os.environ.get(env[0], env[1]).rstrip("/")


def check_url(url, what):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOCAL_HOSTS):
        return
    raise Fail("%s: refusing to fetch %r (only https, or http to this machine)" % (what, url))


class _Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl, "a redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


OPENER = urllib.request.build_opener(_Redirects)


class HTTPStatus(Exception):
    def __init__(self, status, url, body=b"", headers=None):
        super().__init__("HTTP %d" % status)
        self.status, self.url, self.body, self.headers = status, url, body, headers or {}


def _open(url, headers=None, timeout=None, what="the request"):
    """The response once its headers are in. A server that trickles even those is given up on after
    HEADER_SECONDS (each socket read alone would wait only `timeout`, but a trickle never lets one time out)."""
    if OFFLINE:
        raise Fail("%s needs the network and --offline forbids it" % what)
    timeout = API_READ_TIMEOUT if timeout is None else timeout
    check_url(url, what)
    host = urllib.parse.urlsplit(url).hostname
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    for key, value in (headers or {}).items():
        req.add_unredirected_header(key, value)          # never sent on to another host by a redirect
    box = {}

    def attempt():
        try:
            box["resp"] = _open_now(req, url, host, timeout)
        except BaseException as exc:  # noqa: BLE001 - handed over to the caller's thread below
            box["error"] = exc
    worker = threading.Thread(target=attempt, daemon=True)
    worker.start()
    worker.join(HEADER_SECONDS)
    if worker.is_alive():
        raise Fail("%s took more than %d s to start its answer (it sends too slowly); stopped" % (host, HEADER_SECONDS))
    if "error" in box:
        raise box["error"]
    return box["resp"]


def _open_now(req, url, host, timeout):
    try:
        return OPENER.open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read(65536)
        except Exception:  # noqa: BLE001
            pass
        raise HTTPStatus(exc.code, url, body, dict(exc.headers or {})) from None
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise Fail("%s did not answer within %d s" % (host, timeout)) from None
        reason = getattr(reason, "strerror", None) or str(reason)
        raise Fail("no network: could not reach %s (%s)" % (host, reason)) from None
    except (socket.timeout, TimeoutError):
        raise Fail("%s did not answer within %d s" % (host, timeout)) from None
    except (ConnectionError, OSError, http.client.HTTPException) as exc:
        raise Fail("no network: could not reach %s (%s)" % (host, exc)) from None


def _read_some(resp, size):
    """Whatever has arrived (at most `size`), without waiting for all of it: a trickle cannot hold us forever."""
    read1 = getattr(resp, "read1", None)
    return read1(size) if read1 is not None else resp.read(size)


def read_answer(resp, what, limit=MAX_JSON, seconds=None):
    """A whole API answer, within `seconds` in all; a stall or a trickle ends with a plain reason."""
    seconds = JSON_SECONDS if seconds is None else seconds
    start, got, chunks = time.monotonic(), 0, []
    with resp:
        while True:
            try:
                chunk = _read_some(resp, 65536)
            except (socket.timeout, TimeoutError):
                raise Fail("%s stalled: no data for %d s" % (what, API_READ_TIMEOUT)) from None
            except (ConnectionError, OSError, http.client.HTTPException) as exc:
                raise Fail("%s broke off (%s)" % (what, exc)) from None
            if not chunk:
                break
            got += len(chunk)
            if got > limit:
                raise Fail("%s answered with more than %s; refused" % (what, human_bytes(limit)))
            chunks.append(chunk)
            if time.monotonic() - start > seconds:
                raise Fail("%s took more than %d s to answer (the server sends too slowly); stopped" % (what, seconds))
    return b"".join(chunks)


def parse_json(data, what):
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise Fail("%s did not answer with JSON (starts %r)" % (what, data[:60])) from None


def http_json(url, headers=None, what="the request"):
    resp = _open(url, headers, what=what)
    return parse_json(read_answer(resp, what), what)


def need_dict(value, what):
    if not isinstance(value, dict):
        raise Fail("%s answered in a shape this tool does not know (%s, not an object)" % (what, type(value).__name__))
    return value


def _disk_full(exc):
    return getattr(exc, "errno", None) in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)) or \
        getattr(exc, "winerror", None) in (39, 112)


def _free_bytes(path):
    path = Path(path)
    while not path.exists() and path.parent != path:
        path = path.parent
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def check_room(total, near, what):
    """Refuse before the first byte when the download is over --max-mb or the disk has no room for it."""
    if total > LIMIT_BYTES:
        raise Fail("%s is %s to download, over the %s limit; pass --max-mb %d to allow it"
                   % (what, human_bytes(total), human_bytes(LIMIT_BYTES), math.ceil(total / (1 << 20)) + 1),
                   "refused", download_bytes=total)
    free = _free_bytes(near)
    if free is not None and total + DISK_SPARE > free:
        raise Fail("not enough free disk space for %s: it needs %s and %s is free where the cache is (%s)"
                   % (what, human_bytes(total), human_bytes(free), near), "refused", download_bytes=total)


def download(url, dest, size=None, md5=None, headers=None, what=None, max_bytes=None):
    """Stream url to dest (a new file). Checks the published size and md5, the server's own length, a size cap
    and a minimum rate (MIN_RATE over every RATE_WINDOW seconds)."""
    what = what or posixpath.basename(urllib.parse.urlsplit(url).path) or "a file"
    max_bytes = LIMIT_BYTES if max_bytes is None else max_bytes
    resp = _open(url, headers, timeout=FILE_READ_TIMEOUT, what="downloading %s" % what)
    got = 0
    h_md5, h_sha = hashlib.md5(), hashlib.sha256()
    length = resp.headers.get("Content-Length")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        f = open(dest, "xb")                     # a new file only: nothing that was there is ever overwritten
    except OSError as exc:
        resp.close()
        if _disk_full(exc):
            raise Fail("disk full while writing %s; nothing was kept" % dest) from None
        raise Fail("could not create %s: %s" % (dest, exc)) from None
    window_start, window_got = time.monotonic(), 0
    try:
        with resp, f:
            while True:
                chunk = _read_some(resp, 1 << 20)
                if not chunk:
                    break
                got += len(chunk)
                if got > max_bytes:
                    raise Fail("%s is more than %s; stopped (pass --max-mb to allow more)" % (what, human_bytes(max_bytes)))
                if size is not None and got > int(size):
                    raise Fail("%s: the server sends more than the %d bytes the source publishes; stopped" % (what, int(size)))
                h_md5.update(chunk)
                h_sha.update(chunk)
                f.write(chunk)
                now = time.monotonic()
                if now - window_start >= RATE_WINDOW:
                    rate = (got - window_got) / (now - window_start)
                    if rate < MIN_RATE:
                        raise Fail("the download of %s is too slow (%.1f KB/s over the last %d s); stopped, nothing kept"
                                   % (what, rate / 1024.0, RATE_WINDOW))
                    window_start, window_got = now, got
    except Fail:
        _discard(dest)
        raise
    except OSError as exc:
        _discard(dest)
        if _disk_full(exc):
            raise Fail("disk full while writing %s (%s); nothing was kept" % (dest, exc.strerror or exc)) from None
        if isinstance(exc, (socket.timeout, TimeoutError)):
            raise Fail("the download of %s stalled (no data for %d s); nothing was kept" % (what, FILE_READ_TIMEOUT)) from None
        raise Fail("could not write %s: %s" % (dest, exc)) from None
    except Exception as exc:  # http.client.IncompleteRead and friends
        _discard(dest)
        raise Fail("the download of %s broke off (%s); nothing was kept" % (what, exc)) from None
    if length is not None and length.isdigit() and got != int(length):
        _discard(dest)
        raise Fail("the download of %s was cut short: %d of %s bytes; nothing was kept" % (what, got, length))
    if size is not None and got != int(size):
        _discard(dest)
        raise Fail("%s: got %d bytes but the source says it is %d; the download is broken and was thrown away"
                   % (what, got, int(size)))
    if md5 and h_md5.hexdigest() != md5.lower():
        _discard(dest)
        raise Fail("%s: the checksum does not match what the source publishes; the download is damaged and "
                   "was thrown away" % what)
    return {"size": got, "md5": h_md5.hexdigest(), "sha256": h_sha.hexdigest(),
            "content_type": resp.headers.get("Content-Type", "")}


def _discard(path):
    try:
        os.remove(path)
    except OSError:
        pass


# ------------------------------------------------------------------------------------------------ cache

def cache_root():
    env = os.environ.get("BLENDER_AI_ASSETS_DIR")
    if env:
        return Path(env).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "blender-ai-assets" / "cache"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "blender-ai-assets"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "blender-ai-assets"


def safe_name(text):
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(text))[:120] or "_"


def entry_dir(source, ident, variant):
    return cache_root() / source / safe_name(ident) / safe_name(variant)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_entry(directory):
    """(manifest, None) for a sound entry, (None, why) for a damaged one, (None, None) when absent."""
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        return None, None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, "its manifest cannot be read (%s)" % exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list) or \
            not isinstance(manifest.get("meta"), dict):
        return None, "its manifest is not one this tool wrote"
    for item in manifest["files"]:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            return None, "its manifest lists a file it cannot read"
        path = directory / item["path"]
        if not path.is_file():
            return None, "%s is missing" % item["path"]
        actual = path.stat().st_size
        if actual != item.get("size"):
            return None, "%s is %d bytes, the cache recorded %s" % (item["path"], actual, item.get("size"))
        if _sha256(path) != item.get("sha256"):
            return None, "%s changed since it was fetched (its checksum differs)" % item["path"]
    return manifest, None


def write_text(path, text):
    """Write a small file through a temporary name; a full disk is a plain reason, never a half file."""
    path = Path(path)
    tmp = path.with_name(path.name + ".%s.part" % uuid.uuid4().hex[:6])
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError as exc:
        _discard(tmp)
        if _disk_full(exc):
            raise Fail("disk full while writing %s; nothing was kept" % path) from None
        raise Fail("could not write %s: %s" % (path, exc)) from None


def commit_entry(partial, final, manifest):
    write_text(partial / "manifest.json", json.dumps(manifest, indent=1))
    if final.exists():
        trash = final.with_name(final.name + ".old-" + uuid.uuid4().hex[:8])
        os.replace(final, trash)
        shutil.rmtree(trash, ignore_errors=True)
    os.replace(partial, final)


def new_partial(final):
    final.parent.mkdir(parents=True, exist_ok=True)
    for stale in final.parent.glob(final.name + ".partial-*"):
        try:
            if time.time() - stale.stat().st_mtime > 3600:
                shutil.rmtree(stale, ignore_errors=True)
        except OSError:
            pass
    partial = final.with_name(final.name + ".partial-" + uuid.uuid4().hex[:8])
    partial.mkdir()
    return partial


WIN_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$", re.IGNORECASE)


def member_problem(name, target):
    """Why an archive member or include path may not be written under target, or None."""
    n = name.replace("\\", "/")
    if not n or "\x00" in n:
        return "has an empty or broken name"
    if n.startswith("/") or re.match(r"^[A-Za-z]:", n):
        return "is an absolute path"
    parts = n.split("/")
    if ".." in parts:
        return "climbs out with '..'"
    for part in parts:
        if part in ("", "."):
            continue
        # Windows rewrites these when writing, so what lands on disk would not be what was checked.
        if part != part.rstrip(". "):
            return "has a name part ending in a dot or a space (%r)" % part
        if re.search(r'[<>:"|?*\x00-\x1f]', part):
            return "has a character Windows does not allow in names (%r)" % part
        if WIN_RESERVED.match(part):
            return "uses a name Windows keeps for a device (%r)" % part
    real_target = os.path.realpath(target)
    real = os.path.realpath(os.path.join(real_target, n))
    if real != real_target and not real.startswith(real_target + os.sep):
        return "would land outside its folder"
    return None


def extract_zip(archive, target):
    try:
        with zipfile.ZipFile(archive) as z:
            infos = z.infolist()
            total = 0
            for info in infos:
                problem = member_problem(info.filename, target)
                if problem is None and ((info.external_attr >> 16) & 0o170000) == 0o120000:
                    problem = "is a symbolic link"
                if problem:
                    raise Fail("the archive %s has a member %r that %s; refused, nothing extracted"
                               % (archive.name, info.filename, problem))
                total += info.file_size
            if total > MAX_UNPACKED:
                raise Fail("the archive %s would unpack to %s; refused, nothing extracted" % (archive.name, human_bytes(total)))
            free = _free_bytes(target)
            if free is not None and total + DISK_SPARE > free:
                raise Fail("not enough free disk space to unpack %s (%s, %s free); nothing extracted"
                           % (archive.name, human_bytes(total), human_bytes(free)), "refused")
            target.mkdir(parents=True, exist_ok=True)
            real_target = os.path.realpath(target)
            written = []
            for info in infos:
                landed = os.path.realpath(z.extract(info, target))
                if landed != real_target and not landed.startswith(real_target + os.sep):
                    raise Fail("the archive %s put %r outside its folder; refused" % (archive.name, info.filename))
                if not info.is_dir():
                    written.append(Path(os.path.relpath(landed, real_target)).as_posix())
            return written
    except zipfile.BadZipFile:
        raise Fail("%s is not a valid zip archive; refused" % archive.name) from None
    except OSError as exc:
        if _disk_full(exc):
            raise Fail("disk full while unpacking %s; nothing was kept" % archive.name) from None
        raise Fail("could not unpack %s: %s (on Windows a path over 260 characters fails; a shorter "
                   "BLENDER_AI_ASSETS_DIR helps)" % (archive.name, exc)) from None


def check_glb(path):
    with open(path, "rb") as f:
        head = f.read(12)
    if len(head) < 12 or head[:4] != b"glTF":
        raise Fail("%s is not a glTF binary (no glTF header)" % path.name)
    declared = int.from_bytes(head[8:12], "little")
    actual = path.stat().st_size
    if declared != actual:
        raise Fail("%s is broken: its header says %d bytes, the file has %d" % (path.name, declared, actual))


def gltf_uri_problems(path):
    """Images and buffers a glTF names that are not inside its own folder (or are URLs): a downloaded model may
    otherwise make Blender read, and --pack store, any file on the disk."""
    path = Path(path)
    root = path.parent
    data = path.read_bytes()
    try:
        if path.suffix.lower() == ".glb":
            if len(data) < 20 or data[16:20] != b"JSON":
                return []
            size = int.from_bytes(data[12:16], "little")
            doc = json.loads(data[20:20 + size].decode("utf-8"))
        else:
            doc = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        return [("(the file itself)", "is not readable glTF JSON")]
    if not isinstance(doc, dict):
        return [("(the file itself)", "is not a glTF document")]
    real_root = os.path.realpath(root)
    problems = []
    for group in ("images", "buffers"):
        for item in doc.get(group) or []:
            uri = item.get("uri") if isinstance(item, dict) else None
            if not isinstance(uri, str) or not uri or uri.startswith("data:"):
                continue
            name = urllib.parse.unquote(uri)
            if re.match(r"^[A-Za-z][A-Za-z0-9+.-]+:", name):
                problems.append((uri, "is a URL, not a file beside the model"))
                continue
            if name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", name):
                problems.append((uri, "is an absolute path"))
                continue
            real = os.path.realpath(os.path.join(real_root, name))
            if real != real_root and not real.startswith(real_root + os.sep):
                problems.append((uri, "points outside the model's folder"))
    return problems


def refuse_gltf_escape(main, what):
    if str(main).lower().endswith((".gltf", ".glb")):
        problems = gltf_uri_problems(main)
        if problems:
            uri, why = problems[0]
            raise Fail("%s names the file %r, which %s; refused (a downloaded model may only use files inside its "
                       "own folder)" % (what, uri, why))


def fetch_plan(source, ident, variant, plan, meta, extract=None):
    """Download every file of `plan` into a fresh partial folder, check them, then commit the entry."""
    final = entry_dir(source, ident, variant)
    total = sum(int(i["size"]) for i in plan if isinstance(i.get("size"), int))
    check_room(total, final, "%s %s (%s)" % (source, meta.get("name") or ident, variant))
    partial = new_partial(final)
    try:
        records = []
        for item in plan:
            problem = member_problem(item["rel"], partial)
            if problem:
                raise Fail("%s's file list names %r, which %s; refused" % (meta.get("name") or ident, item["rel"], problem))
            dest = partial / item["rel"]
            got = download(item["url"], dest, item.get("size"), item.get("md5"), item.get("headers"),
                           max_bytes=item.get("max_bytes"))
            if item.get("check") == "glb":
                check_glb(dest)
            records.append({"path": item["rel"], "size": got["size"], "sha256": got["sha256"], "md5": got["md5"]})
        if extract:
            archive = partial / extract["archive"]
            names = extract_zip(archive, partial / extract["into"])
            for name in names:
                path = partial / extract["into"] / name
                rel = path.relative_to(partial).as_posix()
                records.append({"path": rel, "size": path.stat().st_size, "sha256": _sha256(path)})
            meta = dict(meta)
            meta["main"] = extract["pick"](partial / extract["into"], names)
            meta["archive"] = {"name": extract["archive"], "sha256": next(r["sha256"] for r in records
                                                                          if r["path"] == extract["archive"])}
            records = [r for r in records if r["path"] != extract["archive"]]
            os.remove(archive)                   # unpacked and checked: the zip would only double the cache
        if meta.get("main"):
            refuse_gltf_escape(partial / meta["main"], "the model's glTF")
        manifest = {"format": 1, "source": source, "id": ident, "variant": variant, "files": records, "meta": meta,
                    "fetched": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        commit_entry(partial, final, manifest)
        return manifest
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise


def lookup(source, ident, variant, offline):
    """A sound cache entry, or None (with a note on stderr when a damaged one is thrown away)."""
    final = entry_dir(source, ident, variant)
    manifest, damaged = read_entry(final)
    if manifest is not None:
        return manifest, "hit"
    if damaged:
        if offline:
            raise Fail("the cached %s %s is damaged (%s) and --offline forbids fetching it again"
                       % (source, ident, damaged))
        say("cache: %s %s is damaged (%s); fetching it again" % (source, ident, damaged))
        return None, "repaired"
    if offline:
        raise Fail("%s %s (%s) is not in the cache and --offline forbids fetching it" % (source, ident, variant))
    return None, "miss"


def cached_api_json(url, headers=None, what="the request"):
    """API answers that rarely change (Poly Haven's asset list): kept 12 h, then revalidated by ETag."""
    store = cache_root() / "_api" / (hashlib.sha1(url.encode("utf-8")).hexdigest()[:20] + ".json")
    entry = None
    if store.is_file():
        try:
            entry = json.loads(store.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            entry = None
        if not isinstance(entry, dict) or "payload" not in entry or not isinstance(entry.get("fetched", 0), (int, float)):
            entry = None                         # damaged: fetched again below (or a plain refusal offline)
    if entry is not None and (OFFLINE or time.time() - entry.get("fetched", 0) < API_TTL):
        return entry["payload"]
    if OFFLINE:
        raise Fail("%s is not in the cache (or its cached copy is damaged) and --offline forbids fetching it" % what)
    extra = dict(headers or {})
    if entry is not None and entry.get("etag"):
        extra["If-None-Match"] = str(entry["etag"])
    try:
        resp = _open(url, extra, what=what)
    except HTTPStatus as exc:
        if exc.status == 304 and entry is not None:
            entry["fetched"] = time.time()
            try:
                write_text(store, json.dumps(entry))
            except Fail:
                pass
            return entry["payload"]
        raise
    etag = resp.headers.get("ETag")
    payload = parse_json(read_answer(resp, what), what)
    try:
        write_text(store, json.dumps({"url": url, "etag": etag, "fetched": time.time(), "payload": payload}))
    except Fail:
        pass                                     # only a speed-up; the answer is still good
    return payload


# ------------------------------------------------------------------------------------------------ Poly Haven

def ph_page(slug):
    return "https://polyhaven.com/a/" + urllib.parse.quote(slug, safe="")


def ph_thumb(url, size=512):
    """Poly Haven's CDN resizes from the query string; keep its cache-busting v, ask for a PNG."""
    parts = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    query.update({"width": str(size), "height": str(size), "format": "png"})
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def ph_sizes(max_resolution):
    if not isinstance(max_resolution, (list, tuple)) or not max_resolution:
        return []
    try:
        top = int(max_resolution[0]) // 1024
    except (TypeError, ValueError):
        return []
    sizes = [k for k in (1, 2, 4, 8, 16) if k <= top]
    if top and top not in sizes:
        sizes.append(top)
    return ["%dk" % k for k in sizes]


def ph_names(authors):
    if isinstance(authors, dict):
        return sorted(str(a) for a in authors)
    if isinstance(authors, (list, tuple)):
        return [str(a) for a in authors]
    return []


def ph_dimensions(dims):
    try:
        return [float(d) / 1000.0 for d in dims] if dims else None
    except (TypeError, ValueError):
        return None


def ph_record(slug, rec):
    rec = rec if isinstance(rec, dict) else {}
    kind = PH_TYPE_NAMES.get(rec.get("type"), "unknown")
    names = ph_names(rec.get("authors"))
    dims = ph_dimensions(rec.get("dimensions"))
    out = {"source": "polyhaven", "id": slug, "name": rec.get("name") or slug, "type": kind, "license": "CC0",
           "author": ", ".join(names), "authors": names, "sizes": ph_sizes(rec.get("max_resolution")),
           "max_resolution": rec.get("max_resolution"),
           "dimensions_m": [round(d, 4) for d in dims] if dims else None,
           "categories": [c for c in [rec.get("category")] if c] + list(rec.get("categories") or []),
           "tags": rec.get("tags") or [], "thumbnail_url": rec.get("thumbnail_url"), "page_url": ph_page(slug),
           "downloads": rec.get("download_count")}
    if rec.get("polycount"):
        out["polycount"] = rec["polycount"]
    return out


def ph_assets(kind, category):
    params = {}
    if kind:
        params["type"] = PH_API_TYPE[kind]
    if category:
        params["category"] = category
    url = api_base("polyhaven") + "/assets" + ("?" + urllib.parse.urlencode(params) if params else "")
    try:
        assets = cached_api_json(url, what="Poly Haven's asset list")
    except HTTPStatus as exc:
        if exc.status == 400 and category:
            raise Fail("Poly Haven does not know the category %r (%s); `assets.py categories --source polyhaven "
                       "--type %s` lists the ones it has" % (category, exc.body[:80].decode("utf-8", "replace"),
                                                               kind or "texture")) from None
        raise Fail("Poly Haven's asset list answered HTTP %d" % exc.status) from None
    return need_dict(assets, "Poly Haven's asset list")


def ph_keyword_match(words, assets):
    terms = [t for t in words.lower().split() if t]
    scored = []
    for slug, rec in assets.items():
        rec = rec if isinstance(rec, dict) else {}
        hay = " ".join([slug.replace("_", " "), str(rec.get("name") or ""), " ".join(map(str, rec.get("tags") or [])),
                        str(rec.get("category") or ""), " ".join(map(str, rec.get("categories") or []))]).lower()
        hits = sum(1 for t in terms if t in hay)
        if hits:
            scored.append((hits, rec.get("download_count") or 0, slug))
    scored.sort(reverse=True)
    return [s for _h, _d, s in scored]


def ph_search(words, kind, category, limit):
    assets = ph_assets(kind, category)
    query = " ".join(words).strip().lower()
    note = None
    if query and OFFLINE:
        ranked = ph_keyword_match(query, assets)
        note = "offline: plain keyword matches over the cached list (Poly Haven's own search needs the network)"
        ordered = ranked
    elif query:
        params = {"q": query}
        if kind:
            params["t"] = PH_API_TYPE[kind]
        url = api_base("polyhaven") + "/search?" + urllib.parse.urlencode(params)
        try:
            answer = http_json(url, what="Poly Haven search")
            if not isinstance(answer, dict) or not isinstance(answer.get("results"), list):
                raise Fail("Poly Haven search answered without a result list (keys %s); this tool does not know "
                           "that shape" % (sorted(answer)[:6] if isinstance(answer, dict) else type(answer).__name__))
            ranked = [r["slug"] for r in answer["results"] if isinstance(r, dict) and isinstance(r.get("slug"), str)]
        except HTTPStatus as exc:
            if exc.status == 429:
                raise Fail("Poly Haven is rate limiting searches from this address; retry in %s s"
                           % exc.headers.get("Retry-After", "a few")) from None
            if exc.status != 503:
                raise Fail("Poly Haven search answered HTTP %d" % exc.status) from None
            ranked = ph_keyword_match(query, assets)     # what the API asks for when its search is down
            note = "Poly Haven's search was unavailable; these are plain keyword matches"
        ordered = [s for s in ranked if s in assets]     # its ranking, intersected with the filters
    else:
        ordered = sorted(assets, key=lambda s: (assets[s].get("download_count") if isinstance(assets[s], dict) else 0) or 0,
                         reverse=True)
    return [ph_record(s, assets[s]) for s in ordered[:limit]], len(ordered), note


def ph_categories(kind):
    url = api_base("polyhaven") + "/taxonomy/" + PH_API_TYPE[kind]
    try:
        tax = need_dict(cached_api_json(url, what="Poly Haven's categories"), "Poly Haven's categories")
    except HTTPStatus as exc:
        raise Fail("Poly Haven's categories answered HTTP %d" % exc.status) from None
    paths = []

    def walk(nodes):
        for node in nodes or []:
            if isinstance(node, dict):
                if node.get("path"):
                    paths.append(node["path"])
                walk(node.get("children"))
    walk(tax.get("categories"))
    return paths


def _ph_info_ok(info):
    return isinstance(info, dict) and info.get("type") in PH_TYPE_NAMES


def ph_info(slug, offline):
    """Poly Haven's record of an asset (cached beside its entries), or None offline when it is not cached."""
    store = cache_root() / "polyhaven" / safe_name(slug) / "info.json"
    if store.is_file():
        try:
            info = json.loads(store.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            info = None
        if _ph_info_ok(info):
            return info
        if not offline:
            say("cache: the stored record of %s is damaged; asking Poly Haven again" % slug)
    if offline:
        return None
    try:
        info = http_json(api_base("polyhaven") + "/info/" + urllib.parse.quote(slug), what="Poly Haven")
    except HTTPStatus as exc:
        if exc.status == 404:
            raise Fail("Poly Haven has no asset %r (HTTP 404)" % slug) from None
        raise Fail("Poly Haven answered HTTP %d for %r" % (exc.status, slug)) from None
    if not isinstance(info, dict) or "type" not in info:
        raise Fail("Poly Haven's answer for %r has no asset in it" % slug)
    if info.get("type") not in PH_TYPE_NAMES:
        raise Fail("Poly Haven's answer for %r has an asset type this tool does not know (%r)" % (slug, info.get("type")))
    try:
        write_text(store, json.dumps(info))
    except Fail as exc:
        if "disk full" in str(exc):
            raise
    return info


def ph_cached(slug, kind, res, fmt):
    """A sound cache entry of this asset when its record is not at hand (offline): the manifest knows the rest."""
    for k in ([kind] if kind else KINDS):
        variant = "%s-%s-%s" % (k, res, (fmt or DEFAULT_FORMAT[k]).lower())
        manifest, _damaged = read_entry(entry_dir("polyhaven", slug, variant))
        if manifest is not None:
            return manifest
    return None


def ph_kind(slug, offline):
    info = ph_info(slug, offline)
    if info is not None:
        return PH_TYPE_NAMES[info["type"]]
    for k in KINDS:
        for found in (cache_root() / "polyhaven" / safe_name(slug)).glob(k + "-*/manifest.json"):
            return k
    return None


def ph_offered(files, kind):
    """'resolutions 1k, 2k (formats gltf, fbx)' - what an asset has, for the error when a choice is not there."""
    res, fmts = set(), set()
    for key, by_res in files.items():
        if not isinstance(by_res, dict):
            continue
        for r, by_fmt in by_res.items():
            if isinstance(by_fmt, dict):
                hit = {f for f in by_fmt if f in PH_FORMATS[kind]}
                if hit:
                    res.add(r)
                    fmts |= hit
    order = sorted(res, key=lambda r: int(r[:-1]) if r[:-1].isdigit() else 999)
    return "it has %s (formats %s)" % (", ".join(order) or "nothing", ", ".join(sorted(fmts)) or "none")


def ph_file(entry, what):
    """A Poly Haven file record with a url, a size and an md5, or a plain refusal (no silent unchecked file)."""
    if not isinstance(entry, dict) or not isinstance(entry.get("url"), str) or not entry["url"]:
        raise Fail("Poly Haven's file list gives no download address for %s; this tool does not know that shape" % what)
    size, md5 = entry.get("size"), entry.get("md5")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0 or \
            not isinstance(md5, str) or not re.match(r"^[0-9a-fA-F]{32}$", md5):
        raise Fail("Poly Haven's file list gives no size or checksum for %s; refused, since a damaged download "
                   "could not be noticed" % what)
    return {"url": entry["url"], "size": size, "md5": md5}


def url_name(url):
    return safe_name(posixpath.basename(urllib.parse.urlsplit(url).path)) or "file"


def plan_texture_maps(files, res, fmt):
    format_keys = {k for k, v in files.items() if isinstance(v, dict)
                   and any(isinstance(r, dict) and k in r for r in v.values())}
    offered = {}
    for key, by_res in files.items():
        if key in format_keys or not isinstance(by_res, dict):
            continue
        entry = by_res.get(res, {}).get(fmt) if isinstance(by_res.get(res), dict) else None
        if isinstance(entry, dict):
            offered[key] = entry
    chosen = {k: PH_MAP_ROLES[k] for k in offered if k in PH_MAP_ROLES}
    roles = set(chosen.values())
    if "base_color" not in roles:
        for key in sorted(offered):
            if key.lower().startswith(("col", "diff")) and "dirty" not in key.lower():
                chosen[key] = "base_color"
                break
    if "roughness" not in roles and "rough_ao" in offered:
        chosen["rough_ao"] = "roughness"
    if "normal" not in roles and "nor_dx" in offered:
        chosen["nor_dx"] = "normal_dx"
    if "roughness" not in set(chosen.values()) and "arm" in offered:
        chosen["arm"] = "arm"
    skipped = [{"map": k, "why": PH_SKIP_REASONS.get(k, "no rule for this map")} for k in sorted(offered) if k not in chosen]
    return chosen, skipped, offered


def mtlx_recipe(path, slug):
    """Poly Haven's own MaterialX for the asset: which maps it uses and its displacement scale."""
    import xml.etree.ElementTree as ET
    data = path.read_bytes()
    if len(data) > (1 << 20) or b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        return None
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    keys, scale = [], None
    for node in root.iter("input"):
        if node.get("name") == "file" and node.get("type") == "filename":
            base = posixpath.basename(node.get("value") or "")
            m = re.match(r"^%s_(.+?)_\d+k\.\w+$" % re.escape(slug), base)
            if m:
                keys.append(MTLX_CODES.get(m.group(1), m.group(1)))
    for disp in root.iter("displacement"):
        for node in disp.iter("input"):
            if node.get("name") == "scale":
                try:
                    value = float(node.get("value"))
                except (TypeError, ValueError):
                    continue
                if math.isfinite(value):
                    scale = value
    return {"maps": keys, "displacement_scale": scale}


def ph_fetch(slug, kind, res, fmt, offline):
    if not SLUG_RE.match(slug or ""):
        raise Fail("%r is not a Poly Haven id (letters, digits, _ and - only)" % slug, "bad_args")
    if not RES_RE.match(res):
        raise Fail("%r is not a resolution (1k, 2k, 4k, 8k ...)" % res, "bad_args")
    info = ph_info(slug, offline)
    if info is None:                            # offline, and its record is not cached: the entry knows enough
        manifest = ph_cached(slug, kind, res, fmt)
        if manifest is not None:
            return manifest, "hit"
        raise Fail("Poly Haven %s (%s) is not in the cache and --offline forbids looking it up" % (slug, res))
    actual = PH_TYPE_NAMES[info["type"]]
    if kind and kind != actual:
        raise Fail("Poly Haven's %s is a %s, not a %s" % (slug, actual, kind))
    kind = actual
    fmt = (fmt or DEFAULT_FORMAT[kind]).lower()
    if fmt not in PH_FORMATS[kind]:
        raise Fail("a Poly Haven %s comes as %s, not %s" % (kind, ", ".join(PH_FORMATS[kind]), fmt), "bad_args")
    variant = "%s-%s-%s" % (kind, res, fmt)
    manifest, state = lookup("polyhaven", slug, variant, offline)
    if manifest is not None:
        return manifest, state
    try:
        files = http_json(api_base("polyhaven") + "/files/" + urllib.parse.quote(slug), what="Poly Haven's file list")
    except HTTPStatus as exc:
        raise Fail("Poly Haven's file list for %r answered HTTP %d" % (slug, exc.status)) from None
    files = need_dict(files, "Poly Haven's file list")
    names = ph_names(info.get("authors"))
    meta = {"source": "polyhaven", "id": slug, "name": info.get("name") or slug, "kind": kind, "res": res,
            "format": fmt, "author": ", ".join(names), "authors": names, "license": "CC0",
            "page_url": ph_page(slug), "dimensions_m": ph_dimensions(info.get("dimensions"))}
    plan = []

    def pick(group, key):
        by_res = files.get(group) if isinstance(files.get(group), dict) else {}
        at_res = by_res.get(res) if isinstance(by_res.get(res), dict) else {}
        return at_res.get(key)
    if kind == "hdri":
        entry = pick("hdri", fmt)
        if not entry:
            raise Fail("the HDRI %s has no %s %s; %s" % (slug, res, fmt, ph_offered(files, kind)))
        entry = ph_file(entry, "%s %s %s" % (slug, res, fmt))
        rel = "hdri/" + url_name(entry["url"])
        plan.append(dict(entry, rel=rel))
        meta["main"] = rel
    elif kind == "texture":
        chosen, skipped, offered = plan_texture_maps(files, res, fmt)
        if not chosen:
            raise Fail("the texture %s has no maps at %s %s; %s" % (slug, res, fmt, ph_offered(files, kind)))
        maps = []
        for key, role in chosen.items():
            entry = ph_file(offered[key], "the %s map of %s" % (key, slug))
            rel = "maps/" + url_name(entry["url"])
            plan.append(dict(entry, rel=rel))
            maps.append({"rel": rel, "role": role, "key": key})
        mtlx = pick("mtlx", "mtlx")
        if isinstance(mtlx, dict) and mtlx.get("url"):
            plan.append(dict(ph_file(mtlx, "the MaterialX of %s" % slug), rel="material.mtlx"))
            meta["mtlx"] = "material.mtlx"
        meta.update({"maps": maps, "skipped_maps": skipped})
    else:
        raw = pick(fmt, fmt)
        if not raw:
            raise Fail("the model %s has no %s %s; %s" % (slug, res, fmt, ph_offered(files, kind)))
        entry = ph_file(raw, "%s %s %s" % (slug, res, fmt))
        main = "model/" + url_name(entry["url"])
        plan.append(dict(entry, rel=main))
        include = raw.get("include") or {}
        if not isinstance(include, dict):
            raise Fail("Poly Haven's file list for %s has an include list this tool does not know" % slug)
        for inc_path, inc in include.items():
            problem = member_problem(inc_path, "model")
            if problem:
                raise Fail("Poly Haven's file list for %s names the include %r, which %s; refused" % (slug, inc_path, problem))
            plan.append(dict(ph_file(inc, "the file %s of %s" % (inc_path, slug)), rel="model/" + inc_path.replace("\\", "/")))
        meta["main"] = main
        meta["prefer_collections"] = [slug + "_LOD0", slug]
    manifest = fetch_plan("polyhaven", slug, variant, plan, meta)
    if kind == "texture" and manifest["meta"].get("mtlx"):
        recipe = mtlx_recipe(entry_dir("polyhaven", slug, variant) / "material.mtlx", slug)
        if recipe:
            manifest["meta"]["recipe"] = recipe
            if recipe.get("displacement_scale") is not None:
                manifest["meta"]["displacement_scale"] = recipe["displacement_scale"]
            write_text(entry_dir("polyhaven", slug, variant) / "manifest.json", json.dumps(manifest, indent=1))
    return manifest, state


# ------------------------------------------------------------------------------------------------ Poly Pizza

def pp_key():
    return secret("POLYPIZZA_API_KEY", "Poly Pizza", "free at https://poly.pizza/settings/api")


def pp_record(rec):
    creator = rec.get("Creator") or {}
    ident = rec.get("ID")
    return {"source": "polypizza", "id": ident, "name": rec.get("Title"), "type": "model",
            "license": rec.get("Licence"), "author": creator.get("Username") if isinstance(creator, dict) else None,
            "sizes": [], "tri_count": rec.get("Tri Count"), "animated": bool(rec.get("Animated")),
            "categories": [c for c in [rec.get("Category")] if c], "tags": rec.get("Tags") or [],
            "thumbnail_url": rec.get("Thumbnail"), "page_url": "https://poly.pizza/m/%s" % ident,
            "attribution": rec.get("Attribution")}


def pp_category(category):
    if category is None:
        return None
    text = str(category).strip().lower()
    if text.isdigit() and 0 <= int(text) <= 11:
        return int(text)
    if text in PP_CATEGORIES:
        return PP_CATEGORIES[text]
    raise Fail("Poly Pizza has no category %r; it has %s" % (category, ", ".join(PP_CATEGORY_NAMES)), "bad_args")


def pp_search(words, category, limit):
    key = pp_key()
    params = {"Limit": max(1, min(limit, 32))}         # the API ignores lower-case parameter names
    cat = pp_category(category)
    if cat is not None:
        params["Category"] = cat
    query = " ".join(words).strip()
    if not query and cat is None:
        raise Fail("Poly Pizza needs search words or a --category (it refuses an unfiltered listing)", "bad_args")
    url = api_base("polypizza") + "/search" + ("/" + urllib.parse.quote(query, safe="") if query else "")
    url += "?" + urllib.parse.urlencode(params)
    try:
        answer = http_json(url, {"x-auth-token": key}, what="Poly Pizza search")
    except HTTPStatus as exc:
        if exc.status in (401, 403):
            raise Fail("Poly Pizza refused the API key (HTTP %d)" % exc.status) from None
        raise Fail("Poly Pizza search answered HTTP %d" % exc.status) from None
    answer = need_dict(answer, "Poly Pizza search")
    if not isinstance(answer.get("results"), list):
        raise Fail("Poly Pizza search answered without a result list; this tool does not know that shape")
    results = [pp_record(r) for r in answer["results"] if isinstance(r, dict)]
    return results[:limit], answer.get("total", len(results)), None


def pp_fetch(ident, offline):
    if not re.match(r"^[A-Za-z0-9_-]{1,64}$", ident or ""):
        raise Fail("%r is not a Poly Pizza id" % ident, "bad_args")
    variant = "model-glb"
    key = None
    if not offline:
        key = pp_key()
    manifest, state = lookup("polypizza", ident, variant, offline)
    if manifest is not None:
        return manifest, state
    try:
        rec = http_json(api_base("polypizza") + "/model/" + urllib.parse.quote(ident), {"x-auth-token": key},
                        what="Poly Pizza")
    except HTTPStatus as exc:
        if exc.status == 404:
            raise Fail("Poly Pizza has no model %r (HTTP 404)" % ident) from None
        if exc.status in (401, 403):
            raise Fail("Poly Pizza refused the API key (HTTP %d)" % exc.status) from None
        raise Fail("Poly Pizza answered HTTP %d for %r" % (exc.status, ident)) from None
    rec = need_dict(rec, "Poly Pizza")
    url = rec.get("Download")
    if not isinstance(url, str) or not url:
        raise Fail("Poly Pizza's %s has no downloadable file" % ident)
    info = pp_record(rec)
    meta = {"source": "polypizza", "id": ident, "name": info["name"] or ident, "kind": "model", "format": "glb",
            "author": info["author"], "license": info["license"], "attribution": info["attribution"],
            "page_url": info["page_url"], "main": "model/%s.glb" % safe_name(ident)}
    try:
        # The CDN is another host and takes no key: none is sent.
        return fetch_plan("polypizza", ident, variant, [{"rel": meta["main"], "url": url, "check": "glb"}], meta), state
    except HTTPStatus as exc:
        if exc.status == 403 and (b"<" in exc.body[:200] or exc.headers.get("cf-mitigated")):
            raise Fail("Poly Pizza's file server answered with a bot check (HTTP 403) instead of the model. That is "
                       "not the key: it blocks data-centre, VPN and cloud addresses. Retry from another connection, "
                       "or download the .glb from %s and use import-file" % info["page_url"]) from None
        raise Fail("Poly Pizza's file server answered HTTP %d" % exc.status) from None


# ------------------------------------------------------------------------------------------------ Sketchfab

def sf_token():
    return secret("SKETCHFAB_API_TOKEN", "Sketchfab", "Sketchfab > Settings > Password & API")


def sf_thumb(model):
    images = [i for i in (model.get("thumbnails") or {}).get("images") or [] if isinstance(i, dict) and i.get("url")]
    if not images:
        return None
    big = [i for i in images if (i.get("width") or 0) >= 256]
    return (min(big, key=lambda i: i["width"]) if big else max(images, key=lambda i: i.get("width") or 0))["url"]


def sf_record(model):
    user = model.get("user") or {}
    licence = model.get("license") or {}
    archives = model.get("archives") or {}
    return {"source": "sketchfab", "id": model.get("uid"), "name": model.get("name"), "type": "model",
            "license": licence.get("label"), "author": user.get("displayName") or user.get("username"),
            "sizes": sorted(k for k, v in archives.items() if isinstance(v, dict)),
            "archive_bytes": {k: v.get("size") for k, v in archives.items() if isinstance(v, dict)},
            "face_count": model.get("faceCount"), "downloadable": bool(model.get("isDownloadable")),
            "categories": [c.get("name") for c in model.get("categories") or [] if isinstance(c, dict)],
            "tags": [t.get("name") for t in model.get("tags") or [] if isinstance(t, dict)],
            "thumbnail_url": sf_thumb(model), "page_url": model.get("viewerUrl")}


def sf_search(words, category, limit):
    token = sf_token()
    query = " ".join(words).strip()
    if not query:
        raise Fail("Sketchfab needs search words", "bad_args")
    params = {"type": "models", "q": query, "downloadable": "true", "count": max(1, min(limit, 24))}
    if category:
        params["categories"] = category
    url = api_base("sketchfab") + "/search?" + urllib.parse.urlencode(params)
    try:
        answer = http_json(url, {"Authorization": "Token " + token}, what="Sketchfab search")
    except HTTPStatus as exc:
        if exc.status in (401, 403):
            raise Fail("Sketchfab refused the token (HTTP %d)" % exc.status) from None
        raise Fail("Sketchfab search answered HTTP %d" % exc.status) from None
    answer = need_dict(answer, "Sketchfab search")
    if not isinstance(answer.get("results"), list):
        raise Fail("Sketchfab search answered without a result list; this tool does not know that shape")
    results = [sf_record(m) for m in answer["results"] if isinstance(m, dict)]
    return results[:limit], len(results), None


def _pick_gltf(folder, names):
    found = [n for n in names if n.lower().endswith((".gltf", ".glb"))]
    if not found:
        raise Fail("the archive holds no .gltf or .glb file")
    found.sort(key=lambda n: (n.count("/"), not n.lower().endswith("scene.gltf"), n))
    return (folder.name + "/" + found[0]) if folder.name else found[0]


def sf_fetch(uid, fmt, offline):
    if not re.match(r"^[A-Za-z0-9]{8,64}$", uid or ""):
        raise Fail("%r is not a Sketchfab model uid" % uid, "bad_args")
    token = None
    if not offline:
        token = sf_token()
    wanted = (fmt or "").lower() or None
    if wanted and wanted not in ("glb", "gltf", "usdz"):
        raise Fail("a Sketchfab download comes as glb, gltf or usdz, not %s" % wanted, "bad_args")
    for variant in (["model-" + wanted] if wanted else ["model-glb", "model-gltf"]):
        manifest, _ = read_entry(entry_dir("sketchfab", uid, variant))
        if manifest is not None:
            return manifest, "hit"
    _none, state = lookup("sketchfab", uid, "model-" + (wanted or "glb"), offline)   # raises when --offline
    auth = {"Authorization": "Token " + token}
    try:
        info = need_dict(http_json(api_base("sketchfab") + "/models/" + uid, auth, what="Sketchfab"), "Sketchfab")
        if not info.get("isDownloadable", True):
            raise Fail("Sketchfab's %s is not downloadable" % uid)
        links = need_dict(http_json(api_base("sketchfab") + "/models/" + uid + "/download", auth,
                                    what="Sketchfab download"), "Sketchfab download")
    except HTTPStatus as exc:
        if exc.status == 404:
            raise Fail("Sketchfab has no model %r (HTTP 404)" % uid) from None
        if exc.status in (401, 403):
            raise Fail("Sketchfab refused the token or the model is not yours to download (HTTP %d)" % exc.status) from None
        raise Fail("Sketchfab answered HTTP %d for %r" % (exc.status, uid)) from None
    fmt = wanted or ("glb" if isinstance(links.get("glb"), dict) else "gltf")
    link = links.get(fmt)
    if not isinstance(link, dict) or not isinstance(link.get("url"), str) or not link.get("url"):
        raise Fail("Sketchfab offers %s for %s, not %s" % (", ".join(k for k, v in links.items() if isinstance(v, dict)), uid, fmt))
    size = link.get("size") if isinstance(link.get("size"), int) and not isinstance(link.get("size"), bool) else None
    rec = sf_record(info)
    licence = info.get("license") or {}
    meta = {"source": "sketchfab", "id": uid, "name": rec["name"] or uid, "kind": "model", "format": fmt,
            "author": rec["author"], "license": licence.get("label") or "unknown",
            "license_url": licence.get("url"), "license_requirements": licence.get("requirements"),
            "page_url": rec["page_url"]}
    meta["attribution"] = '"%s" by %s (%s), %s%s' % (meta["name"], meta["author"], meta["page_url"], meta["license"],
                                                    (" " + meta["license_url"]) if meta["license_url"] else "")
    variant = "model-" + fmt
    if fmt == "gltf":
        plan = [{"rel": "download.zip", "url": link["url"], "size": size}]
        extract = {"archive": "download.zip", "into": "model", "pick": _pick_gltf}
        return fetch_plan("sketchfab", uid, variant, plan, meta, extract), state
    meta["main"] = "model/%s.%s" % (uid, fmt)
    check = "glb" if fmt == "glb" else None
    return fetch_plan("sketchfab", uid, variant, [{"rel": meta["main"], "url": link["url"], "size": size,
                                                   "check": check}], meta), state


# ------------------------------------------------------------------------------------------------ Blender

def find_blender(explicit=None):
    if explicit:
        if Path(explicit).is_file():
            return explicit, "--blender"
        raise Fail("--blender is %r, which is not a file" % explicit, "bad_args")
    env = os.environ.get("BLENDER_EXE")
    if env:
        if Path(env).is_file():
            return env, "BLENDER_EXE"
        raise Fail("BLENDER_EXE is %r, which is not a file" % env, "bad_args")
    on_path = shutil.which("blender")
    if on_path:
        return on_path, "PATH"
    candidates = []
    if os.name == "nt":
        for root in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
            candidates += glob.glob(os.path.join(root, "Blender Foundation", "Blender*", "blender.exe"))
            candidates += glob.glob(os.path.join(root, "Steam", "steamapps", "common", "Blender", "blender.exe"))
    elif sys.platform == "darwin":
        candidates += ["/Applications/Blender.app/Contents/MacOS/Blender",
                       str(Path.home() / "Applications/Blender.app/Contents/MacOS/Blender")]
    else:
        candidates += ["/usr/bin/blender", "/usr/local/bin/blender", "/snap/bin/blender"]
        candidates += glob.glob("/opt/blender*/blender") + glob.glob(str(Path.home() / "blender*" / "blender"))

    def version(path):
        """'Blender 5.2' in the folder name orders the installs, newest first; unversioned folders come after."""
        return [int(n) for n in re.findall(r"\d+", Path(path).parent.name)] or [-1]
    found = [c for c in candidates if Path(c).is_file()]
    for path in sorted(found, key=version, reverse=True):
        return path, "the usual install folders" + (" (of %d found)" % len(found) if len(found) > 1 else "")
    raise Fail("no Blender found: pass --blender EXE, set BLENDER_EXE, or put blender on PATH")


def _short_path(path):
    """The 8.3 form of an existing Windows path (None when it has none)."""
    if os.name != "nt":
        return None
    import ctypes
    full = os.path.abspath(path)
    long_form = full if full.startswith("\\\\?\\") else "\\\\?\\" + full
    get = ctypes.windll.kernel32.GetShortPathNameW
    n = get(long_form, None, 0)
    if not n:
        return None
    buf = ctypes.create_unicode_buffer(n + 1)
    if not get(long_form, buf, n + 1):
        return None
    out = buf.value
    return out[4:] if out.startswith("\\\\?\\") else out


def for_blender(main, siblings=()):
    """The path Blender is handed for `main`. Blender on Windows cannot open paths of 260 characters or more
    (it says 'No such file' or 'Please select a file'): such a path goes as its short 8.3 form, checked for
    every file Blender will open beside it (glTF textures and buffers), or the job is refused plainly."""
    main = str(main)
    files = [main] + [str(s) for s in siblings]
    if os.name != "nt" or max(len(f) for f in files) < WIN_MAX_PATH:
        return main
    short_dir = _short_path(os.path.dirname(main))
    if short_dir:
        # Only the folders are shortened: an 8.3 file name would cut '.gltf' to '.GLT', and Blender picks the
        # importer (and the glTF its textures) by the real names.
        short_main = os.path.join(short_dir, os.path.basename(main))
        longest = max(len(os.path.join(short_dir, os.path.relpath(f, os.path.dirname(main)))) for f in files)
        if longest < WIN_MAX_PATH:
            return short_main
    raise Fail("the cached file %s is %d characters long; Blender on Windows cannot open paths of %d characters or "
               "more. Set BLENDER_AI_ASSETS_DIR to a shorter folder (for example C:\\blender-assets) and fetch again"
               % (main, max(len(f) for f in files), WIN_MAX_PATH))


def run_headless(job, target, timeout, tmp_dir=None):
    exe = target["exe"]
    say("Blender: %s (found via %s)" % (exe, target["how"]))
    if tmp_dir:
        Path(tmp_dir).mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="blender_ai_assets_", dir=tmp_dir or None))
    try:
        job_path, result_path, log_path = work / "job.json", work / "result.json", work / "blender.log"
        job_path.write_text(json.dumps(job), encoding="utf-8")
        cmd = [exe, "--background", "--factory-startup", "--disable-autoexec"]
        if target["blend"]:
            cmd.append(for_blender(target["blend"]))
        try:
            part = for_blender(BLENDER_PART)             # a skill installed in a deep folder still starts
        except Fail:
            raise Fail("this tool's folder %s is too long for Blender on Windows (%d characters, the limit is %d); "
                       "put the skill in a shorter folder" % (HERE, len(str(BLENDER_PART)), WIN_MAX_PATH)) from None
        cmd += ["--python-exit-code", "3", "--python", part, "--",
                "--job", str(job_path), "--result", str(result_path)]
        with open(log_path, "wb") as log:
            try:
                proc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
            except subprocess.TimeoutExpired:
                raise Fail("the background Blender did not finish within %d s and was stopped" % timeout) from None
        if not result_path.is_file():
            tail = [l for l in log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                    if re.search(r"Error|Traceback|error:", l)][-6:]
            raise Fail("the background Blender exited (code %d) without a result: %s" % (proc.returncode, " | ".join(tail) or "no error line in its log"))
        try:
            return json.loads(result_path.read_text(encoding="utf-8"))
        except ValueError:
            raise Fail("the background Blender wrote a result this tool cannot read") from None
    finally:
        if not os.environ.get("BLENDER_AI_ASSETS_KEEP_WORK"):
            shutil.rmtree(work, ignore_errors=True)


def _bmcp():
    sys.dont_write_bytecode = True            # no __pycache__ appears beside the skill's scripts
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import bmcp  # the skill's bridge client, beside this file
    return bmcp


def bridge_call(code, timeout, what):
    bmcp = _bmcp()
    where = "%s:%s" % (bmcp.HOST, bmcp.PORT)
    try:
        answer = bmcp.execute(code, strict_json=True, timeout=timeout)
    except (ConnectionRefusedError, socket.gaierror) as exc:
        raise Fail("no Blender bridge answers on %s (%s); is Blender open with the MCP extension's server "
                   "started? Use --blend/--new for a background Blender" % (where, exc), "unreachable") from None
    except (socket.timeout, TimeoutError):
        raise Fail("the Blender on %s did not answer within %d s; %s may still be running there - look before "
                   "trying again" % (where, timeout, what)) from None
    except (OSError, RuntimeError, ValueError) as exc:
        raise Fail("talking to the Blender on %s failed: %s" % (where, exc), "unreachable") from None
    if not isinstance(answer, dict):
        raise Fail("the Blender on %s answered in a shape this tool does not know" % where)
    if answer.get("status") != "ok":
        message = str(answer.get("message", "?")).strip().splitlines()
        raise Fail("Blender on %s answered with an error: %s" % (where, message[-1] if message else "?"))
    result = answer.get("result")
    return (result if isinstance(result, dict) else {}), where


PING = ("import bpy, os\nresult = {'pid': os.getpid(), 'file': bpy.data.filepath, "
        "'version': bpy.app.version_string, 'background': bool(bpy.app.background)}\n")


def same_blend(have, want):
    """Is the open file `have` the file `want`? A bare name compares names; a path compares files."""
    if not have:
        return False
    if not os.path.dirname(want):
        return os.path.normcase(os.path.basename(have)) == os.path.normcase(want)
    try:
        if os.path.exists(have) and os.path.exists(want):
            return os.path.samefile(have, want)
    except OSError:
        pass
    return os.path.normcase(os.path.realpath(have)) == os.path.normcase(os.path.realpath(want))


def live_ping(args):
    """Who answers on the bridge, checked against --expect-file / --expect-pid before anything is done."""
    who, where = bridge_call(PING, min(float(args.timeout), 60.0), "the identity check")
    if args.expect_pid is not None and who.get("pid") != args.expect_pid:
        raise Fail("WRONG BLENDER, nothing was touched: its process id is %s, not %d" % (who.get("pid"), args.expect_pid),
                   "refused", touched=False, blender=who)
    if args.expect_file is not None and not same_blend(who.get("file") or "", args.expect_file):
        raise Fail("WRONG BLENDER, nothing was touched: it has %r open, not %r" % (who.get("file") or "(unsaved)",
                                                                                 args.expect_file),
                   "refused", touched=False, blender=who)
    return who, where


def _same_path(a, b):
    try:
        if a.exists() and b.exists():
            return os.path.samefile(a, b)
    except OSError:
        pass
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def plan_target(args, keeps_result):
    """Where the job runs, settled before any download: HEADLESS (Blender found, files checked) or LIVE (the
    bridge answered and is the Blender meant)."""
    if args.expect_file is not None and os.path.dirname(args.expect_file):
        args.expect_file = os.path.abspath(args.expect_file)
    if args.blend or args.new:
        if args.blend and args.new:
            raise Fail("--blend and --new together: pick one", "bad_args")
        if args.expect_file is not None or args.expect_pid is not None:
            raise Fail("--expect-file and --expect-pid check the Blender on the bridge; they mean nothing with "
                       "--blend/--new", "bad_args")
        blend = None
        if args.blend:
            blend = Path(args.blend).resolve()
            if not blend.is_file():
                raise Fail("there is no .blend at %s" % blend, "bad_args")
        save = None
        if args.save_as:
            save = Path(args.save_as).resolve()
            if save.suffix.lower() != ".blend":
                raise Fail("--save-as must end in .blend", "bad_args")
            if not save.parent.is_dir():
                raise Fail("the folder for --save-as does not exist: %s" % save.parent, "bad_args")
            if blend and _same_path(save, blend):
                raise Fail("--save-as is the input file; this tool never saves over it", "refused")
        elif keeps_result and not getattr(args, "dry_run", False):
            raise Fail("a headless import keeps nothing without --save-as PATH (the background Blender exits when "
                       "it is done); add --save-as, or --dry-run to only try it", "bad_args")
        exe, how = find_blender(getattr(args, "blender", None))
        return {"mode": "headless", "blend": str(blend) if blend else None,
                "save_as": str(save) if save else None, "exe": exe, "how": how}
    if args.save_as:
        raise Fail("--save-as is for headless runs (--blend/--new); a live session is never saved", "bad_args")
    if getattr(args, "dry_run", False):
        raise Fail("--dry-run is for headless runs (--blend/--new)", "bad_args")
    who, where = live_ping(args)
    return {"mode": "live", "where": where, "blender": who}


def run_live(job, timeout):
    code = BLENDER_PART.read_text(encoding="utf-8") + "\n\nresult = bridge_entry(%r)\n" % json.dumps(job)
    return bridge_call(code, timeout, "the import")


def run_blender(job, args, target):
    if target["mode"] == "headless":
        if target["save_as"]:
            job["save_as"] = for_blender(target["save_as"]) if os.path.exists(target["save_as"]) else target["save_as"]
        job["new_file"] = bool(args.new)
        result = run_headless(job, target, args.timeout, getattr(args, "tmp_dir", None))
        return result, "headless", target["exe"]
    job["expect_file"] = args.expect_file
    job["expect_pid"] = args.expect_pid
    result, where = run_live(job, args.timeout)
    return result, "live", where


# ------------------------------------------------------------------------------------------------ commands

def fetch_any(source, ident, kind, res, fmt, offline):
    if source == "polyhaven":
        return ph_fetch(ident, kind, res, fmt, offline)
    if kind and kind != "model":
        raise Fail("%s has models only" % source, "bad_args")
    if source == "polypizza":
        return pp_fetch(ident, offline)
    return sf_fetch(ident, fmt, offline)


def describe_fetch(manifest, state):
    folder = entry_dir(manifest["source"], manifest["id"], manifest["variant"])
    total = sum(f["size"] for f in manifest["files"])
    say("%s %s (%s): %d file(s), %s, cache %s - %s" % (manifest["source"], manifest["id"], manifest["variant"],
                                                     len(manifest["files"]), human_bytes(total), state, folder))
    return {"cache": state, "dir": str(folder), "variant": manifest["variant"],
            "files": [str(folder / f["path"]) for f in manifest["files"]], "bytes": total,
            "meta": manifest["meta"]}


def job_from_manifest(manifest, args, opts):
    meta = dict(manifest["meta"])
    folder = entry_dir(manifest["source"], manifest["id"], manifest["variant"])
    every = [str(folder / f["path"]) for f in manifest["files"]]
    kind = meta["kind"]
    files = {}
    if kind in ("hdri", "model"):
        main = folder / meta["main"]
        if kind == "model":
            refuse_gltf_escape(main, "the model's glTF")     # an entry cached by an older version
        files["main"] = for_blender(main, [f for f in every if f != str(main)] if kind == "model" else ())
    else:
        maps = [{"path": for_blender(folder / m["rel"]), "role": m["role"], "key": m["key"]} for m in meta["maps"]]
        if args.no_displacement:
            meta["skipped_maps"] = list(meta.get("skipped_maps") or []) + [
                {"map": m["key"], "why": "left out (--no-displacement)"} for m in maps if m["role"] == "displacement"]
            maps = [m for m in maps if m["role"] != "displacement"]
        files["maps"] = maps
    opts = dict(opts)
    opts["prefer_collections"] = meta.get("prefer_collections") or []
    return {"op": "import", "kind": kind, "files": files, "meta": meta, "options": opts}


def summarize_import(result):
    if not result.get("ok"):
        return
    if result.get("collection"):
        say("imported into collection %r: %d object(s), largest side %.4g m, lowest point z=%.4g m, scale x%.6g"
            % (result["collection"], len(result.get("objects", [])), result.get("final_largest_m") or 0,
               result.get("final_lowest_z_m") or 0, result.get("scale_applied", 1)))
    if result.get("world"):
        say("world %r now lights the scene (strength %s, rotated %s deg); previous world: %s"
            % (result["world"], result.get("strength"), result.get("rotation_deg"), result.get("previous_world")))
    for mat in result.get("materials") or []:
        say("material %r: %s; tiling %s" % (mat["material"], ", ".join("%s -> %s" % (w["map"], "/".join(w["drives"]))
                                                                     for w in mat["wiring"]), mat["tiling"]))
    for item in result.get("renamed") or []:
        say("name taken: the new %s is %r (yours keeps %r)" % (item["kind"], item["got"], item["wanted"]))
    for line in result.get("warnings") or []:
        say("warning: %s" % line)


def finish_import(result, out, target):
    out["import"] = result
    check = result.get("scene_check") or {}
    if result.get("ok"):
        say("scene check: nothing changed but the import itself (%d expected change(s))" % len(check.get("expected_changes", [])))
        out["ok"] = True
        if target["mode"] == "headless":
            out["saved"] = bool(result.get("saved_as"))
            if result.get("saved_as"):
                out["saved_as"] = result["saved_as"]
            else:
                out.setdefault("warnings", []).append("--dry-run: the background Blender exited and nothing was kept")
    else:
        out["ok"] = False
        out["code"] = result.get("code") or "failed"
        out["error"] = result.get("error") or "the import failed"
        if "scene_unchanged" in result:
            say("the import failed and %s" % ("the scene is exactly as it was" if result["scene_unchanged"]
                                              else "THE SCENE WAS LEFT CHANGED: %s" % check.get("unexpected_changes")))
    summarize_import(result)
    return out


def cmd_search(args):
    if args.limit < 1:
        raise Fail("--limit must be 1 or more, not %d" % args.limit, "bad_args")
    if args.source == "polyhaven":
        results, total, note = ph_search(args.words, args.type, args.category, args.limit)
    elif args.source == "polypizza":
        if args.type and args.type != "model":
            raise Fail("Poly Pizza has models only", "bad_args")
        if OFFLINE:
            raise Fail("Poly Pizza search needs the network and --offline forbids it")
        results, total, note = pp_search(args.words, args.category, args.limit)
    else:
        if args.type and args.type != "model":
            raise Fail("Sketchfab has models only", "bad_args")
        if OFFLINE:
            raise Fail("Sketchfab search needs the network and --offline forbids it")
        results, total, note = sf_search(args.words, args.category, args.limit)
    thumbs = []
    if args.thumbs:
        folder = Path(args.thumbs)
        folder.mkdir(parents=True, exist_ok=True)
        for rec in results:
            url = rec.get("thumbnail_url")
            if not url or not isinstance(url, str) or OFFLINE:
                continue
            if args.source == "polyhaven":
                url = ph_thumb(url)
            stem = folder / ("%s_%s" % (args.source, safe_name(rec["id"])))
            tmp = stem.with_suffix(".part")
            _discard(tmp)
            try:
                got = download(url, tmp, what="the thumbnail of %s" % rec["id"], max_bytes=THUMB_MAX)
            except (Fail, HTTPStatus) as exc:
                rec["thumbnail_error"] = str(exc)
                continue
            kind = got["content_type"].lower()
            ext = ".png" if "png" in kind else ".webp" if "webp" in kind else ".jpg" if "jpeg" in kind or "jpg" in kind \
                else (posixpath.splitext(urllib.parse.urlsplit(url).path)[1] or ".img")
            final = stem.with_suffix(ext)
            os.replace(tmp, final)
            rec["thumbnail_file"] = str(final)
            thumbs.append(str(final))
    for rec in results:
        say("%-28s %-8s %-10s %s" % (rec["id"], rec["type"], rec.get("license") or "?", rec.get("name") or ""))
    say("%d shown of %d%s" % (len(results), total, ("; " + note) if note else ""))
    return {"ok": True, "command": "search", "source": args.source, "total": total, "results": results,
            "thumbs": thumbs, "note": note}


def cmd_categories(args):
    if args.source != "polyhaven":
        if args.source == "polypizza":
            return {"ok": True, "command": "categories", "source": "polypizza", "categories": PP_CATEGORY_NAMES}
        raise Fail("Sketchfab's categories are slugs like furniture-home; see https://api.sketchfab.com/v3/categories")
    paths = ph_categories(args.type or "texture")
    return {"ok": True, "command": "categories", "source": "polyhaven", "type": args.type or "texture", "categories": paths}


def cmd_fetch(args):
    check_common(args)
    manifest, state = fetch_any(args.source, args.id, args.type, args.res, args.format, args.offline)
    return {"ok": True, "command": "fetch", **describe_fetch(manifest, state)}


def cmd_import(args):
    check_common(args)
    opts = options_from(args)
    target = plan_target(args, keeps_result=True)
    if args.apply_to:                           # a texture option on what may not be a texture: known before any file
        kind = args.type or ("model" if args.source != "polyhaven" else None)
        if kind is None and SLUG_RE.match(args.id or ""):
            kind = ph_kind(args.id, args.offline)
        if kind is not None and kind != "texture":
            raise Fail("--apply-to is for textures, and %s %s is a %s" % (args.source, args.id, kind), "bad_args")
    manifest, state = fetch_any(args.source, args.id, args.type, args.res, args.format, args.offline)
    fetched = describe_fetch(manifest, state)
    job = job_from_manifest(manifest, args, opts)
    if job["kind"] != "texture" and args.apply_to:
        raise Fail("--apply-to is for textures", "bad_args")
    result, mode, where = run_blender(job, args, target)
    out = {"command": "import", "mode": mode, "blender_at": where, "fetch": {k: v for k, v in fetched.items() if k != "meta"}}
    return finish_import(result, out, target)


def cmd_import_file(args):
    check_common(args)
    opts = options_from(args)
    path = Path(args.path).resolve()
    if not path.is_file():
        raise Fail("there is no file at %s" % path, "bad_args")
    ext = path.suffix.lower()
    if ext not in MODEL_EXTENSIONS + HDRI_EXTENSIONS:
        raise Fail("import-file takes %s, not %s" % (" ".join(MODEL_EXTENSIONS + HDRI_EXTENSIONS), ext or "a file without extension"), "bad_args")
    if ext == ".glb":
        check_glb(path)
    if args.apply_to:
        raise Fail("--apply-to is for textures; import-file brings models and HDRIs", "bad_args")
    target = plan_target(args, keeps_result=True)
    kind = "hdri" if ext in HDRI_EXTENSIONS else "model"
    digest = _sha256(path)
    # The credit names the file and its checksum, never its folder (which holds the user's name on most machines).
    meta = {"source": "file", "id": path.name, "name": path.stem, "kind": kind, "author": "",
            "license": "unknown (a local file)", "page_url": "local file, sha256 %s" % digest[:16]}
    job = {"op": "import", "kind": kind, "files": {"main": for_blender(path)}, "meta": meta, "options": opts}
    result, mode, where = run_blender(job, args, target)
    return finish_import(result, {"command": "import-file", "mode": mode, "blender_at": where, "file": str(path),
                                  "sha256": digest}, target)


def cmd_credits(args):
    check_common(args)
    target = plan_target(args, keeps_result=False)
    result, mode, where = run_blender({"op": "credits"}, args, target)
    if not result.get("ok"):
        raise Fail(result.get("error") or "reading the credits failed", result.get("code") or "failed")
    for line in result.get("credits") or []:
        say(line)
    out = {"ok": True, "command": "credits", "mode": mode, "blender_at": where, "credits": result.get("credits")}
    if result.get("linked_credits"):
        out["linked_credits"] = result["linked_credits"]
    return out


def cmd_cache(args):
    root = cache_root()
    entries = []
    for manifest_path in sorted(root.glob("*/*/*/manifest.json")):
        folder = manifest_path.parent
        item = {"dir": str(folder), "source": folder.parent.parent.name, "id": folder.parent.name, "variant": folder.name}
        if args.verify:
            manifest, damaged = read_entry(folder)
        else:
            damaged = None
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
                    manifest, damaged = None, "its manifest is not one this tool wrote"
            except (OSError, ValueError) as exc:
                manifest, damaged = None, "its manifest cannot be read (%s)" % exc
        if manifest:
            item["bytes"] = sum(f.get("size", 0) for f in manifest["files"] if isinstance(f, dict))
        if args.verify or damaged:
            item["sound"] = damaged is None
            if damaged:
                item["problem"] = damaged
        entries.append(item)
        say("%s %s %s %s" % (item["source"], item["id"], item["variant"], item.get("problem") or human_bytes(item.get("bytes", 0))))
    return {"ok": True, "command": "cache", "root": str(root), "entries": entries}


def add_target_options(p):
    g = p.add_argument_group("where it runs (LIVE through the bridge unless --blend or --new)")
    g.add_argument("--blend", metavar="FILE", help="HEADLESS: open this .blend in a background Blender")
    g.add_argument("--new", action="store_true", help="HEADLESS: start from an empty new file")
    g.add_argument("--save-as", metavar="PATH", help="HEADLESS: save the result here (never over the input); "
                                                     "an import needs it, or --dry-run")
    g.add_argument("--dry-run", action="store_true", help="HEADLESS: do the import and keep nothing (a try-out)")
    g.add_argument("--blender", metavar="EXE", help="HEADLESS: this Blender (else BLENDER_EXE, PATH, install folders)")
    g.add_argument("--tmp-dir", metavar="DIR", help="HEADLESS: where the job and Blender's log go (default: the "
                                                    "system temp folder; removed afterwards)")
    g.add_argument("--expect-file", metavar="PATH", help="LIVE: refuse unless the Blender that answers has this file "
                                                         "open (a bare name matches the name only)")
    g.add_argument("--expect-pid", type=int, metavar="N", help="LIVE: refuse unless the Blender that answers is process N")
    g.add_argument("--timeout", type=float, default=600.0, help="seconds to wait for Blender (default 600)")


def add_import_options(p):
    g = p.add_argument_group("placing a model")
    g.add_argument("--size", type=float, help="largest side in metres (before --rotate); default: its real size")
    g.add_argument("--at", default="0,0,0", help="x,y,z in metres: the footprint centre goes here (default 0,0,0)")
    g.add_argument("--no-ground", action="store_true", help="keep the file's own height instead of resting the lowest point on z")
    g.add_argument("--rotate", type=float, default=0.0, help="turn about Z, degrees")
    g.add_argument("--name", help="name for the new collection, world or material (default: the asset's)")
    g.add_argument("--pick", help=".blend: collections or objects to append, comma separated (default: all objects)")
    g.add_argument("--keep-lights", action="store_true", help="keep the lights the file brings (left out by default)")
    g.add_argument("--keep-cameras", action="store_true", help="keep the cameras the file brings (left out by default)")
    g = p.add_argument_group("an HDRI")
    g.add_argument("--strength", type=float, default=1.0, help="world strength (default 1)")
    g.add_argument("--rotation", type=float, default=0.0, help="world rotation about Z, degrees")
    g.add_argument("--replace-world", action="store_true", help="do not keep the old world with a fake user")
    g = p.add_argument_group("a texture")
    g.add_argument("--apply-to", metavar="OBJ[,OBJ]", help="put the material on these objects (replaces their materials). "
                                                           "Tiling is worked out now from each object's size and render "
                                                           "UV map: size the object first, import again after resizing")
    g.add_argument("--slot", help="material slot number, or 'all'; needed when an object has more than one slot")
    g.add_argument("--no-displacement", action="store_true", help="leave the displacement map out")
    p.add_argument("--pack", action="store_true", help="pack images into the .blend instead of reading the cache files")


def add_fetch_limits(p):
    p.add_argument("--max-mb", type=float, default=float(MAX_MB_DEFAULT),
                   help="refuse a download larger than this many MB (default %d)" % MAX_MB_DEFAULT)


class Parser(argparse.ArgumentParser):
    """Usage errors still end in one JSON line on stdout (code bad_args, exit 2)."""

    def error(self, message):
        self.print_usage(sys.stderr)
        say("assets: FAILED (bad_args): %s" % message)
        print(json.dumps({"ok": False, "code": "bad_args", "error": "%s: %s" % (self.prog, message)}), flush=True)
        sys.exit(EXIT["bad_args"])


def build_parser():
    parser = Parser(prog="assets.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("search", help="find assets (JSON list)")
    p.add_argument("--source", required=True, choices=SOURCES)
    p.add_argument("--type", choices=KINDS)
    p.add_argument("words", nargs="*", help="what you are looking for")
    p.add_argument("--category", help="polyhaven: a category path (see `categories`); polypizza: a name; sketchfab: a slug")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--thumbs", metavar="DIR", help="download each result's thumbnail into DIR")
    p.add_argument("--offline", action="store_true", help="answer from cached lists only (Poly Haven; words match "
                                                          "names and tags plainly)")
    p.set_defaults(fn=cmd_search)
    p = sub.add_parser("categories", help="the categories a source filters on")
    p.add_argument("--source", required=True, choices=SOURCES)
    p.add_argument("--type", choices=KINDS)
    p.set_defaults(fn=cmd_categories)
    for name, fn, text in (("fetch", cmd_fetch, "download into the cache (checked)"),
                           ("import", cmd_import, "fetch, then put it in the scene")):
        p = sub.add_parser(name, help=text)
        p.add_argument("source", choices=SOURCES)
        p.add_argument("id")
        p.add_argument("--type", choices=KINDS, help="polyhaven: checked against what the asset is")
        p.add_argument("--res", default="1k", help="1k, 2k, 4k, 8k ... (polyhaven; default 1k)")
        p.add_argument("--format", help="hdri: hdr|exr; texture: jpg|png|exr; model: gltf|fbx|blend|usd (polyhaven), glb|gltf|usdz (sketchfab)")
        p.add_argument("--offline", action="store_true", help="use the cache only; fail plainly when it cannot")
        add_fetch_limits(p)
        if name == "import":
            add_import_options(p)
            add_target_options(p)
        p.set_defaults(fn=fn)
    p = sub.add_parser("import-file", help="put a local file in the scene (glb gltf fbx obj usd* stl ply abc blend hdr exr)")
    p.add_argument("path")
    add_import_options(p)
    add_target_options(p)
    p.set_defaults(fn=cmd_import_file, offline=False, no_displacement=False)
    p = sub.add_parser("credits", help="print the ASSET_CREDITS text of the scene")
    add_target_options(p)
    p.set_defaults(fn=cmd_credits)
    p = sub.add_parser("cache", help="list (and --verify) the cache")
    p.add_argument("--verify", action="store_true")
    p.set_defaults(fn=cmd_cache)
    return parser


def main(argv=None):
    global OFFLINE, LIMIT_BYTES
    args = build_parser().parse_args(argv)
    OFFLINE = bool(getattr(args, "offline", False))
    LIMIT_BYTES = MAX_MB_DEFAULT << 20
    try:
        out = args.fn(args)
    except Fail as exc:
        out = {"ok": False, "command": args.command, "code": exc.code, "error": str(exc)}
        out.update(exc.extra)
    except HTTPStatus as exc:
        out = {"ok": False, "command": args.command, "code": "failed",
               "error": "%s answered HTTP %d" % (exc.url, exc.status)}
    except KeyboardInterrupt:
        out = {"ok": False, "command": args.command, "code": "failed", "error": "interrupted"}
    except Exception as exc:  # noqa: BLE001 - still one JSON line, and the traceback for whoever reads stderr
        import traceback
        traceback.print_exc()
        out = {"ok": False, "command": args.command, "code": "failed",
               "error": "unexpected error: %s: %s" % (type(exc).__name__, exc)}
    if not out.get("ok"):
        out["code"] = out.get("code") or "failed"
        say("assets: FAILED (%s): %s" % (out["code"], out.get("error")))
    print(json.dumps(json_safe(out), default=str, allow_nan=False), flush=True)
    return EXIT["ok"] if out.get("ok") else EXIT.get(out["code"], 1)


if __name__ == "__main__":
    sys.exit(main())

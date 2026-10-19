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
(BLENDER_MCP_HOST / BLENDER_MCP_PORT, default localhost:9876, like bmcp.py). --expect-file PATH and
--expect-pid N refuse before anything is touched when the Blender that answers is not that one. HEADLESS
(--blend FILE or --new) starts "$BLENDER_EXE" (else blender on PATH, else the usual install folders; it says
which) with --background --factory-startup --disable-autoexec, does the job and exits; --save-as PATH writes
the result (never over the input file). Nothing is ever written beside your .blend.

WHAT IT DOES TO A LIVE SCENE. Only the import itself: a model goes into a new collection named for it, an
HDRI becomes a new world (the old one is kept with a fake user unless --replace-world), a texture becomes a
new material (put on --apply-to objects, replacing their materials, which the JSON lists). Frame, mode,
selection, active object and collection are put back, and a fingerprint of the scene taken before and after
must show no other change, or the command fails loudly. A failed import removes everything it made. Name
collisions never rename your datablocks: the new ones get Blender's .001 suffixes, listed under "renamed".

FILES. Every download goes into a cache (BLENDER_AI_ASSETS_DIR; default %LOCALAPPDATA%/blender-ai-assets/cache,
~/Library/Caches/blender-ai-assets, or ~/.cache/blender-ai-assets), checked against the size and checksum
the source publishes; an entry counts only once its manifest is written, so a cut-off download is never used,
and a second fetch is a cache hit with no request at all. Archives are extracted only when every member stays
inside its folder. Downloads happen here, before Blender is asked to do anything, so the bridge never waits
on the network; the import itself runs as one synchronous bridge request (the bridge sets no limit on those;
--timeout is how long this side waits, 600 s by default).

SOURCES. polyhaven: HDRIs, textures and models, CC0, no key. polypizza: low-poly models, CC0 or CC-BY
(credit required), needs POLYPIZZA_API_KEY. sketchfab: models under many licences, needs SKETCHFAB_API_TOKEN;
the licence of each model is recorded. Every import appends one line (date, source, id, name, author, licence,
page) to the text datablock ASSET_CREDITS and puts the same in the JSON.

OUTPUT. One JSON line on stdout; human lines on stderr; exit code 0 on success, 1 on failure (the reason is
in "error"), 2 on a usage error. Needs Python 3.10+, standard library only.
"""
import argparse
import errno
import glob
import hashlib
import json
import os
import posixpath
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path

VERSION = "1.0"
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
OFFLINE = False

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
    """One plain reason. Raised before Blender is asked anything, unless the message says otherwise."""


def say(*parts):
    print(*parts, file=sys.stderr, flush=True)


def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


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


def _open(url, headers=None, timeout=30, what="the request"):
    if OFFLINE:
        raise Fail("%s needs the network and --offline forbids it" % what)
    check_url(url, what)
    host = urllib.parse.urlsplit(url).hostname
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    for key, value in (headers or {}).items():
        req.add_unredirected_header(key, value)          # never sent on to another host by a redirect
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
        reason = getattr(reason, "strerror", None) or str(reason)
        raise Fail("no network: could not reach %s (%s)" % (host, reason)) from None
    except (socket.timeout, TimeoutError):
        raise Fail("%s did not answer within %d s" % (host, timeout)) from None
    except (ConnectionError, OSError) as exc:
        raise Fail("no network: could not reach %s (%s)" % (host, exc)) from None


def http_json(url, headers=None, what="the request"):
    resp = _open(url, headers, what=what)
    with resp:
        data = resp.read(MAX_JSON + 1)
    if len(data) > MAX_JSON:
        raise Fail("%s answered with more than %d MB of JSON; refused" % (what, MAX_JSON >> 20))
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise Fail("%s did not answer with JSON (starts %r)" % (what, data[:60])) from None


def _disk_full(exc):
    return getattr(exc, "errno", None) in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)) or \
        getattr(exc, "winerror", None) in (39, 112)


def download(url, dest, size=None, md5=None, headers=None, what=None):
    """Stream url to dest (a new file). Checks the published size and md5 and the server's own length."""
    what = what or posixpath.basename(urllib.parse.urlsplit(url).path) or "a file"
    resp = _open(url, headers, timeout=60, what="downloading %s" % what)
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
    try:
        with resp, f:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                got += len(chunk)
                h_md5.update(chunk)
                h_sha.update(chunk)
                f.write(chunk)
    except OSError as exc:
        _discard(dest)
        if _disk_full(exc):
            raise Fail("disk full while writing %s (%s); nothing was kept" % (dest, exc.strerror or exc)) from None
        if isinstance(exc, (socket.timeout, TimeoutError)):
            raise Fail("the download of %s stalled; nothing was kept" % what) from None
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
    for item in manifest.get("files", []):
        path = directory / item["path"]
        if not path.is_file():
            return None, "%s is missing" % item["path"]
        actual = path.stat().st_size
        if actual != item["size"]:
            return None, "%s is %d bytes, the cache recorded %d" % (item["path"], actual, item["size"])
        if _sha256(path) != item["sha256"]:
            return None, "%s changed since it was fetched (its checksum differs)" % item["path"]
    return manifest, None


def commit_entry(partial, final, manifest):
    (partial / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
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


def member_problem(name, target):
    """Why an archive member or include path may not be written under target, or None."""
    n = name.replace("\\", "/")
    if not n or "\x00" in n:
        return "has an empty or broken name"
    if n.startswith("/") or re.match(r"^[A-Za-z]:", n):
        return "is an absolute path"
    if ".." in n.split("/"):
        return "climbs out with '..'"
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
                raise Fail("the archive %s would unpack to %s; refused" % (archive.name, human_bytes(total)))
            target.mkdir(parents=True, exist_ok=True)
            for info in infos:
                z.extract(info, target)
            return [i.filename for i in infos if not i.is_dir()]
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


def fetch_plan(source, ident, variant, plan, meta, extract=None):
    """Download every file of `plan` into a fresh partial folder, check them, then commit the entry."""
    final = entry_dir(source, ident, variant)
    partial = new_partial(final)
    try:
        records = []
        for item in plan:
            problem = member_problem(item["rel"], partial)
            if problem:
                raise Fail("%s's file list names %r, which %s; refused" % (meta.get("name") or ident, item["rel"], problem))
            dest = partial / item["rel"]
            got = download(item["url"], dest, item.get("size"), item.get("md5"), item.get("headers"))
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
    if entry is not None and (OFFLINE or time.time() - entry.get("fetched", 0) < API_TTL):
        return entry["payload"]
    extra = dict(headers or {})
    if entry is not None and entry.get("etag"):
        extra["If-None-Match"] = entry["etag"]
    try:
        resp = _open(url, extra, what=what)
    except HTTPStatus as exc:
        if exc.status == 304 and entry is not None:
            entry["fetched"] = time.time()
            _write_atomic(store, json.dumps(entry))
            return entry["payload"]
        raise
    with resp:
        data = resp.read(MAX_JSON + 1)
        etag = resp.headers.get("ETag")
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise Fail("%s did not answer with JSON" % what) from None
    try:
        _write_atomic(store, json.dumps({"url": url, "etag": etag, "fetched": time.time(), "payload": payload}))
    except OSError:
        pass
    return payload


def _write_atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".%s.part" % uuid.uuid4().hex[:6])
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


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
    if not max_resolution:
        return []
    top = int(max_resolution[0]) // 1024
    sizes = [k for k in (1, 2, 4, 8, 16) if k <= top]
    if top and top not in sizes:
        sizes.append(top)
    return ["%dk" % k for k in sizes]


def ph_record(slug, rec):
    kind = PH_TYPE_NAMES.get(rec.get("type"), "unknown")
    authors = rec.get("authors") or {}
    names = sorted(authors) if isinstance(authors, dict) else list(authors)
    dims = rec.get("dimensions")
    out = {"source": "polyhaven", "id": slug, "name": rec.get("name") or slug, "type": kind, "license": "CC0",
           "author": ", ".join(names), "authors": names, "sizes": ph_sizes(rec.get("max_resolution")),
           "max_resolution": rec.get("max_resolution"),
           "dimensions_m": [round(float(d) / 1000.0, 4) for d in dims] if dims else None,
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
        return cached_api_json(url, what="Poly Haven's asset list")
    except HTTPStatus as exc:
        if exc.status == 400 and category:
            raise Fail("Poly Haven does not know the category %r (%s); `assets.py categories --source polyhaven "
                       "--type %s` lists the ones it has" % (category, exc.body[:80].decode("utf-8", "replace"),
                                                               kind or "texture")) from None
        raise Fail("Poly Haven's asset list answered HTTP %d" % exc.status) from None


def ph_keyword_match(words, assets):
    terms = [t for t in words.lower().split() if t]
    scored = []
    for slug, rec in assets.items():
        hay = " ".join([slug.replace("_", " "), str(rec.get("name") or ""), " ".join(rec.get("tags") or []),
                        str(rec.get("category") or ""), " ".join(rec.get("categories") or [])]).lower()
        hits = sum(1 for t in terms if t in hay)
        if hits:
            scored.append((hits, rec.get("download_count") or 0, slug))
    scored.sort(reverse=True)
    return [s for _h, _d, s in scored]


def ph_search(words, kind, category, limit):
    assets = ph_assets(kind, category)
    query = " ".join(words).strip().lower()
    note = None
    if query:
        params = {"q": query}
        if kind:
            params["t"] = PH_API_TYPE[kind]
        url = api_base("polyhaven") + "/search?" + urllib.parse.urlencode(params)
        try:
            answer = http_json(url, what="Poly Haven search")
            ranked = [r["slug"] for r in answer.get("results") or [] if r.get("slug")]
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
        ordered = sorted(assets, key=lambda s: assets[s].get("download_count") or 0, reverse=True)
    return [ph_record(s, assets[s]) for s in ordered[:limit]], len(ordered), note


def ph_categories(kind):
    url = api_base("polyhaven") + "/taxonomy/" + PH_API_TYPE[kind]
    try:
        tax = cached_api_json(url, what="Poly Haven's categories")
    except HTTPStatus as exc:
        raise Fail("Poly Haven's categories answered HTTP %d" % exc.status) from None
    paths = []

    def walk(nodes):
        for node in nodes or []:
            if node.get("path"):
                paths.append(node["path"])
            walk(node.get("children"))
    walk(tax.get("categories"))
    return paths


def ph_info(slug, offline):
    store = cache_root() / "polyhaven" / safe_name(slug) / "info.json"
    if store.is_file():
        try:
            return json.loads(store.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    if offline:
        raise Fail("Poly Haven %s is not in the cache and --offline forbids looking it up" % slug)
    try:
        info = http_json(api_base("polyhaven") + "/info/" + urllib.parse.quote(slug), what="Poly Haven")
    except HTTPStatus as exc:
        if exc.status == 404:
            raise Fail("Poly Haven has no asset %r (HTTP 404)" % slug) from None
        raise Fail("Poly Haven answered HTTP %d for %r" % (exc.status, slug)) from None
    if not isinstance(info, dict) or "type" not in info:
        raise Fail("Poly Haven's answer for %r has no asset in it" % slug)
    _write_atomic(store, json.dumps(info))
    return info


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
        if isinstance(entry, dict) and entry.get("url"):
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
                    scale = float(node.get("value"))
                except (TypeError, ValueError):
                    pass
    return {"maps": keys, "displacement_scale": scale}


def ph_fetch(slug, kind, res, fmt, offline):
    if not SLUG_RE.match(slug or ""):
        raise Fail("%r is not a Poly Haven id (letters, digits, _ and - only)" % slug)
    if not RES_RE.match(res):
        raise Fail("%r is not a resolution (1k, 2k, 4k, 8k ...)" % res)
    info = ph_info(slug, offline)
    actual = PH_TYPE_NAMES.get(info.get("type"))
    if kind and kind != actual:
        raise Fail("Poly Haven's %s is a %s, not a %s" % (slug, actual, kind))
    kind = actual
    fmt = (fmt or DEFAULT_FORMAT[kind]).lower()
    if fmt not in PH_FORMATS[kind]:
        raise Fail("a Poly Haven %s comes as %s, not %s" % (kind, ", ".join(PH_FORMATS[kind]), fmt))
    variant = "%s-%s-%s" % (kind, res, fmt)
    manifest, state = lookup("polyhaven", slug, variant, offline)
    if manifest is not None:
        return manifest, state
    try:
        files = http_json(api_base("polyhaven") + "/files/" + urllib.parse.quote(slug), what="Poly Haven's file list")
    except HTTPStatus as exc:
        raise Fail("Poly Haven's file list for %r answered HTTP %d" % (slug, exc.status)) from None
    authors = info.get("authors") or {}
    names = sorted(authors) if isinstance(authors, dict) else list(authors)
    dims = info.get("dimensions")
    meta = {"source": "polyhaven", "id": slug, "name": info.get("name") or slug, "kind": kind, "res": res,
            "format": fmt, "author": ", ".join(names), "authors": names, "license": "CC0",
            "page_url": ph_page(slug), "dimensions_m": [float(d) / 1000.0 for d in dims] if dims else None}
    plan = []
    if kind == "hdri":
        entry = (files.get("hdri") or {}).get(res, {}).get(fmt)
        if not entry:
            raise Fail("the HDRI %s has no %s %s; %s" % (slug, res, fmt, ph_offered(files, kind)))
        rel = "hdri/" + url_name(entry["url"])
        plan.append({"rel": rel, "url": entry["url"], "size": entry.get("size"), "md5": entry.get("md5")})
        meta["main"] = rel
    elif kind == "texture":
        chosen, skipped, offered = plan_texture_maps(files, res, fmt)
        if not chosen:
            raise Fail("the texture %s has no maps at %s %s; %s" % (slug, res, fmt, ph_offered(files, kind)))
        maps = []
        for key, role in chosen.items():
            entry = offered[key]
            rel = "maps/" + url_name(entry["url"])
            plan.append({"rel": rel, "url": entry["url"], "size": entry.get("size"), "md5": entry.get("md5")})
            maps.append({"rel": rel, "role": role, "key": key})
        mtlx = (files.get("mtlx") or {}).get(res, {}).get("mtlx")
        if isinstance(mtlx, dict) and mtlx.get("url"):
            plan.append({"rel": "material.mtlx", "url": mtlx["url"], "size": mtlx.get("size"), "md5": mtlx.get("md5")})
            meta["mtlx"] = "material.mtlx"
        meta.update({"maps": maps, "skipped_maps": skipped})
    else:
        entry = (files.get(fmt) or {}).get(res, {}).get(fmt)
        if not entry:
            raise Fail("the model %s has no %s %s; %s" % (slug, res, fmt, ph_offered(files, kind)))
        main = "model/" + url_name(entry["url"])
        plan.append({"rel": main, "url": entry["url"], "size": entry.get("size"), "md5": entry.get("md5")})
        for inc_path, inc in (entry.get("include") or {}).items():
            problem = member_problem(inc_path, "model")
            if problem:
                raise Fail("Poly Haven's file list for %s names the include %r, which %s; refused" % (slug, inc_path, problem))
            plan.append({"rel": "model/" + inc_path.replace("\\", "/"), "url": inc["url"], "size": inc.get("size"),
                         "md5": inc.get("md5")})
        meta["main"] = main
        meta["prefer_collections"] = [slug + "_LOD0", slug]
    manifest = fetch_plan("polyhaven", slug, variant, plan, meta)
    if kind == "texture" and manifest["meta"].get("mtlx"):
        recipe = mtlx_recipe(entry_dir("polyhaven", slug, variant) / "material.mtlx", slug)
        if recipe:
            manifest["meta"]["recipe"] = recipe
            if recipe.get("displacement_scale") is not None:
                manifest["meta"]["displacement_scale"] = recipe["displacement_scale"]
            _write_atomic(entry_dir("polyhaven", slug, variant) / "manifest.json", json.dumps(manifest, indent=1))
    return manifest, state


# ------------------------------------------------------------------------------------------------ Poly Pizza

def pp_key():
    key = os.environ.get("POLYPIZZA_API_KEY", "").strip()
    if not key:
        raise Fail("Poly Pizza needs an API key: set POLYPIZZA_API_KEY (free at https://poly.pizza/settings/api). "
                   "Nothing was requested")
    return key


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
    raise Fail("Poly Pizza has no category %r; it has %s" % (category, ", ".join(PP_CATEGORY_NAMES)))


def pp_search(words, category, limit):
    key = pp_key()
    params = {"Limit": max(1, min(limit, 32))}         # the API ignores lower-case parameter names
    cat = pp_category(category)
    if cat is not None:
        params["Category"] = cat
    query = " ".join(words).strip()
    if not query and cat is None:
        raise Fail("Poly Pizza needs search words or a --category (it refuses an unfiltered listing)")
    url = api_base("polypizza") + "/search" + ("/" + urllib.parse.quote(query, safe="") if query else "")
    url += "?" + urllib.parse.urlencode(params)
    try:
        answer = http_json(url, {"x-auth-token": key}, what="Poly Pizza search")
    except HTTPStatus as exc:
        if exc.status in (401, 403):
            raise Fail("Poly Pizza refused the API key (HTTP %d)" % exc.status) from None
        raise Fail("Poly Pizza search answered HTTP %d" % exc.status) from None
    results = [pp_record(r) for r in answer.get("results") or [] if isinstance(r, dict)]
    return results[:limit], answer.get("total", len(results)), None


def pp_fetch(ident, offline):
    if not re.match(r"^[A-Za-z0-9_-]{1,64}$", ident or ""):
        raise Fail("%r is not a Poly Pizza id" % ident)
    variant = "model-glb"
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
    url = rec.get("Download")
    if not url:
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
    token = os.environ.get("SKETCHFAB_API_TOKEN", "").strip()
    if not token:
        raise Fail("Sketchfab needs an API token: set SKETCHFAB_API_TOKEN (Sketchfab > Settings > Password & API). "
                   "Nothing was requested")
    return token


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
        raise Fail("Sketchfab needs search words")
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
    results = [sf_record(m) for m in answer.get("results") or [] if isinstance(m, dict)]
    return results[:limit], len(results), None


def _pick_gltf(folder, names):
    found = [n for n in names if n.lower().endswith((".gltf", ".glb"))]
    if not found:
        raise Fail("the archive holds no .gltf or .glb file")
    found.sort(key=lambda n: (n.count("/"), not n.lower().endswith("scene.gltf"), n))
    return (folder.name + "/" + found[0]) if folder.name else found[0]


def sf_fetch(uid, fmt, offline):
    if not re.match(r"^[A-Za-z0-9]{8,64}$", uid or ""):
        raise Fail("%r is not a Sketchfab model uid" % uid)
    if not offline:
        token = sf_token()
    wanted = (fmt or "").lower() or None
    if wanted and wanted not in ("glb", "gltf", "usdz"):
        raise Fail("a Sketchfab download comes as glb, gltf or usdz, not %s" % wanted)
    for variant in (["model-" + wanted] if wanted else ["model-glb", "model-gltf"]):
        manifest, _ = read_entry(entry_dir("sketchfab", uid, variant))
        if manifest is not None:
            return manifest, "hit"
    _none, state = lookup("sketchfab", uid, "model-" + (wanted or "glb"), offline)   # raises when --offline
    auth = {"Authorization": "Token " + token}
    try:
        info = http_json(api_base("sketchfab") + "/models/" + uid, auth, what="Sketchfab")
        if not info.get("isDownloadable", True):
            raise Fail("Sketchfab's %s is not downloadable" % uid)
        links = http_json(api_base("sketchfab") + "/models/" + uid + "/download", auth, what="Sketchfab download")
    except HTTPStatus as exc:
        if exc.status == 404:
            raise Fail("Sketchfab has no model %r (HTTP 404)" % uid) from None
        if exc.status in (401, 403):
            raise Fail("Sketchfab refused the token or the model is not yours to download (HTTP %d)" % exc.status) from None
        raise Fail("Sketchfab answered HTTP %d for %r" % (exc.status, uid)) from None
    fmt = wanted or ("glb" if isinstance(links.get("glb"), dict) else "gltf")
    link = links.get(fmt)
    if not isinstance(link, dict) or not link.get("url"):
        raise Fail("Sketchfab offers %s for %s, not %s" % (", ".join(k for k, v in links.items() if isinstance(v, dict)), uid, fmt))
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
        plan = [{"rel": "download.zip", "url": link["url"], "size": link.get("size")}]
        extract = {"archive": "download.zip", "into": "model", "pick": _pick_gltf}
        return fetch_plan("sketchfab", uid, variant, plan, meta, extract), state
    meta["main"] = "model/%s.%s" % (uid, fmt)
    check = "glb" if fmt == "glb" else None
    return fetch_plan("sketchfab", uid, variant, [{"rel": meta["main"], "url": link["url"], "size": link.get("size"),
                                                   "check": check}], meta), state


# ------------------------------------------------------------------------------------------------ Blender

def find_blender():
    env = os.environ.get("BLENDER_EXE")
    if env:
        if Path(env).is_file():
            return env, "BLENDER_EXE"
        raise Fail("BLENDER_EXE is %r, which is not a file" % env)
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
    raise Fail("no Blender found: set BLENDER_EXE to blender's executable, or put blender on PATH")


def run_headless(job, blend, timeout):
    exe, how = find_blender()
    say("Blender: %s (found via %s)" % (exe, how))
    work = Path(tempfile.mkdtemp(prefix="blender_ai_assets_"))
    try:
        job_path, result_path, log_path = work / "job.json", work / "result.json", work / "blender.log"
        job_path.write_text(json.dumps(job), encoding="utf-8")
        cmd = [exe, "--background", "--factory-startup", "--disable-autoexec"]
        if blend:
            cmd.append(str(blend))
        cmd += ["--python-exit-code", "3", "--python", str(BLENDER_PART), "--",
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
        return json.loads(result_path.read_text(encoding="utf-8")), exe
    finally:
        if not os.environ.get("BLENDER_AI_ASSETS_KEEP_WORK"):
            shutil.rmtree(work, ignore_errors=True)


def run_live(job, timeout):
    sys.path.insert(0, str(HERE))
    import bmcp  # the skill's bridge client, beside this file
    code = BLENDER_PART.read_text(encoding="utf-8") + "\n\nresult = bridge_entry(%r)\n" % json.dumps(job)
    where = "%s:%s" % (bmcp.HOST, bmcp.PORT)
    try:
        answer = bmcp.execute(code, strict_json=True, timeout=timeout)
    except (ConnectionRefusedError, socket.gaierror) as exc:
        raise Fail("no Blender bridge answers on %s (%s); is Blender open with the MCP extension's server "
                   "started? Use --blend/--new for a background Blender" % (where, exc)) from None
    except (socket.timeout, TimeoutError):
        raise Fail("the Blender on %s did not answer within %d s; the import may still be running there - look "
                   "before trying again" % (where, timeout)) from None
    except (OSError, RuntimeError, ValueError) as exc:
        raise Fail("talking to the Blender on %s failed: %s" % (where, exc)) from None
    if answer.get("status") != "ok":
        message = str(answer.get("message", "?")).strip().splitlines()
        raise Fail("Blender on %s answered with an error: %s" % (where, message[-1] if message else "?"))
    return answer.get("result") or {}, where


def run_blender(job, args):
    if args.blend or args.new:
        if args.expect_file is not None or args.expect_pid is not None:
            raise Fail("--expect-file and --expect-pid check the Blender on the bridge; they mean nothing with --blend/--new")
        blend = None
        if args.blend:
            blend = Path(args.blend).resolve()
            if not blend.is_file():
                raise Fail("there is no .blend at %s" % blend)
        if args.save_as:
            target = Path(args.save_as).resolve()
            if blend and os.path.normcase(str(target)) == os.path.normcase(str(blend)):
                raise Fail("--save-as is the input file; this tool never saves over it")
            if target.suffix.lower() != ".blend":
                raise Fail("--save-as must end in .blend")
            if not target.parent.is_dir():
                raise Fail("the folder for --save-as does not exist: %s" % target.parent)
            job["save_as"] = str(target)
        job["new_file"] = bool(args.new)
        result, exe = run_headless(job, blend, args.timeout)
        return result, "headless", exe
    if args.save_as:
        raise Fail("--save-as is for headless runs (--blend/--new); a live session is never saved")
    job["expect_file"] = args.expect_file
    job["expect_pid"] = args.expect_pid
    result, where = run_live(job, args.timeout)
    return result, "live", where


# ------------------------------------------------------------------------------------------------ commands

def parse_at(text):
    try:
        values = [float(v) for v in text.split(",")]
    except ValueError:
        values = []
    if len(values) != 3:
        raise Fail("--at takes x,y,z in metres, like 1,0,0.75")
    return values


def fetch_any(source, ident, kind, res, fmt, offline):
    if source == "polyhaven":
        return ph_fetch(ident, kind, res, fmt, offline)
    if kind and kind != "model":
        raise Fail("%s has models only" % source)
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


def job_from_manifest(manifest, args):
    meta = dict(manifest["meta"])
    folder = entry_dir(manifest["source"], manifest["id"], manifest["variant"])
    kind = meta["kind"]
    files = {}
    if kind in ("hdri", "model"):
        files["main"] = str(folder / meta["main"])
    else:
        maps = [{"path": str(folder / m["rel"]), "role": m["role"], "key": m["key"]} for m in meta["maps"]]
        if args.no_displacement:
            meta["skipped_maps"] = list(meta.get("skipped_maps") or []) + [
                {"map": m["key"], "why": "left out (--no-displacement)"} for m in maps if m["role"] == "displacement"]
            maps = [m for m in maps if m["role"] != "displacement"]
        files["maps"] = maps
    return {"op": "import", "kind": kind, "files": files, "meta": meta, "options": options_from(args, meta)}


def options_from(args, meta):
    apply_to = [n.strip() for n in (args.apply_to or "").split(",") if n.strip()]
    slot = None
    if args.slot not in (None, "all"):
        if not str(args.slot).isdigit():
            raise Fail("--slot takes a slot number or 'all'")
        slot = int(args.slot)
    if args.size is not None and not args.size > 0:
        raise Fail("--size is the largest side in metres and must be more than 0")
    return {"size": args.size, "at": parse_at(args.at), "ground": not args.no_ground, "rotate": args.rotate,
            "name": args.name, "strength": args.strength, "rotation": args.rotation,
            "replace_world": args.replace_world, "apply_to": apply_to, "slot": slot, "pack": args.pack,
            "picks": [n.strip() for n in (args.pick or "").split(",") if n.strip()],
            "prefer_collections": meta.get("prefer_collections") or []}


def summarize_import(result):
    if not result.get("ok"):
        return
    if result.get("collection"):
        final = result.get("final") or {}
        say("imported into collection %r: %d object(s), largest side %.4g m, lowest point z=%.4g, scale x%.6g"
            % (result["collection"], len(result.get("objects", [])), result.get("final_largest_m") or 0,
               final.get("min", [0, 0, 0])[2], result.get("scale_applied", 1)))
    if result.get("world"):
        say("world %r now lights the scene (strength %s, rotated %s deg); previous world: %s"
            % (result["world"], result.get("strength"), result.get("rotation_deg"), result.get("previous_world")))
    for mat in result.get("materials") or []:
        say("material %r: %s; tiling %s" % (mat["material"], ", ".join("%s -> %s" % (w["map"], "/".join(w["drives"]))
                                                                     for w in mat["wiring"]), mat["tiling"]))
    for item in result.get("renamed") or []:
        say("name taken: the new %s is %r (yours keeps %r)" % (item["kind"], item["got"], item["wanted"]))


def finish_import(result, out):
    out["import"] = result
    check = result.get("scene_check") or {}
    if result.get("ok"):
        say("scene check: nothing changed but the import itself (%d expected change(s))" % len(check.get("expected_changes", [])))
        out["ok"] = True
    else:
        out["ok"] = False
        out["error"] = result.get("error") or "the import failed"
        if "scene_unchanged" in result:
            say("the import failed and %s" % ("the scene is exactly as it was" if result["scene_unchanged"]
                                              else "THE SCENE WAS LEFT CHANGED: %s" % check.get("unexpected_changes")))
    summarize_import(result)
    return out


def cmd_search(args):
    if args.source == "polyhaven":
        results, total, note = ph_search(args.words, args.type, args.category, args.limit)
    elif args.source == "polypizza":
        if args.type and args.type != "model":
            raise Fail("Poly Pizza has models only")
        results, total, note = pp_search(args.words, args.category, args.limit)
    else:
        if args.type and args.type != "model":
            raise Fail("Sketchfab has models only")
        results, total, note = sf_search(args.words, args.category, args.limit)
    thumbs = []
    if args.thumbs:
        folder = Path(args.thumbs)
        folder.mkdir(parents=True, exist_ok=True)
        for rec in results:
            url = rec.get("thumbnail_url")
            if not url:
                continue
            if args.source == "polyhaven":
                url = ph_thumb(url)
            stem = folder / ("%s_%s" % (args.source, safe_name(rec["id"])))
            tmp = stem.with_suffix(".part")
            _discard(tmp)
            try:
                got = download(url, tmp, what="the thumbnail of %s" % rec["id"])
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
    manifest, state = fetch_any(args.source, args.id, args.type, args.res, args.format, args.offline)
    return {"ok": True, "command": "fetch", **describe_fetch(manifest, state)}


def cmd_import(args):
    manifest, state = fetch_any(args.source, args.id, args.type, args.res, args.format, args.offline)
    fetched = describe_fetch(manifest, state)
    job = job_from_manifest(manifest, args)
    if job["kind"] != "texture" and args.apply_to:
        raise Fail("--apply-to is for textures")
    result, mode, where = run_blender(job, args)
    out = {"command": "import", "mode": mode, "blender_at": where, "fetch": {k: v for k, v in fetched.items() if k != "meta"}}
    return finish_import(result, out)


def cmd_import_file(args):
    path = Path(args.path).resolve()
    if not path.is_file():
        raise Fail("there is no file at %s" % path)
    ext = path.suffix.lower()
    if ext not in MODEL_EXTENSIONS + HDRI_EXTENSIONS:
        raise Fail("import-file takes %s, not %s" % (" ".join(MODEL_EXTENSIONS + HDRI_EXTENSIONS), ext or "a file without extension"))
    if ext == ".glb":
        check_glb(path)
    kind = "hdri" if ext in HDRI_EXTENSIONS else "model"
    meta = {"source": "file", "id": path.name, "name": path.stem, "kind": kind, "author": "",
            "license": "unknown (a local file)", "page_url": path.as_uri()}
    job = {"op": "import", "kind": kind, "files": {"main": str(path)}, "meta": meta, "options": options_from(args, meta)}
    result, mode, where = run_blender(job, args)
    return finish_import(result, {"command": "import-file", "mode": mode, "blender_at": where, "file": str(path)})


def cmd_credits(args):
    result, mode, where = run_blender({"op": "credits"}, args)
    if not result.get("ok"):
        raise Fail(result.get("error") or "reading the credits failed")
    for line in result.get("credits") or []:
        say(line)
    return {"ok": True, "command": "credits", "mode": mode, "blender_at": where, "credits": result.get("credits")}


def cmd_cache(args):
    root = cache_root()
    entries = []
    for manifest_path in sorted(root.glob("*/*/*/manifest.json")):
        folder = manifest_path.parent
        manifest, damaged = read_entry(folder) if args.verify else (json.loads(manifest_path.read_text(encoding="utf-8")), None)
        item = {"dir": str(folder), "source": folder.parent.parent.name, "id": folder.parent.name, "variant": folder.name}
        if manifest:
            item["bytes"] = sum(f["size"] for f in manifest["files"])
        if args.verify:
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
    g.add_argument("--save-as", metavar="PATH", help="HEADLESS: save the result here (never over the input)")
    g.add_argument("--expect-file", metavar="PATH", help="LIVE: refuse unless the Blender that answers has this file open")
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
    g = p.add_argument_group("an HDRI")
    g.add_argument("--strength", type=float, default=1.0, help="world strength (default 1)")
    g.add_argument("--rotation", type=float, default=0.0, help="world rotation about Z, degrees")
    g.add_argument("--replace-world", action="store_true", help="do not keep the old world with a fake user")
    g = p.add_argument_group("a texture")
    g.add_argument("--apply-to", metavar="OBJ[,OBJ]", help="put the material on these objects (replaces their materials)")
    g.add_argument("--slot", help="only this material slot number (default: all slots)")
    g.add_argument("--no-displacement", action="store_true", help="leave the displacement map out")
    p.add_argument("--pack", action="store_true", help="pack images into the .blend instead of using the cache files")


def build_parser():
    parser = argparse.ArgumentParser(prog="assets.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("search", help="find assets (JSON list)")
    p.add_argument("--source", required=True, choices=SOURCES)
    p.add_argument("--type", choices=KINDS)
    p.add_argument("words", nargs="*", help="what you are looking for")
    p.add_argument("--category", help="polyhaven: a category path (see `categories`); polypizza: a name; sketchfab: a slug")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--thumbs", metavar="DIR", help="download each result's thumbnail into DIR")
    p.add_argument("--offline", action="store_true", help="answer from cached lists only")
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
    global OFFLINE
    args = build_parser().parse_args(argv)
    OFFLINE = bool(getattr(args, "offline", False))
    try:
        out = args.fn(args)
    except Fail as exc:
        out = {"ok": False, "command": args.command, "error": str(exc)}
        say("FAILED: %s" % exc)
    except HTTPStatus as exc:
        out = {"ok": False, "command": args.command, "error": "%s answered HTTP %d" % (exc.url, exc.status)}
        say("FAILED: %s" % out["error"])
    except KeyboardInterrupt:
        out = {"ok": False, "command": args.command, "error": "interrupted"}
    except Exception as exc:  # noqa: BLE001 - still one JSON line, and the traceback for whoever reads stderr
        import traceback
        traceback.print_exc()
        out = {"ok": False, "command": args.command, "error": "unexpected error: %s: %s" % (type(exc).__name__, exc)}
    print(json.dumps(out, default=str), flush=True)
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())

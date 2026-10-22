# SPDX-License-Identifier: GPL-3.0-or-later
"""The fixtures of the attack rows (R20-R36 of test_assets.py), made from the base fixtures with the standard
library only. make_fixtures.py runs it last; it can also run alone:

    python make_attack_fixtures.py [<fixtures folder>]

  files/fx_crate_badlen.glb   fx_crate.glb with a header that claims 5000 bytes (a GLB cut or padded in transit)
  files/sf_escape.zip         a sound zip whose glTF names an image at ../../secret_outside.png
  api/ph_*_fx_huge.json       an HDRI whose 24k EXR is published as 2.45 GB (served by the tiny sky file)
  api/ph_*_fx_http.json       an HDRI whose file address is plain http:// on another host
  api/sf_*_<uid>.json         Sketchfab records for the two files above
"""
import hashlib
import json
import os
import sys
import zipfile

OUT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__)))
FILES = os.path.join(OUT, "files")
API = os.path.join(OUT, "api")
SF_BADGLB = "sfbadglb000000000000000000000005"
SF_ESCAPE = "sfescape000000000000000000000006"


def info(path):
    data = open(path, "rb").read()
    return {"size": len(data), "md5": hashlib.md5(data).hexdigest()}


def entry(rel):
    out = info(os.path.join(FILES, rel))
    out["url"] = "{BASE}/files/" + rel.replace(os.sep, "/")
    return out


def save_json(name, data):
    with open(os.path.join(API, name), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)


def load_json(name):
    with open(os.path.join(API, name), encoding="utf-8") as f:
        return json.load(f)


# a GLB whose header length is wrong
glb = bytearray(open(os.path.join(FILES, "fx_crate.glb"), "rb").read())
glb[8:12] = (5000).to_bytes(4, "little")
open(os.path.join(FILES, "fx_crate_badlen.glb"), "wb").write(bytes(glb))

# a zip that is sound as an archive, but whose glTF reaches outside the asset's folder for an image
box = os.path.join(FILES, "fx_box")
with open(os.path.join(box, "fx_box_1k.gltf"), encoding="utf-8") as f:
    doc = json.load(f)
doc["images"][0]["uri"] = "../../secret_outside.png"
members = []
for folder, _dirs, names in os.walk(box):
    for name in names:
        members.append(os.path.relpath(os.path.join(folder, name), box).replace(os.sep, "/"))
with zipfile.ZipFile(os.path.join(FILES, "sf_escape.zip"), "w", zipfile.ZIP_DEFLATED) as z:
    for rel in sorted(members):
        info_ = zipfile.ZipInfo("scene/" + rel, date_time=(2026, 10, 22, 0, 0, 0))
        if rel == "fx_box_1k.gltf":
            z.writestr(info_, json.dumps(doc, indent=1, sort_keys=True))
        else:
            z.writestr(info_, open(os.path.join(box, rel), "rb").read())

# Poly Haven: a huge download and a plain-http address
sky_info = load_json("ph_info_fx_sky.json")
sky = load_json("ph_files_fx_sky2.json")["hdri"]["1k"]["hdr"]
save_json("ph_info_fx_huge.json", dict(sky_info, name="Huge Sky", max_resolution=[24576, 12288]))
save_json("ph_files_fx_huge.json", {"hdri": {"1k": {"hdr": sky},
                                             "24k": {"exr": dict(sky, size=2451000000)}}})
save_json("ph_info_fx_http.json", dict(sky_info, name="Plain Http Sky"))
save_json("ph_files_fx_http.json", {"hdri": {"1k": {"hdr": dict(sky, url="http://example.invalid/fx_http_1k.hdr")}}})

# Sketchfab records for the two files
model = load_json("sf_model_sfboxok0000000000000000000000001.json")
for uid, name, links in ((SF_BADGLB, "Bad Length Glb", {"glb": dict(entry("fx_crate_badlen.glb"), expires=300)}),
                         (SF_ESCAPE, "Escaping Zip", {"gltf": dict(entry("sf_escape.zip"), expires=300)})):
    save_json("sf_model_%s.json" % uid, dict(model, uid=uid, name=name,
                                             viewerUrl="https://sketchfab.com/3d-models/fixture-" + uid))
    save_json("sf_download_%s.json" % uid, {k: {kk: vv for kk, vv in v.items() if kk != "md5"} for k, v in links.items()})
print("ATTACK_FIXTURES|", OUT)

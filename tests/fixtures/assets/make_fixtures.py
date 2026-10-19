# SPDX-License-Identifier: GPL-3.0-or-later
"""Make the network-free fixtures for test_assets.py: tiny files of our own and the API answers that point at them.

    blender --background --factory-startup --python make_fixtures.py -- <output folder>

Everything here is made from nothing (no real asset is copied): 16 x 16 JPEGs, an 8 x 4 HDR, a box model
exported as glTF and GLB, zips built from them (one sound, one with a '..' member, one with an absolute
member), a MaterialX file, and JSON answers in the shapes the real APIs use (recorded from Poly Haven's and
Sketchfab's public endpoints on 2026-10-19 and trimmed; Poly Pizza's from its API description). URLs inside
the JSON carry the placeholder {BASE}, which the test server replaces with its own address.
"""
import hashlib
import json
import os
import shutil
import sys
import zipfile

import bpy

OUT = os.path.abspath(sys.argv[sys.argv.index("--") + 1])
FILES = os.path.join(OUT, "files")
API = os.path.join(OUT, "api")


def fresh(path):
    if os.path.isdir(path):
        shutil.rmtree(path)
    os.makedirs(path)


def info(path):
    data = open(path, "rb").read()
    return {"size": len(data), "md5": hashlib.md5(data).hexdigest()}


def entry(rel, url_prefix="{BASE}/files/"):
    out = info(os.path.join(FILES, rel))
    out["url"] = url_prefix + rel.replace(os.sep, "/")
    return out


def save_json(name, data):
    with open(os.path.join(API, name), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)


def image(name, path, rgba, fmt="JPEG", size=(16, 16), float_buffer=False):
    img = bpy.data.images.new(name, size[0], size[1], float_buffer=float_buffer, alpha=False)
    w, h = size
    px = []
    for y in range(h):
        for x in range(w):
            k = ((x // 4 + y // 4) % 2) * 0.2
            px += [min(1.0, rgba[0] + k), min(1.0, rgba[1] + k), min(1.0, rgba[2] + k), 1.0]
    img.pixels[:] = px
    img.filepath_raw = path
    img.file_format = fmt
    img.save()
    return img


os.makedirs(OUT, exist_ok=True)
fresh(FILES)                          # only our two folders are emptied; this script lives beside them
fresh(API)
os.makedirs(os.path.join(FILES, "thumbs"))

# ---------------------------------------------------------------- pictures
image("thumb", os.path.join(FILES, "thumbs", "thumb.png"), (0.8, 0.5, 0.1), fmt="PNG", size=(8, 8))
image("sky", os.path.join(FILES, "fx_sky_1k.hdr"), (0.6, 0.7, 1.0), fmt="HDR", size=(8, 4), float_buffer=True)
data = open(os.path.join(FILES, "fx_sky_1k.hdr"), "rb").read()
open(os.path.join(FILES, "fx_short_1k.hdr"), "wb").write(data[: len(data) // 2])        # served short
ROCK = {"diff": (0.5, 0.45, 0.4), "rough": (0.7, 0.7, 0.7), "nor_gl": (0.5, 0.5, 1.0), "nor_dx": (0.5, 0.5, 1.0),
        "disp": (0.5, 0.5, 0.5), "ao": (0.9, 0.9, 0.9), "arm": (0.9, 0.7, 0.0)}
for code, colour in ROCK.items():
    image("rock_" + code, os.path.join(FILES, "fx_rock_%s_1k.jpg" % code), colour)
METAL = {"col_1": (0.6, 0.6, 0.65), "metal": (1.0, 1.0, 1.0), "nor_dx": (0.5, 0.5, 1.0), "arm": (1.0, 0.3, 1.0)}
for code, colour in METAL.items():
    image("metal_" + code, os.path.join(FILES, "fx_metal_%s_1k.jpg" % code), colour)
with open(os.path.join(FILES, "fx_rock_1k.mtlx"), "w", encoding="utf-8") as f:
    f.write("""<?xml version="1.0"?>
<materialx version="1.38">
  <nodegraph name="NG_fx_rock">
    <image name="diff" type="color3"><input name="file" type="filename" value="textures/fx_rock_diff_1k.jpg" /></image>
    <image name="rough" type="float"><input name="file" type="filename" value="textures/fx_rock_rough_1k.exr" /></image>
    <image name="normal" type="vector3"><input name="file" type="filename" value="textures/fx_rock_nor_gl_1k.exr" /></image>
    <image name="disp" type="float"><input name="file" type="filename" value="textures/fx_rock_disp_1k.png" /></image>
  </nodegraph>
  <displacement name="Displacement" type="displacementshader">
    <input name="displacement" type="float" output="output_disp_out" nodegraph="NG_fx_rock" />
    <input name="scale" type="float" value="0.02" />
  </displacement>
</materialx>
""")

# ---------------------------------------------------------------- the box model
bpy.ops.wm.read_homefile(use_empty=True)
scene = bpy.context.scene
root = bpy.data.objects.new("FxRoot", None)
scene.collection.objects.link(root)
root.location = (0.3, 0.2, 0.0)


def box(name, lo, hi, parent):
    import bmesh
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    for v in bm.verts:
        v.co.x = lo[0] if v.co.x < 0 else hi[0]
        v.co.y = lo[1] if v.co.y < 0 else hi[1]
        v.co.z = lo[2] if v.co.z < 0 else hi[2]
    bm.to_mesh(me)
    bm.free()
    me.uv_layers.new(name="UVMap")
    ob = bpy.data.objects.new(name, me)
    scene.collection.objects.link(ob)
    ob.parent = parent
    return ob


body = box("Box", (-0.35, -0.37, 0.1), (-0.15, -0.07, 0.6), root)        # world x -0.05..0.15, y -0.17..0.13
lid = box("Lid", (-0.33, -0.35, 0.6), (-0.17, -0.09, 0.7), root)          # on top: total z 0.1..0.7
mat = bpy.data.materials.new("fx_box_mat")
mat.use_nodes = True
tex = mat.node_tree.nodes.new("ShaderNodeTexImage")
tex.image = image("fx_box_diff_1k", os.path.join(FILES, "fx_box_diff_1k.jpg"), (0.8, 0.6, 0.2))
bsdf = next(n for n in mat.node_tree.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled")
mat.node_tree.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
body.data.materials.append(mat)
lid.data.materials.append(mat)
bpy.context.view_layer.update()
lo = [min((ob.matrix_world @ v.co)[i] for ob in (body, lid) for v in ob.data.vertices) for i in range(3)]
hi = [max((ob.matrix_world @ v.co)[i] for ob in (body, lid) for v in ob.data.vertices) for i in range(3)]
gltf_dir = os.path.join(FILES, "fx_box")
os.makedirs(gltf_dir)
bpy.ops.export_scene.gltf(filepath=os.path.join(gltf_dir, "fx_box_1k.gltf"), export_format="GLTF_SEPARATE",
                          export_texture_dir="textures")
bpy.ops.export_scene.gltf(filepath=os.path.join(FILES, "fx_crate.glb"), export_format="GLB")
tex.image.pack()
os.remove(os.path.join(FILES, "fx_box_diff_1k.jpg"))
# The .blend a Poly Haven model comes as: its objects in a collection named for the asset, plus a driver that
# would run Python (it must arrive muted) and a text marked to run on load (it must arrive unrun).
holder = bpy.data.collections.new("fx_boxblend")
scene.collection.children.link(holder)
for ob in (root, body, lid):
    scene.collection.objects.unlink(ob)
    holder.objects.link(ob)
lid["fx_prop"] = 0.0
fc = lid.driver_add('["fx_prop"]')
fc.driver.type = "SCRIPTED"
fc.driver.expression = "0.0 if frame > -1 else __import__('os').getcwd()"
text = bpy.data.texts.new("fixture_autorun.py")
text.write("print('FIXTURE TEXT RAN - it must never run')" + chr(10))
text.use_module = True
bpy.ops.wm.save_as_mainfile(filepath=os.path.join(FILES, "fx_boxblend_1k.blend"), copy=True, compress=True)
gltf_files = []
for folder, _dirs, names in os.walk(gltf_dir):
    for name in names:
        gltf_files.append(os.path.relpath(os.path.join(folder, name), gltf_dir).replace(os.sep, "/"))

# ---------------------------------------------------------------- zips for Sketchfab
with zipfile.ZipFile(os.path.join(FILES, "sf_box.zip"), "w", zipfile.ZIP_DEFLATED) as z:
    for rel in gltf_files:
        z.write(os.path.join(gltf_dir, rel), "scene/" + rel)
with zipfile.ZipFile(os.path.join(FILES, "sf_slip.zip"), "w") as z:
    for rel in gltf_files:
        z.write(os.path.join(gltf_dir, rel), rel)
    z.writestr("../../../../zipslip_escaped.txt", "this must never be written")
with zipfile.ZipFile(os.path.join(FILES, "sf_abs.zip"), "w") as z:
    for rel in gltf_files:
        z.write(os.path.join(gltf_dir, rel), rel)
    z.writestr(zipfile.ZipInfo("/zipslip_absolute.txt"), "this must never be written")

# ---------------------------------------------------------------- Poly Haven answers
THUMB = "{BASE}/files/thumbs/thumb.png?width=256&height=256&format=webp&v=fx1"


def ph_asset(name, type_, downloads, **extra):
    rec = {"name": name, "type": type_, "authors": {"Fixture Author": "All"}, "download_count": downloads,
           "categories": ["fixture"], "tags": ["fixture"], "max_resolution": [1024, 512] if type_ == 0 else [1024, 1024],
           "thumbnail_url": THUMB, "date_published": 1700000000}
    rec.update(extra)
    return rec


size_mm = [round((h - l) * 1000.0, 3) for l, h in zip(lo, hi)]
hdris = {"fx_sky": ph_asset("Fixture Sky", 0, 900, category="Skies"),
         "fx_sky2": ph_asset("Fixture Sky Two", 0, 100, category="Skies"),
         "fx_cut": ph_asset("Cut Sky", 0, 5), "fx_short": ph_asset("Short Sky", 0, 4),
         "fx_badmd5": ph_asset("Bad Sky", 0, 3)}
textures = {"fx_rock": ph_asset("Fixture Rock", 1, 500, dimensions=[1800, 1800], category="Stone/Walls"),
            "fx_metal": ph_asset("Fixture Metal", 1, 50, dimensions=[1000, 500], category="Metal")}
models = {"fx_box": ph_asset("Fixture Box", 2, 300, dimensions=size_mm, category="Props", polycount=24),
          "fx_slip": ph_asset("Slipping Box", 2, 1, dimensions=size_mm),
          "fx_boxblend": ph_asset("Fixture Blend Box", 2, 2, dimensions=size_mm)}
save_json("ph_assets_hdris.json", hdris)
save_json("ph_assets_textures.json", textures)
save_json("ph_assets_models.json", models)
save_json("ph_assets_all.json", {**hdris, **textures, **models})
save_json("ph_assets_textures_stone.json", {"fx_rock": textures["fx_rock"]})
save_json("ph_search_sky_hdris.json", {"query": "sky", "results": [{"slug": "fx_sky2", "score": 0.7},
                                                                    {"slug": "fx_sky", "score": 0.6},
                                                                    {"slug": "not_in_list", "score": 0.5}]})
save_json("ph_taxonomy_textures.json", {"type": "textures", "categories": [
    {"name": "Stone", "path": "Stone", "children": [{"name": "Walls", "path": "Stone/Walls", "children": []}]},
    {"name": "Metal", "path": "Metal", "children": []}]})
for slug, rec in {**hdris, **textures, **models}.items():
    save_json("ph_info_%s.json" % slug, rec)
sky = entry("fx_sky_1k.hdr")
save_json("ph_files_fx_sky.json", {"hdri": {"1k": {"hdr": sky}}, "tonemapped": entry("thumbs/thumb.png")})
save_json("ph_files_fx_sky2.json", {"hdri": {"1k": {"hdr": sky}}})
cut = dict(sky, url="{BASE}/cut/fx_sky_1k.hdr")
save_json("ph_files_fx_cut.json", {"hdri": {"1k": {"hdr": cut}}})
save_json("ph_files_fx_short.json", {"hdri": {"1k": {"hdr": dict(sky, url="{BASE}/files/fx_short_1k.hdr")}}})
save_json("ph_files_fx_badmd5.json", {"hdri": {"1k": {"hdr": dict(sky, md5="0" * 32)}}})
rock = {}
for code, key in (("diff", "Diffuse"), ("rough", "Rough"), ("nor_gl", "nor_gl"), ("nor_dx", "nor_dx"),
                  ("disp", "Displacement"), ("ao", "AO"), ("arm", "arm")):
    rock[key] = {"1k": {"jpg": entry("fx_rock_%s_1k.jpg" % code)}}
rock["mtlx"] = {"1k": {"mtlx": dict(entry("fx_rock_1k.mtlx"), include={})}}
rock["gltf"] = {"1k": {"gltf": {"url": "{BASE}/files/never_fetched.gltf", "size": 1, "md5": "0" * 32, "include": {}}}}
save_json("ph_files_fx_rock.json", rock)
metal = {"col_1": {"1k": {"jpg": entry("fx_metal_col_1_1k.jpg")}},
         "Metal": {"1k": {"jpg": entry("fx_metal_metal_1k.jpg")}},
         "nor_dx": {"1k": {"jpg": entry("fx_metal_nor_dx_1k.jpg")}},
         "arm": {"1k": {"jpg": entry("fx_metal_arm_1k.jpg")}}}
save_json("ph_files_fx_metal.json", metal)
include = {rel: entry("fx_box/" + rel) for rel in gltf_files if not rel.endswith(".gltf")}
main = entry("fx_box/fx_box_1k.gltf")
main["include"] = include
save_json("ph_files_fx_box.json", {"gltf": {"1k": {"gltf": main}}, "blend": {"1k": {"blend": dict(
    entry("fx_boxblend_1k.blend"), include={})}}})
slip = dict(main, include=dict(include, **{"../../escape.bin": entry("fx_box/" + [r for r in gltf_files if r.endswith(".bin")][0])}))
save_json("ph_files_fx_slip.json", {"gltf": {"1k": {"gltf": slip}}})
save_json("ph_files_fx_boxblend.json", {"blend": {"1k": {"blend": dict(entry("fx_boxblend_1k.blend"), include={})}}})

# ---------------------------------------------------------------- Poly Pizza answers (shape: api.poly.pizza v1.1)
def pp(ident, title, download, licence="CC-BY 3.0"):
    return {"ID": ident, "Title": title, "Attribution": '"%s" by Fixture Maker, https://poly.pizza/m/%s. Licence %s'
            % (title, ident, licence), "Thumbnail": "{BASE}/files/thumbs/thumb.png", "Download": download,
            "Tri Count": 24, "Creator": {"Username": "Fixture Maker"}, "Category": "Objects", "Tags": ["box"],
            "Licence": licence, "Animated": False}


crate = pp("PPfxCrate", "Fixture Crate", "{BASE}/files/fx_crate.glb")
save_json("pp_model_PPfxCrate.json", crate)
save_json("pp_model_PPfxBot.json", pp("PPfxBot", "Bot Check", "{BASE}/botcheck/model.glb"))
save_json("pp_search_crate.json", {"total": 1, "results": [crate]})

# ---------------------------------------------------------------- Sketchfab answers (shape recorded from api.sketchfab.com/v3)
def sf(uid, name):
    return {"uid": uid, "name": name, "viewerUrl": "https://sketchfab.com/3d-models/fixture-" + uid,
            "isDownloadable": True, "faceCount": 24, "vertexCount": 16,
            "license": {"label": "CC Attribution", "slug": "by", "url": "http://creativecommons.org/licenses/by/4.0/",
                        "requirements": "Author must be credited. Commercial use is allowed."},
            "user": {"username": "fixture_artist", "displayName": "Fixture Artist"},
            "categories": [{"name": "furniture-home"}], "tags": [{"name": "box", "slug": "box"}],
            "thumbnails": {"images": [{"url": "{BASE}/files/thumbs/thumb.png", "width": 720, "height": 405},
                                      {"url": "{BASE}/files/thumbs/thumb.png", "width": 200, "height": 112}]},
            "archives": {"gltf": {"size": 1000, "type": "gltf"}, "glb": {"size": 900, "type": "glb"}}}


uids = {"sfboxok0000000000000000000000001": ("Fixture Zip Box", {"gltf": dict(entry("sf_box.zip"), expires=300)}),
        "sfslip00000000000000000000000002": ("Slip Zip", {"gltf": dict(entry("sf_slip.zip"), expires=300)}),
        "sfabs000000000000000000000000003": ("Absolute Zip", {"gltf": dict(entry("sf_abs.zip"), expires=300)}),
        "sfglb000000000000000000000000004": ("Fixture Glb", {"glb": dict(entry("fx_crate.glb"), expires=300),
                                                             "gltf": dict(entry("sf_box.zip"), expires=300)})}
for uid, (name, links) in uids.items():
    save_json("sf_model_%s.json" % uid, sf(uid, name))
    save_json("sf_download_%s.json" % uid, {k: {kk: vv for kk, vv in v.items() if kk != "md5"} for k, v in links.items()})
save_json("sf_search_box.json", {"cursors": {"next": None, "previous": None}, "next": None, "previous": None,
                                 "results": [sf(u, n) for u, (n, _l) in uids.items()][:2]})
save_json("expected.json", {"box_bounds": {"min": lo, "max": hi, "size": [h - l for l, h in zip(lo, hi)]},
                            "box_objects": ["FxRoot", "Box", "Lid"], "gltf_files": gltf_files})
print("FIXTURES|", OUT, sorted(os.listdir(FILES)), sorted(os.listdir(API)))

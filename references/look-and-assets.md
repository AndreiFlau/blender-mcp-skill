# Seeing the scene and bringing assets in: look.py and assets.py

Two command-line tools beside `bmcp.py`:

- `scripts/look.py` shows a scene as pictures: one labelled contact sheet plus every view as its own PNG.
- `scripts/assets.py` finds HDRIs, textures and models (Poly Haven, Poly Pizza, Sketchfab, or a file on
  disk) and puts them in a scene at the right size, with credits.

Both are one script each, Python 3.10+, standard library only. The part that runs inside Blender is plain
bpy + numpy (Blender ships numpy). `--help` works offline and lists every option.

Below, `look.py` means `python <this-skill-dir>/scripts/look.py`, and the same for `assets.py` and
`bmcp.py`. Every command here was run on Windows with Blender 5.2.2 and bridge 1.0.3 on 2026-10-22,
except the ones marked as needing a window or as not run. The example scene has a `Floor`, a `Chair` (in
collection `Furniture`), a rig `Rig` whose bone `hips` moves `Body`, a hidden `Lamp` and a camera.

## Which Blender it works on

**LIVE** (the default) goes through the bridge of a running Blender, at `BLENDER_MCP_HOST` /
`BLENDER_MCP_PORT` (default `localhost:9876`), like `bmcp.py`. Ask who answers, then name it:

```
bmcp.py "import os; result = {'pid': os.getpid(), 'file': bpy.data.filepath}"
look.py --expect-pid 34648 --mode angles --target Chair
assets.py import polyhaven rubber_duck_toy --size 0.3 --at 1,0,0 --expect-pid 34648
```

- `--expect-pid N` refuses unless process N answers.
- `--expect-file PATH` refuses unless that file is open. A full path means that exact file, however it is
  spelled. A bare name matches that name in any folder, and look.py warns that it did.
- The wrong Blender gives exit 3: `WRONG BLENDER, nothing was run` (look) or `nothing was touched`
  (assets). assets.py checks this before it downloads anything.

**HEADLESS** (`--blend FILE` or `--new`) starts a background Blender with `--factory-startup` and script
auto-run off, does the job and exits.

```
look.py --blend demo.blend --mode angles --target Chair
look.py --new --setup build_demo.py --mode angles --save-as demo.blend
assets.py import polyhaven rubber_duck_toy --blend demo.blend --save-as duck.blend
```

- Blender is found from `--blender EXE`, then `$BLENDER_EXE`, then `blender` on PATH, then the usual
  install folders (Program Files, Steam, /Applications, /usr/bin, /opt). The tool prints which.
- `look.py --new` is Blender's factory scene (cube, camera, light). `assets.py --new` is an empty file.
- Factory startup means your installed add-ons are not loaded. Load the one you test in `--setup`.
- `--save-as PATH` saves a copy at the end, never over the input file, however its path is spelled.
  A headless `assets.py import` keeps nothing without it, so it needs `--save-as` (or `--dry-run`).
  When look.py saves over an older copy, Blender leaves its `.blend1` backup beside it.
- Nothing is written beside your .blend: look.py refuses an `--out` in the .blend's folder.

### A bridge on a spare port

For tests, or for a second Blender an AI can use without touching yours.

The extension's own command needs the extension installed and enabled in the profile Blender starts with,
so not with `--factory-startup`. Its `cli.py` takes `--host` and `--port`. This form was not run here:

```
blender --background --online-mode FILE.blend --command blender_mcp --port 9895
```

Without installing anything: unzip the add-on (1.0.3 or later) into a folder and boot it with this
script, `serve_bridge.py`. This one was run, and both tools worked through it:

```python
# blender --background --factory-startup FILE.blend --python serve_bridge.py -- PACKAGE_DIR PORT
import importlib.util, os, sys

pkg, port = sys.argv[sys.argv.index("--") + 1:][:2]
spec = importlib.util.spec_from_file_location("bridge_mcp", os.path.join(pkg, "__init__.py"),
                                              submodule_search_locations=[pkg])
sys.modules["bridge_mcp"] = module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
from bridge_mcp import execute_blocking, mcp_to_blender_server as server

server.start("127.0.0.1", int(port))
print("BRIDGE_UP", port, os.getpid(), flush=True)
try:
    execute_blocking.run()          # serves until the Blender is stopped
finally:
    server.stop()
```

Then `BLENDER_MCP_PORT=9900 look.py --expect-pid <the pid it printed> ...`. Check the zip's sha256
before unzipping it (1.0.3: `a7a9da816192502e5a0a202a396444e266b47d8fc4f74ad4698048bd43040707`). A
background bridge answers one request at a time, each in one go: it has no deferred responses.

## look.py

### Modes

- `angles`: the targets from several sides, framed on their real vertices.
  `look.py --mode angles --target Chair --views front,right,top,three_quarter`
- `camera`: through the scene camera, at the render's aspect.
  `look.py --mode camera --shading material --out shots/cam.png`
- `frames`: a strip over the animation, one fixed view.
  `look.py --mode frames --frame-count 8 --view front --target Rig --center-bones hips`
- `viewport`: what the user sees in the largest 3D view. Windowed Blender only.
  `look.py --mode viewport --expect-pid 34648`
- `image`: a file, an image datablock, or `"Render Result"`.
  `look.py --mode image --image shots/cam.png`
- `ui`: a screenshot of part of the window. Windowed Blender only. It is a real screenshot, so whatever sits
  on top of that part (the start-up splash, an open menu, a popup) is in the picture too.
  `look.py --mode ui --ui sidebar:MyAddon --expect-pid 34648`

With no `--mode`: viewport in a windowed Blender, angles otherwise. look.py warns when a live background
Blender made it choose angles.

### Options, one example each

What to look at:

- `--target Chair` objects (their children too) or collections (`--target Furniture`), comma separated,
  repeatable. Default: every visible object. A rig named here brings the meshes it deforms.
- `--unhide` shows hidden targets for the pictures: `--target Lamp --unhide`.
- `--isolate` hides everything else for the pictures: `--target Chair --isolate`. The selection and the
  hiding come back as they were.

Angles and framing:

- `--views front,back,left,right,top,bottom,three_quarter`, or a direction toward the eye:
  `--views 1:-1:0.3`. Up to 6. Default `front,right,top,three_quarter`.
- `--ortho` orthographic: `--views front,top --ortho`.
- `--distance 4` Blender units from the targets' centre to the eye (perspective only).
- `--fov 50` vertical field of view, 5 to 120 degrees (default 35).
- `--fill 0.6` how much of the tile the targets fill, 0.2 to 0.98 (default 0.85).

Frames:

- `--frames 1-41:10`, `--frames 1,5,9` or `--frames 1-21`, at most 24 frames.
- `--frame-count 8` spread over the scene range, or the preview range when it is on (2 to 24, default 6).
- `--view camera`, `--view viewport`, an angle name or `x:y:z`: one view for the whole strip.
- `--center-bones hips` keeps those bones in the middle of every tile (with an angle view).
- `--armature Rig` says whose bones: `--target Body --armature Rig --center-bones hips`.

Every frames tile is the animation at that frame, as playback shows it, the current frame too. Changes not
yet keyed are kept and put back afterwards.

Shading, size and output:

- `--shading solid|material|rendered|wireframe|xray`. Default: the viewport's own when live, solid
  headless. `material` uses Blender's studio light. `rendered` uses the scene's world, with EEVEE (a
  Cycles scene is drawn with EEVEE, and a warning says so). `xray` is see-through with bones in front.
- `--overlays` keeps the viewport overlays (bones, outlines) in camera, angles and frames views.
- `--max-size 512` longest side of each tile (64 to 4096, default 768).
- `--sheet-size 1024` longest side of the sheet (256 to 8192, default 1536).
- `--out shots/chair` a folder: it gets `look_<mode>_sheet.png`, `look_<mode>_NN_<view>.png` and
  `look_<mode>_result.json`. `--out shots/cam.png` with one picture: the clean picture is `cam.png`, its
  labelled sheet `cam_sheet.png`. A .png with several pictures: the sheet there, the tiles beside it.
  Default: a new folder in the system temp folder. Earlier pictures of the same name are replaced.
- `--brief` a short JSON line without matrices. The whole result is still in `result_file`.

Image and ui (the ui options are only checked by the windowed check below):

- `--image "Render Result"`, `--image rock_wall_08_diff_1k.jpg` (an image datablock's name), or a file.
  A relative path is taken from where you run the command.
- `--ui window | view3d | sidebar:TAB | properties:TAB | preferences:ADDON | area:TYPE`. For the sidebar,
  TAB is the tab's label (`sidebar:Item`, or your add-on's `bl_category`). For Properties it is the tab's
  name (`properties:MATERIAL`). ADDON is the add-on's module or name. TYPE is an editor (`area:OUTLINER`).
- `--ui-object Chair` pins that object in the Properties editor: `--ui properties:MATERIAL --ui-object Chair`.
- `--redraws 5` redraws before the screenshot (1 to 10, default 3).
- `--fallback camera` (or `angles`) is what to show when the viewport cannot be drawn:
  `--mode viewport --fallback camera`. It applies to viewport only.

How it runs:

- `--backend render` renders each tile with Workbench or EEVEE from a temporary copy of the scene, instead
  of drawing the viewport. Background Blender only. The warnings say what differs.
- `--run-handlers` lets the session's frame-change, depsgraph and render handlers run (see below).
- `--no-deferred` does the whole job in one bridge request (matters live, in a windowed Blender).
- `--tick-budget 0` seconds of work per bridge tick (default 0.15; 0 is one tile a tick).
- `--timeout 120` seconds to wait (default 300 live, 600 headless).
- `--tmp-dir tmp/look` where Blender writes its short-lived files.

Headless only:

- `--setup build_demo.py` runs a Python file first. What it prints comes back as `setup:` lines.
- `--save-as demo.blend`.
- `--blender path/to/blender` this Blender, not the one it would find.
- `--enable-autoexec` lets the file's scripts and Python drivers run. Only for files you trust.
- `--keep-log` keeps Blender's log. Its path is `client.log` in the JSON.

### The JSON line

One line on stdout, the same as `result_file`. Read these first:

- `ok`, and on failure `code` and `error`.
- `sheet.path`, and `tiles[]` with `path`, `label`, `shows` (view, frame, shading) and `projection`
  (eye, centre, view and projection matrices, to map a 3D point to a pixel).
- `targets`: requested, framed, hidden, excluded, linked, unhidden, collections.
- `warnings`: read every one. They say when a picture may not be what you think it is.
- `fingerprint`: `equal`, `values` (how many were compared), `differences`, `covers`.
- `frames` (frames mode): the frames, and which range they came from (scene or preview).

Human lines go to stderr: the Blender used, each tile, the warnings, and
`checked: the session is as it was in the 1047 values compared`.

### How the pictures are made

Blender draws its own viewport into an offscreen buffer, with the view this tool works out. A background
Blender 5.2 or later can do that too, after `gpu.init()`, so headless pictures are Blender's real viewport
drawing. A headless run takes 1 to 2 s. Before 5.2, or with `--backend render`, each tile is a render of a
temporary copy of the scene; wireframe and X-ray are imitated, and the warnings say so. The user's Render
Result and render settings are never touched.

In a windowed Blender a job of several tiles is cut into bridge ticks (the bridge's deferred response,
`check_is_finished`). Each tick puts everything back and is checked before Blender's event loop runs
again, so the window stays alive and the bridge's request timeout is never reached. A background Blender
does the whole job in one go.

The window still pauses during each tick: two fingerprints (before and after, to tell the tool's changes
from the user's), the tiles, the frame changes and putting everything back. Measured on 2026-10-22 on a
scene of 3000 objects, a 12-frame look took 7 ticks of 0.39 s on average and 0.68 s at most (each
fingerprint 0.085 s, each tile 0.03 s). `--tick-budget 0` gives one tile a tick but cannot go below the
two fingerprints, and in a frames look every frame change costs what the scene's own evaluation costs (a
heavy rig that takes seconds per frame pauses the window that long per tile).

### Handlers are paused

While a look works, the session's frame-change, depsgraph-update and render handlers are paused, then put
back in order. An add-on that records or sends every change (a live link, a recorder) sees none of the
temporary ones. The JSON lists what was paused (`handlers_paused`), and frames mode warns.

The catch for add-on work: if your add-on moves things from a frame-change handler, a frames look shows
the animation without it. Add `--run-handlers` to see what playback shows; whatever the handlers do is
then not undone. Checked: a handler that moves `Chair` with the frame gave two identical tiles by default
and two different ones with `--run-handlers`. The control, without the handler, gave identical tiles.

### What headless can show, and what it cannot

It can show angles, camera, frames and image in every shading, the scene after a `--setup` script built it
or ran an add-on's operators, and rendered views lit by the scene's world.

It cannot show:

- the user's viewport or any UI (`viewport` and `ui` need a windowed Blender; `--fallback` shows camera
  or angles instead of viewport);
- anything that needs Blender's event loop: timers, modal operators, the deferred path;
- what Python drivers do, unless `--enable-autoexec` (look.py names the drivers that did not run);
- your add-ons, preferences and theme (factory startup), unless `--setup` loads them.

To check a panel's `draw()` without a window, use the blender-headless-qa skill.

## assets.py

### Commands, one example each

```
assets.py search --source polyhaven --type hdri sky --limit 3 --thumbs thumbs
assets.py search --source polyhaven --type texture --category "Brick & Block" --limit 3
assets.py categories --source polyhaven --type texture
assets.py fetch polyhaven rubber_duck_toy --res 1k
assets.py fetch polyhaven rock_wall_08 --type texture --res 1k --format jpg
assets.py import polyhaven kloppenheim_06_puresky --strength 0.8 --rotation 90 --blend demo.blend --save-as lit.blend
assets.py import polyhaven rock_wall_08 --apply-to Floor --blend lit.blend --save-as floor.blend
assets.py import polyhaven rubber_duck_toy --size 0.3 --at 1,0,0 --rotate 45 --blend floor.blend --save-as duck.blend
assets.py import-file <this-skill-dir>/tests/fixtures/assets/files/fx_crate.glb --size 0.5 --new --save-as crate.blend
assets.py credits --blend duck.blend
assets.py cache --verify
```

- `search` prints id, name, type, licence, author, sizes and categories. `--thumbs DIR` saves the
  thumbnails to look at. `--offline` answers from cached lists (Poly Haven, plain word matching).
- `categories` lists what `--category` takes.
- `fetch` downloads into the cache and checks it. `import` fetches, then puts it in the scene.
- `import-file` takes a local glb, gltf, fbx, obj, usd/usda/usdc/usdz, stl, ply, abc, blend, hdr or exr.
- `credits` prints the scene's `ASSET_CREDITS` text. `cache` lists the cache; `--verify` checks every
  file again.

### HDRIs

A new world, wired and lighting the scene. The old world is kept with a fake user.

- `--strength 0.8`, `--rotation 90` (degrees about Z).
- `--replace-world` does not keep the old one.

### Textures

A Principled material with every map wired (colour, roughness, metal, normal, displacement), at the
texture's real size.

- `--apply-to Floor,Wall` puts it on those objects, replacing their materials. The old ones are kept with
  a fake user and listed in the JSON.
- The tiling is worked out at import, from each object's current size and the UV map renders use (box
  projection when there is none). Size the object first. After resizing it, import again.
- `--slot 1` on an object with several material slots, or `--slot all`. Without it the import stops and
  lists the slots.
- `--no-displacement` leaves the displacement map out. By default it is wired as Blender's bump. For real
  displacement, set the material's displacement method and subdivide (the JSON says the same).
- `--name RockFloor` names the material.
- `--pack` packs the images into the .blend. Without it the saved file reads them from the cache.

### Models

A new collection named after the asset, measured on its real geometry, resting on the ground.

- `--size 0.3` largest side in metres, measured before `--rotate`. Default: its real size.
- `--at 1,0,0` where the centre of its footprint goes, in metres.
- `--rotate 45` turns it about Z.
- `--no-ground` keeps the file's own height.
- `--name Duck` names the collection.
- `--pick fx_boxblend` (a .blend only) appends just these collections or objects. A wrong name lists the
  ones the file has.
- `--keep-lights`, `--keep-cameras`: a model's own lights and cameras are left out by default, with a
  warning, so they do not light or film your scene.
- `--pack` packs the model's images.

Name clashes never rename your things: the new ones get `.001`, listed under `renamed`. Animated or
constrained roots hang under a placement empty instead of being moved.

### Sizes and units

`--size`, `--at` and texture sizes are metres. In a scene with another unit scale (centimetres: Unit
Scale 0.01) they are converted, and the JSON gives both metres (`_m`) and Blender units (`_bu`).

### Cache, downloads and keys

- The cache is `BLENDER_AI_ASSETS_DIR`, by default `%LOCALAPPDATA%/blender-ai-assets/cache`,
  `~/Library/Caches/blender-ai-assets` or `~/.cache/blender-ai-assets`: outside any project. There is one
  folder per asset and variant, `<source>/<id>/<variant>/`, with its `manifest.json`.
- Every file is checked against the size and checksum the source publishes. An entry counts only once its
  manifest is written, so a cut-off download is never used. A second fetch makes no request at all.
- `--offline` uses the cache only, and says plainly when it cannot.
- `--max-mb 1` sets the size limit (default 500). A bigger download, or one larger than the free disk
  space, is refused before the first byte, and the message says how big it is. A server that sends too
  slowly is given up on.
- Archives are unpacked only when every member stays inside its folder. A downloaded glTF may only point
  at files inside its own folder.
- On Windows a cache path of 260 characters or more goes to Blender in its short 8.3 form, or is refused
  plainly. A shorter `BLENDER_AI_ASSETS_DIR` fixes it.
- Keys: Poly Haven needs none. Poly Pizza needs `POLYPIZZA_API_KEY` (free at
  <https://poly.pizza/settings/api>). Sketchfab needs `SKETCHFAB_API_TOKEN` (Sketchfab > Settings >
  Password & API). Without one the command stops with exit 1 and nothing is requested. Keys are never sent
  to file servers, or along a redirect to another host.
- Files are downloaded before the import is sent, so the bridge never waits on the network. The import is
  one bridge request; `--timeout 300` is how long this side waits (default 600).

### Credits

Every import adds one line to the local text datablock `ASSET_CREDITS`, and the same line to the JSON:

```
2026-10-22 20:56:52Z | polyhaven | rubber_duck_toy | Rubber Duck Toy | Plat251 | CC0 | https://polyhaven.com/a/rubber_duck_toy
```

Poly Haven is CC0. Poly Pizza is CC0 or CC-BY (credit required). Sketchfab models carry their own
licence, which is recorded. A local file is named by its file name and checksum, never by its folder.

### A downloaded .blend

It is appended, never opened. Every driver in it that would run Python is muted before anything is
evaluated (on objects, meshes, shape keys and node trees), even in a Blender that runs scripts. The muted
ones are listed under `scripted_drivers_muted`.

### Headless helpers

- `--dry-run` does the import and keeps nothing, to try it out:
  `assets.py import-file <this-skill-dir>/tests/fixtures/assets/files/fx_boxblend_1k.blend --new --dry-run`
- `--blender path/to/blender`, `--tmp-dir tmp/assets` (where the job and Blender's log go; removed
  afterwards).
- `BLENDER_AI_ASSETS_KEEP_WORK=1` keeps that job folder, to read Blender's log after a failure.

## Safety, and how it is checked

Both tools:

- never save, never open another file, never write beside your .blend;
- check `--expect-pid` / `--expect-file` before anything else;
- put back the frame, mode, selection, active object and every setting they touched;
- compare a fingerprint of the session before and after, and fail with exit 5 when it differs.

look.py's fingerprint covers render, display and colour settings, view layers and collections, every
object's visibility, selection, transforms and pose, animated values (actions and NLA), shape keys, the 3D
views (shading, overlays, view, local view), editors and tabs, the frame, mode and active object, the 3D
cursor and tool settings, the contents of cameras, lights, armatures, materials, worlds, meshes, images and
texts, custom properties, which datablock each name is, and the preferences' section and UI scale. The JSON
lists this under `fingerprint.covers`. It does not hash node groups, geometry-nodes modifier inputs,
constraints, particles, the sequencer or the compositor.

In a windowed Blender every tick is checked on its own. A change made between ticks, by the user or
another tool, is reported as `changed_meanwhile` and not blamed on the look. `file_marked_modified: true`
means the file now shows as unsaved: Blender does not let Python clear that flag.

assets.py refuses while the timeline is playing, and touches nothing. It then takes two fingerprints with
nothing in between; if they differ (a driver or handler that gives a new value each time it is asked) it
refuses too. The whole import runs in one bridge tick, so nothing else runs in Blender while it works.
After the import only the import's own changes may differ:
the new collection, world or material, the named objects' materials, and the `ASSET_CREDITS` text. A
failed import removes everything it made, and then nothing new may remain. The JSON's `scene_check` says
what was compared. In a windowed Blender the import is one undo step, checked in a window (rows G01 to
G03 below).

How the checks themselves were checked: two fingerprints with nothing between are equal, and each planted
change (a moved vertex, a moved cursor, a changed lens, a selection, a frame) is found. The test suites run
these rows, and each safety feature has a broken copy that its row must catch (see Tests).

## When it fails

Exit codes, the same for both: 0 ok, 1 failed, 2 bad arguments (nothing was done), 3 refused (nothing was
touched), 4 no Blender reachable, 5 Blender was left changed. Every failure also prints one stderr line,
`look: FAILED (code): reason` or `assets: FAILED (code): reason`.

- `no Blender bridge at localhost:9876` (look) or `no Blender bridge answers on` (assets), exit 4:
  start the extension's server, check `BLENDER_MCP_PORT`, or work headless with `--blend` / `--new`.
- `WRONG BLENDER, nothing was run` (exit 3): find the right one with `bmcp.py` and pass its pid.
- `viewport: this Blender runs in the background` (exit 3): add `--fallback camera`, or use
  `--mode camera`.
- `ui: ... no window to screenshot` (exit 3): serve the bridge from a windowed Blender, or check the
  panel's `draw()` headless with the blender-headless-qa skill.
- `the Render Result has nothing in it yet (render first)` (exit 3).
- `no object or collection named 'Nothing'` (exit 2): check the name. Collections work too.
- `--save-as is the input file` (exit 3): save somewhere else.
- `a headless import keeps nothing without --save-as PATH` (exit 2): add `--save-as`, or `--dry-run`.
- `... is 1.0 GB to download, over the 500.0 MB limit; pass --max-mb 1037` (exit 3): pick a smaller
  `--res`, or pass that number.
- `Sketchfab needs an API token: set SKETCHFAB_API_TOKEN` (exit 1): set it, or use Poly Haven.
- `'Shelf' has 2 material slots (0: Wood, 1: Paint); say which with --slot N` (exit 1).
- `the .blend has no collection or object named 'fx_box'; it has collections [...]` (exit 1): pick one
  of those.
- `no network: could not reach api.polyhaven.com` (exit 1): `--offline` works for what is cached.
- `the cached file ... is N characters long` (exit 1): set `BLENDER_AI_ASSETS_DIR` to a short folder.
- `the timeline is playing` (exit 3): stop playback (Esc) and import again.
- `the scene changes by itself between two looks` (exit 3): a driver or handler that changes the scene
  each time it is evaluated; mute it for the import.
- `THE SESSION WAS NOT PUT BACK AS IT WAS: ...` (look) or `BLENDER WAS LEFT CHANGED ...` (assets),
  exit 5: stop. The list says what is different. Tell the user, put it right with them, and treat it as a
  bug in the tool. Do not run it again to see.
- `no answer within 300 s ... its whole result is written to ..._result.json` (look): read that file
  once the job ends. A background bridge stays busy until then.
- `the Blender on ... did not answer within 600 s; the import may still be running there` (assets):
  look at the scene before trying again, or the import happens twice.
- `Blender exited (1) without a result; its log: ...` (look): read the log. `the --setup script failed:`
  gives the last line; the whole traceback is in `detail`.

Warnings worth acting on:

- `frame-change handlers were paused ...`: add `--run-handlers` if a handler is what you are testing.
- `N Python driver(s) did not run`: `--enable-autoexec`, for a file you trust.
- `X is too small to see ...; look at it alone with --target X`.
- `nothing visible to frame`, `every frame asked for ... is outside the scene range`: wrong targets or
  wrong frames.
- `an armature's bones are drawn only with --shading xray (or --overlays)`.
- `--expect-file 'demo.blend' was matched by file name only`: give the full path.
- `render fallback: ...`: the picture is a render, not the viewport; the warning says what differs.
- `the file brought camera 'GltfCam', light 'L_POINT' ...; left out`: `--keep-lights` / `--keep-cameras`.

## Two ways to work

Building a scene: headless first, then the user's Blender.

```
look.py --new --setup build_demo.py --mode angles --save-as demo.blend
assets.py import polyhaven kloppenheim_06_puresky --blend demo.blend --save-as lit.blend
assets.py import polyhaven rock_wall_08 --apply-to Floor --blend lit.blend --save-as floor.blend
assets.py import polyhaven rubber_duck_toy --size 0.3 --at 1,0,0 --blend floor.blend --save-as duck.blend
look.py --blend duck.blend --mode camera --shading rendered --out shots/check.png
```

Look at every picture before saying it is right. A ground that ends inside the camera's view, or a model
too small to see, shows only in the picture.

Developing an add-on: load it in `--setup`, run its operator, look at the result.

```python
# try_addon.py, beside the add-on's source ring_addon.py
import os, sys
import bpy
sys.dont_write_bytecode = True                 # no __pycache__ beside your add-on
sys.path.insert(0, os.path.dirname(__file__))
import ring_addon
ring_addon.register()
bpy.ops.object.ring_of_cubes(count=12)
```

```
look.py --new --setup try_addon.py --mode angles --views top,three_quarter
```

Use `--mode frames` when the add-on animates something. In the user's Blender, reload the add-on the safe
way (sessions-and-reload.md), then `look.py --expect-pid N --mode ui --ui sidebar:MyTab` for its panel.

## Tests

Run them from the skill folder; they write only into the `--work` folder. Both start background Blenders
one at a time, only with 6000 MB of memory free. Keep the work folder short on Windows. Every row is
first run on a control with a known answer.

look.py, `tests/test_look.py`:

```
python tests/test_look.py --work DIR                                            # headless
python tests/test_look.py --work DIR --bridge-port 9900 --bridge-pkg DIR/mcp    # and the real bridge
python tests/test_look.py --work DIR --only cli,refusals,paths                  # some groups
python tests/test_look.py --work DIR --mutate --mutations 20                    # break a copy, see its row fail
```

- Groups: instruments, framing, views_differ, shading, motion, refusals, hidden, image_mode,
  render_backend, save_as, cli, paths, mend, harness, bridge.
- The bridge rows use the port given and the next one. `--bridge-pkg` is the unzipped add-on folder. Its
  name is imported as a package, so keep it a plain name like `mcp`.
- `--mutate` runs 23 broken copies of the tool, such as "frame not put back" and "handlers not paused",
  each against the rows aimed at it. `--mutations 20` picks some by number.
- `--scripts DIR` tests another copy of the scripts. `--blender EXE`, `--min-free-mb 6000`.
- It prints PASS or FAIL per row and ends with `N of N rows passed`. The rows go to `DIR/test_rows.json`.

assets.py, `tests/test_assets.py`:

```
python -X utf8 tests/test_assets.py --work DIR                                   # no network: a local fixture server
python -X utf8 tests/test_assets.py --work DIR --online                          # and the real Poly Haven
python -X utf8 tests/test_assets.py --work DIR --bridge-zip mcp-1.0.3.zip --bridge-port 9900   # and the LIVE rows
python -X utf8 tests/test_assets.py --work DIR --only R11,R19
python -X utf8 tests/test_assets.py --work DIR --mutants --only R11
```

- Rows R01 to R36 need no network. They use recorded answers and tiny files from `tests/fixtures/assets`
  (rebuilt by `make_fixtures.py` and `make_attack_fixtures.py`). O01 to O04 use the real Poly Haven (O04
  the bridge too).
- R18 is the LIVE row. Without `--bridge-zip` and `--bridge-port` it shows as FAIL, "skipped".
- `--bridge-zip` must be the 1.0.3 release (its sha256 is checked). That Blender runs with script
  auto-run on, the worst case for a downloaded .blend.
- `--mutants` plants one defect per row in a scratch copy; each row must then fail.
- It ends with a JSON line of counts. The details go to `DIR/summary.json`.

Last full runs (2026-10-22): test_look 168 of 168 rows, headless and bridge, and 23 of 23 broken copies
caught. test_assets 40 of 40 rows (321 checks, online and bridge included), and 53 of 53 planted defects
caught. After the windowed checks' fixes, the same evening: test_look headless all rows, and its bridge
group 19 of 19; test_assets 36 of 36 rows (301 checks) with a bridge, and R18 and R20 catch their 3
planted defects. Not tested: real Poly Pizza and Sketchfab downloads (no key), macOS and Linux, Blender
before 5.2.

## The windowed checks

A background Blender has no window, so the viewport and ui modes, the deferred path and an import's undo
step are checked by scripts run in a windowed Blender with a sandboxed profile, on a spare port. Both ran
on 2026-10-22 (Blender 5.2.2, Windows) and pass: look_gui_check 36 of 36 rows, test_assets G01 to G04 22
of 22 checks. The first windowed runs, with the breakers' own windowed attacks, caught what no background
Blender can show:
- a sidebar never opened before is left remembering a tab (now a note, not a failure);
- an import while the timeline played went through: the "two looks" check cannot see playback inside one
  bridge tick (now refused by asking Blender, row G04);
- the Local View warning was said even when the new objects were in that view (now it names exactly those
  outside it);
- two test faults: the G7 test clicked before its job had started, and the driver trap's expression passed
  Blender's 255-character limit in a deep work folder, so its control could never fire.
Both start-up scripts turn the splash screen off, since it would otherwise be in the screenshots.

**look.py: `tests/look_gui_check.py`.** In Git Bash:

```
SANDBOX=<an empty folder>; mkdir -p "$SANDBOX/tmp"
BLENDER_USER_RESOURCES="$SANDBOX" TEMP="$SANDBOX/tmp" TMP="$SANDBOX/tmp" \
  blender --factory-startup -p 60 60 1500 950 --python tests/look_gui_check.py -- WORK_DIR PORT BRIDGE_PACKAGE_DIR
```

It builds the test scene, serves the bridge, runs look.py cases G1 to G13 the way an AI would, prints
`GUI| PASS` or `GUI| FAIL` per row, writes `WORK_DIR/gui_rows.json` and quits. Add `--keep-open` after the
folders to look around afterwards. What it must show:

- every row PASS: the viewport (G1, G2), deferred angles and frames (G3, G4, G5), a rendered camera (G6),
  a selection made during a job reported and not blamed (G7), ui shots put back (G8), refusals (G9), the
  Render Result (G10), local view (G11), `--isolate` keeping the selection (G12), and the session's
  frame-change handler not run and still registered (G13);
- the window flickering for the ui cases and coming back as it was each time;
- `viewport.png` the front view with the red cube; `sidebar.png` the Item tab at its real width;
  `properties.png` Green's Material tab; `preferences.png` the Cycles add-on expanded; `window.png` the
  whole window; `localview.png` the green sphere, although the 3D view was in local view with only the red
  cube.

**assets.py: rows G01 to G04 of `tests/test_assets.py`.**

1. `python -X utf8 tests/test_assets.py --work DIR --write-gui-boot --bridge-zip mcp-1.0.3.zip --gui-port 9901`
   writes the scene, the boot script and the bridge package, and prints the next two commands (its
   `set` lines are cmd.exe syntax).
2. Start the windowed Blender it printed, with `BLENDER_USER_CONFIG` and `BLENDER_USER_SCRIPTS` pointed
   at its empty profile. It prints `BRIDGE_UP <port> <pid>`.
3. `python -X utf8 tests/test_assets.py --work DIR --only G01,G02,G03,G04 --gui-port 9901 --gui-pid <pid>`

What it must show:

- G01: a model imported while Floor is in Edit Mode comes back to Edit Mode with the unsaved edit kept,
  the 3D view does not move, and one undo step is recorded.
- G02: one undo through the bridge takes the HDRI's world away; redo brings it back.
- G03: a texture put on Floor in Edit Mode keeps Edit Mode and the edit, records an undo step and leaves
  the view where it was; for a model imported while a 3D view is in Local View, the warning names exactly
  the new objects Blender says are outside it (whether they join depends on the importer: the test box
  does not, a glTF duck did), and `gui_local_view.png` shows Floor alone.
- G04: an import while the timeline plays is refused (exit 3, "the timeline is playing") and touches
  nothing.

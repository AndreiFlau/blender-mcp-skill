---
name: blender-mcp
description: Execute Python inside a live Blender session via the official Blender Labs MCP extension (TCP localhost:9876) — for building/testing Blender addons, inspecting scenes, running operators, and render-based visual verification. Use when working on Blender addons, when the user mentions Blender MCP, or when a task needs a running Blender instance.
---

# Blender MCP bridge (official Blender Labs extension)

Talk to a **running Blender session** by executing Python on its main thread. The
bridge is the official "MCP" extension by Blender Lab, source
<https://projects.blender.org/lab/blender_mcp>, installed from Blender's
`lab.blender.org` extension repository (not extensions.blender.org), package id
`lab_blender_org/mcp`. No MCP server registration in Codex is needed —
speak to the extension's TCP socket directly with the bundled client. (Each
release also ships a `.mcpb` MCP-server bundle for chat apps; this skill does not
use it.)

**Keep it at 1.0.3 or later.** 1.0.0 to 1.0.2 send replies with a non-blocking
`sendall`, so a large reply (a Hi-DPI screenshot, a big `result` dict) can arrive
cut short and fail as truncated JSON ("Unterminated string"); 1.0.3 queues the
rest. The `lab.blender.org` repo has "sync on startup" off, so Blender only
learns of a new version when its listing is refreshed (Preferences → Get
Extensions shows an Update button once it is): check there, or read
`extensions/lab_blender_org/.blender_ext/index.json` against the installed
`blender_manifest.toml`. Updating reloads the add-on and drops the socket: never
update it from inside a bmcp request.

**This is NOT the community `ahujasid/blender-mcp` protocol.** Requests there use
`{"type": "get_scene_info"|"execute_code", "params": {...}}` with no terminator;
the official extension will answer those with `Client timed out`. If you see that
error, you sent the wrong protocol or forgot the null terminator.

## Quick start

```
python <this-skill-dir>/scripts/bmcp.py "print(bpy.context.scene.name, bpy.data.filepath)"
python <this-skill-dir>/scripts/bmcp.py -f myscript.py          # run a script file
python <this-skill-dir>/scripts/bmcp.py -t 600 -f render.py     # long timeout
```

Host/port override via env vars `BLENDER_MCP_HOST` / `BLENDER_MCP_PORT` (default
`localhost:9876`).

First call after idle can lag ~1 s (the server polls its socket on a Blender timer
that backs off to a 1 s interval when idle); subsequent calls are fast (0.05–0.25 s
active tick).

Before mutation, read the recipient's PID, filepath, scene and expected object,
then assert that identity inside the mutating script. Multiple GUI instances can
share a nominal port setup. Coordinate live-app/render ownership with other agents;
see [sessions-and-reload.md](references/sessions-and-reload.md).

`python <this-skill-dir>/scripts/bmcp.py --help` is local-only; it never connects.

## Protocol (if writing your own client)

Null-byte-delimited JSON over TCP:

- Request: `{"type": "execute", "code": "<python>", "strict_json": false}` UTF-8
  encoded **+ a trailing `b"\0"`**. Omitting the `\0` hangs the request; the server
  evicts you after ~10 s with `{"status": "error", "message": "Client timed out"}`.
- Response: JSON + `b"\0"`:
  `{"status": "ok"|"error", "result": {...} | "message": "<traceback>", "stdout"?: str, "stderr"?: str}`.
- One request per connection; the server closes it after responding.

## Writing code for the bridge

- The code string runs via `exec()` in a **fresh namespace** — `import bpy` yourself
  (the bundled client prepends it for you).
- To return structured data, assign a **dict** to a variable named `result`
  (anything else errors: a list is refused with "The `result` variable must be a
  dict, not list. Wrap your return value: `result = {"key": value}`"). With
  `strict_json: false`, non-serializable values are `repr()`-ed. `print()` output
  comes back in `"stdout"`.
- Exceptions return `status: "error"` with the full traceback — read it, don't guess.
- A weak sandbox blocks: `sys.exit()`, `bpy.ops.wm.quit_blender`,
  `bpy.ops.wm.read_factory_settings`, `read_factory_userpref`, `read_userpref`.
- Code runs from a **timer context**: some operators need a context override —
  `with bpy.context.temp_override(window=..., area=...)`. Find areas via
  `bpy.context.window_manager.windows[0].screen.areas`.
- **`temp_override(active_object=X)` does not redirect `bpy.ops.object.mode_set`**
  (Blender 5.3, live GUI, 2026-11-20). Asked to put an armature in Edit Mode, it
  toggled the mesh the user had just clicked instead; the follow-up
  `mode_set('OBJECT')` then saw the overridden armature already in Object Mode and
  did nothing, leaving the user's mesh stuck in Edit Mode and `edit_bones` empty
  (so the edit loop silently ran zero times). Make X the real active object:
  save `view_layer.objects.active` and the selection, set `objects.active = X`,
  `mode_set`, do the work, `mode_set('OBJECT')`, then restore both. Assert
  `X.mode == 'EDIT'` and `len(X.data.edit_bones) > 0` before editing.
- **Long jobs (renders)**: define a callable `check_is_finished` in your code that
  returns `None` while running and a response dict when done — the server holds the
  connection open (up to 1 h) and polls it on its timer. Keep the checker cheap
  (flag/file-existence check). Simpler alternative for most cases: run the job
  synchronously and give the client a large `-t` timeout.

## Addon development loop

1. **Inspect first**: scene/object/material state via `result = {...}` queries.
2. **Reload the active package safely**: identify the installed namespace and
   source path; snapshot user state, unregister the active module, then purge only
   that exact namespace and its children before importing/registering tested code.
   Follow [sessions-and-reload.md](references/sessions-and-reload.md), including
   stale test-package aliases and callback identity checks. Do not blindly call
   `register()` on an already registered or second short-name copy.
3. **Exercise operators** (`bpy.ops.myaddon.thing()`), then re-inspect state.
4. **Verify visually**: render to a file, then view it with the Read tool:
   ```python
   sc = bpy.context.scene
   sc.render.filepath = r"<scratchpad>\check.png"
   sc.render.image_settings.file_format = 'PNG'
   bpy.ops.render.render(write_still=True)
   ```
   A viewport grab is cheaper when shading setup doesn't matter:
   `bpy.ops.screen.screenshot(filepath=...)` (needs a window context override).

## Safety with the user's live session

The code mutates **the file the user has open**. Prefer read-only inspection;
before destructive experiments either work on a copy headlessly (see below), ask,
or make sure the change is undoable (`bpy.ops.ed.undo_push(message=...)` before
mutating helps). **Never** save (`bpy.ops.wm.save_mainfile`) or open another file
in the live session unless the user asked.

**A "read-only" census can still hurt the session: reading `Image.size` loads the
picture** (2026-11-21). A walk over `bpy.data.images` reading `size` (to total up
texture memory) froze the user's Blender for 37 s and loaded all 2,146 pictures:
7.3 GB became 19.2 GB, with 4.3 GB of RAM left free. `has_data` reads `True` for
every picture afterwards, so it cannot tell you what was loaded before. The same
goes for `pixels`, and for anything that evaluates every object (`evaluated_get`,
`to_mesh`) on a big scene.
- In the live session read only plain properties: names, counts, `users`,
  `packed_file.size`, `filepath`, transforms. Time the snippet on a slice first
  (`[:50]`) and scale it up in your head before running it over everything.
- For picture sizes, do the census on a background copy of the saved file, and
  call `image.buffers_free()` right after each `size` read so memory stays flat.
- If it has already happened: `image.buffers_free()` on every picture with
  `has_data`, skipping those with `is_dirty` (unsaved paint) and any whose
  `source` is not `FILE`, `SEQUENCE` or `TILED`. Blender reloads them on demand;
  the file is not marked changed. It took 1 s and brought memory back.

## If the server isn't running

- Check: is anything listening? `Get-NetTCPConnection -LocalPort 9876 -State Listen`
  (PowerShell) — the owning process should be `blender`.
- The extension autostarts its server ~1 s after Blender launch (preference
  "Auto Start", on by default) but **requires "Allow Online Access"** in
  Preferences → System. Manual start/stop lives in Preferences → Add-ons → MCP →
  the extension's preferences panel ("Start MCP Bridge Server").
- Headless/background mode (timers don't run there — the operator refuses):
  ```
  blender --background --online-mode file.blend --command blender_mcp
  ```
  This serves the same protocol synchronously (no deferred responses).
- If the user hasn't installed it: Preferences → Get Extensions → search "MCP"
  (it's in the `lab.blender.org` repository, which has to be added first if it is
  not listed) → install + enable, then enable Online Access.

## Live posing and interaction diagnostics

- Find the installed extension's actual module name before inspecting or
  reloading it: e.g. `bl_ext.user_default.cascadeur_live_posing`, not necessarily
  `cascadeur_live_posing`. Importing a second copy can inspect an idle module
  while the installed copy keeps its timers and state. Unregister the active
  copy and release its callbacks before purging modules.
- For jerky navigation, modal drags or live-pose latency, read
  [performance.md](references/performance.md). It includes the verified
  BlenderKit clipboard-stall case and how to distinguish callback blocking from
  rig evaluation cost.
- For transferring poses onto existing rigs, recording them and preserving
  native controls, read [live-rigging.md](references/live-rigging.md).

## Seeing the scene, and bringing assets in

Two tools beside `bmcp.py`, for scene building and add-on work alike. Each prints one JSON
line (read its `warnings`), runs LIVE through the bridge or HEADLESS (`--blend FILE`,
`--new`), never saves, and checks with a fingerprint that the session is put back (exit 5).

- `scripts/look.py`: the scene as pictures, a labelled sheet plus each view. Use it whenever
  numbers cannot say a result looks right: a model, a material, a rig, an animation.
- `scripts/assets.py`: HDRIs, textures and models from Poly Haven, Poly Pizza, Sketchfab or a
  file, put in the scene at real size and credited in the `ASSET_CREDITS` text.

```
python <this-skill-dir>/scripts/look.py --expect-pid N --mode angles --target Chair
python <this-skill-dir>/scripts/look.py --blend scene.blend --mode frames --frame-count 8 --view front --target Rig --center-bones hips
python <this-skill-dir>/scripts/assets.py search --source polyhaven --type texture rock wall --limit 5
python <this-skill-dir>/scripts/assets.py import polyhaven rock_wall_08 --apply-to Floor --expect-pid N
```

Live, always pass `--expect-pid N` from a `bmcp.py` identity query. `--mode viewport` and
`--mode ui` need a windowed Blender. A look pauses frame-change handlers (`--run-handlers`
lets an add-on's run). A headless import needs `--save-as`. Tiling is set at import from the
object's size: size it first. All modes and options, safety checks, failures, a bridge on a
spare port and the tests: [look-and-assets.md](references/look-and-assets.md).

Status (2026-10-22): attacked by two breakers and a first-time user, then mended; self-tests
pass headless, through a 1.0.3 bridge and in a windowed Blender (`tests/look_gui_check.py`,
`tests/test_assets.py --only G01,G02,G03,G04`). In a window a look pauses Blender for each
tick (0.4-0.7 s on 3000 objects). Adapted code from MCP for Blender (MIT):
`THIRD_PARTY_NOTICES.md`.

## Focused workflows

- Pictures of the scene, and assets brought into it (look.py, assets.py):
  [look-and-assets.md](references/look-and-assets.md).
- Paint canvas, shader layers, dirty image packing and UV/API traps:
  [painting-and-materials.md](references/painting-and-materials.md).
- Native render thread boundaries, ID retirement, demand-driven caches and 5.2
  Geometry Nodes inputs: [render-and-cache-safety.md](references/render-and-cache-safety.md).
- Production render comparisons, evaluated snapshots and fresh-process save/reload
  proof: use the **blender-headless-qa** skill and its production-fixtures reference.

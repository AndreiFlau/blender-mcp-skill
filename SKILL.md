---
name: blender-mcp
description: Execute Python inside a live Blender session via the official Blender Labs MCP extension (TCP localhost:9876) — for building/testing Blender addons, inspecting scenes, running operators, and render-based visual verification. Use when working on Blender addons, when the user mentions Blender MCP, or when a task needs a running Blender instance.
---

# Blender MCP bridge (official Blender Labs extension)

Talk to a **running Blender session** by executing Python on its main thread. The
bridge is the official "MCP" extension from extensions.blender.org (Labs), package
id `lab_blender_org/mcp`. No MCP server registration in Claude Code is needed —
speak to the extension's TCP socket directly with the bundled client.

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
  (anything else errors). With `strict_json: false`, non-serializable values are
  `repr()`-ed. `print()` output comes back in `"stdout"`.
- Exceptions return `status: "error"` with the full traceback — read it, don't guess.
- A weak sandbox blocks: `sys.exit()`, `bpy.ops.wm.quit_blender`,
  `bpy.ops.wm.read_factory_settings`, `read_factory_userpref`, `read_userpref`.
- Code runs from a **timer context**: some operators need a context override —
  `with bpy.context.temp_override(window=..., area=...)`. Find areas via
  `bpy.context.window_manager.windows[0].screen.areas`.
- **Long jobs (renders)**: define a callable `check_is_finished` in your code that
  returns `None` while running and a response dict when done — the server holds the
  connection open (up to 1 h) and polls it on its timer. Keep the checker cheap
  (flag/file-existence check). Simpler alternative for most cases: run the job
  synchronously and give the client a large `-t` timeout.

## Addon development loop

1. **Inspect first**: scene/object/material state via `result = {...}` queries.
2. **Load the addon from the repo** (no reinstall per edit):
   ```python
   import sys, importlib
   sys.path.insert(0, r"<repo dir containing the addon package>")
   import myaddon
   importlib.reload(myaddon)   # plus reload submodules, or bump a loader script
   myaddon.register()
   ```
   For a full clean reload of a multi-file addon, purge `sys.modules` entries
   first: `[sys.modules.pop(k) for k in list(sys.modules) if k.startswith("myaddon")]`.
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
  (it's a Labs extension) → install + enable, then enable Online Access.

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

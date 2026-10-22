# blender-mcp — Claude Code skill

A [Claude Code](https://claude.com/claude-code) skill that lets Claude execute
Python inside a **running Blender session** through the official
[Blender MCP extension](https://projects.blender.org/lab/blender_mcp) (Blender Lab,
package `lab_blender_org/mcp`) — no MCP server registration required, the
skill talks to the extension's TCP bridge directly.

Useful for building and debugging Blender addons interactively: inspect the
scene, hot-reload an addon from its repo, run its operators, and verify
results with renders — all in the file you have open.

## Install

Clone into your user-level Claude Code skills directory:

```
git clone https://github.com/AndreiFlau/blender-mcp-skill "%USERPROFILE%\.claude\skills\blender-mcp"
```

(macOS/Linux: `~/.claude/skills/blender-mcp`.)

## Requirements

- Blender 5.x with the official **MCP** extension, version 1.0.3 or later
  (Preferences → Get Extensions → search "MCP"; it is in the `lab.blender.org`
  repository, not extensions.blender.org) enabled, plus
  **Allow Online Access** in Preferences → System.
- The extension auto-starts its bridge server on `localhost:9876`.
- The scripts need Python 3.10 or later and nothing else: standard library only, nothing to
  install. The parts that run inside Blender use bpy and numpy, which ship with Blender.
- look.py's headless pictures are Blender's own viewport drawing from Blender 5.2 on; older
  versions get renders instead.
- assets.py: Poly Haven needs no key; Poly Pizza needs `POLYPIZZA_API_KEY` and Sketchfab
  `SKETCHFAB_API_TOKEN`.

## What's inside

- `SKILL.md` — protocol documentation and workflow instructions Claude loads
  when the skill activates (wire protocol, addon hot-reload loop, render
  verification, live-session safety rules, background/headless mode).
- `scripts/bmcp.py` — a dependency-free Python client for the bridge, also
  usable standalone:

  ```
  python scripts/bmcp.py "print(bpy.context.scene.name)"
  python scripts/bmcp.py -f myscript.py
  python scripts/bmcp.py -t 600 -f render_job.py
  ```

- `scripts/look.py` — the scene as pictures (angles, camera, frames, viewport, image, an add-on's UI), live or headless.
- `scripts/look_blender.py` — the part of look.py that runs inside Blender.
- `scripts/assets.py` — search, fetch and import Poly Haven, Poly Pizza, Sketchfab or local assets at real size, with credits.
- `scripts/assets_blender.py` — the part of assets.py that runs inside Blender.
- `references/look-and-assets.md` — how to use both tools: every option, the safety checks, failures, tests.
- `tests/test_look.py` — look.py's self-tests, headless and through a bridge on a spare port.
- `tests/look_gui_check.py` — look.py's check in a windowed Blender (viewport, ui, deferred ticks).
- `tests/test_assets.py` — assets.py's self-tests: a fixture server, real Poly Haven, a bridge, windowed rows.
- `tests/fixtures/assets/` — recorded API answers and tiny files for those tests, rebuilt by `make_fixtures.py`.
- `THIRD_PARTY_NOTICES.md` — portions adapted from MCP for Blender (MIT).

## License

GPL-3.0-or-later (matches the Blender extension it interfaces with).

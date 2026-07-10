# blender-mcp — Claude Code skill

A [Claude Code](https://claude.com/claude-code) skill that lets Claude execute
Python inside a **running Blender session** through the official
[Blender MCP extension](https://extensions.blender.org/) (Blender Labs,
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

- Blender 5.x with the official **MCP** extension (Preferences → Get
  Extensions → search "MCP", a Blender Labs extension) enabled, plus
  **Allow Online Access** in Preferences → System.
- The extension auto-starts its bridge server on `localhost:9876`.

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

## License

GPL-3.0-or-later (matches the Blender extension it interfaces with).

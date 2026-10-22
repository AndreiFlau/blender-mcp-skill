# Third-party notices

## MCP for Blender

Portions of these files are adapted from MCP for Blender
(<https://github.com/ahujasid/mcp-for-blender>, commit
`25e1fe008fc523b1a947c6f3d28510a362a0f06e`, 2026-10-18), Copyright (c) 2025 Siddharth Ahuja,
MIT License:

- `scripts/look.py`, `scripts/look_blender.py`: the look tool's modes and shading names, drawing the
  viewport offscreen with computed view and projection matrices, the table of view angles, the camera
  matrix, saving and restoring settings, and the wireframe and X-ray recipes.
- `scripts/assets.py`, `scripts/assets_blender.py`: the Poly Haven, Poly Pizza and Sketchfab API use
  (endpoints, filters, ranking, error messages, the checked download, thumbnail addresses), the texture
  map table and colour spaces, and real-world sizes from the asset record.

Each of these four files says so in its first lines. No other file in this skill holds their code; the
tests and their fixtures were written for this skill. Nothing of its telemetry, trajectory capture,
consent prompts or premium hints was taken.

The rest of this skill is GPL-3.0-or-later; MIT-licensed code may be combined with it. The notice
follows in full, as it stands in `LICENSE` at that commit.

```
MIT License

Copyright (c) 2025 Siddharth Ahuja

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

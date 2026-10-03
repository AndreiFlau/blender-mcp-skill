# Painting, material layers and image persistence

Observed in Futafy 0.5.16–0.5.18 on Blender 5.2.2 LTS, Windows. Inspect RNA when
supporting other versions; do not generalize these API details to every release.

## Diagnose the layer, not just the brush color

Capture the actual edited/pinned object, `image_paint.canvas`, active/render UVs,
shading mode, layer toggles, image pixels and shader values. White on a factor mask
reveals the downstream color layer; existing pale color-detail strokes can therefore
look like painting white even when the clean-skin picker is brown. Independently
ablate mask, detail and base on a copy before blaming alpha or color management.
In the verified case an independent detail-layer toggle fixed the appearance;
changing STRAIGHT to CHANNEL_PACKED alpha did not establish an alpha-mode cause.

Show the active canvas in the UI. Give detail paint independent visibility and make
the color-paint action enable that layer. Preserve artist pixels when disabling a
layer. Inspect existing graph-discovery contracts before naming new helper groups:
legacy prefix-based discovery can accidentally classify an auxiliary group as the
main body shader. Construct upstream closures before rewiring; node creation order
is not topological order. Flat-graph tests do not establish arbitrary nested-group
support.

## Dirty pixels and ownership

Do not assume `Image.copy()` captures current unsaved pixels or makes a private
filepath. For an isolated paint copy, create an owned image, explicitly transfer
`pixels.foreach_get/foreach_set`, and verify the current buffers and ownership.
An older packed image can coexist with `is_dirty=True`. Pack current owned paint
before saving and before `bpy.data.libraries.write`, which does not invoke ordinary
save handlers in the tested export path. Never overwrite or repack user source
images merely to prepare a diagnostic. Export copies/current pixel arrays instead.

Test actual save/reload with current baseline textures too; stale fixture pixels
can falsely implicate the addon. An 8-bit packed round-trip can map 0.7 to
0.7019608: establish the format's error on a control, use a justified tolerance,
and retain exact comparisons for untouched raw buffers. See the QA skill's
production-fixtures reference for fresh-process proof.

## Paint and UV API traps

- In the inspected 5.2 build, unified paint settings live at
  `scene.tool_settings.image_paint.unified_paint_settings`; older versions may use
  `tool_settings.unified_paint_settings`. Inspect/fallback by capability.
- Image Editor pinning is `use_image_pin`. Inspect RNA instead of guessing `pin`.
- UV boolean collections such as `layer.pin` may have length zero until allocated,
  despite thousands of UV loops. Treat missing flags as false when supported and
  size `foreach_get` buffers to the actual collection. Old `MeshUVLoop.select` and
  `select_edge` access is not supported in the inspected 5.2 API.
- The eight-map UV limit needs an explicit fallback, not arbitrary map deletion.
  Futafy archives a proven unreferenced map to private CORNER attributes and checks
  topology/order, data and flags on restoration. Unknown Geometry Nodes, dynamic
  or external consumers invalidate that proof; refuse rather than assume unused.
- Prepare atlas UVs on a temporary mesh, keep original active/render UVs, and restore
  selection/mode. Never paint the original body texture as a side effect of setup.

Evidence: Futafy commits e8e26a5 (0.5.16), 6780c0b (0.5.17), ca89be8 (0.5.18);
`futafy/color.py`, `tests/test_color_performance.py`, and saved local 0.5.16–0.5.18
validation reports. User confirmation supports the detail-layer diagnosis; it is
not a claim that every white paint artifact has that cause.

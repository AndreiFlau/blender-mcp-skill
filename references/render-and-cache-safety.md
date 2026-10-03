# Render callbacks, private caches and Blender 5.2 inputs

Evidence: Fluid Painter Evolved 0.9.6, commit 752ea13, Blender 5.2.2 on Windows.
References below are relative to that project, not bundled with this skill.

## Native rendering has a different callback path

Synchronous `bpy.ops.render.render()` passing does not validate native interactive
rendering. Traces of `render('INVOKE_DEFAULT', animation=True)` found `render_pre`
on a worker thread. Do not mutate Blender IDs or call `view_layer.update()` there.
The tested design registers a main-thread pump before rendering; worker handlers
enqueue preparation and wait. Completion/cancellation defers cleanup until the
native job ends. Cancel timed-out work that has not started so it cannot execute
late; do not abandon preparation already executing. Guard low-level evaluation
against unexpected worker callers. Do not introduce a new Python worker to touch
Blender data. Lock Interface alone does not enforce this boundary.

A two-frame native GPU test checked thread IDs, per-frame masks against independent
references, paint preservation and cleanup after `native_job=false`. An original
production crash stack was consistent with this race, but was not deliberately
reproduced: its causal attribution remains a supported hypothesis. Successful image
output alone is insufficient; a handler can fail while Blender renders stale data.
See `docs/render_safety_validation.md`, `FP_RenderThread.py`,
`tests/live_render_thread.py`, `tests/test_render_dispatch.py`.

## Detach, retire, then free

Development experiments crashed when deleting replaced mesh IDs during dependency
observation or proxy IDs during render initialization. Detach consumers to native
data first; retire replaced IDs after observers return, keep them alive throughout
the render, and free only after the native job ends and ownership is rechecked.
Preserve fake users and externally adopted data. Names plus pointer identity checks
can prevent deleting a different ID that reused a name, but never dereference stale
RNA after undo/deletion. An arbitrary timer is not proof that deletion is safe.

Low-level library export may bypass save hooks: explicitly suspend private cache
references around `bpy.data.libraries.write`. See `FP_SurfaceCache.py` and
`docs/contact_publication_validation.md`.

## Cache only for actual supported consumers

Configured targets and source visibility do not establish demand. An unlinked
managed helper caused needless deformation copies even after hidden outputs were
filtered. Check supported outgoing consumers and visible outputs; hidden inputs
can feed visible joins, and a shared binding remains needed by any active consumer.
On disconnect/hide mark retained data unavailable; refresh from current evaluated
data before reconnecting. Unsupported readers use native data. Reuse geometry on
color-only edits only if the consumer contract excludes color attributes; topology,
coordinates, split normals and transforms still invalidate it. Per-area Local View
and simultaneous-window combinations were not covered by these tests.
See `tests/live_surface_cache_unused.py`, `tests/live_surface_cache_visibility.py`.

## Geometry Nodes modifier inputs in 5.2

In the inspected build, ID-property reads missed defaults after undo and dictionary
writes bypassed normal notifications. Use stable socket identifiers and inspect:
`modifier.path_resolve('properties.inputs.' + identifier).value` for values,
`properties.inputs` for presence, `layout.prop(resolved_input, 'value')` for UI,
and `properties.outputs.<identifier>.attribute_name` for named outputs. Retain a
version/capability-gated older ID-property path. Validate undo/redo as separate UI
events, use an ordinary custom property as control, and reacquire IDs after undo.
See `FP_Utilities.py`, `docs/interaction_validation.md`, `tests/live_surface_patch.py`.

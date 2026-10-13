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

## A view layer of your own, and Blender's compositor preview (5.2, read in its source)

Paid for on 2026-10-13 with a crash in the user's face. An add-on kept a hidden
view layer in the user's scene to evaluate rigs cheaply, and removed it when it
was done.

- After ANY evaluation of ANY view layer of a scene on the main thread
  (`ViewLayer.update()`, `scene.frame_set()`, which evaluates every layer of the
  scene in list order), Blender calls `update_compositor`
  (`editors/render/render_update.cc`). If the scene has a compositor tree whose
  output is tagged (a frame change on an animated tree, a first evaluation, a
  node edit) and an editor shows its result (a node editor backdrop, an Image
  Editor on the Viewer or the Render Result), it starts a compositor job FOR
  THAT LAYER. The job stores the `ViewLayer*`
  (`editors/space_node/node_compositor_job.cc`).
- While a compositor job runs, the next is queued (`WM_jobs_start`) and its
  `compositor_job_init` reads the pointer when the running one ends. A job that
  may not run beside a render or a bake waits for that
  (`wm_jobs_test_suspend_stop`, every job with `WM_JOB_EXCL_RENDER`).
- `scene.view_layers.remove(layer)` in between is a use after free:
  `EXCEPTION_ACCESS_VIOLATION` in `layer_collection_sync` under
  `BKE_view_layer_synced_ensure` under `compositor_job_init`. Blender alone does
  it: `scene.frame_set(n); scene.view_layers.remove(extra)` in one script turn,
  animated compositor tree, backdrop on.
- **Whether such a job waits cannot be asked from Python.**
  `bpy.app.is_job_running` is `WM_jobs_has_running_type`: it knows jobs that
  RUN. A compositor preview suspended behind a render, a bake or a preview
  render has `running` false and holds the pointer, it starts up to 0.1 s after
  what it waited for ends (Python timers run in that gap), and light bakes and
  multires bakes cannot be asked about at all. A first mend that asked
  ("free when nothing runs, else retry from a timer") was killed four ways by
  an independent reviewer; do not write that mend again.
- The only moments a view layer of your own may be freed: in a handler of
  `undo_pre`/`redo_pre`/`undo_post`/`redo_post` and `load_post` (Blender calls
  `WM_jobs_kill_all` in `ed_undo_step_pre` and in
  `wm_file_read_setup_wm_init` first), and even there only if none of
  COMPOSITE, RENDER, RENDER_PREVIEW, OBJECT_BAKE runs: another add-on's handler
  ahead of yours can start a preview render and set the frame, and then a
  compositor preview waits for your layer again. NOT on `save_pre`, not at the
  end of your own job, not from a timer. In background mode
  (`bpy.app.background`) no compositor preview exists and freeing is safe.
- While the user works, leave the layer in the scene. Do NOT reuse a layer you
  stopped using for a new evaluation: measured, a rig followed in a layer that
  was left and taken up again came out 4.6 cm and 13 degrees from the same
  follow in a new layer (the take's keys are played over what was just
  written). Make a new one beside it and free both at the next undo or load.
- A view layer with `use = False` is Blender's "default view layer"
  (`BKE_view_layer_default_view`: the first one not used for rendering): when
  the user deletes the layer a window shows, the window lands on yours. Put the
  window back on one of the scene's own.
- Have an independent reviewer attack a crash mend with the same instrument
  before believing it. Twice the mend "passed everything" and twice it died.

## A crash that comes "sometimes" is often a use after free: make it come always

Freed memory still reads fine until something else is put there, so the same
bug survived 10 runs of 13. `blender --debug-memory` fills freed memory with
255 and the bug died every time, which turned guessing into a control (the old
code dies) and a proof (the new code lives). Run both.

A windowed Blender that crashes shows a "restart or close" window and waits for
a click, so a script that runs it hangs and the user gets a pile of dialogs.
Watch for `<TEMP>/<blend name or "blender">.crash.txt` and kill the process by
a marker in its own command line as soon as that file appears.

Blender's source for the exact version is one `curl` away:
`https://projects.blender.org/blender/blender/raw/branch/blender-v5.2-release/source/blender/<path>`.
Read it before theorising: an hour of guesses about which pointer was stale
ended in ten minutes of reading.

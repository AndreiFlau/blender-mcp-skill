# Live interaction and pose-stream performance

Findings from Blender 5.2.2 on Windows, September 2026. Recheck installed versions
and reproduce the user's symptom before applying a past workaround.

## Measure the interaction that is failing

- A scripted transform followed by an update is not the same workload as a user
  dragging a gizmo. Ask whether the hitch is continuous or only at start/confirm,
  then capture a real drag when automation does not reproduce it.
- Verify that the instrumented object/control actually changed. An automated
  Cascadeur drag selected a gizmo without moving the pose; those timings were
  invalid. Empty/stationary windows are not evidence of smooth interaction.
- For native Blender versus live retargeting, replay the same final control
  channels in the same scene with the bridge unregistered. One native bone move
  versus a whole-body stream is not an equivalent speed comparison. Keep mesh
  quality and unrelated add-on settings identical; include paused and unchanged
  stream controls where relevant.
- Separate capture wait, transport/scheduling, pose application and redraw.
  `VIEW_3D` `POST_PIXEL` is a CPU draw callback, not GPU completion, displayed FPS
  or mouse-to-photon latency. Timer gaps measure main-thread availability, not
  animation quality. Cross-process timestamps need compatible clocks or a
  measured offset; match samples by sequence, not arrival order alone.

## Attribute stalls to the callback that blocks

Time suspected callbacks with `time.perf_counter()` around the actual call,
including its return and exception paths. A timer nominally firing every second
can still block the UI for hundreds of milliseconds during a drag. Absence of
main-thread HTTP requests does not rule out other synchronous work.

Use a bounded capture and restore exact original function objects, timer
registrations, draw handlers and profiler state afterwards. Some add-ons have
watchdogs that re-register missing timers: disabling a worker alone may not
isolate it. Confirm the intended callbacks remained off throughout the test.

### Verified BlenderKit case

BlenderKit **3.21.2.260918** caused continuous cube-scaling stutter with Cascadeur
closed. The control sequence was: add-on disabled = smooth; interface enabled
with background callbacks paused = smooth; normal callbacks restored = jerky.
Direct wall-clock timing caught `search.check_clipboard` blocking for roughly
**147–633 ms**, inside `timer.client_communication_timer`.

Turning off the preference **Use Clipboard Scan** (`use_clipboard_scan=False`)
restored smooth user drags with the normal interface and callbacks enabled.
Experimental/threaded communication had removed main-thread network calls but
had **not** fixed this clipboard stall. Treat these as separate mechanisms; do
not enable experimental settings as a universal performance fix. Inspect the
actual installed preference package instead of assuming its module prefix.

The upstream [issue #2244](https://github.com/BlenderKit/Blendkit/issues/2244)
was closed by a Linux/X11-specific clipboard change
([commit](https://github.com/BlenderKit/Blendkit/commit/f5be8f82e3c263123f967add80f822fb8c8967e2)).
That did not establish a Windows fix. Recheck current releases before predicting
an ETA or suggesting re-enabling the scan. Save preferences only when the setting
change is authorized; a diagnostic session does not require saving the `.blend`.

## Avoid making every character pay for one edit

An updated scene packet does not mean every rig changed. Compare per-character
pose content against the **last applied pose**, not just the previous received
sample; otherwise small intended movements can disappear forever. Source solver
noise can change untouched rigs slightly. A bounded preview tolerance helped,
but offline baking must still process every requested sample without that filter.

Independent rigs should skip unchanged poses. Corrections, animated pin objects
and directed contacts are additional invalidation inputs: a moving leader can
require its stationary follower to update. Evaluate the leader before its
follower and reject cycles.

Repeated dependency-graph updates can dominate matrix math because they also
evaluate meshes. In this bridge, a temporary rig-only view layer for intermediate
solves plus one final visible evaluation reduced cost without changing modifiers.
This needs explicit dependency coverage and cleanup on disconnect, save, undo,
reload and unregister; it is not a generic instruction to hide meshes.

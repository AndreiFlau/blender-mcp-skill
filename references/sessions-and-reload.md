# Session identity and reloads

Verified on Windows, Blender 5.2.2 LTS with the official Labs bridge. Ports and
PIDs are runtime discoveries, never permanent defaults.

## Identify the recipient before mutation

A listening port and successful response do not prove which open Blender owns
the connection. Multiple GUI instances have shared the nominal default setup.
Read `os.getpid()`, `bpy.data.filepath`, scene, version and expected objects;
assert the expected identity again inside each mutating script. Map each instance
to its endpoint. Check protocol and process ownership before restarting a service.
An unrelated HTTP service occupying a diagnostic port is not a Blender failure.

If runtime port separation is needed, inspect the installed bridge implementation
first. In the tested build, deferring its stop/start through a Blender timer let
the current response finish before rebinding. This is implementation-specific;
do not save global preferences or prescribe today's alternate port.

With multiple agents, agree on one owner for each live-app mutation/render window,
announce release, and continue offline work during another agent's window. Do not
close user-owned GUI instances merely to serialize tests. After a handoff, verify
the endpoint again. A client timeout does not prove the Blender operation stopped.

## Reload the code actually running

Find the active installed namespace and `__file__` (often `bl_ext...`), unregister
that module and release callbacks before purging only the exact namespace and its
children. A prefix match without the separating dot can remove unrelated modules.
Do not import a second short-name copy and inspect its idle state.

Snapshot filepath, authored poses, evaluated transforms, actions/slots, frame,
selection/mode, Auto Key, visibility, character bindings and relevant paint canvas,
image identities and pixels. Restore the intended paused/listening state explicitly.
Undo, deletion and reload invalidate cached RNA references; reacquire IDs.

Purging `sys.modules` alone may leave a parent test package holding an old child
attribute. Use `importlib.reload(importlib.import_module('tests.module'))` and
assert the test's addon module reference `is` the installed module. Verify source
path/version and exactly one observer registration. Retest after returning through
the event loop. Tests with all rigs paused do not establish safe reload during a
live drag or active stream.

Evidence: Cascadeur Live Link v0.3.3, commit 8a15433, `.work/install_033_validation.py`
and `tests/multi_character_reliability.py`; Fluid Painter Evolved v0.9.6,
commit 752ea13, installed-release verification and `local/admin/refine_install_live.py`;
Futafy v0.5.18, commit ca89be8. These are project-relative provenance, not bundled files.

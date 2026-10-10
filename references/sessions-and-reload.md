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

## Swap an installed extension in a session that must not restart

`blender --command extension install-file -r user_default --enable <zip>`, run from
another process, replaces the files and nothing else: an open Blender keeps the
modules it imported, and unticking and ticking the add-on reloads only its
`__init__` when the package imports lazily. To make the open session run the new
code, inside that Blender:

1. Find the name in `bpy.context.preferences.addons` whose last part is the id.
   Refuse while the add-on has work in progress (a running job, a pending timer).
2. Compile every `.py` of the installed folder in memory first
   (`compile(path.read_text(), str(path), 'exec')`), so a broken update cannot
   leave the session with no add-on. `py_compile.compile` writes a `.pyc` and
   failed on a missing `__pycache__`.
3. Snapshot the scene settings and `preferences.is_dirty`.
4. `addon_utils.disable(name, default_set=False)`, delete exactly `name` and its
   `name + '.'` children from `sys.modules`, `importlib.invalidate_caches()`,
   `addon_utils.enable(name, default_set=False)`. `default_set=False` leaves the
   saved preferences alone.
5. Put back any setting that changed, restart the add-on's own timers, restore
   `is_dirty`, tag the areas for redraw, and report the version now running.

Scene `PropertyGroup` values and the add-on's preferences came through by
themselves; settings the old version did not have appeared at their defaults; the
preferences were not marked for saving.

When the bridge answers from a different Blender than the one meant (two
instances on one port) and rebinding is not yours to do, hand the user one line
for that Blender's Python Console instead:
`exec(open(r"<path to the reload script>", encoding="utf8").read())`.

Evidence: Resolve Bridge v0.5.3, commit 441c103, `tools/reload_in_blender.py` and
`.work/test_reload.py` (0.5.0 and 0.5.1 running, 0.5.3 installed from another
process, in a sandboxed profile). Not exercised through the live bridge.

Evidence: Cascadeur Live Link v0.3.3, commit 8a15433, `.work/install_033_validation.py`
and `tests/multi_character_reliability.py`; Fluid Painter Evolved v0.9.6,
commit 752ea13, installed-release verification and `local/admin/refine_install_live.py`;
Futafy v0.5.18, commit ca89be8. These are project-relative provenance, not bundled files.

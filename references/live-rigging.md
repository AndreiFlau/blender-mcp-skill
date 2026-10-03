# Existing rigs, live poses and saved keys

Validated on specific humanoid adapters in the Blender–Cascadeur bridge,
September 2026. The mechanisms below guide inspection; rig names and values are
not universal presets.

## Follow the rig's authored controls

- Inspect constraints, drivers, IK/FK settings and parent spaces before deciding
  which bone to drive. A bone named like a head control can be a look-at target.
  On the inspected RedEyes Black Cat/Dylan rigs, rotating `Head Control` did not
  turn the head with tracking off; the correct FK control was `head`, with the
  rig's head mode and neck-copy rotation accounted for.
- Near-straight elbows/knees can make a generic midpoint pole plane unstable.
  The inspected Black Cat IK chains also contained an extra elbow/knee segment.
  Incoming upper-limb orientation plus the rig's authored pole angles prevented
  flips without requiring an FK-to-IK workflow or replacing its constraints.
- Matching wrist bone matrices is insufficient when source/target palm axes
  differ. Use wrist/knuckle landmarks and reference hand axes. Different limb
  lengths and reference angles can require segment-direction retargeting with
  target lengths; that still does not guarantee identical world contacts.
- Inspect existing finger curl/copy-rotation constraints. Applying a mapped bend
  to every phalanx can count inherited curl twice. All segments, including thumbs,
  need deliberate mapping; a three-finger rig needs an explicit anatomical
  mapping instead of treating missing fingers as a failed five-finger match.
- Preserve unmapped local channels such as hair and face. Their evaluated world
  transforms can still change through parents and drivers. Existing posed hair
  is not evidence that the bridge changed it.

## Store poses without multiplying Actions

Blender keyframes live in Actions. A live bridge can insert ordinary control
keys at one chosen frame without baking an interval. Reuse one Action per rig
per take, and create a new take only when explicitly requested. A changed active
Action can make the timeline display different keys without deleting the old
Action. Give retained result Actions a fake user when they must survive unassignment.

In Blender 5.2, object/bone channels and armature-data custom properties can use
different slots of the **same Action**. Snapshot the channels being recorded
before changing the active Action, because Action assignment can trigger
evaluation. Preserve the rig-mode properties needed to reproduce the keyed pose.

For a broader native snapshot, Pose Mode's **K → Whole Character** inserts
unlocked bone channels and bone custom properties on the active rig, regardless
of bone selection. It excludes technical name prefixes (`DEF`, `GEO`, `MCH`,
`ORG`, `COR`, `VIS`). It does not capture every property in the character:
separate accessory armatures, directly edited mesh shape keys and object/data
properties outside that set need their own keys. Inspect the installed
`scripts/startup/keyingsets_builtins.py` when coverage matters.

Live preview may overwrite animation evaluation while scrubbing. Pause the
bridge to review recorded keys. If a correction editor temporarily disables
Auto Key, restore the user's prior value on every exit, including cancel, undo,
load and unregister. Undo can recreate Blender IDs; discard caches indexed by
old object pointers and pause preview so incoming data cannot erase the undo.

For the current project's exact button behavior, read its
`docs/character-contacts.md` and `cascadeur_live_posing/actions.py`; avoid assuming
that a button labelled “Key All” includes unmapped hair, face or other objects.


## Reconnection and portable animation proof

A connected replacement socket does not imply fresh pose data. Clear freshness on
disconnect; resume only after a new validated packet while preserving each rig's
paused/enabled intent. A regression should connect an empty replacement socket,
change a correction so stale application is observable, tick, and verify authored
and evaluated poses remain unchanged. Then send fresh data and check intended
resumption. See Cascadeur Live Link v0.3.3 `tests/multi_character_reliability.py`.

Prove addon-independent animation in a separate factory-startup Blender process
with auto-execution disabled, not just by unregistering the producer. A bpy/stdlib
verifier should assert addon modules/RNA absent, action keys, owner/slot assignments
and evaluated world matrices at recorded frames. The five-rig/two-pose fixture
tested OBJECT and ARMATURE slots; its ~2.5e-6 maximum matrix delta is a fixture
measurement, not a universal tolerance. See `tests/verify_keyed_pose.py` and the
headless QA skill's production-fixtures reference.

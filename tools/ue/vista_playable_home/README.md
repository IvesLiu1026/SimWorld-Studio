# VISTA Playable Home Unreal composition

`planning.py` deterministically compiles the closed
`simworld.vista.playable-home-build-plan/v1` document into a fixed Editor
operation list. `contract.py` binds logical asset IDs to pinned host files in
an attempt-local execution manifest. Host bindings cannot provide Unreal
object paths or classes: builtin URIs resolve through a source allowlist and
all other destinations are derived below the new revision namespace.

The fixed execution order is:

1. import assets with `import_assets_commandlet.py`;
2. pin the successful import receipt;
3. compose, save, reload, and verify with `compose_home_commandlet.py`.

Each non-builtin asset must import as exactly one combined primary
`StaticMesh` at its derived path. A partial import/map is retained only as a
quarantined fresh revision; it never overwrites an accepted namespace or
silently falls back to another map.

Room bundles are hollow combined floor/wall/ceiling meshes and therefore use
complex-as-simple static collision; they must never receive one enclosing
convex hull. Player and NPC source transforms are floor-contact transforms,
so planning records and applies the native 96 cm capsule-half-height offset.
The composition also creates one deterministic ceiling light per room and
binds the resident NPC's patrol targets to stable room-centre anchors.

Representative invocation after an execution manifest has been materialized:

```bash
VISTA_PLAYABLE_HOME_EXECUTION=/abs/attempt/execution.json \
VISTA_PLAYABLE_HOME_EXECUTION_SHA256=<sha256> \
VISTA_PLAYABLE_HOME_PROJECT=/abs/attempt/project/VistaHome.uproject \
UnrealEditor-Cmd /abs/attempt/project/VistaHome.uproject \
  -run=pythonscript \
  -script=/abs/repo/tools/ue/vista_playable_home/import_assets_commandlet.py \
  -unattended -nop4 -nosplash

VISTA_PLAYABLE_HOME_EXECUTION=/abs/attempt/execution.json \
VISTA_PLAYABLE_HOME_EXECUTION_SHA256=<sha256> \
VISTA_PLAYABLE_HOME_IMPORT_RECEIPT_SHA256=<sha256> \
VISTA_PLAYABLE_HOME_PROJECT=/abs/attempt/project/VistaHome.uproject \
UnrealEditor-Cmd /abs/attempt/project/VistaHome.uproject \
  -run=pythonscript \
  -script=/abs/repo/tools/ue/vista_playable_home/compose_home_commandlet.py \
  -unattended -nop4 -nosplash
```

These commands are documentation, not evidence that the current source has
been compiled or executed.

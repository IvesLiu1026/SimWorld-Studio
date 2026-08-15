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

Texture acceptance is byte- and reference-backed. The importer parses each
pinned GLB JSON chunk and records how many core `texture.source` entries point
to embedded `image/png` or `image/jpeg` buffer views. When that count is
positive, acceptance requires at least one `Texture2D` returned by
Interchange and the same object path reported by
`MaterialEditingLibrary.get_used_textures` for a material assigned to the
primary mesh. A material slot name alone is not texture evidence; missing or
unbound Texture2D assets quarantine the fresh revision.

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

## Host project orchestrator

`build_home.py` is the only supported host-side preparation path for the
disposable content-only project. Its default mode is a zero-write dry run: it
validates the canonical BuildPlan, Blender receipt and every artifact SHA,
the compiled plugin tree, Manny `Characters` tree, optional full HSSD
attribution overlay, revision namespace, and path containment. It then prints
the exact `execution.json` and both fixed command arrays without requiring an
installed Editor at the printed path.

```bash
uv run --project tools python tools/ue/vista_playable_home/build_home.py \
  --run-root /abs/run \
  --attempt-root /abs/run/ue/attempt-01 \
  --build-plan /abs/run/contracts/build-plan.json \
  --build-plan-sha256 <file-sha256> \
  --blender-manifest /abs/run/blender/build-a/manifest.json \
  --blender-manifest-sha256 <file-sha256> \
  --plugin-package /abs/plugin-package \
  --plugin-package-tree-sha256 <tree-sha256> \
  --characters-content /abs/Content/Characters \
  --characters-content-tree-sha256 <tree-sha256> \
  --unreal-editor-cmd /abs/Engine/Binaries/Linux/UnrealEditor-Cmd
```

For the licensed presentation pass, also provide
`--visual-binding-manifest` and its explicit SHA. Only a canonical `full`
HSSD build with closed coverage, CC BY-NC 4.0 attribution, matching binding
plan, one primary PBR mesh per output, a valid BasisU transport receipt where
required, and an inspection recomputed from the pinned GLB bytes may override
forge presentation GLBs. The gameplay/collision binding digest always remains
the BuildPlan `source_digest`.

`--apply` additionally requires `--unreal-editor-cmd-sha256`. It creates one
new direct child below `<run-root>/ue`, installs the compiled plugin and Manny
content using reflink with byte-copy fallback, writes the pinned execution
manifest, and runs import before composition with `-nocrashreports`. Logs and
receipts are exclusive append-only files. `accepted.json` and `current.json`
are updated under one publication lock only after both commandlet receipts and
markers pass. Each apply owns its attempt through an unguessable sentinel and
reaps its UE process group on timeout or interruption; a failed or partial
attempt remains quarantined and cannot replace either pointer.

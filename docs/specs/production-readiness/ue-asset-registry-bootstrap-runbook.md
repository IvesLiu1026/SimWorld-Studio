# UE AssetRegistry bootstrap for the official minimal content

This runbook turns a read-only Unreal AssetRegistry enumeration into two
revision-bound artifacts:

1. `object-manifest.json`, containing only `StaticMesh` and non-character
   `Blueprint` object candidates for later semantic indexing;
2. `content-capabilities.json`, containing a bounded, explicitly non-semantic
   inventory of character Blueprint candidates, animation clips, skeletal
   meshes, AnimBlueprints, skeletons, IK assets, and Control Rig assets.

It does **not** create a `simworld-asset-snapshot/v1`. AssetRegistry class and
path evidence is not proof of loadability, spawnability, PBR materials,
collision, skeleton compatibility, Postgres rows, Qdrant points, or embedding
revision.

## Bound official source

The source for this procedure is the already verified archive receipt:

```text
/mnt/NAS2/yhliu/SimWorldStudio/0.2.0-806e869a/receipts/archive-receipt.json
```

That receipt binds:

- repository: `SimWorld-AI/SimWorld-Studio` (Hugging Face dataset);
- dataset revision: `26bdd2ca18f06ab455023b0a602ede60b3afb243`;
- archive: `SimWorld-Studio-Minimal.tar.gz`;
- size: `15,170,703,068` bytes;
- SHA-256:
  `806e869ad1c65b298f05a39854b28e4188bb50817f539744451849e054990e2f`;
- source patch commit: `51426e97354477dca1635217455e644e9ca98976`.

The verified extraction contains the project:

```text
/mnt/NAS2/yhliu/SimWorldStudio/0.2.0-806e869a/runtime/SimWorld-Studio-Minimal-806e869a/gym_citynav/gym_citynav.uproject
```

The filesystem-only audit counted 2,937 `.uasset`/`.umap` files, but that count
must never be copied into the semantic catalog count. It includes maps,
textures, materials, animation and helper packages.

## Safety contract

`tools/build_ue_asset_registry_bootstrap.py` has two mutually exclusive modes:

- `--audit-input ABSOLUTE_JSON --dry-run` is the default safe verification path.
  It performs no UE, network, VLM, Postgres, or Qdrant call and writes nothing.
- `--live-query` authorizes exactly one fixed AssetRegistry query. It also
  requires an explicit `--transport`, `--host`, `--port`, and absolute
  `--output-dir`.

The fixed live script calls `AssetRegistry.get_assets_by_path("/Game", ...)`
and returns only package name, asset name and class for a fixed class allowlist.
It does not call `load_asset`, `EditorAssetLibrary`, `open`, save APIs, or read
files in `Content`. Unknown fields, classes, non-`/Game` paths, duplicate rows,
row-count mismatches and project-name mismatches fail closed.

Every publication requires all three immutable bindings:

- `--project-revision` — for this archive,
  `source-patch:51426e97354477dca1635217455e644e9ca98976`;
- `--content-revision` — for this archive,
  `sha256:806e869ad1c65b298f05a39854b28e4188bb50817f539744451849e054990e2f`;
- `--archive-receipt` — the verified receipt above. The tool validates matching
  expected/actual bytes and hash, strips host-local paths from outputs, and
  records a canonical receipt digest.

These are not free-form labels: `--project-revision` must equal the verified
receipt's `source-patch:<commit>` binding (or its archive-revision binding when
no source patch exists), and `--content-revision` must equal
`sha256:<verified-archive-sha256>`. A same-named project or audit cannot be
relabelled as the official content by supplying different revision strings.

Output publication is non-overwriting. Its existing parent must be owned by
the current user, have no group/other permissions, and contain no symlink path
component. The tool creates a new mode-`0700` directory, writes mode-`0600`
JSON files atomically, and writes
`bootstrap-receipt.json` last. An existing output directory is an error; use a
new revisioned directory instead of deleting evidence.

## Offline contract smoke

The checked-in fixture is synthetic and is only a schema/filter regression
test. It is safe to run without UE:

```bash
cd /absolute/path/to/SimWorld-Studio

uv run --project tools --frozen python \
  tools/build_ue_asset_registry_bootstrap.py \
  --audit-input "$(realpath tools/tests/fixtures/ue_asset_registry_audit_v1.json)" \
  --project-name gym_citynav \
  --project-revision source-patch:51426e97354477dca1635217455e644e9ca98976 \
  --content-revision sha256:806e869ad1c65b298f05a39854b28e4188bb50817f539744451849e054990e2f \
  --archive-receipt /mnt/NAS2/yhliu/SimWorldStudio/0.2.0-806e869a/receipts/archive-receipt.json \
  --dry-run
```

The summary intentionally prints `"snapshot_complete": false`.

To test publication with an already captured, operator-reviewed real audit,
replace the fixture path and choose a new absolute output directory:

```bash
install -d -m 0700 /absolute/operator/evidence/ue-asset-bootstrap

uv run --project tools --frozen python \
  tools/build_ue_asset_registry_bootstrap.py \
  --audit-input /absolute/operator/input/gym-citynav-registry-audit.json \
  --project-name gym_citynav \
  --project-revision source-patch:51426e97354477dca1635217455e644e9ca98976 \
  --content-revision sha256:806e869ad1c65b298f05a39854b28e4188bb50817f539744451849e054990e2f \
  --archive-receipt /mnt/NAS2/yhliu/SimWorldStudio/0.2.0-806e869a/receipts/archive-receipt.json \
  --inventory-limit 250 \
  --output-dir /absolute/operator/evidence/ue-asset-bootstrap/official-minimal-26bdd2ca-r1
```

## Admin-gated live enumeration

Do not launch UE merely to run this command. An administrator first needs to:

1. approve and prepare a disposable operator-owned workspace for the exact
   verified project; do not modify the NAS extraction in place;
2. approve the matching Linux Unreal Editor and checked-in plugin build;
3. start the project with directory watching disabled and confirm the selected
   MCP/bridge is listening on a specific host/port;
4. record the editor version, project name, command line, bridge build and log
   location in the evidence directory.

Only after that approval, query the explicitly selected compatible newline-JSON
TCP bridge. Replace the endpoint and revisioned output path; there are no port
or output defaults:

```bash
install -d -m 0700 /absolute/operator/evidence/ue-asset-bootstrap

uv run --project tools --frozen python \
  tools/build_ue_asset_registry_bootstrap.py \
  --live-query \
  --transport legacy-tcp \
  --host 127.0.0.1 \
  --port APPROVED_UE_BRIDGE_PORT \
  --project-name gym_citynav \
  --project-revision source-patch:51426e97354477dca1635217455e644e9ca98976 \
  --content-revision sha256:806e869ad1c65b298f05a39854b28e4188bb50817f539744451849e054990e2f \
  --archive-receipt /mnt/NAS2/yhliu/SimWorldStudio/0.2.0-806e869a/receipts/archive-receipt.json \
  --inventory-limit 250 \
  --output-dir /absolute/operator/evidence/ue-asset-bootstrap/official-minimal-26bdd2ca-live-r1
```

For an MCP implementation with a different transport, an approved adapter must
produce `simworld-ue-asset-registry-audit/v1` JSON matching
`tools/ue_asset_registry_audit_schema.json`; feed that regular JSON file into
the offline mode. Do not add caller-authored Python or Content-file reads to
this tool to accommodate a bridge.

## Review before any semantic indexing

Treat a bundle as complete only when `bootstrap-receipt.json` exists, its file
hashes match, `bundle_complete` is `true`, and `snapshot_complete` remains
`false`. Then review:

- `registry-audit.json` has the expected project and archive/content binding;
- `object-manifest.json` contains only `StaticMesh`/`Blueprint` candidates;
- chair and box candidates have the expected `/Game/...` paths;
- `content-capabilities.json` contains lift/fall/IK/Control Rig candidates but
  has no top-level `assets` list;
- every truncated capability group is explicitly reviewed or rerun with a
  higher bounded `--inventory-limit`. Within a truncated group, known
  lift/fall/IK/rig signals are retained first, followed by deterministic
  class/path order; truncation still means the inventory is incomplete.

The object manifest can become the immutable input to the existing render/VLM
index runner only after operator review. Never pass
`content-capabilities.json` to semantic object indexing.

## Project-owned animation profile assessment

A focused filename-only audit of the verified extraction found enough material
to define a *candidate* project-owned profile for the next UE inspection:

- character/Pawn candidates under `/Game/Human_Avatar/Blueprint/` and
  `/Game/Human_Avatar/DefaultCharacter/Blueprint/`, including
  `BP_Human_Base`, `BP_DefaultHuman`, `BP_Default_Character`,
  `ABP_Human_Base`, `ABP_Default_Character`, and `CR_Default_Character`;
- Manny mesh and animation candidates such as
  `/Game/Characters/Mannequins/Meshes/SKM_Manny`,
  `/Game/Characters/Mannequins/Animations/ABP_Manny`, and their checked-in
  copies below `/Game/Human_Avatar/DefaultCharacter/Characters/Mannequins/`;
- many lift sequences and montages below
  `/Game/Human_Avatar/Animation/LiftSet/`, including PickUp, PutAside and Throw
  variants at several heights;
- the fall candidate
  `/Game/Characters/Mannequins/Animations/Manny/MM_Fall_Loop`;
- `/Game/Characters/Mannequins/Rigs/IK_Mannequin` and
  `/Game/Characters/Mannequins/Rigs/CR_Mannequin_BasicFootIK`, with related
  copies below the Human_Avatar subtree.

Those are filesystem-derived package *candidates*, not proven object paths.
Without the AssetRegistry audit and bounded UE inspection, the following links
remain unknown: actual asset class and `package.name` object path, generated
Blueprint class (`_C`), Pawn/Character inheritance, SkeletalMesh Skeleton,
AnimBlueprint TargetSkeleton, montage slot and Skeleton, fall clip Skeleton and
root-motion settings, IK Rig target/preview mesh, Control Rig target Skeleton,
and compatibility across the `/Game/Characters` and `/Game/Human_Avatar`
copies. Therefore the inventory is sufficient to drive a small inspection
matrix, but not sufficient to register an executable production animation
content profile.

## Remaining independent gates

Each of the following still needs separate administrator or cost approval and
separate evidence:

- **UE load/spawn smoke:** load the selected chair/box/cabinet/stool candidates,
  capture class, bounds, collision and material slots, and prove no redirector
  or missing dependency;
- **animation compatibility:** prove the humanoid SkeletalMesh, Skeleton,
  AnimBlueprint, lift/fall clips, IK Rig and Control Rig are mutually
  compatible in the exact UE project; AssetRegistry names alone do not prove
  it;
- **VLM/provider call:** approve model/provider, bounded asset slice, token
  budget, render evidence and failure policy before generating descriptions;
- **Postgres/Qdrant writes:** provision credentials/schema/collection, migrate
  only the reviewed object manifest, stamp the exact asset snapshot revision,
  and verify row/point parity;
- **snapshot receipt:** run the asset snapshot verifier and bind catalog digest,
  Postgres schema/count, Qdrant collection/count, embedding model revisions and
  the exact UE content revision.

Until those gates pass, the result is an AssetRegistry bootstrap candidate—not
a production semantic asset snapshot and not proof that the VISTA 3D scene is
fully buildable.

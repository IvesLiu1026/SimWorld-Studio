# 2026-07-21 official-minimal UE content audit

This is a focused, read-only audit of a previously downloaded SimWorld
artifact on the local NAS. It identifies a credible UE Content source for the
next asset-indexing step. It is not an asset snapshot, a database receipt, or
proof that any individual package is spawnable.

## Bound source artifact

The existing archive receipt at
`/mnt/NAS2/yhliu/SimWorldStudio/0.2.0-806e869a/receipts/archive-receipt.json`
records all of the following:

- Hugging Face dataset repository: `SimWorld-AI/SimWorld-Studio`;
- dataset revision: `26bdd2ca18f06ab455023b0a602ede60b3afb243`;
- archive: `SimWorld-Studio-Minimal.tar.gz`;
- exact size: `15,170,703,068` bytes;
- verified SHA-256:
  `806e869ad1c65b298f05a39854b28e4188bb50817f539744451849e054990e2f`.

The receipt reports matching expected and observed size/hash. The extracted
runtime is under
`/mnt/NAS2/yhliu/SimWorldStudio/0.2.0-806e869a/runtime/SimWorld-Studio-Minimal-806e869a`.
No file in that runtime was changed during this audit.

## UE project and filesystem inventory

The extracted project is
`gym_citynav/gym_citynav.uproject`. It targets Linux and enables the checked-in
`UnrealMCP`, `PixelStreaming`, `EditorScriptingUtilities`,
`PythonScriptPlugin`, and `SunPosition` plugins. The same runtime contains
regular executable `UnrealEditor` and `UnrealEditor-Cmd` files as well as a
compiled `UnrealMCP` editor module.

A filesystem-only count found `2,937` `.uasset` plus `.umap` files below the
project's `Content` directory. This number includes animations, materials,
textures, maps, helper assets, and other non-spawnable packages; it must not be
used as the catalog or Postgres/Qdrant row count. Top-level content roots
include `CityDatabase`, `Human_Avatar`, `Characters`, `CitySampleCrowd`,
`Camping_Pack`, `Industrial_Carts`, `GasStation`, and several vehicle packs.

Concrete package files relevant to the first `mmg_040` scene are present:

- `CityDatabase/meshes/SM_chair_b.uasset`;
- `CityDatabase/blueprints/BP_Box.uasset`, `BP_Box2.uasset`, and
  `BP_Box3.uasset`;
- `Camping_Pack/Props/Seat_Table_01/Meshes/SM_SeatTable_01a.uasset`, a
  filename-only candidate for the visible stable step/seat alternative;
- `Industrial_Carts/Meshes/SM_Industrial_Carts_Static_Carts_1.uasset` and
  `SM_Industrial_Carts_Service_Carts_8.uasset`, filename-only candidates for a
  tall storage support or cabinet surrogate;
- the industrial-cart pack also contains separate 4K BaseColor, Normal,
  Roughness, and Metallic texture packages for its pinned material families;
- multiple `Human_Avatar/Animation/LiftSet` sequences and montages, including
  look, pickup, put-aside, idle, and throw variants at several heights;
- `Characters/Mannequins/Animations/Manny/MM_Fall_Loop.uasset`;
- `Characters/Mannequins/Rigs/IK_Mannequin.uasset` and
  `CR_Mannequin_BasicFootIK.uasset` (with corresponding copies in the
  `Human_Avatar` subtree).

No package named literally as a cabinet, shelf, or step stool was identified by
the bounded filename search. A follow-up synonym search found the seat-table
and industrial-cart candidates above. Their names and adjacent PBR packages
make them useful live-inspection candidates, but do not prove dimensions,
stability, support-surface suitability, semantic role, or spawnability.

## What remains unproven

The extracted project does not contain a discovered `AssetRegistry.bin`, a
complete `catalog/**/*.json` corpus, or a `category_index.json`. No UE process
was launched, so this audit does not establish:

- asset class or exact `/Game/...` object path;
- Blueprint spawnability;
- dimensions, collision, material slots, or PBR texture bindings;
- animation skeleton/AnimBP compatibility;
- cabinet/stool or storage-rack/stable-step semantic matches;
- a catalog digest, Postgres row count, Qdrant point count, or embedding
  revision.

The archive revision and SHA-256 are therefore a strong candidate for the
`ue_content_revision` input, not yet an accepted `simworld-asset-snapshot/v1`.

## Next controlled action

Run AssetRegistry enumeration against this exact project in a disposable
operator-owned UE workspace, with directory watching disabled and all output
written outside the extracted runtime. The resulting strict object manifest
must be reviewed before any VLM indexing or database write. The first bounded
smoke should load only the chair, box, likely cabinet/stool candidates, one
humanoid, the fall sequence, and the IK/Control Rig assets; it should capture
class, object path, bounds, collision, materials, skeleton and load/spawn
results.

That UE launch, any VLM calls, and Postgres/Qdrant provisioning remain separate
state-changing gates. This audit performed none of them.

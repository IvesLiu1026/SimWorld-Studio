# Design: VISTA Multi-Scene Continuous 3D World

Status: Proposed — awaiting user approval
Updated: 2026-08-11
Depends on: `requirements.md`

## Summary

The system will compose one persistent Unreal map from deterministic modular
Blender bundles. A strict `WorldPack` describes the full eight-zone VISTA
Living Lab, while milestone revisions first deliver three named connected
slices and then finish five and eight zones. Scenario overlays alter reusable
state without regenerating the environment.

The first user-visible checkpoint is deliberately early: a continuous
office-to-corridor-to-kitchen walk with navigation, one fixed interaction,
Pixel Streaming, and evidence that the world continues beyond 12 seconds.
Studio catalog work does not block that slice.

The existing MMG040 code and evidence remain immutable. The current minimal UE
artifact supports editor-PIE M1a-M2 only. A later compiled plugin on a complete
matching UE toolchain supplies authoritative WorldSession behavior for M3.

## Architecture

```text
VISTA taxonomy/scenario manifest
                |
                v
WorldPack source + Overlay source + external-asset lock
                |
       strict validate/resolve
                v
immutable build plan ---------> deterministic Blender bundle builds
                |                           |
                +---------- exact manifests/GLBs/PBR locks
                                            |
                                  generic UE import spike
                                            |
                               revisioned namespace + receipts
                                            |
                                  persistent-map composition
                                            |
                           NavMesh/collision/material/perf receipts
                                            |
                              receipt-bound editor-PIE runtime
                                            |
                     loopback Pixel Streaming + generic live QA
                                            |
                    M3 compiled WorldSession (later toolchain)
```

All build and runtime attempts are append-only. A receipt advances only the
gate it measures; a generated GLB, saved map, or HTTP 200 alone is never a
milestone.

## World Topology and Milestone Revisions

The source graph declares all eight zones from M0:

```text
office/workplace ─ corridor/lobby ─ teaching kitchen/café
                         ├──────── classroom/library
                         ├──────── lab/workshop
                         └──────── outdoor quad ─ parking/crosswalk/transit
                                           └──── store/commons
```

- `r1-m1a`: three finished slices—legacy office core, main corridor/lobby, and
  teaching-kitchen/café—with two real portals. Meeting/print/pantry,
  elevator/stair, and the remaining zone 1-3 subspaces are explicitly absent.
- `r1-m1`: finished zones 1-5 and at least four traversable boundaries.
- `r1-m2`: finished zones 1-8 and the complete required graph.

The generic resolver, import, and composition tools accept any immutable pack
revision; M2 is a fresh revisioned import/composition, not an in-place edit of
M1. They do not hard-code this topology or an eight-zone ceiling. Runtime
hot-switching is deferred.

## Contracts

### World-pack source

Schema ID: `simworld.vista.world-pack-source/v1`

It binds `pack_id`, `revision`, seed, single-persistent-level composition,
base-project profile, bundles, zones, anchors, paired portals, fixed cameras,
navigation/interaction probes, overlay references, runtime profile, absolute
and pack budgets, external asset locks, and provenance.

### Blender asset bundle

Schema ID: `simworld.vista.blender-asset-bundle/v2`

Meshes remain in bundle-local coordinates. Every mesh records stable semantic
ID, role, bounds, material bindings, collision policy, and optional
interaction role. The content digest excludes timestamps and host paths.

### Scenario overlay

Schema ID: `simworld.vista.scenario-overlay/v1`

An overlay contains:

- `overlay_id`, revision, compatible pack/revision range;
- allowlisted operations and stable semantic object targets;
- initial/desired state plus reset policy;
- public runtime payload separate from VISTA provenance;
- source scenario IDs and evidence references stored outside restricted model
  inputs;
- optional interaction and acceptance probes.

M1a-M2 resolve and validate overlays and may apply one at launch through fixed,
source-controlled code. M3 adds atomic apply/clear, generation checks,
idempotency, reconciliation, and baseline/checkpoint restoration.

### Plans and receipts

- `simworld.vista.world-build-plan/v1`
- `simworld.vista.world-import-receipt/v1`
- `simworld.vista.world-compose-receipt/v1`
- `simworld.vista.world-runtime-receipt/v1`
- `simworld.vista.world-performance-receipt/v1`

The plan pins pack/tool/input/engine hashes, resolved centimetre transforms,
namespaces, map paths, expected budgets, and output roots. Import and compose
receipts bind exact revisions; runtime receipts cannot silently select an
unverified map.

## Sealed Legacy Input

The legacy adapter reads only:

```text
/mnt/NAS2/yhliu/SimWorldStudio/vista-blender-world/runs/
  20260811T-procedural-chair-upgrade-r3/blender/manifest.json
/mnt/NAS2/yhliu/SimWorldStudio/vista-blender-world/runs/
  20260811T-procedural-chair-upgrade-r3/blender/vista_mmg040_office.glb
```

It verifies the hashes in `requirements.md`, returns an in-memory normalized
bundle view, and imports it into a new pack namespace. It neither rewrites the
v1 files nor reads/copies the live attempt-11 project. Attempt-07 is historical
evidence, not a mutable seed project.

## Capability-First Unreal Strategy

Bulk world authoring is gated by a small disposable two-bundle fixture. The
fixture proves, on the current UE 5.3.2 artifact:

1. bitmap base-color/normal/roughness import and correct UE color spaces;
2. material construction/binding and stable semantic coverage;
3. two namespace-isolated bundles with colliding source names;
4. walkable versus decorative versus interaction collision policies;
5. NavMesh build and authoritative non-partial path query across one portal;
6. server-frame, process-memory, GPU-memory, decoded-frame, hitch, and freeze
   telemetry.

The fixture can use tiny deterministic generated textures and geometry, so it
does not require external downloads. Static/mock validation may run during
code work, but every Blender executable/generation invocation and every actual
UE/GPU execution still uses the separate R13 apply gates. If this spike fails,
zone builders are not bulk-generated; the limitation is fixed or the design is
revised first.

## Blender and PBR Authoring

New builders share `vista_world` modules for semantic IDs, transforms,
materials, collision roles, validation, previews, and deterministic manifests.
Each bundle is generated locally around its anchor and instanced by the pack.

External assets follow two phases:

1. read-only candidate ledger: URL, provider, author, license, expected files,
   redistribution decision, and size estimate;
2. approved acquisition: download into a new append-only NAS root, inspect for
   executables/path traversal, hash, validate maps/UVs, and seal a lockfile.

The accepted legacy office uses its existing constant-material geometry. New
corridor and kitchen assets supply the first bitmap-PBR proof. Fixed cameras
and an explicit visual rubric make review reproducible.

## Unreal Import and Composition

New namespaces use:

```text
/Game/VISTA/WorldPacks/<PackId>/<Revision>/Assets/<BundleId>/...
/Game/VISTA/WorldPacks/<PackId>/<Revision>/Maps/<MapName>
```

The importer is revision-repeatable and fail-closed. It verifies source hashes,
creates material assets with correct texture roles, applies per-role collision,
and writes inventory/coverage receipts. The composer creates a fresh persistent
map, places stable-ID actors, portals, PlayerStart, NavMesh bounds, lighting,
and fixed probes, then saves and reloads the map before acceptance.

No commandlet edits the base source map or a previously accepted revision.
Ambiguous saves and partial imports quarantine the attempt.

## Runtime Design

### Editor-PIE M1a-M2

The runtime remains launcher-owned. `launch.py` gains a mutually exclusive
`--world-compose-receipt` mode while preserving legacy `--map`. The receipt
mode verifies project/map/content hashes.

`runtime.py`, `launch.py`, verification tools, tests, and README are changed
together to replace the current GPU-1-only assertion with an explicit owned
GPU parameter. Defaults remain backward compatible; a new GPU/port set is
accepted only through preflight and run authorization. Ports are passed as a
validated non-overlapping profile, never guessed from live listeners.

Editor lifetime states remain the existing process-supervision states. M1a-M2
claim only continuous owned-process lifetime and browser-close survival.

### Authoritative M3

The compiled WorldSession uses:

```text
booting -> running -> resetting -> running
                   -> stopping -> stopped
                   -> failed | quarantined
```

It owns the pack revision, monotonic session clock, capability lease, state
generation, stable UUID registry, typed command transport, checkpoints,
overlay transactions, persistence, and telemetry. There is no `switching`
state in this milestone. A different pack starts a new session.

## M1a Interaction and Evidence

M1a launches directly from the composition receipt and does not wait for a
Studio catalog/selector. One fixed-code door or refrigerator interaction has a
stable target, before/after state query, and correlated receipt.

Before composition, a dedicated preview-surface increment implements:

- a fixed UE/runtime state probe for pawn position and stable object state;
- a server-side zone resolver using pack bounds;
- a professional web-player HUD with zone, position, and elapsed session time;
- one allowlisted fixed-code door/refrigerator command adapter;
- a receipt-bound launch hook that applies one validated overlay and a clean
  baseline restart that omits it.

It owns new bounded runtime/server modules plus
`simworld_studio_workspace/web/public/ue-player.html`, related server routes,
and focused tests. It never exposes caller-authored Python or oracle metadata.

The acceptance recording is uncut, 60-90 seconds, travels at least 40 metres,
crosses both portals out and back, performs the interaction, and demonstrates
elapsed time beyond 12 seconds. A six-frame contact sheet uses fixed view IDs
from the pack. A second short evidence pair proves overlay-applied launch and
clean-baseline restart.

## Studio Surface

Studio registry work follows M1a and is not on its critical path:

- `GET /api/world-packs`
- `GET /api/world-packs/active`

Responses are sanitized and read-only. The unrestricted legacy map-loading
route is not reused for activation. M3 lifecycle endpoints later require the
lease/capability model. If a selector is built, its visual language is a
restrained professional control surface—no chat bubbles, novelty gradients,
anthropomorphic copy, or decorative AI branding.

## Performance and Observability

A performance sampler records:

- UE server-frame time at 1 Hz after a five-minute warmup;
- UE PID RSS/PSS and assigned-GPU memory at 1-minute intervals;
- browser decoded-frame counters at 15-second intervals;
- hitches over 100 ms;
- freeze intervals where decoded frames fail to advance for over two seconds.

Receipts specify process IDs, GPU UUID/index, resolution/FPS, warmup, sample
window, counts, percentiles, missing samples, and aggregation code digest.
M1a uses a 10-minute run; M1/M2 use 30 minutes with the minute-10-to-30 memory
delta; M3 uses an eight-hour soak and post-hour-one growth. Results from
editor-PIE are always labeled `dev-profile`, never Production. Actor,
triangle, material, and collision counts come from the reloaded map/assets;
import, NavMesh, and cold-launch timing boundaries are those defined by R12.

## Security and Permission Model

Spec approval opens only source/schema/test implementation. Work is separated
into `plan/dry-run` and `apply` operations. External downloads, every Blender
executable or generation invocation (including tiny fixtures), UE project/map
writes, GPU runtime, paid calls, uploads, deploys, merges, and public listeners
each require their recorded gate.

Every run uses a named tmux session, explicitly owned loopback ports, private
tokens, GPU assignment, and a new NAS-resident project/run root. GPU 1 and all
currently reserved sessions/ports remain untouched. GPU 0 must pass NVIDIA and
render-device preflight before assignment.

## File Plan

New paths:

- `world_packs/schemas/{world-pack-source-v1,scenario-overlay-v1}.schema.json`
- `world_packs/vista_living_lab_r1/{pack,provenance,overlays}.json`
- `tools/worlds/{resolve,validate,legacy_adapter,overlay}.py`
- `tools/blender/vista_world/{forge,transforms,bundle_manifest,materials,registry}.py`
- `tools/blender/{build_vista_zone_bundle,validate_vista_asset_bundle}.py`
- generic world import/compose modules and schemas under
  `tools/ue/vista_blender_world/`
- `tools/runtime/vista_blender_world/{world_profile,world_qa,performance}.py`
- `tools/runtime/vista_blender_world/world_preview.py`
- `simworld_studio_workspace/web/server/vista-world-preview-runtime.js`
- `simworld_studio_workspace/web/public/ue-player.html` and focused tests for
  the bounded HUD/state/interaction/overlay preview surface
- `simworld_studio_workspace/web/server/world-pack-registry.js`
- focused Python/Node tests for every boundary.

Existing MMG040 generators, v1 schemas, commandlets, receipts, `--map` mode,
and historical evidence remain unchanged. Runtime launcher modules may receive
backward-compatible generic options with focused regression tests.

The compiled plugin is isolated under `unreal_plugins/` only after the full UE
development toolchain is provisioned.

## Test Strategy

### Offline

- JSON Schema 2020-12 meta-validation and closed-schema negatives.
- Duplicate/non-finite/path/namespace/graph/portal/budget/license/digest
  failures.
- Deterministic transform, plan, bundle digest, and overlay fixtures.
- Oracle/review-field leakage negatives.
- Exact r3 legacy hashes and all existing MMG040 regression suites.

### Blender

- Two clean builds per fixture/bundle.
- glTF structure, semantic inventory, local bounds, topology, materials,
  collision roles, UV coverage, PBR role/color-space metadata, previews, and
  milestone budgets.

### Unreal

- Capability spike before bulk generation.
- Fresh revisioned imports; namespace collision fixture; source unchanged;
  saved map reload; stable IDs; explicit collision; portal capsule clearance;
  authoritative NavMesh paths.
- Fixed cameras and objective visual review ledger.

### Live

- Loopback/auth/listeners, advancing frames, possession, movement/key-up,
  mouse yaw, typed interaction, overlay launch, continuous process lifetime,
  browser-close survival, performance samples, and secret-free traces.
- M3 adds lifecycle/generation/idempotency, 100 reset and door cycles,
  checkpoint tolerances, injected timeout reconciliation, and eight-hour soak.

## Git and Evidence Sequence

For each live milestone:

1. tests pass and implementation is committed as a clean candidate;
2. the isolated runtime launches from that exact candidate commit;
3. append-only external evidence binds the candidate commit and artifacts;
4. acceptance is reviewed;
5. a focused post-acceptance spec/evidence-pointer commit is created;
6. the collaboration branch is pushed and handed off.

This avoids a self-referential manifest hash. Generated projects, maps,
textures, tokens, traces, and recordings remain outside Git.

## Milestones and Infrastructure Boundary

- **M0**: implementable now with no administrator.
- **M1a-M2**: conditionally implementable as editor-PIE dev-profile evidence
  using pinned Blender/minimal UE, NAS, an approved GPU 0 run, and fresh ports.
- **M3**: requires a matching full UE 5.3.2 development installation with
  RunUAT, UBT/UHT, headers, Linux SDK/compiler, and a successful clean plugin
  compile/cook spike.
- **M4**: additionally needs durable service identity/supervision, monitoring,
  and DNS/TLS/Coturn/firewall/NAT only if public WebRTC is requested.

A full Epic source checkout is not required unless an engine patch is later
demonstrated to be necessary.

## Rejected Alternatives

- One map per VISTA clip: duplicates environments and couples maps to episodes.
- Building all eight zones before a live proof: delays user-visible value and
  risks discovering UE limitations late.
- Runtime hot-switching first: adds transactional failure modes before one
  persistent world is proven.
- World Partition now: unnecessary for this compact pack and unvalidated in the
  minimal editor lane.
- Refactoring accepted MMG040 code: invalidates source-bound evidence.
- Depending on DiverseMaps screenshots: no accepted `.umap` payload exists on
  this host and their prior visual/lighting/navigation quality is inconsistent.

## Approval

- Requested by: Codex `/root`
- Approved by: pending user approval
- Date: pending

# Requirements: VISTA Multi-Scene Continuous 3D World

Status: Proposed — awaiting user approval
Updated: 2026-08-11

## Problem

The accepted VISTA Blender/Unreal vertical slice proves one detailed office
room and one loopback interactive editor runtime. It does not yet prove a
reusable, multi-zone 3D world. The generator, Unreal composition, live probes,
and acceptance evidence are still tied to `mmg_040`.

VISTA contains much broader environmental coverage. The current 835-row
dashboard includes kitchen/dining (224), road/crosswalk (149), vehicle/parking
(106), store/mall (59), outdoor/public (58), entry/hallway (51), and
office/workspace (50) as major scene groups. These are reusable environment
families, not 835 unrelated maps. A 12-second interval is an optional recording
or evaluation episode; it must never be the lifetime of the world.

## Delivery Levels

The phrase **full 3D environment simulation** is divided into testable levels.
This prevents an editor preview from being reported as a Production simulator.

| Level | Deliverable | Completion meaning |
| --- | --- | --- |
| M0 | Contracts | Strict world-pack, bundle, overlay, plan, and receipt contracts validate offline. |
| M1a | Visible connected slice | Legacy office core → main corridor/lobby → teaching-kitchen/café is one persistent, walkable, interactive editor-PIE map. |
| M1 | Indoor living lab | Zones 1-5 are connected and usable as one continuous editor-PIE world. |
| M2 | Full first world pack | All eight zones are connected and usable as one continuous editor-PIE world. |
| M3 | Authoritative simulation | A compiled WorldSession owns lifecycle, state generation, typed commands, overlays, checkpoint/reset, persistence, telemetry, and an eight-hour soak. |
| M4 | Production distribution | Cooked build, service supervision, 24-hour soak, and an optional public WebRTC deployment. |

M1a-M2 are development previews backed by real Unreal state and navigation.
They are not labeled Production. M3 is the first level that satisfies the
project's strict meaning of a full, stateful 3D environment simulation.

Human IK/fall animation, autonomous pedestrians or traffic, public WebRTC, and
photoreal digital-twin reconstruction remain separate systems even at M3
unless they receive their own approved requirements and evidence.

## Goals

- Replace the one-off MMG-specific path with a data-driven world-pack
  architecture.
- Deliver visible spatial scale early through M1a, then grow the same pack to
  five and eight zones without throwaway work.
- Keep the world alive until an owned explicit stop or failure; remove any
  12-second lifetime assumption.
- Separate persistent environment geometry from reusable VISTA scenario-state
  overlays such as an open appliance, left item, spill, or blocked path.
- Preserve deterministic Blender generation, strict Unreal composition,
  loopback Pixel Streaming, append-only evidence, and Mac-over-SSH access.
- Establish a controlled path to authoritative session state, reset,
  persistence, and typed interactions.

## Non-goals

- Reconstruct every VISTA clip as a separate map.
- Infer a canonical campus topology, exact dimensions, weather, or art style
  from videos; authored choices receive explicit provenance.
- Expose arbitrary caller-authored Unreal Python as an interaction API.
- Claim a cooked build, World Partition, public WebRTC, or runtime map hot-swap
  from the current minimal UE 5.3.2 editor artifact.
- Make Postgres/Qdrant semantic retrieval a prerequisite for the first
  deterministic pack.
- Mutate or copy from the live attempt-11 project to construct the new world.

## Definitions

- **World pack**: a versioned description of a world, reusable bundles, zone
  graph, overlays, runtime profile, budgets, and provenance.
- **Zone**: a bounded, semantically named area inside one persistent Unreal
  map; it is not a mandatory separate level or process.
- **Portal**: an authored zone connection with dimensions, direction,
  collision policy, and navigation probes.
- **Scenario overlay**: a resettable, data-only set of object/agent states
  linked to VISTA scenario IDs.
- **World session**: the M3 runtime owner of world revision, lease, state
  generation, monotonic time, commands, and checkpoints.
- **Episode**: an optional recording or evaluation window of any duration; it
  never controls world lifetime.
- **Hero prop**: a visibly identifiable, purpose-specific object with a stable
  semantic ID, non-proxy silhouette/detail, bound material, explicit collision
  role, plausible placement, and a fixed review view.
- **Validated PBR set**: base-color (sRGB), normal (linear; convention recorded
  and converted for Unreal), and roughness (linear), or a documented packed
  equivalent, with UV coverage, digest, license/provenance, and verified Unreal
  material binding.

## Evidence Baseline

- Dataset taxonomy:
  `/home/yhliu/VISTA/vista_frontend_new/public/dataset-visualization/assist-step-dashboard.json`
- Scenario provenance:
  `/home/yhliu/VISTA/docs/project_management/scenario_pairing_manifest_2026-05-07/scenario_pairing_manifest.json`
- Accepted legacy source branch:
  `codex/vista-blender-world@1dfff7f67a426e57c5c632b01784d32f471cd92d`
- Immutable legacy bundle root:
  `/mnt/NAS2/yhliu/SimWorldStudio/vista-blender-world/runs/20260811T-procedural-chair-upgrade-r3/blender`
- Legacy manifest:
  `manifest.json`, SHA-256
  `1c1008ebcd3b9cb6f130a54f65e04de530e81f3a6954c042e43f237fbe35cc55`
- Legacy GLB:
  `vista_mmg040_office.glb`, SHA-256
  `c3d67a34f0f0bd720133dc8ce08c7bb52b0c3008386aec04957014d76010e759`
- Historical map evidence only: attempt-07 map
  `/Game/VISTA/Scenes/MMG040_Office_BlenderR1`, SHA-256
  `8dd416ca31f66cbdbbdc60c1ef2c21c8bdf7d83b1abe36e177f202bc01e8d9f4`.
  The new pack reimports the immutable r3 bundle into a fresh project; it does
  not depend on the live attempt-11 project.
- Current engine lane: UE 5.3.2 `UnrealEditor`/`UnrealEditor-Cmd` with Python,
  Interchange, and Editor Pixel Streaming. RunUAT, UBT/UHT, Engine Source, and
  a cooking toolchain are absent.

## Assumptions

- The first authored topology is a compact office/campus living lab because it
  reuses the verified office and supports VISTA's work, food, learning, public,
  and mobility settings.
- One persistent map per pack is the initial model. Runtime hot-switching is
  deferred beyond M3; a different pack is selected before launch.
- Generated content and disposable UE projects live on NAS because the root
  filesystem is approximately 96% full.
- The live attempt-11 runtime, Blender MCP session, older demo, GPU 1, and their
  ports remain untouched.
- GPU 0 is only used after a fresh render preflight and explicit runtime
  ownership authorization.
- External assets require source, digest, license, redistribution status, and
  explicit download authorization. Paid model/API calls are separately gated.

## Requirements

### R1 — Continuous world lifetime and lifecycle truth

WHEN an M1a-M2 editor world is ready THEN its owned Unreal process SHALL remain
alive until an explicit owned stop, process failure, or administrator
termination. A browser disconnect and any episode duration SHALL NOT stop it.

WHEN M3 is implemented THEN WorldSession SHALL use `booting`, `running`,
`resetting`, `stopping`, `stopped`, `failed`, and `quarantined`; reset SHALL
return to `running`. Only stop, failure, or administrator termination ends the
session. Runtime switching is not part of this state machine.

Acceptance notes:

- No world timeout is derived from 12 seconds.
- M1a-M2 may provide immutable-baseline process restart, but may not claim
  in-process reset, state generations, or checkpoint semantics.
- M3 owns a monotonic session clock independent of episode clocks.

### R2 — Strict world-pack contracts

WHEN a pack, overlay, plan, or receipt is accepted THEN it SHALL validate
against a versioned closed JSON schema and bind stable IDs, revisions, seeds,
exact inputs, transforms, budgets, provenance, and tool hashes.

Acceptance notes:

- Unknown fields, duplicate IDs, non-finite transforms, traversal/path
  injection, invalid namespaces, disconnected required zones, unbound assets,
  and digest mismatches fail closed.
- Contracts contain data only; executable Python/shell is forbidden.
- Machine paths and timestamps do not participate in deterministic content
  digests.
- Validators and generic tools cannot hard-code eight zones, Living Lab IDs,
  or the initial scenario list; those are properties of the first pack, not an
  architectural ceiling.

### R3 — Connected world coverage

`vista_living_lab_r1` SHALL declare this complete eight-zone target:

1. office/storage/meeting/print/pantry;
2. lobby/hallway/elevator/stair circulation;
3. cafeteria/teaching kitchen/coffee;
4. classroom/library;
5. lab/workshop;
6. outdoor quad/paths/seating/athletic edge;
7. parking/drop-off/crosswalk/transit edge;
8. convenience store/commons.

Acceptance notes:

- M1a accepts only three named slices across zones 1-3: the legacy office core,
  main corridor/lobby, and teaching-kitchen/café. It does not claim the
  meeting/print/pantry, elevator/stair, or full cafeteria subspaces. M1
  completes zones 1-5, and M2 completes all eight; the eight-zone graph exists
  from M0.
- Every accepted zone has stable bounds and anchors, a fixed review view, a
  semantic inventory, and at least one navigation probe.
- Every required portal has paired endpoints and capsule-clearance rules.
- Spawn-to-anchor and portal graphs are connected without teleportation.

### R4 — Early visible value

WHEN M1a is accepted THEN one uncut 60-90 second traversal SHALL go from the
office desk through corridor/lobby into the teaching kitchen/café, perform one
typed interaction, turn around, and walk back.

Acceptance notes:

- The traversed route is at least 40 metres and crosses two zone boundaries in
  both directions without teleportation or live editing.
- A sanitized on-screen overlay shows current zone, position, and elapsed
  session time, visibly continuing beyond 12 seconds.
- Evidence includes six fixed frames: office spawn wide; office/hall threshold;
  corridor/lobby scale view; kitchen threshold with connector visible;
  kitchen hero before interaction; and the same camera after interaction.
- Nav-path and interaction-state receipts correlate to the traversal.

### R5 — VISTA grounding without oracle leakage

WHEN a zone or overlay is authored THEN it SHALL cite the taxonomy and scenario
IDs motivating its spaces, props, affordances, or states without copying oracle
labels, review notes, or evidence atoms into restricted agent inputs.

Initial evidence cases are `mmg_040`, `mmg_047`, `mmg_072`, `mmg_075`,
`mmg_087`, `mmg_037`, `mmg_027`, and `mmg_055`. Geometry SHALL remain reusable
across scenarios and assistance modes.

### R6 — Reproducible modular Blender bundles

WHEN a bundle is built twice from clean state with the same pinned inputs THEN
its semantic inventory, topology, materials, bounds, placements, and content
digest SHALL match.

Acceptance notes:

- Bundle geometry uses local coordinates; the pack owns world placement.
- Builders, Blender/dependency versions, seed, source hashes, outputs,
  previews, and validation results are recorded.
- The legacy MMG040 generator and historical receipts remain immutable.

### R7 — Objective visual and asset-quality gates

WHEN accepted views are reviewed THEN spaces SHALL be visibly purpose-specific
and finished rather than fallback geometry.

Acceptance notes:

- Visible hero objects cannot use `/Engine/BasicShapes/*`, error materials, or
  untextured proxy cubes. Tagged hidden collision/support geometry is allowed.
- M1a requires at least 3 validated PBR sets and 6 hero props; M1 requires 5
  PBR sets and 8 hero props; M2 requires 8 PBR sets and 12 hero props.
- Each counted PBR set passes texture-role, color-space, normal-convention,
  resolution/UV, digest/provenance, and Unreal-binding checks.
- Each counted hero prop passes the full definition above and appears in a
  fixed review camera.
- Per-zone review scores five binary criteria: purpose legibility, plausible
  scale/placement, sufficient prop density, coherent material identity, and
  usable lighting. Every criterion must pass; failures are documented rather
  than averaged away.
- No black/checker frames, gross floating objects, structural penetrations,
  duplicate global light rigs, or warning text appear in accepted evidence.

### R8 — Capability spike and revision-repeatable Unreal composition

BEFORE bulk zone authoring THEN a tiny two-bundle fixture SHALL prove bitmap
PBR import/material binding, namespace isolation, explicit collision policies,
NavMesh build/path query, and performance telemetry in the pinned UE lane.

WHEN a world revision is composed THEN generic tools SHALL re-import and save a
fresh revisioned namespace and map without mutating source projects, prior
revisions, or historical receipts.

Acceptance notes:

- Namespace:
  `/Game/VISTA/WorldPacks/<PackId>/<Revision>/{Assets,Maps}/...`.
- Collision policy is semantic-role-specific, never blanket `BlockAll`.
- Every required portal admits the configured capsule with margin and has an
  authoritative, non-partial UE NavMesh path.
- The source project/map and external dependency hashes remain unchanged.
- A failed import/save is quarantined and cannot replace an accepted revision.

### R9 — Scenario overlays

WHEN a scenario overlay is resolved THEN it SHALL target stable semantic
object IDs using an allowlisted operation and separate VISTA provenance from
the runtime payload.

Acceptance notes:

- Initial operations cover object presence/transform, door or appliance state,
  movable prop state, and hazard/spill enable/disable.
- M1a-M2 validate overlays offline and demonstrate at least one source-controlled
  overlay through a fresh isolated launch/restart; they do not claim live
  transactional switching.
- M3 supports apply, clear, baseline restore, state-generation checks,
  idempotency keys, stale-generation rejection, and no state leakage.
- Overlay payloads contain no oracle answers, review notes, arbitrary scripts,
  or unrestricted asset paths.

### R10 — Typed interactions and authoritative reset

WHEN the user or agent mutates M3 state THEN it SHALL issue an allowlisted
typed command to a stable object UUID with expected state generation and an
idempotency key.

Acceptance notes:

- Types include door open/close/lock, pickup/drop, appliance on/off/open/close,
  movable prop, hazard enable/disable, teleport-to-authored-anchor, and
  read-only inspection.
- M1a proves at least one bounded fixed-code door or appliance interaction;
  M1/M2 may add more preview interactions without claiming M3 authority.
- M3 checkpoints include stable IDs, transforms, velocity/sleep state,
  door/appliance state, controller state, task state, and RNG state.
- 100 reset cycles and 100 door cycles create no duplicate actors or stale
  state. Outcome-unknown mutations quarantine the session.
- Generic `execute_python_script` is not a Production surface.

### R11 — Remote interactive delivery

WHEN a milestone is exposed THEN Studio, UE, and MCP listeners SHALL remain
loopback-only and be reached from the Mac through authenticated SSH tunnels.

Ready requires authorized Studio access, advancing decoded frames, UE/MCP
connectivity, pawn possession, keyboard movement and key-up stop, mouse yaw,
and a correlated typed interaction. Tokens and browser network logs are absent
from traces. Closing the browser SHALL NOT stop the owned world process.
Any new world-selection UI SHALL use a restrained professional control-room
style and avoid assistant chat bubbles, novelty gradients, anthropomorphic AI
language, or decorative "AI" branding.

### R12 — Measured performance and endurance

WHEN M1a-M2 are accepted THEN results SHALL be labeled `editor-PIE dev-profile`
and use pinned GPU/stream/render settings. The harness SHALL sample:

- UE server frame time at least once per second after a five-minute warmup;
- UE process RSS/PSS and GPU memory once per minute;
- browser decoded-frame progress at least every 15 seconds;
- hitches above 100 ms and freezes where decoded frames do not advance for
  more than two consecutive seconds.

Absolute dev-profile ceilings are:

| Gate | M1a | M1 | M2 |
| --- | ---: | ---: | ---: |
| Saved actors | 1,500 | 3,500 | 6,500 |
| Imported static triangles | 12 M | 30 M | 60 M |
| Material instances | 400 | 900 | 1,600 |
| Collision bodies | 1,200 | 3,000 | 6,000 |
| Import wall time | 30 min | 45 min | 90 min |
| NavMesh build wall time | 5 min | 10 min | 20 min |
| Cold launch to advancing frames | 20 min | 25 min | 35 min |

Counts use the reloaded saved map: actors are all saved actors; triangles are
the sum of LOD0 triangles across unique imported static-mesh assets; materials
are unique saved material instances; collision bodies are saved
collision-enabled primitive-component instances. Import time is commandlet
start-to-exit, NavMesh time is build request-to-successful path query, and cold
launch uses an empty per-run cache whose path and initial state are recorded.

At 1280x720@30 after warmup, server-frame p95 SHALL be at most 33.3 ms,
p99 at most 50 ms, hitches under one per minute, and freeze time under 1%.
M1a runs a 10-minute smoke; M1 and M2 run 30 minutes. Between minutes 10 and
30, M1/M2 RSS/PSS and GPU-memory growth SHALL each remain below both 10% and
512 MiB. M3 runs eight hours and applies the same growth bound from the end of
hour one; M4 runs 24 hours. Pack-authored budgets may be stricter but never
weaken these ceilings.

### R13 — Security, storage, ownership, and permission gates

WHEN a tool or runtime is started THEN it SHALL use named tmux sessions, owned
loopback ports, an explicitly assigned GPU, mode-0600 tokens, and append-only
run roots on NAS.

Approval of this spec authorizes source-code changes only. Each of the
following remains a separate recorded apply gate:

1. external asset download or redistribution;
2. every Blender executable or generation invocation, including tiny fixtures;
3. Unreal commandlet import/composition or project/map mutation;
4. live GPU/Pixel Streaming launch and bounded acceptance run;
5. paid model/API use, upload, deploy, merge, or public exposure.

Code, schemas, tests, dry-run plans, hashes, and preflight checks may be
prepared before the relevant apply gate. Existing attempt-11, Blender MCP,
older demo, GPU 1, their ports, production port 8000, canonical datasets, and
historical receipts SHALL remain untouched.

### R14 — Backward compatibility and rollback

WHEN the generic path is added THEN the accepted MMG040 scripts, schemas,
receipts, launcher `--map` mode, and historical evidence SHALL continue to
validate without mutation.

The read-only legacy adapter binds the exact r3 manifest/GLB above and rebuilds
into a fresh namespace. It never rewrites v1 evidence or reads live attempt-11
as a build input. Failures remain quarantined in their new attempt directories.

### R15 — Evidence, Git publication, and handoff

WHEN a live milestone is evaluated THEN the source SHALL first be a clean,
local candidate commit. The runtime and external append-only evidence manifest
SHALL bind that candidate commit plus engine, pack revision, inputs, outputs,
process identities, tests, metrics, views, and limitations.

After acceptance, a separate focused evidence/spec commit may reference the
candidate commit and external manifest; it need not recursively hash itself.
Only then is the collaboration branch pushed. Runtime artifacts, `.umap`, PBR
payloads, traces, tokens, and generated projects are never committed unless a
separate artifact policy allows them.

## Important Edge Cases

- Two bundles export the same mesh/material name.
- A portal aligns geometrically but has no authoritative NavMesh path.
- Decorative collision fragments navigation.
- A command times out after Unreal mutates state but before acknowledgement.
- Reset leaves a second pawn, stale physics body, or overlay state.
- A PBR dependency changes license or digest.
- Pixel Streaming is connected while frames freeze or input is ignored.
- Shader/DDC warmup exceeds its bounded readiness window.
- Editor save crashes and leaves an ambiguous package.
- One child process exits while unrelated live sessions remain healthy.

## Approval

- Requested by: Codex `/root`
- Scope of approval: source-code implementation only; R13 apply gates remain
  separate.
- Approved by: pending user approval
- Date: pending

# Design: VISTA Playable Home

Status: Approved for implementation
Updated: 2026-08-15
Depends on: `requirements.md`

## Summary

VISTA Playable Home is a persistent semantic house compiled into one Unreal
map. The base house is immutable for a revision. Verified VISTA cases are
data-only EventSpec overlays that change initial object states, goals, and
triggers without rebuilding the environment.

The VISTA World Agent is the domain planner. SimWorld validates and compiles
its typed HouseSpec/EventSpec operations. Blender supplies deterministic room
bundles and missing assets. Unreal owns gameplay, physics, navigation, NPC
execution, event state, and the player-visible game surface.

## Architecture and Flow

```text
Natural-language request
          |
          v
VISTA World Agent (semantic planner)
          |
          v
HouseSpec v1 + EventSpec v1 + typed action plan
          |
       strict validation -----------------------------+
          |                                            |
          v                                            v
World resolver/compiler                    asset registry / Blender forge
          |                                            |
          +-------------- immutable build plan <------+ 
                                  |
                                  v
                    revisioned Unreal composition
                                  |
                                  v
       BP/C++ gameplay kernel + NavMesh + event runtime
                                  |
               +------------------+------------------+
               v                                     v
       Pixel Streaming                         game-only X11 window
                                                     |
                                             Sunshine / Moonlight
```

## Base World Topology

The r1 floor plan is a compact residence/living-lab with one circulation
spine. Dimensions are authored, not inferred as VISTA ground truth.

```text
                         +----------------------+
                         | bathroom / laundry   |
                         +----------+-----------+
                                    |
+-------------+   +-----------------+----------------+   +-------------+
| bedroom     +---+ entry / hall                     +---+ office      |
+-------------+   +----------+--------------+--------+   +-------------+
                             |              |
                    +--------+-----+  +-----+----------+
                    | living room  +--+ kitchen/dining |
                    +--------------+  +----------------+
```

Every connection is a `PortalSpec`, even when it is an open arch. This makes
door state, capsule clearance, navigation, and room-transition evidence
explicit.

## Contracts

### HouseSpec v1

Schema ID: `simworld.vista.playable-house/v1`

Top-level fields:

- `schema_version`, `house_id`, `revision`, `seed`, `units`;
- `rooms`: stable ID, semantic kind, bounds, anchor, review views;
- `portals`: paired rooms, transform, clearance, door entity, nav policy;
- `entities`: stable ID, room, asset reference, transform, tags, mobility,
  collision, affordances and initial state;
- `relations`: `inside`, `supports`, `contains`, `adjacent_to`;
- `runtime_profile`: player start, pawn, game mode, interaction distance,
  navigation agent, NPC profiles and remote surface;
- `budgets` and `provenance`.

The validator is generic: it cannot hard-code six rooms or a particular event
list. The r1 fixture supplies those values.

### EventSpec v1

Schema ID: `simworld.vista.playable-event/v1`

An event contains:

- public `event_id`, title, compatible house revision, participating room and
  entity IDs;
- ordered, allowlisted initial-state operations;
- public goals, triggers, success/failure conditions, timeout and reset policy;
- sanitized source references and a private evaluation reference stored
  separately from the public runtime payload;
- deterministic digest.

Initial operations are restricted to `spawn_fixture`, `set_transform`,
`set_state`, `set_visibility`, `set_portable`, `set_npc_queue`, and
`set_goal`. Runtime actions are the affordances declared by HouseSpec.

### Build plan

Schema ID: `simworld.vista.playable-home-build-plan/v1`

The compiler resolves room-local transforms into world centimetres, checks
portal connectivity, binds assets and collision policies, expands event
targets, freezes exact source/tool hashes, and emits deterministic Unreal
instructions. Timestamps and host paths are receipt metadata, not digest
inputs.

## Stable Identity and State

Examples:

```text
home.r1/room.kitchen_dining
home.r1/portal.entry_hall-living_room.01
home.r1/room.living_room/entity.keys.01
home.r1/room.bedroom/entity.phone.01
home.r1/room.kitchen_dining/entity.stove.01
```

State is split into:

1. immutable base entity definition;
2. baseline runtime state;
3. active EventSpec overlay state;
4. transient gameplay state such as held object or NPC path progress.

Reset discards layers 3-4 and restores layer 2. It never regenerates Blender
geometry.

## Blender Asset Pipeline

New code lives under `tools/blender/vista_playable_home/` and is headless-first.
It creates a modular 10 cm grid house with room shells, door openings, floors,
ceilings, trim, purpose-specific furniture, lighting proxies, collision roles,
and semantic custom properties. It saves a `.blend`, exports `.glb`, writes a
normalized manifest, and renders fixed previews.

Import granularity is role-aware:

- room shell and static furniture assemblies are merged by room/material;
- repeated decoration uses instance groups;
- doors, drawers, pickups, appliances and chairs remain distinct;
- detail meshes use `NoCollision`;
- pickups use convex `PhysicsActor` collision;
- walls/floors and furniture use coarse authored collision.

The accepted MMG040 office remains a sealed reusable source. Its one-room
script is not expanded into a monolithic house generator.

The procedural forge is also the stable gameplay/collision layer; its first
preview is intentionally a semantic blockout, not the final realism claim. A
separate visual-binding pass may replace presentation meshes without changing
entity IDs, transforms, affordances or collision actors. Revision r1 uses the
already-local HSSD catalog for this pass:

- deterministic category-to-source selection with exact GLB hashes;
- axis, floor origin and target-bound normalization in pinned Blender 4.5.8;
- retained embedded PBR materials and texture slots;
- one primary presentation mesh per logical asset for strict UE import;
- a CC BY-NC 4.0 attribution manifest and procedural fallback when a visual
  binding is unavailable.

Room shells, door kinematics, hazards and collision proxies remain authored by
the project. Visual meshes never redefine navigation or event semantics.

## Unreal Gameplay Kernel

The target compiled plugin is `VistaPlayableHome`. It contains:

- `AVistaPlayableHomeGameMode` and a third-person default pawn;
- `UVistaInteractionComponent` for local trace, focus and typed interaction;
- `IVistaInteractable` with state/result structs;
- `AVistaPickupActor` using a held-item attachment or PhysicsHandle and
  authoritative drop/place;
- `AVistaDoorActor` with bounded open/close state and obstruction handling;
- `AVistaStatefulApplianceActor` for stove/washer/faucet-style toggles;
- `AVistaHomeNpcCharacter` and `AVistaHomeNpcController` for NavMesh action
  queues;
- `UVistaEventSubsystem` for compatible overlay apply/reset and event status;
- a minimal professional HUD for interaction prompt, held item, room and event.

The host now has a complete UE 5.7.3 toolchain (RunUAT, UBT, UHT, headers and
editor binaries), so r1 builds the `VistaPlayableHome` C++ plugin and installs
it into a fresh content-only project. The same contract is also represented by
a deterministic Unreal Editor composition script for import, map creation and
reload verification. Any compile or runtime failure is retained as a blocker
receipt rather than simulated in the web UI.

## Agent and Server Interfaces

The server owns two separate surfaces:

- authoring operations change a revisioned HouseSpec and require compilation;
- live operations address a running revision and mutate only declared state.

Recommended endpoints:

```text
POST /api/vista-world/compile
GET  /api/vista-world/revisions/:revision
POST /api/vista-world/sessions
GET  /api/vista-world/sessions/:id
POST /api/vista-world/sessions/:id/actions
POST /api/vista-world/sessions/:id/events/:eventId/start
POST /api/vista-world/sessions/:id/events/reset
```

All writes require the current revision/session generation. The server maps
NLP output to the same typed action enum used by Unreal; it does not forward
free-form Python or Blueprint code.

Placement anchors use one wire-safe semantic identity across Node and Unreal:
`<entity_id>/anchor.<anchor_id>`. The source HouseSpec may retain the compact
`<entity_id>#<anchor_id>` value in baseline `placed_at` state, but the compiler
and composer materialize the wire identity as a tagged TargetPoint. A `place`
action therefore resolves an exact anchor component instead of silently using
the parent actor origin.

## Unreal Composition

Generated revision paths use:

```text
/Game/VISTA/PlayableHome/<Revision>/Assets/...
/Game/VISTA/PlayableHome/<Revision>/Maps/VistaPlayableHome
```

Composition order:

1. verify build plan and exact bundle digests;
2. import into a new revision namespace;
3. create materials and role-based collision;
4. place room assemblies, independent gameplay actors and stable-ID tags;
5. place PlayerStart, navigation bounds/links, room anchors and NPC;
6. assign GameMode/map defaults in the disposable project;
7. build navigation, save, reload, query connectivity and write a receipt.

## Remote Runtime

The launcher adds a `playable-home` profile and never reuses reserved live
ports. It supports:

- preview: `UnrealEditor <project> <map> -game`, visible on an owned X11
  display and optionally Pixel Streamed;
- distribution: cooked Development executable once the full UE toolchain is
  present;
- Sunshine application: a named `VISTA World` entry that invokes a fixed
  wrapper, waits for the game window, and stops only its owned process group.

The preflight is read-only and reports encoder, display, capture window,
Tailnet reachability, `/dev/uinput`, `/dev/uhid`, and direct/DERP path. Missing
input permission marks Moonlight view-only but does not disable browser input.

## Failure Handling and Rollback

- Schema, graph, digest or provenance failure prevents plan creation.
- Blender failure leaves only a quarantined new run directory.
- Import/composition failure never edits an accepted namespace.
- Event apply validates every target before committing any operation.
- Interaction/NPC commands have bounded timeouts and structured failure codes.
- Baseline restart remains the recovery path until compiled in-process reset is
  live-proven.
- Legacy runtime and production port 8000 are not stopped or reconfigured.

## Testing Strategy

- JSON Schema meta-validation and negative fixtures.
- Deterministic compiler golden tests, transform/connectivity tests and
  oracle-leakage tests.
- Blender static unit tests plus two clean headless builds, GLB inspection,
  manifest comparison and nonblank previews.
- Unreal source contract tests and mocked composition tests before any UE run.
- Disposable UE import/compose/reload/NavMesh smoke on an isolated lane.
- Browser input and game-only window checks.
- Sunshine preflight plus Moonlight view/control classification.
- One retained uncut traversal and three event start/reset receipts.
- Existing MMG040 asset/runtime and Studio frontend tests remain green.

## File Plan and Ownership

- Coordinator: `docs/specs/vista-playable-home/**`, runtime integration,
  Sunshine wrapper/config generator, final merge and evidence.
- Contracts worker: `world_packs/schemas/**`,
  `world_packs/vista_playable_home_r1/**`, `tools/worlds/**`, focused tests.
- Blender worker: `tools/blender/vista_playable_home/**` and focused tests.
- Unreal worker: `unreal_plugins/VistaPlayableHome/**`,
  `tools/ue/vista_playable_home/**` and focused tests.

No worker owns production configuration, canonical VISTA data, historical
receipts, current live tmux sessions, or another worker's paths.

## Rollout and Observability

1. Land schemas/compiler and code-only tests.
2. Land Blender/Unreal source and dry-run validation.
3. Generate one sealed bundle in a fresh append-only run.
4. Compose one disposable Unreal project on an isolated GPU/port profile.
5. Accept Pixel Streaming game input and three event overlays.
6. Add the Sunshine application and validate Mac access.
7. Cook a Development build only after the toolchain preflight passes.

Every phase writes a machine-readable receipt containing source commit,
profile, inputs, outputs, results and residual blockers.

## Traceability

- R1, R3, R9, R10 -> runtime profile, launcher and acceptance harness.
- R2, R6-R8, R11 -> schemas, resolver/compiler and fixtures.
- R4-R5, R9 -> gameplay plugin and Unreal composition.
- R8 -> deterministic Blender forge and import contract.
- R12-R13 -> validation, evidence, legacy regression and isolated rollout.

## Approval

- Requested by: user in the VISTA/SimWorld Codex task
- Approved by: user, "好的請你幫我做好"
- Date: 2026-08-15

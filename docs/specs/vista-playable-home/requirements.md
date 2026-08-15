# Requirements: VISTA Playable Home

Status: Approved for implementation
Updated: 2026-08-15

## Problem

The current VISTA/SimWorld stack proves a detailed Blender-authored office, a
loopback Unreal Pixel Streaming lane, and a separate NLP-driven Studio lane.
It does not yet provide one continuous game-like indoor world. A user cannot
launch a clean game view, walk between household rooms, open doors, carry
objects, observe an autonomous person, or start and reset verified VISTA
events from the same session.

The desired product is not an Unreal Editor demonstration. It is a persistent,
third-person indoor simulation that uses Unreal as the runtime engine while
exposing a game surface through Pixel Streaming or Sunshine/Moonlight.

## Goals

- Deliver one continuous, single-floor home/living-lab with an entry/hall,
  living room, kitchen/dining area, bedroom, office, and bathroom/laundry.
- Make the world immediately playable with a third-person character, camera,
  collisions, doors, object pickup/drop, and one autonomous NPC.
- Compile verified VISTA situations into resettable scenario overlays rather
  than regenerating the base home for every event.
- Let the VISTA World Agent plan semantic world changes while SimWorld applies
  only typed, validated actions to the same runtime used by the human player.
- Preserve deterministic Blender generation, stable semantic IDs, revisioned
  Unreal composition, provenance, and evidence.
- Provide a game-only remote surface suitable for a Mac using Tailscale and
  either browser Pixel Streaming or Sunshine/Moonlight.

## Non-goals

- Reproduce GTA's city scale, combat, driving, economy, or full animation set.
- Reconstruct exact room dimensions from VISTA videos when the dataset does
  not provide a canonical floor plan.
- Generate one Unreal map per VISTA sample.
- Expose arbitrary Unreal Python, shell, Blueprint bytecode, or filesystem
  writes through the agent action API.
- Make Postgres/Qdrant or paid generative asset services prerequisites for the
  first playable revision.
- Claim a cooked Shipping build before a matching full UE 5.3.2 build
  toolchain is available.
- Modify production port 8000, canonical VISTA datasets, accepted historical
  receipts, or the existing live GPU-1 sessions.

## Assumptions and Approved Waivers

- The user approved this architecture and requested implementation on
  2026-08-15. This approval covers code, tests, deterministic local Blender
  generation, disposable Unreal project/map writes, and an isolated local GPU
  validation lane after preflight.
- External downloads, paid APIs, uploads, deployment, production changes, and
  public exposure remain separate gates.
- The first interaction model may use one held-item slot instead of a complete
  inventory. It must still support carrying an item between rooms and placing
  or dropping it.
- Until RunUAT/UBT/UHT and matching engine headers are provisioned, an
  `UnrealEditor -game` process may satisfy the game-only preview requirement.
  A cooked Development executable remains the distribution target.
- Generated artifacts and disposable UE projects belong on a new append-only
  NAS run root; source code, contracts, fixtures, and small previews belong in
  Git.

## Requirements

### R1 — Persistent playable world

WHEN the VISTA Playable Home runtime becomes ready THEN the world SHALL remain
alive until an owned explicit stop, process failure, or administrator action.
A browser disconnect, Moonlight disconnect, or scenario duration SHALL NOT end
the world.

Acceptance notes:

- No 12-second world-lifetime assumption is allowed.
- The base revision and active overlay are reported independently.
- A clean baseline restart is available even before in-process checkpointing.

### R2 — Connected household topology

WHEN revision `vista_playable_home_r1` is resolved THEN it SHALL contain six
semantically named zones: `entry_hall`, `living_room`, `kitchen_dining`,
`bedroom`, `office`, and `bathroom_laundry`.

Acceptance notes:

- Every zone has stable bounds, a room anchor, at least one review camera, and
  a semantic inventory.
- Portals declare the two connected zones, clear width/height, door state, and
  navigation policy.
- The PlayerStart-to-room-anchor graph is connected without teleportation.
- The floor plan is single-floor for revision r1; schemas do not hard-code
  this limitation for future packs.

### R3 — Third-person game feel

WHEN a human enters the runtime THEN the system SHALL provide an over-shoulder
third-person pawn with keyboard/mouse locomotion and a camera that can traverse
the complete home without exposing Unreal Editor chrome.

Acceptance notes:

- Required controls are move, look, jump, sprint, crouch, and interact.
- An optional first-person camera is allowed but does not block r1.
- Collision must not trap the pawn on decorative detail or room thresholds.
- Input release is verified so remote keys cannot remain stuck.

### R4 — Typed object interaction

WHEN the player or an authorized agent targets an interactable entity THEN the
runtime SHALL support the entity's allowlisted affordances and return its
resulting state.

Acceptance notes:

- r1 affordances are `open`, `close`, `pick_up`, `drop`, `place`, `toggle`,
  `sit`, and `inspect`; an entity declares only the subset it supports.
- At least two doors and three portable hero objects are interactable.
- A portable object can be carried across a portal and released with physics
  or placed at a stable anchor.
- Door collision and navigation state agree with open/closed state.
- Failed interactions are explicit and do not partially mutate state.

### R5 — Autonomous indoor agent

WHEN the runtime starts with the NPC feature enabled THEN at least one
humanoid NPC SHALL navigate between room anchors and execute a bounded action
queue.

Acceptance notes:

- Required typed actions are `navigate_to`, `look_at`, `pick_up`, `place`,
  `open_door`, `close_door`, `sit`, `wait`, and `speak`.
- r1 may use waypoint patrol plus queued actions; a general planner is not
  required inside Unreal.
- Navigation failure times out and reports the blocked target rather than
  hanging the session.

### R6 — VISTA scenario overlays

WHEN a verified scenario is selected THEN the system SHALL apply a data-only
overlay to the persistent home, expose its public initial state and goals, and
restore the clean baseline on reset.

Acceptance notes:

- r1 ships verified fixtures for `mmg_001` (stove left on), `mmg_044`
  (forgotten keys), and `mmg_045` (forgotten phone).
- The schema also represents `mmg_013` (slipper/fall/spill), `mmg_021`
  (running bath), `mmg_040` (rolling-chair fall), and `mmg_070` (washer not
  started) without changing the base-house schema.
- Overlay operations target stable semantic IDs and are deterministic,
  idempotent, revision-compatible, and resettable.
- `oracle_assistance_required`, review notes, and private evidence atoms never
  enter restricted agent inputs or public runtime payloads.

### R7 — NLP-to-world typed control

WHEN the VISTA World Agent receives a supported natural-language request THEN
it SHALL produce a validated HouseSpec/EventSpec operation plan, and SimWorld
SHALL execute only typed operations against the declared revision.

Acceptance notes:

- Minimum world-authoring tools are `create_house`, `add_room`,
  `connect_rooms`, `place_asset`, `move_entity`, `set_interactable`,
  `set_entity_state`, `validate_navigation`, and `save_scene_revision`.
- Minimum live-action tools are the actions in R4 and R5 plus `start_event`
  and `reset_event`.
- Unknown fields, unknown IDs, stale revisions, invalid transforms, and
  unsupported affordances fail closed.
- No arbitrary `execute_python_script` or caller-authored Blueprint graph is
  accepted as a live-action fallback.

### R8 — Reproducible assets and semantic identity

WHEN a Blender house bundle is built twice from the same pinned inputs THEN
its normalized manifest, semantic inventory, topology, material assignments,
and content digest SHALL match.

Acceptance notes:

- Stable IDs use the form
  `home.r1/room.<room>/entity.<category>.<ordinal>` and
  `home.r1/portal.<room-a>-<room-b>.<ordinal>`.
- GLTF extras or the companion manifest bind `room_id`, `category`,
  `component_role`, `affordances`, `mobility`, `collision_policy`,
  `nav_obstacle`, and optional `instance_group`.
- Static decorative detail is merged or instanced where practical; screws and
  trim do not become blocking actors.
- Walls/floors, furniture, decoration, pickups, and doors use distinct
  collision policies.
- The first revision has visibly differentiated rooms and a nonblank preview;
  basic geometry may be used for hidden collision but not as every hero prop.

### R9 — Revisioned Unreal composition

WHEN a validated build plan is composed THEN Unreal SHALL create a fresh
revision namespace and map containing the room bundles, portals, stable entity
tags, PlayerStart, lighting, collision, navigation bounds, gameplay actors,
and scenario anchors.

Acceptance notes:

- Source bundles and previously accepted revisions are immutable.
- Import or composition failure quarantines the new attempt and never falls
  back silently to an older map.
- The saved map is reloaded before acceptance.
- `GameDefaultMap`, default GameMode, and default pawn point to the playable
  home in the disposable project or packaged target.

### R10 — Game-only remote operation

WHEN the user launches the demo remotely THEN the primary surface SHALL be the
Unreal game view rather than Chromium Studio or Unreal Editor UI.

Acceptance notes:

- Before cooking is possible, launch uses `UnrealEditor -game` on the owned
  display and capture surface; after toolchain provisioning it uses a cooked
  Development executable.
- Sunshine has a named `VISTA World` application entry with deterministic
  start/stop behavior.
- Browser Pixel Streaming remains a fallback and uses loopback signalling.
- Preflight reports Tailscale reachability, Sunshine encoder readiness,
  capture display, game window visibility, `/dev/uinput` and `/dev/uhid`
  permissions, and direct-versus-DERP connectivity without changing them.

### R11 — Security, provenance, and rollback

WHEN any contract, asset, event, or runtime command is accepted THEN the
system SHALL bind its revision and provenance while excluding secrets and
unsafe executable payloads.

Acceptance notes:

- Contracts are closed JSON schemas and reject path traversal, non-finite
  transforms, duplicate IDs, digest mismatches, and executable fields.
- No token, Tailnet credential, model secret, or absolute private dataset path
  is returned to the public client.
- Every mutable live command has a bounded timeout, result, and rollback or
  reset behavior.

### R12 — Observable acceptance evidence

WHEN r1 is claimed playable THEN a retained evidence bundle SHALL prove the
exact source commit, inputs, map revision, runtime profile, controls, visual
output, navigation, interaction state, event reset, and remote surface.

Acceptance notes:

- The uncut traversal visits every room, opens/closes a door, carries one
  object between rooms, and observes the NPC moving between anchors.
- Three VISTA overlays start and reset without restarting the base build.
- Required fixed views are nonblack and show purpose-specific room identity.
- The first performance target is a stable 1280x720 stream with advancing
  frames and no persistent freeze; measurements are reported rather than
  fabricated when 60 fps is not achieved.

### R13 — Compatibility and isolation

WHEN this work is implemented THEN existing MMG040 assets, legacy launch mode,
Studio API behavior, production port 8000, live GPU-1 processes, canonical
datasets, and append-only evidence SHALL remain unchanged unless a later
explicit migration is approved.

Acceptance notes:

- New tests include legacy regression coverage.
- Development uses a dedicated worktree, disposable project/run root, owned
  GPU and non-overlapping ports.
- Source commits never include generated UE projects, `.uasset`/`.umap`
  outputs, secrets, or runtime logs.

## Edge Cases

- A door closes while a pawn or object occupies its sweep: closing is rejected
  or reverses without trapping the actor.
- A held object crosses a portal or the player disconnects: ownership remains
  server-authoritative and reset can recover it.
- An NPC path is blocked by a closed door: the queue reports blocked or opens
  the door only when its action list authorizes that affordance.
- An overlay references an entity absent from the selected revision: apply
  fails atomically.
- Moonlight input permission is missing: preflight reports view-only status
  and the browser-input fallback remains available.
- A matching cook toolchain is absent: game-only preview is allowed, but the
  build is not labeled packaged or Production.

## Open Questions

No question blocks r1. Character hand IK, ragdoll quality, liquid simulation,
multi-floor homes, multi-NPC social behavior, and public Internet WebRTC are
follow-up milestones with separate evidence gates.

## Approval

- Requested by: user in the VISTA/SimWorld Codex task
- Approved by: user, "好的請你幫我做好"
- Date: 2026-08-15

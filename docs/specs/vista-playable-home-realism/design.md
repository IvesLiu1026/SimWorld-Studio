# Design: VISTA Playable Home Realistic Interior

Status: Draft — approval required before implementation
Updated: 2026-08-16
Depends on: requirements.md

## Summary

The r2 work is a presentation revision over the accepted
vista_playable_home_r1 semantic/gameplay world.  It separates three layers that
are currently too tightly coupled:

1. immutable semantic state and interaction actors;
2. simple authoritative collision/navigation proxies;
3. replaceable high-detail presentation geometry, materials, lights, and
   review cameras.

The first acceptance slice covers entry hall, living room, and kitchen/dining.
Blender deterministically authors the architectural kit and any project-owned
assets.  A visual resolver binds those outputs and licensed catalog assets to
stable semantic IDs.  Unreal imports the bindings into an explicit high-quality
renderer profile, keeps r1 collision/gameplay authority, and produces packaged
runtime, visual, performance, and provenance receipts.

## Baseline Findings

- The accepted review receipt proves six distinct nonblank captures, not
  finished visual quality.
- Current review-camera transforms use an XYZ rotation contract but values such
  as [-10, 0, yaw] are interpreted by Unreal as negative roll rather than
  downward pitch.  The resulting frames are visibly tilted.
- The current lighting profile creates a movable directional light, skylight,
  one 3200-intensity point light near each room centre, and a global manual
  exposure bias of -6.0.
- DefaultEngine.ini explicitly disables static lighting but does not pin a
  complete Lumen, reflection, VSM, Nanite, antialiasing, or exposure profile.
- Existing logs identify the current Linux Vulkan path as SM5 with hardware ray
  tracing disabled.  SM6/Nanite/VSM support must therefore be proven through a
  fresh cook and observed-runtime receipt, not inferred from the A6000.
- HSSD binding is deterministic and textured, but it is a static one-mesh
  category replacement under CC BY-NC 4.0.  It is useful for secondary
  research-demo assets, not sufficient by itself for the final art and release
  gates.
- The accepted HSSD set contains 23 props, about 366k triangles, and 84
  256×256 textures.  Its current material gate accepts any one PBR texture
  slot; it does not require complete base-colour, normal, and roughness
  semantics.
- Current alias/bounds scoring has produced visually wrong category matches
  such as rolling-chair/stool, cooking-pot/planter, and ladder/stall-bar.  Hero
  assets therefore require a visual contact-sheet decision in addition to
  semantic and dimensional checks.
- The Studio Postgres/Qdrant search and Playable Home visual resolver are
  separate paths today.  A database hit alone is not a portable, licensed,
  reproducible asset binding.

## Architecture and Flow

    HouseSpec r1 + EventSpecs
                  |
                  +--------------------------+
                  | semantic/collision layer |
                  +--------------------------+
                              |
    VisualProfile r2 ----------+---------- Asset entitlement registry
          |                                |
          v                                v
    Blender architecture forge      visual binding resolver
          |                                |
          +------- normalized assets ------+
                              |
                              v
                 immutable Unreal build plan
                              |
                 +------------+------------+
                 |                         |
          presentation actors       gameplay/collision actors
                 |                         |
                 +------------+------------+
                              |
                 renderer + lighting profile
                              |
                    packaged Development build
                              |
             review / gameplay / performance evidence

The r1 HouseSpec remains the authority for room IDs, entity IDs, portals,
affordances, state, and event targets.  The r2 VisualProfile can only bind or
decorate those identities and add non-interactive presentation entities in an
allowlisted namespace.

## Vertical-Slice Layout

The first slice keeps the existing room bounds and portals but rebuilds their
presentation:

- entry hall: exterior door reveal, wall trim, floor transition, shoe/coat
  dressing, controlled daylight cue;
- living room: finished floor/walls/ceiling, window assembly, sofa/coffee-table
  hero group, television/media wall, lamps and lived-in clutter;
- kitchen/dining: coherent cabinetry run, countertop/backsplash, sink/stove/
  fridge presentation, dining group, practical fixtures and table dressing.

The remaining three rooms retain their functional r1 presentation and are
excluded from r2 beauty claims until scale-out.

## Interfaces and Contracts

### VisualProfile v1

Schema ID: simworld.vista.playable-home-visual-profile/v1

Top-level fields:

- schema_version, visual_profile_id, house_revision, seed;
- finished_room_ids and compatibility_room_ids;
- architecture_profile and material_quality_tier;
- semantic_visual_bindings;
- dressing_instances;
- renderer_profile and lighting_rig;
- review_shots;
- performance_budget;
- provenance and content_digest.

The profile is closed-world JSON.  It rejects unknown room/entity IDs,
non-finite transforms, path traversal, duplicate binding targets, executable
fields, and an r1 gameplay mutation disguised as presentation data.

### AssetSourceReceipt v1

Every presentation asset resolves to:

- logical_asset_id and stable semantic target or dressing namespace;
- source_kind: project_authored, existing_local, HSSD, or Fab;
- source identity, exact bytes/tree hash, version, and measured metric bounds;
- license ID, entitlement/acquisition evidence, attribution, modification, and
  redistribution policy;
- material/texture inventory and import policy;
- Nanite eligibility, mobility, visual LOD policy, and collision policy.

The source path is private receipt metadata.  Public clients receive logical
IDs and digests, never absolute dataset paths or account data.

### MaterialReceipt v1

For every material slot:

- shader class and blend mode;
- base-colour, normal, roughness, metalness, AO, emissive, opacity, and
  transmission bindings where applicable;
- texture dimensions, colour-space policy, channel packing, UV set, and hash;
- measured or declared texel density;
- Unreal material/object paths and effective Texture2D references after import.

No synthetic fallback is created when a required slot is missing.

### ReviewShot v1

A shot declares:

- shot_id, room_id, purpose;
- eye_location_cm and look_at_target_cm;
- horizontal FOV, near-field clearance, and allowed visibility layers;
- gameplay or pinned physical-camera exposure profile;
- expected hero IDs and optional forbidden foreground IDs.

The compiler derives a normalized quaternion/Rotator from the look vector and
forces roll to zero.  It never accepts a caller-authored Euler rotation for r2
review shots.

### RendererProfile v1

The high desktop tier pins:

- Linux Vulkan desktop deferred rendering on a supported SM6 path;
- Lumen dynamic GI and reflections;
- Virtual Shadow Maps;
- Nanite on eligible opaque static meshes only;
- TSR or a documented measured replacement;
- extended physical luminance range and pre-exposure;
- explicit scalability, screen percentage, texture pool, and quality values.

Exact Unreal config keys and observed runtime CVars are both written to the
acceptance receipt.  A config file alone is not proof that the packaged runtime
used the requested path.  If the UE 5.7 Linux toolchain cannot cook or run the
requested Vulkan SM6 tier, that tier remains blocked and a separately named
software-Lumen balanced profile may be measured without being mislabeled.

## Blender and Asset Pipeline

New headless-first code lives under
tools/blender/vista_playable_home_realism.  It reads HouseSpec plus
VisualProfile and emits:

- modular architectural presentation GLBs;
- project-authored prop GLBs where practical;
- embedded or content-addressed texture payloads;
- normalized bounds and semantic custom properties;
- visual-binding, material, attribution, and build manifests;
- fixed Blender preview renders for early failure detection.

Script structure:

- config.py: versioned profiles, metric constants, deterministic seeds;
- architecture.py: walls, openings, trim, ceilings, floors, windows, cabinetry;
- materials.py: node graphs, UV/texel-density rules, texture receipts;
- dressing.py: bounded placement using authored anchors and exclusion volumes;
- export.py: role-aware GLB export and normalized manifest;
- build.py: CLI orchestration, fail-closed output-root and source identity;
- inspect.py: independent GLB/material/manifest inspection.

Blender MCP may be used for optional visual diagnosis, but every accepted edit
must be reproducible by the version-controlled script and pinned Blender 4.5.8.

## Visual Binding Policy

Asset priority:

1. project-authored architecture and interaction-critical pieces;
2. already-local assets with verified entitlement;
3. HSSD secondary assets for private non-commercial research only;
4. user-approved Fab assets with a recorded entitlement;
5. explicit “missing asset” build failure.

The r2 build never silently drops to basic cubes.  Procedural geometry remains
allowed only for hidden collision, physically simple architecture, or a
deliberately authored object whose final material/shape passes review.

Third-party source binaries and generated Unreal content stay outside Git.
Small schema fixtures, scripts, manifests without private paths, and approved
preview thumbnails may be versioned.

### Audited zero-download sources

- HSSD at /mnt/NAS2/yhliu/habitat_data/versioned_data/hssd-hab:
  CC BY-NC 4.0, 168 local scenes/stages and 1,519 articulated URDF assets.
  Scene 102817200 is a useful living/kitchen/hall density donor, but its layout
  is reference input rather than a replacement for the VISTA topology.
- YCB at /mnt/NAS2/yhliu/habitat_data/versioned_data/ycb:
  locally declared CC BY 4.0, with 79 raw GLBs.  Audited kitchen objects such as
  mug, bowl, plate, and skillet contain embedded 4096×4096 textures and are
  strong pickup/close-up candidates after a dedicated attribution adapter.
- Poly Haven v2 at
  /home/yhliu/.simworld/vendor-assets/vista-mmg-040-polyhaven-v2:
  CC0 stool, shelf, and cardboard-box assets with 1K PBR maps and a prior UE
  5.3 import receipt; they still require a current UE 5.7 binding.
- SimWorld Minimal content is excluded from accepted r2 bindings until its
  third-party content licenses are resolved asset by asset.  The repository's
  Apache license is not assumed to license bundled CitySample, Quixel, or other
  vendor content.

No usable Fab/Megascans library is currently present.  The root filesystem has
insufficient headroom for a new high-resolution library, so any approved
acquisition is stored on NAS and content-addressed in the registry.

## Unreal Composition

### Presentation separation

Each semantic gameplay actor remains the parent/authority.  Its high-detail
visual is attached as a presentation component or child actor with collision
disabled unless an allowlisted exception is proven.  Existing collision proxies
stay hidden in game but collision-enabled.  Non-interactive dressing receives
NoCollision or a coarse static proxy and cannot shadow a semantic ID.

### Architecture

The r1 room-bundle actor is split logically into:

- hidden collision shell;
- visible r2 architectural shell;
- visible dressing and fixtures.

Portal transforms and clearances do not move.  Any presentation change that
requires topology movement becomes a future HouseSpec revision rather than an
untracked art tweak.

### Lighting

The final neutral-day rig uses:

- one directional sun and sky contribution with physical units;
- window/daylight apertures that match visible geometry;
- room-specific rect/spot practical lights with visible fixtures;
- Lumen GI/reflections and VSM contact/shadowing;
- bounded gameplay histogram eye adaptation;
- per-shot pinned physical exposure for deterministic review.

The current identical centre-point-light rig remains only as the r1 rollback.
The implementation begins with software Lumen for Linux compatibility; hardware
ray tracing is an optional measured profile and cannot become a silent
prerequisite.

### Camera correction

Review cameras switch from ambiguous Euler input to look-at contracts.  The
composer checks:

- finite nonzero eye-to-target direction;
- zero roll after conversion;
- eye point inside the declared room or approved doorway;
- near-field sphere/ray clearance;
- expected hero visibility through a fixed trace set.

### Indoor player camera

The packaged pawn gains a room-aware camera profile separate from fixed review
shots.  It begins with a 180–240 cm adaptive boom and 75–85 degree FOV, uses
camera collision to shorten before a wall, damps recovery to avoid popping, and
supports an optional shoulder swap.  An occluder-fade path is enabled only if
it remains deterministic and does not hide event-critical objects.  The
existing fixed 320 cm spring arm remains the r1 fallback profile.

### Nanite and materials

Import enables Nanite for eligible high-detail static opaque meshes and records
the resulting setting.  Doors, pickups, NPCs, cloth/deforming meshes, glass, and
other unsupported or poor-fit cases retain a documented non-Nanite route.
Material inspection occurs after Unreal import so a valid GLB that resolves to
a default/basic material is still rejected.

## Data Model and Compatibility

New visual-profile data is additive.  No migration rewrites the accepted r1
HouseSpec or EventSpecs.

Expected source layout:

- world_packs/schemas/vista-playable-home-visual-profile-v1.schema.json
- world_packs/vista_playable_home_r1/visual_profiles/realistic_interior_r2.json
- tools/blender/vista_playable_home_realism/**
- focused additions under tools/ue/vista_playable_home/**
- tools/tests/test_vista_playable_home_realism_*.py
- docs/specs/vista-playable-home-realism/evidence.md after implementation

The current r1 plan/compiler remains supported.  VisualProfile absence selects
the existing r1 path.  VisualProfile failure never changes the default package
pointer.

## File Plan

- requirements.md, design.md, tasks.md, ACTIVE_WORK.md: approved source of scope.
- visual-profile schema and fixture: closed contract and three-room art plan.
- tools/blender/vista_playable_home_realism: deterministic architecture,
  material, dressing, export, and inspection pipeline.
- tools/ue/vista_playable_home/planning.py: additive r2 presentation operations.
- tools/ue/vista_playable_home/import_assets_commandlet.py: material, Nanite,
  provenance, and collision inspection.
- tools/ue/vista_playable_home/compose_home_commandlet.py: presentation actors,
  renderer/lighting rig, and look-at cameras.
- tools/ue/vista_playable_home/build_home.py: renderer config and r2 receipt
  wiring.
- tools/ue/vista_playable_home/capture_review_views.py: versioned r2 shot set,
  obstruction metadata, and 1920x1080 capture.
- unreal_plugins/VistaPlayableHome character/camera code: additive indoor
  camera profile with r1 defaults preserved when r2 is absent.
- tools/runtime/vista_playable_home and package_receipt.py: immutable r2 package
  and observed runtime renderer/performance receipt.
- focused test files: schema, determinism, camera, materials, composition,
  packaging, gameplay regression, and performance parser.

## Failure Handling and Rollback

- Every generated step writes only to a fresh append-only attempt.
- Missing license, texture, material, mesh, renderer capability, or exact input
  hash fails before map promotion.
- Unreal import/composition failures quarantine the candidate namespace.
- Gameplay or visual-review failure leaves the r1 package and Sunshine entry
  unchanged.
- Renderer-profile mismatch falls back only when the user explicitly launches a
  separately named balanced profile; it never rewrites the high-tier receipt.
- Rollback is selecting the currently accepted r1 package/profile, not deleting
  r2 evidence.

## Testing Strategy

### Pure source tests

- JSON Schema closure, digest stability, semantic-target compatibility, and
  malicious-path/executable-field rejection.
- Look-at-to-Unreal rotation fixtures, including the current negative-roll
  regression.
- license/entitlement and material-slot validation.
- stable dressing placement and exclusion-volume tests.
- renderer config generation and required observed-CVar list.

### Blender tests

- two clean headless builds with matching normalized manifests;
- independent GLB structure, metric bounds, tangent/normal, UV, material, and
  texture inspection;
- nonblank architecture and hero previews;
- no accepted output written into a dirty or nonempty build root.

### Unreal commandlet tests

- import and re-open every r2 presentation asset;
- verify effective Texture2D references, Nanite policy, and collision state;
- compose/save/reload a fresh visual namespace;
- verify renderer and lighting actors, review shots, stable semantic parents,
  hidden collision proxies, and portal/NavMesh connectivity.

### Packaged end-to-end tests

- 60-second fixed traversal at 1920x1080 after warm-up;
- two doors, cross-room carry/place, NPC navigation, and three event resets;
- observed runtime CVars, frame times, VRAM, draw calls, texture streaming, and
  encoder/frame-advance evidence;
- six vertical-slice review views plus an uncut player traversal;
- retained human review decision with reject reasons where applicable.

## Rollout and Observability

1. Freeze r1 baseline and add r2 contracts/tests.
2. Fix review-camera semantics and renderer observation without changing r1.
3. Build the project-owned three-room architecture and material foundation.
4. Bind already-local licensed assets and produce an honest gap report.
5. Pause hero-asset acquisition at the explicit Fab/external gate if needed.
6. Compose and review one living-room hero shot before expanding the full slice.
7. Complete all three finished rooms, gameplay regression, and performance pass.
8. Package under a new immutable receipt; do not replace the accepted r1 launch
   pointer until the user accepts r2.
9. Expand the remaining rooms through separate task updates.

Receipts expose asset coverage, missing bindings, material failures, renderer
path, frame statistics, review-shot obstruction, gameplay pass/fail, and package
identity.  No “photorealistic” or “GTA-grade” label is emitted automatically.

## Tradeoffs and Rejected Options

- Rejected: only increasing texture resolution.  It does not fix scale,
  silhouette, construction detail, lighting, framing, or collision coupling.
- Rejected: replacing the entire home with one purchased scene.  It breaks
  stable VISTA identities, event anchors, and reproducibility.
- Rejected: using visual meshes as complex gameplay collision everywhere.  It
  produces navigation and interaction instability.
- Rejected: hardware ray tracing as a mandatory first profile.  Linux/runtime
  compatibility and performance must be measured before promotion.
- Chosen: a three-room vertical slice before six-room scale-out.  It creates a
  reviewable quality bar and avoids multiplying flawed art decisions.

## Traceability

- R1 -> vertical-slice profile, compatibility rooms, rollout phases 4–7
- R2 -> architecture forge, portal-preserving composition, Blender/UE checks
- R3 -> MaterialReceipt, visual resolver, import inspection
- R4 -> AssetSourceReceipt, deterministic manifests, fail-closed binding
- R5 -> RendererProfile, physical lighting rig, runtime CVar receipt
- R6 -> ReviewShot look-at contract, camera tests, visual review
- R7 -> dressing anchors, hero bindings, EventSpec regression
- R8 -> presentation separation, packaged gameplay tests
- R9 -> performance profile and fixed traversal evidence
- R10 -> immutable package, Sunshine/browser runtime checks
- R11 -> worktree isolation, append-only attempts, rollback
- R12 -> human review and room-by-room promotion

## Open Questions

- Hero asset procurement policy is intentionally deferred to its explicit gate:
  existing/project-authored only, or user-authorized Fab acquisition with
  entitlement receipts.
- Hardware Lumen/ray tracing is evaluated after the software-Lumen slice; it is
  not an approval blocker.

## Approval

- Requested by: user
- Approved by:
- Date:

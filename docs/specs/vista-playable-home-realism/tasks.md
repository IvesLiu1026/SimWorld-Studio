# Tasks: VISTA Playable Home Realistic Interior

Status: Draft — implementation blocked on spec approval
Updated: 2026-08-16
Depends on: requirements.md, design.md

## Rules

- Do not begin production implementation until requirements and design are
  approved or the user explicitly waives the gate.
- Keep the accepted r1 package, live runtime, GPU-1 sessions, production port
  8000, and canonical VISTA data unchanged.
- Generated assets and evidence use fresh append-only run roots and stay out of
  Git.
- External download, purchase, account use, paid API, or public deployment
  requires its own explicit approval.
- Stage named files only; one logical commit per reviewable change.

## Task List

- [ ] T1. Approve the r2 scope and asset policy
  - Files: docs/specs/vista-playable-home-realism/**
  - Depends on: none
  - Requirements: R1-R12
  - Validation: requirements/design/tasks contain no blocking ambiguity; user
    approval and chosen procurement policy are recorded.

- [ ] T2. Add closed visual-profile and receipt contracts
  - Files: world_packs/schemas/vista-playable-home-visual-profile-v1.schema.json,
    world_packs/vista_playable_home_r1/visual_profiles/realistic_interior_r2.json,
    focused contract tests
  - Depends on: T1
  - Requirements: R1-R7, R9, R11
  - Validation: valid fixture passes; unknown fields, executable fields, stale
    house revisions, duplicate bindings, invalid transforms, missing licenses,
    and path escapes fail closed; normalized digests repeat.

- [ ] T3. Implement provider-neutral visual source and entitlement resolution
  - Files: new resolver module under tools/blender/vista_playable_home_realism,
    focused resolver/provenance tests
  - Depends on: T2
  - Requirements: R3-R4, R11
  - Validation: project-authored, existing-local, HSSD, and gated Fab records
    normalize to one AssetSourceReceipt; unverified entitlements and silent
    fallback are rejected.

- [ ] T4. Replace ambiguous review-camera Euler input with look-at shots
  - Files: visual profile, tools/ue/vista_playable_home/planning.py,
    compose_home_commandlet.py, capture_review_views.py, focused camera tests
  - Depends on: T2
  - Requirements: R5-R6, R11
  - Validation: the current [-10, 0, yaw] regression fails before the fix;
    compiled r2 cameras have zero roll, finite look direction, correct downward
    pitch, stable FOV, and clear near field.

- [ ] T5. Build the deterministic three-room architectural presentation forge
  - Files: tools/blender/vista_playable_home_realism/config.py,
    architecture.py, export.py, build.py, inspect.py, focused Blender tests
  - Depends on: T2
  - Requirements: R1-R2, R4, R11
  - Validation: two pinned Blender 4.5.8 builds match normalized manifests;
    walls, openings, trim, floor transitions, ceilings, windows, and kitchen
    cabinetry pass metric, join, back-face, and GLB inspection.

- [ ] T6. Implement material authoring and import receipts
  - Files: tools/blender/vista_playable_home_realism/materials.py,
    tools/ue/vista_playable_home/import_assets_commandlet.py, focused tests
  - Depends on: T3, T5
  - Requirements: R3-R5, R11
  - Validation: required base-colour/normal/roughness data, channel packing,
    texture dimensions, colour space, UV set, texel density, effective Unreal
    Texture2D use, and absence of default materials are independently proven.

- [ ] T7. Author deterministic dressing anchors and exclusion volumes
  - Files: visual profile, tools/blender/vista_playable_home_realism/dressing.py,
    focused placement tests
  - Depends on: T2, T5
  - Requirements: R1-R2, R7-R8
  - Validation: stable seed gives stable placements; portal clearance, pawn
    corridor, interaction trace, event-critical object visibility, and NPC path
    exclusion volumes remain clear.

- [ ] T8. Produce the local-only hero-asset coverage and visual contact sheet
  - Files: resolver fixtures/tests; generated report and contact sheet in a new
    append-only run root
  - Depends on: T3, T6, T7
  - Requirements: R3-R4, R7, R11-R12
  - Validation: evaluate expanded HSSD dressing, HSSD scene 102817200 as a
    layout-density donor, YCB 4K kitchen pickups, and the local Poly Haven
    stool/shelf/box; every entry/living/kitchen hero binding is visually
    reviewed for category, style, dimensions, license, and material coverage;
    known HSSD mismatches such as chair/stool, pot/planter, table/desk,
    slipper/shoe, and ladder/stall-bar cannot pass by semantic alias alone.

- [ ] T9. Execute the external asset acquisition gate if local coverage is
      insufficient
  - Files: entitlement/source receipts in the append-only asset registry; no
    licensed source binaries committed to Git
  - Depends on: T8 and separate user authorization
  - Requirements: R3-R4, R11-R12
  - Validation: each acquired item has a verifiable account entitlement,
    applicable license, exact source hash, allowed collaboration/redistribution
    policy, and approved contact-sheet entry.

- [ ] T10. Separate presentation from semantic collision/gameplay actors
  - Files: tools/ue/vista_playable_home/planning.py,
    import_assets_commandlet.py, compose_home_commandlet.py, focused Unreal
    planning/composition tests
  - Depends on: T5-T9, with T9 skipped only if T8 closes coverage
  - Requirements: R2-R4, R8, R11
  - Validation: visible r2 components have disabled or explicitly allowlisted
    collision; hidden r1 proxies retain collision/navigation; semantic parent,
    tags, affordances, and event targets are unchanged after save/reload.

- [ ] T11. Add and observe the high-quality Unreal renderer profile
  - Files: tools/ue/vista_playable_home/build_home.py, planning.py,
    compose_home_commandlet.py, package/runtime receipt code, focused tests
  - Depends on: T2
  - Requirements: R5, R9-R11
  - Validation: generated config and packaged-runtime evidence agree on Vulkan
    desktop deferred/SM6, Lumen GI/reflections, VSM, eligible Nanite, TSR,
    extended luminance range, pre-exposure, scalability, and screen percentage.

- [ ] T12. Replace validation point lights with the r2 physical lighting rig
  - Files: visual profile, planning.py, compose_home_commandlet.py, focused tests
  - Depends on: T5, T11
  - Requirements: R2, R5-R6
  - Validation: sun/sky, apertures, practical fixtures, light types/units,
    gameplay exposure bounds, and fixed-shot exposure survive save/reload;
    centre-point-light parity is rejected for the r2 profile.

- [ ] T13. Add the indoor third-person camera profile
  - Files: unreal_plugins/VistaPlayableHome character/camera source,
    visual-profile wiring, focused plugin/source and packaged camera tests
  - Depends on: T2, T10
  - Requirements: R8-R10
  - Validation: r2 uses a measured collision-aware 180–240 cm target boom and
    75–85 degree FOV profile; doorway and wall approach/recovery do not trap the
    view or cause persistent wall-filled framing; r1 retains its existing
    default when the r2 profile is absent.

- [ ] T14. Execute and seal the first living-room hero-shot attempt
  - Files: no generated Git content; one fresh append-only Blender/UE attempt
  - Depends on: T4-T12
  - Requirements: R1-R6, R11-R12
  - Validation: 1920x1080 overview and hero views pass structure/material/
    renderer gates and human review; reject reasons are retained before any
    full-slice expansion.

- [ ] T15. Execute and seal the complete three-room visual slice
  - Files: no generated Git content; one fresh append-only attempt and
    docs/specs/vista-playable-home-realism/evidence.md pointers
  - Depends on: accepted T14
  - Requirements: R1-R7, R11-R12
  - Validation: six required views are distinct, unobstructed, correctly
    exposed, hash-bound, and human-accepted; no default/blockout surface is
    visible in the claimed slice.

- [ ] T16. Run packaged gameplay and semantic regression
  - Files: focused acceptance tests and append-only runtime evidence
  - Depends on: T13, T15
  - Requirements: R7-R8, R10-R11
  - Validation: continuous traversal, two doors, cross-room carry/place, NPC
    route, and mmg_001/mmg_044/mmg_045 start/reset pass in one packaged session;
    legacy r1 tests remain green.

- [ ] T17. Run and tune the fixed performance traversal
  - Files: performance parser/tests and append-only profile evidence
  - Depends on: T16
  - Requirements: R5, R9, R11
  - Validation: 60-second 1920x1080 high-tier run records median and 1% low fps,
    CPU/GPU frame time, VRAM, draw calls, and texture streaming; requirements'
    minimums pass or the blocker is reported without relabeling the tier.

- [ ] T18. Package r2 and validate the game-only remote surface
  - Files: package/runtime receipt updates, Sunshine app fixture if required,
    evidence pointers
  - Depends on: T17
  - Requirements: R9-R11
  - Validation: immutable Development package re-verifies after readiness;
    Moonlight/browser shows advancing packaged-game frames; remote input is
    proven or the existing device-permission blocker remains explicit.

- [ ] T19. Obtain final r2 room-promotion decision and plan scale-out
  - Files: requirements/design/tasks/evidence status updates
  - Depends on: T18
  - Requirements: R12
  - Validation: user/human reviewer accepts or rejects each finished room;
    bedroom/office/bathroom work is scheduled only from the accepted pipeline
    and retained review notes.

## Current Discovery Evidence

- Accepted HSSD output has 23 bound props, about 366k total triangles, and 84
  textures at 256×256.  It is a functioning textured import, not a missing-
  database fallback, but it is below the r2 presentation target.
- Across 69 imported HSSD materials, normal-map coverage is sparse and the
  current gate only requires at least one PBR slot, not complete material
  semantics.
- Existing architecture is box-wall/solid-colour construction without the
  detailed shell and finish layers required by R2-R3.
- Local HSSD contains additional stage and articulated data, but the current
  pipeline does not consume stages and flattens selected props into static
  single meshes.  Any reuse must pass the new visual and interaction gates.
- Local YCB provides CC BY 4.0 kitchen candidates with audited embedded 4K
  textures; local Poly Haven provides three CC0 1K PBR candidates.  Neither is
  wired into the current UE 5.7 home revision yet.
- No usable Fab/Megascans cache is installed.  New high-resolution libraries
  must be user-authorized and stored on NAS, not the nearly full root disk.

## Notes

- This task list creates a reviewable vertical slice, not an automatic six-room
  AAA claim.
- The current live r1 runtime and Sunshine process remain untouched during the
  spec phase.

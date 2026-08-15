# Requirements: VISTA Playable Home Realistic Interior

Status: Draft — approval required before implementation
Updated: 2026-08-16
Parent: docs/specs/vista-playable-home

## Problem

The accepted VISTA Playable Home proves a persistent six-room Unreal runtime,
typed interaction, VISTA event overlays, packaging, and remote viewing.  Its
presentation layer is still a technical blockout.  The retained review images
are frequently dominated by walls or oversized furniture, the camera rotations
are visibly rolled, architectural surfaces have little construction detail,
and the lighting is a fixed validation setup rather than a believable interior.
Higher-resolution textures alone cannot close this gap.

The user wants the same playable, agent-controllable home to feel like a
high-end third-person game interior: believable scale, dense PBR materials,
natural and practical lighting, intentional room dressing, and smooth movement
through a packaged game surface.

## Interpretation of “GTA-grade”

This project SHALL use “GTA-grade” as a direction and quality bar, not as a
claim of asset-for-asset parity with a Rockstar production.  The measurable
target is a polished real-time vertical slice whose still frames do not read as
procedural blockout geometry and whose existing VISTA gameplay remains usable.
No source, branding, or copyrighted content from GTA will be copied.

## Goals

- Deliver a finished connected entry hall, living room, and kitchen/dining
  vertical slice while keeping all six existing rooms reachable.
- Replace visible blockout architecture with construction detail, layered PBR
  materials, licensed or project-authored hero meshes, and purposeful dressing.
- Establish a deterministic visual-profile pipeline that can be expanded to the
  bedroom, office, and bathroom/laundry without changing semantic identities.
- Use a physically coherent Unreal lighting and renderer profile suitable for
  the local RTX A6000 and the target RTX 5090 server.
- Preserve walking, doors, pickup/drop/place, NPC navigation, and VISTA event
  start/reset behavior.
- Produce truthful image, performance, provenance, and remote-runtime evidence.

## Non-goals

- Reproduce GTA's city scale, traffic, combat, driving, economy, or full
  character-animation production.
- Finish all six rooms to the same art quality in the first vertical slice.
- Replace the typed VISTA/SimWorld action protocol or regenerate the house for
  every event.
- Make Blender MCP or an interactive Blender session the authoritative build
  path; production assets remain scriptable and headless-reproducible.
- Download, purchase, redistribute, or publish third-party assets without the
  applicable user approval and license entitlement.
- Modify production port 8000, GPU-1 processes, canonical VISTA datasets,
  accepted r1 receipts, or the currently running demo.

## Assumptions and Gates

- The first build remains a private research/demo build.  Existing HSSD content
  may be used under CC BY-NC 4.0, but cannot satisfy a commercial/public release
  gate by itself.
- Existing local, properly licensed assets and project-authored Blender assets
  are the default source.  Fab or other external acquisition is a separate
  explicit gate and requires a per-asset entitlement receipt.
- The source house revision and semantic IDs remain vista_playable_home_r1.
  “realistic_interior_r2” is a presentation revision layered on that house.
- The local validation GPU is an NVIDIA RTX A6000 with 48 GiB VRAM.  A future
  RTX 5090 run is a compatibility target, not evidence available on this host.
- Existing hidden collision proxies remain authoritative unless a focused
  gameplay test approves a replacement.

## Requirements

### R1 — Honest vertical-slice scope

WHEN visual revision realistic_interior_r2 is selected THEN the entry hall,
living room, and kitchen/dining SHALL form one visibly finished, connected
interior while the bedroom, office, and bathroom/laundry remain accessible and
functionally compatible.

Acceptance notes:

- The finished slice contains no visible grey-box or default-grid surfaces in
  its required review shots.
- Unfinished rooms are labeled “functional r1,” not presented as r2-quality.
- The slice can be entered from PlayerStart and traversed without teleporting.

### R2 — Believable architectural shell

WHEN the player views the finished slice at walking distance THEN the shell
SHALL include physically plausible wall thickness, door/window reveals,
baseboards, trim, floor transitions, ceiling treatment, and sealed joins.

Acceptance notes:

- Door clearances and the navigation corridor remain compatible with the
  existing pawn capsule.
- No z-fighting, open exterior cracks, floating trim, or camera-visible back
  faces are accepted.
- Windows or controlled daylight apertures include frames, glass, and an
  exterior treatment; they are not bright unbounded rectangles.
- Geometry remains aligned to the approved metric coordinate system.

### R3 — PBR material and asset quality

WHEN a required r2 review shot contains a visible primary surface or hero prop
THEN it SHALL use an intentional PBR material and an attributable presentation
mesh rather than Unreal default material or procedural collision geometry.

Acceptance notes:

- Architecture targets 512–1024 texels per metre at the capture quality tier;
  hero props use 2K or 4K source maps where screen size warrants it.
- Opaque hero materials declare base colour, normal, and roughness information;
  metalness, AO, opacity, emissive, or transmission are required only where
  physically relevant.
- Material validation rejects missing texture references, default/basic
  materials, obvious non-uniform stretch, and duplicate UV overlap where the
  material contract forbids it.
- Repetition is broken with bounded colour/roughness variation, decals, or
  dressing; damage and grime remain context-appropriate rather than uniformly
  applied.

### R4 — Deterministic visual binding and provenance

WHEN the same pinned visual profile and source assets are built twice THEN the
normalized visual-binding manifest, material graph receipt, semantic binding
set, and content digest SHALL match.

Acceptance notes:

- Every third-party source records asset ID, source URI or catalog identity,
  source hash, license identifier, entitlement/acquisition record, modification
  notice, and redistribution restriction.
- Visual meshes may replace presentation only; semantic IDs, affordances,
  baseline state, event targets, and hidden collision identities stay stable.
- Missing or unlicensed hero assets fail the r2 build closed; no silent cube or
  HSSD fallback is presented as final.

### R5 — Realistic renderer and lighting profile

WHEN the r2 packaged build starts at the high-quality desktop tier THEN Unreal
SHALL load an explicit renderer profile with dynamic GI/reflections, high
resolution dynamic shadows, temporal upsampling/antialiasing, calibrated
physical light units, and controlled exposure.

Acceptance notes:

- The implementation explicitly verifies Lumen GI/reflections, Virtual Shadow
  Maps, Nanite eligibility for suitable static meshes, and TSR or a documented
  equivalent; it does not rely on project defaults.
- Gameplay uses bounded eye adaptation suitable for movement between entry,
  living, and kitchen spaces.  Deterministic review captures use a pinned
  physical-camera exposure per shot.
- Practical fixtures use appropriate point, spot, or rect lights and visible
  emitting geometry.  One identical point light at every room centre is not an
  accepted final setup.
- Lighting avoids clipped white walls, crushed interiors, light leaks, and
  visibly detached contact shadows in the required shots.

### R6 — Correct and reviewable cameras

WHEN fixed r2 review views are materialized THEN camera orientation SHALL be
derived from a world-space eye position and look-at target, with zero roll and
an unobstructed near field.

Acceptance notes:

- The existing XYZ-to-Unreal rotation ambiguity is covered by a regression
  test; a requested downward view cannot become camera roll.
- The vertical slice has at least two 1920x1080 views per finished room: one
  architectural overview and one human-eye/hero view.
- Each view records transform, look-at target, FOV, exposure, output hash, and
  an obstruction result.
- In an architectural overview, unintended near-field wall/furniture occlusion
  may cover at most 25% of the frame and at least three declared room-defining
  assets must be visible.
- The eye position stays at least 25 cm outside every non-translucent blocking
  AABB unless the shot explicitly targets a macro close-up.
- A nonblank-image test is necessary but not sufficient; final acceptance
  includes side-by-side human visual review.

### R7 — Environmental storytelling and VISTA grounding

WHEN the vertical slice is presented THEN room dressing SHALL communicate
residential use and SHALL retain at least the mmg_001 stove, mmg_044 keys, and
mmg_045 phone scenario affordances.

Acceptance notes:

- Dressing includes a controlled mix of functional, decorative, and small
  clutter assets without blocking navigation or interaction traces; the first
  three-room slice targets 30–60 purposeful dressing objects rather than
  uniform random scatter.
- Event-critical objects remain visually identifiable at normal play distance.
- Scenario overlays change state without rebuilding the visual base.

### R8 — Gameplay compatibility

WHEN r2 presentation assets are enabled THEN all accepted r1 gameplay and
semantic contracts SHALL remain unchanged unless this spec explicitly versions
them.

Acceptance notes:

- Player traversal, two doors, one cross-room carry/place action, NPC navigation,
  and three event start/reset cycles pass in the same packaged session.
- Visual meshes never become the sole collision for thin trim, clutter, cloth,
  glass, or high-poly furniture.
- Interaction traces resolve the stable semantic actor, not an untagged visual
  child.
- The indoor third-person camera adapts to tight rooms instead of retaining the
  current fixed 320 cm spring arm.  The first target range is a 180–240 cm
  boom and 75–85 degree FOV with collision-aware shortening, stable recovery,
  and no persistent wall-filled view; exact values are tuned and measured.

### R9 — Performance and memory budget

WHEN the packaged Development build is tested locally on GPU 0 after a warm-up
THEN the r2 high-quality tier SHALL sustain playable 1920x1080 traversal while
reporting actual CPU, GPU, VRAM, draw-call, and streaming measurements.

Acceptance notes:

- Target: median at least 55 fps over a fixed 60-second traversal.
- Minimum acceptance: 1% low at least 30 fps, no post-warm-up freeze longer
  than one second, and peak dedicated VRAM no greater than 24 GiB.
- A missed target remains a measured blocker; resolution scaling may be offered
  as a separate balanced tier but cannot falsify high-tier evidence.
- Performance profiling must include opening a door, carrying an object, and
  crossing the living/kitchen portal.

### R10 — Game-only remote delivery

WHEN the user launches r2 from Moonlight or the browser fallback THEN the
primary surface SHALL remain the packaged Unreal game, not Unreal Editor or a
mock web scene.

Acceptance notes:

- The existing package/Sunshine profile is reused through a new immutable
  package receipt and deterministic application entry.
- 1080p60 is the remote target; frame advancement and input are proven
  separately.
- Missing /dev/uinput or /dev/uhid access is reported as a host permission
  blocker and is not worked around by weakening device security.

### R11 — Isolation, rollback, and evidence

WHEN an r2 build, import, composition, review, or package step fails THEN it
SHALL be quarantined under a new append-only attempt and the accepted r1
package SHALL remain launchable.

Acceptance notes:

- Generated Blender, Unreal, texture, cache, package, and log artifacts stay
  out of Git.
- Source changes use an isolated worktree/branch and explicit staging.
- Evidence binds source commit, visual-profile digest, asset receipts, engine
  and Blender versions, map/package hashes, screenshots, and performance logs.

### R12 — Claim and expansion gate

WHEN the team labels a room “r2 realistic” THEN all automated gates, license
checks, gameplay regressions, and a human visual review SHALL be accepted for
that room.

Acceptance notes:

- Visual review explicitly checks scale, composition, material response,
  lighting, clipping, repetition, and interaction readability.
- VLM review may assist but cannot replace the retained human decision.
- Bedroom, office, and bathroom/laundry are expanded only after the three-room
  slice passes, using the same contracts and evidence format.

## Edge Cases

- A high-detail mesh imports with valid textures but the wrong real-world size:
  normalization fails before Unreal composition.
- A visual asset has a Fab/Marketplace file but no verifiable entitlement:
  it is excluded from the accepted build.
- Lumen or VSM silently falls back because the runtime RHI/profile is wrong:
  runtime acceptance rejects the tier.
- Auto exposure pumps while looking through a window: the bounded gameplay
  exposure curve is tuned and the review camera remains deterministic.
- A Nanite-ineligible translucent, masked, skeletal, or deforming asset is
  routed through an explicit non-Nanite policy.
- Added clutter blocks a doorway, pickup trace, or NPC path: the visual attempt
  fails gameplay acceptance and the r1 collision layer is restored.
- Review framing passes nonblank pixel checks but is mostly wall/furniture:
  obstruction/composition review rejects it.

## Open Questions

No question blocks contract and architectural work.  Before the hero-asset
beauty pass, the user must choose whether to authorize Fab/external downloads
and confirm the applicable Epic/Fab account entitlement.  Until then the
implementation uses project-authored and already-local licensed sources only.

## Approval

- Requested by: user, “那我現在想要 GTA 等級的寫實室內”
- Approved by:
- Date:

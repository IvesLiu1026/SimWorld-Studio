# Evidence: VISTA Playable Home Realistic Interior

Status: source implementation, Blender CPU smoke, local-only hero audit, and
host-side Unreal presentation planning are complete; no r2 visual, Unreal
runtime, gameplay, performance, package, or remote-stream acceptance is
claimed.

Updated: 2026-08-16

## Source milestone

- Working branch: `codex/vista-playable-home-realism`.
- Integrated presentation commits: `194357cf` (four-phase wiring) and
  `c41f952c` (closed scene-identity and reload verification).
- Approved visual profile:
  `world_packs/vista_playable_home_r1/visual_profiles/realistic_interior_r2.json`.
- Visual-profile content digest:
  `a6b9ccb043c1f92778d28b9b6fe5033e80853cc2ec5675c9b5aad30612cb4179`.
- The coffee-table and stove receipts now bind the exact audited Poly Haven
  CC0 source-tree digests, measured floor-aligned bounds, 4K texture channels,
  and conservative non-Nanite import policies.  The stove additionally records
  its opacity map, masked blend mode, and the pending Blender 4.5.8
  `GREATER_THAN 0.5` alpha-graph sanitization.  That receipt records intended
  source modification; it is not GLB runtime proof until the production forge
  and independent inspection gate succeed.
- The closed profile contract rejects unknown fields, stale HouseSpec pins,
  executable fields, duplicate bindings, invalid transforms, unlicensed
  sources, review cameras outside their room, and practical lights outside
  their declared room.
- The Unreal source path compiles six zero-roll look-at review cameras, a
  physical r2 lighting request, and a Vulkan SM6/Lumen/VSM/TSR renderer
  request.  Its receipts deliberately retain `runtime_proof=false` and
  `renderer_runtime_observation=pending` until an actual UE run is observed.
- The provider-neutral local source resolver supports project-authored,
  existing-local, HSSD, YCB, Poly Haven, and gated Fab inputs.  Missing files,
  path escape, symlinks, incompatible licenses, missing Fab entitlement, and
  silent provider fallback fail closed.
- The r2 gameplay source now has an exact-ID opt-in indoor camera profile:
  220 cm boom, 80 degree FOV, an 18 cm `ECC_Camera` probe, immediate safe
  collision retraction, and bounded clear-line recovery.  With no profile or
  an unknown profile, the accepted r1 320 cm/default-FOV behavior remains in
  place.  Package flag propagation and doorway/wall runtime observation remain
  pending.
- The r2 build path now verifies and schedules each pinned room bundle through
  four explicit phases: base import, presentation import, base composition,
  and presentation composition.  The accepted r1 two-phase path remains
  byte-stable.  Host-side validation binds the exact manifest, receipts, GLB
  bytes, identity mesh root, transforms, material-slot count, `NoCollision`,
  hidden/blocking r1 authority, and semantic parent attachment.
- The legacy `scene_receipt_sha256` continues to identify the base semantic
  scene.  The additive r2 presentation scene has its own explicit identity,
  preventing downstream consumers from silently changing the meaning of the
  accepted r1 field.

## Blender CPU smoke evidence

Append-only run root:

`/mnt/NAS2/yhliu/SimWorldStudio/vista-playable-home-realism/runs/20260816T060010Z`

The latest retained source smoke is:

`blender/attempt-04-honesty-smoke`

This retained smoke predates the truthful external-hero receipt update.  Its
digests below remain historical evidence and must not be relabeled as a build
of the current visual-profile identity.

It was generated with pinned Blender 4.5.8 LTS, factory startup, Cycles CPU,
and an explicit 64 px smoke texture override.  It contains 182 architectural
components, 10 openings, 15 image-backed base-colour/normal/roughness material
sets, four review GLBs, 11 dressing anchors, and 11 exclusion volumes.

- Forge-plan digest:
  `d376ef0d3397b654b85e35a32dc9734ab32c5a062c8a8c051397ddf252bc9eb0`.
- Normalized-manifest SHA-256:
  `edaaf9c63fbc55a046567fc7757ce747629e95b207dfb536c146caf1a79d56c6`.
- Build-receipt SHA-256:
  `2a308018484be83f5be1a4d78841c70bbaa5cb5b042e48f3645436c89f4634a4`.
- Inspection-receipt SHA-256:
  `17dde3be064baa33a874ca1f8da7b8725da89e1d5304e8d95b02466c4e56d7c2`.
- Overview preview SHA-256:
  `994450b2c25dd247a80fce104d2723ecda81d1792ea920f1de9e8fc05ecdf08e`.
- Full review GLB SHA-256:
  `32c9903b22011fb093491d6de8fe4572567b0199acac84ecf23297d96459c98c`.
- Blender source SHA-256:
  `c624fddcbe46d975f3bf84ef518ddcbcc49856ad73c20df8b7f350f36911be7c`.

The retained receipt states:

- `quality_class=smoke_only`;
- `eligible_as_architecture_source_evidence=false`;
- `accepted_as_r2_visual_evidence=false`;
- `requires_downstream_asset_and_ue_review=true`;
- `r2_visual_acceptance_authority=downstream_seal_and_human_review`.

Attempts 01 and 02 proved byte-identical normalized manifests before the
receipt privacy fix.  Attempt 03 proved persistent inspection receipts contain
only output-root-relative artifact paths.  Those earlier attempts remain
append-only diagnostic evidence and are not promoted.

## Blender-to-Unreal bundle smoke evidence

The integrated bundle producer was executed in a fresh append-only attempt:

`blender/attempt-05-ue-bundles-smoke`

The build used the same pinned Blender 4.5.8 LTS, Cycles CPU, and explicit
64 px smoke override.  In addition to the four review GLBs, it produced one
identity-root presentation bundle per finished room.  Each bundle contains
exactly one mesh, has no camera or light, declares `NoCollision`, preserves
all complete base-colour/normal/roughness material bindings, and is bound by
the normalized manifest and artifact receipt.

- Normalized-manifest SHA-256:
  `850d5c89dcdd2f9ebc31e4339ee966074b967ef9f0d47d1393643915b0fb0bb2`.
- Artifact-receipt SHA-256:
  `dc0bd5c1102e1a4cbac98ef90eaf5e0d485d4ba8421d0e08175e3dbba4df1947`.
- Inspection-receipt SHA-256:
  `e83967ebddaa909e97a737dfdaf2d53f312f88451acf75d19cf9518a062231e2`.
- Build-receipt SHA-256:
  `48067546b47b8b99a322a5dfc7bce0f14402d9d7404c803a153522d865591e4c`.
- Entry bundle: one mesh, 5 complete PBR materials, 15 textures, SHA-256
  `a0da986326f102860075234948ada8f437215987cbdb20d7e74a386d96bd2ad6`.
- Living bundle: one mesh, 7 complete PBR materials, 21 textures, SHA-256
  `51c38c09b6ad80e6f4a1a921984874e03d95110dd65647da1a36a8c959c6d3d3`.
- Kitchen bundle: one mesh, 12 complete PBR materials, 36 textures, SHA-256
  `91d2f0f32e9547f5beb33e2dea116a9fa578b968cbacaa8c3c7577c58b24f2c5`.

This is import-source evidence only.  It is still a smoke-resolution,
unfurnished architectural view and does not prove Unreal Interchange material
creation, Lumen rendering, or GTA-like visual quality.

## Unreal presentation host evidence

The exact `blender/attempt-05-ue-bundles-smoke` inputs passed the integrated
host-side four-phase build plan.  The three presentation GLBs are verified as
single, identity-root mesh scenes before an Unreal commandlet may run.  The
planned reload observation is closed over the exact mesh object path,
transform and scale, material slots, collision state, attachment, and hidden
r1 gameplay authority.  Decoy roots, parented roots, changed GLB bytes,
changed receipts, missing room observations, and presentation/base scene hash
substitution fail closed.

An independent source review identified three blockers in the first draft:
legacy scene-receipt identity drift, incomplete post-reload verification, and
ambiguous GLB root identity.  All three were fixed and the follow-up review
reported no remaining merge blocker in those areas.  This is host/source
evidence only: Unreal 5.7 Interchange import, asset rename, map save/reload,
material instantiation, collision persistence, and attachment behavior have
not yet been executed.

## Local hero-asset gap evidence

The closed local-only audit CLI was executed against five explicit HSSD r1
outputs without recursive discovery or download.  Its append-only result is:

`asset-audit/attempt-01-hssd-hero-gap`

- Input SHA-256:
  `9d3ba324773207bb66c2c3f7846ad6ed73d239eb5c863f927ba09e3b2d575a64`.
- Coverage evidence digest:
  `4c7634db53d00c48f17dd73cbf79c1fc59348c11dd7cedb6d2b2804424b1da6c`.
- Coverage matrix digest:
  `0ce0a0c6bad6459f0b588d7b146ef7effddd4dcf33a25f7e9bb579013dbdbed5`.
- Contact-sheet plan digest:
  `2d983442019436ba4af93add475ca9c2e3db2b686017f69cf8a728838f7c31fe`.

All five candidates resolved with exact source hashes and compatible private
research licenses, but automated coverage remained incomplete.  The shoe
bench, coffee table, sofa, dining table, and stove all failed the 2048 px PBR
screen-resolution gate because their retained HSSD textures are 256 px.  All
remain style-unreviewed; the sofa also failed the required local-axis dimension
range.  The command deliberately exited 4 after retaining diagnostics,
produced no promotion gate, and serialized no private absolute paths.

## Licensed external acquisition evidence

The approved account-free acquisition was executed into a fresh private NAS
attempt:

`/mnt/NAS2/yhliu/SimWorldStudio/vista-playable-home-realism/runs/20260816T073747Z/external-assets/attempt-01-poly-haven-cc0`

The closed manifest pins 22 Poly Haven asset IDs and their upstream
`files_hash` values.  The downloader accepts only the official API/CDN paths,
uses no credential, verifies every provider size and MD5, adds SHA-256 for each
file, and removes partial files on failure.  Poly Haven publishes these assets
under CC0 1.0; the acquisition remains bound to the exact official URLs and
receipt below.

- Acquisition manifest SHA-256:
  `317ca0f30409d04365ae8d7b5aa096e8454d8bc8fbe13a8b386935b19e719774`.
- Approved download-plan digest:
  `20d0ecf836034fb1061556578635b2483c48d82da9f5506a9e8ce08100a272bb`.
- Acquisition receipt digest:
  `a8a6b03c8fae71b299a2fcb36764e2dc1ec32c1e4dcd0b30ff0d3db3223fef70`.
- Acquisition-receipt file SHA-256:
  `6b894d75f61115a2d2d63769c091ae4da511e9ce9697cd0809fff1b3d1f910a3`.
- Retained payload: 22 unique assets, 103 files, 359,529,243 exact
  provider-declared bytes, and zero `.partial` files.

The pack deliberately does not promote every downloaded model to a hero.  The
modern coffee table is a direct contemporary candidate; the electric stove
requires measured normalization and a non-Nanite translucent-glass policy.
The contemporary shoe bench, sofa, and dining table remain project-authored
geometry using the acquired 4K white-oak and wool PBR materials.  Vintage or
rustic sofa/table candidates were not silently relabelled as contemporary.
Resolver-compatible receipts, Blender realization, contact sheets, and human
visual acceptance remain downstream gates.

## Packaged VSM page-pressure diagnosis

The accepted package attempt at
`runs/20260816T211647Z-production-r3/ue/package-linux-development/attempt-03-vsm-path-sanitized`
was inspected through UE 5.7.3's non-Nanite page-area diagnostics. This was a
runtime-only A/B measurement; it did not mutate the sealed package or promote
the already-warning-bearing process as renderer evidence.

- At the original high-shadow settings, the player skeletal mesh covered
  approximately 290--310 VSM pages and the NPC covered 18--33. The fixed
  marking queue holds 128 jobs, so the player alone could overflow it.
- `r.Shadow.Virtual.ResolutionLodBiasDirectional=0.5` reduced the observed
  player footprint to 27 pages while retaining directional VSM shadows.
- The combined room presentation meshes reached 220 local-light pages at bias
  `0`; bias `0.5` bounds the equivalent half-resolution footprint near 110
  pages. Moving-light variants are pinned to the same value for consistency.
- The package-only coarse-page exclusion remains enabled. It does not disable
  ordinary pixel-requested VSM shadows.

The source importer now also requires every verified opaque/masked static mesh
to persist with Nanite enabled instead of accepting an eligible-but-disabled
receipt. A fresh source attempt and a fresh package process are still required
before the renderer log gate can accept this mitigation.

## Validation retained in this milestone

- All integrated realism contract, resolver, audit CLI, Blender forge/bundle,
  presentation import/composition, indoor-camera, and Unreal source tests:
  110 tests passed after integration.
- Unreal r2 profile/build wiring: 43 focused and r1 regression tests passed.
- Blender forge plus legacy Blender contract: 19 tests passed.
- Existing r1 Unreal/build/review/package regression: 60 tests passed.

## Open acceptance gates

- The current Blender overview proves architecture/material generation, not a
  GTA-like furnished room.  Living-room hero props and dense dressing are not
  present in that image.
- HSSD hero furniture is usable for a private research demo but remains 256 px
  source material and CC BY-NC; it cannot by itself satisfy the final r2 hero
  quality or commercial-use target.
- Local YCB kitchen objects are useful interactive-prop candidates but the
  inspected mug GLB exposes base colour only, not complete hero PBR semantics.
  The retained Poly Haven stool/shelf/box sources are 1K.  Neither closes the
  2K hero-furniture gap.
- UE import, presentation composition, save/reload, Lumen/CVar observation,
  six 1920x1080 captures, packaged gameplay, performance, and
  Sunshine/Moonlight input remain unrun.
- The root filesystem has about 139 MiB free and both A6000 GPUs have existing
  Unreal work.  UE/UAT launch remains blocked until the host has a safe root
  margin and GPU 0 can be used without disturbing accepted r1; GPU 1 remains
  forbidden.  All r2 build/cache/output paths are specified as NAS-only.
- The accepted r1 package and live runtime remain the rollback target and were
  not restarted or modified during this milestone.

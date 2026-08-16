# Active Work: VISTA Playable Home Realistic Interior

Updated: 2026-08-16

## Ownership

- Branch: codex/vista-playable-home-realism
- Worktree: /home/yhliu/SimWorld-Studio-worktrees/vista-playable-home-realism
- Base commit: 205ed1c59410621ec1037a528936e36a43b61a31
- Current owner: /root (implementation integrator)
- Integrator-owned paths:
  docs/specs/vista-playable-home-realism/**
- Integrated worker ownership:
  - contracts: visual-profile schema, fixture, and focused contract tests;
  - Blender forge: architecture, materials, dressing anchors, GLB export,
    inspection, and focused tests;
  - source/catalog: entitlement resolver, local hero coverage, contact-sheet
    planning, append-only audit CLI, and focused tests;
  - UE import bundles: one-mesh identity-root room bundles, cross-receipt/GLB
    validation, and focused Blender tests;
  - Unreal profile: additive camera, lighting, renderer request, build pinning,
    and focused tests;
  - presentation import: exact GLB/receipt verification, additive four-phase
    import/composition wiring, closed reload observations, and focused tests;
  - indoor camera: additive r2 third-person spring-arm profile and focused
    source tests.
- Active isolated implementation workers:
  - `realism_forge_review`: branch `codex/vista-home-external-forge`, worktree
    `/mnt/NAS2/yhliu/SimWorldStudio/worktrees/vista-home-external-forge`, owns
    dual-mode external-asset forge/placement/export source and focused tests;
  - `realism_ue_readiness`: branch `codex/vista-home-r2-runtime-harness`,
    worktree
    `/mnt/NAS2/yhliu/SimWorldStudio/worktrees/vista-home-r2-runtime-harness`,
    owns phase routing, r2 capture/runtime/acceptance source and focused tests.
  `/root` owns acquisition, generated attempts, integration, documentation,
  final validation, and any later runtime lifecycle.

## Runtime Ownership

- `/root` owns the approved r2 validation lifecycle.  No GPU launch is active:
  GPU 1 remains forbidden, and GPU 0 is shared by accepted r1/Sunshine rather
  than idle.
- Do not restart, replace, or stop the accepted r1 packaged runtime.
- Do not touch GPU 1 or its existing Unreal processes.
- Do not change Sunshine, Tailscale, production port 8000, or the r1 package
  pointer during this phase.

## External-State Gates

- The user authorized external 2K/4K asset downloads to NAS and a non-GPU-1
  Unreal 5.7 import/Lumen/package validation run with “允許！” on 2026-08-16.
- Default acquisition is account-free CC0 with exact URL, licence, size, and
  SHA-256 receipts.  Existing Fab/Epic entitlement may be used only if it can
  be verified locally; purchase, new credentials, paid APIs, uploads, public
  deployment, and redistribution remain unauthorized.
- Every generated implementation artifact uses a new append-only attempt.
- The first approved acquisition is complete at
  `/mnt/NAS2/yhliu/SimWorldStudio/vista-playable-home-realism/runs/20260816T073747Z/external-assets/attempt-01-poly-haven-cc0`:
  22 CC0 assets, 103 files, and 359,529,243 exact bytes with no partial files.

## Handoff

- Requirements/design/tasks were approved by the user on 2026-08-16.
- Workers receive separate worktrees and non-overlapping file ownership.
- Source contracts, Blender forge, local source/coverage gates, Unreal
  renderer/profile compiler, presentation import/composition, and indoor
  camera source are integrated on the working branch.
- Integrated validation is green: 110 realism tests and 60 accepted-r1
  Unreal/build/review/package regressions passed.  Independent follow-up review
  found no remaining merge blocker in the three corrected presentation
  identity/reload areas.
- The retained CPU smoke root is
  `/mnt/NAS2/yhliu/SimWorldStudio/vista-playable-home-realism/runs/20260816T060010Z`;
  its latest source attempt is `blender/attempt-05-ue-bundles-smoke`, explicitly
  smoke-only and not visually accepted.  The retained local hero audit is
  `asset-audit/attempt-01-hssd-hero-gap` and correctly reports incomplete
  coverage without a promotion gate.
- The accepted r1 live runtime remains untouched.  The r2 GPU run is now
  authorized but has not started.  Root filesystem headroom (about 243 MiB)
  and the absence of an idle legal GPU block safe UAT/capture execution; all
  future HOME/TMP/XDG/DDC/output paths must be NAS-only.

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
- Active isolated workers: none; `/root` owns final integration and evidence.

## Runtime Ownership

- None during contract/source implementation.
- Do not restart, replace, or stop the accepted r1 packaged runtime.
- Do not touch GPU 1 or its existing Unreal processes.
- Do not change Sunshine, Tailscale, production port 8000, or the r1 package
  pointer during this phase.

## External-State Gates

- No external asset download, purchase, Epic/Fab account use, paid model/API,
  upload, public deployment, or long-running GPU build is authorized by the
  implementation approval.
- Generated implementation artifacts will use a new append-only run root after
  approval.

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
- The accepted r1 live runtime remains untouched.  No r2 Unreal commandlet,
  package, or GPU execution has been authorized or run in this phase.

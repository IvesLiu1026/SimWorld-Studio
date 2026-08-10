# Tasks: VISTA Blender-to-Unreal Interactive World

Status: In progress — loopback runtime and release validation accepted; Git publication pending
Updated: 2026-08-11
Depends on: requirements.md, design.md

## Rules

- Work only in the dedicated worktree and append-only run root.
- Do not touch the current GPU 1 demo's processes or ports.
- Do not expose Blender, Studio, MCP, Cirrus or UE control listeners publicly.
- Maximum two Claude-assisted iterations; no undefined quality loop.
- Update task status and evidence as implementation changes scope.

## Task List

- [x] **T1. Establish goal, ownership and live preflight**
  - Requirements: R1, R7, R8
  - Validation: branch/worktree, remote base, group/tool/GPU/port/disk inventory.

- [x] **T2. Approve requirements, design and bounded execution tasks**
  - Depends on: T1
  - Requirements: R1-R8
  - Validation: SDD checklist review and `git diff --check`.
  - Note: the user's delivery directive explicitly waives a separate waiting
    period between spec creation and implementation; safety/state gates remain.

- [x] **T3. Provision pinned user-space Blender and glTF inspection tooling**
  - Depends on: T2
  - Requirements: R2, R3
  - Validation: version/checksum receipt, clean background startup, GLB and
    JSON glTF inspect.
  - Evidence: Blender `4.5.8 LTS`; final r3 validation status `passed` at
    `/mnt/NAS2/yhliu/SimWorldStudio/vista-blender-world/runs/20260811T-procedural-chair-upgrade-r3/blender/validation.json`.

- [x] **T4. Install and validate the loopback-only Blender MCP lane**
  - Depends on: T3
  - Requirements: R3, R7
  - Validation: pinned source revision, localhost listener, bounded ping/scene
    inspection, no secret/canonical mounts, clean shutdown.
  - Evidence: pinned `zorak1103/blender-mcp` v0.5.1 commit
    `43d60c36aadc892739d42051f64f87fe55a57b48`; locked upstream tests and
    credential-isolation smoke passed; `execute_python` is disabled.
  - Final live evidence: `20260811T-final-blender-mcp-r1` serves the exact r3
    `.blend` on loopback port `8400`. Its authenticated probe passed with 42
    tools, `execute_python_enabled: false`, four collections, 294 objects, and
    89 chair-named root/objects. Probe receipt SHA-256 is
    `9bd496f9d22f0010db8f38f25194a07411885bc905ac97f7c95fa40a293a4c5e`.

- [x] **T5. Implement and test the deterministic `mmg_040` asset generator**
  - Files: `tools/blender/**`
  - Depends on: T3
  - Requirements: R2, R6
  - Validation: clean-start build twice, script/output hashes, mesh/material/
    bounds checks, nonblank preview and visual inspection.
  - Evidence: fixed seed `4040`; 285 meshes, 33 materials and 72,320
    triangles across the room shell (76), tall cabinet (121), and ergonomic
    chair (88). Manifest SHA-256 is
    `1c1008ebcd3b9cb6f130a54f65e04de530e81f3a6954c042e43f237fbe35cc55`;
    GLB SHA-256 is
    `c3d67a34f0f0bd720133dc8ce08c7bb52b0c3008386aec04957014d76010e759`.

- [x] **T6. Import the generated asset into a fresh UE 5.3.2 project**
  - Files: bounded scripts under `tools/ue/**` or compatible existing namespace.
  - Depends on: T5
  - Requirements: R4, R6
  - Validation: commandlet exit 0, saved packages, material/collision/bounds and
    import receipt tied to exact GLB or JSON glTF dependency hashes.
  - Evidence: attempt-07 imported 318 objects: 285 Static Mesh assets plus 33
    Material assets. Required coverage is 121/121 cabinet, 76/76 room, and
    88/88 chair with no missing required mesh. Import receipt SHA-256 is
    `07726899bcbfcb36e6470bde5d88d17c396757fdb9467f5dc47dbaaf3dd456c1`.

- [x] **T7. Compose and machine-validate `MMG040_Office_BlenderR1`**
  - Depends on: T6
  - Requirements: R4-R6
  - Validation: map save/load, actor inventory, no basic hero fallback, fixed
    camera/layout and physics/movable prop checks.
  - Evidence: attempt-07 saved 285 generated actors in a 296-actor inventory,
    removed `VISTA_office_chair_provisional` and the four other recorded
    surrogates, configured the cardboard box as a movable physics prop, and
    passed the PlayerStart capsule-clearance gate with zero blockers. Map
    SHA-256 is
    `8dd416ca31f66cbdbbdc60c1ef2c21c8bdf7d83b1abe36e177f202bc01e8d9f4`.
  - Boundary: this closes commandlet composition only. The signed scene
    receipt deliberately retains its historical `saved_machine_candidate`
    status and pending candidate fields; the separate attempt-11 evidence
    closes rendered review, live physics observation, and runtime input under
    T8/T9 without mutating that receipt.

- [x] **T8. Launch and accept the isolated Studio/Pixel Streaming runtime**
  - Depends on: T7
  - Requirements: R5, R6
  - Validation: UE/MCP health, decoded frames, keyboard/mouse input, fixed
    screenshots and SSH tunnel command.
  - Final delivery: attempt-11 in owned tmux
    `vista-blender-world-final-r5-20260811` on GPU 1 and loopback ports
    `3022/55582/8615/8616/8919`; runtime `ready_at` is
    `2026-08-10T18:21:24.771742Z`.
  - Evidence: authenticated and unauthenticated health checks, exact loopback
    listener ownership, UE/MCP connectivity, initial grounded PIE state,
    correlated browser keyboard input, and final reset-to-PlayerStart grounded
    state all passed. Browser displacement was `86.58065473498606 cm`.
  - Trace safety: the accepted action-only trace is 48,955,877 bytes, SHA-256
    `786663baabbb6027a28f0954cefb02b0fa440335a25b7fb988c154ff8e32f5ec`,
    and its signed receipt records `active_token_present: false` and
    `network_log_included: false`.
  - Cold-start correction: runtime readiness is now bounded to 60-900 seconds
    with a 600-second default; attempt-11 explicitly used 900 seconds. This
    replaces the insufficient first cold-start allowance without permitting an
    unbounded wait.

- [x] **T9. Complete bounded visual review**
  - Depends on: T8
  - Requirements: R6, R7
  - Validation: structured issue/repair ledger, cost/turn/time receipt, no P0
    deterministic or visible failures in the accepted attempt.
  - Result: accepted with zero Claude/VLM iterations and no repair loop. The
    four retained attempt-11 views cover the complete Studio page, final UE
    PlayerStart view, Blender room overview, and Blender chair detail; all are
    nonblank and show the detailed chair rather than the provisional proxy.
  - Honest boundary: this is a bounded manual/deterministic vertical-slice
    review, not a real Text/Visual Review provider result. The known bitmap-PBR
    and broader Production gaps remain documented and were not relabeled as
    fixed.

- [ ] **T10. Validate, commit, push and write final handoff**
  - Depends on: T3-T9
  - Requirements: R8
  - Validation: focused tests, frontend build if touched, `git diff --check`,
    reviewed targeted staging, atomic commits, pushed branch and exact handoff.
  - Validation result: 76 tests plus 17 subtests passed; Ruff, shell syntax,
    Python compile, JSON schema/receipt validation, source secret scanning and
    `git diff --check` all passed.
  - Local source commits: `fcfff03e`, `3e1557ab`, `91b5af94`, and `82f46f33`.
  - Status: targeted source commits are complete. The documentation commit,
    inherited-upstream correction, and GitHub push are still pending. Do not
    report the branch as pushed yet.

## Notes

- Administrator gates intentionally deferred from tonight's critical path:
  GPU 0 render-group access, writable `/data/simworld`, full UE 5.3.2 build
  tree, managed Postgres/Qdrant/embedding, public Coturn/DNS/TLS.
- Attempt-06 is quarantined because its preparation contract predated the
  three-asset final bundle. Attempt-07 remains the authoritative machine
  import/composition evidence; attempt-11 is the only delivery runtime.
- Attempt-07's first cold launch timed out before UE readiness. Attempt-09 was
  stopped after token exposure and its token was rotated. Attempt-10 was
  stopped and quarantined after its Playwright `.network` trace was found to
  contain the HTTP-only auth cookie; that trace is secret-bearing and must not
  be shared or promoted. Attempt-11 uses a newly rotated token and an
  action-only trace with no network log.
- The current materials are deterministic Principled material constants. They
  are not bitmap PBR texture/normal/roughness sets and must not be described as
  such.

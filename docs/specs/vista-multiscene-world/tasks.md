# Tasks: VISTA Multi-Scene Continuous 3D World

Status: Proposed — implementation gated on user approval
Updated: 2026-08-11
Depends on: `requirements.md`, `design.md`

## Execution Rules

- Approval of this document authorizes code/schema/test edits only. Every
  Blender executable/generation invocation, external download, UE write, GPU
  run, paid call/upload, and merge/deploy remains a separate recorded gate.
- Work only on `codex/vista-multiscene-world` and dedicated child worktrees.
- Preserve live attempt-11, Blender MCP, older demo, GPU 1, reserved ports,
  production port 8000, historical evidence, and canonical VISTA datasets.
- Use new append-only NAS run roots and fresh disposable UE projects.
- Stage explicit files only; never commit `.umap`, projects, downloaded assets,
  tokens, traces, recordings, or unrelated changes.
- A live milestone runs from a clean candidate commit; external evidence binds
  it; a later focused evidence-pointer commit is then pushed.
- Update requirements/design before implementing an approved scope change.

## Phase A — M0 Contracts

- [ ] **T1. Record code-scope approval and file ownership**
  - Owns: `docs/specs/vista-multiscene-world/**`, active-work registry.
  - Requirements: R1-R15.
  - Validation: approval explicitly says code-only; branch/worktree/status and
    non-overlapping owners recorded; all apply gates remain closed.

- [ ] **T2. Add strict world-pack and scenario-overlay schemas**
  - Owns: `world_packs/schemas/**`, schema fixtures/tests.
  - Depends on: T1.
  - Requirements: R2, R3, R5, R9, R12-R15.
  - Validation: meta-validation plus unknown-field, duplicate, non-finite,
    traversal, namespace, graph, portal, budget, license, overlay-operation,
    oracle-leakage, and digest negatives.

- [ ] **T3. Add deterministic resolver and immutable build-plan contract**
  - Owns: `tools/worlds/{resolve,validate,overlay}.py`, plan schema/tests.
  - Depends on: T2.
  - Requirements: R2-R3, R5-R9, R12-R15.
  - Validation: golden transforms/digests, identical resolution twice,
    revision namespaces, exact tool/input binding, data-only contracts.

- [ ] **T4. Add sealed read-only MMG040 legacy adapter**
  - Owns: `tools/worlds/legacy_adapter.py`, fixtures/tests.
  - Depends on: T2.
  - Requirements: R3, R6, R14-R15.
  - Validation: exact canonical r3 manifest/GLB paths and hashes; normalized
    in-memory view; zero byte changes; no attempt-11 dependency; existing
    MMG040 asset/UE/runtime suites remain green.

- [ ] **T5. Add reusable Blender bundle-v2 framework**
  - Owns: `tools/blender/vista_world/**`, generic build/validate CLI/tests.
  - Depends on: T2-T3.
  - Requirements: R6-R7, R12-R15.
  - Code-only validation: static/mock manifests, deterministic digest logic,
    local bounds, semantic/material/collision/PBR metadata, preview contracts,
    and budget failures without invoking Blender.
  - Apply gate: explicit authorization is required before any Blender
    executable or generation invocation, including tiny fixtures. Two clean
    fixture builds become execution evidence only after that gate.

## Phase B — Capability Spike Before Bulk Authoring

- [ ] **T6. Add revision-repeatable generic UE import/composition contracts**
  - Owns: new generic modules/schemas/tests under
    `tools/ue/vista_blender_world/**`.
  - Depends on: T3-T5.
  - Requirements: R2, R6-R8, R12-R15.
  - Validation: mocked/offline tests for namespace isolation, texture roles and
    colorspaces, explicit collision, stable IDs, source immutability,
    quarantine, saved-map reload contract, portal/NavMesh receipts.

- [ ] **T7. Generalize runtime profile, launcher, and performance harness**
  - Owns: `tools/runtime/vista_blender_world/{runtime,launch,world_profile,world_qa,performance}.py`,
    README and focused tests.
  - Depends on: T3, T6.
  - Requirements: R1, R4, R8, R11-R15.
  - Validation: `--map` regression; mutually exclusive receipt mode;
    receipt/hash verification; explicit approval-bound GPU and non-overlapping
    port profile; no hard GPU-1 rejection for a newly owned lane; metric
    sampling/aggregation/freeze fixtures; no change to live runtime state.

- [ ] **T8. Execute the two-bundle textured UE capability spike**
  - Owns: tiny deterministic fixture and one new append-only run only.
  - Depends on: T5-T7.
  - Requirements: R7-R8, R11-R13.
  - Plan/dry-run: validate exact Blender, UE, project, output, GPU, ports, and
    stop commands without mutation.
  - Apply gates: separate authorization for every Blender fixture invocation,
    UE write, and live GPU run.
  - Validation: bitmap PBR binding/colorspace, same-name namespace isolation,
    semantic collision roles, one portal capsule/NavMesh path, advancing
    frames, and complete telemetry receipt. Bulk zone generation stays blocked
    until this passes.

## Phase C — Pack, Overlays, and Approved Assets

- [ ] **T9. Author the complete eight-zone topology and provenance**
  - Owns: `world_packs/vista_living_lab_r1/{pack,provenance}.json`, fixtures.
  - Depends on: T2-T4.
  - Requirements: R2-R5, R8, R12.
  - Validation: complete graph; stable zones/anchors/portals/views/probes;
    M1a slice/M1/M2 revisions and absolute budgets; initial VISTA evidence;
    generic validators contain no eight-zone/Living-Lab special case.

- [ ] **T10. Author overlay fixtures and apply/clear contract tests**
  - Owns: pack overlay files and focused resolver/runtime tests.
  - Depends on: T2-T3, T9.
  - Requirements: R5, R9-R10, R14-R15.
  - Validation: left-item, appliance, spill/hazard, and blocked-path fixtures;
    public payload/provenance separation; deterministic resolve; forbidden
    code/oracle fields; stale generation, idempotency, reset, and no-leakage
    tests. Authoritative execution remains M3.

- [ ] **T11. Prepare a read-only external asset candidate ledger**
  - Owns: source/license/size/role proposal only; no payload download.
  - Depends on: T9.
  - Requirements: R7, R12-R13, R15.
  - Validation: URL/provider/author/license/redistribution/expected-files and
    estimated byte budget for each candidate; reviewer selection recorded.

- [ ] **T12. Acquire, inspect, and seal the approved PBR/asset set**
  - Owns: lock manifest and one append-only NAS asset root.
  - Depends on: T8, T11.
  - Gate: explicit user approval of exact sources and downloads.
  - Requirements: R7, R12-R13, R15.
  - Validation: archive/path/executable safety, digests, roles/colorspaces,
    normal convention, resolution/UV coverage, license and redistribution
    record, bounded storage, no repository secret access.

## Phase D — M1a Visible Connected Slice

- [ ] **T13. Implement the main-corridor/lobby and teaching-kitchen/café bundles**
  - Owns: the named zone 2/3 slices and their tests only. The legacy adapter
    supplies the office core; this task does not claim finished zones 1-3.
  - Depends on: T8-T9, T12.
  - Requirements: R3-R8, R12-R14.
  - Code-only validation: deterministic static/mock outputs, two portal
    contracts, semantic props, explicit collision, at least 3 PBR sets and 6
    reviewable hero props across M1a, fixed views, and budgets.
  - Apply gate: every Blender invocation remains separately authorized.

- [ ] **T14. Implement the M1a preview HUD, interaction, and overlay hook**
  - Owns: `tools/runtime/vista_blender_world/world_preview.py`, new bounded
    world-preview server module/routes,
    `simworld_studio_workspace/web/public/ue-player.html`, its Vite-built
    `web/dist/ue-player.html` output, the reviewed player digest/pin and patch
    contract in `runtime.py`, plus focused Python/Node/UI tests.
  - Depends on: T6-T7, T9-T10.
  - Requirements: R1, R4, R9-R11, R13-R15.
  - Validation: fixed pawn/object-state probe; pack-bound zone resolver;
    professional zone/position/elapsed HUD; one allowlisted door/refrigerator
    command with before/after query; receipt-bound overlay-at-launch and clean
    baseline restart hook; Vite development build; byte-identical public/dist
    player files with the reviewed digest updated; legacy `--map` preflight
    regression; no arbitrary Python, oracle metadata, or token leak.

- [ ] **T15. Build, resolve, import, and compose `r1-m1a`**
  - Owns: one new append-only Blender run and disposable UE project/revision.
  - Depends on: T4, T6, T9-T10, T13-T14.
  - Apply gates: every Blender invocation and every UE project/map write.
  - Requirements: R2-R10, R12-R15.
  - Validation: clean deterministic bundles, sealed legacy reimport, exact
    plan/import/compose receipts, saved-map reload, PBR bindings, objective
    visual rubric, capsule clearance, authoritative paths, source unchanged.

- [ ] **T16. Accept and publish M1a from an isolated runtime**
  - Depends on: T7, T14-T15.
  - Sequence: commit clean candidate; record exact commit; obtain GPU/live-run
    authorization; launch only from that commit; write external evidence;
    review; create focused evidence-pointer commit; push branch.
  - Requirements: R1-R15.
  - Validation: fresh GPU 0/render/port/tmux preflight; loopback auth; 60-90 s
    uncut >=40 m out-and-back traversal; zone/position/elapsed HUD; movement,
    key-up, mouse yaw, fixed typed interaction; six-frame contact sheet with a
    same-camera before/after pair; overlay-applied launch plus clean-baseline
    restart; NavMesh/state correlation; browser-close survival; secret-free
    trace; 10-minute dev-profile smoke; exact Mac tunnel/start/stop handoff.

## Phase E — M1 Indoor Living Lab

- [ ] **T17. Author the remaining workplace and circulation subspaces**
  - Owns: office/storage/meeting/print/pantry extensions and elevator/stair
    circulation builders/tests.
  - Depends on: T12, T16.
  - Requirements: R3-R8, R12-R15.
  - Validation: static/mock deterministic contracts, portals/views/props,
    objective visual and budget checks. Blender execution is separately gated.

- [ ] **T18. Author classroom/library and lab/workshop bundles**
  - Owns: zone 4 and zone 5 builders/tests.
  - Depends on: T12, T16.
  - Requirements: R3-R8, R12-R15.
  - Validation: static/mock deterministic contracts, portals/views/props,
    objective visual and budget checks. Blender execution is separately gated.

- [ ] **T19. Build, resolve, import, and compose `r1-m1`**
  - Depends on: T17-T18.
  - Apply gates: every Blender invocation and UE revision write.
  - Requirements: R1-R10, R12-R15.
  - Validation: fresh revision plan/import/compose receipts; completed zones
    1-5; at least 5 PBR sets and 8 hero props; four boundary crossings; all
    anchors/paths/views; overlay launch and clean restart; absolute budgets.

- [ ] **T20. Accept and publish M1**
  - Depends on: T19.
  - Gate/sequence: candidate commit → authorized isolated live run → external
    manifest → review → evidence-pointer commit → push.
  - Validation: zones 1-5, interactions/overlay, browser controls, 30-minute
    dev-profile telemetry including minute-10-to-30 memory gate, secret-free
    evidence, and Mac handoff.

## Phase F — M2 Full First Pack

- [ ] **T21. Author the outdoor quad/paths/athletic-edge bundle**
  - Owns: zone 6 builder/tests only.
  - Depends on: T12, T20.
  - Requirements: R3-R8, R12-R15.
  - Validation: exterior topology, lighting/material views, safe collision,
    portal/NavMesh contracts, semantic props, objective visuals, and budget.

- [ ] **T22. Author the parking/drop-off/crosswalk/transit bundle**
  - Owns: zone 7 builder/tests only.
  - Depends on: T12, T20.
  - Requirements: R3-R8, R12-R15.
  - Validation: mobility topology, safety boundaries, portal/NavMesh contracts,
    semantic props, objective visuals, and budget.

- [ ] **T23. Author the convenience-store/commons bundle**
  - Owns: zone 8 builder/tests only.
  - Depends on: T12, T20.
  - Requirements: R3-R8, R12-R15.
  - Validation: retail/commons topology, shelving/checkout/left-item affordance,
    portal/NavMesh contracts, objective visuals, and budget.

- [ ] **T24. Build, resolve, import, and compose `r1-m2`**
  - Depends on: T21-T23.
  - Apply gates: every Blender invocation and UE revision write.
  - Requirements: R1-R10, R12-R15.
  - Validation: new full revision plan/import/compose receipts; all eight
    zones/portals/paths/views; at least 8 PBR sets and 12 hero props; overlay
    launch/restart; objective visuals and absolute budgets.

- [ ] **T25. Accept and publish M2**
  - Depends on: T24.
  - Gate/sequence: candidate commit → authorized isolated live run → external
    manifest → review → evidence-pointer commit → push.
  - Validation: full traversal graph, interactions/overlay, browser controls,
    30-minute dev-profile telemetry/memory gate, secret-free evidence, and Mac
    handoff.

- [ ] **T26. Add the non-blocking read-only Studio world catalog**
  - Owns: server registry/routes/tests; minimal professional UI only if
    separately accepted.
  - Depends on: T16; may run in parallel with T17-T25.
  - Requirements: R2, R11, R13-R15.
  - Validation: allowlisted sanitized registry and active summary, auth and
    traversal negatives, no arbitrary map activation; restrained professional
    non-chat/AI visual language; frontend build if frontend changes.

## Phase G — M3 Authoritative Simulation

- [ ] **T27. Provision a matching full UE 5.3.2 development toolchain**
  - Depends on: T1.
  - Blocked on: administrator/infrastructure owner.
  - Requirements: R10, R12-R13.
  - Validation: exact engine/build/SDK manifest, RunUAT/UBT/UHT/headers,
    user-space compiler access, clean sample plugin compile and cook smoke.

- [ ] **T28. Implement compiled WorldSession lifecycle and typed transport**
  - Owns: isolated `unreal_plugins/` code/tests.
  - Depends on: T20, T27.
  - Requirements: R1-R2, R9-R10, R12-R15.
  - Validation: lifecycle, monotonic time, lease, stable UUID registry, state
    generation, typed commands, idempotency, stale rejection, timeout
    reconciliation, telemetry; no runtime switching.

- [ ] **T29. Implement authoritative overlays, checkpoints, and persistence**
  - Depends on: T10, T28.
  - Requirements: R9-R10, R12-R15.
  - Validation: atomic apply/clear, baseline reset, checkpoint restore,
    transforms/physics/door/appliance/controller/task/RNG state, 100 resets,
    100 door cycles, injected failures, no duplicates or overlay leakage.

- [ ] **T30. Accept and publish M3 authoritative simulation**
  - Depends on: T25, T28-T29.
  - Gates: candidate commit, approved GPU run, and approved cook/runtime writes.
  - Validation: complete eight-zone pack, authoritative commands/overlays/
    reset/persistence, input-state correlation, eight-hour soak, post-hour-one
    memory bound, failure recovery, secret scan, external immutable evidence,
    evidence-pointer commit, branch push, and administrator handoff.

## Phase H — Optional M4 Production Distribution

- [ ] **T31. Design and approve Production deployment separately**
  - Depends on: T30.
  - Requirements: R11-R15 plus a new deployment spec.
  - Validation: cooked build, durable service identity/supervision, capacity
    monitoring, 24-hour soak, rollback, and—only if requested—public
    DNS/TLS/Coturn/firewall/NAT threat model and acceptance.

## Notes

- M0 tasks T2-T5 can run in parallel after T1 using separate worktrees.
- M1a is the shortest convincing result and is not blocked by Studio catalog.
- M1a accepts three named slices, not finished zones 1-3. M1 finishes zones
  1-5; M2 finishes zones 1-8.
- M1a-M2 are editor-PIE dev-profile milestones; M3 is the first complete
  stateful simulation level.
- The 12-second timeline remains an optional episode/recording overlay only.
- DiverseMaps50 has prompts/logs/screenshots but no accepted loadable `.umap`
  on this host; it is reference material, not world-pack input.

## Approval

- Requested by: Codex `/root`
- Scope: code/schema/test work only; every apply gate above remains separate.
- Approved by: pending user approval
- Date: pending

# Tasks: VISTA Playable Home

Status: Integrated game-only preview accepted; production residuals open
Updated: 2026-08-15
Depends on: `requirements.md`, `design.md`

## Execution Rules

- Work from `codex/vista-playable-home` and dedicated child worktrees.
- The user's 2026-08-15 approval covers code/test edits, deterministic local
  Blender generation, disposable UE writes, and one isolated local GPU run
  after preflight.
- External downloads, paid APIs, upload, deployment, production changes and
  public exposure remain separately gated.
- Preserve GPU 1; ports 3012/3022/55570/55582/8595/8596/8615/8616/8899/8919
  and 8400; production 8000; live tmux sessions; canonical datasets; accepted
  MMG040 artifacts and historical evidence.
- Commit source/contracts/tests only. Generated `.blend`, `.glb`, Unreal
  projects/assets/maps, logs and recordings go to new append-only run roots.
- Stage explicit files and keep commits logical.

## Phase A — Approved Specification and Ownership

- [x] **T1. Replace the campus-first proposal with the playable-home spec**
  - Owns: `docs/specs/vista-playable-home/**`.
  - Requirements: R1-R13.
  - Validation: requirements, design, task traceability and user approval are
    recorded; no open question blocks r1.

- [x] **T2. Establish isolated ownership and runtime exclusions**
  - Owns: `docs/specs/vista-playable-home/ACTIVE_WORK.md`.
  - Requirements: R11, R13.
  - Validation: branch, child worktrees, owned paths, forbidden paths,
    validation and handoff rules are explicit.

## Phase B — Semantic World and Event Contracts

- [x] **T3. Implement strict HouseSpec/EventSpec/build-plan schemas**
  - Owns: `world_packs/schemas/**` and schema tests.
  - Depends on: T1-T2.
  - Requirements: R2, R4, R6-R9, R11, R13.
  - Validation: schema meta-validation plus unknown-field, duplicate-ID,
    invalid transform, traversal, graph, portal, affordance, stale revision,
    event target, executable-field and oracle-field negatives.

- [x] **T4. Author the r1 house and seven verified event fixtures**
  - Owns: `world_packs/vista_playable_home_r1/**`.
  - Depends on: T3.
  - Requirements: R2, R6-R8, R11.
  - Validation: six-room connected topology; stable IDs; complete portals,
    entities, affordances and public provenance; three r1 events executable;
    four follow-up events schema-valid.

- [x] **T5. Implement deterministic validator/resolver/compiler**
  - Owns: `tools/worlds/**` and focused tests.
  - Depends on: T3-T4.
  - Requirements: R2, R6-R9, R11-R13.
  - Validation: two identical resolutions have the same content digest;
    transforms, connectivity, overlay atomicity and sanitized public payload
    have golden tests; legacy code is untouched.

## Phase C — Reproducible Multi-room Assets

- [x] **T6. Implement the modular headless Blender house forge**
  - Owns: `tools/blender/vista_playable_home/**` and focused tests.
  - Depends on: T3-T4.
  - Requirements: R2, R8, R11-R13.
  - Validation: static tests cover stable identity, 10 cm grid placement,
    metadata, role-based collision, merge/instance policy and manifest digest.

- [x] **T7. Execute and seal two deterministic house builds**
  - Owns: one new append-only NAS run root; no generated Git files except a
    small approved preview/evidence pointer.
  - Depends on: T6.
  - Requirements: R8, R12-R13.
  - Validation: Blender exits cleanly twice; normalized manifests/digests
    match; GLB is inspectable; preview is nonblank; room/entity counts and
    file sizes are recorded.

- [ ] **T7A. Bind licensed high-detail PBR presentation assets**
  - Owns: `tools/blender/vista_playable_home_hssd/**`, focused tests, and one
    new append-only visual build under the active run root.
  - Depends on: T7.
  - Requirements: R8, R11-R13.
  - Validation: deterministic HSSD selection; CC BY-NC 4.0 attribution and
    exact source hashes; normalized orientation/origin/bounds; one primary
    mesh per logical asset; embedded PBR material retention; nonblank
    all-room Unreal screenshots. Procedural blockout remains the collision
    fallback and is not presented as the final realism result.
  - Current evidence: 23 deterministic HSSD PBR bindings, exact source hashes,
    CC BY-NC 4.0 attribution, normalized geometry and retained material/texture
    inspection pass. This remains open because a fixed nonblank screenshot for
    every room has not been retained and the current visual pass is not claimed
    photorealistic.

## Phase D — Unreal Gameplay and Composition

- [x] **T8. Implement the VistaPlayableHome gameplay source contract**
  - Owns: `unreal_plugins/VistaPlayableHome/**` and focused tests.
  - Depends on: T3.
  - Requirements: R1, R3-R6, R9, R11-R13.
  - Validation: source/manifest checks cover GameMode, interaction interface,
    pickup, door, appliance, NPC queue, event subsystem, input actions,
    state/result enums and timeout/reset behavior.
  - Evidence: the plugin compiled for UnrealEditor Development, UnrealGame
    Development and UnrealGame Shipping; the retained package tree is bound in
    `evidence.md` and the same plugin executes in the accepted runtime.

- [x] **T9. Implement deterministic Unreal import/composition scripts**
  - Owns: `tools/ue/vista_playable_home/**` and focused tests.
  - Depends on: T5-T8.
  - Requirements: R2-R9, R11-R13.
  - Validation: mocked tests cover namespace isolation, material/collision
    roles, stable tags, room/portal placement, PlayerStart, GameMode, NavMesh,
    NPC, saved-map reload and quarantine.
  - Evidence: accepted attempt 09 passed map save/reload, semantic tags,
    PlayerStart, GameMode, NavMesh bounds, dynamic lighting, deterministic
    exposure and persisted input-mapping gates.

- [x] **T10. Prove the best available gameplay path in a disposable project**
  - Owns: one append-only UE attempt and receipts.
  - Depends on: T7-T9.
  - Requirements: R1-R6, R8-R9, R12-R13.
  - Validation: record whether the current archive can use the compiled plugin
    or only existing Blueprint assets; never simulate unsupported behavior in
    Studio; map reload and room graph/NavMesh probes pass.
  - Evidence: the compiled C++ plugin, Manny character assets and composed map
    were bound into one disposable project. A real `UnrealEditor -game`
    runtime returned typed door, pickup, NPC-navigation and event results.

## Phase E — Agent API and Remote Game Surface

- [x] **T11. Add typed Vista World compile/session/action APIs**
  - Owns: new bounded server modules/routes/tests; avoid unrelated `App.jsx`
    edits.
  - Depends on: T5, T8-T9.
  - Requirements: R4-R7, R9, R11, R13.
  - Validation: auth/session-generation/action allowlist, timeout, event
    start/reset, stale target, oracle leak, arbitrary-code and rollback tests.
  - Evidence: the documented `/api/vista-world` compile/revision/session/
    action/event routes, file catalog, compiler adapter, lease identity,
    generation checks and fixed UE transport are implemented. The production
    MCP surface exposes only closed typed tools; 28 focused Node tests pass.

- [x] **T12. Implement game-only runtime profile and preflight**
  - Owns: new profile/wrapper modules under
    `tools/runtime/vista_playable_home/**` and focused tests.
  - Depends on: T9-T10.
  - Requirements: R1, R3, R9-R13.
  - Validation: no Editor chrome in game mode; owned process group; fixed map;
    non-overlapping ports; read-only Sunshine/Tailscale/display/input/toolchain
    report; legacy runtime regression.
  - Evidence: the retained launch plan uses GPU 0, display `:117`, loopback
    port `55620`, a fixed map and an owned supervisor/process group. The latest
    read-only preflight reports preview/toolchain ready and one input blocker.

- [x] **T13. Add a deterministic Sunshine `VISTA World` application plan**
  - Owns: source-controlled config generator and user-level installation
    script; actual installed config is backed up and separately evidenced.
  - Depends on: T12.
  - Requirements: R10-R13.
  - Validation: dry-run diff, executable path, display, encoder and process
    lifecycle; missing uinput/uhid produces view-only status, not false ready.
  - Evidence: the installed user-level entry is generated from a mode-0600
    closed profile, Sunshine is reachable over the Tailnet, X11/NVENC capture
    is ready, and root-only uinput/uhid correctly classify it as view-only.

## Phase F — Integrated Acceptance

- [x] **T14. Launch the isolated game-only world**
  - Depends on: T7, T10-T13.
  - Requirements: R1-R13.
  - Validation: fresh GPU/display/port/tmux/disk preflight; exact clean source
    commit; advancing game frames; browser/Sunshine capture; no reserved
    runtime changed.
  - Evidence: source commit `57fc8485097cd4514a9f223cfd8fffda3d8c3c87`
    launched through the exact Sunshine profile. The 1280x720 X11 game window,
    typed READY handshake and W-key frame change are retained; GPU 1 and all
    reserved ports/services were preserved.

- [ ] **T15. Accept player, NPC and event behavior**
  - Depends on: T14.
  - Requirements: R1-R7, R9-R12.
  - Validation: uncut all-room traversal; two doors; one carried object; one
    moving NPC; `mmg_001`, `mmg_044`, `mmg_045` start/reset; fixed screenshots,
    state receipts and honest unsupported-feature ledger.
  - Current evidence: keyboard translation/jump, one door open/close with
    physical NPC crossing, keys pickup/place, NPC movement and all three event
    start/status/reset cycles pass in UE. This remains open because an uncut
    six-room traversal, cross-room carry and a second live door interaction
    were not retained.

- [x] **T16. Commit, push and publish the collaboration handoff**
  - Depends on: every completed task above.
  - Requirements: R11-R13.
  - Validation: atomic commits, explicit staging, tests/build summary, no
    secrets/generated projects, pushed `origin/codex/vista-playable-home`,
    exact Mac launch/stop instructions and residual administrator blockers.
  - Evidence: source and evidence were committed with explicit staging and
    pushed to `origin/codex/vista-playable-home`; `evidence.md` contains the
    exact Mac/Tailnet lifecycle, test totals, artifact bindings and the sole
    administrator input-device blocker. Generated projects remain on NAS.

## Residual Production Milestone

- [ ] **T17. Cook a Development game executable**
  - A complete UE 5.7.3 toolchain with RunUAT/UBT/UHT, headers and compiler is
    available; build and cook into the append-only NAS run root after T8-T10.
  - Requirements: R9-R10, R12-R13.
  - Validation: clean cook/package receipt, packaged smoke, direct Sunshine
    capture and rollback to the game-only preview profile.
  - Current evidence: RunUAT/UBT/UHT, engine source and compiler preflight are
    ready. No cook/package receipt exists yet, so the current demo remains the
    explicitly allowed `UnrealEditor -game` preview.

## Notes

- Task checkboxes reflect retained evidence, not intent.
- If the current UE archive cannot compile runtime source, T8/T9 may complete
  while T10 records a precise blocker; the web UI must not counterfeit the
  missing gameplay.
- Scope changes update requirements/design before implementation.

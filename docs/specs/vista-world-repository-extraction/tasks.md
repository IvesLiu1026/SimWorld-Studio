# Tasks: VISTA World Repository Extraction

Status: In Progress
Updated: 2026-08-21
Depends on: requirements.md, design.md

## Rules

- Do not edit production code until requirements/design/tasks are approved or explicitly waived.
- Keep each task reviewable and map it to requirement IDs.
- Do not touch GPU 1, VISTA production port 8000, canonical datasets or append-only evidence.
- Never create/push with a mismatched selected publisher principal. Wrong GitHub CLI identity blocks
  CLI bootstrap; an approved GitHub App validates its own installation independently.

## Task List

- [x] T1. Approve repository identity and migration spec
  - Depends on: none
  - Requirements: R1-R8
  - Validation: user records repo name, owner, visibility and approval in the spec.
  - Completed: 2026-08-21; public `IvesLiu1026/VISTA-World`, selected-path history.

- [ ] T2. Audit and classify retained Git history
  - Depends on: T1
  - Requirements: R2, R8
  - Validation: ledger covers selected paths and every unmerged/WIP/quarantine unique patch;
    no branch is bulk-merged or deleted.

- [ ] T3. Authenticate the intended GitHub owner and create standalone remote
  - Depends on: T1
  - Requirements: R1, R6
  - Validation: `isFork=false`, owner/name/visibility exact, `main` default, author email mapped.

- [ ] T4. Extract contracts, world packs and pure compiler with history
  - Depends on: T2, T3
  - Requirements: R2, R3, R8
  - Validation: focused unit tests pass without SimWorld imports; v1 schemas/receipts unchanged.

- [ ] T5. Extract `VistaPlayableHome` UE plugin
  - Depends on: T4
  - Requirements: R3, R5
  - Validation: dependency audit shows UE-only modules; clean plugin compile/package passes.

- [ ] T6. Extract Blender, UE and packaged runtime pipelines
  - Depends on: T4, T5
  - Requirements: R2, R3, R5
  - Validation: offline fixture suite and standalone package smoke pass; no binary asset/secret leak.

- [ ] T7. Introduce `VistaWorldTransport`
  - Depends on: T4
  - Requirements: R4, R8
  - Validation: contract/fake transport tests and standalone loopback integration pass.

- [ ] T8. Package the SimWorld Studio compatibility adapter
  - Depends on: T7
  - Requirements: R4, R8
  - Validation: pinned SimWorld smoke passes without core importing Studio server/UI modules.

- [ ] T9. Establish CI, branch protection and release metadata
  - Depends on: T3-T6
  - Requirements: R1, R2, R5-R7
  - Validation: required checks gate `main`; force push disabled; release manifest contains SHAs,
    schema versions, license inventory and rollback pin.

- [ ] T10. Establish daily issue-to-main workflow
  - Depends on: T3, T9
  - Requirements: R6, R7
  - Validation: approve and implement `../vista-world-daily-maintainer/`; one real vertical slice is
    implemented on a short worktree branch, independently validated and merged under its risk-tier
    policy; audit confirms the SHA is default-branch reachable and automation attribution is clear.

- [ ] T11. Accept standalone packaged VISTA World
  - Depends on: T5-T9
  - Requirements: R2-R5, R8
  - Validation: game-only package launches without SimWorld server/UI, renderer/gameplay receipts
    pass, Moonlight delivery blocker status remains truthful, and rollback is rehearsed.

- [ ] T12. Plan the native VISTA World Control Center
  - Depends on: T7, T8, T11
  - Requirements: R4, R5
  - Validation: separate approved UI/control-plane spec; no direct inheritance of the monolithic
    SimWorld `App.jsx`/`index.js` architecture.

## Notes

- From 2026-08-21 through 2026-12-31 there are 133 calendar days. The backlog should provide
  meaningful daily slices, but quality gates take precedence over artificial streak preservation.
- Current accepted SimWorld realism branch remains the operational rollback until T11 passes.

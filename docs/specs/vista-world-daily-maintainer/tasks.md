# Tasks: VISTA World Daily Maintainer

Status: In Progress
Updated: 2026-08-21
Depends on: requirements.md, design.md

## Rules

- Do not implement or install automation until requirements/design/tasks are approved.
- Validate the selected publisher principal before every write. A wrong `gh` login blocks `gh`
  bootstrap mode; GitHub App mode validates its installation and does not depend on `gh` login.
- Keep each task independently reviewable and test-first for security/state logic.
- Do not touch GPU, Unreal/Sunshine/Tailscale runtime, production port 8000, datasets or evidence.
- Stage named paths only; daily agent never edits its own policy/workflow after activation.

## Task List

- [x] T1. Approve product and attribution choices
  - Depends on: none
  - Requirements: R1-R11
  - Validation: record owner/name, public/private, bot/personal attribution, Tier 0 auto-merge and
    permitted model budget in the spec approval sections.
  - Completed: 2026-08-21; public `IvesLiu1026/VISTA-World`, Ives author + automation trailer,
    Tier 0 only after pilot, existing Codex login and no new paid provider.

- [x] T2. Create and protect standalone `VISTA-World`
  - Depends on: T1 and repository-extraction approval
  - Requirements: R6, R8
  - Validation: repo exists with `isFork=false`, `main` default, force-push/delete disabled and no
    automation bypass permission. Required-check contexts remain unset until T11 lands baseline CI.
  - Completed: public non-fork repo, protected `main`, strict baseline checks, no
    force-push/delete and retained topic branches. The non-bypass unattended principal remains T3.

- [ ] T3. Provision the publisher identity
  - Depends on: T1, T2
  - Requirements: R8, R10
  - Validation: headless preflight reports selected principal type, exact App installation or CLI
    login, repo and scopes; test branch/PR round-trip works. Wrong principal fails before mutation.
  - Progress: `IvesLiu1026` CLI/SSH bootstrap and protected PR round-trip passed. A non-admin,
    repo-scoped App/principal is still required before unattended publication.

- [x] T4. Define candidate and receipt contracts
  - Files: `automation/daily-maintainer/*.schema.json`, typed models
  - Depends on: T1
  - Requirements: R2, R9, R11
  - Validation: schema positive/negative tests cover missing provenance, non-allowlisted validation
    profile, any candidate command/argv, invalid status transitions and secret redaction.
  - Completed: strict schemas, typed candidate/receipt models, canonical digests and negative
    contract tests are merged to protected public `main`.

- [ ] T5. Build the 133-day micro-task inventory
  - Files: `docs/maintenance/backlog.yaml`
  - Depends on: extraction path mapping, T4
  - Requirements: R2, R4, R11
  - Validation: every item has stable ID, acceptance, allowlisted paths, risk tier and allowlisted
    offline validation profile ID; backlog/registry require human review and are protected from the
    agent; at least 14 eligible candidates survive a dry selector pass.
  - Progress: reviewed draft contains 28 useful tasks (15 Tier 0, 13 Tier 1) and a 15-day Tier 0
    selector buffer. Exact draft SHA-256 is
    `5e08d1f2f784aa5940e0606a58637e5006f0892b3c7971b2eb6fba669e2d4fa5`; it remains
    `CodexDraft` until Ives explicitly approves those bytes. The remaining 105-task capacity or
    equivalent deterministic scouts is still open.

- [ ] T6. Implement selector and deterministic scouts
  - Files: `automation/daily-maintainer/candidate.py`, tests
  - Depends on: T4, T5
  - Requirements: R1, R2, R7, R11
  - Validation: deterministic ordering, duplicate/closed/stale handling, trusted-source checks,
    no-candidate behavior and prompt-injection fixtures pass.
  - Progress: deterministic reviewed-backlog selector and fail-closed authority policy are
    implemented; dynamic structured scouts are not yet implemented.

- [x] T7. Implement isolated run/worktree manager
  - Files: `automation/daily-maintainer/cli.py`, state/lock module, tests
  - Depends on: T4
  - Requirements: R1, R3, R9
  - Validation: duplicate date, reboot catch-up, dirty checkout, stale lock and remote-main movement
    integration tests pass against a local bare remote.
  - Completed: exact HTTPS repository allowlist, replay-safe state, singleton locking, hook/filter
    neutralization and local-bare-remote tests are implemented. T13 still owns service isolation.

- [ ] T8. Implement patcher prompt boundary
  - Files: `automation/daily-maintainer/prompts/patcher.md`, patcher adapter, tests
  - Depends on: T4, T6, T7
  - Requirements: R2-R4, R8, R10
  - Validation: patcher runs under a dedicated UID/rootless container, receives only normalized
    candidate context/worktree/Codex credential, and cannot read operator home, `.ssh`, gh config,
    `SSH_AUTH_SOCK` or publisher state; adversarial text cannot expand allowlist/profile IDs.
  - Blocker: the current personal ChatGPT-managed login is attended-only under official Codex
    public/open-source automation guidance. Unattended activation requires a separately approved
    dedicated credential; implementation and report-only tests can proceed without it.
  - Progress: shell-free pinned invocation, credential/file evidence, worktree content verification,
    output schema and adversarial regression suite are implemented and independently accepted.
    A real positive T13 sandbox and compliant dedicated credential remain required.

- [x] T9. Implement deterministic guard and verifier
  - Files: `automation/daily-maintainer/guard.py`, `verifier.py`, tests
  - Depends on: T4, T7
  - Requirements: R3-R5, R7, R9
  - Validation: protected path, symlink, binary, secret, line/file limit, assertion deletion,
    test weakening and unapproved external side-effect fixtures all fail closed.
  - Completed: guard/verifier reject ignored worktree state, protected toolchain/config authority,
    validation-time mutation and unbounded Git output. Offline focused validation uses an injected,
    repository-external pinned Python runtime.

- [ ] T10. Implement publisher and GitHub PR contract
  - Files: `automation/daily-maintainer/publisher.py`, tests
  - Depends on: T3, T4, T9
  - Requirements: R5, R6, R8, R9
  - Validation: fake-GitHub integration proves digest binding, separate principal, draft PR body,
    no force-push/duplicate PR, author/committer/PR actor attribution and no model credential.
  - Progress: publisher orchestration contract, principal/policy checks and draft-only read-back
    protocol are implemented. The typed finalizer/authority bridge is under acceptance review;
    immutable spool plus concrete Git/GitHub/runtime adapters remain open.

- [ ] T11. Add repo-owned CI and merge policy
  - Files: `.github/workflows/daily-maintainer-ci.yml`, policy docs
  - Depends on: T2, T9, T10
  - Requirements: R5, R6, R8-R10
  - Validation: reviewed CI first lands and succeeds without preconfigured required contexts; then
    exact contexts are enabled with minimal permissions. Separate promotion controller marks a draft
    ready only after digest/CI pass; bot-authored workflow changes are rejected; Tier 1+ do not merge.
  - Progress: reviewed baseline CI landed and exact GitHub Actions check identities now gate `main`.
    Daily-maintainer CI, promotion and risk-tier enforcement remain open.

- [ ] T12. Implement receipt journal and heartbeat ingress
  - Files: receipt publisher, `.github/workflows/daily-maintainer-heartbeat.yml`, tests, runbook
  - Depends on: T7-T11
  - Requirements: R1, R7-R10
  - Validation: local receipt retention is at least 180 days; selected publisher appends exact
    date/repo marker + digest to one journal issue for merged/no-change; heartbeat detects wrong actor,
    malformed/missing/duplicate receipts and updates only one incident issue.

- [ ] T13. Add isolated systemd services/timer
  - Files: `ops/systemd/*`, sandbox tests, runbook
  - Depends on: T7-T12
  - Requirements: R1, R3, R8-R10
  - Validation: patcher and publisher use distinct credential boundaries; enable/disable/reboot/
    missed-run drill, Asia/Taipei schedule, singleton lock and three-failure halt all pass without
    touching runtime services.

- [ ] T14. Run report-only and canary acceptance
  - Depends on: T5-T13
  - Requirements: R1-R11
  - Validation: three report-only runs produce safe candidates/no-change truthfully, followed by
    one explicitly approved draft PR promoted by the independent controller; merge SHA is reachable
    from remote `main`.

- [ ] T15. Run two-week PR-only pilot
  - Depends on: T14
  - Requirements: R1-R11
  - Validation: 14 run receipts reviewed; zero protected-path escape, secret exposure, direct-main
    push or rollback incident; no-change and rejection reasons are actionable.

- [ ] T16. Enable Tier 0 auto-merge
  - Depends on: T15 and explicit user approval
  - Requirements: R5-R9
  - Validation: one Tier 0 PR auto-merges only after required CI; simulated failure remains open;
    emergency disable/revert drill passes.

## Notes

- Public standalone `IvesLiu1026/VISTA-World` now exists; server `gh` and Git SSH both use
  `IvesLiu1026`. The older stored CLI login is inactive and must never be selected by automation.
- Baseline CI and required checks are live. Repository auto-merge and unattended publication remain
  disabled while the deterministic core, isolated publisher and pilot are incomplete.
- Tier 1 auto-merge, GitHub Actions-hosted model execution and UE/Blender maintenance require
  separate approvals after the initial pilot.

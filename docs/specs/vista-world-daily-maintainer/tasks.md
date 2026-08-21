# Tasks: VISTA World Daily Maintainer

Status: Draft
Updated: 2026-08-21
Depends on: requirements.md, design.md

## Rules

- Do not implement or install automation until requirements/design/tasks are approved.
- Do not publish while `gh` is authenticated as an account other than `IvesLiu1026`.
- Keep each task independently reviewable and test-first for security/state logic.
- Do not touch GPU, Unreal/Sunshine/Tailscale runtime, production port 8000, datasets or evidence.
- Stage named paths only; daily agent never edits its own policy/workflow after activation.

## Task List

- [ ] T1. Approve product and attribution choices
  - Depends on: none
  - Requirements: R1-R11
  - Validation: record owner/name, public/private, bot/personal attribution, Tier 0 auto-merge and
    permitted model budget in the spec approval sections.

- [ ] T2. Create and protect standalone `VISTA-World`
  - Depends on: T1 and repository-extraction approval
  - Requirements: R6, R8
  - Validation: repo exists with `isFork=false`, `main` default, required checks configured,
    force-push/delete disabled, and no automation bypass permission.

- [ ] T3. Provision the publisher identity
  - Depends on: T1, T2
  - Requirements: R8, R10
  - Validation: headless preflight reports exact owner/login/scopes; test branch/PR round-trip works;
    wrong-account fixture and current wrong `gh` identity fail before remote mutation.

- [ ] T4. Define candidate and receipt contracts
  - Files: `automation/daily-maintainer/*.schema.json`, typed models
  - Depends on: T1
  - Requirements: R2, R9, R11
  - Validation: schema positive/negative tests cover missing provenance, unsafe commands, invalid
    status transitions and secret redaction.

- [ ] T5. Build the 133-day micro-task inventory
  - Files: `docs/maintenance/backlog.yaml`
  - Depends on: extraction path mapping, T4
  - Requirements: R2, R4, R11
  - Validation: every item has stable ID, acceptance, allowlisted paths, risk tier and offline test;
    protected surfaces absent; at least 14 eligible candidates survive a dry selector pass.

- [ ] T6. Implement selector and deterministic scouts
  - Files: `automation/daily-maintainer/candidate.py`, tests
  - Depends on: T4, T5
  - Requirements: R1, R2, R7, R11
  - Validation: deterministic ordering, duplicate/closed/stale handling, trusted-source checks,
    no-candidate behavior and prompt-injection fixtures pass.

- [ ] T7. Implement isolated run/worktree manager
  - Files: `automation/daily-maintainer/cli.py`, state/lock module, tests
  - Depends on: T4
  - Requirements: R1, R3, R9
  - Validation: duplicate date, reboot catch-up, dirty checkout, stale lock and remote-main movement
    integration tests pass against a local bare remote.

- [ ] T8. Implement patcher prompt boundary
  - Files: `automation/daily-maintainer/prompts/patcher.md`, patcher adapter, tests
  - Depends on: T4, T6, T7
  - Requirements: R2-R4, R8, R10
  - Validation: patcher receives only normalized candidate context and no publisher credentials;
    adversarial repository text cannot expand allowlist or commands.

- [ ] T9. Implement deterministic guard and verifier
  - Files: `automation/daily-maintainer/guard.py`, `verifier.py`, tests
  - Depends on: T4, T7
  - Requirements: R3-R5, R7, R9
  - Validation: protected path, symlink, binary, secret, line/file limit, assertion deletion,
    test weakening and unapproved external side-effect fixtures all fail closed.

- [ ] T10. Implement publisher and GitHub PR contract
  - Files: `automation/daily-maintainer/publisher.py`, tests
  - Depends on: T3, T4, T9
  - Requirements: R5, R6, R8, R9
  - Validation: fake-GitHub integration proves digest binding, draft PR body, no force-push,
    no duplicate PR, attribution trailer and no model credential in publisher process.

- [ ] T11. Add repo-owned CI and merge policy
  - Files: `.github/workflows/daily-maintainer-ci.yml`, policy docs
  - Depends on: T2, T9, T10
  - Requirements: R5, R6, R8-R10
  - Validation: required checks run with minimal permissions; bot-authored workflow changes are
    rejected; Tier 1+ never auto-merge.

- [ ] T12. Add systemd user service/timer and heartbeat
  - Files: `ops/systemd/*`, `.github/workflows/daily-maintainer-heartbeat.yml`, runbook
  - Depends on: T7-T11
  - Requirements: R1, R7, R9, R10
  - Validation: enable/disable/reboot/missed-run drill, Asia/Taipei schedule, singleton lock and
    three-failure halt all produce expected receipts without touching runtime services.

- [ ] T13. Run report-only and canary acceptance
  - Depends on: T5-T12
  - Requirements: R1-R11
  - Validation: three report-only runs produce safe candidates/no-change truthfully, followed by
    one explicitly approved real PR whose merge SHA is reachable from remote `main`.

- [ ] T14. Run two-week PR-only pilot
  - Depends on: T13
  - Requirements: R1-R11
  - Validation: 14 run receipts reviewed; zero protected-path escape, secret exposure, direct-main
    push or rollback incident; no-change and rejection reasons are actionable.

- [ ] T15. Enable Tier 0 auto-merge
  - Depends on: T14 and explicit user approval
  - Requirements: R5-R9
  - Validation: one Tier 0 PR auto-merges only after required CI; simulated failure remains open;
    emergency disable/revert drill passes.

## Notes

- Current GitHub connector is `IvesLiu1026`, but server `gh` remains `aN0NyMoUs0000`.
- Git SSH can push to the existing fork, but cannot create the missing standalone repository.
- `IvesLiu1026/VISTA-World` did not exist at the 2026-08-21 audit.
- Tier 1 auto-merge, GitHub Actions-hosted model execution and UE/Blender maintenance require
  separate approvals after the initial pilot.

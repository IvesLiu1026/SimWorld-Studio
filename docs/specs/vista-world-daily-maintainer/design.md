# Design: VISTA World Daily Maintainer

Status: Draft
Updated: 2026-08-21
Depends on: requirements.md

## Summary

採用「server systemd patcher + GitHub CI/publisher gate」的混合架構。常駐 server 每天
在隔離 worktree 中讓 Codex 處理一個受信任、低風險候選；deterministic guard 在模型
之外驗證 diff 與 tests；正確 GitHub identity 再 push branch/open PR。GitHub Actions
只負責不含模型 secret 的獨立 CI、merge policy 與 missed-run heartbeat。tmux 只供人工
觀察，不承擔排程。

## Architecture and Flow

```text
systemd --user timer (09:17 Asia/Taipei, Persistent=true)
        |
        v
single-run lock + identity/repo/base preflight
        |
        v
trusted backlog / deterministic scouts
        |
        v
ephemeral branch + worktree ---> Codex patcher (no publisher token)
        |                                  |
        |                                  v
        +---------------------- deterministic diff guard
                                           |
                                           v
                           fresh verifier (no model secret)
                                           |
                                           v
                          commit + SSH push + draft PR
                                           |
                                           v
                             GitHub required CI/checks
                                           |
                              +------------+------------+
                              |                         |
                         Tier 0 merge              review/blocker
                              |
                              v
                    remote main reachability receipt
```

GitHub scheduled heartbeat verifies that a run receipt arrived. It may open/update one incident
issue but never fabricates a code commit when the server did not produce a valid patch.

## Trust Boundaries

1. **Selector** reads only normalized backlog fields or deterministic finding JSON. It treats all
   repository/issue prose as untrusted data and never concatenates arbitrary text into privileged
   instructions.
2. **Patcher** gets a clean worktree, candidate contract, allowlisted files and focused commands.
   It does not receive GitHub write credentials, sudo, production runtime ownership or paid keys.
3. **Guard/verifier** is deterministic, runs in a fresh process, and rejects protected paths,
   large/binary files, secret-like content, excessive diffs and test weakening.
4. **Publisher** receives only a validated patch/worktree and minimal repo-write identity. It cannot
   invoke the model or edit the patch.
5. **GitHub CI** reruns repo-owned checks from the reviewed default-branch workflow. Daily agent
   cannot edit `.github/workflows/**`.

## Interfaces and Contracts

### Candidate manifest

```yaml
id: VW-DM-0001
title: Reject non-finite coordinates in room schema
risk_tier: 1
allowed_paths:
  - contracts/python/vista_world/**
  - tests/contracts/**
acceptance:
  - malformed NaN/Infinity input fails closed
validation:
  - uv run --offline --with pytest pytest -q tests/contracts/test_coordinates.py
source:
  kind: curated_backlog
  maintainer_approved: true
```

Required fields: stable ID, title, source, risk tier, allowed paths, acceptance criteria, validation
commands, expected external side effects (`none` for unattended candidates), and optional issue URL.

### Run receipt

```json
{
  "schema_version": "vista.world.daily-maintainer.receipt.v1",
  "run_id": "2026-08-21/IvesLiu1026/VISTA-World",
  "status": "merged",
  "base_sha": "...",
  "head_sha": "...",
  "candidate_id": "VW-DM-0001",
  "validation": [{"command_id": "focused-test", "exit_code": 0, "output_sha256": "..."}],
  "protected_paths_touched": [],
  "pr_url": "...",
  "merge_sha": "...",
  "automated": true
}
```

Allowed status values: `skipped`, `no_change`, `patch_rejected`, `validation_failed`, `pr_open`,
`merged`, `infrastructure_failed`, `halted`. Raw secret-bearing logs never enter receipts.

### Local CLI

Planned commands:

```text
vista-daily-maintainer preflight
vista-daily-maintainer select --date YYYY-MM-DD
vista-daily-maintainer patch --candidate <id>
vista-daily-maintainer verify --run <run-id>
vista-daily-maintainer publish --run <run-id>
vista-daily-maintainer status [--json]
```

Every command is idempotent or refuses an incompatible existing state. `publish` verifies the exact
patch digest produced by `verify`.

## Candidate Policy

Initial allowlist, ordered by risk:

1. broken internal documentation link or documented command/path drift;
2. test-only regression for already specified behavior;
3. one contract/schema invariant plus negative test;
4. pure Python compiler/parser fail-closed edge case;
5. Node fake-transport timeout, cleanup, validation or redaction case;
6. hard-coded worktree/POSIX portability bug with fixture test;
7. frontend label/ARIA/keyboard regression with build and focused test;
8. one standalone extraction interface/test slice.

The selector prefers explicitly approved `agent:ready`, `scope:tiny`, `risk:low` items. Dynamic
scouts may nominate a candidate but must produce a reproducible command and evidence before patching.

Protected surfaces include runtime lifecycle, real Unreal/Blender/GPU execution, ports, Sunshine,
Tailscale, systemd state, auth/secrets, workflow/policy files, dependencies/lockfiles, binaries,
world package receipts, datasets, scenes, reports, generated artifacts and NAS paths.

## Git and GitHub Model

- Canonical target: non-fork `IvesLiu1026/VISTA-World`, `main` default.
- Branch: `codex/daily/YYYY-MM-DD-<candidate-slug>` from current remote `main`.
- One Conventional Commit per run; no amend/force-push after PR review begins.
- Draft PR contains candidate ID, evidence, diff limit, validation commands/results and
  `Automated-by: Codex Daily Maintainer`.
- `main` requires PR and checks; automation receives no bypass permission.
- Recommended write identity is a repo-scoped GitHub App. Bootstrap may use the server's `gh` CLI
  only after its active account is verified as `IvesLiu1026`; Git SSH alone cannot create a repo.

Attribution is a product decision, not a hidden implementation detail. Default design uses a bot
author. If the user explicitly selects personal attribution, the exact author/committer/trailer
contract must be recorded before rollout.

## Validation Profiles

Candidate-specific focused tests are mandatory. The verifier can additionally choose only offline,
isolated profiles:

```bash
git diff --check

cd tools
uv lock --check
uv run --group dev python -m unittest discover -s tests -p 'test_*.py'

cd simworld_studio_workspace/web
npm run test:server:unit
node --test --test-reporter=tap server/tests/*.test.js

cd unreal_plugins/VistaAnimationContentApi
node --test Tests/offline-contract.test.mjs Tests/mmg040-content-profile.test.mjs
sh -n Scripts/install-plugin.sh
sh -n Scripts/build-plugin.sh
```

Frontend changes also require the established build. Browser tests that reuse shared ports,
integration/pipeline suites, commandlets and real runtime checks remain manual until isolated.

## Data Model and Migration

- Backlog lives in versioned manifests with stable IDs; completion binds candidate ID to merge SHA.
- Mutable run state, locks and raw logs live outside Git under an operator-owned state directory.
- Sanitized receipts are uploaded as CI artifacts or summarized in one rolling GitHub issue.
- Only materially changed findings become repository audit documents; no daily timestamp-only file.
- Extraction from the current fork preserves selected-path history before daily automation is enabled.

## File Plan

Expected implementation in the standalone repo:

```text
automation/daily-maintainer/
  cli.py
  candidate.py
  guard.py
  verifier.py
  publisher.py
  receipt.schema.json
  prompts/patcher.md
ops/systemd/
  vista-world-daily-maintainer.service
  vista-world-daily-maintainer.timer
docs/maintenance/
  policy.md
  backlog.yaml
.github/workflows/
  daily-maintainer-ci.yml
  daily-maintainer-heartbeat.yml
tests/automation/daily-maintainer/
```

The agent itself may edit none of the policy, prompt, workflow or systemd files after activation.

## Failure Handling

- Identity/repo mismatch: fail before worktree creation or remote write.
- No eligible candidate: emit `no_change`; do not commit.
- Model timeout/budget exhaustion: discard incomplete worktree; no fallback commit.
- Guard or tests fail: retain sanitized digest/summary, remove credentials, do not push.
- Remote main moves: recreate from latest base and rerun; never force-push reviewed work.
- Existing bot PR: finish/reconcile it or skip; do not open a queue of stale PRs.
- Three consecutive failures: halt unattended publication and update one incident issue.
- Post-merge regression: create a revert PR; do not silently patch `main`.
- Emergency rollback: disable user timer and GitHub App, close bot PR, preserve receipts.

## Testing Strategy

- Unit tests for candidate schema, path glob resolution, size/line limits, receipt redaction and
  state transitions.
- Adversarial fixtures for prompt injection, symlink escape, binary/secret content, test deletion,
  workflow self-edit and malicious validation commands.
- Integration test with a local bare remote and fake `gh`; no real GitHub writes.
- Report-only dry runs against the extracted repo.
- One user-approved canary PR, followed by a two-week PR-only pilot.
- Fault injection for server reboot, duplicate timer, stale lock, API timeout and main movement.

## Rollout and Observability

1. Approve repo identity, visibility, attribution and this spec.
2. Create standalone repo, selected-path seed and protected `main`.
3. Correct server GitHub identity or install a repo-scoped GitHub App.
4. Build/test deterministic selector, guard, verifier, publisher and receipt pipeline.
5. Run three report-only dry runs; confirm zero writes and useful candidate selection.
6. Run one approved canary PR.
7. Operate 14 days in PR-only mode; require zero policy escapes/rollback incidents.
8. Enable Tier 0 auto-merge only; Tier 1 remains a separate approval.
9. Maintain at least 14 days backlog buffer and review monthly metrics.

Metrics: runs, eligible candidates, patch acceptance, validation failure, time-to-PR, merge rate,
rollback rate, no-change rate, policy rejection and consecutive failures. A heartbeat detects missing
receipts but never substitutes a fake commit.

## Operational Impact

- One local Codex attempt per day plus deterministic tests; no GPU or runtime workload.
- GitHub Actions consumes CI minutes only after a branch/PR or heartbeat run.
- The server must remain on for AI patching; `Persistent=true` provides one catch-up run.
- A correctly scoped GitHub write identity is required; current Codex GitHub connector cannot be
  assumed available to a headless systemd process.

## Rejected Options

- **tmux scheduler:** lacks reliable boot persistence, locking and declarative failure state.
- **direct push to main:** bypasses independent verification and makes rollback/audit weaker.
- **daily empty/timestamp commit:** creates misleading history without engineering value.
- **pure GitHub Actions AI by default:** requires an explicitly funded API key and exposes more
  untrusted repository context; it remains an optional future runner.
- **unbounded autonomous issue processing:** exposes prompt-injection and scope-escalation risk.

## Traceability

- R1 -> systemd schedule, locking, idempotent CLI
- R2 -> candidate manifest, selector and trust boundary
- R3-R4 -> isolated worktree, diff guard and protected surfaces
- R5 -> fresh verifier and validation profiles
- R6 -> branch/PR/CI/merge tiers
- R7 -> no-change receipt and heartbeat behavior
- R8 -> GitHub model and attribution contract
- R9 -> receipt schema, metrics and incident handling
- R10 -> patch attempt/budget and runner selection
- R11 -> versioned backlog and selector buffer

## Open Questions

- Public/private and attribution mode must be approved before repository creation.
- Branch-protection feature availability depends on repository plan/visibility.
- GitHub App is preferred; CLI + SSH may be used only as an explicit bootstrap choice.

## Approval

- Requested by: Codex integrator
- Approved by:
- Date:

# Design: VISTA World Daily Maintainer

Status: Approved
Updated: 2026-08-21
Depends on: requirements.md

## Summary

採用「隔離 service-account patcher + credential-free verifier + separate publisher + GitHub
CI」的混合架構。常駐 server 每天在專用 UID/rootless container 與隔離 worktree 中讓
Codex 處理一個受信任、低風險候選；deterministic guard 在模型之外驗證 diff 與 tests；
另一個 publisher principal 才能 push/open PR。GitHub Actions 負責不含模型 secret 的
獨立 CI、promotion/merge policy 與 missed-run heartbeat。tmux 只供人工觀察，不承擔排程。

## Architecture and Flow

```text
systemd timer (09:17 Asia/Taipei, Persistent=true)
        |
        v
single-run lock + identity/repo/base preflight
        |
        v
trusted backlog / deterministic scouts
        |
        v
ephemeral worktree ---> isolated Codex patcher UID/container
        |                                  |
        |                                  v
        +---------------------- deterministic diff guard
                                           |
                                           v
                       fresh verifier (no model/publisher secret)
                                           |
                                           v
                    separate publisher: commit + push + draft PR
                                           |
                                           v
                             GitHub required CI/checks
                                           |
                              promotion controller marks ready
                                           |
                              +------------+------------+
                              |                         |
                         Tier 0 merge              review/blocker
                              |
                              v
                    remote main reachability receipt
```

Publisher also appends one structured digest comment to a single receipt-journal issue. GitHub
scheduled heartbeat verifies the exact date marker, publisher actor and digest arrived. It may
update one incident issue but never fabricates a code commit when the server produced no valid patch.

## Trust Boundaries

1. **Selector** reads only a human-reviewed, protected backlog or deterministic finding JSON. It
   treats all repository/issue prose as untrusted data and never concatenates arbitrary text into
   privileged instructions. GitHub labels/body are not executable candidate authority in v1.
2. **Patcher** runs as a dedicated Unix UID or rootless container. It gets only a clean worktree,
   normalized candidate, allowlisted files and its scoped Codex credential. The sandbox does not
   mount the operator home, `~/.ssh`, gh config, `SSH_AUTH_SOCK`, publisher token/state, sudo,
   production runtime ownership or paid keys. A worktree alone is not a credential boundary. The
   operator's personal ChatGPT-managed `auth.json` is never copied into this public-repository
   unattended boundary; it is limited to attended canary work.
3. **Guard/verifier** is deterministic, runs in a fresh process, and rejects protected paths,
   large/binary files, secret-like content, excessive diffs and test weakening.
4. **Publisher** runs under a separate principal and receives only an immutable patch digest plus
   validated worktree. Its short-lived GitHub App credential is injected after patcher exit. It
   cannot invoke the model or edit the patch.
5. **GitHub CI** reruns repo-owned checks from the reviewed default-branch workflow. Daily agent
   cannot edit `.github/workflows/**`.
6. **Promotion controller** is separate from patcher. It verifies risk tier, receipt digest and
   required CI before changing draft status; only then can the approved merge policy act.

## Interfaces and Contracts

### Candidate manifest

```yaml
id: VW-DM-0001
title: Cover strict loader rejection of non-object JSON values
risk_tier: 0
allowed_paths:
  - tools/tests/test_vista_playable_home_contracts.py
acceptance:
  - arrays, strings, numbers, booleans and null fail with the stable top-level JSON error
validation_profiles:
  - tools-python-offline
expected_external_side_effects: none
source:
  kind: curated_backlog
  manifest_revision: 7
  approved_by: IvesLiu1026
```

Required fields: stable ID, title, source, risk tier, allowed paths, acceptance criteria, validation
profile IDs, expected external side effects (`none` for unattended candidates), manifest revision,
approver and optional issue URL. Validation profiles are code-owned registry entries whose argv/cwd
are resolved by the verifier without a shell; candidate YAML cannot provide commands or arguments.

### Run receipt

```json
{
  "schema_version": "vista.world.daily-maintainer.receipt.v1",
  "run_id": "2026-08-21/IvesLiu1026/VISTA-World@1111111111111111111111111111111111111111",
  "run_date": "2026-08-21",
  "repository": "IvesLiu1026/VISTA-World",
  "status": "merged",
  "base_sha": "1111111111111111111111111111111111111111",
  "head_sha": "2222222222222222222222222222222222222222",
  "candidate_id": "VW-DM-0001",
  "validation": [{
    "command_id": "tools-python-offline",
    "exit_code": 0,
    "output_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "duration_ms": 1234,
    "timed_out": false
  }],
  "diff_summary": {
    "files_changed": 1,
    "production_lines": 0,
    "test_lines": 12,
    "patch_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  },
  "protected_paths_touched": [],
  "pr_url": "https://github.com/IvesLiu1026/VISTA-World/pull/42",
  "merge_sha": "3333333333333333333333333333333333333333",
  "duration_ms": 5000,
  "failure_category": null,
  "actors": {
    "commit_author": {"name": "Ives Liu", "email": "zhiy0517xiang@gmail.com"},
    "git_committer": {
      "name": "VISTA World Publisher",
      "email": "publisher@users.noreply.github.com"
    },
    "pr_actor": "vista-world-publisher[bot]",
    "promotion_actor": "vista-world-publisher[bot]"
  },
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

V1 selector accepts only items from the protected backlog merged through human review. Issue labels
such as `agent:ready`, `scope:tiny`, `risk:low` are organizational hints, not authorization. A future
issue-backed selector must verify the label event actor against a configured maintainer allowlist
and still normalize body content into a reviewed manifest before execution. Dynamic scouts can use
only built-in validation profile IDs and structured evidence; they cannot invent commands.

Protected surfaces include runtime lifecycle, real Unreal/Blender/GPU execution, ports, Sunshine,
Tailscale, systemd state, auth/secrets, workflow/policy files, dependencies/lockfiles, binaries,
world package receipts, datasets, scenes, reports, generated artifacts and NAS paths.

## Git and GitHub Model

- Canonical target: non-fork `IvesLiu1026/VISTA-World`, `main` default.
- Branch: `codex/daily/YYYY-MM-DD-<candidate-slug>` from current remote `main`.
- One Conventional Commit per run; no amend/force-push after PR review begins.
- Draft PR contains candidate ID, evidence, diff limit, validation profile/results and
  `Automated-by: Codex Daily Maintainer`.
- `main` requires PR and checks; automation receives no bypass permission.
- Recommended write identity is a repo-scoped GitHub App. Bootstrap may use the server's `gh` CLI
  only after its active account is verified as `IvesLiu1026`; Git SSH alone cannot create a repo.
- Bootstrap first disables force-push/delete and lands reviewed CI without required-check contexts;
  required contexts are enabled only after that CI has run successfully on `main`.
- Publisher opens draft. A separate promotion controller marks it ready only after required checks
  and receipt digest pass. Draft PRs are never directly auto-merged.

Attribution is a product decision, not a hidden implementation detail. The approved v1 contract
uses the mapped Ives author identity and the mandatory automation trailer. The publisher App remains
the separately recorded committer/PR actor; this does not represent an unattended commit as manual
human work.

| Role | Recommended principal | Recorded evidence |
|---|---|---|
| Commit author | `Ives Liu <zhiy0517xiang@gmail.com>` | exact author email + `Automated-by: Codex Daily Maintainer` |
| Git committer | publisher GitHub App/bot | committer identity + signed/verified state |
| PR actor | repo-scoped publisher App | PR author/login |
| Promotion actor | separate workflow/App | check/promotion event actor |
| Human reviewer | `IvesLiu1026` when required | GitHub review event |

## Validation Profiles

Candidate-specific focused tests are mandatory. Candidate manifests reference immutable profile IDs;
the verifier maps each ID to fixed `cwd` and argv and invokes it without `sh -c`. Initial registry
entries correspond to these offline, isolated commands:

```bash
git diff --check

cd tools
python3 -m unittest \
  tests/test_vista_playable_home_contracts.py \
  tests/test_vista_playable_home_compiler.py

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
The `python3` entry is resolved through the verifier's pinned executable registry. Production T13
must inject a root-owned, immutable interpreter outside the patcher-writable worktree with the
locked dependencies preinstalled offline; a repository `.venv` and inherited package cache are
never validation authority.

## Data Model and Migration

- Backlog lives in human-reviewed protected manifests with stable IDs; completion binds candidate
  ID to merge SHA. Daily agent cannot edit backlog or validation registry.
- Mutable run state, locks and raw logs live outside Git under an operator-owned state directory.
- Full sanitized receipts are append-only local JSON, retained at least 180 days. Publisher appends
  one comment to a single `VISTA Daily Maintainer Receipt Journal` issue with marker
  `<!-- vista-daily-receipt:YYYY-MM-DD:repo -->`, status, base/head SHA, candidate/PR and receipt
  SHA-256. The comment actor must match the selected publisher principal.
- At 23:17 Asia/Taipei, heartbeat queries that issue for the exact date/repo marker, validates actor
  and digest syntax, and updates a single incident issue if absent. `no_change` uses the same ingress;
  no PR or commit is required. Local JSON is canonical; the journal is the remote liveness index.
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
  validation-profiles.yaml
.github/workflows/
  daily-maintainer-ci.yml
  daily-maintainer-heartbeat.yml
tests/automation/daily-maintainer/
```

The agent itself may edit none of the backlog, validation profiles, policy, prompt, workflow or
systemd files after activation.

## Failure Handling

- Selected publisher principal/repo mismatch: fail before worktree creation or remote write. A `gh`
  mismatch blocks CLI bootstrap; a GitHub App run validates its installation instead.
- No eligible candidate: emit `no_change`; do not commit.
- Model timeout/budget exhaustion: discard incomplete worktree; no fallback commit.
- No dedicated automation credential: run selector/report-only and emit a truthful blocker; never
  fall back to the operator's personal ChatGPT-managed login.
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
- Integration test with a local bare remote and fake GitHub App/`gh`; no real GitHub writes.
- Sandbox test proves patcher cannot read operator home, SSH/gh credentials, publisher state or
  inherited agent sockets; same-UID worktree isolation does not pass acceptance.
- Validation registry tests prove YAML-supplied commands/arguments and shell metacharacters fail.
- Promotion test proves draft remains draft until independent required checks and digest pass.
- Receipt journal/heartbeat tests cover `merged`, `no_change`, wrong actor, duplicate marker,
  malformed digest, missing day and retention behavior.
- Report-only dry runs against the extracted repo.
- One user-approved canary PR, followed by a two-week PR-only pilot.
- Fault injection for server reboot, duplicate timer, stale lock, API timeout and main movement.

## Rollout and Observability

1. Approve repo identity, visibility, attribution and this spec.
2. Create standalone repo; disable force-push/delete, then land baseline CI before enabling its
   required-check contexts.
3. Provision dedicated patcher sandbox plus separate publisher App, or explicitly approved `gh`
   bootstrap principal.
4. Build/test deterministic selector, validation registry, guard, verifier, publisher, promotion and
   receipt-journal pipeline.
5. Run three report-only dry runs; confirm zero writes and useful candidate selection.
6. Run one approved canary draft PR through independent ready-for-review promotion.
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
- Dedicated patcher UID/container and publisher credential injection may require administrator help;
  until available, runs remain report-only and are not called credential-separated unattended mode.
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

- Resolved: public `IvesLiu1026/VISTA-World`, selected-path history, Ives author plus explicit
  automation trailer, and Tier 0-only auto-merge after pilot.
- Resolved: correctly authenticated CLI + SSH is the bootstrap publisher; repo-scoped GitHub App is
  required before unattended auto-merge.
- Branch-protection feature availability will be detected after repository creation; unsupported
  protections fail closed and remain a documented blocker rather than being silently skipped.

## Approval

- Requested by: Codex integrator
- Approved by: IvesLiu1026
- Date: 2026-08-21

# Tasks: SimWorld migration to 140.113.215.82

Status: In progress
Updated: 2026-07-15
Depends on: `requirements.md`, `design.md`

## Rules

- Never overwrite an existing remote path or use rsync `--delete`.
- Never copy secrets, provider login homes, raw provider logs or SSH material.
- Do not use sudo, change groups, install system packages, touch80/443/14500, or launch public listeners in this task.
- Keep source and runtime activation separate; a successful transfer is not a successful deployment.
- Update each checkbox only after the validation evidence exists.

## Phase 0 — Discovery and specification

- [x] **T0.1** Record Studio branch, HEAD, dirty counts and source size.
  Requirements: MIG-001, MIG-006
  Evidence: `codex/vista-loopback`, `caf6d930...`, 48 tracked modified, 69 untracked after adding this six-file migration spec package; approximately1.1 GiB including disposable dependencies/evidence.
- [x] **T0.2** Record Python/bridge dirty state and size.
  Requirements: MIG-001, MIG-003
  Evidence: `main`, `0921180909105158a7ff87445eb032706b10113e`, 5 tracked modified, 104 untracked, approximately802 MiB including308 MiB venv.
- [x] **T0.3** Audit target hardware, tools, disk, GPU, ports and missing services read-only.
  Requirements: MIG-007, MIG-010
  Evidence: 2×RTX5090, 125 GiB RAM, 670 GiB free; source/runtime absent; Docker daemon denied;80/443/14500 occupied.
- [x] **T0.4** Freeze full requirements/design/tasks/runbook/handoff structure.
  Requirements: MIG-011.

## Phase 1 — Source transfer

- [x] **T1.1** Verify canonical target paths are absent and create only migration-owned partial roots.
  Requirements: MIG-002
  Validation: remote `test ! -e` for both canonical and partial paths before first write.
- [x] **T1.2** Create and transfer Studio `git bundle --all` + binary tracked patch + reviewed untracked tar.
  Requirements: MIG-001, MIG-004, MIG-005
  Validation: all three artifacts match SHA manifest; materialized tree contains no excluded dependency/secret path.
- [x] **T1.3** Compare Studio Git branch/HEAD/status and promote partial path.
  Requirements: MIG-001, MIG-002, MIG-006
  Validation: branch and HEAD exact; tracked/untracked parity under the same exclude policy.
- [x] **T1.4** Materialize Python/bridge bundle + patch + reviewed untracked tar including experiments/videos, then promote after parity.
  Requirements: MIG-001, MIG-003, MIG-004, MIG-005
  Validation: branch/HEAD/status parity; `.venv`, `wget-log*` and secrets absent.

## Phase 2 — Runtime and evidence transfer

- [x] **T2.1** Transfer canonical UE archive to apartial filename.
  Requirements: MIG-003, MIG-006
  Validation: exactly15,170,703,068 bytes and SHA-256 `806e869a...990e2f` on target.
- [x] **T2.2** Promote archive and extract into a new partial binary directory.
  Requirements: MIG-002, MIG-003, MIG-007
  Validation: fixed UnrealEditor/uproject/cirrus paths exist; at least50 GiB remains free.
- [x] **T2.3** Promote extracted runtime without modifying its Engine/Content.
  Requirements: MIG-002, MIG-006
  Validation: canonical directory absent before rename; post-rename archive remains unchanged.
- [x] **T2.4** Transfer sanitized Opus/VISTA evidence only.
  Requirements: MIG-003, MIG-005, MIG-006
  Validation: acceptance screenshots/metadata present; raw model logs, MCP token config, node_modules and PID files absent.

## Phase 3 — Dependency rehydration and offline validation

- [x] **T3.1** Set operator PATH to use user Node22 and verify tool versions.
  Requirements: MIG-004, MIG-007.
- [x] **T3.2** Run `npm ci` in web and server from committed lockfiles.
  Requirements: MIG-004
  Validation: clean exit; no lockfile change.
- [x] **T3.3** Provision `uv` at user scope or obtain admin-approved package, then rebuild the Python repo environment.
  Requirements: MIG-004, MIG-010
  Validation: `uv run python` import smoke; no copied local venv.
- [x] **T3.4** Run relevant Node/server/UI/schema tests and Vite build.
  Requirements: MIG-008, MIG-009
  Validation: expected suites pass without UE/provider/DB access.
- [x] **T3.5** Update `HANDOFF.md` with target-side versions, test totals and deviations.
  Requirements: MIG-006, MIG-011.

## Phase 4 — Loopback runtime smoke

- [ ] **T4.1 [Stateful Gate]** Validate NVIDIA ICD/Vulkan readiness; request admin `vulkan-tools` only if needed.
  Requirements: MIG-007, MIG-010.
- [ ] **T4.2 [Stateful Gate]** Create isolated slot/state/config directories and generate a fresh Studio token.
  Requirements: MIG-005, MIG-007.
- [ ] **T4.3 [Stateful Gate]** Launch one Cirrus and one UE on GPU0 with loopback ports and model-off/NOWRITE policy.
  Requirements: MIG-007, MIG-012
  Validation: only expected loopback listeners; PID ownership recorded.
- [ ] **T4.4 [Stateful Gate]** Launch source Studio on loopback port3002 and verify authenticated health/readiness.
  Requirements: MIG-007, MIG-008.
- [ ] **T4.5 [Stateful Gate]** Validate MCP, decoded Pixel Streaming frame, read-only screenshot and fixed VISTA setup/state/stop.
  Requirements: MIG-009
  Validation: saved local evidence and clean Stop; no source/content write.

## Phase 5 — Deferred production integrations

- [ ] **T5.1 [Admin/Data]** Obtain the exact asset snapshot matching the transferred UE content revision.
  Requirements: MIG-008, MIG-010.
- [ ] **T5.2 [Admin]** Enable Docker safely or provision managed Postgres/Qdrant/embed services.
  Requirements: MIG-008, MIG-010.
- [ ] **T5.3 [Cost/State]** After explicit approval, run one real Text and read-only Visual review smoke.
  Requirements: MIG-008.
- [ ] **T5.4 [UE Content]** Implement/validate drag, brace and lift-foot adapters before strict 12-second timeline Start.
  Requirements: MIG-009.
- [ ] **T5.5 [Admin/Network]** Integrate existing80/443 ingress, same-origin WSS, Coturn and firewall; run external forced-relay E2E.
  Requirements: MIG-007, MIG-010.
- [ ] **T5.6 [Release Gate]** Complete backup/restore, service supervision, observability and rollback drill before Production-ready claim.
  Requirements: MIG-012.

## Completion definition

The migration portion is complete after T1.1–T3.5. A runnable loopback stack requires T4.1–T4.5. Production readiness additionally requires applicable Phase5 tasks and remains governed by `../production-readiness/tasks.md`.

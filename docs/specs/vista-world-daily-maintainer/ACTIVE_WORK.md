# VISTA World Daily Maintainer — Active Work

Updated: 2026-08-21

## Coordinator ownership

- Agent: Codex root integrator
- Worktree: `/home/yhliu/SimWorld-Studio-worktrees/vista-playable-home-realism`
- Branch: `codex/vista-playable-home-realism`
- Goal: bootstrap public standalone `IvesLiu1026/VISTA-World` and implement the approved Daily
  Maintainer through report-only/canary readiness.
- Owns: GitHub repository lifecycle, approved specs, integration, commits, pushes, systemd design,
  final validation and handoff.
- Must not touch: GPU/UE/Sunshine/Tailscale lifecycle, production port 8000, canonical datasets,
  NAS evidence, accepted world receipts, unrelated active work.
- Runtime ownership: no GPU or application ports; only short-lived local test processes.
- Validation: spec traceability, offline unit/integration tests, Git/GitHub identity and remote checks.

## Current gates

- Approved by `IvesLiu1026` on 2026-08-21.
- Public standalone repository does not yet exist.
- Codex GitHub connector and Git SSH identify as `IvesLiu1026`.
- Server `gh` CLI currently identifies as `aN0NyMoUs0000`; remote creation/publication is blocked
  until the CLI bootstrap identity is corrected or the GitHub App publisher exists.
- Credential-separated patcher service account/container is required before unattended publishing.

## Worker ownership

- No writing worker has overlapping ownership. Read-only audits may inspect extraction paths,
  automation architecture and GitHub capability without modifying files or external state.

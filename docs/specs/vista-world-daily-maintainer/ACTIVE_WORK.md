# VISTA World Daily Maintainer — Active Work

Updated: 2026-08-21

## Coordinator ownership

- Agent: Codex root integrator
- Worktree: `/home/yhliu/SimWorld-Studio-worktrees/vista-daily-maintainer-integration`
- Branch: `codex/vista-daily-maintainer-integration`
- Goal: integrate the approved Daily Maintainer safety core and publish it to the protected public
  `IvesLiu1026/VISTA-World` repository before adapter and rollout work.
- Owns: GitHub repository lifecycle, approved specs, integration, commits, pushes, systemd design,
  final validation and handoff.
- Must not touch: GPU/UE/Sunshine/Tailscale lifecycle, production port 8000, canonical datasets,
  NAS evidence, accepted world receipts, unrelated active work.
- Runtime ownership: no GPU or application ports; only short-lived local test processes.
- Validation: spec traceability, offline unit/integration tests, Git/GitHub identity and remote checks.

## Current gates

- Product/spec choices were approved by `IvesLiu1026` on 2026-08-21.
- Public standalone `IvesLiu1026/VISTA-World` exists as a non-fork; `main` requires the exact
  `contracts-and-compiler` and `repository-policy` checks with strict branch protection.
- Server `gh` and Git SSH both identify as `IvesLiu1026` for attended bootstrap publication.
- The 28-item backlog remains an unapproved `CodexDraft` at SHA-256
  `5e08d1f2f784aa5940e0606a58637e5006f0892b3c7971b2eb6fba669e2d4fa5`.
- Unattended activation remains blocked on a dedicated public-automation Codex credential,
  credential-separated patcher/verifier/publisher principals, authenticated immutable spool,
  concrete Git/GitHub/runtime adapters, canary evidence and the 14-day PR-only pilot.

## Worker ownership

- No writing worker has overlapping ownership. Read-only audits may inspect extraction paths,
  automation architecture and GitHub capability without modifying files or external state.

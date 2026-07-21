# VISTA-world Production Completion — Active Work

- Agent: Codex `/root`
- Session: VISTA-world production completion
- Worktree: `/home/yhliu/SimWorld-Studio-worktrees/vista-production`
- Branch: `codex/vista-production-completion`
- Base: `origin/codex/vista-loopback` at `7a20573a`
- Goal: Complete the approved production-readiness path from deterministic
  `mmg_040` scene construction through real asset retrieval, review, timeline
  animation, public WebRTC, and release evidence.

## Ownership

- Owns: `docs/specs/production-readiness/**`
- Owns: production-readiness backend modules and their tests under
  `simworld_studio_workspace/web/server/**`
- Owns: VISTA/review/streaming UI integration and focused browser tests under
  `simworld_studio_workspace/web/src/**` and
  `simworld_studio_workspace/web/tests/ui/**`
- Owns: focused asset-index tooling and tests under `tools/**`
- Must not touch: `/home/yhliu/SimWorld-Studio-src` dirty files
- Must not touch: canonical VISTA datasets, review ledgers, NAS outputs, or
  production VISTA port `8000`

## Current Worker Assignments

| Agent | Worktree / branch | Owns | Must not touch | Runtime ownership |
| --- | --- | --- | --- | --- |
| Codex `/root` | `vista-production` / `codex/vista-production-completion` | Scene build orchestration, importer/API/UI integration, merge queue, final validation | Dirty source checkout and canonical datasets | Existing local web smoke only |
| `animation-runtime` | `animation-runtime-v2` / `codex/vista-animation-runtime-v2` | New animation/IK/timeline adapter modules, schemas, focused tests, focused runbook section | Existing scene/import routes, `index.js`, UI, UE runtime | None; fake broker only |
| `streaming-readiness` | `streaming-readiness-v2` / `codex/vista-streaming-readiness-v2` | New WebRTC readiness/evidence modules and focused tests | Existing gateway, Nginx, Compose, UI, live ports | None; no Coturn/Cirrus launch |
| `review-provider` | `review-provider-v2` / `codex/vista-review-provider-v2` | New bounded provider smoke runner/CLI, schemas, focused tests | Existing chat/review routes, UI, provider credentials | None; no paid/provider call |

The coordinator is the only merge owner. Workers must commit a single coherent
change and report validation commands plus remaining live/admin gates.

## Runtime Ownership

- The coordinator owns the existing loopback-only development smoke runtime.
- Workers own no live runtime during source integration.
- UE, model-provider, database, GPU, network, and public-ingress work remains
  gated until the relevant preflight is recorded.
- No production deploy is authorized by this work item.

## Validation

- Focused Node unit/contract tests for each slice
- `cd simworld_studio_workspace/web && npx vite build --mode development`
- Focused Playwright mock E2E where applicable
- `git diff --check`
- Live evidence only after the corresponding state/admin/cost gate

## Handoff

- This file plus `tasks.md` and phase-specific evidence under
  `docs/specs/production-readiness/evidence/`

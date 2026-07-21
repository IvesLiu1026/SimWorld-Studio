# VISTA-world Production Completion — Active Work

- Agent: Codex `/root`
- Session: VISTA-world production completion
- Worktree: `/home/yhliu/SimWorld-Studio-worktrees/semantic-production-adapter`
- Branch: `codex/semantic-production-adapter`
- Base: production checkpoint `6b0d5046`
- Latest pushed implementation checkpoint: `e31ee835` (`feat: expand mmg040 official asset candidates`)
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
| Codex `/root` | `semantic-production-adapter` / `codex/semantic-production-adapter` | v2 semantic-index integration, docs, merge queue, final validation | Dirty source checkout, canonical datasets, legacy index runners | None; offline code only |
| `approval_aggregate` | shared worktree, completed | six-party opaque approval aggregate in new files only | Shared schemas/launcher/state/terminal and runtime | None |
| `adapter_registration_audit` | shared worktree, completed read-only | real query/build/registration/source-closure inventory | All source/docs edits and runtime | None |

The coordinator is the only merge owner. Workers must commit a single coherent
change and report validation commands plus remaining live/admin gates.

## Runtime Ownership

- This work item owns no runtime, GPU, port, database, UE lease, or provider.
- A separate isolated demo task owns GPU 1 and loopback ports
  `3012/55570/8595/8596/8899`; this work item must not inspect, restart, bind,
  or terminate those resources.
- Production VISTA port `8000` is also outside this work item.
- UE, model-provider, database, GPU, network, and public-ingress work remains
  gated until the relevant preflight is recorded.
- No production deploy is authorized by this work item.

## Current Offline Semantic-v2 Checkpoint

- Formal release closure: 21 schemas, including the signed launcher
  verification receipt.
- Unregistered adapter digest surface: 20 schemas; the registry remains empty
  and every execution gate returns `ADAPTER_NOT_REGISTERED`.
- Closed-schema runtime corpus: 24 schemas.
- Implemented offline: real Ed25519 approval verification, a six-party opaque
  verified aggregate, exact one-use verified launcher handoff composition,
  durable ordinary/control prepare-result ledgers, exact replay, isolated
  process-group execution with absolute deadlines, Linux pidfd fallback,
  subreaper cleanup for escaped descendants, complete inherited-FD closure and
  bounded reaping, and commit-before-send service ordering for authenticated
  phase/control sessions.
- Still required before Production registration: release-pinned projection of
  the approval aggregate; detached worker signing for executor-owned phase
  evidence; independently pinned launcher/control wiring; fixed static worker
  dispatch; seven real operation executors; a reproducible worker image and
  recalculated source closure; root-capability handoff evidence; live
  UE/PostgreSQL/Qdrant/embedding/provider observations; a new registered adapter
  revision that resolves the intentional false/true capability gate; PA-01
  through PA-21 evidence and explicit release approval.

## Current `mmg_040` scene candidate checkpoint

- The archive-bound scene candidate set now contains seven exact
  Content-relative package locators: the official chair, three `BP_Box`
  variants, `SM_SeatTable_01a`, `SM_Industrial_Carts_Static_Carts_1`, and
  `SM_Industrial_Carts_Service_Carts_8`.
- The seat-table is only a stable-step/seat candidate; the two cart meshes are
  only high-storage/cabinet-surrogate candidates. Their source packages and
  adjacent PBR families exist in the verified official archive, but no UE
  class/load/spawn/bounds/collision/material or visual-role claim has been
  made.
- Candidate-source raw-byte pin:
  `35aaf9741650d028f8f11e303035ab10168ef78d5244411c64d9f37db7cf9f4e`.
  The generated profile remains `candidate_unverified`,
  `start_allowed=false`, and `runtime_ready=false` until a live receipt exists.
- Next authoritative action is a runtime-owner AssetRegistry and disposable
  load/spawn/material/bounds/collision inspection of these seven objects. If
  one step alternative and one high-storage candidate pass, a non-fixture
  layout can be emitted without the separately gated CC0 download.

## Current Review checkpoint

- Text/Visual server-side access-token injection and authenticated loopback
  `/api/chat` chaining were revalidated against the current source and fake
  HTTP provider matrix; the previous `Studio access token required` response
  is not reproduced by this branch's current wiring.
- The real Claude adapter remains tool-free, strict-schema, image-over-stdin,
  budgeted, cancellable, and stripped of Studio/database credentials.
- Review smoke receipts no longer trust caller-supplied CLI identity. The
  runner measures the exact configured Claude binary with a bounded
  `--version` preflight and rejects a mismatch before any paid call.
- Local read-only measurement reports Claude Code `2.1.215`; no model request
  was sent. `T1A.11` still requires user approval for one real Text and one
  read-only Visual `claude-opus-4-8` call.

## Validation

- Focused Node unit/contract tests for each slice
- `cd simworld_studio_workspace/web && npx vite build --mode development`
- Focused Playwright mock E2E where applicable
- `git diff --check`
- Live evidence only after the corresponding state/admin/cost gate

## Handoff

- This file plus `tasks.md` and phase-specific evidence under
  `docs/specs/production-readiness/evidence/`
- Current semantic-v2 code evidence:
  `evidence/2026-07-21-semantic-index-v2-offline-checkpoint.md`
- Current control/signature follow-up evidence:
  `evidence/2026-07-21-semantic-control-signature-offline-checkpoint.md`
- Current isolation and `mmg_040` asset-gap follow-up:
  `evidence/2026-07-22-semantic-isolation-and-mmg040-gap-checkpoint.md`
- Current Review auth/CLI identity follow-up:
  `evidence/2026-07-22-review-cli-identity-checkpoint.md`

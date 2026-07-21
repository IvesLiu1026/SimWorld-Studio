# VISTA-world Production Completion — Active Work

- Agent: Codex `/root`
- Session: VISTA-world production completion
- Worktree: `/home/yhliu/SimWorld-Studio-worktrees/semantic-production-adapter`
- Branch: `codex/semantic-production-adapter`
- Base: production checkpoint `6b0d5046`
- Latest pushed checkpoint: `c6ded6ec` (`docs: record live mmg040 scene build`)
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
| Codex `/root` | `semantic-production-adapter` / `codex/semantic-production-adapter` | v2 semantic-index integration, docs, merge queue, final validation | Dirty source checkout, canonical datasets, legacy index runners | GPU 0; loopback `3010/55560/8585/8586/8889` for the bounded `mmg_040` live run |
| `approval_aggregate` | shared worktree, completed | six-party opaque approval aggregate in new files only | Shared schemas/launcher/state/terminal and runtime | None |
| `adapter_registration_audit` | shared worktree, completed read-only | real query/build/registration/source-closure inventory | All source/docs edits and runtime | None |
| `commandlet_executor` | shared worktree, active | commandlet-only Interchange executor and focused tests | Runtime, UE, network, docs and all other source | None |
| `semantic_smoke_catalog` | shared worktree, active | new non-Production `mmg_040` three-record catalog preparer and focused tests | Existing source/docs, DB/network/runtime, and Production readiness claims | None |
| `mmg040_animation_content_map` | shared worktree, active | `VistaAnimationContentApi` r2 PickUp parity slice and one new focused parity test | Runtime evidence, UE/GPU/ports, docs, commandlet/semantic files, and Production readiness claims | None |

The coordinator is the only merge owner. Workers must commit a single coherent
change and report validation commands plus remaining live/admin gates.

## Runtime Ownership

- The user authorized a bounded live verification run on 2026-07-22. This work
  item owns exactly GPU 0 and loopback ports `3010/55560/8585/8586/8889` for
  that run, including the UE lease, Pixel Streaming stack, fixed
  `execute_python_script` asset inspection, deterministic `mmg_040` build,
  screenshot, and cleanup.
- The run is model-off, loopback-only, and does not authorize a public listener,
  provider call, PostgreSQL/Qdrant write, production deploy, or mutation of the
  canonical archived UE project. A disposable staged workspace and append-only
  evidence paths must be used.
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

## Current `mmg_040` live asset checkpoint

- A revision-bound UE 5.3.2 AssetRegistry bootstrap observed 1,350 rows and
  402 object candidates in the verified official archive. The seven selected
  `mmg_040` objects all loaded and spawned in a disposable runtime. Four are
  complete real `StaticMesh` candidates: the chair, seat-table and two carts.
  The three `BP_Box` variants expose collision helpers but no visible mesh or
  material and are rejected as box assets.
- The operator-authorized CC0 v2 bundle is acquired and byte-pinned at
  `/home/yhliu/.simworld/vendor-assets/vista-mmg-040-polyhaven-v2`: three Poly
  Haven assets, 15 files, 4,648,718 bytes, manifest SHA-256
  `f887de3303fc9ad513fb0c75172e8332d75f8e0dc29e2af9fd542972160ab5f2`,
  tree SHA-256
  `0d3858dc07a1cf77d9dd592a6eb897865f2fb7c3e4796a4f86d215ac4969ac36`.
- Studio-socket `AssetImportTask` reached Interchange but did not return or
  write a package within the fixed timeout. It is `transport_ambiguous`, was
  not retried, and its disposable project is quarantined. The same fixed
  import through `UnrealEditor-Cmd -run=pythonscript` succeeded for all three
  assets in a new disposable project and wrote 15 local `.uasset` files.
- Each imported asset has one real static mesh, a non-default material slot,
  three texture assets, finite nonzero bounds, and one convex simple-collision
  element. Base color is sRGB; packed roughness/metallic and normal are
  non-sRGB; normal compression is `TC_NORMALMAP`; UE flipped the OpenGL normal
  green channel.
- The durable operator observation is pinned by SHA-256
  `ec473e1be15613bf857702b829c39edad63b3292c5b69db6699a3959d51bf0d3`.
  It deliberately keeps material dependency, Interchange pipeline fingerprint,
  rendered scale/contact/collision/PBR, Production readiness and semantic-index
  eligibility open.
- A UE 5.3 compatibility fix is pushed at `c3b0fb27`. Two fail-closed,
  unsaved disposable scene builds preserve the rejected overlap-method and
  SkyLight-property attempts. A third fresh build saved
  `/Game/VISTA/Scenes/MMG040_Office_CommandletR3` with 14 actors and exact
  official/Poly Haven/character references. The 23,886-byte map SHA-256 is
  `afa9ecddf4133a443080827922686b44b4f61bd28418d087da378d429d7bfd14`;
  the scene-build receipt SHA-256 is
  `1689f72e1f88205edb17d8056ddec7cb27d135f62542619a232435cf2f90025b`.
- A fresh GPU 0 offscreen render probe failed before MCP startup with
  `VK_ERROR_INCOMPATIBLE_DRIVER` and created no screenshot. The current user
  is not in the `render` group. PCI bus `0000:16:00.0` is GPU 0 and maps via
  `/dev/dri/by-path/pci-0000:16:00.0-render` to `/dev/dri/renderD128`, which is
  owned by `root:render` with mode `0660`; `yhliu` cannot open it. That
  permission must be fixed and a new login/Vulkan preflight completed before
  using another fresh render project. Failed render receipt SHA-256:
  `170d6f82acf9a7839c162dd369825d3624b74539562970c9420587484a831c2f`.

## Current animation checkpoint

- Commit `74524aa4` adds a typed `pick_up` contract with fixed
  `vista_pick_up_ik_v1`, target attachment, upper-body IK, hand-contact anchor
  and exact `vista_pick_up_attached` completion signal. A separate
  counterfactual 12-second slice compiles `look_at` at 0 s, `pick_up` at 2 s,
  `pause` at 5 s, `fall` at 9 s and a terminal checkpoint at 12 s.
- Focused animation/importer tests pass 27/27. This is contract evidence, not a
  live character mutation. The broker still hardcodes a different pawn class
  from the pinned `BP_MMG040Character_C`; exact live character content,
  montage/IK adapters and rendered fall/pick-up evidence remain required.

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
- Current live Interchange and real-asset follow-up:
  `evidence/2026-07-22-mmg040-live-interchange-checkpoint.md`
- Current Review auth/CLI identity follow-up:
  `evidence/2026-07-22-review-cli-identity-checkpoint.md`

# Handoff: SimWorld on 140.113.215.82

Status: Migration and offline validation complete; stateful runtime smoke gated  
Updated: 2026-07-15 Asia/Taipei

## Objective

Continue the approved staged migration, validate the copied dirty source exactly, rebuild disposable dependencies, then obtain a separate decision before launching a loopback UE smoke. Do not interpret the presence of source files as Production readiness.

## Source of truth

- Studio: `/home/yhliu/SimWorld-Studio-src`, branch`codex/vista-loopback`, HEAD`caf6d9309ad4fe256a6ba1e212d8bb1fb1fa7f7b` plus dirty layer.
- Python/bridge: `/home/yhliu/SimWorld`, branch`main` plus dirty layer.
- UE: canonical archive SHA-256`806e869ad1c65b298f05a39854b28e4188bb50817f539744451849e054990e2f`.
- Product/technical intent: `../production-readiness/` and this migration spec directory.

## Current product progress included in the dirty layer

- Review pipeline: authenticated internal requests, strict provider/verdict contract, cancellation, budget isolation and read-only visual capture foundation.
- Asset retrieval: fail-closed policy, readiness/snapshot schema/audit foundation; real Postgres/Qdrant/embed snapshot is still missing.
- VISTA importer: sanitized`mmg_040` golden fixture, deterministic SceneSpec, preview/commit/status service and professional workbench UI.
- Timeline: schema/compiler/scheduler, strict unsupported-action preflight, monotonic clock, timeout/Stop/Replay contracts; realUE action adapters/UI are missing.
- Streaming: loopback/trusted-proxy profile, Cirrus/TURN config builder and opaque endpoint registry; existing runtime route/player/ingress are not yet wired to it.
- Process lifecycle: registry foundation exists but startup/heartbeat/shutdown integration is missing.

## Important truth about the accepted Opus scene

The2026-07-14 run used one authorized`claude-opus-4-8` call and created16 actors, followed by non-model scale/spacing corrections. The UE process used`-NOWRITE`; therefore the exact live scene is not a saved map and cannot be migrated as a persistent UE asset. Sanitized screenshots, metadata, prompt and acceptance evidence are transferable. Recreating the scene requires a new disposable runtime and explicit model/state approval, or a deterministic non-model SceneSpec/adapter implementation.

## Target audit summary

- Hardware is sufficient: 2×RTX5090,125 GiB RAM, roughly670 GiB free.
- Source andUE runtime were absent at audit.
- User Node22, Codex and Claude exist under`~/.local/bin`; noninteractivePATH must be fixed.
- Docker daemon is inaccessible to`yhliu`; assets remain blocked without an admin decision.
- NVIDIA Vulkan libraries exist;`vulkaninfo` is missing.
- Ports80/443/14500 are occupied and out of scope. Initialloopback ports werefree but must be rechecked.

## Transfer ledger

Update this table only after target-side verification.

| Unit | Target | Transfer | Integrity | Promotion |
| --- | --- | --- | --- | --- |
| Studio dirty source | `/home/yhliu/SimWorld-Studio-src` | complete | bundle/patch/tar SHA, HEAD, dirty diff and file-count parity verified | promoted |
| Python/bridge dirty source | `/home/yhliu/SimWorld` | complete | bundle/patch/tar SHA, HEAD, dirty diff and file-count parity verified | promoted |
| UE archive | `~/.local/share/simworld-studio/downloads/SimWorld-Studio-Minimal-806e869a.tar.gz` | complete | exact bytes and full target-side SHA-256 verified | promoted |
| Extracted UE runtime | `~/.local/share/simworld-studio/binary/SimWorld-Studio-Minimal-806e869a` | complete | UnrealEditor/uproject/Cirrus fixed-path hashes verified | promoted, stock Cirrus intentionally unpatched |
| Sanitized evidence | `~/.local/share/simworld-studio/evidence/20260714T120039-simworld-opus` | complete | archive SHA verified; 29 files extracted; forbidden runtime/token paths absent | promoted |

## First remote-agent actions

1. Read`AGENTS.md`, this entire spec directory and`../production-readiness/`.
2. Run onlyread-only preflight and integrity checks in`runbook.md`.
3. Compare dirty status; do not clean it.
4. Rebuild Node dependencies and run offline tests.
5. Record versions, test totals and any deviation below.
6. Stop and ask beforeUE/runtime launch, model, DB, Docker, sudo or public network changes.

## Validation ledger

| Check | Expected | Actual | Status |
| --- | --- | --- | --- |
| Archive bytes |15170703068 |15170703068 | pass |
| Archive SHA-256 |`806e869a...990e2f` |`806e869a...990e2f` | pass |
| Studio branch/HEAD |`codex/vista-loopback` / `caf6d930...` |`codex/vista-loopback` / `caf6d930...` | pass |
| Studio dirty parity |48 modified +69 untracked in migration snapshot |48 modified +69 untracked; diff SHA `049e919a...e4dd31` | pass |
| Python dirty parity |5 modified +104 untracked at discovery |5 modified +102 included; two `wget-log*` files deliberately excluded; diff SHA `acaa2967...929ce6` | pass |
| Node selected |v22.23.1 user-local |v22.23.1 / npm10.9.8 | pass |
| Relevant Node contracts |230 source-host baseline |230 pass /0 fail /0 skip across24 files | pass |
| Server unit |11 pass /18 skip baseline |11 pass /18 intentional skip | pass |
| UI unit/E2E |11 unit +2 E2E baseline |11 unit pass; Review1 pass; VISTA Import1 pass | pass |
| Vite build |1879 modules baseline |1879 modules transformed | pass |
| Python environment |fresh Python3.12 environment |uv0.11.0, Python3.12.3,37 packages, import smoke and `uv pip check` pass | pass |

## Blockers requiring a decision

- Asset snapshot/catalog plus Postgres/Qdrant/embed provisioning.
- Docker/rootless/managed service strategy.
- Vulkan diagnostic package and possible render-group access.
- Real provider smoke/cost approval.
- UE adapters for drag/brace/lift-foot and timeline UI/runtime wiring.
- Existing ingress owner, DNS/TLS, Coturn and firewall for public WebRTC.

## Remote notes

Append dated notes here. Never paste secrets, raw provider output or private infrastructure credentials.

### 2026-07-15 — Verified transfer

- Migration package: `/home/yhliu/SimWorld-Migration/20260715T163851-simworld-to-140-113-215-82`.
- Studio was reconstructed in a unique partial directory from `studio-all.bundle`, the binary dirty patch and the reviewed untracked archive. `git fsck --full` passed; two harmless dangling commits remain exactly as in the source object database.
- Python/bridge was reconstructed the same way. Its three pre-existing trailing-whitespace warnings in `simworld/communicator.py` were retained for source parity rather than silently rewritten.
- Runtime archive was transferred as `.partial`, checked on the target, renamed, extracted to a unique partial directory, verified, then promoted. The extracted runtime is 21 GiB. Key hashes: UnrealEditor `4294c00a...52efcfd`, uproject `134f3a14...a6c21`, stock Cirrus `92298e88...be29a4`.
- The stock Cirrus binary must not be launched. The loopback/token patch remains a separate stateful runtime gate; expected patched hash is `133a12cf...5300e`.
- Studio keeps the snapshot as `migration-bundle` and now uses `https://github.com/SimWorld-AI/SimWorld-Studio.git` as `origin`. A target-side fetch confirmed `origin/main=d2d439ea3785d205a77e845604135added7d2cc7` and the working branch is0 behind /12 ahead. Python/bridge still keeps its migration bundle as `origin` because the intended external URL has not been confirmed.
- No API/OAuth/Studio token, provider home, raw provider log, SSH material, live PID or mutable session state was transferred.

### 2026-07-15 — Offline dependency and test validation

- Target runtime tools: Node22.23.1, npm10.9.8, uv0.11.0 and Python3.12.3. The uv binary was copied through a restricted partial path; source and target SHA-256 are `0a6ec289b04da0352d8b439cb0b05fbe43dff1face7707bd5764fdd4478c1561`.
- `npm ci` installed585 web and111 server packages. Both lockfile hashes were identical before and after: web `40a212d1...55dd`, server `02ea9742...cf7`.
- npm reported existing dependency audit debt: web4 findings (1 low,2 moderate,1 high) and server5 findings (3 moderate,2 high). No automatic `npm audit fix` was run because that would change the reviewed dependency graph.
- Chromium was installed only in the user Playwright cache with `npx playwright install chromium`; no sudo or host package changes were used.
- Exact results: production contracts230/230 pass; server unit11 pass and18 intentional skip; UI unit11 pass; Vite1879 modules; Review mock E2E1 pass; VISTA Import mock E2E1 pass.
- Python/bridge created a fresh gitignored `.venv`, installed37 packages with `uv pip install -e '.[dev]'`, passed `uv pip check`, and imported simworld/OpenCV/Gymnasium/Numpy/Pillow/Requests/YAML. The project has no lockfile, so current broad dependency ranges are a reproducibility risk even though this smoke passed.
- A broader, non-gating31-file server diagnostic produced258 pass /2 fail. Both are existing portability defects outside the approved230 baseline: `mcp-server.test.js` hard-codes `/data/jingtian/.../mcp-server.js`; `skills.test.js` ignores its intended test directory and discovers the17 workspace skills. These should be repaired before treating the full suite as host-independent.
- No Studio server, UE, Cirrus, provider, database, Docker container or public listener was started. No test process remains active.

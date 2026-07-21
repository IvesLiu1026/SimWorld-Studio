# Handoff: VISTA production continuation on 140.113.215.82

Status: code-ready foundation documented; target continuation blocked on connectivity and live/admin
evidence

Updated: 2026-07-21 Asia/Taipei

## Objective

Continue from the GitHub integration branch to one real, PBR VISTA 3D scene on the RTX 5090 target,
then close character animation/IK/fall, two real Review smokes, and public Coturn/WebRTC without weakening
the fail-closed contracts. Do not interpret transferred files, offline tests, fake receipts, or UI state as
Production readiness.

## Immediate blocker

On 2026-07-21 a read-only SSH retry to `yhliu@140.113.215.82` failed with:

```text
No route to host
```

The TCP/SSH session was not established. Authentication did not run and **no remote command was
executed**. No file, process, service, database, UE scene, provider, firewall or network state changed.
Network/host owner must restore the approved route before Phase 1 of [runbook.md](runbook.md).

## Current source of truth

- Repository: `git@github.com:IvesLiu1026/SimWorld-Studio.git`
- Integration branch: `codex/vista-production-completion`
- Deployment commit: **not hardcoded**. Resolve from GitHub with `git ls-remote`, compare to the
  coordinator-announced reviewed checkpoint, and record the exact 40-character SHA at execution time.
- Local worktree commits and separate integration worktrees are authoring state, not a deployment SHA;
  this handoff deliberately does not pin either one.
- Historical target checkout: `/home/yhliu/SimWorld-Studio-src`, branch `codex/vista-loopback`, old HEAD
  plus its dirty migration layer. Preserve it as evidence; do not pull, clean, merge, copy or deploy it.
- Historical Python/bridge tree: `/home/yhliu/SimWorld`, also preserved and not a current source sync path.

The coordinator has pushed the reviewed integration branch and verified one matching local/remote head.
Because this handoff update itself advances that branch, the target must resolve the final current SHA with
`git ls-remote` rather than copying a SHA from prose. New target source generations live under
`~/.local/share/simworld-studio/checkouts/<exact-sha>` and are clean/detached. Remote changes go to
`codex/remote-82-*` branches and return through GitHub.

## What the integration branch provides

These are code capabilities, not target live evidence:

- `claude-opus-4-8` builder production default/policy, with lease-scoped capabilities and no root token
  or direct UE ports exposed to the child runtime.
- Server-owned Studio session/slot/lease identity, process/port registry, exact broker revalidation and
  backend-authoritative PIE setup/state/stop.
- Authoritative verified VISTA raw staging, typed import/SceneSpec, owner-bound commit/status, semantic
  resolution and deterministic 12-second `mmg_040` timeline.
- Typed Scene BuildPlan/preflight/execute/rollback; production rejects BasicShapes/fallback surfaces,
  unverified `/Game` assets, missing exact material slots/PBR evidence and stale content receipts.
- Fixed animation action/transport/readiness/runtime contracts and UI/routes. The integrated
  `VistaAnimationContentApi` v1.1.0 source has a concrete pinned content-driver
  contract and 13 derived `mmg_040` target paths with deterministic source-audit tests. It has not been
  compiled/loaded with UE 5.3.2 or proven live. The UE 5.7.3 v1.0.0 package is historical only.
- Semantic stack deployment contracts: pinned model manifests/images, file secrets, schema/migration/
  index, snapshot/live audit, backup manifest, fail-closed readiness and sealed pending-job preparation.
  The reviewed executor is integrated with sealed independent pins, typed allowlisted phases, resumable
  terminal evidence and bounded source/process checks. Only the non-Production offline fixture adapter is
  registered; preparing or fixture-executing a pending job did not build an index, and no
  DB/Qdrant/model service was run.
- Isolated Review coordinator with durable outbox/artifact binding, execution preflight, tool-free strict
  provider adapter, fake HTTP failure matrix, browser recovery, bounded live smoke CLI and separate
  Text/Visual receipt readiness. Managed opaque evidence handles, bounded/cancellable capture,
  fail-closed retain/cleanup and path redaction are integrated and verified offline; this is not live
  evidence, and no paid/live provider call was made.
- Trusted-proxy WebRTC path: secure session cookie, opaque same-origin WSS endpoint, loopback Cirrus,
  short-lived TURN REST credentials, redacted telemetry and external readiness receipt verifier.
- Unified append-only import/scene/review/timeline artifact journal with offline recovery and ownership
  tests. No Production journal root, retention cleanup or backup/restore drill has been executed.

A read-only filename audit found 2,937 `.uasset`/`.umap` package files and names consistent with
mannequin/skeletal/AnimBP/Control Rig/IK/lifting/fall/chair/box/table candidates. This is not an Asset
Registry, catalog/index row count, or proof of class, object path, loadability or spawnability. The current
profile remains `verification_status=candidate_unverified`, `source_lineage_status=candidate_unverified`,
`runtime_ready=false` and `start_allowed=false`. No verified derived profile, package digest, immutable
Content binding or live receipt was produced.

Production generic free-form UE mutation remains intentionally blocked. NLP scene generation must compile
into the typed SceneSpec/BuildPlan route.

## What is still missing

| Area | Missing external/live evidence | Current truthful state |
| --- | --- | --- |
| Connectivity | working route to target and fresh inventory | blocked |
| Source | pushed reviewed integration SHA and clean target generation | GitHub branch synchronized; target generation pending |
| UE plugin | exact UE 5.3.2 build, project install/load, listener dispatch and nonce/process receipt | v1.1.0 source integrated; no target build/load |
| VISTA data | dataset-owner verified projection and approved staged bundle | not staged on current target generation |
| Assets | reviewed Production adapter, immutable model files, pinned services, full Postgres/Qdrant index, live audit, restore drill | offline executor integrated; no Production adapter or index |
| 3D scene | verified profile/digests and live Blueprint + StaticMesh PBR disposable build | filename-only candidates inventoried; not proven |
| Character | derived pawn/skeleton/AnimBP/Control Rig profile, hand/foot IK, drag, fall/recover montages/notifies | v1.1.0 source contract; no live proof |
| Timeline | 0/2/5/9/12 live evidence plus timeout/Stop/disconnect/restart matrix | not proven |
| Review | exactly one paid Text and one read-only Visual `PASS`, same scene digest, current receipts | coordinator/evidence code only |
| WebRTC | DNS/TLS/ingress, Coturn/firewall, two-network normal/forced relay and rotation receipt | not ready |
| Operations | live journal root, retention, observability, backup/restore and rollback sign-off | unified journal code only |

## Historical 2026-07-15 transfer ledger

This table is retained as provenance only. It does not show current integration-branch deployment.

| Unit | Historical target | 2026-07-15 result | Current use |
| --- | --- | --- | --- |
| Studio dirty source | `/home/yhliu/SimWorld-Studio-src` | bundle/patch/tar parity verified and promoted | preserve; never use as new source release |
| Python/bridge dirty source | `/home/yhliu/SimWorld` | parity verified; two `wget-log*` files intentionally excluded | preserve; not current code sync |
| UE archive | `~/.local/share/simworld-studio/downloads/SimWorld-Studio-Minimal-806e869a.tar.gz` | 15,170,703,068 bytes; SHA-256 `806e869ad1c65b298f05a39854b28e4188bb50817f539744451849e054990e2f` | re-inventory; immutable base only |
| Extracted runtime | `~/.local/share/simworld-studio/binary/SimWorld-Studio-Minimal-806e869a` | key paths/hashes verified; approximately 21 GiB | re-inventory version/toolchain; do not mutate in place |
| Sanitized Opus evidence | `~/.local/share/simworld-studio/evidence/20260714T120039-simworld-opus` | 29 files; forbidden token/runtime paths absent | historical visual evidence only |

Historical accepted run facts:

- one authorized `claude-opus-4-8` call created 16 actors, followed by non-model scale/spacing fixes;
- UE ran with `-NOWRITE`, so the live scene was not a persistent map asset;
- screenshots/metadata/prompt evidence were transferred, but recreating the scene needs the current typed
  path and a separately approved disposable runtime;
- old stock Cirrus/runtime or old test totals do not satisfy current WebRTC/runtime readiness.

## Historical target facts requiring refresh

The following were verified on 2026-07-15 and are stale until Phase 1 inventory repeats them:

- Ubuntu 24.04, 2x RTX 5090 32 GiB, 125 GiB RAM, about 670 GiB free;
- user-local Node 22.23.1/npm 10.9.8, uv 0.11.0/Python 3.12.3, Codex and Claude;
- Docker/Compose binaries installed but `yhliu` denied daemon access;
- NVIDIA libraries present, `vulkaninfo` absent;
- ports 80/443/14500 occupied; candidate loopback ports were then free;
- historical source/runtime transfer and that snapshot's offline suites passed;
- no live asset DB/index/model, provider, public WebRTC, UE compile/load/animation/content proof or
  production deploy was run.

Do not act on any item without refreshing it read-only.

## First remote-agent actions

1. Read `AGENTS.md`, this entire directory, then:
   - `../production-readiness/asset-stack-operations.md`
   - `../production-readiness/vista-raw-staging-runbook.md`
   - `../production-readiness/animation-ue-plugin-readiness.md`
   - `../production-readiness/animation-runtime-contract.md`
   - `../production-readiness/review-provider-smoke-runbook.md`
   - `../production-readiness/webrtc-coturn-runbook.md`
2. Run only `runbook.md` Phase 1 read-only inventory.
3. Resolve owners for host, listeners, GPU/slot, source checkout, UE/project/content, asset migration,
   provider budget and public network.
4. Confirm the coordinator has pushed and announced the exact integration SHA.
5. Create/verify a clean GitHub checkout generation; never copy the dirty snapshot.
6. Run offline tests and record results; do not patch the detached checkout.
7. Request one bounded gate at a time in dependency order:
   UE 5.3.2 toolchain/plugin -> VISTA staging -> semantic executor/asset stack -> typed scene -> content/timeline ->
   provider smokes -> public WebRTC -> release.

## Live evidence ledger

Do not pre-fill `Actual` or mark pass without an artifact path and digest.

| Check | Required evidence | Actual | Status |
| --- | --- | --- | --- |
| Connectivity | successful read-only session + timestamp | none after 2026-07-21 no-route | blocked |
| Source | GitHub URL/branch/exact SHA, clean status, fsck | branch pushed and coordinator heads matched; target checks pending | Git sync complete; target pending |
| Offline suite | exact generation + command/totals/build output | none on target for refreshed branch | pending |
| UE 5.3.2 plugin | v1.1.0 build log, binary/manifest SHA, load and nonce receipts | historical UE 5.7.3 v1.0.0 compile only | pending |
| VISTA source | dry-run/apply result and bundle digest | code fixture only | pending |
| Semantic executor | reviewed exact implementation + offline terminal receipt | integrated; focused 53/53 and independent QA clean | code verified; live pending |
| Models/services | model/image/config digests + unauthorized/authorized probes | none; no service started | pending |
| Asset snapshot | counts/revisions/live receipt + restore proof | read-only candidate inventory only | pending |
| Typed scene | import/plan/preflight/result + exact PBR/content evidence | none | pending |
| Animation | content/plugin/live receipts + 0/2/5/9/12 matrix | none | pending |
| Text Review | current tool-free `PASS` receipt | none | pending |
| Visual Review | current zero-diff tool-free `PASS` receipt | none | pending |
| Public WebRTC | deployment/probe/readiness receipts + rotation drill | none | pending |
| Rollback/release | executed drill + user/admin sign-off | none | pending |

## Administrator request summary

Do not send secret values in the request. Ask for decisions/capabilities:

1. restore approved SSH route;
2. provide exact UE 5.3.2 full build root/toolchain and Vulkan diagnostics if missing;
3. choose rootless Docker, explicit root-equivalent docker-group access, or managed services;
4. allocate private secret/model/DB/backup/config storage with correct ownership/modes;
5. provide authoritative VISTA verified projection and matching UE Content/asset catalog revision;
6. approve service/image/model provenance and database/index/backup windows;
7. assign current 80/443 ingress owner, Studio/TURN DNS, TLS/cert renewal, Coturn IP/realm/quota,
   firewall/NAT/relay range and external test networks;
8. identify release and rollback approvers.

## Git and rollback contract

- Integration: `codex/vista-production-completion` on `IvesLiu1026/SimWorld-Studio`.
- Remote work: one bounded `codex/remote-82-*` branch/worktree, explicit owned paths, atomic commits.
- Sync: push remote branch, coordinator integrates, integration branch is pushed, target resolves a new
  exact SHA and creates a new clean generation.
- Activation: point service config at one verified immutable code/plugin/project/content/model/index
  generation; do not edit it in place.
- Rollback: stop only exact registry/checkpoint-owned PID/service, reconcile PIE/actions, select prior
  verified generation, rerun readiness. Never broad-kill, broad-delete, reset dirty evidence, or take over
  unknown ports.

## Append-only remote notes

Add dated, secret-free notes below after target execution. Include the exact Git SHA and evidence digest;
never paste credentials, DSNs, raw provider output, SDP/candidate addresses, private signed URLs or
absolute restricted dataset paths.

### 2026-07-21 — Handoff refresh

- Converted ongoing source synchronization from old dirty-snapshot transfer to GitHub-only immutable
  integration checkpoints.
- Reconciled the handoff with the current code-ready runtime, raw staging, asset, typed scene, animation,
  Review and WebRTC architecture.
- Removed the stale authoring-commit claim; the remote must resolve the coordinator-announced exact SHA.
- Recorded the v1.1.0 animation source checkpoint, real `gym_citynav` candidate inventory, semantic
  pending-job/executor split, managed Review evidence checkpoint and unified journal code/live split.
- Integrated the reviewed sealed semantic executor after two independent QA rounds. Its fixture-only
  adapter remains non-Production; no schema, catalog, embedding, Postgres or Qdrant mutation occurred.
- Completed the local integration matrix: server 698, frontend 23, tools 208, semantic focused 53,
  animation contracts 22, candidate profile 10, packaging security 35, staging 6, Review Chromium 4 and
  VISTA Import Chromium 1 all passed; Vite built 1,879 modules and 25 schemas passed meta-schema checks.
  This is local code evidence only, not a test result from `140.113.215.82`.
- Pushed `codex/vista-production-completion` to GitHub and verified the local/remote heads matched before
  this follow-up handoff commit. The target must resolve the final current head after the follow-up push.
- Preserved the 2026-07-15 transfer/test facts strictly as historical provenance.
- Recorded the read-only SSH failure as `No route to host`; no target command or state change occurred.
- Left all UE, dataset, asset, provider, public-network, operations and release gates open.
- No provider, DB/Qdrant/model service, UE compile/load/mutation or public listener was run for this refresh.

# Tasks: VISTA production continuation on 140.113.215.82

Status: Specification refreshed; remote continuation blocked on connectivity

Updated: 2026-07-21 Asia/Taipei

Depends on: [requirements.md](requirements.md), [design.md](design.md),
[runbook.md](runbook.md)

## Status rules

- `[x]` means the named evidence exists; a code-only item never implies a live gate passed.
- `[ ] [Remote]` requires a fresh target-side command/result.
- `[ ] [Admin]`, `[Data]`, `[Cost]`, `[State]`, or `[Public]` requires that separate approval.
- Update [HANDOFF.md](HANDOFF.md) with evidence path/digest before checking an item.
- Never overwrite the historical dirty checkout, copy it as a release, put secrets in evidence, use
  broad cleanup, or claim Production-ready from partial results.

## Phase 0 — Historical baseline and current reachability

- [x] **T0.1** Preserve the 2026-07-15 migration ledger as historical evidence.
  Evidence: dirty Studio/Python snapshots, UE archive/runtime and sanitized run evidence were transferred
  and that date's offline baseline passed. This does not validate the new integration branch.
- [x] **T0.2** Record the 2026-07-21 read-only SSH retry.
  Evidence: connection failed with `No route to host`; no session was established and no remote command ran.
- [x] **T0.3** Freeze GitHub-only synchronization policy and phased handoff in this six-file package.
  Requirement: RMT-001 through RMT-012.
- [ ] **T0.4 [Remote]** Restore route/auth and perform only the read-only inventory in `runbook.md`.
  Acceptance: timestamped safe inventory; no package/service/file/network mutation in the same step.
- [ ] **T0.5 [Remote]** Identify owners of existing 80/443/14500 and every candidate Studio/Cirrus/MCP/UE
  listener/process/directory.
  Acceptance: unknown resources remain untouched; selected slot/GPU/ports are unowned and recorded.

## Phase 1 — GitHub checkpoint and clean checkout

- [ ] **T1.1 [Git Sync]** Coordinator pushes the reviewed
  `codex/vista-production-completion` checkpoint to `IvesLiu1026/SimWorld-Studio` and announces its exact SHA.
- [ ] **T1.2 [Remote]** Resolve branch SHA with `git ls-remote`; stop if absent/mismatched.
  Requirement: RMT-001.
- [ ] **T1.3 [Remote]** Clone the branch into a unique partial generation, verify remote URL, exact HEAD,
  clean status and `git fsck`, then promote to `checkouts/<full-sha>`.
- [ ] **T1.4 [Remote]** Record `source.json` and ownership checkpoint; do not modify detached generation.
- [ ] **T1.5 [Remote]** If code changes are needed, create `codex/remote-82-<task>` in an isolated worktree,
  commit specific files, push to GitHub and wait for coordinator integration before activation.
- [ ] **T1.6 [Remote]** Verify rollback can select the prior clean checkout without deleting either generation.

## Phase 2 — Host prerequisites and offline acceptance

- [ ] **T2.1 [Remote]** Recheck disk capacity before new checkout, model artifacts, DB volumes, UE plugin build
  and disposable project generation.
- [ ] **T2.2 [Admin]** Provide `vulkan-tools` only if diagnostic evidence requires it; grant render/device access
  only after a demonstrated permission failure.
- [ ] **T2.3 [Admin]** Choose rootless Docker, reviewed docker-group access, or managed Postgres/Qdrant/embed.
  Record that docker group is root-equivalent; do not silently use `sudo docker`.
- [ ] **T2.4 [Admin]** Provide an exact UE 5.3.2 full build root with `RunUAT.sh`, headers and matching Linux
  compiler/toolchain if the migrated runtime lacks them.
- [ ] **T2.5 [Remote]** Rebuild Node dependencies from lockfiles and Python tools through frozen `uv`; verify no
  lockfile change.
- [ ] **T2.6 [Remote]** Run the full Node server suite, VISTA/Review UI tests, Vite production build, Python tool
  tests, plugin offline tests, shell syntax and `git diff --check` from the exact generation.
  Acceptance: failures are recorded, not patched in the detached checkout.

## Phase 3 — UE 5.3.2 plugin package and live transport

- [x] **T3.1 [Code Evidence]** Portable `VistaAnimationContentApi` source, exact four-command contract,
  install/build/manifest scripts, dedicated server transport and offline tests exist.
- [x] **T3.2 [Historical Code Evidence]** UE 5.7.3 UHT/BuildPlugin package succeeded on the source host.
  Limitation: this package is not target-compatible proof.
- [ ] **T3.3 [State/Admin]** Dry-run then build plugin with target UE 5.3.2 and a new opaque build ID.
  Acceptance: UHT/build/package logs, module SHA, engine/platform and artifact manifest match.
- [ ] **T3.4 [State]** Create a writable disposable project generation without changing the canonical runtime
  archive/project; install the UE 5.3.2 package there.
- [ ] **T3.5 [UE Code]** Implement/review private-listener exact dispatch for the four reserved commands before
  generic bridge; prove unknown `vista_animation_*` terminal reject and mutation no-retry.
- [ ] **T3.6 [State]** Launch one owned UE process and prove plugin load, process instance, current lease binding,
  live nonce challenge, content/plugin manifest binding and clean stop.
  Acceptance: `ANIMATION_UE_PLUGIN_LIVE_PROOF_MISSING` clears only for that exact process/lease/revision.

## Phase 4 — Authoritative VISTA source bundle

- [x] **T4.1 [Code Evidence]** `tools/stage_vista_import_bundle.py` supports explicit verified manifest/JSONL,
  checksum/bytes/duration/media/identity validation, oracle boundary, dry-run, atomic apply and idempotency.
- [ ] **T4.2 [Data]** Dataset owner publishes immutable verified projection for selected sample/attempt plus exact
  render script, no-oracle dialogue and MP4.
- [ ] **T4.3 [Remote]** Run staging dry-run and archive the safe validation result; inspect source identity,
  12-second duration, timestamps, media dimensions, restricted-field absence and bundle digest.
- [ ] **T4.4 [State/Data]** Approve and run the byte-identical command with `--apply` into a private output root.
  Acceptance: modes `0700/0600`, exact file set, idempotent second apply, no canonical source mutation.
- [ ] **T4.5 [Remote]** Install the registry snippet through protected deployment config and prove public APIs do
  not accept filesystem paths.

## Phase 5 — Complete semantic asset index

- [x] **T5.1 [Code Evidence]** Pinned model artifact, deployment preflight, file-secret, schema/migration/index,
  snapshot/live-audit, backup bundle and fail-closed runtime contracts exist.
- [ ] **T5.2 [Data/Admin]** Select immutable UE Content, catalog, snapshot, embedding recipe, dense/sparse models,
  collection and image digests. Download/build/model population is a separate approved job.
- [ ] **T5.3 [Admin]** Provision private secret files, model directories, persistent volumes, backup root,
  ownership, retention and minimum free-space policy.
- [ ] **T5.4 [Remote]** Capture and verify dense/sparse `artifact-manifest.json`; run offline asset stack preflight
  and `docker compose ... config --quiet` with pinned images.
- [ ] **T5.5 [State/Admin]** Start loopback Postgres/Qdrant/embedding in the approved service context; prove
  unauthorized Qdrant/embed access is denied and authorized health works without printing credentials.
- [ ] **T5.6 [State/Data]** Apply schema, inspect PostgreSQL migration dry-run/count, approve and import exact
  catalog, then inspect Qdrant dry-run/pending count and approve the full index build.
- [ ] **T5.7 [State/UE]** Probe at least one Blueprint and one StaticMesh from the matching UE Content; verify exact
  `/Game` paths, dimensions, material slots and PBR textures.
- [ ] **T5.8 [Remote]** Capture and immediately verify snapshot/live-audit receipt; counts, revisions, dense size,
  model artifacts and UE Content must match exactly.
- [ ] **T5.9 [State/Admin]** Create Postgres/Qdrant backup bundle, restore into disposable services, repeat the
  live audit, and document rollback generation.

## Phase 6 — Typed SceneSpec/BuildPlan disposable scene

- [x] **T6.1 [Code Evidence]** Typed importer, owner-bound persistence, semantic resolver, Scene BuildPlan,
  production real-asset/PBR/content proof, lease-bound executor, rollback and backend PIE lifecycle exist.
- [ ] **T6.2 [Data/UE Content]** Publish a production layout profile and SHA bound to the active asset snapshot;
  replace every fixture/test/BasicShapes surface with verified `/Game` assets.
- [ ] **T6.3 [State]** Acquire one Studio session/slot and keep its cookie/lease current; run import preview and
  commit for the staged sample.
- [ ] **T6.4 [State]** Generate plan, inspect exact asset pins/transforms/collision, run read-only preflight, then
  explicitly confirm the same plan ID and execute in a disposable scene.
- [ ] **T6.5 [State]** Verify Blueprint + StaticMesh actor snapshot, exact material slots/PBR paths, content receipt,
  zero collision/floating report and screenshot all share one scene digest.
- [ ] **T6.6 [State]** Revalidate live digest, start backend PIE/possession, query state, then stop; prove no toolbar
  coordinate/browser command, stale lease, pending mutation or fallback actor remains.
- [ ] **T6.7 [State]** Run failure probes for stale plan, content/material drift, revoked lease and rollback; all
  fail closed before unsafe continuation.

## Phase 7 — Character content driver, IK, fall/recover and 12 seconds

- [ ] **T7.1 [UE Content]** Implement `IVistaAnimationContentDriver` for look-at, brace, drag, lift-foot, pause,
  fall and recover with verified pawn/skeleton/AnimBP or Control Rig/montages/notifies.
- [ ] **T7.2 [UE Content]** Create immutable `vista-animation-content-profile/v1` and verification receipt;
  bind hand/foot targets, chair drag/caster physics, fall collision and recover capsule alignment.
- [ ] **T7.3 [State]** Run live plugin capability/preflight against the exact scene plan, content profile, plugin
  manifest, process and lease; all required actions must be executable before Start.
- [ ] **T7.4 [State]** Execute the 12-second server-authoritative timeline and capture 0/2/5/9/12 pose,
  interaction, screenshot, engine-time/drift and terminal scene-validation evidence.
- [ ] **T7.5 [State]** Verify normal completion, notify timeout, operator Stop race, disconnect/outcome-unknown,
  server restart/recovery-required reconciliation, fall and explicit recover.
- [ ] **T7.6 [State]** Verify Stop cancels pending events, stops/releases/restores known actions, ends PIE and confirms
  stopped. Replay requires a fresh exact preflight and same revisions.

## Phase 8 — Real Text/Visual Review

- [x] **T8.1 [Code Evidence]** Fake HTTP coordinator matrix and bounded two-receipt provider smoke runner/readiness
  exist; Review Off makes zero critic/VLM calls.
- [ ] **T8.2 [Cost/State]** User approves exactly two tool-free `claude-opus-4-8` calls with stated per-call/total
  budget, timeout and token limits; no retries.
- [ ] **T8.3 [State]** Shared broker captures canonical scene-before digest and immutable evidence; no build/timeline
  run is active.
- [ ] **T8.4 [Cost]** Run one Text smoke; verify exact provider/model/CLI/build, usage bounds and `PASS` receipt.
- [ ] **T8.5 [Cost]** Run one read-only Visual smoke; recapture scene and prove before/after digest equality before
  accepting `PASS` receipt.
- [ ] **T8.6 [Remote]** Pin both unexpired receipt file hashes in deployment config and confirm Production readiness
  rejects missing, stale, mismatched or single receipts.

## Phase 9 — DNS/TLS/Coturn/public WebRTC

- [x] **T9.1 [Code Evidence]** Trusted-proxy security, HttpOnly session, opaque WSS route, loopback Cirrus config,
  short-lived TURN REST credentials, telemetry and external receipt validation exist.
- [ ] **T9.2 [Admin/Public]** Decide Studio/TURN DNS, existing ingress owner, TLS/cert renewal, Coturn public/private
  mapping, realm, quotas, monitoring and whether TURN/TLS needs a separate IP.
- [ ] **T9.3 [Admin/Public]** Install/materialize Coturn and same-origin gateway; open only approved HTTPS/WSS,
  TURN UDP/TCP/TLS and bounded relay ports. Keep Node/Cirrus/UE/MCP/SFU/UnrealCV private.
- [ ] **T9.4 [Remote]** Archive server and outside-network listener/firewall audits; prove raw control/stream ports,
  copied opaque paths and cross-slot cookies are denied.
- [ ] **T9.5 [State/Public]** Live E2E proves WSS `101`, streamer registration, advancing decoded frames,
  keyboard/mouse data channel, reconnect and active session isolation.
- [ ] **T9.6 [External]** From at least two external network classes run normal ICE plus forced UDP/TCP/TLS relay;
  prove relay candidate and no loopback/raw endpoint/mixed content.
- [ ] **T9.7 [Admin/Public]** Execute an approved maintenance-drain or blue/green TURN credential rotation and
  repeat listener/forced-relay tests.

## Phase 10 — Operations and release

- [ ] **T10.1 [State/Admin]** Finalize durable import/scene/review/timeline artifact revision journal, owner ACL,
  retention, cleanup and backup/restore.
- [ ] **T10.2 [Admin]** Add supervision, duplicate-stack alerts, dependency/review/asset/ICE/timeline metrics and
  safe log retention/redaction.
- [ ] **T10.3 [State]** Execute source/plugin/content/asset/service rollback drill to prior generations without
  touching historical snapshots or unknown services.
- [ ] **T10.4 [Release]** Security review and user/admin sign-off verify every requirement/evidence digest.
- [ ] **T10.5 [Release]** Only now change product status from component-specific `not_ready` to Production-ready.

## Completion definition

The refreshed handoff itself is complete when these docs are committed. The user's product goal is not
complete until T0.4 through T10.5 pass on the target. Code-ready checkmarks indicate only that the branch
contains the fail-closed machinery needed to gather live proof.

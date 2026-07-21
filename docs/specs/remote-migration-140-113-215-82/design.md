# Design: GitHub checkpoint to live VISTA production evidence

Status: Approved design; remote execution blocked on connectivity and independent gates

Updated: 2026-07-21 Asia/Taipei

Depends on: [requirements.md](requirements.md)

## 1. Design decision

後續不再把 2026-07-15 的 dirty migration tree 當作 active source。它保留為不可覆寫的
historical evidence；新程式碼只從 GitHub branch
`codex/vista-production-completion` 建立 immutable checkout generation。每次 execution
動態解析 branch 的 exact SHA，建立新 generation，驗證後才讓 runtime 指向它。Remote
changes 走獨立 branch/worktree再 push，不直接修改 activation checkout。

Runtime completion採「code-ready foundation + external evidence adapters」：不為了讓 UI 顯示
綠燈而放寬 generic MCP、BasicShapes、test fixtures、legacy env flags或 fake receipts。缺任何
真實 asset、content、provider、TURN證據時，對應 subsystem保持 `not_ready`。

## 2. Current boundary

下表是本 spec 撰寫時對 integration branch 已提交程式的判定。它是 source capability，
不是 target live status。

| Area | Code-ready in integration branch | Still requires target/external evidence |
| --- | --- | --- |
| Git/runtime identity | slot/process/port leases、owner/session/slot/lease broker resolution、backend PIE lifecycle、lease-scoped builder capabilities | target listener/process inventory；single lifecycle owner；live restart/reconcile |
| VISTA source | authoritative verified-source staging CLI、checksums/media/oracle boundary、typed importer/SceneSpec、preview/commit/status | real dataset-owner projection；dry-run review；approved atomic apply |
| Scene build | server-pinned BuildPlan、production surface/PBR/content receipt checks、TOCTOU revalidation、typed UE adapter/rollback | authoritative layout profile、matching `/Game` assets、live Blueprint+StaticMesh build and screenshot |
| Assets | pinned images/model artifact contracts、secret-file handling、preflight、schema/migration/index/audit/backup tooling | actual model files、Postgres/Qdrant/embed services、full index、matching UE Content revision、restore drill |
| Animation | fixed action registry、dedicated transport、server scheduler、backend Start/Stop/Replay、plugin source、UE 5.7.3 compile/package evidence | UE 5.3.2 rebuild/load、listener exact dispatch、project content driver、IK/montage/notifies、12-second live evidence |
| Review | isolated coordinator、tool-free strict provider adapter、fake HTTP matrix、two-receipt smoke runner/readiness | cost approval、one Text and one Visual live `PASS` receipt、Visual scene zero-diff |
| WebRTC | trusted-proxy profile、opaque session endpoint、same-origin WSS gateway、Cirrus/TURN config builder、redacted telemetry、external receipt validator | DNS/TLS/ingress owner、Coturn/firewall/NAT、live Cirrus/UE、two-network normal/forced-relay and rotation receipt |
| Persistence/ops | bounded per-feature records and runtime registries exist | unified durable artifact revisions/retention, backup/restore, dashboards/alerts, release sign-off |

Builder production default/policy pins `claude-opus-4-8`, but no live call has been performed by this
handoff. Production generic NLP mutation remains intentionally unavailable; NLP must compile into the
typed SceneSpec/BuildPlan path.

## 3. Target layout and generations

```text
/home/yhliu/
  SimWorld-Studio-src/                         # 2026-07-15 dirty snapshot; historical, do not overwrite
  SimWorld/                                    # historical Python/bridge snapshot; do not overwrite
  .local/share/simworld-studio/
    checkouts/
      <exact-git-sha>/                          # clean detached GitHub generation
    binary/
      SimWorld-Studio-Minimal-806e869a/         # migrated UE runtime; inventory before use
    ue-builds/
      ue-5.3.2/                                 # matching full build root if admin provides it
    ue-project-generations/
      <git-sha>-<content-revision>/             # disposable writable project generation
    plugin-packages/
      <build-id>/                               # exact UE 5.3.2 BuildPlugin output + manifest
    vista-import-bundles/
      <dataset-revision>/<sample-attempt>/
    asset-models/
      dense/<artifact-revision>/
      sparse/<artifact-revision>/
    asset-db/                                   # catalog/snapshot manifests; data service volumes are admin-owned
    evidence/
      production-readiness/<checkpoint>/<phase>/
  .local/state/simworld-studio/
    checkpoints/                               # mode 0700 dir, 0600 non-secret ledgers
    runtime-registry/
    vista-imports/
    vista-scene-builds/
    vista-animation/
    logs/
```

System deployments may use `/opt`, `/var/lib`, `/etc/simworld`, and `/run/simworld` instead. Those
paths are administrator-owned and must map to the same immutable Git/config/content generations.
Secrets never live beneath Git checkout or evidence roots.

## 4. Source synchronization design

### 4.1 Release generation

The coordinator first pushes a reviewed integration checkpoint. The target then:

1. runs `git ls-remote` for the exact branch;
2. records the remote SHA without assuming a future HEAD;
3. clones that branch into a unique `.partial-<timestamp>` path;
4. verifies HEAD equals the earlier remote SHA, status is clean, remote URL is correct, and `git fsck`
   succeeds;
5. renames the checkout to `checkouts/<full-sha>`;
6. runs offline validation from that generation;
7. only after phase-specific approval, binds runtime config to that absolute generation.

An existing generation is immutable. If `checkouts/<sha>` already exists, compare it and reuse only
when clean/exact; never overwrite. A branch update creates another generation.

### 4.2 Remote development

Remote Codex creates a worktree/branch named `codex/remote-82-<bounded-task>` from the recorded
checkpoint. It owns explicit paths, commits one logical unit, pushes it to GitHub, and hands the SHA
to the coordinator. The coordinator integrates it into `codex/vista-production-completion`; target
activation waits for a new integration checkpoint. No scp/rsync of source changes is allowed.

## 5. Runtime trust graph

```text
authoritative verified VISTA row
  -> private staged vista-import-source/v1 bundle
  -> owner-bound vista-simworld-scene/v1 artifact
  -> exact asset snapshot + manual/semantic bindings
  -> server-pinned vista-scene-build-plan/v1
  -> active Studio lease + exact slot broker
  -> disposable UE scene + immutable content receipt/PBR evidence
  -> backend PIE + dedicated animation transport/content driver
  -> timeline artifact + 0/2/5/9/12 evidence
```

Every arrow validates its upstream revision/digest again. The browser, Claude, NLP prompt, or public
request cannot provide owner IDs, slot ports, `/Game` paths, plugin identities, animation functions,
or arbitrary script bodies. Session/slot/lease come from server state; asset/content/plugin pins come
from operator-controlled files.

## 6. Phase design

### 6.1 Connectivity, inventory, and ownership

The first recovered SSH session is read-only. It refreshes stale 2026-07-15 facts and records current
listeners, Docker access, GPU/driver, UE version/paths, toolchain, disk, permissions and services. Any
unknown listener or directory blocks that resource. An ownership/checkpoint file is written only after
the operator chooses a phase and owner.

### 6.2 UE 5.3.2 plugin and project generation

There are three distinct proofs:

1. **Portable source/offline contract:** already code-ready.
2. **Exact-engine package:** rebuild with the target's full UE 5.3.2 `RunUAT.sh`, record UHT/build logs,
   binary SHA and artifact manifest. The UE 5.7.3 package cannot cross this boundary.
3. **Project live proof:** install into a disposable project generation, exact-dispatch the four reserved
   commands before the generic bridge, load the module, answer a nonce challenge from the active lease,
   and preserve a process-specific receipt.

The packaged runtime/archive remains immutable. Writable project content is copied/reflinked into a
new generation after disk review. Rollback switches to the prior project/plugin generation; it does
not edit the archive in place.

### 6.3 VISTA staging and asset stack

Raw staging is a filesystem-only adapter and precedes any service. The dataset owner names every input;
default invocation is dry-run. Approved apply creates an immutable private bundle and registry snippet.

Asset provisioning then pins five identities before any index write:

- UE Content revision;
- catalog/snapshot revision;
- dense model artifact revision and dimension;
- sparse model artifact revision;
- PostgreSQL schema/image and Qdrant/embedding image digests.

Services stay loopback. Order is preflight -> service config -> approved start -> schema -> migration
dry-run/apply -> Qdrant dry-run/apply -> Blueprint/StaticMesh UE probe -> snapshot capture/verify ->
backup/restore. Readiness consumes a short-lived digest-bound live-audit receipt.

### 6.4 Typed scene proof

The Studio session is acquired first so all later calls resolve the same owner/session/slot/lease.
The staged bundle is previewed and committed. Scene build then requires:

1. a production layout profile pinned by raw-file SHA;
2. all required asset bindings resolved against the active snapshot;
3. plan and preflight IDs from server output;
4. explicit exact-plan confirmation;
5. UE result with actor class/asset/material paths, object GUIDs, content receipt, collision/floating
   reports and screenshot bound to one scene digest;
6. a second live digest check immediately before backend PIE.

The scene is disposable and rollback-capable. Any fallback asset, `/Engine/BasicShapes/*`, empty/PBR-
ineligible material slot, stale lease/content receipt, or changed digest prevents Start.

### 6.5 Project character content and timeline

The project owner implements `IVistaAnimationContentDriver` inside reviewed UE project code. A pinned
`vista-animation-content-profile/v1` maps the seven fixed actions to packaged implementation assets and
completion signals. The HTTP/NLP side only sees opaque binding/action IDs.

Live acceptance uses the same scene build proof, plugin manifest, content profile, active lease and UE
process instance. Required matrices cover normal completion, notify timeout, Stop race, disconnect with
outcome unknown, server restart/recovery-required state, fall collision, recover alignment, hand/foot
contact, caster physics, and terminal scene zero-diff. Browser controls never act as the clock or click
UE toolbar coordinates.

### 6.6 Review and public WebRTC

Review is enabled only after two separately approved, tool-free calls produce build-bound receipts.
Visual capture goes through the shared read-only UE broker and must leave the canonical scene digest
unchanged.

Public streaming preserves private data-plane listeners:

```text
browser HTTPS/WSS -> existing managed ingress -> Node session gateway
                                      -> loopback Cirrus HttpPort
UE streamer --------------------------> loopback Cirrus StreamerPort
browser ICE <--------------------------> public Coturn -> bounded UDP relay range
```

The release receipt combines listener/firewall audit, DNS/certificate/config fingerprints, session-
isolation probes, decoded frames/input/reconnect, normal ICE and forced UDP/TCP/TLS relay from external
networks, plus one approved credential-rotation drill.

## 7. Evidence layout

Each phase writes beneath a new checkpoint directory outside Git:

```text
<checkpoint>/
  source.json                  # repo URL, branch, exact SHA, clean status
  ownership.json               # owner, phase, paths, slot/GPU/ports, gate reference
  inventory/                   # redacted read-only host reports
  ue-plugin/                   # build logs, hashes, artifact/load/capability receipts
  vista-source/                # safe staging report and bundle digest
  assets/                      # model/image/config digests, audit and restore receipts
  scene/                       # import/plan/preflight/result and safe visual evidence
  animation/                   # content/plugin/live/run/evidence receipts
  review/                      # safe Text/Visual receipts only
  webrtc/                      # listener/probe/readiness receipts; no SDP/IP secrets
  rollback.json                # prior generations and executed result
```

Mode is `0700` for directories and `0600` for files. Raw prompts, provider stderr, credentials, absolute
private media paths, SDP/candidate addresses, or secret-bearing configs are excluded.

## 8. Rollback model

| Failure | Rollback |
| --- | --- |
| Source validation | delete/rename only the new partial checkout; keep prior generation active |
| Dependency/test | keep failed generation as evidence; do not alter active runtime |
| Plugin build/load | unload/stop only owned disposable UE process; switch to prior project/plugin generation |
| Asset migration/index | stop approved writers; restore prior DB/Qdrant backup into disposable targets; do not relabel partial index ready |
| Scene/timeline | Stop via exact lease, reconcile PIE/actions, preserve failed run; discard only the owned disposable scene generation |
| Review | write safe failure code; no automatic retry and no receipt |
| WebRTC | close newly approved rules/listeners, restore prior pinned config/secret generation, repeat listener audit |

Rollback never uses `git reset --hard`, broad deletion, broad `pkill`, or an unknown listener/service.
Production activation is a pointer/config switch to a previously verified immutable generation, followed
by readiness and rollback verification.

## 9. Traceability

- RMT-001/003/012 -> immutable GitHub checkout, ownership checkpoint, generation rollback.
- RMT-002/004 -> read-only first contact, secret/evidence separation.
- RMT-005/009 -> exact UE 5.3.2 plugin, content driver, backend PIE/timeline evidence.
- RMT-006 -> authoritative raw staging and privilege boundary.
- RMT-007/008 -> full semantic stack and typed PBR scene build.
- RMT-010 -> two real provider receipts.
- RMT-011 -> managed public WebRTC/Coturn and external forced relay.

Detailed command sequences are in [runbook.md](runbook.md); live status is recorded only in
[HANDOFF.md](HANDOFF.md).

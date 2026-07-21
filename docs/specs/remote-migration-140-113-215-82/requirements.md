# Requirements: VISTA production continuation on 140.113.215.82

Status: Approved for staged continuation; all Admin/Data/Cost/State/Public-Network
gates remain independent

Updated: 2026-07-21 Asia/Taipei

Target: `yhliu@140.113.215.82`

## 1. Problem

2026-07-15 已把當時的 dirty Studio/Python source、UE runtime archive 與 sanitized
evidence 搬到目標機，並完成當時的 offline baseline。但現在主要實作已移到
`IvesLiu1026/SimWorld-Studio` 的 `codex/vista-production-completion` integration
branch；舊 dirty tree 不再是後續程式碼同步來源。

目前 code 已具備 typed VISTA import/SceneSpec/BuildPlan、lease-bound runtime、
fail-closed semantic asset contracts、固定 animation protocol、Review smoke runner、
以及 public WebRTC security contracts。尚未取得的是目標主機上的外部證據：完整
Postgres/Qdrant/embedding index、匹配 UE Content 的真實 PBR assets、UE 5.3.2 plugin
live load、project-owned IK/montage content driver、兩次真 provider smoke，以及
DNS/TLS/Coturn/firewall/forced-relay。

2026-07-21 的 read-only SSH 重試回傳 `No route to host`。連線未建立，沒有任何遠端
command 被執行，因此不能把舊盤點當作當前 live state。

## 2. Goals

1. 從 GitHub integration branch 建立可重現、可回滾的 exact commit checkout；不複製
   任一 dirty working tree。
2. 在 target 重新盤點 hardware、disk、GPU、UE、listeners、toolchain 與既有 service
   ownership，所有 state change 前先取得 gate。
3. 以目標 UE 5.3.2 重建、安裝並 live-load `VistaAnimationContentApi`；UE 5.7.3
   package 只保留為跨版本 compile evidence，不能直接部署。
4. 將 authoritative verified VISTA row 以 dry-run-first 流程 stage 成
   `vista-import-source/v1` bundle。
5. Provision immutable embedding model artifacts、Postgres、Qdrant、embedding service，
   完成一致的 asset snapshot/live-audit receipt。
6. 以 typed `SceneSpec -> BuildPlan -> preflight -> execute` 在 disposable UE scene
   生成 Blueprint 與 StaticMesh，證明 exact `/Game` path、材質 slots、PBR 與 scene
   receipt；不得退回 BasicShapes。
7. 實作並驗證 project-owned character driver：look-at、brace、drag、lift-foot、pause、
   fall、recover，包含 hand/foot IK、montage notify、collision、Stop/reconcile 與
   0/2/5/9/12 秒 evidence。
8. 在明確成本核准後，各執行一次真實 Text Review 與 read-only Visual Review。
9. 在管理員完成 DNS/TLS/Coturn/firewall 後，以至少兩個外部網路完成 normal ICE 與
   forced-relay 測試，不依賴 SSH/Xpra。

## 3. Non-goals and standing prohibitions

- 本 spec 的存在不授權 `sudo`、package install、Docker daemon access、database
  migration、model download/index build、UE mutation、provider call、DNS/firewall change、
  public listener 或 deploy。
- 不覆寫或清理 `/home/yhliu/SimWorld-Studio-src` 舊 migration snapshot，也不把它
  merge、rsync 或 tar 回 integration branch。
- 不複製 `.env`、Claude/Codex login state、SSH keys、Studio/TURN/DB/provider secrets、
  raw provider logs、run-local MCP configs 或 browser session state。
- 不把 `claude-opus-4-8` default 或 fake-provider tests 當成真 provider evidence。
- 不把 UE 5.7.3 BuildPlugin success 當成 UE 5.3.2 load/content/timeline evidence。
- 不接受 arbitrary Python、console、Blueprint reflection、`vbp`、caller asset path 或
  free-form MCP mutation 作為 production NLP/animation path。
- 不以 basic Cube/Plane、test fixture profile、UI success label、health `200` 或 browser
  telemetry取代 live asset/content/runtime evidence。

## 4. Requirements

### RMT-001 — GitHub-only source checkpoint

WHEN target 同步程式碼 THEN source SHALL 是
`git@github.com:IvesLiu1026/SimWorld-Studio.git` 的 remote branch
`codex/vista-production-completion`。

WHEN 執行同步 THEN operator SHALL 先用 `git ls-remote` 解析 exact commit，再建立新的
clean checkout generation、驗證 `git status --porcelain` 為空並記錄 commit。文件不得
硬編碼未來 eventual HEAD。

IF integration branch 尚未 push、remote SHA 與 coordinator 公告不一致或不是 reviewed
checkpoint THEN同步 SHALL停止。不得以 local rsync/copy dirty checkout 繞過 GitHub。

Remote-only 修正 SHALL 在獨立 `codex/remote-82-*` branch/worktree 完成、commit、push，
再由 coordinator 整合；不得直接改 detached activation checkout。

### RMT-002 — Connectivity and read-only inventory first

IF SSH route/auth 尚未恢復 THEN所有 target action SHALL保持 blocked。

WHEN connectivity 恢復 THEN第一個 session SHALL只執行 `hostname`、`id`、OS/kernel、
GPU/driver、memory/disk、Node/npm/uv/git、Docker access、UE paths/version、listeners、
systemd unit presence 與 filesystem ownership盤點。不得同一輪安裝、啟服務或修改設定。

### RMT-003 — Explicit ownership and checkpoint

BEFORE 任一 stateful phase THEN operator SHALL記錄：human/agent owner、Git SHA、source
checkout、runtime generation、target paths、GPU、slot、ports、tmux/systemd unit、start time、
gate approver與rollback generation。

Only one owner MAY control UE/Cirrus/Studio lifecycle or asset migration at a time。未知 PID、
listener、directory 或 service unit SHALL視為他人資產；不得 broad `pkill`、刪除或搶 port。

### RMT-004 — Secret and evidence separation

All secrets SHALL由 target 的 mode `0600` regular non-symlink file、systemd credential 或
approved secret manager提供。Secrets不得進 Git、argv、URL、receipt、browser-readable
storage或未遮罩 log。

Each live proof SHALL保存 safe receipt，至少綁定 exact Git SHA、UE engine/content revision、
plugin/model/image/config digest、owner/session/slot/lease（適用時）、timestamp、result與
artifact SHA。Code-only tests SHALL與 live evidence 分開標示。

### RMT-005 — UE 5.3.2 plugin rebuild and load

The target SHALL先證明 actual project 使用 UE `5.3.2`，並使用相同 minor/patch/platform 的
full build root執行 UHT/BuildPlugin。若 packaged runtime 沒有可用 `RunUAT.sh`/headers/toolchain，
管理員 SHALL提供 matching build root；不得使用 UE 5.7.3 binary。

The final loaded module SHA、build ID、engine version與 target platform SHALL寫入 root-owned
`vista-animation-ue-plugin-artifact/v1` manifest。Private listener SHALL對四個 reserved
`vista_animation_*` command exact-dispatch，unknown command terminal reject，mutation
`maxAttempts=1`。

Plugin source/package success SHALL NOT enable timeline readiness until live nonce challenge、
process instance、slot/content binding與 dedicated transport receipt 全部通過。

### RMT-006 — Authoritative VISTA raw staging

The dataset owner SHALL提供 immutable `vista-verified-source-manifest/v1` 或完整
`vista-verified-sample/v1` JSONL、selected attempt、render script、no-oracle dialogue與 media，
包含 exact checksums/bytes/duration/identity。

Staging SHALL先 dry-run；`--apply` 需要單獨 state approval。Oracle labels、review notes、
seeds、visible-evidence atoms與 assist-step labels不得進 evaluation-safe input。Output SHALL
是 private、atomic、idempotent bundle；任何 drift/symlink/extra file fail closed。

### RMT-007 — Complete semantic asset stack

Before provisioning，管理員 SHALL核准 pinned image digests、immutable dense/sparse model
directories與 manifests、secret files、persistent volumes、backup root、retention、free-space
threshold、database/index revision及 maintenance window。

Postgres、Qdrant、embedding SHALL只 bind loopback。Schema/migration/index SHALL依序執行
offline preflight、dry-run/count review、explicit apply、live audit、backup/restore drill。

Readiness SHALL要求 catalog/Postgres/Qdrant counts、snapshot revision、embedding recipe/model
artifact revision、dense dimension與 exact UE Content revision 全部一致且 live receipt 未過期。
任一失敗 SHALL回 `not_ready`；不得生成 Cube/Plane 或宣稱 PBR ready。

### RMT-008 — Typed scene generation proof

Natural language SHALL只被編譯成 validated `vista-simworld-scene/v1` 與 server-pinned
`vista-scene-build-plan/v1`；runtime SHALL不直接執行自由文字或 generic MCP mutation。

Disposable proof SHALL使用同一 owner/session/slot/lease：commit import、plan、preflight、
explicit confirm、execute、backend PIE setup/state/stop。Every actor SHALL有 verified
`/Game` class/asset path；every mesh material slot SHALL有 exact `/Game` MaterialInterface path
與 PBR evidence；content receipt/digest在 start 前重新驗證。

Acceptance SHALL至少包含一個 Blueprint與一個 StaticMesh，以及 chair、cabinet、box、stool、
character required bindings。Collision/floating reports、screenshot與 actor snapshot SHALL綁定
同一 scene digest。任何 TOCTOU drift、lease change、fallback surface或 unresolved required
binding SHALL在 mutation 前 fail closed。

### RMT-009 — Character driver, IK, montage, and timeline

Project content owner SHALL實作 `IVistaAnimationContentDriver`，以 server固定 action IDs 對應
verified pawn、skeleton、AnimBP/Control Rig、IK anchors、montages、notifies與 cleanup。Caller
不得提供 path/function/script。

Required proof SHALL涵蓋 look-at、two-hand brace、chair drag with caster physics、lift-foot、
pause、directional fall與 explicit recover/capsule alignment。每個 action SHALL有 precondition、
completion signal、timeout、single-attempt mutation與 compensating cleanup。

The 12-second run SHALL由 backend PIE lifecycle與 server monotonic scheduler控制。Start 需
confirmed PIE/possession；Stop需取消 pending events、stop/release/restore、end PIE並確認 stopped。
Normal、timeout、Stop race、disconnect、outcome-unknown與 restart reconciliation SHALL各有
disposable evidence；0/2/5/9/12 秒 pose/interaction/screenshot/scene validation全部綁定同一
binary/content/source revision。

### RMT-010 — Two real Review provider smokes

AFTER exact cost approval，operator SHALL執行恰好一個 Text與一個 read-only Visual smoke，
explicit provider/model/build/CLI、no tools、no retry、bounded timeout/budget/tokens。Visual
before/after canonical scene digest SHALL相同。

Production readiness SHALL同時 pin兩份未過期 `PASS` receipts；fake HTTP tests、單一 receipt、
legacy `REVIEW_READINESS_VERIFIED` 或 provider availability probe 不足以通過。

### RMT-011 — Public Coturn/WebRTC

Administrator SHALL決定 Studio/TURN DNS、既有 80/443 ingress owner、TLS/certificate renewal、
Coturn realm/public-private mapping、REST/HMAC secret rotation、quota/monitoring、firewall/ACL/NAT
與 bounded UDP relay range。

Node、Cirrus、UE streamer/SFU/MCP/UnrealCV SHALL保持 loopback；public only HTTPS/WSS/TURN。
Acceptance SHALL從至少兩個 independent external networks執行 normal ICE與 forced UDP/TCP/TLS
relay，驗證 decoded frames、input data channel、reconnect、cross-slot denial與無 raw/loopback
endpoint。Browser telemetry alone SHALL NOT satisfy readiness。

### RMT-012 — Rollback and release truth

Each phase SHALL有可獨立回滾的 generation。Rollback只能停止 registry/checkpoint記錄的
owned process，並切回 prior Git/config/plugin/model/index/content generation；不得改動舊
migration snapshot或未知 service。

Production-ready only WHEN typed scene、animation、two review receipts、asset live audit、public
WebRTC external receipt、backup/restore、observability與 user/admin sign-off 全部存在。任何一項
仍是 external gate時，status SHALL分別寫 `code_ready`、`blocked` 或 `not_ready`，不得寫
`complete`。

## 5. Gate matrix

| Gate | Approver | State change / cost | Required before |
| --- | --- | --- | --- |
| Connectivity | network/host owner | SSH route/ACL | any target command |
| Admin prerequisites | administrator | packages, group/service/filesystem ownership | UE build, Docker, public ingress |
| VISTA data | dataset owner | authoritative projection and staged bundle | import commit |
| Asset data | data/admin owner | models, DB migration, full index, backups | production scene build |
| UE scene | runtime/content owner | disposable project mutation and PIE | scene/timeline proof |
| Review cost | user | exactly two provider calls | review readiness |
| Public network | admin/security | DNS/TLS/Coturn/firewall | external WebRTC |
| Release | user + admin | activation/rollback | Production-ready claim |

## 6. Approval interpretation

使用者已核准繼續完成整體目標與本 spec 更新；這授權 code/spec work 與離線驗證，不會
合併上述 gate。每個付費、資料、UE mutation、system/admin、public-network action 仍需在
執行前取得對應批准。

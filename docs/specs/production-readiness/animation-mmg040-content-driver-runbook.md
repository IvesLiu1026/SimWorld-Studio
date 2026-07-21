# `mmg_040` Project-owned Animation Content Driver Runbook

狀態：**source/profile contract 與 concrete policy driver 已完成；UE 5.3.2 content、project
backend、live inspection receipt、plugin rebuild/load 與 12 秒 evidence 尚未完成。** 本文件不
是人物動畫已可執行的證明。

## 目前結論

`packaging/simworld_arena` 的 launcher 會釘住 UE 5.3 demo 的 Third Person Blueprint 與
Mannequin dependency tree digest，但 repository package 本身沒有 `.uproject`、沒有
`VistaAnimationContentApi` binary，也沒有 `mmg_040` 專用的 skeleton、AnimBP、Control
Rig、IK Rig、montage 或 notify。官方 minimal archive 裡的 Manny、LiftSet、fall loop 與 IK
assets 只能當 retarget／authoring source；目前沒有 live AssetRegistry／load／skeleton
compatibility receipt，所以不能直接映射成 executable action。

這個 change set 新增：

- `FVistaMmg040ContentDriver`：完整實作 `IVistaAnimationContentDriver` 的 project-owned policy
  layer；
- `IVistaMmg040ProjectBackend`：只有七個 typed start methods，沒有 generic command、path、
  reflection、Python、console 或 `vbp`；
- byte-pinned `vista-animation-project-profile-source/v1`；
- strict `vista-animation-content-inspection-receipt/v1` schema；
- non-mutating profile preparation helper；
- subsystem configure-time receipt binding；driver receipt 與 server trusted proof 不一致時，
  listener 不會進入 configured state。

## Pinned content namespace

可執行內容只能由 project content owner 建立在 `/Game/VISTA/MMG040/`。官方 archive 的
`/Game/Human_Avatar/...` 或 `/Game/Characters/...` 不能直接被 caller 指定，也不能因為檔名
相似就通過 preflight。

| Role | Exact object path | Live requirement |
| --- | --- | --- |
| Pawn class | `/Game/VISTA/MMG040/Character/BP_MMG040Character.BP_MMG040Character_C` | spawnable generated class |
| Skeletal mesh | `/Game/VISTA/MMG040/Character/SK_MMG040Character.SK_MMG040Character` | exact skeleton binding |
| Skeleton | `/Game/VISTA/MMG040/Character/SKEL_MMG040Character.SKEL_MMG040Character` | immutable package digest |
| AnimBP class | `/Game/VISTA/MMG040/Character/ABP_MMG040Character.ABP_MMG040Character_C` | exact generated class/target skeleton |
| Control Rig | `/Game/VISTA/MMG040/Rigs/CR_MMG040Character.CR_MMG040Character` | hand/foot/gaze controls verified |
| IK Rig | `/Game/VISTA/MMG040/Rigs/IK_MMG040Character.IK_MMG040Character` | preview mesh/skeleton/chain compatibility |
| Look-at | `/Game/VISTA/MMG040/Montages/AM_MMG040_LookAt.AM_MMG040_LookAt` | `vista_look_at_completed`, no root motion |
| Brace | `/Game/VISTA/MMG040/Montages/AM_MMG040_Brace.AM_MMG040_Brace` | `vista_brace_contact_verified`, no root motion |
| Drag | `/Game/VISTA/MMG040/Montages/AM_MMG040_DragChair.AM_MMG040_DragChair` | `vista_drag_distance_reached`, root motion |
| Lift foot | `/Game/VISTA/MMG040/Montages/AM_MMG040_LiftFootHesitate.AM_MMG040_LiftFootHesitate` | `vista_lift_foot_contact_verified`, no root motion |
| Pause | `/Game/VISTA/MMG040/Montages/AM_MMG040_HoldPose.AM_MMG040_HoldPose` | `vista_pause_completed`, no root motion |
| Fall | `/Game/VISTA/MMG040/Montages/AM_MMG040_Fall.AM_MMG040_Fall` | `vista_fall_landed`, root motion/collision |
| Recover | `/Game/VISTA/MMG040/Montages/AM_MMG040_Recover.AM_MMG040_Recover` | `vista_recover_aligned`, root/capsule alignment |

完整 class pin、supporting assets、timeout、capabilities、anchors 與 live checks 位於：

```text
unreal_plugins/VistaAnimationContentApi/ContentProfiles/
  vista-mmg040-project-profile-source-v1.json
```

第一個 production profile 只允許已由現有 server defaults 產生、且可由一份 live behavior
evidence 精確證明的參數：

| Action | Exact allowed parameters | Required observed live checks |
| --- | --- | --- |
| `look_at` | `duration_sec=1` | `constrained_gaze`, `completion_notify` |
| `brace` | `hand=both`, `duration_sec=2` | `both_hand_contact`, `feet_planted`, `completion_notify` |
| `drag` | `hand=right`, `distance_cm=120`, `duration_sec=2` | `hand_contact`, `root_motion`, `caster_physics`, `completion_notify` |
| `lift_foot` | `foot=left`, `height_cm=35`, `duration_sec=2` | `foot_contact`, `no_penetration`, `completion_notify` |
| `pause` | `duration_sec=3` | `pose_hold`, `completion_notify` |
| `fall` | `direction=forward` | `collision_transition`, `landed_pose`, `completion_notify` |
| `recover` | `direction=forward` | `root_alignment`, `capsule_alignment`, `completion_notify` |

`backward`／`left`／`right` fall 或 recover 目前一律
`ANIMATION_MMG040_PARAMETERS_UNVERIFIED`；不得用 forward evidence 解鎖其他方向。若要擴大
parameter domain，必須逐 variant 取得 evidence、升 profile revision 並重新封裝，而不是只放寬
subsystem 的 wire range。

修改任何 path、notify、root-motion policy、parameter contract 或 action identity 都必須同時升 profile revision、
更新 byte pin、重新 review、重新 build plugin，並取得新的 live receipt。不能只改 JSON。

## Project backend 行為

Content owner 必須實作 `IVistaMmg040ProjectBackend`。它接受的是 enum 與 typed parameters；
caller 不會傳入 montage path 或 function name。

必要條件：

1. `InspectPinnedAsset` 從可信 AssetRegistry/package receipt 讀取指定 enum 的實際 object path、
   class、package SHA-256、skeleton、notify 與 root-motion flag。driver 會逐欄比對 compiled pin。
2. `ResolveActorBinding` 只接受 slot-scoped opaque binding，證明 Pawn class、skeleton、AnimBP
   及所需 actor capabilities。
3. `ResolveTargetBinding` 證明 gaze／hand／foot anchors；drag target 另需 `draggable` 與 caster
   physics 行為證據。
4. `StartLookAt`、`StartBrace`、`StartDrag`、`StartLiftFoot`、`StartPause`、`StartFall`、
   `StartRecover` 必須直接呼叫 project-owned typed implementation。不得在 backend 內再做
   string dispatch、`ProcessEvent`、console、Python、Blueprint reflection 或 generic MCP。
   Action handle 由 driver 使用每個 instance 的 random process-local GUID namespace 加單調序號先
   產生並保留，再交給 typed Start；backend 不能挑選或覆寫 handle。每個 driver instance 最多
   發出 1,000,000 個 handle，達到 deterministic budget 後 quarantine 並要求 rotation/restart，
   不保留無界歷史 set。Start 回 `false` 必須是 atomic no-side-effect，已套用的 montage／IK／
   root-motion／collision／target state 必須先由 backend 自行復原。
5. completion 只能由 exact notify／verified contact signal 產生；`Wait` 必須回傳
   `ObservedCompletionSignal`、opaque `CompletionEvidenceId`、該 immutable artifact 的 lowercase
   SHA-256，且 completion evidence ID 必須包含在 `EvidenceIds`。wall-clock elapsed 不能自己
   變成成功，subsystem 也不會再以 trusted config 字串合成 completion。
6. `Wait` 與 `Stop` 可能同時發生；backend 必須 thread-safe，mutation 不得 transport retry。
7. Start 回 `true` 後若 engine time 或 driver reservation postcondition 無效，driver 會呼叫
   action-typed `RollbackFailedStart(Action, Handle)`。只有它證明 exact handle 已無 transient
   state 才能安全失敗；rollback 失敗會回
   `ANIMATION_MMG040_MUTATION_OUTCOME_UNKNOWN` 並 quarantine preflight。失敗的 Start 不會把
   handle 回傳到 wire caller；只有 driver/backend 內部知道該 handle，因此必須由 trusted
   backend/operator reconciliation 清理並隔離 process/slot，最終以 restart 收斂，不能期待
   client 再呼叫 Wait／Stop／Release。不得繼續新 action。
8. `CaptureEvidence` 必須產生真正的 immutable artifact；不能合成 SHA 或 `pass` assertion。
   Required-target action 的 evidence 也必須使用同一個已驗證 action-target capability/anchor
   pair；不能拿 gaze-only target 當 drag interaction evidence。

## Live receipt workflow

### 1. 先確認 repository state 仍是 blocked

```bash
PLUGIN=/absolute/reviewed/SimWorld-Studio/unreal_plugins/VistaAnimationContentApi
CONTRACT="$PLUGIN/ContentProfiles/vista-mmg040-project-profile-source-v1.json"

node "$PLUGIN/Scripts/prepare-content-profile.mjs" \
  --contract "$CONTRACT" \
  --mode preflight
```

預期 exit `3`，且輸出 `ready=false`、`start_allowed=false`。這不是錯誤繞過，而是目前沒有
content/live receipt 的正確狀態。

### 2. Content owner 在 disposable UE 5.3.2 project 產生 inspection receipt

Receipt 必須符合：

```text
Contract/vista-animation-content-inspection-receipt-v1.schema.json
```

必要 evidence 包含：

- exact engine `5.3.2`、project descriptor digest、project/content revision；
- 13 個 pinned assets 的 load/class/package SHA/skeleton/notify/root-motion observation；
- 7 個 actions 的 implementation/skeleton/completion observation、exact `observed_live_checks` 與
  `verified_parameters`；這兩欄連同 behavior evidence SHA 都會進 canonical content digest；
- brace/drag/lift-foot contact、drag/fall/recover root motion、fall collision、recover alignment；
- Pawn spawn、generated class、mesh/AnimBP compatibility、redirector/dependency closure；
- disposable PIE 與 scene zero-diff check。

Receipt 檔必須是 absolute canonical path、root/current owner、single-link regular file，且所有
ancestor 與檔案都不可 group/world writable；symlink、hard link、duplicate JSON key 或
oversized JSON 會被拒絕。

### 3. 計算 canonical content digest

第一次可以把 receipt 的 `content_digest` 暫填 64 個 `0`，只執行 digest mode：

```bash
node "$PLUGIN/Scripts/prepare-content-profile.mjs" \
  --contract "$CONTRACT" \
  --receipt /absolute/protected/mmg040-inspection-receipt.json \
  --mode digest
```

Digest mode 仍會驗證所有 asset/action/live fields，但輸出固定為 `ready=false`、
`start_allowed=false`；它不會產生 executable profile。將輸出的 digest 寫回 operator-owned
receipt 後，再做下一步。

### 4. 產生 server-owned verified content profile

```bash
node "$PLUGIN/Scripts/prepare-content-profile.mjs" \
  --contract "$CONTRACT" \
  --receipt /absolute/protected/mmg040-inspection-receipt.json \
  --mode profile \
  > /absolute/private-staging/vista-mmg040-content-profile.json
```

Helper 只寫 stdout，不會修改 project、receipt 或 Studio。輸出仍須由管理員放到 root-owned
deployment location，並與 plugin artifact manifest、UE process/slot/scene binding 一起 pin。

### 5. Configure subsystem

Project module 建立 `FVistaMmg040VerifiedProfileReceipt` 與 typed backend，呼叫
`FVistaMmg040ContentDriver::Create`。Factory 會重新驗證 source-contract SHA、13 asset
receipts、7 behavior receipts 與 canonical content digest。之後 `ConfigureTrustedRuntime` 還會
再次把 server trusted proof/action adapter ID、bridge ID、completion signal、timeout 與 driver
receipt 比對；任一 mismatch
回 `ANIMATION_MMG040_*` safe error，listener 不得開放。

## Preflight failure reasons

主要 fail-closed codes：

| Code | Meaning |
| --- | --- |
| `ANIMATION_MMG040_RECEIPT_INVALID` | profile identity/checks/receipt shape 不完整 |
| `ANIMATION_MMG040_PINNED_ASSET_MISSING` | live runtime 無法檢查 exact enum asset |
| `ANIMATION_MMG040_PINNED_ASSET_MISMATCH` | object path/class/skeleton 不符 compiled pin |
| `ANIMATION_MMG040_ASSET_DIGEST_MISMATCH` | runtime package SHA 與 verified receipt 不符 |
| `ANIMATION_MMG040_NOTIFY_MISMATCH` | exact completion notify 缺少或多出 |
| `ANIMATION_MMG040_ROOT_MOTION_MISMATCH` | montage root-motion policy 不符 |
| `ANIMATION_MMG040_ACTOR_CAPABILITY_MISMATCH` | Pawn/skeleton/AnimBP/actor capability 不符 |
| `ANIMATION_MMG040_TARGET_CAPABILITY_MISMATCH` | target class/anchor/capability 不符 |
| `ANIMATION_MMG040_PARAMETERS_UNVERIFIED` | runtime parameters 不等於此 revision 的 exact verified variant |
| `ANIMATION_MMG040_COMPLETION_SIGNAL_MISMATCH` | backend 未回 exact montage notify/contact signal |
| `ANIMATION_MMG040_COMPLETION_EVIDENCE_INVALID` | completion evidence ID/SHA/coverage 不可驗證 |
| `ANIMATION_MMG040_START_OUTPUT_INVALID` | backend 已安全 rollback 一個 postcondition 無效的 start |
| `ANIMATION_MMG040_MUTATION_OUTCOME_UNKNOWN` | failed-start rollback 未證明完成；process/slot 必須 quarantine |
| `ANIMATION_MMG040_MUTATION_QUARANTINED` | 先前 unknown mutation 尚未由 restart/reconciliation 收斂 |
| `ANIMATION_MMG040_ACTION_HANDLE_BUDGET_EXHAUSTED` | driver handle budget 已達上限；必須 rotation/restart |
| `ANIMATION_MMG040_TRUSTED_PROFILE_MISMATCH` | server proof 與 driver sealed receipt 不符 |
| `ANIMATION_MMG040_CONTENT_DIGEST_MISMATCH` | canonical content binding 已變更 |

Preflight error 不得改寫成 walk、generic montage、timer success、Cube、Python 或 `vbp`。

## Offline verification

```bash
node --test \
  unreal_plugins/VistaAnimationContentApi/Tests/offline-contract.test.mjs \
  unreal_plugins/VistaAnimationContentApi/Tests/mmg040-content-profile.test.mjs
node --check \
  unreal_plugins/VistaAnimationContentApi/Scripts/prepare-content-profile.mjs
sh -n unreal_plugins/VistaAnimationContentApi/Scripts/install-plugin.sh
sh -n unreal_plugins/VistaAnimationContentApi/Scripts/build-plugin.sh
git diff --check
```

這些測試只證明 source contract、fail-closed receipt/profile preparation、typed dispatch 與 package
inventory。它們不取代 UE compile/load、content authoring、runtime behavior 或 visual evidence。

## 尚未解除的 live gates

- 13 個 `/Game/VISTA/MMG040/...` assets 尚未 author/import；
- UE 5.3.2 project backend 尚未實作；
- plugin `1.1.0` 尚未以 exact UE 5.3.2 rebuild/package/load；舊 `1.0.0`／UE 5.7.3 binary
  receipt 不適用於這份 source；
- private listener 尚未 exact-dispatch 到新的 driver；
- server `EXPECTED_PLUGIN_SOURCE_FILES`／source audit 尚只涵蓋四個核心檔，
  `source_tree_complete=true` 不能證明 concrete driver/profile/schema 完整；此項由 server
  readiness owner 擴充；
- 尚未取得 live inspection/content/profile receipt；
- chair/cabinet/stool anchors、caster physics 與 collision 尚未驗證；
- 尚未完成 0／2／5／9／12 秒、notify timeout、Stop race、disconnect/restart、fall/recover、
  pose/contact/screenshot/scene-validation evidence。

在以上全部完成前，Production 狀態必須維持：

```text
timeline / character_animation = not_ready
start_allowed = false
cause = ANIMATION_UE_PLUGIN_LIVE_PROOF_MISSING
```

# VISTA Animation／IK Runtime Contract

狀態：code-only contract completed；尚未取得真實 UE content receipt，也尚未跑
disposable UE integration。這份文件不能當作「人物動畫已在 Production 可用」的證明。

## 範圍

本層位於既有 `vista-timeline-compiler.js` 與
`vista-timeline-scheduler.js` 之間，負責把已驗證的 UE content 轉成固定 action
adapters，再把 strict timeline 降成 frame-ordered animation program。

支援的固定 action 名稱為：

- `look_at`
- `brace`（必須有 hand-contact IK target）
- `drag`（必須同時有 hand-contact、draggable、root-motion 能力）
- `lift_foot`（必須有 foot-contact IK target）
- `pause`
- `fall`（只有已驗證 fall montage 時才註冊）
- `recover`（只有已驗證 recover montage 時才註冊）

沒有對應 content receipt 或 live broker capability 的 action 不會被改寫成 walk、
generic montage 或自由 `vbp` 命令；它不會進 capability registry，strict timeline 會在
mutation 前被阻擋。

## 執行順序

1. Content owner 產出 `vista-animation-content-profile/v1`。每個 entry 必須使用
   server 固定的 adapter／bridge action ID，並綁定 UE implementation asset、completion
   signal、content digest 與 verification receipt。
2. Shared UE broker 執行 read-only `preflightAnimation`，逐一確認 pawn class、skeleton、
   actor/target capabilities、IK anchor、implementation asset 與 completion signal。
3. Server 產生 `vista-animation-preflight/v1`。只有 profile receipt 與 live preflight 都
   通過的 action 才形成 executable adapter。
4. 原有 timeline compiler 以該 registry 做 semantic binding/capability check。
5. `vista-animation-program/v1` 將絕對秒數轉成固定 FPS frame，使用
   `(at_frame, frame_order, event_id)` 的穩定順序；12 秒 terminal checkpoint 不能省略。
6. Scheduler 執行每個 adapter 的
   `precondition → execute → completion → cleanup`。Browser 不是 clock authority。
7. Terminal run 產生 `vista-animation-evidence/v1`，綁定 run digest、pose、interaction、
   screenshot 與 scene validation evidence。

## Broker 高階介面

Runtime 只接受以下 server-side 高階方法；不接受 caller-authored Python、montage path、
Blueprint function name 或 `vbp` command：

| 方法 | Mutation | 必要證明 |
| --- | --- | --- |
| `preflightAnimation` | No | exact content/profile digest、capabilities、anchors、completion signal |
| `snapshotAnimationState` | No | snapshot ID、actor/target IDs、state digest、engine time |
| `startAnimationAction` | Yes | fixed bridge action ID、opaque action handle、engine time |
| `waitAnimationAction` | No | exact verified completion signal、engine time、evidence IDs |
| `stopAnimationAction` | Yes | `stopped` 或 `already_stopped` for exact handle |
| `releaseAnimationAction` | Yes | transient IK/root-motion controls released |
| `restoreAnimationState` | Yes | snapshot ID 與 state digest 完全相同 |

`vista-animation-ue-adapter.js` 現在把這七個方法固定為一個 closed operation
allowlist。每個 operation 都有 immutable operation ID、SHA-256 contract
fingerprint、request digest、單次 invocation ID，以及由 server 產生的 128-bit nonce
marker。Nonce 是 JSON request 的資料欄位，不會被插入 caller-authored script 或 command。
Transport response 必須以 exact JSON shape 回傳並逐項 echo operation identity、digest、
invocation 與 nonce；任何遺漏、額外欄位或 stale marker 都 fail closed。

Adapter 對 injected transport 的唯一介面是：

```js
transport.invokeAnimationContentApi(requestJson, brokerOptions)
```

`requestJson` 不含 `/Game` pawn／skeleton／implementation path。它只帶固定 operation、
opaque binding／handle、已正規化 action parameters，以及 server-owned content
profile revision／digest／receipt proof。真實 Content API 必須在 UE plugin 或另一個
受信任的 server-side implementation 內，用 profile proof 查到固定 content；不能讓
HTTP、NLP、Claude 或 browser 指定 Python、Blueprint function、`vbp` command、montage
path 或任意 asset path。

`brokerOptions` 是目前先行固定的 transport contract。所有 mutation（start、stop、
release、restore）必定帶 `mutation: true` 與 `maxAttempts: 1`；transport 必須真的遵守，
不得在 timeout、disconnect 或 marker missing 時內部重送。這些情況回報
`ANIMATION_UE_MUTATION_OUTCOME_UNKNOWN`，同一 semantic mutation 會被 adapter 擋掉，
需由 snapshot compensation／operator reconciliation 處理。Read-only preflight、
snapshot、wait 才允許 `maxAttempts: 2`。

Adapter 另行維護單一 active preflight 與 lifecycle state，要求 snapshot → start；未完成
的 handle 必須先 stop-attempt 再 release-attempt，rollback 才能 restore exact snapshot
ID + digest。所有七種 response payload 都再次做 exact validation，而不是只依賴外層
runtime validator。

若 start 已送出卻拿不到可驗證 handle，runtime 會嘗試 restore snapshot，但 cleanup 必須
標記為 incomplete；不得宣稱角色已停止。一般 failure、timeout 或 operator Stop 則依序：

1. stop known action handle；
2. release transient IK/root-motion controls；
3. restore pre-action snapshot；
4. capture rollback pose／interaction／screenshot evidence。

成功完成的 action 只 release transient controls，不回復成果。例如成功的 drag 不會把
chair 移回原位。`fall` 也不會暗中自動 recover；timeline 必須有一個通過 preflight 的
explicit `recover` event。

## Evidence hooks

Runtime 要求四個 read-only collector 全部存在：

- `pose_snapshot`
- `interaction_state`
- `screenshot`
- `scene_validation`

`brace`、`drag`、`lift_foot` 的 after-event `interaction_state.assertion` 必須是
`pass`，terminal `scene_validation.assertion` 也必須是 `pass`。Evidence 僅保存 opaque
relative artifact reference 與 SHA-256；absolute path、URL、`..` traversal 會被拒絕。

成功 run 至少需要每個 event 的 before／after checkpoint，以及 duration terminal
checkpoint。失敗／取消 run 會保留可取得的 before／rollback／terminal evidence，並在
coverage 中明列缺口，不會假裝完整。

## `mmg_040` 注意事項

Importer 的 golden SceneSpec 現在是 0／2／5／5／9 秒：`look_at`、`drag`、`brace`、
`lift_foot`、`pause`。來源的 5 秒描述明示 brace 與 lift-foot，因此 importer 依固定規則
輸出 `beat-0003-brace` 後接 `beat-0003-lift_foot`；兩者保留同一時間戳，source pointer
分別綁定 `/Scene/Actions/2/brace` 與 `/Scene/Actions/2/lift_foot`。句中其餘動詞不會被
猜測成額外 event。原始 `Scene.Actions` 時間戳仍須嚴格遞增且唯一，只有經來源驗證後的
normalized SceneSpec 允許同秒 event 以非遞減順序存在。

30 FPS 的 `mmg_040` checkpoints 應至少對應 frame 0／60／150／270／360。已明確拆成
同一個 5 秒 frame 的 brace 與 lift-foot 依 stable event ID 取得 `frame_order`
0／1，不依 JavaScript callback race 決定順序。

## 尚未解除的 Live UE／Content Gates

- UE content owner 尚未提供真實 pawn、skeleton、AnimBP／Control Rig、montage 與
  completion notify 的 immutable revision + digest + receipt。
- `mmg_040` 的 chair／cabinet／stool actors 尚未建立並驗證 gaze、hand-contact、
  foot-contact anchors；wheeled chair 尚未證明可安全 drag 且保留 caster physics。
- 尚未把這些高階 broker 方法接到 single-owner UE bridge；現有 generic `agent_action`
  registry 不是等價實作，不能當作 verified adapter。
- Server-side immutable operation fingerprint registry 與 live capability/readiness contract
  已完成，但尚未有受信任、可編譯與已部署的 UE `invokeAnimationContentApi` plugin
  implementation。Repo audit、固定 plugin artifact 要求與管理員部署 gate 見
  `animation-ue-plugin-readiness.md`。現有 generic `execute_python_script`／`vbp`／montage
  path 工具不能接到此 adapter；缺少專用 transport 時必須 fail closed。
- 現有 `UeMcpBroker` 尚未實作並證明 `maxAttempts` contract；把 options 傳入但 transport
  忽略它不算安全接線。正式 integration 必須加入 no-retry mutation transport test，並
  驗證 timeout／disconnect 後不會送出第二個 UE mutation。
- Backend Start／End PIE 與 stopped-state reconciliation 仍由外層 runtime route 負責；
  此 module 只保證 event action cleanup，不宣稱 PIE 已結束。
- 尚未在 disposable map 驗證正常完成、montage notify timeout、Stop race、stream
  disconnect、server restart/reconcile、fall collision 與 recover root alignment。
- 尚未把 terminal evidence 寫入正式 artifact store／retention policy，也尚未接 UI。

因此，沒有真實 profile receipt 時，部署應保持 `start_allowed=false`。測試用 fake
profile／broker 只驗證 contract 與 failure semantics，不能複製到 Production 設定。

## Focused validation

```bash
node --test simworld_studio_workspace/web/server/tests/vista-animation-runtime.test.js
node --test simworld_studio_workspace/web/server/tests/vista-animation-ue-adapter.test.js
node --test simworld_studio_workspace/web/server/tests/vista-animation-ue-readiness.test.js
node --test \
  simworld_studio_workspace/web/server/tests/vista-timeline-compiler.test.js \
  simworld_studio_workspace/web/server/tests/vista-timeline-scheduler.test.js \
  simworld_studio_workspace/web/server/tests/vista-animation-runtime.test.js \
  simworld_studio_workspace/web/server/tests/vista-animation-ue-adapter.test.js \
  simworld_studio_workspace/web/server/tests/vista-animation-ue-readiness.test.js
git diff --check
```

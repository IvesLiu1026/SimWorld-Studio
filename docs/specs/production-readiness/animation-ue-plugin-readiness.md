# VISTA Animation UE Plugin Capability / Readiness Contract

狀態：**server-side contract、四命令 dedicated transport、portable UE plugin source 與
`mmg_040` concrete content-policy driver/profile contract 已完成；project-owned assets/backend、
UE 5.3.2 rebuild、project listener 與 live-load/content receipt 尚未完成，因此 Live UE 仍是
`not_ready`。** 舊 `1.0.0` source 曾通過 UE 5.7.3 BuildPlugin；目前 `1.1.0` source 已變更，
舊 binary/hash 不再是有效 build evidence。本輪沒有啟動 Unreal Editor、修改場景，或把
generic MCP、Python、console／`vbp` 包裝成假的 Content API。

## 結論

目前 checkout 已提供完整、可攜的
`unreal_plugins/VistaAnimationContentApi` source/module、machine-readable API contract、
byte-pinned `mmg_040` content source/receipt schemas、concrete policy driver、dry-run install/build
scripts，以及 artifact/profile helpers。Server 也已把 timeline runtime 接到只有四個方法的
dedicated transport；mutation policy 由固定 operation fingerprint 推導，`UeMcpBroker` 在
queue wait 與每次 bounded read retry 前重驗 lease，所有 mutation 永遠 `maxAttempts=1`。

2026-07-21 的舊 `1.0.0` 可重現建置使用
`/mnt/NAS2/yhliu/UE_5.7.3_prebuilt`。UHT、UnrealEditor Development、UnrealGame
Development、UnrealGame Shipping 與 BuildPlugin package 全部成功。最終 Editor module
SHA-256 為
`d9b43eb89bcf50bdd185933a6d4a199b52cf0cf32ff8a123784ba795f0d58443`，compiled build ID
為 `ue573-9a5eb314-fourcmd2`；完整證據見
`evidence/2026-07-21-host-preflight.md`。

該紀錄只關閉舊 revision 的 protocol／compile／package gate，不是目前 `1.1.0` source 的
compile gate，更不是 content／live gate。目前 plugin 有 `FVistaMmg040ContentDriver`，會把
server proof 綁到 byte-pinned source contract、13 asset receipts、7 behavior receipts，且只
在 receipt 的 exact live-check/parameter coverage 通過後呼叫七個 typed project backend
methods。Runtime completion 還必須帶 backend-observed signal 與 immutable evidence ID/SHA，
不接受 timer 或 subsystem 合成成功；但 repo 仍沒有真實人物 skeleton、AnimBP／Control
Rig、hand/foot anchors、drag physics、fall/recover montages、completion notify 或 backend，
也沒有把四個 reserved commands exact-dispatch 到實際 project listener。因此不能宣稱人物
手腳 IK、跌倒／復原或 12 秒 timeline 已在 UE 執行成功。

## Server-side machine contract

實作：

- `simworld_studio_workspace/web/server/vista-animation-ue-readiness.js`
- `simworld_studio_workspace/web/server/schemas/vista-animation-ue-capability-probe-v1.schema.json`
- `simworld_studio_workspace/web/server/schemas/vista-animation-ue-capability-v1.schema.json`

`createVistaAnimationUeReadinessProbe(...)` 只接受一個具有下列**專用方法**的 injected
transport：

```js
transport.probeAnimationContentApi(requestJson, brokerOptions)
```

只有 `send(...)`、`execute_python_script(...)`、`invokeAnimationContentApi(...)` 或 generic
MCP dispatch 的 object 一律被視為 transport missing。`TIMELINE_AUTOMATION_VERIFIED=true`
也不是 evidence，不能讓這個 probe 變成 ready。

每次 probe 都產生新的 128-bit nonce，建立 request digest，並綁定：

- server-owned `owner_id`；
- Claude/session identity `session_id`；
- single-owner UE `slot_id`；
- exact `scene_revision`；
- pinned content profile revision/digest/verification receipt；
- 七個固定 animation operations 的 allowlist digest。

Capability transport call 永遠是 read-only、`maxAttempts: 1`，即使 underlying transport
有 retry default 也不得重送 challenge。Probe 自己有 bounded timeout；就算 transport
忽略 AbortSignal，也會在 deadline 回傳 `not_ready`。

UE response 必須通過 exact JSON shape 並 echo nonce、challenge digest、slot binding。它還
必須自行回報 compiled plugin artifact、process instance、content proof、security policy 與
完整 operation fingerprints。任何 unknown field、stale nonce、不同 binary SHA、不同
slot/content、缺 operation 或 mutation retry policy 不一致，都 fail closed。

Capability request 刻意**不送** expected plugin artifact 給 UE；UE 不能單純把 server 預期
的 binary manifest 照抄回來。Server 用 root-owned pinned artifact config 與 UE 自行回報的
compiled identity 比對。這個 challenge 證明 protocol liveness/correlation；binary 安裝來源
仍必須由管理員在啟動 UE 前獨立核對 SHA-256，不能只相信 plugin 自我陳述。

### Fixed operation allowlist

| Operation | Mutation | Maximum attempts |
| --- | ---: | ---: |
| `vista.animation.preflight.v1` | No | 2 |
| `vista.animation.snapshot.v1` | No | 2 |
| `vista.animation.start.v1` | Yes | **1** |
| `vista.animation.wait.v1` | No | 2 |
| `vista.animation.stop.v1` | Yes | **1** |
| `vista.animation.release.v1` | Yes | **1** |
| `vista.animation.restore.v1` | Yes | **1** |

Operation ID 之外，request schema、response schema、mutation bit、maximum attempts 與
SHA-256 fingerprint 都被 schema v1 固定。新增或修改 operation 必須升 capability schema
version，不能在 Production 動態擴充。

### Fixed security declaration

一個 ready response 必須逐項符合：

```json
{
  "schema": "vista-animation-ue-security-policy/v1",
  "json_only": true,
  "fixed_operation_allowlist": true,
  "nonce_echo_required": true,
  "request_digest_echo_required": true,
  "slot_binding_required": true,
  "mutation_max_attempts": 1,
  "caller_python": false,
  "caller_console": false,
  "caller_script": false,
  "caller_asset_paths": false
}
```

這不是自由 metadata；任一值不同就 `not_ready`。

## Administrator-owned UE plugin artifact

管理員必須從受控 source/build pipeline 取得或建立名為
`VistaAnimationContentApi` 的 plugin，安裝到實際 UE project。Production source manifest 固定為
以下 16 個 byte-pinned files：

```text
Plugins/VistaAnimationContentApi/VistaAnimationContentApi.uplugin
Plugins/VistaAnimationContentApi/Config/FilterPlugin.ini
Plugins/VistaAnimationContentApi/ContentProfiles/vista-mmg040-project-profile-source-v1.json
Plugins/VistaAnimationContentApi/Contract/vista-animation-content-api-v1.json
Plugins/VistaAnimationContentApi/Contract/vista-animation-content-inspection-receipt-v1.schema.json
Plugins/VistaAnimationContentApi/Contract/vista-animation-project-profile-source-v1.schema.json
Plugins/VistaAnimationContentApi/Source/VistaAnimationContentApi/VistaAnimationContentApi.Build.cs
Plugins/VistaAnimationContentApi/Source/VistaAnimationContentApi/Public/VistaAnimationContentApiModule.h
Plugins/VistaAnimationContentApi/Source/VistaAnimationContentApi/Private/VistaAnimationContentApiModule.cpp
Plugins/VistaAnimationContentApi/Source/VistaAnimationContentApi/Public/VistaAnimationContentApiSubsystem.h
Plugins/VistaAnimationContentApi/Source/VistaAnimationContentApi/Private/VistaAnimationContentApiSubsystem.cpp
Plugins/VistaAnimationContentApi/Source/VistaAnimationContentApi/Public/VistaAnimationContentDriver.h
Plugins/VistaAnimationContentApi/Source/VistaAnimationContentApi/Private/VistaAnimationStrictJson.h
Plugins/VistaAnimationContentApi/Source/VistaAnimationContentApi/Private/VistaAnimationStrictJson.cpp
Plugins/VistaAnimationContentApi/Source/VistaAnimationContentApi/Public/VistaMmg040ContentDriver.h
Plugins/VistaAnimationContentApi/Source/VistaAnimationContentApi/Private/VistaMmg040ContentDriver.cpp
```

其 canonical manifest digest 為
`bdd97f8f967aff67569de708f7c4f18475c54c68371e791e4af4b4c5b09e5b71`。
`inspectVistaAnimationUePluginSource(projectRoot)` 會逐檔以 `O_NOFOLLOW` 開啟，對
`lstat/open/fstat/read/fstat/lstat` identity 做一致性檢查，在 1 MiB 上限內計算 SHA-256；
project root 與所有 ancestors 必須是 canonical non-symlink directories，檔案必須是 single-link
regular file。任何 missing、hash mismatch、hardlink、FIFO、directory、unreadable、oversize、
path replacement 或 TOCTOU identity change 都會令 `source_tree_complete=false`，並出現在
`mismatched_files`／`policy_violations` diagnostics。

`Config/`、`ContentProfiles/`、`Contract/`、`Source/` 使用 recursive exact allowlist；任何額外
entry（尤其 UBT 會自動編譯的 `.cpp`）都列入 `unexpected_entries` 並 fail closed。Plugin root
只明確允許 optional non-production `.gitignore`、`README.md`、`Scripts/`、`Tests/`、
`Binaries/`、`Intermediate/`；它們不屬於 16-file manifest，也不能用來滿足 production source
evidence。`source_tree_complete=true` 仍只代表此 source inventory/bytes 完整，不代表 compile、
loaded binary provenance 或 live behavior 已驗證。

管理員提供給 Studio 的 root-owned pinned manifest 必須是 exact shape（不能加安裝 path、
token 或任意設定）：

```json
{
  "schema": "vista-animation-ue-plugin-artifact/v1",
  "plugin_name": "VistaAnimationContentApi",
  "plugin_version": "1.1.0",
  "plugin_build_id": "ue532-mmg040-reviewed-build-id",
  "binary_sha256": "<64 lowercase hex>",
  "engine_version": "5.3.2",
  "target_platform": "linux-x86_64",
  "api_schema": "vista-animation-ue-content-api/v1"
}
```

部署責任：

1. 用目標機器相同 Unreal Engine minor/patch、platform 與 project modules build plugin。
2. 對最終載入 binary 計算 SHA-256，與 root-owned manifest 比對；manifest 權限不得給
   Studio/browser/agent 修改。
3. 將上述 artifact identity 編譯或封裝進 plugin build；不能從 capability request 取值。
4. 將 verified content profile registry 以 immutable revision/digest 安裝進 trusted UE
   runtime；外部 caller 只能送 profile proof，不能送 `/Game` pawn、skeleton、AnimBP、
   montage 或 Control Rig path。
5. UE 啟動時由 trusted launcher 注入 owner/session/slot/scene binding；capability handler
   必須與自己的 active binding 比對，不能無條件 echo caller JSON。
6. 產生每次 UE process boot 都不同的 opaque `process_instance_id`。
7. 在 disposable project build、load、PIE、normal completion、timeout、disconnect、
   restart/reconciliation 全部驗證後，才可把 timeline policy 設成 required。

## UE plugin endpoint requirements

Plugin 只保留四個固定 command types；名稱與 wire schema 由 portable contract 固定：

- `vista_animation_capabilities`：只處理
  `vista-animation-ue-capability-probe/v1`，read-only；
- `vista_animation_content_api`：只處理 adapter 的
  `vista-animation-ue-request/v1` 七種 operations。
- `vista_animation_engine_time`：只處理 slot-bound、digest-correlated 的 process monotonic
  engine-time sample。
- `vista_animation_evidence_capture`：只把 exact typed context 交給 trusted content driver，
  並回傳 immutable artifact descriptor；plugin 不會合成 screenshot 或 `pass` assertion。

即使共用既有 private listener，也必須在進入 generic
`Bridge->ExecuteCommand(type, params)` **之前**做 exact dispatch；這四個 types 不得落入
generic bridge、Python、console、Blueprint reflection 或 UnrealCV `vbp`。

Plugin implementation 已具備以下 protocol/state-machine 護欄；project listener 與 content
driver 必須維持這些條件：

- 使用 bounded UTF-8 JSON framing 與 exact key/type/range validation；拒絕 unknown fields、
  oversized payload、duplicate semantic mutation 與 malformed JSON。
- 維護 bounded nonce replay cache，並核對 challenge/request digest。
- 只從 compiled fixed registry 將 operation ID + fingerprint 對應到 C++ handlers；沒有
  caller-supplied function name、class name、content path、console command 或 script body。
- 由 slot owner/session/scene context 驗證 actor/target opaque binding ID；caller 不能透過
  binding ID 跨 slot 存取 actor。
- Trusted config 的 adapter ID、bridge ID、completion signal、timeout 必須逐 action 與 sealed
  content contract 相同；第一個 `mmg_040` revision 只接受 server defaults 與 forward
  fall/recover，未驗證 variants 必須 fail closed。
- `Wait` 必須證明 exact completion notify/contact signal，並回傳 evidence list 內的 immutable
  completion evidence ID 與 lowercase SHA-256；wall-clock elapsed 本身不是成功條件。
- mutation 不做 transport retry。若 socket timeout/disconnect 發生在送出後，server 端
  只能得到 outcome-unknown，再走 snapshot/reconcile；不能重送 start/stop/release/restore。
- 可用 invocation ID + request digest 做 idempotency journal 以辨識 duplicate，但 duplicate
  只能回傳已記錄結果或 outcome-unknown，不能再次執行 mutation。
- 不記錄完整 request/response、content path、credential 或 user prompt；structured logs 只
  留 operation ID、fingerprint、digest prefix、process instance 與結果 code。

目前 orphan `MCPServerRunnable_fixed.cpp` 不符合上述要求，而且還會 log raw response；不得
直接把 capability handler 塞進該檔案後宣稱完成。

## Integration gate（部分完成）

Server-side 專用 transport、current lease/slot revalidation、root-owned artifact/content pins、
timeline routes、global readiness proof expiry/revocation與 UI workbench 已接線；
`TIMELINE_AUTOMATION_VERIFIED` 不能取代 live evidence。仍必須完成：

1. 將 package 安裝到 disposable／正式 project，並在 private listener 對四個 reserved
   commands exact-dispatch；任何 `vista_animation_*` unknown command 都 terminal reject。
2. 依 `animation-mmg040-content-driver-runbook.md` author 13 個 pinned project assets，實作
   `IVistaMmg040ProjectBackend`，產生 live inspection/content receipt；現有 concrete driver
   只會驗證與調度 typed backend，不會替代真正 montage／IK／physics/evidence implementation。
3. 以 root-owned manifest、verified content profile及 current owner/session/slot/scene 執行
   live nonce challenge；readiness 失效必須立即 fail closed。
4. 將 plugin package receipt、live capability receipt、content receipt與 disposable UE
   0／2／5／9／12 秒 evidence 綁定同一 source/content/binary revision。
5. 驗證 normal completion、timeout、Stop race、disconnect、restart reconciliation、fall
   collision、recover alignment、hand/foot contact與 scene zero-diff review。

在完成這些 live/content 步驟以前，對外狀態應維持：

```text
timeline / character_animation = not_ready
start_allowed = false
cause = ANIMATION_UE_PLUGIN_LIVE_PROOF_MISSING
```

## Focused verification

```bash
node --test \
  simworld_studio_workspace/web/server/tests/vista-animation-ue-readiness.test.js \
  simworld_studio_workspace/web/server/tests/vista-animation-ue-adapter.test.js \
  simworld_studio_workspace/web/server/tests/vista-animation-runtime.test.js
git diff --check
```

Focused tests cover exact schema/fingerprints, regular-file source audit, four-command dispatch,
valid live challenge, forbidden generic transports, legacy env override rejection, nonce/digest/slot/
artifact/content/security/operation mismatch, unknown fields, response bounds, credential-safe failures,
timeout/cancellation, nonce replay, queue-wait lease revocation and mutation no-retry behavior. The
old `1.0.0` UE 5.7.3 package build is separately recorded as historical host evidence; it does not
attest `1.1.0`, and tests do not substitute for an exact UE 5.3.2 build or live project/content evidence.

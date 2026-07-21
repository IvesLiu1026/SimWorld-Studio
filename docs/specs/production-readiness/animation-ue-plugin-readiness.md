# VISTA Animation UE Plugin Capability / Readiness Contract

狀態：**server-side contract 與 probe 已完成；UE plugin artifact 不存在，因此 Live UE
仍是 `not_ready`。** 本次審查沒有啟動 Unreal Editor，也沒有把 generic MCP、Python、
console 或 `vbp` 包裝成假的 Content API。

## 結論

目前這個 Studio checkout 無法安全實作或編譯
`invokeAnimationContentApi` 的 UE 端點。缺少的是完整 UE plugin source/module，不是再加一個
Node route 就能補上的小缺口。

本次 local source audit 的可重現證據如下：

- tracked UE/C++ build material 只有
  `patches/MCPServerRunnable_fixed.cpp`；沒有 `.uplugin`、`.Build.cs`、`.uproject`、Public
  headers 或完整 module source tree。
- `SimWorld` 是 gitlink `d91058d1e88e4d7da1d84e1cb3527d90cafd3253`，目前 checkout
  沒有可供 Studio build 的 plugin source。
- `apply-mcp-fix.sh` 明確要求另一個外部 UE project 已經存在
  `Plugins/UnrealMCP/Source/UnrealMCP/Private/MCPServerRunnable.cpp`，再把上述單一 `.cpp`
  複製進去；這個 script 不是 plugin source distribution。
- 該 `.cpp` 仍把 caller 提供的 `type` / `params` 交給 generic
  `Bridge->ExecuteCommand(...)`，而且缺少其 header、bridge implementation 與 build
  descriptor，不能據此建立可信的 fixed API。
- 現有 `mcp-server.js` / `unreal-bridge.js` 仍提供 arbitrary
  `execute_python_script`、generic `agent_action` / `vbp`，且 `UeMcpBroker` 以內部固定 retry
  policy 執行，沒有落實 animation adapter 傳入的 mutation `maxAttempts: 1`。

因此本 slice 依 fail-closed 決策只提供：

1. 固定、可機器驗證的 capability response schema；
2. live nonce/digest/slot challenge probe；
3. expected UE source-tree audit；
4. 管理員必須建置與部署的 artifact 契約。

它不宣稱人物手腳 IK、fall/recover montage 或 12-second runtime 已在 UE 執行成功。

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
`VistaAnimationContentApi` 的 plugin，安裝到實際 UE project。最低 source tree 固定為：

```text
Plugins/VistaAnimationContentApi/VistaAnimationContentApi.uplugin
Plugins/VistaAnimationContentApi/Source/VistaAnimationContentApi/VistaAnimationContentApi.Build.cs
Plugins/VistaAnimationContentApi/Source/VistaAnimationContentApi/Public/VistaAnimationContentApiModule.h
Plugins/VistaAnimationContentApi/Source/VistaAnimationContentApi/Private/VistaAnimationContentApiModule.cpp
```

`inspectVistaAnimationUePluginSource(projectRoot)` 只確認這四個都是 project root 內的 regular
files，不接受 symlink。`source_tree_complete=true` 仍只代表 source inventory 完整，不代表
compile、load 或 live behavior 已驗證。

管理員提供給 Studio 的 root-owned pinned manifest 必須是 exact shape（不能加安裝 path、
token 或任意設定）：

```json
{
  "schema": "vista-animation-ue-plugin-artifact/v1",
  "plugin_name": "VistaAnimationContentApi",
  "plugin_version": "1.0.0",
  "plugin_build_id": "vista-animation-linux-ue5.3-build001",
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

Plugin 最多新增兩個固定 command types；名稱與 wire schema 必須由 implementation spec
固定：

- `vista_animation_capabilities`：只處理
  `vista-animation-ue-capability-probe/v1`，read-only；
- `vista_animation_content_api`：只處理 adapter 的
  `vista-animation-ue-request/v1` 七種 operations。

即使共用既有 private listener，也必須在進入 generic
`Bridge->ExecuteCommand(type, params)` **之前**做 exact dispatch；這兩個 types 不得落入
generic bridge、Python、console、Blueprint reflection 或 UnrealCV `vbp`。

Plugin implementation 必須：

- 使用 bounded UTF-8 JSON framing 與 exact key/type/range validation；拒絕 unknown fields、
  oversized payload、duplicate semantic mutation 與 malformed JSON。
- 維護 bounded nonce replay cache，並核對 challenge/request digest。
- 只從 compiled fixed registry 將 operation ID + fingerprint 對應到 C++ handlers；沒有
  caller-supplied function name、class name、content path、console command 或 script body。
- 由 slot owner/session/scene context 驗證 actor/target opaque binding ID；caller 不能透過
  binding ID 跨 slot 存取 actor。
- mutation 不做 transport retry。若 socket timeout/disconnect 發生在送出後，server 端
  只能得到 outcome-unknown，再走 snapshot/reconcile；不能重送 start/stop/release/restore。
- 可用 invocation ID + request digest 做 idempotency journal 以辨識 duplicate，但 duplicate
  只能回傳已記錄結果或 outcome-unknown，不能再次執行 mutation。
- 不記錄完整 request/response、content path、credential 或 user prompt；structured logs 只
  留 operation ID、fingerprint、digest prefix、process instance 與結果 code。

目前 orphan `MCPServerRunnable_fixed.cpp` 不符合上述要求，而且還會 log raw response；不得
直接把 capability handler 塞進該檔案後宣稱完成。

## Integration gate（尚未完成）

這個 module 尚未改動 shared `studio-readiness.js` 或 routes。整合 owner 應在管理員部署真實
artifact 後：

1. 建立專用 transport，分別實作 `probeAnimationContentApi` 與
   `invokeAnimationContentApi`；兩者都不能轉送 arbitrary generic command。
2. 用 root-owned manifest、verified content profile、current owner/session/slot/scene 建立
   `createVistaAnimationUeReadinessProbe(...)`。
3. 將它作為 Studio readiness 的 `timeline` probe override；移除
   `TIMELINE_AUTOMATION_VERIFIED` 作為 Production ready 證據。
4. 只有 live challenge `ready` 時才建立/啟用 animation runtime adapter；readiness 失效要
   fail closed，不能 fallback 到 basic action、generic montage 或 Python。
5. 將 plugin build receipt、live capability receipt、content receipt 與 disposable UE
   evidence 綁定同一 source/content/binary revision。

在完成這五步以前，對外狀態應維持：

```text
timeline / character_animation = not_ready
start_allowed = false
cause = ANIMATION_UE_PLUGIN_TRANSPORT_MISSING
```

## Focused verification

```bash
node --test \
  simworld_studio_workspace/web/server/tests/vista-animation-ue-readiness.test.js \
  simworld_studio_workspace/web/server/tests/vista-animation-ue-adapter.test.js \
  simworld_studio_workspace/web/server/tests/vista-animation-runtime.test.js
git diff --check
```

Focused tests cover exact schema/fingerprints, current-repo source absence, regular-file source audit,
valid live challenge, forbidden generic transports, legacy env override rejection, nonce/digest/slot/
artifact/content/security/operation mismatch, unknown fields, response bounds, credential-safe failures,
timeout/cancellation and nonce replay.

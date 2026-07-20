# SimWorld Studio × VISTA Production Readiness Design

狀態：Approved，對應 `requirements.md`  
原則：先恢復可觀測、可失敗的基礎鏈路，再自動化資料與時間軸，最後公開網路。

## 1. 稽核摘要與設計判斷

| 區域 | 現況 | 判定 |
| --- | --- | --- |
| Core live stack | Studio/UE/MCP/local Cirrus 可用 | Development-ready |
| Asset retrieval | runtime env、catalog、Postgres、Qdrant、embed 均缺；default hybrid 實際被跳過 | Blocked |
| Review auth | Bearer patch + 20 tests通過 | Partial |
| Text Review | inner non-2xx 可被誤判 PASS；尚無真實成功 trace | Blocked |
| Visual Review | capture 會修改 scene，並直接呼叫 UE Python | Unsafe / Blocked |
| Public WebRTC | loopback WebRTC 可用；AWS skeleton 沒接 ICE、session 或 hardened security | Blocked |
| VISTA import | source artifacts存在，SimWorld importer不存在 | Missing |
| Start/Stop | 有固定 script、lease、state reconcile、Stop confirmation | Partial foundation |
| 12 秒 timeline | 有 actions registry，沒有 compiler/scheduler/telemetry | Missing |

另外，active server 的 `/api/health` 只探 UE TCP；同機還有數套歷史 Studio/UE/Cirrus process。設計將 readiness 與 lifecycle ownership 列為先決條件。

## 2. 目標架構

### 2.1 控制面

- **Studio API**：對使用者提供 session、import、build、review、timeline、artifact 與 readiness APIs。
- **Run coordinator**：每個操作建立 `run_id`、綁定 `session_id/slot_id`，持有 cancellation、state transitions 與 correlation context。
- **Artifact store**：保存 source manifest、normalized SceneSpec、asset resolution、review evidence、timeline events 與 scene revision。
- **Readiness registry**：聚合每個 dependency 的狀態與 revision；liveness 不因下游故障而失敗，readiness 會。

### 2.2 資料面

- **UE broker**：唯一 UE command owner；MCP、review、timeline 不得另開未協調 TCP path。
- **Asset services**：versioned catalog + PostgreSQL metadata/FTS + Qdrant vector index + embedding service。
- **Review adapter**：無工具的 provider abstraction，輸入 screenshot/structured scene/request，輸出 strict verdict schema。
- **Timeline runtime**：在 server 端排程、在 UE adapter 執行；browser 僅顯示與送控制命令。
- **Streaming gateway**：loopback Cirrus behind existing trusted ingress；same-origin WSS + TURN ICE，session-to-slot authorization。

## 3. 核心資料契約

### 3.1 Asset snapshot manifest

```json
{
  "schema": "simworld-asset-snapshot/v1",
  "snapshot_id": "ue-content-<revision>-<index-revision>",
  "ue_content_revision": "...",
  "catalog": { "count": 0, "sha256": "..." },
  "postgres": { "schema_version": 1, "row_count": 0 },
  "qdrant": {
    "collection": "...",
    "point_count": 0,
    "dense_name": "text_dense",
    "dense_size": 1024,
    "sparse_name": "text_sparse"
  },
  "embedding": { "version": "...", "dense_model": "...", "sparse_model": "..." }
}
```

所有 count/revision 必須一致後才能把 retrieval 標記 ready。Catalog 與 DB cache key 必須包含 `snapshot_id`，部署新 index 時先 shadow-read，再原子切換 revision。

### 3.2 Normalized VISTA SceneSpec

```json
{
  "schema": "vista-simworld-scene/v1",
  "scene_id": "mmg_040@<source-checksum>",
  "source": {
    "dataset_revision": "...",
    "visual_id": "mmg_040",
    "case_scope": "multimodal_grounded_safety_040",
    "attempt": 7,
    "files": []
  },
  "duration_sec": 12,
  "environment": { "description": "...", "lighting": "..." },
  "camera": { "perspective": "first_person", "constraints": [] },
  "entities": [
    {
      "id": "chair",
      "semantic_query": "black wheeled office chair",
      "required": true,
      "asset_binding": null
    }
  ],
  "relations": [],
  "timeline": [
    {
      "event_id": "beat-0002",
      "at_sec": 2.0,
      "action": "drag",
      "actor_id": "camera_wearer",
      "target_id": "chair",
      "parameters": {},
      "source_pointer": "Scene.Actions[1]"
    }
  ],
  "unresolved": []
}
```

SceneSpec 是 importer、asset resolver、scene builder 與 timeline compiler 之間的唯一 contract。Natural-language source 永遠保留，但 runtime 不直接執行它。

### 3.3 Timeline run artifact

```json
{
  "schema": "vista-timeline-run/v1",
  "run_id": "...",
  "scene_revision": "...",
  "duration_sec": 12,
  "clock": "server_monotonic",
  "events": [
    {
      "event_id": "beat-0002",
      "planned_sec": 2.0,
      "actual_sec": 2.04,
      "engine_time": 2.01,
      "status": "completed",
      "result": {}
    }
  ],
  "final_state": "completed"
}
```

## 4. 模組設計

### 4.1 Retrieval readiness 與 fail-closed build

建議新增：

- `server/retrieval-readiness.js`：catalog/DB/Qdrant/embed probes、revision comparison、short timeout。
- `server/internal-http.js`：共用 authenticated request、status validation、typed error。
- `tools/verify_asset_snapshot.*`：只讀 count/schema/checksum audit。

調整：

1. `/api/chat` 一律呼叫 `resolveAssetMode(body)`；移除 `_amRaw` truthy gate。
2. `searchAssets()` 將 Qdrant/embedding failure 保存在 `causes[]`，Postgres fallback成功時仍輸出 degraded telemetry。
3. `require_real_assets` 在 Production 預設為 true。失敗時 build 在任何 UE mutation 前終止。
4. basic geometry fallback 改成顯式 policy，而不是 agent 自行判斷。
5. `start.sh`、compose、systemd/AWS env 統一名稱與 default；移除 DSN echo、固定弱密碼與 `latest` image。
6. Compose/managed deployment需包含 embedding service與 snapshot mount；目前只有 Postgres/Qdrant不完整。

### 4.2 Review pipeline

建議新增共用抽象：

- `ReviewRunCoordinator`：round state、budget、AbortController、session/run isolation。
- `ReviewProvider`：`review({prompt, scene, images, signal}) -> ReviewVerdict`，禁止 tools。
- `ReviewEvidenceStore`：截圖與 structured evidence、TTL cleanup。

關鍵修改：

1. Text/Visual inner calls 使用同一 `internal-http`，非 2xx 在讀 SSE 前就失敗。
2. Request contract用 `agent`，移除 `runner`/`agent`歧義；builder與critic各自有明確 provider/model。
3. Visual capture只讀 screenshot與actor snapshot；刪除會重建天光/地板/相機的 capture script。
4. Review與repair分成兩階段：Review只輸出 verdict；使用者/政策允許時，下一個 builder round才修改scene。
5. 整併三套 VLM邏輯成同一 adapter與verdict schema。Provider failure使用5xx/typed error，不產生假分數。
6. `/api/verifier-update` 若保留，需 `run_id`、exact schema、Bearer、ownership check；否則改用child-process IPC並移除HTTP callback。
7. Review Off時不執行 CodingVerifierPanel auto-score；mode作為conversation metadata持久化。

Verdict schema至少包含：`status=PASS|NEEDS_IMPROVEMENT|FAIL`、`issues[]`、`suggestions[]`、`provider`、`model`、`evidence_ids[]`、`usage`、`latency_ms`。

### 4.3 Public Pixel Streaming / TURN

保留所有 runtime listeners 在 loopback，整合既有佔用 `80/443` 的 ingress，不另起競爭 port 的 Nginx。

Public request flow：

1. 使用者以 HTTPS取得 Studio session；server以Secure/HttpOnly cookie或等價server-side identity綁定slot。
2. `/api/pixel-streaming-endpoint`只回傳same-origin logical endpoint，例如 `/pixel-stream/session/<opaque-id>`。
3. Ingress驗證session後，將browser signalling WebSocket proxy到該slot的Cirrus **HttpPort**。
4. Cirrus收到由secret-backed config產生的`peerConnectionOptions.iceServers`。
5. Browser/UE完成ICE；telemetry回報selected candidate type但不暴露credential。

必要修改：

- `runtime-security.js`新增`trusted_proxy` profile、固定 public origin、proxy source validation與public CSP。
- `ue-player.html`接受受驗證的same-origin path；不要求raw numeric port，也不允許任意external `ss`。
- `/api/pixel-streaming-url`移除port guessing與Host-derived HTTP URL。
- `useSession`/backend恢復完整slot identity；iframe與signalling均做cross-slot authorization。
- Cirrus launcher生成`BindAddress=127.0.0.1`、access control與ICE config；AWS launcher與hardened launcher共用config builder。
- Coturn採HMAC短效credential或secret-backed static credential，配置external IP、quota、relay range與監控。

現有AWS scaffold有三項不可直接沿用：browser WS目前誤導向StreamerPort、Node security拒絕public Host/Origin、player不支援proxy path。完成forced-relay E2E以前仍標示draft。

### 4.4 VISTA importer

Importer分四階段，全部可dry-run：

1. **Source adapter**：讀取明確manifest row與選定attempt，定位`render_script.yaml`及media；不掃描任意backup/runtime資料夾。
2. **Validator**：驗證identity、checksums、12秒duration、timestamp順序、dialogue與media join。
3. **Normalizer**：把environment、camera、entities、relations與actions轉成SceneSpec；不確定欄位進`unresolved[]`。
4. **Resolver**：以semantic retrieval解析assets與capabilities，輸出preview；commit後才建立scene artifact/build run。

建議 APIs：

- `POST /api/vista/imports/preview`
- `POST /api/vista/imports`
- `GET /api/vista/imports/:run_id`
- `GET /api/vista/scenes/:scene_id`

Request只能指定allowlisted dataset revision、sample id與attempt，不能直接給server任意file path。Idempotency key由source checksums + importer version組成。

Privilege boundary：reconstruction profile可使用經核准render script建立場景；evaluation-input exporter則只取allowlisted no-oracle fields。兩種artifact用不同schema/profile標籤與ACL，避免之後的assist-step eval洩漏。

### 4.5 Timeline compiler與runtime

Pipeline：

1. 解析`[HH:MM]`/seconds並排序。
2. Entity binder連結SceneSpec entity → UE actor/asset。
3. Capability checker把semantic action映射到固定adapter。
4. Preflight回報unsupported/ambiguous events；strict mode有一項不支援就不開始。
5. Backend以monotonic clock排程，adapter透過shared UE broker執行。
6. 每個event以completion signal而非單純sleep結束；超時依policy fail/skip/stop。
7. Stop清除pending queue、呼叫adapter cleanup、結束PIE並確認stopped state。

初始action adapter範圍可包含：`look_at`、`move_to`、`pause`、`pick_up/drop`、既有agent animation。`drag`、`brace`、`lift_foot`只有在對應Blueprint function或montage完成後才宣告支援。

Start必須由UE runtime bridge執行與確認，不再讓browser以固定toolbar座標點擊。現有`vista-runtime-broker.js`的lease、nonce marker、state validation與idempotent Stop可保留並擴充為timeline FSM。

## 5. Security 與 failure policy

- 外部使用者credential、Studio internal service credential、TURN credential與provider credential彼此分離。
- 所有internal HTTP先驗證status/content-type/schema，才處理body/SSE。
- 所有mutating runs具有owner、slot、deadline、AbortSignal與idempotency key。
- Review screenshot與import media視為可能敏感artifact，使用TTL、access check與no-store。
- Retrieval、review、timeline任何required dependency失敗時fail closed；degraded mode需使用者明確選擇並記錄。
- Arbitrary file path、signalling URL、UE Python、provider CLI tools皆不得由public input直接控制。

## 6. Observability

每個run共用：`correlation_id`、`run_id`、`session_id`、`slot_id`、`scene_revision`。Metrics至少包含：

- Retrieval：mode、snapshot revision、各dependency latency、fallback、candidate/result count。
- Review：round、provider/model、HTTP status、schema status、usage、evidence IDs、verdict。
- WebRTC：signalling state、streamer registered、ICE state、candidate type、reconnect；不記IP/credential明文。
- Timeline：planned/actual/engine time、drift、adapter result、cleanup result。

`/health/live`只證明process可服務；`/health/ready`依transport profile與feature policy聚合required components。舊`/api/health`保留相容期，但不得再把UE TCP等同整個stack健康。

## 7. 測試策略

- Unit：mode resolution、typed errors、auth/status handling、schema validators、timestamp parser、capability mapping、FSM/idempotency。
- Contract：Postgres/Qdrant/embed fake/containers、provider fake SSE、verifier callback、session-to-slot、Cirrus config serialization。
- UE integration：disposable map中spawn real assets、read-only visual capture diff、timeline adapter、Stop cleanup。
- Browser E2E：review mode persistence/evidence/cancel、same-origin signalling、cross-slot denial。
- Network E2E：外部網路、forced TURN relay、reconnect、credential rotation。
- Data golden tests：`mmg_040` SceneSpec snapshot與0/2/5/9秒beats；invalid/missing/privileged-field cases。

實際VLM、DB migration/full index及public network tests都需要對應核准；CI預設用fake provider與small fixture。

## 8. Rollout 與 rollback

1. 先部署readiness與觀測，不改現行user path。
2. Review修復用feature flag逐步啟用Text，再啟用read-only Visual。
3. Asset snapshot先shadow query，達到一致性與spawn smoke後，開啟`require_real_assets`。
4. Importer先preview-only，golden fixtures穩定後才允許commit/build。
5. Timeline先strict preflight +少數adapters，再擴充animations。
6. Public WebRTC先單slot、allowlisted testers、forced TURN，最後才多slot。

每階段需能回退feature flag與previous artifact/index revision。Xpra維持admin recovery；不得把Xpra可用當作public WebRTC rollback成功標準。

## 9. 尚待使用者/管理員決策

- Asset snapshot來源與目前UE Content revision是否有可用完整index。
- Production artifact store採本機持久卷、Postgres metadata或object storage。
- 正式Studio/TURN DNS及既有ingress整合方式。
- TURN採REST/HMAC或初期secret-backed static credential。
- Timeline第一版必須支援哪些VISTA actions；`mmg_040`的drag/brace/lift-foot是否列為P0。
- 首次真實Text/Visual VLM smoke的provider/model與成本上限。

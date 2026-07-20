# SimWorld Studio × VISTA Production Readiness Requirements

狀態：Approved（使用者於 2026-07-14 核准）  
日期：2026-07-14  
範圍：Semantic asset retrieval、Review pipeline、公開 Pixel Streaming、VISTA importer、12 秒 UE timeline，以及直接阻擋 Production 的共通能力。

## 1. 現況基線

目前的 Live development stack 只可判定為「核心開發路徑可用」，不可判定為 Production-ready：

- Studio `3022`、UE MCP `55570` 與本機 Cirrus `8595/8596` 正在執行；`/api/health` 回報 UE/MCP connected。
- Builder runtime 設為 `claude-opus-4-8`。
- 遠端畫面仍是 MacBook → SSH tunnel → Xpra；原生 WebRTC 只存在於 server 內部的 UE → Cirrus → server-side Chromium。
- Semantic retrieval 所需的 catalog、Postgres、Qdrant、embedding service 皆未就緒。
- Review 的 Bearer 傳遞已有局部修補與單元測試，但 Text/Visual 都沒有成功的真實端到端證據。
- VISTA 有安全的 setup/state/stop FSM 與個別 agent actions，但沒有 dataset importer、時間軸 compiler 或 12 秒 server-authoritative scheduler。
- Active UE 以 `-NOWRITE` sandbox 啟動；目前 live scene 不應被視為已持久化 artifact。

## 2. 目標

1. 場景生成預設使用可驗證、可生成且保有材質的真實 UE assets；Production 不得靜默降級成 Cube/Plane。
2. Text 與 Visual Review 能正確驗證、隔離、取消及回報失敗，且 Review Off 不產生 VLM 呼叫。
3. 使用者能在不開 SSH/Xpra 的情況下，透過 HTTPS/WSS 與 TURN 直接操作 Pixel Streaming。
4. 經驗證的 VISTA sample 能以版本化、可重現、無資料洩漏的流程轉為 SimWorld scene artifact。
5. VISTA 的 timestamped actions 能在 UE 內依 12 秒時間軸執行、停止、重播並產生可核對 telemetry。
6. 服務 readiness、secrets、session isolation、artifact persistence 與 observability 足以支援 Production rollout。

## 3. 非目標

- 本規格核准前不安裝系統套件、不公開防火牆、不啟動 costly model/indexing job，也不修改 Production ingress。
- 不在本階段完成 PPO/DAgger/BC、完整 Agent Training 或 Co-evolution orchestration。
- 不把任意 Unreal Python 暴露成公開 API。
- 不以 UI 顯示成功取代 backend contract、runtime telemetry 或端到端測試。

## 4. 功能需求

### 4.1 真實資產與 Semantic Retrieval

- **ASSET-001**：系統必須提供單一、版本化的 asset snapshot manifest，記錄 UE Content revision、catalog count、Postgres row count、Qdrant point count、embedding model/version 與 checksum。
- **ASSET-002**：Production 啟動時必須驗證 `ASSET_DB_DIR`、`POSTGRES_URL`、`QDRANT_URL`、collection、embedding service 及 catalog revision；任一 required dependency 不相容時 readiness 必須失敗。
- **ASSET-003**：未在 request 指定 mode 時，backend 必須真正採用 documented default `hybrid`；不得因 request/env 原始值為空而跳過 resolver。
- **ASSET-004**：`search_assets` 必須保留 Qdrant、embedding、Postgres 各自的 root cause、timeout 與 retryability；不得吞掉上游錯誤後只顯示 `POSTGRES_URL is not set`。
- **ASSET-005**：Qdrant 正常時可在 Postgres 暫停的情況下查詢；Qdrant/embedding 暫停時可使用 Postgres full-text fallback；全部失敗時回傳 typed `ASSET_RETRIEVAL_UNAVAILABLE`。
- **ASSET-006**：Production 的 `require_real_assets=true` 時，retrieval 失敗必須阻止 build；basic geometry 僅能在使用者明確選擇 degraded mode 時使用，UI 與 artifact 必須標記降級原因。
- **ASSET-007**：至少驗證一個 Blueprint 與一個 StaticMesh 的 `/Game/...` path 能在目前 UE Content spawn，且 material slots/PBR textures 不為空。
- **ASSET-008**：中文及英文代表性 query 都必須有測試；若 Postgres English FTS 無法達標，需加入 query normalization 或適合的 multilingual strategy。

### 4.2 Text / Visual Review

- **REVIEW-001**：所有 Studio internal calls 必須經共用 auth client 傳遞 Bearer/service credential；credential 不得出現在 URL、argv、client-readable storage 或未遮罩 log。
- **REVIEW-002**：任何 inner `/api/chat` 的非 2xx、timeout、invalid SSE、provider failure 都必須使該 round 與最終 `done` 明確失敗；不得默認為 PASS。
- **REVIEW-003**：UI 選定的 `agent`、builder model、critic provider/model 必須完整傳遞並驗證相容性；不相容組合要在 mutation 前拒絕。
- **REVIEW-004**：Visual capture 必須是 read-only；不得刪除或新增 actor、修改 lighting/sky/ground/camera state。所有 UE 操作必須走 shared broker 與 queue/backpressure。
- **REVIEW-005**：Critic 必須使用無工具、strict-schema 的 VLM adapter；不得以 `--dangerously-skip-permissions` 或 sandbox bypass 執行可存取主機的 agent CLI。
- **REVIEW-006**：Text critic、MCP verifier 與 `/api/vlm-score` 必須整併成一致的 provider/model/schema/error policy；provider outage 不得以 HTTP 200 + 中性分數掩蓋。
- **REVIEW-007**：Review Off 必須保證零 critic/VLM 呼叫；右側自動 VLM score 必須成為獨立且明確的 opt-in。
- **REVIEW-008**：`/api/verifier-update` 必須實作 authenticated、schema-validated contract，或移除 callback 與死程式碼；不得保留 ghost endpoint。
- **REVIEW-009**：Stop 必須以 conversation/run id 取消 builder、critic、summarizer、visual capture 與後續 rounds；不同 session 不得共享 intent summary 或取消狀態。
- **REVIEW-010**：UI 必須保存/恢復實際 review mode，顯示 screenshot evidence、provider/model、round status 與失敗原因。

### 4.3 公開原生 WebRTC / Coturn

- **RTC-001**：UE streamer、MCP、Cirrus internal listeners 維持 loopback；對外只暴露既有受管理 ingress 的 HTTPS/WSS 與 TURN endpoints。
- **RTC-002**：backend 必須有明確的 `loopback` 與 `trusted_proxy` transport profiles。Public profile 只接受設定的 `STUDIO_PUBLIC_ORIGIN`、可信本機 proxy 與安全 forwarded headers。
- **RTC-003**：`/api/pixel-streaming-url` 必須回傳 session-bound same-origin signalling path，不得從未驗證 Host header 猜 hostname、掃描 raw ports 或回傳 `127.0.0.1:85xx`。
- **RTC-004**：Browser signalling WebSocket 必須 proxy 到 Cirrus `HttpPort` 的 player/signalling endpoint，不得暴露或誤接 UE `StreamerPort`。
- **RTC-005**：Cirrus config 必須注入經驗證的 `peerConnectionOptions.iceServers`；至少包含 STUN 與 TURN，並可在測試中強制 `relay`。
- **RTC-006**：TURN credential 必須由 secret store 提供並可輪替；Production 優先採 REST/HMAC 短效 credential。不得使用 repo 內靜態 `CHANGE_ME` 密碼。
- **RTC-007**：session acquisition、viewport URL、signalling subscription 與 UE input 必須綁定同一 slot；session A 不得觀看或操作 session B。
- **RTC-008**：CSP、Secure/HttpOnly cookie、Origin/Host 驗證與 WSS path 必須同時支援 public profile，且 loopback profile 的限制不可被弱化。
- **RTC-009**：Xpra/SSH 保留為受限 admin recovery path，不是 Production 使用者資料平面。

### 4.4 VISTA Dataset Importer

- **IMPORT-001**：Importer 必須支援明確版本的 verified manifest/JSONL 與對應 `pipeline_v2/media/render_script.yaml`，不得以任意目錄掃描猜測 source-of-truth。
- **IMPORT-002**：匯入前必須驗證 sample identity、selected attempt、source checksum、`Duration_sec`、timestamped `Scene.Actions`、media references 與 dialogue join；缺欄位或互相矛盾時產生可讀 validation report。
- **IMPORT-003**：Importer 必須輸出版本化 `vista-simworld-scene/v1` 中介格式，至少包含 provenance、environment、entities、relations、camera contract、duration、timeline、dialogue 與 unresolved mappings。
- **IMPORT-004**：匯入流程必須有 preview/dry-run、commit、status 與 idempotency key；同一 source revision 重跑不得產生重複 scene artifact。
- **IMPORT-005**：資料 profile 必須區分 reconstruction-only privileged fields 與 evaluation-safe fields。Assist-step prediction輸入不得混入 oracle labels、review notes、visible-evidence atoms 或其他受限資訊。
- **IMPORT-006**：Semantic asset mapping 必須記錄 query、candidate IDs、selected path、confidence 與人工 override；無可靠 match 時標記 unresolved，不得自動填入 Cube。
- **IMPORT-007**：任何 LLM-based normalization 都必須保存 prompt/model/version/output checksum，並在實際 mutation 前通過 schema validation 與人工 preview。

### 4.5 12 秒時間軸與角色動畫

- **TIME-001**：Timeline schema 必須使用 `0 <= at_sec <= duration_sec` 的絕對時間、穩定 event id、actor/target references、action、parameters 與 provenance；預設 VISTA duration 為經來源驗證的 12 秒。
- **TIME-002**：Compiler 必須在 UE mutation 前完成 entity binding 與 capability check。Unsupported action（例如 drag chair、brace、lift foot）必須明確列出，不得悄悄改成 generic walk。
- **TIME-003**：Production Start 不得依賴瀏覽器對 UE toolbar 的硬編碼座標點擊；必須由 backend/UE runtime bridge 建立、possess 並確認 play session。
- **TIME-004**：Server-authoritative scheduler 必須使用 monotonic clock，記錄 planned/actual time、drift、result 與 UE `engine_time`；瀏覽器 `setTimeout` 不得成為時間軸真相來源。
- **TIME-005**：Start、Pause（若支援）、Stop、Replay 必須是 session-bound、idempotent FSM；Stop 必須取消未執行 events、停止角色動作並在 timeout 後回報未清理項目。
- **TIME-006**：角色 animation/montage、object interaction、camera/gaze 必須有可測試的 action adapters；每個 adapter 要定義 precondition、completion signal、timeout 與 compensating cleanup。
- **TIME-007**：執行完成後必須產生 timeline run artifact，可對照 0/2/5/9/12 秒關鍵狀態、截圖與 scene validator 結果。

### 4.6 共通 Production 能力

- **OPS-001**：提供 liveness 與 readiness 分離的 health endpoints；readiness 至少涵蓋 UE broker、Cirrus streamer registration、asset catalog/Postgres/Qdrant/embed、review provider、artifact store，public profile 另含 TURN/WSS。
- **OPS-002**：場景、import、review 與 timeline artifacts 必須有持久化、revision、owner/session、TTL/retention、backup/restore 與 rollback policy。
- **OPS-003**：Production service 必須有單一 lifecycle owner；禁止同一 GPU/port family 上殘留多套未登記 Studio/UE/Cirrus stack。
- **OPS-004**：所有 secrets 由 secret store/systemd credentials 提供；start scripts 不得印出完整 DSN、TURN credential 或 access token。
- **OPS-005**：所有跨元件操作帶 correlation id，紀錄 latency、retry、provider/model、asset/index revision、slot 與 outcome，但不得記錄 secret。
- **OPS-006**：部署設定、README、actual defaults 與 tests 必須一致；AWS scaffold 未通過 E2E 前不得標示為 Production-ready。

## 5. 驗收情境

1. 在 retrieval 全部健康時，以中英文查詢「black wheeled office chair」類資產，回傳 exact `/Game/...` path，並在 UE 生成保有材質的 chair、cabinet、box 與 step stool。
2. 分別中斷 Qdrant、Postgres、embedding service，驗證允許的 fallback；全部中斷時 build 被阻止且 UI 顯示 structured root causes。
3. Fake provider 對 Text/Visual 路徑回傳 401、429、500、timeout、malformed SSE，所有 case 都必須失敗而非 PASS。
4. 在 disposable scene 執行一次經批准的真實 Text Review 與 Visual Review；Visual Review 前後 actor/light/ground snapshot 完全相同。
5. MacBook 不使用 SSH/Xpra，以 HTTPS 連線；強制 TURN relay 時 video/data channel 可用，且 DevTools 沒有 loopback/raw 85xx/mixed content。
6. 兩個並行 session 只能看到與操作自己的 UE slot。
7. 對 `mmg_040` 執行 importer preview，輸出 12 秒 scene spec，保留 `[00:00]`、`[00:02]`、`[00:05]`、`[00:09]` beats 與 unresolved action list。
8. Timeline run 能依上述 beats 執行或在開始前明確阻止 unsupported actions；Stop 後 PIE、pending events 與角色動作皆被確認停止。

## 6. 外部依賴與管理員核准

- Asset snapshot/UE Content revision、持久化儲存與 service credentials。
- 正式 Studio/TURN DNS、既有 ingress owner、TLS certificate、自動續期。
- Coturn 安裝/營運、public/private IP mapping、secret rotation、quota 與監控。
- 防火牆/ACL：HTTPS/WSS、TURN listener 與受控 UDP relay range；實際 ports 由管理員依現有 ingress 決定。
- 至少兩個外部網路的 WebRTC/TURN 驗收環境。
- 任何實際 VLM smoke、full asset indexing 或資料庫 migration 前的成本與 state-change 核准。

## 7. 規格核准 Gate

本文件、`design.md` 與 `tasks.md` 經使用者確認後才進入跨檔 production implementation。若使用者要跳過此 gate，需明確指定先做哪一個 bounded phase；公開網路、DB migration、model calls 與系統安裝仍各自需要對應授權。

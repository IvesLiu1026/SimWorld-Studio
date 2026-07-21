# SimWorld Studio × VISTA Production Readiness Tasks

狀態：Approved；Phase 1A／2 與 Phase 1B／3／4 的可離線實作已大幅完成，正式資料、UE Content、provider 與 public network 仍受 live/admin gates 約束（使用者於 2026-07-14 核准）
依賴：`requirements.md` → `design.md` → 本文件
執行原則：每一 phase 是獨立可驗收 change set；不使用 `git add .`，不混入目前工作樹的既有 UI 變更。

## Phase 0 — Baseline、ownership 與安全護欄

- [x] **T0.1** 唯讀確認目前 live path：Studio/UE/MCP/local Cirrus connected，remote path仍為Xpra/SSH。
- [x] **T0.2** 唯讀確認baseline builder runtime為`claude-opus-4-8`、critic/summarizer當時仍為Sonnet；Phase 1A 現已將builder/critic安全預設統一為`claude-opus-4-8`。
- [x] **T0.3** 盤點五大區域及extra Production gaps，保存本規格。
- [x] **T0.4** 在任何implementation前整理現有dirty worktree ownership；將既有Bearer/validator patch與其他UI work拆成可追蹤change set。
- [x] **T0.5** 新增run/correlation context與`/health/live`、`/health/ready`骨架，先以feature flags保持現行行為。
- [x] **T0.6** 新增process/port registry；startup偵測同一slot/GPU/port family的unmanaged duplicate stacks並拒絕或告警。

驗收：既有build仍可用；readiness能顯示asset/review/streaming為not-ready，不再只有整體`ok`。

Implementation note（2026-07-21）：所有工作已隔離到乾淨 integration branch/worktrees，原始 dirty checkouts 保持不動。T0.6 的原子 endpoint lease registry 已接到 slot spawn 前檢查、parent PID/start-token、heartbeat、失敗與 shutdown release；unmanaged listener、corrupt registry、重複 physical slot/port family 均 fail closed。離線測試不會掃描、終止或重啟實際 process。

## Phase 1A — Review pipeline correctness（P0，可與1B平行）

- [x] **T1A.1** 建立共用authenticated internal HTTP/SSE client：Bearer、timeout、AbortSignal、content-type、non-2xx與schema驗證。
- [x] **T1A.2** 將Text/Visual inner `/api/chat`改用共用client；401/403/429/5xx/timeout/malformed SSE全部回`builder_error`及`done.isError=true`。
- [x] **T1A.3** 統一`agent`/builder model/critic provider/model contract；在啟動前驗證相容矩陣。
- [x] **T1A.4** 將critic改成無工具、strict JSON schema provider adapter；移除Claude/Codex danger flags與host-capable agent execution。
- [x] **T1A.5** 移除Visual capture中的actor/light/sky/ground/camera mutations；所有read走shared UE broker。
- [x] **T1A.6** 整併Text critic、MCP verifier、`/api/vlm-score`的schema/error/model policy。
- [x] **T1A.7** 實作或移除`/api/verifier-update`；新增真實HTTP contract test，不再只做source regex。
- [x] **T1A.8** 以conversation/run id實作完整cancel；隔離intent summary、round state與budget。
- [x] **T1A.9** 修正UI stale `loopMode`、conversation persistence、visual evidence/multi-shot顯示；Review Off關閉所有自動VLM。
- [x] **T1A.10** Fake-provider E2E涵蓋Text/Visual成功、所有failure classes、cancel、跨session isolation與cost budget。
- [ ] **T1A.11** 經使用者核准後，在disposable scene各執行一次真實Text與read-only Visual smoke，保存provider/model/usage/verdict與scene diff。

驗收：`requirements.md` REVIEW-001～010全部通過；Visual前後scene snapshot零差異。

Implementation note（2026-07-21）：T1A.8 的 conversation/run cancellation、builder/critic/summarizer/capture abort、intent、round 與 aggregate dollar budget isolation 已完成；builder 與 critic 都受 run 剩餘額度約束，budget 耗盡會在下一個 mutation 前停止。T1A.10 現由實際 HTTP `/api/chat` coordinator harness 覆蓋 Text/Visual success、401、429、500、timeout、malformed SSE、builder failure 不叫 critic、Visual-only capture、exact cancel、獨立 cost budget 與跨 lease isolation。Production review scope 已改由 server-side active Studio lease 權限衍生，不再信任 caller 提供的 session id；loopback 則保留可預期的開發相容行為。T1A.11 仍依 cost/state gate 刻意未執行。

## Phase 1B — Asset retrieval foundation（P0，需要asset snapshot/admin）

- [ ] **T1B.1 [Admin/Data Gate]** 取得與目前UE Content revision一致的完整asset snapshot；若只有partial index，先決定是否重建及成本/時間上限。
- [x] **T1B.2** 實作snapshot manifest與只讀audit：catalog/Postgres/Qdrant count、checksums、vectors、embedding version一致。
- [ ] **T1B.3 [Admin]** Provision固定版本PostgreSQL、Qdrant與embedding service；persistent volumes、healthcheck、restart、backup/restore齊備。
- [ ] **T1B.4 [State-change Gate]** 若無snapshot，依序套schema、catalog migration、Qdrant build；先dry-run/count，核准後才執行full job。
- [x] **T1B.5** 統一local compose、start script、systemd/AWS env與DB/collection names；加入embed service與catalog mount。
- [x] **T1B.6** 移除DSN echo、固定弱密碼、`qdrant:latest`及不存在的`/data/siddhant/...` default；改用secret-backed設定。
- [x] **T1B.7** 修正`/api/chat`的`_amRaw` gate，使documented default `hybrid`真正生效。
- [x] **T1B.8** 為Qdrant/embed/Postgres加bounded timeout與structured causes；保留成功fallback telemetry。
- [x] **T1B.9** 實作`require_real_assets` fail-closed與顯式degraded policy；UI/artifact顯示retrieval revision及fallback。
- [x] **T1B.10** 實作revision-aware cache invalidation與readiness probes。
- [x] **T1B.11** Unit/contract tests涵蓋default mode、每個dependency outage、中文/英文query、snapshot mismatch。
- [ ] **T1B.12** 在disposable UE scene spawn一個Blueprint與一個StaticMesh，檢查exact path、dimensions、material slots與PBR rendering。

驗收：全部服務健康時不再產生whitebox fallback；全掛時build在UE mutation前被阻止。

Implementation note（2026-07-21）：T1B.2 與 T1B.5～11 的 code-only contract 已完成。Local/AWS profiles 共用 pinned-image、loopback、file-secret、catalog/model mount 與 embedding service contract；offline preflight、model artifact manifest、backup bundle及 live-audit receipt 都 fail closed。`/health/ready` 在沒有正式 `simworld-asset-snapshot/v1`、一致 counts 與已驗證 revision 時仍明確 not-ready。T1B.1／3／4／12 受 Data/Admin/State-change Gate 約束：目前未取得 authoritative full catalog、未啟動 DB/index、未執行 live audit、未修改 UE scene。

## Phase 2 — VISTA importer與SceneSpec（依賴Phase 1B contract）

- [x] **T2.1** 定義JSON Schema：`vista-simworld-scene/v1`、source profile、unresolved mapping、privilege labels。
- [x] **T2.2** 建立最小fixture bundle，以`mmg_040`的manifest/no-oracle row與verified`render_script.yaml`為golden case；fixture去除signed URLs/secrets與大型media。
- [x] **T2.3** 實作allowlisted source adapter：dataset revision + sample id + attempt；拒絕任意server file path與backup目錄。
- [x] **T2.4** 實作identity/checksum/duration/timestamp/dialogue/media join validator與validation report。
- [x] **T2.5** 實作deterministic normalizer：environment、camera、entities、relations、timeline及source pointers。
- [x] **T2.6** 實作privilege boundary tests：reconstruction-only fields不得流入evaluation-safe export。
- [x] **T2.7** 實作asset resolver，保存query/candidates/confidence/override；無match留unresolved，不用Cube。
- [x] **T2.8** 實作preview/commit/status APIs、idempotency key、owner/session與artifact persistence。
- [x] **T2.9** 增加UI preview：source、12秒beats、asset bindings、unsupported actions與commit確認。
- [x] **T2.10** Golden/negative tests：missing render script、duration mismatch、duplicate timestamps、invalid attempt、oracle leakage與rerun idempotency。
- [x] **T2.11** 建立 verified-source staging adapter：explicit sample/attempt、逐檔 checksum、MP4 metadata、no-oracle join、dry-run、atomic private apply與現有 importer 相容性。
- [ ] **T2.12 [Data Gate]** 由 VISTA dataset owner 發佈 authoritative verified projection，並 stage 真實 `mmg_040` selected attempt bundle。

驗收：`mmg_040` preview穩定產出12秒SceneSpec，包含0/2/5/9秒beats；重跑不重複artifact。

Implementation note（2026-07-21）：Phase 2 的 code-only contract、API、artifact persistence、專業 workbench UI、golden/negative tests與 fake semantic resolver E2E已完成。新增 staging adapter 會對管理員明確指定的 verified row、render script、no-oracle dialogue 與 MP4 做 exact identity/checksum/bytes/duration/dimensions 驗證，預設 dry-run，`--apply` 才以 0700/0600 atomic/idempotent bundle 落盤；oracle/review/seed/visible-evidence 欄位會被拒絕。尚未讀取 canonical/NAS 或建立 live bundle。沒有 asset snapshot/DB 時 entity 仍明確維持 `no_asset_match`，不使用 Cube。

## Phase 3 — Timeline compiler、animations與runtime（依賴Phase 2）

- [x] **T3.1** 定義`vista-timeline/v1`與`vista-timeline-run/v1` schema、strict/lenient policy與event lifecycle。
- [x] **T3.2** 實作timestamp parser、entity binder與capability checker；所有unsupported actions在Start前列出。
- [x] **T3.3** 建立fixed action adapter interface：precondition、execute、completion、timeout、cancel、cleanup。
- [x] **T3.4** 將現有`agent_action` registry接入adapter層並補unit tests；不要讓LLM自由組合未驗證`vbp`命令。
- [ ] **T3.5** 與UE content owner完成第一批必要Blueprint/montage functions。若`mmg_040`為P0，至少包含drag chair、brace、lift-foot/hesitate及look-at。
- [x] **T3.6** 將Start從browser硬編碼toolbar click搬到backend/UE runtime bridge；保留lease、nonce、state reconciliation與idempotent Stop。
- [x] **T3.7** 實作server monotonic scheduler、drift measurement、UE engine-time sampling與bounded queue。
- [x] **T3.8** 實作Stop/Replay：清pending events、adapter cleanup、停止角色、結束PIE、確認state。
- [x] **T3.9** 建立timeline UI：preflight、elapsed time、event status、drift、Stop與artifact link。
- [ ] **T3.10** Disposable UE integration：正常完成、event timeout、stream disconnect、Stop race、server restart/reconcile。
- [ ] **T3.11** `mmg_040` 0/2/5/9/12秒keyframe/state validation與visual evidence。

驗收：browser不是clock authority；所有event可追溯，Stop後無PIE/pending action殘留。

Implementation note（2026-07-21）：Browser toolbar coordinates、synthetic Escape 與 iframe Play/Stop 已移除。Lease-bound backend Start/state/Stop、PIE/possession gate、live mesh/material/content receipt revalidation、monotonic scheduler、Stop/Replay、cleanup quarantine、restart recovery-required state及 timeline workbench 已完成離線驗證。`ended_pie` 只在 backend state 明確確認後記錄。Legacy `agent_action` 目前只提供 `humanoid.stop_action → pause` candidate；仍需 pinned profile、dedicated driver、live `hold_pose` capability與 completion signal 才會註冊成 executable adapter，絕不 fallback 到 `vbp`/Python。T3.5／10／11仍需真實 UE：目前 plugin 只有 abstract content driver，沒有 skeleton/AnimBP/Control Rig、drag/brace/lift-foot/fall/recover montage、notify/contact proof，也尚未在 disposable scene 驗證 0／2／5／9／12 秒 keyframes。

## Phase 4 — Public WebRTC + Coturn（可先做程式碼，開網需Admin Gate）

- [ ] **T4.1 [Admin Decision]** 確認正式Studio/TURN DNS、目前80/443 ingress owner、TLS與certificate renewal；不可另起Nginx搶已使用ports。
- [x] **T4.2** 實作`loopback`/`trusted_proxy`profiles、固定`STUDIO_PUBLIC_ORIGIN`、trusted proxy validation、public CSP與Secure cookie。
- [x] **T4.3** 建立session-bound opaque streaming endpoint；移除Host-derived URL、port scan與production raw port response。
- [x] **T4.4** 修正player支援same-origin WSS path，維持任意external signalling URL rejection。
- [x] **T4.5** 將session/slot identity完整保存在server-side；viewport/signalling/input均做cross-slot authorization。
- [x] **T4.6** 共用Cirrus config builder：loopback bind、正確HttpPort/StreamerPort、access control與secret-backed`peerConnectionOptions.iceServers`。
- [x] **T4.7** 修正ingress WebSocket proxy到Cirrus HttpPort；StreamerPort/SFU/MCP保持外部不可達。
- [ ] **T4.8 [Admin]** 安裝/營運Coturn，設定external IP、realm、REST/HMAC或secret credential、quota、relay range與監控。
- [ ] **T4.9 [Admin]** 開放經核准的HTTPS/WSS、TURN與受控relay ports，建立ACL/NAT規則。
- [x] **T4.10** 增加Cirrus/streamer/WSS/ICE/TURN readiness及selected candidate telemetry。
- [ ] **T4.11** E2E：unauthenticated/cross-slot denial、WSS 101、decoded frames、data channel control、reconnect、credential rotation。
- [ ] **T4.12 [External Test]** 從至少兩個外部網路執行normal ICE與forced relay；確認不需SSH/Xpra且DevTools無loopback/raw port/mixed content。

驗收：RTC-001～009通過；Xpra只保留admin recovery。

Implementation note（2026-07-21）：T4.2～7已接入production source。`/api/pixel-streaming-url`只回傳session/slot/lease-bound opaque path；browser bearer已由`sessionStorage`移到Secure/HttpOnly cookie，Node upgrade gateway驗證Origin/Host/session後只代理到loopback Cirrus `HttpPort`。Player、frontend、per-session port router與Nginx均不再接受raw Cirrus port；Cirrus launcher以原子config builder注入短效Coturn REST/HMAC credential、forced-relay policy與loopback listeners。T4.10 的程式能力已完成：production readiness 必須驗證並綁定 external receipt，browser 同時以嚴格、租約綁定、去識別化 schema 回報 WSS/ICE、decoded frames、data channel 及 selected candidate type/protocol/TURN transport，並拒絕 SDP、IP、port、credential 等欄位。Browser telemetry 僅供營運觀測，不取代簽署的 external readiness receipt。TURN credential TTL 也必須覆蓋 hard session max 加 reconnect grace；rotation contract 有離線驗證。T4.1／8／9仍需要管理員DNS/TLS/Coturn/firewall決策；T4.11～12仍需真Cirrus/UE、外部瀏覽器、live rotation 與 forced-relay evidence，因此尚未宣稱公開WebRTC ready，也未開任何public port。

## Phase 5 — Persistence、operations與release gate

- [ ] **T5.1** 建立scene/import/review/timeline artifact revisions、owner ACL、retention與cleanup。
- [ ] **T5.2** 在可寫且受控的Production workspace驗證Save/restore；active `-NOWRITE` demo sandbox不可當持久化測試。
- [ ] **T5.3** 建立backup/restore與rollback drill：asset snapshot、artifact metadata、TURN secret rotation、service config。
- [ ] **T5.4** 建立dashboards/alerts：duplicate stacks、dependency readiness、retrieval fallback、review errors/cost、ICE relay failure、timeline drift。
- [ ] **T5.5** 對照actual code更新README、AWS SOP、port matrix及completion status；刪除已失真的「complete」或「stub」敘述。
- [ ] **T5.6** Security review：prompt injection、arbitrary path/URL、secret redaction、session isolation、UE command allowlist、rate/cost limits。
- [ ] **T5.7** Release checklist與rollback plan經user/admin sign-off後，才標示Production-ready。

## 後續但不納入本次五項的產品缺口

以下舊文件與現行程式碼互相矛盾，需另開bounded audit，不能直接宣稱已完成或未完成：

- Task Generation：舊文件說UI stub，但目前已有`task-gen.js`與routes；需驗證實際NavMesh/episode/API/UI E2E及persistence。
- Agent Training：repo已有training/datahub相關程式，但RL framework、RGB-D observation、metrics真實性與replay需重新驗證。
- Co-evolution：UI與部分模組存在，curriculum round lifecycle/mastery/difficulty adaptation是否真實連通仍未證明。
- Artifact chain：UI存在，但跨refresh/restart的durable lineage仍未證明。
- Scene save/load：active demo以`-NOWRITE`啟動；Production persistence與content revision compatibility仍未證明。

建議在Phase 5前逐一建立相同的evidence-based readiness matrix。

## 2026-07-14 離線驗證紀錄

- Relevant Node contract suite：230 passed、0 failed、0 skipped。
- Existing server unit suite：11 passed、18 integration/pipeline tests依設計skip。
- Review/VISTA UI model tests：11 passed。
- Chromium mock E2E：Review 1 passed；VISTA Import 1 passed；沒有呼叫live Studio、UE或model provider。
- Vite development build：1879 modules transformed。
- JSON Schema Draft 2020-12：6個schema通過meta-schema檢查；2個sanitized golden fixture通過instance validation。
- 本輪未restart live server、未連線真實asset DB、未呼叫provider、未mutate UE、未啟公網listener、未stage或commit。

## 每個change set的最低驗證

- Targeted unit/contract tests。
- `cd simworld_studio_workspace/web && npx vite build --mode development`。
- `git diff --check`。
- 不新增emoji UI文字或未tokenized hardcoded colors。
- 若碰UE：使用disposable scene並保存before/after state；不得在未備份live scene直接測試。
- 若碰model/DB/network：先取得對應cost/state/admin核准。

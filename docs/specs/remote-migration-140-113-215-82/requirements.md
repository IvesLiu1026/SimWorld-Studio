# Requirements: SimWorld migration to 140.113.215.82

Status: Approved for staged migration and handoff  
Updated: 2026-07-15  
Requested and approved by: user request in this Codex task

## Problem

目前可工作的 SimWorld 並不是單一 Git checkout。它由 dirty 的 SimWorld Studio source、另一個 dirty 的 Python/UE bridge repo、21 GiB packaged Unreal runtime、run-local evidence、使用者登入/secret，以及尚未建好的 asset services組成。只在目標主機執行 `git clone` 或 `git pull` 會遺失目前主要進度，也無法啟動 Unreal。

目標主機 `yhliu@140.113.215.82` 有雙 RTX 5090、125 GiB RAM與約670 GiB可用空間，但盤點時沒有 SimWorld、Unreal runtime、asset snapshot、Postgres、Qdrant或embedding service；Docker CLI存在但使用者無daemon權限。

## Goals

- 將目前兩個 dirty source trees逐位元保留到目標主機，包含已追蹤修改與必要untracked source。
- 將可執行的 canonical packaged Unreal runtime archive搬到目標主機並以checksum驗證。
- 保存足以理解目前VISTA/Opus場景成果的sanitized evidence與production-readiness規格。
- 先達成loopback、model-off、無外部DB的可重現bring-up，再逐項打開model、assets與public WebRTC。
- 產生一份遠端Codex GPT-5.6 Sol Ultra可以直接接手的runbook、task list與known-gaps handoff。

## Non-goals

- 本次migration不代表Production deploy。
- 本次不更動目標主機既有80/443/14500 listener、ingress、TLS或防火牆。
- 不在未核准下安裝system packages、修改群組、使用sudo、啟動Coturn或建立public listener。
- 不傳輸Claude/Codex登入狀態、API keys、Studio access token、TURN secret、database password、SSH keys或`.env`。
- 不聲稱目前live UE scene可持久化；原始run使用`-NOWRITE`，scene只存在於原process memory。
- 不在migration階段執行付費model call、full asset indexing或VISTA generation/evaluation。

## Assumptions

- SSH identity `yhliu@140.113.215.82`持續可用。
- 目標主機的`/home/yhliu/SimWorld-Studio-src`與`/home/yhliu/SimWorld`在migration開始時不存在。
- Canonical runtime archive保持：15,170,703,068 bytes，SHA-256 `806e869ad1c65b298f05a39854b28e4188bb50817f539744451849e054990e2f`。
- 目標主機使用`/home/yhliu/.local/bin/node` v22；system Node v18只作fallback。
- Source tree中的dirty layer是migration source of truth；GitHub不是目前完整狀態。

## Requirements

### MIG-001 — Preserve dirty source provenance

WHEN Studio source被傳輸 THEN system SHALL保留branch `codex/vista-loopback`、base HEAD `caf6d9309ad4fe256a6ba1e212d8bb1fb1fa7f7b`、全部tracked modifications與必要untracked source，且remote `git status --porcelain=v2`與migration snapshot相符。Migration spec加入後的snapshot包含48個tracked modified與69個untracked files。

WHEN Python/bridge repo被傳輸 THEN system SHALL保留`main` checkout、5個tracked modifications與必要untracked `docs/`、`experiments/`、`scripts/`、`ue_plugin/`。

### MIG-002 — Never overwrite an unknown remote tree

IF canonical target path已存在 THEN migration SHALL停止並要求人工比對，不得使用`--delete`、強制覆寫或先清空目錄。

WHEN傳輸開始 THEN每個unit SHALL先寫入唯一`.partial-<migration-id>`路徑，驗證後才rename到canonical path。

### MIG-003 — Transfer the complete runnable units

WHEN migration完成 THEN target SHALL至少具有：

1. `/home/yhliu/SimWorld-Studio-src`
2. `/home/yhliu/SimWorld`，base HEAD `0921180909105158a7ff87445eb032706b10113e`加dirty layer
3. `/home/yhliu/.local/share/simworld-studio/downloads/SimWorld-Studio-Minimal-806e869a.tar.gz`
4. `/home/yhliu/.local/share/simworld-studio/binary/SimWorld-Studio-Minimal-806e869a`
5. `/home/yhliu/.local/share/simworld-studio/evidence/20260714T120039-simworld-opus`
6. `/home/yhliu/.local/state/simworld-studio`與`/home/yhliu/.config/simworld-studio`的empty operator-owned roots。

### MIG-004 — Rebuild disposable dependencies

WHEN source被同步 THEN `node_modules/`、Python `.venv/`、`dist/`、build caches、DDC、temporary screenshots、runtime logs與test reports SHALL預設不從source checkout傳輸，並由lockfile或明確runbook重建。

Canonical packaged runtime archive不受上述cache排除規則影響；其內容不得以source exclude policy裁切。

### MIG-005 — Secret separation

WHEN files被同步 THEN migration SHALL排除`.env*`、local Claude/Codex/Hermes settings、credentials、private keys、access tokens、run-local MCP configs與raw provider logs。

WHEN remote stack第一次啟動 THEN `STUDIO_ACCESS_TOKEN`及其他runtime secrets SHALL在遠端重新產生，使用mode 0600的operator-controlled storage或service secret manager，且不得寫入Git。

### MIG-006 — Integrity and evidence

WHEN任一transfer unit完成 THEN migration SHALL保存size、source/target checksum或`rsync --checksum --dry-run`零差異證據。

IF archive checksum或source parity不符 THEN extraction/activation SHALL停止；partial資料保留供診斷，不得宣告成功。

### MIG-007 — Safe runtime activation

WHEN首次bring-up THEN UE、Cirrus與Studio SHALL只bind loopback，使用未占用ports，model mode為`off`，asset policy為fail-closed，並且不得接管80/443/14500。

WHENUE啟動 THEN它 SHALL使用writable isolated state/cache、`-NOWRITE`或等價保護、`-RenderOffScreen`與明確GPU；source/project content不得在smoke test中被覆寫。

### MIG-008 — Dependency and readiness truthfulness

IF Postgres/Qdrant/embed/snapshot不存在 THEN `/health/ready` SHALL明確回報asset retrieval not-ready，importer SHALL保留unresolved assets，不得回退成Cube或宣稱PBR production-ready。

IF real Text/Visual review尚未經核准與smoke THEN UI/runtime SHALL保持Review Off或fake-provider only。

### MIG-009 — Reproduce current VISTA functionality before expansion

WHENoffline validation執行 THEN target SHALL通過既有Node contract tests、UI unit tests、Vite build、mock browser E2E與`mmg_040` importer/timeline golden tests，且不呼叫UE、provider或external DB。

WHENUE smoke獲得核准 THEN target SHALL先驗證MCP、Pixel Streaming、read-only screenshot與model-off VISTA setup/stop，再考慮場景重建或model call。

### MIG-010 — Explicit admin gates

IF需要Docker asset services、Vulkan diagnostics、public WebRTC或system service THEN handoff SHALL列出管理員需求，且未取得核准前不得自行使用sudo或繞過既有ingress。

### MIG-011 — Remote Codex handoff

WHENsource與archive完成傳輸 THEN repository SHALL包含`requirements.md`、`design.md`、`tasks.md`、`runbook.md`與`HANDOFF.md`，明確區分safe/offline、stateful、cost、admin及public-network commands。

### MIG-012 — Rollback and local preservation

WHENremote驗證失敗 THEN rollback SHALL只停止本migration建立的process並rename/remove本migration的target paths；不得影響原主機live stack或目標主機既有服務。

Local source、runtime archive與live stack SHALL保持不變，直到remote acceptance明確完成。

## Edge Cases

- SSH/rsync中斷：使用partial path與rsync resume；不要重新建立canonical path。
- Remote path在transfer期間被其他agent建立：停止promotion並比對ownership。
- Node解析到system v18：先修正`PATH=$HOME/.local/bin:$PATH`，再安裝依賴。
- Archive空間不足：在extract前要求至少50 GiB可用；目前盤點約670 GiB。
- Docker權限不足：asset services保持not-ready；不得把`sudo docker`寫進自動runbook。
- Vulkan smoke失敗：保存log並要求`vulkan-tools`/render-group管理員檢查，不切換到不受控software rendering。
- 80/443/14500已使用：仍以loopback 3002/55559/8585/8586 bring-up，不另起public proxy。
- Existing source hardcoded local paths：以remote env/生成的runtime config覆蓋，不直接把local MCP config當secret/config source。

## Approval

本文件將使用者「把SimWorld整個先移植到`yhliu@140.113.215.82`，若環境不好搬則留下完整spec給遠端Codex接手」視為staged transfer與handoff的明確核准。付費model、sudo/admin、public ingress、database migration及UE scene mutation仍各自需要其原有gate。

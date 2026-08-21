# Requirements: VISTA World Repository Extraction

Status: Draft
Updated: 2026-08-21

## Problem

VISTA Playable Home 的 Unreal 世界、world contracts、Blender/UE pipeline 與
Sunshine runtime 已具有獨立產品價值，但目前仍與 `SimWorld-Studio` fork 共用
repository。這使產品邊界、上游同步、授權、發布與多代理 ownership 變得模糊；
同時 GitHub 不會把 fork 內的 commits 視為個人 contribution graph 的合格
standalone repository commits。

## Goals

- 建立一個非 fork 的 canonical `VISTA-World` repository。
- 保留現有 accepted package、receipt、測試、Git authorship 與可重現性。
- 讓 VISTA World 核心可在沒有 SimWorld Studio server/UI 的情況下建置與執行。
- 將 SimWorld Studio 降為可替換的 NLP/control-plane adapter。
- 建立每天一個可驗證成果、當天合併 default branch 的工作節奏。

## Non-goals

- 本階段不刪除或改寫現有 `IvesLiu1026/SimWorld-Studio` fork。
- 本階段不搬移 NAS artifacts、UE engine、付費資產或 credentials。
- 不為 contribution graph 製造 empty、timestamp、無意義 README 或回填日期 commit。
- 不立即重寫所有 `simworld.vista.*` v1 schema IDs。
- 不在未核准前部署公開服務或啟動 GPU rebuild。

## Assumptions

- 預設 repository 名稱為 `IvesLiu1026/VISTA-World`，default branch 為 `main`。
- 初期採單一 monorepo，避免多語言 pipeline 在尚未穩定前被過早拆散。
- `IvesLiu1026/SimWorld-Studio` 保留為相容性、上游歷史與 incubator repository。
- 大型資產與 build artifacts 留在外部儲存，Git 只保存 manifest、license 與 digest。

## Requirements

### R1. Canonical standalone repository

WHEN migration 開始 THEN 系統 SHALL 使用非 fork 的 `VISTA-World` repository
作為 VISTA World 唯一 canonical product source。

Acceptance notes:
- GitHub metadata SHALL 顯示 `isFork=false`。
- `main` SHALL 是 default branch。
- repository visibility 必須由使用者明確決定。

### R2. Reproducible history and attribution

WHEN VISTA-owned source 被抽離 THEN migration SHALL 保留可追溯的 commit authorship、
Apache-2.0/第三方 attribution、accepted receipt SHA 與來源 commit pins。

Acceptance notes:
- 不得把現有 accepted evidence 誤寫成新 repo 產生。
- 舊 NAS 路徑可以作歷史證據，但新 release 不得依賴可變動的 worktree path。

### R3. Standalone VISTA World core

WHEN 使用者建置或啟動 packaged VISTA World THEN 系統 SHALL 不要求 SimWorld
Studio web server、Studio UI、Postgres/Qdrant 或 SimWorld submodule 存在。

Acceptance notes:
- Unreal plugin 只依賴明列的 UE modules。
- pure compiler、world packs、Blender/UE pipelines 與 packaged runtime 必須有
  standalone validation。

### R4. Optional adapters

WHEN VISTA World 連接 SimWorld Studio、NLP agent、Pixel Streaming 或其他控制面
THEN integration SHALL 經由版本化 adapter/transport interface，而不是反向污染
核心 world contracts 或 gameplay plugin。

Acceptance notes:
- `vista_world_action` 必須有 standalone transport contract。
- SimWorld 的 `UeMcpBroker` SHALL 成為其中一個 adapter，而非唯一 runtime。

### R5. Versioned deliverables

WHEN `main` 產生 release THEN repository SHALL 可分別產生 contracts/compiler、
Unreal plugin、world pack 與 game/runtime launcher 的版本化 deliverables。

Acceptance notes:
- release metadata 必須包含 source SHA、schema version、asset manifests 與 licenses。
- binaries、secrets、UE engine 與未授權資產不得進 Git history。

### R6. Daily meaningful integration

WHEN 每日開發結束 THEN 至少一個有 issue/acceptance criterion、通過 validation 的
logical change SHOULD 在當天經 PR 合併到 standalone repository 的 `main`。

Acceptance notes:
- automation 可以在已核准的 daily-maintainer spec、allowlist 與 PR/CI gate 內完成
  低風險工作，但不得自動製造、回填或 push 假 commit。
- 自動 commit 必須透明標記 automation identity；若要歸屬 `IvesLiu1026`，author/
  committer/trailer 規則必須由使用者明確核准。
- 若沒有通過 DoD 的變更，應記錄 blocker，而不是犧牲 history 品質。

### R7. Multi-agent isolation

WHEN 多個 agents 同時工作 THEN 每個 writing worker SHALL 使用獨立 short-lived
branch/worktree，並記錄 owned paths、runtime ownership、validation 與 handoff。

Acceptance notes:
- 只有 integrator 可以合併 `main`。
- GPU、Sunshine、Pixel Streaming 與 generated evidence 在同一時間只能有一位 owner。

### R8. Compatibility and rollback

WHEN 新 repo 尚未完成某項控制面能力 THEN 系統 SHALL 可透過 pinned SimWorld
adapter 使用既有能力，且能回退到目前 accepted package/source commit。

Acceptance notes:
- v1 `simworld.vista.*` IDs 保持可讀；新 v2 namespace 由 adapter 明確轉換。
- fork 不得在 migration acceptance 前被刪除或 force-rewrite。

## Edge Cases

- GitHub CLI 登入帳號與 repository owner 不一致時，建立與發布必須 fail closed。
- 未審查 local branches、WIP/quarantine commits 不得批次灌入新 `main`。
- HSSD 等非商業授權資產不得被包裝成商業可發布內容。
- 同一天只有 feature branch commit、但未進 default branch時，不得宣稱 daily
  GitHub contribution gate 已通過。

## Open Questions

- `VISTA-World` 要 public 還是 private？建議 public；若 private，使用者需啟用
  GitHub private contribution 顯示。
- 初期 owner 使用 `IvesLiu1026`，還是先建立 VISTA organization？建議先使用
  `IvesLiu1026`，產品穩定後再 transfer。
- 第一次 extraction 是否保留 selected-path history（建議），或採單一 import commit？

## Approval

- Requested by: Codex integrator
- Approved by:
- Date:

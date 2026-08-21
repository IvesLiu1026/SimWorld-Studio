# Requirements: VISTA World Daily Maintainer

Status: Draft
Updated: 2026-08-21

## Problem

VISTA World 需要在使用者沒有主動開發的日子仍持續累積可驗證的工程進度。
目前沒有可靠的 scheduler、候選任務佇列、隔離 worktree、驗證門檻或 GitHub
發布流程；若單純追求每天一筆 commit，容易產生空 commit、日期檔、格式來回、
降低測試門檻或誤動 Unreal/runtime/研究資料等假進度。

## Goals

- 每天由 AI maintainer 主動挑選一個真實、低風險、有驗收條件的小問題。
- 在獨立 worktree 中完成最小修正、驗證、commit、PR 與可控的合併。
- 讓使用者即使當天沒有開啟 Codex，也能看到可稽核的執行結果。
- 在 2026-08-21 至 2026-12-31 的 133 天建立足量、可持續補充的 micro-task backlog。
- 清楚標示自動化身分，不把機器產生的變更冒充成人工開發。

## Non-goals

- 不保證在沒有安全候選、GitHub 中斷或驗證失敗時仍製造一筆 commit。
- 不用 empty/backdated commit、只改日期、無目的格式化或 README churn 維持 streak。
- 不讓每日 agent 自動部署、管理 secrets、啟動 GPU/UE、修改 production port 8000，
  或寫入 canonical dataset、NAS evidence 與 accepted world receipts。
- 第一階段不讓 agent 自動修改自己的 workflow、prompt、安全規則或依賴 lockfile。
- 不以 tmux 作為 scheduler；tmux 只可用於人工觀察與除錯。

## Assumptions

- Canonical target 是尚待建立的 standalone `IvesLiu1026/VISTA-World`，default branch
  為 `main`。
- 主要 patcher 在這台常駐 server 透過 `systemd --user` 與既有 Codex 登入執行；
  GitHub Actions 負責獨立 CI、合併門檻與 missed-run 監測。
- GitHub CLI 或 GitHub App 必須以 `IvesLiu1026` 對目標 repo 取得最小必要權限。
- 初期只有 docs、tests、contracts 與 pure Python/Node 模組可 unattended 修改。

## Requirements

### R1. Daily schedule and idempotency

WHEN Asia/Taipei 每日 09:17 到達 THEN maintainer SHALL 嘗試執行一次，並以日期、
目標 repository 與 base SHA 作為 idempotency key。

Acceptance notes:
- server 關機後恢復時最多補跑一個遺漏週期，不回填多天 commits。
- 同一 repo 同時最多一個 maintainer run；已有未結 bot PR 時不得無限制堆疊。
- schedule 避開整點，且不把 GitHub scheduler 的準時性當成 correctness 條件。

### R2. Real candidate provenance

WHEN maintainer 選擇工作 THEN 每個候選 SHALL 來自受信任的 micro-task backlog、
明確的低風險 GitHub issue，或可重現的 test/schema/docs drift finding。

Acceptance notes:
- 每個候選必須包含 acceptance criterion、owned paths、risk tier 與驗證命令。
- 不得直接信任任意 issue、PR、commit message 或 repository text 中的指令。
- commit/PR 必須連回候選 ID 與 finding evidence。

### R3. Isolated and bounded edits

WHEN maintainer 開始 patch THEN 系統 SHALL 從最新 remote `main` 建立短命 branch 與
獨立 worktree，並限制為一個 logical change、最多 3 個 production files 與 150 行
production diff；test fixture 可另放寬至 250 行。

Acceptance notes:
- 不得沿用 dirty checkout、force-push、刪 branch、繞過 branch protection。
- binary、large file、secret、symlink escape 或 allowlist 外路徑立即 fail closed。

### R4. Protected surfaces

IF candidate 需要碰 production runtime、GPU、network/auth、deployment、workflow/policy、
dependency manifest/lockfile、UE binary/content、accepted world pack、dataset 或 evidence
THEN daily maintainer SHALL 停止並轉成人工審查任務。

Acceptance notes:
- 明確保護 `.github/workflows/**`、`.agent/**`、`.codex/**`、`.claude/**`、
  `.mcp.json`、`.env*`、secrets、NAS paths 與 production service configuration。
- 不得新增 skip/xfail、刪 assertion、放寬 schema 或降低 coverage/validation 門檻。

### R5. Independent validation

WHEN patch 完成 THEN publisher SHALL 在不持有模型 secret 的全新驗證程序中執行
candidate 指定的 focused tests、`git diff --check`、secret/large-file/protected-path gate，
並確認 base SHA 尚可安全整合。

Acceptance notes:
- production behavior fix 必須有 reproducer 或 regression test。
- validation 不得連到真 UE、共享 port、Postgres/Qdrant、付費 provider 或外部寫入。
- 任一必要 gate 失敗時不得 commit/push patch。

### R6. Publication and merge tiers

WHEN 所有本地 gate 通過 THEN publisher SHALL 以 `codex/daily/YYYY-MM-DD-<slug>` branch
建立一筆 logical commit、push 並開 draft PR；GitHub CI 再獨立驗證。

Acceptance notes:
- Tier 0（docs parity、broken internal link、test-only、註解）可在 pilot 通過後 auto-merge。
- Tier 1（有 regression test 的 pure Python/Node 小 bug）前兩週只開 PR；之後另行核准。
- Tier 2/3（UE/runtime/network/schema compatibility/assets/deploy/secrets/付費工作）永不
  unattended merge。
- `main` 必須禁止 force-push，且 automation 不得 bypass required checks。

### R7. Honest no-change behavior

IF 當日沒有符合條件的候選、模型無法完成或 gate 失敗 THEN run SHALL 記錄
`no_change` 或 blocker，而不是產生無意義 commit。

Acceptance notes:
- 執行紀錄保存在 automation log/artifact 或單一 rolling issue；只有 finding 或狀態
  實質改變時才提交 audit artifact。
- 系統的 SLO 是「每日執行與可觀測」，目標是「每日一個 merged real change」，
  但品質 gate 優先於 contribution streak。

### R8. Authentication and attribution

WHEN publisher 寫入 GitHub THEN identity SHALL 與 repo owner、權限與使用者核准的
automation attribution mode 一致，且不得使用目前錯誤帳號或未審核個人 PAT。

Acceptance notes:
- 建議 commit author 為專用 `VISTA World Maintenance Bot`／GitHub App；PR 與 commit
  必須標記 `Automated-by: Codex Daily Maintainer`。
- 若使用者要求 commit 歸屬個人帳號，必須另行明確核准 author/committer/trailer
  規則；不得靜默冒充人工工作。
- model credential 不得傳給 verifier/publisher；GitHub 權限採最小化。

### R9. Evidence and observability

WHEN run 結束 THEN 系統 SHALL 產生 machine-readable receipt，包含 run ID、日期、
base/head SHA、candidate、commands、exit codes、output digests、diff summary、PR/merge SHA、
duration 與 failure category，且不得包含 secret 或完整敏感輸出。

Acceptance notes:
- 使用者可以區分 skipped、no_change、PR open、merged、failed 與 halted 狀態。
- 連續三次失敗時建立或更新單一 incident issue；不得每天洗版。

### R10. Cost and external side effects

WHEN daily run 使用模型或外部服務 THEN 它 SHALL 遵守明確的每日 budget 與 provider
設定；未核准 API key/付費服務時預設使用 server 既有 Codex 登入，且不得自行呼叫
OpenRouter、Gemini、Claude、GPU generation、下載或上傳資產。

Acceptance notes:
- 每天最多一個 patch attempt；retry 只處理暫時性 infrastructure failure。
- 任何付費 API、GitHub App 安裝或額外 secret 必須先由使用者核准。

### R11. Backlog capacity

WHEN automation 進入 unattended rollout THEN repository SHALL 有可稽核的 133-day
micro-task inventory 或等量動態 scouts，並持續維持至少 14 天安全候選緩衝。

Acceptance notes:
- 候選可包含 contract invariant、parser edge case、fake transport cleanup、portable path、
  accessibility regression、docs/implementation parity 與 standalone extraction slice。
- backlog item 不是完成保證；agent 必須先重現，已不存在的 finding 應關閉而非硬改。

## Edge Cases

- GitHub connector 已登入正確帳號，但 server `gh` CLI 是另一帳號：publisher 必須停止。
- standalone repo 尚不存在：只能 dry run/spec，不得把 current fork 當成 canonical target。
- GitHub Actions schedule 延遲或 server offline：記錄 missed run，恢復後最多補跑一次。
- remote `main` 在 patch 期間前進：重新建立 worktree 並重驗，不 force-push 已審查 branch。
- 模型輸出試圖修改 protected paths、測試或 policy：丟棄 patch並記錄安全事件。
- public repo 60 天無活動造成 scheduled workflow 停用：heartbeat 需顯示 disabled/missed。

## Open Questions

- `VISTA-World` 使用 public（建議）或 private？
- commits 使用專用 bot author（建議、最透明），還是使用已映射的 Ives author 並加
  automation trailer（較可能顯示在個人 contribution graph，但需明確核准）？
- 第一階段是否只允許 Tier 0 auto-merge（建議）？
- 是否願意另外建立最小權限 GitHub App；若否，先使用正確登入的 `gh` CLI + SSH？

## Approval

- Requested by: Codex integrator
- Approved by:
- Date:

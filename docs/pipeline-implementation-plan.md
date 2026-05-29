# SimWorld Studio — Pipeline Implementation Plan (Task Gen · Training · Co-evolve)

> Status: **planning doc** (audit + architecture). The Scene-generation stage is fully
> live; the Task → Training → Co-evolve stages need the work below. Author: audit on
> 2026-05-29.

## 1. Current state — the core finding

For every stage past **Scene**, the **compute engine already exists and runs via CLI**, but
there is **no web API connecting it**, the **UI panels are mock shells**, and the **data layer
is fragmented** (each stage's data is filesystem-only with no lineage). The work is therefore
an **orchestration + data layer + UI-wiring** problem, *not* a "build the ML from scratch" one.

| Stage | Engine (exists, runs via CLI) | Web API | UI |
|---|---|---|---|
| Scene | scene-gen agent + MCP | ✅ `/api/chat`, `/api/scenes`, checkpoints, saved-maps | ✅ live |
| **Task Gen** | `nav_task/` (generator, validator, measures, CLI); `datasets/diverse50/*.jsonl` | ❌ none | 🟡 `App.jsx:578-689` shell, mock inspector, no-op buttons |
| **Training** | `gym_env/` (`batch_runner.py` 1.4k LOC, `epoch_runner.py`, `runner.py`, memory, env); `results/` (~62 runs) | ❌ none | 🟡 `App.jsx:692-742` shell, hardcoded metrics |
| **Co-evolve** | `co_evolve/loop.py` (PIE curriculum loop); `evolution.js` `EvolutionManager` (written, never instantiated) | ❌ none | 🟡 `App.jsx:745-838` mock |

### Pipeline disconnects (each seam is manual today)
`Scene → Task` (no generation from a live scene) · `Task → Training` (no UI trigger) ·
`Training → Co-evolve` (results never feed curriculum) · `Co-evolve → Scene` (generated
scenes never re-imported). Metrics are in-memory/ephemeral (`metrics-hub.js`); no historical curves.

## 2. Target architecture — three layers

```
┌─ Frontend panels (wire to APIs, drop mock) ──────────────────────────────┐
│  TaskGenPanel · TrainingConfigPanel · CurriculumBuilderPanel + inspectors │
└───────────────▲──────────────────────────────────────────────────────────┘
                │ REST + SSE
┌─ (A) Orchestration / job layer (NEW, web/server) ─────────────────────────┐
│  JobManager: spawn Python CLI as a tracked job, capture JSONL stdout,      │
│  status (queued/running/done/failed), cancel, SSE progress stream.         │
│  Endpoints: /api/tasks/* · /api/training/* · /api/coevolve/* · /api/evolution/* │
└───────────────▲──────────────────────────────────────────────────────────┘
                │ reads/writes
┌─ (B) Data middle-platform (NEW, 数据中台) ────────────────────────────────┐
│  ArtifactRegistry: scenes → tasksets → runs → policies → generations,      │
│  with lineage (parent ids) + status + metrics. One manager, one store.     │
│  Persists metrics (no longer ephemeral). Connects the pipeline seams.      │
└─ existing engines: nav_task/ · gym_env/ · co_evolve/ · evolution.js ───────┘
```

### (A) Orchestration / job layer
A generic `JobManager` in `web/server/` (mirrors the existing chat/gemini runner pattern):
- `spawn(kind, argv, cwd, env)` → launches a Python CLI (`python -m nav_task …`,
  `python -m gym_env.batch_runner …`, `python -m co_evolve …`) as a child process.
- Streams the child's **stdout JSONL** lines → SSE to the client (reuse `/api/events`-style
  push). The runners already emit per-episode/epoch JSONL + summary JSON — standardize on a
  small progress envelope (`{type, jobId, ...}`).
- Tracks `{jobId, kind, status, startedAt, args, artifactId, exitCode}` in the registry.
- `cancel(jobId)` → SIGTERM the child (mirror `_chatProcs`).
- Run the agent CLIs under the same **bwrap read-only-repo sandbox** (`agent-sandbox.js`)
  where they shell out to coding agents.

### (B) Data middle-platform (the 数据中台)
A single `ArtifactRegistry` (new `web/server/artifacts.js`, JSON store first, SQLite if it
grows) — same shape as `SceneManager`/`CheckpointManager`. Entities + lineage:

```
Scene        { id, name, umapPath, source }                         (← Save As / scenes.js)
TaskSet      { id, sceneId, taskType, params, episodeCount, path }   (← nav_task)
TrainingRun  { id, taskSetId, model, method, config, status,
               metricsSeries[], summary, resultsDir }                (← gym_env)
Policy       { id, runId, artifactPath }                             (← gym_env best ckpt)
Generation   { id, parentRunId, sceneId, difficulty, metrics }       (← co_evolve)
```
- Lineage via parent ids → the pipeline connects: a TaskSet knows its Scene, a Run knows its
  TaskSet, etc. The UI can show the chain and let Stage N pick a Stage N-1 artifact.
- **Persist metrics** here (currently `metrics-hub.js` is RAM-only) so training/eval curves
  survive restarts and feed co-evolve.
- Storage under `simworld_studio_workspace/artifacts/` (or reuse `datasets/` + `results/`
  with an index on top — decision below).

### Frontend wiring
Replace the hardcoded panels with real calls: launch (POST), live progress (SSE), results
(GET from the registry). Remove/▸wire the no-op buttons. Add a lineage/artifact picker so
each panel consumes the previous stage's output.

## 3. Per-module API surface (proposed)

**Task Gen** — wraps `nav_task`:
- `POST /api/tasks/generate` `{ sceneId|roads, taskType, episodes, minPath, maxPath, successRadius, maxSteps }` → `{ jobId }`
- `GET  /api/tasks/:jobId/stream` (SSE progress) · `POST /api/tasks/:id/validate` · `GET /api/tasksets` · `POST /api/tasks/:id/export`
- Needs: extract the **roads/navmesh graph from the live scene** (UCV) so generation can run on a just-built scene, not only static maps.

**Training** — wraps `gym_env.batch_runner`/`epoch_runner`:
- `POST /api/training/start` `{ taskSetId, model, observationMode, method, memory, episodeBudget|epochs, splits, seed }` → `{ jobId }`
- `GET /api/training/:jobId/stream` (SSE: SR/SPL/SoftSPL/nDTW/reward per step/epoch) · `POST /api/training/:jobId/cancel` · `GET /api/runs` · `GET /api/runs/:id`
- Surface the engine's WandB-style metrics through SSE instead of (or alongside) WandB.

**Co-evolve** — wraps `co_evolve.loop`:
- `POST /api/coevolve/start` `{ sceneId, generations, teacher params }` → `{ jobId }`
- `GET /api/coevolve/:jobId/stream` (SSE: round, difficulty, SR) · `GET /api/coevolve/runs`
- Close the loop: write generated scenes back as Scene artifacts (re-importable into Studio).

**Evolution (tools/skills)** — wire the already-written `EvolutionManager`:
- Instantiate it in `index.js`; add `/api/evolution/*` (start, status, sessions, batches).
- Re-enable `reprocess()` (currently hard-disabled `strict_one_pass_mode`, `evolution.js:832`) or expose its mode as a flag.

## 4. Phased rollout (vertical slices — each independently usable)

- **Phase 0 — cleanups (DONE / quick):** ✅ resolved the committed merge conflicts in
  `co_evolve/loop.py` (6 hunks) + `coding_agent.py` (1) — both now compile. Remaining small
  stubs: `nav_task/measures.py` `nDTW`/`coverage` are SoftSPL approximations; `evolution.js`
  `reprocess` disabled.
- **Phase 1 — Data registry skeleton:** `artifacts.js` + schemas + `/api/artifacts` list/get;
  index the existing `datasets/` and `results/` so the UI can *see* what's already there.
- **Phase 2 — Task Gen vertical:** JobManager + `/api/tasks/*` → `nav_task`; roads extraction
  from live scene; wire `TaskGenPanel` (generate → validate → export → real inspector).
- **Phase 3 — Training vertical:** `/api/training/*` → `gym_env`; SSE live metrics; persist
  to registry; wire `TrainingConfigPanel` + a runs/results view.
- **Phase 4 — Co-evolve + Evolution:** `/api/coevolve/*` → `co_evolve`; instantiate
  `EvolutionManager` + `/api/evolution/*`; close Scene↔Co-evolve loop.

Recommended first build after this doc: **Phase 1 + Phase 2** (registry + Task Gen) — highest
leverage, `nav_task` is ready, and it's the first real link after Scene.

## 5. Key decisions to confirm before building
1. **Store**: flat JSON files (fast to ship, matches existing managers) vs SQLite (lineage
   queries, scale). Recommend JSON now, SQLite if/when needed.
2. **Metrics source of truth**: keep WandB and *also* stream to the UI, or make the registry
   the single source. Recommend registry + optional WandB.
3. **Compute placement**: jobs run as local subprocesses on the Studio host (current model) —
   confirm that's acceptable vs a separate worker/queue for heavy training.
4. **Multi-tenant**: tasksets/runs are currently global; decide per-user vs shared (ties into
   the shared-content/permissions discussion).

## 6. Cleanups completed in this pass
- `co_evolve/loop.py` + `co_evolve/coding_agent.py`: removed unresolved `git stash` conflict
  markers (kept the "Updated upstream" / committed-baseline side); both now `py_compile` clean.
  ⚠️ The co_evolve module had been committed broken across `koe`/`dev`/`main` — **re-test it
  end-to-end before relying on it**, since the resolution favored one side mechanically.

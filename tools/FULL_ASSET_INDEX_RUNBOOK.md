# Full Asset Index Runbook

This runbook is for the one-time GPT-5.5 metadata generation pass over the full
SimWorld asset manifest and the incremental Postgres/Qdrant sync that follows
each batch of generated catalog JSONs.

## Current local inputs

- Repo: `/data/siddhant/SimWorld-Studio`
- Asset DB: `/data/siddhant/asset_db`
- Full manifest: `/data/siddhant/asset_db/manifest_full.json`
- Full manifest count as of 2026-06-09: `14,700`
- Existing generated catalog JSONs as of 2026-06-09: `1,209`
- Default VLM provider/model: `codex` / `gpt-5.5`
- Qwen smoke-tested provider/model: `qwen` / `Qwen3.6-35B-A3B`

## What the runner does

`tools/full_asset_index_runner.py run`:

1. Imports the existing catalog into Postgres and upserts it into Qdrant.
2. Skips any asset that already has `catalog/*/<asset_id>.json`.
3. Skips configured helper/non-spawnable asset ids from
   `tools/full_asset_index_skip_helpers.txt`.
4. For each missing, non-skipped asset, spawns it in UE, measures geometry, renders orbit views,
   calls the configured VLM provider, and atomically writes the catalog JSON. The provider can
   be Codex/GPT-5.5 or an OpenAI-compatible Qwen endpoint.
5. Logs per-asset state, failures, Codex usage, and heuristic quality warnings.
6. Periodically syncs newly generated assets to Postgres and Qdrant.
7. Rebuilds `category_index.json` after syncs.

The launcher uses `nohup setsid`, so the run keeps going after SSH disconnects
or the Codex chat is closed. It does not survive a machine reboot.

## Persistence behavior

- Catalog JSONs are written atomically per asset.
- Render images are written under `asset_db/renders/<asset_id>/`. The runner waits for each
  screenshot to have a valid image header, nonzero size, and stable file size before copying it.
- Future render captures force the UE editor viewport into Game View/game show flags before
  `HighResShot`, so screenshots use game-style rendering and hide editor-only icons/overlays.
- Postgres import commits every `DB_COMMIT_EVERY` rows, default `50`.
- Qdrant upserts run with `wait=True`; the builder verifies point existence before skipping
  unchanged embeddings.
- Pending DB sync ids are stored in `pending_db_asset_ids.txt` inside the run dir.
- Pending ids and core state files are written atomically.
- Rerunning with the same `RUN_DIR` resumes status, reconciles current-run catalog JSONs
  back into pending sync, and retries pending DB sync.

## Before launching

Start the required services yourself:

```bash
cd /data/siddhant/SimWorld-Studio
docker compose up -d
```

UE must also be running with the MCP socket reachable on `MCP_PORT`, default
`55571`. The launcher assumes:

```bash
UE_PROJECT=/data/siddhant/simworld_studio_projects
POSTGRES_URL=postgresql://USER:PASSWORD@127.0.0.1:55432/asset_db
QDRANT_URL=http://127.0.0.1:6333
```

## No-service smoke check

This only checks files, manifest selection, schema, and Codex CLI availability:

```bash
cd /data/siddhant/SimWorld-Studio
python3 tools/full_asset_index_runner.py check \
  --asset-db-dir /data/siddhant/asset_db \
  --manifest /data/siddhant/asset_db/manifest_full.json \
  --limit 3 \
  --skip-service-checks \
  --no-db-sync
```

Dry-run the runner without UE, Postgres, or Qdrant:

```bash
python3 tools/full_asset_index_runner.py run \
  --asset-db-dir /data/siddhant/asset_db \
  --manifest /data/siddhant/asset_db/manifest_full.json \
  --limit 3 \
  --dry-run \
  --skip-service-checks \
  --no-db-sync \
  --run-dir /tmp/simworld_full_index_smoke
```

## Live preflight before full launch

After Docker and UE are up, run this without `--skip-service-checks`:

```bash
python3 tools/full_asset_index_runner.py check \
  --asset-db-dir /data/siddhant/asset_db \
  --manifest /data/siddhant/asset_db/manifest_full.json \
  --limit 3
```

Before the long run, confirm FastEmbed can load the dense+sparse models and Qdrant can be
mutated. If the model files are not cached, this step needs network access:

```bash
POSTGRES_URL=postgresql://USER:PASSWORD@127.0.0.1:55432/asset_db \
QDRANT_URL=http://127.0.0.1:6333 \
  python3 tools/build_qdrant_index.py \
  --asset-ids broo_kb3d_brk_bldglg_a \
  --force
```

Recommended live smoke before the full run:

```bash
RUN_ID=full_index_gpt55_live_smoke ./tools/launch_full_asset_index.sh --limit 2
```

After the smoke succeeds, inspect:

```bash
python3 tools/full_asset_index_runner.py status \
  --run-dir /data/siddhant/asset_db/runs/full_index_gpt55_live_smoke
```

## Qwen provider

The full runner supports the local OpenAI-compatible Qwen endpoint without writing through
Codex:

```bash
python3 tools/full_asset_index_runner.py check \
  --caption-provider qwen \
  --qwen-base-url http://137.110.161.132:8005/v1 \
  --qwen-model Qwen3.6-35B-A3B \
  --asset-db-dir /data/siddhant/asset_db \
  --manifest /data/siddhant/asset_db/manifest_full.json \
  --limit 3
```

For generation, add the same `--caption-provider qwen --qwen-base-url ... --qwen-model ...`
flags to `run` or the launcher. Qwen thinking is off by default; enable it only with
`--qwen-enable-thinking`.

Smoke result from 2026-06-10:

- Run dir: `/data/siddhant/asset_db_qwen_smoke/qwen36_full_pipeline_smoke_20260610_035626/runs/run`
- Isolated Postgres DB: `asset_db_qwen_smoke_20260610_035626`
- Isolated Qdrant collection: `assets_qwen_smoke_20260610_035626`
- Result after retry: catalog/Postgres/Qdrant all `10`; production DB/Qdrant stayed at `2039`.
- Mean successful asset time before DB sync: `46.4s`; mean Qwen VLM call: `6.0s`.
- Mean tokens: `8724` prompt, `389` completion, `9113` total.

## Full launch

Only run this after the explicit go-ahead:

```bash
cd /data/siddhant/SimWorld-Studio
./tools/launch_full_asset_index.sh \
  --skip-asset-id-file tools/full_asset_index_skip_helpers.txt
```

The launcher prints the PID, run dir, log path, and status command.

## Monitor

```bash
python3 tools/full_asset_index_runner.py status --asset-db-dir /data/siddhant/asset_db
tail -f /data/siddhant/asset_db/runs/<run_id>/runner.log
tail -f /data/siddhant/asset_db/runs/<run_id>/events.ndjson
```

Important files inside the run dir:

- `runner.log` - main detached process log
- `state.json` - resumable state summary
- `selected_asset_ids.txt` - exact manifest slice selected for the run
- `asset_results.ndjson` - one structured row per asset outcome
- `events.ndjson` - per-step event log
- `failures.ndjson` - error events for assets that failed generation
- `failed_assets.ndjson` - structured failed-asset rows for analysis
- `failed_asset_ids.txt` - retry-ready failed asset id list
- `skipped_assets.ndjson` - structured skipped-asset rows
- `skipped_asset_ids.txt` - skipped asset id list
- `usage.ndjson` - token usage row per attempted asset; pre-Codex failures have `usage: null`
- `metering.ndjson` - per-attempt status, phase, duration, usage, and raw Codex log paths
- `quality_warnings.ndjson` - heuristic quality warnings
- `quality_snapshot.md` - latest readable progress/quality snapshot
- `run_summary.md` - final readable run summary with counts and token totals
- `run_summary.json` - machine-readable final summary
- `pending_db_asset_ids.txt` - generated assets not yet synced to DB/Qdrant
- `db_sync/<sync_id>/` - logs for schema/import/Qdrant/category syncs

## Resume

If the process stops, restart with the same run dir:

```bash
RUN_DIR=/data/siddhant/asset_db/runs/<run_id> \
  ./tools/launch_full_asset_index.sh \
  --skip-asset-id-file tools/full_asset_index_skip_helpers.txt
```

The runner skips catalog JSONs that already exist, skips configured helper ids, and
retries pending DB sync ids.

For the UE 5.8 parallel runner, resume is fast-forwarded before workers are launched:
it builds the work queue from `manifest - existing catalog JSONs - configured skip ids`.
It does not replay `skip_existing` rows for every already-indexed asset. Missing
configured skips are recorded once as `skip_configured`, and the run logs a single
`resume_fast_forward` event with existing, configured-skip, and remaining-work counts.
The monitor and final summary use effective per-asset terminal status so historical
relaunch rows cannot make an `ok` asset look skipped or failed.

## Configured helper/non-spawnable skips

The strict skip list is `tools/full_asset_index_skip_helpers.txt` and currently has
`688` asset ids. It includes FoliageType assets, controller blueprints,
parent/procedural/customizer blueprints, editor/demo/baker helpers, global sky/water/godray
assets, proxy/template/test assets, and `626` decal/floor-marking/static overlay assets.
The decal cohort was added on 2026-06-09 after repeated nonpositive-dimension failures
around facade decal meshes; these are not useful standalone retrieval candidates.

These ids are logged as `skip_configured`, not as failures. That avoids wasting GPT-5.5
calls on assets that are not useful independent retrieval candidates and prevents clusters
of helper assets from tripping the repeated failure abort guard.

The runner also validates UE-measured dimensions before rendering or calling Codex, so any
future zero-size assets fail cheaply before model tokens are spent.

## Legacy catalog id remap

The earlier `1.2k` catalog used older asset ids for many of the same Unreal asset paths.
Use `tools/remap_catalog_ids_to_manifest.py` to remap those JSONs to current
`manifest_full.json` ids by exact `technical.unreal_asset_path`.

Applied on 2026-06-09:

- Report: `/data/siddhant/asset_db/catalog_id_remap_reports/apply_20260609_1707`
- Archive: `/data/siddhant/asset_db/catalog_id_remap_archive/apply_20260609_1707`
- Remapped to current ids: `1044`
- Archived old duplicate/conflict JSONs: `78`
- Unmatched legacy JSONs left in catalog: `87`
- Post-remap counts: catalog `1630`, Postgres `1630`, Qdrant `1630`

After applying the remap, run Postgres import, Qdrant build, and `build_category_index.py`
before resuming the full indexer.

## Reindex failures later

Create a text file with failed asset ids and run:

```bash
RUN_ID=full_index_gpt55_failure_retry \
  ./tools/launch_full_asset_index.sh \
  --asset-id-file /path/to/failed_asset_ids.txt \
  --force
```

Use `--force` only for intentional regeneration; it can move an asset JSON if the
new VLM category differs from the old one.

## Defaults worth knowing

- Sync every `50` newly generated assets or `1800` seconds, whichever comes first.
- Codex timeout per asset: `900` seconds by default. This is not a model-side guarantee;
  it is a conservative wall-clock guardrail for the full GPT-5.5 run. The earlier benchmark
  used `420` seconds and completed, while the old prototype used `300` seconds, but the full
  production run should avoid avoidable timeout failures.
- Render views per asset: `8`; minimum accepted views: `4`.
- Capture mode for future runs: `editor_game_view_highres`. This is still launched through
  `SimWorldEditor`, but the screenshot viewport is switched to UE Game View before capture.
- Consecutive failures: after `5`, the runner checks UE. If UE is unresponsive,
  it aborts so the run can be resumed after UE is fixed.
- Repeated Codex/VLM failures abort after `3` consecutive `codex_vlm` failures by default.
- Repeated failures in the same phase abort after `5` phase failures by default.
- `CODEX_REASONING_EFFORT` is not overridden by default. This preserves the
  current Codex config behavior for GPT-5.5.

## UE 5.8 backend/content migration notes

Updated on 2026-06-10.

New content root:

```bash
/data/koe/simworld-content-store/current/Content
```

`current` resolves to:

```bash
/data/koe/simworld-content-store/releases/ue58-citynav-20260610
```

Filesystem stats before UE AssetRegistry spawnable filtering:

- Content size: about `333G`.
- Top-level content folders: `166`.
- Total files: `85,886`.
- `.uasset` files: `84,886`.
- `.umap` files: `754`.
- Exact `.uasset` full paths are unique, but leaf filenames repeat: `77,253`
  unique case-insensitive leaf names, `3,155` duplicate-name groups, and
  `10,788` files in duplicate-name groups. Most large repeats are material-like
  names such as `m_bldg_glass`, not separate spawnable objects.
- Top `.uasset` roots by count: `Building` `11,302`, `hongkong` `6,862`,
  `VRHM_Urban_NPC` `2,800`, `__ExternalActors__` `2,508`, `VRHAsian` `2,337`,
  `SoccerStadiumArena` `2,246`, `ModularVictorianCity` `1,808`,
  `UrbanDistrict` `1,537`, `CitySampleCrowd` `1,437`, `CitySampleVehicles` `1,406`.
- Top `.umap` roots by count: `Environment` `148`, `Building` `112`,
  `DiverseMaps50` `65`, `Maps` `22`, `Medieval_Environment` `19`,
  `SoccerStadiumArena` `16`.

UE 5.8 AssetRegistry counts from the running shared editor:

- Registered `/Game` assets: `67,105`.
- Largest classes: `Texture2D` `21,851`, `StaticMesh` `21,009`,
  `MaterialInstanceConstant` `10,884`, `Material` `2,679`,
  `AnimSequence` `2,551`, `SkeletalMesh` `1,845`, `Blueprint` `1,640`.
- Obvious path noise overlaps: texture paths/names `24,426`, material paths/names
  `20,242`, map paths `2,874`, external actor/object paths `4,404`.
- Heuristic spawnable retrieval candidates after excluding obvious texture/material/map/test/helper paths:
  `24,200` total: `20,917` `StaticMesh`, `1,507` `Blueprint`, `1,776` `SkeletalMesh`.
- If skeletal/character assets are excluded for the first pass, the static-mesh/blueprint
  candidate set is about `22,424`.
- Strict actual-object manifest:
  `/data/siddhant/asset_db/ue58_object_manifest.json`.
- Rejected audit rows:
  `/data/siddhant/asset_db/ue58_object_manifest_rejects.jsonl`.
- The strict object manifest is generated by
  `python3 tools/build_ue58_object_manifest.py`. It uses token-aware filtering so
  object props such as road cones, hydrants, bollards, signs, lamps, barriers, and
  ceiling lights are not dropped just because they contain surface words such as
  `road`, `wall`, or `ceiling`.
- Current strict actual-object candidate count: `16,800` total:
  `15,609` `StaticMesh`, `1,191` `Blueprint`.
- Rejected from the broad spawnable set by this stricter object filter:
  `4,696` surface/modular-shell assets, `2,146` character/body-part assets,
  `470` helper/system assets, `294` noise-path assets, and `88` remaining skeletal assets.
- Launch the full UE 5.8 object-only run with
  `MANIFEST=/data/siddhant/asset_db/ue58_object_manifest.json` after the
  multi-worker UE 5.8 soak passes.

Separate checkouts created to avoid touching the dirty current Studio checkout:

- `/data/siddhant/SimWorld-Studio-main`: `origin/main` worktree at `62512cf4`.
- `/data/siddhant/SimWorld-UE58`: `SimWorld-AI/SimWorld.git` at `1f588ec9`.

UE 5.8 project/backend:

- Project: `/data/koe/SimWorld_SPEAR/SimWorld.uproject`.
- Editor binary: `/data/koe/SimWorld_SPEAR/Binaries/Linux/SimWorldEditor`.
- Content symlink: `/data/koe/SimWorld_SPEAR/Content -> /data/koe/simworld-content-store/current/Content`.
- Official MCP is enabled in the UE 5.8 project. The compatibility bridge is
  `/data/koe/SimWorld_SPEAR/tools/studio_migration/official_mcp_tcp_bridge.py`.
- Existing shared UE 5.8 instance is listening through the legacy TCP bridge on
  `127.0.0.1:55568`.

Verified against the existing shared UE 5.8 instance:

- `execute_python_script` through `127.0.0.1:55568` returns the same
  `result.python_logs` shape expected by `full_asset_index_runner.py`.
- Non-destructive asset load succeeds for
  `/Game/CityDatabase/meshes/SM_chair_b.SM_chair_b` from the new content store.

`tools/ue_multi_instance_smoke.py` now has an opt-in `--backend ue58-bridge` mode:

- Creates Siddhant-owned local project instance dirs under `/data/siddhant/ue58_smoke_instances`.
- Symlinks `Binaries`, `Plugins`, `Source`, `tools`, and `Content` from the UE 5.8 project/content store.
- Copies the small `Config` folder into each worker project and writes
  `[ConsoleVariables] AssetRegistry.DisableDirectoryWatcher=1` into the local
  `DefaultEngine.ini`. This is safe for read-only asset rendering and avoids each
  editor consuming tens of thousands of inotify watches.
- Copies warmed `Intermediate/CachedAssetRegistry_*.bin` from `/data/koe/SimWorld_SPEAR` into each local instance.
- Supports `--ddc-mode default` for the normal shared UE DDC/Zen path and
  `--ddc-mode local` for a Siddhant-owned seeded DDC at `/data/siddhant/ue58_ddc`.

Resolved UE 5.8 local smoke issue:

- The earlier suspected symlink/content blocker was not the root cause. A local
  worker using symlinked UE 5.8 `Binaries`, `Plugins`, `Source`, `tools`, and
  `Content` can load the project, open official MCP, run the TCP compatibility
  bridge, spawn `/Game/CityDatabase/meshes/SM_chair_b.SM_chair_b`, measure
  geometry, and save a valid screenshot.
- Code fix: UE 5.8 smoke results now write total `boot_sec` in addition to
  `official_boot_sec` and `bridge_boot_sec`, so summaries no longer show
  `boot_sec=[None]`.
- Code fix: worker `Config` is now copied locally and patched with
  `AssetRegistry.DisableDirectoryWatcher=1`. The first command-line-only attempt
  still hit `max_user_watches=65536`; the config-based fix removed the inotify
  warning in the verified one-worker run.
- Verified one-worker smoke after the fix:
  `/data/siddhant/ue58_bridge_smoke/one_worker_config_fix_verify_20260610_0558`.
  Result: `ok=True`, `boot_sec=110.115`, `render_sec=93.461`, `valid_views=1`,
  no inotify warning markers, and GPU memory after render about `2524MB`.

Verified 2/3/4/5/6-worker low-view smoke on GPU 3 after the fix:
`/data/siddhant/ue58_bridge_smoke/multi_worker_2to6_low_20260610_0608`.

- Batch 2: `ok=True`, render seconds `[81.083, 81.44]`,
  GPU after render `5031MB/37%`.
- Batch 3: `ok=True`, render seconds `[56.941, 69.327, 81.122]`,
  GPU after render `7827MB/97%`.
- Batch 4: `ok=True`, render seconds `[62.181, 62.31, 72.523, 84.576]`,
  GPU after render `10390MB/97%`.
- Batch 5: `ok=True`, render seconds `[60.183, 60.686, 62.22, 73.641, 79.57]`,
  GPU after render `13008MB/97%`.
- Batch 6: `ok=True`, render seconds `[58.766, 63.687, 64.171, 66.324, 79.079, 86.973]`,
  GPU after render `15717MB/97%`.

GPU memory is comfortable for 6 low-view render workers on the L40S. Compute
hits about full utilization during render bursts, which is expected and useful
for the indexing bottleneck. After the run, all local smoke UE/bridge processes
were cleaned up and GPU 3 returned to idle.

UE 5.8 parallel full-pipeline runner readiness:

- Parallel runner: `tools/ue58_parallel_asset_index_runner.py`.
- Detached launcher: `tools/launch_ue58_parallel_asset_index.sh`.
- Postgres DB helper: `tools/ensure_postgres_database.py`.
- Isolated UE 5.8/Qwen asset DB dir: `/data/siddhant/asset_db_ue58_qwen`.
- Isolated Postgres database: `asset_db_ue58_qwen`.
- Isolated Qdrant collection: `assets_ue58_qwen`.
- Strict UE 5.8 object manifest: `/data/siddhant/asset_db/ue58_object_manifest.json`
  with `16,800` actual-object candidates.
- Worker projects: `/data/siddhant/ue58_smoke_instances/inst_*`.
- Default model path: Qwen provider, `Qwen3.6-35B-A3B`, thinking disabled.
- The parallel runner launches its own UE 5.8 workers and TCP bridges, so it
  does not depend on the old single `MCP_PORT=55571` serial setup.
- The runner refuses sustained non-dry runs when
  `fs.inotify.max_user_watches` is below `524288`, unless explicitly overridden.
- Readiness check passed on 2026-06-10:
  `python3 tools/ue58_parallel_asset_index_runner.py check --sample-size 3`.
  It verified Qwen, Postgres, Qdrant, the UE 5.8 manifest, and
  `max_user_watches=524288`.
- Isolated DB prep completed on 2026-06-10:
  Postgres schema applied to `asset_db_ue58_qwen`, empty catalog import returned
  `processed=0, failed=0`, and Qdrant collection `assets_ue58_qwen` was created
  with zero pending embeddings.
- Dry-run artifact:
  `/data/siddhant/asset_db_ue58_qwen/runs/dry_run_readiness_20260610`.

When ready for the real 100-asset UE 5.8/Qwen parallel test:

```bash
cd /data/siddhant/SimWorld-Studio
UE58_WORKERS=6 SIMWORLD_GPU=3 \
  ./tools/launch_ue58_parallel_asset_index.sh \
  --sample-size 100 \
  --sample-seed 58 \
  --sync-every-assets 25 \
  --sync-min-seconds 600
```

Monitor it with:

```bash
python3 tools/full_asset_index_runner.py status \
  --asset-db-dir /data/siddhant/asset_db_ue58_qwen \
  --run-dir /data/siddhant/asset_db_ue58_qwen/runs/<run_id>
```

100-asset UE 5.8/Qwen parallel test completed on 2026-06-10:

- Run dir:
  `/data/siddhant/asset_db_ue58_qwen/runs/ue58_parallel_qwen36_100asset_20260610_065740`.
- Config: 6 UE workers on GPU 3, `8` views per asset, `1024` render resolution,
  Qwen `Qwen3.6-35B-A3B` with thinking disabled.
- Result: selected `100`, indexed `93`, failed `7`, quality warnings `0`.
- Final persistence: catalog JSONs `93`, Postgres rows `93`, Qdrant points `93`,
  pending DB/Qdrant sync `0`, DB sync failures `0`.
- Token usage over successful Qwen calls: `810,946` prompt tokens, `35,703`
  completion tokens, `846,649` total tokens. Average per successful asset:
  about `8,720` prompt tokens and `384` completion tokens.
- Successful asset timing from metering: mean wall time `41.2s`, median wall
  time `35.6s`; mean render section `20.3s`; mean Qwen VLM call `5.8s`.
- Failed asset ids:
  `hongkong_kb3d_hok_proptrashbags_c`, `trainstation_bp_electricalwire`,
  `hongkong_kb3d_hok_smstorageblock_a_build6posterc`, `infinityweather_cm_test`,
  `soccerstadiumarena_sm_wire_coll_5`, `soccerstadiumarena_bp_airpipes_coll_1`,
  `modularscifi_sm_hsdecal_024`.
- Failure modes were UE spawn/measure or nonpositive-dimension failures, not
  Qwen, Postgres, Qdrant, or inotify failures.
- After completion, the runner cleaned up its worker editors/bridges and GPU 3
  returned to idle.

Remaining UE 5.8 environment issue:

- The worker still emits shared Zen/DDC permission warnings and waits about
  `76s` before using the existing Zen service. This is environment/setup related
  because the running Zen data/lock path is owned by another user, not an asset
  retrieval code bug. It affects startup latency, not correctness in the
  verified smoke.
- In the 2/3/4/5/6-worker smoke, inotify warnings reappeared when the
  machine-level watch limit was `65536`. The limit now reads `524288`, which is
  the target for the 6-worker pipeline. If it drops back below that value, ask
  an admin to raise `fs.inotify.max_user_watches` before a production
  multi-worker run. If startup latency or Zen contention is too high, ask an
  admin to provide a clean per-user Zen/DDC setup or raise the relevant system
  limits; do not copy the `333G` content tree just to solve this.

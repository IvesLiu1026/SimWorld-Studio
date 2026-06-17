# Asset Retrieval Deliverable TODO

This tracks the remaining work needed to make the asset retrieval component a complete
deliverable: final database quality validation, failure handling, runtime integration,
A/B evaluation, and a generalized indexing workflow that other users can run on their
own asset libraries.

## 1. Finish Current Full-Library Index

- [ ] Let the UE 5.8/Qwen full indexing run finish.
- [ ] Confirm the final run did a forced final sync.
- [ ] Confirm `pending_db_asset_ids.txt` is empty.
- [ ] Save final `run_summary.md`, `run_summary.json`, `quality_snapshot.md`, and failure logs.
- [ ] Record final counts:
  - selected manifest assets
  - indexed catalog JSONs
  - configured skips
  - failure count by reason
  - Postgres rows
  - Qdrant points
  - quality warning count
  - total runtime and token usage

## 2. Final Catalog/Postgres/Qdrant Quality Audit

Create a final audit script, likely `tools/audit_asset_retrieval_db.py`, that outputs:

- `final_asset_db_audit.md`
- `final_asset_db_audit.json`
- `bad_catalog_asset_ids.txt`
- `recoverable_failed_asset_ids.txt`
- `permanent_skip_asset_ids.txt`

Checks:

- [ ] Every catalog JSON parses.
- [ ] Every catalog JSON passes the metadata schema/record validation.
- [ ] Required identity fields are present:
  - `asset_id`
  - `name`
  - `category`
  - `source_pack`
- [ ] Required semantic fields are present and non-empty:
  - `setting`
  - `short_description`
  - `description`
  - `function`
  - `tags`
  - `materials`
  - `scene_types`
- [ ] Required geometry fields are positive:
  - `width`
  - `depth`
  - `height`
  - `bounding_radius_m`
- [ ] Required technical fields are present:
  - `unreal_asset_path`
  - `asset_type`
  - `render_views`
  - `view_count`
  - `caption_model`
- [ ] No duplicate `asset_id`.
- [ ] Catalog category counts match `category_index.json`.
- [ ] Postgres row count matches catalog JSON count.
- [ ] Postgres has no nulls in required runtime fields.
- [ ] Postgres full-text column `search_tsv` is populated.
- [ ] Qdrant collection exists.
- [ ] Qdrant point count matches Postgres row count.
- [ ] Every Postgres `qdrant_point_id` exists in Qdrant.
- [ ] Qdrant payloads match Postgres for:
  - `asset_id`
  - `category`
  - `setting`
  - `asset_type`
  - `unreal_asset_path`
- [ ] Qdrant named vectors exist:
  - `text_dense`
  - `text_sparse`
- [ ] Run targeted retrieval sanity queries and save top results for inspection.

## 3. Render Quality Audit

The current indexer already rejects corrupt/missing screenshots, too-few valid views,
and nonpositive dimensions. A valid PNG can still be visually weak, so add a post-run
render audit.

Automated checks:

- [ ] Each catalog render path exists.
- [ ] Each render has a valid image header.
- [ ] Each render has expected resolution.
- [ ] Each render is not blank or near-blank.
- [ ] Each render has enough entropy/visual variance.
- [ ] Each render is not mostly flat gray/sky/ground.
- [ ] Assets with suspicious render stats are written to `bad_render_asset_ids.txt`.

Manual/VLM-assisted checks:

- [ ] Generate contact sheets for a stratified random sample.
- [ ] Sample across all categories.
- [ ] Sample across major source packs.
- [ ] Always include quality-warning assets.
- [ ] Always include dimension outliers.
- [ ] Always include unusual category/setting combinations.
- [ ] Create a short report with screenshots, captions, tags, materials, and notes.

Recommended first sample size:

- 10 assets per large category.
- 5 assets per small category.
- 100 additional random assets globally.
- All assets from `quality_warnings.ndjson`.

## 4. Failure Triage and Retry Plan

Classify final failures by reason.

Permanent skip candidates:

- `nonpositive dimensions`
- known decals/posters/flat overlays
- sky/water/global helpers
- controllers/managers/spawners/templates
- empty/proxy/test/editor assets

Recoverable retry candidates:

- `spawn/measure failed` where asset name/path looks like a real object.
- `only N valid views rendered`.
- Qwen request/API timeout.
- JSON/schema parse failure.
- transient UE/bridge failure.
- transient Postgres/Qdrant sync failure.

Actions:

- [ ] Generate failure reason histogram.
- [ ] Write permanent skips to `permanent_skip_asset_ids.txt`.
- [ ] Write retryable ids to `recoverable_failed_asset_ids.txt`.
- [ ] Relaunch a retry run with `--asset-id-file recoverable_failed_asset_ids.txt`.
- [ ] Use the same isolated asset DB and Qdrant/Postgres target.
- [ ] Do not reindex successful catalog assets unless explicitly using `--force`.
- [ ] After retry, rerun final sync and final audit.
- [ ] Document final unresolved failures and why they are acceptable.

## 5. Runtime SimWorld Integration

Core retrieval integration is already present in the server:

- `asset-retrieval.js` supports `off`, `file`, `db`, and `baseline_full`.
- `asset-retrieval-db.js` performs Qdrant hybrid retrieval plus Postgres fallback.
- `/api/chat` injects the retrieved asset palette when mode is not `off`.
- Codex builder mode also calls the same retrieval block.

Remaining integration tasks:

- [ ] Point runtime env vars to the UE 5.8/Qwen full library:
  - `ASSET_DB_DIR=/data/siddhant/asset_db_ue58_qwen`
  - `POSTGRES_URL=postgresql://USER:PASSWORD@127.0.0.1:55432/asset_db_ue58_qwen`
  - `QDRANT_URL=http://127.0.0.1:6333`
  - `QDRANT_COLLECTION=assets_ue58_qwen`
  - `EMBED_SERVICE_URL=http://127.0.0.1:7777`
  - `ASSET_RETRIEVAL_MODE=db`
  - `PREFILTER_TOP_K=150`
- [ ] Start/verify the embedding service.
- [ ] Confirm `server/start.sh` defaults are updated or document required overrides.
- [ ] Run retrieval-only smoke tests through Node.
- [ ] Run `/api/chat` smoke test with `assetRetrievalMode=db`.
- [ ] Confirm generated prompt contains the retrieved palette.
- [ ] Confirm spawned actor paths come from the retrieved palette.
- [ ] Confirm retrieval failure stops the scene generation request instead of silently falling back.

## 6. A/B Evaluation Modes

Use these modes for comparison:

- `db`: final retrieval component, Qdrant/Postgres prefilter plus LLM selection.
- `off`: no asset retrieval; old baseline behavior.
- `file`: file-catalog-only retrieval. Useful for debugging, but may be too heavy for the full library.
- `baseline_full`: exposes the full catalog through the same formatter. Not recommended for the full 16.8k library because context will be too large.

Recommended A/B comparison:

- [ ] Use `db` vs `off` for the main paper/demo comparison.
- [ ] Keep the same model, prompt, seed/session setup, and scene-generation settings.
- [ ] Save:
  - prompt
  - retrieval mode
  - retrieved categories
  - retrieved asset ids
  - spawned actor paths
  - final screenshot(s)
  - generated scene logs
  - failure notes
- [ ] Optionally add a small UI selector for retrieval mode.
- [ ] For scripted tests, pass request body fields:
  - `assetRetrievalMode: "db"`
  - `assetRetrievalMode: "off"`

## 7. Full-Library Scene Generation Test Set

Use diverse scenes that should benefit from the full 16.8k library.

Initial 10 prompts:

1. A foggy medieval market square outside a gothic cathedral with wooden stalls, candles, barrels, carts, and wet cobblestone.
2. A dense Hong Kong night alley with neon signs, rollup doors, AC units, cables, carts, trash bins, and shopfront clutter.
3. An industrial harbor loading dock with shipping containers, barrels, cranes, warehouse props, pallets, and warning signs.
4. An East Asian temple courtyard with stone lanterns, carved statues, shrine props, plants, benches, and ceremonial objects.
5. A sci-fi research outpost interior with consoles, glowing panels, crates, cables, metal grating, and lab equipment.
6. A winter village street with snow piles, lanterns, bare trees, firewood stacks, market stalls, benches, and fences.
7. A suburban park plaza with benches, trash bins, trees, planters, playground-like props, street lamps, and path clutter.
8. A Middle Eastern bazaar courtyard with awnings, pottery, crates, carts, carpets, lanterns, plants, and market clutter.
9. A hospital operating room or medical lab with surgical lights, monitors, carts, cabinets, beds, and clean equipment.
10. A coastal fishing dock with boats, nets, crates, lamps, barrels, ropes, buoys, weathered props, and harbor clutter.

For each prompt:

- [ ] Run once with `assetRetrievalMode=db`.
- [ ] Run once with `assetRetrievalMode=off`.
- [ ] Capture final screenshots from the same camera policy.
- [ ] Save retrieval traces and spawned asset paths.
- [ ] Score manually or with a visual critic on:
  - prompt relevance
  - object specificity
  - asset diversity
  - visual coherence
  - reduced generic assets
  - placement quality

## 8. Generalized External Asset Library Indexing Script

Goal: provide a user-facing script so someone who clones the repo can point to an
Unreal content directory and build an asset retrieval DB without knowing our internal
one-off paths.

Proposed script:

- `tools/index_unreal_asset_library.py`

Main behavior:

1. Accept a user-provided content root or project.
2. Discover nested `.uasset` files.
3. Resolve valid Unreal asset paths through UE AssetRegistry, not only filesystem names.
4. Apply smart filtering to keep independently spawnable object assets.
5. Build a manifest.
6. Launch parallel UE workers on the selected GPU.
7. Render orbit views.
8. Call the configured VLM endpoint.
9. Write catalog JSONs.
10. Sync Postgres.
11. Build/update Qdrant.
12. Write a final audit/report.

Important design constraints:

- Use UE AssetRegistry as the authoritative source of asset class/type.
- Support deeply nested content directories.
- Avoid indexing textures, materials, maps, animations, surfaces, helpers, editor assets, and templates.
- Keep positive object terms so real objects are not over-filtered.
- Let users review the manifest before indexing.
- Make all paths and ports explicit CLI args.
- Default to safe settings; allow advanced parallel settings.

Suggested CLI:

```bash
python3 tools/index_unreal_asset_library.py \
  --content-root /path/to/UnrealProject/Content \
  --ue-project /path/to/UnrealProject/Project.uproject \
  --ue-editor /path/to/UnrealEditor \
  --asset-db-dir /path/to/output_asset_db \
  --postgres-url postgresql://user:pass@host:port/db \
  --qdrant-url http://host:6333 \
  --qdrant-collection assets_custom \
  --qwen-base-url http://host:8005/v1 \
  --qwen-model Qwen3.6-35B-A3B \
  --gpu 0 \
  --workers 1 \
  --n-views 8 \
  --res 1024
```

Required arguments:

- `--content-root`
- `--ue-project`
- `--ue-editor`
- `--asset-db-dir`
- `--postgres-url`
- `--qdrant-url`
- `--qdrant-collection`
- `--gpu`

VLM provider arguments:

- `--caption-provider qwen`
- `--qwen-base-url`
- `--qwen-model`
- `--qwen-enable-thinking`
- `--qwen-timeout`
- `--qwen-max-tokens`

Parallel/indexing arguments:

- `--workers`
- `--base-mcp-port`
- `--base-official-mcp-port`
- `--n-views`
- `--min-views`
- `--res`
- `--sync-every-assets`
- `--sync-min-seconds`
- `--sample-size`
- `--sample-seed`
- `--asset-id-file`
- `--skip-asset-id-file`
- `--force`
- `--dry-run`
- `--manifest-out`
- `--run-dir`

Future provider TODO:

- Add OpenAI-compatible paid API provider support as a clean provider option.
- Keep it disabled/commented in the first deliverable unless we decide to support it officially.
- Required future args would likely be:
  - `--api-base-url`
  - `--api-key-env`
  - `--api-model`
  - `--api-timeout`
  - `--api-max-tokens`

Implementation reuse:

- Reuse manifest filtering logic from `tools/build_ue58_object_manifest.py`.
- Reuse worker launching/render logic from `tools/ue58_parallel_asset_index_runner.py`.
- Reuse schema/validation helpers from `tools/full_asset_index_runner.py`.
- Reuse Postgres/Qdrant sync scripts:
  - `tools/migrate_to_postgres.py`
  - `tools/build_qdrant_index.py`
  - `tools/build_category_index.py`
- Avoid hardcoding `/data/siddhant` paths.
- Avoid hardcoding UE 5.8-only paths where possible.

Acceptance criteria:

- [ ] `--dry-run` discovers assets and writes a manifest without starting indexing.
- [ ] User can inspect filter statistics before indexing.
- [ ] User can index a 10-asset sample end-to-end.
- [ ] User can resume an interrupted run.
- [ ] User can choose GPU id and worker count.
- [ ] User can point to a self-hosted vLLM/OpenAI-compatible endpoint.
- [ ] Output DB can be used by SimWorld retrieval by setting env vars.
- [ ] Documentation includes setup, launch, monitor, resume, and troubleshooting.

## 9. Documentation Needed Before Push

- [ ] High-level README for the asset retrieval component.
- [ ] Setup docs for Postgres/Qdrant.
- [ ] Setup docs for embedding service.
- [ ] Indexing guide for an external UE asset library.
- [ ] Runtime integration guide for SimWorld Studio.
- [ ] A/B evaluation guide.
- [ ] Troubleshooting:
  - Docker/Postgres/Qdrant unavailable.
  - Qwen/vLLM endpoint unavailable.
  - UE worker startup failure.
  - low inotify watch limit.
  - bad content-root/project mismatch.
  - zero-dimension/helper assets.
  - Qdrant point count mismatch.
  - retrieval returns no candidates.

## 10. Final Deliverables

- [ ] Full indexed UE 5.8/Qwen asset DB.
- [ ] Postgres database with structured metadata.
- [ ] Qdrant collection with dense+sparse semantic vectors.
- [ ] Final audit report.
- [ ] Failure/skip report.
- [ ] Render quality sample report.
- [ ] A/B scene-generation report.
- [ ] Generalized external-library indexing script.
- [ ] Documentation for setup, indexing, runtime use, and evaluation.

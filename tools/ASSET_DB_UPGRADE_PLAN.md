# Asset Retrieval DB Upgrade: Postgres + Qdrant Prefilter

## Purpose of this document

Implementation plan for adding a Postgres + Qdrant prefilter layer between the existing LLM category router and the LLM per-category select step. This document contains the full current system context + the detailed implementation plan so it can be executed end-to-end in a new session without needing access to this conversation.

---

## 0. Implementation status and decisions (updated 2026-06-09)

The first DB-prefilter implementation is now wired into the repo. Normal `/api/chat`
prompt injection is controlled by `ASSET_RETRIEVAL_MODE` and now defaults to `db`
from `server/start.sh`, while per-request `assetMode` / `assetRetrievalMode`
overrides still allow A/B runs.

Implemented files:
- `docker-compose.yml` — Postgres 16 + Qdrant.
- `tools/schema.sql` — canonical Postgres table, indexes, `search_tsv` trigger.
- `tools/apply_schema.py` — applies `schema.sql` without requiring the `psql` CLI.
- `tools/migrate_to_postgres.py` — imports `/data/siddhant/asset_db/catalog/*/*.json` into Postgres.
  It now supports asset-id filters and chunked commits for long-run incremental syncs.
- `tools/build_qdrant_index.py` — embeds Postgres rows with local FastEmbed dense+sparse models and upserts to Qdrant.
  It now supports asset-id filters and skips model loading when no rows need embedding.
- `tools/embed_service.py` — local query embedding service for Node runtime.
- `simworld_studio_workspace/web/server/asset-retrieval-db.js` — Qdrant hybrid prefilter + Postgres bounded fallback.
- `tools/shadow_test.js` — containment validation against current full-list retrieval.
- `tools/full_asset_index_runner.py` — resumable full-manifest GPT-5.5 indexing runner with
  per-asset status, configured helper skips, usage logs, quality warnings, and periodic
  Postgres/Qdrant sync.
- `tools/launch_full_asset_index.sh` — detached `nohup setsid` launcher for long indexing runs.
- `tools/full_asset_index_skip_helpers.txt` — strict 688-id skip list for helper/non-spawnable
  assets such as FoliageType assets, controller BPs, editor helpers, global sky/water/godray
  assets, proxy/template/test assets, and decal/floor-marking/static overlay assets.
- `tools/remap_catalog_ids_to_manifest.py` — one-time legacy catalog id remapper that preserves
  old GPT-5.5 JSON metadata by matching `technical.unreal_asset_path` to the current manifest id.
- `tools/FULL_ASSET_INDEX_RUNBOOK.md` — launch, monitor, resume, and retry runbook.
- `tools/ASSET_RETRIEVAL_DELIVERABLE_TODO.md` — remaining deliverable checklist for final
  quality audits, failure triage, SimWorld integration/A-B testing, full-library scene tests,
  and a generalized user-facing indexing script for external asset libraries.

Implemented server changes:
- `asset-retrieval.js` router now returns `{categories, plan}` with `semantic_query`,
  `primary_settings`, `hard_exclude_settings`, `must_terms`, and `avoid_terms`.
- `asset-retrieval.js` supports `off`, `file`, `db`, and `baseline_full` modes.
  `db` uses Qdrant/Postgres prefilter candidates before stage-2 selection; `file`
  keeps the JSON catalog routing/selection path without DB prefiltering.
- Retrieval cache keys now include model, prefilter flag, top-K, collection, and embedding version.
- `/api/chat` now treats retrieval injection failure as fatal for the request: it emits a
  `retrieval` error event, sends a text error, sends `done` with `isError:true`, and does not
  launch the builder without a valid retrieval palette.
- `/api/chat`, Codex builder mode, and text/visual scene-loop inner turns all use the same
  asset retrieval mode resolver, so normal scene generation receives the palette block.
- `server/start.sh` now documents/exports asset DB retrieval env defaults.
- Repo-local `.codex/config.toml` now enables `execute_python_script` and `delete_all_spawned`
  for automatic SimWorld runs. The global Codex config was not changed.

Implementation decisions that supersede older snippets below:
- **Qdrant point ID:** do not use `asset_id` as the point id. Qdrant point ids are UUIDs or
  unsigned integers, so the implementation uses deterministic UUIDv5 values derived from
  `asset_id`, and keeps `asset_id` in payload/Postgres as the stable application id.
- **Fallback behavior:** no unbounded full-category fallback in prefilter mode. Runtime order is:
  Qdrant hybrid search -> Postgres capped fallback -> fatal retrieval error. A full-list fallback
  is only allowed for local debugging when `ASSET_FULL_FALLBACK_MAX` is explicitly set above the
  category size. Its default is `0`.
- **Soft setting boost:** Qdrant payload filters are used only for hard category and hard setting
  exclusion. Preferred settings are included in query text and in Postgres ranking; they are not
  encoded as Qdrant `should` filters because that can reduce recall rather than act as a pure boost.
- **Embedding model default:** the original plan specified BGE-M3 for both dense and sparse vectors,
  but FastEmbed `0.8.0` does not expose `BAAI/bge-m3` through `TextEmbedding`. The implemented
  default is `EMBED_DENSE_MODEL=BAAI/bge-large-en-v1.5` plus
  `EMBED_SPARSE_MODEL=Qdrant/bm25`, with `EMBED_VERSION=bge-large-en-v1.5-bm25-v1`.
  These are env-configurable if we later move to a FastEmbed version/model stack with BGE-M3 support.
- **Model forwarding in visual loop:** not in scope for this implementation. Model choice remains
  config/env-driven.

Current bring-up sequence:
```bash
cd /data/siddhant/SimWorld-Studio
docker compose up -d

export POSTGRES_URL=postgresql://USER:PASSWORD@127.0.0.1:55432/asset_db
python3 tools/apply_schema.py

ASSET_DB_DIR=/data/siddhant/asset_db \
  python3 tools/migrate_to_postgres.py

QDRANT_URL=http://127.0.0.1:6333 \
  python3 tools/build_qdrant_index.py

PORT=7777 python3 tools/embed_service.py
```

Validation before enabling:
```bash
ASSET_PREFILTER=false \
ASSET_DB_DIR=/data/siddhant/asset_db \
POSTGRES_URL=postgresql://USER:PASSWORD@127.0.0.1:55432/asset_db \
QDRANT_URL=http://127.0.0.1:6333 \
EMBED_SERVICE_URL=http://127.0.0.1:7777 \
PREFILTER_TOP_K=150 \
  node tools/shadow_test.js
```

Retrieval mode controls:
```bash
# Default from start.sh: DB/Qdrant retrieval.
ASSET_RETRIEVAL_MODE=db PREFILTER_TOP_K=150 ./simworld_studio_workspace/web/server/start.sh

# A/B comparison modes.
ASSET_RETRIEVAL_MODE=off ./simworld_studio_workspace/web/server/start.sh
ASSET_RETRIEVAL_MODE=file ./simworld_studio_workspace/web/server/start.sh
ASSET_RETRIEVAL_MODE=baseline_full ./simworld_studio_workspace/web/server/start.sh
```

Full catalog indexing workflow:
```bash
cd /data/siddhant/SimWorld-Studio

# No-service smoke check:
python3 tools/full_asset_index_runner.py check \
  --asset-db-dir /data/siddhant/asset_db \
  --manifest /data/siddhant/asset_db/manifest_full.json \
  --limit 3 \
  --skip-service-checks \
  --no-db-sync

# After Docker + UE are up, launch only when explicitly approved:
./tools/launch_full_asset_index.sh \
  --skip-asset-id-file tools/full_asset_index_skip_helpers.txt
```

Current local full manifest count is `14,700`; current generated catalog count is `1,209`.
The full-index runner defaults to `gpt-5.5`, skips existing catalog JSONs, performs an initial
full import of the existing catalog, then syncs newly generated assets to Postgres/Qdrant every
`50` assets or `1800` seconds. It also skips the configured strict helper list as
`skip_configured` rows so helper/decal clusters do not waste Codex calls or count as failures.
It validates UE-measured dimensions before render/VLM work so zero-size assets fail cheaply if
they are not already in the skip list. It uses a process lock, atomic state/pending writes,
startup reconciliation for current-run catalog JSONs, startup retry of pending DB sync ids,
DB/Qdrant verification before clearing pending ids, unique Codex attempt logs, and early aborts
for repeated Codex/phase failures. See `tools/FULL_ASSET_INDEX_RUNBOOK.md`.

Legacy catalog id remap applied on 2026-06-09:
- Report: `/data/siddhant/asset_db/catalog_id_remap_reports/apply_20260609_1707`
- Archive: `/data/siddhant/asset_db/catalog_id_remap_archive/apply_20260609_1707`
- `1044` old JSONs remapped to current manifest ids by exact Unreal asset path.
- `78` old duplicate/conflict JSONs archived because current-id JSONs already existed.
- `87` unmatched legacy JSONs left unchanged.
- Post-remap sync counts: catalog `1630`, Postgres `1630`, Qdrant `1630`.

Known design items not changed by the robustness pass:
- Normal Studio UI/runtime routing now uses the asset retrieval mode resolver by default, but no
  visible UI toggle has been added; use env or request-body overrides for controlled A/B runs.
- Runtime retrieval still uses the file catalog as the canonical in-process asset cache, with
  Postgres/Qdrant as the optional prefilter. Making Postgres the runtime source of truth is a
  larger design change.

---

## 1. Current system — what exists and how it works

### 1.1 File layout

```
/data/siddhant/SimWorld-Studio/
  simworld_studio_workspace/web/server/
    asset-retrieval.js       ← 3-stage LLM retrieval pipeline (main file to modify)
    llm-oneshot.js           ← spawns claude CLI for one-shot LLM calls (used by retrieval)
    model-config.js          ← resolves which model to use (shared by coder + retrieval)
    index.js                 ← Express server; integrates retrieval via buildPromptBlock()
    mcp-server.js            ← MCP bridge to Unreal Engine
  tools/
    index_assets.py          ← VLM asset indexer (spawns in UE, renders, calls Codex)
    build_category_index.py  ← rebuilds category_index.json from catalog/
    categories.json          ← 26 category id+description pairs
    vlm_output_schema.json   ← JSON schema enforced on VLM captioning output

/data/siddhant/asset_db/     ← generated data, NOT in git
  category_index.json        ← flat index of all 26 categories + asset list per category
  catalog/
    <category>/
      <asset_id>.json        ← one JSON per asset (see schema below)
  renders/
    <asset_id>/
      view_00.png … view_07.png   ← 8 orbit renders per asset
  schema/
    vlm_output_schema.json   ← same as tools/vlm_output_schema.json
```

### 1.2 Asset JSON schema (actual fields)

Every asset JSON at `catalog/<category>/<asset_id>.json` has this structure:

```json
{
  "identity": {
    "asset_id": "su_farm_bp_fieldspline",
    "name": "Curved Plowed Field Row",
    "category": "agricultural_props",
    "subcategory": "curved soil furrow spline",
    "source_pack": "UltimateFarming"
  },
  "semantic": {
    "short_description": "One sentence.",
    "description": "2-4 sentences.",
    "tags": ["farm", "field", "furrow"],
    "style": "realistic rural farming game asset",
    "materials": ["packed soil", "loose dirt"],
    "color_palette": ["tan brown", "dry ochre"],
    "mood": ["rural", "practical"],
    "typical_placement": ["on farmland", "on bare ground"],
    "function": "Defines a prepared strip of tilled soil.",
    "affordances": ["can form curved field rows"],
    "scene_types": ["farm", "rural countryside", "medieval market"],
    "condition": "worn",
    "setting": "nature_rural"
  },
  "geometry": {
    "dimensions_m": {"width": 2.56, "depth": 13.962, "height": 2.56},
    "footprint_m": {"width": 2.56, "depth": 13.962},
    "bounding_radius_m": 7.212,
    "up_axis": "Z",
    "pivot": "base_center",
    "is_symmetric": false,
    "default_scale": 1.0
  },
  "technical": {
    "unreal_asset_path": "/Game/UltimateFarming/Blueprints/BP_FieldSpline.BP_FieldSpline",
    "asset_type": "Blueprint",
    "mobility": "MOVABLE",
    "has_collision": false,
    "material_slots": ["MI_Planter_C"],
    "lod_count": 1,
    "triangle_count": 1293
  },
  "indexing": {
    "render_views": ["renders/su_farm_bp_fieldspline/view_00.png"],
    "view_count": 8,
    "caption_model": "gpt-5.5",
    "schema_version": "1.0"
  }
}
```

**Enums used by VLM (enforced via JSON schema):**

`setting` (15 values):
```
modern_urban, industrial, suburban_residential, commercial_retail,
nature_rural, coastal_harbor, medieval, fantasy_gothic, ancient_temple,
middle_eastern, east_asian, winter, sci_fi, indoor, generic
```

`category` (26 values):
```
agricultural_props, barriers_and_fencing, building_pieces, buildings,
camping_outdoor, carts_and_vendors, decor_and_landmarks, furniture_indoor,
ground_and_road, indoor_clutter, industrial_goods, lighting,
litter_and_debris, market_goods, medieval_fantasy_props, nature_terrain,
pipes_tanks_infra, religious_ritual, sci_fi_props, seating,
signage, tools_equipment, vegetation, vehicles, waste_and_bins, winter_snow_props
```

### 1.3 Current retrieval pipeline — asset-retrieval.js

```
loadDB()
  reads category_index.json → loads all 1209 asset JSONs into memory as compact objects
  compact = {id, name, category, subcategory, desc, tags[8], sceneTypes[5],
             setting, dims, path, assetType, spawnTool}

retrieve(scene_text, opts)
  stage 1: routeCategories(scene, db, opts)
    → one LLM call via oneshotJSON()
    → returns: [{id, emphasis:"primary|secondary", reason}]

  stage 2: selectInCategory(scene, cat, emphasis, opts)
    → one LLM call per routed category, all in PARALLEL via Promise.all()
    → input: all assets in the category (full list, no prefilter currently)
    → returns: [{id, reason}]

  stage 3: aggregate(scene, picked, db, opts)
    → one LLM call over all selected assets from all categories
    → drops off-theme, orders by importance
    → returns: {rationale, final:[{id}]}

buildPromptBlock(scene, mode, opts)
  → orchestrates retrieve() then formats assets for the coder system prompt
  → called from index.js /api/chat endpoint
```

**The integration point in index.js (around line 748):**
```js
const __blk = await require("./asset-retrieval").buildPromptBlock(t, __am, {
  model: __mdl,
  log: (x) => logToFile("retrieval", x)
});
```

**LLM calls use llm-oneshot.js:**
```js
// oneshotJSON(prompt, opts) — spawns claude CLI, returns parsed JSON
// opts: { model, timeoutMs, claudeBin, cwd }
const { oneshotJSON } = require("./llm-oneshot");
```

### 1.4 Current scale and cost problem

- 1,209 assets across 26 categories
- Large categories: building_pieces (235), signage (94), vegetation (118), barriers_and_fencing (68)
- The LLM select step sees ALL assets in each routed category
- At 100k+ assets this means 100k-400k tokens per retrieval request just for the select step
- **Goal**: insert a fast DB prefilter between stage 1 (route) and stage 2 (select) to narrow each category down to 100-200 candidates before the LLM sees them

---

## 2. Target architecture

```
Prompt
  ↓
[EXISTING] LLM category router  → enhanced to also return semantic_query + setting plan
  ↓
[NEW] Qdrant/Postgres prefilter per routed category
  → hard filter: category match
  → hard exclude: incompatible settings
  → soft boost: preferred settings + scene_type overlap
  → dense + sparse hybrid vectors: BGE-M3 (BAAI/bge-m3) on composite asset text
  → payload filter fusion
  → top 150 candidates per category
  ↓
[EXISTING] LLM per-category select  ← now receives 150 candidates instead of full list
  ↓
[EXISTING] LLM aggregate
  ↓
Builder
```

**Source of truth:** Postgres (canonical metadata, raw JSONB, admin queries)
**Search index:** Qdrant (vector recall + payload filtering)
**LLM pipeline:** unchanged — only the input to selectInCategory() changes

---

## 3. Infrastructure setup

### 3.1 Docker compose

Create `/data/siddhant/SimWorld-Studio/docker-compose.yml`:

```yaml
version: "3.9"
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_DB: asset_db
      POSTGRES_USER: simworld
      POSTGRES_PASSWORD: simworld
    ports:
      - "${POSTGRES_HOST_PORT:-55432}:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data

  qdrant:
    image: qdrant/qdrant:latest
    ports:
      - "6333:6333"   # HTTP REST
      - "6334:6334"   # gRPC
    volumes:
      - qdrant_data:/qdrant/storage

volumes:
  postgres_data:
  qdrant_data:
```

Launch: `docker compose up -d`

### 3.2 Environment variables

Add to server startup (or `.env` file read by index.js):

```bash
# Postgres
POSTGRES_URL=postgresql://USER:PASSWORD@127.0.0.1:55432/asset_db

# Qdrant
QDRANT_URL=http://127.0.0.1:6333
QDRANT_COLLECTION=assets

# Embedding microservice (BGE-M3 via FastEmbed — see section 3.4)
EMBED_SERVICE_URL=http://127.0.0.1:7777

# Feature flag — enables the prefilter; false = fall back to current in-memory full list
ASSET_PREFILTER=false
```

Note: `OPENAI_API_KEY` is still needed for VLM asset captioning (`index_assets.py` calls
`codex exec`), but NOT for embeddings — those are handled locally by BGE-M3.

The `ASSET_PREFILTER=false` default means the existing pipeline is unchanged until the flag is set.

### 3.3 npm dependencies to add

In `simworld_studio_workspace/web/server/`:

```bash
npm install pg @qdrant/js-client-rest
```

In Python (for migration scripts + embedding service):

```bash
pip install psycopg2-binary qdrant-client fastembed fastapi uvicorn
```

### 3.4 Embedding microservice — BGE-M3 via FastEmbed

BGE-M3 (BAAI/bge-m3) is the embedding model. It produces **both dense (1024-dim) and sparse
vectors** from the same model — enabling true hybrid retrieval with no external API dependency.

FastEmbed downloads the model on first run (~570MB ONNX weights) and caches it locally.

Create `tools/embed_service.py`:

```python
#!/usr/bin/env python3
"""
Local BGE-M3 embedding service (dense + sparse).
FastEmbed downloads model on first run (~570MB) and caches it.

Usage: python tools/embed_service.py
       PORT=7777 python tools/embed_service.py
"""
import os
from fastapi import FastAPI
from pydantic import BaseModel
import uvicorn
from fastembed import TextEmbedding, SparseTextEmbedding

PORT = int(os.environ.get("PORT", "7777"))
MODEL = "BAAI/bge-m3"

print(f"Loading BGE-M3 (first run downloads ~570MB)...")
_dense  = TextEmbedding(MODEL)
_sparse = SparseTextEmbedding(MODEL)
print("Model loaded.")

app = FastAPI()

class EmbedRequest(BaseModel):
    texts: list[str]

@app.post("/embed")
def embed(req: EmbedRequest):
    dense_vecs     = [v.tolist() for v in _dense.embed(req.texts)]
    sparse_results = list(_sparse.embed(req.texts))
    sparse_vecs    = [
        {"indices": r.indices.tolist(), "values": r.values.tolist()}
        for r in sparse_results
    ]
    return {"dense": dense_vecs, "sparse": sparse_vecs}

@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL}

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT)
```

Launch alongside the Node server (tmux pane or systemd unit):

```bash
python tools/embed_service.py
```

Query embedding at runtime is ~5-15ms per request once the service is warm.

---

## 4. Postgres schema

Run this SQL once to create the table:

```sql
CREATE TABLE IF NOT EXISTS assets (
  -- identity
  asset_id          TEXT PRIMARY KEY,
  name              TEXT NOT NULL,
  category          TEXT NOT NULL,
  subcategory       TEXT,
  source_pack       TEXT,

  -- semantic (high-cardinality query fields)
  setting           TEXT,
  style             TEXT,
  condition         TEXT,
  short_description TEXT,
  description       TEXT,
  "function"        TEXT,
  scene_types       TEXT[]  DEFAULT '{}',
  tags              TEXT[]  DEFAULT '{}',
  materials         TEXT[]  DEFAULT '{}',
  mood              TEXT[]  DEFAULT '{}',
  typical_placement TEXT[]  DEFAULT '{}',
  affordances       TEXT[]  DEFAULT '{}',
  color_palette     TEXT[]  DEFAULT '{}',

  -- geometry
  width_m           DOUBLE PRECISION,
  depth_m           DOUBLE PRECISION,
  height_m          DOUBLE PRECISION,
  footprint_w_m     DOUBLE PRECISION,
  footprint_d_m     DOUBLE PRECISION,
  bounding_radius_m DOUBLE PRECISION,
  is_symmetric      BOOLEAN,

  -- technical
  unreal_asset_path TEXT NOT NULL,
  asset_type        TEXT,           -- 'Blueprint' | 'StaticMesh'
  mobility          TEXT,
  has_collision     BOOLEAN,
  triangle_count    INTEGER,
  lod_count         INTEGER,
  material_slots    TEXT[]  DEFAULT '{}',

  -- indexing
  caption_model     TEXT,
  render_views      TEXT[]  DEFAULT '{}',
  view_count        INTEGER DEFAULT 0,
  schema_version    TEXT    DEFAULT '1.0',

  -- upgrade tracking
  embedding_version TEXT,           -- set after Qdrant upsert (e.g. 'bge-m3-v1')
  embedding_hash    TEXT,           -- sha256 of embedding input — skip re-embed if unchanged
  raw_metadata      JSONB   NOT NULL,

  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Structured filters
CREATE INDEX IF NOT EXISTS assets_category_idx    ON assets (category);
CREATE INDEX IF NOT EXISTS assets_setting_idx     ON assets (setting);
CREATE INDEX IF NOT EXISTS assets_asset_type_idx  ON assets (asset_type);
CREATE INDEX IF NOT EXISTS assets_source_pack_idx ON assets (source_pack);
CREATE INDEX IF NOT EXISTS assets_dims_idx        ON assets (width_m, depth_m, height_m);

-- Array filters (GIN)
CREATE INDEX IF NOT EXISTS assets_tags_gin_idx        ON assets USING GIN (tags);
CREATE INDEX IF NOT EXISTS assets_scene_types_gin_idx ON assets USING GIN (scene_types);
CREATE INDEX IF NOT EXISTS assets_materials_gin_idx   ON assets USING GIN (materials);

-- Full-text search
ALTER TABLE assets ADD COLUMN IF NOT EXISTS search_tsv tsvector;

CREATE INDEX IF NOT EXISTS assets_search_tsv_idx ON assets USING GIN (search_tsv);

-- Trigger to keep search_tsv updated
CREATE OR REPLACE FUNCTION assets_search_tsv_update() RETURNS trigger AS $$
BEGIN
  NEW.search_tsv :=
    setweight(to_tsvector('english', coalesce(NEW.name,              '')), 'A') ||
    setweight(to_tsvector('english', coalesce(NEW.subcategory,       '')), 'A') ||
    setweight(to_tsvector('english', array_to_string(NEW.tags,         ' ')), 'A') ||
    setweight(to_tsvector('english', array_to_string(NEW.scene_types,  ' ')), 'B') ||
    setweight(to_tsvector('english', coalesce(NEW.function,          '')), 'B') ||
    setweight(to_tsvector('english', coalesce(NEW.short_description, '')), 'B') ||
    setweight(to_tsvector('english', coalesce(NEW.description,       '')), 'C');
  RETURN NEW;
END
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER assets_tsv_trigger
  BEFORE INSERT OR UPDATE ON assets
  FOR EACH ROW EXECUTE FUNCTION assets_search_tsv_update();

-- Backfill search_tsv for existing rows
UPDATE assets SET search_tsv = (
  setweight(to_tsvector('english', coalesce(name,              '')), 'A') ||
  setweight(to_tsvector('english', coalesce(subcategory,       '')), 'A') ||
  setweight(to_tsvector('english', array_to_string(tags,         ' ')), 'A') ||
  setweight(to_tsvector('english', array_to_string(scene_types,  ' ')), 'B') ||
  setweight(to_tsvector('english', coalesce("function",        '')), 'B') ||
  setweight(to_tsvector('english', coalesce(short_description, '')), 'B') ||
  setweight(to_tsvector('english', coalesce(description,       '')), 'C')
);
```

---

## 5. Qdrant collection design

One collection: `assets`

**Named vectors (BGE-M3 produces both from the same model):**
- `text_dense`  — 1024-dim, cosine distance
- `text_sparse` — sparse (SPLADE-style), dot product
- *(image vectors deferred to phase 2)*

**Payload fields indexed** (create payload index for each):
```
category        keyword
setting         keyword
asset_type      keyword
source_pack     keyword
scene_types     keyword   (array of strings)
tags            keyword   (array of strings)
width_m         float
depth_m         float
height_m        float
triangle_count  integer
```

### 5.1 Collection creation (JS)

```js
const { QdrantClient } = require("@qdrant/js-client-rest");
const qdrant = new QdrantClient({ url: process.env.QDRANT_URL });

await qdrant.createCollection("assets", {
  vectors: {
    text_dense: { size: 1024, distance: "Cosine" }
  },
  sparse_vectors: {
    text_sparse: {}   // sparse — no size needed; dot product by default
  }
});

// Create payload indexes
const payloadIndexes = [
  { field: "category",       schema: "keyword" },
  { field: "setting",        schema: "keyword" },
  { field: "asset_type",     schema: "keyword" },
  { field: "source_pack",    schema: "keyword" },
  { field: "scene_types",    schema: "keyword" },
  { field: "tags",           schema: "keyword" },
  { field: "width_m",        schema: "float"   },
  { field: "depth_m",        schema: "float"   },
  { field: "height_m",       schema: "float"   },
  { field: "triangle_count", schema: "integer" },
];

for (const { field, schema } of payloadIndexes) {
  await qdrant.createPayloadIndex("assets", {
    field_name: field,
    field_schema: schema
  });
}
```

### 5.2 Qdrant point structure

```json
{
  "id": "<asset_id>",
  "vector": {
    "text_dense": [0.01, 0.02, "...1024 values..."]
  },
  "sparse_vector": {
    "text_sparse": { "indices": [10, 231, 921], "values": [0.4, 0.8, 0.2] }
  },
  "payload": {
    "asset_id":         "su_farm_bp_fieldspline",
    "name":             "Curved Plowed Field Row",
    "category":         "agricultural_props",
    "subcategory":      "curved soil furrow spline",
    "setting":          "nature_rural",
    "style":            "realistic rural farming game asset",
    "scene_types":      ["farm", "rural countryside", "medieval market"],
    "tags":             ["farm", "field", "furrow", "soil"],
    "materials":        ["packed soil", "loose dirt"],
    "asset_type":       "Blueprint",
    "source_pack":      "UltimateFarming",
    "unreal_asset_path":"/Game/UltimateFarming/Blueprints/BP_FieldSpline.BP_FieldSpline",
    "width_m":          2.56,
    "depth_m":          13.962,
    "height_m":         2.56,
    "triangle_count":   1293,
    "embedding_version":"bge-m3-v1"
  }
}
```

Note: `id` in Qdrant must be a string UUID or unsigned integer. Use `asset_id` as string.

---

## 6. Embedding text construction

Build one composite text string per asset for embedding. Include all semantically-rich fields:

```js
function buildEmbeddingText(asset) {
  // asset = full catalog JSON (identity + semantic sections)
  const sem = asset.semantic || {};
  const id  = asset.identity  || {};
  return [
    `name: ${id.name || ""}`,
    `category: ${id.category || ""}`,
    `subcategory: ${id.subcategory || ""}`,
    `setting: ${sem.setting || ""}`,
    `style: ${sem.style || ""}`,
    `condition: ${sem.condition || ""}`,
    `scene types: ${(sem.scene_types || []).join(", ")}`,
    `tags: ${(sem.tags || []).join(", ")}`,
    `materials: ${(sem.materials || []).join(", ")}`,
    `mood: ${(sem.mood || []).join(", ")}`,
    `typical placement: ${(sem.typical_placement || []).join(", ")}`,
    `function: ${sem.function || ""}`,
    `affordances: ${(sem.affordances || []).join(", ")}`,
    `short description: ${sem.short_description || ""}`,
    `description: ${sem.description || ""}`,
  ].filter(s => !s.endsWith(": ")).join(" | ");
}
```

**Embedding hash** (use to skip re-embedding unchanged assets):

```js
const crypto = require("crypto");
function embeddingHash(text, modelVersion) {
  return crypto.createHash("sha256")
    .update(text + "|" + modelVersion)
    .digest("hex")
    .slice(0, 16);
}
```

---

## 7. Setting compatibility map

Used by the prefilter to build soft-boost and hard-exclude lists. When the LLM router identifies the scene genre, this map controls which settings are boosted (in `should` filter) and which are hard-excluded (in `must_not` filter).

```js
const SETTING_COMPAT = {
  medieval:             { boost: ["medieval", "fantasy_gothic", "nature_rural", "generic"],
                          allow: ["ancient_temple", "middle_eastern", "east_asian", "winter", "coastal_harbor"],
                          exclude: ["sci_fi", "modern_urban", "industrial", "suburban_residential", "commercial_retail"] },

  fantasy_gothic:       { boost: ["fantasy_gothic", "medieval", "generic"],
                          allow: ["ancient_temple", "winter", "nature_rural", "middle_eastern"],
                          exclude: ["sci_fi", "modern_urban", "industrial", "suburban_residential", "commercial_retail"] },

  ancient_temple:       { boost: ["ancient_temple", "middle_eastern", "east_asian", "generic"],
                          allow: ["medieval", "fantasy_gothic", "nature_rural", "coastal_harbor"],
                          exclude: ["sci_fi", "modern_urban", "industrial", "winter"] },

  middle_eastern:       { boost: ["middle_eastern", "ancient_temple", "generic"],
                          allow: ["medieval", "east_asian", "nature_rural", "coastal_harbor", "commercial_retail"],
                          exclude: ["sci_fi", "modern_urban", "industrial", "winter"] },

  east_asian:           { boost: ["east_asian", "middle_eastern", "ancient_temple", "generic"],
                          allow: ["medieval", "nature_rural", "coastal_harbor"],
                          exclude: ["sci_fi", "modern_urban", "industrial"] },

  modern_urban:         { boost: ["modern_urban", "industrial", "commercial_retail", "suburban_residential", "generic"],
                          allow: ["coastal_harbor", "indoor"],
                          exclude: ["medieval", "fantasy_gothic", "ancient_temple", "middle_eastern", "east_asian", "sci_fi", "winter"] },

  industrial:           { boost: ["industrial", "modern_urban", "generic"],
                          allow: ["coastal_harbor", "suburban_residential"],
                          exclude: ["medieval", "fantasy_gothic", "ancient_temple", "middle_eastern", "east_asian", "sci_fi"] },

  suburban_residential: { boost: ["suburban_residential", "modern_urban", "commercial_retail", "generic"],
                          allow: ["nature_rural", "indoor"],
                          exclude: ["medieval", "fantasy_gothic", "sci_fi", "ancient_temple"] },

  commercial_retail:    { boost: ["commercial_retail", "modern_urban", "suburban_residential", "generic"],
                          allow: ["coastal_harbor", "indoor"],
                          exclude: ["medieval", "fantasy_gothic", "sci_fi", "ancient_temple"] },

  coastal_harbor:       { boost: ["coastal_harbor", "industrial", "modern_urban", "generic"],
                          allow: ["nature_rural", "medieval", "commercial_retail"],
                          exclude: ["sci_fi", "fantasy_gothic", "ancient_temple", "east_asian"] },

  nature_rural:         { boost: ["nature_rural", "generic"],
                          allow: ["medieval", "coastal_harbor", "suburban_residential", "winter", "camping_outdoor"],
                          exclude: ["sci_fi", "modern_urban", "industrial", "fantasy_gothic"] },

  winter:               { boost: ["winter", "nature_rural", "generic"],
                          allow: ["medieval", "fantasy_gothic", "suburban_residential"],
                          exclude: ["sci_fi", "modern_urban", "industrial"] },

  sci_fi:               { boost: ["sci_fi", "generic"],
                          allow: ["industrial", "modern_urban", "indoor"],
                          exclude: ["medieval", "fantasy_gothic", "ancient_temple", "middle_eastern", "east_asian", "nature_rural"] },

  indoor:               { boost: ["indoor", "generic"],
                          allow: ["modern_urban", "commercial_retail", "suburban_residential", "sci_fi"],
                          exclude: [] },

  generic:              { boost: ["generic"],
                          allow: ["*"],    // never exclude generic assets
                          exclude: [] },
};
```

---

## 8. Upgraded LLM router output

Extend the stage 1 `routeCategories()` prompt to also return a structured retrieval plan alongside the category list. This richer output drives the prefilter:

**New expected JSON from the router:**
```json
{
  "categories": [
    {"id": "market_goods", "emphasis": "primary", "reason": "stalls and produce"},
    {"id": "buildings",    "emphasis": "secondary", "reason": "cathedral backdrop"}
  ],
  "semantic_query": "foggy medieval market outside gothic cathedral, wooden stalls, candles, cobblestone",
  "primary_settings": ["medieval", "fantasy_gothic"],
  "hard_exclude_settings": ["sci_fi", "modern_urban", "industrial"],
  "must_terms": ["market stall", "cobblestone", "candle", "banner"],
  "avoid_terms": ["neon", "computer", "plastic", "electronics"]
}
```

**Router prompt addition (append to existing prompt in routeCategories()):**

```
Additionally output these fields at the top level:
- "semantic_query": the scene description optimized for vector search (rephrase with key nouns/adjectives, include style/genre words)
- "primary_settings": array of 1-3 setting values from the enum that best match this scene
- "hard_exclude_settings": setting values that are clearly wrong for this scene
- "must_terms": 3-6 key object/concept terms that should appear in good assets
- "avoid_terms": 2-4 terms indicating wrong-genre assets to avoid

Setting enum values: modern_urban, industrial, suburban_residential, commercial_retail, nature_rural, coastal_harbor, medieval, fantasy_gothic, ancient_temple, middle_eastern, east_asian, winter, sci_fi, indoor, generic
```

---

## 9. Prefilter implementation — `asset-retrieval.js` changes

### 9.1 New file: `asset-retrieval-db.js`

Create `simworld_studio_workspace/web/server/asset-retrieval-db.js`. This module handles Postgres + Qdrant. Keep it separate from `asset-retrieval.js` to avoid breaking the existing pipeline:

```js
"use strict";
const { QdrantClient } = require("@qdrant/js-client-rest");

const COLLECTION       = process.env.QDRANT_COLLECTION  || "assets";
const EMBED_SERVICE    = process.env.EMBED_SERVICE_URL   || "http://127.0.0.1:7777";
const TOP_K            = parseInt(process.env.PREFILTER_TOP_K || "150");

let _qdrant = null;

function qdrant() {
  if (!_qdrant) _qdrant = new QdrantClient({ url: process.env.QDRANT_URL || "http://127.0.0.1:6333" });
  return _qdrant;
}

// Embed a query string via the local BGE-M3 FastEmbed service (dense + sparse)
async function embedQuery(text) {
  const resp = await fetch(`${EMBED_SERVICE}/embed`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ texts: [text] }),
  });
  if (!resp.ok) throw new Error(`embed service error: ${resp.status}`);
  const data = await resp.json();
  return { dense: data.dense[0], sparse: data.sparse[0] };
}

// Build Qdrant filter from the retrieval plan for one category
function buildQdrantFilter(category, plan) {
  const compat = SETTING_COMPAT[plan.primary_settings && plan.primary_settings[0]] || {};
  const exclude = plan.hard_exclude_settings || compat.exclude || [];

  const filter = {
    must: [
      { key: "category", match: { value: category } }
    ],
    must_not: [],
    should: [],
  };

  if (exclude.length) {
    filter.must_not.push({ key: "setting", match: { any: exclude } });
  }

  const boost = compat.boost || [];
  if (boost.length) {
    filter.should.push({ key: "setting", match: { any: boost } });
  }

  // Clean up empty arrays (Qdrant doesn't want empty must_not/should)
  if (!filter.must_not.length) delete filter.must_not;
  if (!filter.should.length)   delete filter.should;

  return filter;
}

// Prefilter: returns top-K compact asset objects for one category
async function prefilterCategory(category, plan) {
  const queryText = [
    plan.semantic_query || "",
    ...(plan.must_terms || []),
    ...(plan.primary_settings || []),
  ].filter(Boolean).join(" ");

  const { dense: denseVec, sparse: sparseVec } = await embedQuery(queryText);
  const filter = buildQdrantFilter(category, plan);

  // Hybrid query: RRF fusion of dense + sparse channels
  const results = await qdrant().query(COLLECTION, {
    prefetch: [
      { query: denseVec,  using: "text_dense",  filter, limit: TOP_K },
      { query: sparseVec, using: "text_sparse", filter, limit: TOP_K },
    ],
    query:        { fusion: "rrf" },
    limit:        TOP_K,
    with_payload: true,
  });

  return results.map(r => ({
    id:          r.payload.asset_id,
    name:        r.payload.name,
    category:    r.payload.category,
    subcategory: r.payload.subcategory || "",
    desc:        r.payload.short_description || "",
    tags:        (r.payload.tags || []).slice(0, 8),
    sceneTypes:  (r.payload.scene_types || []).slice(0, 5),
    setting:     r.payload.setting || "generic",
    dims:        r.payload.width_m != null
                   ? { width: r.payload.width_m, depth: r.payload.depth_m, height: r.payload.height_m }
                   : null,
    path:        r.payload.unreal_asset_path || "",
    assetType:   r.payload.asset_type || "",
    spawnTool:   r.payload.asset_type === "Blueprint" ? "spawn_blueprint_actor" : "spawn_actor",
    _score:      r.score,
  }));
}

module.exports = { prefilterCategory };
```

### 9.2 Modify asset-retrieval.js — integrate prefilter

The change is minimal. In `retrieve()`, before calling `selectInCategory()`, optionally call `prefilterCategory()` if the flag is enabled:

```js
// At top of asset-retrieval.js, add:
const ASSET_PREFILTER = process.env.ASSET_PREFILTER === "true";

// Modify retrieve() — the routed result from routeCategories() now includes the plan:
async function retrieve(scene, opts) {
  // ... existing setup code ...

  // Stage 1: route (now returns richer plan object)
  const routeResult = await routeCategories(scene, db, o);
  const routed   = routeResult.categories || routeResult;  // back-compat if router returns old shape
  const plan     = routeResult.plan || {};  // {semantic_query, primary_settings, ...}
  trace.routed = routed;

  // Stage 2: select — with optional prefilter
  const results = await Promise.all(routed.map(async r => {
    const cat = db.byCat.get(r.id);
    if (!cat) return [];

    let candidates;
    if (ASSET_PREFILTER) {
      try {
        const { prefilterCategory } = require("./asset-retrieval-db");
        candidates = { ...cat, assets: await prefilterCategory(r.id, plan) };
        log(`prefilter ${r.id}: ${candidates.assets.length} candidates (from ${cat.count})`);
      } catch (e) {
        log(`prefilter ${r.id} FAILED (${e.message}) — falling back to full list`);
        candidates = cat;  // graceful fallback
      }
    } else {
      candidates = cat;
    }

    try {
      const sel = await selectInCategory(scene, candidates, r.emphasis, o);
      trace.perCategory[r.id] = sel.map(s => s.id);
      log(`select ${r.id}: ${sel.length}/${cat.count}`);
      return sel.map(s => ({ ...s, category: r.id }));
    } catch (e) {
      log(`select ${r.id} FAILED: ${e.message}`);
      return [];
    }
  }));

  // ... rest of retrieve() unchanged (dedup → aggregate → cache) ...
}
```

**Key constraint:** `selectInCategory()` already receives a `cat` object with `cat.assets` array. Prefilter just replaces that assets array with a smaller filtered list — the function signature doesn't change.

### 9.3 Router changes

Modify `routeCategories()` to return a two-key object:

```js
async function routeCategories(scene, db, opts) {
  // existing cats string ...
  const prompt = [
    "You are the CATEGORY ROUTER for a 3D scene asset retriever...",
    // ... existing prompt ...
    "",
    'Output ONLY JSON: {"categories":[{"id":"<cat>","emphasis":"primary|secondary","reason":"<short>"}],'
    + '"semantic_query":"<search-optimized scene description>",'
    + '"primary_settings":["<setting1>"],'
    + '"hard_exclude_settings":["<setting>",...],'
    + '"must_terms":["<term>",...],'
    + '"avoid_terms":["<term>",...]}',
  ].join("\n");

  const out = await oneshotJSON(prompt, opts);

  // validate and return both categories + plan
  const valid = new Set(db.categories.map(c => c.id));
  const seen = new Set(), uniq = [];
  for (const c of (out.categories || [])) {
    if (c && valid.has(c.id) && !seen.has(c.id)) { seen.add(c.id); uniq.push(c); }
  }

  return {
    categories: uniq,
    plan: {
      semantic_query:         out.semantic_query        || scene,
      primary_settings:       out.primary_settings      || [],
      hard_exclude_settings:  out.hard_exclude_settings || [],
      must_terms:             out.must_terms            || [],
      avoid_terms:            out.avoid_terms           || [],
    },
  };
}
```

---

## 10. Migration scripts

### 10.1 `tools/migrate_to_postgres.py`

Imports all existing catalog JSONs into Postgres:

```python
#!/usr/bin/env python3
"""
Import existing asset_db/catalog/ JSON files into Postgres.

Usage:
  ASSET_DB_DIR=/data/siddhant/asset_db \
  POSTGRES_URL=postgresql://USER:PASSWORD@127.0.0.1:55432/asset_db \
  python tools/migrate_to_postgres.py
"""
import os, json, glob, sys
import psycopg2
from psycopg2.extras import Json

ASSET_DB_DIR = os.environ["ASSET_DB_DIR"]
POSTGRES_URL = os.environ["POSTGRES_URL"]

conn = psycopg2.connect(POSTGRES_URL)
cur  = conn.cursor()

catalog_files = glob.glob(f"{ASSET_DB_DIR}/catalog/*/*.json")
print(f"Found {len(catalog_files)} asset JSONs")

for fp in catalog_files:
    try:
        rec  = json.load(open(fp))
        ident = rec.get("identity", {})
        sem   = rec.get("semantic", {})
        geo   = rec.get("geometry", {})
        tech  = rec.get("technical", {})
        idx   = rec.get("indexing", {})
        dims  = geo.get("dimensions_m") or {}
        footp = geo.get("footprint_m") or {}

        cur.execute("""
          INSERT INTO assets (
            asset_id, name, category, subcategory, source_pack,
            setting, style, condition, short_description, description, function,
            scene_types, tags, materials, mood, typical_placement, affordances, color_palette,
            width_m, depth_m, height_m, footprint_w_m, footprint_d_m, bounding_radius_m, is_symmetric,
            unreal_asset_path, asset_type, mobility, has_collision,
            triangle_count, lod_count, material_slots,
            caption_model, render_views, view_count, schema_version,
            raw_metadata
          ) VALUES (
            %s,%s,%s,%s,%s,
            %s,%s,%s,%s,%s,%s,
            %s,%s,%s,%s,%s,%s,%s,
            %s,%s,%s,%s,%s,%s,%s,
            %s,%s,%s,%s,
            %s,%s,%s,
            %s,%s,%s,%s,
            %s
          )
          ON CONFLICT (asset_id) DO UPDATE SET
            name=EXCLUDED.name, raw_metadata=EXCLUDED.raw_metadata,
            updated_at=now()
        """, (
            ident.get("asset_id"),
            ident.get("name"),
            ident.get("category"),
            ident.get("subcategory"),
            ident.get("source_pack"),
            sem.get("setting"),
            sem.get("style"),
            sem.get("condition"),
            sem.get("short_description"),
            sem.get("description"),
            sem.get("function"),
            sem.get("scene_types", []),
            sem.get("tags", []),
            sem.get("materials", []),
            sem.get("mood", []),
            sem.get("typical_placement", []),
            sem.get("affordances", []),
            sem.get("color_palette", []),
            dims.get("width"),
            dims.get("depth"),
            dims.get("height"),
            footp.get("width"),
            footp.get("depth"),
            geo.get("bounding_radius_m"),
            geo.get("is_symmetric"),
            tech.get("unreal_asset_path"),
            tech.get("asset_type"),
            tech.get("mobility"),
            tech.get("has_collision"),
            tech.get("triangle_count"),
            tech.get("lod_count"),
            tech.get("material_slots", []),
            idx.get("caption_model"),
            idx.get("render_views", []),
            idx.get("view_count", 0),
            idx.get("schema_version", "1.0"),
            Json(rec),
        ))
        print(f"  OK: {ident.get('asset_id')}")
    except Exception as e:
        print(f"  ERR {fp}: {e}", file=sys.stderr)
        conn.rollback()
        continue

conn.commit()
print("Import complete.")
```

### 10.2 `tools/build_qdrant_index.py`

Reads from Postgres, computes BGE-M3 dense + sparse embeddings via FastEmbed, upserts to Qdrant.
No OpenAI dependency — runs fully local.

```python
#!/usr/bin/env python3
"""
Build or update Qdrant index from Postgres asset records using BGE-M3 (local, no API key).
Only re-embeds assets whose embedding_hash has changed.

Start embed_service.py first, OR set EMBED_INLINE=1 to run FastEmbed in-process
(slower first call due to model load, but no separate service needed).

Usage:
  POSTGRES_URL=... QDRANT_URL=http://127.0.0.1:6333 python tools/build_qdrant_index.py
"""
import os, hashlib, sys
import psycopg2, psycopg2.extras
from qdrant_client import QdrantClient
from qdrant_client.models import (
    PointStruct, VectorParams, SparseVectorParams, Distance,
    SparseVector, NamedVector, NamedSparseVector
)

POSTGRES_URL = os.environ["POSTGRES_URL"]
QDRANT_URL   = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333")
COLLECTION   = os.environ.get("QDRANT_COLLECTION", "assets")
EMBED_VER    = "bge-m3-v1"
BATCH_SIZE   = 32   # BGE-M3 is heavier than small models; 32 is comfortable

qd = QdrantClient(url=QDRANT_URL)

# Create collection if not exists
existing = [c.name for c in qd.get_collections().collections]
if COLLECTION not in existing:
    qd.create_collection(
        COLLECTION,
        vectors_config={
            "text_dense": VectorParams(size=1024, distance=Distance.COSINE)
        },
        sparse_vectors_config={
            "text_sparse": SparseVectorParams()
        }
    )
    for field, schema in [
        ("category","keyword"),("setting","keyword"),("asset_type","keyword"),
        ("source_pack","keyword"),("scene_types","keyword"),("tags","keyword"),
        ("width_m","float"),("depth_m","float"),("height_m","float"),
        ("triangle_count","integer"),
    ]:
        qd.create_payload_index(COLLECTION, field_name=field, field_schema=schema)
    print(f"Created Qdrant collection: {COLLECTION}")

# Load embedding models (inline — no separate service needed for batch indexing)
print("Loading BGE-M3 (first run downloads ~570MB)...")
from fastembed import TextEmbedding, SparseTextEmbedding
_dense  = TextEmbedding("BAAI/bge-m3")
_sparse = SparseTextEmbedding("BAAI/bge-m3")
print("Model loaded.")

def build_embedding_text(row):
    parts = [
        f"name: {row['name'] or ''}",
        f"category: {row['category'] or ''}",
        f"subcategory: {row['subcategory'] or ''}",
        f"setting: {row['setting'] or ''}",
        f"style: {row['style'] or ''}",
        f"condition: {row['condition'] or ''}",
        f"scene types: {', '.join(row['scene_types'] or [])}",
        f"tags: {', '.join(row['tags'] or [])}",
        f"materials: {', '.join(row['materials'] or [])}",
        f"mood: {', '.join(row['mood'] or [])}",
        f"typical placement: {', '.join(row['typical_placement'] or [])}",
        f"function: {row['function'] or ''}",
        f"affordances: {', '.join(row['affordances'] or [])}",
        f"short description: {row['short_description'] or ''}",
        f"description: {row['description'] or ''}",
    ]
    return " | ".join(p for p in parts if not p.endswith(": "))

def embedding_hash(text):
    return hashlib.sha256((text + "|" + EMBED_VER).encode()).hexdigest()[:16]

def embed_batch(texts):
    dense_vecs  = [v.tolist() for v in _dense.embed(texts)]
    sparse_res  = list(_sparse.embed(texts))
    sparse_vecs = [{"indices": r.indices.tolist(), "values": r.values.tolist()} for r in sparse_res]
    return dense_vecs, sparse_vecs

conn = psycopg2.connect(POSTGRES_URL)
cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute("SELECT * FROM assets ORDER BY asset_id")
rows = cur.fetchall()
print(f"Processing {len(rows)} assets...")

to_upsert = []
for row in rows:
    text = build_embedding_text(row)
    h    = embedding_hash(text)
    if row["embedding_hash"] == h:
        continue  # unchanged — skip
    to_upsert.append((row, text, h))

print(f"Need to embed/upsert: {len(to_upsert)} assets (skipping {len(rows)-len(to_upsert)} unchanged)")

for i in range(0, len(to_upsert), BATCH_SIZE):
    batch       = to_upsert[i:i+BATCH_SIZE]
    texts       = [t for _, t, _ in batch]
    dense_vecs, sparse_vecs = embed_batch(texts)

    points = []
    for (row, _, h), dvec, svec in zip(batch, dense_vecs, sparse_vecs):
        payload = {
            "asset_id":          row["asset_id"],
            "name":              row["name"],
            "category":          row["category"],
            "subcategory":       row["subcategory"] or "",
            "setting":           row["setting"] or "generic",
            "style":             row["style"] or "",
            "scene_types":       row["scene_types"] or [],
            "tags":              row["tags"] or [],
            "materials":         row["materials"] or [],
            "asset_type":        row["asset_type"] or "",
            "source_pack":       row["source_pack"] or "",
            "unreal_asset_path": row["unreal_asset_path"] or "",
            "short_description": row["short_description"] or "",
            "width_m":           float(row["width_m"] or 0),
            "depth_m":           float(row["depth_m"] or 0),
            "height_m":          float(row["height_m"] or 0),
            "triangle_count":    int(row["triangle_count"] or 0),
            "embedding_version": EMBED_VER,
        }
        points.append(PointStruct(
            id=row["asset_id"],
            vector={
                "text_dense":  dvec,
                "text_sparse": SparseVector(indices=svec["indices"], values=svec["values"]),
            },
            payload=payload,
        ))

    qd.upsert(collection_name=COLLECTION, points=points)

    upd_cur = conn.cursor()
    for (row, _, h) in batch:
        upd_cur.execute(
            "UPDATE assets SET embedding_hash=%s, embedding_version=%s, updated_at=now() WHERE asset_id=%s",
            (h, EMBED_VER, row["asset_id"])
        )
    conn.commit()
    print(f"  Upserted {i+len(batch)}/{len(to_upsert)}")

print("Qdrant index build complete.")
```

---

## 11. Shadow mode validation

Before enabling `ASSET_PREFILTER=true`, run a containment test to confirm the prefilter doesn't drop assets the LLM would have selected.

### 11.1 `tools/shadow_test.js`

```js
#!/usr/bin/env node
// Shadow mode: compare prefilter candidates vs full-list LLM selections
// Usage: ASSET_PREFILTER=false node tools/shadow_test.js
const { loadDB, retrieve } = require("../simworld_studio_workspace/web/server/asset-retrieval");
const { prefilterCategory } = require("../simworld_studio_workspace/web/server/asset-retrieval-db");

const TEST_PROMPTS = [
  "A foggy medieval market square outside a gothic cathedral, wooden stalls, candles, wet cobblestone",
  "An industrial harbor loading dock with shipping containers, cranes, and warehouse facades",
  "A rural East Asian temple courtyard with stone lanterns, cherry blossom trees, and wooden shrines",
  "A sci-fi research station interior with control panels, glowing conduits, and metal grating",
  "A winter village market with snow-covered stalls, firewood, and hanging lanterns",
];

async function run() {
  const db = loadDB();
  for (const prompt of TEST_PROMPTS) {
    console.log(`\nPrompt: "${prompt.slice(0, 60)}..."`);
    const result = await retrieve(prompt, { log: () => {} });
    const selectedIds = new Set(result.final.map(f => f.id));

    // also get prefilter candidates for each routed category
    // ... compare intersection
    let totalSelected = selectedIds.size, contained = 0;
    // (Implementation: run prefilterCategory for each routed category, collect candidate ids,
    //  check how many of selectedIds are in the candidate set)

    console.log(`  Selected: ${totalSelected}, containment: ${contained}/${totalSelected} (${(100*contained/totalSelected).toFixed(1)}%)`);
  }
}
run().catch(console.error);
```

**Target:** ≥95% containment before enabling the prefilter.

---

## 12. Implementation order (for Codex)

Work through these in sequence. Each step is independently verifiable.

### Step 1 — Infrastructure
- [ ] Add `docker-compose.yml` (Postgres 16 + Qdrant latest)
- [ ] Run `docker compose up -d`
- [ ] Add env vars to server startup / `.env` file
- [ ] Run `npm install pg @qdrant/js-client-rest` in server dir
- [ ] Run `pip install psycopg2-binary qdrant-client fastembed fastapi uvicorn`
- [ ] Create `tools/embed_service.py` (from section 3.4) and start it: `python tools/embed_service.py`
- [ ] Verify health: `curl http://127.0.0.1:7777/health`

### Step 2 — Postgres schema
- [ ] Create `tools/schema.sql` with the full CREATE TABLE + indexes from section 4
- [ ] Run `psql $POSTGRES_URL -f tools/schema.sql`

### Step 3 — Migrate existing catalog to Postgres
- [ ] Create `tools/migrate_to_postgres.py` (from section 10.1)
- [ ] Run: `ASSET_DB_DIR=/data/siddhant/asset_db POSTGRES_URL=... python tools/migrate_to_postgres.py`
- [ ] Verify: `SELECT count(*), count(distinct category) FROM assets;` → should be ~1209 assets, 26 categories

### Step 4 — Build Qdrant index
- [ ] Create `tools/build_qdrant_index.py` (from section 10.2)
- [ ] Run: `POSTGRES_URL=... QDRANT_URL=... python tools/build_qdrant_index.py`  (no API key needed — BGE-M3 runs locally)
- [ ] Verify: Qdrant collection has 1209 points with `text_dense` vectors

### Step 5 — asset-retrieval-db.js
- [ ] Create `simworld_studio_workspace/web/server/asset-retrieval-db.js` (from section 9.1)
- [ ] Include `SETTING_COMPAT` map (from section 7)
- [ ] Quick test: `node -e "require('./asset-retrieval-db').prefilterCategory('buildings', {semantic_query:'medieval market', primary_settings:['medieval'], hard_exclude_settings:['sci_fi']}).then(r => console.log(r.length, r[0].name))"`

### Step 6 — Extend router output
- [ ] Modify `routeCategories()` in `asset-retrieval.js` to return `{categories, plan}` (section 9.3)
- [ ] Backward-compat: `retrieve()` should handle both old shape (array) and new shape (object)

### Step 7 — Wire prefilter into retrieve()
- [ ] Modify `retrieve()` in `asset-retrieval.js` to check `ASSET_PREFILTER` and call `prefilterCategory()` (section 9.2)
- [ ] Graceful fallback: any Qdrant/Postgres error → falls back to full category list

### Step 8 — Shadow mode validation
- [ ] Keep `ASSET_PREFILTER=false`
- [ ] Write `tools/shadow_test.js` and run against the 26 test prompts
- [ ] Log: per-category candidate count and containment percentage
- [ ] Target: ≥95% containment on all test prompts

### Step 9 — Enable prefilter
- [ ] Set `ASSET_PREFILTER=true` in server env
- [ ] Monitor logs for `prefilter <cat>: N candidates (from M)` lines
- [ ] Monitor for fallback lines indicating Qdrant errors

### Step 10 — Index future assets (keep in sync)
- [ ] Preferred for the full one-time pass: use `tools/launch_full_asset_index.sh`, which runs
  `tools/full_asset_index_runner.py` detached and performs periodic Postgres/Qdrant syncs.
- [ ] For manual/small indexing with `index_assets.py`, run `tools/migrate_to_postgres.py` and then
  `tools/build_qdrant_index.py`.
- [ ] Both sync scripts support `--asset-id-file` for incremental runs.
- [ ] `build_qdrant_index.py` only re-embeds assets with changed `embedding_hash` — safe to run incrementally.

---

## 13. What does NOT change

- `selectInCategory()` function signature and prompt — unchanged
- `aggregate()` function — unchanged
- `buildPromptBlock()` — unchanged
- `formatAssetsForPrompt()` — unchanged
- The compact object shape passed to the LLM select step — unchanged
- The output format (spawn paths, spawn tools, etc.) — unchanged
- `_spawnTool()` logic — unchanged
- The in-memory `loadDB()` cache — still used as fallback and for non-prefiltered mode

---

## 14. Candidate budget

Start wide. Only shrink after shadow tests pass:

| Phase | Per-category candidates | Total across all categories |
|---|---|---|
| Shadow / initial | 150-200 | up to 1500 |
| After containment test | 100-150 | up to 1000 |
| After quality tuning | 50-100 | up to 600 |

Controlled via `PREFILTER_TOP_K` env var (default: 150).

---

## 15. Future: image embeddings (phase 2)

After text retrieval is stable, add image vectors:
- Use OpenCLIP or SigLIP over the 8 orbit renders per asset
- Average all 8 into one `image` vector (1024-dim)
- Add as a second named vector in Qdrant: `"image": VectorParams(size=1024, distance=Cosine)`
- Fuse with text_dense via RRF in the Qdrant hybrid query
- Weight: text 1.0, image 0.3

Not needed for the first working version.

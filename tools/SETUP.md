# Asset DB tooling — setup guide

Scripts in this directory build and maintain the asset catalog used by `asset-retrieval.js`.

Python dependencies are managed with the checked-in `pyproject.toml` and
`uv.lock`. Install them with `uv sync --project tools --frozen`; do not use
system `pip`. Production snapshot provisioning and verification are documented
in `docs/specs/production-readiness/asset-stack-operations.md`.

---

## Before running: things you must configure

### 1. `web/mcp.json`
Points the MCP server at this repo and at the UE MCP port.

```json
{
  "mcpServers": {
    "simworld": {
      "args": ["<ABSOLUTE_PATH_TO_THIS_REPO>/simworld_studio_workspace/web/server/mcp-server.js"],
      "env": {
        "UNREAL_HOST": "127.0.0.1",
        "UNREAL_PORT": "<YOUR_MCP_PORT>"  // e.g. 55571
      }
    }
  }
}
```

### 2. `simworld_studio_workspace/cirrus-config.json`
Pixel Streaming ports — change if they conflict with other instances on your machine.

```json
{
  "HttpPort":     8687,   // change if port is taken
  "StreamerPort": 8688,
  "SFUPort":      8990
}
```

### 3. `ASSET_DB_DIR` env var (for the indexer + retrieval server)
The retrieval module (`asset-retrieval.js`) reads:
```
process.env.ASSET_DB_DIR  (fallback: /data/siddhant/asset_db)
```
Set it to wherever your asset DB lives, e.g.:
```bash
export ASSET_DB_DIR=/data/yourname/asset_db
```

---

## Indexing workflow

### Step 1 — Prepare a manifest
Create a manifest JSON listing the assets to index:
```json
{
  "schema_version": "1.0",
  "assets": [
    {
      "asset_id": "unique_snake_case_id",
      "ue_name": "BP_MyProp",
      "ue_path": "/Game/MyPack/Blueprints/BP_MyProp.BP_MyProp",
      "asset_type": "Blueprint",
      "source_pack": "MyPack"
    }
  ]
}
```
`asset_type` must be `"Blueprint"` or `"StaticMesh"`.

### Step 2 — Run the indexer
```bash
export ASSET_DB_DIR=/data/yourname/asset_db
export UE_PROJECT=/data/yourname/simworld_studio_projects  # or set UE_SHOTDIR directly
export MANIFEST=/path/to/your_manifest.json
export MCP_PORT=55571

uv run --project tools --frozen python tools/index_assets.py
```
The indexer is resumable — re-run after a crash and it skips already-indexed assets.

### Step 3 — Rebuild the category index
```bash
uv run --project tools --frozen python tools/build_category_index.py
```
This regenerates `$ASSET_DB_DIR/category_index.json`, which `asset-retrieval.js` loads at startup.
Run it after every indexing session.

---

## Environment variable reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ASSET_DB_DIR` | yes | — | Root of the asset DB directory |
| `UE_SHOTDIR` | yes* | derived | Where UE writes HighResShots |
| `UE_PROJECT` | *if no UE_SHOTDIR | — | UE project root; derives `UE_SHOTDIR` automatically |
| `MCP_PORT` | no | 55571 | TCP port of the UE MCP server |
| `CODEX_MODEL` | no | gpt-5.5 | VLM model for asset captioning |
| `MANIFEST` | no | `$ASSET_DB_DIR/manifest.json` | Asset list to index |
| `VLM_SCHEMA` | no | `$ASSET_DB_DIR/schema/vlm_output_schema.json` | JSON schema enforced on VLM output |
| `N_VIEWS` | no | 8 | Orbit views rendered per asset |
| `RES` | no | 1024 | Render resolution (square) |

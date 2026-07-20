# Semantic asset stack operations

This runbook turns the code-only asset retrieval foundation into a versioned,
fail-closed deployment. It does not authorize a database migration, full index
build, model download, production service restart, or UE mutation. Those remain
Admin/Data/State-change gates.

## 1. Receipt contract

`tools/verify_asset_snapshot.py` is the only supported writer for a production
`simworld-asset-snapshot/v1` receipt. It audits these live facts before writing:

- the complete `catalog/**/*.json` corpus is valid, has unique asset IDs, and
  exactly matches `category_index.json`;
- the canonical catalog checksum and count;
- PostgreSQL `assets` row count and the integer schema revision recorded in
  `simworld_schema_metadata`;
- every PostgreSQL asset row uses the live embedding recipe version;
- Qdrant collection health, point count, named dense vector size, named sparse
  vector presence, and an exact filtered count proving every point carries the
  live embedding recipe/model revisions;
- embedding service health, loaded-state, recipe version, model IDs, and model
  artifact revisions;
- an operator-supplied UE Content revision.

Catalog, PostgreSQL, and Qdrant counts must be identical. The embedding dense
size must equal the Qdrant dense vector size. A missing field, dependency error,
partial embedding revision, stale category index, or mismatch exits non-zero.
The tool uses a temporary file plus `fsync`/rename, so it never leaves a valid-
looking partial receipt.

The catalog checksum is SHA-256 over the catalog records sorted by POSIX
relative path. Each path and canonical JSON record is length-framed and the
digest is domain-separated with `simworld-catalog-canonical-json/v1`. JSON
whitespace changes therefore do not change a snapshot, while path or content
changes do.

## 2. Reproducible Python environment

Do not use system `pip`. The complete asset-tool dependency graph is locked in
`tools/uv.lock` for Python 3.10–3.12.

```bash
uv sync --project tools --frozen
uv run --project tools python tools/verify_asset_snapshot.py --help
```

Updating dependencies is a separate reviewed change: edit
`tools/pyproject.toml`, run `uv lock --project tools`, inspect the diff, and
rerun all tool tests. A snapshot operation must use `--frozen`.

## 3. Loopback service profile

PostgreSQL, Qdrant, and embedding ports remain bound to `127.0.0.1`. The
embedding container is enabled by the `asset-stack` Compose profile and uses
the frozen uv lock. Supply approved pinned image tags or digests and exact
model revisions through an admin-managed environment/secret store:

```bash
export POSTGRES_IMAGE='<approved postgres tag or digest>'
export QDRANT_IMAGE='<approved qdrant tag or digest>'
export ASSET_TOOLS_PYTHON_IMAGE='<approved python tag or digest>'
export ASSET_TOOLS_UV_IMAGE='<approved uv tag or digest>'

export POSTGRES_PASSWORD='<secret-store value>'
export EMBED_DENSE_MODEL='BAAI/bge-large-en-v1.5'
export EMBED_DENSE_REVISION='<verified model artifact revision>'
export EMBED_SPARSE_MODEL='Qdrant/bm25'
export EMBED_SPARSE_REVISION='<verified model artifact revision>'
export EMBED_DENSE_SIZE='1024'
export EMBED_VERSION='bge-large-en-v1.5-bm25-v1'

docker compose --profile asset-stack config --quiet
```

`docker compose ... config` is read-only. Running `build` or `up` can pull
images, download/preload models, create persistent volumes, and change service
state; obtain the corresponding approval first. The Compose healthcheck becomes
healthy only after both embedding models load successfully. The model cache is
kept in the `embedding_model_cache` volume.

## 4. Provisioning order (state-changing gate)

After backups, storage capacity, image/model provenance, and an execution
window are approved:

1. Start the pinned loopback asset stack.
2. Apply `tools/schema.sql`; this creates/updates the `asset_catalog` schema
   revision used by the verifier.
3. Dry-run catalog migration and inspect counts.
4. Run the approved PostgreSQL migration.
5. Dry-run the Qdrant build and inspect the pending count.
6. Build Qdrant using the exact embedding configuration above.
7. Probe at least one Blueprint and one StaticMesh in the matching UE Content.
8. Capture and immediately re-verify the snapshot receipt.

Representative commands (do not run them before the gate):

```bash
POSTGRES_URL='<secret-store injected>' \
  uv run --project tools --frozen python tools/apply_schema.py

ASSET_DB_DIR=/srv/simworld/asset-db \
POSTGRES_URL='<secret-store injected>' \
  uv run --project tools --frozen python tools/migrate_to_postgres.py --dry-run

POSTGRES_URL='<secret-store injected>' \
QDRANT_URL=http://127.0.0.1:6333 \
  uv run --project tools --frozen python tools/build_qdrant_index.py --dry-run
```

Never put `POSTGRES_URL`, Qdrant API keys, or embedding bearer tokens in argv,
logs, receipts, or shell history. The verifier accepts them only from its
process environment.

## 5. Capture and verify

Set the operator-observed UE Content revision to the immutable build/content
identifier used by the running Unreal project. A branch name such as `main` is
not sufficient.

```bash
export ASSET_DB_DIR=/srv/simworld/asset-db
export POSTGRES_URL='<secret-store injected>'
export QDRANT_URL=http://127.0.0.1:6333
export QDRANT_COLLECTION=assets-v1
export EMBED_SERVICE_URL=http://127.0.0.1:7777
export UE_CONTENT_REVISION='ue-content-<immutable-revision>'

uv run --project tools --frozen python tools/verify_asset_snapshot.py capture \
  --output "$ASSET_DB_DIR/snapshot-manifest.json"

uv run --project tools --frozen python tools/verify_asset_snapshot.py verify \
  --manifest "$ASSET_DB_DIR/snapshot-manifest.json"
```

Use `--replace` only for an intentional atomic snapshot transition. A successful
verification prints a `simworld-asset-snapshot-audit/v1` result containing the
verified `snapshot_id`. Only then may the deployment set:

```bash
export ASSET_SNAPSHOT_REVISION='<verified snapshot_id>'
export ASSET_READINESS_VERIFIED_REVISION='<verified snapshot_id>'
```

Any catalog, DB, index, model, vector schema, or UE Content change invalidates
the receipt. Rebuild/verify a new snapshot and switch the revision only after
shadow queries and real UE spawn smoke pass.

`build_qdrant_index.py` stores `embedding_version`, dense/sparse model IDs,
dense/sparse immutable revisions, and dense size in every point payload. A
revision change invalidates its embedding hash and forces an upsert; legacy
points without these fields cannot pass snapshot verification.

## 6. Remaining administrator/data gates

- provide the authoritative, complete catalog and its matching UE Content
  revision; partial indexes cannot be marked ready;
- approve persistent storage, backup/restore policy, retention, and rollback;
- provide pinned container image references and model artifact revisions;
- inject PostgreSQL/Qdrant/embedding credentials through the service secret
  mechanism and rotate any historical credentials that appeared in logs;
- approve model cache population and the full embedding/index build budget;
- approve and execute Blueprint + StaticMesh material/PBR spawn smoke in a
  disposable writable UE scene.

Until these gates pass, the correct production state is `not_ready`; the scene
builder must not silently fall back to Cube or other basic geometry.

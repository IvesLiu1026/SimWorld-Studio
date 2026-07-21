# Semantic asset stack operations

This runbook turns the code-only asset retrieval foundation into a versioned,
fail-closed deployment. It does not authorize a database migration, full index
build, model download, production service restart, or UE mutation. Those remain
Admin/Data/State-change gates.

## 1. Snapshot and live-audit contracts

The operator must choose a unique immutable `ASSET_SNAPSHOT_REVISION` before
migrating any row. It is the `assets.asset_snapshot_revision` value in
PostgreSQL, the `asset_snapshot_revision` payload value in every Qdrant point,
and the `snapshot_id` in the `simworld-asset-snapshot/v1` manifest. Values such
as `main`, `latest`, or `unversioned` are rejected. Never reuse a revision after
changing the catalog, UE Content, embedding recipe, or index.

`tools/verify_asset_snapshot.py` is the only supported writer for a production
manifest and its short-lived live-audit receipt. It audits these live facts:

- the complete `catalog/**/*.json` corpus is valid, has unique asset IDs, and
  exactly matches `category_index.json`;
- the canonical catalog checksum and count;
- PostgreSQL `assets` row count, exact schema revision `2`, and the integer
  schema revision recorded in `simworld_schema_metadata`;
- every PostgreSQL asset row has the exact operator-selected
  `asset_snapshot_revision`;
- every PostgreSQL asset row uses the live embedding recipe version;
- Qdrant collection health, point count, named dense vector size, named sparse
  vector presence, and an exact filtered count proving every point carries the
  operator snapshot revision and live embedding recipe/model revisions;
- embedding service health, loaded-state, recipe version, model IDs, and model
  artifact revisions;
- an operator-supplied UE Content revision.

Catalog, PostgreSQL, and Qdrant counts must be identical. The embedding dense
size must equal the Qdrant dense vector size. A missing field, dependency error,
partial embedding revision, stale category index, or mismatch exits non-zero.
The static manifest remains deterministic evidence. Each successful `capture`
or `verify` also atomically writes a separate
`simworld-asset-live-audit/v1` receipt with exactly these fields:

```text
schema, snapshot_id, manifest_sha256, observations_sha256,
issued_at, expires_at, ttl_seconds, observations
```

`observations` contains `asset_snapshot_revision`, the manifest's UE revision,
catalog facts, PostgreSQL facts, Qdrant facts, and embedding facts. The receipt is bound to the exact
manifest file bytes with `manifest_sha256`, not merely to equivalent parsed
JSON. Its default TTL is 300 seconds and the maximum is 900 seconds. A receipt
with a different snapshot, manifest digest, observation digest, validity
window, future issuance time, or expired `expires_at` is invalid. The command
prints both `manifest_sha256` and `live_audit_receipt_sha256`; the latter is the
digest a consuming runtime must pin. Neither file contains DSNs, service URLs,
API keys, bearer tokens, or other credentials.

Both files use a same-directory temporary file, `fsync`, atomic link/rename,
and directory `fsync`. A failed audit writes neither a valid live receipt nor a
ready result. A crash after a new manifest but before its receipt is also fail-
closed because no digest-bound, unexpired receipt exists for that manifest.

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
export ASSET_SNAPSHOT_REVISION='asset-snapshot-<immutable-operator-revision>'

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
2. Apply `tools/schema.sql`; this stages PostgreSQL asset schema revision `2`
   and adds `assets.asset_snapshot_revision`. Existing v1 rows remain
   intentionally not-ready until migrated.
3. Dry-run catalog migration and inspect counts.
4. Run the approved PostgreSQL migration.
5. Dry-run the Qdrant build and inspect the pending count.
6. Build Qdrant using the exact embedding configuration above.
7. Probe at least one Blueprint and one StaticMesh in the matching UE Content.
8. Capture and immediately re-verify the snapshot receipt.

Representative commands (do not run them before the gate):

```bash
POSTGRES_URL='<secret-store injected>' \
ASSET_SNAPSHOT_REVISION='asset-snapshot-<immutable-operator-revision>' \
  uv run --project tools --frozen python tools/apply_schema.py

ASSET_DB_DIR=/srv/simworld/asset-db \
POSTGRES_URL='<secret-store injected>' \
ASSET_SNAPSHOT_REVISION='asset-snapshot-<immutable-operator-revision>' \
  uv run --project tools --frozen python tools/migrate_to_postgres.py --dry-run

POSTGRES_URL='<secret-store injected>' \
QDRANT_URL=http://127.0.0.1:6333 \
ASSET_SNAPSHOT_REVISION='asset-snapshot-<immutable-operator-revision>' \
  uv run --project tools --frozen python tools/build_qdrant_index.py --dry-run
```

The PostgreSQL dry-run is offline with respect to PostgreSQL: it validates the
revision and resolves catalog files without connecting. The Qdrant dry-run is
read-only but does connect to PostgreSQL and Qdrant to calculate the pending
set; it does not create collections/indexes, load embedding models, or upsert
points. Run it only inside the approved loopback maintenance window.

The migration refuses missing requested IDs, the wrong schema version, an
empty selection, a full-catalog/table count mismatch, or a table containing a
different snapshot revision. Once the complete table matches, it validates the
staged check constraint and makes the column `NOT NULL`. The indexer refuses
PostgreSQL rows from another revision and will not reuse a Qdrant point whose
`asset_id` or `asset_snapshot_revision` payload differs. A revision change is
part of the embedding hash and therefore forces an upsert.

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
export ASSET_SNAPSHOT_REVISION='asset-snapshot-<immutable-operator-revision>'

uv run --project tools --frozen python tools/verify_asset_snapshot.py capture \
  --output "$ASSET_DB_DIR/snapshot-manifest.json" \
  --receipt-output "$ASSET_DB_DIR/snapshot-live-audit.json" \
  --receipt-ttl-seconds 300

uv run --project tools --frozen python tools/verify_asset_snapshot.py verify \
  --manifest "$ASSET_DB_DIR/snapshot-manifest.json" \
  --receipt-output "$ASSET_DB_DIR/snapshot-live-audit.json" \
  --receipt-ttl-seconds 300 \
  --replace
```

Use `--replace` only for an intentional atomic snapshot transition. A successful
verification prints a `simworld-asset-snapshot-audit/v1` result containing the
verified `snapshot_id`, exact manifest digest, live receipt digest, and expiry.
Record the complete JSON result in the approved deployment evidence. Only then
may the deployment set the readiness revision to the already-selected snapshot:

```bash
export ASSET_READINESS_VERIFIED_REVISION='<verified snapshot_id>'
export ASSET_LIVE_AUDIT_RECEIPT_SHA256='<live_audit_receipt_sha256>'
```

The live receipt is deliberately short-lived. Runtime/startup integration must
validate its exact schema, raw-file SHA-256, manifest binding, snapshot binding,
observation digest, and expiry before claiming ready. A static
`ASSET_READINESS_VERIFIED_REVISION` alone is not fresh live evidence.

Any catalog, DB, index, model, vector schema, or UE Content change invalidates
the receipt. Rebuild/verify a new snapshot and switch the revision only after
shadow queries and real UE spawn smoke pass.

`build_qdrant_index.py` stores `asset_snapshot_revision`, `embedding_version`, dense/sparse model IDs,
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

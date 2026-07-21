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
uv run --project tools --frozen python tools/verify_asset_snapshot.py --help
```

Updating dependencies is a separate reviewed change: edit
`tools/pyproject.toml`, run `uv lock --project tools`, inspect the diff, and
rerun all tool tests. A snapshot operation must use `--frozen`.

### 2.1 Deterministic offline deployment preflight

Before any container, database, model, or index operation, run the production
preflight. It reuses the exact catalog validator from
`verify_asset_snapshot.py`, but it does **not** open a socket, import rows,
load a model, write catalog state, or mutate a service (an explicit `--output`
only writes the result). It rejects unpinned
images, generic revisions/collection names, non-loopback URLs, a DSN in the
environment, symlinked or group-readable secret files, UE revision drift, an
incomplete category index, and an incomplete backup policy.

The following variables are required in addition to the service variables in
the next section:

```bash
export ASSET_STACK_PROFILE=local        # or aws
export POSTGRES_DB=asset_db
export POSTGRES_USER=simworld
export POSTGRES_URL_FILE=/run/secrets/postgres_url
export QDRANT_API_KEY_FILE=/run/secrets/qdrant_api_key
export EMBED_SERVICE_TOKEN_FILE=/run/secrets/embed_service_token
export ASSET_BACKUP_ROOT=/srv/backups/simworld/asset-stack
export ASSET_BACKUP_RETENTION_DAYS=30
export ASSET_BACKUP_MIN_FREE_BYTES=107374182400
export VISTA_UE_CONTENT_REVISION="$UE_CONTENT_REVISION"
```

All three credential files must be root/service-owned regular files with mode
`0600` and exactly one line. The preflight reads the DSN only to verify its
loopback host and database and validates that the Qdrant/embedding credentials
contain at least 32 bytes. No credential value or digest is written to the
result.

```bash
uv run --project tools --frozen python tools/asset_stack_preflight.py \
  --output /srv/simworld/asset-db/deployment-preflight.json
```

The output is deterministic: the same safe configuration and catalog bytes
produce the same `config_sha256` and plan. `ready_for_admin_gates` means only
that offline inputs are coherent. It does not mean PostgreSQL, Qdrant,
embedding, UE Content, backups, or restore have been observed live.

## 3. Loopback service profile

PostgreSQL, Qdrant, and embedding ports remain bound to `127.0.0.1`. The
embedding container is enabled by the `asset-stack` Compose profile and uses
the frozen uv lock. Supply reviewed image content digests and exact
model revisions through an admin-managed environment/secret store:

```bash
export POSTGRES_IMAGE='postgres@sha256:<approved-digest>'
export QDRANT_IMAGE='qdrant/qdrant@sha256:<approved-digest>'
export ASSET_TOOLS_PYTHON_IMAGE='python@sha256:<approved-digest>'
export ASSET_TOOLS_UV_IMAGE='ghcr.io/astral-sh/uv@sha256:<approved-digest>'
export EMBED_SERVICE_IMAGE='<published embedding image@sha256:digest>'

export EMBED_DENSE_MODEL='BAAI/bge-large-en-v1.5'
export EMBED_DENSE_MODEL_DIR_HOST='/opt/simworld-models/dense/<revision>'
export EMBED_DENSE_REVISION='sha256:<artifact-manifest-digest>'
export EMBED_SPARSE_MODEL='Qdrant/bm25'
export EMBED_SPARSE_MODEL_DIR_HOST='/opt/simworld-models/sparse/<revision>'
export EMBED_SPARSE_REVISION='sha256:<artifact-manifest-digest>'
export EMBED_DENSE_SIZE='1024'
export EMBED_VERSION='bge-large-en-v1.5-bm25-v1'
export ASSET_SNAPSHOT_REVISION='asset-snapshot-<immutable-operator-revision>'
export UE_CONTENT_REVISION='ue-content-<immutable-revision>'
export VISTA_UE_CONTENT_REVISION="$UE_CONTENT_REVISION"

export POSTGRES_PASSWORD_FILE_HOST='/etc/simworld/secrets/postgres_password'
export POSTGRES_URL_FILE='/etc/simworld/secrets/postgres_url'
export QDRANT_API_KEY_FILE='/etc/simworld/secrets/qdrant_api_key'
export EMBED_SERVICE_TOKEN_FILE_HOST='/etc/simworld/secrets/embed_service_token'
export EMBED_SERVICE_TOKEN_FILE="$EMBED_SERVICE_TOKEN_FILE_HOST"

docker compose --profile asset-stack config --quiet
```

The two model revisions are not operator labels. Each must be the raw SHA-256
of a local `artifact-manifest.json` that enumerates every model/tokenizer file,
size, and checksum. After an approved model download into an isolated
directory, capture and re-verify it offline:

```bash
uv run --project tools --frozen python tools/embedding_model_artifact.py capture \
  --model-dir "$EMBED_DENSE_MODEL_DIR_HOST" \
  --kind dense --model-id "$EMBED_DENSE_MODEL" \
  --dense-size "$EMBED_DENSE_SIZE"

uv run --project tools --frozen python tools/embedding_model_artifact.py capture \
  --model-dir "$EMBED_SPARSE_MODEL_DIR_HOST" \
  --kind sparse --model-id "$EMBED_SPARSE_MODEL"
```

Set each `EMBED_*_REVISION` to the returned `sha256:...` value. The preflight,
embedding service, and Qdrant indexer all re-hash the exact file set and reject
symlinks, extras, missing files, wrong dimensions, or checksum drift. FastEmbed
is invoked with `specific_model_path` and `local_files_only=True`; a production
query or index build cannot silently download a newer artifact by model ID.
When running the Qdrant indexer directly on the host, also set
`EMBED_DENSE_MODEL_PATH="$EMBED_DENSE_MODEL_DIR_HOST"` and the corresponding
sparse path. Compose maps the reviewed host directories read-only to fixed
container paths.

`docker compose ... config` is read-only. The local file and the AWS compose
profile use the same loopback defaults: PostgreSQL `55432`, Qdrant HTTP/gRPC
`6333/6334`, and embedding `7777`. PostgreSQL consumes a mounted password file,
not `POSTGRES_PASSWORD`; Qdrant has an HTTP `/readyz` healthcheck; embedding is
healthy only after both exact model artifacts load and the dense dimension
matches. The embedding `/embed` endpoint requires the same file-backed bearer
token used by the Web retrieval client. All three services have persistent
storage and `unless-stopped` restart policy.

The Qdrant Web client sends the file-backed API key, but the exact pinned
Qdrant image must also be configured by the administrator to enforce that same
key. Do not mark the stack ready merely because the client credential is
mounted: record an unauthorized `401/403` probe and an authorized `/readyz`
and query probe against the pinned image. How the server consumes its key is
image/version-specific and remains an explicit Admin Gate; do not copy the key
into Compose interpolation, argv, or a committed YAML file.

The production compose files consume a pre-built immutable embedding image.
Building `tools/Dockerfile.embedding`, pulling base images, populating the
model cache, or starting containers is state-changing and requires approval.
After an approved build, publish the image and set
`EMBED_SERVICE_IMAGE=name@sha256:<content-digest>`; do not deploy a local
mutable tag.

For AWS, first populate the host paths and secrets documented in
`deploy/aws/templates/simworld.env`, then validate the combined profile:

```bash
docker compose -f deploy/aws/docker/docker-compose.yml \
  --profile asset-stack config --quiet
```

Running either `up` command can pull images, create storage, and download
models. It remains an Admin/State-change Gate.

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
POSTGRES_URL_FILE='/run/secrets/postgres_url' \
QDRANT_API_KEY_FILE='/run/secrets/qdrant_api_key' \
ASSET_SNAPSHOT_REVISION='asset-snapshot-<immutable-operator-revision>' \
  uv run --project tools --frozen python tools/apply_schema.py

ASSET_DB_DIR=/srv/simworld/asset-db \
ASSET_SNAPSHOT_REVISION='asset-snapshot-<immutable-operator-revision>' \
  uv run --project tools --frozen python tools/migrate_to_postgres.py --dry-run

POSTGRES_URL_FILE='/run/secrets/postgres_url' \
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
logs, receipts, or shell history. Production tooling supports
`POSTGRES_URL_FILE`, `QDRANT_API_KEY_FILE`, and
`EMBED_SERVICE_TOKEN_FILE`, rejects conflicting inline/file sources, opens
files with `O_NOFOLLOW`, and requires mode `0600`. Direct environment values
remain supported only for a bounded administrator invocation where a secret
delivery agent cannot mount a file. The migration, database-provision, and
Qdrant-builder CLIs no longer accept `--postgres-url`.

## 5. Capture and verify

Set the operator-observed UE Content revision to the immutable build/content
identifier used by the running Unreal project. A branch name such as `main` is
not sufficient.

```bash
export ASSET_DB_DIR=/srv/simworld/asset-db
export POSTGRES_URL_FILE='/run/secrets/postgres_url'
export QDRANT_API_KEY_FILE='/run/secrets/qdrant_api_key'
export QDRANT_URL=http://127.0.0.1:6333
export QDRANT_COLLECTION=assets-v1
export EMBED_SERVICE_URL=http://127.0.0.1:7777
export EMBED_SERVICE_TOKEN_FILE='/run/secrets/embed_service_token'
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
export ASSET_LIVE_AUDIT_RECEIPT="$ASSET_DB_DIR/snapshot-live-audit.json"
export ASSET_LIVE_AUDIT_RECEIPT_SHA256='<live_audit_receipt_sha256>'
export POSTGRES_URL_FILE='/run/secrets/postgres_url'
export QDRANT_API_KEY_FILE='/run/secrets/qdrant_api_key'
export EMBED_SERVICE_TOKEN_FILE='/run/secrets/embed_service_token'
```

For the long-running Web/MCP service, use root-managed mode-`0600`
`POSTGRES_URL_FILE`, `QDRANT_API_KEY_FILE`, and
`EMBED_SERVICE_TOKEN_FILE`; never set a direct value together with its file
source. The runtime opens each with `O_NOFOLLOW`, checks regular-file type,
owner, permissions, stable size/metadata, bounded UTF-8, and exactly one line.
It keeps values non-enumerable in verified runtime config and passes them only
in memory to the PostgreSQL pool, Qdrant client, and embedding Authorization
header. Readiness and startup status expose only source names, never a value or
path. Bounded admin CLI invocations use the same file-backed sources.

The live receipt is deliberately short-lived. Runtime/startup integration must
validate its exact schema, raw-file SHA-256, manifest binding, snapshot binding,
observation digest, and expiry before claiming ready. A static
`ASSET_READINESS_VERIFIED_REVISION` alone is not fresh live evidence.
The Web runtime repeats the expiry check before every semantic search, so a
process that outlives the pinned receipt fails closed before contacting
Qdrant/PostgreSQL. Refresh the receipt through the read-only verifier, update
the pinned digest through the deployment secret/config mechanism, and restart
or roll the Web process before the old receipt expires.

Any catalog, DB, index, model, vector schema, or UE Content change invalidates
the receipt. Rebuild/verify a new snapshot and switch the revision only after
shadow queries and real UE spawn smoke pass.

`build_qdrant_index.py` stores `asset_snapshot_revision`, `embedding_version`, dense/sparse model IDs,
dense/sparse immutable revisions, and dense size in every point payload. A
revision change invalidates its embedding hash and forces an upsert; legacy
points without these fields cannot pass snapshot verification.

## 6. Backup bundle and disposable restore drill

Provisioning is not complete until PostgreSQL and Qdrant backups have been
restored into disposable targets and the restored stack passes the same live
snapshot audit. During an approved backup window, create one directory beneath
`ASSET_BACKUP_ROOT` containing exactly these roles:

- `postgres_dump`: custom-format `pg_dump`, using `PGPASSFILE` and separate
  `PGHOST`, `PGPORT`, `PGUSER`, and `PGDATABASE` variables so no DSN is argv;
- `qdrant_snapshot`: downloaded snapshot of the exact pinned collection;
- `asset_snapshot_manifest`: the verified static snapshot manifest;
- `embedding_cache_manifest`: checksums/model revisions for the deployed model
  artifacts (not an unbounded cache directory claim);
- `ue_content_manifest`: immutable UE Content build/revision receipt.

PostgreSQL dump, Qdrant snapshot creation/download, cache inventory, and UE
inventory are live/storage operations and require the Admin/Data Gate. Once
those files exist, manifest capture and verification are offline:

```bash
BUNDLE=/srv/backups/simworld/asset-stack/asset-backup-<immutable-id>

uv run --project tools --frozen python tools/asset_stack_backup_bundle.py capture \
  --bundle-dir "$BUNDLE" \
  --backup-id asset-backup-<immutable-id> \
  --snapshot-revision "$ASSET_SNAPSHOT_REVISION" \
  --preflight-sha256 '<deployment-preflight config_sha256>' \
  --postgres-dump "$BUNDLE/postgres.dump" \
  --qdrant-snapshot "$BUNDLE/qdrant.snapshot" \
  --asset-snapshot-manifest "$BUNDLE/snapshot-manifest.json" \
  --embedding-cache-manifest "$BUNDLE/embedding-cache-manifest.json" \
  --ue-content-manifest "$BUNDLE/ue-content-manifest.json" \
  --output "$BUNDLE/backup-manifest.json"

uv run --project tools --frozen python tools/asset_stack_backup_bundle.py verify \
  --manifest "$BUNDLE/backup-manifest.json"

uv run --project tools --frozen python tools/asset_stack_backup_bundle.py restore-plan \
  --manifest "$BUNDLE/backup-manifest.json"
```

The bundle manifest contains relative paths, sizes, and SHA-256 digests. It
rejects symlinks, path escape, changed files, missing roles, and a mismatch to
the exact deployment preflight. `restore-plan` deliberately does not perform a
restore. An administrator must approve and execute its ordered steps against
empty disposable PostgreSQL/Qdrant targets, then run:

```bash
uv run --project tools --frozen python tools/verify_asset_snapshot.py capture \
  --output "$BUNDLE/restored-snapshot-manifest.json" \
  --receipt-output "$BUNDLE/restored-live-audit.json"
```

The drill passes only when the restored catalog/PostgreSQL/Qdrant/model counts,
revisions, vectors and checksums match, representative Chinese/English shadow
queries pass, and the disposable UE Blueprint + StaticMesh PBR smoke passes.
Record RTO/RPO, commands, service/image digests, and cleanup evidence. Merely
capturing or verifying a backup bundle does not satisfy the restore gate.

## 7. Remaining administrator/data gates

- provide the authoritative, complete catalog and its matching UE Content
  revision; partial indexes cannot be marked ready;
- approve persistent storage, backup/restore policy, retention, and rollback;
- provide pinned container image references and model artifact revisions;
- provide complete local dense/sparse artifact directories, capture their
  immutable manifests, and approve the one-time model download/cache budget;
- inject PostgreSQL/Qdrant/embedding credentials through the service secret
  mechanism, configure the pinned Qdrant server to enforce its key, prove
  unauthorized access is denied, and rotate any historical credentials that
  appeared in logs;
- approve model cache population and the full embedding/index build budget;
- approve and execute Blueprint + StaticMesh material/PBR spawn smoke in a
  disposable writable UE scene.
- execute and record one disposable PostgreSQL/Qdrant restore drill using the
  exact backup manifest and re-run the live snapshot audit.

Until these gates pass, the correct production state is `not_ready`; the scene
builder must not silently fall back to Cube or other basic geometry.

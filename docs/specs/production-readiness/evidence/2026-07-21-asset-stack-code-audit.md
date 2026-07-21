# Semantic asset stack code/ops audit — 2026-07-21

Scope: T1B.2, T1B.5, and T1B.6 code/operations readiness. No database,
Qdrant, embedding service, canonical catalog, NAS path, UE scene, container, or
remote endpoint was opened or modified for this audit.

## Closed code-only gaps

- Added a zero-network deterministic deployment preflight. It reuses the
  canonical catalog/index validator and binds image/model/content/snapshot,
  endpoint, secret-delivery, and backup-policy configuration to one digest.
- Added file-backed secret loading with `O_NOFOLLOW`, regular-file/owner/mode
  checks, bounded size, conflict rejection, and no durable secret output.
- Removed the PostgreSQL DSN argv surface from migration, Qdrant build, and
  database creation tools. Full-index launchers now use the frozen `uv`
  environment and no longer default to a developer-specific `/data/siddhant`
  path.
- Local and AWS compose profiles now define the same loopback PostgreSQL,
  Qdrant, and embedding endpoints, immutable image inputs, persistent storage,
  restart policy, and healthchecks. PostgreSQL consumes a mounted password
  file; the AWS web service already consumes a separate DSN file.
- Added an offline, checksum-bound five-artifact backup bundle verifier and
  deterministic restore plan. It does not pretend a restore happened.
- Replaced declarative-only embedding revision labels with content-addressed
  local model artifact manifests. Both the query service and indexer verify the
  exact file set before loading and force FastEmbed local-only model paths.
- Closed the Web runtime secret-delivery mismatch: file-backed PostgreSQL,
  Qdrant, and embedding credentials now pass through one fail-closed loader to
  the actual clients; credentials are non-enumerable, readiness-safe, and the
  embedding service validates its bearer token.
- Removed the remaining UE58 parallel runner fallback to personal `/data/...`
  paths. Production launch now requires explicit absolute editor, project,
  Content, worker, bridge, and (when local) DDC paths.

## Static validation performed

- Focused asset Python tests, including secret handling, offline network/DB
  assertions, catalog determinism, snapshot revisions, embedding contract, and
  backup tamper/path/symlink rejection.
- Local `docker compose --profile asset-stack config --quiet` with dummy pins.
- AWS `docker compose -f deploy/aws/docker/docker-compose.yml --profile
  asset-stack config --quiet` with dummy pins.
- Shell syntax, Python compile/tests, source secret/path scans, and
  `git diff --check` are required again at the final commit gate.

## Explicitly not complete

T1B.2 remains a Data/Live Gate: there is no authoritative complete catalog and
matching UE Content revision in this worktree, so no real manifest/live receipt
was captured. T1B.3/T1B.4 remain Admin/State-change Gates: no service was
provisioned, no schema or migration ran, no model was downloaded, and no point
was embedded/upserted. T1B.5/T1B.6 configuration code is substantially closed,
but production readiness still requires secret installation, image provenance,
historical credential rotation/log scrub, service supervision approval, and a
successful live health/audit receipt. T1B.12 and the backup/restore drill are
also unexecuted.

## Exact administrator/data inputs still required

1. Complete `catalog/**/*.json` plus exact `category_index.json`, and the
   immutable UE Content revision that supplies every `/Game/...` path.
2. Five immutable image digests: PostgreSQL, Qdrant, embedding, Python base,
   and uv; exact dense/sparse model IDs, artifact revisions, vector size, and
   embedding recipe revision.
3. Root/service-owned mode-0600 Postgres password/DSN, Qdrant API key, and
   embedding bearer token files. The pinned Qdrant server must be configured
   to enforce the same key and deny an unauthorized probe.
4. Approved persistent Postgres, Qdrant, model-cache, catalog, evidence, and
   backup paths; capacity threshold, retention, RPO/RTO, and rollback owner.
5. A bounded execution window and budget for schema/migration/model-cache/full
   indexing, then a separate writable disposable UE spawn smoke.
6. A disposable restore target and approval to restore the checksum-bound
   backup, run shadow queries/live audit/PBR smoke, record evidence, and destroy
   the target.

The verification commands and required environment are maintained in
`asset-stack-operations.md`. Until they succeed with real inputs, production
must remain `not_ready` and `require_real_assets` must block geometry fallback.

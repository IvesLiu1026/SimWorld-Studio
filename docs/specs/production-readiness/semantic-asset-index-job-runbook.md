# Semantic object-index job preparation

Status: the preparation path is code-verified and offline. No Unreal Editor,
caption provider, PostgreSQL, Qdrant, embedding service, model download, or
network request is authorized or performed by this runbook.

This contract converts one reviewed UE AssetRegistry bootstrap candidate into
a deterministic **pending semantic-index job**. It does not convert the
candidate list into an authoritative catalog and it never reports a completed
asset snapshot.

## 1. Safety boundary

`tools/prepare_semantic_asset_index_job.py` has two behaviors:

- the default is a dry run: validate inputs, construct the job in memory, print
  a bounded result, and write nothing;
- `--apply` atomically publishes only `semantic-index-job.json` and
  `preparation-receipt.json` into a new mode-`0700` directory with mode-`0600`
  files. Publication is non-overwriting and requires a non-secret approval
  reference.

The preparer uses only local file and JSON operations. It reuses the bootstrap
producer's deterministic validation/build functions, but does not invoke or
construct any transport, subprocess, provider, database, Qdrant, embedding, or
Unreal client. Its published receipt explicitly records all of those operations
as false. `--apply` means **apply the evidence publication**, not execute
indexing.

The contracts are:

- `tools/semantic_asset_index_recipe_schema.json`;
- `tools/semantic_asset_index_job_schema.json`;
- `simworld-semantic-asset-index-job-preparation-receipt/v1`, described below.

## 2. Required trusted inputs and external pins

Use an operator-reviewed bundle produced by
`tools/build_ue_asset_registry_bootstrap.py`. The following two files must be
from the same current-user-owned mode-`0700` bundle directory:

```text
bootstrap-receipt.json
object-manifest.json
```

Both must be current-user-owned, mode `0600`, non-symlink regular files with a
single hard link. Every path component is checked without following symlinks,
and each file's device, inode, mode, uid, gid, link count, size, mtime, and ctime
are compared before open, after open, and after read. Duplicate JSON keys,
non-finite values, unknown fields, changed metadata, or oversized input fail
closed.

Do not trust a mutually consistent receipt and manifest by themselves. Supply
all of these independently recorded pins:

- raw `bootstrap-receipt.json` SHA-256 and its canonical `bundle_revision`;
- raw `object-manifest.json` SHA-256;
- exact object count;
- exact project revision;
- exact content revision;
- raw recipe SHA-256;
- a new immutable `asset-snapshot-*` target revision.

The preparer re-derives the bootstrap bundle revision, verifies every bootstrap
member's exact byte descriptor, and applies the producer's complete registry,
object-manifest, and capability-inventory validators. It then rebuilds the
object manifest and capability inventory from the canonical registry audit and
requires byte-independent structural equality. Exact path/class membership,
receipt counts/revisions, and disjoint semantic-object versus capability paths
are reconciled before a job can be prepared. The manifest, receipt, archive,
every candidate, and the CLI pins must all carry the same project/content
binding. All object rows must still be `indexed: false` and must be
deterministically ordered `Blueprint` or `StaticMesh` candidates.

## 3. Pinned recipe

Create a private mode-`0600` recipe matching
`simworld-semantic-asset-index-recipe/v1`. The values below are illustrative;
replace every digest and provider snapshot with reviewed evidence before using
it for a real plan:

```json
{
  "schema": "simworld-semantic-asset-index-recipe/v1",
  "caption": {
    "provider_id": "anthropic-claude-cli",
    "model_id": "claude-opus-4-8",
    "model_revision": "provider-snapshot:REVIEWED-IMMUTABLE-SNAPSHOT",
    "prompt_revision": "sha256:REPLACE_WITH_64_HEX",
    "output_schema_revision": "sha256:REPLACE_WITH_64_HEX",
    "render_recipe_revision": "sha256:REPLACE_WITH_64_HEX",
    "views_per_asset": 8,
    "image_width_px": 1024,
    "image_height_px": 1024,
    "max_output_tokens_per_asset": 1200,
    "temperature_milli": 0
  },
  "embedding": {
    "recipe_revision": "bge-large-en-v1.5-bm25-v1-REVIEWED",
    "dense_model_id": "BAAI/bge-large-en-v1.5",
    "dense_model_revision": "sha256:REPLACE_WITH_DENSE_ARTIFACT_MANIFEST_SHA256",
    "dense_size": 1024,
    "sparse_model_id": "Qdrant/bm25",
    "sparse_model_revision": "sha256:REPLACE_WITH_SPARSE_ARTIFACT_MANIFEST_SHA256",
    "batch_size": 32
  },
  "storage": {
    "postgres_schema_revision": 2,
    "qdrant_collection": "assets-v1-rIMMUTABLE",
    "qdrant_dense_vector_name": "dense",
    "qdrant_sparse_vector_name": "sparse"
  },
  "limits": {
    "max_assets": 5000,
    "max_rendered_views": 40000,
    "max_render_pixels": 41943040000,
    "max_total_caption_output_tokens": 6000000,
    "max_catalog_record_bytes": 65536,
    "max_total_catalog_bytes": 327680000,
    "max_postgres_rows": 5000,
    "max_qdrant_points": 5000,
    "max_dense_vector_payload_bytes_estimate": 20480000
  }
}
```

Hosted caption providers may use a reviewed `provider-snapshot:<nonempty>`
binding; local caption artifacts may use `sha256:*`. Snapshot namespaces must
have a nonempty revision, and floating tokens are rejected even when embedded
in a collection name such as `assets-v1-latest`. Dense and sparse embedding
revisions must be the SHA-256 bindings of the complete offline artifact
manifests described in `asset-stack-operations.md`. Floating values such as
`latest`, `main`, `dev`, or `unknown` are rejected.

The job binds the recipe's raw file digest and canonical caption/embedding
sub-recipe digests. It calculates deterministic upper bounds for rendered
views/pixels, caption output tokens, catalog bytes, embedding batches,
PostgreSQL rows, and Qdrant points. The dense float32 byte estimate explicitly
excludes sparse vectors, payloads, indexes, replication, and storage overhead.
Any bound above the recipe limit fails before publication.

## 4. Dry-run preparation

Record the pins in the operator evidence ledger, then run the default dry run.
This command does not require a provider token, database DSN, Qdrant key,
embedding token, UE process, or listening service.

```bash
uv run --project tools --frozen python \
  tools/prepare_semantic_asset_index_job.py \
  --bootstrap-receipt /absolute/private/bootstrap-r1/bootstrap-receipt.json \
  --object-manifest /absolute/private/bootstrap-r1/object-manifest.json \
  --recipe /absolute/private/semantic-index-recipe-r1.json \
  --expected-bootstrap-receipt-sha256 '<64-hex>' \
  --expected-bundle-revision 'sha256:<64-hex>' \
  --expected-object-manifest-sha256 '<64-hex>' \
  --expected-object-count '<reviewed-integer>' \
  --expected-project-revision 'source-patch:<exact-commit>' \
  --expected-content-revision 'sha256:<exact-content-sha256>' \
  --expected-recipe-sha256 '<64-hex>' \
  --asset-snapshot-revision 'asset-snapshot-<immutable-operator-revision>'
```

A valid result has `status: dry_run`, the deterministic job revision and job
SHA-256, all source/recipe/snapshot pins, the pending object count, resource
upper bounds, and only false side-effect flags. It intentionally has
`catalog_complete: false` and `snapshot_complete: false`.

## 5. Publish the private job bundle

Choose a new path beneath an existing current-user-owned private directory.
The approval reference is hashed with a domain separator; the raw value is not
written.

```bash
install -d -m 0700 /absolute/private/semantic-index-jobs

uv run --project tools --frozen python \
  tools/prepare_semantic_asset_index_job.py \
  --bootstrap-receipt /absolute/private/bootstrap-r1/bootstrap-receipt.json \
  --object-manifest /absolute/private/bootstrap-r1/object-manifest.json \
  --recipe /absolute/private/semantic-index-recipe-r1.json \
  --expected-bootstrap-receipt-sha256 '<64-hex>' \
  --expected-bundle-revision 'sha256:<64-hex>' \
  --expected-object-manifest-sha256 '<64-hex>' \
  --expected-object-count '<reviewed-integer>' \
  --expected-project-revision 'source-patch:<exact-commit>' \
  --expected-content-revision 'sha256:<exact-content-sha256>' \
  --expected-recipe-sha256 '<64-hex>' \
  --asset-snapshot-revision 'asset-snapshot-<immutable-operator-revision>' \
  --output-dir /absolute/private/semantic-index-jobs/official-minimal-r1 \
  --apply \
  --approval-ref 'CHANGE-SEMANTIC-INDEX-R1'
```

Publication writes the job first and the preparation receipt last in a private
temporary directory, `fsync`s files and directories, and uses
`renameat2(RENAME_NOREPLACE)` for atomic non-overwriting publication. The
preparation receipt marks only `bundle_complete: true`; execution, catalog,
and snapshot completion remain false.

Before writing, publication discards caller-derived views and deterministically
reprojects the entire canonical job from a private, non-init trusted basis. That
basis retains the exact raw receipt, registry, capability inventory, object
manifest, and recipe bytes plus the independently supplied pins; every raw hash,
bundle/member relationship, recipe projection, and operator pin is revalidated.
`PreparedJob` exposes derived read-only properties but has no replaceable public
dataclass fields, so synchronized replacement of job bytes and all old identity
scalars is rejected. A same-count replacement of valid-looking pending
identities therefore fails even if the embedded asset digest and job revision
are recomputed, and changing caption/input contracts cannot diverge from the raw
recipe/bootstrap evidence. Mutating a caller's parsed job view cannot change the
published bytes or receipt identity. The output lock remains open and is removed
only when its complete metadata identity still matches, so a replaced foreign
lock is preserved. If the atomic rename succeeds but the parent-directory
`fsync` fails, the CLI returns the distinct
`SEMANTIC_INDEX_OUTPUT_COMMITTED_NOT_DURABLE` condition with
`committed: true` and `durability_uncertain: true`; inspect that already
committed path and do not retry it.

## 6. Execution is a separate gated product

The prepared job deliberately emits no executable command. It carries this
fail-closed policy:

- explicit operator execution is required;
- `full_asset_index_runner.py` is not authorized directly;
- a separately reviewed adapter must first validate the exact job bytes and
  job revision, enforce the pending set and resource limits, persist durable
  per-asset results, and obtain the individual live gates;
- `--dangerously-bypass-approvals-and-sandbox` is prohibited as a production
  default.

The legacy runner currently invokes Codex with that bypass flag and directly
combines UE rendering, paid captioning, catalog writes, PostgreSQL migration,
and Qdrant upserts. Do not point it at this job or treat its ordinary
`--dry-run` as validation of this contract. A future adapter/executor must be a
separate reviewed change with explicit operator invocation; it must never be
started by the preparer.

## 7. Remaining live gates

Every published job has seven pending gates:

1. Data owner approves the bounded object candidate slice and rejection
   ledger. AssetRegistry filtering is not semantic catalog review.
2. UE loads/spawns each approved object and records class, bounds, collision,
   PBR material slots, dependency state, and render hashes in the exact content
   revision.
3. Cost owner approves the caption provider, immutable model binding, output
   schema, prompt/render recipe, asset count, image count, and token ceiling.
4. Administrator verifies complete offline dense/sparse model artifact
   manifests, dimensions, cache/storage, and embedding budget.
5. Administrator applies PostgreSQL schema revision 2, backup/restore policy,
   and revision-bound row migration.
6. Administrator configures authenticated Qdrant, builds revision-bound named
   dense/sparse vectors, and verifies point identity and payloads.
7. Operator runs `verify_asset_snapshot.py` and records a fresh digest-bound
   live-audit receipt proving catalog/PostgreSQL/Qdrant/model/content parity.

Only gate 7 can establish `simworld-asset-snapshot/v1`. Until then, the bundle
is a deterministic pending job for an incomplete candidate manifest—not a
complete Postgres/Qdrant semantic index and not a production-ready catalog.

## 8. Offline validation

```bash
uv run --project tools --frozen python -m unittest \
  tools.tests.test_prepare_semantic_asset_index_job -v

uv run --project tools --frozen python -m json.tool \
  tools/semantic_asset_index_recipe_schema.json >/dev/null

uv run --project tools --frozen python -m json.tool \
  tools/semantic_asset_index_job_schema.json >/dev/null
```

The 27-test focused suite covers deterministic job bytes, exact external pins,
producer-contract reprojection, registry/capability membership and separation,
receipt count reconciliation, manifest/receipt revision checks, duplicate JSON
keys, weak/symlink/hard-link inputs, empty or floating revisions and collection
tokens, resource ceilings, default no-write behavior, private atomic
publication, non-secret approvals, schema drift, absence of direct live
client/process imports, opaque raw-basis reprojection, synchronized recipe/input
forgery rejection, immutable pending identity, metadata drift, lock
replacement, committed-but-uncertain durability, and the legacy execution
boundary.

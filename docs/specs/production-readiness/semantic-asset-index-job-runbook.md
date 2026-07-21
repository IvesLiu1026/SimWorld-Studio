# Semantic object-index job preparation

Status: the preparation path and the fail-closed execution coordinator are
code-verified offline. No production phase adapter is registered. No Unreal
Editor, caption provider, PostgreSQL, Qdrant, embedding service, model
download, container, or network request is authorized or performed by this
runbook.

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
- `tools/semantic_asset_index_execution_plan_schema.json`;
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

## 6. Fail-closed execution coordinator

`tools/execute_semantic_asset_index_job.py` is the reviewed coordinator for a
sealed pending job. It is not a production indexer by itself. Its default is a
validation-only dry run, and its static adapter registry currently contains
only `offline-fixture-v1`, whose `production_capable` value is false. Selecting
the production profile with that adapter fails with
`SEMANTIC_EXECUTOR_ADAPTER_NOT_PRODUCTION`. Unknown and dynamically discovered
adapters are rejected.

The coordinator never starts an adapter subprocess, shell, network client, or
legacy runner. Its one process boundary is a fixed `/usr/bin/git
--no-optional-locks` attestation client. That client receives a minimal
environment, no shell, bounded output, and a deadline, and it is restricted to
`config`, `rev-parse`, and `cat-file` reads. `GIT_NO_LAZY_FETCH=1` is fixed in
that environment, and repositories carrying partial-clone or promisor config
are rejected before object access. Git runs in a new process group; a timeout
or output-bound failure sends `SIGKILL` to the entire group and boundedly reaps
the direct Git child under one absolute cleanup deadline. The group kill is
still attempted if the direct Git leader has already exited while a descendant
keeps its inherited pipes open. Descendants are
guaranteed only to be non-running; a killed non-child helper may remain as a
zombie until its owning process or system reaper collects it. These local reads
do not invoke hooks, filters, textconv,
external diff, or repository programs, and cannot lazily retrieve a missing
object. The legacy runner remains
unauthorized; do not point it at this bundle and do not treat its ordinary
`--dry-run` as evidence for this contract. The prohibited bypass flag in the
pending job also remains prohibited.

There are two distinct trust boundaries. The Python entrypoint is already
executing when its runtime checks begin and cannot establish its own
pre-execution trust. A release launcher or security manifest must therefore
verify the exact entrypoint bytes, expected execution-plan digest, and operator
approval before launch. At runtime, before executing any repository-local
adapter bytes, the coordinator binds every dependency in the source closure to
the externally pinned plan, exact Git HEAD, and exact HEAD blob bytes. The
entrypoint comparison at this stage detects operational drift only; it is not a
substitute for launcher attestation. Neither the preparer nor its transitive
bootstrap module is imported by the executor.

The launcher must also attest the system interpreter and invoke the coordinator
exactly as `/usr/bin/python3 -I -S`. Execution supports reviewed CPython minor
versions 3.10, 3.11, and 3.12. It requires isolated mode, ignored environment,
disabled user site, disabled `site`, a non-virtualenv prefix, and the exact
system executable. Python 3.10 has no `sys.flags.safe_path`; the gate therefore
uses the version-independent isolation flags there and additionally requires
`safe_path=true` on Python 3.11 and 3.12. `-S` keeps virtualenv/site `.pth`,
`sitecustomize`, and `usercustomize` processing outside the execution loader.
These runtime checks still cannot attest the interpreter binary, native loader,
entrypoint, or code already executed during startup. Those remain external
release-launcher responsibilities. No external release attestation receipt is
implemented in this repository, and no production adapter exists, so
production execution remains unavailable rather than claiming that pre-launch
attestation has completed. The minimal environment used for the child Git
client protects only that Git client, not the parent Python loader.

### 6.1 Seal an execution plan

Create a current-user-owned mode-`0600` canonical JSON file matching
`tools/semantic_asset_index_execution_plan_schema.json`. Record its raw SHA-256
independently. The plan duplicates and binds all facts that may change what is
executed:

- the raw job and preparation-receipt digests, canonical job revision,
  bootstrap bundle/receipt, object manifest, recipe, project, content, pending
  set/count, and target snapshot revisions;
- the exact Git object format and commit containing the coordinator, plus raw
  hashes of the coordinator, adapter registry, preparer, the preparer's
  transitive AssetRegistry bootstrap contract, job schema, and execution-plan
  schema;
- caption provider/model snapshot, prompt, output schema, render recipe,
  image dimensions/view count/token ceiling, and temperature;
- dense/sparse model artifact revisions, IDs, dimensions, recipe, and batch
  size;
- immutable `name@sha256:<digest>` pins for Unreal, PostgreSQL, Qdrant, and
  embedding runtime images;
- PostgreSQL schema and Qdrant collection/vector names;
- four non-secret production credential bindings: a credential generation and
  an exact provider project, PostgreSQL deployment/schema, Qdrant
  cluster/collection, or embedding project/model target identity;
- exact catalog/row/point/vector/render counts, per-phase timeout, total
  deadline, adapter ID/revision/profile, and a domain-separated hash of the
  non-secret execution approval reference.

The profile has a closed credential contract. `offline_fixture` requires an
empty `credential_files_required` list and rejects every supplied credential.
`production` requires exactly `caption`, `postgres`, `qdrant`, and `embedding`
in that order. It also requires exactly four closed `credential_bindings`.
Each binding records a non-secret `credential_generation` plus the exact target
identity for that credential class. The offline profile requires this mapping
to be empty. The generation and targets are copied into durable state, and
every phase result must report all four exact target identities before it can
be accepted. A failed run therefore cannot resume under a different credential
generation, provider project, database/schema, cluster/collection, or
embedding project/model. Rotating secret file contents under the same reviewed
generation and target identity is permitted on restart; secret bytes are
captured again and are never used as resume identity.
The CLI accepts those credentials only as absolute private file paths through
the four `--*-secret-file` arguments. Each file is securely opened and read
exactly once; the adapter receives one immutable in-memory byte snapshot, not a
re-readable path. Replacing a credential path after capture therefore cannot
change later phases. The CLI has no password, token, API-key, DSN, or
inline-secret argument. Secret values, paths, and digests are never written to
state, terminal receipts, stdout, stderr, or adapter-result evidence.

Before constructing the plan, record the source pins from the reviewed clean
commit. These commands are local and read-only:

```bash
git rev-parse HEAD
git rev-parse --show-object-format
sha256sum \
  tools/execute_semantic_asset_index_job.py \
  tools/semantic_index_job_adapters.py \
  tools/prepare_semantic_asset_index_job.py \
  tools/build_ue_asset_registry_bootstrap.py \
  tools/semantic_asset_index_job_schema.json \
  tools/semantic_asset_index_execution_plan_schema.json
```

The plan must be serialized with sorted keys, two-space indentation, UTF-8,
and one trailing newline. Its `executor.git_commit` and
`executor.git_object_format` must describe the commit that contains the exact
six hashed files—not an earlier working-tree base. The externally recorded raw
plan SHA-256 and approval binding authorize those plan pins; the plan's own
source digests do not self-authorize. The fixed Git client reads each pinned
blob directly, and the coordinator requires the worktree bytes, HEAD blob, and
plan SHA-256 to agree. Staged, unstaged, untracked replacement,
dependency-name shadowing after isolated launch, or transitive-source drift
therefore fails before an adapter is loaded, a phase starts, or a run directory
is created.

### 6.2 Validation-only dry run

The job and preparation receipt must remain under the same original private
mode-`0700` published bundle directory, with their original filenames and
mode-`0600`, single-link files. Supply all five independently recorded raw
digests:

```bash
/usr/bin/python3 -I -S \
  /absolute/SimWorld-Studio/tools/execute_semantic_asset_index_job.py \
  --job /absolute/private/job-r1/semantic-index-job.json \
  --preparation-receipt /absolute/private/job-r1/preparation-receipt.json \
  --execution-plan /absolute/private/execution-plan-r1.json \
  --expected-job-sha256 '<64-hex>' \
  --expected-preparation-receipt-sha256 '<64-hex>' \
  --expected-execution-plan-sha256 '<64-hex>' \
  --expected-job-schema-sha256 '<64-hex>' \
  --expected-execution-plan-schema-sha256 '<64-hex>' \
  --repo-root /absolute/SimWorld-Studio \
  --adapter offline-fixture-v1
```

This writes nothing. A successful result has `status: dry_run`, all side-effect
flags false, and `production_complete`, `catalog_complete`, and
`snapshot_complete` false. It does not prove that any pinned image is running;
it proves only that the reviewed expected pins are sealed consistently.

### 6.3 Offline fixture execution and durable resume

The only executable adapter is a deterministic fixture. It performs no live
observation or mutation. Use it only to validate coordinator behavior, never
as semantic-index evidence:

```bash
install -d -m 0700 /absolute/private/semantic-index-runs

/usr/bin/python3 -I -S \
  /absolute/SimWorld-Studio/tools/execute_semantic_asset_index_job.py \
  --job /absolute/private/job-r1/semantic-index-job.json \
  --preparation-receipt /absolute/private/job-r1/preparation-receipt.json \
  --execution-plan /absolute/private/execution-plan-r1.json \
  --expected-job-sha256 '<64-hex>' \
  --expected-preparation-receipt-sha256 '<64-hex>' \
  --expected-execution-plan-sha256 '<64-hex>' \
  --expected-job-schema-sha256 '<64-hex>' \
  --expected-execution-plan-schema-sha256 '<64-hex>' \
  --repo-root /absolute/SimWorld-Studio \
  --adapter offline-fixture-v1 \
  --run-dir /absolute/private/semantic-index-runs/fixture-r1 \
  --execute \
  --approval-ref 'CHANGE-SEMANTIC-EXECUTOR-R1'
```

The allowlisted order is `inspect`, `render`, `caption`, `embed`, `postgres`,
`qdrant`, then `reconcile`. Before every call the coordinator durably records a
stable phase idempotency key. A crash or fixed-code phase failure can be
resumed with the exact same command and key; completed phases are not rerun.
An `flock` prevents concurrent owners. Per-phase alarms and the sealed total
deadline are enforced. On every process start or resume, the remaining wall
deadline is anchored once to the monotonic clock, so a later wall-clock change
cannot extend it. Every wall-clock sample must be timezone-aware and
nondecreasing, and a resume clock cannot predate the durable start/update
timestamps. Every count must equal the pending set, and final
catalog/PostgreSQL/Qdrant counts plus snapshot revision must reconcile.

`execution-state.json` and `terminal-receipt.json` are canonical mode-`0600`
files under the new mode-`0700` run directory. Both are atomically written and
directory-`fsync`ed. Before lock acquisition, the coordinator opens the private
parent and run directory with `O_DIRECTORY|O_NOFOLLOW`, retains both
descriptors, and binds them to their device/inode identities. The lock, state,
temporary evidence, terminal receipt, recovery quarantine, reads, renames, and
unlinks are all performed relative to those held descriptors. Parent path,
run-directory name, and lock inode are revalidated at I/O and phase boundaries;
renaming the run directory and recreating its old pathname therefore fails
closed instead of redirecting later writes. `renameat2` receives the actual
source and destination directory descriptors rather than `AT_FDCWD` paths.
New-file publication uses
`renameat2(RENAME_NOREPLACE)`, so it never creates the two-link interval of the
older link/unlink pattern. Directory inspection occurs only after acquiring
the run lock. Under that lock, a recognized single-link interrupted temp file
is moved into a private `.recovery-quarantine`; a recognized legacy temp hard
link is removed only when it is the second link to the exact committed target.
Unsafe or unknown residue fails closed. A newly constructed terminal receipt is
validated against the durable state before publication. A terminal receipt
written just before a crash is the recovery authority and is revalidated before
state is repaired; a repair clock cannot predate the durable terminal evidence.
The fixture's terminal status is always `offline_fixture_complete`, with all
production, catalog, and snapshot completion values false.

The offline regression suite exercises these claims with real isolated system-
Python subprocesses, not exception-only crash simulation. A parent launches
each child in a new process group and sends `SIGKILL` at state-temp write,
state-temp `fsync`, state rename, state directory-`fsync`, adapter return,
completed-phase commit, terminal pre/post rename, and terminal-to-state link
commit boundaries. Every case resumes to one terminal result with the original
idempotency keys. Attempts to reopen the interrupted journal with either a
different credential generation or a different target identity fail before
state or terminal evidence changes. Separate live-child tests rename and
recreate the run directory, its parent, and the lock pathname while descriptors
are held; the child returns the original bounded binding error and writes no
state byte into replacement directories. Two actual subprocesses also contend
on the same plan/run, proving that only the lock owner reaches an adapter phase.
The production-shaped adapter used by these tests exists only inside the test
harness, performs deterministic in-memory arithmetic, and is not registered or
available to the executor CLI.

### 6.4 Gate for a future production adapter

A production adapter must be added as an explicit reviewed source change to
the sealed registry; filesystem discovery, entry points, caller-supplied
instances, arbitrary plugin imports, shell wrappers, and legacy-runner wrappers
are not accepted. The public execution API accepts only an adapter ID and the
exact opaque bundle object registered by the same validation call; copied or
`dataclasses.replace`-constructed bundles are rejected. Immediately before
compilation, the coordinator recomputes the adapter-byte SHA-256 against the
plan pin, then compiles that captured immutable byte string into a fresh private
module and constructs a fresh exact registered type; the fixture instance has
no writable instance dictionary. This is a process-internal integrity barrier,
not a sandbox against arbitrary code already executing in the same Python
process: unrestricted same-process monkeypatching is trusted/out of scope and
must instead be prevented by the externally attested one-shot launcher. A
production adapter review must prove that
every phase is idempotent for the supplied key, honors the deadline, receives
only disposable copies of validated job/plan data plus the immutable secret
snapshot, and returns the exact bounded typed result. Production reconciliation
additionally must return all four
observed runtime image digests exactly equal to the plan, equal catalog/row/
point counts, the exact snapshot revision, and a fresh live-audit receipt
digest. Until that adapter and its live evidence are separately approved, no
execution may produce `production_complete: true`.

## 7. Remaining live gates

The deterministic coordinator, sealed plan, resume state, timeout/count checks,
and terminal receipt now exist. The concrete production adapter and every live
dependency observation still do not. Every published job therefore retains
all seven pending gates:

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

uv run --project tools --frozen python -m unittest \
  tools.tests.test_execute_semantic_asset_index_job -v

uv run --project tools --frozen python -m json.tool \
  tools/semantic_asset_index_recipe_schema.json >/dev/null

uv run --project tools --frozen python -m json.tool \
  tools/semantic_asset_index_job_schema.json >/dev/null

uv run --project tools --frozen python -m json.tool \
  tools/semantic_asset_index_execution_plan_schema.json >/dev/null
```

The 27-test preparation suite covers deterministic job bytes, exact external pins,
producer-contract reprojection, registry/capability membership and separation,
receipt count reconciliation, manifest/receipt revision checks, duplicate JSON
keys, weak/symlink/hard-link inputs, empty or floating revisions and collection
tokens, resource ceilings, default no-write behavior, private atomic
publication, non-secret approvals, schema drift, absence of direct live
client/process imports, opaque raw-basis reprojection, synchronized recipe/input
forgery rejection, immutable pending identity, metadata drift, lock
replacement, committed-but-uncertain durability, and the legacy execution
boundary.

The 53-test executor suite covers independent raw bundle/plan/schema pins,
closed canonical contracts, exact Git/source/model/image/storage/count binding,
private same-directory sealed inputs, symlink/hard-link/split-bundle rejection,
default no-write behavior, static adapter selection, production fail-closed
behavior, credential-file-only handling, secret-free errors/evidence, ordered
phase execution, timeout and count failures, stable idempotency keys, crash
resume, out-of-order/extended-state rejection, disposable adapter inputs,
atomic private evidence, committed-but-not-durable evidence reporting, and
terminal-receipt recovery. It also exercises a real local Git closure against
dirty transitive source, shadow modules for the removed repo-local imports,
caller-adapter bypass,
credential-path replacement after snapshot capture, runtime-image length/schema
parity, non-isolated execution rejection, lock-before-residue inspection,
orphan quarantine, legacy two-link repair, and unsafe temp rejection.
It additionally rejects partial/promisor repositories before object access,
kills a timed-out Git process group while boundedly reaping the direct child,
proves cleanup never performs an unbounded wait, kills a pipe-holding sleeper
after its direct leader exits, validates the exact system
Python 3.10-compatible `-I -S` command, and rejects copied bundles or changed
adapter bytes between validation and compilation. It also enforces the closed
credential-generation/target contract, rejects a changed binding on failed-run
resume while allowing secret-byte rotation under the same identity, requires
each phase to echo every sealed target, rejects terminal recovery against a
different target identity, verifies real two-process lock
contention, and proves that run-directory rename/recreate is detected through
held directory descriptors without writing to the replacement path. Descriptor
cleanup is also fault-injected to confirm that parent, run, and lock descriptors
close while the original bounded execution error remains primary.
The suite additionally runs a nine-point process-group `SIGKILL` matrix, real
same-plan/same-run subprocess contention, and live subprocess parent/run/lock
rebinding with zero writes to replacement directories. It also covers exact
scalar type enforcement against Boolean/float aliases, a process-monotonic
total deadline, aware nondecreasing wall-clock samples, expired and clock-
regressed resumes, terminal self-validation before publication, and exact
terminal pending-count types. Both duplicate PostgreSQL schema-revision fields
in the sealed job reject Boolean and floating-point numeric aliases.
These are offline tests; they do not satisfy any of the seven live gates.

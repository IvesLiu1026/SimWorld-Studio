# Semantic index production contract v2

Status: draft implementation addendum derived from the approved Production
Readiness requirements; not separately approved for live execution. The parent
specification is approved, but **no live data, cost, database, Unreal, provider,
or activation approval is implied by this addendum**.

Date: 2026-07-21

## 1. Why v2 is required

The v1 pending-job and execution-plan contracts remain useful for deterministic
offline preparation and coordinator testing, but they cannot safely authorize a
Production index run:

- the v1 job deliberately describes an unreviewed, all-candidate set and records
  `cost_authorized=false`;
- the v1 plan does not bind a UE lease, worker endpoint, generation workspace,
  provider endpoints, artifact contracts, deployment preflight, or approval
  receipts;
- the isolated coordinator runs as `/usr/bin/python3 -I -S`, so it cannot load
  the uv-managed PostgreSQL, Qdrant, or model clients;
- a v1 phase result contains only counts and adapter-supplied hashes. The
  coordinator never validates the evidence bytes behind those claims;
- the current PostgreSQL primary key and upsert path can overwrite the active
  snapshot, so it cannot provide shadow-read or rollback guarantees.

Changing those meanings in place would make previously sealed v1 plans
ambiguous. Therefore:

1. `simworld-semantic-asset-index-job/v1` and
   `simworld-semantic-asset-index-execution-plan/v1` stay offline-only.
2. A Production run requires new v2 job and plan contracts plus canonical
   approval, phase-evidence, state, and terminal-receipt contracts.
3. Registering a Production adapter is prohibited until the v2 validator and
   evidence lifecycle are integrated and the offline-fake form of PA-01 through
   PA-21 passes. Enabling a production-capable release or verified live terminal
   state additionally requires every live-tier row in section 9.

This addendum strengthens ASSET-001, ASSET-002, ASSET-007, ASSET-008 and the
OPS evidence/lifecycle requirements. Parent ASSET-003 through ASSET-006 still
govern runtime hybrid retrieval, dependency fallbacks, typed failure, and
`require_real_assets`. Consequently `snapshot_built_verified` is not equivalent
to retrieval readiness or overall Production readiness; the runtime resolver,
active-generation pointer, readiness probes, and fail-closed scene build must
all accept the same epoch.

## 2. Trust and process architecture

```text
externally attested launcher
        |
isolated coordinator (/usr/bin/python3 -I -S)
        |
launcher-attested connected Unix socket FD + fixed framed canonical-JSON protocol
        |
pinned semantic-index worker image
        |
UE broker / caption provider / embedding / new PostgreSQL generation /
new Qdrant collection / content-addressed generation artifacts
```

The coordinator remains responsible for validating sealed inputs, capturing
credentials once, enforcing total and phase deadlines, journaling intent,
validating and persisting bounded evidence bytes, and deciding terminal truth.
It must not import third-party clients, launch Compose, execute shell commands,
or call any legacy runner.

The worker owns third-party clients inside an immutable, digest-pinned runtime.
It exposes only the seven phase operations and four recovery-control operations
in section 6 over a service-owned Unix socket. The plan binds the socket path,
socket owner UID/GID, protocol
revision, worker deployment identity, worker image digest, and deployment
attestation receipt. Before the executor starts, the launcher verifies the
socket inode, peer PID and process-start token, UID/GID, cgroup/runtime image,
and deployment receipt, then passes an already-connected FD. The executor does
not reconnect by pathname. The client rejects non-AF_UNIX FDs, changed peer
identity, unknown fields, redirects, and responses over the per-message byte
limit.

Each connected FD is an ownership-transferred one-shot channel for exactly one
request and one response. After sending its request the client half-closes its
write side; the worker requires EOF before executing. After sending one bounded
canonical response the worker half-closes its write side; the client requires
EOF before accepting success. Either side rejects trailing bytes/frames and
closes the channel. The protocol never duplicates an FD for caller reuse and
never leaves shared `O_NONBLOCK`/timeout state behind.

The externally attested launcher must verify the system interpreter, exact
coordinator and transitive source closure, exact plan and input digests, and
worker/runtime attestation before the entrypoint executes. Runtime source
checks do not replace this pre-execution trust boundary.

The signed deployment receipt is the source of the non-secret endpoint map,
transport policy, authenticated `WhoAmI` identities, and runtime image
observations. The plan copies and pins those safe facts and the raw receipt
digest; neither adapter nor worker may discover replacements from ambient
configuration.

## 3. Contract set

The implementation must define these immutable schemas without changing v1
semantics:

- `simworld-semantic-asset-index-job/v2`;
- `simworld-semantic-asset-index-execution-plan/v2`;
- `simworld-semantic-index-production-approval-basis/v1`;
- `simworld-semantic-index-approval-receipt/v1`;
- `simworld-semantic-index-launcher-verification-receipt/v1`;
- `simworld-semantic-index-phase-request/v1`;
- `simworld-semantic-index-worker-result/v1`;
- `simworld-semantic-index-worker-artifact-root/v1`;
- `simworld-semantic-index-phase-evidence/v1`;
- `simworld-semantic-index-control-request/v1`;
- `simworld-semantic-index-control-result/v1`;
- `simworld-semantic-asset-index-execution-state/v2`;
- `simworld-semantic-asset-index-terminal-receipt/v2`;
- `simworld-semantic-index-inspect-artifact/v1`;
- `simworld-semantic-index-render-artifact/v1`;
- `simworld-semantic-index-caption-artifact/v1`;
- `simworld-semantic-index-embed-artifact/v1`;
- `simworld-semantic-index-postgres-artifact/v1`;
- `simworld-semantic-index-qdrant-artifact/v1`;
- `simworld-semantic-index-reconcile-artifact/v1`;
- adapter contract `semantic-index-production-adapter/v2`;
- worker protocol `simworld-semantic-index-worker/v1`.

Every schema is closed (`additionalProperties=false`), bounded by exact count,
string, array, nesting, and byte ceilings, and serialized as canonical UTF-8
JSON. Parsing rejects duplicate keys, NaN/Infinity, non-integer contract
numbers, control characters, and text that is not already Unicode NFC.
Canonical bytes use RFC 8785 JCS, UTF-8 without BOM, and no trailing whitespace
or newline. Persisted v2 documents store those exact bytes; human-readable
renderings are non-authoritative. Every raw input file has an independently
supplied SHA-256. A hash in an untrusted document never authorizes itself.

To avoid a receipt/plan hash cycle, approvals bind the SHA-256 of a separate
canonical `simworld-semantic-index-production-approval-basis/v1` document, not
a projection of the final plan. That closed document contains every reviewed
job, run, target, generation nonce and target-generation identity, operation,
resource, cost, mutation, rollback, and runtime commitment that an approver is
authorizing. It intentionally does not contain the final derived generation
ID or any generation-specific storage name whose derivation uses the basis
digest. It also contains neither its own digest nor any approval receipt,
receipt digest, issuer key, signature, or trust-bundle reference. Its
canonical JCS bytes are hashed once;
approval receipts sign that raw digest. The final plan contains the exact
basis digest plus the derived generation ID/storage names, receipt digests,
issuer key IDs, signature algorithms, and trust-bundle digest, but does not
redefine the basis. The semantic validator
reconstructs the expected basis from the plan commitments, compares exact
canonical bytes supplied as a separate sealed input, and recomputes every
layer. Generic exclusion lists or caller-selected JSON paths are forbidden.
The generation-independent basis commitment explicitly includes the complete
generation-reservation object digest (receipt schema/digest, reservation ID,
exclusive-create mode, owner, and validity interval), artifact layout and
namespace revision, and catalog namespace revision. Changing any of those
facts therefore changes the basis digest and final generation ID; they cannot
be rebound after approval while retaining the same generation.
An externally attested launcher verifies the signatures against
deployment-owned trust roots before execution and records that verification
in its runtime attestation receipt; the coordinator still validates the
canonical receipt bytes and all basis/scope/expiry fields.

The launcher verification receipt is a distinct, release-pinned formal
contract. The offline verifier accepts only caller-supplied exact canonical
bytes, an explicit frozen Ed25519 trust-key map, explicit expected pins, and an
explicit UTC time. It returns an opaque verified object rather than a digest
echo. A second exact-type binding step combines that object with the separately
attested one-shot worker handoff, compares the two receipt digests, and transfers
socket ownership once. The raw handoff receipt alone is not signature evidence,
and neither object may be reconstructed from caller-selected scalar fields.

### 3.1 Reviewed v2 job

The v2 job derives from one sealed v1 candidate job but replaces
`all_unindexed_objects_in_pinned_bootstrap_manifest` with an explicit reviewed
accepted set. It must bind:

- the raw v1 job and preparation-receipt hashes and revisions;
- the original ordered candidate-set digest and count;
- exact accepted and rejected asset lists, their independent digests and
  counts, and proof that they are disjoint and exactly partition the source
  candidates;
- the raw rejection-ledger hash;
- a data-owner approval receipt bound to the source job, accepted set,
  rejection ledger, content revision, review role, issue time, and expiry;
- unchanged recipe, project, content, and target snapshot identities;
- resource estimates recomputed from the accepted set only.

The reviewed v2 job is a reusable data-selection artifact and therefore does
not contain a run, correlation, lease, slot, or operator-session identity. Run
identity begins in the v2 execution plan. The plan and every later request and
evidence item bind a safe `run_id`, `correlation_id`, operator/service owner
identity, UE lease/slot identity where applicable, and retention class. These
identifiers contain no host path or credential material.

The reviewed job still does not authorize cost or mutations. Empty accepted
sets are rejected rather than being represented as a successful index.

### 3.2 Production execution plan

The v2 plan must bind all operational input rather than discovering it from
the environment:

- exact reviewed-job, schema, source-closure, adapter, worker-protocol, worker
  image, dependency lock, SQL, and evidence-schema digests;
- exact run/correlation/operator/service-owner identities and the canonical
  approval-basis digest;
- a canonical deployment-preflight receipt and its raw hash;
- launcher-observed worker and Unreal/caption/embedding/PostgreSQL/Qdrant
  runtime identities, pinned runtime image (caption is exactly `null`),
  `WhoAmI` digest, runtime-attestation digest, peer-attestation digest, host
  boot ID, PID/start token, and connected-socket inode binding needed for a
  deterministic plan-to-request projection;
- a new immutable generation nonce and derived generation ID,
  content-addressed artifact namespace,
  previous active snapshot, and cleanup/quarantine policy;
- the Unix socket path, expected owner UID/GID, protocol revision, peer
  deployment identity, maximum frame bytes, and absolute request limits;
- UE project/content/runtime image, fixed broker operations, slot and bounded
  lease identity;
- caption provider/project/model snapshot, prompt bytes digest, output-schema
  bytes digest, render-recipe bytes digest, token/call/cost ceiling, and
  provider idempotency policy;
- dense/sparse model IDs, artifact-manifest digests, dimensions, batching, and
  embedding endpoint identity;
- isolated PostgreSQL generation/database/schema and Qdrant immutable
  collection/vector names; neither may identify the current active generation;
- exact expected asset, render, catalog, vector, row, and point sets/counts;
- required Chinese and English shadow queries, normalization revision, expected
  asset IDs/rank thresholds, and the exact Blueprint and StaticMesh smoke
  targets;
- data, cost, admin state-change, deployment, runtime, and rollback-readiness
  approval receipts, each bound to the exact `approval_basis_sha256`, scope,
  issuer, issuer key ID, signature algorithm, trust bundle, and expiry;
- per-phase non-secret credential labels/generations/target bindings, deadlines,
  attempt ceilings, and allowed mutation
  states;
- the worker idempotency-ledger identity, implementation revision, append-only
  prepare/result durability mode, and fail-closed incomplete-entry policy.

No hostname, port, URL, filesystem path, model, collection, provider, command,
or runtime image may be read from an ambient environment by the adapter.

Cost ceilings use an ISO-4217 currency, integer minor units, a pinned rounding
rule, and explicit treatment of provider retries/ambiguous accepted calls.
Floating-point money is forbidden.

## 4. Credentials and target identity

Production credentials are capability tokens for exact worker targets, not
free-form DSNs or provider keys exposed in protocol JSON. Required scopes are:

| Phase | Credential visible to that phase | Allowed effect |
|---|---|---|
| `inspect` | `ue_disposable_scene_inspect_spawn_cleanup` | exact load/spawn inspection and mandatory cleanup only |
| `render` | `ue_disposable_scene_render_staging_cleanup` | immutable exact-view render staging |
| `caption` | `caption_bounded_idempotent_call` | bounded, idempotent provider requests |
| `embed` | `embedding_immutable_vector_staging` | immutable vector artifact staging |
| `postgres` | `postgres_generation_write` | one-generation transaction and ledger |
| `qdrant` | `qdrant_generation_write` | immutable collection upsert/readback |
| `reconcile` | `postgres_generation_read_only`, `qdrant_generation_read_only`, and `ue_runtime_read_only` | observation and evidence only; no spawn or write |

Each capability has a non-secret generation and target identity in the plan.
The executor captures each private file exactly once and constructs a narrow
phase request; it never supplies the complete secret set to one ordinary
phase. Credential bytes never enter JSON, argv, environment, paths, logs, or
evidence. On Linux they are copied into a sealed anonymous `memfd` and passed
only for the current request with `SCM_RIGHTS`; the JSON frame contains only
the scope, generation, and target binding. The worker verifies the seals,
reads the descriptor once, and closes it. Reconcile uses separate read-only
capabilities. Rotation may resume only when the sealed non-secret generation
and target identity are unchanged.

Recovery uses four additional one-operation capabilities, each bound to the
same run, generation, target worker control plane, and original idempotency
identity: `semantic_index_phase_status_read`,
`semantic_index_phase_receipt_recover`,
`semantic_index_phase_work_cancel`, and
`semantic_index_generation_quarantine`. An ordinary phase capability cannot be
reused as a recovery capability or vice versa.

Target identities and runtime images must come from authenticated worker
`WhoAmI` and deployment/runtime attestation evidence. Copying plan strings into
a result is not an observation.

Although inspect must clean up its disposable scene, its spawn/destroy actions
are state changes and require the admin state-change approval. Reconcile does
not repeat that smoke operation; it consumes the already validated inspect
evidence with the three read-only capabilities above.

## 5. Generation isolation and activation

All catalog files, PostgreSQL rows, and Qdrant points are written to a new
generation. The active snapshot is never updated by the seven build phases.

PostgreSQL schema revision 2 may be retained only if every snapshot uses a new
database or schema and the active DSN is switched outside the build. If a table
is shared, schema revision 3 is required with snapshot-scoped keys, an
idempotency journal, generation status, and an atomic active-revision pointer.

Qdrant uses a new immutable collection per snapshot. A build cannot write the
active collection or change an active alias. Deterministic point IDs and exact
payload/vector digests are read back before reconciliation. An existing point
is an idempotent replay only when both payload and vector digests are equal;
ordinary upsert must not overwrite a different digest.

The successful build terminal state is `snapshot_built_verified`, not an
implicit Production activation. There is no cross-system atomic transaction
over a catalog, PostgreSQL, and Qdrant. Instead, every reader resolves all three
component identities from one versioned active-generation pointer. Activation
is a separate approved compare-and-swap of that single pointer after the new
generation is verified. Readiness re-resolves the pointer and component epoch;
failure compare-and-swaps back to the previous generation and records a
rollback receipt. A failed build never changes the pointer and quarantines only
the new generation.

For a first deployment, `previous_active_snapshot` may be exactly `null`; the
activation receipt records that bootstrap case explicitly. All later
activations require a non-null previous generation and matching pointer epoch.

## 6. Fixed worker operations

The phase worker operations are:

1. `inspect_exact_assets`;
2. `render_exact_views`;
3. `caption_exact_render_set`;
4. `embed_exact_text_set`;
5. `upsert_postgres_exact`;
6. `upsert_qdrant_exact`;
7. `reconcile_exact_snapshot`.

The only additional protocol control operations are
`query_phase_status`, `recover_phase_receipt`, `cancel_phase_work`, and
`quarantine_generation`. They accept the same sealed generation, phase, and
idempotency identities and cannot carry a free-form command or operation name.
The plan lists the seven phase operations and four control operations in
separate fixed arrays and binds the four exact recovery capability scopes from
section 4.

Control-operation mutation states are also closed. `query_phase_status` and
`recover_phase_receipt` are read-only and may only return `none` on success or
failure. `cancel_phase_work` and `quarantine_generation` return `committed` on
success and may fail only as `none`, `committed`, or `ambiguous`; they never
return `staged`. Only a `none` failure may be retryable, and every retry uses
the same control idempotency key. A recovered phase receipt is immutable and
must retain the original phase request digest.

There is no arbitrary UE Python, shell, subprocess, Compose, DDL, database
creation, model download, caller path, plugin discovery, URL override, or
free-form command. Provisioning, backup, activation, rollback, and destructive
cleanup remain separate admin operations.

Every request binds the reviewed job and approval-basis digests, host boot
identity, launcher-observed peer process-start and socket-inode bindings,
phase, exact input artifact digest, idempotency key, credential
generation/target, operation revision, and a host-shared absolute
`deadline_monotonic_ns`. Client and worker run on the same kernel monotonic
clock; each I/O derives its remaining timeout from that absolute value. The
worker keeps a durable ledger
keyed by `(job_revision, generation_id, phase, idempotency_key,
credential_generation, target_identity)`. The same key and request digest
returns the same immutable receipt; a different request digest hard-fails.
The validator consumes the seven raw, independently sealed phase requests,
validates them against the release-pinned wire schema, and binds each evidence
receipt to the raw request digest, operation revision, idempotency key, and
plan-pinned ledger identity and implementation revision. The worker service
receives the expected ledger revision as an independent pinned configuration
input; copying the request's own value into that input is prohibited production
wiring. A receipt cannot self-authorize a substituted request hash or ledger
revision.

The ledger publishes two immutable records per key. The prepare record is
durable before any remote effect; the result record is written to a private
temporary inode, fsynced, linked into its final no-replace name, and followed
by a directory fsync. A prepare record without an identical immutable result
is `recovery_required`, never permission for a blind retry. Same key plus a
different canonical request digest is a permanent conflict.

The generation ID is exactly `semantic-generation:` plus the lowercase hex of:

```text
SHA256(
  b"simworld-semantic-index-generation-id/v1\x00" ||
  ASCII(reviewed_job_revision) ||
  b"\x00" ||
  ASCII(approval_basis_sha256)
)
```

The independently sealed approval basis
already commits the target generation identities and a 128-bit random
generation nonce, so including the final generation ID in that basis would
create a forbidden hash cycle. Generation-specific PostgreSQL schema/Qdrant
collection/artifact namespace names are derived from the resulting generation
ID and separately verified by the semantic validator. Let `<hex>` be the 64
lowercase hex characters after `semantic-generation:`. The exact v1 mapping is:

- artifact workspace: `<workspace_root>/semantic_generation_<hex>`;
- PostgreSQL database: `semantic_gen_<first 50 hex characters>`;
- PostgreSQL schema: `snapshot_<first 54 hex characters>`;
- Qdrant collection: `semantic_gen_<hex>`.

The dense and sparse Qdrant vector names are separately pinned
generation-independent commitments. No caller-selected suffix, normalization,
truncation, or collision fallback is allowed. The worker atomically reserves
the generation before any artifact or mutation. A
different job, basis, or nonce cannot join an existing generation, and one
exclusive generation lock serializes state transitions.

Artifact metadata, the PostgreSQL generation ledger, and Qdrant collection
metadata each enforce the mapping
`generation_id -> reviewed_job_revision + approval_basis_sha256 + exact target
set`. An existing equal mapping may resume; any mismatch fails before mutation.

All connect, read, write, statement, provider, and batch timeouts are computed
from the one absolute deadline. Each phase runs in a worker-owned isolated
process group; at the deadline the worker kills and boundedly reaps that group.
This contains local computation but cannot revoke a provider or database
operation already accepted remotely. A timeout reports a fixed mutation state:
`none`, `staged`, `committed`, or `ambiguous`. `ambiguous` provider or mutation
states require manual reconciliation and are never automatically retried.

## 7. Executor-owned evidence

The worker first returns a bounded canonical
`simworld-semantic-index-worker-artifact-root/v1` envelope. That envelope is a
transport result, never the authoritative phase evidence. It names the fixed
phase-specific artifact schema and the content-addressed root/chunk digests
available through the held artifact capability. The executor fetches every
referenced byte, then constructs and validates the full
`simworld-semantic-index-phase-evidence/v1` document against the sealed job,
plan, approval basis, runtime observations, and prior phase receipts. Only
those executor-owned exact bytes may be published with
held-directory/no-follow/no-replace semantics and committed to phase state.

For large sets, a phase returns one bounded root receipt plus content-addressed
chunk manifests. The executor reads every chunk through its held artifact
capability, validates each canonical document, recomputes per-chunk hash/count/
bytes, and recomputes the ordered root. A worker-supplied Merkle or aggregate
digest without those bytes is insufficient.

The executor must validate and retain:

1. reviewed-selection receipt and rejection ledger;
2. deployment preflight receipt;
3. data, cost, admin state-change, and runtime approval receipts;
4. launcher and worker/runtime attestation receipt;
5. inspect receipt with exact IDs, paths/classes, live content revision,
   load/spawn, bounds, collision, material slots, PBR texture dependencies, and
   cleanup state;
6. render manifest with exact asset/view set, recipe, resolution, image hashes,
   UE revision, and cleanup state;
7. caption receipt with render digest, provider/project/model snapshot,
   prompt/schema pins, strict parse, record digests, usage/cost, and provider
   request/idempotency IDs;
8. embed receipt with catalog digest, model artifact digests, dimensions, exact
   asset/vector set, and vector-bundle manifest digest;
9. PostgreSQL receipt with deployment/schema/generation identities, transaction
   marker, exact row/ID set digest, catalog digest, and idempotency result;
10. Qdrant receipt with cluster/collection/vector identities, exact point/ID
    set digest, payload/vector digests, and readback result;
11. exact `simworld-asset-snapshot/v1` manifest bytes as compatibility evidence;
12. exact fresh `simworld-asset-live-audit/v1` bytes, including manifest and
    observations bindings, TTL, and expiry;
13. acceptance receipt containing the Chinese/English query probes and one
    Blueprint plus one StaticMesh load/spawn/material/PBR result referenced
    from the already validated inspect receipt (reconcile does not spawn);
14. previous-snapshot and rollback-readiness receipt.

Counts are secondary checks. Exact sorted asset-set digests, record digests,
vector-input/output digests, target identities, and receipt chains must agree.
An equal-count wrong-set must fail.

The existing snapshot v1 manifest and live-audit v1 receipt remain consumer
compatibility artifacts. They cannot by themselves satisfy v2 terminal truth;
the authoritative source is the exact-set, chunk-validated v2 reconcile and
acceptance receipt chain.

## 8. Error, recovery, and cancellation contract

Worker failures use a closed typed error with dependency, public code,
retryability, and mutation state. The wire response contains no remote
free-form message: the coordinator maps the closed dependency/code pair to a
fixed local public message. Before parsing or persisting a frame, both sides
scan for each declared secret's raw bytes and the exact bounded transform set
named by the adapter contract: one-to-eight-pass percent encoding, padded and
unpadded base64/base64url, SHA-256 hex, and raw capability hex. This is a
declared-transform leak detector, not a claim that arbitrary encodings (for
example base32 or application-specific reversible transforms) can be
exhaustively recognized. The adapter must set
`undeclared_transform_coverage_claimed=false`.
No raw provider
body, secret-file or host-absolute path, endpoint URL, header, token, DSN,
exception, stdout, or stderr enters durable evidence. Validated `/Game/...`
logical object paths and content-addressed relative artifact IDs are permitted
because they are required non-secret asset evidence.

Mutation states have fixed meaning: `none` permits a same-key retry; `staged`
permits status/recovery and continuation only within the same immutable
generation; `committed` requires recovery of the exact durable receipt before
local completion; `ambiguous` forbids automatic retry and requires a separately
authorized reconciliation decision.

The phase matrix is closed:

| Phase | Success state | Allowed failure states |
|---|---|---|
| `inspect` | `none` | `none`, `ambiguous` |
| `render` | `staged` | `none`, `staged`, `ambiguous` |
| `caption` | `committed` | `none`, `committed`, `ambiguous` |
| `embed` | `staged` | `none`, `staged`, `ambiguous` |
| `postgres` | `committed` | `none`, `committed`, `ambiguous` |
| `qdrant` | `committed` | `none`, `staged`, `committed`, `ambiguous` |
| `reconcile` | `none` | `none` |

Qdrant uniquely permits a failed `staged` result because an immutable new
collection can contain a verified partial point set before the phase is
complete. Only `none` may carry `retryable=true`. A `staged` result is
`retryable=false` in the ordinary phase result and can proceed only through a
fixed same-key status/recovery/continuation control operation, never a fresh
logical attempt. `committed` and `ambiguous` are also always
`retryable=false`; reconcile is read-only and therefore cannot truthfully
report a mutation ambiguity.

The adapter protocol adds fixed recovery operations for a previously journaled
idempotency key: query status, recover immutable receipt, cancel cancellable
work, and quarantine an unactivated generation. Recovery never accepts an
arbitrary command. Caption calls without provider idempotency support must use
the worker ledger; loss after provider acceptance but before durable receipt is
`ambiguous` and requires operator resolution before another paid call.

Coordinator restart reuses the same key, checks remote ledger state, and either
recovers the exact receipt or resumes safe staging. It never assumes that a
local timeout cancelled a remote mutation.

After an approval or ordinary phase capability expires, no new mutation may
start. A separately scoped recovery capability may still query status, recover
an existing immutable receipt, cancel cancellable work, or quarantine the same
generation. Cancellation is accepted only from the original run identity or an
attested admin-recovery role and cannot target another run.
The admin state-change approval contains a fixed
`allowed_control_operations` array listing all four control operations; merely
committing their names in the generic basis is not sufficient authorization
for cancel or quarantine.

## 9. Acceptance matrix

| ID | Tier | Required evidence before release |
|---|---|---|
| PA-01 | Offline | Exact static Production adapter ID/type/revision; unknown, dynamic, copied, or monkeypatched instances fail before credentials are read. |
| PA-02 | Offline + live | Pre-launch attestation verifies interpreter, source closure, plan, schemas, protocol, lockfile, SQL, and worker image. |
| PA-03 | Offline | Every endpoint, workspace, artifact, model, generation, and approval input is sealed in v2; no ambient discovery. |
| PA-04 | Offline + data-owner | Accepted set exactly matches the data-owner receipt and source candidates minus the rejection ledger. |
| PA-05 | Offline | Every phase receives only its scoped capability; reconcile receives distinct read-only capabilities. |
| PA-06 | Offline | One-shot secret capture, stable allowed rotation, and changed generation/target rejection survive resume. |
| PA-07 | Live | Every target and runtime image is independently observed/attested, never adapter-echoed. |
| PA-08 | Disposable UE | Inspect proves class, load/spawn, bounds, collision, materials, PBR textures, content revision, and cleanup for the exact set. |
| PA-09 | Disposable UE | Render proves exact views, dimensions, recipe, hashes, nonblank output, UE identity, and cleanup. |
| PA-10 | Offline + approved provider | Caption enforces no-tools strict schema, provider/model/prompt pins, budget, usage, and paid-call deduplication. |
| PA-11 | Offline + live model | Embed proves exact IDs, finite dimensions, model artifacts, input/output digests, and vector manifest. |
| PA-12 | Disposable PostgreSQL | PostgreSQL writes one new generation with an atomic transaction ledger and safe kill-after-commit replay. |
| PA-13 | Disposable Qdrant | Qdrant uses plan vector names and an immutable new collection with deterministic IDs, payload/vector digests, and readback. |
| PA-14 | Offline + live faults | Failures preserve dependency/root cause/retryability/mutation state without secret leakage. |
| PA-15 | Offline + live faults | External commit-before-local-receipt and all other crash boundaries reconcile without duplicate logical effects. |
| PA-16 | Offline + live | Executor validates snapshot/live-audit bytes, exact-set receipt chain, TTL, and runtime attestation itself. |
| PA-17 | Live acceptance | Chinese/English shadow queries and Blueprint/StaticMesh PBR smoke are terminal requirements. |
| PA-18 | Disposable generation | Any build failure leaves the previous active snapshot ready and quarantines only the new generation. |
| PA-19 | Activation drill | One active-generation pointer is compare-and-swapped and records either an activation or rollback receipt; readers never compose mixed epochs. |
| PA-20 | Offline + live scan | Logs, state, receipts, artifacts, errors, argv, and environment contain no secret-file/host-path/DSN/token material or secret digest. |
| PA-21 | Offline | A fake adapter returning correct counts and plausible hashes cannot produce a verified terminal state. |

For every mutating phase, fault injection covers: before external side effect,
after external commit but before worker receipt, after worker receipt but before
executor evidence publication, and after evidence publication but before local
state commit. The suite also covers trickle/oversized frames, wrong peer,
wrong-set equal-count responses, concurrent same-key requests, changed payload
under one key, different keys against one generation, stale/future/forged/
wrong-role approval receipts, tampered evidence chunks, pointer-CAS crash,
same-UID socket replacement, and terminal recovery. Secret scans cover raw,
URL-encoded, base64, and SHA-256 forms across frames, argv, environment, logs,
state, receipts, and errors.

## 10. Delivery order and live gates

Implementation order is deliberately fail-closed:

1. add v2 schemas and pure validators;
2. add reviewed-selection and approval preparation with dry-run/private atomic
   publication;
3. add evidence storage and state/terminal v2 validation;
4. add fixed UDS worker protocol and deterministic fake worker;
5. add the static Production adapter and complete PA-01 through PA-21 offline;
6. build and attest the real worker image and generation-isolated storage path;
7. obtain separate data-owner, cost-owner, admin state-change, and runtime
   approvals;
8. execute one bounded disposable generation, preserve exact evidence, and
   keep activation off;
9. run shadow retrieval and disposable UE PBR smoke;
10. request a separate activation approval, then exercise rollback.

The v2 implementation uses a separate version-aware executor/client source
closure. It must not weaken the v1 fixture loader's existing no-socket,
no-network, singleton-registry contract; v1 tests continue to prove that path
has no Production transport capability.

Steps 7 through 10 are not authorized by this addendum. Until they occur, the
truthful state remains: code path under construction, no authoritative full
catalog, no live semantic snapshot, and no Production activation.

### 10.1 Offline implementation checkpoint (2026-07-21)

The repository currently closes 21 release-pinned formal schemas, including
the launcher-verification receipt. The unregistered adapter binds 20 schema
digests, and the local closed-subset runtime validates a 24-schema semantic
corpus. The following code paths have focused offline tests under both the
uv-managed runtime and isolated system Python where their dependencies permit:

- Ed25519 verification of the launcher receipt and exact, one-use composition
  with an attested worker FD handoff;
- a pathless caller-supplied-directory-FD ledger with durable prepare/result
  publication, immutable replay, conflict detection, and ordinary phase
  identity/revision binding;
- a separately pinned pathless control ledger for all four closed recovery
  operations; an incomplete prepare yields a deterministic recovery-required
  result and never blind re-execution;
- a single-threaded fork/session/process-group isolation primitive with an
  absolute monotonic deadline, bounded result channel, complete group kill and
  bounded leader reap; authenticated phase/control service paths commit the
  exact validated bytes before reply;
- real Ed25519 verification for all six approval purposes followed by an opaque
  six-party aggregate bound to independently supplied expected receipt, scope,
  authorization, issuer, key, trust, environment, basis and validity pins;
- release-pinned schema-source validation, cross-document semantic validation,
  and an explicitly empty Production adapter registry.

This checkpoint is deliberately not a Production adapter or PA-01 through
PA-21 completion. The seven real target executors, a fixed non-injectable worker
dispatch/bootstrap, static release registration, a reproducible worker image
and recalculated source closure, root-capability public handoff evidence, and
live target observations are still absent. The isolated callback boundary is a
reviewed primitive, not permission to supply arbitrary Production code. The
current uv Python also lacks the Linux `memfd`/seal APIs needed by the launcher
path; an ephemeral Python 3.10 validation environment proves code compatibility
only and is not an attested deployment environment.

Two cryptographic provenance projections remain release blockers. Approval
receipts are now cryptographically verified against explicit issuer trust roots
and aggregated only as opaque verified results, but that aggregate proof is not
yet a release-pinned schema and is not signed into the launcher, execution state
or terminal receipt. It cannot authorize a launch. Phase evidence still carries
only worker payload/signature digests and no detached signature bytes, signer
identity, trust bundle or public-key binding; the verifier therefore fails
closed. In addition, the execution-plan schema describes a future
`production_capable=true` adapter while the release-pinned adapter manifest
schema intentionally permits only the current unregistered
`production_capable=false` instance; no adapter can satisfy both until a new
reviewed registered revision is introduced.

## 11. Explicit prohibitions

The Production adapter or worker must never import, execute, wrap, or delegate
to `full_asset_index_runner.py`, `index_assets.py`,
`ue58_parallel_asset_index_runner.py`, `migrate_to_postgres.py`,
`build_qdrant_index.py`, `apply_schema.py`, or
`ensure_postgres_database.py`. Pure deterministic algorithms may be rewritten
behind the v2 typed contracts and covered by new tests; legacy CLI/runtime
behavior is not an accepted backend.

The adapter never starts Unreal, databases, Qdrant, embedding, Coturn, Cirrus,
containers, or public listeners. Provisioning and service lifecycle remain
explicit operator/admin responsibilities.

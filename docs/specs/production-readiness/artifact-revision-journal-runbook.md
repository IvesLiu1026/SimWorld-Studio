# Artifact Revision Journal Runbook

This component is the append-only persistence boundary for committed VISTA
imports, terminal scene builds, terminal timeline runs, and terminal Text or
Visual Review runs. The Studio runtime derives owner/session/correlation from
the authenticated server-side session, persisted operation record, or active
lease; request-body identity fields are never journal authority. Review Off
(`vanilla`) never invokes a provider and never writes a journal revision.

## Production configuration and readiness

Configure all three variables before starting a production Studio:

```bash
VISTA_ARTIFACT_JOURNAL_ENABLED=1
VISTA_ARTIFACT_JOURNAL_ROOT=/var/lib/simworld/artifact-journal
VISTA_ARTIFACT_JOURNAL_RETENTION_DAYS=365
install -d -m 0700 -o simworld -g simworld /var/lib/simworld/artifact-journal
```

The root must already exist in production. `/health/ready` reports the
`artifact_journal` feature as blocking until the root is an absolute,
canonical, owner-only POSIX directory and `verifyIntegrity()` has validated
the full entry chain and every active content blob. A missing, corrupt,
symlinked, group-readable, or wrong-owner root never degrades to optional.
Development and tests may leave journaling disabled or inject a recorder, but
production cannot disable it.

The process performs and coalesces that expensive full verification at
startup (and after a write failure), then caches the result. Routine readiness
probes still revalidate the root and Review outbox directory type, owner, mode,
and canonical path, but do not repeatedly take the journal lock or hash every
blob. Cancelling one HTTP readiness request only stops that request waiting;
it does not cancel the shared verification or revoke a previously verified
process write gate.

The current chain has a hard 10,000-entry safety capacity. Readiness becomes
blocking at that boundary because the next append cannot succeed. Retention
removes eligible content blobs but deliberately preserves chain entries, so an
operator must schedule a reviewed backup-and-rotation migration before the
chain reaches capacity; startup never rotates or truncates it automatically.

Domain records and the journal are separate filesystem stores; there is no
cross-filesystem transaction. Services therefore persist their terminal domain
record, `fsync` the containing private directory, append its deterministic
journal revision, and only then return a
durable terminal response. If append fails, status/commit retries replay the
same fixed creator session, domain timestamp, revision, expiry, and exact
canonical domain-record digest. A journal error does not rewrite a successful UE execution as a
domain failure. Review streaming similarly holds authoritative PASS,
`loop_done`, and final `done` frames until its compact terminal summary is
appended; a journal or bounded-gate failure discards those success frames.

Review has one additional crash boundary because the critic/provider may be a
paid, non-idempotent call. The browser creates one stable `runId`, persists it
with the conversation, and reuses it for an explicit Retry Review operation;
production rejects a Review request without that id before registry or provider
execution. Before invoking the provider, the runtime captures and hashes the
exact UE actor snapshot, then persists an owner-scoped v3 outbox record with a
server-minted journal identity. The compact terminal then moves through
`prepared -> terminal_pending -> published`:
`terminal_pending` is fsynced before journal append, and `published` is fsynced
only after append succeeds. A retry with the same public `runId` reuses the
persisted creator session and journal identity, publishes or replays the saved
terminal, and never invokes the provider again. Startup verification also
replays `terminal_pending` records idempotently. An orphaned `prepared` record
is deliberately not guessed safe to rerun; that run returns
`REVIEW_TERMINAL_RECOVERY_REQUIRED` for operator review.

Only unresolved `prepared` and `terminal_pending` receipts consume the
per-owner active allocation (256 by default). `published` receipts remain
immutable evidence and still count toward the global 10,000-record capacity,
but cannot permanently starve that owner of new runs. A reviewed orphan may
also move from `prepared -> abandoned`; it remains on disk and receives a
separate append-only `vista-review-abandonment` journal revision. The original
public run id then fails permanently with `ARTIFACT_REVIEW_RUN_ABANDONED` and
can never invoke or publish a provider result. Only a newly minted browser run
id can start a later attempt.

The v2 terminal binds the pre- and post-execution actor-snapshot digests, slot
and hashed lease identity, provider/model, and SHA-256 ids derived from the
actual screenshot bytes. It never persists screenshot paths, prompts, provider
prose, or raw lease ids. If the active lease has a journaled VISTA build, the
terminal also carries that exact scene-build revision/content digest as source
lineage. A replay must observe the stored post-scene binding; scene drift is an
idempotency conflict, not permission to call the provider again. PASS cannot be
published without provider/model, content-hash evidence, and a post-scene
binding. Failure to capture the post-scene is itself journaled as a failed
terminal and the held success response is discarded.

## Storage contract

- Construct `createArtifactRevisionJournal({ root })` with a dedicated,
  absolute, canonical path owned by the service account.
- The root and its `entries/`, `blobs/`, `pending/`, and `review-outbox/`
  children must be mode `0700`. Journal and outbox files are mode `0600`.
  Symlinks, hard-linked live files,
  non-regular files, wrong owners, wrong modes, and non-canonical root paths
  fail closed.
- Every revision entry is an atomic create-only file and links to the previous
  entry digest. Content is canonical JSON in a separate immutable blob. Raw
  idempotency keys are never persisted.
- The root must be on one local filesystem that supports `O_NOFOLLOW`, hard
  links, directory `fsync`, and exclusive file creation. NFS or object-store
  mounts are outside this contract.

The current runtime accepts only `simworld-review-terminal-outbox/v3` records.
Before the first deployment of this unreleased format, verify that
`review-outbox/` is empty apart from no lock file. A future migration from a
deployed older format requires a separately reviewed offline migration; never
rename or rewrite receipt files while Studio is running.

## Operator abandonment of an orphaned paid Review

Abandonment is an exceptional offline/operator action, not an HTTP route and
not an automatic timeout. First reconcile the provider billing/request logs
and build a redacted evidence pack. Its canonical bytes must have a recorded
SHA-256 digest. Record the exact outbox filename digest, owner, creator session,
server-minted `journal_run_id`, one stable decision id, and one of these reason
codes:

- `provider_not_started`
- `provider_result_unrecoverable`
- `provider_charge_reconciled_no_result`

Provision one high-entropy operator token outside the Studio environment and
configure only its digest plus the operator identity for the operator process:

```bash
export VISTA_REVIEW_ABANDON_OPERATOR_ID='production-operator-id'
export VISTA_REVIEW_ABANDON_TOKEN_SHA256='<sha256-of-at-least-32-byte-token>'
```

Do not put the raw token in an environment variable, command argument, shell
history, evidence pack, or log. Supply it to the operator process through a
protected inherited file descriptor. After a forced successful readiness
verification, the operator integration calls the recorder with the reviewed
identity and decision:

```js
await runtime.recorder.abandonPreparedReview({
  identity: {
    lookupDigest: reviewedReceipt.lookup_digest,
    ownerId: reviewedReceipt.owner_id,
    sessionId: reviewedReceipt.session_id,
    journalRunId: reviewedReceipt.journal_run_id,
  },
  authorizationToken: tokenReadFromProtectedInheritedFd,
  decision: {
    decisionId: "abandon-20260721-001",
    decidedAt: "2026-07-21T12:34:56.000Z",
    reasonCode: "provider_charge_reconciled_no_result",
    evidenceDigest: "<64-lowercase-hex-evidence-pack-digest>",
  },
});
```

The runtime validates all four receipt identity fields under the outbox lock,
uses a constant-time comparison against the configured token digest, appends
the audit revision first, and only then fsyncs the `abandoned` receipt. A crash
after audit append but before receipt publication stays fail-closed as
`prepared`; rerun the exact same decision after `readinessProbe({force:true})`.
Changing any decision field is an idempotency conflict. Never abandon
`terminal_pending` or `published`: both already have an authoritative terminal
and must use normal recovery/replay.

The shared runtime performs the append; direct service calls are shown only to
document the underlying primitive:

```js
const { createArtifactRevisionJournal } = require("./artifact-revision-journal");

const journal = createArtifactRevisionJournal({
  root: "/var/lib/simworld/artifact-journal",
});

await journal.append({
  kind: "vista-scene-build",
  artifactId: "mmg_040",
  revision: "build-v1",
  ownerId: authenticatedLease.ownerId,
  sessionId: authenticatedLease.sessionId,
  correlationId: run.correlationId,
  idempotencyKey: serverGeneratedIdempotencyKey,
  expiresAt: "2026-08-01T00:00:00.000Z",
  sourceLineage: [],
  content: verifiedBuildRecord,
});
```

Public reads are owner-scoped and pagination is bounded:

```js
await journal.readRevision({ kind, artifactId, revision, ownerId });
await journal.listRevisions({ ownerId, kind, artifactId, limit: 50, cursor });
```

Studio currently exposes no public journal read route. If one is added later,
it must derive `ownerId` from the same authenticated session and retain these
bounds; query/body owner fields are not authority.

## Integrity, retention, and backup

Run `verifyIntegrity()` before declaring journal readiness. A changed record,
missing chain link, missing active blob, unsafe file, or invalid retained target
throws `ARTIFACT_JOURNAL_CORRUPT`; do not serve partial results.

Retention is always two-step and is never run by normal service startup.
Planning does not write or delete anything:

```js
const plan = await journal.planRetention();
// Persist/review plan.plan_digest through an operator-controlled channel.
await journal.applyRetention(plan, {
  operatorApply: true,
  operatorId: authenticatedOperator.id,
});
```

Apply revalidates the exact head and plan under the filesystem lock. It appends
an immutable retention event before unlinking only its exact expired, non-head
blob paths. The audit entry remains forever. If deletion is interrupted, the
next dry-run exposes `pending_deletions`; a new explicit apply completes only
those already-authorized paths. Never script recursive deletion in this root.

Backup support only creates and verifies a deterministic file manifest; it does
not create an archive, copy files, or contact a network service:

```js
const manifest = await journal.generateBackupManifest();
await journal.verifyBackupManifest(manifest);
```

Copy the manifest-listed hash-only relative paths with a separate approved
backup process, preserving `0600` files and `0700` directories. Verify the
manifest immediately before and after the bounded copy. Orphan blobs from a
crash before entry publication are reported by `verifyIntegrity()` but are not
part of a backup or retention plan; removal requires a separate, reviewed
operator procedure.

## Fixed safety limits and recovery

Defaults cap content at 1 MiB, journal entries and Review outbox receipts at
10,000 each, an outbox receipt at 32 KiB, JSON depth at 16, JSON nodes at
50,000, lineage items at 64, a public page at 100, and a retention apply at 256
targets. The exact outbox-capacity-fulfilling create immediately makes readiness
blocking while still allowing an existing receipt to be finalized/replayed.
Callers may lower but not raise these hard limits.
Secret-shaped keys and values, credential URLs, private-key material, local
home/system paths, traversal paths, and secret-file paths are rejected.

A process crash can leave `.artifact-journal.lock` or
`review-outbox/.review-outbox.lock`; the module intentionally does not guess
that a lock is stale or remove it. Stop all journal writers, inspect the owning
process and root permissions, run a read-only filesystem review, remove only
that exact lock as the operator, then run
`verifyIntegrity()`. Never auto-repair, renumber, rewrite, or skip a damaged
chain.

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
record, append its deterministic journal revision, and only then return a
durable terminal response. If append fails, status/commit retries replay the
same fixed creator session, domain timestamp, revision, expiry, and bounded
summary. A journal error does not rewrite a successful UE execution as a
domain failure. Review streaming similarly holds authoritative PASS,
`loop_done`, and final `done` frames until its compact terminal summary is
appended; a journal or bounded-gate failure discards those success frames.

Review has one additional crash boundary because the critic/provider may be a
paid, non-idempotent call. Before invoking it, the runtime persists an
owner-scoped outbox record with a server-minted journal identity. The compact
terminal then moves through `prepared -> terminal_pending -> published`:
`terminal_pending` is fsynced before journal append, and `published` is fsynced
only after append succeeds. A retry with the same public `runId` reuses the
persisted creator session and journal identity, publishes or replays the saved
terminal, and never invokes the provider again. Startup verification also
replays `terminal_pending` records idempotently. An orphaned `prepared` record
is deliberately not guessed safe to rerun; that run returns
`REVIEW_TERMINAL_RECOVERY_REQUIRED` for operator review.

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
targets. Callers may lower but not raise these hard limits.
Secret-shaped keys and values, credential URLs, private-key material, local
home/system paths, traversal paths, and secret-file paths are rejected.

A process crash can leave `.artifact-journal.lock` or
`review-outbox/.review-outbox.lock`; the module intentionally does not guess
that a lock is stale or remove it. Stop all journal writers, inspect the owning
process and root permissions, run a read-only filesystem review, remove only
that exact lock as the operator, then run
`verifyIntegrity()`. Never auto-repair, renumber, rewrite, or skip a damaged
chain.

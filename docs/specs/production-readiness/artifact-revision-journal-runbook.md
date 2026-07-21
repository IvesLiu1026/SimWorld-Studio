# Artifact Revision Journal Runbook

This component is an integration-neutral, append-only persistence primitive for
scene builds, timeline runs, review receipts, and later production artifacts.
It is not wired into any service yet. The integrating service must derive
`ownerId` from its authenticated server-side lease; it must never accept an
untrusted request body's owner or operator identity.

## Storage contract

- Construct `createArtifactRevisionJournal({ root })` with a dedicated,
  absolute, canonical path owned by the service account.
- The root and its `entries/`, `blobs/`, and `pending/` children must be mode
  `0700`. Journal files are mode `0600`. Symlinks, hard-linked live files,
  non-regular files, wrong owners, wrong modes, and non-canonical root paths
  fail closed.
- Every revision entry is an atomic create-only file and links to the previous
  entry digest. Content is canonical JSON in a separate immutable blob. Raw
  idempotency keys are never persisted.
- The root must be on one local filesystem that supports `O_NOFOLLOW`, hard
  links, directory `fsync`, and exclusive file creation. NFS or object-store
  mounts are outside this contract.

Minimal append:

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

## Integrity, retention, and backup

Run `verifyIntegrity()` before declaring journal readiness. A changed record,
missing chain link, missing active blob, unsafe file, or invalid retained target
throws `ARTIFACT_JOURNAL_CORRUPT`; do not serve partial results.

Retention is always two-step. Planning does not write or delete anything:

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

Defaults cap content at 1 MiB, journal entries at 10,000, JSON depth at 16,
JSON nodes at 50,000, lineage items at 64, a public page at 100, and a retention
apply at 256 targets. Callers may lower but not raise these hard limits.
Secret-shaped keys and values, credential URLs, private-key material, local
home/system paths, traversal paths, and secret-file paths are rejected.

A process crash can leave `.artifact-journal.lock`; the module intentionally
does not guess that a lock is stale or remove it. Stop all journal writers,
inspect the owning process and root permissions, run a read-only filesystem
review, remove only that exact lock as the operator, then run
`verifyIntegrity()`. Never auto-repair, renumber, rewrite, or skip a damaged
chain.

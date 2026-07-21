"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");

const {
  ArtifactRevisionJournalError,
  createArtifactRevisionJournal,
} = require("../artifact-revision-journal");

async function tempJournal(t, overrides = {}) {
  const parent = await fs.mkdtemp(path.join(os.tmpdir(), "simworld-artifact-journal-"));
  const root = path.join(parent, "journal");
  t.after(() => fs.rm(parent, { recursive: true, force: true }));
  return {
    root,
    journal: createArtifactRevisionJournal({ root, ...overrides }),
  };
}

function request(overrides = {}) {
  return {
    kind: "vista-scene",
    artifactId: "mmg_040",
    revision: "scene-v1",
    ownerId: "owner-a",
    sessionId: "session-a",
    correlationId: "corr-a",
    idempotencyKey: "idem-mmg-040-v1",
    expiresAt: "2026-08-01T00:00:00.000Z",
    sourceLineage: [{
      kind: "vista-import",
      artifactId: "mmg_040-import",
      revision: "import-v1",
      contentDigest: "a".repeat(64),
    }],
    content: {
      actors: [{ asset: "/Game/VISTA/Chair", transform: [0, 0, 0] }],
      status: "verified",
    },
    ...overrides,
  };
}

function expectCode(code) {
  return (error) => error instanceof ArtifactRevisionJournalError && error.code === code;
}

async function names(directory) {
  return (await fs.readdir(directory)).sort();
}

function permissionBits(stat) {
  return stat.mode & 0o777;
}

test("append creates canonical owner-only records and public reads enforce owner ACLs", async (t) => {
  const { root, journal } = await tempJournal(t, {
    now: () => new Date("2026-07-21T00:00:00.000Z"),
  });
  const first = await journal.append(request({
    content: { z: 1, a: { label: "chair" } },
  }));
  assert.equal(first.created, true);
  assert.equal(first.idempotent, false);
  assert.equal(first.revision.status, "active");
  assert.equal(
    first.revision.content_digest,
    crypto.createHash("sha256").update('{"a":{"label":"chair"},"z":1}').digest("hex"),
  );

  const read = await journal.readRevision({
    kind: "vista-scene",
    artifactId: "mmg_040",
    revision: "scene-v1",
    ownerId: "owner-a",
  });
  assert.deepEqual(read.content, { a: { label: "chair" }, z: 1 });
  await assert.rejects(
    journal.readRevision({
      kind: "vista-scene",
      artifactId: "mmg_040",
      revision: "scene-v1",
      ownerId: "owner-b",
    }),
    expectCode("ARTIFACT_ACCESS_DENIED"),
  );
  assert.deepEqual((await journal.listRevisions({ ownerId: "owner-b" })).revisions, []);

  await journal.append(request({
    artifactId: "mmg_041",
    revision: "scene-v2",
    correlationId: "corr-b",
    idempotencyKey: "idem-mmg-041-v2",
  }));
  const pageOne = await journal.listRevisions({ ownerId: "owner-a", limit: 1 });
  assert.equal(pageOne.revisions.length, 1);
  assert.ok(pageOne.next_cursor);
  const pageTwo = await journal.listRevisions({
    ownerId: "owner-a",
    limit: 1,
    cursor: pageOne.next_cursor,
  });
  assert.equal(pageTwo.revisions.length, 1);
  assert.equal(pageTwo.next_cursor, null);
  await assert.rejects(
    journal.listRevisions({ ownerId: "owner-b", cursor: pageOne.next_cursor }),
    expectCode("ARTIFACT_JOURNAL_INVALID_INPUT"),
  );

  assert.equal(permissionBits(await fs.stat(root)), 0o700);
  for (const directory of ["entries", "blobs", "pending"]) {
    assert.equal(permissionBits(await fs.stat(path.join(root, directory))), 0o700);
  }
  for (const directory of ["entries", "blobs"]) {
    for (const filename of await names(path.join(root, directory))) {
      assert.equal(permissionBits(await fs.stat(path.join(root, directory, filename))), 0o600);
    }
  }
  const entryText = await fs.readFile(path.join(root, "entries", (await names(path.join(root, "entries")))[0]), "utf8");
  assert.doesNotMatch(entryText, /idem-mmg-040-v1/);
  assert.equal((await journal.verifyIntegrity()).status, "valid");
});

test("idempotency returns the existing canonical request and rejects changed bytes", async (t) => {
  let now = Date.parse("2026-07-21T00:00:00.000Z");
  const { journal } = await tempJournal(t, { now: () => new Date(now) });
  const original = request({ content: { b: 2, a: 1 } });
  const first = await journal.append(original);
  const second = await journal.append({ ...original, content: { a: 1, b: 2 } });
  assert.equal(second.created, false);
  assert.equal(second.idempotent, true);
  assert.equal(second.revision.sequence, first.revision.sequence);

  await assert.rejects(
    journal.append({ ...original, content: { a: 1, b: 3 } }),
    expectCode("ARTIFACT_IDEMPOTENCY_CONFLICT"),
  );
  await assert.rejects(
    journal.append({ ...original, idempotencyKey: "different-idem-key" }),
    expectCode("ARTIFACT_REVISION_CONFLICT"),
  );

  now = Date.parse("2026-08-02T00:00:00.000Z");
  const expiredRetry = await journal.append({ ...original, content: { a: 1, b: 2 } });
  assert.equal(expiredRetry.idempotent, true);
  assert.equal((await journal.verifyIntegrity()).revision_count, 1);
});

test("filesystem lock serializes concurrent writers into one unbroken chain", async (t) => {
  const { root, journal: first } = await tempJournal(t, {
    now: () => new Date("2026-07-21T00:00:00.000Z"),
    lockRetryMs: 1,
  });
  const second = createArtifactRevisionJournal({
    root,
    now: () => new Date("2026-07-21T00:00:00.000Z"),
    lockRetryMs: 1,
  });
  const [left, right] = await Promise.all([
    first.append(request({
      revision: "scene-v1",
      idempotencyKey: "concurrent-idem-v1",
      correlationId: "concurrent-a",
    })),
    second.append(request({
      revision: "scene-v2",
      idempotencyKey: "concurrent-idem-v2",
      correlationId: "concurrent-b",
      content: { actors: [{ asset: "/Game/VISTA/Table" }] },
    })),
  ]);
  assert.deepEqual(new Set([left.revision.sequence, right.revision.sequence]), new Set([1, 2]));
  const integrity = await first.verifyIntegrity();
  assert.equal(integrity.entry_count, 2);
  assert.equal(integrity.revision_count, 2);

  const entries = await names(path.join(root, "entries"));
  const firstRecord = JSON.parse(await fs.readFile(path.join(root, "entries", entries[0]), "utf8"));
  const secondRecord = JSON.parse(await fs.readFile(path.join(root, "entries", entries[1]), "utf8"));
  assert.equal(firstRecord.previous_digest, null);
  assert.equal(secondRecord.previous_digest, firstRecord.record_digest);
  assert.equal(integrity.head_digest, secondRecord.record_digest);
});

test("tampered or missing chain entries fail closed", async (t) => {
  await t.test("tamper", async (t) => {
    const { root, journal } = await tempJournal(t, {
      now: () => new Date("2026-07-21T00:00:00.000Z"),
    });
    await journal.append(request());
    const entryPath = path.join(root, "entries", (await names(path.join(root, "entries")))[0]);
    const record = JSON.parse(await fs.readFile(entryPath, "utf8"));
    record.artifact.id = "tampered";
    await fs.writeFile(entryPath, JSON.stringify(record), { mode: 0o600 });
    await assert.rejects(journal.verifyIntegrity(), expectCode("ARTIFACT_JOURNAL_CORRUPT"));
  });

  await t.test("missing link", async (t) => {
    const { root, journal } = await tempJournal(t, {
      now: () => new Date("2026-07-21T00:00:00.000Z"),
    });
    await journal.append(request());
    await journal.append(request({
      artifactId: "mmg_041",
      revision: "scene-v2",
      idempotencyKey: "missing-chain-v2",
    }));
    const entryPath = path.join(root, "entries", (await names(path.join(root, "entries")))[0]);
    await fs.unlink(entryPath);
    await assert.rejects(journal.verifyIntegrity(), expectCode("ARTIFACT_JOURNAL_CORRUPT"));
  });
});

test("symlink content is never followed and missing live content fails closed", async (t) => {
  const { root, journal } = await tempJournal(t, {
    now: () => new Date("2026-07-21T00:00:00.000Z"),
  });
  await journal.append(request());
  const blobPath = path.join(root, "blobs", (await names(path.join(root, "blobs")))[0]);
  await fs.unlink(blobPath);
  await fs.symlink("/etc/passwd", blobPath);
  await assert.rejects(
    journal.readRevision({
      kind: "vista-scene",
      artifactId: "mmg_040",
      revision: "scene-v1",
      ownerId: "owner-a",
    }),
    expectCode("ARTIFACT_JOURNAL_CORRUPT"),
  );
  await assert.rejects(journal.verifyIntegrity(), expectCode("ARTIFACT_JOURNAL_CORRUPT"));
});

test("a symlinked or group-readable operator root is rejected", async (t) => {
  const parent = await fs.mkdtemp(path.join(os.tmpdir(), "simworld-artifact-root-"));
  t.after(() => fs.rm(parent, { recursive: true, force: true }));
  const realRoot = path.join(parent, "real-root");
  await fs.mkdir(realRoot, { mode: 0o700 });
  const linkedRoot = path.join(parent, "linked-root");
  await fs.symlink(realRoot, linkedRoot);
  await assert.rejects(
    createArtifactRevisionJournal({ root: linkedRoot }).initialize(),
    expectCode("ARTIFACT_JOURNAL_ROOT_INSECURE"),
  );

  await fs.chmod(realRoot, 0o750);
  await assert.rejects(
    createArtifactRevisionJournal({ root: realRoot }).initialize(),
    expectCode("ARTIFACT_JOURNAL_ROOT_INSECURE"),
  );
});

test("secret-bearing data and bounded JSON limit violations are rejected before write", async (t) => {
  const { root, journal } = await tempJournal(t, {
    now: () => new Date("2026-07-21T00:00:00.000Z"),
    limits: { maxArtifactBytes: 128, maxArrayItems: 2, maxJsonDepth: 3 },
  });
  await assert.rejects(
    journal.append(request({ content: { apiKey: "not-allowed-here" } })),
    expectCode("ARTIFACT_JOURNAL_SECRET_REJECTED"),
  );
  await assert.rejects(
    journal.append(request({ content: { note: "Bearer abcdefghijklmnop" } })),
    expectCode("ARTIFACT_JOURNAL_SECRET_REJECTED"),
  );
  await assert.rejects(
    journal.append(request({ content: { source: "/home/yhliu/.ssh/id_rsa" } })),
    expectCode("ARTIFACT_JOURNAL_SECRET_REJECTED"),
  );
  await assert.rejects(
    journal.append(request({ content: { values: [1, 2, 3] } })),
    expectCode("ARTIFACT_JOURNAL_LIMIT_EXCEEDED"),
  );
  await assert.rejects(
    journal.append(request({ content: { text: "x".repeat(256) } })),
    expectCode("ARTIFACT_JOURNAL_LIMIT_EXCEEDED"),
  );
  assert.deepEqual(await names(path.join(root, "entries")), []);
  assert.deepEqual(await names(path.join(root, "blobs")), []);
});

test("retention is dry-run by default and applies only expired non-head revisions", async (t) => {
  let now = Date.parse("2026-07-21T00:00:00.000Z");
  const { root, journal } = await tempJournal(t, { now: () => new Date(now) });
  await journal.append(request({
    revision: "scene-v1",
    idempotencyKey: "retention-scene-v1",
    expiresAt: "2026-07-21T00:10:00.000Z",
  }));
  now = Date.parse("2026-07-21T00:05:00.000Z");
  await journal.append(request({
    revision: "scene-v2",
    idempotencyKey: "retention-scene-v2",
    expiresAt: "2026-07-21T00:15:00.000Z",
    content: { actors: [{ asset: "/Game/VISTA/Table" }] },
  }));
  now = Date.parse("2026-07-21T00:20:00.000Z");

  const before = await names(path.join(root, "blobs"));
  const plan = await journal.planRetention();
  assert.equal(plan.candidates.length, 1);
  assert.equal(plan.candidates[0].sequence, 1);
  assert.equal(plan.pending_deletions.length, 0);
  assert.deepEqual(await names(path.join(root, "blobs")), before);
  await assert.rejects(
    journal.applyRetention(plan, { operatorId: "operator-a" }),
    expectCode("ARTIFACT_RETENTION_APPLY_REQUIRED"),
  );
  assert.deepEqual(await names(path.join(root, "blobs")), before);

  const applied = await journal.applyRetention(plan, {
    operatorApply: true,
    operatorId: "operator-a",
  });
  assert.equal(applied.retained_count, 1);
  assert.equal(applied.deleted_blob_count, 1);
  assert.equal((await names(path.join(root, "blobs"))).length, 1);
  const integrity = await journal.verifyIntegrity();
  assert.equal(integrity.status, "valid");
  assert.equal(integrity.entry_count, 3);
  assert.equal(integrity.retained_revision_count, 1);

  const listed = await journal.listRevisions({
    ownerId: "owner-a",
    kind: "vista-scene",
    artifactId: "mmg_040",
  });
  assert.deepEqual(listed.revisions.map((item) => item.status), ["retained", "active"]);
  await assert.rejects(
    journal.readRevision({
      kind: "vista-scene",
      artifactId: "mmg_040",
      revision: "scene-v1",
      ownerId: "owner-a",
    }),
    expectCode("ARTIFACT_REVISION_RETAINED"),
  );
  assert.deepEqual(
    (await journal.readRevision({
      kind: "vista-scene",
      artifactId: "mmg_040",
      revision: "scene-v2",
      ownerId: "owner-a",
    })).content,
    { actors: [{ asset: "/Game/VISTA/Table" }] },
  );
  const secondPlan = await journal.planRetention();
  assert.equal(secondPlan.candidates.length, 0);
  assert.equal(secondPlan.pending_deletions.length, 0);
});

test("retention refuses a stale plan and a symlink race before recording deletion", async (t) => {
  let now = Date.parse("2026-07-21T00:00:00.000Z");
  const { root, journal } = await tempJournal(t, { now: () => new Date(now) });
  await journal.append(request({
    revision: "scene-v1",
    idempotencyKey: "symlink-retention-v1",
    expiresAt: "2026-07-21T00:10:00.000Z",
  }));
  now = Date.parse("2026-07-21T00:05:00.000Z");
  await journal.append(request({
    revision: "scene-v2",
    idempotencyKey: "symlink-retention-v2",
    expiresAt: "2026-07-21T00:15:00.000Z",
  }));
  now = Date.parse("2026-07-21T00:20:00.000Z");
  const plan = await journal.planRetention();
  const candidatePath = path.join(root, plan.candidates[0].blob_relative_path);
  await fs.unlink(candidatePath);
  await fs.symlink("/etc/passwd", candidatePath);
  await assert.rejects(
    journal.applyRetention(plan, { operatorApply: true, operatorId: "operator-a" }),
    expectCode("ARTIFACT_JOURNAL_CORRUPT"),
  );
  assert.equal((await names(path.join(root, "entries"))).length, 2);
});

test("retention rejects a once-valid plan after the journal head advances", async (t) => {
  let now = Date.parse("2026-07-21T00:00:00.000Z");
  const { journal } = await tempJournal(t, { now: () => new Date(now) });
  await journal.append(request({
    revision: "scene-v1",
    idempotencyKey: "stale-retention-v1",
    expiresAt: "2026-07-21T00:10:00.000Z",
  }));
  now = Date.parse("2026-07-21T00:05:00.000Z");
  await journal.append(request({
    revision: "scene-v2",
    idempotencyKey: "stale-retention-v2",
    expiresAt: "2026-07-21T00:15:00.000Z",
  }));
  now = Date.parse("2026-07-21T00:20:00.000Z");
  const plan = await journal.planRetention();
  await journal.append(request({
    artifactId: "mmg_041",
    revision: "scene-v1",
    idempotencyKey: "stale-retention-other",
    expiresAt: "2026-08-01T00:00:00.000Z",
  }));
  await assert.rejects(
    journal.applyRetention(plan, { operatorApply: true, operatorId: "operator-a" }),
    expectCode("ARTIFACT_RETENTION_PLAN_STALE"),
  );
  assert.equal((await journal.verifyIntegrity()).retained_revision_count, 0);
});

test("backup manifests bind the exact chain and content without archiving or networking", async (t) => {
  const { journal } = await tempJournal(t, {
    now: () => new Date("2026-07-21T00:00:00.000Z"),
  });
  await journal.append(request());
  const manifest = await journal.generateBackupManifest();
  assert.equal(manifest.files.length, 2);
  assert.ok(manifest.files.every((file) => /^(entries|blobs)\//.test(file.relative_path)));
  const verified = await journal.verifyBackupManifest(manifest);
  assert.equal(verified.status, "valid");

  const changed = {
    ...manifest,
    files: manifest.files.map((file, index) => index === 0 ? { ...file, size: file.size + 1 } : file),
  };
  await assert.rejects(
    journal.verifyBackupManifest(changed),
    expectCode("ARTIFACT_BACKUP_MANIFEST_INVALID"),
  );

  await journal.append(request({
    artifactId: "mmg_041",
    revision: "scene-v2",
    idempotencyKey: "backup-mmg-041-v2",
  }));
  await assert.rejects(
    journal.verifyBackupManifest(manifest),
    expectCode("ARTIFACT_BACKUP_MANIFEST_INVALID"),
  );
});

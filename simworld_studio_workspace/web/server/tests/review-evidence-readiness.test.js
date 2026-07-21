"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const {
  acquireReviewEvidenceAdmission,
  bootstrapReviewEvidenceHierarchy,
  cleanupPrivateEvidenceRun,
  createPrivateEvidenceRun,
  createReviewEvidenceReadinessProbe,
  evidenceCapacitySnapshot,
  finalizeReviewEvidenceAdmission,
  managedEvidenceStat,
  managedScreenshotReference,
  openReviewEvidenceAdmission,
  reserveEvidenceFile,
  resolveManagedScreenshotReference,
  retainPrivateEvidenceRun,
  sealManagedScreenshot,
  takeScreenshot,
} = require("../scene-critic");

const PNG_BYTES = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1]);
const RESERVED_CAPTURE_NAME = `critic-${"c".repeat(32)}.png`;

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitUntil(predicate, { timeoutMs = 2_000, intervalMs = 5 } = {}) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await delay(intervalMs);
  }
  assert.fail("condition did not become true before timeout");
}

function activeCapacity(snapshot = evidenceCapacitySnapshot()) {
  return {
    active_runs: snapshot.active_runs,
    active_admissions: snapshot.active_admissions,
    active_files: snapshot.active_files,
    active_bytes: snapshot.active_bytes,
    reserved_files: snapshot.reserved_files,
    reserved_bytes: snapshot.reserved_bytes,
  };
}

function isEvidenceRunRenameSource(source, runPath) {
  const supplied = String(source || "");
  return path.basename(supplied) === path.basename(runPath)
    && (path.resolve(supplied) === runPath || supplied.startsWith("/proc/self/fd/"));
}

test("startup bootstrap creates fixed private roots while accepting an owner-controlled 0755 tmp", async (t) => {
  const arena = fs.mkdtempSync(path.join(os.tmpdir(), "review-evidence-bootstrap-"));
  const runtimeRoot = path.join(arena, "tmp");
  t.after(() => fs.rmSync(arena, { recursive: true, force: true }));
  fs.mkdirSync(runtimeRoot, { mode: 0o755 });
  fs.chmodSync(runtimeRoot, 0o755);

  const roots = await bootstrapReviewEvidenceHierarchy({ arenaRoot: arena, deadlineMs: 1_000 });

  assert.deepEqual(roots, [
    path.join(arena, "tmp", "review-evidence", "text"),
    path.join(arena, "tmp", "review-evidence", "visual"),
  ]);
  assert.equal(fs.statSync(runtimeRoot).mode & 0o777, 0o755);
  for (const directory of [path.dirname(roots[0]), ...roots]) {
    const stat = fs.lstatSync(directory);
    assert.equal(stat.isDirectory(), true);
    assert.equal(stat.isSymbolicLink(), false);
    assert.equal(stat.mode & 0o777, 0o700);
  }
  assert.deepEqual(
    await bootstrapReviewEvidenceHierarchy({ arenaRoot: arena, deadlineMs: 1_000 }),
    roots,
  );
  const report = await createReviewEvidenceReadinessProbe({ roots, deadlineMs: 1_000 })();
  assert.equal(report.status, "ready", JSON.stringify(report));
});

test("startup bootstrap fails closed on symlink and path-replacement evidence roots", async (t) => {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), "review-evidence-bootstrap-races-"));
  t.after(() => fs.rmSync(parent, { recursive: true, force: true }));

  const symlinkArena = path.join(parent, "symlink-arena");
  const outside = path.join(parent, "outside");
  fs.mkdirSync(path.join(symlinkArena, "tmp"), { recursive: true, mode: 0o755 });
  fs.mkdirSync(outside, { mode: 0o700 });
  fs.symlinkSync(outside, path.join(symlinkArena, "tmp", "review-evidence"));
  await assert.rejects(
    () => bootstrapReviewEvidenceHierarchy({ arenaRoot: symlinkArena, deadlineMs: 1_000 }),
    (error) => error.code === "REVIEW_EVIDENCE_INVALID",
  );
  assert.deepEqual(fs.readdirSync(outside), []);

  const writableArena = path.join(parent, "writable-arena");
  const writableTmp = path.join(writableArena, "tmp");
  fs.mkdirSync(writableTmp, { recursive: true, mode: 0o700 });
  fs.chmodSync(writableTmp, 0o777);
  await assert.rejects(
    () => bootstrapReviewEvidenceHierarchy({ arenaRoot: writableArena, deadlineMs: 1_000 }),
    (error) => error.code === "REVIEW_EVIDENCE_INVALID",
  );
  assert.equal(fs.existsSync(path.join(writableTmp, "review-evidence")), false);

  const replacedArena = path.join(parent, "replaced-arena");
  const runtimeRoot = path.join(replacedArena, "tmp");
  const displaced = `${runtimeRoot}-displaced`;
  const outsideSwapTarget = path.join(parent, "outside-swap-target");
  fs.mkdirSync(runtimeRoot, { recursive: true, mode: 0o755 });
  fs.mkdirSync(outsideSwapTarget, { mode: 0o700 });
  const originalMkdir = fs.promises.mkdir;
  let replaced = false;
  fs.promises.mkdir = async function replaceParentBeforeAnchoredMkdir(directory, ...args) {
    if (!replaced && path.basename(String(directory)) === "review-evidence") {
      replaced = true;
      await fs.promises.rename(runtimeRoot, displaced);
      await fs.promises.symlink(outsideSwapTarget, runtimeRoot);
    }
    return originalMkdir.call(fs.promises, directory, ...args);
  };
  try {
    await assert.rejects(
      () => bootstrapReviewEvidenceHierarchy({ arenaRoot: replacedArena, deadlineMs: 1_000 }),
      (error) => error.code === "REVIEW_EVIDENCE_INVALID",
    );
  } finally {
    fs.promises.mkdir = originalMkdir;
  }
  assert.equal(replaced, true);
  assert.equal(fs.statSync(displaced).isDirectory(), true);
  assert.equal(fs.existsSync(path.join(displaced, "review-evidence")), true);
  assert.equal(fs.lstatSync(runtimeRoot).isSymbolicLink(), true);
  assert.deepEqual(fs.readdirSync(outsideSwapTarget), []);
});

test("a hung cleanup close cannot overwrite a primary identity failure", async (t) => {
  const arena = fs.mkdtempSync(path.join(os.tmpdir(), "review-evidence-primary-close-"));
  t.after(() => fs.rmSync(arena, { recursive: true, force: true }));
  fs.mkdirSync(path.join(arena, "tmp"), { recursive: true, mode: 0o755 });
  const evidenceRoot = path.join(arena, "tmp", "review-evidence");
  const displaced = `${evidenceRoot}-displaced`;
  const originalOpen = fs.promises.open;
  let closeReached = false;
  let releaseClose = null;
  fs.promises.open = async function replaceIdentityAndHangCleanup(file, ...args) {
    if (path.basename(String(file)) !== "review-evidence" || closeReached) {
      return originalOpen.call(fs.promises, file, ...args);
    }
    await fs.promises.rename(evidenceRoot, displaced);
    await fs.promises.mkdir(evidenceRoot, { mode: 0o700 });
    const handle = await originalOpen.call(fs.promises, file, ...args);
    const originalClose = handle.close.bind(handle);
    let resolveClose;
    const closeGate = new Promise((resolve) => { resolveClose = resolve; });
    handle.close = () => {
      closeReached = true;
      return closeGate.then(() => originalClose());
    };
    releaseClose = resolveClose;
    return handle;
  };
  const deadlineMs = 500;
  const startedAt = Date.now();
  try {
    await assert.rejects(
      () => bootstrapReviewEvidenceHierarchy({ arenaRoot: arena, deadlineMs }),
      (error) => error.code === "REVIEW_EVIDENCE_INVALID",
    );
  } finally {
    fs.promises.open = originalOpen;
    if (releaseClose) releaseClose();
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.equal(closeReached, true);
  assert.ok(Date.now() - startedAt < deadlineMs + 500);
  assert.equal(fs.statSync(displaced).isDirectory(), true);
  assert.equal(fs.statSync(evidenceRoot).isDirectory(), true);
});

test("startup bootstrap has a hard deadline for hung mkdir, fsync, and close operations", async (t) => {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), "review-evidence-bootstrap-hung-"));
  t.after(() => fs.rmSync(parent, { recursive: true, force: true }));
  const originalMkdir = fs.promises.mkdir;
  const originalOpen = fs.promises.open;
  // Leave enough CI headroom to deterministically enter each injected fault;
  // the assertions below still prove that every hung operation is bounded.
  const hungOperationDeadlineMs = 500;
  const hungOperationWallClockLimitMs = hungOperationDeadlineMs + 500;

  const mkdirArena = path.join(parent, "mkdir-arena");
  fs.mkdirSync(path.join(mkdirArena, "tmp"), { recursive: true, mode: 0o755 });
  const hungEvidenceRoot = path.join(mkdirArena, "tmp", "review-evidence");
  let mkdirReached = false;
  fs.promises.mkdir = function hungEvidenceMkdir(directory, ...args) {
    if (path.basename(String(directory)) === path.basename(hungEvidenceRoot)) {
      mkdirReached = true;
      return new Promise(() => {});
    }
    return originalMkdir.call(fs.promises, directory, ...args);
  };
  const mkdirStartedAt = Date.now();
  try {
    await assert.rejects(
      () => bootstrapReviewEvidenceHierarchy({ arenaRoot: mkdirArena, deadlineMs: hungOperationDeadlineMs }),
      (error) => error.code === "REVIEW_EVIDENCE_ROOT_QUARANTINED",
    );
  } finally {
    fs.promises.mkdir = originalMkdir;
  }
  assert.equal(mkdirReached, true);
  assert.ok(Date.now() - mkdirStartedAt < hungOperationWallClockLimitMs);
  const retryStartedAt = Date.now();
  await assert.rejects(
    () => bootstrapReviewEvidenceHierarchy({ arenaRoot: mkdirArena, deadlineMs: 1_000 }),
    (error) => error.code === "REVIEW_EVIDENCE_ROOT_QUARANTINED",
  );
  assert.ok(Date.now() - retryStartedAt < 100, "quarantined root re-entered a hung mutation queue");

  const fsyncArena = path.join(parent, "fsync-arena");
  const fsyncRuntimeRoot = path.join(fsyncArena, "tmp");
  fs.mkdirSync(fsyncRuntimeRoot, { recursive: true, mode: 0o700 });
  let fsyncReached = false;
  let releaseFsync = null;
  fs.promises.open = async function hungBootstrapParentFsync(file, ...args) {
    const handle = await originalOpen.call(fs.promises, file, ...args);
    if (!fsyncReached && path.resolve(String(file)) === fsyncRuntimeRoot) {
      const originalSync = handle.sync.bind(handle);
      let resolveFsync;
      const fsyncGate = new Promise((resolve) => { resolveFsync = resolve; });
      handle.sync = () => {
        fsyncReached = true;
        return fsyncGate.then(() => originalSync());
      };
      releaseFsync = resolveFsync;
    }
    return handle;
  };
  try {
    await assert.rejects(
      () => bootstrapReviewEvidenceHierarchy({ arenaRoot: fsyncArena, deadlineMs: hungOperationDeadlineMs }),
      (error) => error.code === "REVIEW_EVIDENCE_ROOT_QUARANTINED",
    );
  } finally {
    fs.promises.open = originalOpen;
    if (releaseFsync) releaseFsync();
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.equal(fsyncReached, true);
  const fsyncRetryStartedAt = Date.now();
  await assert.rejects(
    () => bootstrapReviewEvidenceHierarchy({ arenaRoot: fsyncArena, deadlineMs: 1_000 }),
    (error) => error.code === "REVIEW_EVIDENCE_ROOT_QUARANTINED",
  );
  assert.ok(Date.now() - fsyncRetryStartedAt < 100, "uncertain parent fsync was retried as clean");

  const closeArena = path.join(parent, "close-arena");
  fs.mkdirSync(path.join(closeArena, "tmp"), { recursive: true, mode: 0o755 });
  const closeEvidenceRoot = path.join(closeArena, "tmp", "review-evidence");
  let closeReached = false;
  let releaseClose = null;
  fs.promises.open = async function hungBootstrapClose(file, ...args) {
    const handle = await originalOpen.call(fs.promises, file, ...args);
    if (!closeReached && path.basename(String(file)) === path.basename(closeEvidenceRoot)) {
      closeReached = true;
      const originalClose = handle.close.bind(handle);
      let resolveClose;
      const closeGate = new Promise((resolve) => { resolveClose = resolve; });
      handle.close = () => closeGate.then(() => originalClose());
      releaseClose = resolveClose;
    }
    return handle;
  };
  const closeStartedAt = Date.now();
  try {
    await assert.rejects(
      () => bootstrapReviewEvidenceHierarchy({ arenaRoot: closeArena, deadlineMs: hungOperationDeadlineMs }),
      (error) => error.code === "REVIEW_EVIDENCE_SWEEP_DEADLINE",
    );
  } finally {
    fs.promises.open = originalOpen;
    if (releaseClose) releaseClose();
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.equal(closeReached, true);
  assert.ok(Date.now() - closeStartedAt < hungOperationWallClockLimitMs);
});

test("Review evidence readiness is read-only and reports a bounded crash orphan", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "review-evidence-orphan-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const orphan = path.join(root, `scope-${"a".repeat(64)}-${"b".repeat(32)}`);
  fs.mkdirSync(orphan, { mode: 0o700 });
  fs.writeFileSync(path.join(orphan, RESERVED_CAPTURE_NAME), PNG_BYTES, { mode: 0o600 });

  const report = await createReviewEvidenceReadinessProbe({ roots: [root] })();

  assert.equal(report.status, "not_ready");
  assert.ok(report.causes.some((cause) => cause.code === "REVIEW_EVIDENCE_ORPHAN_BACKLOG"));
  assert.equal(report.revision.schema, "simworld-review-evidence-readiness/v1");
  assert.equal(report.revision.roots_checked, 1);
  assert.equal(report.revision.orphan_backlog, 1);
  assert.equal(report.revision.durability_fault_roots, 0);
  assert.equal(report.revision.sweep_complete, true);
  assert.ok(report.revision.sweep_directories >= 2);
  assert.equal(report.revision.sweep_files, 0);
  assert.ok(report.revision.sweep_operations > 3);
  assert.deepEqual(fs.readdirSync(root), [path.basename(orphan)]);
  assert.deepEqual(fs.readFileSync(path.join(orphan, RESERVED_CAPTURE_NAME)), PNG_BYTES);
});

test("orphan preflight preserves every file when a known capture has an unknown sentinel", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "review-evidence-preflight-unknown-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const orphanName = `scope-${"1".repeat(64)}-${"2".repeat(32)}`;
  const orphan = path.join(root, orphanName);
  const known = path.join(orphan, RESERVED_CAPTURE_NAME);
  const sentinel = path.join(orphan, "operator-sentinel.txt");
  fs.mkdirSync(orphan, { mode: 0o700 });
  fs.writeFileSync(known, PNG_BYTES, { mode: 0o600 });
  fs.writeFileSync(sentinel, "preserve all", { mode: 0o600 });

  const report = await createReviewEvidenceReadinessProbe({ roots: [root] })();

  assert.equal(report.status, "not_ready");
  assert.deepEqual(fs.readdirSync(root), [orphanName]);
  assert.deepEqual(fs.readFileSync(known), PNG_BYTES);
  assert.equal(fs.readFileSync(sentinel, "utf8"), "preserve all");
});

test("orphan preflight preserves every file when a reserved-name entry is a symlink", async (t) => {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), "review-evidence-preflight-symlink-"));
  const root = path.join(parent, "evidence");
  const orphanName = `scope-${"3".repeat(64)}-${"4".repeat(32)}`;
  const orphan = path.join(root, orphanName);
  const known = path.join(orphan, RESERVED_CAPTURE_NAME);
  const target = path.join(parent, "target.png");
  const link = path.join(orphan, `visual-${"d".repeat(32)}.png`);
  t.after(() => fs.rmSync(parent, { recursive: true, force: true }));
  fs.mkdirSync(orphan, { recursive: true, mode: 0o700 });
  fs.chmodSync(root, 0o700);
  fs.writeFileSync(known, PNG_BYTES, { mode: 0o600 });
  fs.writeFileSync(target, PNG_BYTES, { mode: 0o600 });
  fs.symlinkSync(target, link);

  const report = await createReviewEvidenceReadinessProbe({ roots: [root] })();

  assert.equal(report.status, "not_ready");
  assert.deepEqual(fs.readdirSync(root), [orphanName]);
  assert.deepEqual(fs.readFileSync(known), PNG_BYTES);
  assert.equal(fs.lstatSync(link).isSymbolicLink(), true);
  assert.deepEqual(fs.readFileSync(target), PNG_BYTES);
});

test("orphan preflight preserves every file when a reserved-name entry is hardlinked", async (t) => {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), "review-evidence-preflight-hardlink-"));
  const root = path.join(parent, "evidence");
  const orphanName = `scope-${"5".repeat(64)}-${"6".repeat(32)}`;
  const orphan = path.join(root, orphanName);
  const known = path.join(orphan, RESERVED_CAPTURE_NAME);
  const target = path.join(parent, "target.png");
  const link = path.join(orphan, `visual-${"e".repeat(32)}.png`);
  t.after(() => fs.rmSync(parent, { recursive: true, force: true }));
  fs.mkdirSync(orphan, { recursive: true, mode: 0o700 });
  fs.chmodSync(root, 0o700);
  fs.writeFileSync(known, PNG_BYTES, { mode: 0o600 });
  fs.writeFileSync(target, PNG_BYTES, { mode: 0o600 });
  fs.linkSync(target, link);

  const report = await createReviewEvidenceReadinessProbe({ roots: [root] })();

  assert.equal(report.status, "not_ready");
  assert.deepEqual(fs.readdirSync(root), [orphanName]);
  assert.deepEqual(fs.readFileSync(known), PNG_BYTES);
  assert.equal(fs.statSync(link).nlink, 2);
  assert.deepEqual(fs.readFileSync(target), PNG_BYTES);
});

test("an entry inserted after root EOF invalidates the clean pass and is found by the shared-budget rescan", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "review-evidence-eof-race-"));
  const sentinel = path.join(root, "operator-after-eof.txt");
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const originalOpendir = fs.promises.opendir;
  let injected = false;
  fs.promises.opendir = async function injectAfterRootEof(directory, ...args) {
    const handle = await originalOpendir.call(fs.promises, directory, ...args);
    if (path.resolve(String(directory)) !== root) return handle;
    return {
      async read() {
        const entry = await handle.read();
        if (!entry && !injected) {
          injected = true;
          await fs.promises.writeFile(sentinel, "preserve", { mode: 0o600 });
        }
        return entry;
      },
      close() {
        return handle.close();
      },
    };
  };
  let report;
  try {
    report = await createReviewEvidenceReadinessProbe({ roots: [root] })();
  } finally {
    fs.promises.opendir = originalOpendir;
  }

  assert.equal(injected, true);
  assert.equal(report.status, "not_ready");
  assert.equal(report.revision.roots_checked, 1);
  assert.equal(report.revision.sweep_complete, true);
  assert.ok(report.revision.sweep_directories >= 1);
  assert.equal(fs.readFileSync(sentinel, "utf8"), "preserve");
  await assert.rejects(
    () => createPrivateEvidenceRun({ root, scopeId: "eof-race" }),
    (error) => error.code === "REVIEW_EVIDENCE_ORPHAN_BACKLOG",
  );
});

test("a replaced evidence-root inode invalidates the sweep cache and preserves unknown sentinels", async (t) => {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), "review-evidence-replaced-"));
  const root = path.join(parent, "evidence");
  const displaced = path.join(parent, "evidence-before-replacement");
  fs.mkdirSync(root, { mode: 0o700 });
  t.after(() => fs.rmSync(parent, { recursive: true, force: true }));

  const initial = await createReviewEvidenceReadinessProbe({ roots: [root] })();
  assert.equal(initial.status, "ready");

  fs.renameSync(root, displaced);
  fs.mkdirSync(root, { mode: 0o700 });
  const sentinel = path.join(root, "operator-sentinel.txt");
  fs.writeFileSync(sentinel, "preserve me", { mode: 0o600 });

  await assert.rejects(
    () => createPrivateEvidenceRun({ root, scopeId: "replaced-root" }),
    (error) => error.code === "REVIEW_EVIDENCE_ORPHAN_BACKLOG",
  );
  const report = await createReviewEvidenceReadinessProbe({ roots: [root] })();
  assert.equal(report.status, "not_ready");
  assert.equal(report.revision.orphan_backlog, 1);
  assert.equal(fs.readFileSync(sentinel, "utf8"), "preserve me");
});

test("Review evidence sweep file budget is global across tombstone roots", async (t) => {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), "review-evidence-global-files-"));
  const roots = [path.join(parent, "text"), path.join(parent, "visual")];
  t.after(() => fs.rmSync(parent, { recursive: true, force: true }));
  for (const [index, root] of roots.entries()) {
    fs.mkdirSync(root, { mode: 0o700 });
    const tombstones = path.join(root, ".review-evidence-tombstones");
    fs.mkdirSync(tombstones, { mode: 0o700 });
    fs.mkdirSync(
      path.join(tombstones, `tombstone-${String(index + 1).repeat(64)}`),
      { mode: 0o700 },
    );
  }

  const report = await createReviewEvidenceReadinessProbe({
    roots,
    maxFiles: 3,
    maxOperations: 500,
    deadlineMs: 1_000,
    batchOperations: 1,
  })();

  assert.equal(report.status, "not_ready");
  assert.equal(report.revision.roots_checked, 1);
  assert.equal(report.revision.sweep_complete, false);
  assert.equal(report.revision.sweep_files, 3);
  assert.ok(report.revision.sweep_directories >= 2);
  assert.ok(report.causes.some(
    (cause) => cause.code === "REVIEW_EVIDENCE_SWEEP_BUDGET_EXHAUSTED",
  ));
  assert.deepEqual(fs.readdirSync(roots[0]), [".review-evidence-tombstones"]);
  assert.deepEqual(fs.readdirSync(roots[1]), [".review-evidence-tombstones"]);
});

test("Review evidence sweep operation budget is global across roots", async (t) => {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), "review-evidence-global-ops-"));
  const roots = [path.join(parent, "text"), path.join(parent, "visual")];
  t.after(() => fs.rmSync(parent, { recursive: true, force: true }));
  for (const root of roots) fs.mkdirSync(root, { mode: 0o700 });

  const report = await createReviewEvidenceReadinessProbe({
    roots,
    maxOperations: 30,
    deadlineMs: 1_000,
    batchOperations: 1,
  })();

  assert.equal(report.status, "not_ready");
  assert.ok(report.revision.roots_checked <= 1);
  assert.equal(report.revision.sweep_operations, 30);
  assert.equal(report.revision.sweep_complete, false);
  assert.ok(report.causes.some(
    (cause) => cause.code === "REVIEW_EVIDENCE_SWEEP_BUDGET_EXHAUSTED",
  ));
});

test("Review evidence sweep yields in interruptible batches", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "review-evidence-abort-batch-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  for (let index = 0; index < 4; index += 1) {
    fs.writeFileSync(path.join(root, `operator-sentinel-${index}.txt`), "preserve", { mode: 0o600 });
  }
  const controller = new AbortController();
  setImmediate(() => controller.abort());

  const report = await createReviewEvidenceReadinessProbe({
    roots: [root],
    maxOperations: 32,
    deadlineMs: 1_000,
    batchOperations: 1,
  })({ signal: controller.signal });

  assert.equal(report.status, "not_ready");
  assert.equal(report.revision.sweep_complete, false);
  assert.ok(report.causes.some((cause) => cause.code === "REVIEW_EVIDENCE_PROBE_ABORTED"));
  assert.equal(fs.readdirSync(root).length, 4);
});

test("runtime cache miss fails closed without any synchronous directory scan", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "review-evidence-runtime-miss-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const originalReaddir = fs.readdirSync;
  let readdirCalls = 0;
  fs.readdirSync = function forbiddenRuntimeScan() {
    readdirCalls += 1;
    throw new Error("runtime must not scan a cache miss");
  };
  try {
    await assert.rejects(
      () => createPrivateEvidenceRun({ root, scopeId: "runtime-cache-miss" }),
      (error) => error.code === "REVIEW_EVIDENCE_ORPHAN_BACKLOG",
    );
  } finally {
    fs.readdirSync = originalReaddir;
  }
  assert.equal(readdirCalls, 0);
});

test("readiness orphan cleanup uses no synchronous filesystem primitive", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "review-evidence-async-only-"));
  const orphan = path.join(root, `scope-${"7".repeat(64)}-${"8".repeat(32)}`);
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  fs.mkdirSync(orphan, { mode: 0o700 });
  fs.writeFileSync(path.join(orphan, RESERVED_CAPTURE_NAME), PNG_BYTES, { mode: 0o600 });
  const forbidden = [
    "existsSync",
    "mkdirSync",
    "lstatSync",
    "realpathSync",
    "openSync",
    "fstatSync",
    "readdirSync",
    "renameSync",
    "unlinkSync",
    "fsyncSync",
    "rmdirSync",
  ];
  const originals = new Map(forbidden.map((name) => [name, fs[name]]));
  for (const name of forbidden) {
    fs[name] = function forbiddenSyncPrimitive() {
      throw new Error(`readiness called forbidden fs.${name}`);
    };
  }
  let report;
  try {
    report = await createReviewEvidenceReadinessProbe({ roots: [root] })();
  } finally {
    for (const [name, implementation] of originals) fs[name] = implementation;
  }

  assert.equal(report.status, "not_ready");
  assert.equal(fs.readdirSync(root).length, 1);
});

test("readiness async sweep keeps the main event loop responsive", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "review-evidence-heartbeat-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  for (let index = 0; index < 128; index += 1) {
    fs.writeFileSync(path.join(root, `operator-sentinel-${index}.txt`), "preserve", { mode: 0o600 });
  }
  let running = true;
  let heartbeats = 0;
  function heartbeat() {
    if (!running) return;
    heartbeats += 1;
    setImmediate(heartbeat);
  }
  setImmediate(heartbeat);
  let report;
  try {
    report = await createReviewEvidenceReadinessProbe({
      roots: [root],
      maxDirectories: 256,
      maxOperations: 5_000,
      deadlineMs: 1_000,
      batchOperations: 4,
    })();
  } finally {
    running = false;
  }

  assert.equal(report.status, "not_ready");
  assert.ok(heartbeats > 0);
  assert.equal(fs.readdirSync(root).length, 128);
});

test("same-root readiness sweeps serialize and a queued sweep remains cancellable", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "review-evidence-serialized-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const originalOpendir = fs.promises.opendir;
  let releaseFirst;
  let markFirstEntered;
  let blocked = false;
  const firstEntered = new Promise((resolve) => { markFirstEntered = resolve; });
  const firstGate = new Promise((resolve) => { releaseFirst = resolve; });
  fs.promises.opendir = async function blockFirstRootScan(directory, ...args) {
    if (path.resolve(String(directory)) === root && !blocked) {
      blocked = true;
      markFirstEntered();
      await firstGate;
    }
    return originalOpendir.call(fs.promises, directory, ...args);
  };
  const probe = createReviewEvidenceReadinessProbe({
    roots: [root],
    deadlineMs: 1_000,
    batchOperations: 1,
  });
  const first = probe();
  await firstEntered;
  const controller = new AbortController();
  const second = probe({ signal: controller.signal });
  controller.abort();
  const secondReport = await second;
  releaseFirst();
  let firstReport;
  try {
    firstReport = await first;
  } finally {
    fs.promises.opendir = originalOpendir;
  }

  assert.equal(firstReport.status, "ready");
  assert.equal(secondReport.status, "not_ready");
  assert.ok(secondReport.causes.some((cause) => cause.code === "REVIEW_EVIDENCE_PROBE_ABORTED"));
});

test("queued admission times out before reserving run capacity", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "review-evidence-admission-queue-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  assert.equal((await createReviewEvidenceReadinessProbe({ roots: [root] })()).status, "ready");
  const baseline = activeCapacity();
  const originalOpendir = fs.promises.opendir;
  let releaseFirst;
  let markFirstEntered;
  let blocked = false;
  const firstEntered = new Promise((resolve) => { markFirstEntered = resolve; });
  const firstGate = new Promise((resolve) => { releaseFirst = resolve; });
  fs.promises.opendir = async function blockRootCertification(directory, ...args) {
    if (!blocked && path.resolve(String(directory)) === root) {
      blocked = true;
      markFirstEntered();
      await firstGate;
    }
    return originalOpendir.call(fs.promises, directory, ...args);
  };
  const firstProbe = createReviewEvidenceReadinessProbe({
    roots: [root],
    deadlineMs: 1_000,
  })();
  await firstEntered;
  const startedAt = Date.now();
  try {
    await assert.rejects(
      () => acquireReviewEvidenceAdmission({
        root,
        scopeId: "queued-admission",
        deadlineMs: 25,
      }),
      (error) => error.code === "REVIEW_EVIDENCE_SWEEP_DEADLINE",
    );
    assert.ok(Date.now() - startedAt < 500);
    assert.deepEqual(activeCapacity(), baseline);
  } finally {
    releaseFirst();
    await firstProbe;
    fs.promises.opendir = originalOpendir;
  }
  assert.deepEqual(activeCapacity(), baseline);
});

test("already-aborted capture allocates no directory, file, or tombstone", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "review-evidence-aborted-capture-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  assert.equal((await createReviewEvidenceReadinessProbe({ roots: [root] })()).status, "ready");
  const baseline = activeCapacity();
  const controller = new AbortController();
  controller.abort();
  let brokerCalls = 0;
  const broker = { send: async () => { brokerCalls += 1; return {}; } };

  await assert.rejects(
    () => takeScreenshot({
      root,
      evidenceRoot: root,
      scopeId: "already-aborted",
      ueBroker: broker,
      signal: controller.signal,
    }),
    (error) => error.code === "REVIEW_ABORTED",
  );
  assert.equal(brokerCalls, 0);
  assert.deepEqual(fs.readdirSync(root), []);
  assert.deepEqual(activeCapacity(), baseline);

  const admission = await acquireReviewEvidenceAdmission({
    root,
    scopeId: "admitted-then-aborted",
  });
  await assert.rejects(
    () => takeScreenshot({
      evidenceAdmission: admission,
      scopeId: "admitted-then-aborted",
      ueBroker: broker,
      signal: controller.signal,
    }),
    (error) => error.code === "REVIEW_ABORTED",
  );
  assert.equal(brokerCalls, 0);
  assert.deepEqual(fs.readdirSync(root), []);
  assert.equal(await finalizeReviewEvidenceAdmission(admission, {
    retain: false,
    deadlineMs: 1_000,
  }), true);
  assert.deepEqual(fs.readdirSync(root), []);
  assert.deepEqual(activeCapacity(), baseline);
});

test("hung opendir, open, and close settle by deadline or Abort without poisoning the root tail", async (t) => {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), "review-evidence-hung-io-"));
  t.after(() => fs.rmSync(parent, { recursive: true, force: true }));
  const originalOpendir = fs.promises.opendir;
  const originalOpen = fs.promises.open;

  for (const operation of ["opendir", "open", "close"]) {
    for (const control of ["deadline", "abort"]) {
      const root = path.join(parent, `${operation}-${control}`);
      fs.mkdirSync(root, { mode: 0o700 });
      let releaseClose = null;
      let injected = false;
      let markInjected;
      const injectedPromise = new Promise((resolve) => { markInjected = resolve; });
      if (operation === "opendir") {
        fs.promises.opendir = function hungOpendir(directory, ...args) {
          if (!injected && path.resolve(String(directory)) === root) {
            injected = true;
            markInjected();
            return new Promise(() => {});
          }
          return originalOpendir.call(fs.promises, directory, ...args);
        };
      } else {
        fs.promises.open = async function hungOpenOrClose(file, ...args) {
          if (operation === "open" && !injected && path.resolve(String(file)) === root) {
            injected = true;
            markInjected();
            return new Promise(() => {});
          }
          const handle = await originalOpen.call(fs.promises, file, ...args);
          if (operation === "close" && !injected && path.resolve(String(file)) === root) {
            injected = true;
            markInjected();
            const originalClose = handle.close.bind(handle);
            let resolveGate;
            let closeStarted = false;
            const gate = new Promise((resolve) => { resolveGate = resolve; });
            handle.close = () => {
              closeStarted = true;
              return gate.then(() => originalClose());
            };
            releaseClose = () => {
              if (closeStarted) resolveGate();
            };
          }
          return handle;
        };
      }

      const controller = new AbortController();
      const probe = createReviewEvidenceReadinessProbe({
        roots: [root],
        deadlineMs: control === "deadline" ? 25 : 1_000,
        batchOperations: 1,
      });
      const startedAt = Date.now();
      const pending = probe({ signal: controller.signal });
      if (control === "abort") {
        await injectedPromise;
        controller.abort();
      }
      const report = await pending;
      const elapsedMs = Date.now() - startedAt;
      fs.promises.opendir = originalOpendir;
      fs.promises.open = originalOpen;
      if (releaseClose) releaseClose();
      await new Promise((resolve) => setImmediate(resolve));

      assert.equal(injected, true, `${operation}/${control} injection was not reached`);
      assert.ok(elapsedMs < 500, `${operation}/${control} exceeded bounded response time`);
      assert.equal(report.status, "not_ready");
      assert.ok(report.causes.some((cause) => cause.code === (
        control === "abort" ? "REVIEW_EVIDENCE_PROBE_ABORTED" : "REVIEW_EVIDENCE_SWEEP_DEADLINE"
      )));

      const recovered = await createReviewEvidenceReadinessProbe({
        roots: [root],
        deadlineMs: 1_000,
      })();
      assert.equal(recovered.status, "ready", `${operation}/${control} poisoned the root tail`);
    }
  }
});

test("a late rejected read operation leaves no deadline timer or unhandled rejection", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "review-evidence-late-reject-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const originalOpen = fs.promises.open;
  const originalSetTimeout = global.setTimeout;
  const originalClearTimeout = global.clearTimeout;
  const deadlineTimers = new Set();
  const unhandled = [];
  const onUnhandled = (error) => unhandled.push(error);
  process.on("unhandledRejection", onUnhandled);
  global.setTimeout = function trackedTimeout(callback, timeout, ...args) {
    let timer;
    timer = originalSetTimeout(() => {
      deadlineTimers.delete(timer);
      callback(...args);
    }, timeout);
    if (timeout === 20) deadlineTimers.add(timer);
    return timer;
  };
  global.clearTimeout = function trackedClear(timer) {
    deadlineTimers.delete(timer);
    return originalClearTimeout(timer);
  };
  let rejectLate;
  fs.promises.open = function lateRejectOpen(file, ...args) {
    if (path.resolve(String(file)) === root && !rejectLate) {
      return new Promise((_resolve, reject) => { rejectLate = reject; });
    }
    return originalOpen.call(fs.promises, file, ...args);
  };
  let report;
  try {
    report = await createReviewEvidenceReadinessProbe({ roots: [root], deadlineMs: 20 })();
    rejectLate(new Error("late read rejection"));
    await delay(20);
  } finally {
    fs.promises.open = originalOpen;
    global.setTimeout = originalSetTimeout;
    global.clearTimeout = originalClearTimeout;
    process.removeListener("unhandledRejection", onUnhandled);
  }

  assert.equal(report.status, "not_ready");
  assert.ok(report.causes.some((cause) => cause.code === "REVIEW_EVIDENCE_SWEEP_DEADLINE"));
  assert.equal(deadlineTimers.size, 0);
  assert.deepEqual(unhandled, []);
});

test("runtime create and cleanup cannot launder an unknown root sibling into a clean cache", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "review-evidence-cache-launder-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  assert.equal((await createReviewEvidenceReadinessProbe({ roots: [root] })()).status, "ready");
  const run = await createPrivateEvidenceRun({ root, scopeId: "cache-launder-cleanup" });
  const screenshot = await reserveEvidenceFile(run, "capture");
  fs.writeFileSync(screenshot, PNG_BYTES);
  await sealManagedScreenshot(screenshot);
  const stat = await managedEvidenceStat(screenshot);
  assert.equal(typeof stat.dev, "string");
  assert.equal(typeof stat.ino, "string");

  const sentinel = path.join(root, "operator-before-cleanup.txt");
  fs.writeFileSync(sentinel, "preserve", { mode: 0o600 });
  assert.equal(await cleanupPrivateEvidenceRun(run), true);
  assert.equal(fs.readFileSync(sentinel, "utf8"), "preserve");
  await assert.rejects(
    () => createPrivateEvidenceRun({ root, scopeId: "cache-launder-next" }),
    (error) => error.code === "REVIEW_EVIDENCE_ORPHAN_BACKLOG",
  );
});

test("chunked screenshot seal keeps the event loop responsive and expiry is enforced on retrieval", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "review-evidence-chunked-read-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  assert.equal((await createReviewEvidenceReadinessProbe({ roots: [root] })()).status, "ready");
  const baseline = activeCapacity();
  const run = await createPrivateEvidenceRun({ root, scopeId: "chunked-read" });
  const screenshot = await reserveEvidenceFile(run, "large-capture");
  const payload = Buffer.alloc(8 * 1024 * 1024);
  PNG_BYTES.copy(payload);
  await fs.promises.writeFile(screenshot, payload, { mode: 0o600 });
  let running = true;
  let heartbeats = 0;
  const heartbeat = () => {
    if (!running) return;
    heartbeats += 1;
    setImmediate(heartbeat);
  };
  setImmediate(heartbeat);
  let evidence;
  try {
    evidence = await sealManagedScreenshot(screenshot, { deadlineMs: 2_000 });
  } finally {
    running = false;
  }
  assert.ok(heartbeats > 0, "chunked read/hash blocked the event loop");
  assert.equal(evidence.data.length, payload.length);
  assert.deepEqual(await managedScreenshotReference(screenshot), evidence.reference);

  await retainPrivateEvidenceRun(run, { retentionMs: 5 });
  const busyUntil = Date.now() + 20;
  while (Date.now() < busyUntil) {
    // Keep the timer callback pending so retrieval itself must enforce the
    // absolute expiry timestamp.
  }
  await assert.rejects(
    () => resolveManagedScreenshotReference({
      scopeId: "chunked-read",
      evidenceId: evidence.reference.evidence_id,
      handle: evidence.reference.handle,
    }),
    (error) => error.code === "REVIEW_EVIDENCE_UNAVAILABLE",
  );
  await cleanupPrivateEvidenceRun(run);
  assert.deepEqual(activeCapacity(), baseline);
});

test("screenshot fsync has a hard deadline and cleanup remains durable", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "review-evidence-hung-fsync-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  assert.equal((await createReviewEvidenceReadinessProbe({ roots: [root] })()).status, "ready");
  const baseline = activeCapacity();
  const run = await createPrivateEvidenceRun({ root, scopeId: "hung-fsync" });
  const screenshot = await reserveEvidenceFile(run, "capture");
  await fs.promises.writeFile(screenshot, PNG_BYTES, { mode: 0o600 });
  const sample = await fs.promises.open(screenshot, fs.constants.O_RDONLY);
  const fileHandlePrototype = Object.getPrototypeOf(sample);
  await sample.close();
  const originalSync = fileHandlePrototype.sync;
  let syncReached = false;
  fileHandlePrototype.sync = async function hungFileSync() {
    const stat = await this.stat();
    if (stat.isFile() && !syncReached) {
      syncReached = true;
      return new Promise(() => {});
    }
    return originalSync.call(this);
  };
  const startedAt = Date.now();
  try {
    await assert.rejects(
      () => sealManagedScreenshot(screenshot, { deadlineMs: 25 }),
      (error) => error.code === "REVIEW_EVIDENCE_SWEEP_DEADLINE",
    );
  } finally {
    fileHandlePrototype.sync = originalSync;
  }
  assert.equal(syncReached, true);
  assert.ok(Date.now() - startedAt < 500);
  assert.equal(await cleanupPrivateEvidenceRun(run, { deadlineMs: 1_000 }), true);
  assert.deepEqual(activeCapacity(), baseline);
});

test("a sibling inserted after runtime mkdir prevents run publication and remains preserved", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "review-evidence-create-race-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  assert.equal((await createReviewEvidenceReadinessProbe({ roots: [root] })()).status, "ready");
  const originalMkdir = fs.promises.mkdir;
  const sentinel = path.join(root, "operator-after-mkdir.txt");
  let injected = false;
  fs.promises.mkdir = async function injectSibling(directory, options) {
    const result = await originalMkdir.call(fs.promises, directory, options);
    if (!injected && path.dirname(String(directory)) === root
        && path.basename(String(directory)).startsWith("scope-")) {
      injected = true;
      await fs.promises.writeFile(sentinel, "preserve", { mode: 0o600 });
    }
    return result;
  };
  try {
    await assert.rejects(
      () => createPrivateEvidenceRun({ root, scopeId: "mkdir-sibling-race" }),
      (error) => error.code === "REVIEW_EVIDENCE_ORPHAN_BACKLOG",
    );
  } finally {
    fs.promises.mkdir = originalMkdir;
  }
  assert.equal(injected, true);
  assert.equal(fs.readFileSync(sentinel, "utf8"), "preserve");
  assert.ok(fs.readdirSync(root).includes(".review-evidence-tombstones"));
});

test("whole-directory tombstoning preserves every entry across a pre-rename replacement race", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "review-evidence-tombstone-race-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  assert.equal((await createReviewEvidenceReadinessProbe({ roots: [root] })()).status, "ready");
  const run = await createPrivateEvidenceRun({ root, scopeId: "tombstone-race" });
  const first = await reserveEvidenceFile(run, "first");
  const second = await reserveEvidenceFile(run, "second");
  fs.writeFileSync(first, PNG_BYTES);
  fs.writeFileSync(second, PNG_BYTES);
  await sealManagedScreenshot(first);
  await sealManagedScreenshot(second);
  const originalRename = fs.promises.rename;
  const originalUnlink = fs.promises.unlink;
  let serverUnlinkCalls = 0;
  fs.promises.unlink = async () => {
    serverUnlinkCalls += 1;
    throw new Error("server must not unlink tombstone contents");
  };
  fs.promises.rename = async function injectReplacement(source, destination) {
    if (isEvidenceRunRenameSource(source, run.path)) {
      fs.unlinkSync(second);
      fs.writeFileSync(second, Buffer.concat([PNG_BYTES, Buffer.from("replacement")]), { mode: 0o600 });
      fs.symlinkSync(first, path.join(run.path, "operator-link"));
      fs.linkSync(first, path.join(run.path, "operator-hardlink"));
      fs.writeFileSync(path.join(run.path, "operator-sentinel.txt"), "preserve", { mode: 0o600 });
    }
    return originalRename.call(fs.promises, source, destination);
  };
  let durable;
  try {
    durable = await cleanupPrivateEvidenceRun(run);
  } finally {
    fs.promises.rename = originalRename;
    fs.promises.unlink = originalUnlink;
  }

  assert.equal(durable, true);
  assert.equal(serverUnlinkCalls, 0);
  const tombstoneRoot = path.join(root, ".review-evidence-tombstones");
  const [tombstoneName] = fs.readdirSync(tombstoneRoot);
  const tombstone = path.join(tombstoneRoot, tombstoneName);
  assert.deepEqual(fs.readFileSync(first.replace(run.path, tombstone)), PNG_BYTES);
  assert.match(fs.readFileSync(second.replace(run.path, tombstone)).subarray(PNG_BYTES.length).toString(), /replacement/);
  assert.equal(fs.readFileSync(path.join(tombstone, "operator-sentinel.txt"), "utf8"), "preserve");
  assert.equal(fs.lstatSync(path.join(tombstone, "operator-link")).isSymbolicLink(), true);
  assert.equal(fs.statSync(path.join(tombstone, "operator-hardlink")).nlink, 2);
});

test("tombstone quota is bounded and produces structured not-ready diagnostics", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "review-evidence-tombstone-quota-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const tombstones = path.join(root, ".review-evidence-tombstones");
  fs.mkdirSync(tombstones, { mode: 0o700 });
  for (const digit of ["1", "2"]) {
    const retained = path.join(tombstones, `tombstone-${digit.repeat(64)}`);
    fs.mkdirSync(retained, { mode: 0o700 });
    fs.writeFileSync(path.join(retained, "payload.bin"), "12345", { mode: 0o600 });
  }
  const report = await createReviewEvidenceReadinessProbe({
    roots: [root],
    maxTombstones: 1,
    maxTombstoneBytes: 9,
  })();
  assert.equal(report.status, "not_ready");
  assert.equal(report.revision.tombstone_roots, 1);
  assert.equal(report.revision.tombstone_runs, 2);
  assert.equal(report.revision.tombstone_entries, 2);
  assert.equal(report.revision.tombstone_bytes, 10);
  assert.equal(report.revision.tombstone_quota_exceeded, true);
  assert.ok(report.causes.some(
    (cause) => cause.code === "REVIEW_EVIDENCE_TOMBSTONE_QUOTA_EXCEEDED",
  ));
});

test("readiness never renames or deletes orphan evidence", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "review-evidence-deadline-"));
  const orphan = path.join(root, `scope-${"9".repeat(64)}-${"a".repeat(32)}`);
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  fs.mkdirSync(orphan, { mode: 0o700 });
  fs.writeFileSync(path.join(orphan, RESERVED_CAPTURE_NAME), PNG_BYTES, { mode: 0o600 });

  const originalRename = fs.promises.rename;
  const originalUnlink = fs.promises.unlink;
  const originalRmdir = fs.promises.rmdir;
  let renameCalls = 0;
  let unlinkCalls = 0;
  let rmdirCalls = 0;
  fs.promises.rename = async () => {
    renameCalls += 1;
    throw new Error("readiness must not rename");
  };
  fs.promises.unlink = async () => {
    unlinkCalls += 1;
    throw new Error("readiness must not unlink");
  };
  fs.promises.rmdir = async () => {
    rmdirCalls += 1;
    throw new Error("readiness must not rmdir");
  };
  let report;
  try {
    report = await createReviewEvidenceReadinessProbe({
      roots: [root],
      deadlineMs: 1_000,
      batchOperations: 256,
    })();
  } finally {
    fs.promises.rename = originalRename;
    fs.promises.unlink = originalUnlink;
    fs.promises.rmdir = originalRmdir;
  }

  assert.equal(report.status, "not_ready");
  assert.equal(report.revision.sweep_complete, true);
  assert.ok(report.causes.some((cause) => cause.code === "REVIEW_EVIDENCE_ORPHAN_BACKLOG"));
  assert.equal(renameCalls, 0);
  assert.equal(unlinkCalls, 0);
  assert.equal(rmdirCalls, 0);
  assert.deepEqual(fs.readdirSync(root), [path.basename(orphan)]);
  assert.deepEqual(fs.readFileSync(path.join(orphan, RESERVED_CAPTURE_NAME)), PNG_BYTES);
  await assert.rejects(
    () => createPrivateEvidenceRun({ root, scopeId: "read-only-orphan" }),
    (error) => error.code === "REVIEW_EVIDENCE_ORPHAN_BACKLOG",
  );
});

test("unknown evidence-root entries block readiness and new paid Review runs", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "review-evidence-unknown-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const sentinel = path.join(root, "operator-sentinel.txt");
  fs.writeFileSync(sentinel, "do not delete", { mode: 0o600 });

  const report = await createReviewEvidenceReadinessProbe({ roots: [root] })();

  assert.equal(report.status, "not_ready");
  assert.equal(report.revision.orphan_backlog, 1);
  assert.ok(report.causes.some((cause) => cause.code === "REVIEW_EVIDENCE_ORPHAN_BACKLOG"));
  assert.equal(fs.readFileSync(sentinel, "utf8"), "do not delete");
  await assert.rejects(
    () => createPrivateEvidenceRun({ root, scopeId: "blocked-scope" }),
    (error) => error.code === "REVIEW_EVIDENCE_ORPHAN_BACKLOG",
  );
});

test("parallel lifecycle reservations enforce exactly 64 runs and 512 files", async (t) => {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), "review-evidence-capacity-"));
  t.after(() => fs.rmSync(parent, { recursive: true, force: true }));
  const roots = Array.from({ length: 65 }, (_, index) => path.join(parent, `root-${index}`));
  for (const root of roots) fs.mkdirSync(root, { mode: 0o700 });
  for (let offset = 0; offset < roots.length; offset += 8) {
    const report = await createReviewEvidenceReadinessProbe({
      roots: roots.slice(offset, offset + 8),
      deadlineMs: 1_000,
    })();
    assert.equal(report.status, "ready", JSON.stringify(report));
  }
  const baseline = evidenceCapacitySnapshot();
  assert.equal(baseline.active_runs, 0);
  assert.equal(baseline.active_files, 0);
  const pendingRuns = roots.slice(0, 64).map((root, index) => createPrivateEvidenceRun({
    root,
    scopeId: `capacity-${index}`,
  }));
  const runs = await Promise.all(pendingRuns);
  await assert.rejects(
    () => createPrivateEvidenceRun({ root: roots[64], scopeId: "capacity-overflow" }),
    (error) => error.code === "REVIEW_EVIDENCE_CAPACITY_EXHAUSTED",
  );
  assert.equal(evidenceCapacitySnapshot().active_runs, 64);
  assert.equal(await cleanupPrivateEvidenceRun(runs[63]), true);

  for (let index = 0; index < 512; index += 1) {
    await reserveEvidenceFile(runs[0], `capacity-${index}`);
  }
  const beforeOverflow = evidenceCapacitySnapshot();
  assert.equal(beforeOverflow.active_files, 512);
  await assert.rejects(
    () => reserveEvidenceFile(runs[1], "capacity-overflow"),
    (error) => error.code === "REVIEW_EVIDENCE_CAPACITY_EXHAUSTED",
  );
  await assert.rejects(
    () => acquireReviewEvidenceAdmission({
      root: roots[64],
      scopeId: "pre-builder-capacity-overflow",
      mode: "text_loop",
      maxRounds: 1,
    }),
    (error) => error.code === "REVIEW_EVIDENCE_CAPACITY_EXHAUSTED"
      && /file/.test(error.message),
  );
  assert.deepEqual(evidenceCapacitySnapshot(), beforeOverflow);

  const cleanupResults = await Promise.all(runs.slice(0, 63).map((run) => cleanupPrivateEvidenceRun(run)));
  assert.equal(cleanupResults.every(Boolean), true);
  const finalCapacity = evidenceCapacitySnapshot();
  assert.equal(finalCapacity.active_runs, baseline.active_runs);
  assert.equal(finalCapacity.active_files, baseline.active_files);
  assert.equal(finalCapacity.active_bytes, baseline.active_bytes);
  assert.equal(finalCapacity.reserved_files, baseline.reserved_files);
  assert.equal(finalCapacity.reserved_bytes, baseline.reserved_bytes);
});

test("mode-aware admission reserves the full round envelope and releases it exactly once", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "review-evidence-reserved-envelope-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  assert.equal((await createReviewEvidenceReadinessProbe({ roots: [root] })()).status, "ready");
  const baseline = evidenceCapacitySnapshot();
  const admission = await acquireReviewEvidenceAdmission({
    root,
    scopeId: "reserved-envelope",
    mode: "text_loop",
    maxRounds: 3,
  });
  let snapshot = evidenceCapacitySnapshot();
  assert.equal(snapshot.active_runs, baseline.active_runs + 1);
  assert.equal(snapshot.active_admissions, baseline.active_admissions + 1);
  assert.equal(snapshot.reserved_files, baseline.reserved_files + 3);
  assert.equal(snapshot.reserved_bytes, baseline.reserved_bytes + 3 * 25 * 1024 * 1024);

  const run = await openReviewEvidenceAdmission(admission);
  const filepath = await reserveEvidenceFile(run, "reserved");
  fs.writeFileSync(filepath, PNG_BYTES);
  await sealManagedScreenshot(filepath);
  snapshot = evidenceCapacitySnapshot();
  assert.equal(snapshot.active_admissions, baseline.active_admissions);
  assert.equal(snapshot.active_files, baseline.active_files + 1);
  assert.equal(snapshot.reserved_files, baseline.reserved_files + 2);
  assert.equal(snapshot.active_bytes, baseline.active_bytes + PNG_BYTES.length);
  assert.equal(
    snapshot.reserved_bytes,
    baseline.reserved_bytes + 3 * 25 * 1024 * 1024 - PNG_BYTES.length,
  );

  assert.equal(await finalizeReviewEvidenceAdmission(admission, { retain: false }), true);
  assert.deepEqual(evidenceCapacitySnapshot(), baseline);
  assert.equal(await finalizeReviewEvidenceAdmission(admission, { retain: false }), true);
  assert.deepEqual(evidenceCapacitySnapshot(), baseline);
});

test("retention uses one nonblocking async cleanup timer with no unhandled rejection", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "review-evidence-retention-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  assert.equal((await createReviewEvidenceReadinessProbe({ roots: [root] })()).status, "ready");
  const baselineRuns = evidenceCapacitySnapshot().active_runs;
  const run = await createPrivateEvidenceRun({ root, scopeId: "retention-heartbeat" });
  const screenshot = await reserveEvidenceFile(run, "capture");
  fs.writeFileSync(screenshot, PNG_BYTES);
  await sealManagedScreenshot(screenshot);

  const originalRename = fs.promises.rename;
  let renameCalls = 0;
  fs.promises.rename = async function slowTombstoneRename(...args) {
    if (isEvidenceRunRenameSource(args[0], run.path)) {
      renameCalls += 1;
      await delay(75);
    }
    return originalRename.call(fs.promises, ...args);
  };
  let running = true;
  let heartbeats = 0;
  const heartbeat = () => {
    if (!running) return;
    heartbeats += 1;
    setImmediate(heartbeat);
  };
  setImmediate(heartbeat);
  const unhandled = [];
  const onUnhandled = (error) => unhandled.push(error);
  process.on("unhandledRejection", onUnhandled);
  try {
    assert.equal(await retainPrivateEvidenceRun(run, { retentionMs: 100 }), 100);
    assert.equal(await retainPrivateEvidenceRun(run, { retentionMs: 5 }), 5);
    await waitUntil(() => evidenceCapacitySnapshot().active_runs === baselineRuns);
    const report = await createReviewEvidenceReadinessProbe({ roots: [root], deadlineMs: 1_000 })();
    assert.equal(report.status, "ready", JSON.stringify(report));
    await delay(110);
  } finally {
    running = false;
    fs.promises.rename = originalRename;
    process.removeListener("unhandledRejection", onUnhandled);
  }
  assert.ok(heartbeats > 0, "slow retention cleanup blocked the event loop");
  assert.equal(renameCalls, 1, "superseded retention timer was not cleared");
  assert.deepEqual(unhandled, []);
});

test("cleanup directory-fsync failure latches a fail-closed durability fault", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "review-evidence-fsync-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  assert.equal((await createReviewEvidenceReadinessProbe({ roots: [root] })()).status, "ready");
  const run = await createPrivateEvidenceRun({ root, scopeId: "fsync-fault" });
  const screenshot = await reserveEvidenceFile(run, "capture");
  fs.writeFileSync(screenshot, PNG_BYTES);
  await sealManagedScreenshot(screenshot);

  const sampleHandle = await fs.promises.open(root, fs.constants.O_RDONLY | fs.constants.O_DIRECTORY);
  const fileHandlePrototype = Object.getPrototypeOf(sampleHandle);
  await sampleHandle.close();
  const originalSync = fileHandlePrototype.sync;
  fileHandlePrototype.sync = async function injectedDirectoryFsync() {
    if ((await this.stat()).isDirectory()) throw new Error("injected directory fsync failure");
    return originalSync.call(this);
  };
  try {
    assert.equal(await cleanupPrivateEvidenceRun(run), false);
  } finally {
    fileHandlePrototype.sync = originalSync;
  }

  const report = await createReviewEvidenceReadinessProbe({ roots: [root] })();
  assert.equal(report.status, "not_ready");
  assert.equal(report.revision.durability_fault_roots, 1);
  assert.ok(report.causes.some((cause) => cause.code === "REVIEW_EVIDENCE_DURABILITY_FAULT"));
});

test("retention cleanup failure is consumed and latches a durability fault", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "review-evidence-retention-failure-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  assert.equal((await createReviewEvidenceReadinessProbe({ roots: [root] })()).status, "ready");
  const run = await createPrivateEvidenceRun({ root, scopeId: "retention-failure" });
  const originalRename = fs.promises.rename;
  const initialFaults = evidenceCapacitySnapshot().durability_fault_roots;
  const unhandled = [];
  const onUnhandled = (error) => unhandled.push(error);
  process.on("unhandledRejection", onUnhandled);
  fs.promises.rename = async function failRetentionRename(source, ...args) {
    if (isEvidenceRunRenameSource(source, run.path)) {
      throw new Error("injected retention rename failure");
    }
    return originalRename.call(fs.promises, source, ...args);
  };
  try {
    await retainPrivateEvidenceRun(run, { retentionMs: 1 });
    await waitUntil(() => evidenceCapacitySnapshot().durability_fault_roots > initialFaults);
    await delay(10);
  } finally {
    fs.promises.rename = originalRename;
    process.removeListener("unhandledRejection", onUnhandled);
  }
  assert.deepEqual(unhandled, []);
  const report = await createReviewEvidenceReadinessProbe({ roots: [root] })();
  assert.equal(report.status, "not_ready");
  assert.ok(report.causes.some((cause) => cause.code === "REVIEW_EVIDENCE_DURABILITY_FAULT"));
});

test("hung first-write mkdir quarantines the root without a tombstone or capacity leak", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "review-evidence-hung-mkdir-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  assert.equal((await createReviewEvidenceReadinessProbe({ roots: [root] })()).status, "ready");
  const baseline = activeCapacity();
  const admission = await acquireReviewEvidenceAdmission({ root, scopeId: "hung-mkdir" });
  assert.equal(activeCapacity().active_runs, baseline.active_runs + 1);
  assert.equal(activeCapacity().active_admissions, baseline.active_admissions + 1);
  const originalMkdir = fs.promises.mkdir;
  let mkdirReached = false;
  fs.promises.mkdir = function hungRunMkdir(directory, ...args) {
    if (!mkdirReached && path.dirname(path.resolve(String(directory))) === root
        && path.basename(String(directory)).startsWith("scope-")) {
      mkdirReached = true;
      return new Promise(() => {});
    }
    return originalMkdir.call(fs.promises, directory, ...args);
  };
  const startedAt = Date.now();
  try {
    await assert.rejects(
      () => openReviewEvidenceAdmission(admission, { deadlineMs: 25 }),
      (error) => error.code === "REVIEW_EVIDENCE_ROOT_QUARANTINED",
    );
  } finally {
    fs.promises.mkdir = originalMkdir;
  }
  assert.equal(mkdirReached, true);
  assert.ok(Date.now() - startedAt < 500);
  assert.equal(await finalizeReviewEvidenceAdmission(admission, {
    retain: false,
    deadlineMs: 100,
  }), false);
  assert.deepEqual(fs.readdirSync(root), []);
  assert.deepEqual(activeCapacity(), baseline);
  const retryStartedAt = Date.now();
  await assert.rejects(
    () => acquireReviewEvidenceAdmission({ root, scopeId: "hung-mkdir-retry", deadlineMs: 1_000 }),
    (error) => error.code === "REVIEW_EVIDENCE_ROOT_QUARANTINED",
  );
  assert.ok(Date.now() - retryStartedAt < 100);
});

// Keep indeterminate post-commit cases at the end: they intentionally retain
// conservative capacity until process restart/operator recovery.
test("hung finalization rename is bounded, quarantines the root, and never releases uncertain capacity", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "review-evidence-hung-rename-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  assert.equal((await createReviewEvidenceReadinessProbe({ roots: [root] })()).status, "ready");
  const baseline = activeCapacity();
  const admission = await acquireReviewEvidenceAdmission({ root, scopeId: "hung-rename" });
  const run = await openReviewEvidenceAdmission(admission);
  const originalRename = fs.promises.rename;
  let renameReached = false;
  fs.promises.rename = function hungRunRename(source, ...args) {
    if (!renameReached && isEvidenceRunRenameSource(source, run.path)) {
      renameReached = true;
      return new Promise(() => {});
    }
    return originalRename.call(fs.promises, source, ...args);
  };
  const startedAt = Date.now();
  let finalized;
  try {
    finalized = await finalizeReviewEvidenceAdmission(admission, {
      retain: false,
      deadlineMs: 100,
    });
  } finally {
    fs.promises.rename = originalRename;
  }
  assert.equal(renameReached, true);
  assert.equal(finalized, false);
  assert.ok(Date.now() - startedAt < 500);
  assert.equal(activeCapacity().active_runs, baseline.active_runs + 1);
  assert.equal(activeCapacity().active_admissions, baseline.active_admissions);
  const afterFailure = activeCapacity();
  const retryStartedAt = Date.now();
  await assert.rejects(
    () => acquireReviewEvidenceAdmission({ root, scopeId: "hung-rename-retry", deadlineMs: 1_000 }),
    (error) => error.code === "REVIEW_EVIDENCE_ROOT_QUARANTINED",
  );
  assert.ok(Date.now() - retryStartedAt < 100);
  assert.deepEqual(activeCapacity(), afterFailure);
});

test("post-mkdir durability timeout cannot be finalized as an empty admission", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "review-evidence-post-mkdir-timeout-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  assert.equal((await createReviewEvidenceReadinessProbe({ roots: [root] })()).status, "ready");
  const baseline = activeCapacity();
  const admission = await acquireReviewEvidenceAdmission({ root, scopeId: "post-mkdir-timeout" });
  const originalOpen = fs.promises.open;
  let syncReached = false;
  let releaseSync = null;
  fs.promises.open = async function hangRootDirectorySync(file, ...args) {
    const handle = await originalOpen.call(fs.promises, file, ...args);
    if (path.resolve(String(file)) === root) {
      const originalSync = handle.sync.bind(handle);
      let resolveSync;
      const gate = new Promise((resolve) => { resolveSync = resolve; });
      handle.sync = () => {
        syncReached = true;
        return gate.then(() => originalSync());
      };
      releaseSync = resolveSync;
    }
    return handle;
  };
  const syncDeadlineMs = 500;
  const startedAt = Date.now();
  try {
    await assert.rejects(
      () => openReviewEvidenceAdmission(admission, { deadlineMs: syncDeadlineMs }),
      (error) => error.code === "REVIEW_EVIDENCE_ROOT_QUARANTINED",
    );
  } finally {
    fs.promises.open = originalOpen;
    if (releaseSync) releaseSync();
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.equal(syncReached, true);
  assert.ok(Date.now() - startedAt < syncDeadlineMs + 500);
  assert.equal(await finalizeReviewEvidenceAdmission(admission, {
    retain: false,
    deadlineMs: 100,
  }), false);
  assert.equal(activeCapacity().active_runs, baseline.active_runs + 1);
  assert.equal(activeCapacity().active_admissions, baseline.active_admissions);
  assert.ok(fs.readdirSync(root).some((entry) => entry.startsWith("scope-")));
});

test("fresh process cleanup rejects a root ABA without releasing capacity or masking identity failure", () => {
  const criticPath = path.resolve(__dirname, "..", "scene-critic.js");
  const script = String.raw`
    "use strict";
    const assert = require("node:assert/strict");
    const fs = require("node:fs");
    const os = require("node:os");
    const path = require("node:path");
    const critic = require(${JSON.stringify(criticPath)});
    const png = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1]);
    const active = (value = critic.evidenceCapacitySnapshot()) => ({
      active_runs: value.active_runs,
      active_admissions: value.active_admissions,
      active_files: value.active_files,
      active_bytes: value.active_bytes,
      reserved_files: value.reserved_files,
      reserved_bytes: value.reserved_bytes,
    });
    (async () => {
      const root = fs.mkdtempSync(path.join(os.tmpdir(), "review-evidence-root-aba-child-"));
      const displaced = root + "-displaced";
      try {
        assert.equal((await critic.createReviewEvidenceReadinessProbe({ roots: [root] })()).status, "ready");
        const run = await critic.createPrivateEvidenceRun({ root, scopeId: "root-aba-child" });
        const screenshot = await critic.reserveEvidenceFile(run, "capture");
        fs.writeFileSync(screenshot, png);
        await critic.sealManagedScreenshot(screenshot);
        const operatorRun = path.join(run.path, "operator-run-sentinel.txt");
        fs.writeFileSync(operatorRun, "preserve-run", { mode: 0o600 });
        const beforeCleanup = active();

        const originalLstat = fs.promises.lstat;
        const originalOpen = fs.promises.open;
        const originalRename = fs.promises.rename;
        let abaInjected = false;
        let closeInjected = false;
        let closeReached = false;
        let cleanupRenameCalls = 0;
        fs.promises.open = async function injectCloseFailure(file, ...args) {
          const handle = await originalOpen.call(fs.promises, file, ...args);
          if (!closeInjected && path.resolve(String(file)) === root) {
            closeInjected = true;
            const originalClose = handle.close.bind(handle);
            handle.close = async () => {
              closeReached = true;
              await originalClose();
              throw new Error("injected cleanup authority close failure");
            };
          }
          return handle;
        };
        fs.promises.rename = async function countCleanupRename(source, destination, ...args) {
          if (String(source).startsWith("/proc/self/fd/")
              && path.basename(String(source)) === path.basename(run.path)) {
            cleanupRenameCalls += 1;
          }
          return originalRename.call(fs.promises, source, destination, ...args);
        };
        fs.promises.lstat = async function injectRootAba(file, ...args) {
          if (!abaInjected && path.resolve(String(file)) === run.path) {
            abaInjected = true;
            await originalRename.call(fs.promises, root, displaced);
            await fs.promises.writeFile(
              path.join(displaced, "displaced-root-sentinel.txt"),
              "preserve-displaced",
              { mode: 0o600 },
            );
            await fs.promises.mkdir(root, { mode: 0o700 });
            await fs.promises.writeFile(
              path.join(root, "replacement-root-sentinel.txt"),
              "preserve-replacement",
              { mode: 0o600 },
            );
            await originalRename.call(
              fs.promises,
              path.join(displaced, path.basename(run.path)),
              path.join(root, path.basename(run.path)),
            );
          }
          return originalLstat.call(fs.promises, file, ...args);
        };

        let cleaned;
        try {
          cleaned = await critic.cleanupPrivateEvidenceRun(run);
        } finally {
          fs.promises.lstat = originalLstat;
          fs.promises.open = originalOpen;
          fs.promises.rename = originalRename;
        }

        assert.equal(abaInjected, true);
        assert.equal(closeInjected, true);
        assert.equal(closeReached, true);
        assert.equal(cleanupRenameCalls, 0);
        assert.equal(cleaned, false);
        assert.deepEqual(active(), beforeCleanup);
        assert.equal(fs.readFileSync(screenshot).equals(png), true);
        assert.equal(fs.readFileSync(operatorRun, "utf8"), "preserve-run");
        assert.equal(
          fs.readFileSync(path.join(root, "replacement-root-sentinel.txt"), "utf8"),
          "preserve-replacement",
        );
        assert.equal(
          fs.readFileSync(path.join(displaced, "displaced-root-sentinel.txt"), "utf8"),
          "preserve-displaced",
        );
        assert.equal(fs.existsSync(path.join(root, ".review-evidence-tombstones")), false);
        assert.equal(fs.existsSync(path.join(displaced, ".review-evidence-tombstones")), false);

        const readiness = await critic.createReviewEvidenceReadinessProbe({ roots: [root] })();
        assert.equal(readiness.status, "not_ready");
        assert.ok(readiness.revision.durability_fault_roots >= 1);
        await assert.rejects(
          () => critic.acquireReviewEvidenceAdmission({ root, scopeId: "root-aba-retry" }),
          (error) => [
            "REVIEW_EVIDENCE_ORPHAN_BACKLOG",
            "REVIEW_EVIDENCE_ROOT_QUARANTINED",
          ].includes(error.code),
        );
      } finally {
        fs.rmSync(root, { recursive: true, force: true });
        fs.rmSync(displaced, { recursive: true, force: true });
      }
    })().catch((error) => {
      console.error(error && error.stack || error);
      process.exitCode = 1;
    });
  `;
  const result = spawnSync(process.execPath, ["-e", script], {
    encoding: "utf8",
    timeout: 15_000,
  });
  assert.equal(
    result.status,
    0,
    [result.error && result.error.stack, result.stdout, result.stderr].filter(Boolean).join("\n"),
  );
});

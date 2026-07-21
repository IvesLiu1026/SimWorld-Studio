"use strict";

// VLM-based scene critic. Provider execution is delegated to the tool-free,
// strict-schema review adapter; this module owns evidence capture/validation
// and preserves the loop-facing verdict contract.
const crypto = require("node:crypto");
const fs = require("fs");
const path = require("path");
const { ReviewProviderError, review: reviewScene } = require("./review-provider");
const { getUeBroker } = require("./unreal-bridge");

const ARENA_ROOT = path.resolve(__dirname, "..", "..");
const SCREENSHOT_DIR = path.join(ARENA_ROOT, "tmp", "review-evidence", "text");
const MAX_SCREENSHOT_BYTES = 25 * 1024 * 1024;
const MAX_ACTOR_CONTEXT_BYTES = 2 * 1024 * 1024;
const PRIVATE_DIRECTORY_MODE = 0o700;
const PRIVATE_FILE_MODE = 0o600;
const MAX_ACTIVE_EVIDENCE_RUNS = 64;
const MAX_ACTIVE_EVIDENCE_FILES = 512;
const MAX_ACTIVE_EVIDENCE_BYTES = 512 * 1024 * 1024;
const DEFAULT_EVIDENCE_RETENTION_MS = 60 * 1000;
const MAX_EVIDENCE_RETENTION_MS = 5 * 60 * 1000;
const EVIDENCE_RUN_STATES = new WeakMap();
const EVIDENCE_ADMISSION_STATES = new WeakMap();
const EVIDENCE_RUNS_BY_PATH = new Map();
const EVIDENCE_FILES = new Map();
const EVIDENCE_HANDLES = new Map();
const SWEPT_EVIDENCE_ROOTS = new Map();
const EVIDENCE_ORPHAN_BACKLOGS = new Map();
const EVIDENCE_DURABILITY_FAULT_ROOTS = new Set();
const EVIDENCE_SWEEP_TAILS = new Map();
const EVIDENCE_ROOT_POISONS = new Map();
const EVIDENCE_SWEEP_CERTIFICATION = Symbol("review-evidence-sweep-certification");
const BROKER_AUTHORITY_DIGESTS = new WeakMap();
const EVIDENCE_CAPACITY = {
  activeRuns: 0,
  activeAdmissions: 0,
  activeFiles: 0,
  activeBytes: 0,
  reservedFiles: 0,
  reservedBytes: 0,
};
const VISUAL_SCREENSHOT_DIR = path.join(ARENA_ROOT, "tmp", "review-evidence", "visual");
const DEFAULT_REVIEW_EVIDENCE_ROOTS = Object.freeze([SCREENSHOT_DIR, VISUAL_SCREENSHOT_DIR]);
const DEFAULT_EVIDENCE_SWEEP_MAX_OPERATIONS = 20_000;
const DEFAULT_EVIDENCE_SWEEP_DEADLINE_MS = 250;
const DEFAULT_EVIDENCE_SWEEP_BATCH_OPERATIONS = 16;
const EVIDENCE_TOMBSTONE_DIRECTORY = ".review-evidence-tombstones";
const EVIDENCE_TOMBSTONE_PATTERN = /^tombstone-[a-f0-9]{64}$/;
const DEFAULT_MAX_EVIDENCE_TOMBSTONES = 512;
const DEFAULT_MAX_EVIDENCE_TOMBSTONE_ENTRIES = 4_096;
const DEFAULT_MAX_EVIDENCE_TOMBSTONE_BYTES = 64 * 1024 * 1024;
const DEFAULT_EVIDENCE_OPERATION_DEADLINE_MS = 5_000;
const DEFAULT_EVIDENCE_IO_DEADLINE_MS = 10_000;
const EVIDENCE_IO_CHUNK_BYTES = 256 * 1024;
let DEFAULT_EVIDENCE_BOOTSTRAP_PROMISE = null;

const CRITIC_SYSTEM_PROMPT = `You are a 3D scene verification expert for SimWorld Studio (Unreal Engine 5).
Analyze only the supplied scene screenshot, actor list, and scene request. Do not invoke tools or
infer that you can inspect the host system.

Evaluate:
1. Completeness: Are all requested objects present?
2. Placement: Are objects in good positions? (X/Y within -9500 to 9500, not overlapping, not outside ground)
3. Scale: Do objects look appropriately sized relative to each other?
4. Realism: Does the scene match the original request?
5. Issues: Any obvious problems (floating objects above ground, buried below ground, misaligned, upside-down)?
6. Navigation/walkability: Large buildings (BP_Building_*) block navigation near PlayerStart and belong as
   background scenery far from center (>2500 UU). Small props are fine anywhere. Trees belong at the edge.

Return one JSON object matching the supplied schema. status must be PASS, NEEDS_IMPROVEMENT, or FAIL.
issues and suggestions must be concise string arrays. raw_notes may contain a concise evidence summary.`;

function typedError(code, message, details = {}) {
  return new ReviewProviderError(code, message, details);
}

function requireSecureFilesystemPrimitives() {
  if (process.platform !== "linux"
      || !Number.isInteger(fs.constants.O_NOFOLLOW)
      || !Number.isInteger(fs.constants.O_DIRECTORY)
      || !Number.isInteger(fs.constants.O_EXCL)) {
    throw typedError(
      "REVIEW_EVIDENCE_PLATFORM_UNSUPPORTED",
      "Review evidence requires Linux no-follow and exclusive-create filesystem semantics",
    );
  }
}

function currentUid() {
  return typeof process.getuid === "function" ? process.getuid() : null;
}

function bootstrapDescriptorChild(handle, name) {
  if (!handle || !Number.isSafeInteger(handle.fd) || handle.fd < 0
      || typeof name !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(name)) {
    throw typedError("REVIEW_EVIDENCE_INVALID", "Review evidence bootstrap child is invalid");
  }
  return `/proc/self/fd/${handle.fd}/${name}`;
}

async function openBootstrapAuthority(directory, budget, { privateDirectory = false } = {}) {
  const checked = path.resolve(directory);
  const poisoned = evidenceRootPoisonError(checked);
  if (poisoned) throw poisoned;
  let handle;
  let primaryError = null;
  try {
    const lexical = await runSweepFsOperation(
      budget,
      () => fs.promises.lstat(checked, { bigint: true }),
    );
    const resolved = await runSweepFsOperation(budget, () => fs.promises.realpath(checked));
    const modeValid = privateDirectory
      ? statMode(lexical) === PRIVATE_DIRECTORY_MODE
      : (statMode(lexical) & 0o022) === 0;
    if (!lexical.isDirectory() || lexical.isSymbolicLink() || resolved !== checked
        || statNlink(lexical) < 1 || !modeValid
        || (currentUid() !== null && statUid(lexical) !== currentUid())) {
      throw typedError(
        "REVIEW_EVIDENCE_INVALID",
        privateDirectory
          ? "Review evidence bootstrap directory is not private and owner-controlled"
          : "Review evidence bootstrap parent is not owner-controlled",
      );
    }
    handle = await runSweepFsOperation(
      budget,
      () => fs.promises.open(
        checked,
        fs.constants.O_RDONLY | fs.constants.O_DIRECTORY | fs.constants.O_NOFOLLOW,
      ),
      { disposeLate: detachSweepHandleClose },
    );
    const opened = await runSweepFsOperation(budget, () => handle.stat({ bigint: true }));
    const openedModeValid = privateDirectory
      ? statMode(opened) === PRIVATE_DIRECTORY_MODE
      : (statMode(opened) & 0o022) === 0;
    if (!opened.isDirectory() || !sameIdentity(opened, lexical) || !openedModeValid
        || (currentUid() !== null && statUid(opened) !== currentUid())) {
      throw typedError("REVIEW_EVIDENCE_INVALID", "Review evidence bootstrap directory changed during verification");
    }
    return { checked, handle, identity: opened, privateDirectory };
  } catch (error) {
    primaryError = error;
    throw error;
  } finally {
    if (primaryError && handle) {
      await closeEvidenceHandle(handle, {
        budget,
        primaryError,
        faultRoot: checked,
        message: "Review evidence bootstrap authority handle could not be closed safely",
      });
    }
  }
}

async function revalidateBootstrapAuthority(authority, budget) {
  const lexical = await runSweepFsOperation(
    budget,
    () => fs.promises.lstat(authority.checked, { bigint: true }),
  );
  const resolved = await runSweepFsOperation(
    budget,
    () => fs.promises.realpath(authority.checked),
  );
  const modeValid = authority.privateDirectory
    ? statMode(lexical) === PRIVATE_DIRECTORY_MODE
    : (statMode(lexical) & 0o022) === 0;
  if (!lexical.isDirectory() || lexical.isSymbolicLink() || resolved !== authority.checked
      || !sameIdentity(lexical, authority.identity) || statNlink(lexical) < 1 || !modeValid
      || (currentUid() !== null && statUid(lexical) !== currentUid())) {
    throw typedError("REVIEW_EVIDENCE_INVALID", "Review evidence bootstrap parent changed during creation");
  }
}

async function closeBootstrapAuthority(authority, budget, primaryError = null) {
  if (!authority || !authority.handle) return;
  const handle = authority.handle;
  authority.handle = null;
  await closeEvidenceHandle(handle, {
    budget,
    primaryError,
    faultRoot: authority.checked,
    durabilityFault: Boolean(primaryError),
    message: "Review evidence bootstrap authority handle could not be closed safely",
  });
}

async function assertBootstrapParent(directory, budget) {
  const authority = await openBootstrapAuthority(directory, budget);
  await closeBootstrapAuthority(authority, budget);
}

async function bootstrapDirectory(directory, budget, { privateDirectory }) {
  const checked = path.resolve(directory);
  const parent = path.dirname(checked);
  const childName = path.basename(checked);
  let parentAuthority;
  let childHandle;
  let primaryError = null;
  try {
    parentAuthority = await openBootstrapAuthority(parent, budget);
    const anchoredChild = bootstrapDescriptorChild(parentAuthority.handle, childName);
    let created = false;
    try {
      await runTrackedRootMutation(
        checked,
        budget,
        () => fs.promises.mkdir(anchoredChild, { mode: PRIVATE_DIRECTORY_MODE }),
        { operationName: privateDirectory ? "bootstrap_mkdir" : "bootstrap_runtime_root_mkdir" },
      );
      created = true;
    } catch (error) {
      if (!error || error.code !== "EEXIST") throw error;
    }
    const lexical = await runSweepFsOperation(
      budget,
      () => fs.promises.lstat(anchoredChild, { bigint: true }),
    );
    const modeValid = privateDirectory
      ? statMode(lexical) === PRIVATE_DIRECTORY_MODE
      : (statMode(lexical) & 0o022) === 0;
    if (!lexical.isDirectory() || lexical.isSymbolicLink() || statNlink(lexical) < 1 || !modeValid
        || (currentUid() !== null && statUid(lexical) !== currentUid())) {
      throw typedError(
        "REVIEW_EVIDENCE_INVALID",
        privateDirectory
          ? "Review evidence bootstrap directory is not private and owner-controlled"
          : "Review evidence bootstrap parent is not owner-controlled",
      );
    }
    childHandle = await runSweepFsOperation(
      budget,
      () => fs.promises.open(
        anchoredChild,
        fs.constants.O_RDONLY | fs.constants.O_DIRECTORY | fs.constants.O_NOFOLLOW,
      ),
      { disposeLate: detachSweepHandleClose },
    );
    const opened = await runSweepFsOperation(budget, () => childHandle.stat({ bigint: true }));
    if (!opened.isDirectory() || !sameIdentity(opened, lexical)
        || (privateDirectory && statMode(opened) !== PRIVATE_DIRECTORY_MODE)
        || (!privateDirectory && (statMode(opened) & 0o022) !== 0)
        || (currentUid() !== null && statUid(opened) !== currentUid())) {
      throw typedError("REVIEW_EVIDENCE_INVALID", "Review evidence bootstrap directory changed during verification");
    }
    await revalidateBootstrapAuthority(parentAuthority, budget);
    if (created) {
      await runTrackedRootMutation(checked, budget, () => parentAuthority.handle.sync(), {
        operationName: "bootstrap_parent_fsync",
      });
      await revalidateBootstrapAuthority(parentAuthority, budget);
    }
    return checked;
  } catch (error) {
    primaryError = error;
    throw error;
  } finally {
    let cleanupError = null;
    if (childHandle) {
      try {
        await closeEvidenceHandle(childHandle, {
          budget,
          primaryError,
          faultRoot: checked,
          message: "Review evidence bootstrap directory handle could not be closed safely",
        });
      } catch (error) {
        cleanupError = error;
      }
    }
    try {
      await closeBootstrapAuthority(parentAuthority, budget, primaryError || cleanupError);
    } catch (error) {
      if (!cleanupError) cleanupError = error;
    }
    if (!primaryError && cleanupError) throw cleanupError;
  }
}

async function bootstrapPrivateDirectory(directory, budget) {
  return bootstrapDirectory(directory, budget, { privateDirectory: true });
}

async function bootstrapOwnerControlledParent(directory, budget) {
  return bootstrapDirectory(directory, budget, { privateDirectory: false });
}

async function bootstrapReviewEvidenceHierarchy({
  arenaRoot = ARENA_ROOT,
  signal,
  deadlineMs = DEFAULT_EVIDENCE_OPERATION_DEADLINE_MS,
} = {}) {
  requireSecureFilesystemPrimitives();
  const checkedArenaRoot = path.resolve(String(arenaRoot || ""));
  if (checkedArenaRoot === path.parse(checkedArenaRoot).root) {
    throw typedError("REVIEW_EVIDENCE_INVALID", "Review evidence arena root is invalid");
  }
  const budget = createRuntimeEvidenceBudget({ signal, deadlineMs });
  try {
    await assertBootstrapParent(checkedArenaRoot, budget);
    // `tmp` is shared with legacy runtime artifacts and may intentionally be
    // 0755. It must be owner-controlled and non-symlink, while the dedicated
    // evidence parent and both leaves are exactly 0700.
    const runtimeRoot = await bootstrapOwnerControlledParent(path.join(checkedArenaRoot, "tmp"), budget);
    const evidenceRoot = await bootstrapPrivateDirectory(path.join(runtimeRoot, "review-evidence"), budget);
    const roots = [path.join(evidenceRoot, "text"), path.join(evidenceRoot, "visual")];
    for (const directory of roots) await bootstrapPrivateDirectory(directory, budget);
    return Object.freeze(roots);
  } finally {
    disposeSweepBudget(budget);
  }
}

function bootstrapDefaultReviewEvidenceRoots() {
  if (DEFAULT_EVIDENCE_BOOTSTRAP_PROMISE) return DEFAULT_EVIDENCE_BOOTSTRAP_PROMISE;
  DEFAULT_EVIDENCE_BOOTSTRAP_PROMISE = bootstrapReviewEvidenceHierarchy().catch((error) => {
    DEFAULT_EVIDENCE_BOOTSTRAP_PROMISE = null;
    if (error instanceof ReviewProviderError) throw error;
    throw typedError(
      "REVIEW_EVIDENCE_UNAVAILABLE",
      "Default Review evidence roots could not be bootstrapped safely",
      { retryable: false, cause: error },
    );
  });
  return DEFAULT_EVIDENCE_BOOTSTRAP_PROMISE;
}

function bigintIdentityPart(value) {
  if (typeof value === "bigint") return value.toString();
  if (typeof value === "string" && /^\d+$/.test(value)) return value;
  if (typeof value === "number" && Number.isSafeInteger(value) && value >= 0) return String(value);
  return null;
}

function sameIdentity(left, right) {
  return Boolean(left && right
    && bigintIdentityPart(left.dev) !== null
    && bigintIdentityPart(left.ino) !== null
    && bigintIdentityPart(left.dev) === bigintIdentityPart(right.dev)
    && bigintIdentityPart(left.ino) === bigintIdentityPart(right.ino));
}

function statMode(stat) {
  return Number(typeof stat.mode === "bigint" ? stat.mode & 0o7777n : stat.mode & 0o7777);
}

function statUid(stat) {
  return Number(stat.uid);
}

function statNlink(stat) {
  return Number(stat.nlink);
}

function statSize(stat) {
  const size = typeof stat.size === "bigint" ? Number(stat.size) : stat.size;
  return Number.isSafeInteger(size) && size >= 0 ? size : Number.NaN;
}

function directoryIdentityFromStats(stat) {
  if (typeof stat.ctimeNs !== "bigint" || typeof stat.mtimeNs !== "bigint") {
    throw typedError(
      "REVIEW_EVIDENCE_INVALID",
      "Review evidence identity requires nanosecond-precision filesystem metadata",
    );
  }
  return Object.freeze({
    dev: bigintIdentityPart(stat.dev),
    ino: bigintIdentityPart(stat.ino),
    uid: statUid(stat),
    mode: statMode(stat),
    size: typeof stat.size === "bigint" ? stat.size.toString() : String(stat.size),
    ctimeNs: stat.ctimeNs.toString(),
    mtimeNs: stat.mtimeNs.toString(),
  });
}

function sameSweepRootIdentity(left, right) {
  return sameIdentity(left, right)
    && left.size === right.size
    && typeof left.ctimeNs === "string"
    && typeof right.ctimeNs === "string"
    && typeof left.mtimeNs === "string"
    && typeof right.mtimeNs === "string"
    && left.ctimeNs === right.ctimeNs
    && left.mtimeNs === right.mtimeNs;
}

function sameOptionalSweepIdentity(left, right) {
  return left === null || right === null
    ? left === right
    : sameSweepRootIdentity(left, right);
}

function cacheEvidenceSweep(root, identity, backlog, tombstoneIdentity, authority) {
  if (authority !== EVIDENCE_SWEEP_CERTIFICATION) {
    throw new TypeError("Only a complete stable Review evidence scan may publish sweep state");
  }
  const checked = path.resolve(String(root || ""));
  const effectiveBacklog = EVIDENCE_DURABILITY_FAULT_ROOTS.has(checked)
    ? Math.max(1, Number(backlog) || 0)
    : Math.max(0, Number(backlog) || 0);
  EVIDENCE_ORPHAN_BACKLOGS.set(checked, effectiveBacklog);
  SWEPT_EVIDENCE_ROOTS.set(checked, Object.freeze({
    dev: identity.dev,
    ino: identity.ino,
    size: identity.size,
    ctimeNs: identity.ctimeNs,
    mtimeNs: identity.mtimeNs,
    tombstoneIdentity: tombstoneIdentity || null,
  }));
  return effectiveBacklog;
}

function markEvidenceBacklog(root, { durabilityFault = false } = {}) {
  const checked = path.resolve(String(root || ""));
  EVIDENCE_ORPHAN_BACKLOGS.set(checked, Math.max(1, EVIDENCE_ORPHAN_BACKLOGS.get(checked) || 0));
  SWEPT_EVIDENCE_ROOTS.delete(checked);
  if (durabilityFault) EVIDENCE_DURABILITY_FAULT_ROOTS.add(checked);
}

function evidenceRootPoisonError(root) {
  const checked = path.resolve(String(root || ""));
  const poison = EVIDENCE_ROOT_POISONS.get(checked);
  if (!poison) return null;
  return typedError(
    "REVIEW_EVIDENCE_ROOT_QUARANTINED",
    "Review evidence root has an unresolved timed-out filesystem mutation",
    { retryable: false, operation: poison.operation },
  );
}

function poisonEvidenceRoot(root, operation, pending) {
  const checked = path.resolve(String(root || ""));
  if (!EVIDENCE_ROOT_POISONS.has(checked)) {
    EVIDENCE_ROOT_POISONS.set(checked, Object.freeze({
      operation: String(operation || "filesystem_mutation"),
      poisonedAt: Date.now(),
    }));
  }
  markEvidenceBacklog(checked, { durabilityFault: true });
  // The late operation remains observed for the lifetime of the process. Its
  // result can only reinforce quarantine; it can never republish authority,
  // release capacity, or make the root clean.
  Promise.resolve(pending).then(
    () => markEvidenceBacklog(checked, { durabilityFault: true }),
    () => markEvidenceBacklog(checked, { durabilityFault: true }),
  ).catch(() => {});
}

function sweepControlError(code, message, details = {}) {
  return typedError(code, message, { retryable: true, ...details });
}

function createSweepBudget(limits, signal) {
  const budget = {
    ...limits,
    operations: 0,
    directories: 0,
    files: 0,
    tombstoneEntries: 0,
    tombstoneBytes: 0,
    tombstoneRoots: 0,
    stepsSinceYield: 0,
    deadlineAt: Date.now() + limits.deadlineMs,
    signal,
    controlError: null,
    controlTimer: null,
    abortHandler: null,
    settleControl: null,
  };
  budget.controlPromise = new Promise((resolve) => {
    budget.settleControl = (error) => {
      if (budget.controlError) return;
      budget.controlError = error;
      resolve(error);
    };
  });
  budget.controlTimer = setTimeout(() => budget.settleControl(sweepControlError(
    "REVIEW_EVIDENCE_SWEEP_DEADLINE",
    "Review evidence verification exceeded its bounded deadline",
  )), Math.max(1, limits.deadlineMs));
  if (signal) {
    budget.abortHandler = () => budget.settleControl(sweepControlError(
      "REVIEW_EVIDENCE_PROBE_ABORTED",
      "Review evidence readiness verification was cancelled",
    ));
    if (signal.aborted) budget.abortHandler();
    else signal.addEventListener("abort", budget.abortHandler, { once: true });
  }
  return budget;
}

function disposeSweepBudget(budget) {
  if (budget.controlTimer) {
    clearTimeout(budget.controlTimer);
    budget.controlTimer = null;
  }
  if (budget.signal && budget.abortHandler) {
    budget.signal.removeEventListener("abort", budget.abortHandler);
    budget.abortHandler = null;
  }
}

function assertSweepControl(budget) {
  if (budget.controlError) throw budget.controlError;
  if (budget.signal && budget.signal.aborted) {
    const error = sweepControlError(
      "REVIEW_EVIDENCE_PROBE_ABORTED",
      "Review evidence readiness verification was cancelled",
    );
    if (budget.settleControl) budget.settleControl(error);
    throw error;
  }
  if (Date.now() >= budget.deadlineAt) {
    const error = sweepControlError(
      "REVIEW_EVIDENCE_SWEEP_DEADLINE",
      "Review evidence verification exceeded its bounded deadline",
    );
    if (budget.settleControl) budget.settleControl(error);
    throw error;
  }
}

async function waitForSerializedSweep(predecessor, budget) {
  assertSweepControl(budget);
  const winner = await Promise.race([
    predecessor.then(
      () => ({ type: "predecessor" }),
      () => ({ type: "predecessor" }),
    ),
    budget.controlPromise.then((error) => ({ type: "control", error })),
  ]);
  if (winner.type === "control") throw winner.error;
  assertSweepControl(budget);
}

async function withSerializedEvidenceSweep(root, budget, work) {
  const checked = path.resolve(String(root || ""));
  const poisonedBeforeQueue = evidenceRootPoisonError(checked);
  if (poisonedBeforeQueue) throw poisonedBeforeQueue;
  const predecessor = EVIDENCE_SWEEP_TAILS.get(checked) || Promise.resolve();
  let release;
  const completion = new Promise((resolve) => { release = resolve; });
  const tail = predecessor.catch(() => {}).then(() => completion);
  EVIDENCE_SWEEP_TAILS.set(checked, tail);
  tail.then(() => {
    if (EVIDENCE_SWEEP_TAILS.get(checked) === tail) EVIDENCE_SWEEP_TAILS.delete(checked);
  });
  try {
    if (budget) await waitForSerializedSweep(predecessor, budget);
    else await predecessor.catch(() => {});
    const poisonedAfterQueue = evidenceRootPoisonError(checked);
    if (poisonedAfterQueue) throw poisonedAfterQueue;
    return await work();
  } finally {
    release();
  }
}

async function runTrackedRootMutation(root, budget, operation, {
  operationName = "filesystem_mutation",
  disposeLate = null,
} = {}) {
  const checked = path.resolve(String(root || ""));
  const poisoned = evidenceRootPoisonError(checked);
  if (poisoned) throw poisoned;
  if (!budget) return operation();
  consumeSweepOperation(budget);
  assertSweepControl(budget);
  const pending = Promise.resolve().then(operation);
  const observed = pending.then(
    (value) => ({ type: "result", value }),
    (error) => ({ type: "error", error }),
  );
  const winner = await Promise.race([
    observed,
    budget.controlPromise.then((error) => ({ type: "control", error })),
  ]);
  if (winner.type === "control") {
    poisonEvidenceRoot(checked, operationName, pending);
    if (disposeLate) {
      observed.then((late) => {
        if (late.type === "result") {
          Promise.resolve().then(() => disposeLate(late.value)).catch(() => {});
        }
      }).catch(() => {});
    }
    throw evidenceRootPoisonError(checked);
  }
  if (winner.type === "error") throw winner.error;
  // A completed mutator is handed back synchronously to its caller so the
  // caller can record the resulting identity before observing cancellation.
  // Subsequent bounded operations perform the next control checkpoint.
  return winner.value;
}

function consumeSweepOperation(budget) {
  assertSweepControl(budget);
  if (budget.operations >= budget.maxOperations) {
    throw sweepControlError(
      "REVIEW_EVIDENCE_SWEEP_BUDGET_EXHAUSTED",
      "Review evidence verification exhausted its global operation budget",
      { resource: "operation" },
    );
  }
  budget.operations += 1;
}

function consumeSweepResource(budget, kind) {
  assertSweepControl(budget);
  if (kind === "directory" && budget.directories >= budget.maxDirectories) {
    throw sweepControlError(
      "REVIEW_EVIDENCE_SWEEP_BUDGET_EXHAUSTED",
      "Review evidence verification exhausted its global directory budget",
      { resource: "directory" },
    );
  }
  if (kind === "file" && budget.files >= budget.maxFiles) {
    throw sweepControlError(
      "REVIEW_EVIDENCE_SWEEP_BUDGET_EXHAUSTED",
      "Review evidence verification exhausted its global file budget",
      { resource: "file" },
    );
  }
  if (kind === "directory") budget.directories += 1;
  if (kind === "file") budget.files += 1;
}

async function checkpointSweepBudget(budget) {
  assertSweepControl(budget);
  budget.stepsSinceYield += 1;
  if (budget.stepsSinceYield < budget.batchOperations) return;
  budget.stepsSinceYield = 0;
  const yielded = new Promise((resolve) => setImmediate(resolve));
  const winner = await Promise.race([
    yielded.then(() => ({ type: "yield" })),
    budget.controlPromise.then((error) => ({ type: "control", error })),
  ]);
  if (winner.type === "control") throw winner.error;
  assertSweepControl(budget);
}

function detachSweepHandleClose(handle) {
  Promise.resolve()
    .then(() => handle.close())
    .catch((error) => {
      if (!error || error.code !== "ERR_DIR_CLOSED") return undefined;
      return undefined;
    });
}

async function runSweepFsOperation(budget, operation, { disposeLate = null } = {}) {
  consumeSweepOperation(budget);
  const pending = Promise.resolve().then(operation);
  const observed = pending.then(
    (value) => ({ type: "result", value }),
    (error) => ({ type: "error", error }),
  );
  const winner = await Promise.race([
    observed,
    budget.controlPromise.then((error) => ({ type: "control", error })),
  ]);
  if (winner.type === "control") {
    // Readiness operations are read-only. Their late result is detached from
    // the probe, and late handles are closed without touching sweep state.
    observed.then((late) => {
      if (late.type === "result" && disposeLate) {
        Promise.resolve().then(() => disposeLate(late.value)).catch(() => {});
      }
    }).catch(() => {});
    throw winner.error;
  }
  assertSweepControl(budget);
  if (winner.type === "error") throw winner.error;
  assertSweepControl(budget);
  await checkpointSweepBudget(budget);
  return winner.value;
}

async function closeSweepHandle(handle, budget, onStart = null) {
  try {
    await runSweepFsOperation(budget, () => {
      if (onStart) onStart();
      return handle.close();
    });
  } catch (error) {
    if (error && error.code === "ERR_DIR_CLOSED") {
      assertSweepControl(budget);
      return;
    }
    throw error;
  }
}

async function closeEvidenceHandle(handle, {
  budget = null,
  primaryError = null,
  faultRoot = null,
  durabilityFault = false,
  message = "Review evidence filesystem handle could not be closed safely",
} = {}) {
  let closeInFlight = false;
  try {
    if (budget) {
      await closeSweepHandle(handle, budget, () => { closeInFlight = true; });
    } else {
      closeInFlight = true;
      await handle.close();
    }
    return true;
  } catch (closeError) {
    if (closeError && closeError.code === "ERR_DIR_CLOSED") return true;
    // A failed close is detached and observed, but can never replace the
    // primary mutation/quarantine/identity failure that led to cleanup.
    // A deadline that won after close started already has an observed close in
    // flight; only a close that never started, or rejected, needs a detached
    // retry.
    if (!closeInFlight || !isSweepControlFailure(closeError)) {
      detachSweepHandleClose(handle);
    }
    if (faultRoot) markEvidenceBacklog(faultRoot, { durabilityFault });
    if (primaryError) return false;
    if (closeError instanceof ReviewProviderError) throw closeError;
    throw typedError(
      "REVIEW_EVIDENCE_UNAVAILABLE",
      message,
      { retryable: true, cause: closeError },
    );
  }
}

async function runCleanupPreservingPrimary(primaryError, cleanup, {
  faultRoot = null,
  durabilityFault = true,
} = {}) {
  try {
    return await cleanup();
  } catch (cleanupError) {
    if (faultRoot) markEvidenceBacklog(faultRoot, { durabilityFault });
    if (primaryError) return false;
    throw cleanupError;
  }
}

async function readBoundedDirectoryEntries(directory, budget, kind) {
  let handle;
  let primaryError = null;
  const names = [];
  try {
    handle = await runSweepFsOperation(
      budget,
      () => fs.promises.opendir(directory),
      { disposeLate: detachSweepHandleClose },
    );
    while (true) {
      const entry = await runSweepFsOperation(budget, () => handle.read());
      if (!entry) break;
      consumeSweepResource(budget, kind);
      names.push(entry.name);
    }
  } catch (error) {
    primaryError = error;
    throw error;
  } finally {
    if (handle) {
      await closeEvidenceHandle(handle, {
        budget,
        primaryError,
        message: "Review evidence directory iterator could not be closed safely",
      });
    }
  }
  return names;
}

async function privateDirectoryIdentityAsync(directory, budget, expected = null) {
  requireSecureFilesystemPrimitives();
  const checked = path.resolve(String(directory || ""));
  if (!path.isAbsolute(checked) || checked === path.parse(checked).root) {
    throw typedError("REVIEW_EVIDENCE_INVALID", "Review evidence directory is invalid");
  }
  let lexical;
  let resolved;
  let handle;
  let primaryError = null;
  try {
    lexical = await runSweepFsOperation(budget, () => fs.promises.lstat(checked, { bigint: true }));
    resolved = await runSweepFsOperation(budget, () => fs.promises.realpath(checked));
    if (!lexical.isDirectory() || lexical.isSymbolicLink() || resolved !== checked
        || statNlink(lexical) < 1 || statMode(lexical) !== PRIVATE_DIRECTORY_MODE
        || (currentUid() !== null && statUid(lexical) !== currentUid())
        || (expected && !sameIdentity(lexical, expected))) {
      throw typedError("REVIEW_EVIDENCE_INVALID", "Review evidence directory is not private and owner-controlled");
    }
    handle = await runSweepFsOperation(
      budget,
      () => fs.promises.open(
        checked,
        fs.constants.O_RDONLY | fs.constants.O_DIRECTORY | fs.constants.O_NOFOLLOW,
      ),
      { disposeLate: detachSweepHandleClose },
    );
    const opened = await runSweepFsOperation(budget, () => handle.stat({ bigint: true }));
    if (!opened.isDirectory() || !sameIdentity(opened, lexical)
        || statMode(opened) !== PRIVATE_DIRECTORY_MODE
        || (currentUid() !== null && statUid(opened) !== currentUid())) {
      throw typedError("REVIEW_EVIDENCE_INVALID", "Review evidence directory changed during verification");
    }
    return directoryIdentityFromStats(opened);
  } catch (cause) {
    primaryError = cause instanceof ReviewProviderError ? cause : typedError(
      "REVIEW_EVIDENCE_UNAVAILABLE",
      "Review evidence directory is unavailable",
      {
        retryable: true,
        cause,
      },
    );
    throw primaryError;
  } finally {
    if (handle) {
      await closeEvidenceHandle(handle, {
        budget,
        primaryError,
        faultRoot: checked,
        message: "Review evidence directory handle could not be closed safely",
      });
    }
  }
}

async function activeEvidenceRunOwnsDirectoryAsync(directory, rootIdentity, budget) {
  const run = EVIDENCE_RUNS_BY_PATH.get(directory);
  const state = run && EVIDENCE_RUN_STATES.get(run);
  if (!state || state.closed || state.closing || state.tainted || state.path !== directory
      || !sameIdentity(state.rootIdentity, rootIdentity)) {
    return false;
  }
  try {
    await privateDirectoryIdentityAsync(directory, budget, state.identity);
    return true;
  } catch (error) {
    if (isSweepControlFailure(error)) throw error;
    return false;
  }
}

function sameStringArray(left, right) {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

function sameTombstoneSnapshot(left, right) {
  if (left === null || right === null) return left === right;
  if (!left || !right || !left.stable || !right.stable
      || !sameSweepRootIdentity(left.identity, right.identity)
      || left.invalid !== right.invalid
      || left.bytes !== right.bytes
      || left.entries !== right.entries
      || !sameStringArray(left.names, right.names)) {
    return false;
  }
  for (const name of left.names) {
    const leftRecord = left.records.get(name) || null;
    const rightRecord = right.records.get(name) || null;
    if (leftRecord === null || rightRecord === null) {
      if (leftRecord !== rightRecord) return false;
    } else if (!sameTombstoneRunSnapshot(leftRecord, rightRecord)) {
      return false;
    }
  }
  return true;
}

function sameTombstoneRunSnapshot(left, right) {
  if (!left || !right || !left.stable || !right.stable
      || left.invalid !== right.invalid
      || left.bytes !== right.bytes
      || !sameSweepRootIdentity(left.identity, right.identity)
      || !sameStringArray(left.names, right.names)) {
    return false;
  }
  for (const name of left.names) {
    const leftEntry = left.records.get(name) || null;
    const rightEntry = right.records.get(name) || null;
    if (leftEntry === null || rightEntry === null) {
      if (leftEntry !== rightEntry) return false;
    } else if (!sameSweepRootIdentity(leftEntry, rightEntry)) {
      return false;
    }
  }
  return true;
}

async function scanTombstoneRunSnapshot(directory, budget) {
  let start;
  try {
    start = await privateDirectoryIdentityAsync(directory, budget);
  } catch (error) {
    if (isSweepControlFailure(error)) throw error;
    return {
      stable: true,
      invalid: true,
      identity: null,
      names: [],
      records: new Map(),
      bytes: 0,
    };
  }
  const names = (await readBoundedDirectoryEntries(directory, budget, "file")).sort();
  const records = new Map();
  let bytes = 0;
  let invalid = false;
  for (const name of names) {
    try {
      const stat = await runSweepFsOperation(
        budget,
        () => fs.promises.lstat(path.join(directory, name), { bigint: true }),
      );
      const identity = directoryIdentityFromStats(stat);
      records.set(name, identity);
      const entryBytes = statSize(stat);
      if (!Number.isSafeInteger(entryBytes) || entryBytes < 0 || stat.isDirectory()) invalid = true;
      else bytes += entryBytes;
    } catch (error) {
      if (isSweepControlFailure(error)) throw error;
      invalid = true;
    }
  }
  let end = null;
  try {
    end = await privateDirectoryIdentityAsync(directory, budget, start);
  } catch (error) {
    if (isSweepControlFailure(error)) throw error;
    invalid = true;
  }
  return {
    stable: Boolean(end && sameSweepRootIdentity(start, end)),
    invalid: invalid || records.size !== names.length,
    identity: end || start,
    names,
    records,
    bytes,
  };
}

async function scanTombstoneDirectorySnapshot(root, rootIdentity, budget) {
  const directory = path.join(root, EVIDENCE_TOMBSTONE_DIRECTORY);
  let start;
  try {
    start = await privateDirectoryIdentityAsync(directory, budget);
  } catch (error) {
    if (isSweepControlFailure(error)) throw error;
    return {
      stable: true,
      invalid: true,
      identity: null,
      names: [],
      records: new Map(),
      bytes: 0,
      entries: 0,
    };
  }
  const names = (await readBoundedDirectoryEntries(directory, budget, "file")).sort();
  const records = new Map();
  let bytes = 0;
  let entries = 0;
  let invalid = false;
  for (const name of names) {
    if (!EVIDENCE_TOMBSTONE_PATTERN.test(name)) {
      invalid = true;
      continue;
    }
    try {
      const retained = await scanTombstoneRunSnapshot(path.join(directory, name), budget);
      records.set(name, retained);
      bytes += retained.bytes;
      entries += retained.names.length;
      if (!retained.stable || retained.invalid) invalid = true;
    } catch (error) {
      if (isSweepControlFailure(error)) throw error;
      invalid = true;
    }
  }
  let end = null;
  try {
    end = await privateDirectoryIdentityAsync(directory, budget, start);
    await privateDirectoryIdentityAsync(root, budget, rootIdentity);
  } catch (error) {
    if (isSweepControlFailure(error)) throw error;
    invalid = true;
  }
  return {
    stable: Boolean(end && sameSweepRootIdentity(start, end)),
    invalid: invalid || records.size !== names.length,
    identity: end || start,
    names,
    records,
    bytes,
    entries,
  };
}

async function scanEvidenceRootSnapshot(root, expectedRootIdentity, budget) {
  const start = await privateDirectoryIdentityAsync(root, budget, expectedRootIdentity);
  const names = (await readBoundedDirectoryEntries(root, budget, "directory")).sort();
  let backlog = 0;
  let tombstones = null;
  for (const name of names) {
    await privateDirectoryIdentityAsync(root, budget, expectedRootIdentity);
    if (name === EVIDENCE_TOMBSTONE_DIRECTORY) {
      tombstones = await scanTombstoneDirectorySnapshot(root, expectedRootIdentity, budget);
      if (!tombstones.stable || tombstones.invalid) backlog += 1;
      continue;
    }
    if (/^scope-[a-f0-9]{64}-[a-f0-9]{32}$/.test(name)) {
      const source = path.join(root, name);
      if (!await activeEvidenceRunOwnsDirectoryAsync(source, expectedRootIdentity, budget)) backlog += 1;
      continue;
    }
    // Legacy quarantine and every unrecognized entry are operator-owned
    // evidence. Readiness is strictly read-only and preserves them verbatim.
    backlog += 1;
  }
  const end = await privateDirectoryIdentityAsync(root, budget, expectedRootIdentity);
  return {
    stable: sameSweepRootIdentity(start, end),
    identity: end,
    names,
    backlog,
    tombstones,
  };
}

function sameEvidenceRootSnapshot(left, right) {
  return Boolean(left && right && left.stable && right.stable
    && sameSweepRootIdentity(left.identity, right.identity)
    && left.backlog === right.backlog
    && sameStringArray(left.names, right.names)
    && sameTombstoneSnapshot(left.tombstones, right.tombstones));
}

function isSweepControlFailure(error) {
  return error && [
    "REVIEW_EVIDENCE_PROBE_ABORTED",
    "REVIEW_EVIDENCE_SWEEP_DEADLINE",
    "REVIEW_EVIDENCE_SWEEP_BUDGET_EXHAUSTED",
    "REVIEW_EVIDENCE_SWEEP_UNSTABLE",
  ].includes(error.code);
}

async function certifyStableEvidenceRoot(root, {
  budget,
  force = false,
} = {}) {
  const checkedRoot = path.resolve(String(root || ""));
  if (!budget || typeof budget !== "object") {
    throw new TypeError("A shared Review evidence sweep budget is required");
  }
  const rootIdentity = await privateDirectoryIdentityAsync(checkedRoot, budget);
  const cachedIdentity = SWEPT_EVIDENCE_ROOTS.get(checkedRoot);
  let currentTombstoneIdentity = null;
  if (cachedIdentity && cachedIdentity.tombstoneIdentity) {
    try {
      currentTombstoneIdentity = await privateDirectoryIdentityAsync(
        path.join(checkedRoot, EVIDENCE_TOMBSTONE_DIRECTORY),
        budget,
        cachedIdentity.tombstoneIdentity,
      );
    } catch (error) {
      if (isSweepControlFailure(error)) throw error;
    }
  }
  if (!force && sameSweepRootIdentity(cachedIdentity, rootIdentity)
      && sameOptionalSweepIdentity(cachedIdentity.tombstoneIdentity || null, currentTombstoneIdentity)) {
    return {
      backlog: EVIDENCE_ORPHAN_BACKLOGS.get(checkedRoot) || 0,
      tombstoneRuns: 0,
      tombstoneEntries: 0,
      tombstoneBytes: 0,
      tombstoneRoots: currentTombstoneIdentity ? 1 : 0,
      tombstoneQuotaExceeded: false,
    };
  }
  SWEPT_EVIDENCE_ROOTS.delete(checkedRoot);
  try {
    for (let pass = 0; pass < 4; pass += 1) {
      const scanned = await scanEvidenceRootSnapshot(checkedRoot, rootIdentity, budget);
      const confirmed = await scanEvidenceRootSnapshot(checkedRoot, rootIdentity, budget);
      if (!sameEvidenceRootSnapshot(scanned, confirmed)) continue;
      assertSweepControl(budget);
      const tombstoneRuns = confirmed.tombstones ? confirmed.tombstones.names.length : 0;
      const tombstoneEntries = confirmed.tombstones ? confirmed.tombstones.entries : 0;
      const tombstoneBytes = confirmed.tombstones ? confirmed.tombstones.bytes : 0;
      const tombstoneQuotaExceeded = tombstoneRuns > budget.maxTombstones
        || tombstoneEntries > budget.maxTombstoneEntries
        || tombstoneBytes > budget.maxTombstoneBytes;
      const backlog = confirmed.backlog + (tombstoneQuotaExceeded ? 1 : 0);
      const effectiveBacklog = EVIDENCE_DURABILITY_FAULT_ROOTS.has(checkedRoot)
        ? Math.max(1, backlog)
        : backlog;
      cacheEvidenceSweep(
        checkedRoot,
        confirmed.identity,
        effectiveBacklog,
        confirmed.tombstones ? confirmed.tombstones.identity : null,
        EVIDENCE_SWEEP_CERTIFICATION,
      );
      return {
        backlog: effectiveBacklog,
        tombstoneRuns,
        tombstoneEntries,
        tombstoneBytes,
        tombstoneRoots: confirmed.tombstones ? 1 : 0,
        tombstoneQuotaExceeded,
      };
    }
    throw sweepControlError(
      "REVIEW_EVIDENCE_SWEEP_UNSTABLE",
      "Review evidence root did not remain stable across a bounded scan",
    );
  } catch (error) {
    EVIDENCE_ORPHAN_BACKLOGS.set(
      checkedRoot,
      Math.max(1, EVIDENCE_ORPHAN_BACKLOGS.get(checkedRoot) || 0),
    );
    SWEPT_EVIDENCE_ROOTS.delete(checkedRoot);
    throw error;
  }
}

function authorityDigest({ scopeId, ueBroker } = {}) {
  const scope = String(scopeId || "");
  if (scope) {
    if (Buffer.byteLength(scope, "utf8") > 1024) {
      throw typedError("REVIEW_EVIDENCE_INVALID", "Review evidence scope is too large");
    }
    return crypto.createHash("sha256")
      .update("simworld/review-evidence-authority/v1\0")
      .update(scope, "utf8")
      .digest("hex");
  }
  const broker = ueBroker && (typeof ueBroker === "object" || typeof ueBroker === "function")
    ? ueBroker : null;
  if (!broker) {
    throw typedError("REVIEW_EVIDENCE_INVALID", "A server-authoritative Review scope or broker is required");
  }
  let digest = BROKER_AUTHORITY_DIGESTS.get(broker);
  if (!digest) {
    digest = crypto.createHash("sha256")
      .update("simworld/review-evidence-broker/v1\0")
      .update(String(Number.isSafeInteger(Number(broker.port)) ? Number(broker.port) : "loopback"))
      .update("\0")
      .update(crypto.randomBytes(32))
      .digest("hex");
    BROKER_AUTHORITY_DIGESTS.set(broker, digest);
  }
  return digest;
}

function capacityError(resource) {
  return typedError(
    "REVIEW_EVIDENCE_CAPACITY_EXHAUSTED",
    `Review evidence ${resource} capacity is exhausted`,
    { retryable: true },
  );
}

function reviewEvidenceReservationForMode(mode, maxRounds) {
  if (mode === undefined || mode === null) {
    return Object.freeze({ enforced: false, files: 0, bytes: 0 });
  }
  const rounds = Number(maxRounds);
  if (!Number.isSafeInteger(rounds) || rounds < 1 || rounds > 100) {
    throw typedError("REVIEW_EVIDENCE_INVALID", "Review evidence max rounds is invalid");
  }
  if (mode === "text_loop") {
    return Object.freeze({
      enforced: true,
      files: rounds,
      bytes: rounds * MAX_SCREENSHOT_BYTES,
    });
  }
  if (mode === "visual_loop") {
    return Object.freeze({
      enforced: true,
      files: rounds * 8,
      bytes: rounds * 64 * 1024 * 1024,
    });
  }
  throw typedError("REVIEW_EVIDENCE_INVALID", "Review evidence mode is invalid");
}

function evidenceCapacitySnapshot() {
  return Object.freeze({
    active_runs: EVIDENCE_CAPACITY.activeRuns,
    active_admissions: EVIDENCE_CAPACITY.activeAdmissions,
    active_files: EVIDENCE_CAPACITY.activeFiles,
    active_bytes: EVIDENCE_CAPACITY.activeBytes,
    reserved_files: EVIDENCE_CAPACITY.reservedFiles,
    reserved_bytes: EVIDENCE_CAPACITY.reservedBytes,
    max_active_runs: MAX_ACTIVE_EVIDENCE_RUNS,
    max_active_files: MAX_ACTIVE_EVIDENCE_FILES,
    max_active_bytes: MAX_ACTIVE_EVIDENCE_BYTES,
    orphan_backlog: [...EVIDENCE_ORPHAN_BACKLOGS.values()].reduce((sum, value) => sum + value, 0),
    durability_fault_roots: EVIDENCE_DURABILITY_FAULT_ROOTS.size,
  });
}

function boundedSweepOption(name, value, maximum) {
  const checked = Number(value);
  if (!Number.isSafeInteger(checked) || checked < 1 || checked > maximum) {
    throw new TypeError(`${name} must be an integer between 1 and ${maximum}`);
  }
  return checked;
}

function createReviewEvidenceReadinessProbe({
  roots = DEFAULT_REVIEW_EVIDENCE_ROOTS,
  maxDirectories = 256,
  maxFiles = 2048,
  maxOperations = DEFAULT_EVIDENCE_SWEEP_MAX_OPERATIONS,
  deadlineMs = DEFAULT_EVIDENCE_SWEEP_DEADLINE_MS,
  batchOperations = DEFAULT_EVIDENCE_SWEEP_BATCH_OPERATIONS,
  maxTombstones = DEFAULT_MAX_EVIDENCE_TOMBSTONES,
  maxTombstoneEntries = DEFAULT_MAX_EVIDENCE_TOMBSTONE_ENTRIES,
  maxTombstoneBytes = DEFAULT_MAX_EVIDENCE_TOMBSTONE_BYTES,
} = {}) {
  if (!Array.isArray(roots) || roots.length < 1 || roots.length > 8) {
    throw new TypeError("Review evidence readiness roots must contain between one and eight paths");
  }
  const normalizedRoots = [...new Set(roots.map((root) => path.resolve(String(root || ""))))];
  if (normalizedRoots.some((root) => root === path.parse(root).root)) {
    throw new TypeError("Review evidence readiness roots must not be filesystem roots");
  }
  const sweepLimits = Object.freeze({
    maxDirectories: boundedSweepOption("maxDirectories", maxDirectories, 10_000),
    maxFiles: boundedSweepOption("maxFiles", maxFiles, 10_000),
    maxOperations: boundedSweepOption("maxOperations", maxOperations, 50_000),
    deadlineMs: boundedSweepOption("deadlineMs", deadlineMs, 5_000),
    batchOperations: boundedSweepOption("batchOperations", batchOperations, 256),
    maxTombstones: boundedSweepOption("maxTombstones", maxTombstones, 10_000),
    maxTombstoneEntries: boundedSweepOption(
      "maxTombstoneEntries",
      maxTombstoneEntries,
      100_000,
    ),
    maxTombstoneBytes: boundedSweepOption(
      "maxTombstoneBytes",
      maxTombstoneBytes,
      1024 * 1024 * 1024,
    ),
  });
  return async function probeReviewEvidence({ signal } = {}) {
    const budget = createSweepBudget(sweepLimits, signal);
    let rootsChecked = 0;
    let orphanBacklog = 0;
    let tombstoneRuns = 0;
    let tombstoneEntries = 0;
    let tombstoneBytes = 0;
    let tombstoneRoots = 0;
    let tombstoneQuotaExceeded = false;
    let sweepComplete = true;
    const causes = [];
    try {
      for (let rootIndex = 0; rootIndex < normalizedRoots.length; rootIndex += 1) {
        const root = normalizedRoots[rootIndex];
        try {
          assertSweepControl(budget);
          const result = await withSerializedEvidenceSweep(root, budget, () => (
            certifyStableEvidenceRoot(root, {
              budget,
              force: true,
            })
          ));
          orphanBacklog += result.backlog;
          tombstoneRuns += result.tombstoneRuns;
          tombstoneEntries += result.tombstoneEntries;
          tombstoneBytes += result.tombstoneBytes;
          tombstoneRoots += result.tombstoneRoots;
          tombstoneQuotaExceeded ||= result.tombstoneQuotaExceeded;
          rootsChecked += 1;
        } catch (error) {
          sweepComplete = false;
          markEvidenceBacklog(root);
          orphanBacklog += EVIDENCE_ORPHAN_BACKLOGS.get(root) || 1;
          const code = String(error && error.code || "");
          causes.push({
            code: /^[A-Z][A-Z0-9_]{1,63}$/.test(code) ? code : "REVIEW_EVIDENCE_PROBE_FAILED",
            message: code === "REVIEW_EVIDENCE_PLATFORM_UNSUPPORTED"
              ? "Review evidence requires unavailable secure filesystem primitives."
              : "Review evidence storage failed its private durable filesystem check.",
            retryable: error && error.retryable === true,
            dependency: "review_evidence",
          });
          if (isSweepControlFailure(error)) {
            for (const remainingRoot of normalizedRoots.slice(rootIndex + 1)) {
              markEvidenceBacklog(remainingRoot);
              orphanBacklog += EVIDENCE_ORPHAN_BACKLOGS.get(remainingRoot) || 1;
            }
            break;
          }
        }
      }
    } finally {
      disposeSweepBudget(budget);
    }
    if (tombstoneQuotaExceeded) {
      causes.push({
        code: "REVIEW_EVIDENCE_TOMBSTONE_QUOTA_EXCEEDED",
        message: "Review evidence tombstone retention exceeded its bounded operator-drain quota.",
        retryable: false,
        dependency: "review_evidence",
      });
    }
    if (orphanBacklog > 0) {
      causes.push({
        code: "REVIEW_EVIDENCE_ORPHAN_BACKLOG",
        message: "Review evidence storage has an unresolved cleanup backlog.",
        retryable: false,
        dependency: "review_evidence",
      });
    }
    const durabilityFaults = normalizedRoots.filter((root) => EVIDENCE_DURABILITY_FAULT_ROOTS.has(root)).length;
    if (durabilityFaults > 0) {
      causes.push({
        code: "REVIEW_EVIDENCE_DURABILITY_FAULT",
        message: "Review evidence cleanup durability could not be confirmed; restart verification is required.",
        retryable: false,
        dependency: "review_evidence",
      });
    }
    return {
      status: causes.length === 0 ? "ready" : "not_ready",
      revision: {
        schema: "simworld-review-evidence-readiness/v1",
        roots_checked: rootsChecked,
        orphan_backlog: orphanBacklog,
        durability_fault_roots: durabilityFaults,
        sweep_complete: sweepComplete && rootsChecked === normalizedRoots.length,
        sweep_operations: budget.operations,
        sweep_directories: budget.directories,
        sweep_files: budget.files,
        tombstone_roots: tombstoneRoots,
        tombstone_runs: tombstoneRuns,
        tombstone_entries: tombstoneEntries,
        tombstone_bytes: tombstoneBytes,
        tombstone_quota_exceeded: tombstoneQuotaExceeded,
      },
      causes,
    };
  };
}

async function runtimeDirectoryIdentity(directory, expected = null, budget = null) {
  if (budget) return privateDirectoryIdentityAsync(directory, budget, expected);
  requireSecureFilesystemPrimitives();
  const checked = path.resolve(String(directory || ""));
  if (!path.isAbsolute(checked) || checked === path.parse(checked).root) {
    throw typedError("REVIEW_EVIDENCE_INVALID", "Review evidence directory is invalid");
  }
  let handle;
  let primaryError = null;
  try {
    const lexical = await fs.promises.lstat(checked, { bigint: true });
    const resolved = await fs.promises.realpath(checked);
    if (!lexical.isDirectory() || lexical.isSymbolicLink() || resolved !== checked
        || statNlink(lexical) < 1 || statMode(lexical) !== PRIVATE_DIRECTORY_MODE
        || (currentUid() !== null && statUid(lexical) !== currentUid())
        || (expected && !sameIdentity(lexical, expected))) {
      throw typedError("REVIEW_EVIDENCE_INVALID", "Review evidence directory is not private and owner-controlled");
    }
    handle = await fs.promises.open(
      checked,
      fs.constants.O_RDONLY | fs.constants.O_DIRECTORY | fs.constants.O_NOFOLLOW,
    );
    const opened = await handle.stat({ bigint: true });
    if (!opened.isDirectory() || !sameIdentity(opened, lexical)
        || statMode(opened) !== PRIVATE_DIRECTORY_MODE
        || (currentUid() !== null && statUid(opened) !== currentUid())) {
      throw typedError("REVIEW_EVIDENCE_INVALID", "Review evidence directory changed during verification");
    }
    return directoryIdentityFromStats(opened);
  } catch (cause) {
    primaryError = cause instanceof ReviewProviderError ? cause : typedError(
      "REVIEW_EVIDENCE_UNAVAILABLE",
      "Review evidence directory is unavailable",
      { retryable: true, cause },
    );
    throw primaryError;
  } finally {
    if (handle) {
      await closeEvidenceHandle(handle, {
        primaryError,
        faultRoot: checked,
        message: "Review evidence directory handle could not be closed safely",
      });
    }
  }
}

async function runtimeTombstoneIdentity(root, budget = null) {
  const directory = path.join(root, EVIDENCE_TOMBSTONE_DIRECTORY);
  try {
    if (budget) {
      await runSweepFsOperation(budget, () => fs.promises.lstat(directory, { bigint: true }));
    } else {
      await fs.promises.lstat(directory, { bigint: true });
    }
  } catch (error) {
    if (error && error.code === "ENOENT") return null;
    throw error;
  }
  return runtimeDirectoryIdentity(directory, null, budget);
}

async function syncRuntimeDirectory(directory, evidenceRoot, budget = null) {
  let handle;
  let primaryError = null;
  try {
    const openDirectory = () => fs.promises.open(
      directory,
      fs.constants.O_RDONLY | fs.constants.O_DIRECTORY | fs.constants.O_NOFOLLOW,
    );
    handle = budget
      ? await runSweepFsOperation(budget, openDirectory, { disposeLate: detachSweepHandleClose })
      : await openDirectory();
    if (budget) {
      await runTrackedRootMutation(evidenceRoot, budget, () => handle.sync(), {
        operationName: "directory_fsync",
      });
    }
    else await handle.sync();
  } catch (error) {
    primaryError = error;
    markEvidenceBacklog(evidenceRoot, { durabilityFault: true });
    throw error;
  } finally {
    if (handle) {
      await closeEvidenceHandle(handle, {
        budget,
        primaryError,
        faultRoot: evidenceRoot,
        durabilityFault: true,
        message: "Review evidence durability handle could not be closed safely",
      });
    }
  }
}

function runtimeAuthorityChildPath(authority, name) {
  if (!authority || !authority.handle || !Number.isSafeInteger(authority.handle.fd)
      || typeof name !== "string" || name === "." || name === ".."
      || !/^[A-Za-z0-9.][A-Za-z0-9._-]{0,127}$/.test(name)) {
    throw typedError("REVIEW_EVIDENCE_INVALID", "Review evidence cleanup entry is invalid");
  }
  return `/proc/self/fd/${authority.handle.fd}/${name}`;
}

function assertPrivateRuntimeDirectoryStat(value, expected, message) {
  if (!value || !value.isDirectory() || value.isSymbolicLink()
      || statNlink(value) < 1 || statMode(value) !== PRIVATE_DIRECTORY_MODE
      || (currentUid() !== null && statUid(value) !== currentUid())
      || (expected && !sameIdentity(value, expected))) {
    throw typedError("REVIEW_EVIDENCE_INVALID", message);
  }
}

async function openRuntimeRootAuthority(root, expected, budget) {
  requireSecureFilesystemPrimitives();
  const checked = path.resolve(String(root || ""));
  let handle = null;
  let primaryError = null;
  try {
    const lexical = await runSweepFsOperation(
      budget,
      () => fs.promises.lstat(checked, { bigint: true }),
    );
    const resolved = await runSweepFsOperation(budget, () => fs.promises.realpath(checked));
    assertPrivateRuntimeDirectoryStat(
      lexical,
      expected,
      "Review evidence cleanup root is not private and owner-controlled",
    );
    if (resolved !== checked) {
      throw typedError("REVIEW_EVIDENCE_INVALID", "Review evidence cleanup root is not canonical");
    }
    handle = await runSweepFsOperation(
      budget,
      () => fs.promises.open(
        checked,
        fs.constants.O_RDONLY | fs.constants.O_DIRECTORY | fs.constants.O_NOFOLLOW,
      ),
      { disposeLate: detachSweepHandleClose },
    );
    const opened = await runSweepFsOperation(budget, () => handle.stat({ bigint: true }));
    assertPrivateRuntimeDirectoryStat(
      opened,
      lexical,
      "Review evidence cleanup root changed while opening its authority",
    );
    const identity = directoryIdentityFromStats(opened);
    return { path: checked, handle, identity };
  } catch (cause) {
    primaryError = cause instanceof ReviewProviderError ? cause : typedError(
      "REVIEW_EVIDENCE_UNAVAILABLE",
      "Review evidence cleanup root is unavailable",
      { retryable: true, cause },
    );
    if (handle) {
      await closeEvidenceHandle(handle, {
        budget,
        primaryError,
        faultRoot: checked,
        message: "Review evidence cleanup root handle could not be closed safely",
      });
    }
    throw primaryError;
  }
}

async function revalidateRuntimeRootAuthority(authority, budget, {
  allowMetadataChange = false,
} = {}) {
  const lexical = await runSweepFsOperation(
    budget,
    () => fs.promises.lstat(authority.path, { bigint: true }),
  );
  const resolved = await runSweepFsOperation(budget, () => fs.promises.realpath(authority.path));
  assertPrivateRuntimeDirectoryStat(
    lexical,
    authority.identity,
    "Review evidence cleanup root identity changed",
  );
  if (resolved !== authority.path) {
    throw typedError("REVIEW_EVIDENCE_INVALID", "Review evidence cleanup root path changed");
  }
  const opened = await runSweepFsOperation(
    budget,
    () => authority.handle.stat({ bigint: true }),
  );
  assertPrivateRuntimeDirectoryStat(
    opened,
    lexical,
    "Review evidence cleanup root authority changed",
  );
  const identity = directoryIdentityFromStats(opened);
  if (!allowMetadataChange && !sameSweepRootIdentity(identity, authority.identity)) {
    throw typedError("REVIEW_EVIDENCE_INVALID", "Review evidence cleanup root metadata changed");
  }
  authority.identity = identity;
  return identity;
}

async function openRuntimeChildAuthority(parentAuthority, name, expected, budget, message) {
  const checked = runtimeAuthorityChildPath(parentAuthority, name);
  let handle = null;
  let primaryError = null;
  try {
    const lexical = await runSweepFsOperation(
      budget,
      () => fs.promises.lstat(checked, { bigint: true }),
    );
    assertPrivateRuntimeDirectoryStat(lexical, expected, message);
    handle = await runSweepFsOperation(
      budget,
      () => fs.promises.open(
        checked,
        fs.constants.O_RDONLY | fs.constants.O_DIRECTORY | fs.constants.O_NOFOLLOW,
      ),
      { disposeLate: detachSweepHandleClose },
    );
    const opened = await runSweepFsOperation(budget, () => handle.stat({ bigint: true }));
    assertPrivateRuntimeDirectoryStat(opened, lexical, message);
    return {
      path: checked,
      name,
      parent: parentAuthority,
      handle,
      identity: directoryIdentityFromStats(opened),
    };
  } catch (cause) {
    primaryError = cause instanceof ReviewProviderError ? cause : typedError(
      "REVIEW_EVIDENCE_UNAVAILABLE",
      message,
      { retryable: true, cause },
    );
    if (handle) {
      await closeEvidenceHandle(handle, {
        budget,
        primaryError,
        faultRoot: parentAuthority.path,
        message: "Review evidence cleanup child handle could not be closed safely",
      });
    }
    throw primaryError;
  }
}

async function revalidateRuntimeChildAuthority(parentAuthority, authority, budget, {
  allowMetadataChange = false,
} = {}) {
  const checked = runtimeAuthorityChildPath(parentAuthority, authority.name);
  const lexical = await runSweepFsOperation(
    budget,
    () => fs.promises.lstat(checked, { bigint: true }),
  );
  assertPrivateRuntimeDirectoryStat(
    lexical,
    authority.identity,
    "Review evidence cleanup child identity changed",
  );
  const opened = await runSweepFsOperation(
    budget,
    () => authority.handle.stat({ bigint: true }),
  );
  assertPrivateRuntimeDirectoryStat(
    opened,
    lexical,
    "Review evidence cleanup child authority changed",
  );
  const identity = directoryIdentityFromStats(opened);
  if (!allowMetadataChange && !sameSweepRootIdentity(identity, authority.identity)) {
    throw typedError("REVIEW_EVIDENCE_INVALID", "Review evidence cleanup child metadata changed");
  }
  authority.identity = identity;
  return identity;
}

async function syncRuntimeAuthority(authority, evidenceRoot, budget) {
  try {
    await runTrackedRootMutation(evidenceRoot, budget, () => authority.handle.sync(), {
      operationName: "directory_fsync",
    });
  } catch (error) {
    markEvidenceBacklog(evidenceRoot, { durabilityFault: true });
    throw error;
  }
}

async function closeRuntimeAuthorities(authorities, {
  budget,
  primaryError,
  faultRoot,
  durabilityFault,
}) {
  let cleanupError = null;
  for (const authority of authorities) {
    if (!authority || !authority.handle) continue;
    try {
      const closed = await closeEvidenceHandle(authority.handle, {
        budget,
        primaryError: primaryError || cleanupError,
        faultRoot,
        durabilityFault,
        message: "Review evidence cleanup authority could not be closed safely",
      });
      if (!closed && !primaryError && !cleanupError) {
        cleanupError = typedError(
          "REVIEW_EVIDENCE_UNAVAILABLE",
          "Review evidence cleanup authority could not be closed safely",
          { retryable: true },
        );
      }
    } catch (error) {
      if (!cleanupError) cleanupError = error;
    }
  }
  return cleanupError;
}

async function requireCertifiedEvidenceRoot(root, budget = null) {
  const checked = path.resolve(String(root || ""));
  const identity = await runtimeDirectoryIdentity(checked, null, budget);
  const cached = SWEPT_EVIDENCE_ROOTS.get(checked);
  let tombstoneIdentity = null;
  try {
    tombstoneIdentity = await runtimeTombstoneIdentity(checked, budget);
  } catch (_error) {
    markEvidenceBacklog(checked);
    throw typedError(
      "REVIEW_EVIDENCE_ORPHAN_BACKLOG",
      "Review evidence tombstone storage is not privately controlled",
      { retryable: false },
    );
  }
  if (!sameSweepRootIdentity(cached, identity)
      || !sameOptionalSweepIdentity(cached && cached.tombstoneIdentity || null, tombstoneIdentity)
      || (EVIDENCE_ORPHAN_BACKLOGS.get(checked) || 0) > 0) {
    markEvidenceBacklog(checked);
    throw typedError(
      "REVIEW_EVIDENCE_ORPHAN_BACKLOG",
      "Review evidence root has not been certified by a complete stable readiness scan",
      { retryable: true },
    );
  }
  // The cached root/tombstone directory identities are only an admission
  // guard. A full stable scan is still required before every mutation so
  // retained child metadata cannot change behind an otherwise-stable parent.
  const certification = await certifyEvidenceRootAfterMutation(checked, { budget });
  if (certification.backlog > 0) {
    throw typedError(
      "REVIEW_EVIDENCE_ORPHAN_BACKLOG",
      "Review evidence root failed its stable pre-mutation certification",
      { retryable: false },
    );
  }
  return {
    path: checked,
    identity: await runtimeDirectoryIdentity(checked, identity, budget),
    tombstoneIdentity: await runtimeTombstoneIdentity(checked, budget),
  };
}

function internalSweepLimits() {
  return Object.freeze({
    maxDirectories: 2_048,
    maxFiles: 4_096,
    maxOperations: 50_000,
    deadlineMs: 5_000,
    batchOperations: DEFAULT_EVIDENCE_SWEEP_BATCH_OPERATIONS,
    maxTombstones: DEFAULT_MAX_EVIDENCE_TOMBSTONES,
    maxTombstoneEntries: DEFAULT_MAX_EVIDENCE_TOMBSTONE_ENTRIES,
    maxTombstoneBytes: DEFAULT_MAX_EVIDENCE_TOMBSTONE_BYTES,
  });
}

function createRuntimeEvidenceBudget({
  signal = null,
  deadlineMs = DEFAULT_EVIDENCE_OPERATION_DEADLINE_MS,
} = {}) {
  const limits = {
    ...internalSweepLimits(),
    deadlineMs: boundedSweepOption("deadlineMs", deadlineMs, 30_000),
  };
  return createSweepBudget(Object.freeze(limits), signal);
}

async function certifyEvidenceRootAfterMutation(root, { budget: suppliedBudget = null } = {}) {
  const budget = suppliedBudget || createRuntimeEvidenceBudget();
  try {
    return await certifyStableEvidenceRoot(root, { budget, force: true });
  } finally {
    if (!suppliedBudget) disposeSweepBudget(budget);
  }
}

function invalidateEvidenceStateHandles(run, state) {
  state.closing = true;
  if (state.retentionTimer) {
    clearTimeout(state.retentionTimer);
    state.retentionTimer = null;
  }
  EVIDENCE_RUNS_BY_PATH.delete(state.path);
  for (const record of state.files.values()) {
    EVIDENCE_FILES.delete(record.path);
    EVIDENCE_HANDLES.delete(record.handle);
  }
}

function releaseEvidenceStateCapacity(state) {
  if (state.capacityReleased) return;
  state.capacityReleased = true;
  EVIDENCE_CAPACITY.activeRuns = Math.max(0, EVIDENCE_CAPACITY.activeRuns - 1);
  EVIDENCE_CAPACITY.reservedFiles = Math.max(
    0,
    EVIDENCE_CAPACITY.reservedFiles - (state.reservedFilesRemaining || 0),
  );
  EVIDENCE_CAPACITY.reservedBytes = Math.max(
    0,
    EVIDENCE_CAPACITY.reservedBytes - (state.reservedBytesRemaining || 0),
  );
  state.reservedFilesRemaining = 0;
  state.reservedBytesRemaining = 0;
  if (state.admission) {
    const admissionState = EVIDENCE_ADMISSION_STATES.get(state.admission);
    if (admissionState) {
      admissionState.capacityReserved = false;
      admissionState.reservedFilesRemaining = 0;
      admissionState.reservedBytesRemaining = 0;
      admissionState.status = "released";
    }
  }
  for (const record of state.files.values()) {
    EVIDENCE_CAPACITY.activeFiles = Math.max(0, EVIDENCE_CAPACITY.activeFiles - 1);
    if (record.sealed) {
      EVIDENCE_CAPACITY.activeBytes = Math.max(0, EVIDENCE_CAPACITY.activeBytes - record.sealed.size);
    }
  }
  state.files.clear();
}

async function tombstoneEvidenceState(state, budget = null) {
  let mutated = false;
  let runMutated = false;
  let durable = false;
  let primaryError = null;
  let rootAuthority = null;
  let runAuthority = null;
  let tombstoneAuthority = null;
  try {
    if (budget) assertSweepControl(budget);
    rootAuthority = await openRuntimeRootAuthority(state.root, state.rootIdentity, budget);
    if (path.dirname(state.path) !== state.root
        || !/^scope-[a-f0-9]{64}-[a-f0-9]{32}$/.test(path.basename(state.path))) {
      throw typedError("REVIEW_EVIDENCE_INVALID", "Review evidence cleanup run path is invalid");
    }

    // Keep the pathname read for compatibility with the public run identity,
    // then immediately prove that it still belongs to the already-open root.
    // An ABA replacement can preserve the run inode, but cannot preserve the
    // root authority opened above.
    const lexicalRun = await runSweepFsOperation(
      budget,
      () => fs.promises.lstat(state.path, { bigint: true }),
    );
    assertPrivateRuntimeDirectoryStat(
      lexicalRun,
      state.identity,
      "Review evidence cleanup run identity changed",
    );
    await revalidateRuntimeRootAuthority(rootAuthority, budget);
    runAuthority = await openRuntimeChildAuthority(
      rootAuthority,
      path.basename(state.path),
      state.identity,
      budget,
      "Review evidence cleanup run is unavailable or unsafe",
    );
    await revalidateRuntimeRootAuthority(rootAuthority, budget);

    const tombstonePath = runtimeAuthorityChildPath(
      rootAuthority,
      EVIDENCE_TOMBSTONE_DIRECTORY,
    );
    try {
      await runTrackedRootMutation(
        state.root,
        budget,
        () => fs.promises.mkdir(tombstonePath, { mode: PRIVATE_DIRECTORY_MODE }),
        { operationName: "tombstone_root_mkdir" },
      );
      mutated = true;
      markEvidenceBacklog(state.root);
      await syncRuntimeAuthority(rootAuthority, state.root, budget);
      await revalidateRuntimeRootAuthority(rootAuthority, budget, { allowMetadataChange: true });
    } catch (error) {
      if (!error || error.code !== "EEXIST") throw error;
    }
    tombstoneAuthority = await openRuntimeChildAuthority(
      rootAuthority,
      EVIDENCE_TOMBSTONE_DIRECTORY,
      null,
      budget,
      "Review evidence tombstone root is unavailable or unsafe",
    );
    await revalidateRuntimeRootAuthority(rootAuthority, budget);
    await revalidateRuntimeChildAuthority(rootAuthority, tombstoneAuthority, budget);

    for (let attempt = 0; attempt < 8; attempt += 1) {
      if (budget) assertSweepControl(budget);
      const destinationName = `tombstone-${crypto.randomBytes(32).toString("hex")}`;
      const source = runtimeAuthorityChildPath(rootAuthority, runAuthority.name);
      const destination = runtimeAuthorityChildPath(tombstoneAuthority, destinationName);
      try {
        await runTrackedRootMutation(
          state.root,
          budget,
          () => fs.promises.rename(source, destination),
          { operationName: "evidence_run_rename" },
        );
        mutated = true;
        runMutated = true;
        markEvidenceBacklog(state.root);

        // Rename modifies both parent directories. Rebind their metadata to
        // the same open inodes, then durably sync and require a stable second
        // pass before releasing any logical capacity.
        await revalidateRuntimeRootAuthority(rootAuthority, budget, { allowMetadataChange: true });
        await revalidateRuntimeChildAuthority(
          rootAuthority,
          tombstoneAuthority,
          budget,
          { allowMetadataChange: true },
        );
        const movedAuthority = await openRuntimeChildAuthority(
          tombstoneAuthority,
          destinationName,
          runAuthority.identity,
          budget,
          "Review evidence moved run identity changed",
        );
        let movedCloseError = null;
        let moved;
        try {
          const stillOpen = await runSweepFsOperation(
            budget,
            () => runAuthority.handle.stat({ bigint: true }),
          );
          assertPrivateRuntimeDirectoryStat(
            stillOpen,
            movedAuthority.identity,
            "Review evidence moved run no longer matches its source authority",
          );
          moved = movedAuthority.identity;
        } finally {
          movedCloseError = await closeRuntimeAuthorities([movedAuthority], {
            budget,
            primaryError: null,
            faultRoot: state.root,
            durabilityFault: true,
          });
        }
        if (movedCloseError) throw movedCloseError;

        await syncRuntimeAuthority(tombstoneAuthority, state.root, budget);
        await syncRuntimeAuthority(rootAuthority, state.root, budget);
        await revalidateRuntimeRootAuthority(rootAuthority, budget);
        await revalidateRuntimeChildAuthority(rootAuthority, tombstoneAuthority, budget);
        const confirmedMoved = await openRuntimeChildAuthority(
          tombstoneAuthority,
          destinationName,
          moved,
          budget,
          "Review evidence moved run changed after durability sync",
        );
        const confirmedCloseError = await closeRuntimeAuthorities([confirmedMoved], {
          budget,
          primaryError: null,
          faultRoot: state.root,
          durabilityFault: true,
        });
        if (confirmedCloseError) throw confirmedCloseError;
        state.tombstonePath = path.join(
          state.root,
          EVIDENCE_TOMBSTONE_DIRECTORY,
          destinationName,
        );
        state.tombstoneIdentity = moved;
        durable = true;
        break;
      } catch (error) {
        if (!runMutated && error && ["EEXIST", "ENOTEMPTY"].includes(error.code)) continue;
        throw error;
      }
    }
    if (!durable) {
      throw typedError("REVIEW_EVIDENCE_UNAVAILABLE", "A unique Review evidence tombstone could not be allocated", {
        retryable: true,
      });
    }
  } catch (error) {
    primaryError = error;
    durable = false;
  } finally {
    const cleanupError = await closeRuntimeAuthorities(
      [runAuthority, tombstoneAuthority, rootAuthority],
      {
        budget,
        primaryError,
        faultRoot: state.root,
        durabilityFault: mutated,
      },
    );
    if (cleanupError && !primaryError) {
      primaryError = cleanupError;
      durable = false;
    }
  }
  if (!durable || primaryError) {
    markEvidenceBacklog(state.root, { durabilityFault: true });
    return false;
  }
  return true;
}

async function closeEvidenceStateWithinRootLock(run, state, budget = null) {
  invalidateEvidenceStateHandles(run, state);
  const durable = await tombstoneEvidenceState(state, budget);
  state.closed = true;
  state.closing = false;
  if (!durable) return false;
  releaseEvidenceStateCapacity(state);
  try {
    await certifyEvidenceRootAfterMutation(state.root, { budget });
  } catch (_error) {
    markEvidenceBacklog(state.root);
  }
  return true;
}

async function acquireReviewEvidenceAdmission({
  root = SCREENSHOT_DIR,
  scopeId,
  ueBroker,
  signal,
  mode,
  maxRounds,
  deadlineMs = DEFAULT_EVIDENCE_OPERATION_DEADLINE_MS,
} = {}) {
  if (signal && signal.aborted) {
    throw typedError("REVIEW_ABORTED", "Review evidence admission was aborted before allocation");
  }
  const managedRoot = path.resolve(String(root || ""));
  const digest = authorityDigest({ scopeId, ueBroker });
  const reservation = reviewEvidenceReservationForMode(mode, maxRounds);
  const budget = createRuntimeEvidenceBudget({ signal, deadlineMs });
  try {
    return await withSerializedEvidenceSweep(managedRoot, budget, async () => {
      assertSweepControl(budget);
      const certified = await requireCertifiedEvidenceRoot(managedRoot, budget);
      assertSweepControl(budget);
      // The slot is reserved only after this admission owns the serialized
      // root operation. Timed-out queued callers never consume capacity.
      if (EVIDENCE_CAPACITY.activeRuns >= MAX_ACTIVE_EVIDENCE_RUNS) {
        throw capacityError("run");
      }
      if (EVIDENCE_CAPACITY.activeFiles + EVIDENCE_CAPACITY.reservedFiles + reservation.files
          > MAX_ACTIVE_EVIDENCE_FILES) {
        throw capacityError("file");
      }
      if (EVIDENCE_CAPACITY.activeBytes + EVIDENCE_CAPACITY.reservedBytes + reservation.bytes
          > MAX_ACTIVE_EVIDENCE_BYTES) {
        throw capacityError("byte");
      }
      EVIDENCE_CAPACITY.activeRuns += 1;
      EVIDENCE_CAPACITY.activeAdmissions += 1;
      EVIDENCE_CAPACITY.reservedFiles += reservation.files;
      EVIDENCE_CAPACITY.reservedBytes += reservation.bytes;
      const admission = Object.freeze({});
      EVIDENCE_ADMISSION_STATES.set(admission, {
        root: managedRoot,
        rootIdentity: certified.identity,
        authorityDigest: digest,
        signal: signal || null,
        run: null,
        openPromise: null,
        finalizePromise: null,
        capacityReserved: true,
        reservationEnforced: reservation.enforced,
        reservedFilesRemaining: reservation.files,
        reservedBytesRemaining: reservation.bytes,
        status: "admitted",
      });
      return admission;
    });
  } finally {
    disposeSweepBudget(budget);
  }
}

function requireEvidenceAdmission(admission) {
  const state = EVIDENCE_ADMISSION_STATES.get(admission);
  if (!state || state.status === "released" || state.status === "finalizing") {
    throw typedError("REVIEW_EVIDENCE_INVALID", "Review evidence admission is not active");
  }
  return state;
}

function openReviewEvidenceAdmission(admission, {
  signal,
  deadlineMs = DEFAULT_EVIDENCE_OPERATION_DEADLINE_MS,
} = {}) {
  const admissionState = requireEvidenceAdmission(admission);
  if (admissionState.run) return Promise.resolve(admissionState.run);
  if (admissionState.openPromise) return admissionState.openPromise;
  const operationSignal = signal || admissionState.signal;
  if (operationSignal && operationSignal.aborted) {
    return Promise.reject(typedError(
      "REVIEW_ABORTED",
      "Review evidence capture was aborted before run allocation",
    ));
  }
  const budget = createRuntimeEvidenceBudget({ signal: operationSignal, deadlineMs });
  admissionState.openPromise = withSerializedEvidenceSweep(
    admissionState.root,
    budget,
    async () => {
      assertSweepControl(budget);
      const currentAdmission = requireEvidenceAdmission(admission);
      if (currentAdmission.run) return currentAdmission.run;
      const certified = await requireCertifiedEvidenceRoot(currentAdmission.root, budget);
      assertSweepControl(budget);
      let directory = null;
      let state = null;
      let run = null;
      try {
        for (let attempt = 0; attempt < 8; attempt += 1) {
          assertSweepControl(budget);
          const nonce = crypto.randomBytes(16).toString("hex");
          const candidate = path.join(
            currentAdmission.root,
            `scope-${currentAdmission.authorityDigest}-${nonce}`,
          );
          try {
            // Mutating operations are never raced against the deadline. Once
            // submitted, they are awaited and either committed durably or
            // rolled into whole-directory cleanup before this lock releases.
            await runTrackedRootMutation(
              currentAdmission.root,
              budget,
              () => fs.promises.mkdir(candidate, { mode: PRIVATE_DIRECTORY_MODE }),
              { operationName: "evidence_run_mkdir" },
            );
            directory = candidate;
            markEvidenceBacklog(currentAdmission.root);
            break;
          } catch (error) {
            if (!error || error.code !== "EEXIST") throw error;
          }
        }
        if (!directory) {
          throw typedError(
            "REVIEW_EVIDENCE_UNAVAILABLE",
            "A unique Review evidence directory could not be allocated",
            { retryable: true },
          );
        }
        const identity = await runtimeDirectoryIdentity(directory, null, budget);
        run = Object.freeze({ path: directory });
        state = {
          root: currentAdmission.root,
          rootIdentity: certified.identity,
          path: directory,
          identity,
          authorityDigest: currentAdmission.authorityDigest,
          admission,
          files: new Map(),
          retentionTimer: null,
          expiresAt: null,
          cleanupPromise: null,
          capacityReleased: false,
          reservationEnforced: currentAdmission.reservationEnforced,
          reservedFilesRemaining: currentAdmission.reservedFilesRemaining,
          reservedBytesRemaining: currentAdmission.reservedBytesRemaining,
          tainted: false,
          closing: false,
          closed: false,
        };
        EVIDENCE_RUN_STATES.set(run, state);
        EVIDENCE_RUNS_BY_PATH.set(directory, run);
        currentAdmission.run = run;
        currentAdmission.status = "converted";
        EVIDENCE_CAPACITY.activeAdmissions = Math.max(
          0,
          EVIDENCE_CAPACITY.activeAdmissions - 1,
        );
        await syncRuntimeDirectory(currentAdmission.root, currentAdmission.root, budget);
        state.rootIdentity = await runtimeDirectoryIdentity(
          currentAdmission.root,
          certified.identity,
          budget,
        );
        const certification = await certifyEvidenceRootAfterMutation(
          currentAdmission.root,
          { budget },
        );
        if (certification.backlog > 0) {
          const backlogError = typedError(
            "REVIEW_EVIDENCE_ORPHAN_BACKLOG",
            "Review evidence root changed while allocating a paid Review run",
            { retryable: false },
          );
          await runCleanupPreservingPrimary(
            backlogError,
            () => closeEvidenceStateWithinRootLock(run, state, budget),
            { faultRoot: currentAdmission.root },
          );
          throw backlogError;
        }
        return run;
      } catch (cause) {
        if (state && !state.closed && !state.closing) {
          await runCleanupPreservingPrimary(
            cause,
            () => closeEvidenceStateWithinRootLock(run, state, budget),
            { faultRoot: currentAdmission.root },
          );
        } else if (directory) {
          poisonEvidenceRoot(
            currentAdmission.root,
            "evidence_run_mkdir_postcommit",
            Promise.resolve(),
          );
        }
        if (cause instanceof ReviewProviderError) throw cause;
        throw typedError(
          "REVIEW_EVIDENCE_UNAVAILABLE",
          "Private Review evidence directory could not be created",
          { retryable: true, cause },
        );
      }
    },
  ).finally(() => disposeSweepBudget(budget));
  admissionState.openPromise.catch(() => {});
  return admissionState.openPromise;
}

async function createPrivateEvidenceRun({
  root = SCREENSHOT_DIR,
  scopeId,
  ueBroker,
  signal,
  deadlineMs = DEFAULT_EVIDENCE_OPERATION_DEADLINE_MS,
} = {}) {
  const admission = await acquireReviewEvidenceAdmission({
    root,
    scopeId,
    ueBroker,
    signal,
    deadlineMs,
  });
  try {
    return await openReviewEvidenceAdmission(admission, { signal, deadlineMs });
  } catch (error) {
    const admissionState = EVIDENCE_ADMISSION_STATES.get(admission);
    await runCleanupPreservingPrimary(
      error,
      () => finalizeReviewEvidenceAdmission(admission, { retain: false, deadlineMs }),
      { faultRoot: admissionState && admissionState.root },
    );
    throw error;
  }
}

function requireEvidenceRunState(run) {
  const state = EVIDENCE_RUN_STATES.get(run);
  if (!state || state.closed || state.closing || state.tainted
      || EVIDENCE_RUNS_BY_PATH.get(state.path) !== run) {
    throw typedError("REVIEW_EVIDENCE_INVALID", "Review evidence run is not active");
  }
  return state;
}

async function requireEvidenceRun(run, budget = null) {
  const state = requireEvidenceRunState(run);
  await runtimeDirectoryIdentity(state.path, state.identity, budget);
  if (requireEvidenceRunState(run) !== state) {
    throw typedError("REVIEW_EVIDENCE_INVALID", "Review evidence run changed during verification");
  }
  return state;
}

function privateFileStat(stat, expected, { allowEmpty = false } = {}) {
  const uid = currentUid();
  if (!stat || !stat.isFile() || stat.isSymbolicLink()
      || statNlink(stat) !== 1 || statMode(stat) !== PRIVATE_FILE_MODE
      || (uid !== null && statUid(stat) !== uid)
      || (expected && !sameIdentity(stat, expected))
      || (!allowEmpty && (statSize(stat) < 4 || statSize(stat) > MAX_SCREENSHOT_BYTES))
      || (allowEmpty && statSize(stat) > MAX_SCREENSHOT_BYTES)) {
    throw typedError("REVIEW_EVIDENCE_INVALID", "Review screenshot file identity or permissions are invalid");
  }
}

async function reserveEvidenceFile(run, label = "capture", {
  signal,
  deadlineMs = DEFAULT_EVIDENCE_OPERATION_DEADLINE_MS,
} = {}) {
  const initialState = requireEvidenceRunState(run);
  if (signal && signal.aborted) {
    throw typedError("REVIEW_ABORTED", "Review evidence file allocation was aborted");
  }
  const safeLabel = String(label || "capture");
  if (!/^[A-Za-z0-9][A-Za-z0-9.-]{0,63}$/.test(safeLabel)) {
    throw typedError("REVIEW_EVIDENCE_INVALID", "Review screenshot label is invalid");
  }
  const budget = createRuntimeEvidenceBudget({ signal, deadlineMs });
  try {
    return await withSerializedEvidenceSweep(initialState.root, budget, async () => {
      const state = await requireEvidenceRun(run, budget);
      assertSweepControl(budget);
      let consumedAdmissionReservation = false;
      if (state.reservationEnforced) {
        if (state.reservedFilesRemaining < 1) throw capacityError("file");
        state.reservedFilesRemaining -= 1;
        EVIDENCE_CAPACITY.reservedFiles = Math.max(0, EVIDENCE_CAPACITY.reservedFiles - 1);
        const admissionState = state.admission && EVIDENCE_ADMISSION_STATES.get(state.admission);
        if (admissionState) admissionState.reservedFilesRemaining = state.reservedFilesRemaining;
        consumedAdmissionReservation = true;
      } else if (EVIDENCE_CAPACITY.activeFiles + EVIDENCE_CAPACITY.reservedFiles
          >= MAX_ACTIVE_EVIDENCE_FILES) {
        throw capacityError("file");
      }
      // Convert one admission reservation (or dynamically claim one legacy
      // slot) immediately before the first filesystem mutation.
      EVIDENCE_CAPACITY.activeFiles += 1;
      let fileCapacityReserved = true;
      const filename = `${safeLabel}-${crypto.randomBytes(16).toString("hex")}.png`;
      const handleToken = `evidence-${crypto.randomBytes(24).toString("hex")}`;
      const filepath = path.join(state.path, filename);
      let fileHandle;
      let createdIdentity = null;
      let record = null;
      let authoritative = false;
      let primaryError = null;
      try {
        assertSweepControl(budget);
        fileHandle = await runTrackedRootMutation(
          state.root,
          budget,
          () => fs.promises.open(
            filepath,
            fs.constants.O_RDWR | fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_NOFOLLOW,
            PRIVATE_FILE_MODE,
          ),
          {
            operationName: "evidence_file_create",
            disposeLate: detachSweepHandleClose,
          },
        );
        const opened = await runSweepFsOperation(
          budget,
          () => fileHandle.stat({ bigint: true }),
        );
        createdIdentity = Object.freeze({
          dev: bigintIdentityPart(opened.dev),
          ino: bigintIdentityPart(opened.ino),
          uid: statUid(opened),
        });
        privateFileStat(opened, null, { allowEmpty: true });
        const lexical = await runSweepFsOperation(
          budget,
          () => fs.promises.lstat(filepath, { bigint: true }),
        );
        privateFileStat(lexical, opened, { allowEmpty: true });
        await runtimeDirectoryIdentity(state.path, state.identity, budget);
        record = {
          path: filepath,
          identity: createdIdentity,
          handle: handleToken,
          sealed: null,
          reference: null,
        };
        // The record enters capacity accounting before durability work. It is
        // not serving-authoritative until file and directory fsync complete.
        state.files.set(filepath, record);
        await runTrackedRootMutation(state.root, budget, () => fileHandle.sync(), {
          operationName: "evidence_file_fsync",
        });
        await syncRuntimeDirectory(state.path, state.root, budget);
        await requireEvidenceRun(run, budget);
        const closingHandle = fileHandle;
        try {
          await closeEvidenceHandle(closingHandle, {
            budget,
            faultRoot: state.root,
            durabilityFault: true,
            message: "Review screenshot allocation handle could not be closed safely",
          });
        } finally {
          fileHandle = null;
        }
        EVIDENCE_FILES.set(filepath, { run, record });
        EVIDENCE_HANDLES.set(record.handle, { run, record });
        authoritative = true;
        fileCapacityReserved = false;
        return filepath;
      } catch (cause) {
        primaryError = cause;
        if (fileHandle && !createdIdentity) {
          try {
            const recovered = await fileHandle.stat({ bigint: true });
            if (recovered.isFile() && !recovered.isSymbolicLink()
                && (currentUid() === null || statUid(recovered) === currentUid())) {
              createdIdentity = Object.freeze({
                dev: bigintIdentityPart(recovered.dev),
                ino: bigintIdentityPart(recovered.ino),
                uid: statUid(recovered),
              });
            }
          } catch (_identityError) {}
        }
        if (authoritative && record) {
          EVIDENCE_FILES.delete(filepath);
          EVIDENCE_HANDLES.delete(record.handle);
        }
        if (createdIdentity) {
          if (!record) {
            record = {
              path: filepath,
              identity: createdIdentity,
              handle: handleToken,
              sealed: null,
              reference: null,
            };
            state.files.set(filepath, record);
          }
          // There is no inode-conditional pathname unlink. Preserve the whole
          // run and its capacity until serialized tombstoning is durable.
          state.tainted = true;
          markEvidenceBacklog(state.root);
          fileCapacityReserved = false;
        }
        if (fileCapacityReserved) {
          EVIDENCE_CAPACITY.activeFiles = Math.max(0, EVIDENCE_CAPACITY.activeFiles - 1);
          if (consumedAdmissionReservation) {
            state.reservedFilesRemaining += 1;
            EVIDENCE_CAPACITY.reservedFiles += 1;
            const admissionState = state.admission && EVIDENCE_ADMISSION_STATES.get(state.admission);
            if (admissionState) admissionState.reservedFilesRemaining = state.reservedFilesRemaining;
          }
        }
        primaryError = cause instanceof ReviewProviderError ? cause : typedError(
          "REVIEW_EVIDENCE_UNAVAILABLE",
          "Review screenshot file could not be reserved",
          { retryable: true, cause },
        );
        throw primaryError;
      } finally {
        if (fileHandle) {
          await closeEvidenceHandle(fileHandle, {
            budget,
            primaryError,
            faultRoot: state.root,
            durabilityFault: true,
            message: "Review screenshot allocation handle could not be closed safely",
          });
        }
      }
    });
  } finally {
    disposeSweepBudget(budget);
  }
}

function expireEvidenceEntry(entry) {
  const state = EVIDENCE_RUN_STATES.get(entry && entry.run);
  if (!state || !Number.isFinite(state.expiresAt) || Date.now() < state.expiresAt) return false;
  cleanupPrivateEvidenceRun(entry.run).catch(() => {
    if (state.root) markEvidenceBacklog(state.root, { durabilityFault: true });
  });
  return true;
}

async function managedEvidenceEntry(filepath, budget = null) {
  const supplied = String(filepath || "");
  const checked = path.resolve(supplied);
  const entry = supplied === checked ? EVIDENCE_FILES.get(checked) : null;
  if (!entry || expireEvidenceEntry(entry)) {
    throw typedError("REVIEW_EVIDENCE_INVALID", "Review screenshot is not owned by the active evidence run");
  }
  const state = await requireEvidenceRun(entry.run, budget);
  if (entry.record.path !== checked || state.files.get(checked) !== entry.record
      || path.dirname(checked) !== state.path) {
    throw typedError("REVIEW_EVIDENCE_INVALID", "Review screenshot ownership is invalid");
  }
  return { ...entry, state };
}

async function managedEvidenceStat(filepath, {
  allowEmpty = false,
  signal,
  deadlineMs = DEFAULT_EVIDENCE_IO_DEADLINE_MS,
} = {}) {
  const budget = createRuntimeEvidenceBudget({ signal, deadlineMs });
  let handle;
  let entry = null;
  let primaryError = null;
  try {
    entry = await managedEvidenceEntry(filepath, budget);
    const lexical = await runSweepFsOperation(
      budget,
      () => fs.promises.lstat(entry.record.path, { bigint: true }),
    );
    privateFileStat(lexical, entry.record.identity, { allowEmpty });
    handle = await runSweepFsOperation(
      budget,
      () => fs.promises.open(entry.record.path, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW),
      { disposeLate: detachSweepHandleClose },
    );
    const opened = await runSweepFsOperation(budget, () => handle.stat({ bigint: true }));
    privateFileStat(opened, entry.record.identity, { allowEmpty });
    if (!sameIdentity(opened, lexical) || statSize(opened) !== statSize(lexical)) {
      throw typedError("REVIEW_EVIDENCE_INVALID", "Review screenshot changed during identity verification");
    }
    return Object.freeze({
      dev: bigintIdentityPart(opened.dev),
      ino: bigintIdentityPart(opened.ino),
      uid: statUid(opened),
      mode: statMode(opened),
      size: statSize(opened),
    });
  } catch (cause) {
    primaryError = cause instanceof ReviewProviderError ? cause : typedError(
      "REVIEW_EVIDENCE_UNAVAILABLE",
      "Review screenshot is unavailable",
      { retryable: true, cause },
    );
    throw primaryError;
  } finally {
    try {
      if (handle) {
        await closeEvidenceHandle(handle, {
          budget,
          primaryError,
          faultRoot: entry && entry.state && entry.state.root,
          message: "Review screenshot stat handle could not be closed safely",
        });
      }
    } finally {
      disposeSweepBudget(budget);
    }
  }
}

async function readManagedScreenshot(filepath, {
  seal = false,
  signal,
  deadlineMs = DEFAULT_EVIDENCE_IO_DEADLINE_MS,
} = {}) {
  const budget = createRuntimeEvidenceBudget({ signal, deadlineMs });
  let handle;
  let entry = null;
  let primaryError = null;
  try {
    entry = await managedEvidenceEntry(filepath, budget);
    const lexicalBefore = await runSweepFsOperation(
      budget,
      () => fs.promises.lstat(entry.record.path, { bigint: true }),
    );
    privateFileStat(lexicalBefore, entry.record.identity);
    handle = await runSweepFsOperation(
      budget,
      () => fs.promises.open(
        entry.record.path,
        (seal ? fs.constants.O_RDWR : fs.constants.O_RDONLY) | fs.constants.O_NOFOLLOW,
      ),
      { disposeLate: detachSweepHandleClose },
    );
    if (seal) await runSweepFsOperation(budget, () => handle.sync());
    const before = await runSweepFsOperation(budget, () => handle.stat({ bigint: true }));
    privateFileStat(before, entry.record.identity);
    if (!sameIdentity(before, lexicalBefore) || statSize(before) !== statSize(lexicalBefore)) {
      throw typedError("REVIEW_EVIDENCE_INVALID", "Review screenshot changed before reading");
    }
    const expectedSize = statSize(before);
    const digestState = crypto.createHash("sha256");
    const data = Buffer.allocUnsafe(expectedSize);
    let position = 0;
    while (position < expectedSize) {
      assertSweepControl(budget);
      const length = Math.min(EVIDENCE_IO_CHUNK_BYTES, expectedSize - position);
      const chunk = data.subarray(position, position + length);
      const result = await runSweepFsOperation(
        budget,
        () => handle.read(chunk, 0, length, position),
      );
      if (!result || result.bytesRead < 1) {
        throw typedError("REVIEW_EVIDENCE_INVALID", "Review screenshot ended before its verified size");
      }
      const bytes = result.bytesRead === chunk.length ? chunk : chunk.subarray(0, result.bytesRead);
      digestState.update(bytes);
      position += result.bytesRead;
      await checkpointSweepBudget(budget);
    }
    const after = await runSweepFsOperation(budget, () => handle.stat({ bigint: true }));
    const lexicalAfter = await runSweepFsOperation(
      budget,
      () => fs.promises.lstat(entry.record.path, { bigint: true }),
    );
    privateFileStat(after, entry.record.identity);
    privateFileStat(lexicalAfter, entry.record.identity);
    if (!sameIdentity(before, after) || !sameIdentity(after, lexicalAfter)
        || statSize(before) !== statSize(after) || statSize(after) !== statSize(lexicalAfter)
        || data.length !== statSize(after)) {
      throw typedError("REVIEW_EVIDENCE_INVALID", "Review screenshot changed while reading");
    }
    const isJpeg = data.length >= 3 && data[0] === 0xff && data[1] === 0xd8 && data[2] === 0xff;
    const isPng = data.length >= 8
      && data.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]));
    if (!isJpeg && !isPng) {
      throw typedError("REVIEW_EVIDENCE_INVALID", "Review screenshot is not a supported PNG or JPEG file");
    }
    const digest = digestState.digest("hex");
    if (entry.record.sealed
        && (entry.record.sealed.digest !== digest || entry.record.sealed.size !== data.length)) {
      throw typedError("REVIEW_EVIDENCE_INVALID", "Review screenshot content changed after capture");
    }
    const confirmed = await managedEvidenceEntry(filepath, budget);
    if (confirmed.record !== entry.record) {
      throw typedError("REVIEW_EVIDENCE_INVALID", "Review screenshot authority changed while reading");
    }
    if (seal && !entry.record.sealed) {
      if (entry.state.reservationEnforced) {
        if (data.length > entry.state.reservedBytesRemaining) throw capacityError("byte");
        entry.state.reservedBytesRemaining -= data.length;
        EVIDENCE_CAPACITY.reservedBytes = Math.max(
          0,
          EVIDENCE_CAPACITY.reservedBytes - data.length,
        );
        const admissionState = entry.state.admission
          && EVIDENCE_ADMISSION_STATES.get(entry.state.admission);
        if (admissionState) admissionState.reservedBytesRemaining = entry.state.reservedBytesRemaining;
      } else if (EVIDENCE_CAPACITY.activeBytes + EVIDENCE_CAPACITY.reservedBytes + data.length
          > MAX_ACTIVE_EVIDENCE_BYTES) {
        throw capacityError("byte");
      }
      entry.record.sealed = Object.freeze({ digest, size: data.length });
      entry.record.reference = Object.freeze({
        schema: "simworld-review-evidence-ref/v1",
        scope_digest: entry.state.authorityDigest,
        evidence_id: `sha256:${digest}`,
        handle: entry.record.handle,
      });
      EVIDENCE_CAPACITY.activeBytes += data.length;
    }
    return {
      filepath: entry.record.path,
      data,
      mediaType: isJpeg ? "image/jpeg" : "image/png",
      evidenceId: `sha256:${digest}`,
      reference: entry.record.reference,
    };
  } catch (cause) {
    primaryError = cause instanceof ReviewProviderError ? cause : typedError(
      "REVIEW_EVIDENCE_UNAVAILABLE",
      "Review screenshot is unavailable",
      { retryable: true, cause },
    );
    throw primaryError;
  } finally {
    try {
      if (handle) {
        await closeEvidenceHandle(handle, {
          budget,
          primaryError,
          faultRoot: entry && entry.state && entry.state.root,
          durabilityFault: Boolean(seal),
          message: "Review screenshot read handle could not be closed safely",
        });
      }
    } finally {
      disposeSweepBudget(budget);
    }
  }
}

function sealManagedScreenshot(filepath, options = {}) {
  return readManagedScreenshot(filepath, { ...options, seal: true });
}

function cleanupPrivateEvidenceRun(run, {
  deadlineMs = DEFAULT_EVIDENCE_OPERATION_DEADLINE_MS,
} = {}) {
  const state = EVIDENCE_RUN_STATES.get(run);
  if (!state) return Promise.resolve(false);
  if (state.cleanupPromise) return state.cleanupPromise;
  if (state.closed) return Promise.resolve(state.cleanupResult === true);

  // Invalidate serving authority synchronously before the first await. The
  // logical capacity remains reserved until rename and both parent fsyncs are
  // complete.
  invalidateEvidenceStateHandles(run, state);
  const budget = createRuntimeEvidenceBudget({ deadlineMs });
  state.cleanupPromise = withSerializedEvidenceSweep(state.root, budget, async () => {
    const durable = await tombstoneEvidenceState(state, budget);
    state.closed = true;
    state.closing = false;
    state.cleanupResult = durable;
    if (!durable) return false;
    releaseEvidenceStateCapacity(state);
    try {
      await certifyEvidenceRootAfterMutation(state.root, { budget });
    } catch (_error) {
      markEvidenceBacklog(state.root);
    }
    return true;
  }).catch((_error) => {
    state.closed = true;
    state.closing = false;
    state.cleanupResult = false;
    markEvidenceBacklog(state.root, { durabilityFault: true });
    return false;
  }).finally(() => {
    disposeSweepBudget(budget);
  });
  return state.cleanupPromise;
}

async function cleanupEvidenceForPath(filepath) {
  const supplied = String(filepath || "");
  const checked = path.resolve(supplied);
  const entry = supplied === checked ? EVIDENCE_FILES.get(checked) : null;
  return entry ? cleanupPrivateEvidenceRun(entry.run) : false;
}

async function retainPrivateEvidenceRun(run, {
  retentionMs = DEFAULT_EVIDENCE_RETENTION_MS,
  signal,
  deadlineMs = DEFAULT_EVIDENCE_OPERATION_DEADLINE_MS,
} = {}) {
  const budget = createRuntimeEvidenceBudget({ signal, deadlineMs });
  let state;
  try {
    state = await requireEvidenceRun(run, budget);
  } finally {
    disposeSweepBudget(budget);
  }
  const duration = Number(retentionMs);
  if (!Number.isSafeInteger(duration) || duration < 0 || duration > MAX_EVIDENCE_RETENTION_MS) {
    throw typedError("REVIEW_EVIDENCE_INVALID", "Review evidence retention is invalid");
  }
  if (state.retentionTimer) clearTimeout(state.retentionTimer);
  if (duration === 0) {
    state.expiresAt = Date.now();
    await cleanupPrivateEvidenceRun(run);
    return 0;
  }
  state.expiresAt = Date.now() + duration;
  const timer = setTimeout(() => {
    if (state.retentionTimer !== timer) return;
    state.retentionTimer = null;
    // The timer enqueues asynchronous whole-directory tombstoning only. Every
    // rejection is consumed and latched; the timer never runs sync cleanup.
    cleanupPrivateEvidenceRun(run).then((durable) => {
      if (!durable) markEvidenceBacklog(state.root, { durabilityFault: true });
    }).catch(() => {
      markEvidenceBacklog(state.root, { durabilityFault: true });
    });
  }, duration);
  state.retentionTimer = timer;
  if (typeof state.retentionTimer.unref === "function") state.retentionTimer.unref();
  return duration;
}

async function retainEvidenceForPath(filepath, options) {
  const budget = createRuntimeEvidenceBudget(options || {});
  let entry;
  try {
    entry = await managedEvidenceEntry(filepath, budget);
  } finally {
    disposeSweepBudget(budget);
  }
  return retainPrivateEvidenceRun(entry.run, options);
}

async function managedScreenshotReference(filepath, options) {
  const evidence = await readManagedScreenshot(filepath, options);
  if (!evidence.reference) {
    throw typedError("REVIEW_EVIDENCE_INVALID", "Review screenshot is not sealed");
  }
  return evidence.reference;
}

async function resolveManagedScreenshotReference({
  scopeId,
  evidenceId,
  handle,
  signal,
  deadlineMs = DEFAULT_EVIDENCE_IO_DEADLINE_MS,
} = {}) {
  const normalizedEvidenceId = String(evidenceId || "").toLowerCase();
  const normalizedHandle = String(handle || "");
  if (!/^sha256:[a-f0-9]{64}$/.test(normalizedEvidenceId)
      || !/^evidence-[a-f0-9]{48}$/.test(normalizedHandle)) {
    throw typedError("REVIEW_EVIDENCE_INVALID", "Review evidence reference is invalid");
  }
  const entry = EVIDENCE_HANDLES.get(normalizedHandle);
  if (!entry || expireEvidenceEntry(entry)) {
    throw typedError("REVIEW_EVIDENCE_UNAVAILABLE", "Review evidence reference has expired");
  }
  const state = requireEvidenceRunState(entry.run);
  const expectedScopeDigest = authorityDigest({ scopeId });
  const reference = entry.record.reference;
  if (!reference || state.authorityDigest !== expectedScopeDigest
      || reference.scope_digest !== expectedScopeDigest
      || reference.evidence_id !== normalizedEvidenceId
      || reference.handle !== normalizedHandle) {
    throw typedError("REVIEW_EVIDENCE_INVALID", "Review evidence reference does not belong to this scope");
  }
  const evidence = await readManagedScreenshot(entry.record.path, { signal, deadlineMs });
  if (evidence.evidenceId !== normalizedEvidenceId) {
    throw typedError("REVIEW_EVIDENCE_INVALID", "Review evidence reference content does not match");
  }
  return Object.freeze({
    data: Buffer.from(evidence.data),
    mediaType: evidence.mediaType,
    size: evidence.data.length,
    evidenceId: evidence.evidenceId,
    reference,
  });
}

function finalizeReviewEvidenceAdmission(admission, {
  retain = false,
  retentionMs = DEFAULT_EVIDENCE_RETENTION_MS,
  deadlineMs = DEFAULT_EVIDENCE_OPERATION_DEADLINE_MS,
} = {}) {
  const admissionState = EVIDENCE_ADMISSION_STATES.get(admission);
  if (!admissionState) {
    return Promise.reject(typedError(
      "REVIEW_EVIDENCE_INVALID",
      "Review evidence admission is unknown",
    ));
  }
  if (admissionState.finalizePromise) return admissionState.finalizePromise;
  if (admissionState.status === "released") return Promise.resolve(true);
  const checkedDeadlineMs = boundedSweepOption("deadlineMs", deadlineMs, 30_000);
  admissionState.status = "finalizing";
  admissionState.finalizePromise = (async () => {
    const deadlineAt = Date.now() + checkedDeadlineMs;
    const remainingDeadlineMs = () => Math.max(1, deadlineAt - Date.now());
    if (admissionState.openPromise) {
      try { await admissionState.openPromise; } catch (_openError) {}
    }
    const run = admissionState.run;
    const runState = run && EVIDENCE_RUN_STATES.get(run);
    if (run && (!runState || runState.closed)) {
      // Conversion crossed the first namespace mutation, but a prior cleanup
      // could not prove the directory rename durable. Never misclassify this
      // as an empty admission and release its conservative global capacity.
      const safelyReleased = admissionState.capacityReserved === false;
      admissionState.status = safelyReleased ? "released" : "quarantined";
      return safelyReleased;
    }
    if (runState && !runState.closed) {
      const hasSealedEvidence = [...runState.files.values()].some(
        (record) => Boolean(record && record.sealed && record.reference),
      );
      if (retain && hasSealedEvidence && !runState.closing && !runState.tainted) {
        try {
          await retainPrivateEvidenceRun(run, {
            retentionMs,
            deadlineMs: remainingDeadlineMs(),
          });
          admissionState.status = "retained";
          return true;
        } catch (_retainError) {
          // A failed retain is never allowed to leave serving authority live.
          // Use the remainder of the same finalization deadline for cleanup.
          const durable = await cleanupPrivateEvidenceRun(run, {
            deadlineMs: remainingDeadlineMs(),
          });
          admissionState.status = durable ? "released" : "quarantined";
          return false;
        }
      }
      const durable = await cleanupPrivateEvidenceRun(run, {
        deadlineMs: remainingDeadlineMs(),
      });
      admissionState.status = durable ? "released" : "quarantined";
      return durable;
    }
    if (!admissionState.capacityReserved) {
      admissionState.status = "released";
      return true;
    }
    // No first write occurred. Verify the admitted root without creating a
    // tombstone, then release the provisional capacity exactly once.
    const budget = createRuntimeEvidenceBudget({ deadlineMs: remainingDeadlineMs() });
    let verified = false;
    try {
      await withSerializedEvidenceSweep(admissionState.root, budget, async () => {
        await runtimeDirectoryIdentity(
          admissionState.root,
          admissionState.rootIdentity,
          budget,
        );
        await requireCertifiedEvidenceRoot(admissionState.root, budget);
        verified = true;
      });
    } catch (_error) {
      markEvidenceBacklog(admissionState.root, { durabilityFault: true });
    } finally {
      disposeSweepBudget(budget);
      if (admissionState.capacityReserved) {
        admissionState.capacityReserved = false;
        EVIDENCE_CAPACITY.activeRuns = Math.max(0, EVIDENCE_CAPACITY.activeRuns - 1);
        EVIDENCE_CAPACITY.activeAdmissions = Math.max(
          0,
          EVIDENCE_CAPACITY.activeAdmissions - 1,
        );
        EVIDENCE_CAPACITY.reservedFiles = Math.max(
          0,
          EVIDENCE_CAPACITY.reservedFiles - admissionState.reservedFilesRemaining,
        );
        EVIDENCE_CAPACITY.reservedBytes = Math.max(
          0,
          EVIDENCE_CAPACITY.reservedBytes - admissionState.reservedBytesRemaining,
        );
        admissionState.reservedFilesRemaining = 0;
        admissionState.reservedBytesRemaining = 0;
      }
      admissionState.status = verified ? "released" : "quarantined";
    }
    return verified;
  })();
  admissionState.finalizePromise.catch(() => {});
  return admissionState.finalizePromise;
}

function isManagedEvidencePath(filepath) {
  const supplied = String(filepath || "");
  const checked = path.resolve(supplied);
  if (supplied !== checked) return false;
  if (EVIDENCE_FILES.has(checked)) return true;
  for (const directory of EVIDENCE_RUNS_BY_PATH.keys()) {
    if (checked === directory || checked.startsWith(`${directory}${path.sep}`)) return true;
  }
  return checked.split(path.sep).some((part) => /^scope-[a-f0-9]{64}-[a-f0-9]{32}$/.test(part));
}

function throwIfCaptureAborted(signal) {
  if (signal && signal.aborted) {
    throw typedError("REVIEW_ABORTED", "Review evidence capture was aborted");
  }
}

function awaitBrokerResult(pending, signal) {
  if (!signal) return Promise.resolve(pending);
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (fn, value) => {
      if (settled) return;
      settled = true;
      signal.removeEventListener("abort", onAbort);
      fn(value);
    };
    const onAbort = () => finish(reject, typedError("REVIEW_ABORTED", "Review evidence capture was aborted"));
    if (signal.aborted) { onAbort(); return; }
    signal.addEventListener("abort", onAbort, { once: true });
    Promise.resolve(pending).then(
      (value) => finish(resolve, value),
      (error) => finish(reject, error),
    );
  });
}

async function brokerCommand(type, params, { ueBroker, signal, timeoutMs, queueDeadlineMs }) {
  throwIfCaptureAborted(signal);
  const broker = ueBroker || getUeBroker();
  if (!broker || typeof broker.send !== "function") {
    throw typedError("REVIEW_EVIDENCE_UNAVAILABLE", "Shared UE broker is unavailable", { retryable: true });
  }
  let result;
  try {
    const brokerOptions = { timeoutMs, queueDeadlineMs };
    if (signal) brokerOptions.signal = signal;
    result = await awaitBrokerResult(
      broker.send(type, params || {}, brokerOptions),
      signal,
    );
  } catch (cause) {
    if (cause instanceof ReviewProviderError && cause.code === "REVIEW_ABORTED") throw cause;
    throw typedError("REVIEW_EVIDENCE_UNAVAILABLE", `UE evidence command '${type}' failed`, {
      retryable: true,
      cause,
    });
  }
  throwIfCaptureAborted(signal);
  return result;
}

async function takeScreenshot(options = {}) {
  const { signal, scopeId, evidenceAdmission = null } = options || {};
  // Cancellation must win before admission conversion, directory creation, or
  // file capacity reservation. An already-aborted capture leaves no tombstone.
  throwIfCaptureAborted(signal);
  const ueBroker = options && options.ueBroker || getUeBroker();
  const ownsAdmission = !evidenceAdmission;
  const admission = evidenceAdmission || await acquireReviewEvidenceAdmission({
    root: options && options.evidenceRoot || SCREENSHOT_DIR,
    scopeId,
    ueBroker,
    signal,
  });
  let evidenceRun = null;
  let filepath = null;
  let completed = false;
  let primaryError = null;
  try {
    evidenceRun = await openReviewEvidenceAdmission(admission, { signal });
    throwIfCaptureAborted(signal);
    filepath = await reserveEvidenceFile(evidenceRun, "critic", { signal });
    await brokerCommand("take_screenshot", { filepath }, {
      ueBroker,
      signal,
      timeoutMs: 30000,
      queueDeadlineMs: 45000,
    });
    await sealManagedScreenshot(filepath, { signal });
    completed = true;
    return filepath;
  } catch (error) {
    primaryError = error;
    throw error;
  } finally {
    if (!completed && ownsAdmission) {
      const admissionState = EVIDENCE_ADMISSION_STATES.get(admission);
      await runCleanupPreservingPrimary(
        primaryError,
        () => finalizeReviewEvidenceAdmission(admission, { retain: false }),
        { faultRoot: admissionState && admissionState.root },
      );
    }
  }
}

async function getActors(options = {}) {
  const { signal, ueBroker } = options || {};
  const result = await brokerCommand("get_actors_in_level", {}, {
    ueBroker,
    signal,
    timeoutMs: 15000,
    queueDeadlineMs: 30000,
  });
  if (!result || typeof result !== "object") {
    throw typedError("REVIEW_EVIDENCE_INVALID", "UE actor snapshot was malformed");
  }
  return result;
}

// Legacy parser remains exported for callers that render stored historical
// verdicts. New provider output never passes through this permissive parser.
function parseCriticFeedback(text) {
  const value = String(text || "");
  const statusMatch = value.match(/(?:\*\*Status\*\*|\bStatus\b)\s*:\s*([A-Za-z_]+)/i);
  let status = "NEEDS_IMPROVEMENT";
  if (statusMatch) {
    const candidate = statusMatch[1].toUpperCase();
    if (["PASS", "NEEDS_IMPROVEMENT", "FAIL"].includes(candidate)) status = candidate;
  }
  const labelPattern = /(?:\*\*(Status|Issues|Suggestions)\*\*|\b(Status|Issues|Suggestions)\b)\s*:\s*/gi;
  const positions = [];
  let match;
  while ((match = labelPattern.exec(value)) !== null) {
    positions.push({
      label: (match[1] || match[2] || "").toLowerCase(),
      start: match.index,
      contentStart: match.index + match[0].length,
    });
  }
  const sectionBody = (label) => {
    const index = positions.findIndex((entry) => entry.label === label.toLowerCase());
    if (index < 0) return "";
    const end = index + 1 < positions.length ? positions[index + 1].start : value.length;
    return value.slice(positions[index].contentStart, end);
  };
  const toBullets = (section) => String(section || "")
    .split(/\n/)
    .map((line) => line.replace(/^[\s\-•*\d.)]+/, "").trim())
    .filter((item) => item && !/^\(?none\)?$/i.test(item));
  return {
    status,
    issues: toBullets(sectionBody("Issues")),
    suggestions: toBullets(sectionBody("Suggestions")),
  };
}

function screenshotEvidence(filepath, options) {
  return readManagedScreenshot(filepath, options);
}

function actorContext(actors) {
  let serialized;
  try {
    serialized = JSON.stringify(actors || {}, null, 2);
  } catch (cause) {
    throw typedError("REVIEW_EVIDENCE_INVALID", "Actor evidence is not serializable", { cause });
  }
  if (Buffer.byteLength(serialized) > MAX_ACTOR_CONTEXT_BYTES) {
    throw typedError("REVIEW_EVIDENCE_INVALID", "Actor evidence exceeds the review size limit");
  }
  return serialized;
}

function countActors(actors) {
  const list = actors && actors.result && actors.result.actors;
  return Array.isArray(list) ? list.length : 0;
}

// Loop-facing contract. `runner` remains a compatibility alias for provider;
// unknown providers and unversioned models fail before a child is spawned.
async function runCritic({
  originalPrompt,
  focus,
  screenshot,
  actors,
  model,
  maxBudgetUsd,
  timeoutMs = 120000,
  provider,
  runner,
  signal,
} = {}) {
  if (signal && signal.aborted) throw typedError("REVIEW_ABORTED", "Review was aborted before evidence loading");
  const evidence = await screenshotEvidence(screenshot, { signal });
  const actorsJson = actorContext(actors);
  const prompt = [
    "Evaluate the supplied screenshot and actor list for this SimWorld Studio scene.",
    originalPrompt ? `Original scene request: ${JSON.stringify(String(originalPrompt))}` : "",
    focus ? `Review focus: ${String(focus)}` : "",
    "Current actors in the scene:",
    actorsJson,
  ].filter(Boolean).join("\n\n");
  const verdict = await reviewScene({
    provider: provider || runner,
    model,
    maxBudgetUsd,
    timeoutMs,
    signal,
    systemPrompt: CRITIC_SYSTEM_PROMPT,
    prompt,
    images: [{ mediaType: evidence.mediaType, data: evidence.data }],
    evidenceIds: [evidence.evidenceId],
  });
  return {
    ...verdict,
    screenshot: evidence.filepath,
    screenshotRef: evidence.reference,
    actorsCount: countActors(actors),
  };
}

async function critique({
  originalPrompt,
  focus,
  model,
  maxBudgetUsd,
  timeoutMs,
  provider,
  runner,
  signal,
  ueBroker,
  scopeId,
  evidenceRoot,
  evidenceAdmission,
} = {}) {
  let screenshot = null;
  let completed = false;
  let primaryError = null;
  try {
    screenshot = await takeScreenshot({
      signal,
      ueBroker,
      scopeId,
      evidenceRoot,
      evidenceAdmission,
    });
    const actors = await getActors({ signal, ueBroker });
    const verdict = await runCritic({
      originalPrompt,
      focus,
      screenshot,
      actors,
      model,
      maxBudgetUsd,
      timeoutMs,
      provider,
      runner,
      signal,
    });
    if (!evidenceAdmission) await retainEvidenceForPath(screenshot);
    completed = true;
    return verdict;
  } catch (error) {
    primaryError = error;
    throw error;
  } finally {
    if (screenshot && !completed && !evidenceAdmission) {
      const entry = EVIDENCE_FILES.get(path.resolve(String(screenshot)));
      await runCleanupPreservingPrimary(
        primaryError,
        () => cleanupEvidenceForPath(screenshot),
        { faultRoot: entry && EVIDENCE_RUN_STATES.get(entry.run)?.root },
      );
    }
  }
}

module.exports = {
  critique,
  runCritic,
  takeScreenshot,
  getActors,
  parseCriticFeedback,
  screenshotEvidence,
  acquireReviewEvidenceAdmission,
  openReviewEvidenceAdmission,
  finalizeReviewEvidenceAdmission,
  bootstrapDefaultReviewEvidenceRoots,
  bootstrapReviewEvidenceHierarchy,
  DEFAULT_REVIEW_EVIDENCE_ROOTS,
  createPrivateEvidenceRun,
  createReviewEvidenceReadinessProbe,
  reserveEvidenceFile,
  managedEvidenceStat,
  sealManagedScreenshot,
  cleanupPrivateEvidenceRun,
  cleanupEvidenceForPath,
  retainPrivateEvidenceRun,
  retainEvidenceForPath,
  managedScreenshotReference,
  resolveManagedScreenshotReference,
  isManagedEvidencePath,
  evidenceCapacitySnapshot,
  CRITIC_SYSTEM_PROMPT,
};

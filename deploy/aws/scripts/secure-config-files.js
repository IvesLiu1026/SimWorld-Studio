"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

class SecureConfigFileError extends Error {
  constructor(message) {
    super(message);
    this.name = "SecureConfigFileError";
  }
}

function fail(message, details = {}) {
  const error = new SecureConfigFileError(message);
  Object.assign(error, details);
  throw error;
}

function requireSecurityPrimitives() {
  if (
    process.platform !== "linux"
    || typeof process.geteuid !== "function"
    || typeof process.getegid !== "function"
    || !Number.isInteger(fs.constants.O_NOFOLLOW)
    || !Number.isInteger(fs.constants.O_DIRECTORY)
  ) {
    fail("secure config files require Linux O_NOFOLLOW/O_DIRECTORY semantics");
  }
}

function absoluteNormalized(filename, label) {
  if (typeof filename !== "string" || !filename || filename.includes("\0") || !path.isAbsolute(filename)) {
    fail(`${label} must be an absolute path`);
  }
  const normalized = path.normalize(filename);
  if (normalized !== filename) fail(`${label} must not contain path aliases`);
  return normalized;
}

function rejectSymlinkComponents(filename, label, { omitFinal = false } = {}) {
  const target = absoluteNormalized(filename, label);
  const parsed = path.parse(target);
  const parts = target.slice(parsed.root.length).split(path.sep).filter(Boolean);
  let current = parsed.root;
  let rootMetadata;
  try { rootMetadata = fs.lstatSync(current); }
  catch { fail(`${label} path is unavailable`); }
  validateAncestor(rootMetadata, label);
  const limit = Math.max(0, parts.length - 1);
  for (let index = 0; index < limit; index += 1) {
    current = path.join(current, parts[index]);
    let metadata;
    try { metadata = fs.lstatSync(current); }
    catch { fail(`${label} path is unavailable`); }
    if (metadata.isSymbolicLink()) fail(`${label} must not traverse symlinks`);
    validateAncestor(metadata, label);
  }
  if (!omitFinal && parts.length > 0) {
    current = path.join(current, parts[parts.length - 1]);
    let metadata;
    try { metadata = fs.lstatSync(current); }
    catch { fail(`${label} path is unavailable`); }
    if (metadata.isSymbolicLink()) fail(`${label} must not traverse symlinks`);
  }
  return target;
}

function allowedOwner(metadata) {
  const effectiveUid = typeof process.geteuid === "function" ? process.geteuid() : metadata.uid;
  return metadata.uid === 0 || metadata.uid === effectiveUid;
}

function validateAncestor(metadata, label) {
  if (!metadata.isDirectory() || metadata.isSymbolicLink()) {
    fail(`${label} must traverse directories only`);
  }
  const mode = metadata.mode & 0o7777;
  const rootStickyDirectory = metadata.uid === 0
    && (mode & 0o1000) !== 0
    && (mode & 0o022) !== 0;
  if (!allowedOwner(metadata) && !rootStickyDirectory) {
    fail(`${label} ancestor owner does not meet policy`);
  }
  if ((mode & 0o022) !== 0 && !rootStickyDirectory) {
    fail(`${label} must not traverse writable ancestors`);
  }
}

function validGroupId(value) {
  return Number.isSafeInteger(value) && value > 0 && value <= 0xffffffff;
}

function safeMode(metadata, { expectedGroupId = undefined } = {}) {
  const mode = metadata.mode & 0o7777;
  if ((mode & 0o7000) !== 0 || (mode & 0o007) !== 0) return false;
  const ownerBits = mode & 0o700;
  if (ownerBits !== 0o400 && ownerBits !== 0o600) return false;
  const groupBits = mode & 0o070;
  if (groupBits === 0) return true;
  return groupBits === 0o040
    && validGroupId(expectedGroupId)
    && metadata.gid === expectedGroupId;
}

function sameFile(before, after) {
  return before.dev === after.dev
    && before.ino === after.ino
    && before.size === after.size
    && before.mtimeMs === after.mtimeMs
    && before.ctimeMs === after.ctimeMs
    && before.mode === after.mode
    && before.uid === after.uid
    && before.gid === after.gid
    && before.nlink === after.nlink;
}

function readSecretFile(filename, {
  label = "secret file",
  minimumBytes = 32,
  maximumBytes = 4096,
  expectedGroupId = undefined,
} = {}) {
  requireSecurityPrimitives();
  const target = rejectSymlinkComponents(filename, label);
  const flags = fs.constants.O_RDONLY
    | fs.constants.O_NOFOLLOW;
  let descriptor;
  try { descriptor = fs.openSync(target, flags); }
  catch { fail(`${label} could not be opened safely`); }
  try {
    const before = fs.fstatSync(descriptor);
    if (
      !before.isFile()
      || before.nlink !== 1
      || !allowedOwner(before)
      || !safeMode(before, { expectedGroupId })
    ) {
      fail(`${label} owner, link count, or mode does not meet policy`);
    }
    if (before.size < minimumBytes || before.size > maximumBytes) {
      fail(`${label} size does not meet policy`);
    }
    const value = fs.readFileSync(descriptor, "utf8");
    const after = fs.fstatSync(descriptor);
    if (!sameFile(before, after) || Buffer.byteLength(value) !== before.size) {
      fail(`${label} changed while it was read`);
    }
    const normalized = value.replace(/\r?\n$/, "");
    if (
      Buffer.byteLength(normalized) < minimumBytes
      || Buffer.byteLength(normalized) > maximumBytes
      || /[\u0000-\u001f\u007f]/.test(normalized)
      || !/^[A-Za-z0-9+/=_-]+$/.test(normalized)
    ) {
      fail(`${label} content does not meet policy`);
    }
    return normalized;
  } finally {
    fs.closeSync(descriptor);
  }
}

function validateOutputParent(target, label) {
  rejectSymlinkComponents(target, label, { omitFinal: true });
  const parent = path.dirname(target);
  let metadata;
  try { metadata = fs.lstatSync(parent); }
  catch { fail(`${label} parent is unavailable`); }
  if (
    !metadata.isDirectory()
    || metadata.isSymbolicLink()
    || !allowedOwner(metadata)
    || (metadata.mode & 0o022) !== 0
  ) {
    fail(`${label} parent must be owned and not group/world writable`);
  }
}

function validateExistingOutput(target, label, { expectedGroupId = undefined } = {}) {
  let metadata;
  try { metadata = fs.lstatSync(target); }
  catch (error) {
    if (error && error.code === "ENOENT") return;
    fail(`${label} could not be inspected`);
  }
  if (
    metadata.isSymbolicLink()
    || !metadata.isFile()
    || metadata.nlink !== 1
    || !allowedOwner(metadata)
    || !safeMode(metadata, { expectedGroupId })
  ) {
    fail(`${label} existing target does not meet replacement policy`);
  }
}

function statIdentity(filename) {
  try {
    const metadata = fs.statSync(filename);
    return { dev: metadata.dev, ino: metadata.ino };
  } catch (error) {
    if (error && error.code === "ENOENT") return null;
    throw error;
  }
}

function rejectInputOutputAliases(target, inputs, label) {
  const targetIdentity = statIdentity(target);
  for (const input of inputs) {
    if (!input || typeof input.filename !== "string") {
      fail(`${label} forbidden-input contract is invalid`);
    }
    const inputPath = path.resolve(input.filename);
    const inputLabel = input.label || "protected input";
    if (inputPath === target) fail(`${label} must not replace ${inputLabel}`);
    const inputIdentity = statIdentity(inputPath);
    if (
      targetIdentity
      && inputIdentity
      && targetIdentity.dev === inputIdentity.dev
      && targetIdentity.ino === inputIdentity.ino
    ) {
      fail(`${label} must not alias ${inputLabel}`);
    }
  }
}

function writePrivateAtomic(filename, content, {
  label = "output",
  forbiddenInputs = [],
  outputGroupId = undefined,
} = {}) {
  requireSecurityPrimitives();
  const target = absoluteNormalized(filename, label);
  if (outputGroupId !== undefined && !validGroupId(outputGroupId)) {
    fail(`${label} output group id is invalid`);
  }
  rejectInputOutputAliases(target, forbiddenInputs, label);
  validateOutputParent(target, label);
  validateExistingOutput(target, label, { expectedGroupId: outputGroupId });
  const temporary = path.join(
    path.dirname(target),
    `.${path.basename(target)}.${process.pid}.${crypto.randomBytes(8).toString("hex")}.tmp`,
  );
  let ownsTemporary = false;
  let descriptor;
  let committed = false;
  try {
    descriptor = fs.openSync(
      temporary,
      fs.constants.O_WRONLY
        | fs.constants.O_CREAT
        | fs.constants.O_EXCL
        | fs.constants.O_NOFOLLOW,
      0o600,
    );
    ownsTemporary = true;
    const effectiveUid = process.geteuid();
    const effectiveGid = process.getegid();
    if (outputGroupId !== undefined && outputGroupId !== effectiveGid) {
      fs.fchownSync(descriptor, effectiveUid, outputGroupId);
    }
    const finalMode = outputGroupId === undefined ? 0o600 : 0o640;
    fs.fchmodSync(descriptor, finalMode);
    fs.writeFileSync(descriptor, content, "utf8");
    fs.fsyncSync(descriptor);
    const staged = fs.fstatSync(descriptor);
    if (
      !staged.isFile()
      || staged.nlink !== 1
      || staged.uid !== effectiveUid
      || staged.gid !== (outputGroupId === undefined ? effectiveGid : outputGroupId)
      || (staged.mode & 0o7777) !== finalMode
    ) {
      fail(`${label} staged file does not meet publication policy`);
    }
    fs.closeSync(descriptor);
    descriptor = undefined;
    fs.renameSync(temporary, target);
    ownsTemporary = false;
    committed = true;
    const directory = fs.openSync(
      path.dirname(target),
      fs.constants.O_RDONLY
        | fs.constants.O_DIRECTORY,
    );
    try { fs.fsyncSync(directory); } finally { fs.closeSync(directory); }
  } catch (error) {
    if (committed) {
      fail(
        `${label} was installed but directory durability could not be confirmed: ${error && error.code || "fsync_failed"}`,
        { code: "SECURE_CONFIG_COMMITTED_NOT_DURABLE", committed: true },
      );
    }
    fail(`${label} could not be atomically written: ${error && error.code || "write_failed"}`);
  } finally {
    if (descriptor !== undefined) {
      try { fs.closeSync(descriptor); } catch {}
    }
    if (ownsTemporary) {
      try { fs.unlinkSync(temporary); } catch {}
    }
  }
  return target;
}

module.exports = {
  SecureConfigFileError,
  readSecretFile,
  writePrivateAtomic,
};

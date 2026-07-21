"use strict";

const fs = require("node:fs");
const path = require("node:path");

function requireDirectoryPrimitives() {
  if (process.platform !== "linux"
      || !Number.isInteger(fs.constants.O_DIRECTORY)
      || !Number.isInteger(fs.constants.O_NOFOLLOW)) {
    const error = new Error("durable artifact directories require Linux O_DIRECTORY/O_NOFOLLOW semantics");
    error.code = "DURABLE_ARTIFACT_PLATFORM_UNSUPPORTED";
    throw error;
  }
}

function normalizeDirectory(directory) {
  const value = String(directory || "");
  if (!path.isAbsolute(value) || path.normalize(value) !== value
      || value === path.parse(value).root) {
    const error = new Error("durable artifact directory must be an absolute normalized non-root path");
    error.code = "DURABLE_ARTIFACT_DIRECTORY_INVALID";
    throw error;
  }
  return value;
}

async function openPrivateDirectory(directory) {
  requireDirectoryPrimitives();
  const checked = normalizeDirectory(directory);
  const lexical = await fs.promises.lstat(checked);
  const resolved = await fs.promises.realpath(checked);
  const uid = typeof process.getuid === "function" ? process.getuid() : null;
  if (!lexical.isDirectory() || lexical.isSymbolicLink() || resolved !== checked
      || lexical.nlink < 1 || (lexical.mode & 0o777) !== 0o700
      || (uid !== null && lexical.uid !== uid)) {
    const error = new Error("durable artifact directory is not a private owner-controlled directory");
    error.code = "DURABLE_ARTIFACT_DIRECTORY_INSECURE";
    throw error;
  }
  const handle = await fs.promises.open(
    checked,
    fs.constants.O_RDONLY | fs.constants.O_DIRECTORY | fs.constants.O_NOFOLLOW,
  );
  try {
    const opened = await handle.stat();
    if (!opened.isDirectory() || opened.dev !== lexical.dev || opened.ino !== lexical.ino
        || (opened.mode & 0o777) !== 0o700 || (uid !== null && opened.uid !== uid)) {
      const error = new Error("durable artifact directory changed during verification");
      error.code = "DURABLE_ARTIFACT_DIRECTORY_INSECURE";
      throw error;
    }
    return handle;
  } catch (error) {
    await handle.close().catch(() => {});
    throw error;
  }
}

async function readPrivateFile(filename, { minBytes = 1, maxBytes } = {}) {
  requireDirectoryPrimitives();
  const checked = String(filename || "");
  if (!path.isAbsolute(checked) || path.normalize(checked) !== checked
      || !Number.isSafeInteger(minBytes) || minBytes < 0
      || !Number.isSafeInteger(maxBytes) || maxBytes < Math.max(1, minBytes)) {
    const error = new Error("durable artifact file read options are invalid");
    error.code = "DURABLE_ARTIFACT_FILE_INVALID";
    throw error;
  }
  const uid = typeof process.getuid === "function" ? process.getuid() : null;
  const directory = path.dirname(checked);
  let directoryHandle;
  let handle;
  try {
    directoryHandle = await openPrivateDirectory(directory);
    const directoryBefore = await directoryHandle.stat();
    handle = await fs.promises.open(
      checked,
      fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW,
    );
    const before = await handle.stat();
    if (!before.isFile() || before.nlink !== 1 || (before.mode & 0o777) !== 0o600
        || before.size < minBytes || before.size > maxBytes
        || (uid !== null && before.uid !== uid)) {
      const error = new Error("durable artifact file is not a private owner-controlled file");
      error.code = before.isFile() && before.nlink === 2
        && (before.mode & 0o777) === 0o600 && (uid === null || before.uid === uid)
        ? "DURABLE_ARTIFACT_FILE_BUSY"
        : "DURABLE_ARTIFACT_FILE_INSECURE";
      throw error;
    }
    const bytes = await handle.readFile();
    const after = await handle.stat();
    if (!after.isFile() || after.nlink !== 1 || (after.mode & 0o777) !== 0o600
        || after.dev !== before.dev || after.ino !== before.ino || after.size !== before.size
        || bytes.length !== after.size || (uid !== null && after.uid !== uid)) {
      const error = new Error("durable artifact file changed during verification");
      error.code = after.isFile() && after.nlink === 2
        && (after.mode & 0o777) === 0o600 && (uid === null || after.uid === uid)
        ? "DURABLE_ARTIFACT_FILE_BUSY"
        : "DURABLE_ARTIFACT_FILE_INSECURE";
      throw error;
    }
    const directoryAfter = await directoryHandle.stat();
    const liveDirectory = await fs.promises.lstat(directory);
    const resolvedDirectory = await fs.promises.realpath(directory);
    if (!liveDirectory.isDirectory() || liveDirectory.isSymbolicLink()
        || (liveDirectory.mode & 0o777) !== 0o700 || resolvedDirectory !== directory
        || liveDirectory.dev !== directoryBefore.dev || liveDirectory.ino !== directoryBefore.ino
        || directoryAfter.dev !== directoryBefore.dev || directoryAfter.ino !== directoryBefore.ino
        || (uid !== null && (liveDirectory.uid !== uid || directoryAfter.uid !== uid))) {
      const error = new Error("durable artifact directory changed during file verification");
      error.code = "DURABLE_ARTIFACT_DIRECTORY_INSECURE";
      throw error;
    }
    return bytes;
  } finally {
    if (handle) await handle.close().catch(() => {});
    if (directoryHandle) await directoryHandle.close().catch(() => {});
  }
}

async function assertPrivateDirectory(directory) {
  const handle = await openPrivateDirectory(directory);
  await handle.close();
  return normalizeDirectory(directory);
}

async function syncPrivateDirectory(directory) {
  const handle = await openPrivateDirectory(directory);
  try {
    await handle.sync();
  } finally {
    await handle.close().catch(() => {});
  }
  return normalizeDirectory(directory);
}

module.exports = {
  assertPrivateDirectory,
  readPrivateFile,
  syncPrivateDirectory,
};

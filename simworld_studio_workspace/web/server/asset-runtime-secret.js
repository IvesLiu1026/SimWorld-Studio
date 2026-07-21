"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { TextDecoder } = require("node:util");

const DEFAULT_MAXIMUM_BYTES = 8 * 1024;

function fail(label, reason) {
  throw new TypeError(`${label} ${reason}`);
}

function validateSecretText(value, label, minimumBytes = 1) {
  const text = typeof value === "string" ? value : "";
  if (!text || /[\x00-\x20\x7f]/.test(text)) {
    fail(label, "must contain exactly one non-empty line without control characters");
  }
  if (Buffer.byteLength(text, "utf8") < minimumBytes) {
    fail(label, `must contain at least ${minimumBytes} bytes`);
  }
  return text;
}

function requireAbsoluteSecretFile(value, label) {
  const candidate = typeof value === "string" ? value.trim() : "";
  if (!candidate || !path.isAbsolute(candidate)) {
    fail(label, "must be an absolute file path");
  }
  const resolved = path.resolve(candidate);
  if (resolved === path.parse(resolved).root) fail(label, "cannot be a filesystem root");
  return resolved;
}

function sameStableFile(before, after) {
  return before.dev === after.dev
    && before.ino === after.ino
    && before.size === after.size
    && before.mode === after.mode
    && before.uid === after.uid
    && before.mtimeMs === after.mtimeMs
    && before.ctimeMs === after.ctimeMs;
}

function readSecretFile(filename, label, options = {}) {
  const fsImpl = options.fsImpl || fs;
  const maximumBytes = options.maximumBytes || DEFAULT_MAXIMUM_BYTES;
  const descriptor = fsImpl.openSync(
    filename,
    fs.constants.O_RDONLY | Number(fs.constants.O_NOFOLLOW || 0),
  );
  try {
    const before = fsImpl.fstatSync(descriptor);
    if (!before.isFile() || before.size < 1 || before.size > maximumBytes) {
      fail(label, "must reference a bounded regular file");
    }
    if ((before.mode & 0o077) !== 0) {
      fail(label, "must not be readable or writable by group/other users");
    }
    const currentUid = typeof process.getuid === "function" ? process.getuid() : null;
    if (Number.isInteger(currentUid) && before.uid !== 0 && before.uid !== currentUid) {
      fail(label, "must be owned by root or the current service uid");
    }
    const bytes = fsImpl.readFileSync(descriptor);
    const after = fsImpl.fstatSync(descriptor);
    const buffer = Buffer.isBuffer(bytes) ? bytes : Buffer.from(bytes);
    if (buffer.length !== before.size || !sameStableFile(before, after)) {
      fail(label, "changed while it was being read");
    }
    let decoded;
    try {
      decoded = new TextDecoder("utf-8", { fatal: true }).decode(buffer);
    } catch (_error) {
      fail(label, "must contain valid UTF-8");
    }
    if (!/^[^\r\n]*(?:\r?\n)?$/.test(decoded)) {
      fail(label, "must contain exactly one line");
    }
    return decoded.replace(/\r?\n$/, "");
  } finally {
    fsImpl.closeSync(descriptor);
  }
}

function resolveRuntimeSecret(env, directName, fileName, options = {}) {
  const direct = typeof env[directName] === "string" ? env[directName] : "";
  const secretFile = typeof env[fileName] === "string" ? env[fileName].trim() : "";
  if (direct && secretFile) {
    throw new TypeError(`${directName} and ${fileName} are mutually exclusive`);
  }
  let value = direct;
  let source = direct ? directName : "";
  if (secretFile) {
    value = readSecretFile(
      requireAbsoluteSecretFile(secretFile, fileName),
      fileName,
      options,
    );
    source = fileName;
  }
  if (!value) {
    if (options.required) {
      throw new TypeError(`${directName} or ${fileName} is required`);
    }
    return Object.freeze({ value: "", source: "" });
  }
  return Object.freeze({
    value: validateSecretText(value, source || directName, options.minimumBytes || 1),
    source,
  });
}

module.exports = {
  readSecretFile,
  resolveRuntimeSecret,
  validateSecretText,
};

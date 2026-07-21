"use strict";

/**
 * Append-only artifact revision journal.
 *
 * The journal deliberately has no HTTP or service integration. An operator
 * supplies one dedicated, absolute, owner-only root. Immutable chain entries
 * live separately from content blobs so retention can remove expired content
 * without rewriting or weakening the audit chain.
 */

const crypto = require("node:crypto");
const fsConstants = require("node:fs").constants;
const fs = require("node:fs/promises");
const path = require("node:path");

const JOURNAL_ENTRY_SCHEMA = "simworld-artifact-revision-entry/v1";
const RETENTION_PLAN_SCHEMA = "simworld-artifact-retention-plan/v1";
const BACKUP_MANIFEST_SCHEMA = "simworld-artifact-backup-manifest/v1";
const PUBLIC_REVISION_SCHEMA = "simworld-artifact-public-revision/v1";

const ENTRY_FILE_RE = /^(\d{12})-([a-f0-9]{64})\.json$/;
const BLOB_FILE_RE = /^(\d{12})-([a-f0-9]{64})\.json$/;
const PENDING_FILE_RE = /^[a-f0-9-]{8,80}\.pending$/;
const SHA256_RE = /^[a-f0-9]{64}$/;
const SAFE_LABEL_RE = /^[A-Za-z0-9][A-Za-z0-9._:@-]{0,159}$/;
const IDEMPOTENCY_KEY_RE = /^[A-Za-z0-9][A-Za-z0-9._:@-]{7,127}$/;
const ISO_MILLIS_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/;
const MAX_EXPIRY_WINDOW_MS = 10 * 366 * 24 * 60 * 60 * 1_000;

const HARD_LIMITS = Object.freeze({
  maxArtifactBytes: 1_048_576,
  maxJournalEntries: 10_000,
  maxJsonDepth: 16,
  maxJsonNodes: 50_000,
  maxObjectKeys: 256,
  maxArrayItems: 4_096,
  maxKeyBytes: 128,
  maxStringBytes: 65_536,
  maxLineageItems: 64,
  maxPageSize: 100,
  maxRetentionTargets: 256,
  maxRecordBytes: 262_144,
});

const ERROR_MESSAGES = Object.freeze({
  ARTIFACT_JOURNAL_INVALID_INPUT: "The artifact journal request is invalid.",
  ARTIFACT_JOURNAL_ROOT_INSECURE: "The artifact journal root is not an owner-only canonical directory.",
  ARTIFACT_JOURNAL_BUSY: "The artifact journal is busy.",
  ARTIFACT_JOURNAL_CORRUPT: "The artifact journal integrity check failed.",
  ARTIFACT_JOURNAL_LIMIT_EXCEEDED: "The artifact journal safety limit was exceeded.",
  ARTIFACT_JOURNAL_SECRET_REJECTED: "Secret-bearing artifact data is not permitted in the journal.",
  ARTIFACT_IDEMPOTENCY_CONFLICT: "The idempotency key was already used for different request bytes.",
  ARTIFACT_REVISION_CONFLICT: "The artifact revision already exists.",
  ARTIFACT_ACCESS_DENIED: "The artifact revision is unavailable to this owner.",
  ARTIFACT_REVISION_RETAINED: "The artifact revision content has been retained out of the live journal.",
  ARTIFACT_RETENTION_APPLY_REQUIRED: "Retention requires an explicit operator apply flag.",
  ARTIFACT_RETENTION_PLAN_STALE: "The retention plan no longer matches the journal head.",
  ARTIFACT_RETENTION_INCOMPLETE: "Retention was recorded but one or more content blobs remain pending deletion.",
  ARTIFACT_BACKUP_MANIFEST_INVALID: "The backup manifest does not match the journal.",
});

class ArtifactRevisionJournalError extends Error {
  constructor(code, details) {
    super(ERROR_MESSAGES[code] || "The artifact journal operation failed.");
    this.name = "ArtifactRevisionJournalError";
    this.code = code;
    if (details !== undefined) this.details = details;
  }
}

function fail(code, details) {
  throw new ArtifactRevisionJournalError(code, details);
}

function sha256(bytes) {
  return crypto.createHash("sha256").update(bytes).digest("hex");
}

function isPlainObject(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function exactObject(value, field, keys) {
  if (!isPlainObject(value)) fail("ARTIFACT_JOURNAL_INVALID_INPUT", { field });
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) {
    fail("ARTIFACT_JOURNAL_INVALID_INPUT", { field });
  }
  return value;
}

function optionalExactObject(value, field, requiredKeys, optionalKeys = []) {
  if (!isPlainObject(value)) fail("ARTIFACT_JOURNAL_INVALID_INPUT", { field });
  const allowed = new Set([...requiredKeys, ...optionalKeys]);
  if (Object.keys(value).some((key) => !allowed.has(key))
      || requiredKeys.some((key) => !(key in value))) {
    fail("ARTIFACT_JOURNAL_INVALID_INPUT", { field });
  }
  return value;
}

function safeLabel(value, field, max = 160) {
  if (typeof value !== "string" || value.length > max || !SAFE_LABEL_RE.test(value)) {
    fail("ARTIFACT_JOURNAL_INVALID_INPUT", { field });
  }
  if (looksSensitiveValue(value)) fail("ARTIFACT_JOURNAL_SECRET_REJECTED", { field });
  return value;
}

function digest(value, field) {
  if (typeof value !== "string" || !SHA256_RE.test(value)) {
    fail("ARTIFACT_JOURNAL_INVALID_INPUT", { field });
  }
  return value;
}

function isoTimestamp(value, field) {
  if (typeof value !== "string" || !ISO_MILLIS_RE.test(value)
      || new Date(value).toISOString() !== value) {
    fail("ARTIFACT_JOURNAL_INVALID_INPUT", { field });
  }
  return value;
}

function normalizeKeyName(key) {
  return String(key)
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .replace(/[^A-Za-z0-9]+/g, "_")
    .toLowerCase();
}

function looksSensitiveKey(key) {
  const normalized = `_${normalizeKeyName(key)}_`;
  return /_(?:password|passwd|pwd|secret|token|authorization|cookie|credential|credentials|private_key|client_secret|api_key|apikey|access_key|session_key|signing_key|dsn|database_url|postgres_url)_/.test(normalized)
    || normalized.includes("_secret_key_");
}

function looksSensitiveValue(value) {
  if (typeof value !== "string") return false;
  if (/-----BEGIN(?: [A-Z0-9]+)* PRIVATE KEY-----/.test(value)) return true;
  if (/\bBearer\s+[A-Za-z0-9._~+/-]{8,}/i.test(value)) return true;
  if (/\b(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{12,}|AIza[A-Za-z0-9_-]{20,}|(?:AKIA|ASIA)[A-Z0-9]{16})\b/.test(value)) return true;
  if (/\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b/.test(value)) return true;
  if (/[A-Za-z][A-Za-z0-9+.-]*:\/\/[^\s/:@]+:[^\s/@]+@/.test(value)) return true;
  if (/(?:password|passwd|secret|api[_-]?key|access[_-]?token|private[_-]?key)\s*[:=]\s*[^\s,;]{4,}/i.test(value)) return true;
  if (/\bfile:\/\//i.test(value) || /(?:^|[\\/])\.\.(?:[\\/]|$)/.test(value)) return true;
  if (/(?:^|[\\/])(?:\.ssh|\.aws|\.gnupg|secrets?)(?:[\\/]|$)/i.test(value)) return true;
  if (/(?:^|[\\/])(?:\.env(?:\.[^\\/]*)?|credentials|id_rsa|id_ed25519|shadow)(?:$|[\\/])/i.test(value)) return true;
  if (/^(?:\/home\/|\/root\/|\/etc\/|\/proc\/|\/sys\/|\/run\/secrets\/|[A-Za-z]:\\Users\\)/i.test(value)) return true;
  return false;
}

function canonicalJsonRaw(value, state = { seen: new Set() }) {
  if (value === null || typeof value === "boolean" || typeof value === "string") {
    return JSON.stringify(value);
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) fail("ARTIFACT_JOURNAL_INVALID_INPUT");
    return JSON.stringify(value);
  }
  if (typeof value !== "object") fail("ARTIFACT_JOURNAL_INVALID_INPUT");
  if (state.seen.has(value)) fail("ARTIFACT_JOURNAL_INVALID_INPUT");
  state.seen.add(value);
  let result;
  if (Array.isArray(value)) {
    result = `[${value.map((entry) => canonicalJsonRaw(entry, state)).join(",")}]`;
  } else {
    if (!isPlainObject(value)) fail("ARTIFACT_JOURNAL_INVALID_INPUT");
    result = `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJsonRaw(value[key], state)}`).join(",")}}`;
  }
  state.seen.delete(value);
  return result;
}

function canonicalizeArtifact(value, limits) {
  const state = { nodes: 0, seen: new Set() };

  function visit(current, depth, field) {
    state.nodes += 1;
    if (state.nodes > limits.maxJsonNodes || depth > limits.maxJsonDepth) {
      fail("ARTIFACT_JOURNAL_LIMIT_EXCEEDED", { field: "content" });
    }
    if (current === null || typeof current === "boolean") return JSON.stringify(current);
    if (typeof current === "number") {
      if (!Number.isFinite(current)) fail("ARTIFACT_JOURNAL_INVALID_INPUT", { field });
      return JSON.stringify(current);
    }
    if (typeof current === "string") {
      if (Buffer.byteLength(current, "utf8") > limits.maxStringBytes) {
        fail("ARTIFACT_JOURNAL_LIMIT_EXCEEDED", { field: "content" });
      }
      if (/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(current) || looksSensitiveValue(current)) {
        fail("ARTIFACT_JOURNAL_SECRET_REJECTED", { field: "content" });
      }
      return JSON.stringify(current);
    }
    if (typeof current !== "object" || state.seen.has(current)) {
      fail("ARTIFACT_JOURNAL_INVALID_INPUT", { field });
    }
    state.seen.add(current);
    let rendered;
    if (Array.isArray(current)) {
      if (current.length > limits.maxArrayItems) {
        fail("ARTIFACT_JOURNAL_LIMIT_EXCEEDED", { field: "content" });
      }
      rendered = `[${current.map((entry, index) => visit(entry, depth + 1, `${field}[${index}]`)).join(",")}]`;
    } else {
      if (!isPlainObject(current)) fail("ARTIFACT_JOURNAL_INVALID_INPUT", { field });
      const keys = Object.keys(current);
      if (keys.length > limits.maxObjectKeys) {
        fail("ARTIFACT_JOURNAL_LIMIT_EXCEEDED", { field: "content" });
      }
      rendered = `{${keys.sort().map((key) => {
        if (!key || Buffer.byteLength(key, "utf8") > limits.maxKeyBytes
            || /[\x00-\x1f\x7f]/.test(key)) {
          fail("ARTIFACT_JOURNAL_INVALID_INPUT", { field: "content" });
        }
        if (["__proto__", "prototype", "constructor"].includes(key) || looksSensitiveKey(key)) {
          fail("ARTIFACT_JOURNAL_SECRET_REJECTED", { field: "content" });
        }
        return `${JSON.stringify(key)}:${visit(current[key], depth + 1, `${field}.${key}`)}`;
      }).join(",")}}`;
    }
    state.seen.delete(current);
    return rendered;
  }

  if (!isPlainObject(value) && !Array.isArray(value)) {
    fail("ARTIFACT_JOURNAL_INVALID_INPUT", { field: "content" });
  }
  const canonical = visit(value, 0, "content");
  const bytes = Buffer.from(canonical, "utf8");
  if (bytes.length < 2 || bytes.length > limits.maxArtifactBytes) {
    fail("ARTIFACT_JOURNAL_LIMIT_EXCEEDED", { field: "content" });
  }
  return { canonical, bytes, digest: sha256(bytes) };
}

function publicRevision(record, retained) {
  return Object.freeze({
    schema: PUBLIC_REVISION_SCHEMA,
    artifact: Object.freeze({ ...record.artifact }),
    sequence: record.sequence,
    owner_id: record.owner_id,
    session_id: record.session_id,
    correlation_id: record.correlation_id,
    source_lineage: Object.freeze(record.source_lineage.map((item) => Object.freeze({ ...item }))),
    created_at: record.created_at,
    expires_at: record.expires_at,
    content_digest: record.content.digest,
    content_bytes: record.content.byte_length,
    status: retained ? "retained" : "active",
  });
}

function normalizeLineage(raw, limits, field = "sourceLineage") {
  if (raw === undefined) raw = [];
  if (!Array.isArray(raw) || raw.length > limits.maxLineageItems) {
    fail("ARTIFACT_JOURNAL_LIMIT_EXCEEDED", { field });
  }
  return raw.map((item, index) => {
    exactObject(item, `${field}[${index}]`, ["kind", "artifactId", "revision", "contentDigest"]);
    return Object.freeze({
      kind: safeLabel(item.kind, `${field}[${index}].kind`, 80),
      artifact_id: safeLabel(item.artifactId, `${field}[${index}].artifactId`),
      revision: safeLabel(item.revision, `${field}[${index}].revision`),
      content_digest: digest(item.contentDigest, `${field}[${index}].contentDigest`),
    });
  });
}

function effectiveLimits(overrides) {
  if (overrides === undefined) return HARD_LIMITS;
  if (!isPlainObject(overrides)) fail("ARTIFACT_JOURNAL_INVALID_INPUT", { field: "limits" });
  const result = { ...HARD_LIMITS };
  for (const [key, value] of Object.entries(overrides)) {
    if (!(key in HARD_LIMITS) || !Number.isSafeInteger(value) || value < 1 || value > HARD_LIMITS[key]) {
      fail("ARTIFACT_JOURNAL_INVALID_INPUT", { field: `limits.${key}` });
    }
    result[key] = value;
  }
  return Object.freeze(result);
}

function statMode(stat) {
  return stat.mode & 0o777;
}

function currentUid() {
  return typeof process.getuid === "function" ? process.getuid() : null;
}

async function validateSecureDirectory(directory, { create = false } = {}) {
  if (create) {
    try {
      await fs.mkdir(directory, { mode: 0o700, recursive: false });
    } catch (error) {
      if (!error || error.code !== "EEXIST") throw error;
    }
  }
  let stat;
  try {
    stat = await fs.lstat(directory);
  } catch (_error) {
    fail("ARTIFACT_JOURNAL_ROOT_INSECURE");
  }
  if (!stat.isDirectory() || stat.isSymbolicLink() || statMode(stat) !== 0o700
      || (currentUid() !== null && stat.uid !== currentUid())) {
    fail("ARTIFACT_JOURNAL_ROOT_INSECURE");
  }
  let resolved;
  try {
    resolved = await fs.realpath(directory);
  } catch (_error) {
    fail("ARTIFACT_JOURNAL_ROOT_INSECURE");
  }
  if (resolved !== path.resolve(directory)) fail("ARTIFACT_JOURNAL_ROOT_INSECURE");
  return stat;
}

async function openSecureFile(filename, maxBytes) {
  let handle;
  try {
    handle = await fs.open(filename, fsConstants.O_RDONLY | fsConstants.O_NOFOLLOW);
    const before = await handle.stat();
    if (!before.isFile() || before.isSymbolicLink() || before.nlink !== 1
        || statMode(before) !== 0o600
        || before.size < 1 || before.size > maxBytes
        || (currentUid() !== null && before.uid !== currentUid())) {
      fail("ARTIFACT_JOURNAL_CORRUPT");
    }
    const bytes = await handle.readFile();
    const after = await handle.stat();
    if (before.dev !== after.dev || before.ino !== after.ino || before.size !== after.size
        || bytes.length !== after.size) {
      fail("ARTIFACT_JOURNAL_CORRUPT");
    }
    return { bytes, stat: after };
  } catch (error) {
    if (error instanceof ArtifactRevisionJournalError) throw error;
    fail("ARTIFACT_JOURNAL_CORRUPT");
  } finally {
    if (handle) await handle.close().catch(() => {});
  }
}

async function secureFileStat(filename, maxBytes) {
  const opened = await openSecureFile(filename, maxBytes);
  return opened.stat;
}

function relativeBlobPath(name) {
  if (!BLOB_FILE_RE.test(name)) fail("ARTIFACT_JOURNAL_CORRUPT");
  return `blobs/${name}`;
}

function parseJsonBytes(bytes) {
  try {
    return JSON.parse(bytes.toString("utf8"));
  } catch (_error) {
    fail("ARTIFACT_JOURNAL_CORRUPT");
  }
}

function validateRevisionRecord(record) {
  exactObject(record, "record", [
    "schema", "entry_type", "sequence", "previous_digest", "artifact",
    "owner_id", "session_id", "correlation_id", "source_lineage",
    "created_at", "expires_at", "idempotency_digest", "request_digest",
    "content", "record_digest",
  ]);
  if (record.schema !== JOURNAL_ENTRY_SCHEMA || record.entry_type !== "revision") fail("ARTIFACT_JOURNAL_CORRUPT");
  if (!Number.isSafeInteger(record.sequence) || record.sequence < 1) fail("ARTIFACT_JOURNAL_CORRUPT");
  if (record.previous_digest !== null) digest(record.previous_digest, "previous_digest");
  exactObject(record.artifact, "artifact", ["kind", "id", "revision"]);
  safeLabel(record.artifact.kind, "artifact.kind", 80);
  safeLabel(record.artifact.id, "artifact.id");
  safeLabel(record.artifact.revision, "artifact.revision");
  safeLabel(record.owner_id, "owner_id");
  safeLabel(record.session_id, "session_id");
  safeLabel(record.correlation_id, "correlation_id");
  if (!Array.isArray(record.source_lineage) || record.source_lineage.length > HARD_LIMITS.maxLineageItems) fail("ARTIFACT_JOURNAL_CORRUPT");
  for (const item of record.source_lineage) {
    exactObject(item, "source_lineage", ["kind", "artifact_id", "revision", "content_digest"]);
    safeLabel(item.kind, "source_lineage.kind", 80);
    safeLabel(item.artifact_id, "source_lineage.artifact_id");
    safeLabel(item.revision, "source_lineage.revision");
    digest(item.content_digest, "source_lineage.content_digest");
  }
  isoTimestamp(record.created_at, "created_at");
  isoTimestamp(record.expires_at, "expires_at");
  const expiryWindow = Date.parse(record.expires_at) - Date.parse(record.created_at);
  if (expiryWindow <= 0 || expiryWindow > MAX_EXPIRY_WINDOW_MS) fail("ARTIFACT_JOURNAL_CORRUPT");
  digest(record.idempotency_digest, "idempotency_digest");
  digest(record.request_digest, "request_digest");
  exactObject(record.content, "content", ["digest", "byte_length", "blob_name"]);
  digest(record.content.digest, "content.digest");
  const blobMatch = BLOB_FILE_RE.exec(record.content.blob_name);
  if (!Number.isSafeInteger(record.content.byte_length) || record.content.byte_length < 2
      || record.content.byte_length > HARD_LIMITS.maxArtifactBytes
      || !blobMatch || Number(blobMatch[1]) !== record.sequence
      || blobMatch[2] !== record.content.digest) fail("ARTIFACT_JOURNAL_CORRUPT");
  digest(record.record_digest, "record_digest");
  return record;
}

function validateRetentionRecord(record) {
  exactObject(record, "record", [
    "schema", "entry_type", "sequence", "previous_digest", "applied_at",
    "applied_by", "plan_digest", "targets", "record_digest",
  ]);
  if (record.schema !== JOURNAL_ENTRY_SCHEMA || record.entry_type !== "retention") fail("ARTIFACT_JOURNAL_CORRUPT");
  if (!Number.isSafeInteger(record.sequence) || record.sequence < 1) fail("ARTIFACT_JOURNAL_CORRUPT");
  if (record.previous_digest !== null) digest(record.previous_digest, "previous_digest");
  isoTimestamp(record.applied_at, "applied_at");
  safeLabel(record.applied_by, "applied_by");
  digest(record.plan_digest, "plan_digest");
  digest(record.record_digest, "record_digest");
  if (!Array.isArray(record.targets) || record.targets.length < 1
      || record.targets.length > HARD_LIMITS.maxRetentionTargets) fail("ARTIFACT_JOURNAL_CORRUPT");
  let previousSequence = 0;
  for (const target of record.targets) {
    exactObject(target, "retention target", [
      "sequence", "record_digest", "blob_name", "content_digest", "expires_at",
    ]);
    if (!Number.isSafeInteger(target.sequence) || target.sequence <= previousSequence) fail("ARTIFACT_JOURNAL_CORRUPT");
    previousSequence = target.sequence;
    digest(target.record_digest, "target.record_digest");
    digest(target.content_digest, "target.content_digest");
    isoTimestamp(target.expires_at, "target.expires_at");
    if (!BLOB_FILE_RE.test(target.blob_name)) fail("ARTIFACT_JOURNAL_CORRUPT");
  }
  return record;
}

function recordDigest(record) {
  const envelope = { ...record };
  delete envelope.record_digest;
  return sha256(Buffer.from(canonicalJsonRaw(envelope), "utf8"));
}

function artifactIdentity(record) {
  return `${record.artifact.kind}\u0000${record.artifact.id}`;
}

function revisionIdentity(record) {
  return `${artifactIdentity(record)}\u0000${record.artifact.revision}`;
}

function targetFor(record) {
  return Object.freeze({
    sequence: record.sequence,
    record_digest: record.record_digest,
    blob_name: record.content.blob_name,
    content_digest: record.content.digest,
    expires_at: record.expires_at,
  });
}

function timestampFrom(clock) {
  let value;
  try {
    value = clock();
  } catch (_error) {
    fail("ARTIFACT_JOURNAL_INVALID_INPUT", { field: "clock" });
  }
  const date = value instanceof Date ? value : new Date(value);
  if (!Number.isFinite(date.getTime())) fail("ARTIFACT_JOURNAL_INVALID_INPUT", { field: "clock" });
  return date.toISOString();
}

function encodeCursor(afterSequence, ownerId, filterDigest) {
  return Buffer.from(canonicalJsonRaw({ after_sequence: afterSequence, owner_digest: sha256(ownerId), filter_digest: filterDigest }), "utf8").toString("base64url");
}

function decodeCursor(cursor, ownerId, filterDigest) {
  if (cursor == null) return 0;
  if (typeof cursor !== "string" || cursor.length < 8 || cursor.length > 512) fail("ARTIFACT_JOURNAL_INVALID_INPUT", { field: "cursor" });
  let parsed;
  try {
    parsed = JSON.parse(Buffer.from(cursor, "base64url").toString("utf8"));
  } catch (_error) {
    fail("ARTIFACT_JOURNAL_INVALID_INPUT", { field: "cursor" });
  }
  exactObject(parsed, "cursor", ["after_sequence", "owner_digest", "filter_digest"]);
  if (!Number.isSafeInteger(parsed.after_sequence) || parsed.after_sequence < 0
      || parsed.owner_digest !== sha256(ownerId) || parsed.filter_digest !== filterDigest) {
    fail("ARTIFACT_JOURNAL_INVALID_INPUT", { field: "cursor" });
  }
  return parsed.after_sequence;
}

class ArtifactRevisionJournal {
  constructor({
    root,
    now = () => new Date(),
    randomUUID = () => crypto.randomUUID(),
    sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)),
    lockAttempts = 200,
    lockRetryMs = 5,
    limits,
  } = {}) {
    if (typeof root !== "string" || !path.isAbsolute(root) || path.normalize(root) !== root || root === path.parse(root).root) {
      fail("ARTIFACT_JOURNAL_INVALID_INPUT", { field: "root" });
    }
    if (typeof now !== "function" || typeof randomUUID !== "function" || typeof sleep !== "function") {
      fail("ARTIFACT_JOURNAL_INVALID_INPUT");
    }
    if (!Number.isInteger(fsConstants.O_NOFOLLOW) || !Number.isInteger(fsConstants.O_DIRECTORY)) {
      fail("ARTIFACT_JOURNAL_ROOT_INSECURE", { reason: "required filesystem flags unavailable" });
    }
    if (!Number.isSafeInteger(lockAttempts) || lockAttempts < 1 || lockAttempts > 10_000
        || !Number.isSafeInteger(lockRetryMs) || lockRetryMs < 0 || lockRetryMs > 1_000) {
      fail("ARTIFACT_JOURNAL_INVALID_INPUT", { field: "lock" });
    }
    this.root = root;
    this.entriesDirectory = path.join(root, "entries");
    this.blobsDirectory = path.join(root, "blobs");
    this.pendingDirectory = path.join(root, "pending");
    this.lockPath = path.join(root, ".artifact-journal.lock");
    this.now = now;
    this.randomUUID = randomUUID;
    this.sleep = sleep;
    this.lockAttempts = lockAttempts;
    this.lockRetryMs = lockRetryMs;
    this.limits = effectiveLimits(limits);
  }

  async initialize() {
    try {
      await fs.mkdir(this.root, { mode: 0o700, recursive: true });
    } catch (_error) {
      fail("ARTIFACT_JOURNAL_ROOT_INSECURE");
    }
    await validateSecureDirectory(this.root);
    await validateSecureDirectory(this.entriesDirectory, { create: true });
    await validateSecureDirectory(this.blobsDirectory, { create: true });
    await validateSecureDirectory(this.pendingDirectory, { create: true });
  }

  async _acquireLock() {
    for (let attempt = 0; attempt < this.lockAttempts; attempt += 1) {
      let handle;
      try {
        handle = await fs.open(
          this.lockPath,
          fsConstants.O_WRONLY | fsConstants.O_CREAT | fsConstants.O_EXCL | fsConstants.O_NOFOLLOW,
          0o600,
        );
        await handle.chmod(0o600);
        await handle.writeFile(Buffer.from(canonicalJsonRaw({
          schema: "simworld-artifact-journal-lock/v1",
          pid: process.pid,
        }), "utf8"));
        await handle.sync();
        const stat = await handle.stat();
        if (!stat.isFile() || stat.nlink !== 1 || statMode(stat) !== 0o600) fail("ARTIFACT_JOURNAL_ROOT_INSECURE");
        return { handle, stat };
      } catch (error) {
        if (handle) await handle.close().catch(() => {});
        if (!error || error.code !== "EEXIST") {
          if (error instanceof ArtifactRevisionJournalError) throw error;
          fail("ARTIFACT_JOURNAL_ROOT_INSECURE");
        }
        if (attempt + 1 < this.lockAttempts) await this.sleep(this.lockRetryMs);
      }
    }
    fail("ARTIFACT_JOURNAL_BUSY");
  }

  async _releaseLock(lock) {
    try {
      const current = await fs.lstat(this.lockPath);
      if (!current.isFile() || current.isSymbolicLink()
          || current.dev !== lock.stat.dev || current.ino !== lock.stat.ino) {
        fail("ARTIFACT_JOURNAL_ROOT_INSECURE");
      }
      await fs.unlink(this.lockPath);
    } finally {
      await lock.handle.close().catch(() => {});
    }
  }

  async _withLock(operation) {
    await this.initialize();
    const lock = await this._acquireLock();
    let result;
    let operationError;
    try {
      await validateSecureDirectory(this.root);
      await validateSecureDirectory(this.entriesDirectory);
      await validateSecureDirectory(this.blobsDirectory);
      await validateSecureDirectory(this.pendingDirectory);
      result = await operation();
    } catch (error) {
      operationError = error;
    }
    try {
      await this._releaseLock(lock);
    } catch (releaseError) {
      if (!operationError) operationError = releaseError;
    }
    if (operationError) throw operationError;
    return result;
  }

  async _fsyncDirectory(directory) {
    let handle;
    try {
      handle = await fs.open(directory, fsConstants.O_RDONLY | fsConstants.O_DIRECTORY | fsConstants.O_NOFOLLOW);
      await handle.sync();
    } catch (_error) {
      fail("ARTIFACT_JOURNAL_ROOT_INSECURE");
    } finally {
      if (handle) await handle.close().catch(() => {});
    }
  }

  async _atomicCreateOnly(filename, bytes, maxBytes) {
    if (!Buffer.isBuffer(bytes) || bytes.length < 1 || bytes.length > maxBytes) fail("ARTIFACT_JOURNAL_LIMIT_EXCEEDED");
    const rawRandom = String(this.randomUUID()).toLowerCase();
    if (!/^[a-f0-9-]{8,80}$/.test(rawRandom)) fail("ARTIFACT_JOURNAL_INVALID_INPUT", { field: "randomUUID" });
    const pendingPath = path.join(this.pendingDirectory, `${rawRandom}.pending`);
    let handle;
    let pendingStat;
    try {
      handle = await fs.open(
        pendingPath,
        fsConstants.O_WRONLY | fsConstants.O_CREAT | fsConstants.O_EXCL | fsConstants.O_NOFOLLOW,
        0o600,
      );
      await handle.chmod(0o600);
      await handle.writeFile(bytes);
      await handle.sync();
      pendingStat = await handle.stat();
      if (!pendingStat.isFile() || pendingStat.nlink !== 1 || statMode(pendingStat) !== 0o600
          || pendingStat.size !== bytes.length) fail("ARTIFACT_JOURNAL_ROOT_INSECURE");
      await handle.close();
      handle = null;
      await fs.link(pendingPath, filename);
      const finalStat = await fs.lstat(filename);
      if (!finalStat.isFile() || finalStat.isSymbolicLink()
          || finalStat.dev !== pendingStat.dev || finalStat.ino !== pendingStat.ino
          || finalStat.nlink !== 2 || statMode(finalStat) !== 0o600) {
        fail("ARTIFACT_JOURNAL_ROOT_INSECURE");
      }
      await fs.unlink(pendingPath);
      await this._fsyncDirectory(path.dirname(filename));
      await this._fsyncDirectory(this.pendingDirectory);
    } catch (error) {
      if (handle) await handle.close().catch(() => {});
      if (error instanceof ArtifactRevisionJournalError) throw error;
      if (error && error.code === "EEXIST") fail("ARTIFACT_JOURNAL_CORRUPT");
      fail("ARTIFACT_JOURNAL_ROOT_INSECURE");
    } finally {
      try {
        const stat = await fs.lstat(pendingPath);
        if (pendingStat && stat.isFile() && !stat.isSymbolicLink()
            && stat.dev === pendingStat.dev && stat.ino === pendingStat.ino) {
          await fs.unlink(pendingPath);
        }
      } catch (_error) {
        // Missing pending files are the successful path. Never remove a path
        // whose inode is not the one this operation created.
      }
    }
  }

  async _directoryFiles(directory, pattern, maxCount) {
    let entries;
    try {
      entries = await fs.readdir(directory, { withFileTypes: true });
    } catch (_error) {
      fail("ARTIFACT_JOURNAL_CORRUPT");
    }
    if (entries.length > maxCount) fail("ARTIFACT_JOURNAL_LIMIT_EXCEEDED");
    const names = [];
    for (const entry of entries) {
      if (!entry.isFile() || entry.isSymbolicLink() || !pattern.test(entry.name)) {
        fail("ARTIFACT_JOURNAL_CORRUPT");
      }
      names.push(entry.name);
    }
    return names.sort();
  }

  async _verifyBlob(record) {
    try {
      const blobPath = path.join(this.blobsDirectory, record.content.blob_name);
      const { bytes } = await openSecureFile(blobPath, this.limits.maxArtifactBytes);
      if (bytes.length !== record.content.byte_length || sha256(bytes) !== record.content.digest) {
        fail("ARTIFACT_JOURNAL_CORRUPT");
      }
      const content = parseJsonBytes(bytes);
      const canonical = canonicalizeArtifact(content, this.limits);
      if (!bytes.equals(canonical.bytes) || canonical.digest !== record.content.digest) {
        fail("ARTIFACT_JOURNAL_CORRUPT");
      }
      return content;
    } catch (error) {
      if (error instanceof ArtifactRevisionJournalError
          && error.code === "ARTIFACT_JOURNAL_CORRUPT") throw error;
      fail("ARTIFACT_JOURNAL_CORRUPT");
    }
  }

  async _loadState({ verifyAllBlobs = false } = {}) {
    const entryNames = await this._directoryFiles(this.entriesDirectory, ENTRY_FILE_RE, this.limits.maxJournalEntries);
    const records = [];
    const revisions = [];
    const revisionsByDigest = new Map();
    const revisionsByIdentity = new Map();
    const idempotency = new Map();
    const heads = new Map();
    const retained = new Set();
    let previousDigest = null;

    for (let index = 0; index < entryNames.length; index += 1) {
      const sequence = index + 1;
      const name = entryNames[index];
      const match = ENTRY_FILE_RE.exec(name);
      if (!match || Number(match[1]) !== sequence) fail("ARTIFACT_JOURNAL_CORRUPT");
      let opened;
      let record;
      try {
        opened = await openSecureFile(path.join(this.entriesDirectory, name), this.limits.maxRecordBytes);
        const parsed = parseJsonBytes(opened.bytes);
        const canonical = Buffer.from(canonicalJsonRaw(parsed), "utf8");
        if (!opened.bytes.equals(canonical)) fail("ARTIFACT_JOURNAL_CORRUPT");
        record = parsed.entry_type === "revision"
          ? validateRevisionRecord(parsed)
          : parsed.entry_type === "retention"
            ? validateRetentionRecord(parsed)
            : fail("ARTIFACT_JOURNAL_CORRUPT");
        if (record.sequence !== sequence || record.previous_digest !== previousDigest
            || record.record_digest !== recordDigest(record) || match[2] !== record.record_digest) {
          fail("ARTIFACT_JOURNAL_CORRUPT");
        }
      } catch (error) {
        if (error instanceof ArtifactRevisionJournalError
            && error.code === "ARTIFACT_JOURNAL_CORRUPT") throw error;
        fail("ARTIFACT_JOURNAL_CORRUPT");
      }
      if (record.entry_type === "revision") {
        if (revisionsByDigest.has(record.record_digest) || revisionsByIdentity.has(revisionIdentity(record))
            || idempotency.has(record.idempotency_digest)) fail("ARTIFACT_JOURNAL_CORRUPT");
        revisions.push(record);
        revisionsByDigest.set(record.record_digest, record);
        revisionsByIdentity.set(revisionIdentity(record), record);
        idempotency.set(record.idempotency_digest, record);
        heads.set(artifactIdentity(record), record);
      } else {
        const appliedAt = Date.parse(record.applied_at);
        for (const target of record.targets) {
          const revision = revisionsByDigest.get(target.record_digest);
          if (!revision || revision.sequence !== target.sequence
              || retained.has(revision.record_digest)
              || heads.get(artifactIdentity(revision)) === revision
              || target.blob_name !== revision.content.blob_name
              || target.content_digest !== revision.content.digest
              || target.expires_at !== revision.expires_at
              || Date.parse(revision.expires_at) > appliedAt) {
            fail("ARTIFACT_JOURNAL_CORRUPT");
          }
          retained.add(revision.record_digest);
        }
      }
      records.push({ name, record, bytes: opened.bytes });
      previousDigest = record.record_digest;
    }

    const blobNames = await this._directoryFiles(
      this.blobsDirectory,
      BLOB_FILE_RE,
      this.limits.maxJournalEntries + this.limits.maxRetentionTargets,
    );
    const blobSet = new Set(blobNames);
    const referencedBlobs = new Map();
    for (const revision of revisions) {
      if (referencedBlobs.has(revision.content.blob_name)) fail("ARTIFACT_JOURNAL_CORRUPT");
      referencedBlobs.set(revision.content.blob_name, revision);
      const exists = blobSet.has(revision.content.blob_name);
      if (!retained.has(revision.record_digest) && !exists) fail("ARTIFACT_JOURNAL_CORRUPT");
      if (exists && verifyAllBlobs) await this._verifyBlob(revision);
    }
    for (const blobName of blobNames) {
      const revision = referencedBlobs.get(blobName);
      if (!revision) {
        // A complete, unreferenced blob can remain after a crash between blob
        // creation and entry publication. It is never public or retained by a
        // plan, but must still be an owner-only regular file.
        await secureFileStat(path.join(this.blobsDirectory, blobName), this.limits.maxArtifactBytes);
      }
    }

    return {
      records,
      revisions,
      revisionsByDigest,
      revisionsByIdentity,
      idempotency,
      heads,
      retained,
      blobSet,
      referencedBlobs,
      headDigest: previousDigest,
    };
  }

  _normalizeAppendRequest(raw, createdAt) {
    optionalExactObject(raw, "request", [
      "kind", "artifactId", "revision", "ownerId", "sessionId",
      "correlationId", "idempotencyKey", "expiresAt", "content",
    ], ["sourceLineage"]);
    const artifact = Object.freeze({
      kind: safeLabel(raw.kind, "kind", 80),
      id: safeLabel(raw.artifactId, "artifactId"),
      revision: safeLabel(raw.revision, "revision"),
    });
    const ownerId = safeLabel(raw.ownerId, "ownerId");
    const sessionId = safeLabel(raw.sessionId, "sessionId");
    const correlationId = safeLabel(raw.correlationId, "correlationId");
    if (typeof raw.idempotencyKey !== "string" || !IDEMPOTENCY_KEY_RE.test(raw.idempotencyKey)
        || looksSensitiveValue(raw.idempotencyKey)) {
      fail("ARTIFACT_JOURNAL_INVALID_INPUT", { field: "idempotencyKey" });
    }
    const expiresAt = isoTimestamp(raw.expiresAt, "expiresAt");
    const sourceLineage = normalizeLineage(raw.sourceLineage, this.limits);
    const content = canonicalizeArtifact(raw.content, this.limits);
    const requestEnvelope = {
      artifact,
      owner_id: ownerId,
      session_id: sessionId,
      correlation_id: correlationId,
      source_lineage: sourceLineage,
      expires_at: expiresAt,
      content_digest: content.digest,
      content_bytes: content.bytes.length,
    };
    return {
      artifact,
      ownerId,
      sessionId,
      correlationId,
      sourceLineage,
      expiresAt,
      content,
      idempotencyDigest: sha256(Buffer.from(`${ownerId}\u0000${raw.idempotencyKey}`, "utf8")),
      requestDigest: sha256(Buffer.from(canonicalJsonRaw(requestEnvelope), "utf8")),
    };
  }

  async append(raw) {
    return this._withLock(async () => {
      const createdAt = timestampFrom(this.now);
      const request = this._normalizeAppendRequest(raw, createdAt);
      const state = await this._loadState();
      const prior = state.idempotency.get(request.idempotencyDigest);
      if (prior) {
        if (prior.request_digest !== request.requestDigest) fail("ARTIFACT_IDEMPOTENCY_CONFLICT");
        return Object.freeze({
          created: false,
          idempotent: true,
          revision: publicRevision(prior, state.retained.has(prior.record_digest)),
        });
      }
      const expiryWindow = Date.parse(request.expiresAt) - Date.parse(createdAt);
      if (expiryWindow <= 0 || expiryWindow > MAX_EXPIRY_WINDOW_MS) {
        fail("ARTIFACT_JOURNAL_INVALID_INPUT", { field: "expiresAt" });
      }
      const identity = `${request.artifact.kind}\u0000${request.artifact.id}\u0000${request.artifact.revision}`;
      if (state.revisionsByIdentity.has(identity)) fail("ARTIFACT_REVISION_CONFLICT");
      const sequence = state.records.length + 1;
      if (sequence > this.limits.maxJournalEntries) fail("ARTIFACT_JOURNAL_LIMIT_EXCEEDED");
      const prefix = String(sequence).padStart(12, "0");
      const blobName = `${prefix}-${request.content.digest}.json`;
      await this._atomicCreateOnly(
        path.join(this.blobsDirectory, blobName),
        request.content.bytes,
        this.limits.maxArtifactBytes,
      );
      const envelope = {
        schema: JOURNAL_ENTRY_SCHEMA,
        entry_type: "revision",
        sequence,
        previous_digest: state.headDigest,
        artifact: request.artifact,
        owner_id: request.ownerId,
        session_id: request.sessionId,
        correlation_id: request.correlationId,
        source_lineage: request.sourceLineage,
        created_at: createdAt,
        expires_at: request.expiresAt,
        idempotency_digest: request.idempotencyDigest,
        request_digest: request.requestDigest,
        content: {
          digest: request.content.digest,
          byte_length: request.content.bytes.length,
          blob_name: blobName,
        },
      };
      const record = { ...envelope, record_digest: sha256(Buffer.from(canonicalJsonRaw(envelope), "utf8")) };
      const entryName = `${prefix}-${record.record_digest}.json`;
      const entryBytes = Buffer.from(canonicalJsonRaw(record), "utf8");
      await this._atomicCreateOnly(
        path.join(this.entriesDirectory, entryName),
        entryBytes,
        this.limits.maxRecordBytes,
      );
      return Object.freeze({
        created: true,
        idempotent: false,
        revision: publicRevision(record, false),
      });
    });
  }

  async readRevision(raw) {
    exactObject(raw, "request", ["kind", "artifactId", "revision", "ownerId"]);
    const ownerId = safeLabel(raw.ownerId, "ownerId");
    const kind = safeLabel(raw.kind, "kind", 80);
    const artifactId = safeLabel(raw.artifactId, "artifactId");
    const revision = safeLabel(raw.revision, "revision");
    return this._withLock(async () => {
      const state = await this._loadState();
      const record = state.revisionsByIdentity.get(`${kind}\u0000${artifactId}\u0000${revision}`);
      if (!record || record.owner_id !== ownerId) fail("ARTIFACT_ACCESS_DENIED");
      if (state.retained.has(record.record_digest)) fail("ARTIFACT_REVISION_RETAINED");
      const content = await this._verifyBlob(record);
      return Object.freeze({ revision: publicRevision(record, false), content });
    });
  }

  async listRevisions(raw) {
    optionalExactObject(raw, "request", ["ownerId"], ["kind", "artifactId", "cursor", "limit"]);
    const ownerId = safeLabel(raw.ownerId, "ownerId");
    const kind = raw.kind === undefined ? null : safeLabel(raw.kind, "kind", 80);
    const artifactId = raw.artifactId === undefined ? null : safeLabel(raw.artifactId, "artifactId");
    if (artifactId !== null && kind === null) fail("ARTIFACT_JOURNAL_INVALID_INPUT", { field: "kind" });
    const limit = raw.limit === undefined ? 50 : raw.limit;
    if (!Number.isSafeInteger(limit) || limit < 1 || limit > this.limits.maxPageSize) {
      fail("ARTIFACT_JOURNAL_INVALID_INPUT", { field: "limit" });
    }
    const filterDigest = sha256(Buffer.from(canonicalJsonRaw({ kind, artifact_id: artifactId }), "utf8"));
    const afterSequence = decodeCursor(raw.cursor, ownerId, filterDigest);
    return this._withLock(async () => {
      const state = await this._loadState();
      const matches = state.revisions.filter((record) => record.sequence > afterSequence
        && record.owner_id === ownerId
        && (kind === null || record.artifact.kind === kind)
        && (artifactId === null || record.artifact.id === artifactId));
      const page = matches.slice(0, limit);
      const hasMore = matches.length > page.length;
      const nextCursor = hasMore
        ? encodeCursor(page[page.length - 1].sequence, ownerId, filterDigest)
        : null;
      return Object.freeze({
        revisions: Object.freeze(page.map((record) => publicRevision(record, state.retained.has(record.record_digest)))),
        next_cursor: nextCursor,
      });
    });
  }

  _retentionPlanFromState(state, asOf) {
    const asOfMillis = Date.parse(asOf);
    const candidates = [];
    const pending = [];
    for (const record of state.revisions) {
      const item = Object.freeze({
        sequence: record.sequence,
        record_digest: record.record_digest,
        blob_relative_path: relativeBlobPath(record.content.blob_name),
        content_digest: record.content.digest,
        expires_at: record.expires_at,
      });
      if (state.retained.has(record.record_digest)) {
        if (state.blobSet.has(record.content.blob_name)) pending.push(item);
      } else if (state.heads.get(artifactIdentity(record)) !== record
          && Date.parse(record.expires_at) <= asOfMillis) {
        candidates.push(item);
      }
    }
    if (candidates.length + pending.length > this.limits.maxRetentionTargets) {
      fail("ARTIFACT_JOURNAL_LIMIT_EXCEEDED", { field: "retention targets" });
    }
    const envelope = {
      schema: RETENTION_PLAN_SCHEMA,
      as_of: asOf,
      journal_head_digest: state.headDigest,
      journal_entry_count: state.records.length,
      candidates,
      pending_deletions: pending,
    };
    return Object.freeze({
      ...envelope,
      plan_digest: sha256(Buffer.from(canonicalJsonRaw(envelope), "utf8")),
    });
  }

  async planRetention(raw = {}) {
    optionalExactObject(raw, "request", [], ["asOf"]);
    const current = timestampFrom(this.now);
    const asOf = raw.asOf === undefined ? current : isoTimestamp(raw.asOf, "asOf");
    if (Date.parse(asOf) > Date.parse(current)) fail("ARTIFACT_JOURNAL_INVALID_INPUT", { field: "asOf" });
    return this._withLock(async () => this._retentionPlanFromState(await this._loadState(), asOf));
  }

  _validateRetentionPlan(plan) {
    exactObject(plan, "plan", [
      "schema", "as_of", "journal_head_digest", "journal_entry_count",
      "candidates", "pending_deletions", "plan_digest",
    ]);
    if (plan.schema !== RETENTION_PLAN_SCHEMA) fail("ARTIFACT_RETENTION_PLAN_STALE");
    isoTimestamp(plan.as_of, "plan.as_of");
    if (plan.journal_head_digest !== null) digest(plan.journal_head_digest, "plan.journal_head_digest");
    if (!Number.isSafeInteger(plan.journal_entry_count) || plan.journal_entry_count < 0) fail("ARTIFACT_RETENTION_PLAN_STALE");
    for (const field of ["candidates", "pending_deletions"]) {
      if (!Array.isArray(plan[field]) || plan[field].length > this.limits.maxRetentionTargets) fail("ARTIFACT_RETENTION_PLAN_STALE");
      for (const item of plan[field]) {
        exactObject(item, `plan.${field}`, [
          "sequence", "record_digest", "blob_relative_path", "content_digest", "expires_at",
        ]);
        if (!Number.isSafeInteger(item.sequence) || item.sequence < 1
            || item.blob_relative_path !== `blobs/${path.basename(item.blob_relative_path)}`
            || !BLOB_FILE_RE.test(path.basename(item.blob_relative_path))) fail("ARTIFACT_RETENTION_PLAN_STALE");
        digest(item.record_digest, "plan target record_digest");
        digest(item.content_digest, "plan target content_digest");
        isoTimestamp(item.expires_at, "plan target expires_at");
      }
    }
    digest(plan.plan_digest, "plan.plan_digest");
    const envelope = { ...plan };
    delete envelope.plan_digest;
    if (plan.plan_digest !== sha256(Buffer.from(canonicalJsonRaw(envelope), "utf8"))) fail("ARTIFACT_RETENTION_PLAN_STALE");
    return plan;
  }

  async _deleteAuthorizedBlob(record) {
    const filename = path.join(this.blobsDirectory, record.content.blob_name);
    let opened;
    try {
      opened = await openSecureFile(filename, this.limits.maxArtifactBytes);
    } catch (error) {
      try {
        await fs.lstat(filename);
      } catch (statError) {
        if (statError && statError.code === "ENOENT") return false;
      }
      throw error;
    }
    if (opened.bytes.length !== record.content.byte_length || sha256(opened.bytes) !== record.content.digest) {
      fail("ARTIFACT_JOURNAL_CORRUPT");
    }
    const current = await fs.lstat(filename).catch(() => null);
    if (!current || !current.isFile() || current.isSymbolicLink()
        || current.dev !== opened.stat.dev || current.ino !== opened.stat.ino) {
      fail("ARTIFACT_JOURNAL_CORRUPT");
    }
    await fs.unlink(filename);
    return true;
  }

  async applyRetention(plan, { operatorApply = false, operatorId } = {}) {
    if (operatorApply !== true) fail("ARTIFACT_RETENTION_APPLY_REQUIRED");
    const appliedBy = safeLabel(operatorId, "operatorId");
    this._validateRetentionPlan(plan);
    return this._withLock(async () => {
      const now = timestampFrom(this.now);
      if (Date.parse(plan.as_of) > Date.parse(now)) fail("ARTIFACT_RETENTION_PLAN_STALE");
      let state = await this._loadState({ verifyAllBlobs: true });
      const expected = this._retentionPlanFromState(state, plan.as_of);
      if (canonicalJsonRaw(expected) !== canonicalJsonRaw(plan)) fail("ARTIFACT_RETENTION_PLAN_STALE");

      if (plan.candidates.length > 0) {
        const targets = plan.candidates.map((item) => {
          const record = state.revisionsByDigest.get(item.record_digest);
          if (!record) fail("ARTIFACT_RETENTION_PLAN_STALE");
          return targetFor(record);
        });
        const sequence = state.records.length + 1;
        if (sequence > this.limits.maxJournalEntries) fail("ARTIFACT_JOURNAL_LIMIT_EXCEEDED");
        const envelope = {
          schema: JOURNAL_ENTRY_SCHEMA,
          entry_type: "retention",
          sequence,
          previous_digest: state.headDigest,
          applied_at: now,
          applied_by: appliedBy,
          plan_digest: plan.plan_digest,
          targets,
        };
        const record = { ...envelope, record_digest: sha256(Buffer.from(canonicalJsonRaw(envelope), "utf8")) };
        const entryName = `${String(sequence).padStart(12, "0")}-${record.record_digest}.json`;
        await this._atomicCreateOnly(
          path.join(this.entriesDirectory, entryName),
          Buffer.from(canonicalJsonRaw(record), "utf8"),
          this.limits.maxRecordBytes,
        );
        state = await this._loadState({ verifyAllBlobs: true });
      }

      const authorized = [...plan.candidates, ...plan.pending_deletions];
      let deleted = 0;
      const failures = [];
      for (const item of authorized) {
        const record = state.revisionsByDigest.get(item.record_digest);
        if (!record || !state.retained.has(record.record_digest)) fail("ARTIFACT_JOURNAL_CORRUPT");
        try {
          if (await this._deleteAuthorizedBlob(record)) deleted += 1;
      } catch (error) {
          if (error instanceof ArtifactRevisionJournalError
              && error.code === "ARTIFACT_JOURNAL_CORRUPT") throw error;
          failures.push(record.record_digest);
        }
      }
      await this._fsyncDirectory(this.blobsDirectory);
      if (failures.length > 0) {
        fail("ARTIFACT_RETENTION_INCOMPLETE", { pending_count: failures.length });
      }
      const finalState = await this._loadState();
      return Object.freeze({
        applied: true,
        retained_count: plan.candidates.length,
        deleted_blob_count: deleted,
        journal_head_digest: finalState.headDigest,
      });
    });
  }

  async verifyIntegrity() {
    return this._withLock(async () => {
      const state = await this._loadState({ verifyAllBlobs: true });
      const orphanCount = [...state.blobSet].filter((name) => !state.referencedBlobs.has(name)).length;
      return Object.freeze({
        schema: "simworld-artifact-journal-integrity/v1",
        status: "valid",
        entry_count: state.records.length,
        revision_count: state.revisions.length,
        retained_revision_count: state.retained.size,
        orphan_blob_count: orphanCount,
        head_digest: state.headDigest,
      });
    });
  }

  async _backupFiles(state) {
    const files = [];
    for (const entry of state.records) {
      files.push(Object.freeze({
        relative_path: `entries/${entry.name}`,
        sha256: sha256(entry.bytes),
        size: entry.bytes.length,
        mode: "0600",
      }));
    }
    for (const record of state.revisions) {
      if (!state.blobSet.has(record.content.blob_name)) continue;
      const { bytes } = await openSecureFile(
        path.join(this.blobsDirectory, record.content.blob_name),
        this.limits.maxArtifactBytes,
      );
      files.push(Object.freeze({
        relative_path: relativeBlobPath(record.content.blob_name),
        sha256: sha256(bytes),
        size: bytes.length,
        mode: "0600",
      }));
    }
    return files.sort((left, right) => left.relative_path.localeCompare(right.relative_path));
  }

  _backupEnvelope(state, generatedAt, files) {
    return {
      schema: BACKUP_MANIFEST_SCHEMA,
      generated_at: generatedAt,
      journal_head_digest: state.headDigest,
      journal_entry_count: state.records.length,
      revision_count: state.revisions.length,
      retained_revision_count: state.retained.size,
      files,
    };
  }

  async generateBackupManifest() {
    const generatedAt = timestampFrom(this.now);
    return this._withLock(async () => {
      const state = await this._loadState({ verifyAllBlobs: true });
      const envelope = this._backupEnvelope(state, generatedAt, await this._backupFiles(state));
      return Object.freeze({
        ...envelope,
        manifest_digest: sha256(Buffer.from(canonicalJsonRaw(envelope), "utf8")),
      });
    });
  }

  _validateBackupManifest(manifest) {
    exactObject(manifest, "manifest", [
      "schema", "generated_at", "journal_head_digest", "journal_entry_count",
      "revision_count", "retained_revision_count", "files", "manifest_digest",
    ]);
    if (manifest.schema !== BACKUP_MANIFEST_SCHEMA) fail("ARTIFACT_BACKUP_MANIFEST_INVALID");
    isoTimestamp(manifest.generated_at, "manifest.generated_at");
    if (manifest.journal_head_digest !== null) digest(manifest.journal_head_digest, "manifest.journal_head_digest");
    for (const field of ["journal_entry_count", "revision_count", "retained_revision_count"]) {
      if (!Number.isSafeInteger(manifest[field]) || manifest[field] < 0) fail("ARTIFACT_BACKUP_MANIFEST_INVALID");
    }
    if (!Array.isArray(manifest.files) || manifest.files.length > this.limits.maxJournalEntries * 2) fail("ARTIFACT_BACKUP_MANIFEST_INVALID");
    let previous = "";
    for (const file of manifest.files) {
      exactObject(file, "manifest file", ["relative_path", "sha256", "size", "mode"]);
      if (typeof file.relative_path !== "string"
          || !/^(?:entries|blobs)\/\d{12}-[a-f0-9]{64}\.json$/.test(file.relative_path)
          || file.relative_path <= previous || file.mode !== "0600"
          || !Number.isSafeInteger(file.size) || file.size < 1) fail("ARTIFACT_BACKUP_MANIFEST_INVALID");
      previous = file.relative_path;
      digest(file.sha256, "manifest file sha256");
    }
    digest(manifest.manifest_digest, "manifest.manifest_digest");
    const envelope = { ...manifest };
    delete envelope.manifest_digest;
    if (manifest.manifest_digest !== sha256(Buffer.from(canonicalJsonRaw(envelope), "utf8"))) {
      fail("ARTIFACT_BACKUP_MANIFEST_INVALID");
    }
    return manifest;
  }

  async verifyBackupManifest(manifest) {
    this._validateBackupManifest(manifest);
    return this._withLock(async () => {
      const state = await this._loadState({ verifyAllBlobs: true });
      const expectedEnvelope = this._backupEnvelope(state, manifest.generated_at, await this._backupFiles(state));
      const actualEnvelope = { ...manifest };
      delete actualEnvelope.manifest_digest;
      if (canonicalJsonRaw(expectedEnvelope) !== canonicalJsonRaw(actualEnvelope)) {
        fail("ARTIFACT_BACKUP_MANIFEST_INVALID");
      }
      return Object.freeze({
        schema: "simworld-artifact-backup-verification/v1",
        status: "valid",
        manifest_digest: manifest.manifest_digest,
        journal_head_digest: state.headDigest,
        file_count: manifest.files.length,
      });
    });
  }
}

function createArtifactRevisionJournal(options) {
  return new ArtifactRevisionJournal(options);
}

module.exports = {
  BACKUP_MANIFEST_SCHEMA,
  HARD_LIMITS,
  JOURNAL_ENTRY_SCHEMA,
  RETENTION_PLAN_SCHEMA,
  ArtifactRevisionJournal,
  ArtifactRevisionJournalError,
  createArtifactRevisionJournal,
};

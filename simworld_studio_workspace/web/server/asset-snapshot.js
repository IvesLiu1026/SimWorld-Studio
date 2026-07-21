"use strict";

const crypto = require("node:crypto");

const SNAPSHOT_SCHEMA = "simworld-asset-snapshot/v1";
const LIVE_AUDIT_SCHEMA = "simworld-asset-live-audit/v1";
const ASSET_SCHEMA_VERSION = 2;
const MAX_RECEIPT_TTL_SECONDS = 900;
const MAX_CLOCK_SKEW_MS = 5_000;
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,159}$/;
const SHA256 = /^[a-f0-9]{64}$/;
const UTC_SECONDS = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;
const UNPINNED_REVISIONS = new Set(["dev", "latest", "main", "master", "unknown", "unversioned"]);

class AssetSnapshotError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = "AssetSnapshotError";
    this.code = code;
    this.retryable = details.retryable === true;
    this.details = Object.freeze(details.field ? { field: details.field } : {});
  }
}

function fail(code, message, details) {
  throw new AssetSnapshotError(code, message, details);
}

function object(value, field, keys) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    fail("ASSET_SNAPSHOT_INVALID", `${field} must be an object`, { field });
  }
  const allowed = new Set(keys);
  if (Object.keys(value).some((key) => !allowed.has(key)) || keys.some((key) => !(key in value))) {
    fail("ASSET_SNAPSHOT_INVALID", `${field} has an invalid shape`, { field });
  }
  return value;
}

function id(value, field, max = 160) {
  if (typeof value !== "string" || value.length > max || !SAFE_ID.test(value)) {
    fail("ASSET_SNAPSHOT_INVALID", `${field} is invalid`, { field });
  }
  return value;
}

function immutableRevision(value, field) {
  const revision = id(value, field);
  if (UNPINNED_REVISIONS.has(revision.toLowerCase())) {
    fail("ASSET_SNAPSHOT_INVALID", `${field} must identify an immutable revision`, { field });
  }
  return revision;
}

function count(value, field) {
  if (!Number.isSafeInteger(value) || value < 1 || value > 1_000_000_000) {
    fail("ASSET_SNAPSHOT_INVALID", `${field} must be a positive integer`, { field });
  }
  return value;
}

function validateAssetSnapshotManifest(manifest) {
  object(manifest, "manifest", ["schema", "snapshot_id", "ue_content_revision", "catalog", "postgres", "qdrant", "embedding"]);
  if (manifest.schema !== SNAPSHOT_SCHEMA) fail("ASSET_SNAPSHOT_INVALID", "Unsupported asset snapshot schema", { field: "schema" });
  immutableRevision(manifest.snapshot_id, "snapshot_id");
  immutableRevision(manifest.ue_content_revision, "ue_content_revision");
  object(manifest.catalog, "catalog", ["count", "sha256"]);
  count(manifest.catalog.count, "catalog.count");
  if (typeof manifest.catalog.sha256 !== "string" || !SHA256.test(manifest.catalog.sha256)) fail("ASSET_SNAPSHOT_INVALID", "catalog.sha256 is invalid", { field: "catalog.sha256" });
  object(manifest.postgres, "postgres", ["schema_version", "row_count"]);
  count(manifest.postgres.schema_version, "postgres.schema_version");
  if (manifest.postgres.schema_version !== ASSET_SCHEMA_VERSION) {
    fail("ASSET_SNAPSHOT_INVALID", "postgres.schema_version is unsupported", { field: "postgres.schema_version" });
  }
  count(manifest.postgres.row_count, "postgres.row_count");
  object(manifest.qdrant, "qdrant", ["collection", "point_count", "dense_name", "dense_size", "sparse_name"]);
  id(manifest.qdrant.collection, "qdrant.collection");
  count(manifest.qdrant.point_count, "qdrant.point_count");
  id(manifest.qdrant.dense_name, "qdrant.dense_name");
  if (!Number.isSafeInteger(manifest.qdrant.dense_size) || manifest.qdrant.dense_size < 1 || manifest.qdrant.dense_size > 65_536) fail("ASSET_SNAPSHOT_INVALID", "qdrant.dense_size is invalid", { field: "qdrant.dense_size" });
  id(manifest.qdrant.sparse_name, "qdrant.sparse_name");
  object(manifest.embedding, "embedding", ["version", "dense_model", "sparse_model"]);
  id(manifest.embedding.version, "embedding.version");
  for (const field of ["dense_model", "sparse_model"]) {
    if (typeof manifest.embedding[field] !== "string" || !manifest.embedding[field].trim() || manifest.embedding[field].length > 240 || /[\x00-\x1f\x7f]/.test(manifest.embedding[field])) {
      fail("ASSET_SNAPSHOT_INVALID", `embedding.${field} is invalid`, { field: `embedding.${field}` });
    }
  }
  return manifest;
}

function sha256(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
}

function canonicalJson(value) {
  if (value === null || typeof value === "boolean" || typeof value === "string") return JSON.stringify(value);
  if (typeof value === "number") {
    if (!Number.isFinite(value)) fail("ASSET_LIVE_AUDIT_RECEIPT_INVALID", "Receipt contains a non-finite number");
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map((entry) => canonicalJson(entry)).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  }
  fail("ASSET_LIVE_AUDIT_RECEIPT_INVALID", "Receipt contains a non-JSON value");
}

function parseExactJsonBytes(bytes, field, validator) {
  if (!Buffer.isBuffer(bytes) || bytes.length < 2) {
    fail("ASSET_LIVE_AUDIT_RECEIPT_INVALID", `${field} bytes are invalid`, { field });
  }
  let parsed;
  try {
    parsed = JSON.parse(bytes.toString("utf8"));
  } catch (_error) {
    fail("ASSET_LIVE_AUDIT_RECEIPT_INVALID", `${field} bytes are not valid JSON`, { field });
  }
  return validator(parsed);
}

function utcSecond(value, field) {
  if (typeof value !== "string" || !UTC_SECONDS.test(value)) {
    fail("ASSET_LIVE_AUDIT_RECEIPT_INVALID", `${field} must be a whole-second UTC timestamp`, { field });
  }
  const millis = Date.parse(value);
  if (!Number.isFinite(millis) || new Date(millis).toISOString() !== value.replace(/Z$/, ".000Z")) {
    fail("ASSET_LIVE_AUDIT_RECEIPT_INVALID", `${field} is invalid`, { field });
  }
  return millis;
}

function liveObservations(manifest) {
  return {
    asset_snapshot_revision: manifest.snapshot_id,
    ue_content_revision: manifest.ue_content_revision,
    catalog: manifest.catalog,
    postgres: manifest.postgres,
    qdrant: manifest.qdrant,
    embedding: manifest.embedding,
  };
}

function validateAssetLiveAuditReceipt(receipt, options = {}) {
  const manifest = validateAssetSnapshotManifest(options.manifest);
  const manifestBytes = options.manifestBytes;
  const receiptBytes = options.receiptBytes;
  const expectedReceiptSha256 = options.expectedReceiptSha256;
  if (!Buffer.isBuffer(manifestBytes) || manifestBytes.length < 2
      || !Buffer.isBuffer(receiptBytes) || receiptBytes.length < 2) {
    fail("ASSET_LIVE_AUDIT_RECEIPT_INVALID", "Exact manifest and receipt bytes are required");
  }
  const parsedManifest = parseExactJsonBytes(manifestBytes, "manifest", validateAssetSnapshotManifest);
  if (canonicalJson(parsedManifest) !== canonicalJson(manifest)) {
    fail("ASSET_LIVE_AUDIT_MANIFEST_MISMATCH", "Manifest bytes do not match the validated manifest");
  }
  const parsedReceipt = parseExactJsonBytes(receiptBytes, "receipt", (value) => value);
  if (canonicalJson(parsedReceipt) !== canonicalJson(receipt)) {
    fail("ASSET_LIVE_AUDIT_RECEIPT_INVALID", "Receipt bytes do not match the validated receipt");
  }
  if (typeof expectedReceiptSha256 !== "string" || !SHA256.test(expectedReceiptSha256)
      || sha256(receiptBytes) !== expectedReceiptSha256) {
    fail("ASSET_LIVE_AUDIT_RECEIPT_DIGEST_MISMATCH", "Live-audit receipt digest does not match the deployment pin");
  }
  object(receipt, "receipt", [
    "schema", "snapshot_id", "manifest_sha256", "observations_sha256",
    "issued_at", "expires_at", "ttl_seconds", "observations",
  ]);
  if (receipt.schema !== LIVE_AUDIT_SCHEMA) {
    fail("ASSET_LIVE_AUDIT_RECEIPT_INVALID", "Unsupported live-audit receipt schema", { field: "schema" });
  }
  if (receipt.snapshot_id !== manifest.snapshot_id) {
    fail("ASSET_LIVE_AUDIT_SNAPSHOT_MISMATCH", "Live-audit receipt is bound to a different snapshot");
  }
  if (typeof receipt.manifest_sha256 !== "string" || !SHA256.test(receipt.manifest_sha256)
      || receipt.manifest_sha256 !== sha256(manifestBytes)) {
    fail("ASSET_LIVE_AUDIT_MANIFEST_MISMATCH", "Live-audit receipt does not match the exact manifest file");
  }
  const observations = liveObservations(manifest);
  object(receipt.observations, "receipt.observations", [
    "asset_snapshot_revision", "ue_content_revision", "catalog", "postgres", "qdrant", "embedding",
  ]);
  if (canonicalJson(receipt.observations) !== canonicalJson(observations)
      || typeof receipt.observations_sha256 !== "string"
      || !SHA256.test(receipt.observations_sha256)
      || receipt.observations_sha256 !== sha256(Buffer.from(canonicalJson(observations), "utf8"))) {
    fail("ASSET_LIVE_AUDIT_OBSERVATIONS_MISMATCH", "Live-audit observations do not match the snapshot manifest");
  }
  if (!Number.isSafeInteger(receipt.ttl_seconds)
      || receipt.ttl_seconds < 1 || receipt.ttl_seconds > MAX_RECEIPT_TTL_SECONDS) {
    fail("ASSET_LIVE_AUDIT_RECEIPT_INVALID", "Live-audit receipt TTL is invalid", { field: "ttl_seconds" });
  }
  const issuedAt = utcSecond(receipt.issued_at, "issued_at");
  const expiresAt = utcSecond(receipt.expires_at, "expires_at");
  if (expiresAt - issuedAt !== receipt.ttl_seconds * 1_000) {
    fail("ASSET_LIVE_AUDIT_RECEIPT_INVALID", "Live-audit receipt validity window is inconsistent");
  }
  const clock = typeof options.clock === "function" ? options.clock : () => new Date();
  let now;
  try {
    const value = clock();
    now = value instanceof Date ? value.getTime() : new Date(value).getTime();
  } catch (_error) {
    now = NaN;
  }
  if (!Number.isFinite(now)) fail("ASSET_LIVE_AUDIT_CLOCK_INVALID", "Live-audit clock is invalid");
  if (issuedAt > now + MAX_CLOCK_SKEW_MS) {
    fail("ASSET_LIVE_AUDIT_NOT_YET_VALID", "Live-audit receipt was issued in the future");
  }
  if (expiresAt <= now) fail("ASSET_LIVE_AUDIT_EXPIRED", "Live-audit receipt has expired");
  return receipt;
}

function cause(code, dependency, message) {
  return Object.freeze({ code, dependency, message, retryable: false });
}

function auditAssetSnapshot({ manifest, catalogBytes, observed = {} } = {}) {
  validateAssetSnapshotManifest(manifest);
  if (!observed || typeof observed !== "object" || Array.isArray(observed)) fail("ASSET_SNAPSHOT_AUDIT_INVALID", "observed counts must be an object");
  const causes = [];
  if (catalogBytes !== undefined) {
    const buffer = Buffer.isBuffer(catalogBytes) ? catalogBytes : Buffer.from(String(catalogBytes));
    if (sha256(buffer) !== manifest.catalog.sha256) causes.push(cause("ASSET_CATALOG_CHECKSUM_MISMATCH", "catalog", "Catalog checksum does not match the snapshot manifest."));
  } else {
    causes.push(cause("ASSET_CATALOG_NOT_AUDITED", "catalog", "Catalog bytes were not supplied to the read-only audit."));
  }
  const checks = [
    ["catalog_count", manifest.catalog.count, "ASSET_CATALOG_COUNT_MISMATCH", "catalog"],
    ["postgres_row_count", manifest.postgres.row_count, "ASSET_POSTGRES_COUNT_MISMATCH", "postgres"],
    ["qdrant_point_count", manifest.qdrant.point_count, "ASSET_QDRANT_COUNT_MISMATCH", "qdrant"],
    ["postgres_schema_version", manifest.postgres.schema_version, "ASSET_POSTGRES_SCHEMA_MISMATCH", "postgres"],
    ["qdrant_dense_size", manifest.qdrant.dense_size, "ASSET_QDRANT_VECTOR_SIZE_MISMATCH", "qdrant"],
  ];
  for (const [field, expected, code, dependency] of checks) {
    if (!Number.isSafeInteger(observed[field])) {
      causes.push(cause("ASSET_DEPENDENCY_NOT_AUDITED", dependency, `${field} was not supplied to the read-only audit.`));
    } else if (observed[field] !== expected) {
      causes.push(cause(code, dependency, `${field} does not match the snapshot manifest.`));
    }
  }
  for (const [field, expected, code, dependency] of [
    ["qdrant_collection", manifest.qdrant.collection, "ASSET_QDRANT_COLLECTION_MISMATCH", "qdrant"],
    ["embedding_version", manifest.embedding.version, "ASSET_EMBEDDING_VERSION_MISMATCH", "embedding"],
    ["ue_content_revision", manifest.ue_content_revision, "ASSET_UE_CONTENT_REVISION_MISMATCH", "unreal"],
  ]) {
    if (typeof observed[field] !== "string" || !observed[field]) {
      causes.push(cause("ASSET_DEPENDENCY_NOT_AUDITED", dependency, `${field} was not supplied to the read-only audit.`));
    } else if (observed[field] !== expected) {
      causes.push(cause(code, dependency, `${field} does not match the snapshot manifest.`));
    }
  }
  const uniqueCauses = [...new Map(causes.map((item) => [`${item.code}:${item.dependency}:${item.message}`, item])).values()];
  return Object.freeze({
    schema: "simworld-asset-snapshot-audit/v1",
    snapshot_id: manifest.snapshot_id,
    status: uniqueCauses.length ? "not_ready" : "ready",
    causes: Object.freeze(uniqueCauses),
    revision: Object.freeze({
      ue_content_revision: manifest.ue_content_revision,
      catalog_count: manifest.catalog.count,
      postgres_row_count: manifest.postgres.row_count,
      qdrant_point_count: manifest.qdrant.point_count,
      embedding_version: manifest.embedding.version,
    }),
  });
}

module.exports = {
  ASSET_SCHEMA_VERSION,
  LIVE_AUDIT_SCHEMA,
  SNAPSHOT_SCHEMA,
  AssetSnapshotError,
  auditAssetSnapshot,
  canonicalJson,
  validateAssetLiveAuditReceipt,
  validateAssetSnapshotManifest,
};

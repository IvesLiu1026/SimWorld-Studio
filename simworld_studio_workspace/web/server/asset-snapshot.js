"use strict";

const crypto = require("node:crypto");

const SNAPSHOT_SCHEMA = "simworld-asset-snapshot/v1";
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,159}$/;
const SHA256 = /^[a-f0-9]{64}$/;

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

function count(value, field) {
  if (!Number.isSafeInteger(value) || value < 1 || value > 1_000_000_000) {
    fail("ASSET_SNAPSHOT_INVALID", `${field} must be a positive integer`, { field });
  }
  return value;
}

function validateAssetSnapshotManifest(manifest) {
  object(manifest, "manifest", ["schema", "snapshot_id", "ue_content_revision", "catalog", "postgres", "qdrant", "embedding"]);
  if (manifest.schema !== SNAPSHOT_SCHEMA) fail("ASSET_SNAPSHOT_INVALID", "Unsupported asset snapshot schema", { field: "schema" });
  id(manifest.snapshot_id, "snapshot_id");
  id(manifest.ue_content_revision, "ue_content_revision");
  object(manifest.catalog, "catalog", ["count", "sha256"]);
  count(manifest.catalog.count, "catalog.count");
  if (typeof manifest.catalog.sha256 !== "string" || !SHA256.test(manifest.catalog.sha256)) fail("ASSET_SNAPSHOT_INVALID", "catalog.sha256 is invalid", { field: "catalog.sha256" });
  object(manifest.postgres, "postgres", ["schema_version", "row_count"]);
  count(manifest.postgres.schema_version, "postgres.schema_version");
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
  SNAPSHOT_SCHEMA,
  AssetSnapshotError,
  auditAssetSnapshot,
  validateAssetSnapshotManifest,
};

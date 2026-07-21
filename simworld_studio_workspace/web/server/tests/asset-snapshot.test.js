"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");

const {
  auditAssetSnapshot,
  canonicalJson,
  validateAssetLiveAuditReceipt,
  validateAssetSnapshotManifest,
} = require("../asset-snapshot");

const catalog = Buffer.from("curated asset catalog\n");
const manifest = Object.freeze({
  schema: "simworld-asset-snapshot/v1",
  snapshot_id: "ue58-assets-2026-07-14",
  ue_content_revision: "ue58-content-r1",
  catalog: { count: 16000, sha256: crypto.createHash("sha256").update(catalog).digest("hex") },
  postgres: { schema_version: 2, row_count: 16000 },
  qdrant: { collection: "assets_ue58_qwen", point_count: 16000, dense_name: "text_dense", dense_size: 1024, sparse_name: "text_sparse" },
  embedding: { version: "qwen-bm25-v1", dense_model: "Qwen/Qwen3-Embedding", sparse_model: "bm25-v1" },
});

function observed(overrides = {}) {
  return {
    catalog_count: 16000,
    postgres_row_count: 16000,
    qdrant_point_count: 16000,
    postgres_schema_version: 2,
    qdrant_dense_size: 1024,
    qdrant_collection: "assets_ue58_qwen",
    embedding_version: "qwen-bm25-v1",
    ue_content_revision: "ue58-content-r1",
    ...overrides,
  };
}

test("validated snapshot becomes ready only when every observed revision and count matches", () => {
  assert.equal(validateAssetSnapshotManifest(manifest), manifest);
  assert.deepEqual(auditAssetSnapshot({ manifest, catalogBytes: catalog, observed: observed() }), {
    schema: "simworld-asset-snapshot-audit/v1",
    snapshot_id: "ue58-assets-2026-07-14",
    status: "ready",
    causes: [],
    revision: {
      ue_content_revision: "ue58-content-r1",
      catalog_count: 16000,
      postgres_row_count: 16000,
      qdrant_point_count: 16000,
      embedding_version: "qwen-bm25-v1",
    },
  });
});

test("audit reports independent checksum, count, vector, embedding, and UE revision causes", () => {
  const result = auditAssetSnapshot({
    manifest,
    catalogBytes: "tampered",
    observed: observed({
      postgres_row_count: 15999,
      qdrant_point_count: 15000,
      qdrant_dense_size: 768,
      embedding_version: "other",
      ue_content_revision: "other",
    }),
  });
  assert.equal(result.status, "not_ready");
  assert.deepEqual(new Set(result.causes.map((item) => item.code)), new Set([
    "ASSET_CATALOG_CHECKSUM_MISMATCH",
    "ASSET_POSTGRES_COUNT_MISMATCH",
    "ASSET_QDRANT_COUNT_MISMATCH",
    "ASSET_QDRANT_VECTOR_SIZE_MISMATCH",
    "ASSET_EMBEDDING_VERSION_MISMATCH",
    "ASSET_UE_CONTENT_REVISION_MISMATCH",
  ]));
});

test("missing observations fail closed instead of treating config presence as health", () => {
  const result = auditAssetSnapshot({ manifest });
  assert.equal(result.status, "not_ready");
  assert.ok(result.causes.some((item) => item.code === "ASSET_CATALOG_NOT_AUDITED"));
  assert.ok(result.causes.filter((item) => item.code === "ASSET_DEPENDENCY_NOT_AUDITED").length >= 3);
});

test("manifest validation is strict and rejects weak or partial snapshots", () => {
  assert.throws(() => validateAssetSnapshotManifest({ ...manifest, extra: true }), /invalid shape/);
  assert.throws(() => validateAssetSnapshotManifest({ ...manifest, schema: "asset-snapshot/v1" }), /Unsupported/);
  assert.throws(() => validateAssetSnapshotManifest({ ...manifest, catalog: { ...manifest.catalog, count: 0 } }), /positive integer/);
  assert.throws(() => validateAssetSnapshotManifest({ ...manifest, catalog: { ...manifest.catalog, sha256: "bad" } }), /sha256/);
  assert.throws(() => validateAssetSnapshotManifest({ ...manifest, postgres: { ...manifest.postgres, schema_version: 1 } }), /unsupported/);
});

test("live audit receipt binds exact raw files, observations, deployment digest, and expiry", () => {
  const manifestBytes = Buffer.from(`${JSON.stringify(manifest, null, 2)}\n`, "utf8");
  const observations = {
    asset_snapshot_revision: manifest.snapshot_id,
    ue_content_revision: manifest.ue_content_revision,
    catalog: manifest.catalog,
    postgres: manifest.postgres,
    qdrant: manifest.qdrant,
    embedding: manifest.embedding,
  };
  const receipt = {
    schema: "simworld-asset-live-audit/v1",
    snapshot_id: manifest.snapshot_id,
    manifest_sha256: crypto.createHash("sha256").update(manifestBytes).digest("hex"),
    observations_sha256: crypto.createHash("sha256").update(canonicalJson(observations), "utf8").digest("hex"),
    issued_at: "2026-07-21T01:00:00Z",
    expires_at: "2026-07-21T01:05:00Z",
    ttl_seconds: 300,
    observations,
  };
  const receiptBytes = Buffer.from(`${JSON.stringify(receipt, null, 2)}\n`, "utf8");
  const receiptSha256 = crypto.createHash("sha256").update(receiptBytes).digest("hex");
  const options = {
    manifest,
    manifestBytes,
    receiptBytes,
    expectedReceiptSha256: receiptSha256,
    clock: () => new Date("2026-07-21T01:04:59Z"),
  };
  assert.equal(validateAssetLiveAuditReceipt(receipt, options), receipt);
  assert.throws(
    () => validateAssetLiveAuditReceipt(receipt, { ...options, expectedReceiptSha256: "0".repeat(64) }),
    (error) => error.code === "ASSET_LIVE_AUDIT_RECEIPT_DIGEST_MISMATCH",
  );
  assert.throws(
    () => validateAssetLiveAuditReceipt(receipt, { ...options, clock: () => new Date("2026-07-21T01:05:00Z") }),
    (error) => error.code === "ASSET_LIVE_AUDIT_EXPIRED",
  );
  const changed = { ...receipt, observations: { ...receipt.observations, asset_snapshot_revision: "other" } };
  const changedBytes = Buffer.from(`${JSON.stringify(changed, null, 2)}\n`, "utf8");
  assert.throws(
    () => validateAssetLiveAuditReceipt(changed, {
      ...options,
      receiptBytes: changedBytes,
      expectedReceiptSha256: crypto.createHash("sha256").update(changedBytes).digest("hex"),
    }),
    (error) => error.code === "ASSET_LIVE_AUDIT_OBSERVATIONS_MISMATCH",
  );
});

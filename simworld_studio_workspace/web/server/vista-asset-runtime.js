"use strict";

const fs = require("node:fs");
const path = require("node:path");

const { searchAssets: defaultSearchAssets } = require("./asset-retrieval-db");
const { resolveRuntimeSecret } = require("./asset-runtime-secret");
const {
  validateAssetLiveAuditReceipt,
  validateAssetSnapshotManifest,
} = require("./asset-snapshot");
const { createVistaAssetResolver } = require("./vista-asset-resolver");

const MAX_MANIFEST_BYTES = 1024 * 1024;
const MAX_RECEIPT_BYTES = 1024 * 1024;
const SHA256 = /^[a-f0-9]{64}$/;

function flag(value) {
  return /^(?:1|true|yes|on)$/i.test(String(value || "").trim());
}

function requireText(value, field) {
  const text = typeof value === "string" ? value.trim() : "";
  if (!text) throw new TypeError(`${field} is required when VISTA asset resolution is enabled`);
  return text;
}

function requireOrigin(value, field) {
  const text = requireText(value, field);
  let url;
  try {
    url = new URL(text);
  } catch (_error) {
    throw new TypeError(`${field} must be an HTTP(S) origin`);
  }
  if (!new Set(["http:", "https:"]).has(url.protocol) || !url.hostname
      || url.username || url.password || url.search || url.hash || (url.pathname && url.pathname !== "/")) {
    throw new TypeError(`${field} must be an HTTP(S) origin without credentials, path, query, or fragment`);
  }
  return url.origin;
}

function requireAbsoluteFile(value, field) {
  const text = requireText(value, field);
  if (!path.isAbsolute(text)) throw new TypeError(`${field} must be an absolute file path`);
  const resolved = path.resolve(text);
  if (resolved === path.parse(resolved).root) throw new TypeError(`${field} cannot be a filesystem root`);
  return resolved;
}

function readBoundedFile(file, maximum, label, fsImpl = fs) {
  const descriptor = fsImpl.openSync(file, fs.constants.O_RDONLY | Number(fs.constants.O_NOFOLLOW || 0));
  try {
    const before = fsImpl.fstatSync(descriptor);
    if (!before.isFile() || before.size < 2 || before.size > maximum) {
      throw new TypeError(`${label} must be a bounded regular file`);
    }
    const bytes = fsImpl.readFileSync(descriptor);
    const after = fsImpl.fstatSync(descriptor);
    const buffer = Buffer.isBuffer(bytes) ? bytes : Buffer.from(bytes);
    if (buffer.length !== before.size || after.size !== before.size
        || after.mtimeMs !== before.mtimeMs || after.ctimeMs !== before.ctimeMs) {
      throw new TypeError(`${label} changed while it was being read`);
    }
    return buffer;
  } finally {
    fsImpl.closeSync(descriptor);
  }
}

function parseJson(bytes, label) {
  try {
    return JSON.parse(bytes.toString("utf8"));
  } catch (_error) {
    throw new TypeError(`${label} must contain valid JSON`);
  }
}

function requireConfidence(value) {
  const confidence = value === undefined || value === "" ? 0.7 : Number(value);
  if (!Number.isFinite(confidence) || confidence < 0 || confidence > 1) {
    throw new TypeError("VISTA_ASSET_MIN_CONFIDENCE must be a number from 0 to 1");
  }
  return confidence;
}

function requirePositiveInt(value, fallback, field, maximum) {
  const parsed = value === undefined || value === "" ? fallback : Number(value);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > maximum) {
    throw new TypeError(`${field} must be an integer from 1 to ${maximum}`);
  }
  return parsed;
}

function resolvePostgresUrl(env, fsImpl) {
  const { value } = resolveRuntimeSecret(env, "POSTGRES_URL", "POSTGRES_URL_FILE", {
    fsImpl,
    required: true,
  });
  let parsed;
  try { parsed = new URL(value); } catch (_error) {
    throw new TypeError("PostgreSQL DSN is invalid");
  }
  if (!new Set(["postgres:", "postgresql:"]).has(parsed.protocol) || !parsed.hostname || !parsed.pathname) {
    throw new TypeError("PostgreSQL DSN is invalid");
  }
  return value;
}

function resolveVistaAssetRuntimeConfig(env = process.env, options = {}) {
  const enabled = flag(env.VISTA_ASSET_RESOLUTION_ENABLED);
  if (!enabled) return Object.freeze({ enabled: false, manifest: null, snapshotId: null });

  const fsImpl = options.fsImpl || fs;
  const manifestPath = requireAbsoluteFile(env.ASSET_SNAPSHOT_MANIFEST, "ASSET_SNAPSHOT_MANIFEST");
  const manifestBytes = readBoundedFile(manifestPath, MAX_MANIFEST_BYTES, "ASSET_SNAPSHOT_MANIFEST", fsImpl);
  const manifest = validateAssetSnapshotManifest(parseJson(manifestBytes, "ASSET_SNAPSHOT_MANIFEST"));
  const liveAuditReceiptPath = requireAbsoluteFile(
    env.ASSET_LIVE_AUDIT_RECEIPT,
    "ASSET_LIVE_AUDIT_RECEIPT",
  );
  const liveAuditReceiptBytes = readBoundedFile(
    liveAuditReceiptPath,
    MAX_RECEIPT_BYTES,
    "ASSET_LIVE_AUDIT_RECEIPT",
    fsImpl,
  );
  const liveAuditReceipt = parseJson(liveAuditReceiptBytes, "ASSET_LIVE_AUDIT_RECEIPT");
  const liveAuditReceiptSha256 = requireText(
    env.ASSET_LIVE_AUDIT_RECEIPT_SHA256,
    "ASSET_LIVE_AUDIT_RECEIPT_SHA256",
  ).toLowerCase();
  if (!SHA256.test(liveAuditReceiptSha256)) {
    throw new TypeError("ASSET_LIVE_AUDIT_RECEIPT_SHA256 must be a lowercase SHA-256 digest");
  }
  const snapshotRevision = requireText(env.ASSET_SNAPSHOT_REVISION, "ASSET_SNAPSHOT_REVISION");
  const verifiedRevision = requireText(env.ASSET_READINESS_VERIFIED_REVISION, "ASSET_READINESS_VERIFIED_REVISION");
  if (snapshotRevision !== manifest.snapshot_id || verifiedRevision !== manifest.snapshot_id) {
    throw new TypeError("Configured and verified asset snapshot revisions must match the manifest");
  }
  if (manifest.catalog.count !== manifest.postgres.row_count || manifest.catalog.count !== manifest.qdrant.point_count) {
    throw new TypeError("Asset snapshot catalog, PostgreSQL, and Qdrant counts must match");
  }
  const qdrantCollection = requireText(env.QDRANT_COLLECTION, "QDRANT_COLLECTION");
  const embeddingVersion = requireText(env.EMBED_VERSION, "EMBED_VERSION");
  const ueContentRevision = requireText(env.VISTA_UE_CONTENT_REVISION, "VISTA_UE_CONTENT_REVISION");
  if (qdrantCollection !== manifest.qdrant.collection) throw new TypeError("QDRANT_COLLECTION does not match the asset snapshot");
  if (embeddingVersion !== manifest.embedding.version) throw new TypeError("EMBED_VERSION does not match the asset snapshot");
  if (ueContentRevision !== manifest.ue_content_revision) throw new TypeError("VISTA_UE_CONTENT_REVISION does not match the asset snapshot");
  const postgresUrl = resolvePostgresUrl(env, fsImpl);
  const qdrantApiKey = resolveRuntimeSecret(
    env,
    "QDRANT_API_KEY",
    "QDRANT_API_KEY_FILE",
    { fsImpl, required: true, minimumBytes: 32 },
  ).value;
  const embedServiceToken = resolveRuntimeSecret(
    env,
    "EMBED_SERVICE_TOKEN",
    "EMBED_SERVICE_TOKEN_FILE",
    { fsImpl, required: true, minimumBytes: 32 },
  ).value;

  const clock = typeof options.clock === "function" ? options.clock : () => new Date();
  const assertLiveAuditFresh = () => validateAssetLiveAuditReceipt(liveAuditReceipt, {
    manifest,
    manifestBytes,
    receiptBytes: liveAuditReceiptBytes,
    expectedReceiptSha256: liveAuditReceiptSha256,
    clock,
  });
  assertLiveAuditFresh();

  const config = {
    enabled: true,
    manifest,
    manifestPath,
    liveAuditReceiptPath,
    liveAuditReceiptSha256,
    liveAuditExpiresAt: liveAuditReceipt.expires_at,
    snapshotId: manifest.snapshot_id,
    qdrantUrl: requireOrigin(env.QDRANT_URL, "QDRANT_URL"),
    qdrantCollection,
    embedServiceUrl: requireOrigin(env.EMBED_SERVICE_URL, "EMBED_SERVICE_URL"),
    embeddingVersion,
    ueContentRevision,
    minConfidence: requireConfidence(env.VISTA_ASSET_MIN_CONFIDENCE),
    maxCandidates: requirePositiveInt(env.VISTA_ASSET_MAX_CANDIDATES, 8, "VISTA_ASSET_MAX_CANDIDATES", 40),
    timeoutMs: requirePositiveInt(env.VISTA_ASSET_SEARCH_TIMEOUT_MS, 5_000, "VISTA_ASSET_SEARCH_TIMEOUT_MS", 60_000),
    totalTimeoutMs: requirePositiveInt(env.VISTA_ASSET_TOTAL_TIMEOUT_MS, 30_000, "VISTA_ASSET_TOTAL_TIMEOUT_MS", 120_000),
    assertLiveAuditFresh,
  };
  Object.defineProperties(config, {
    postgresUrl: { value: postgresUrl, enumerable: false },
    qdrantApiKey: { value: qdrantApiKey, enumerable: false },
    embedServiceToken: { value: embedServiceToken, enumerable: false },
  });
  return Object.freeze(config);
}

function normalizeRetrievalCandidates(raw, snapshotId) {
  const assets = Array.isArray(raw) ? raw : [];
  const source = assets.retrieval && assets.retrieval.source;
  const candidates = assets.map((asset) => {
    const confidence = asset && (asset.confidence ?? asset.score ?? asset._score);
    if (!asset || asset.asset_snapshot_revision !== snapshotId) {
      const error = new Error("Semantic asset retrieval returned a candidate from a different asset snapshot");
      error.code = "VISTA_ASSET_SNAPSHOT_MISMATCH";
      error.retryable = false;
      throw error;
    }
    if (!asset || typeof confidence !== "number" || !Number.isFinite(confidence) || confidence < 0 || confidence > 1) {
      const error = new Error("Semantic asset retrieval returned an uncalibrated confidence score");
      error.code = "VISTA_ASSET_SEARCH_SCORE_INVALID";
      error.retryable = false;
      throw error;
    }
    return {
      snapshot_id: snapshotId,
      asset_id: asset.asset_id || asset.id,
      ue_path: asset.ue_path || asset.path || asset.unreal_asset_path,
      confidence,
      retrieval_source: source || "unknown",
    };
  });
  return { snapshot_id: snapshotId, candidates };
}

function createVistaAssetRuntime(options = {}) {
  const env = options.env || process.env;
  const config = options.config || resolveVistaAssetRuntimeConfig(env, {
    fsImpl: options.fsImpl,
    clock: options.clock,
  });
  if (!config.enabled) return Object.freeze({ config, resolver: null, searchAssets: null });
  const retrieval = options.searchAssets || defaultSearchAssets;
  if (typeof retrieval !== "function") throw new TypeError("searchAssets must be a function");
  const searchAssets = async (request = {}) => {
    if (typeof config.assertLiveAuditFresh !== "function") {
      throw new TypeError("Verified asset runtime requires a live-audit freshness check");
    }
    config.assertLiveAuditFresh();
    return retrieval({
      query: request.query,
      ...(request.category ? { category: request.category } : {}),
      ...(request.k ? { k: request.k } : {}),
    }, {
      signal: request.signal,
      qdrantUrl: config.qdrantUrl,
      collection: config.qdrantCollection,
      embedServiceUrl: config.embedServiceUrl,
      embeddingTimeoutMs: config.timeoutMs,
      qdrantTimeoutMs: config.timeoutMs,
      postgresTimeoutMs: config.timeoutMs,
      postgresUrl: config.postgresUrl,
      qdrantApiKey: config.qdrantApiKey,
      embedServiceToken: config.embedServiceToken,
      assetSnapshotRevision: config.snapshotId,
      assertLiveAuditFresh: config.assertLiveAuditFresh,
    });
  };
  const resolver = createVistaAssetResolver({
    snapshotId: config.snapshotId,
    minConfidence: config.minConfidence,
    maxCandidates: config.maxCandidates,
    timeoutMs: config.timeoutMs,
    totalTimeoutMs: config.totalTimeoutMs,
    searchAssets: async (request) => {
      return normalizeRetrievalCandidates(await searchAssets(request), config.snapshotId);
    },
  });
  return Object.freeze({ config, resolver, searchAssets });
}

module.exports = {
  createVistaAssetRuntime,
  normalizeRetrievalCandidates,
  resolveVistaAssetRuntimeConfig,
};

"use strict";

const fs = require("node:fs");
const path = require("node:path");

const { searchAssets: defaultSearchAssets } = require("./asset-retrieval-db");
const { validateAssetSnapshotManifest } = require("./asset-snapshot");
const { createVistaAssetResolver } = require("./vista-asset-resolver");

const MAX_MANIFEST_BYTES = 1024 * 1024;

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

function readManifest(file, fsImpl = fs) {
  const descriptor = fsImpl.openSync(file, fs.constants.O_RDONLY | Number(fs.constants.O_NOFOLLOW || 0));
  try {
    const stat = fsImpl.fstatSync(descriptor);
    if (!stat.isFile() || stat.size < 2 || stat.size > MAX_MANIFEST_BYTES) {
      throw new TypeError("ASSET_SNAPSHOT_MANIFEST must be a bounded regular file");
    }
    return validateAssetSnapshotManifest(JSON.parse(fsImpl.readFileSync(descriptor, "utf8")));
  } finally {
    fsImpl.closeSync(descriptor);
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

function resolveVistaAssetRuntimeConfig(env = process.env, options = {}) {
  const enabled = flag(env.VISTA_ASSET_RESOLUTION_ENABLED);
  if (!enabled) return Object.freeze({ enabled: false, manifest: null, snapshotId: null });

  const manifestPath = requireAbsoluteFile(env.ASSET_SNAPSHOT_MANIFEST, "ASSET_SNAPSHOT_MANIFEST");
  const manifest = readManifest(manifestPath, options.fsImpl || fs);
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
  requireText(env.POSTGRES_URL, "POSTGRES_URL");

  return Object.freeze({
    enabled: true,
    manifest,
    manifestPath,
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
  });
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
  const config = options.config || resolveVistaAssetRuntimeConfig(env, { fsImpl: options.fsImpl });
  if (!config.enabled) return Object.freeze({ config, resolver: null });
  const retrieval = options.searchAssets || defaultSearchAssets;
  if (typeof retrieval !== "function") throw new TypeError("searchAssets must be a function");
  const resolver = createVistaAssetResolver({
    snapshotId: config.snapshotId,
    minConfidence: config.minConfidence,
    maxCandidates: config.maxCandidates,
    timeoutMs: config.timeoutMs,
    totalTimeoutMs: config.totalTimeoutMs,
    searchAssets: async (request) => normalizeRetrievalCandidates(await retrieval({
      query: request.query,
      k: request.k,
    }, {
      signal: request.signal,
      qdrantUrl: config.qdrantUrl,
      collection: config.qdrantCollection,
      embedServiceUrl: config.embedServiceUrl,
      embeddingTimeoutMs: config.timeoutMs,
      qdrantTimeoutMs: config.timeoutMs,
      postgresTimeoutMs: config.timeoutMs,
      assetSnapshotRevision: config.snapshotId,
    }), config.snapshotId),
  });
  return Object.freeze({ config, resolver });
}

module.exports = {
  createVistaAssetRuntime,
  normalizeRetrievalCandidates,
  resolveVistaAssetRuntimeConfig,
};

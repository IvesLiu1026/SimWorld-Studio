"use strict";

const fs = require("node:fs");
const net = require("node:net");
const path = require("node:path");

const { createReadinessRegistry } = require("./readiness-registry");
const { resolveReviewConfig } = require("./review-provider");
const {
  readReviewSmokeReceipt,
  resolveReviewSmokeReceiptPath,
  verifyReviewSmokeReceipt,
} = require("./review-smoke-receipt");
const { validateAssetSnapshotManifest } = require("./asset-snapshot");

const FEATURE_NAMES = Object.freeze(["review", "retrieval", "streaming", "timeline"]);
const POLICY_VALUES = new Set(["required", "optional", "disabled"]);

function envFlag(value) {
  return /^(1|true|yes|on)$/i.test(String(value || "").trim());
}

function normalizeTransportProfile(value) {
  const profile = String(value || "loopback").trim().toLowerCase();
  if (["public", "public_webrtc", "public-webrtc", "webrtc"].includes(profile)) return "public_webrtc";
  return "loopback";
}

function explicitPolicy(env, feature, fallback) {
  const raw = String(env[`READINESS_${feature.toUpperCase()}_POLICY`] || "").trim().toLowerCase();
  if (!raw) return fallback;
  if (!POLICY_VALUES.has(raw)) {
    throw new TypeError(`READINESS_${feature.toUpperCase()}_POLICY must be required, optional, or disabled`);
  }
  return raw;
}

function resolveStudioFeaturePolicy(env = process.env) {
  const transportProfile = normalizeTransportProfile(
    env.STUDIO_TRANSPORT_PROFILE || env.TRANSPORT_PROFILE,
  );
  const production = String(env.NODE_ENV || "").trim().toLowerCase() === "production";
  const requireRealAssets = envFlag(
    env.ASSET_REQUIRE_REAL_ASSETS !== undefined
      ? env.ASSET_REQUIRE_REAL_ASSETS
      : env.REQUIRE_REAL_ASSETS,
  );
  return Object.freeze({
    review: explicitPolicy(env, "review", production ? "required" : "optional"),
    retrieval: explicitPolicy(env, "retrieval", requireRealAssets ? "required" : "optional"),
    streaming: explicitPolicy(env, "streaming", transportProfile === "public_webrtc" ? "required" : "optional"),
    timeline: explicitPolicy(env, "timeline", envFlag(env.TIMELINE_REQUIRED) ? "required" : "optional"),
  });
}

function publicCause(code, message, retryable, dependency) {
  return {
    code,
    message,
    retryable: retryable === true,
    ...(dependency ? { dependency } : {}),
  };
}

function executableExists(command, env = process.env, fsImpl = fs) {
  const candidate = String(command || "").trim();
  if (!candidate) return false;
  const paths = candidate.includes(path.sep)
    ? [candidate]
    : String(env.PATH || "").split(path.delimiter).filter(Boolean).map((dir) => path.join(dir, candidate));
  return paths.some((file) => {
    try {
      fsImpl.accessSync(file, fs.constants.X_OK);
      return true;
    } catch (_error) {
      return false;
    }
  });
}

function reviewReceiptFailureStatus(env) {
  return String(env.NODE_ENV || "").trim().toLowerCase() === "production"
    ? "not_ready"
    : "degraded";
}

function createReviewReadinessProbe({
  env = process.env,
  claudeBin,
  fsImpl = fs,
  now = Date.now,
  sourceRevision,
} = {}) {
  return async function probeReview() {
    let config;
    try {
      config = resolveReviewConfig({
        provider: env.CRITIC_PROVIDER || "claude",
        model: env.CRITIC_MODEL || env.CLAUDE_MODEL || "claude-opus-4-8",
      }, env);
    } catch (_error) {
      return {
        status: "not_ready",
        causes: [publicCause(
          "REVIEW_CONFIG_INVALID",
          "The review provider or model configuration is invalid.",
          false,
          "review_provider",
        )],
      };
    }

    const revision = {
      provider: config.provider,
      model: config.model,
      source_revision: String(sourceRevision || env.SIMWORLD_BUILD_REVISION || "").trim() || null,
    };
    const production = String(env.NODE_ENV || "").trim().toLowerCase() === "production";
    const demoOverride = envFlag(env.REVIEW_READINESS_DEMO_OVERRIDE);
    const testOverride = envFlag(env.REVIEW_READINESS_TEST_OVERRIDE);
    if (demoOverride || testOverride) {
      const allowed = !production && (demoOverride || String(env.NODE_ENV || "").trim().toLowerCase() === "test");
      if (!allowed) {
        return {
          status: "not_ready",
          revision,
          causes: [publicCause(
            "REVIEW_READINESS_OVERRIDE_FORBIDDEN",
            "Review readiness overrides are forbidden in production and test overrides require NODE_ENV=test.",
            false,
            "review_provider",
          )],
        };
      }
      return {
        status: "ready",
        revision: {
          ...revision,
          verification: demoOverride ? "demo_override" : "test_override",
        },
        causes: [],
      };
    }

    if (!executableExists(claudeBin || env.CLAUDE_BIN || "claude", env, fsImpl)) {
      return {
        status: "not_ready",
        revision,
        causes: [publicCause(
          "REVIEW_PROVIDER_EXECUTABLE_MISSING",
          "The configured review provider executable is unavailable.",
          false,
          "review_provider",
        )],
      };
    }

    if (!revision.source_revision || revision.source_revision === "working-tree") {
      return {
        status: reviewReceiptFailureStatus(env),
        revision,
        causes: [publicCause(
          "REVIEW_SOURCE_REVISION_UNPINNED",
          "A concrete SIMWORLD_BUILD_REVISION is required to verify a review provider smoke receipt.",
          false,
          "review_provider",
        )],
      };
    }

    const expectedType = String(env.REVIEW_SMOKE_RECEIPT_TYPE || "").trim().toLowerCase();
    if (expectedType && expectedType !== "text" && expectedType !== "visual") {
      return {
        status: "not_ready",
        revision,
        causes: [publicCause(
          "REVIEW_SMOKE_TYPE_INVALID",
          "REVIEW_SMOKE_RECEIPT_TYPE must be text or visual when configured.",
          false,
          "review_provider",
        )],
      };
    }

    let receipt;
    try {
      receipt = readReviewSmokeReceipt(resolveReviewSmokeReceiptPath(env), { fsImpl });
      verifyReviewSmokeReceipt(receipt, {
        provider: config.provider,
        model: config.model,
        sourceRevision: revision.source_revision,
        ...(expectedType ? { reviewType: expectedType } : {}),
        now,
      });
    } catch (error) {
      const legacyOverride = envFlag(env.REVIEW_READINESS_VERIFIED);
      const knownCode = error && typeof error.code === "string" && /^REVIEW_SMOKE_[A-Z0-9_]+$/.test(error.code)
        ? error.code
        : "REVIEW_SMOKE_RECEIPT_INVALID";
      const code = legacyOverride && knownCode === "REVIEW_SMOKE_RECEIPT_UNAVAILABLE"
        ? "REVIEW_LEGACY_OVERRIDE_REJECTED"
        : knownCode;
      const messages = {
        REVIEW_LEGACY_OVERRIDE_REJECTED: "REVIEW_READINESS_VERIFIED is legacy metadata and cannot replace a provider smoke receipt.",
        REVIEW_SMOKE_RECEIPT_UNAVAILABLE: "A real tool-free provider smoke receipt is not available.",
        REVIEW_SMOKE_RECEIPT_EXPIRED: "The review provider smoke receipt has expired.",
        REVIEW_SMOKE_RECEIPT_MISMATCH: "The review provider smoke receipt does not match the running provider, model, source revision, or review type.",
        REVIEW_SMOKE_RECEIPT_NOT_YET_VALID: "The review provider smoke receipt timestamp is not yet valid.",
        REVIEW_SMOKE_SCENE_MUTATED: "The Visual Review smoke changed the scene digest and is not read-only.",
        REVIEW_SMOKE_VERDICT_FAILED: "The review provider smoke verdict did not pass.",
        REVIEW_SMOKE_RECEIPT_SENSITIVE: "The review provider smoke receipt contains forbidden credential-like material.",
        REVIEW_SMOKE_RECEIPT_INVALID: "The review provider smoke receipt is invalid.",
      };
      return {
        status: reviewReceiptFailureStatus(env),
        revision,
        causes: [publicCause(
          code,
          messages[code] || messages.REVIEW_SMOKE_RECEIPT_INVALID,
          code === "REVIEW_SMOKE_RECEIPT_UNAVAILABLE",
          "review_provider",
        )],
      };
    }

    return {
      status: "ready",
      revision: {
        ...revision,
        verification: "provider_smoke_receipt",
        receipt_id: receipt.receipt_id,
        review_type: receipt.review_type,
        cli_name: receipt.cli.name,
        cli_version: receipt.cli.version,
        recorded_at: receipt.recorded_at,
        expires_at: receipt.expires_at,
      },
      causes: [],
    };
  };
}

function safeReadJson(file, fsImpl) {
  return JSON.parse(fsImpl.readFileSync(file, "utf8"));
}

function createRetrievalReadinessProbe({ env = process.env, fsImpl = fs } = {}) {
  return async function probeRetrieval() {
    const dataRoot = env.XDG_DATA_HOME
      || (env.HOME ? path.join(env.HOME, ".local", "share") : path.resolve(__dirname, "..", ".runtime"));
    const assetDbDir = env.ASSET_DB_DIR || path.join(dataRoot, "simworld-studio", "asset-db");
    const indexPath = path.join(assetDbDir, "category_index.json");
    const manifestPath = env.ASSET_SNAPSHOT_MANIFEST || path.join(assetDbDir, "snapshot-manifest.json");
    let index;
    let manifest;
    const causes = [];

    try {
      index = safeReadJson(indexPath, fsImpl);
      if (!index || !Array.isArray(index.categories)) throw new Error("invalid category index");
    } catch (_error) {
      causes.push(publicCause(
        "ASSET_CATALOG_UNAVAILABLE",
        "The asset catalog index is missing or invalid.",
        false,
        "catalog",
      ));
    }
    try {
      manifest = validateAssetSnapshotManifest(safeReadJson(manifestPath, fsImpl));
    } catch (_error) {
      causes.push(publicCause(
        "ASSET_SNAPSHOT_MANIFEST_MISSING",
        "A valid simworld-asset-snapshot/v1 manifest is not available.",
        false,
        "catalog",
      ));
    }

    if (!env.POSTGRES_URL) {
      causes.push(publicCause(
        "ASSET_POSTGRES_CONFIG_MISSING",
        "The PostgreSQL asset catalog connection is not configured.",
        false,
        "postgres",
      ));
    }
    if (!env.QDRANT_URL) {
      causes.push(publicCause(
        "ASSET_QDRANT_CONFIG_MISSING",
        "The Qdrant asset index connection is not configured.",
        false,
        "qdrant",
      ));
    }
    if (!env.EMBED_SERVICE_URL) {
      causes.push(publicCause(
        "ASSET_EMBED_CONFIG_MISSING",
        "The embedding service is not configured.",
        false,
        "embedding",
      ));
    }

    const categoryCount = index && Array.isArray(index.categories) ? index.categories.length : null;
    const catalogCount = index && Array.isArray(index.categories)
      ? index.categories.reduce((sum, category) => sum + Number(category && category.count || 0), 0)
      : null;
    if (manifest && catalogCount !== manifest.catalog.count) {
      causes.push(publicCause(
        "ASSET_SNAPSHOT_COUNT_MISMATCH",
        "The asset catalog count does not match the snapshot manifest.",
        false,
        "catalog",
      ));
    }
    if (manifest && (manifest.catalog.count !== manifest.postgres.row_count
      || manifest.catalog.count !== manifest.qdrant.point_count)) {
      causes.push(publicCause(
        "ASSET_SNAPSHOT_DEPENDENCY_COUNT_MISMATCH",
        "The catalog, PostgreSQL, and Qdrant counts in the snapshot manifest are inconsistent.",
        false,
        "asset_stack",
      ));
    }
    if (manifest && env.QDRANT_COLLECTION && env.QDRANT_COLLECTION !== manifest.qdrant.collection) {
      causes.push(publicCause(
        "ASSET_QDRANT_COLLECTION_MISMATCH",
        "The configured Qdrant collection does not match the snapshot manifest.",
        false,
        "qdrant",
      ));
    }
    if (manifest && env.EMBED_VERSION && env.EMBED_VERSION !== manifest.embedding.version) {
      causes.push(publicCause(
        "ASSET_EMBEDDING_VERSION_MISMATCH",
        "The configured embedding version does not match the snapshot manifest.",
        false,
        "embedding",
      ));
    }

    const revision = manifest ? {
      snapshot_id: manifest.snapshot_id,
      ue_content_revision: manifest.ue_content_revision || null,
      embedding_version: manifest.embedding.version,
      catalog_count: catalogCount,
      category_count: categoryCount,
      postgres_row_count: manifest.postgres.row_count,
      qdrant_point_count: manifest.qdrant.point_count,
      qdrant_collection: manifest.qdrant.collection,
    } : {
      snapshot_id: env.ASSET_SNAPSHOT_REVISION || env.ASSET_CATALOG_REVISION || "unversioned",
      catalog_count: catalogCount,
      category_count: categoryCount,
    };
    if (causes.length) return { status: "not_ready", revision, causes };

    const verifiedRevision = String(env.ASSET_READINESS_VERIFIED_REVISION || "").trim();
    const verified = verifiedRevision && verifiedRevision === String(manifest.snapshot_id);
    return {
      status: verified ? "ready" : "degraded",
      revision,
      causes: verified ? [] : [publicCause(
        "ASSET_DEPENDENCY_HEALTH_UNVERIFIED",
        "Snapshot metadata is present, but dependency counts and health have not been verified for this revision.",
        true,
        "asset_stack",
      )],
    };
  };
}

function tcpReachable({ host, port, signal, connect = (options) => net.createConnection(options) }) {
  return new Promise((resolve) => {
    let socket;
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      if (signal) signal.removeEventListener("abort", onAbort);
      try { if (socket) socket.destroy(); } catch (_error) {}
      resolve(value);
    };
    const onAbort = () => finish(false);
    if (signal && signal.aborted) return finish(false);
    if (signal) signal.addEventListener("abort", onAbort, { once: true });
    try {
      socket = connect({ host, port });
      socket.once("connect", () => finish(true));
      socket.once("error", () => finish(false));
    } catch (_error) {
      finish(false);
    }
  });
}

function createStreamingReadinessProbe({
  env = process.env,
  host = "127.0.0.1",
  port = 8585,
  connect,
} = {}) {
  const profile = normalizeTransportProfile(env.STUDIO_TRANSPORT_PROFILE || env.TRANSPORT_PROFILE);
  return async function probeStreaming({ signal }) {
    const reachable = await tcpReachable({ host, port, signal, ...(connect ? { connect } : {}) });
    if (!reachable) {
      return {
        status: "not_ready",
        revision: { profile },
        causes: [publicCause(
          "STREAMING_SIGNALING_UNREACHABLE",
          "The local Pixel Streaming signaling endpoint is unreachable.",
          true,
          "cirrus",
        )],
      };
    }
    if (profile === "public_webrtc" && !envFlag(env.PUBLIC_WEBRTC_EXTERNAL_VERIFIED)) {
      return {
        status: "degraded",
        revision: { profile },
        causes: [publicCause(
          "TURN_RELAY_UNVERIFIED",
          "Local signaling is reachable, but public ICE/TURN relay has not passed an external verification.",
          false,
          "turn",
        )],
      };
    }
    return { status: "ready", revision: { profile }, causes: [] };
  };
}

function createTimelineReadinessProbe({ env = process.env } = {}) {
  return async function probeTimeline() {
    if (envFlag(env.TIMELINE_AUTOMATION_VERIFIED)) {
      return {
        status: "ready",
        revision: { schema: env.TIMELINE_SCHEMA_REVISION || "vista-timeline/v1" },
        causes: [],
      };
    }
    return {
      status: "not_ready",
      revision: { schema: "unimplemented" },
      causes: [publicCause(
        "TIMELINE_AUTOMATION_UNAVAILABLE",
        "The server-authoritative VISTA timeline and animation workflow is not implemented.",
        false,
        "timeline",
      )],
    };
  };
}

function createStudioReadiness({
  env = process.env,
  claudeBin,
  cirrusHost = "127.0.0.1",
  cirrusPort = 8585,
  revision,
  fsImpl = fs,
  connect,
  probeOverrides = {},
} = {}) {
  for (const name of Object.keys(probeOverrides)) {
    if (!FEATURE_NAMES.includes(name) || typeof probeOverrides[name] !== "function") {
      throw new TypeError(`Invalid readiness probe override: ${name}`);
    }
  }
  const buildRevision = revision && revision.build
    ? revision.build
    : (env.SIMWORLD_BUILD_REVISION || "working-tree");
  const probes = {
    review: probeOverrides.review || createReviewReadinessProbe({
      env,
      claudeBin,
      fsImpl,
      sourceRevision: buildRevision,
    }),
    retrieval: probeOverrides.retrieval || createRetrievalReadinessProbe({ env, fsImpl }),
    streaming: probeOverrides.streaming || createStreamingReadinessProbe({
      env,
      host: cirrusHost,
      port: cirrusPort,
      connect,
    }),
    timeline: probeOverrides.timeline || createTimelineReadinessProbe({ env }),
  };
  return createReadinessRegistry({
    revision: revision || { build: buildRevision },
    featurePolicy: resolveStudioFeaturePolicy(env),
    defaultTimeoutMs: 750,
    probes,
  });
}

module.exports = {
  createRetrievalReadinessProbe,
  createReviewReadinessProbe,
  createStreamingReadinessProbe,
  createStudioReadiness,
  createTimelineReadinessProbe,
  executableExists,
  normalizeTransportProfile,
  resolveStudioFeaturePolicy,
  tcpReachable,
};

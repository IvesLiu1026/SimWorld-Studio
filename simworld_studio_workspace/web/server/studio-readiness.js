"use strict";

const fs = require("node:fs");
const net = require("node:net");
const path = require("node:path");

const { resolveRuntimeSecret } = require("./asset-runtime-secret");
const { createReadinessRegistry } = require("./readiness-registry");
const { resolveReviewConfig } = require("./review-provider");
const {
  readReviewSmokeReceipt,
  resolveReviewSmokeReceiptPath,
  verifyReviewSmokeReceipt,
} = require("./review-smoke-receipt");
const {
  validateAssetLiveAuditReceipt,
  validateAssetSnapshotManifest,
} = require("./asset-snapshot");
const {
  digestJson: digestWebRtcReceipt,
  readWebRtcReadinessReceipt,
  verifyWebRtcReadinessReceipt,
} = require("./webrtc-readiness-receipt");

const FEATURE_NAMES = Object.freeze([
  "artifact_journal",
  "review",
  "retrieval",
  "streaming",
  "timeline",
  "nlp_generation",
]);
const POLICY_VALUES = new Set(["required", "optional", "disabled"]);

function envFlag(value) {
  return /^(1|true|yes|on)$/i.test(String(value || "").trim());
}

function normalizeTransportProfile(value) {
  const profile = String(value || "loopback").trim().toLowerCase();
  if (["public", "public_webrtc", "public-webrtc", "webrtc", "trusted_proxy", "trusted-proxy"].includes(profile)) {
    return "public_webrtc";
  }
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
  const artifactJournalPolicy = explicitPolicy(
    env,
    "artifact_journal",
    production
      ? "required"
      : (envFlag(env.VISTA_ARTIFACT_JOURNAL_ENABLED) || Boolean(String(env.VISTA_ARTIFACT_JOURNAL_ROOT || "").trim())
        ? "optional" : "disabled"),
  );
  if (production && artifactJournalPolicy !== "required") {
    throw new TypeError("READINESS_ARTIFACT_JOURNAL_POLICY must be required in production");
  }
  return Object.freeze({
    artifact_journal: artifactJournalPolicy,
    review: explicitPolicy(env, "review", production ? "required" : "optional"),
    retrieval: explicitPolicy(env, "retrieval", requireRealAssets ? "required" : "optional"),
    streaming: explicitPolicy(env, "streaming", transportProfile === "public_webrtc" ? "required" : "optional"),
    timeline: explicitPolicy(env, "timeline", envFlag(env.TIMELINE_REQUIRED) ? "required" : "optional"),
    nlp_generation: production ? "required" : "optional",
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
    if (!production && expectedType && expectedType !== "text" && expectedType !== "visual") {
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

    const requirements = production
      ? [
        {
          reviewType: "text",
          path: String(env.REVIEW_TEXT_SMOKE_RECEIPT_PATH || "").trim(),
          sha256: String(env.REVIEW_TEXT_SMOKE_RECEIPT_SHA256 || "").trim().toLowerCase(),
        },
        {
          reviewType: "visual",
          path: String(env.REVIEW_VISUAL_SMOKE_RECEIPT_PATH || "").trim(),
          sha256: String(env.REVIEW_VISUAL_SMOKE_RECEIPT_SHA256 || "").trim().toLowerCase(),
        },
      ]
      : [{
        reviewType: expectedType || null,
        path: resolveReviewSmokeReceiptPath(env),
        sha256: String(env.REVIEW_SMOKE_RECEIPT_SHA256 || "").trim().toLowerCase() || null,
      }];
    if (production && requirements.some((entry) => !entry.path)) {
      return {
        status: "not_ready",
        revision,
        causes: [publicCause(
          envFlag(env.REVIEW_READINESS_VERIFIED)
            ? "REVIEW_LEGACY_OVERRIDE_REJECTED"
            : "REVIEW_SMOKE_RECEIPTS_INCOMPLETE",
          envFlag(env.REVIEW_READINESS_VERIFIED)
            ? "REVIEW_READINESS_VERIFIED is legacy metadata and cannot replace Text and Visual provider receipts."
            : "Production requires separate current Text and Visual provider smoke receipts.",
          false,
          "review_provider",
        )],
      };
    }
    if (production && requirements.some((entry) => !/^[a-f0-9]{64}$/.test(entry.sha256))) {
      return {
        status: "not_ready",
        revision,
        causes: [publicCause(
          "REVIEW_SMOKE_RECEIPT_PINS_INCOMPLETE",
          "Production requires SHA-256 pins for both Text and Visual provider smoke receipts.",
          false,
          "review_provider",
        )],
      };
    }

    const receipts = [];
    try {
      for (const requirement of requirements) {
        const receipt = readReviewSmokeReceipt(requirement.path, {
          fsImpl,
          expectedSha256: requirement.sha256,
        });
        verifyReviewSmokeReceipt(receipt, {
          provider: config.provider,
          model: config.model,
          sourceRevision: revision.source_revision,
          ...(requirement.reviewType ? { reviewType: requirement.reviewType } : {}),
          now,
        });
        receipts.push(receipt);
      }
      if (production && (receipts[0].cli.name !== receipts[1].cli.name
          || receipts[0].cli.version !== receipts[1].cli.version)) {
        const mismatch = new Error("Text and Visual review smoke CLI identities differ");
        mismatch.code = "REVIEW_SMOKE_RECEIPT_MISMATCH";
        throw mismatch;
      }
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
        REVIEW_SMOKE_RECEIPTS_INCOMPLETE: "Separate Text and Visual provider smoke receipts are required.",
        REVIEW_SMOKE_RECEIPT_PINS_INCOMPLETE: "Separate Text and Visual provider smoke receipt SHA-256 pins are required.",
        REVIEW_SMOKE_RECEIPT_DIGEST_MISMATCH: "The review provider smoke receipt does not match its deployment SHA-256 pin.",
        REVIEW_SMOKE_RECEIPT_PIN_INVALID: "The review provider smoke receipt SHA-256 pin is invalid.",
        REVIEW_SMOKE_RECEIPT_CHANGED: "The review provider smoke receipt changed while it was being read.",
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
        verification: production ? "text_visual_provider_smoke_receipts" : "provider_smoke_receipt",
        receipt_ids: Object.fromEntries(receipts.map((receipt) => [receipt.review_type, receipt.receipt_id])),
        ...(requirements.some((entry) => entry.sha256) ? {
          receipt_sha256: Object.fromEntries(
            requirements
              .filter((entry) => entry.sha256)
              .map((entry, index) => [entry.reviewType || receipts[index].review_type, entry.sha256]),
          ),
        } : {}),
        review_types: receipts.map((receipt) => receipt.review_type).sort(),
        cli_name: receipts[0].cli.name,
        cli_version: receipts[0].cli.version,
        recorded_at: Object.fromEntries(receipts.map((receipt) => [receipt.review_type, receipt.recorded_at])),
        expires_at: Object.fromEntries(receipts.map((receipt) => [receipt.review_type, receipt.expires_at])),
      },
      causes: [],
    };
  };
}

function combineReviewReadinessProbes(providerProbe, evidenceProbe) {
  if (typeof providerProbe !== "function") throw new TypeError("providerProbe must be a function");
  if (typeof evidenceProbe !== "function") return providerProbe;
  return async function probeReviewWithEvidence(options = {}) {
    const provider = await providerProbe(options);
    const evidence = await evidenceProbe(options);
    const statuses = [provider && provider.status, evidence && evidence.status];
    const status = statuses.includes("not_ready")
      ? "not_ready"
      : (statuses.includes("degraded") ? "degraded" : "ready");
    return {
      status,
      revision: {
        ...(provider && provider.revision && typeof provider.revision === "object"
          ? provider.revision
          : {}),
        evidence: evidence && evidence.revision || null,
      },
      causes: [
        ...(provider && Array.isArray(provider.causes) ? provider.causes : []),
        ...(evidence && Array.isArray(evidence.causes) ? evidence.causes : []),
      ],
    };
  };
}

function safeReadBytes(file, fsImpl, maximum = 1024 * 1024) {
  const value = fsImpl.readFileSync(file);
  const bytes = Buffer.isBuffer(value) ? value : Buffer.from(String(value), "utf8");
  if (bytes.length < 2 || bytes.length > maximum) throw new Error("invalid bounded JSON file");
  return bytes;
}

function safeParseJson(bytes) {
  return JSON.parse(bytes.toString("utf8"));
}

function createRetrievalReadinessProbe({ env = process.env, fsImpl = fs, clock = () => new Date() } = {}) {
  return async function probeRetrieval() {
    const dataRoot = env.XDG_DATA_HOME
      || (env.HOME ? path.join(env.HOME, ".local", "share") : path.resolve(__dirname, "..", ".runtime"));
    const assetDbDir = env.ASSET_DB_DIR || path.join(dataRoot, "simworld-studio", "asset-db");
    const indexPath = path.join(assetDbDir, "category_index.json");
    const manifestPath = env.ASSET_SNAPSHOT_MANIFEST || path.join(assetDbDir, "snapshot-manifest.json");
    const liveAuditReceiptPath = env.ASSET_LIVE_AUDIT_RECEIPT
      || path.join(assetDbDir, "snapshot-live-audit.json");
    let index;
    let manifest;
    let manifestBytes;
    let liveAuditReceipt;
    let liveAuditReceiptBytes;
    const causes = [];

    try {
      index = safeParseJson(safeReadBytes(indexPath, fsImpl));
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
      manifestBytes = safeReadBytes(manifestPath, fsImpl);
      manifest = validateAssetSnapshotManifest(safeParseJson(manifestBytes));
    } catch (_error) {
      causes.push(publicCause(
        "ASSET_SNAPSHOT_MANIFEST_MISSING",
        "A valid simworld-asset-snapshot/v1 manifest is not available.",
        false,
        "catalog",
      ));
    }
    try {
      liveAuditReceiptBytes = safeReadBytes(liveAuditReceiptPath, fsImpl);
      liveAuditReceipt = safeParseJson(liveAuditReceiptBytes);
    } catch (_error) {
      causes.push(publicCause(
        "ASSET_LIVE_AUDIT_RECEIPT_MISSING",
        "A current asset live-audit receipt is not available.",
        true,
        "asset_stack",
      ));
    }

    try {
      resolveRuntimeSecret(env, "POSTGRES_URL", "POSTGRES_URL_FILE", {
        fsImpl,
        required: true,
      });
    } catch (_error) {
      causes.push(publicCause(
        "ASSET_POSTGRES_CONFIG_MISSING",
        "The PostgreSQL asset catalog credential is missing or invalid.",
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
    try {
      resolveRuntimeSecret(env, "QDRANT_API_KEY", "QDRANT_API_KEY_FILE", {
        fsImpl,
        required: true,
        minimumBytes: 32,
      });
    } catch (_error) {
      causes.push(publicCause(
        "ASSET_QDRANT_CREDENTIAL_MISSING",
        "The Qdrant asset index credential is missing or invalid.",
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
    try {
      resolveRuntimeSecret(env, "EMBED_SERVICE_TOKEN", "EMBED_SERVICE_TOKEN_FILE", {
        fsImpl,
        required: true,
        minimumBytes: 32,
      });
    } catch (_error) {
      causes.push(publicCause(
        "ASSET_EMBED_CREDENTIAL_MISSING",
        "The embedding service credential is missing or invalid.",
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
    const verifiedRevision = String(env.ASSET_READINESS_VERIFIED_REVISION || "").trim();
    if (!verifiedRevision || (manifest && verifiedRevision !== manifest.snapshot_id)) {
      causes.push(publicCause(
        "ASSET_READINESS_REVISION_MISMATCH",
        "The deployment readiness revision does not match the asset snapshot.",
        false,
        "asset_stack",
      ));
    }
    const liveAuditReceiptSha256 = String(env.ASSET_LIVE_AUDIT_RECEIPT_SHA256 || "").trim().toLowerCase();
    if (!/^[a-f0-9]{64}$/.test(liveAuditReceiptSha256)) {
      causes.push(publicCause(
        "ASSET_LIVE_AUDIT_RECEIPT_PIN_MISSING",
        "The deployment does not pin a valid live-audit receipt digest.",
        false,
        "asset_stack",
      ));
    }
    if (manifest && manifestBytes && liveAuditReceipt && liveAuditReceiptBytes
        && /^[a-f0-9]{64}$/.test(liveAuditReceiptSha256)) {
      try {
        validateAssetLiveAuditReceipt(liveAuditReceipt, {
          manifest,
          manifestBytes,
          receiptBytes: liveAuditReceiptBytes,
          expectedReceiptSha256: liveAuditReceiptSha256,
          clock,
        });
      } catch (error) {
        const code = error && typeof error.code === "string" && /^[A-Z0-9_]{3,100}$/.test(error.code)
          ? error.code
          : "ASSET_LIVE_AUDIT_RECEIPT_INVALID";
        causes.push(publicCause(
          code,
          code === "ASSET_LIVE_AUDIT_EXPIRED"
            ? "The asset live-audit receipt has expired."
            : "The asset live-audit receipt is invalid for this deployment.",
          code === "ASSET_LIVE_AUDIT_EXPIRED",
          "asset_stack",
        ));
      }
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
      live_audit_expires_at: liveAuditReceipt && typeof liveAuditReceipt.expires_at === "string"
        ? liveAuditReceipt.expires_at
        : null,
    } : {
      snapshot_id: env.ASSET_SNAPSHOT_REVISION || env.ASSET_CATALOG_REVISION || "unversioned",
      catalog_count: catalogCount,
      category_count: categoryCount,
    };
    if (causes.length) return { status: "not_ready", revision, causes };
    return {
      status: "ready",
      revision,
      causes: [],
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
  fsImpl = fs,
  clock = () => Date.now(),
} = {}) {
  const profile = normalizeTransportProfile(env.STUDIO_TRANSPORT_PROFILE || env.TRANSPORT_PROFILE);
  return async function probeStreaming({ signal } = {}) {
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
    if (profile === "public_webrtc") {
      const receiptPath = String(env.WEBRTC_READINESS_RECEIPT_PATH || "").trim();
      const expectedDigest = String(env.WEBRTC_READINESS_RECEIPT_SHA256 || "").trim().toLowerCase();
      const buildRevision = String(env.SIMWORLD_BUILD_REVISION || "").trim().toLowerCase();
      const deploymentFingerprint = String(env.WEBRTC_DEPLOYMENT_FINGERPRINT || "").trim().toLowerCase();
      const publicOrigin = String(env.STUDIO_PUBLIC_ORIGIN || "").trim();
      const certificateSha256 = String(env.WEBRTC_CERTIFICATE_SHA256 || "").trim().toLowerCase();
      if (!receiptPath || !/^[a-f0-9]{64}$/.test(expectedDigest)
          || !/^[a-f0-9]{40}$/.test(buildRevision)
          || !/^[a-f0-9]{64}$/.test(deploymentFingerprint)
          || !publicOrigin) {
        return {
          status: "not_ready",
          revision: { profile },
          causes: [publicCause(
            "WEBRTC_READINESS_CONFIG_MISSING",
            "Public WebRTC requires a pinned, deployment-bound external readiness receipt.",
            false,
            "turn",
          )],
        };
      }
      try {
        const receipt = readWebRtcReadinessReceipt(receiptPath, { fsImpl });
        if (digestWebRtcReceipt(receipt) !== expectedDigest) {
          const mismatch = new Error("WebRTC readiness receipt digest mismatch");
          mismatch.code = "WEBRTC_EVIDENCE_MISMATCH";
          throw mismatch;
        }
        verifyWebRtcReadinessReceipt(receipt, {
          buildRevision,
          deploymentFingerprint,
          publicOrigin,
          ...(certificateSha256 ? { certificateSha256 } : {}),
          now: clock,
        });
        return {
          status: "ready",
          revision: {
            profile,
            verification: "external_forced_relay_receipt",
            receipt_id: receipt.receipt_id,
            public_origin: receipt.public_endpoint.origin,
            expires_at: receipt.expires_at,
          },
          causes: [],
        };
      } catch (error) {
        const code = error && typeof error.code === "string" && /^[A-Z0-9_]{3,100}$/.test(error.code)
          ? error.code
          : "WEBRTC_RECEIPT_UNAVAILABLE";
        return {
          status: "not_ready",
          revision: { profile },
          causes: [publicCause(
            code,
            code === "WEBRTC_EVIDENCE_EXPIRED"
              ? "The public WebRTC external readiness receipt has expired."
              : "The public WebRTC external readiness receipt is unavailable or invalid.",
            code === "WEBRTC_EVIDENCE_EXPIRED" || code === "WEBRTC_RECEIPT_UNAVAILABLE",
            "turn",
          )],
        };
      }
    }
    return { status: "ready", revision: { profile }, causes: [] };
  };
}

function createTimelineReadinessProbe({ animationUeProbe } = {}) {
  return async function probeTimeline({ signal } = {}) {
    if (typeof animationUeProbe === "function") {
      let result;
      try {
        result = await animationUeProbe({ signal });
      } catch (_error) {
        result = null;
      }
      if (result && result.status === "ready" && Array.isArray(result.causes)
          && result.causes.length === 0 && result.revision
          && result.revision.verification === "live_plugin_challenge") {
        return result;
      }
      if (result && result.status === "not_ready" && Array.isArray(result.causes)
          && result.revision && typeof result.revision === "object") return result;
    }
    return {
      status: "not_ready",
      revision: { schema: "vista-timeline/v1", animation_content_api: "not_ready" },
      causes: [publicCause(
        "ANIMATION_UE_PLUGIN_TRANSPORT_MISSING",
        "The server timeline is implemented, but a trusted live VISTA animation UE plugin is not available.",
        false,
        "vista_animation_ue_plugin",
      )],
    };
  };
}

function createNlpGenerationReadinessProbe() {
  return async function probeNlpGeneration() {
    return {
      status: "not_ready",
      revision: {
        schema: "simworld-nlp-scene/v1",
        free_form_typed_mutation: "unavailable",
        vista_scene_build_plan: "available",
      },
      causes: [publicCause(
        "NLP_TYPED_MUTATION_UNAVAILABLE",
        "Free-form NLP generation has no trusted typed mutation adapter; verified VISTA SceneBuildPlan execution remains available.",
        false,
        "nlp_typed_mutation",
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
  animationUeProbe,
  artifactJournalProbe,
  reviewEvidenceProbe,
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
  const reviewProviderProbe = createReviewReadinessProbe({
    env,
    claudeBin,
    fsImpl,
    sourceRevision: buildRevision,
  });
  const probes = {
    artifact_journal: {
      probe: probeOverrides.artifact_journal || artifactJournalProbe || (async () => ({
        status: "not_ready",
        causes: [publicCause(
          "ARTIFACT_JOURNAL_NOT_CONFIGURED",
          "The durable artifact journal is not configured.",
          false,
          "artifact_journal",
        )],
      })),
      timeoutMs: 10_000,
    },
    review: probeOverrides.review || combineReviewReadinessProbes(
      reviewProviderProbe,
      reviewEvidenceProbe,
    ),
    retrieval: probeOverrides.retrieval || createRetrievalReadinessProbe({ env, fsImpl }),
    streaming: probeOverrides.streaming || createStreamingReadinessProbe({
      env,
      host: cirrusHost,
      port: cirrusPort,
      connect,
      fsImpl,
    }),
    timeline: probeOverrides.timeline || createTimelineReadinessProbe({ animationUeProbe }),
    nlp_generation: probeOverrides.nlp_generation || createNlpGenerationReadinessProbe(),
  };
  return createReadinessRegistry({
    revision: revision || { build: buildRevision },
    featurePolicy: resolveStudioFeaturePolicy(env),
    defaultTimeoutMs: 750,
    probes,
  });
}

module.exports = {
  combineReviewReadinessProbes,
  createNlpGenerationReadinessProbe,
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

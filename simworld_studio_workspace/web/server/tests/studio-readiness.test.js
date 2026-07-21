"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  createRetrievalReadinessProbe,
  createReviewReadinessProbe,
  createStreamingReadinessProbe,
  createStudioReadiness,
  createTimelineReadinessProbe,
  resolveStudioFeaturePolicy,
} = require("../studio-readiness");
const {
  createReviewSmokeReceipt,
  digestReviewScene,
  writeReviewSmokeReceiptAtomic,
} = require("../review-smoke-receipt");
const { canonicalJson } = require("../asset-snapshot");
const { digestJson: digestWebRtcReceipt } = require("../webrtc-readiness-receipt");

const REVIEW_NOW = Date.parse("2026-07-21T05:00:00.000Z");
const ASSET_NOW = new Date("2026-07-21T06:00:00.000Z");

function addLiveAssetReceipt(files, manifestPath = "/assets/snapshot-manifest.json") {
  const manifestBytes = Buffer.from(files[manifestPath], "utf8");
  const manifest = JSON.parse(manifestBytes.toString("utf8"));
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
    issued_at: "2026-07-21T06:00:00Z",
    expires_at: "2026-07-21T06:05:00Z",
    ttl_seconds: 300,
    observations,
  };
  const receiptBytes = Buffer.from(`${JSON.stringify(receipt)}\n`, "utf8");
  files["/assets/snapshot-live-audit.json"] = receiptBytes.toString("utf8");
  return crypto.createHash("sha256").update(receiptBytes).digest("hex");
}

function writeSmoke(file, overrides = {}) {
  const digest = digestReviewScene({ actors: [{ name: "Chair", location: [0, 0, 0] }] });
  const receipt = createReviewSmokeReceipt({
    receiptId: "readiness-smoke-1",
    reviewType: "visual",
    provider: "claude",
    model: "claude-opus-4-8",
    cliName: "claude-code",
    cliVersion: "2.1.17",
    sourceRevision: "build-abc123",
    recordedAt: new Date(REVIEW_NOW).toISOString(),
    expiresAt: new Date(REVIEW_NOW + 60 * 60 * 1000).toISOString(),
    sceneDigestBefore: digest,
    sceneDigestAfter: digest,
    usage: { input_tokens: 100, output_tokens: 20, cost_usd: 0.01 },
    verdict: { status: "PASS", issues: [], suggestions: [], raw_notes: "ok" },
    ...overrides,
  });
  writeReviewSmokeReceiptAtomic(file, receipt);
  return receipt;
}

function executableFs(exists) {
  return {
    accessSync() {
      if (!exists) throw new Error("missing");
    },
  };
}

function memoryFs(files) {
  return {
    readFileSync(file) {
      if (!Object.prototype.hasOwnProperty.call(files, file)) throw new Error("missing");
      return files[file];
    },
  };
}

function successfulConnection() {
  const socket = new EventEmitter();
  socket.destroy = () => {};
  queueMicrotask(() => socket.emit("connect"));
  return socket;
}

function webRtcReceipt() {
  return {
    schema: "simworld-webrtc-readiness-receipt/v1",
    receipt_id: "webrtc-prod-20260721-001",
    outcome: "ready",
    build_revision: "a".repeat(40),
    deployment_fingerprint: "b".repeat(64),
    recorded_at: "2026-07-21T04:00:00.000Z",
    expires_at: "2026-07-21T05:00:00.000Z",
    public_endpoint: {
      origin: "https://studio.example.edu",
      certificate_sha256: "c".repeat(64),
      tls_version: "TLSv1.3",
      https_status: 200,
      wss_status: 101,
    },
    transport: {
      cirrus_reachable: true,
      streamer_registered: true,
      forced_relay: true,
      networks_tested: 2,
      sessions_tested: 6,
      turn_transports: ["tcp", "tls", "udp"],
      minimum_session_seconds: 720,
      max_input_round_trip_ms: 94,
    },
    security: {
      no_mixed_content: true,
      no_host_candidates: true,
      no_private_addresses: true,
      no_raw_ports: true,
      no_credentials: true,
      no_tokens: true,
      unauthenticated_denied: true,
      cross_session_denied: true,
    },
    evidence: {
      probe_schema: "simworld-webrtc-probe-results/v1",
      probe_id: "webrtc-prod-20260721-001",
      probe_sha256: "d".repeat(64),
    },
  };
}

test("feature policy makes production retrieval and public streaming blocking", () => {
  const policy = resolveStudioFeaturePolicy({
    NODE_ENV: "production",
    ASSET_REQUIRE_REAL_ASSETS: "true",
    STUDIO_TRANSPORT_PROFILE: "public_webrtc",
  });
  assert.equal(policy.retrieval, "required");
  assert.equal(policy.streaming, "required");
  assert.equal(policy.review, "required");
  assert.equal(policy.timeline, "optional");
});

test("review readiness ignores the legacy flag and requires a matching, current provider receipt", async (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "simworld-readiness-review-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const textReceiptPath = path.join(directory, "text-receipt.json");
  const visualReceiptPath = path.join(directory, "visual-receipt.json");
  const env = {
    PATH: "/bin",
    NODE_ENV: "production",
    CRITIC_PROVIDER: "claude",
    CRITIC_MODEL: "claude-opus-4-8",
    CRITIC_MAX_BUDGET_USD: "0.05",
    SIMWORLD_BUILD_REVISION: "build-abc123",
    REVIEW_TEXT_SMOKE_RECEIPT_PATH: textReceiptPath,
    REVIEW_VISUAL_SMOKE_RECEIPT_PATH: visualReceiptPath,
  };
  const probe = createReviewReadinessProbe({ env, claudeBin: process.execPath, now: () => REVIEW_NOW + 1 });
  const report = await probe();
  assert.equal(report.status, "not_ready");
  assert.deepEqual(report.revision, {
    provider: "claude",
    model: "claude-opus-4-8",
    source_revision: "build-abc123",
  });
  assert.equal(report.causes[0].code, "REVIEW_SMOKE_RECEIPT_UNAVAILABLE");

  const legacy = await createReviewReadinessProbe({
    env: { ...env, REVIEW_READINESS_VERIFIED: "1", REVIEW_VERIFIED_MODEL: "claude-opus-4-8" },
    claudeBin: process.execPath,
    now: () => REVIEW_NOW + 1,
  })();
  assert.equal(legacy.status, "not_ready");
  assert.equal(legacy.causes[0].code, "REVIEW_LEGACY_OVERRIDE_REJECTED");

  writeSmoke(textReceiptPath, { receiptId: "readiness-smoke-text", reviewType: "text" });
  writeSmoke(visualReceiptPath, { receiptId: "readiness-smoke-visual", reviewType: "visual" });
  const verified = await probe();
  assert.equal(verified.status, "ready");
  assert.equal(verified.revision.verification, "text_visual_provider_smoke_receipts");
  assert.deepEqual(verified.revision.review_types, ["text", "visual"]);
  assert.equal(verified.revision.cli_version, "2.1.17");
  assert.equal(JSON.stringify(verified).includes(directory), false);
});

test("review readiness fails closed for receipt mismatch and expiry without provider calls", async (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "simworld-readiness-mismatch-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const textReceiptPath = path.join(directory, "text-receipt.json");
  const visualReceiptPath = path.join(directory, "visual-receipt.json");
  writeSmoke(textReceiptPath, { receiptId: "readiness-smoke-text", reviewType: "text" });
  writeSmoke(visualReceiptPath, { receiptId: "readiness-smoke-visual", reviewType: "visual" });
  const base = {
    PATH: "/bin",
    NODE_ENV: "production",
    CRITIC_PROVIDER: "claude",
    CRITIC_MODEL: "claude-opus-4-8",
    CRITIC_MAX_BUDGET_USD: "0.05",
    SIMWORLD_BUILD_REVISION: "build-abc123",
    REVIEW_TEXT_SMOKE_RECEIPT_PATH: textReceiptPath,
    REVIEW_VISUAL_SMOKE_RECEIPT_PATH: visualReceiptPath,
  };

  const mismatch = await createReviewReadinessProbe({
    env: { ...base, SIMWORLD_BUILD_REVISION: "build-other" },
    claudeBin: process.execPath,
    now: () => REVIEW_NOW + 1,
  })();
  assert.equal(mismatch.status, "not_ready");
  assert.equal(mismatch.causes[0].code, "REVIEW_SMOKE_RECEIPT_MISMATCH");

  const expired = await createReviewReadinessProbe({
    env: base,
    claudeBin: process.execPath,
    now: () => REVIEW_NOW + 60 * 60 * 1000,
  })();
  assert.equal(expired.status, "not_ready");
  assert.equal(expired.causes[0].code, "REVIEW_SMOKE_RECEIPT_EXPIRED");

  writeSmoke(visualReceiptPath, {
    receiptId: "readiness-smoke-visual-failed",
    reviewType: "visual",
    verdict: {
      status: "FAIL",
      issues: ["Provider smoke did not pass."],
      suggestions: ["Fix the evidence and run a newly approved smoke."],
      raw_notes: "Not persisted.",
    },
  });
  const failedVerdict = await createReviewReadinessProbe({
    env: base,
    claudeBin: process.execPath,
    now: () => REVIEW_NOW + 1,
  })();
  assert.equal(failedVerdict.status, "not_ready");
  assert.equal(failedVerdict.causes[0].code, "REVIEW_SMOKE_VERDICT_FAILED");
});

test("explicit demo and test overrides stay non-production only", async () => {
  const config = {
    CRITIC_PROVIDER: "claude",
    CRITIC_MODEL: "claude-opus-4-8",
    CRITIC_MAX_BUDGET_USD: "0.05",
  };
  const demo = await createReviewReadinessProbe({
    env: { ...config, NODE_ENV: "development", REVIEW_READINESS_DEMO_OVERRIDE: "1" },
    fsImpl: executableFs(false),
  })();
  assert.equal(demo.status, "ready");
  assert.equal(demo.revision.verification, "demo_override");

  const testOverride = await createReviewReadinessProbe({
    env: { ...config, NODE_ENV: "test", REVIEW_READINESS_TEST_OVERRIDE: "1" },
    fsImpl: executableFs(false),
  })();
  assert.equal(testOverride.status, "ready");
  assert.equal(testOverride.revision.verification, "test_override");

  const production = await createReviewReadinessProbe({
    env: { ...config, NODE_ENV: "production", REVIEW_READINESS_DEMO_OVERRIDE: "1" },
    fsImpl: executableFs(true),
  })();
  assert.equal(production.status, "not_ready");
  assert.equal(production.causes[0].code, "REVIEW_READINESS_OVERRIDE_FORBIDDEN");
});

test("unsafe critic providers fail readiness before process execution", async () => {
  const report = await createReviewReadinessProbe({
    env: { PATH: "/bin", CRITIC_PROVIDER: "codex", CRITIC_MODEL: "gpt-5.5" },
    fsImpl: executableFs(true),
  })();
  assert.equal(report.status, "not_ready");
  assert.equal(report.causes[0].code, "REVIEW_CONFIG_INVALID");
});

test("retrieval readiness reports all missing required foundations", async () => {
  const report = await createRetrievalReadinessProbe({
    env: { ASSET_DB_DIR: "/assets" },
    fsImpl: memoryFs({}),
  })();
  assert.equal(report.status, "not_ready");
  assert.deepEqual(
    new Set(report.causes.map((cause) => cause.code)),
    new Set([
      "ASSET_CATALOG_UNAVAILABLE",
      "ASSET_SNAPSHOT_MANIFEST_MISSING",
      "ASSET_LIVE_AUDIT_RECEIPT_MISSING",
      "ASSET_POSTGRES_CONFIG_MISSING",
      "ASSET_QDRANT_CONFIG_MISSING",
      "ASSET_EMBED_CONFIG_MISSING",
      "ASSET_READINESS_REVISION_MISMATCH",
      "ASSET_LIVE_AUDIT_RECEIPT_PIN_MISSING",
    ]),
  );
  assert.equal(JSON.stringify(report).includes("postgres://"), false);
});

test("matching verified snapshot can become retrieval-ready", async () => {
  const files = {
    "/assets/category_index.json": JSON.stringify({ categories: [{ count: 2 }, { count: 3 }] }),
    "/assets/snapshot-manifest.json": JSON.stringify({
      schema: "simworld-asset-snapshot/v1",
      snapshot_id: "snapshot-7",
      ue_content_revision: "ue-content-7",
      catalog: { count: 5, sha256: "a".repeat(64) },
      postgres: { schema_version: 2, row_count: 5 },
      qdrant: { collection: "assets-7", point_count: 5, dense_name: "text_dense", dense_size: 1024, sparse_name: "text_sparse" },
      embedding: { version: "embed-2", dense_model: "dense-model", sparse_model: "sparse-model" },
    }),
  };
  const receiptSha256 = addLiveAssetReceipt(files);
  const report = await createRetrievalReadinessProbe({
    env: {
      ASSET_DB_DIR: "/assets",
      POSTGRES_URL: "postgres://user:secret@db/assets",
      QDRANT_URL: "http://qdrant:6333",
      EMBED_SERVICE_URL: "http://embed:7777",
      ASSET_READINESS_VERIFIED_REVISION: "snapshot-7",
      ASSET_LIVE_AUDIT_RECEIPT_SHA256: receiptSha256,
    },
    fsImpl: memoryFs(files),
    clock: () => ASSET_NOW,
  })();
  assert.equal(report.status, "ready");
  assert.equal(report.revision.snapshot_id, "snapshot-7");
  assert.equal(JSON.stringify(report).includes("secret"), false);
});

test("snapshot count mismatch fails retrieval readiness", async () => {
  const files = {
    "/assets/category_index.json": JSON.stringify({ categories: [{ count: 2 }] }),
    "/assets/snapshot-manifest.json": JSON.stringify({
      schema: "simworld-asset-snapshot/v1",
      snapshot_id: "snapshot-mismatch",
      ue_content_revision: "ue-content-mismatch",
      catalog: { count: 9, sha256: "b".repeat(64) },
      postgres: { schema_version: 2, row_count: 9 },
      qdrant: { collection: "assets-mismatch", point_count: 9, dense_name: "text_dense", dense_size: 1024, sparse_name: "text_sparse" },
      embedding: { version: "embed-mismatch", dense_model: "dense-model", sparse_model: "sparse-model" },
    }),
  };
  const report = await createRetrievalReadinessProbe({
    env: {
      ASSET_DB_DIR: "/assets",
      POSTGRES_URL: "configured",
      QDRANT_URL: "configured",
      EMBED_SERVICE_URL: "configured",
      ASSET_READINESS_VERIFIED_REVISION: "snapshot-mismatch",
    },
    fsImpl: memoryFs(files),
  })();
  assert.equal(report.status, "not_ready");
  assert.equal(report.causes.some((cause) => cause.code === "ASSET_SNAPSHOT_COUNT_MISMATCH"), true);
});

test("streaming and timeline probes distinguish local transport from missing automation", async () => {
  const streaming = await createStreamingReadinessProbe({
    env: { STUDIO_TRANSPORT_PROFILE: "loopback" },
    connect: successfulConnection,
  })({ signal: new AbortController().signal });
  assert.equal(streaming.status, "ready");

  const timeline = await createTimelineReadinessProbe({
    env: { TIMELINE_AUTOMATION_VERIFIED: "true" },
  })();
  assert.equal(timeline.status, "not_ready");
  assert.equal(timeline.causes[0].code, "ANIMATION_UE_PLUGIN_TRANSPORT_MISSING");

  const verifiedTimeline = await createTimelineReadinessProbe({
    animationUeProbe: async () => ({
      status: "ready",
      revision: {
        verification: "live_plugin_challenge",
        plugin_build_id: "vista-animation-build-1",
      },
      causes: [],
    }),
  })();
  assert.equal(verifiedTimeline.status, "ready");
  assert.equal(verifiedTimeline.revision.verification, "live_plugin_challenge");
});

test("public streaming ignores legacy flags and requires a current deployment-bound forced-relay receipt", async () => {
  const legacy = await createStreamingReadinessProbe({
    env: {
      STUDIO_TRANSPORT_PROFILE: "public_webrtc",
      PUBLIC_WEBRTC_EXTERNAL_VERIFIED: "true",
    },
    connect: successfulConnection,
  })();
  assert.equal(legacy.status, "not_ready");
  assert.equal(legacy.causes[0].code, "WEBRTC_READINESS_CONFIG_MISSING");

  const receipt = webRtcReceipt();
  const receiptPath = "/evidence/webrtc-readiness.json";
  const ready = await createStreamingReadinessProbe({
    env: {
      STUDIO_TRANSPORT_PROFILE: "public_webrtc",
      WEBRTC_READINESS_RECEIPT_PATH: receiptPath,
      WEBRTC_READINESS_RECEIPT_SHA256: digestWebRtcReceipt(receipt),
      SIMWORLD_BUILD_REVISION: receipt.build_revision,
      WEBRTC_DEPLOYMENT_FINGERPRINT: receipt.deployment_fingerprint,
      STUDIO_PUBLIC_ORIGIN: receipt.public_endpoint.origin,
      WEBRTC_CERTIFICATE_SHA256: receipt.public_endpoint.certificate_sha256,
    },
    connect: successfulConnection,
    fsImpl: memoryFs({ [receiptPath]: `${JSON.stringify(receipt)}\n` }),
    clock: () => Date.parse("2026-07-21T04:30:00.000Z"),
  })();
  assert.equal(ready.status, "ready");
  assert.equal(ready.revision.verification, "external_forced_relay_receipt");

  const expired = await createStreamingReadinessProbe({
    env: {
      STUDIO_TRANSPORT_PROFILE: "public_webrtc",
      WEBRTC_READINESS_RECEIPT_PATH: receiptPath,
      WEBRTC_READINESS_RECEIPT_SHA256: digestWebRtcReceipt(receipt),
      SIMWORLD_BUILD_REVISION: receipt.build_revision,
      WEBRTC_DEPLOYMENT_FINGERPRINT: receipt.deployment_fingerprint,
      STUDIO_PUBLIC_ORIGIN: receipt.public_endpoint.origin,
    },
    connect: successfulConnection,
    fsImpl: memoryFs({ [receiptPath]: `${JSON.stringify(receipt)}\n` }),
    clock: () => Date.parse("2026-07-21T05:00:00.000Z"),
  })();
  assert.equal(expired.status, "not_ready");
  assert.equal(expired.causes[0].code, "WEBRTC_EVIDENCE_EXPIRED");
});

test("studio registry remains live while optional dependencies are degraded", async () => {
  const ready = async () => ({ status: "ready", causes: [] });
  const unavailable = async () => ({
    status: "not_ready",
    causes: [{ code: "FEATURE_NOT_READY", message: "Not ready.", retryable: false }],
  });
  const registry = createStudioReadiness({
    env: {},
    probeOverrides: {
      review: ready,
      retrieval: unavailable,
      streaming: ready,
      timeline: unavailable,
    },
  });
  assert.equal(registry.getLiveness().status, "live");
  const report = await registry.getReadiness();
  assert.equal(report.ready, true);
  assert.equal(report.status, "degraded");
});

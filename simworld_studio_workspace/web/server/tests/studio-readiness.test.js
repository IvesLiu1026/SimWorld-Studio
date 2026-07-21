"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
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

const REVIEW_NOW = Date.parse("2026-07-21T05:00:00.000Z");

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
  const receiptPath = path.join(directory, "receipt.json");
  const env = {
    PATH: "/bin",
    NODE_ENV: "production",
    CRITIC_PROVIDER: "claude",
    CRITIC_MODEL: "claude-opus-4-8",
    SIMWORLD_BUILD_REVISION: "build-abc123",
    REVIEW_SMOKE_RECEIPT_PATH: receiptPath,
    REVIEW_SMOKE_RECEIPT_TYPE: "visual",
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

  writeSmoke(receiptPath);
  const verified = await probe();
  assert.equal(verified.status, "ready");
  assert.equal(verified.revision.verification, "provider_smoke_receipt");
  assert.equal(verified.revision.review_type, "visual");
  assert.equal(verified.revision.cli_version, "2.1.17");
  assert.equal(JSON.stringify(verified).includes(receiptPath), false);
});

test("review readiness fails closed for receipt mismatch and expiry without provider calls", async (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "simworld-readiness-mismatch-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const receiptPath = path.join(directory, "receipt.json");
  writeSmoke(receiptPath);
  const base = {
    PATH: "/bin",
    NODE_ENV: "production",
    CRITIC_PROVIDER: "claude",
    CRITIC_MODEL: "claude-opus-4-8",
    SIMWORLD_BUILD_REVISION: "build-abc123",
    REVIEW_SMOKE_RECEIPT_PATH: receiptPath,
    REVIEW_SMOKE_RECEIPT_TYPE: "visual",
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

  writeSmoke(receiptPath, {
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
  const config = { CRITIC_PROVIDER: "claude", CRITIC_MODEL: "claude-opus-4-8" };
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
      "ASSET_POSTGRES_CONFIG_MISSING",
      "ASSET_QDRANT_CONFIG_MISSING",
      "ASSET_EMBED_CONFIG_MISSING",
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
      postgres: { schema_version: 1, row_count: 5 },
      qdrant: { collection: "assets-7", point_count: 5, dense_name: "text_dense", dense_size: 1024, sparse_name: "text_sparse" },
      embedding: { version: "embed-2", dense_model: "dense-model", sparse_model: "sparse-model" },
    }),
  };
  const report = await createRetrievalReadinessProbe({
    env: {
      ASSET_DB_DIR: "/assets",
      POSTGRES_URL: "postgres://user:secret@db/assets",
      QDRANT_URL: "http://qdrant:6333",
      EMBED_SERVICE_URL: "http://embed:7777",
      ASSET_READINESS_VERIFIED_REVISION: "snapshot-7",
    },
    fsImpl: memoryFs(files),
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
      postgres: { schema_version: 1, row_count: 9 },
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

  const timeline = await createTimelineReadinessProbe({ env: {} })();
  assert.equal(timeline.status, "not_ready");
  assert.equal(timeline.causes[0].code, "TIMELINE_AUTOMATION_UNAVAILABLE");
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

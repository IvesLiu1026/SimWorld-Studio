"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");

const {
  createRetrievalReadinessProbe,
  createReviewReadinessProbe,
  createStreamingReadinessProbe,
  createStudioReadiness,
  createTimelineReadinessProbe,
  resolveStudioFeaturePolicy,
} = require("../studio-readiness");

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
    ASSET_REQUIRE_REAL_ASSETS: "true",
    STUDIO_TRANSPORT_PROFILE: "public_webrtc",
  });
  assert.equal(policy.retrieval, "required");
  assert.equal(policy.streaming, "required");
  assert.equal(policy.review, "optional");
  assert.equal(policy.timeline, "optional");
});

test("review readiness validates Opus configuration without invoking a model", async () => {
  const env = {
    PATH: "/bin",
    CRITIC_PROVIDER: "claude",
    CRITIC_MODEL: "claude-opus-4-8",
  };
  const probe = createReviewReadinessProbe({ env, fsImpl: executableFs(true) });
  const report = await probe();
  assert.equal(report.status, "degraded");
  assert.deepEqual(report.revision, { provider: "claude", model: "claude-opus-4-8" });
  assert.equal(report.causes[0].code, "REVIEW_PROVIDER_UNVERIFIED");

  const verified = await createReviewReadinessProbe({
    env: { ...env, REVIEW_READINESS_VERIFIED: "1", REVIEW_VERIFIED_MODEL: "claude-opus-4-8" },
    fsImpl: executableFs(true),
  })();
  assert.equal(verified.status, "ready");
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

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { canonicalJson } = require("../asset-snapshot");
const {
  createVistaAssetRuntime,
  normalizeRetrievalCandidates,
  resolveVistaAssetRuntimeConfig,
} = require("../vista-asset-runtime");

const NOW = new Date("2026-07-21T01:00:00Z");

function manifest() {
  return {
    schema: "simworld-asset-snapshot/v1",
    snapshot_id: "assets-2026-07-21-r1",
    ue_content_revision: "ue-content-2026-07-21-r1",
    catalog: { count: 100, sha256: "a".repeat(64) },
    postgres: { schema_version: 2, row_count: 100 },
    qdrant: { collection: "assets-r1", point_count: 100, dense_name: "text_dense", dense_size: 1024, sparse_name: "text_sparse" },
    embedding: { version: "bge-bm25-r1", dense_model: "BAAI/bge@revision", sparse_model: "Qdrant/bm25@revision" },
  };
}

function enabledEnv(fixtureValue) {
  const value = manifest();
  return {
    VISTA_ASSET_RESOLUTION_ENABLED: "1",
    ASSET_SNAPSHOT_MANIFEST: fixtureValue.file,
    ASSET_LIVE_AUDIT_RECEIPT: fixtureValue.receiptFile,
    ASSET_LIVE_AUDIT_RECEIPT_SHA256: fixtureValue.receiptSha256,
    ASSET_SNAPSHOT_REVISION: value.snapshot_id,
    ASSET_READINESS_VERIFIED_REVISION: value.snapshot_id,
    POSTGRES_URL: "postgresql://not-read-by-config/assets",
    QDRANT_URL: "http://127.0.0.1:6333",
    QDRANT_COLLECTION: value.qdrant.collection,
    QDRANT_API_KEY: "q".repeat(40),
    EMBED_SERVICE_URL: "http://127.0.0.1:7777",
    EMBED_SERVICE_TOKEN: "e".repeat(40),
    EMBED_VERSION: value.embedding.version,
    VISTA_UE_CONTENT_REVISION: value.ue_content_revision,
    VISTA_ASSET_MIN_CONFIDENCE: "0.5",
  };
}

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "vista-asset-runtime-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const file = path.join(root, "snapshot.json");
  const manifestBytes = Buffer.from(`${JSON.stringify(manifest())}\n`, "utf8");
  fs.writeFileSync(file, manifestBytes, { mode: 0o600 });
  const observations = {
    asset_snapshot_revision: manifest().snapshot_id,
    ue_content_revision: manifest().ue_content_revision,
    catalog: manifest().catalog,
    postgres: manifest().postgres,
    qdrant: manifest().qdrant,
    embedding: manifest().embedding,
  };
  const receipt = {
    schema: "simworld-asset-live-audit/v1",
    snapshot_id: manifest().snapshot_id,
    manifest_sha256: crypto.createHash("sha256").update(manifestBytes).digest("hex"),
    observations_sha256: crypto.createHash("sha256").update(canonicalJson(observations), "utf8").digest("hex"),
    issued_at: "2026-07-21T01:00:00Z",
    expires_at: "2026-07-21T01:05:00Z",
    ttl_seconds: 300,
    observations,
  };
  const receiptFile = path.join(root, "snapshot-live-audit.json");
  const receiptBytes = Buffer.from(`${JSON.stringify(receipt, null, 2)}\n`, "utf8");
  fs.writeFileSync(receiptFile, receiptBytes, { mode: 0o600 });
  return {
    file,
    manifestBytes,
    receipt,
    receiptBytes,
    receiptFile,
    receiptSha256: crypto.createHash("sha256").update(receiptBytes).digest("hex"),
    root,
  };
}

function scene() {
  return {
    schema: "vista-simworld-scene/v1",
    scene_id: "mmg_040@0123456789abcdef",
    profile: "reconstruction",
    source: { dataset_revision: "r1" },
    duration_sec: 12,
    environment: { description: "office", lighting: "neutral" },
    camera: { perspective: "first_person" },
    entities: [{
      id: "chair",
      semantic_query: "black wheeled office chair",
      required: true,
      asset_binding: null,
      source_pointer: "/Scene/Key_Visual_Elements/chair",
      privilege: "reconstruction_only",
    }],
    relations: [],
    timeline: [],
    dialogue: [],
    unresolved: [],
    provenance: { importer_version: "1.0.0" },
  };
}

test("runtime is explicitly disabled by default and does not require dependency secrets", () => {
  const config = resolveVistaAssetRuntimeConfig({});
  assert.deepEqual(config, { enabled: false, manifest: null, snapshotId: null });
  assert.equal(createVistaAssetRuntime({ config }).resolver, null);
});

test("enabled runtime requires one matching verified snapshot across all dependencies", (t) => {
  const files = fixture(t);
  const config = resolveVistaAssetRuntimeConfig(enabledEnv(files), { clock: () => NOW });
  assert.equal(config.enabled, true);
  assert.equal(config.snapshotId, manifest().snapshot_id);
  assert.equal(config.qdrantCollection, manifest().qdrant.collection);
  assert.equal(config.ueContentRevision, manifest().ue_content_revision);
  assert.equal(config.postgresUrl, "postgresql://not-read-by-config/assets");
  assert.equal(config.qdrantApiKey, "q".repeat(40));
  assert.equal(config.embedServiceToken, "e".repeat(40));
  assert.doesNotMatch(JSON.stringify(config), /not-read-by-config|q{40}|e{40}/);

  for (const [field, value] of [
    ["ASSET_READINESS_VERIFIED_REVISION", "stale"],
    ["QDRANT_COLLECTION", "wrong"],
    ["EMBED_VERSION", "wrong"],
    ["VISTA_UE_CONTENT_REVISION", "wrong"],
  ]) {
    assert.throws(() => resolveVistaAssetRuntimeConfig({ ...enabledEnv(files), [field]: value }, { clock: () => NOW }));
  }
});

test("enabled runtime accepts a no-follow PostgreSQL DSN secret file", (t) => {
  const files = fixture(t);
  const secretFile = path.join(files.root, "postgres-url");
  fs.writeFileSync(secretFile, "postgresql://asset_user:secret@127.0.0.1/assets\n", { mode: 0o600 });
  const env = enabledEnv(files);
  delete env.POSTGRES_URL;
  env.POSTGRES_URL_FILE = secretFile;
  const config = resolveVistaAssetRuntimeConfig(env, { clock: () => NOW });
  assert.equal(config.postgresUrl, "postgresql://asset_user:secret@127.0.0.1/assets");
  assert.throws(
    () => resolveVistaAssetRuntimeConfig({ ...env, POSTGRES_URL: "postgresql://duplicate/db" }, { clock: () => NOW }),
    /mutually exclusive/,
  );
});

test("enabled runtime resolves Qdrant and embedding file secrets and rejects conflicts", (t) => {
  const files = fixture(t);
  const qdrantFile = path.join(files.root, "qdrant-api-key");
  const embedFile = path.join(files.root, "embed-token");
  fs.writeFileSync(qdrantFile, `${"Q".repeat(40)}\n`, { mode: 0o600 });
  fs.writeFileSync(embedFile, `${"E".repeat(40)}\n`, { mode: 0o600 });
  const env = enabledEnv(files);
  delete env.QDRANT_API_KEY;
  delete env.EMBED_SERVICE_TOKEN;
  env.QDRANT_API_KEY_FILE = qdrantFile;
  env.EMBED_SERVICE_TOKEN_FILE = embedFile;

  const config = resolveVistaAssetRuntimeConfig(env, { clock: () => NOW });
  assert.equal(config.qdrantApiKey, "Q".repeat(40));
  assert.equal(config.embedServiceToken, "E".repeat(40));
  assert.doesNotMatch(JSON.stringify(config), /Q{40}|E{40}|qdrant-api-key|embed-token/);
  assert.throws(
    () => resolveVistaAssetRuntimeConfig(
      { ...env, QDRANT_API_KEY: "duplicate".repeat(5) },
      { clock: () => NOW },
    ),
    /mutually exclusive/,
  );
});

test("runtime requires an exact unexpired digest-bound live audit receipt", (t) => {
  const files = fixture(t);
  const env = enabledEnv(files);
  for (const field of ["ASSET_LIVE_AUDIT_RECEIPT", "ASSET_LIVE_AUDIT_RECEIPT_SHA256"]) {
    assert.throws(() => resolveVistaAssetRuntimeConfig({ ...env, [field]: "" }, { clock: () => NOW }));
  }
  assert.throws(
    () => resolveVistaAssetRuntimeConfig({ ...env, ASSET_LIVE_AUDIT_RECEIPT_SHA256: "f".repeat(64) }, { clock: () => NOW }),
    (error) => error.code === "ASSET_LIVE_AUDIT_RECEIPT_DIGEST_MISMATCH",
  );
  assert.throws(
    () => resolveVistaAssetRuntimeConfig(env, { clock: () => new Date("2026-07-21T01:05:00Z") }),
    (error) => error.code === "ASSET_LIVE_AUDIT_EXPIRED",
  );
});

test("verified search results bind exact asset IDs and UE paths without geometry fallback", async (t) => {
  const files = fixture(t);
  const calls = [];
  const runtime = createVistaAssetRuntime({
    env: enabledEnv(files),
    clock: () => NOW,
    searchAssets: async (request, options) => {
      calls.push({ request, options });
      const assets = [{
        id: "office-chair-1",
        path: "/Game/Office/SM_Chair.SM_Chair",
        asset_snapshot_revision: manifest().snapshot_id,
        _score: 0.91,
      }];
      Object.defineProperty(assets, "retrieval", { value: { source: "qdrant" } });
      return assets;
    },
  });
  const output = await runtime.resolver.resolve(scene());

  assert.deepEqual(output.entities[0].asset_binding, {
    snapshot_id: manifest().snapshot_id,
    asset_id: "office-chair-1",
    ue_path: "/Game/Office/SM_Chair.SM_Chair",
    confidence: 0.91,
  });
  assert.equal(output.unresolved.length, 0);
  assert.deepEqual(calls[0].request, { query: "black wheeled office chair", k: 8 });
  assert.equal(calls[0].options.collection, manifest().qdrant.collection);
  assert.equal(calls[0].options.assetSnapshotRevision, manifest().snapshot_id);
  assert.equal(calls[0].options.qdrantApiKey, "q".repeat(40));
  assert.equal(calls[0].options.embedServiceToken, "e".repeat(40));
});

test("a receipt that expires after startup blocks retrieval before dependencies are called", async (t) => {
  const files = fixture(t);
  let now = NOW;
  let calls = 0;
  const runtime = createVistaAssetRuntime({
    env: enabledEnv(files),
    clock: () => now,
    searchAssets: async () => {
      calls += 1;
      return [];
    },
  });
  now = new Date("2026-07-21T01:05:00Z");
  await assert.rejects(
    runtime.resolver.resolve(scene()),
    (error) => error.code === "VISTA_ASSET_SEARCH_FAILED"
      && error.details.upstream_code === "ASSET_LIVE_AUDIT_EXPIRED",
  );
  assert.equal(calls, 0);
});

test("uncalibrated retrieval scores fail closed instead of becoming false confidence", () => {
  assert.throws(
    () => normalizeRetrievalCandidates([{
      id: "x", path: "/Game/X.X", asset_snapshot_revision: "snapshot-r1", _score: 1.4,
    }], "snapshot-r1"),
    (error) => error.code === "VISTA_ASSET_SEARCH_SCORE_INVALID",
  );
  assert.throws(
    () => normalizeRetrievalCandidates([{
      id: "x", path: "/Game/X.X", asset_snapshot_revision: "snapshot-r1",
    }], "snapshot-r1"),
    (error) => error.code === "VISTA_ASSET_SEARCH_SCORE_INVALID",
  );
});

test("retrieval candidates cannot be relabelled into the configured snapshot", () => {
  assert.throws(
    () => normalizeRetrievalCandidates([{
      id: "x",
      path: "/Game/X.X",
      asset_snapshot_revision: "stale-snapshot",
      _score: 0.9,
    }], "snapshot-r1"),
    (error) => error.code === "VISTA_ASSET_SNAPSHOT_MISMATCH",
  );
});

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  createVistaAssetRuntime,
  normalizeRetrievalCandidates,
  resolveVistaAssetRuntimeConfig,
} = require("../vista-asset-runtime");

function manifest() {
  return {
    schema: "simworld-asset-snapshot/v1",
    snapshot_id: "assets-2026-07-21-r1",
    ue_content_revision: "ue-content-2026-07-21-r1",
    catalog: { count: 100, sha256: "a".repeat(64) },
    postgres: { schema_version: 1, row_count: 100 },
    qdrant: { collection: "assets-r1", point_count: 100, dense_name: "text_dense", dense_size: 1024, sparse_name: "text_sparse" },
    embedding: { version: "bge-bm25-r1", dense_model: "BAAI/bge@revision", sparse_model: "Qdrant/bm25@revision" },
  };
}

function enabledEnv(file) {
  const value = manifest();
  return {
    VISTA_ASSET_RESOLUTION_ENABLED: "1",
    ASSET_SNAPSHOT_MANIFEST: file,
    ASSET_SNAPSHOT_REVISION: value.snapshot_id,
    ASSET_READINESS_VERIFIED_REVISION: value.snapshot_id,
    POSTGRES_URL: "postgresql://not-read-by-config",
    QDRANT_URL: "http://127.0.0.1:6333",
    QDRANT_COLLECTION: value.qdrant.collection,
    EMBED_SERVICE_URL: "http://127.0.0.1:7777",
    EMBED_VERSION: value.embedding.version,
    VISTA_UE_CONTENT_REVISION: value.ue_content_revision,
    VISTA_ASSET_MIN_CONFIDENCE: "0.5",
  };
}

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "vista-asset-runtime-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const file = path.join(root, "snapshot.json");
  fs.writeFileSync(file, `${JSON.stringify(manifest())}\n`, { mode: 0o600 });
  return { file, root };
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
  const { file } = fixture(t);
  const config = resolveVistaAssetRuntimeConfig(enabledEnv(file));
  assert.equal(config.enabled, true);
  assert.equal(config.snapshotId, manifest().snapshot_id);
  assert.equal(config.qdrantCollection, manifest().qdrant.collection);
  assert.equal(config.ueContentRevision, manifest().ue_content_revision);

  for (const [field, value] of [
    ["ASSET_READINESS_VERIFIED_REVISION", "stale"],
    ["QDRANT_COLLECTION", "wrong"],
    ["EMBED_VERSION", "wrong"],
    ["VISTA_UE_CONTENT_REVISION", "wrong"],
  ]) {
    assert.throws(() => resolveVistaAssetRuntimeConfig({ ...enabledEnv(file), [field]: value }));
  }
});

test("verified search results bind exact asset IDs and UE paths without geometry fallback", async (t) => {
  const { file } = fixture(t);
  const calls = [];
  const runtime = createVistaAssetRuntime({
    env: enabledEnv(file),
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

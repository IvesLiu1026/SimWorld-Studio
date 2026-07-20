"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const os = require("node:os");
const path = require("node:path");

const {
  createVistaImportRuntime,
  parseRegistryJson,
  resolveVistaImportConfig,
} = require("../vista-import-runtime");

test("runtime config accepts only operator-side allowlisted roots", () => {
  const root = path.join(os.tmpdir(), "vista-curated-bundle");
  const config = resolveVistaImportConfig({
    HOME: os.tmpdir(),
    VISTA_IMPORT_DATASET_REVISION: "round1_reviewed_latest",
    VISTA_IMPORT_BUNDLE_ROOT: root,
  });
  assert.deepEqual(config.configuredRevisions, ["round1_reviewed_latest"]);
  assert.equal(config.registry.round1_reviewed_latest.root, root);
  assert.equal(config.registry.round1_reviewed_latest.manifestPath, "manifest.json");
  assert.equal(config.artifactRoot, path.join(os.tmpdir(), ".local", "state", "simworld-studio", "vista-imports"));
});

test("registry JSON is strict and rejects traversal or unknown fields", () => {
  const root = path.join(os.tmpdir(), "vista-curated-bundle");
  assert.deepEqual(Object.keys(parseRegistryJson(JSON.stringify({ rev1: root }))), ["rev1"]);
  for (const raw of [
    "[]",
    "{",
    JSON.stringify({ "../private": root }),
    JSON.stringify({ rev1: { root, manifestPath: "../manifest.json" } }),
    JSON.stringify({ rev1: { root, token: "not-allowed" } }),
  ]) {
    assert.throws(() => parseRegistryJson(raw), TypeError);
  }
});

test("simple registry variables must be provided together", () => {
  assert.throws(
    () => resolveVistaImportConfig({ VISTA_IMPORT_DATASET_REVISION: "rev1", HOME: os.tmpdir() }),
    /must be set together/,
  );
  assert.throws(
    () => resolveVistaImportConfig({ VISTA_IMPORT_BUNDLE_ROOT: path.join(os.tmpdir(), "bundle"), HOME: os.tmpdir() }),
    /must be set together/,
  );
});

test("golden fixture is explicit or demo-only and cannot be enabled in production", () => {
  const config = resolveVistaImportConfig({
    HOME: os.tmpdir(),
    NODE_ENV: "development",
    VISTA_IMPORT_ENABLE_GOLDEN_FIXTURE: "1",
  });
  assert.deepEqual(config.configuredRevisions, ["round1_reviewed_latest"]);
  assert.equal(config.fixtureEnabled, true);
  assert.throws(() => resolveVistaImportConfig({
    HOME: os.tmpdir(),
    NODE_ENV: "production",
    VISTA_IMPORT_ENABLE_GOLDEN_FIXTURE: "1",
  }), /not allowed in production/);

  const demo = resolveVistaImportConfig({
    HOME: os.tmpdir(),
    VISTA_DEMO_ENABLED: "1",
  });
  assert.equal(demo.fixtureEnabled, true);
  assert.deepEqual(demo.configuredRevisions, ["round1_reviewed_latest"]);

  const productionDemo = resolveVistaImportConfig({
    HOME: os.tmpdir(),
    NODE_ENV: "production",
    VISTA_DEMO_ENABLED: "1",
  });
  assert.equal(productionDemo.fixtureEnabled, false);
  assert.deepEqual(productionDemo.configuredRevisions, []);
});

test("runtime construction is side-effect free until commit", () => {
  const config = Object.freeze({
    registry: Object.freeze({}),
    artifactRoot: path.join(os.tmpdir(), "vista-import-runtime-test-artifacts"),
    configuredRevisions: Object.freeze([]),
    fixtureEnabled: false,
  });
  const runtime = createVistaImportRuntime({ config });
  assert.equal(typeof runtime.importer.preview, "function");
  assert.equal(typeof runtime.service.commit, "function");
  assert.equal(runtime.config, config);
});

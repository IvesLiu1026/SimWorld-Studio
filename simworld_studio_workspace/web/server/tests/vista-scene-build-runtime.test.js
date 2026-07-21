"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  createVistaSceneBuildRuntime,
  parseLayoutRegistryJson,
  resolveVistaSceneBuildConfig,
} = require("../vista-scene-build-runtime");

const SERVER_ROOT = path.resolve(__dirname, "..");
const FIXTURE_FILE = path.join(SERVER_ROOT, "tests", "fixtures", "vista", "mmg_040", "build-layout.v1.json");

function sha(file) {
  return crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex");
}

test("layout registry is empty by default and golden fixture is explicit non-production only", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "vista-scene-runtime-"));
  try {
    const empty = resolveVistaSceneBuildConfig({ NODE_ENV: "development" }, {
      baseDir: SERVER_ROOT,
      defaultRecordRoot: path.join(root, "records"),
    });
    assert.deepEqual(empty.profileIds, []);
    assert.equal(empty.fixtureEnabled, false);

    const fixture = resolveVistaSceneBuildConfig({
      NODE_ENV: "development",
      VISTA_SCENE_BUILD_ENABLE_GOLDEN_FIXTURE: "1",
    }, {
      baseDir: SERVER_ROOT,
      defaultRecordRoot: path.join(root, "records"),
    });
    assert.deepEqual(fixture.profileIds, ["mmg_040_static_office_v1"]);
    assert.equal(fixture.layoutProfiles.mmg_040_static_office_v1.asset_snapshot_id.includes("test"), true);

    assert.throws(() => resolveVistaSceneBuildConfig({
      NODE_ENV: "production",
      VISTA_SCENE_BUILD_ENABLE_GOLDEN_FIXTURE: "1",
    }, { baseDir: SERVER_ROOT, defaultRecordRoot: path.join(root, "records") }));
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("operator registry requires absolute checksum-pinned profile files", () => {
  const entry = JSON.stringify({
    mmg_040_static_office_v1: { file: FIXTURE_FILE, sha256: sha(FIXTURE_FILE) },
  });
  const parsed = parseLayoutRegistryJson(entry);
  assert.equal(parsed.mmg_040_static_office_v1.file, FIXTURE_FILE);
  assert.equal(parsed.mmg_040_static_office_v1.sha256, sha(FIXTURE_FILE));
  assert.throws(() => parseLayoutRegistryJson(JSON.stringify({
    mmg_040_static_office_v1: { file: "relative.json", sha256: sha(FIXTURE_FILE) },
  })));
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "vista-scene-runtime-production-"));
  try {
    assert.throws(() => resolveVistaSceneBuildConfig({
      NODE_ENV: "production",
      VISTA_SCENE_LAYOUT_REGISTRY_JSON: entry,
    }, { baseDir: SERVER_ROOT, defaultRecordRoot: path.join(root, "records") }), /test\/demo/);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("profile checksum and embedded profile identity are verified before runtime creation", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "vista-scene-runtime-checksum-"));
  try {
    const env = {
      NODE_ENV: "development",
      VISTA_SCENE_LAYOUT_REGISTRY_JSON: JSON.stringify({
        mmg_040_static_office_v1: { file: FIXTURE_FILE, sha256: "0".repeat(64) },
      }),
    };
    assert.throws(() => resolveVistaSceneBuildConfig(env, {
      baseDir: SERVER_ROOT,
      defaultRecordRoot: path.join(root, "records"),
    }), /checksum/);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("runtime can expose planning while keeping UE execution unavailable", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "vista-scene-runtime-service-"));
  try {
    const config = resolveVistaSceneBuildConfig({
      NODE_ENV: "development",
      VISTA_SCENE_BUILD_ENABLE_GOLDEN_FIXTURE: "1",
    }, {
      baseDir: SERVER_ROOT,
      defaultRecordRoot: path.join(root, "records"),
    });
    const runtime = createVistaSceneBuildRuntime({
      config,
      importService: { async status() { throw new Error("not used"); } },
      executor: null,
    });
    assert.deepEqual(runtime.config.profileIds, ["mmg_040_static_office_v1"]);
    assert.equal(typeof runtime.service.plan, "function");
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

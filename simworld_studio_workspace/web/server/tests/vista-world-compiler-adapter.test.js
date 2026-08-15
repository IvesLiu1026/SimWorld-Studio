"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  BUILD_PLAN_SCHEMA,
  createVistaWorldCompilerAdapter,
} = require("../vista-world-compiler-adapter");

const DIGEST = "a".repeat(64);

function fixtureRoot() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "vista-world-compiler-"));
  fs.mkdirSync(path.join(root, "tools", "worlds"), { recursive: true });
  fs.mkdirSync(path.join(root, "world_packs", "vista_playable_home_r1", "events"), { recursive: true });
  fs.writeFileSync(path.join(root, "tools", "worlds", "playable_home.py"), "# fixture\n");
  fs.writeFileSync(path.join(root, "world_packs", "vista_playable_home_r1", "house.json"), "{}\n");
  return root;
}

function plan() {
  return {
    schema_version: BUILD_PLAN_SCHEMA,
    house: { house_id: "home.r1", revision: "vista_playable_home_r1", content_digest: DIGEST },
    content_digest: "b".repeat(64),
  };
}

test("compiler invokes only the fixed module and fixed catalog paths", async (t) => {
  const root = fixtureRoot();
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const calls = [];
  const adapter = createVistaWorldCompilerAdapter({
    repositoryRoot: root,
    uvBin: "/fixed/uv",
    execFileImpl(file, args, options, callback) {
      calls.push({ file, args, options });
      callback(null, JSON.stringify(plan()), "");
    },
  });
  const result = await adapter.compile({
    house_id: "home.r1",
    revision: "vista_playable_home_r1",
    content_digest: DIGEST,
    caller_path: "/etc/shadow",
  });
  assert.equal(result.schema_version, BUILD_PLAN_SCHEMA);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].file, "/fixed/uv");
  assert.deepEqual(calls[0].args.slice(0, 5), ["run", "python", "-m", "worlds.playable_home", "compile"]);
  assert.equal(calls[0].args.includes("/etc/shadow"), false);
  assert.equal(calls[0].args.includes("--output"), false);
  assert.equal(calls[0].options.cwd, path.join(root, "tools"));
  assert.equal(calls[0].options.env.UV_FROZEN, "1");
  assert.equal(calls[0].options.env.UV_NO_SYNC, "1");
  assert.equal(calls[0].options.env.UV_OFFLINE, "1");
  assert.equal(adapter.describe().module_path, path.join(root, "tools", "worlds", "playable_home.py"));
});

test("compiler rejects stale output bindings and invalid JSON", async (t) => {
  const root = fixtureRoot();
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const house = { house_id: "home.r1", revision: "vista_playable_home_r1", content_digest: DIGEST };
  const stale = createVistaWorldCompilerAdapter({
    repositoryRoot: root,
    execFileImpl(_file, _args, _options, callback) {
      callback(null, JSON.stringify({ ...plan(), house: { ...plan().house, revision: "other" } }), "");
    },
  });
  await assert.rejects(stale.compile(house), { code: "VISTA_WORLD_COMPILER_UNAVAILABLE" });
  const malformed = createVistaWorldCompilerAdapter({
    repositoryRoot: root,
    execFileImpl(_file, _args, _options, callback) {
      callback(null, "not-json", "");
    },
  });
  await assert.rejects(malformed.compile(house), { code: "VISTA_WORLD_COMPILER_UNAVAILABLE" });
});

test("compiler aborts before spawning and maps child failures", async (t) => {
  const root = fixtureRoot();
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const house = { house_id: "home.r1", revision: "vista_playable_home_r1", content_digest: DIGEST };
  let calls = 0;
  const adapter = createVistaWorldCompilerAdapter({
    repositoryRoot: root,
    execFileImpl(_file, _args, _options, callback) {
      calls += 1;
      callback(Object.assign(new Error("failed"), { killed: true }), "", "private stderr");
    },
  });
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(adapter.compile(house, { signal: controller.signal }), {
    code: "VISTA_WORLD_COMPILER_UNAVAILABLE",
    retryable: true,
  });
  assert.equal(calls, 0);
  await assert.rejects(adapter.compile(house), {
    code: "VISTA_WORLD_COMPILER_UNAVAILABLE",
    retryable: true,
  });
  assert.equal(calls, 1);
});

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const express = require("express");

const { createVistaImportRouter, safeError } = require("../vista-import-routes");
const { createVistaImportRuntime, resolveVistaImportConfig } = require("../vista-import-runtime");

function listen(server) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      server.removeListener("error", reject);
      resolve(server.address().port);
    });
  });
}

async function fixtureServer(t, service, options = {}) {
  const app = express();
  app.use(express.json());
  app.use("/api/vista/imports", createVistaImportRouter({
    service,
    ownerId: "owner-server",
    sessionId: "session-server",
    ...options,
  }));
  const server = http.createServer(app);
  const port = await listen(server);
  t.after(() => new Promise((resolve) => server.close(resolve)));
  return `http://127.0.0.1:${port}`;
}

async function jsonRequest(url, options) {
  const response = await fetch(url, options);
  return {
    status: response.status,
    cacheControl: response.headers.get("cache-control"),
    body: await response.json(),
  };
}

test("preview is read-only, forwards only the request, and disables caching", async (t) => {
  let received = null;
  const base = await fixtureServer(t, {
    async preview(request) {
      received = request;
      return { schema: "vista-simworld-scene/v1", scene_id: "mmg_040@test" };
    },
    async commit() { throw new Error("not called"); },
    async status() { throw new Error("not called"); },
  });
  const response = await jsonRequest(`${base}/api/vista/imports/preview`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ datasetRevision: "revision-1", sampleId: "mmg_040", attempt: 7 }),
  });
  assert.equal(response.status, 200);
  assert.equal(response.cacheControl, "no-store");
  assert.deepEqual(received, { datasetRevision: "revision-1", sampleId: "mmg_040", attempt: 7 });
  assert.equal(response.body.scene_id, "mmg_040@test");
});

test("commit and status use server-side identity instead of caller owner fields", async (t) => {
  const calls = [];
  const base = await fixtureServer(t, {
    async preview() { return {}; },
    async commit(request, identity) {
      calls.push({ kind: "commit", request, identity });
      return { created: true, run_id: "import-123" };
    },
    async status(runId, identity) {
      calls.push({ kind: "status", runId, identity });
      return { run_id: runId, status: "committed" };
    },
  });
  const commit = await jsonRequest(`${base}/api/vista/imports`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      datasetRevision: "revision-1",
      sampleId: "mmg_040",
      attempt: 7,
      ownerId: "caller-controlled",
    }),
  });
  assert.equal(commit.status, 201);
  assert.deepEqual(calls[0].identity, { ownerId: "owner-server", sessionId: "session-server" });
  assert.equal(calls[0].request.ownerId, "caller-controlled");

  const status = await jsonRequest(`${base}/api/vista/imports/import-123`);
  assert.equal(status.status, 200);
  assert.deepEqual(calls[1], {
    kind: "status",
    runId: "import-123",
    identity: { ownerId: "owner-server", sessionId: "session-server" },
  });
});

test("routes map typed errors and reject invalid run identifiers", async (t) => {
  const typed = Object.assign(new Error("Unknown dataset revision"), {
    code: "VISTA_DATASET_UNAVAILABLE",
    retryable: false,
  });
  const base = await fixtureServer(t, {
    async preview() { throw typed; },
    async commit() { throw typed; },
    async status() { throw typed; },
  });
  const unavailable = await jsonRequest(`${base}/api/vista/imports/preview`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
  assert.equal(unavailable.status, 503);
  assert.equal(unavailable.body.code, "VISTA_DATASET_UNAVAILABLE");

  const invalid = await jsonRequest(`${base}/api/vista/imports/bad%20id`);
  assert.equal(invalid.status, 400);
  assert.equal(invalid.body.code, "VISTA_IMPORT_INVALID");
});

test("public errors redact credentials, query secrets, and server paths", () => {
  const typed = Object.assign(new Error(
    "failed postgres://admin:pw@db.local/x?token=abc at /home/yhliu/private/bundle/manifest.json",
  ), { code: "VISTA_IMPORT_INVALID" });
  const output = JSON.stringify(safeError(typed));
  assert.equal(output.includes("admin:pw"), false);
  assert.equal(output.includes("token=abc"), false);
  assert.equal(output.includes("/home/yhliu"), false);
  assert.equal(output.includes("[redacted]"), true);
  assert.equal(output.includes("[server-path]"), true);

  const internal = safeError(new Error("secret internal detail"));
  assert.deepEqual(internal, {
    error: "VISTA import failed",
    code: "VISTA_IMPORT_INTERNAL",
    retryable: false,
  });
});

test("real curated runtime previews, commits idempotently, and returns session-bound status", async (t) => {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "vista-import-route-e2e-"));
  t.after(() => fs.rmSync(tempRoot, { recursive: true, force: true }));
  const artifactRoot = path.join(tempRoot, "artifacts");
  const config = resolveVistaImportConfig({
    HOME: tempRoot,
    NODE_ENV: "development",
    VISTA_DEMO_ENABLED: "1",
    VISTA_IMPORT_ARTIFACT_ROOT: artifactRoot,
  }, { baseDir: path.resolve(__dirname, "..") });
  const runtime = createVistaImportRuntime({ config });
  const base = await fixtureServer(t, runtime.service);
  const request = {
    datasetRevision: "round1_reviewed_latest",
    sampleId: "mmg_040",
    attempt: 7,
  };
  const preview = await jsonRequest(`${base}/api/vista/imports/preview`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
  assert.equal(preview.status, 200);
  assert.equal(preview.body.duration_sec, 12);
  assert.deepEqual(preview.body.timeline.map((event) => event.at_sec), [0, 2, 5, 9]);
  assert.equal(fs.existsSync(artifactRoot), false);

  const first = await jsonRequest(`${base}/api/vista/imports`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
  const second = await jsonRequest(`${base}/api/vista/imports`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
  assert.equal(first.status, 201);
  assert.equal(second.status, 200);
  assert.equal(first.body.created, true);
  assert.equal(second.body.created, false);
  assert.equal(first.body.artifact_id, second.body.artifact_id);
  assert.equal(fs.readdirSync(artifactRoot).length, 1);

  const status = await jsonRequest(`${base}/api/vista/imports/${first.body.artifact_id}`);
  assert.equal(status.status, 200);
  assert.equal(status.body.artifact_id, first.body.artifact_id);
  assert.equal(status.body.status, "committed");
});

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const express = require("express");

const { createVistaSceneBuildRouter, publicError } = require("../vista-scene-build-routes");

function listen(server) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => resolve(server.address().port));
  });
}

async function fixtureServer(t, service) {
  const app = express();
  app.use(express.json());
  app.use("/api/vista/imports", createVistaSceneBuildRouter({
    service,
    resolveIdentity: () => ({
      ownerId: "owner-route",
      sessionId: "session-route",
      leaseId: "lease-route",
      slotId: 3,
      mcpPort: 55565,
    }),
  }));
  const server = http.createServer(app);
  const port = await listen(server);
  t.after(() => new Promise((resolve) => server.close(resolve)));
  return `http://127.0.0.1:${port}/api/vista/imports`;
}

async function request(url, options) {
  const response = await fetch(url, options);
  return {
    status: response.status,
    cacheControl: response.headers.get("cache-control"),
    body: await response.json(),
  };
}

test("build routes preserve server identity and exact plan/confirmation bodies", async (t) => {
  const calls = [];
  const service = {};
  for (const operation of ["plan", "status", "preflight", "start"]) {
    service[operation] = async (runId, input, context) => {
      calls.push({
        operation,
        runId,
        input,
        ownerId: context.ownerId,
        sessionId: context.sessionId,
        leaseId: context.leaseId,
        slotId: context.slotId,
        mcpPort: context.mcpPort,
      });
      return { operation, runId, input };
    };
  }
  const base = await fixtureServer(t, service);
  const runId = "vim_" + "a".repeat(64);
  const plan = await request(`${base}/${runId}/build/plan`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ profile_id: "mmg_040_static_office_v1", ownerId: "caller" }),
  });
  const preflight = await request(`${base}/${runId}/build/preflight`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ plan_id: "vsp-" + "1".repeat(24) }),
  });
  const execute = await request(`${base}/${runId}/build/execute`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ plan_id: "vsp-" + "1".repeat(24), confirm: true }),
  });
  const status = await request(`${base}/${runId}/build?profile_id=mmg_040_static_office_v1`);

  assert.deepEqual([plan.status, preflight.status, execute.status, status.status], [200, 200, 202, 200]);
  assert.equal(plan.cacheControl, "no-store");
  assert.deepEqual(calls.map(({ operation }) => operation), ["plan", "preflight", "start", "status"]);
  assert.equal(calls.every((call) => call.ownerId === "owner-route" && call.sessionId === "session-route"), true);
  assert.equal(calls.every((call) => call.leaseId === "lease-route"
    && call.slotId === 3 && call.mcpPort === 55565), true);
  assert.equal(calls[0].input.ownerId, "caller", "service exact-shape validation remains authoritative");
});

test("route failures redact secrets, preserve rollback result, and reject malformed ids", async (t) => {
  const error = Object.assign(new Error(
    "failed at /home/yhliu/secret/tree/file.json?token=abc",
  ), {
    code: "SCENE_BUILD_EXECUTION_FAILED",
    status: 502,
    retryable: true,
    result: {
      schema: "vista-scene-build-result/v1",
      plan_id: "vsp-" + "1".repeat(24),
      scene_id: "mmg_040@0123456789abcdef",
      status: "failed",
      mutation_count: 1,
      rollback: { state: "completed", deleted_actor_names: ["safe-actor"], restored_player_start: true, failures: [] },
    },
  });
  const service = {
    async plan() { throw error; },
    async status() { throw error; },
    async preflight() { throw error; },
    async start() { throw error; },
  };
  const base = await fixtureServer(t, service);
  const failed = await request(`${base}/valid-run/build/plan`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
  assert.equal(failed.status, 502);
  assert.equal(failed.body.code, "SCENE_BUILD_EXECUTION_FAILED");
  assert.equal(JSON.stringify(failed.body).includes("/home/yhliu"), false);
  assert.equal(JSON.stringify(failed.body).includes("token=abc"), false);
  assert.equal(failed.body.result.rollback.state, "completed");

  const malformed = await request(`${base}/bad%20id/build/plan`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
  assert.equal(malformed.status, 400);
  assert.equal(malformed.body.code, "SCENE_BUILD_IMPORT_ID_INVALID");
});

test("unknown exceptions become a generic public error", () => {
  assert.deepEqual(publicError(new Error("database password")), {
    error: "VISTA scene build failed",
    code: "SCENE_BUILD_INTERNAL",
    retryable: false,
  });
});

test("build routes fail closed before service access without an active streaming lease", async (t) => {
  let called = false;
  const service = Object.fromEntries(["plan", "status", "preflight", "start"].map((operation) => [
    operation,
    async () => { called = true; return {}; },
  ]));
  const app = express();
  app.use(express.json());
  app.use("/api/vista/imports", createVistaSceneBuildRouter({
    service,
    resolveIdentity: () => ({ ownerId: "owner-route", sessionId: "session-route" }),
  }));
  const server = http.createServer(app);
  const port = await listen(server);
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const response = await request(`http://127.0.0.1:${port}/api/vista/imports/valid-run/build/plan`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
  assert.equal(response.status, 503);
  assert.equal(response.body.code, "SCENE_BUILD_RUNTIME_UNAVAILABLE");
  assert.equal(called, false);
});

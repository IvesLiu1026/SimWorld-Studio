"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const express = require("express");

const { createVistaWorldRouter } = require("../vista-world-routes");

const IDENTITY = Object.freeze({
  ownerId: "owner-route",
  sessionId: "studio-route",
  leaseId: "lease-route",
  slotId: 3,
  mcpPort: 55573,
});

async function withServer(t, service, resolveIdentity = () => IDENTITY) {
  const app = express();
  app.use(express.json());
  app.use("/api/vista-world", createVistaWorldRouter({ service, resolveIdentity }));
  const server = await new Promise((resolve) => {
    const instance = app.listen(0, "127.0.0.1", () => resolve(instance));
  });
  t.after(() => new Promise((resolve) => server.close(resolve)));
  return `http://127.0.0.1:${server.address().port}/api/vista-world`;
}

function fakeService() {
  const calls = [];
  const service = { calls };
  for (const method of ["compile", "revision", "createSession", "status", "action", "startEvent", "resetEvent"]) {
    service[method] = async (...args) => {
      calls.push({ method, args });
      return { method };
    };
  }
  return service;
}

test("router exposes only the typed compile/session/action/event surface", async (t) => {
  const service = fakeService();
  const base = await withServer(t, service);
  const sessionId = `vws-${"a".repeat(24)}`;
  const requests = [
    fetch(`${base}/compile`, { method: "POST", headers: { "content-type": "application/json" }, body: '{"revision":"r1"}' }),
    fetch(`${base}/revisions/r1`),
    fetch(`${base}/sessions`, { method: "POST", headers: { "content-type": "application/json" }, body: '{"revision":"r1"}' }),
    fetch(`${base}/sessions/${sessionId}`),
    fetch(`${base}/sessions/${sessionId}/actions`, { method: "POST", headers: { "content-type": "application/json" }, body: '{"kind":"interaction"}' }),
    fetch(`${base}/sessions/${sessionId}/events/mmg_044/start`, { method: "POST", headers: { "content-type": "application/json" }, body: '{"generation":0}' }),
    fetch(`${base}/sessions/${sessionId}/events/reset`, { method: "POST", headers: { "content-type": "application/json" }, body: '{"generation":1}' }),
  ];
  const responses = await Promise.all(requests);
  assert.deepEqual(responses.map((response) => response.status), [200, 200, 201, 200, 200, 200, 200]);
  assert.equal(responses.every((response) => response.headers.get("cache-control") === "no-store"), true);
  assert.deepEqual(service.calls.map((call) => call.method).sort(), [
    "action", "compile", "createSession", "resetEvent", "revision", "startEvent", "status",
  ]);
  assert.equal(service.calls.every((call) => call.args.at(-1).leaseId === IDENTITY.leaseId), true);
});

test("router redacts dependency errors and does not reflect secret paths", async (t) => {
  const service = fakeService();
  service.action = async () => {
    throw new Error("failed at /home/yhliu/.config/secret?token=bad");
  };
  const base = await withServer(t, service);
  const response = await fetch(`${base}/sessions/vws-${"a".repeat(24)}/actions`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: "{}",
  });
  const body = await response.json();
  assert.equal(response.status, 500);
  assert.deepEqual(body, {
    error: "VISTA world request failed.",
    code: "VISTA_WORLD_INTERNAL",
    retryable: false,
  });
  assert.equal(JSON.stringify(body).includes("/home"), false);
  assert.equal(JSON.stringify(body).includes("token"), false);
});

test("router fails closed before service when active identity is absent", async (t) => {
  const service = fakeService();
  service.revision = async (_revision, identity) => {
    if (!identity.leaseId) {
      const error = new Error("missing identity");
      error.code = "VISTA_WORLD_ACCESS_INVALID";
      error.status = 503;
      error.retryable = true;
      throw error;
    }
    return {};
  };
  const base = await withServer(t, service, () => ({ ownerId: "owner-only" }));
  const response = await fetch(`${base}/revisions/r1`);
  const body = await response.json();
  assert.equal(response.status, 503);
  assert.equal(body.code, "VISTA_WORLD_ACCESS_INVALID");
  assert.equal(body.error, "An active Studio world session is required.");
});

test("router returns authoritative generation after a typed runtime conflict", async (t) => {
  const service = fakeService();
  service.action = async () => {
    const error = new Error("runtime detail stays private");
    error.code = "VISTA_WORLD_GENERATION_STALE";
    error.status = 409;
    error.generation = 7;
    throw error;
  };
  const base = await withServer(t, service);
  const response = await fetch(`${base}/sessions/vws-${"a".repeat(24)}/actions`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: "{}",
  });
  assert.equal(response.status, 409);
  assert.deepEqual(await response.json(), {
    error: "The world session generation is stale.",
    code: "VISTA_WORLD_GENERATION_STALE",
    retryable: false,
    generation: 7,
  });
});

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const express = require("express");

const {
  createVistaAnimationTimelineRouter,
  errorStatus,
  publicError,
} = require("../vista-animation-timeline-routes");

const IMPORT_ID = `vim_${"a".repeat(64)}`;
const RUN_ID = `vtr-${"b".repeat(24)}`;
const IDENTITY = Object.freeze({
  ownerId: "owner-route-test",
  sessionId: "session-route-test",
  leaseId: "lease-route-test",
  slotId: 4,
  mcpPort: 55567,
});

async function withServer(t, service, resolveIdentity = () => IDENTITY) {
  const app = express();
  app.use(express.json());
  app.use("/api/vista/imports", createVistaAnimationTimelineRouter({ service, resolveIdentity }));
  const server = await new Promise((resolve) => {
    const instance = app.listen(0, "127.0.0.1", () => resolve(instance));
  });
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const address = server.address();
  return `http://127.0.0.1:${address.port}/api/vista/imports`;
}

function fakeService() {
  const calls = [];
  const result = (operation) => ({ schema: `test-${operation}/v1`, operation });
  return {
    calls,
    async preflight(importId, body, identity) {
      calls.push({ operation: "preflight", importId, body, identity });
      return result("preflight");
    },
    async start(importId, body, identity) {
      calls.push({ operation: "start", importId, body, identity });
      return result("start");
    },
    async status(importId, runId, identity) {
      calls.push({ operation: "status", importId, runId, identity });
      return result("status");
    },
    async stop(importId, runId, body, identity) {
      calls.push({ operation: "stop", importId, runId, body, identity });
      return result("stop");
    },
    async replay(importId, runId, body, identity) {
      calls.push({ operation: "replay", importId, runId, body, identity });
      return result("replay");
    },
  };
}

test("router exposes the bounded preflight/start/status/stop/replay contract", async (t) => {
  const service = fakeService();
  const base = await withServer(t, service);
  const confirmation = {
    plan_id: `vsp-${"c".repeat(24)}`,
    preflight_id: `vap-${"d".repeat(24)}`,
    timeline_id: `vtl-${"e".repeat(24)}`,
    program_id: `vag-${"f".repeat(24)}`,
    confirm: true,
  };

  const preflight = await fetch(`${base}/${IMPORT_ID}/animation/preflight`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ plan_id: confirmation.plan_id }),
  });
  assert.equal(preflight.status, 200);
  assert.equal(preflight.headers.get("cache-control"), "no-store");

  const start = await fetch(`${base}/${IMPORT_ID}/animation/start`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(confirmation),
  });
  assert.equal(start.status, 202);
  const status = await fetch(`${base}/${IMPORT_ID}/animation/runs/${RUN_ID}`);
  assert.equal(status.status, 200);
  const stop = await fetch(`${base}/${IMPORT_ID}/animation/runs/${RUN_ID}/stop`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: "{}",
  });
  assert.equal(stop.status, 202);
  const replay = await fetch(`${base}/${IMPORT_ID}/animation/runs/${RUN_ID}/replay`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(confirmation),
  });
  assert.equal(replay.status, 202);

  assert.deepEqual(service.calls.map((call) => call.operation), ["preflight", "start", "status", "stop", "replay"]);
  assert.deepEqual(service.calls[0].body, { plan_id: confirmation.plan_id });
  assert.equal(service.calls[1].identity.signal, undefined, "start must survive HTTP disconnects after acceptance");
  assert.equal(service.calls[0].identity.signal instanceof AbortSignal, true, "read-only preflight is abortable");
  assert.deepEqual(service.calls[3].body, {});
  assert.deepEqual(service.calls[4].body, confirmation);
});

test("router fails closed without a full server-resolved active identity", async (t) => {
  const service = fakeService();
  const base = await withServer(t, service, () => ({ ownerId: "owner-only" }));
  const response = await fetch(`${base}/${IMPORT_ID}/animation/preflight`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ plan_id: `vsp-${"c".repeat(24)}` }),
  });
  const body = await response.json();

  assert.equal(response.status, 503);
  assert.deepEqual(body, {
    error: "An active Studio animation session is required.",
    code: "ANIMATION_ACCESS_INVALID",
    retryable: true,
  });
  assert.equal(service.calls.length, 0);
});

test("route errors expose only allowlisted codes and fixed messages", async (t) => {
  const service = fakeService();
  service.preflight = async () => {
    const error = new Error("failed at /home/private/animation/secrets.json?token=super-secret");
    error.code = "BROKEN_PRIVATE_PROVIDER";
    throw error;
  };
  const base = await withServer(t, service);
  const response = await fetch(`${base}/${IMPORT_ID}/animation/preflight`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ plan_id: `vsp-${"c".repeat(24)}` }),
  });
  const body = await response.json();

  assert.equal(response.status, 500);
  assert.equal(body.code, "ANIMATION_TIMELINE_INTERNAL");
  assert.equal(JSON.stringify(body).includes("private"), false);
  assert.equal(JSON.stringify(body).includes("super-secret"), false);
});

test("public error status respects service statuses and never reflects dependency messages", () => {
  const error = Object.assign(new Error("/Game/Private/Secret.Secret"), {
    code: "ANIMATION_RUNTIME_NOT_READY",
    status: 503,
    retryable: true,
  });
  const body = publicError(error);
  assert.deepEqual(body, {
    error: "The trusted VISTA animation runtime is not ready.",
    code: "ANIMATION_RUNTIME_NOT_READY",
    retryable: true,
  });
  assert.equal(errorStatus(error, body), 503);
  assert.throws(() => createVistaAnimationTimelineRouter({ service: fakeService() }), TypeError);
});

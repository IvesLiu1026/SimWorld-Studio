"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const express = require("express");

const { VistaRuntimeError } = require("../vista-runtime-broker");
const { createVistaRuntimeRouter, publicError } = require("../vista-runtime-routes");

const IDENTITY = Object.freeze({
  ownerId: "owner-route",
  sessionId: "session-route",
  slotId: 4,
  leaseId: "lease-route",
  mcpPort: 55567,
});

function listen(server) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => resolve(server.address().port));
  });
}

async function fixture(t, registry) {
  const app = express();
  app.use(express.json());
  app.use("/api/vista", createVistaRuntimeRouter({ registry }));
  const server = http.createServer(app);
  const port = await listen(server);
  t.after(() => new Promise((resolve) => server.close(resolve)));
  return `http://127.0.0.1:${port}/api/vista`;
}

async function request(url, options) {
  const response = await fetch(url, options);
  return {
    status: response.status,
    cacheControl: response.headers.get("cache-control"),
    retryAfter: response.headers.get("retry-after"),
    body: await response.json(),
  };
}

function registryFixture(calls) {
  return {
    async exactIdentityFromRequest(req) {
      calls.push({ operation: "identity", method: req.method });
      return IDENTITY;
    },
    async startForIdentity(identity, options) {
      calls.push({ operation: "start", identity, signal: options.signal });
      return { schema: "vista-runtime-setup/v2", phase: "live", pie: true, possessed: true };
    },
    async stateForIdentity(identity, options) {
      calls.push({ operation: "state", identity, signal: options.signal });
      return { schema: "vista-runtime-state/v2", pie: true, possessed: true };
    },
    async stopForIdentity(identity, options) {
      calls.push({ operation: "stop", identity, signal: options.signal });
      return { schema: "vista-runtime-stop/v2", phase: "stopped", confirmed_stopped: true, ended_pie: true };
    },
  };
}

test("runtime routes derive identity server-side and expose only fixed empty Start/state/Stop operations", async (t) => {
  const calls = [];
  const base = await fixture(t, registryFixture(calls));
  const started = await request(`${base}/setup_vista_play_mode`, { method: "POST" });
  const state = await request(`${base}/get_vista_state?ignored_caller_code=owned`, { method: "GET" });
  const stopped = await request(`${base}/stop_vista_play_mode`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });

  assert.deepEqual([started.status, state.status, stopped.status], [200, 200, 200]);
  assert.equal(started.cacheControl, "no-store");
  assert.deepEqual(calls.map(({ operation }) => operation), [
    "identity", "start", "identity", "state", "identity", "stop",
  ]);
  for (const call of calls.filter((entry) => entry.identity)) {
    assert.deepEqual(call.identity, IDENTITY);
    assert.ok(call.signal instanceof AbortSignal);
  }
});

test("runtime Start/Stop reject caller-controlled fields before identity or UE access", async (t) => {
  const calls = [];
  const base = await fixture(t, registryFixture(calls));
  for (const route of ["setup_vista_play_mode", "stop_vista_play_mode"]) {
    const response = await request(`${base}/${route}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ python: "raise RuntimeError('owned')", pawn_class: "/Game/Other" }),
    });
    assert.equal(response.status, 400);
    assert.equal(response.body.code, "VISTA_EMPTY_BODY_REQUIRED");
  }
  assert.deepEqual(calls, []);
});

test("runtime errors are allowlisted, sanitized, and preserve bounded retry metadata", async (t) => {
  const registry = registryFixture([]);
  registry.startForIdentity = async () => {
    throw new VistaRuntimeError("VISTA_RUNTIME_BUSY", "secret /home/yhliu/file token=abc", {
      status: 429,
      retryable: true,
      retryAfterMs: 1250,
    });
  };
  const base = await fixture(t, registry);
  const response = await request(`${base}/setup_vista_play_mode`, { method: "POST" });
  assert.equal(response.status, 429);
  assert.equal(response.retryAfter, "2");
  assert.deepEqual(response.body, {
    code: "VISTA_RUNTIME_BUSY",
    error: "The UE runtime is busy.",
    retryable: true,
    retry_after_ms: 1250,
  });
  assert.equal(JSON.stringify(response.body).includes("/home/yhliu"), false);
  assert.deepEqual(publicError(new Error("database password")), {
    status: 500,
    headers: {},
    body: {
      code: "VISTA_RUNTIME_INTERNAL",
      error: "VISTA runtime request failed.",
      retryable: false,
    },
  });
});

test("runtime router refuses incomplete controller registries", () => {
  assert.throws(() => createVistaRuntimeRouter({ registry: {} }), TypeError);
});

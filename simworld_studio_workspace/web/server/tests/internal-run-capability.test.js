"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");

const { buildBuilderChildEnv } = require("../builder-runtime-authority");
const {
  createInternalRunCapabilityRegistry,
  isInternalCapabilityCandidate,
} = require("../internal-run-capability");

function identity(slotId) {
  return Object.freeze({
    ownerId: `owner-${slotId}`,
    sessionId: `session-${slotId}`,
    slotId,
    leaseId: `lease-${slotId}`,
    mcpPort: 55559 + slotId * 2,
  });
}

function key(value) {
  return `${value.ownerId}:${value.sessionId}:${value.slotId}:${value.leaseId}:${value.mcpPort}`;
}

function fixture(options = {}) {
  const identities = [identity(0), identity(1)];
  const active = new Set(identities.map(key));
  const ue = new Map(identities.map((item) => [key(item), {
    port: item.mcpPort,
    send: async () => `ue-slot-${item.slotId}`,
  }]));
  const ucv = new Map(identities.map((item) => [key(item), {
    port: 9017 + item.slotId,
    send: async () => `ucv-slot-${item.slotId}`,
  }]));
  const registry = createInternalRunCapabilityRegistry({
    isActiveSessionBinding: (item) => active.has(key(item)),
    resolveUeBroker: (item) => ue.get(key(item)) || null,
    resolveUcvBroker: (item) => ucv.get(key(item)) || null,
    ...options,
  });
  return { active, identities, registry, ucv, ue };
}

test("two run capabilities dispatch to their exact lease brokers and ignore caller routing fields", async () => {
  const { identities, registry } = fixture();
  const first = registry.issue({ identity: identities[0], runId: "run-a", scopeId: "scope-a" });
  const second = registry.issue({ identity: identities[1], runId: "run-b", scopeId: "scope-b" });

  const selectedA = registry.authorize({
    capability: first.capability,
    runId: first.runId,
    channel: "ue",
    body: { type: "get_actors_in_level", sessionId: "session-1", UNREAL_PORT: 55561 },
  });
  const selectedB = registry.authorize({
    capability: second.capability,
    runId: second.runId,
    channel: "ucv",
    body: { cmd: "vget /objects", sessionId: "session-0", UCV_PORT: 9017 },
  });
  const selectedAssets = registry.authorize({
    capability: first.capability,
    runId: first.runId,
    channel: "assets",
    body: { query: "wooden chair", k: 4 },
  });
  assert.equal(await selectedA.broker.send(), "ue-slot-0");
  assert.equal(await selectedB.broker.send(), "ucv-slot-1");
  assert.deepEqual(selectedA.identity, identities[0]);
  assert.deepEqual(selectedB.identity, identities[1]);
  assert.equal(selectedAssets.broker, null);
  assert.deepEqual(selectedAssets.identity, identities[0]);
  assert.throws(
    () => registry.authorize({ capability: first.capability, runId: second.runId, channel: "ue", body: {} }),
    (error) => error.code === "INTERNAL_RUN_CAPABILITY_INVALID",
  );
});

test("semantic asset broker is an exact capability candidate and operation-policy channel", () => {
  const seen = [];
  const { identities, registry } = fixture({
    operationPolicy(context) {
      seen.push(context);
      return { allowed: context.channel === "assets" && context.body.query === "market stall" };
    },
  });
  const issued = registry.issue({ identity: identities[0], runId: "run-assets", scopeId: "scope-assets" });
  const request = {
    method: "POST",
    path: "/api/internal/assets",
    headers: {
      "x-simworld-run-capability": issued.capability,
      "x-simworld-run-id": issued.runId,
    },
  };
  assert.equal(isInternalCapabilityCandidate(request), true);
  const authorized = registry.authorize({
    capability: issued.capability,
    runId: issued.runId,
    channel: "assets",
    body: { query: "market stall" },
  });
  assert.equal(authorized.scopeId, "scope-assets");
  assert.equal(seen.length, 1);
  assert.equal(seen[0].channel, "assets");
  assert.equal(isInternalCapabilityCandidate({ ...request, path: "/api/internal/assets/extra" }), false);
});

test("capabilities fail closed after lease revocation, process exit, explicit stop, and TTL", () => {
  let now = 1000;
  const timers = [];
  const cleaned = [];
  const { active, identities, registry } = fixture({
    now: () => now,
    idleTtlMs: 100,
    hardTtlMs: 500,
    setTimeoutFn(fn, delay) {
      const timer = { fn, delay, cleared: false, unref() {} };
      timers.push(timer);
      return timer;
    },
    clearTimeoutFn(timer) { timer.cleared = true; },
  });

  const revokedLease = registry.issue({
    identity: identities[0], runId: "run-lease", scopeId: "scope-lease", cleanup: (reason) => cleaned.push(reason),
  });
  active.delete(key(identities[0]));
  assert.throws(
    () => registry.authorize({ capability: revokedLease.capability, runId: revokedLease.runId, channel: "ue", body: {} }),
    (error) => error.code === "INTERNAL_RUN_LEASE_INVALID",
  );

  active.add(key(identities[0]));
  const processRun = registry.issue({ identity: identities[0], runId: "run-process", scopeId: "scope-process" });
  const child = new EventEmitter();
  registry.attachProcess(processRun.capability, child);
  child.emit("exit", 0);
  assert.throws(
    () => registry.authorize({ capability: processRun.capability, runId: processRun.runId, channel: "ue", body: {} }),
    (error) => error.code === "INTERNAL_RUN_CAPABILITY_INVALID",
  );

  const stopped = registry.issue({ identity: identities[0], runId: "run-stop", scopeId: "scope-stop" });
  assert.equal(registry.revokeScope({ scopeId: stopped.scopeId, runId: stopped.runId }, "user_stop"), 1);
  const expiring = registry.issue({ identity: identities[0], runId: "run-expire", scopeId: "scope-expire" });
  const expiryTimer = timers.at(-1);
  now = expiring.idleExpiresAt;
  expiryTimer.fn();
  assert.throws(
    () => registry.authorize({ capability: expiring.capability, runId: expiring.runId, channel: "ue", body: {} }),
    (error) => error.code === "INTERNAL_RUN_CAPABILITY_INVALID",
  );
  assert.ok(cleaned.includes("lease_revoked"));
});

test("trusted child environment contains only run authority and strips root/token-file and direct ports", () => {
  const runtime = {
    capability: "a".repeat(43),
    runId: "run-safe",
    serverPort: 3443,
  };
  const env = buildBuilderChildEnv({
    STUDIO_ACCESS_TOKEN: "root-bearer",
    STUDIO_ACCESS_TOKEN_FILE: "/run/secrets/studio",
    SIMWORLD_MCP_CONFIG: "/tmp/caller.json",
    UNREAL_HOST: "attacker.example",
    UNREAL_PORT: "1",
    UCV_PORT: "2",
    OPENAI_API_KEY: "provider-key",
  }, runtime);
  assert.equal(env.STUDIO_ACCESS_TOKEN, undefined);
  assert.equal(env.STUDIO_ACCESS_TOKEN_FILE, undefined);
  assert.equal(env.SIMWORLD_MCP_CONFIG, undefined);
  assert.equal(env.UNREAL_PORT, undefined);
  assert.equal(env.UCV_PORT, undefined);
  assert.equal(env.SIMWORLD_INTERNAL_RUN_CAPABILITY, runtime.capability);
  assert.equal(env.SIMWORLD_INTERNAL_RUN_ID, runtime.runId);
  assert.equal(env.SIMWORLD_INTERNAL_CAPABILITY_REQUIRED, "1");
  assert.equal(env.SIMWORLD_BROKER_HOST, "127.0.0.1");
  assert.equal(env.PORT, "3443");
  assert.equal(env.OPENAI_API_KEY, "provider-key");
});

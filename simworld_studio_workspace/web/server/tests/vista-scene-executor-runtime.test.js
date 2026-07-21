"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const {
  createVistaSlotBrokerResolver,
  resolveVistaSceneExecutorConfig,
} = require("../vista-scene-executor-runtime");

const COMPLETE = Object.freeze({
  VISTA_UE_CONTENT_REVISION: "content-r1",
  VISTA_ASSET_VERIFICATION_REVISION: "verification-r1",
  VISTA_UE_CONTENT_RECEIPT_SHA256: "a".repeat(64),
});

test("scene executor config is disabled by default and treats its three trust pins atomically", () => {
  assert.deepEqual(resolveVistaSceneExecutorConfig({}), { enabled: false });
  for (const missing of Object.keys(COMPLETE)) {
    const env = { ...COMPLETE };
    delete env[missing];
    assert.throws(() => resolveVistaSceneExecutorConfig(env), /configured together/);
  }
  assert.throws(() => resolveVistaSceneExecutorConfig({
    ...COMPLETE,
    VISTA_UE_CONTENT_RECEIPT_SHA256: "A".repeat(64),
  }), /lowercase SHA-256/);
});

test("production executor requires and matches the verified semantic asset runtime", () => {
  assert.throws(() => resolveVistaSceneExecutorConfig({ ...COMPLETE, NODE_ENV: "production" }), /semantic asset runtime/);
  assert.throws(() => resolveVistaSceneExecutorConfig({ ...COMPLETE, NODE_ENV: "production" }, {
    assetConfig: { enabled: true, ueContentRevision: "stale" },
  }), /does not match/);
  assert.equal(resolveVistaSceneExecutorConfig({ ...COMPLETE, NODE_ENV: "production" }, {
    assetConfig: { enabled: true, ueContentRevision: COMPLETE.VISTA_UE_CONTENT_REVISION },
  }).enabled, true);
});

test("slot broker resolver revalidates every lease and only dials loopback ports", () => {
  let active = true;
  let checks = 0;
  const created = [];
  class FakeBroker {
    constructor(options) {
      this.host = options.host;
      this.port = options.port;
      this.send = async () => ({});
      created.push(options);
    }
  }
  const defaultBroker = { port: 55559, send: async () => ({}) };
  const resolve = createVistaSlotBrokerResolver({
    studioStreaming: {
      isActiveSessionBinding(binding) {
        checks += 1;
        assert.deepEqual(Object.keys(binding).sort(), ["leaseId", "mcpPort", "ownerId", "sessionId", "slotId"]);
        return active && binding.leaseId === "lease-1";
      },
    },
    defaultBroker,
    BrokerClass: FakeBroker,
  });
  const base = { ownerId: "owner", sessionId: "session", leaseId: "lease-1", slotId: 0, mcpPort: 55559 };
  assert.equal(resolve({ ...base, signal: new AbortController().signal }), defaultBroker);
  const other = resolve({ ...base, slotId: 1, mcpPort: 55561 });
  assert.equal(other, resolve({ ...base, slotId: 1, mcpPort: 55561 }));
  assert.deepEqual(created, [{ host: "127.0.0.1", port: 55561 }]);
  active = false;
  assert.equal(resolve(base), null);
  assert.equal(checks, 4, "every resolution must revalidate the active lease");
});

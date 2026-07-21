"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { SessionManager } = require("../session-manager");

const OWNER_ID = `browser-${"a".repeat(64)}`;

function leaseIdentity(record) {
  return {
    ownerId: record.userId,
    slotId: record.slotId,
    leaseId: record.leaseId,
    mcpPort: record.uePorts.mcpPort,
  };
}

test("active lease lookup returns only an exact non-secret ready binding", async () => {
  let now = 10_000;
  const manager = new SessionManager({
    clock: () => now,
    sessionTtlMs: 1_000,
    sessionHardMaxMs: 5_000,
  });
  try {
    const record = await manager.acquire(OWNER_ID);
    const identity = leaseIdentity(record);
    const active = manager.resolveActiveLease(identity);
    assert.deepEqual(active, identity);
    assert.equal(Object.isFrozen(active), true);
    assert.equal(Object.hasOwn(active, "token"), false);
    assert.deepEqual(manager.resolveActiveLeaseRuntime(identity), {
      ...identity,
      ucvPort: record.uePorts.ucvPort,
    });

    for (const altered of [
      { ...identity, ownerId: `browser-${"b".repeat(64)}` },
      { ...identity, slotId: identity.slotId + 1 },
      { ...identity, leaseId: "different-active-lease" },
      { ...identity, mcpPort: identity.mcpPort + 1 },
      { ...identity, token: record.token },
    ]) {
      assert.equal(manager.resolveActiveLease(altered), null);
    }

    record.mcpReady = false;
    assert.equal(manager.resolveActiveLease(identity), null);
    record.mcpReady = true;
    now += 100;
    assert.deepEqual(manager.resolveActiveLease(identity), identity);
    assert.equal(record.lastActivity, 10_000);
  } finally {
    manager.destroy();
  }
});

test("release and reacquire invalidate the old lease and grant a distinct active lease", async () => {
  const manager = new SessionManager({
    clock: () => 30_000,
    sessionTtlMs: 1_000,
    sessionHardMaxMs: 5_000,
  });
  try {
    const first = await manager.acquire(OWNER_ID);
    const firstIdentity = leaseIdentity(first);
    assert.ok(manager.resolveActiveLease(firstIdentity));

    manager.release(first.token);
    assert.equal(manager.resolveActiveLease(firstIdentity), null);

    const second = await manager.acquire(OWNER_ID);
    const secondIdentity = leaseIdentity(second);
    assert.equal(second.userId, first.userId);
    assert.notEqual(second.token, first.token);
    assert.notEqual(second.leaseId, first.leaseId);
    assert.equal(manager.resolveActiveLease(firstIdentity), null);
    assert.deepEqual(manager.resolveActiveLease(secondIdentity), secondIdentity);
  } finally {
    manager.destroy();
  }
});

test("idle and hard lease expiry are synchronous and cannot be revived by touch", async () => {
  let now = 20_000;
  const manager = new SessionManager({
    clock: () => now,
    sessionTtlMs: 100,
    sessionHardMaxMs: 200,
  });
  const releases = [];
  manager.on("released", (event) => releases.push(event.reason));
  try {
    const idleRecord = await manager.acquire(OWNER_ID);
    const idleIdentity = leaseIdentity(idleRecord);
    now += 101;
    assert.equal(manager.resolveActiveLease(idleIdentity), null);
    assert.equal(manager.touch(idleRecord.token), null);
    assert.equal(manager.activeSessions, 0);
    assert.deepEqual(releases, ["idle_timeout"]);

    const hardRecord = await manager.acquire(OWNER_ID);
    const hardIdentity = leaseIdentity(hardRecord);
    now += 50;
    assert.ok(manager.touch(hardRecord.token));
    now += 51;
    assert.ok(manager.resolveActiveLease(hardIdentity));
    now += 49;
    assert.ok(manager.touch(hardRecord.token));
    now += 51;
    assert.equal(manager.resolveActiveLease(hardIdentity), null);
    assert.equal(manager.activeSessions, 0);
    assert.deepEqual(releases, ["idle_timeout", "hard_limit"]);
  } finally {
    manager.destroy();
  }
});

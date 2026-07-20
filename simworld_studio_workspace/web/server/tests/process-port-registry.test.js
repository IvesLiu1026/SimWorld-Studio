"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");

const {
  REGISTRY_SCHEMA,
  ProcessPortRegistryError,
  createProcessPortRegistry,
} = require("../process-port-registry");

async function tempRegistryDir(t) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "simworld-process-registry-"));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  return directory;
}

function identityProbe(identities) {
  return async (pid) => {
    const identity = identities.get(pid);
    if (identity instanceof Error) throw identity;
    if (identity === false || identity === undefined) return { alive: false, startToken: null };
    if (identity === null) return { alive: true, startToken: null };
    return { alive: true, startToken: identity };
  };
}

function uuidSequence(prefix) {
  let value = 0;
  return () => `${prefix}-${++value}`;
}

function makeRegistry(directory, identities, overrides = {}) {
  return createProcessPortRegistry({
    directory,
    processProbe: identityProbe(identities),
    listenerProbe: async () => [],
    randomUUID: uuidSequence(overrides.uuidPrefix || "test"),
    now: () => Date.parse("2026-07-14T04:00:00.000Z"),
    lockAttempts: 20,
    lockRetryMs: 1,
    ...overrides,
  });
}

function leaseRequest(overrides = {}) {
  return {
    stackId: "studio-stack-a",
    slotId: "slot-0",
    gpuId: "gpu-0",
    portFamily: "studio-ue-cirrus-v1",
    ports: [
      { name: "studio", host: "127.0.0.1", protocol: "tcp", port: 3002 },
      { name: "mcp", host: "127.0.0.1", protocol: "tcp", port: 55559 },
      { name: "cirrus", host: "127.0.0.1", protocol: "tcp", port: 8585 },
    ],
    ownerPid: 1001,
    ownerStartToken: "linux:1001",
    ...overrides,
  };
}

function expectRegistryError(code) {
  return (error) => error instanceof ProcessPortRegistryError && error.code === code;
}

test("acquire atomically records slot, GPU, port family, owner PID, and a derived start token", async (t) => {
  const directory = await tempRegistryDir(t);
  const identities = new Map([[1001, "linux:1001"]]);
  const registry = makeRegistry(directory, identities);

  const acquired = await registry.acquire(leaseRequest({ ownerStartToken: undefined }));
  assert.equal(acquired.acquired, true);
  assert.equal(acquired.lease.slotId, "slot-0");
  assert.equal(acquired.lease.gpuId, "gpu-0");
  assert.equal(acquired.lease.portFamily, "studio-ue-cirrus-v1");
  assert.equal(acquired.lease.owner.pid, 1001);
  assert.equal(acquired.lease.owner.startTokenFingerprint.length, 16);
  assert.equal(acquired.warnings.length, 0);

  const status = await registry.status();
  assert.equal(status.schema, REGISTRY_SCHEMA);
  assert.equal(status.revision, 1);
  assert.deepEqual(status.summary, { total: 1, alive: 1, stale: 0, unknown: 0 });
  assert.equal(status.leases[0].liveness, "alive");
  assert.equal(status.leases[0].ports[1].port, 55559);

  const stored = JSON.parse(await fs.readFile(path.join(directory, "process-port-registry.json"), "utf8"));
  assert.equal(stored.schema, REGISTRY_SCHEMA);
  assert.equal(stored.leases[0].owner.startToken, "linux:1001");
});

test("acquire rejects caller-supplied owner identity that does not match the live process start token", async (t) => {
  const directory = await tempRegistryDir(t);
  const identities = new Map([[1001, "linux:actual"]]);
  const registry = makeRegistry(directory, identities);

  await assert.rejects(
    registry.acquire(leaseRequest({ ownerStartToken: "linux:claimed" })),
    expectRegistryError("PROCESS_PORT_OWNER_IDENTITY_UNAVAILABLE"),
  );
  const status = await registry.status();
  assert.equal(status.summary.total, 0);
});

test("active leases reject same slot, same GPU, and overlapping ports", async (t) => {
  const directory = await tempRegistryDir(t);
  const identities = new Map([
    [1001, "linux:1001"],
    [1002, "linux:1002"],
  ]);
  const registry = makeRegistry(directory, identities);
  await registry.acquire(leaseRequest());

  await assert.rejects(
    registry.acquire(leaseRequest({
      stackId: "same-slot",
      gpuId: "gpu-2",
      ownerPid: 1002,
      ownerStartToken: "linux:1002",
      ports: [{ name: "other", port: 6001 }],
    })),
    (error) => expectRegistryError("PROCESS_PORT_CONFLICT")(error)
      && error.details.conflicts.some((conflict) => conflict.resourceType === "slot"),
  );

  await assert.rejects(
    registry.acquire(leaseRequest({
      stackId: "same-gpu",
      slotId: "slot-2",
      ownerPid: 1002,
      ownerStartToken: "linux:1002",
      ports: [{ name: "other", port: 6002 }],
    })),
    (error) => expectRegistryError("PROCESS_PORT_CONFLICT")(error)
      && error.details.conflicts.some((conflict) => conflict.resourceType === "gpu"),
  );

  await assert.rejects(
    registry.acquire(leaseRequest({
      stackId: "same-port",
      slotId: "slot-3",
      gpuId: "gpu-3",
      ownerPid: 1002,
      ownerStartToken: "linux:1002",
      ports: [{ name: "other", host: "127.0.0.1", protocol: "tcp", port: 8585 }],
    })),
    (error) => expectRegistryError("PROCESS_PORT_CONFLICT")(error)
      && error.details.conflicts.some((conflict) => conflict.resourceType === "port"),
  );
  assert.equal((await registry.status()).summary.total, 1);
});

test("wildcard listeners overlap an address in the same IP family while TCP and UDP remain distinct", async (t) => {
  const directory = await tempRegistryDir(t);
  const identities = new Map([
    [1001, "linux:1001"],
    [1002, "linux:1002"],
    [1003, "linux:1003"],
  ]);
  const registry = makeRegistry(directory, identities);
  await registry.acquire(leaseRequest({
    ports: [{ name: "wildcard", host: "0.0.0.0", protocol: "tcp", port: 9000 }],
  }));

  await assert.rejects(
    registry.acquire(leaseRequest({
      stackId: "loopback-tcp",
      slotId: "slot-2",
      gpuId: "gpu-2",
      ownerPid: 1002,
      ownerStartToken: "linux:1002",
      ports: [{ name: "loopback", host: "127.0.0.1", protocol: "tcp", port: 9000 }],
    })),
    (error) => expectRegistryError("PROCESS_PORT_CONFLICT")(error)
      && error.details.conflicts.some((conflict) => conflict.resourceType === "port"),
  );

  const udp = await registry.acquire(leaseRequest({
    stackId: "loopback-udp",
    slotId: "slot-3",
    gpuId: "gpu-3",
    ownerPid: 1003,
    ownerStartToken: "linux:1003",
    ports: [{ name: "loopback", host: "127.0.0.1", protocol: "udp", port: 9000 }],
  }));
  assert.equal(udp.acquired, true);
});

test("filesystem lock serializes concurrent conflicting acquisitions", async (t) => {
  const directory = await tempRegistryDir(t);
  const identities = new Map([
    [1001, "linux:1001"],
    [1002, "linux:1002"],
  ]);
  const firstRegistry = makeRegistry(directory, identities, { uuidPrefix: "first" });
  const secondRegistry = makeRegistry(directory, identities, { uuidPrefix: "second" });

  const results = await Promise.allSettled([
    firstRegistry.acquire(leaseRequest()),
    secondRegistry.acquire(leaseRequest({
      stackId: "studio-stack-b",
      ownerPid: 1002,
      ownerStartToken: "linux:1002",
    })),
  ]);
  assert.equal(results.filter((result) => result.status === "fulfilled").length, 1);
  const rejection = results.find((result) => result.status === "rejected");
  assert.ok(expectRegistryError("PROCESS_PORT_CONFLICT")(rejection.reason));
  assert.equal((await firstRegistry.status()).summary.total, 1);
});

test("confirmed dead PID is stale and is pruned atomically when the resources are reacquired", async (t) => {
  const directory = await tempRegistryDir(t);
  const identities = new Map([
    [1001, "linux:1001"],
    [1002, "linux:1002"],
  ]);
  const registry = makeRegistry(directory, identities);
  const oldLease = await registry.acquire(leaseRequest());
  identities.set(1001, false);

  const replacement = await registry.acquire(leaseRequest({
    stackId: "studio-stack-replacement",
    ownerPid: 1002,
    ownerStartToken: "linux:1002",
  }));
  assert.equal(replacement.cleanup.staleLeasesRemoved, 1);
  const status = await registry.status();
  assert.equal(status.summary.total, 1);
  assert.notEqual(status.leases[0].leaseId, oldLease.lease.leaseId);
  assert.equal(status.leases[0].owner.pid, 1002);
});

test("PID reuse is detected by start-token mismatch and stale cleanup never exposes the raw token", async (t) => {
  const directory = await tempRegistryDir(t);
  const identities = new Map([[1001, "super-secret-start-token"]]);
  const registry = makeRegistry(directory, identities);
  await registry.acquire(leaseRequest({ ownerStartToken: "super-secret-start-token" }));
  identities.set(1001, "reused-pid-token");

  const status = await registry.status();
  assert.equal(status.leases[0].liveness, "stale");
  assert.equal(status.leases[0].livenessReason, "OWNER_START_TOKEN_MISMATCH");
  assert.doesNotMatch(JSON.stringify(status), /super-secret-start-token|reused-pid-token/);

  const cleanup = await registry.cleanupStale();
  assert.equal(cleanup.removedCount, 1);
  assert.equal(cleanup.removed[0].liveness, "stale");
  assert.doesNotMatch(JSON.stringify(cleanup), /super-secret-start-token|reused-pid-token/);
  assert.equal((await registry.status()).summary.total, 0);
});

test("unknown process identity is retained and blocks conflicting startup", async (t) => {
  const directory = await tempRegistryDir(t);
  const identities = new Map([
    [1001, "linux:1001"],
    [1002, "linux:1002"],
  ]);
  const registry = makeRegistry(directory, identities);
  await registry.acquire(leaseRequest());
  identities.set(1001, new Error("probe details with bearer secret-value"));

  const status = await registry.status();
  assert.equal(status.leases[0].liveness, "unknown");
  assert.equal(status.leases[0].livenessReason, "OWNER_PROBE_FAILED");
  assert.doesNotMatch(JSON.stringify(status), /secret-value|probe details/i);
  assert.equal((await registry.cleanupStale()).removedCount, 0);

  await assert.rejects(
    registry.acquire(leaseRequest({
      stackId: "blocked-stack",
      ownerPid: 1002,
      ownerStartToken: "linux:1002",
    })),
    (error) => expectRegistryError("PROCESS_PORT_CONFLICT")(error)
      && error.details.conflicts.some((conflict) => conflict.liveness === "unknown"),
  );
});

test("listener probe receives only requested endpoints and unmanaged listeners reject by default", async (t) => {
  const directory = await tempRegistryDir(t);
  const identities = new Map([[1001, "linux:1001"]]);
  let probed = null;
  const registry = makeRegistry(directory, identities, {
    listenerProbe: async ({ ports }) => {
      probed = ports;
      return [ports[1]];
    },
  });

  await assert.rejects(
    registry.acquire(leaseRequest()),
    (error) => expectRegistryError("PROCESS_PORT_UNMANAGED_LISTENER")(error)
      && error.details.listeners[0].name === "mcp",
  );
  assert.deepEqual(probed, leaseRequest().ports);
  assert.equal((await registry.status()).summary.total, 0);
});

test("warn policy records managed conflicts and unmanaged listeners without leaking owner tokens", async (t) => {
  const directory = await tempRegistryDir(t);
  const identities = new Map([
    [1001, "owner-token-one"],
    [1002, "owner-token-two"],
  ]);
  const registry = makeRegistry(directory, identities, {
    policy: "warn",
    listenerProbe: async ({ ports }) => [ports[0]],
  });
  const first = await registry.acquire(leaseRequest({ ownerStartToken: "owner-token-one" }));
  assert.deepEqual(first.warnings.map((warning) => warning.code), ["PROCESS_PORT_UNMANAGED_LISTENER"]);

  const second = await registry.acquire(leaseRequest({
    stackId: "warning-stack",
    ownerPid: 1002,
    ownerStartToken: "owner-token-two",
  }));
  assert.deepEqual(
    second.warnings.map((warning) => warning.code),
    ["PROCESS_PORT_CONFLICT", "PROCESS_PORT_UNMANAGED_LISTENER"],
  );
  assert.equal((await registry.status()).summary.total, 2);
  assert.doesNotMatch(JSON.stringify(second), /owner-token-one|owner-token-two/);
});

test("listener probe failures follow independent reject or warn policy", async (t) => {
  const firstDirectory = await tempRegistryDir(t);
  const secondDirectory = await tempRegistryDir(t);
  const identities = new Map([[1001, "linux:1001"]]);
  const failingProbe = async () => { throw new Error("ss output and credential secret"); };
  const rejecting = makeRegistry(firstDirectory, identities, { listenerProbe: failingProbe });
  const warning = makeRegistry(secondDirectory, identities, {
    listenerProbe: failingProbe,
    policy: { probeFailure: "warn", conflicts: "reject", unmanagedListeners: "reject" },
  });

  await assert.rejects(
    rejecting.acquire(leaseRequest()),
    expectRegistryError("PROCESS_PORT_LISTENER_PROBE_FAILED"),
  );
  const acquired = await warning.acquire(leaseRequest());
  assert.deepEqual(acquired.warnings, [{ code: "PROCESS_PORT_LISTENER_PROBE_FAILED" }]);
  assert.doesNotMatch(JSON.stringify(acquired), /credential secret|ss output/i);
});

test("listener probe is mandatory and bounded by an abortable timeout", async (t) => {
  const directory = await tempRegistryDir(t);
  assert.throws(
    () => createProcessPortRegistry({ directory }),
    /listenerProbe must be a function/,
  );

  const identities = new Map([[1001, "linux:1001"]]);
  let signal = null;
  const registry = makeRegistry(directory, identities, {
    listenerProbeTimeoutMs: 10,
    listenerProbe: ({ signal: receivedSignal }) => {
      signal = receivedSignal;
      return new Promise(() => {});
    },
  });
  await assert.rejects(
    registry.acquire(leaseRequest()),
    expectRegistryError("PROCESS_PORT_LISTENER_PROBE_FAILED"),
  );
  assert.equal(signal.aborted, true);
  assert.equal((await registry.status()).summary.total, 0);
});

test("heartbeat and release require exact PID plus start token and release never kills a process", async (t) => {
  const directory = await tempRegistryDir(t);
  const identities = new Map([[1001, "linux:1001"]]);
  let processProbeCalls = 0;
  const registry = makeRegistry(directory, identities, {
    processProbe: async (pid) => {
      processProbeCalls += 1;
      return identityProbe(identities)(pid);
    },
  });
  const acquired = await registry.acquire(leaseRequest());
  const owner = {
    leaseId: acquired.lease.leaseId,
    ownerPid: 1001,
    ownerStartToken: "linux:1001",
  };

  await assert.rejects(
    registry.heartbeat({ ...owner, ownerStartToken: "wrong-token" }),
    expectRegistryError("PROCESS_PORT_LEASE_ACCESS_DENIED"),
  );
  assert.equal((await registry.heartbeat(owner)).updated, true);
  await assert.rejects(
    registry.release({ ...owner, ownerPid: 1002 }),
    expectRegistryError("PROCESS_PORT_LEASE_ACCESS_DENIED"),
  );
  assert.equal((await registry.release(owner)).released, true);
  assert.equal((await registry.release(owner)).released, false);
  assert.ok(processProbeCalls >= 1);
});

test("corrupt registry fails closed and is not overwritten", async (t) => {
  const directory = await tempRegistryDir(t);
  const registryPath = path.join(directory, "process-port-registry.json");
  const corrupt = "{\"schema\":\"unexpected\",\"authorization\":\"Bearer secret-value\"}\n";
  await fs.writeFile(registryPath, corrupt, { mode: 0o600 });
  const identities = new Map([[1001, "linux:1001"]]);
  const registry = makeRegistry(directory, identities);

  await assert.rejects(registry.status(), expectRegistryError("PROCESS_PORT_REGISTRY_CORRUPT"));
  await assert.rejects(registry.acquire(leaseRequest()), expectRegistryError("PROCESS_PORT_REGISTRY_CORRUPT"));
  assert.equal(await fs.readFile(registryPath, "utf8"), corrupt);
  await assert.rejects(fs.stat(`${registryPath}.lock`), (error) => error && error.code === "ENOENT");
});

test("existing filesystem lock fails busy without touching registry state", async (t) => {
  const directory = await tempRegistryDir(t);
  const lockPath = path.join(directory, "process-port-registry.json.lock");
  await fs.writeFile(lockPath, "occupied\n", { mode: 0o600 });
  const identities = new Map([[1001, "linux:1001"]]);
  const registry = makeRegistry(directory, identities, {
    lockAttempts: 2,
    lockRetryMs: 0,
    sleep: async () => {},
  });

  await assert.rejects(registry.acquire(leaseRequest()), expectRegistryError("PROCESS_PORT_REGISTRY_BUSY"));
  assert.equal(await fs.readFile(lockPath, "utf8"), "occupied\n");
  await assert.rejects(
    fs.stat(path.join(directory, "process-port-registry.json")),
    (error) => error && error.code === "ENOENT",
  );
});

test("invalid endpoints and listener results outside the requested allowlist fail before persistence", async (t) => {
  const directory = await tempRegistryDir(t);
  const identities = new Map([[1001, "linux:1001"]]);
  const registry = makeRegistry(directory, identities, {
    listenerProbe: async () => [{ name: "ssh", host: "127.0.0.1", protocol: "tcp", port: 22 }],
  });

  await assert.rejects(
    registry.acquire(leaseRequest({ ports: [{ name: "bad", host: "example.com", port: 3002 }] })),
    /IP literal/,
  );
  await assert.rejects(
    registry.acquire(leaseRequest()),
    expectRegistryError("PROCESS_PORT_LISTENER_PROBE_FAILED"),
  );
  assert.equal((await registry.status()).summary.total, 0);
});

'use strict';

const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const { SlotPool } = require('./slot-pool.js');

function fakeChild(pid = 4242) {
  const child = new EventEmitter();
  child.pid = pid;
  child.stdout = new EventEmitter();
  child.stderr = new EventEmitter();
  child.kill = () => {
    queueMicrotask(() => child.emit('exit', 0, 'SIGTERM'));
    return true;
  };
  return child;
}

function registryHarness({ acquireError = null } = {}) {
  const calls = [];
  return {
    calls,
    registry: {
      async acquire(request) {
        calls.push(['acquire', request]);
        if (acquireError) throw acquireError;
        return { acquired: true, lease: { leaseId: `lease-slot-${request.slotId}` } };
      },
      async heartbeat(owner) {
        calls.push(['heartbeat', owner]);
        return { updated: true };
      },
      async release(owner) {
        calls.push(['release', owner]);
        return { released: true };
      },
    },
  };
}

function poolOptions(overrides = {}) {
  return {
    poolSize: 2,
    gpuCount: 1,
    baseMcp: 61000,
    baseCirrusHttp: 62000,
    baseCirrusWs: 62100,
    baseCirrusSfu: 62200,
    baseUcv: 62300,
    portStride: 2,
    startupTimeout: 100,
    registryHeartbeatMs: 0,
    processProbe: async () => ({ alive: true, startToken: 'linux:12345' }),
    waitForPortImpl: async () => true,
    logger: () => {},
    ...overrides,
  };
}

test('slot acquires exact endpoint lease before spawn, heartbeats, and releases after stop', async () => {
  const harness = registryHarness();
  const order = [];
  const pool = new SlotPool(poolOptions({
    registry: harness.registry,
    spawnImpl: () => {
      order.push('spawn');
      return fakeChild();
    },
  }));
  const originalAcquire = harness.registry.acquire;
  harness.registry.acquire = async (request) => {
    order.push('acquire');
    return originalAcquire(request);
  };

  const started = await pool.start(0);
  assert.deepEqual(order, ['acquire', 'spawn']);
  assert.equal(started.slotId, 0);
  assert.equal(pool.snapshot()[0].registryManaged, true);

  const request = harness.calls.find(([operation]) => operation === 'acquire')[1];
  assert.equal(request.stackId.length, 33);
  assert.equal(request.slotId, 'ue-slot-0');
  assert.equal(request.gpuId, 'gpu-0-slot-0');
  assert.deepEqual(
    request.ports.map(({ name, host, protocol, port }) => ({ name, host, protocol, port })),
    [
      { name: 'mcp', host: '127.0.0.1', protocol: 'tcp', port: 61000 },
      { name: 'cirrus_http', host: '127.0.0.1', protocol: 'tcp', port: 62000 },
      { name: 'cirrus_streamer', host: '127.0.0.1', protocol: 'tcp', port: 62100 },
      { name: 'cirrus_sfu', host: '127.0.0.1', protocol: 'tcp', port: 62200 },
      { name: 'ucv', host: '127.0.0.1', protocol: 'tcp', port: 62300 },
    ],
  );
  assert.equal(request.ownerStartToken, 'linux:12345');

  await pool._heartbeatRegistry();
  await pool.stop(0);
  assert.equal(pool.status(0), 'down');
  assert.equal(pool.snapshot()[0].registryManaged, false);
  assert.equal(harness.calls.filter(([operation]) => operation === 'heartbeat').length, 1);
  assert.equal(harness.calls.filter(([operation]) => operation === 'release').length, 1);
  assert.doesNotMatch(JSON.stringify(pool.snapshot()), /linux:12345/);
});

test('registry rejection fails before a slot process is spawned', async () => {
  const conflict = Object.assign(new Error('sensitive registry detail'), {
    code: 'PROCESS_PORT_CONFLICT',
  });
  const harness = registryHarness({ acquireError: conflict });
  let spawnCount = 0;
  const pool = new SlotPool(poolOptions({
    registry: harness.registry,
    spawnImpl: () => {
      spawnCount += 1;
      return fakeChild();
    },
  }));

  await assert.rejects(pool.start(1), (error) => error === conflict);
  assert.equal(spawnCount, 0);
  assert.equal(pool.status(1), 'failed');
  assert.equal(pool.snapshot()[1].error, 'PROCESS_PORT_CONFLICT');
  assert.doesNotMatch(JSON.stringify(pool.snapshot()), /sensitive registry detail/);
});

test('production-required registry fails closed when no registry is configured', () => {
  assert.throws(
    () => new SlotPool(poolOptions({
      registry: null,
      registryDirectory: null,
      registryRequired: true,
    })),
    /PROCESS_PORT_REGISTRY_REQUIRED/,
  );
});

test('systemd and Compose production launch surfaces require the persistent registry', () => {
  const repoRoot = path.resolve(__dirname, '../../..');
  for (const relative of [
    'deploy/aws/systemd/simworld-web.service',
    'deploy/aws/docker/docker-compose.yml',
  ]) {
    const source = fs.readFileSync(path.join(repoRoot, relative), 'utf8');
    assert.match(source, /PROCESS_PORT_REGISTRY_DIR=\/var\/lib\/simworld\/runtime-registry/);
    assert.match(source, /PROCESS_PORT_REGISTRY_REQUIRED=1/);
    assert.match(source, /PROCESS_PORT_REGISTRY_HEARTBEAT_MS=/);
  }
});

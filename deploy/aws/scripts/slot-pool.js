'use strict';
/**
 * slot-pool.js — UE child-process lifecycle for the AWS multi-tenant deploy.
 *
 * One instance of SlotPool is created at boot. It does NOT decide who gets a
 * slot (that's session-manager.js's job) — it only knows how to:
 *
 *   - start(slotId)   → spawn slot-launcher.sh, wait for MCP port, resolve
 *   - stop(slotId)    → SIGTERM the child, wait, SIGKILL if needed
 *   - status(slotId)  → 'down' | 'starting' | 'up' | 'failed'
 *
 * session-manager.js calls these from its _assign() and _evict() hooks.
 */

const { spawn } = require('child_process');
const crypto = require('crypto');
const net = require('net');
const path = require('path');
const { EventEmitter } = require('events');

const PROCESS_REGISTRY_MODULE = path.resolve(
  __dirname,
  '../../../simworld_studio_workspace/web/server/process-port-registry.js',
);

const DEFAULTS = {
  poolSize:        parseInt(process.env.UE_POOL_SIZE || '3', 10),
  gpuCount:        parseInt(process.env.UE_GPU_COUNT || '1', 10),
  baseMcp:         parseInt(process.env.UE_BASE_MCP_PORT || '55559', 10),
  baseCirrusHttp:  parseInt(process.env.UE_BASE_CIRRUS_HTTP || '8585', 10),
  baseCirrusWs:    parseInt(process.env.UE_BASE_CIRRUS_WS || '8586', 10),
  baseCirrusSfu:   parseInt(process.env.UE_BASE_CIRRUS_SFU || '8989', 10),
  baseUcv:         parseInt(process.env.UE_BASE_UCV || '9017', 10),
  portStride:      parseInt(process.env.UE_PORT_STRIDE || '2', 10),
  startupTimeout:  parseInt(process.env.UE_STARTUP_TIMEOUT_MS || '180000', 10),
  launcher:        process.env.UE_SLOT_LAUNCHER ||
                   path.resolve(__dirname, 'slot-launcher.sh'),
  slotsRoot:       process.env.SLOTS_ROOT || '/var/lib/simworld/slots',
  registryDirectory: process.env.PROCESS_PORT_REGISTRY_DIR || null,
  registryRequired: /^(?:1|true|yes|on)$/i.test(process.env.PROCESS_PORT_REGISTRY_REQUIRED || ''),
  registryHeartbeatMs: parseInt(process.env.PROCESS_PORT_REGISTRY_HEARTBEAT_MS || '10000', 10),
};

function portsForSlot(slotId, cfg = DEFAULTS) {
  return {
    mcp:        cfg.baseMcp        + slotId * cfg.portStride,
    cirrusHttp: cfg.baseCirrusHttp + slotId * cfg.portStride,
    cirrusWs:   cfg.baseCirrusWs   + slotId * cfg.portStride,
    cirrusSfu:  cfg.baseCirrusSfu  + slotId * cfg.portStride,
    ucv:        cfg.baseUcv        + slotId,
  };
}

function tcpProbe(port, host = '127.0.0.1', timeoutMs = 800) {
  return new Promise((resolve) => {
    const sock = new net.Socket();
    let done = false;
    const finish = (ok) => { if (done) return; done = true; sock.destroy(); resolve(ok); };
    sock.setTimeout(timeoutMs);
    sock.once('connect', () => finish(true));
    sock.once('error',   () => finish(false));
    sock.once('timeout', () => finish(false));
    sock.connect(port, host);
  });
}

async function waitForPort(port, deadlineMs) {
  while (Date.now() < deadlineMs) {
    if (await tcpProbe(port)) return true;
    await new Promise(r => setTimeout(r, 1000));
  }
  return false;
}

async function focusedTcpListenerProbe({ ports, signal }) {
  if (!Array.isArray(ports) || ports.some((endpoint) => endpoint.protocol !== 'tcp')) {
    throw new TypeError('slot listener probe accepts only explicit TCP endpoints');
  }
  const listeners = [];
  for (const endpoint of ports) {
    if (signal && signal.aborted) throw new Error('slot listener probe aborted');
    if (await tcpProbe(endpoint.port, endpoint.host, 250)) listeners.push(endpoint);
  }
  return listeners;
}

function stableStackId(value) {
  const source = String(value || 'unversioned');
  return `simworld-${crypto.createHash('sha256').update(source).digest('hex').slice(0, 24)}`;
}

function registryEndpoints(ports) {
  return Object.entries({
    mcp: ports.mcp,
    cirrus_http: ports.cirrusHttp,
    cirrus_streamer: ports.cirrusWs,
    cirrus_sfu: ports.cirrusSfu,
    ucv: ports.ucv,
  }).map(([name, port]) => ({ name, host: '127.0.0.1', protocol: 'tcp', port }));
}

class SlotPool extends EventEmitter {
  constructor(cfg = {}) {
    super();
    this.cfg = { ...DEFAULTS, ...cfg };
    this._spawn = cfg.spawnImpl || spawn;
    this._waitForPort = cfg.waitForPortImpl || waitForPort;
    this._processProbe = cfg.processProbe || null;
    this._registry = cfg.registry || null;
    this._registryLeases = new Map();
    this._registryHeartbeat = null;
    if (!this._registry && this.cfg.registryDirectory) {
      const { createProcessPortRegistry, defaultProcessProbe } = require(PROCESS_REGISTRY_MODULE);
      this._processProbe = this._processProbe || defaultProcessProbe;
      this._registry = createProcessPortRegistry({
        directory: path.resolve(this.cfg.registryDirectory),
        listenerProbe: focusedTcpListenerProbe,
        policy: 'reject',
      });
    }
    if (this._registry && !this._processProbe) {
      this._processProbe = require(PROCESS_REGISTRY_MODULE).defaultProcessProbe;
    }
    if (this.cfg.registryRequired && !this._registry) {
      throw new Error('PROCESS_PORT_REGISTRY_REQUIRED is set but PROCESS_PORT_REGISTRY_DIR is unavailable');
    }
    /** @type {Map<number, {status, child, ports, startedAt, error}>} */
    this._slots = new Map();
    for (let i = 0; i < this.cfg.poolSize; i++) {
      this._slots.set(i, { status: 'down', child: null, ports: portsForSlot(i, this.cfg) });
    }
    this._log = cfg.logger || ((tag, msg) => console.log(`[slot-pool] [${tag}] ${msg}`));
  }

  get size() { return this.cfg.poolSize; }
  portsForSlot(slotId) { return portsForSlot(slotId, this.cfg); }

  status(slotId) {
    const s = this._slots.get(slotId);
    return s ? s.status : 'unknown';
  }

  snapshot() {
    return [...this._slots.entries()].map(([id, s]) => ({
      slotId: id,
      status: s.status,
      pid: s.child ? s.child.pid : null,
      ports: s.ports,
      ageMs: s.startedAt ? Date.now() - s.startedAt : null,
      error: s.error || null,
      registryManaged: this._registryLeases.has(id),
    }));
  }

  async _acquireRegistryLease(slotId, ports) {
    if (!this._registry) return null;
    const existing = this._registryLeases.get(slotId);
    if (existing) return existing.publicLease;
    const identity = await this._processProbe(process.pid);
    if (!identity || identity.alive !== true || !identity.startToken) {
      const error = new Error('slot owner process identity is unavailable');
      error.code = 'PROCESS_PORT_OWNER_IDENTITY_UNAVAILABLE';
      throw error;
    }
    const acquired = await this._registry.acquire({
      stackId: stableStackId(this.cfg.stackId || process.env.SIMWORLD_BUILD_REVISION),
      slotId: `ue-slot-${slotId}`,
      // A physical GPU may intentionally host multiple bounded slots.  The
      // allocation id remains unique per slot while duplicate launches of the
      // same slot still conflict on slot id and every exact endpoint.
      gpuId: `gpu-${slotId % this.cfg.gpuCount}-slot-${slotId}`,
      portFamily: 'simworld-ue-slot-v1',
      ports: registryEndpoints(ports),
      ownerPid: process.pid,
      ownerStartToken: identity.startToken,
    });
    const lease = {
      publicLease: acquired.lease,
      owner: {
        leaseId: acquired.lease.leaseId,
        ownerPid: process.pid,
        ownerStartToken: identity.startToken,
      },
    };
    this._registryLeases.set(slotId, lease);
    this._ensureRegistryHeartbeat();
    return lease.publicLease;
  }

  _ensureRegistryHeartbeat() {
    if (this._registryHeartbeat || !this._registry || this._registryLeases.size === 0) return;
    const intervalMs = Number(this.cfg.registryHeartbeatMs);
    if (!Number.isFinite(intervalMs) || intervalMs < 1000) return;
    this._registryHeartbeat = setInterval(() => {
      this._heartbeatRegistry().catch((error) => {
        this._log('registry!', `heartbeat failed code=${error && error.code || 'PROCESS_PORT_REGISTRY_UNAVAILABLE'}`);
      });
    }, intervalMs);
    if (typeof this._registryHeartbeat.unref === 'function') this._registryHeartbeat.unref();
  }

  async _heartbeatRegistry() {
    if (!this._registry) return;
    const outcomes = await Promise.allSettled(
      [...this._registryLeases.values()].map((lease) => this._registry.heartbeat(lease.owner)),
    );
    const rejected = outcomes.find((outcome) => outcome.status === 'rejected');
    if (rejected) throw rejected.reason;
  }

  async _releaseRegistryLease(slotId) {
    const lease = this._registryLeases.get(slotId);
    if (!lease || !this._registry) return false;
    this._registryLeases.delete(slotId);
    try {
      await this._registry.release(lease.owner);
    } finally {
      if (this._registryLeases.size === 0 && this._registryHeartbeat) {
        clearInterval(this._registryHeartbeat);
        this._registryHeartbeat = null;
      }
    }
    return true;
  }

  /**
   * Start a slot. Resolves with port info once MCP is ready, or rejects on
   * timeout / spawn failure. Idempotent: if already up, returns immediately.
   * @param {number} slotId
   * @returns {Promise<{slotId, ports}>}
   */
  async start(slotId) {
    const s = this._slots.get(slotId);
    if (!s) throw new Error(`unknown slot ${slotId}`);
    if (s.status === 'up') {
      if (this._registry && !this._registryLeases.has(slotId)) {
        throw new Error(`slot ${slotId} is up without a process registry lease`);
      }
      return { slotId, ports: s.ports };
    }
    if (s.status === 'starting') {
      // Coalesce: wait for in-flight startup
      return new Promise((resolve, reject) => {
        const onReady = (id) => { if (id === slotId) { cleanup(); resolve({ slotId, ports: s.ports }); } };
        const onFailed = (id, err) => { if (id === slotId) { cleanup(); reject(err); } };
        const cleanup = () => {
          this.removeListener('ready', onReady);
          this.removeListener('failed', onFailed);
        };
        this.on('ready', onReady);
        this.on('failed', onFailed);
      });
    }

    if (s.child) throw new Error(`slot ${slotId} still has a managed child`);
    s.status = 'starting';
    s.error = null;
    s.startedAt = Date.now();
    try {
      await this._acquireRegistryLease(slotId, s.ports);
    } catch (error) {
      s.status = 'failed';
      s.error = String(error && error.code || 'PROCESS_PORT_REGISTRY_UNAVAILABLE');
      this.emit('failed', slotId, error);
      throw error;
    }
    this._log(`slot-${slotId}`, `spawning ${this.cfg.launcher}`);

    let child;
    try {
      child = this._spawn(this.cfg.launcher, ['--slot', String(slotId)], {
        env: {
          ...process.env,
          UE_BASE_MCP:         String(this.cfg.baseMcp),
          UE_BASE_CIRRUS_HTTP: String(this.cfg.baseCirrusHttp),
          UE_BASE_CIRRUS_WS:   String(this.cfg.baseCirrusWs),
          UE_BASE_CIRRUS_SFU:  String(this.cfg.baseCirrusSfu),
          UE_BASE_UCV:         String(this.cfg.baseUcv),
          UE_PORT_STRIDE:      String(this.cfg.portStride),
          UE_GPU_COUNT:        String(this.cfg.gpuCount),
          SLOTS_ROOT:          this.cfg.slotsRoot,
        },
        detached: false,
        stdio:    ['ignore', 'pipe', 'pipe'],
      });
    } catch (error) {
      s.status = 'failed';
      s.error = 'launcher spawn failed';
      await this._releaseRegistryLease(slotId);
      this.emit('failed', slotId, error);
      throw error;
    }
    s.child = child;

    child.stdout.on('data', (b) => this._log(`slot-${slotId}`, b.toString().trimEnd()));
    child.stderr.on('data', (b) => this._log(`slot-${slotId}!`, b.toString().trimEnd()));
    child.on('error', (error) => {
      s.error = String(error && error.code || 'launcher spawn failed');
      this._log(`slot-${slotId}!`, `launcher error code=${s.error}`);
    });

    child.on('exit', (code, sig) => {
      this._log(`slot-${slotId}`, `child exited code=${code} sig=${sig}`);
      s.child = null;
      const wasStarting = s.status === 'starting';
      s.status = 'down';
      this._releaseRegistryLease(slotId).catch((error) => {
        this._log(`slot-${slotId}!`, `registry release failed code=${error && error.code || 'PROCESS_PORT_REGISTRY_UNAVAILABLE'}`);
      }).finally(() => {
        if (wasStarting) {
          s.error = `exit ${code}`;
          this.emit('failed', slotId, new Error(`launcher exited code=${code} sig=${sig}`));
        } else {
          this.emit('down', slotId);
        }
      });
    });

    // Wait for MCP port to become ready, bounded by startup timeout
    const deadline = Date.now() + this.cfg.startupTimeout;
    const ready = await this._waitForPort(s.ports.mcp, deadline);
    if (!ready || s.child !== child || s.status !== 'starting') {
      s.status = 'failed';
      s.error = `MCP port ${s.ports.mcp} not ready within ${this.cfg.startupTimeout}ms`;
      try { child.kill('SIGTERM'); } catch (_) {}
      this.emit('failed', slotId, new Error(s.error));
      throw new Error(s.error);
    }
    s.status = 'up';
    this._log(`slot-${slotId}`, `ready MCP=${s.ports.mcp} CirrusHTTP=${s.ports.cirrusHttp}`);
    this.emit('ready', slotId);
    return { slotId, ports: s.ports };
  }

  /**
   * Stop a slot. Idempotent: returns immediately if not running.
   * @param {number} slotId
   * @returns {Promise<void>}
   */
  async stop(slotId) {
    const s = this._slots.get(slotId);
    if (!s) return;
    if (!s.child) {
      s.status = 'down';
      await this._releaseRegistryLease(slotId);
      return;
    }

    const child = s.child;
    s.status = 'stopping';
    this._log(`slot-${slotId}`, `SIGTERM pid=${child.pid}`);

    await new Promise((resolve) => {
      let forceTimer = null;
      const exitHandler = () => {
        if (forceTimer) clearTimeout(forceTimer);
        resolve();
      };
      child.once('exit', exitHandler);
      try { child.kill('SIGTERM'); } catch (_) { resolve(); return; }
      forceTimer = setTimeout(() => {
        if (s.child) {
          try { child.kill('SIGKILL'); } catch (_) {}
        }
        resolve();
      }, 15_000);
    });

    s.status = 'down';
    s.child = null;
    await this._releaseRegistryLease(slotId);
  }

  async stopAll() {
    const ids = [...this._slots.keys()];
    await Promise.all(ids.map((id) => this.stop(id)));
  }
}

let _singleton = null;

/** Return process-wide singleton. */
function getSlotPool(cfg) {
  if (!_singleton) _singleton = new SlotPool(cfg);
  return _singleton;
}

module.exports = { SlotPool, getSlotPool, portsForSlot };

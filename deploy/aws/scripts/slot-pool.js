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
const net = require('net');
const path = require('path');
const fs = require('fs');
const { EventEmitter } = require('events');

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

class SlotPool extends EventEmitter {
  constructor(cfg = {}) {
    super();
    this.cfg = { ...DEFAULTS, ...cfg };
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
    }));
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
    if (s.status === 'up') return { slotId, ports: s.ports };
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

    s.status = 'starting';
    s.error = null;
    s.startedAt = Date.now();
    this._log(`slot-${slotId}`, `spawning ${this.cfg.launcher}`);

    const child = spawn(this.cfg.launcher, ['--slot', String(slotId)], {
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
    s.child = child;

    child.stdout.on('data', (b) => this._log(`slot-${slotId}`, b.toString().trimEnd()));
    child.stderr.on('data', (b) => this._log(`slot-${slotId}!`, b.toString().trimEnd()));

    child.on('exit', (code, sig) => {
      this._log(`slot-${slotId}`, `child exited code=${code} sig=${sig}`);
      s.child = null;
      const wasStarting = s.status === 'starting';
      s.status = 'down';
      if (wasStarting) {
        s.error = `exit ${code}`;
        this.emit('failed', slotId, new Error(`launcher exited code=${code} sig=${sig}`));
      } else {
        this.emit('down', slotId);
      }
    });

    // Wait for MCP port to become ready, bounded by startup timeout
    const deadline = Date.now() + this.cfg.startupTimeout;
    const ready = await waitForPort(s.ports.mcp, deadline);
    if (!ready) {
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
    if (!s.child) { s.status = 'down'; return; }

    const child = s.child;
    s.status = 'stopping';
    this._log(`slot-${slotId}`, `SIGTERM pid=${child.pid}`);

    await new Promise((resolve) => {
      const exitHandler = () => resolve();
      child.once('exit', exitHandler);
      try { child.kill('SIGTERM'); } catch (_) { resolve(); return; }
      setTimeout(() => {
        if (s.child) {
          try { child.kill('SIGKILL'); } catch (_) {}
        }
        resolve();
      }, 15_000);
    });

    s.status = 'down';
    s.child = null;
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

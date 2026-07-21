'use strict';
/**
 * session-manager.js  (CommonJS)
 * UE slot pool: max N concurrent users, 30-min sliding TTL, wait queue.
 */

const crypto       = require('crypto');
const { EventEmitter } = require('events');

const UE_POOL_SIZE     = parseInt(process.env.UE_POOL_SIZE      || '10',  10);
const UE_MAX_QUEUE     = parseInt(process.env.UE_MAX_QUEUE      || '40',  10);
const SESSION_TTL_MS   = parseInt(process.env.SESSION_TTL_MS    || String(30 * 60 * 1000), 10);
const SESSION_HARD_MAX = parseInt(process.env.SESSION_HARD_MAX_MS || String(60 * 60 * 1000), 10);
const SWEEP_INTERVAL   = 60_000;

// UE port layout: each slot gets its own port range (stride = 2)
const UE_BASE_MCP      = parseInt(process.env.UE_BASE_MCP_PORT    || '55559', 10);
const UE_BASE_CIRRUS_H = parseInt(process.env.UE_BASE_CIRRUS_HTTP || '8585',  10);
const UE_BASE_CIRRUS_W = parseInt(process.env.UE_BASE_CIRRUS_WS   || '8586',  10);
const UE_BASE_UCV      = parseInt(process.env.UE_BASE_UCV         || '9017',  10);
const UE_PORT_STRIDE   = parseInt(process.env.UE_PORT_STRIDE       || '2',     10);
const LEASE_ID_PATTERN = /^[A-Za-z0-9_-]{16,128}$/;
const OWNER_ID_PATTERN = /^[A-Za-z0-9:_-]{1,128}$/;

function positiveDuration(value, fallback, field) {
  const resolved = value === undefined ? fallback : Number(value);
  if (!Number.isSafeInteger(resolved) || resolved < 1) {
    throw new TypeError(`${field} must be a positive safe integer`);
  }
  return resolved;
}

function activeLeaseInput(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const expected = ['leaseId', 'mcpPort', 'ownerId', 'slotId'];
  const keys = Object.keys(value).sort();
  if (keys.length !== expected.length || keys.some((key, index) => key !== expected[index])) return null;
  if (typeof value.ownerId !== 'string' || !OWNER_ID_PATTERN.test(value.ownerId) ||
      typeof value.leaseId !== 'string' || !LEASE_ID_PATTERN.test(value.leaseId) ||
      !Number.isSafeInteger(value.slotId) || value.slotId < 0 || value.slotId > 65535 ||
      !Number.isSafeInteger(value.mcpPort) || value.mcpPort < 1 || value.mcpPort > 65535) {
    return null;
  }
  return value;
}

function uePortsForSlot(slotId) {
  return {
    mcpPort:    UE_BASE_MCP      + slotId * UE_PORT_STRIDE,
    cirrusHttp: UE_BASE_CIRRUS_H + slotId * UE_PORT_STRIDE,
    cirrusWs:   UE_BASE_CIRRUS_W + slotId * UE_PORT_STRIDE,
    ucvPort:    UE_BASE_UCV      + slotId,
  };
}

class SessionManager extends EventEmitter {
  /**
   * @param {object} [opts]
   * @param {object} [opts.slotPool] Optional SlotPool that actually starts/stops
   *   UE per slot. If omitted, sessions still get assigned a slot id + ports
   *   (legacy single-UE / dev mode), but no UE process is spawned.
   * @param {Function} [opts.clock] Injectable millisecond clock for validation.
   * @param {number} [opts.sessionTtlMs] Sliding idle lease duration.
   * @param {number} [opts.sessionHardMaxMs] Absolute lease duration.
   */
  constructor(opts = {}) {
    super();
    this._clock = opts.clock === undefined ? Date.now : opts.clock;
    if (typeof this._clock !== 'function') throw new TypeError('clock must be a function');
    this._sessionTtlMs = positiveDuration(opts.sessionTtlMs, SESSION_TTL_MS, 'sessionTtlMs');
    this._sessionHardMaxMs = positiveDuration(
      opts.sessionHardMaxMs,
      SESSION_HARD_MAX,
      'sessionHardMaxMs',
    );
    if (this._sessionHardMaxMs < this._sessionTtlMs) {
      throw new TypeError('sessionHardMaxMs must be greater than or equal to sessionTtlMs');
    }
    /** @type {Map<string, object>} token → record */
    this._sessions  = new Map();
    /** @type {Set<number>} available slot IDs */
    this._freeSlots = new Set(Array.from({ length: UE_POOL_SIZE }, (_, i) => i));
    /** @type {Array<{userId,resolve,reject,enqueuedAt}>} */
    this._queue     = [];
    this._slotPool  = opts.slotPool || null;
    this._sweeper   = setInterval(() => this._sweep(), SWEEP_INTERVAL);
    if (this._sweeper.unref) this._sweeper.unref();
  }

  // ── Public ─────────────────────────────────────────────────────────────────

  get totalSlots()     { return UE_POOL_SIZE; }
  get freeSlots()      { return this._freeSlots.size; }
  get activeSessions() { return this._sessions.size; }
  get queueLength()    { return this._queue.length; }

  /**
   * Attach (or replace) the SlotPool that owns UE lifecycle. Designed to be
   * called once at boot from a deploy-time shim (see deploy/aws/scripts/
   * session-shim.js). Resizes the free-slot set if the pool size differs.
   */
  setSlotPool(pool) {
    this._slotPool = pool;
    if (pool && pool.size && pool.size !== this._freeSlots.size + this._sessions.size) {
      // Pool size overrides UE_POOL_SIZE env var
      this._freeSlots = new Set();
      for (let i = 0; i < pool.size; i++) {
        const inUse = [...this._sessions.values()].some(r => r.slotId === i);
        if (!inUse) this._freeSlots.add(i);
      }
    }
  }

  /**
   * Acquire a session slot (returns existing if userId already has one).
   * @param {string} userId
   * @returns {Promise<object>} session record
   */
  async acquire(userId) {
    const now = this._now();
    this._expireSessions(now);
    // Reuse existing session for same user
    for (const rec of this._sessions.values()) {
      if (rec.userId === userId) {
        rec.lastActivity = now;
        return rec;
      }
    }

    if (this._freeSlots.size > 0) {
      return this._assignAndStart(userId);
    }

    if (this._queue.length >= UE_MAX_QUEUE) {
      const err = new Error('Server at capacity. Please try again later.');
      err.code = 'POOL_FULL';
      err.queueLength = this._queue.length;
      throw err;
    }

    return new Promise((resolve, reject) => {
      this._queue.push({ userId, resolve, reject, enqueuedAt: now });
      this.emit('queued', { userId, position: this._queue.length });
    });
  }

  /**
   * Refresh TTL and return record, or null if token unknown.
   * @param {string} token
   * @returns {object|null}
   */
  touch(token) {
    const rec = this._sessions.get(token);
    if (!rec) return null;
    const now = this._now();
    const reason = this._expirationReason(rec, now);
    if (reason) {
      this._evict(token, reason);
      return null;
    }
    rec.lastActivity = now;
    return rec;
  }

  /**
   * Resolve an exact active UE lease without accepting or returning its raw
   * browser session token. The check does not renew the lease.
   */
  resolveActiveLease(identity) {
    const requested = activeLeaseInput(identity);
    if (!requested || requested.slotId >= this.totalSlots) return null;
    const now = this._now();
    this._expireSessions(now);
    for (const rec of this._sessions.values()) {
      const mcpPort = rec.uePorts && rec.uePorts.mcpPort;
      if (rec.mcpReady === true && rec.userId === requested.ownerId &&
          rec.slotId === requested.slotId && rec.leaseId === requested.leaseId &&
          mcpPort === requested.mcpPort) {
        return Object.freeze({
          ownerId: rec.userId,
          slotId: rec.slotId,
          leaseId: rec.leaseId,
          mcpPort,
        });
      }
    }
    return null;
  }

  /** Resolve internal per-slot ports for a previously validated lease. */
  resolveActiveLeaseRuntime(identity) {
    const active = this.resolveActiveLease(identity);
    if (!active) return null;
    for (const rec of this._sessions.values()) {
      if (rec.userId !== active.ownerId || rec.slotId !== active.slotId ||
          rec.leaseId !== active.leaseId || rec.uePorts.mcpPort !== active.mcpPort) continue;
      const ucvPort = Number(rec.uePorts && rec.uePorts.ucvPort);
      if (!Number.isSafeInteger(ucvPort) || ucvPort < 1 || ucvPort > 65535) return null;
      return Object.freeze({ ...active, ucvPort });
    }
    return null;
  }

  /** Release a slot explicitly (user logout / tab close). */
  release(token) {
    if (this._sessions.has(token)) this._evict(token, 'released');
  }

  /** Admin snapshot for health endpoint. */
  snapshot() {
    const now = this._now();
    return [...this._sessions.values()].map(r => ({
      token:    r.token.slice(0, 8) + '…',
      slotId:   r.slotId,
      userId:   r.userId,
      ageMs:    now - r.acquiredAt,
      idleMs:   now - r.lastActivity,
      uePorts:  r.uePorts,
    }));
  }

  destroy() {
    clearInterval(this._sweeper);
    // Reject all pending queue waiters so callers don't hang
    for (const w of this._queue) {
      try { w.reject(new Error('SessionManager destroyed')); } catch {}
    }
    this._queue = [];
  }

  // ── Internal ───────────────────────────────────────────────────────────────

  _assign(userId) {
    const slotId = [...this._freeSlots][0];
    this._freeSlots.delete(slotId);
    const now = this._now();
    const rec = {
      token:        crypto.randomBytes(32).toString('hex'),
      leaseId:      crypto.randomBytes(24).toString('base64url'),
      slotId,
      userId,
      acquiredAt:   now,
      lastActivity: now,
      uePorts:      uePortsForSlot(slotId),
      mcpReady:     false,
    };
    this._sessions.set(rec.token, rec);
    this.emit('acquired', { token: rec.token, slotId, userId });
    return rec;
  }

  /**
   * Assign a slot AND, if a SlotPool is wired up, start the UE process.
   * Returns once MCP is reachable (or, in no-pool mode, immediately).
   */
  async _assignAndStart(userId) {
    const rec = this._assign(userId);
    if (!this._slotPool) {
      rec.mcpReady = true;
      return rec;
    }
    try {
      const info = await this._slotPool.start(rec.slotId);
      // Pool is authoritative for actual ports
      rec.uePorts = {
        mcpPort:    info.ports.mcp,
        cirrusHttp: info.ports.cirrusHttp,
        cirrusWs:   info.ports.cirrusWs,
        cirrusSfu:  info.ports.cirrusSfu,
        ucvPort:    info.ports.ucv,
      };
      rec.mcpReady = true;
      this.emit('ready', { token: rec.token, slotId: rec.slotId });
      return rec;
    } catch (err) {
      // Roll back the slot assignment so the user can retry / queue
      this._sessions.delete(rec.token);
      this._freeSlots.add(rec.slotId);
      const e = new Error(`Failed to start UE slot ${rec.slotId}: ${err.message}`);
      e.code = 'SLOT_START_FAILED';
      throw e;
    }
  }

  _evict(token, reason) {
    const rec = this._sessions.get(token);
    if (!rec) return;
    this._sessions.delete(token);
    this._freeSlots.add(rec.slotId);
    this.emit('released', { token, slotId: rec.slotId, reason });
    // Stop the UE process for this slot (best-effort, fire-and-forget)
    if (this._slotPool) {
      this._slotPool.stop(rec.slotId).catch((e) => this.emit('error', e));
    }
    // Drain wait queue
    while (this._queue.length > 0 && this._freeSlots.size > 0) {
      const waiter = this._queue.shift();
      // _assignAndStart is async; resolve/reject the original promise
      this._assignAndStart(waiter.userId).then(waiter.resolve, waiter.reject);
    }
  }

  _now() {
    const now = this._clock();
    if (!Number.isSafeInteger(now) || now < 0) throw new Error('Session clock is invalid');
    return now;
  }

  _expirationReason(rec, now) {
    if (!rec || !Number.isSafeInteger(rec.acquiredAt) || !Number.isSafeInteger(rec.lastActivity) ||
        rec.acquiredAt < 0 || rec.lastActivity < rec.acquiredAt ||
        rec.acquiredAt > now || rec.lastActivity > now) {
      return 'invalid_session_state';
    }
    if (now - rec.lastActivity > this._sessionTtlMs) return 'idle_timeout';
    if (now - rec.acquiredAt > this._sessionHardMaxMs) return 'hard_limit';
    return null;
  }

  _expireSessions(now) {
    for (const [token, rec] of this._sessions) {
      const reason = this._expirationReason(rec, now);
      if (reason) this._evict(token, reason);
    }
  }

  _sweep() {
    const now = this._now();
    this._expireSessions(now);
    // Evict stuck waiters (> 5 min in queue)
    const WAIT_MAX = 5 * 60 * 1000;
    this._queue = this._queue.filter(w => {
      if (now - w.enqueuedAt > WAIT_MAX) {
        w.reject(new Error('Queue wait timed out'));
        return false;
      }
      return true;
    });
  }
}

const sessionManager = new SessionManager();
module.exports = { SessionManager, sessionManager };

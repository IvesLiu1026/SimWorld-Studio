'use strict';

// ---------------------------------------------------------------------------
// UnrealBridge — single-process broker for UnrealCV (port 9000).
//
// WHY THIS EXISTS:
//   Before this file, every code path that needed to talk to UnrealCV opened
//   its own one-shot TCP socket: agent-controller's getObservation, agent stop,
//   the per-Claude mcp-server subprocesses, the health check. With N panel
//   agents running, that's 5+ independent UCV clients hammering port 9000 with
//   no global coordination. Spawning a new agent in UE resets the connection,
//   silently killing every other in-flight command. Observation calls had no
//   retry, so agents would run blind without anyone noticing.
//
// WHAT THIS DOES:
//   Owns ONE persistent TCP connection to UCV. Serializes commands through a
//   FIFO queue (single-flight). Auto-reconnects with exponential backoff.
//   Requeues in-flight commands on disconnect. Per-command timeout + retries.
//   Drops jobs that have been queued longer than the queue deadline.
//
// SCOPE:
//   Phase 1: agent-controller.js calls broker directly (same process).
//   Phase 2: mcp-server.js subprocesses call HTTP RPC into the main server,
//            which forwards to this broker. (not yet implemented)
// ---------------------------------------------------------------------------

const net = require('net');
const log = require('./logger');

const UCV_PORT = parseInt(process.env.UCV_PORT || '9000', 10);
const UCV_HOST = process.env.UCV_HOST || '127.0.0.1';
const UCV_MAGIC = 0x9E2B83C1;

const RECONNECT_DELAY_MIN = 500;
const RECONNECT_DELAY_MAX = 5000;

const DEFAULT_TIMEOUT_MS = 10000;
const DEFAULT_RETRIES = 3;
const DEFAULT_QUEUE_DEADLINE_MS = 30000;

class UcvBroker {
  constructor(opts = {}) {
    const host = opts.host === undefined ? UCV_HOST : String(opts.host).trim();
    const port = opts.port === undefined ? UCV_PORT : Number(opts.port);
    if (!host || /[\x00-\x20\x7f/\\]/.test(host)) {
      throw new TypeError('UCV broker host is invalid');
    }
    if (!Number.isSafeInteger(port) || port < 1 || port > 65535) {
      throw new TypeError('UCV broker port is invalid');
    }
    this.host = host;
    this.port = port;
    this.sock = null;
    this.connecting = false;
    this.connected = false;
    this.gotBanner = false;
    this.buf = Buffer.alloc(0);

    this.queue = [];          // [{cmd, timeoutMs, retries, attempts, enqueuedAt, queueDeadlineMs, resolve, reject, timer}]
    this.inFlight = null;     // current job awaiting UCV response

    this.msgIdCounter = 200;
    this.reconnectDelay = RECONNECT_DELAY_MIN;
    this.reconnectTimer = null;

    // Metrics for /api/events health surface
    this.totalSent = 0;
    this.totalErrors = 0;
    this.totalRequeues = 0;
    this.lastError = null;
    this.lastConnectedAt = null;

    // Robustness kill-switch: each UCV (port 9000) connection leaks a thread in
    // UE's UnrealCV plugin, and a connection storm can wedge UE's game thread.
    // Set DISABLE_UCV_BROKER=1 to make the broker a no-op (rejects UCV commands
    // without opening a socket). Scene-gen uses MCP take_screenshot (not UCV),
    // so disabling only affects UCV-based agent-observation features.
    this.disabled = (process.env.DISABLE_UCV_BROKER === "1" || process.env.DISABLE_UCV_BROKER === "true");
    if (this.disabled) { try { console.log("[ucv-broker] DISABLED via DISABLE_UCV_BROKER env — no UCV connections will be opened"); } catch (_e) {} }
  }

  /**
   * Public API — drop-in replacement for the old ucvCommand().
   * Returns a Promise<string> with the UCV payload (id prefix stripped).
   *
   * @param {string} cmd  e.g. "vget /object/Pedestrian_1/location"
   * @param {object} opts
   * @param {number} opts.timeoutMs       per-attempt wire timeout (default 10s)
   * @param {number} opts.retries         max attempts before rejecting (default 3)
   * @param {number} opts.queueDeadlineMs reject if not started within this many ms (default 30s)
   */
  send(cmd, opts = {}) {
    if (this.disabled) return Promise.reject(new Error("UCV broker disabled (DISABLE_UCV_BROKER=1)"));
    const {
      timeoutMs = DEFAULT_TIMEOUT_MS,
      retries = DEFAULT_RETRIES,
      queueDeadlineMs = DEFAULT_QUEUE_DEADLINE_MS,
    } = opts;

    return new Promise((resolve, reject) => {
      const job = {
        cmd,
        timeoutMs,
        retries,
        queueDeadlineMs,
        attempts: 0,
        enqueuedAt: Date.now(),
        resolve,
        reject,
        timer: null,
      };
      this.queue.push(job);
      this._pump();
    });
  }

  status() {
    return {
      connected: this.connected,
      gotBanner: this.gotBanner,
      queueDepth: this.queue.length,
      inFlight: this.inFlight ? this.inFlight.cmd.slice(0, 80) : null,
      totalSent: this.totalSent,
      totalErrors: this.totalErrors,
      totalRequeues: this.totalRequeues,
      lastError: this.lastError,
      lastConnectedAt: this.lastConnectedAt,
    };
  }

  // ── Connection management ────────────────────────────────────────────────

  _ensureConnected() {
    if (this.sock || this.connecting) return;
    this.connecting = true;
    this.gotBanner = false;
    this.buf = Buffer.alloc(0);

    log.agent('debug', `[ucv-broker] connecting to ${this.host}:${this.port}`);
    const sock = new net.Socket();
    this.sock = sock;

    sock.on('connect', () => {
      log.agent('info', `[ucv-broker] socket connected, awaiting banner`);
      this.connecting = false;
      this.connected = true;
      this.lastConnectedAt = Date.now();
      this.reconnectDelay = RECONNECT_DELAY_MIN; // reset backoff
    });

    sock.on('data', (chunk) => this._onData(chunk));
    sock.on('close', () => this._onDisconnect('close'));
    sock.on('error', (err) => {
      this.lastError = err.message;
      this._onDisconnect(`error: ${err.message}`);
    });

    sock.connect(this.port, this.host);
  }

  _onDisconnect(reason) {
    // Idempotent — error and close both fire on bad sockets, only handle once
    if (!this.sock && !this.connecting && !this.connected) return;

    log.agent('warn', `[ucv-broker] disconnected: ${reason}`);
    try { this.sock?.destroy(); } catch {}
    this.sock = null;
    this.connected = false;
    this.connecting = false;
    this.gotBanner = false;
    this.buf = Buffer.alloc(0);

    // If a command was in-flight, requeue it (it'll consume one retry budget)
    if (this.inFlight) {
      const job = this.inFlight;
      this.inFlight = null;
      if (job.timer) { clearTimeout(job.timer); job.timer = null; }

      if (job.attempts < job.retries) {
        this.totalRequeues++;
        log.agent('info', `[ucv-broker] requeue "${job.cmd.slice(0, 60)}" (attempts ${job.attempts}/${job.retries})`);
        this.queue.unshift(job);
      } else {
        this.totalErrors++;
        job.reject(new Error(`UCV disconnected after ${job.attempts} attempts: ${reason}`));
      }
    }

    // Sweep stale queued jobs — important when UCV is unreachable: without this,
    // jobs sit in the queue forever during reconnect cycling because _pump is
    // only called on successful connect. We sweep here AND in the reconnect
    // timer callback so deadlines are honored even in pure-failure loops.
    this._dropStaleJobs();

    // Schedule reconnect only if there are still live jobs waiting.
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    if (this.queue.length > 0) {
      this.reconnectTimer = setTimeout(() => {
        this.reconnectTimer = null;
        this._dropStaleJobs();
        if (this.queue.length > 0) this._ensureConnected();
      }, this.reconnectDelay);
      this.reconnectDelay = Math.min(this.reconnectDelay * 2, RECONNECT_DELAY_MAX);
    }
  }

  _dropStaleJobs() {
    const now = Date.now();
    const live = [];
    for (const job of this.queue) {
      if (now - job.enqueuedAt > job.queueDeadlineMs) {
        this.totalErrors++;
        job.reject(new Error(`UCV queue deadline ${job.queueDeadlineMs}ms exceeded: ${job.cmd.slice(0, 60)}`));
      } else {
        live.push(job);
      }
    }
    this.queue = live;
  }

  // ── Protocol parsing ─────────────────────────────────────────────────────

  _onData(chunk) {
    this.buf = Buffer.concat([this.buf, chunk]);
    while (true) {
      const frame = this._parseFrame();
      if (frame === null) break;
      if (!this.gotBanner) {
        this.gotBanner = true;
        log.agent('debug', `[ucv-broker] banner received, ready`);
        this._pump();
        continue;
      }
      this._onResponse(frame);
    }
  }

  _parseFrame() {
    if (this.buf.length < 8) return null;
    if (this.buf.readUInt32LE(0) !== UCV_MAGIC) {
      // Out of sync — drop connection and reconnect
      log.agent('warn', `[ucv-broker] frame magic mismatch, force reconnect`);
      this._onDisconnect('magic mismatch');
      return null;
    }
    const sz = this.buf.readUInt32LE(4);
    if (this.buf.length < 8 + sz) return null;
    const payload = this.buf.slice(8, 8 + sz).toString('utf-8');
    this.buf = this.buf.slice(8 + sz);
    return payload;
  }

  _onResponse(payload) {
    const job = this.inFlight;
    if (!job) {
      log.agent('warn', `[ucv-broker] unsolicited frame: ${payload.slice(0, 80)}`);
      return;
    }
    // Strip "<id>:" prefix from response (matches old ucvCommand behavior)
    let result = payload;
    const ci = result.indexOf(':');
    if (ci > 0 && ci < 6) result = result.slice(ci + 1);

    if (job.timer) { clearTimeout(job.timer); job.timer = null; }
    this.inFlight = null;
    this.totalSent++;
    job.resolve(result);
    this._pump();
  }

  // ── Queue pump ───────────────────────────────────────────────────────────

  _pump() {
    this._dropStaleJobs();

    if (this.inFlight) return;
    if (this.queue.length === 0) return;

    if (!this.sock || !this.gotBanner) {
      this._ensureConnected();
      return;
    }

    const job = this.queue.shift();
    this.inFlight = job;
    job.attempts++;

    job.timer = setTimeout(() => {
      log.agent('warn', `[ucv-broker] timeout "${job.cmd.slice(0, 60)}" attempt ${job.attempts}/${job.retries}`);
      // Force a disconnect — _onDisconnect will requeue or reject based on retries
      this._onDisconnect('command timeout');
    }, job.timeoutMs);

    const id = this.msgIdCounter++;
    const msg = `${id}:${job.cmd}`;
    const payload = Buffer.from(msg, 'utf-8');
    const header = Buffer.alloc(8);
    header.writeUInt32LE(UCV_MAGIC, 0);
    header.writeUInt32LE(payload.length, 4);

    try {
      this.sock.write(Buffer.concat([header, payload]));
    } catch (err) {
      log.agent('warn', `[ucv-broker] write failed: ${err.message}`);
      // Treat as disconnect — _onDisconnect will requeue with retry budget
      this._onDisconnect(`write error: ${err.message}`);
    }
  }
}

// Singleton — one broker per process
let _instance = null;
function getBroker() {
  if (!_instance) _instance = new UcvBroker();
  return _instance;
}

// ---------------------------------------------------------------------------
// UeMcpBroker — single-process GLOBAL funnel for UE's MCP command port (55559).
//
// WHY: every per-session mcp-server.js subprocess used to own its OWN cmdQueue
// and open one-shot TCP straight to 55559. With N Claude/agent sessions that's
// N independent "serial" queues all firing at UE with zero cross-process
// coordination => effectively N-concurrent bursts that overload/segfault UE.
// All sessions now funnel through this ONE broker via /api/internal/ue, so the
// global concurrency is truly 1 and we can shape traffic (削峰填谷).
//
// Unlike UcvBroker, UE's MCP is ONE-SHOT TCP per command (connect→write→read→
// close), so there is no persistent socket — just global serialization +
// queueing policy on top of _execOnce().
//
// Policy: concurrency=1, bounded queue + backpressure (429/Retry-After),
// token-bucket rate limit, adaptive cooldown, a dedicated execute_python_script
// SLOW LANE (own bounded queue + min start interval), queue-deadline drop, and
// a `paused` flag used by the opt-in crash self-heal while UE restarts.
// ---------------------------------------------------------------------------
const UE_MCP_PORT = parseInt(process.env.UNREAL_PORT || '55559', 10);
const UE_MCP_HOST = process.env.UNREAL_HOST || '127.0.0.1';
const UE_MAX_QUEUE = parseInt(process.env.UE_GATE_MAX_QUEUE || '64', 10);
const UE_MAX_PYQUEUE = parseInt(process.env.UE_GATE_MAX_PYQUEUE || '10', 10);
const UE_PY_MIN_INTERVAL_MS = parseInt(process.env.UE_GATE_PY_INTERVAL_MS || '500', 10);
const UE_BUCKET_CAP = parseInt(process.env.UE_GATE_BUCKET_CAP || '20', 10);
const UE_BUCKET_RATE = parseFloat(process.env.UE_GATE_BUCKET_RATE || '10'); // tokens/sec
const UE_DEFAULT_TIMEOUT_MS = 30000;
const UE_DEFAULT_RETRIES = 2; // was 3 in mcp-server.js — trimmed to dampen retry amplification
const UE_MAX_SCOPED_RESPONSE_BYTES = 16 * 1024 * 1024;
const UE_NON_RETRYABLE_ERROR_CODES = new Set([
  'UE_COMMAND_ABORTED',
  'UE_COMMAND_AUTHORIZATION_EXPIRED',
  'UE_RESPONSE_TOO_LARGE',
]);

const UE_COOLDOWN = {
  spawn_blueprint_actor: 200,
  execute_python_script: 200,
  spawn_actor: 200,
  delete_all_spawned: 300,
  setup_environment: 300,
  delete_actor: 100,
  _default: 50,
};

function _retryAfter(ms, msg) {
  return Object.assign(new Error(msg || 'UE busy'), { retryAfterMs: ms });
}

class UeMcpBroker {
  constructor(opts = {}) {
    const host = opts.host === undefined ? UE_MCP_HOST : String(opts.host).trim();
    const port = opts.port === undefined ? UE_MCP_PORT : Number(opts.port);
    if (!host || /[\x00-\x20\x7f/\\]/.test(host)) {
      throw new TypeError('UE broker host is invalid');
    }
    if (!Number.isSafeInteger(port) || port < 1 || port > 65535) {
      throw new TypeError('UE broker port is invalid');
    }
    this.host = host;
    this.port = port;
    this.queue = [];
    this.pyQueue = [];
    this.inFlight = null;
    this._lastCmdEnd = 0;
    this._lastCooldown = 0;
    this._lastPyStart = 0;
    this.tokens = UE_BUCKET_CAP;
    this._lastRefill = Date.now();
    this.paused = false;
    this._pumpScheduled = false;
    // injectable for tests: a fake one-shot executor
    this._exec = opts.exec || ((type, params, timeoutMs, signal, maxResponseBytes) => (
      this._execOnce(type, params, timeoutMs, signal, maxResponseBytes)
    ));
    this.totalSent = 0;
    this.totalErrors = 0;
    this.total429 = 0;
    this.lastError = null;
  }

  /**
   * Funnel entry point. Resolves with the UE JSON response, or rejects.
   * On backpressure it rejects with err.retryAfterMs set (caller maps to 429).
   */
  send(type, params, opts = {}) {
    const timeoutMs = typeof opts.timeoutMs === 'number' ? opts.timeoutMs : UE_DEFAULT_TIMEOUT_MS;
    const queueDeadlineMs = typeof opts.queueDeadlineMs === 'number'
      ? opts.queueDeadlineMs : Math.max(timeoutMs * 2, 15000);
    const maxAttempts = opts.maxAttempts === undefined ? UE_DEFAULT_RETRIES : Number(opts.maxAttempts);
    const signal = opts.signal;
    const preSendAuthorize = opts.preSendAuthorize;
    const maxResponseBytes = opts.maxResponseBytes === undefined ? null : Number(opts.maxResponseBytes);
    return new Promise((resolve, reject) => {
      if (!Number.isSafeInteger(maxAttempts) || maxAttempts < 1 || maxAttempts > 5) {
        return reject(new TypeError('UE broker maxAttempts must be an integer from 1 to 5'));
      }
      if (preSendAuthorize !== undefined && typeof preSendAuthorize !== 'function') {
        return reject(new TypeError('UE broker preSendAuthorize must be a function'));
      }
      if (maxResponseBytes !== null && (
        !Number.isSafeInteger(maxResponseBytes)
        || maxResponseBytes < 2
        || maxResponseBytes > UE_MAX_SCOPED_RESPONSE_BYTES
      )) {
        return reject(new TypeError('UE broker maxResponseBytes must be an integer from 2 to 16777216'));
      }
      if (signal && signal.aborted) {
        const error = new Error(`UE command '${type}' was aborted`);
        error.name = 'AbortError'; error.code = 'UE_COMMAND_ABORTED';
        return reject(error);
      }
      if (this.paused) { this.total429++; return reject(_retryAfter(2000, 'UE restarting, retry shortly')); }
      const isPy = type === 'execute_python_script';
      const q = isPy ? this.pyQueue : this.queue;
      const cap = isPy ? UE_MAX_PYQUEUE : UE_MAX_QUEUE;
      if (q.length >= cap) {
        this.total429++;
        const drain = Math.max(1000, q.length * (this._lastCooldown || 200));
        return reject(_retryAfter(drain, `UE busy (${isPy ? 'python' : 'cmd'} queue full ${q.length}/${cap})`));
      }
      let settled = false;
      let job;
      const cleanup = () => { if (signal) signal.removeEventListener('abort', onAbort); };
      const safeResolve = (value) => { if (settled) return; settled = true; cleanup(); resolve(value); };
      const safeReject = (error) => { if (settled) return; settled = true; cleanup(); reject(error); };
      const onAbort = () => {
        if (settled) return;
        job.cancelled = true;
        const queued = isPy ? this.pyQueue : this.queue;
        const index = queued.indexOf(job);
        if (index >= 0) queued.splice(index, 1);
        const error = new Error(`UE command '${type}' was aborted`);
        error.name = 'AbortError'; error.code = 'UE_COMMAND_ABORTED';
        this.totalErrors++;
        safeReject(error);
        if (this.inFlight !== job) this._pump();
      };
      job = {
        type, params, timeoutMs, queueDeadlineMs, maxAttempts, preSendAuthorize,
        maxResponseBytes, enqueuedAt: Date.now(),
        resolve: safeResolve, reject: safeReject, signal, cancelled: false,
      };
      if (signal) signal.addEventListener('abort', onAbort, { once: true });
      q.push(job);
      // Honor the queue deadline even if a long inFlight job (e.g. a 300s python)
      // would otherwise block the next _pump from sweeping this one.
      const t = setTimeout(() => this._dropStale(), queueDeadlineMs + 10);
      if (t.unref) t.unref();
      this._pump();
    });
  }

  status() {
    return {
      queueDepth: this.queue.length,
      pyQueueDepth: this.pyQueue.length,
      inFlight: this.inFlight ? this.inFlight.type : null,
      tokens: Math.floor(this.tokens),
      paused: this.paused,
      totalSent: this.totalSent,
      totalErrors: this.totalErrors,
      total429: this.total429,
      lastError: this.lastError,
    };
  }

  setPaused(p) { this.paused = !!p; if (!p) this._pump(); }

  _refill() {
    const now = Date.now();
    const dt = (now - this._lastRefill) / 1000;
    if (dt > 0) {
      this.tokens = Math.min(UE_BUCKET_CAP, this.tokens + dt * UE_BUCKET_RATE);
      this._lastRefill = now;
    }
  }

  _dropStale() {
    const now = Date.now();
    const sweep = (arr) => {
      const live = [];
      for (const job of arr) {
        if (now - job.enqueuedAt > job.queueDeadlineMs) {
          this.totalErrors++;
          job.reject(new Error(`UE queue deadline ${job.queueDeadlineMs}ms exceeded: ${job.type}`));
        } else live.push(job);
      }
      return live;
    };
    this.queue = sweep(this.queue);
    this.pyQueue = sweep(this.pyQueue);
  }

  _schedulePump(ms) {
    if (this._pumpScheduled) return;
    this._pumpScheduled = true;
    setTimeout(() => { this._pumpScheduled = false; this._pump(); }, Math.max(0, ms));
  }

  _pump() {
    this._dropStale();
    if (this.inFlight || this.paused) return;
    if (this.queue.length === 0 && this.pyQueue.length === 0) return;

    this._refill();
    if (this.tokens < 1) { this._schedulePump(Math.ceil(1000 / UE_BUCKET_RATE)); return; }

    const now = Date.now();
    const cdWait = this._lastCooldown - (now - this._lastCmdEnd);
    if (cdWait > 0) { this._schedulePump(cdWait); return; }

    // Normal queue has priority. If only python jobs remain, enforce the slow-lane interval.
    let job;
    if (this.queue.length > 0) {
      job = this.queue.shift();
    } else {
      const wait = UE_PY_MIN_INTERVAL_MS - (now - this._lastPyStart);
      if (wait > 0) { this._schedulePump(wait); return; }
      job = this.pyQueue.shift();
    }
    if (!job) return;

    this.inFlight = job;
    this.tokens -= 1;
    if (job.type === 'execute_python_script') this._lastPyStart = Date.now();
    this._lastCooldown = UE_COOLDOWN[job.type] || UE_COOLDOWN._default;

    Promise.resolve()
      .then(() => this._execWithRetry(
        job.type,
        job.params,
        job.timeoutMs,
        job.maxAttempts,
        job.signal,
        job.maxResponseBytes,
        job.preSendAuthorize,
      ))
      .then((r) => { if (!job.cancelled) { this.totalSent++; job.resolve(r); } })
      .catch((e) => { if (!job.cancelled) { this.totalErrors++; this.lastError = e && e.message; job.reject(e); } })
      .finally(() => { this._lastCmdEnd = Date.now(); this.inFlight = null; this._pump(); });
  }

  _execOnce(type, params, timeoutMs, signal, maxResponseBytes = null) {
    return new Promise((resolve, reject) => {
      if (signal && signal.aborted) {
        const error = new Error(`UE command '${type}' was aborted`);
        error.name = 'AbortError'; error.code = 'UE_COMMAND_ABORTED';
        reject(error); return;
      }
      const sock = new net.Socket();
      let settled = false;
      let buf = '';
      let responseBytes = 0;
      const cleanup = () => {
        clearTimeout(timer);
        if (signal) signal.removeEventListener('abort', onAbort);
        try { sock.destroy(); } catch (_error) {}
      };
      const finish = (fn, value) => { if (settled) return; settled = true; cleanup(); fn(value); };
      const onAbort = () => {
        const error = new Error(`UE command '${type}' was aborted`);
        error.name = 'AbortError'; error.code = 'UE_COMMAND_ABORTED';
        finish(reject, error);
      };
      const timer = setTimeout(() => finish(reject, new Error(`UE command '${type}' timed out after ${timeoutMs}ms`)), timeoutMs);
      if (signal) signal.addEventListener('abort', onAbort, { once: true });
      sock.connect(this.port, this.host, () => { sock.write(JSON.stringify({ type, params }) + '\n'); });
      sock.on('data', (d) => {
        responseBytes += Buffer.isBuffer(d) ? d.length : Buffer.byteLength(String(d), 'utf8');
        if (maxResponseBytes !== null && responseBytes > maxResponseBytes) {
          const error = new Error(`UE command '${type}' response exceeded its fixed byte limit`);
          error.code = 'UE_RESPONSE_TOO_LARGE';
          finish(reject, error);
          return;
        }
        buf += d.toString();
        try { finish(resolve, JSON.parse(buf)); } catch {}
      });
      sock.on('error', (e) => finish(reject, new Error(`UE connection error: ${e.message}`)));
      sock.on('close', () => {
        if (settled) return;
        if (!buf.trim()) return finish(reject, new Error('UE connection closed without a response'));
        try { finish(resolve, JSON.parse(buf)); } catch { finish(reject, new Error('Incomplete response from UE')); }
      });
    });
  }

  async _execWithRetry(type, params, timeoutMs, retries, signal, maxResponseBytes = null, preSendAuthorize) {
    let lastErr;
    for (let i = 0; i < retries; i++) {
      if (signal && signal.aborted) {
        const error = new Error(`UE command '${type}' was aborted`);
        error.name = 'AbortError'; error.code = 'UE_COMMAND_ABORTED';
        throw error;
      }
      if (preSendAuthorize) {
        let authorized = false;
        try { authorized = await preSendAuthorize(); } catch (_error) { authorized = false; }
        if (authorized !== true) {
          const error = new Error(`UE command '${type}' authorization expired before dispatch`);
          error.code = 'UE_COMMAND_AUTHORIZATION_EXPIRED';
          throw error;
        }
      }
      try { return await this._exec(type, params, timeoutMs, signal, maxResponseBytes); }
      catch (e) {
        lastErr = e;
        if (e && UE_NON_RETRYABLE_ERROR_CODES.has(e.code)) throw e;
        if (i < retries - 1) await new Promise((r) => setTimeout(r, 300 * (i + 1)));
      }
    }
    throw lastErr;
  }
}

let _ueInstance = null;
function getUeBroker() { if (!_ueInstance) _ueInstance = new UeMcpBroker(); return _ueInstance; }

module.exports = { UcvBroker, getBroker, UeMcpBroker, getUeBroker };

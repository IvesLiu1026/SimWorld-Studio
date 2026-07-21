'use strict';
/**
 * per-session-ports.js — Transparent per-session UE port routing.
 *
 * The web server's existing code (index.js, unreal-bridge.js, etc.) connects
 * to a single set of ports for UE's MCP and UnrealCV. For multi-tenant deploy
 * we need each request/agent loop to talk to the UE instance assigned to that
 * user, not the global default.
 *
 * Strategy:
 *   1. An Express middleware reads the HttpOnly Studio session cookie, looks
 *      up the session in SessionManager, and runs the rest of the request inside an
 *      AsyncLocalStorage context that carries the slot's port map.
 *   2. A one-time monkey-patch of `net.Socket.prototype.connect` and
 *      `net.createConnection` checks ALS at connect time. If a TCP connection
 *      is being opened to one of the well-known default ports on 127.0.0.1
 *      (UNREAL/MCP port, UCV port) AND we're inside a session context, the
 *      destination port is rewritten to the slot's port.
 *
 *      Outside a session context, behavior is unchanged (single-UE / dev).
 *
 * This avoids touching every TCP callsite in the minified index.js — they
 * still call `net.createConnection(DEFAULT_PORT, '127.0.0.1')` and "just work".
 *
 * Caveat: child_process.spawn() captures env vars at spawn time, so the
 * Claude CLI subprocess gets per-request env passing through `withSession`
 * → `currentEnv()` helper used by the spawn site.
 */

const net = require('net');
const { AsyncLocalStorage } = require('async_hooks');

const als = new AsyncLocalStorage();

/** Defaults that should be intercepted and rewritten when inside a session. */
const DEFAULT_MCP_PORT = parseInt(process.env.UE_DEFAULT_MCP_PORT || '55559', 10);
const DEFAULT_UCV_PORT = parseInt(process.env.UE_DEFAULT_UCV_PORT || '9017',  10);
const DEFAULT_HOSTS    = new Set(['127.0.0.1', 'localhost', '::1']);

let _patched = false;
let _sessionManager = null;
const SESSION_COOKIE = 'vista_stream_session';
const SESSION_TOKEN_PATTERN = /^[a-f0-9]{64}$/;

function setSessionManager(sm) { _sessionManager = sm; }

function sessionTokenFromCookie(header) {
  let found = null;
  for (const item of String(header || '').split(';')) {
    const separator = item.indexOf('=');
    if (separator < 0 || item.slice(0, separator).trim() !== SESSION_COOKIE) continue;
    if (found !== null) return '';
    try { found = decodeURIComponent(item.slice(separator + 1).trim()); }
    catch { return ''; }
  }
  return SESSION_TOKEN_PATTERN.test(found || '') ? found : '';
}

/** Run `fn` (sync or async) inside the session's port context. */
function withSession(rec, fn) {
  if (!rec || !rec.uePorts) return fn();
  return als.run({
    slotId:  rec.slotId,
    ports:   rec.uePorts,
    userId:  rec.userId,
  }, fn);
}

/** Current session context, or null. */
function current() {
  return als.getStore() || null;
}

/**
 * Build the env vars a spawned subprocess (Claude CLI, mcp-server, etc.)
 * should inherit so that *its* probes hit the right UE.
 */
function currentEnv(base = process.env) {
  const ctx = current();
  if (!ctx) return { ...base };
  return {
    ...base,
    UNREAL_HOST: '127.0.0.1',
    UNREAL_PORT: String(ctx.ports.mcpPort || ctx.ports.mcp),
    UCV_HOST:    '127.0.0.1',
    UCV_PORT:    String(ctx.ports.ucvPort || ctx.ports.ucv),
    SIMWORLD_SLOT_ID:       String(ctx.slotId),
  };
}

/** Express middleware: resolve token → session → run rest in ALS. */
function middleware() {
  return (req, res, next) => {
    if (!_sessionManager) return next();
    const tok = sessionTokenFromCookie(req.headers && req.headers.cookie);
    if (!tok) return next();
    const rec = _sessionManager.touch(tok);
    if (!rec) return next();
    if (!rec.mcpReady) {
      // UE is still starting; let the route decide how to respond.
      req.simworldSession = { ...rec, ready: false };
      return next();
    }
    req.simworldSession = rec;
    return withSession(rec, () => next());
  };
}

/**
 * Install net.Socket.prototype.connect monkey-patch. Idempotent.
 *
 * Rewrites connect targets only when:
 *   - inside an ALS session context
 *   - host is loopback
 *   - port matches a known default (MCP or UCV)
 *
 * Everything else passes through unchanged.
 */
function patchNet() {
  if (_patched) return;
  _patched = true;

  const origConnect = net.Socket.prototype.connect;

  net.Socket.prototype.connect = function patchedConnect(...args) {
    const ctx = current();
    if (!ctx) return origConnect.apply(this, args);

    // Normalize the many overloads of Socket.connect(...) into {port, host}
    let opts = null;
    if (args.length > 0 && typeof args[0] === 'object' && args[0] !== null && !Array.isArray(args[0])) {
      opts = args[0];
    } else if (typeof args[0] === 'number' || (typeof args[0] === 'string' && /^\d+$/.test(args[0]))) {
      opts = { port: parseInt(args[0], 10), host: typeof args[1] === 'string' ? args[1] : '127.0.0.1' };
    }
    if (!opts || typeof opts.port !== 'number') return origConnect.apply(this, args);

    const host = opts.host || '127.0.0.1';
    if (!DEFAULT_HOSTS.has(host)) return origConnect.apply(this, args);

    const slotMcp = ctx.ports.mcpPort || ctx.ports.mcp;
    const slotUcv = ctx.ports.ucvPort || ctx.ports.ucv;

    let rewrite = null;
    if (opts.port === DEFAULT_MCP_PORT && slotMcp && slotMcp !== DEFAULT_MCP_PORT) rewrite = slotMcp;
    else if (opts.port === DEFAULT_UCV_PORT && slotUcv && slotUcv !== DEFAULT_UCV_PORT) rewrite = slotUcv;

    if (rewrite == null) return origConnect.apply(this, args);

    const newOpts = { ...opts, port: rewrite };
    if (args.length > 0 && typeof args[0] === 'object') {
      return origConnect.call(this, newOpts, ...args.slice(1));
    }
    // (port, host?, cb?) form
    const cb = args.find((a) => typeof a === 'function');
    return cb
      ? origConnect.call(this, rewrite, host, cb)
      : origConnect.call(this, rewrite, host);
  };

  // net.createConnection / net.connect delegate to Socket.prototype.connect
  // under the hood in Node 18+, so patching the prototype covers both.
}

module.exports = {
  als,
  middleware,
  withSession,
  current,
  currentEnv,
  patchNet,
  setSessionManager,
  sessionTokenFromCookie,
};

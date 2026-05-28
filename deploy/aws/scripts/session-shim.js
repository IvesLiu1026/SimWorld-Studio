'use strict';
/**
 * session-shim.js — Boot-time wiring for the AWS multi-tenant deploy.
 *
 * Loaded into the web server with:
 *     node --require /opt/simworld-studio/deploy/aws/scripts/session-shim.js \
 *          /opt/simworld-studio/simworld_studio_workspace/web/server/index.js
 *
 * (systemd unit does this for us — see deploy/aws/systemd/simworld-web.service.)
 *
 * Responsibilities:
 *   1. Create a SlotPool sized to UE_POOL_SIZE (default 4).
 *   2. Attach it to the SessionManager singleton so acquire() actually
 *      spawns UE per slot and release() kills it.
 *   3. Install graceful-shutdown hooks so all UEs die when systemd stops us.
 *   4. Expose a per-token routing helper for the web server to use.
 */

const path = require('path');

function log(tag, msg) {
  console.log(`[session-shim] [${tag}] ${msg}`);
}

const enabled = process.env.UE_POOL_ENABLED === '1' ||
                process.env.UE_POOL_ENABLED === 'true';

if (!enabled) {
  log('init', 'UE_POOL_ENABLED is unset — running in legacy single-UE mode');
  return;
}

const { getSlotPool } = require(path.resolve(__dirname, 'slot-pool.js'));
const pool = getSlotPool();
log('init', `SlotPool created with size=${pool.size}, ` +
            `base MCP=${pool.cfg.baseMcp} stride=${pool.cfg.portStride}`);

// Attach to the SessionManager. We use require-cache lookup because
// session-manager.js may have been loaded already by index.js — we want the
// same singleton instance.
try {
  const sm = require(path.resolve(
    __dirname,
    '../../../simworld_studio_workspace/web/server/session-manager.js',
  ));
  if (sm && sm.sessionManager && typeof sm.sessionManager.setSlotPool === 'function') {
    sm.sessionManager.setSlotPool(pool);
    log('init', 'SessionManager.setSlotPool() wired up');
  } else {
    log('init', 'WARNING: sessionManager.setSlotPool not found — pool unused');
  }
} catch (e) {
  log('init', `ERROR loading session-manager: ${e.message}`);
}

// Graceful shutdown — kill all UE children on systemd stop.
let _shuttingDown = false;
async function shutdown(reason) {
  if (_shuttingDown) return;
  _shuttingDown = true;
  log('shutdown', `reason=${reason} — stopping all slots`);
  try {
    await pool.stopAll();
    log('shutdown', 'all slots stopped');
  } catch (e) {
    log('shutdown', `error: ${e.message}`);
  }
  process.exit(0);
}
process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT',  () => shutdown('SIGINT'));

// Make the pool reachable from the rest of the server (e.g. for an admin
// /api/pool/snapshot endpoint added later).
global.__simworldSlotPool = pool;

// ── Per-session port routing ────────────────────────────────────────────────
// Patch net.Socket.connect so any TCP connect to default MCP/UCV ports on
// loopback gets transparently rewritten to the requesting session's slot ports.
// This avoids touching every TCP callsite in the (minified) web server code.
try {
  const ports = require(path.resolve(__dirname, 'per-session-ports.js'));
  const sm = require(path.resolve(
    __dirname,
    '../../../simworld_studio_workspace/web/server/session-manager.js',
  ));
  ports.setSessionManager(sm.sessionManager);
  ports.patchNet();
  log('init', 'net.Socket.connect patched for per-session port routing');

  // Expose the helper for index.js and shim users
  global.__simworldPorts = ports;

  // Install the Express middleware lazily. We hook into Express by patching
  // express()'s returned app on first require, which guarantees we run before
  // any route handler. If index.js requires express before this shim, the
  // patch still works because express() returns a fresh app for each call.
  const Module = require('module');
  const origRequire = Module.prototype.require;
  Module.prototype.require = function patchedRequire(id) {
    const exported = origRequire.apply(this, arguments);
    if (id === 'express' && exported && !exported.__simworldPatched) {
      const origFn = exported;
      const wrapped = function (...args) {
        const app = origFn.apply(this, args);
        // Mount our middleware ahead of everything else
        app.use(ports.middleware());
        return app;
      };
      Object.assign(wrapped, origFn);
      wrapped.__simworldPatched = true;
      // Restore the original require for next call to avoid double-wrap
      Module.prototype.require = origRequire;
      return wrapped;
    }
    return exported;
  };
  log('init', 'express() patched to mount per-session middleware');
} catch (e) {
  log('init', `ERROR installing port router: ${e.message}`);
}

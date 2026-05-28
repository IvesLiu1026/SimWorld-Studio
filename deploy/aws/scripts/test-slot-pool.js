'use strict';
/**
 * test-slot-pool.js — Smoke test for SlotPool ↔ SessionManager integration.
 *
 *   node deploy/aws/scripts/test-slot-pool.js
 *
 * Uses a fake launcher that opens TCP listeners on the expected ports so the
 * pool's "wait for MCP" probe succeeds. No real UE involved.
 *
 * Exits 0 on success, non-zero on failure.
 */

const net = require('net');
const path = require('path');
const fs = require('fs');
const os = require('os');

const SHIM = path.resolve(__dirname, 'slot-pool.js');
const SM   = path.resolve(__dirname, '../../../simworld_studio_workspace/web/server/session-manager.js');

const { SlotPool } = require(SHIM);
const { SessionManager } = require(SM);

let failed = 0;
function check(name, cond) {
  if (cond) console.log(`  ✓ ${name}`);
  else { console.log(`  ✗ ${name}`); failed++; }
}

async function main() {
  // ── Build a fake launcher that opens a listener on the MCP port ──────────
  // We use an inline script so the test is self-contained.
  const fakeLauncherPath = path.join(os.tmpdir(), `fake-launcher-${process.pid}.sh`);
  fs.writeFileSync(fakeLauncherPath, `#!/bin/bash
# Fake launcher: parse --slot, open a TCP listener on the MCP port and sleep.
SLOT=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --slot) SLOT="$2"; shift 2;;
    *)      shift;;
  esac
done
PORT=$((${'$'}{UE_BASE_MCP:-55559} + SLOT * ${'$'}{UE_PORT_STRIDE:-2}))
echo "[fake-launcher] slot=$SLOT listening on $PORT" >&2
# 'nc -lk' would work but isn't universally available; use a tiny python TCP listener
python3 -c "
import socket, sys, signal
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('127.0.0.1', $PORT))
s.listen(8)
signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
signal.signal(signal.SIGINT,  lambda *a: sys.exit(0))
while True:
    try:
        c, _ = s.accept()
        c.close()
    except Exception:
        break
"
`);
  fs.chmodSync(fakeLauncherPath, 0o755);

  console.log('test 1: SlotPool starts and stops a fake slot');
  const pool = new SlotPool({
    poolSize:       2,
    launcher:       fakeLauncherPath,
    baseMcp:        61000,  // unlikely to collide
    portStride:     2,
    startupTimeout: 10_000,
  });
  const info = await pool.start(0);
  check('start() resolved', info.slotId === 0);
  check('ports object returned', info.ports && info.ports.mcp === 61000);
  check('status is up', pool.status(0) === 'up');
  await pool.stop(0);
  check('status is down after stop()', pool.status(0) === 'down');

  console.log('test 2: SessionManager with SlotPool acquires + releases a UE');
  const sm = new SessionManager();
  sm.setSlotPool(pool);
  const rec1 = await sm.acquire('user-alice');
  check('acquire resolved with mcpReady', rec1.mcpReady === true);
  check('slot 0 is up', pool.status(0) === 'up');
  check('uePorts contain slot-mapped ports', rec1.uePorts.mcpPort === 61000);

  console.log('test 3: same user reuses slot');
  const rec2 = await sm.acquire('user-alice');
  check('same token returned', rec1.token === rec2.token);

  console.log('test 4: release stops the UE');
  sm.release(rec1.token);
  // stop() is fire-and-forget; give it a moment
  await new Promise(r => setTimeout(r, 100));
  check('slot is freed', sm.freeSlots === 2);
  // wait for child exit (SIGTERM grace period)
  for (let i = 0; i < 50; i++) {
    if (pool.status(0) === 'down') break;
    await new Promise(r => setTimeout(r, 200));
  }
  check('slot 0 is down after release', pool.status(0) === 'down');

  console.log('test 5: legacy mode (no pool) still works');
  const sm2 = new SessionManager();
  const rec3 = await sm2.acquire('user-bob');
  check('acquire works without pool', rec3 && rec3.token);
  check('mcpReady=true in legacy mode', rec3.mcpReady === true);

  // ── Cleanup ──
  await pool.stopAll();
  fs.unlinkSync(fakeLauncherPath);

  if (failed === 0) {
    console.log('\nAll tests passed.');
    process.exit(0);
  } else {
    console.log(`\n${failed} test(s) failed.`);
    process.exit(1);
  }
}

main().catch((e) => { console.error('test crashed:', e); process.exit(2); });

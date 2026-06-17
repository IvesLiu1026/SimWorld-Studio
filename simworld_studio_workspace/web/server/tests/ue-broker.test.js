'use strict';
/* Standalone test for UeMcpBroker (no real UE). Run: node tests/ue-broker.test.js
 * Injects a fake one-shot executor so we exercise the queueing/throttle policy. */

// Test-friendly gate config — set BEFORE requiring the module (constants read at load).
process.env.UE_GATE_MAX_QUEUE = '5';
process.env.UE_GATE_MAX_PYQUEUE = '3';
process.env.UE_GATE_PY_INTERVAL_MS = '120';
process.env.UE_GATE_BUCKET_CAP = '1000';   // effectively unlimited for non-rate tests
process.env.UE_GATE_BUCKET_RATE = '1000';

const { UeMcpBroker } = require('../unreal-bridge');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let failures = 0;
function check(name, cond) {
  console.log(`${cond ? 'PASS' : 'FAIL'}: ${name}`);
  if (!cond) failures++;
}

async function test1_concurrency_and_order() {
  let active = 0, maxActive = 0;
  const order = [];
  const b = new UeMcpBroker({
    exec: async (type, params) => { active++; maxActive = Math.max(maxActive, active); await sleep(15); active--; return { status: 'success', i: params.i }; },
  });
  const ps = [];
  // 1 inFlight + 5 queued = 6 (== MAX_QUEUE cap) so none hit backpressure here.
  for (let i = 0; i < 6; i++) ps.push(b.send('get_actors_in_level', { i }).then((r) => order.push(r.i)).catch((e) => order.push('ERR' + i)));
  await Promise.all(ps);
  check('test1 concurrency never exceeds 1', maxActive === 1);
  check('test1 FIFO order preserved', order.join(',') === '0,1,2,3,4,5');
}

async function test2_backpressure() {
  let release;
  const gate = new Promise((r) => { release = r; });
  const b = new UeMcpBroker({ exec: () => gate.then(() => ({ status: 'success' })) });
  const results = [];
  for (let i = 0; i < 8; i++) b.send('spawn_actor', { i }).then(() => results.push({ i, ok: true })).catch((e) => results.push({ i, retryAfterMs: e.retryAfterMs }));
  await sleep(40); // let sync enqueue + microtask rejections settle
  const rejected = results.filter((r) => r.retryAfterMs);
  // 1 inFlight + 5 queued (cap=5) accepted; 2 beyond cap rejected with Retry-After
  check('test2 exactly 2 rejected on full queue', rejected.length === 2);
  check('test2 rejections carry retryAfterMs', rejected.every((r) => typeof r.retryAfterMs === 'number' && r.retryAfterMs > 0));
  check('test2 broker.total429 counts them', b.status().total429 === 2);
  release();
  await sleep(30);
}

async function test3_python_slow_lane() {
  const starts = [];
  const b = new UeMcpBroker({ exec: async (type) => { if (type === 'execute_python_script') starts.push(Date.now()); await sleep(5); return { status: 'success' }; } });
  await Promise.all([0, 1, 2].map((i) => b.send('execute_python_script', { script: `s${i}` })));
  const gaps = starts.slice(1).map((t, i) => t - starts[i]);
  check('test3 ran all 3 python jobs', starts.length === 3);
  check('test3 python starts spaced >= ~interval (' + gaps.join(',') + ')', gaps.every((g) => g >= 110));
}

async function test4_queue_deadline() {
  let release;
  const gate = new Promise((r) => { release = r; });
  const b = new UeMcpBroker({ exec: () => gate.then(() => ({ status: 'success' })) });
  b.send('spawn_actor', { blocker: true }).catch(() => {});   // becomes stuck inFlight
  let deadlineErr = null;
  b.send('get_actors_in_level', {}, { queueDeadlineMs: 80 }).catch((e) => { deadlineErr = e.message; });
  await sleep(160); // deadline sweep timer (80+10) should fire while inFlight is stuck
  check('test4 queued job rejected by deadline while inFlight blocked', !!deadlineErr && /deadline/i.test(deadlineErr));
  release();
  await sleep(20);
}

async function test5_status_shape() {
  const b = new UeMcpBroker({ exec: async () => ({ status: 'success' }) });
  const s = b.status();
  const keys = ['queueDepth', 'pyQueueDepth', 'inFlight', 'tokens', 'paused', 'totalSent', 'totalErrors', 'total429'];
  check('test5 status has all keys', keys.every((k) => k in s));
}

(async () => {
  await test1_concurrency_and_order();
  await test2_backpressure();
  await test3_python_slow_lane();
  await test4_queue_deadline();
  await test5_status_shape();
  console.log(failures === 0 ? '\nALL TESTS PASSED' : `\n${failures} TEST(S) FAILED`);
  process.exit(failures === 0 ? 0 : 1);
})();

'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { UeMcpBroker } = require('../unreal-bridge');

test('aborting a queued UE command removes it before execution', async () => {
  let releaseBlocker;
  let secondExecuted = false;
  const blocker = new Promise((resolve) => { releaseBlocker = resolve; });
  const broker = new UeMcpBroker({
    exec: async (_type, params) => {
      if (params.blocker) return blocker;
      secondExecuted = true;
      return { ok: true };
    },
  });

  const first = broker.send('get_actors_in_level', { blocker: true });
  const controller = new AbortController();
  const queued = broker.send('take_screenshot', {}, { signal: controller.signal });
  controller.abort();

  await assert.rejects(queued, (error) => error && error.code === 'UE_COMMAND_ABORTED');
  assert.equal(secondExecuted, false);
  assert.equal(broker.status().queueDepth, 0);
  releaseBlocker({ ok: true });
  await first;
});

test('aborting an in-flight UE command propagates to the executor', async () => {
  let observedSignal = null;
  const broker = new UeMcpBroker({
    exec: (_type, _params, _timeoutMs, signal) => new Promise((resolve, reject) => {
      observedSignal = signal;
      signal.addEventListener('abort', () => {
        const error = new Error('aborted in fake executor');
        error.code = 'UE_COMMAND_ABORTED';
        reject(error);
      }, { once: true });
    }),
  });
  const controller = new AbortController();
  const pending = broker.send('take_screenshot', {}, { signal: controller.signal });
  await new Promise((resolve) => setImmediate(resolve));
  controller.abort();

  await assert.rejects(pending, (error) => error && error.code === 'UE_COMMAND_ABORTED');
  assert.equal(observedSignal, controller.signal);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(broker.status().inFlight, null);
});

test('callers can disable transport retries for non-idempotent UE mutations', async () => {
  let attempts = 0;
  const broker = new UeMcpBroker({
    host: '127.0.0.1',
    port: 60001,
    exec: async () => {
      attempts += 1;
      throw new Error('response lost after mutation');
    },
  });

  await assert.rejects(
    broker.send('execute_python_script', { script: 'fixed' }, { maxAttempts: 1 }),
    /response lost/,
  );
  assert.equal(attempts, 1);
  await assert.rejects(
    broker.send('execute_python_script', { script: 'fixed' }, { maxAttempts: 0 }),
    /maxAttempts/,
  );
});

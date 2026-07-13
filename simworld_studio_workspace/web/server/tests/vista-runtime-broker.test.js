"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {
  FIXED_SETUP_SCRIPT,
  FIXED_STATE_SCRIPT,
  FIXED_STOP_SCRIPT,
  VISTA_GAME_MODE_CLASS,
  VISTA_PAWN_CLASS,
  VISTA_SETUP_MARKER,
  VISTA_SETUP_SCHEMA,
  VISTA_STOP_MARKER,
  VISTA_STOP_SCHEMA,
  VISTA_STATE_MARKER,
  VISTA_STATE_SCHEMA,
  bindFixedNonce,
  createVistaRuntimeBroker,
  extractSingleMarker,
  hasStrictlyEmptyBody,
  validateStatePayload,
  validateStopPayload,
} = require("../vista-runtime-broker");

const TEST_NONCE = "a".repeat(32);

function runtimeMarker(marker) {
  return `${marker}:${TEST_NONCE}`;
}

function createTestBroker(options) {
  return createVistaRuntimeBroker({
    initialRuntimePhase: "stopped",
    ...options,
    nonceFactory: () => TEST_NONCE,
  });
}

function setupPayload(overrides = {}) {
  return {
    schema: VISTA_SETUP_SCHEMA,
    phase: "prepared",
    prepared: true,
    game_mode_class: VISTA_GAME_MODE_CLASS,
    pawn_class: VISTA_PAWN_CLASS,
    default_pawn_class: VISTA_PAWN_CLASS,
    player_start_count: 1,
    ...overrides,
  };
}

function statePayload(overrides = {}) {
  return {
    schema: VISTA_STATE_SCHEMA,
    pie: true,
    possessed: true,
    pawn_class: VISTA_PAWN_CLASS,
    location: [100, -200, 110],
    rotation: [1, -90, 0],
    velocity: [300, 0, -1],
    on_ground: true,
    engine_time: 12.5,
    ...overrides,
  };
}

function stoppedPayload(overrides = {}) {
  return {
    schema: VISTA_STATE_SCHEMA,
    pie: false,
    possessed: false,
    pawn_class: null,
    location: null,
    rotation: null,
    velocity: null,
    on_ground: null,
    engine_time: null,
    ...overrides,
  };
}

function stopPayload(overrides = {}) {
  return {
    schema: VISTA_STOP_SCHEMA,
    phase: "stop_requested",
    stop_requested: true,
    was_playing: true,
    ...overrides,
  };
}

function markerReply(marker, payload, extraLogs = []) {
  const boundMarker = [VISTA_SETUP_MARKER, VISTA_STOP_MARKER, VISTA_STATE_MARKER].includes(marker)
    ? runtimeMarker(marker)
    : marker;
  return {
    result: {
      python_logs: [
        "reviewed prefix",
        `LogPython: ${boundMarker}:${JSON.stringify(payload)}`,
        ...extraLogs,
      ],
    },
  };
}

function responseRecorder() {
  return {
    body: null,
    headers: {},
    statusCode: null,
    set(name, value) { this.headers[name] = value; return this; },
    status(code) { this.statusCode = code; return this; },
    json(value) { this.body = value; return this; },
  };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

test("fixed scripts prepare only and expose no caller-authored Python surface", () => {
  const brokerSource = fs.readFileSync(path.resolve(__dirname, "../vista-runtime-broker.js"), "utf8");
  assert.doesNotMatch(brokerSource, /console\.|logToFile|logger\.(?:info|warn|error)/);
  assert.match(FIXED_SETUP_SCRIPT, /set_editor_property\('default_game_mode'/);
  assert.match(FIXED_SETUP_SCRIPT, /PlayerStart/);
  assert.match(FIXED_SETUP_SCRIPT, new RegExp(VISTA_SETUP_MARKER));
  assert.equal(FIXED_SETUP_SCRIPT.split(VISTA_SETUP_MARKER).length - 1, 1);
  assert.match(FIXED_SETUP_SCRIPT, new RegExp(VISTA_GAME_MODE_CLASS.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  assert.match(FIXED_SETUP_SCRIPT, new RegExp(VISTA_PAWN_CLASS.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  assert.doesNotMatch(FIXED_SETUP_SCRIPT, /editor_play|simulate|save_(?:asset|loaded_asset)|save_dirty_packages/i);

  assert.match(FIXED_STATE_SCRIPT, /get_game_world\(\)/);
  assert.match(FIXED_STATE_SCRIPT, /is_in_play_in_editor\(\)/);
  assert.match(FIXED_STATE_SCRIPT, /'pie': False/);
  assert.match(FIXED_STATE_SCRIPT, /get_player_controller\(game_world, 0\)/);
  assert.match(FIXED_STATE_SCRIPT, /get_player_pawn\(game_world, 0\)/);
  assert.doesNotMatch(FIXED_STATE_SCRIPT, /controller\.get_pawn\(\)/);
  assert.match(FIXED_STATE_SCRIPT, new RegExp(VISTA_STATE_MARKER));
  assert.equal(FIXED_STATE_SCRIPT.split(VISTA_STATE_MARKER).length - 1, 1);
  assert.doesNotMatch(FIXED_STATE_SCRIPT, /get_editor_world|editor_play_(?:simulate|in_viewport)|save_/i);

  assert.match(FIXED_STOP_SCRIPT, /editor_request_end_play\(\)/);
  assert.match(FIXED_STOP_SCRIPT, /is_in_play_in_editor\(\)/);
  assert.match(FIXED_STOP_SCRIPT, new RegExp(VISTA_STOP_MARKER));
  assert.equal(FIXED_STOP_SCRIPT.split(VISTA_STOP_MARKER).length - 1, 1);
  assert.doesNotMatch(FIXED_STOP_SCRIPT, /get_editor_world|editor_play_(?:simulate|in_viewport)|save_/i);
});

test("setup accepts only an absent or empty object body and returns the exact prepared contract", async () => {
  assert.equal(hasStrictlyEmptyBody(undefined), true);
  assert.equal(hasStrictlyEmptyBody(undefined, { "content-length": "0" }), true);
  assert.equal(hasStrictlyEmptyBody({}), true);
  assert.equal(hasStrictlyEmptyBody({}, { "content-length": "2" }), true);
  assert.equal(hasStrictlyEmptyBody(undefined, { "content-length": "7" }), false);
  assert.equal(hasStrictlyEmptyBody(undefined, { "transfer-encoding": "chunked" }), false);
  for (const value of [null, [], "", { class_name: VISTA_PAWN_CLASS }, { nested: {} }]) {
    assert.equal(hasStrictlyEmptyBody(value), false);
  }

  const calls = [];
  const ueBroker = {
    async send(type, params, options) {
      calls.push({ type, params, options });
      return markerReply(VISTA_SETUP_MARKER, setupPayload());
    },
  };
  const broker = createTestBroker({ ueBroker });

  const denied = responseRecorder();
  await broker.setupVistaPlayMode({ body: { pawn: "caller supplied" } }, denied);
  assert.equal(denied.statusCode, 400);
  assert.equal(denied.body.code, "VISTA_EMPTY_BODY_REQUIRED");
  assert.equal(calls.length, 0);

  const unparsedBytes = responseRecorder();
  await broker.setupVistaPlayMode(
    { body: undefined, headers: { "content-length": "7", "content-type": "text/plain" } },
    unparsedBytes,
  );
  assert.equal(unparsedBytes.statusCode, 400);
  assert.equal(calls.length, 0);

  const accepted = responseRecorder();
  await broker.setupVistaPlayMode({ body: undefined }, accepted);
  assert.equal(accepted.statusCode, 200);
  assert.deepEqual(accepted.body, {
    schema: VISTA_SETUP_SCHEMA,
    phase: "prepared",
    prepared: true,
    game_mode_class: VISTA_GAME_MODE_CLASS,
    pawn_class: VISTA_PAWN_CLASS,
    player_start_present: true,
    requires_operator_play: true,
    play_lease_granted: true,
    retry_after_ms: 30_000,
    state_probe_grace_ms: 30_000,
  });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].type, "execute_python_script");
  assert.deepEqual(Object.keys(calls[0].params), ["script"]);
  assert.equal(calls[0].params.script, bindFixedNonce(FIXED_SETUP_SCRIPT, VISTA_SETUP_MARKER, TEST_NONCE));
});

test("each execution rejects stale log markers and retries once with a fresh server nonce", async () => {
  const staleNonce = "1".repeat(32);
  const nonces = ["2".repeat(32), "3".repeat(32)];
  const calls = [];
  const ueBroker = {
    async send(_type, params) {
      calls.push(params.script);
      if (calls.length === 1) {
        return markerReply(`${VISTA_SETUP_MARKER}:${staleNonce}`, setupPayload());
      }
      const match = params.script.match(new RegExp(`${VISTA_SETUP_MARKER}:([a-f0-9]{32}):`));
      assert.ok(match);
      return markerReply(`${VISTA_SETUP_MARKER}:${match[1]}`, setupPayload());
    },
  };
  const broker = createVistaRuntimeBroker({
    ueBroker,
    initialRuntimePhase: "stopped",
    nonceFactory: () => nonces.shift(),
  });
  const response = responseRecorder();
  await broker.setupVistaPlayMode({ body: undefined }, response);
  assert.equal(response.statusCode, 200);
  assert.equal(calls.length, 2);
  assert.match(calls[0], new RegExp(`${VISTA_SETUP_MARKER}:${"2".repeat(32)}:`));
  assert.match(calls[1], new RegExp(`${VISTA_SETUP_MARKER}:${"3".repeat(32)}:`));
  assert.doesNotMatch(calls[0], new RegExp(staleNonce));
});

test("server FSM requires fresh stopped reconciliation before initial or post-Stop Play", async () => {
  const calls = [];
  const ueBroker = {
    async send(_type, params) {
      calls.push(params.script);
      if (params.script.includes(runtimeMarker(VISTA_STATE_MARKER))) {
        return markerReply(VISTA_STATE_MARKER, stoppedPayload());
      }
      if (params.script.includes(runtimeMarker(VISTA_SETUP_MARKER))) {
        return markerReply(VISTA_SETUP_MARKER, setupPayload());
      }
      return markerReply(VISTA_STOP_MARKER, stopPayload());
    },
  };
  const broker = createVistaRuntimeBroker({
    ueBroker,
    nonceFactory: () => TEST_NONCE,
    setupGraceMs: 0,
  });

  const unknownSetup = responseRecorder();
  await broker.setupVistaPlayMode({ body: undefined }, unknownSetup);
  assert.equal(unknownSetup.statusCode, 428);
  assert.equal(unknownSetup.body.code, "VISTA_STATE_RECONCILIATION_REQUIRED");
  assert.equal(calls.length, 0);

  const initialState = responseRecorder();
  await broker.getVistaState({}, initialState);
  assert.deepEqual(initialState.body, stoppedPayload());
  const setup = responseRecorder();
  await broker.setupVistaPlayMode({ body: undefined }, setup);
  assert.equal(setup.statusCode, 200);

  const stop = responseRecorder();
  await broker.stopVistaPlayMode({ body: undefined }, stop);
  assert.equal(stop.statusCode, 200);
  const prematureSetup = responseRecorder();
  await broker.setupVistaPlayMode({ body: undefined }, prematureSetup);
  assert.equal(prematureSetup.statusCode, 425);
  assert.equal(prematureSetup.body.code, "VISTA_STOP_IN_PROGRESS");

  const stoppedState = responseRecorder();
  await broker.getVistaState({}, stoppedState);
  assert.deepEqual(stoppedState.body, stoppedPayload());
  const restarted = responseRecorder();
  await broker.setupVistaPlayMode({ body: undefined }, restarted);
  assert.equal(restarted.statusCode, 200);
});

test("setup coalesces UE work, grants one Play lease, and blocks state for the operator grace", async () => {
  let clock = 1_000;
  const pendingSetup = deferred();
  const calls = [];
  const ueBroker = {
    send(type, params) {
      calls.push({ type, params });
      if (calls.length === 1) return pendingSetup.promise;
      return Promise.resolve(markerReply(VISTA_STATE_MARKER, statePayload()));
    },
  };
  const broker = createTestBroker({ ueBroker, now: () => clock });
  const first = responseRecorder();
  const duplicate = responseRecorder();
  const firstPromise = broker.setupVistaPlayMode({ body: undefined }, first);
  const duplicatePromise = broker.setupVistaPlayMode({ body: {} }, duplicate);
  await Promise.resolve();
  assert.equal(calls.length, 1);
  const setupPending = responseRecorder();
  await broker.getVistaState({}, setupPending);
  assert.equal(setupPending.statusCode, 425);
  assert.equal(setupPending.body.code, "VISTA_SETUP_IN_PROGRESS");
  assert.equal(setupPending.body.retry_after_ms, 1_000);
  assert.equal(calls.length, 1, "setup in flight must suppress state probes");
  pendingSetup.resolve(markerReply(VISTA_SETUP_MARKER, setupPayload()));
  await Promise.all([firstPromise, duplicatePromise]);
  assert.equal(first.statusCode, 200);
  assert.equal(first.body.play_lease_granted, true);
  assert.equal(duplicate.statusCode, 409);
  assert.equal(duplicate.body.code, "VISTA_PLAY_LEASE_HELD");

  const cachedSetup = responseRecorder();
  await broker.setupVistaPlayMode({ body: undefined }, cachedSetup);
  assert.equal(calls.length, 1);
  assert.equal(cachedSetup.statusCode, 409);
  assert.equal(cachedSetup.body.code, "VISTA_PLAY_LEASE_HELD");

  clock = 30_999;
  const grace = responseRecorder();
  await broker.getVistaState({}, grace);
  assert.equal(grace.statusCode, 425);
  assert.equal(grace.headers["Cache-Control"], "no-store");
  assert.equal(grace.headers["Retry-After"], "1");
  assert.equal(grace.body.code, "VISTA_PLAY_START_GRACE");
  assert.equal(grace.body.retry_after_ms, 1);
  assert.equal(calls.length, 1, "grace must not enqueue a UE state probe");

  clock = 31_000;
  const ready = responseRecorder();
  await broker.getVistaState({}, ready);
  assert.equal(ready.statusCode, 200);
  assert.equal(calls.length, 2);
});

test("stop accepts only an empty contract, coalesces duplicates, and clears setup grace", async () => {
  let clock = 1_000;
  const pendingStop = deferred();
  const calls = [];
  const ueBroker = {
    send(type, params) {
      calls.push({ type, params });
      if (params.script.includes(runtimeMarker(VISTA_SETUP_MARKER))) {
        return Promise.resolve(markerReply(VISTA_SETUP_MARKER, setupPayload()));
      }
      if (params.script.includes(runtimeMarker(VISTA_STOP_MARKER))) return pendingStop.promise;
      return Promise.resolve(markerReply(VISTA_STATE_MARKER, stoppedPayload()));
    },
  };
  const broker = createTestBroker({ ueBroker, now: () => clock });

  const setup = responseRecorder();
  await broker.setupVistaPlayMode({ body: undefined }, setup);
  assert.equal(setup.statusCode, 200);

  const denied = responseRecorder();
  await broker.stopVistaPlayMode({ body: { command: "caller supplied" } }, denied);
  assert.equal(denied.statusCode, 400);
  assert.equal(denied.body.code, "VISTA_EMPTY_BODY_REQUIRED");
  assert.equal(calls.length, 1);

  const unparsedBytes = responseRecorder();
  await broker.stopVistaPlayMode(
    { body: undefined, headers: { "content-length": "1", "content-type": "text/plain" } },
    unparsedBytes,
  );
  assert.equal(unparsedBytes.statusCode, 400);
  assert.equal(calls.length, 1);

  const first = responseRecorder();
  const duplicate = responseRecorder();
  const firstPromise = broker.stopVistaPlayMode({ body: undefined }, first);
  const duplicatePromise = broker.stopVistaPlayMode({ body: {} }, duplicate);
  await Promise.resolve();
  assert.equal(calls.length, 2);
  assert.equal(calls[1].type, "execute_python_script");
  assert.deepEqual(Object.keys(calls[1].params), ["script"]);
  assert.equal(calls[1].params.script, bindFixedNonce(FIXED_STOP_SCRIPT, VISTA_STOP_MARKER, TEST_NONCE));
  pendingStop.resolve(markerReply(VISTA_STOP_MARKER, stopPayload()));
  await Promise.all([firstPromise, duplicatePromise]);
  assert.deepEqual(duplicate.body, first.body);
  assert.deepEqual(first.body, {
    schema: VISTA_STOP_SCHEMA,
    phase: "stop_requested",
    stop_requested: true,
    was_playing: true,
    retry_after_ms: 500,
  });

  // Stop clears the 30-second setup grace, so state is read immediately.
  clock = 1_001;
  const stopped = responseRecorder();
  await broker.getVistaState({}, stopped);
  assert.equal(stopped.statusCode, 200);
  assert.deepEqual(stopped.body, stoppedPayload());
  assert.equal(calls.length, 3);
});

test("an authoritative stopped-state read releases an abandoned Play lease", async () => {
  let setupCalls = 0;
  const ueBroker = {
    async send(_type, params) {
      if (params.script.includes(runtimeMarker(VISTA_SETUP_MARKER))) {
        setupCalls += 1;
        return markerReply(VISTA_SETUP_MARKER, setupPayload());
      }
      return markerReply(VISTA_STATE_MARKER, stoppedPayload());
    },
  };
  const broker = createTestBroker({ ueBroker, setupGraceMs: 0 });
  const first = responseRecorder();
  await broker.setupVistaPlayMode({ body: undefined }, first);
  assert.equal(first.statusCode, 200);

  const denied = responseRecorder();
  await broker.setupVistaPlayMode({ body: undefined }, denied);
  assert.equal(denied.statusCode, 409);

  const stopped = responseRecorder();
  await broker.getVistaState({}, stopped);
  assert.deepEqual(stopped.body, stoppedPayload());

  const reclaimed = responseRecorder();
  await broker.setupVistaPlayMode({ body: undefined }, reclaimed);
  assert.equal(reclaimed.statusCode, 200);
  assert.equal(reclaimed.body.play_lease_granted, true);
  assert.equal(setupCalls, 2);
});

test("stop validation rejects malformed or extra marker data without leaking raw logs", async () => {
  assert.deepEqual(validateStopPayload(stopPayload({ was_playing: false })), {
    schema: VISTA_STOP_SCHEMA,
    phase: "stop_requested",
    stop_requested: true,
    was_playing: false,
    retry_after_ms: 500,
  });
  for (const rawReply of [
    markerReply(VISTA_STOP_MARKER, { ...stopPayload(), raw_secret: "TOP_SECRET" }),
    markerReply(VISTA_STOP_MARKER, stopPayload({ phase: "wrong" })),
    { result: { python_logs: [`${runtimeMarker(VISTA_STOP_MARKER)}:{malformed TOP_SECRET`] } },
  ]) {
    const broker = createTestBroker({ ueBroker: { send: async () => rawReply } });
    const response = responseRecorder();
    await broker.stopVistaPlayMode({ body: undefined }, response);
    assert.equal(response.statusCode, 502);
    assert.doesNotMatch(JSON.stringify(response.body), /TOP_SECRET|python_logs|raw_secret/);
  }
});

test("state reads coalesce in flight and remain capped at two starts per second", async () => {
  let clock = 5_000;
  const pendingState = deferred();
  const calls = [];
  const ueBroker = {
    send(type, params) {
      calls.push({ type, params });
      if (calls.length === 1) return pendingState.promise;
      return Promise.resolve(markerReply(VISTA_STATE_MARKER, statePayload({ engine_time: 13 })));
    },
  };
  const broker = createTestBroker({ ueBroker, now: () => clock });
  const first = responseRecorder();
  const duplicate = responseRecorder();
  const firstPromise = broker.getVistaState({}, first);
  const duplicatePromise = broker.getVistaState({}, duplicate);
  await Promise.resolve();
  assert.equal(calls.length, 1);
  pendingState.resolve(markerReply(VISTA_STATE_MARKER, statePayload()));
  await Promise.all([firstPromise, duplicatePromise]);
  assert.equal(first.statusCode, 200);
  assert.deepEqual(duplicate.body, first.body);
  assert.equal(first.headers["Cache-Control"], "no-store");

  clock += 499;
  const cached = responseRecorder();
  await broker.getVistaState({}, cached);
  assert.equal(calls.length, 1);
  assert.deepEqual(cached.body, first.body);

  clock += 1;
  const refreshed = responseRecorder();
  await broker.getVistaState({}, refreshed);
  assert.equal(calls.length, 2);
  assert.equal(refreshed.body.engine_time, 13);
});

test("state validation rejects malformed, duplicate, wrong-pawn, non-finite, and extra data", async () => {
  const valid = statePayload();
  assert.deepEqual(validateStatePayload(stoppedPayload()), stoppedPayload());
  assert.throws(
    () => validateStatePayload(stoppedPayload({ location: [0, 0, 0] })),
    /VISTA_STATE_STOPPED_INVALID/,
  );
  assert.throws(
    () => extractSingleMarker({ result: { python_logs: [] } }, VISTA_STATE_MARKER),
    /VISTA_MARKER_MISSING/,
  );
  assert.throws(
    () => extractSingleMarker({
      result: { python_logs: [`${VISTA_STATE_MARKER}:${JSON.stringify(valid)}`, `${VISTA_STATE_MARKER}:${JSON.stringify(valid)}`] },
    }, VISTA_STATE_MARKER),
    /VISTA_MARKER_DUPLICATE/,
  );
  assert.throws(
    () => validateStatePayload(statePayload({ pawn_class: "/Game/Wrong.Wrong_C" })),
    /VISTA_PAWN_CLASS_MISMATCH/,
  );
  assert.throws(
    () => validateStatePayload(statePayload({ velocity: [0, Number.POSITIVE_INFINITY, 0] })),
    /VISTA_STATE_VECTOR_INVALID/,
  );
  assert.throws(
    () => validateStatePayload({ ...statePayload(), raw_secret: "must not pass" }),
    /VISTA_STATE_SHAPE_INVALID/,
  );

  const nonFiniteJson = JSON.stringify(valid).replace('"location":[100,-200,110]', '"location":[1e999,-200,110]');
  const replies = [
    { result: { python_logs: [`${runtimeMarker(VISTA_STATE_MARKER)}:{malformed TOP_SECRET`] } },
    { result: { python_logs: [`${runtimeMarker(VISTA_STATE_MARKER)}:${JSON.stringify(valid)}`, `${runtimeMarker(VISTA_STATE_MARKER)}:${JSON.stringify(valid)}`] } },
    markerReply(VISTA_STATE_MARKER, statePayload({ pawn_class: "/Game/TOP_SECRET.Wrong_C" })),
    { result: { python_logs: [`${runtimeMarker(VISTA_STATE_MARKER)}:${nonFiniteJson}`] } },
    markerReply(VISTA_STATE_MARKER, { ...statePayload(), raw_secret: "TOP_SECRET" }),
  ];
  for (const rawReply of replies) {
    const broker = createTestBroker({ ueBroker: { send: async () => rawReply } });
    const response = responseRecorder();
    await broker.getVistaState({}, response);
    assert.equal(response.statusCode, 502);
    assert.doesNotMatch(JSON.stringify(response.body), /TOP_SECRET|python_logs|raw_secret/);
  }
});

test("transport failures and setup marker extras are sanitized with no raw logs", async () => {
  const unavailable = createTestBroker({
    ueBroker: { send: async () => { throw new Error("TOP_SECRET raw UE reply"); } },
  });
  const unavailableResponse = responseRecorder();
  await unavailable.getVistaState({}, unavailableResponse);
  assert.equal(unavailableResponse.statusCode, 503);
  assert.equal(unavailableResponse.body.code, "VISTA_RUNTIME_UNAVAILABLE");
  assert.doesNotMatch(JSON.stringify(unavailableResponse.body), /TOP_SECRET|raw UE reply/);

  const extraSetup = createTestBroker({
    ueBroker: {
      send: async () => markerReply(VISTA_SETUP_MARKER, { ...setupPayload(), raw_secret: "TOP_SECRET" }),
    },
  });
  const setupResponse = responseRecorder();
  await extraSetup.setupVistaPlayMode({ body: undefined }, setupResponse);
  assert.equal(setupResponse.statusCode, 502);
  assert.doesNotMatch(JSON.stringify(setupResponse.body), /TOP_SECRET|raw_secret/);
});

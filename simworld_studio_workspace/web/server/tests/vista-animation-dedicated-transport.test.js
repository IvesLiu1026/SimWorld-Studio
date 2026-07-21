"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const net = require("node:net");

const { createProductionExecutionGuard, productionMcpToolAllowed } = require("../production-execution-policy");
const { UeMcpBroker } = require("../unreal-bridge");
const { createVistaSlotBrokerResolver } = require("../vista-scene-executor-runtime");
const {
  ANIMATION_UE_OPERATION_ALLOWLIST,
  ANIMATION_UE_REQUEST_SCHEMA,
} = require("../vista-animation-ue-adapter");
const {
  TRANSPORT_METHODS,
  createVistaAnimationDedicatedTransportResolver,
} = require("../vista-animation-dedicated-transport");

const IDENTITY = Object.freeze({
  ownerId: "owner-transport-test",
  sessionId: "session-transport-test",
  slotId: 2,
  leaseId: "lease-transport-test",
  mcpPort: 55559,
  sceneRevision: "scene-transport-r1",
  planId: `vsp-${"a".repeat(24)}`,
});

function success(value) {
  return { status: "success", result: JSON.stringify(value) };
}

function contentRequest(method, { request = {}, operationFingerprint } = {}) {
  const operation = ANIMATION_UE_OPERATION_ALLOWLIST[method];
  if (!operation) throw new Error(`unknown test operation ${method}`);
  return JSON.stringify({
    schema: ANIMATION_UE_REQUEST_SCHEMA,
    operation_id: operation.operation_id,
    operation_fingerprint: operationFingerprint || operation.operation_fingerprint,
    invocation_id: "vau-offline-test",
    request_digest: "b".repeat(64),
    content_proof: {},
    nonce_marker: {},
    request,
  });
}

function createRecordingBroker(port = IDENTITY.mcpPort, responder = (type) => success({ type })) {
  const calls = [];
  return {
    port,
    calls,
    async send(type, params, options) {
      calls.push({ type, params, options });
      return responder(type, params, options);
    },
  };
}

async function waitFor(predicate, timeoutMs = 1000) {
  const startedAt = Date.now();
  while (!predicate()) {
    if (Date.now() - startedAt > timeoutMs) throw new Error("timed out waiting for test condition");
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

function createActiveResolver(broker, binding = IDENTITY) {
  const bindings = [];
  const resolveUeBroker = createVistaSlotBrokerResolver({
    studioStreaming: {
      isActiveSessionBinding(identity) {
        bindings.push(identity);
        return identity.ownerId === binding.ownerId
          && identity.sessionId === binding.sessionId
          && identity.slotId === binding.slotId
          && identity.leaseId === binding.leaseId
          && identity.mcpPort === binding.mcpPort;
      },
    },
    defaultBroker: broker,
    BrokerClass: class UnexpectedBroker {
      constructor() { throw new Error("unexpected secondary broker"); }
    },
  });
  return {
    bindings,
    resolve: createVistaAnimationDedicatedTransportResolver({ resolveUeBroker }),
  };
}

test("resolver exposes only four fixed methods and dispatches exact command/param envelopes", async () => {
  const broker = createRecordingBroker();
  const runtime = createActiveResolver(broker);
  const transport = await runtime.resolve(IDENTITY);

  assert.deepEqual(Object.keys(transport).sort(), [...TRANSPORT_METHODS]);
  assert.equal(Object.isFrozen(transport), true);

  const probe = await transport.probeAnimationContentApi(
    JSON.stringify({ probe: true }),
    { mutation: false, maxAttempts: 1, timeoutMs: 2500, queueDeadlineMs: 2500 },
  );
  const contentRead = await transport.invokeAnimationContentApi(
    contentRequest("preflightAnimation"),
    { mutation: false, maxAttempts: 2, timeoutMs: 15_000, queueDeadlineMs: 30_000 },
  );
  const contentMutation = await transport.invokeAnimationContentApi(
    contentRequest("startAnimationAction"),
    { mutation: true, maxAttempts: 1, timeoutMs: 10_000, queueDeadlineMs: 20_000 },
  );
  const engineTime = await transport.sampleAnimationEngineTime(
    JSON.stringify({ sample: true }),
    { mutation: false, maxAttempts: 1 },
  );
  const evidence = await transport.captureAnimationEvidence(
    JSON.stringify({ kind: "pose_snapshot" }),
    { mutation: false, maxAttempts: 1 },
  );

  assert.deepEqual([probe, contentRead, contentMutation, engineTime, evidence].map(JSON.parse), [
    { type: "vista_animation_capabilities" },
    { type: "vista_animation_content_api" },
    { type: "vista_animation_content_api" },
    { type: "vista_animation_engine_time" },
    { type: "vista_animation_evidence_capture" },
  ]);
  assert.deepEqual(broker.calls.map((call) => call.type), [
    "vista_animation_capabilities",
    "vista_animation_content_api",
    "vista_animation_content_api",
    "vista_animation_engine_time",
    "vista_animation_evidence_capture",
  ]);
  for (const call of broker.calls) {
    assert.deepEqual(Object.keys(call.params), ["request_json"]);
    assert.equal(Object.isFrozen(call.params), true);
    assert.equal(Object.prototype.hasOwnProperty.call(call.params, "script"), false);
    assert.equal(Object.prototype.hasOwnProperty.call(call.params, "command"), false);
  }
  assert.equal(broker.calls[1].options.maxAttempts, 2, "bounded read retry is preserved");
  assert.equal(broker.calls[2].options.maxAttempts, 1, "mutation is no-retry");
  assert.equal(broker.calls[3].options.timeoutMs, 3000, "engine time uses its restricted default");
  assert.equal(broker.calls[4].options.queueDeadlineMs, 15_000, "evidence uses its restricted default");
  assert.equal(runtime.bindings.length, 6, "the binding is checked at resolution and before every command");
  assert.ok(runtime.bindings.every((binding) => binding.mcpPort === IDENTITY.mcpPort));
});

test("slot resolver owns per-slot brokers and transport verifies the identity mcpPort", async () => {
  const defaultBroker = createRecordingBroker();
  const instances = [];
  class SlotBroker {
    constructor({ host, port }) {
      this.host = host;
      this.port = port;
      this.calls = [];
      instances.push(this);
    }

    async send(type, params, options) {
      this.calls.push({ type, params, options });
      return success({ fixed: type });
    }
  }
  const slotIdentity = Object.freeze({ ...IDENTITY, slotId: 3, mcpPort: 55603 });
  const resolveUeBroker = createVistaSlotBrokerResolver({
    studioStreaming: { isActiveSessionBinding: (identity) => identity.mcpPort === slotIdentity.mcpPort },
    defaultBroker,
    BrokerClass: SlotBroker,
  });
  const resolve = createVistaAnimationDedicatedTransportResolver({ resolveUeBroker });
  const transport = await resolve(slotIdentity);
  await transport.sampleAnimationEngineTime("{}", { mutation: false, maxAttempts: 1 });

  assert.equal(instances.length, 1);
  assert.equal(instances[0].host, "127.0.0.1");
  assert.equal(instances[0].port, slotIdentity.mcpPort);
  assert.equal(instances[0].calls[0].type, "vista_animation_engine_time");

  const mismatched = createVistaAnimationDedicatedTransportResolver({
    resolveUeBroker: () => createRecordingBroker(60000),
  });
  await assert.rejects(
    mismatched(IDENTITY),
    (error) => error.code === "ANIMATION_UE_TRANSPORT_BINDING_MISMATCH" && error.status === 409,
  );
});

test("inactive binding returns no transport and malformed runtime identities fail before broker resolution", async () => {
  let resolutions = 0;
  const resolve = createVistaAnimationDedicatedTransportResolver({
    resolveUeBroker: () => { resolutions += 1; return null; },
  });
  assert.equal(await resolve(IDENTITY), null);
  assert.equal(resolutions, 1);

  await assert.rejects(
    resolve({ ...IDENTITY, mcpPort: 0 }),
    (error) => error.code === "ANIMATION_UE_TRANSPORT_IDENTITY_INVALID",
  );
  await assert.rejects(
    resolve({ ...IDENTITY, callerCommand: "execute_python_script" }),
    (error) => error.code === "ANIMATION_UE_TRANSPORT_IDENTITY_INVALID",
  );
  assert.equal(resolutions, 1, "invalid identities never reach the server-owned slot resolver");
  assert.throws(() => createVistaAnimationDedicatedTransportResolver({}), /resolveUeBroker/);
});

test("a lease revoked after runtime resolution fails closed before UE dispatch", async () => {
  let active = true;
  const broker = createRecordingBroker();
  const resolveUeBroker = createVistaSlotBrokerResolver({
    studioStreaming: { isActiveSessionBinding: () => active },
    defaultBroker: broker,
    BrokerClass: class UnexpectedBroker {},
  });
  const resolve = createVistaAnimationDedicatedTransportResolver({ resolveUeBroker });
  const transport = await resolve(IDENTITY);
  active = false;

  await assert.rejects(
    transport.sampleAnimationEngineTime("{}", { mutation: false, maxAttempts: 1 }),
    (error) => error.code === "ANIMATION_UE_TRANSPORT_UNAVAILABLE" && error.retryable === true,
  );
  assert.equal(broker.calls.length, 0);
});

test("a mutation revoked while queued is rejected by the broker pre-send authorization hook", async () => {
  let active = true;
  let releaseBlocker;
  const blocker = new Promise((resolve) => { releaseBlocker = resolve; });
  const executedTypes = [];
  const broker = new UeMcpBroker({
    port: IDENTITY.mcpPort,
    exec: async (type) => {
      executedTypes.push(type);
      if (type === "test_blocker") return blocker;
      return success({ type });
    },
  });
  const resolve = createVistaAnimationDedicatedTransportResolver({
    resolveUeBroker: () => (active ? broker : null),
  });
  const transport = await resolve(IDENTITY);
  const first = broker.send("test_blocker", {}, { maxAttempts: 1 });
  await waitFor(() => broker.status().inFlight === "test_blocker");
  const queuedMutation = transport.invokeAnimationContentApi(
    contentRequest("startAnimationAction"),
    { mutation: true, maxAttempts: 1 },
  );
  await waitFor(() => broker.status().queueDepth === 1);
  assert.deepEqual(Object.keys(broker.queue[0].params), ["request_json"]);

  active = false;
  releaseBlocker(success({ released: true }));
  await first;
  await assert.rejects(
    queuedMutation,
    (error) => error.code === "ANIMATION_UE_TRANSPORT_UNAVAILABLE" && error.retryable === false,
  );
  assert.deepEqual(executedTypes, ["test_blocker"]);
});

test("mutation and restricted read retry/timeout policies fail closed before dispatch", async () => {
  const broker = createRecordingBroker();
  const { resolve } = createActiveResolver(broker);
  const transport = await resolve(IDENTITY);
  const cases = [
    [() => transport.invokeAnimationContentApi(contentRequest("startAnimationAction"), { mutation: true, maxAttempts: 2 }), "ANIMATION_UE_TRANSPORT_OPTIONS_INVALID"],
    [() => transport.invokeAnimationContentApi(contentRequest("preflightAnimation"), { mutation: false, maxAttempts: 3 }), "ANIMATION_UE_TRANSPORT_OPTIONS_INVALID"],
    [() => transport.invokeAnimationContentApi(contentRequest("startAnimationAction"), { mutation: false, maxAttempts: 2 }), "ANIMATION_UE_TRANSPORT_OPTIONS_INVALID"],
    [() => transport.invokeAnimationContentApi(contentRequest("preflightAnimation"), { mutation: true, maxAttempts: 1 }), "ANIMATION_UE_TRANSPORT_OPTIONS_INVALID"],
    [() => transport.invokeAnimationContentApi(contentRequest("startAnimationAction", {
      operationFingerprint: "f".repeat(64),
    }), { mutation: true, maxAttempts: 1 }), "ANIMATION_UE_TRANSPORT_REQUEST_INVALID"],
    [() => transport.probeAnimationContentApi("{}", { mutation: false, maxAttempts: 2 }), "ANIMATION_UE_TRANSPORT_OPTIONS_INVALID"],
    [() => transport.captureAnimationEvidence("{}", { mutation: true, maxAttempts: 1 }), "ANIMATION_UE_TRANSPORT_OPTIONS_INVALID"],
    [() => transport.sampleAnimationEngineTime("{}", { mutation: false, maxAttempts: 1, timeoutMs: 10_001 }), "ANIMATION_UE_TRANSPORT_OPTIONS_INVALID"],
    [() => transport.probeAnimationContentApi("{}", { mutation: false, maxAttempts: 1, queueDeadlineMs: 10_001 }), "ANIMATION_UE_TRANSPORT_OPTIONS_INVALID"],
  ];
  for (const [invoke, code] of cases) {
    await assert.rejects(invoke, (error) => error.code === code);
  }
  assert.equal(broker.calls.length, 0);

  const controller = new AbortController();
  await transport.invokeAnimationContentApi(contentRequest("preflightAnimation"), {
    mutation: false,
    maxAttempts: 2,
    signal: controller.signal,
  });
  assert.equal(broker.calls[0].options.signal, controller.signal);
  assert.deepEqual(Object.keys(broker.calls[0].options).sort(), [
    "maxAttempts",
    "maxResponseBytes",
    "preSendAuthorize",
    "queueDeadlineMs",
    "signal",
    "timeoutMs",
  ]);
});

test("every fixed content operation derives mutation and retry policy from the adapter contract", async () => {
  const broker = createRecordingBroker();
  const { resolve } = createActiveResolver(broker);
  const transport = await resolve(IDENTITY);
  const mutationMethods = [
    "startAnimationAction",
    "stopAnimationAction",
    "releaseAnimationAction",
    "restoreAnimationState",
  ];
  const readMethods = [
    "preflightAnimation",
    "snapshotAnimationState",
    "waitAnimationAction",
  ];
  for (const method of mutationMethods) {
    await transport.invokeAnimationContentApi(contentRequest(method), { mutation: true, maxAttempts: 1 });
  }
  for (const method of readMethods) {
    await transport.invokeAnimationContentApi(contentRequest(method), { mutation: false, maxAttempts: 2 });
  }
  assert.deepEqual(broker.calls.map((call) => call.options.maxAttempts), [1, 1, 1, 1, 2, 2, 2]);
});

test("real UeMcpBroker retries bounded reads but never retries a mutation", async () => {
  let mutationAttempts = 0;
  const mutationBroker = new UeMcpBroker({
    port: IDENTITY.mcpPort,
    exec: async () => {
      mutationAttempts += 1;
      throw new Error("simulated disconnect");
    },
  });
  const mutationTransport = await createVistaAnimationDedicatedTransportResolver({
    resolveUeBroker: () => mutationBroker,
  })(IDENTITY);
  await assert.rejects(
    mutationTransport.invokeAnimationContentApi(
      contentRequest("startAnimationAction"),
      { mutation: true, maxAttempts: 1 },
    ),
    (error) => error.code === "ANIMATION_UE_TRANSPORT_UNAVAILABLE" && error.retryable === false,
  );
  assert.equal(mutationAttempts, 1);

  let readAttempts = 0;
  const readBroker = new UeMcpBroker({
    port: IDENTITY.mcpPort,
    exec: async () => {
      readAttempts += 1;
      throw new Error("simulated disconnect");
    },
  });
  const readTransport = await createVistaAnimationDedicatedTransportResolver({
    resolveUeBroker: () => readBroker,
  })(IDENTITY);
  await assert.rejects(
    readTransport.invokeAnimationContentApi(
      contentRequest("preflightAnimation"),
      { mutation: false, maxAttempts: 2 },
    ),
    (error) => error.code === "ANIMATION_UE_TRANSPORT_UNAVAILABLE" && error.retryable === true,
  );
  assert.equal(readAttempts, 2);
});

test("a bounded read reauthorizes before every retry attempt", async () => {
  let active = true;
  let executionAttempts = 0;
  const broker = new UeMcpBroker({
    port: IDENTITY.mcpPort,
    exec: async () => {
      executionAttempts += 1;
      active = false;
      throw new Error("first read attempt disconnected");
    },
  });
  const transport = await createVistaAnimationDedicatedTransportResolver({
    resolveUeBroker: () => (active ? broker : null),
  })(IDENTITY);

  await assert.rejects(
    transport.invokeAnimationContentApi(
      contentRequest("preflightAnimation"),
      { mutation: false, maxAttempts: 2 },
    ),
    (error) => error.code === "ANIMATION_UE_TRANSPORT_UNAVAILABLE" && error.retryable === true,
  );
  assert.equal(executionAttempts, 1, "revocation prevents the second socket attempt");
});

test("UeMcpBroker enforces a scoped response byte cap before buffering the full reply", async (t) => {
  let connections = 0;
  const server = net.createServer((socket) => {
    connections += 1;
    socket.once("data", () => {
      socket.end(`{"status":"success","result":"${"x".repeat(4096)}"}`);
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const address = server.address();
  const broker = new UeMcpBroker({ host: "127.0.0.1", port: address.port });

  await assert.rejects(
    broker.send("vista_animation_capabilities", { request_json: "{}" }, {
      maxAttempts: 2,
      maxResponseBytes: 256,
    }),
    (error) => error.code === "UE_RESPONSE_TOO_LARGE",
  );
  assert.equal(connections, 1, "oversized replies are non-retryable");
  await assert.rejects(
    broker.send("vista_animation_capabilities", { request_json: "{}" }, { maxResponseBytes: 1 }),
    /maxResponseBytes/,
  );
});

test("request and outer UE result contracts reject malformed, unknown, and oversized data", async () => {
  const replies = [
    { status: "success", result: "{}", extra: true },
    { status: "success", result: {} },
    { status: "success", result: "not-json" },
    { status: "success", result: "[]" },
    { status: "success", result: JSON.stringify({ value: "x".repeat(131_072) }) },
  ];
  const broker = createRecordingBroker(IDENTITY.mcpPort, () => replies.shift());
  const { resolve } = createActiveResolver(broker);
  const transport = await resolve(IDENTITY);
  for (let index = 0; index < 5; index += 1) {
    await assert.rejects(
      transport.probeAnimationContentApi("{}", { mutation: false, maxAttempts: 1 }),
      (error) => error.code === "ANIMATION_UE_TRANSPORT_PROTOCOL_INVALID" && error.status === 502,
    );
  }

  const sentBeforeInvalidRequests = broker.calls.length;
  for (const request of [null, "not-json", "[]", JSON.stringify({ value: "x".repeat(131_072) })]) {
    await assert.rejects(
      transport.probeAnimationContentApi(request, { mutation: false, maxAttempts: 1 }),
      (error) => error.code === "ANIMATION_UE_TRANSPORT_REQUEST_INVALID",
    );
  }
  assert.equal(broker.calls.length, sentBeforeInvalidRequests, "invalid request data never reaches UE");
});

test("broker failures are redacted and there is no Python, console, or fallback dispatch", async () => {
  const secret = "caller-secret-python-body";
  const broker = createRecordingBroker(IDENTITY.mcpPort, async () => {
    throw new Error(secret);
  });
  const { resolve } = createActiveResolver(broker);
  const transport = await resolve(IDENTITY);
  await assert.rejects(
    transport.invokeAnimationContentApi(contentRequest("startAnimationAction", {
      request: { script: secret },
    }), {
      mutation: true,
      maxAttempts: 1,
    }),
    (error) => {
      assert.equal(error.code, "ANIMATION_UE_TRANSPORT_UNAVAILABLE");
      assert.equal(error.retryable, false);
      assert.doesNotMatch(error.message, new RegExp(secret));
      return true;
    },
  );
  assert.deepEqual(broker.calls.map((call) => call.type), ["vista_animation_content_api"]);
  assert.equal(broker.calls.some((call) => /python|console|script/i.test(call.type)), false);
  assert.equal(Object.keys(transport).some((key) => /fallback|generic|python|console/i.test(key)), false);
});

test("fixed animation routes and command names remain compatible with production execution safety", () => {
  const env = { NODE_ENV: "production" };
  let nextCalled = false;
  createProductionExecutionGuard({ env })(
    {
      method: "POST",
      path: "/api/vista/imports/mmg_040/animation/start",
      body: { program_id: "fixed-program", confirm: true },
    },
    {
      status() { throw new Error("fixed route should not be denied"); },
      json() { throw new Error("fixed route should not be denied"); },
    },
    () => { nextCalled = true; },
  );
  assert.equal(nextCalled, true);
  for (const profile of Object.values(require("../vista-animation-dedicated-transport").COMMAND_PROFILES)) {
    assert.equal(productionMcpToolAllowed(profile.commandType, env), true);
    assert.doesNotMatch(profile.commandType, /execute_python_script|console|fallback/);
  }
  assert.equal(productionMcpToolAllowed("execute_python_script", env), false);
});

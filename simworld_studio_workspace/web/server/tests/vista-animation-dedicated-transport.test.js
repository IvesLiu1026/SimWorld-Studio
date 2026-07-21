"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const { createProductionExecutionGuard, productionMcpToolAllowed } = require("../production-execution-policy");
const { UeMcpBroker } = require("../unreal-bridge");
const { createVistaSlotBrokerResolver } = require("../vista-scene-executor-runtime");
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
    JSON.stringify({ operation: "preflight" }),
    { mutation: false, maxAttempts: 2, timeoutMs: 15_000, queueDeadlineMs: 30_000 },
  );
  const contentMutation = await transport.invokeAnimationContentApi(
    JSON.stringify({ operation: "start" }),
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

test("mutation and restricted read retry/timeout policies fail closed before dispatch", async () => {
  const broker = createRecordingBroker();
  const { resolve } = createActiveResolver(broker);
  const transport = await resolve(IDENTITY);
  const cases = [
    () => transport.invokeAnimationContentApi("{}", { mutation: true, maxAttempts: 2 }),
    () => transport.invokeAnimationContentApi("{}", { mutation: false, maxAttempts: 3 }),
    () => transport.probeAnimationContentApi("{}", { mutation: false, maxAttempts: 2 }),
    () => transport.captureAnimationEvidence("{}", { mutation: true, maxAttempts: 1 }),
    () => transport.sampleAnimationEngineTime("{}", { mutation: false, maxAttempts: 1, timeoutMs: 10_001 }),
    () => transport.probeAnimationContentApi("{}", { mutation: false, maxAttempts: 1, queueDeadlineMs: 10_001 }),
  ];
  for (const invoke of cases) {
    await assert.rejects(invoke, (error) => error.code === "ANIMATION_UE_TRANSPORT_OPTIONS_INVALID");
  }
  assert.equal(broker.calls.length, 0);

  const controller = new AbortController();
  await transport.invokeAnimationContentApi("{}", {
    mutation: false,
    maxAttempts: 1,
    signal: controller.signal,
  });
  assert.equal(broker.calls[0].options.signal, controller.signal);
  assert.deepEqual(Object.keys(broker.calls[0].options).sort(), [
    "maxAttempts",
    "queueDeadlineMs",
    "signal",
    "timeoutMs",
  ]);
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
    mutationTransport.invokeAnimationContentApi("{}", { mutation: true, maxAttempts: 1 }),
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
    readTransport.invokeAnimationContentApi("{}", { mutation: false, maxAttempts: 2 }),
    (error) => error.code === "ANIMATION_UE_TRANSPORT_UNAVAILABLE" && error.retryable === true,
  );
  assert.equal(readAttempts, 2);
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
    transport.invokeAnimationContentApi(JSON.stringify({ script: secret }), {
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

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");

const {
  createLeaseBoundReviewBroker,
  createReviewLoopCoordinator,
  createReviewScopeResolver,
  createScopedReviewModeStore,
} = require("../review-loop-coordinator");
const { ReviewRunRegistry } = require("../review-run-registry");
const { handleSceneLoop } = require("../scene-loop");
const { handleVisualSceneLoop } = require("../scene-loop-visual");
const { createVistaSlotBrokerResolver } = require("../vista-scene-executor-runtime");

const TOKEN = "coordinator-test-access-token-0123456789abcdef";

function parseEvents(raw) {
  const events = [];
  for (const frame of String(raw || "").split(/\r?\n\r?\n/)) {
    if (!frame.trim() || frame.trimStart().startsWith(":")) continue;
    let name = null;
    const data = [];
    for (const line of frame.split(/\r?\n/)) {
      if (line.startsWith("event:")) name = line.slice(6).trim();
      if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
    }
    if (name) events.push({ name, data: JSON.parse(data.join("\n")) });
  }
  return events;
}

function identity(request) {
  const principal = String(request.headers["x-test-principal"] || "");
  if (!/^[ab]$/.test(principal)) return null;
  return {
    ownerId: `owner-${principal}`,
    sessionId: `session-${principal.repeat(64)}`,
    slotId: principal === "a" ? 1 : 2,
    leaseId: `lease-${principal}`,
    mcpPort: principal === "a" ? 55561 : 55563,
  };
}

function isActiveIdentity(binding, revoked = new Set()) {
  if (!binding || revoked.has(binding.leaseId)) return false;
  const principal = String(binding.ownerId || "").replace(/^owner-/, "");
  if (!/^[ab]$/.test(principal)) return false;
  const expected = identity({ headers: { "x-test-principal": principal } });
  return Object.keys(expected).every((key) => binding[key] === expected[key]);
}

function readJsonRequest(request) {
  return new Promise((resolve, reject) => {
    let raw = "";
    request.setEncoding("utf8");
    request.on("data", (chunk) => {
      raw += chunk;
      if (raw.length > 1024 * 1024) reject(new Error("request too large"));
    });
    request.on("error", reject);
    request.on("end", () => {
      try { resolve(raw ? JSON.parse(raw) : {}); } catch (error) { reject(error); }
    });
  });
}

function expressResponse(response) {
  response.status = function status(code) { response.statusCode = code; return response; };
  response.json = function json(value) {
    response.setHeader("Content-Type", "application/json");
    response.end(JSON.stringify(value));
    return response;
  };
  response.flushHeaders = response.flushHeaders.bind(response);
  return response;
}

function scenarioFromMessage(message) {
  return String(message || "").match(/\[scenario:([a-z0-9_-]+)\]/i)?.[1] || "success";
}

function builderCost(message) {
  const value = Number(String(message || "").match(/\[builder-cost:([0-9.]+)\]/i)?.[1] || "0.10");
  return value;
}

async function createHarness(t, options = {}) {
  const registry = new ReviewRunRegistry();
  const counters = { critic: 0, capture: 0 };
  const builderBodies = [];
  const processKeys = new Set();
  const revokedLeases = new Set();
  let port = 0;
  const coordinator = createReviewLoopCoordinator({
    registry,
    transportProfile: "trusted_proxy",
    resolveActiveSession(request) {
      const binding = identity(request);
      return isActiveIdentity(binding, revokedLeases) ? binding : null;
    },
    isActiveSessionBinding: (binding) => isActiveIdentity(binding, revokedLeases),
    loopbackSessionId: "loopback-test",
    textHandler: handleSceneLoop,
    visualHandler: handleVisualSceneLoop,
    handlerDependencies: () => ({
      STUDIO_SESSION: "public-studio-test",
      accessToken: TOKEN,
      internalPort: port,
      internalChatTimeoutMs: 40,
      intentStore: new Map(),
      updateIntentSummary: options.updateIntentSummary || (async ({ newPrompt }) => newPrompt),
      env: {
        SCENE_LOOP_MAX_ROUNDS: "1",
        REVIEW_RUN_MAX_BUDGET_USD: "1.00",
        CRITIC_MAX_BUDGET_USD: "0.50",
        CLAUDE_MODEL: "claude-opus-4-8",
      },
      ueBroker: { send: async () => ({}) },
      captureRunner: async () => {
        counters.capture += 1;
        return [{ name: "current", path: "/managed/fake-review-evidence.png" }];
      },
      criticRunner: async (options) => {
        counters.critic += 1;
        return {
          status: "PASS",
          issues: [],
          suggestions: [],
          screenshots: options.screenshots,
          provider: "claude",
          model: "claude-opus-4-8",
          evidence_ids: options.screenshots ? ["visual:fake"] : ["text:fake"],
          usage: { input_tokens: 10, output_tokens: 4 },
          cost_usd: 0.05,
          latency_ms: 3,
        };
      },
    }),
  });

  function fakeBuilder(request, response) {
    if (request.headers.authorization !== `Bearer ${TOKEN}`) {
      response.statusCode = 401;
      response.setHeader("Content-Type", "application/json");
      return response.end(JSON.stringify({ error: "unauthorized" }));
    }
    builderBodies.push({ ...request.body });
    processKeys.add(request.body.sessionId);
    const scenario = scenarioFromMessage(request.body.message);
    if (/^(401|429|500)$/.test(scenario)) {
      response.statusCode = Number(scenario);
      response.setHeader("Content-Type", "application/json");
      return response.end(JSON.stringify({ error: "fake provider failure" }));
    }
    if (scenario === "timeout" || scenario === "hang") return undefined;
    response.statusCode = 200;
    response.setHeader("Content-Type", "text/event-stream");
    if (scenario === "malformed") {
      return response.end("event: done\ndata: not-json\n\n");
    }
    response.write(`event: system\ndata: ${JSON.stringify({ sessionId: request.body.sessionId })}\n\n`);
    response.end(`event: done\ndata: ${JSON.stringify({
      isError: false,
      latestScreenshot: null,
      costUsd: builderCost(request.body.message),
    })}\n\n`);
    return undefined;
  }

  const server = http.createServer(async (request, nativeResponse) => {
    const response = expressResponse(nativeResponse);
    const parsed = new URL(request.url, "http://coordinator.test");
    request.path = parsed.pathname;
    request.query = Object.fromEntries(parsed.searchParams);
    try { request.body = await readJsonRequest(request); }
    catch (_error) { return response.status(400).json({ error: "invalid json" }); }

    if (request.method === "POST" && parsed.pathname === "/api/chat-stop") {
      try {
        const cancellation = coordinator.cancel(request);
        const requestedRunId = request.body && request.body.runId;
        const subprocessStopped = (!requestedRunId || cancellation.record)
          ? processKeys.delete(cancellation.scope.scopeId)
          : false;
        return response.json({
          stopped: Boolean(cancellation.record) || subprocessStopped,
          runId: cancellation.record && cancellation.record.runId,
          subprocessStopped,
        });
      } catch (error) {
        return response.status(error.statusCode || 400).json({ code: error.code, error: error.message });
      }
    }
    if (request.method === "POST" && parsed.pathname === "/api/chat") {
      return coordinator.handleChat(request, response, () => (
        coordinator.bindVanillaChat(request, response, () => fakeBuilder(request, response))
      ));
    }
    return response.status(404).json({ error: "not found" });
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      server.removeListener("error", reject);
      port = server.address().port;
      resolve();
    });
  });
  t.after(async () => {
    if (typeof server.closeAllConnections === "function") server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  });

  function post(pathname, body, principal = "a") {
    return new Promise((resolve, reject) => {
      const payload = Buffer.from(JSON.stringify(body));
      const request = http.request({
        host: "127.0.0.1",
        port,
        path: pathname,
        method: "POST",
        headers: {
          Authorization: `Bearer ${TOKEN}`,
          "Content-Type": "application/json",
          "Content-Length": String(payload.length),
          "x-test-principal": principal,
        },
      }, (response) => {
        let raw = "";
        response.setEncoding("utf8");
        response.on("data", (chunk) => { raw += chunk; });
        response.on("end", () => resolve({ statusCode: response.statusCode, raw }));
      });
      request.on("error", reject);
      request.end(payload);
    });
  }

  return {
    builderBodies,
    coordinator,
    counters,
    post,
    processKeys,
    registry,
    revoke(principal) { revokedLeases.add(`lease-${principal}`); },
  };
}

function reviewRequest(mode, scenario, overrides = {}) {
  return {
    message: `[scenario:${scenario}] build a deterministic fixture`,
    sessionId: "untrusted-client-session",
    conversationId: "shared-conversation",
    loopMode: mode,
    agent: "claude",
    model: "claude-opus-4-8",
    reviewBudgetUsd: 0.5,
    ...overrides,
  };
}

async function waitFor(predicate, timeoutMs = 1000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  throw new Error("condition was not reached");
}

test("full HTTP coordinator completes fake Text and Visual Review with isolated cost", async (t) => {
  const harness = await createHarness(t);
  const [textReply, visualReply] = await Promise.all([
    harness.post("/api/chat", reviewRequest("text_loop", "success", {
      conversationId: "text-conversation",
      message: "[scenario:success] [builder-cost:0.11] text fixture",
    }), "a"),
    harness.post("/api/chat", reviewRequest("visual_loop", "success", {
      conversationId: "visual-conversation",
      message: "[scenario:success] [builder-cost:0.21] visual fixture",
    }), "b"),
  ]);
  for (const reply of [textReply, visualReply]) assert.equal(reply.statusCode, 200);
  const textDone = parseEvents(textReply.raw).find((event) => event.name === "done").data;
  const visualDone = parseEvents(visualReply.raw).find((event) => event.name === "done").data;
  assert.equal(textDone.isError, false);
  assert.equal(visualDone.isError, false);
  assert.equal(textDone.budget.spent_usd, 0.16);
  assert.equal(visualDone.budget.spent_usd, 0.26);
  assert.equal(textDone.review.criticModel, "claude-opus-4-8");
  assert.equal(visualDone.loop.mode, "visual_loop");
  assert.equal(harness.counters.critic, 2);
  assert.equal(harness.counters.capture, 1);
  assert.equal(harness.registry.size, 0);
});

test("fake inner-provider failure matrix fails closed in both Review modes", async (t) => {
  const harness = await createHarness(t);
  for (const mode of ["text_loop", "visual_loop"]) {
    for (const scenario of ["401", "429", "500", "timeout", "malformed"]) {
      const reply = await harness.post("/api/chat", reviewRequest(mode, scenario, {
        conversationId: `${mode}-${scenario}`,
      }));
      assert.equal(reply.statusCode, 200, `${mode}/${scenario}`);
      const events = parseEvents(reply.raw);
      const loopDone = events.find((event) => event.name === "loop_done").data;
      const done = events.find((event) => event.name === "done").data;
      assert.equal(loopDone.reason, "builder_error", `${mode}/${scenario}`);
      assert.equal(done.isError, true, `${mode}/${scenario}`);
      assert.notEqual(loopDone.finalStatus, "PASS", `${mode}/${scenario}`);
    }
  }
  assert.equal(harness.counters.critic, 0);
  assert.equal(harness.counters.capture, 0);
  assert.equal(harness.registry.size, 0);
});

test("lease-derived scopes prevent cross-session cancellation", async (t) => {
  const harness = await createHarness(t);
  const runA = harness.post("/api/chat", reviewRequest("text_loop", "hang", { runId: "run-a" }), "a");
  const runB = harness.post("/api/chat", reviewRequest("text_loop", "hang", { runId: "run-b" }), "b");
  await waitFor(() => harness.registry.size === 2);

  const wrongSession = await harness.post("/api/chat-stop", {
    conversationId: "shared-conversation",
    runId: "run-a",
  }, "b");
  assert.deepEqual(JSON.parse(wrongSession.raw), {
    stopped: false,
    runId: null,
    subprocessStopped: false,
  });
  assert.equal(harness.registry.size, 2);

  const stopA = await harness.post("/api/chat-stop", {
    conversationId: "shared-conversation",
    runId: "run-a",
  }, "a");
  assert.equal(JSON.parse(stopA.raw).stopped, true);
  const replyA = await runA;
  assert.equal(parseEvents(replyA.raw).at(-1).data.loop.reason, "cancelled");
  await waitFor(() => harness.registry.size === 1);
  assert.equal(harness.registry.get({ runId: "run-b" }).signal.aborted, false);

  await harness.post("/api/chat-stop", {
    conversationId: "shared-conversation",
    runId: "run-b",
  }, "b");
  const replyB = await runB;
  assert.equal(parseEvents(replyB.raw).at(-1).data.loop.reason, "cancelled");
  await waitFor(() => harness.registry.size === 0);
});

test("trusted vanilla child keys ignore caller sessionId and only the owning lease can stop them", async (t) => {
  const harness = await createHarness(t);
  const request = {
    message: "[scenario:success] vanilla fixture",
    sessionId: "shared-caller-controlled-id",
    conversationId: "vanilla-conversation",
    loopMode: "vanilla",
  };
  const reply = await harness.post("/api/chat", request, "a");
  assert.equal(reply.statusCode, 200);
  const bound = harness.builderBodies.at(-1);
  assert.match(bound.sessionId, /^review-[a-f0-9]{64}$/);
  assert.equal(bound.sessionId, bound.conversationId);
  assert.notEqual(bound.sessionId, request.sessionId);
  assert.equal(harness.processKeys.has(bound.sessionId), true);

  const crossLease = await harness.post("/api/chat-stop", {
    sessionId: bound.sessionId,
    conversationId: "vanilla-conversation",
  }, "b");
  assert.equal(JSON.parse(crossLease.raw).stopped, false);
  assert.equal(harness.processKeys.has(bound.sessionId), true);

  const ownLease = await harness.post("/api/chat-stop", {
    sessionId: "another-caller-value",
    conversationId: "vanilla-conversation",
  }, "a");
  assert.equal(JSON.parse(ownLease.raw).subprocessStopped, true);
  assert.equal(harness.processKeys.has(bound.sessionId), false);
});

test("trusted vanilla and active Review inner requests fail closed after lease revocation", async (t) => {
  let releaseIntent;
  let markIntentStarted;
  const intentStarted = new Promise((resolve) => { markIntentStarted = resolve; });
  const intentGate = new Promise((resolve) => { releaseIntent = resolve; });
  const harness = await createHarness(t, {
    async updateIntentSummary({ newPrompt }) {
      markIntentStarted();
      await intentGate;
      return newPrompt;
    },
  });
  const activeReview = harness.post("/api/chat", reviewRequest("text_loop", "success", {
    conversationId: "revoked-inner-conversation",
  }), "a");
  await intentStarted;
  harness.revoke("a");
  releaseIntent();
  const reviewReply = await activeReview;
  assert.equal(reviewReply.statusCode, 200);
  const reviewEvents = parseEvents(reviewReply.raw);
  assert.equal(reviewEvents.find((event) => event.name === "loop_done").data.reason, "builder_error");
  assert.equal(reviewEvents.find((event) => event.name === "done").data.isError, true);
  assert.equal(harness.builderBodies.length, 0);

  const denied = await harness.post("/api/chat", {
    message: "vanilla fixture",
    sessionId: "caller-value",
    conversationId: "revoked-conversation",
    loopMode: "vanilla",
  }, "a");
  assert.equal(denied.statusCode, 401);
  assert.equal(JSON.parse(denied.raw).code, "REVIEW_ACTIVE_SESSION_REQUIRED");
  assert.equal(harness.builderBodies.length, 0);
});

test("scoped Review modes isolate trusted leases and preserve loopback global compatibility", () => {
  const resolve = createReviewScopeResolver({
    transportProfile: "trusted_proxy",
    resolveActiveSession: identity,
    loopbackSessionId: "unused",
  });
  const scopeA = resolve({
    headers: { "x-test-principal": "a" },
    body: { conversationId: "shared-conversation" },
    query: {},
  });
  const scopeB = resolve({
    headers: { "x-test-principal": "b" },
    body: { conversationId: "shared-conversation" },
    query: {},
  });
  const trusted = createScopedReviewModeStore({
    transportProfile: "public_webrtc",
    defaultMode: "vanilla",
  });
  assert.equal(trusted.get(scopeA), "vanilla");
  assert.equal(trusted.get(scopeB), "vanilla");
  trusted.set(scopeA, "text_loop");
  trusted.set(scopeB, "visual_loop");
  assert.equal(trusted.get(scopeA), "text_loop");
  assert.equal(trusted.get(scopeB), "visual_loop");
  trusted.delete(scopeA);
  assert.equal(trusted.get(scopeA), "vanilla");
  assert.equal(trusted.get(scopeB), "visual_loop");

  const loopback = createScopedReviewModeStore({
    transportProfile: "loopback",
    defaultMode: "vanilla",
  });
  loopback.set(scopeA, "text_loop");
  assert.equal(loopback.get(scopeB), "text_loop");
  loopback.delete(scopeA);
  assert.equal(loopback.get(scopeB), "text_loop");
});

test("lease-bound Review broker selects two slots and denies cross-slot drift or revoked leases", async () => {
  const valid = [
    identity({ headers: { "x-test-principal": "a" } }),
    identity({ headers: { "x-test-principal": "b" } }),
  ];
  const revoked = new Set();
  const calls = [];
  class FakeBroker {
    constructor({ host, port }) {
      assert.equal(host, "127.0.0.1");
      this.port = port;
    }
    async send(type) {
      calls.push({ port: this.port, type });
      return { port: this.port };
    }
  }
  const defaultBroker = {
    port: valid[0].mcpPort,
    async send(type) {
      calls.push({ port: this.port, type });
      return { port: this.port };
    },
  };
  const resolveUeBroker = createVistaSlotBrokerResolver({
    studioStreaming: {
      isActiveSessionBinding(binding) {
        return valid.some((candidate) => (
          !revoked.has(candidate.leaseId)
          && Object.keys(candidate).every((key) => binding[key] === candidate[key])
        ));
      },
    },
    defaultBroker,
    BrokerClass: FakeBroker,
  });
  const resolveScope = createReviewScopeResolver({
    transportProfile: "trusted_proxy",
    resolveActiveSession: identity,
    loopbackSessionId: "unused",
  });
  const scopeA = resolveScope({
    headers: { "x-test-principal": "a" },
    body: { conversationId: "broker-test" },
    query: {},
  });
  const scopeB = resolveScope({
    headers: { "x-test-principal": "b" },
    body: { conversationId: "broker-test" },
    query: {},
  });
  assert.deepEqual(Object.keys(scopeA).sort(), ["conversationId", "leaseBound", "scopeId"]);
  const brokerA = createLeaseBoundReviewBroker({ scope: scopeA, resolveUeBroker });
  const brokerB = createLeaseBoundReviewBroker({ scope: scopeB, resolveUeBroker });
  assert.deepEqual(await brokerA.send("slot-a"), { port: valid[0].mcpPort });
  assert.deepEqual(await brokerB.send("slot-b"), { port: valid[1].mcpPort });
  assert.equal(resolveUeBroker({ ...valid[0], slotId: valid[1].slotId, mcpPort: valid[1].mcpPort }), null);

  revoked.add(valid[0].leaseId);
  await assert.rejects(
    brokerA.send("must-not-dispatch"),
    (error) => error.code === "REVIEW_BROKER_LEASE_INVALID" && error.statusCode === 409,
  );
  assert.deepEqual(await brokerB.send("slot-b-still-active"), { port: valid[1].mcpPort });
  assert.deepEqual(calls, [
    { port: valid[0].mcpPort, type: "slot-a" },
    { port: valid[1].mcpPort, type: "slot-b" },
    { port: valid[1].mcpPort, type: "slot-b-still-active" },
  ]);
});

test("trusted proxy fails closed without a lease while loopback remains deterministic", async () => {
  const trusted = createReviewLoopCoordinator({
    transportProfile: "trusted_proxy",
    resolveActiveSession: () => null,
    isActiveSessionBinding: () => false,
    loopbackSessionId: "loopback-test",
    textHandler: async () => {},
    visualHandler: async () => {},
  });
  assert.throws(
    () => trusted.resolveScope({ body: { conversationId: "conversation" }, query: {} }),
    (error) => error.code === "REVIEW_ACTIVE_SESSION_REQUIRED" && error.statusCode === 401,
  );

  const leaseBound = createReviewLoopCoordinator({
    transportProfile: "trusted_proxy",
    resolveActiveSession: identity,
    isActiveSessionBinding: (binding) => isActiveIdentity(binding),
    loopbackSessionId: "loopback-test",
    textHandler: async () => {},
    visualHandler: async () => {},
  });
  const leaseRequest = { headers: { "x-test-principal": "a" }, query: {} };
  const untrustedSessionA = leaseBound.resolveScope({
    ...leaseRequest,
    body: { conversationId: "conversation", sessionId: "caller-a" },
  });
  const untrustedSessionB = leaseBound.resolveScope({
    ...leaseRequest,
    body: { conversationId: "conversation", sessionId: "caller-b" },
  });
  assert.equal(untrustedSessionA.leaseBound, true);
  assert.equal(untrustedSessionA.scopeId, untrustedSessionB.scopeId);

  let loopbackResolverCalls = 0;
  const loopback = createReviewLoopCoordinator({
    transportProfile: "loopback",
    resolveActiveSession: () => {
      loopbackResolverCalls += 1;
      return identity({ headers: { "x-test-principal": "a" } });
    },
    loopbackSessionId: "studio-dev",
    textHandler: async () => {},
    visualHandler: async () => {},
  });
  const first = loopback.resolveScope({ body: { conversationId: "conversation" }, query: {} });
  const second = loopback.resolveScope({ body: { conversationId: "conversation" }, query: {} });
  const other = loopback.resolveScope({ body: { conversationId: "other" }, query: {} });
  assert.equal(first.leaseBound, false);
  assert.equal(loopbackResolverCalls, 0);
  assert.equal(first.scopeId, second.scopeId);
  assert.notEqual(first.scopeId, other.scopeId);
});

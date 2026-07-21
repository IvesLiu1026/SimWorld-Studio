"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");

const { createReviewLoopCoordinator } = require("../review-loop-coordinator");
const { ReviewRunRegistry } = require("../review-run-registry");
const { handleSceneLoop } = require("../scene-loop");
const { handleVisualSceneLoop } = require("../scene-loop-visual");

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

async function createHarness(t) {
  const registry = new ReviewRunRegistry();
  const counters = { critic: 0, capture: 0 };
  let port = 0;
  const coordinator = createReviewLoopCoordinator({
    registry,
    transportProfile: "trusted_proxy",
    resolveActiveSession: identity,
    loopbackSessionId: "loopback-test",
    textHandler: handleSceneLoop,
    visualHandler: handleVisualSceneLoop,
    handlerDependencies: () => ({
      STUDIO_SESSION: "public-studio-test",
      accessToken: TOKEN,
      internalPort: port,
      internalChatTimeoutMs: 40,
      intentStore: new Map(),
      updateIntentSummary: async ({ newPrompt }) => newPrompt,
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
        return response.json({
          stopped: Boolean(cancellation.record),
          runId: cancellation.record && cancellation.record.runId,
        });
      } catch (error) {
        return response.status(error.statusCode || 400).json({ code: error.code, error: error.message });
      }
    }
    if (request.method === "POST" && parsed.pathname === "/api/chat") {
      return coordinator.handleChat(request, response, () => fakeBuilder(request, response));
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

  return { coordinator, counters, post, registry };
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
  assert.deepEqual(JSON.parse(wrongSession.raw), { stopped: false, runId: null });
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

test("trusted proxy fails closed without a lease while loopback remains deterministic", async () => {
  const trusted = createReviewLoopCoordinator({
    transportProfile: "trusted_proxy",
    resolveActiveSession: () => null,
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

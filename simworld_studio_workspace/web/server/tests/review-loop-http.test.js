"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");

const {
  requestInternalSse,
  requireStudioAccessToken,
  resolveReviewContract,
} = require("../internal-http");
const { handleSceneLoop, runSceneLoop } = require("../scene-loop");
const { createReviewBudget } = require("../review-budget");
const {
  cleanupEvidenceForPath,
  cleanupPrivateEvidenceRun,
  createPrivateEvidenceRun,
  createReviewEvidenceReadinessProbe,
  reserveEvidenceFile,
  screenshotEvidence,
  sealManagedScreenshot,
} = require("../scene-critic");
const {
  getActorsSnapshot,
  handleVisualSceneLoop,
  multiViewScreenshot,
  runVisualSceneLoop,
  visualCritique,
} = require("../scene-loop-visual");

const TOKEN = "test-studio-access-token-0123456789abcdef";
const PNG_BYTES = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1]);

async function certifyEvidenceRoots(...roots) {
  const report = await createReviewEvidenceReadinessProbe({
    roots,
    deadlineMs: 1_000,
  })();
  assert.equal(report.status, "ready", JSON.stringify(report));
}

function listen(server) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      server.removeListener("error", reject);
      resolve(server.address().port);
    });
  });
}

async function testServer(t, handler) {
  const server = http.createServer(handler);
  const port = await listen(server);
  t.after(() => new Promise((resolve) => server.close(resolve)));
  return port;
}

function parseEvents(raw) {
  const events = [];
  for (const frame of String(raw || "").split(/\r?\n\r?\n/)) {
    if (!frame.trim() || frame.trimStart().startsWith(":")) continue;
    let name = null;
    const dataLines = [];
    for (const line of frame.split(/\r?\n/)) {
      if (line.startsWith("event:")) name = line.slice(6).trim();
      if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
    }
    if (name) events.push({ name, data: JSON.parse(dataLines.join("\n")) });
  }
  return events;
}

function mockResponse() {
  return {
    headers: {},
    statusCode: 200,
    chunks: [],
    writableEnded: false,
    setHeader(name, value) { this.headers[String(name).toLowerCase()] = value; },
    flushHeaders() {},
    write(chunk) { this.chunks.push(String(chunk)); return true; },
    end(chunk) {
      if (chunk) this.chunks.push(String(chunk));
      this.writableEnded = true;
    },
    status(code) { this.statusCode = code; return this; },
    json(value) { this.jsonBody = value; this.end(JSON.stringify(value)); },
    text() { return this.chunks.join(""); },
  };
}

test("internal SSE client authenticates, parses CRLF frames, and requires done", async (t) => {
  let requestBody = null;
  let authorization = null;
  const port = await testServer(t, (req, res) => {
    authorization = req.headers.authorization;
    let body = "";
    req.setEncoding("utf8");
    req.on("data", (chunk) => { body += chunk; });
    req.on("end", () => {
      requestBody = JSON.parse(body);
      res.writeHead(200, { "Content-Type": "text/event-stream; charset=utf-8" });
      res.write("event: text\r\ndata: {\"delta\":\"hello\"}\r\n\r\n");
      res.end("event: done\r\ndata: {\"isError\":false,\"latestScreenshot\":\"/shot.png\"}\r\n\r\n");
    });
  });
  const relayed = [];
  const result = await requestInternalSse({
    port,
    body: { message: "build" },
    token: TOKEN,
    timeoutMs: 1000,
    onEvent: (name, data) => relayed.push({ name, data }),
  });
  assert.equal(authorization, `Bearer ${TOKEN}`);
  assert.deepEqual(requestBody, { message: "build" });
  assert.deepEqual(relayed, [{ name: "text", data: { delta: "hello" } }]);
  assert.deepEqual(result.done, { isError: false, latestScreenshot: "/shot.png" });
});

test("internal SSE client rejects every non-2xx class", async (t) => {
  for (const status of [401, 403, 429, 500]) {
    await t.test(String(status), async (st) => {
      const port = await testServer(st, (_req, res) => {
        res.writeHead(status, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: "denied" }));
      });
      await assert.rejects(
        requestInternalSse({ port, body: {}, token: TOKEN, timeoutMs: 1000 }),
        (error) => error.code === "INTERNAL_HTTP_STATUS" && error.statusCode === status,
      );
    });
  }
});

test("internal SSE client rejects timeout and malformed protocol responses", async (t) => {
  await t.test("timeout", async (st) => {
    const port = await testServer(st, (_req, _res) => {});
    await assert.rejects(
      requestInternalSse({ port, body: {}, token: TOKEN, timeoutMs: 40 }),
      (error) => error.code === "INTERNAL_HTTP_TIMEOUT",
    );
  });

  const cases = [
    {
      name: "wrong content type",
      contentType: "application/json",
      response: JSON.stringify({ isError: false }),
      code: "INTERNAL_HTTP_CONTENT_TYPE",
    },
    {
      name: "invalid event JSON",
      contentType: "text/event-stream",
      response: "event: done\ndata: not-json\n\n",
      code: "INTERNAL_SSE_MALFORMED",
    },
    {
      name: "missing done",
      contentType: "text/event-stream",
      response: "event: text\ndata: {\"delta\":\"x\"}\n\n",
      code: "INTERNAL_SSE_MISSING_DONE",
    },
    {
      name: "partial terminal frame",
      contentType: "text/event-stream",
      response: "event: done\ndata: {\"isError\":false}",
      code: "INTERNAL_SSE_MALFORMED",
    },
    {
      name: "done without isError",
      contentType: "text/event-stream",
      response: "event: done\ndata: {}\n\n",
      code: "INTERNAL_SSE_MALFORMED",
    },
  ];
  for (const fixture of cases) {
    await t.test(fixture.name, async (st) => {
      const port = await testServer(st, (_req, res) => {
        res.writeHead(200, { "Content-Type": fixture.contentType });
        res.end(fixture.response);
      });
      await assert.rejects(
        requestInternalSse({ port, body: {}, token: TOKEN, timeoutMs: 1000 }),
        (error) => error.code === fixture.code,
      );
    });
  }
});

test("review contract keeps builder agent/model while defaulting critic safely", () => {
  const claude = resolveReviewContract(
    { agent: "claude", model: "claude-opus-4-8" },
    {},
  );
  assert.equal(claude.builderAgent, "claude");
  assert.equal(claude.builderModel, "claude-opus-4-8");
  assert.equal(claude.criticProvider, "claude");
  assert.equal(claude.criticModel, "claude-opus-4-8");

  const codexBuilder = resolveReviewContract(
    { agent: "codex", model: "gpt-5" },
    { CLAUDE_MODEL: "claude-opus-4-8" },
  );
  assert.equal(codexBuilder.builderAgent, "codex");
  assert.equal(codexBuilder.builderModel, "gpt-5");
  assert.equal(codexBuilder.criticProvider, "claude");
  assert.equal(codexBuilder.criticModel, "claude-opus-4-8");

  assert.throws(
    () => resolveReviewContract(
      { agent: "codex", model: "gpt-5", criticProvider: "codex", criticModel: "gpt-5" },
      {},
    ),
    (error) => error.code === "REVIEW_PROVIDER_UNSAFE",
  );
  assert.throws(
    () => resolveReviewContract(
      { agent: "claude", model: "claude-opus-4-8", criticModel: "opus" },
      {},
    ),
    (error) => error.code === "REVIEW_MODEL_INVALID",
  );
  assert.throws(
    () => resolveReviewContract({ agent: "claude", runner: "codex" }, {}),
    (error) => error.code === "INVALID_REVIEW_CONTRACT",
  );
  assert.throws(
    () => requireStudioAccessToken({}),
    (error) => error.code === "INTERNAL_AUTH_UNAVAILABLE",
  );
});

test("text loop turns inner HTTP 401 into builder_error and never calls critic", async (t) => {
  let criticCalls = 0;
  const port = await testServer(t, (_req, res) => {
    res.writeHead(401, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: "auth" }));
  });
  const res = mockResponse();
  await handleSceneLoop({
    body: {
      message: "build a scene",
      sessionId: "studio-1",
      conversationId: "conversation-1",
      agent: "claude",
      model: "claude-opus-4-8",
    },
  }, res, {
    STUDIO_SESSION: "studio-default",
    runId: "run-401",
    accessToken: TOKEN,
    internalPort: port,
    internalChatTimeoutMs: 1000,
    env: { SCENE_LOOP_MAX_ROUNDS: "1" },
    intentStore: new Map(),
    updateIntentSummary: async () => "build a scene",
    criticRunner: async () => { criticCalls += 1; return { status: "PASS", issues: [], suggestions: [] }; },
  });
  const events = parseEvents(res.text());
  assert.equal(events[0].name, "run_start");
  assert.deepEqual(events[0].data, {
    runId: "run-401",
    sessionId: "studio-1",
    conversationId: "conversation-1",
    mode: "text_loop",
  });
  assert.equal(events.find((event) => event.name === "builder_done").data.isError, true);
  const loopDone = events.find((event) => event.name === "loop_done").data;
  assert.equal(loopDone.reason, "builder_error");
  assert.equal(loopDone.failureReason, "builder_error");
  assert.equal(loopDone.error.code, "INTERNAL_HTTP_STATUS");
  const done = events.find((event) => event.name === "done").data;
  assert.equal(done.isError, true);
  assert.equal(done.loop.reason, "builder_error");
  assert.equal(done.runId, "run-401");
  assert.equal(criticCalls, 0);
});

test("text loop propagates canonical builder and correlation contract", async (t) => {
  let received = null;
  const port = await testServer(t, (req, res) => {
    let raw = "";
    req.setEncoding("utf8");
    req.on("data", (chunk) => { raw += chunk; });
    req.on("end", () => {
      received = { body: JSON.parse(raw), authorization: req.headers.authorization };
      res.writeHead(200, { "Content-Type": "text/event-stream" });
      res.end("event: done\ndata: {\"isError\":false,\"costUsd\":0.1}\n\n");
    });
  });
  const intentStore = new Map();
  const injectedUeBroker = { send: async () => ({}) };
  const res = mockResponse();
  await handleSceneLoop({
    body: {
      message: "build",
      sessionId: "studio-2",
      conversationId: "conversation-2",
      agent: "codex",
      model: "gpt-5",
      requireRealAssets: false,
      assetDegradedMode: "basic_geometry",
    },
  }, res, {
    STUDIO_SESSION: "studio-default",
    runId: "run-contract",
    accessToken: TOKEN,
    internalPort: port,
    internalChatTimeoutMs: 1000,
    env: { SCENE_LOOP_MAX_ROUNDS: "1", CLAUDE_MODEL: "claude-opus-4-8" },
    intentStore,
    updateIntentSummary: async () => "intent",
    ueBroker: injectedUeBroker,
    criticRunner: async (options) => {
      assert.equal(options.ueBroker, injectedUeBroker);
      assert.equal(options.maxBudgetUsd, 0.5);
      return { status: "PASS", issues: [], suggestions: [], cost_usd: 0.05 };
    },
  });
  assert.equal(received.authorization, `Bearer ${TOKEN}`);
  assert.equal(received.body.agent, "codex");
  assert.equal(received.body.model, "gpt-5");
  assert.equal(received.body.sessionId, "studio-2");
  assert.equal(received.body.conversationId, "conversation-2");
  assert.equal(received.body.runId, "run-contract");
  assert.equal(received.body.useLoop, false);
  assert.equal(received.body.maxBudgetUsd, 2);
  assert.equal(received.body.requireRealAssets, false);
  assert.equal(received.body.assetDegradedMode, "basic_geometry");
  assert.equal(Object.hasOwn(received.body, "runner"), false);
  assert.equal(intentStore.has("conversation-2"), true);
  const done = parseEvents(res.text()).find((event) => event.name === "done").data;
  assert.equal(done.isError, false);
  assert.equal(done.review.builderAgent, "codex");
  assert.equal(done.review.criticProvider, "claude");
  assert.equal(done.budget.spent_usd, 0.15);
  assert.equal(done.budget.remaining_usd, 1.85);
  const criticVerdict = parseEvents(res.text()).find((event) => event.name === "critic_verdict").data;
  assert.equal(criticVerdict.provider, "claude");
  assert.equal(criticVerdict.model, "claude-opus-4-8");
  assert.deepEqual(criticVerdict.evidence_ids, []);
  assert.equal(criticVerdict.usage, null);
  assert.equal(criticVerdict.latency_ms, null);
  assert.equal(criticVerdict.cost_usd, 0.05);
  assert.equal(criticVerdict.budget.spent_usd, 0.15);
});

test("Text Review relays only allowlisted inner fields and never exposes builder host paths", async () => {
  const res = mockResponse();
  await handleSceneLoop({
    body: {
      message: "build",
      sessionId: "studio-public-text",
      conversationId: "conversation-public-text",
      agent: "claude",
    },
  }, res, {
    STUDIO_SESSION: "studio-default",
    runId: "run-public-text",
    accessToken: TOKEN,
    env: { SCENE_LOOP_MAX_ROUNDS: "1" },
    intentStore: new Map(),
    updateIntentSummary: async () => "intent",
    requestInternalSse: async ({ onEvent }) => {
      onEvent("text", {
        delta: "working from /data/secret, /workspace/x, /a/b/c, "
          + "`/home/yhliu/secret.txt`, path:/srv/private, //home/yhliu/double.txt, "
          + "///srv/private/triple.txt, https://example.com/review "
          + "https://[::1]/review https://例え.テスト/review https://example.com./review "
          + "and %2Fcustom%2Fencoded.png",
      });
      onEvent("tool_input", { id: "tool-1", delta: '{"path":"/scratch/private"}' });
      onEvent("screenshot", {
        toolUseId: "shot-1",
        filepath: "/api/screenshot/file?path=%2Fcustom%2Fsecret.png",
      });
      onEvent("tool_details", {
        id: "tool-1",
        name: "mcp__simworld__take_screenshot",
        displayName: "take_screenshot",
        input: { filepath: "/workspace/private-input.png" },
      });
      onEvent("tool_result", {
        toolUseId: "tool-1",
        result: '{"filepath":"/data/private-result.png","status":"success"}',
        isError: false,
        privileged: { path: "/root/private" },
      });
      onEvent("unreviewed_future_event", { path: "/var/private" });
      return {
        done: {
          isError: false,
          costUsd: 0,
          latestScreenshot: "/api/screenshot/file?path=%2Fcustom%2Fsecret.png",
          filepath: "/home/yhliu/private.scene",
        },
      };
    },
    criticRunner: async () => ({
      status: "PASS",
      issues: [],
      suggestions: [],
      cost_usd: 0,
    }),
  });

  const raw = res.text();
  const events = parseEvents(raw);
  assert.doesNotMatch(
    raw,
    /\/data\/secret|\/workspace\/x|\/a\/b\/c|\/home\/yhliu|\/srv\/private|\/root\/private|\/var\/private/,
  );
  assert.doesNotMatch(raw, /%2Fcustom%2Fsecret/i);
  assert.doesNotMatch(raw, /screenshot\/file\?path=/);
  assert.doesNotMatch(raw, /filepath|\/scratch\/private/);
  assert.match(raw, /https:\/\/example\.com\/review/);
  assert.match(raw, /https:\/\/\[::1\]\/review/);
  assert.match(raw, /https:\/\/例え\.テスト\/review/);
  assert.match(raw, /https:\/\/example\.com\.\/review/);
  assert.equal(events.some((event) => event.name === "screenshot"), false);
  assert.equal(events.some((event) => event.name === "unreviewed_future_event"), false);
  const details = events.find((event) => event.name === "tool_details").data;
  assert.deepEqual(details, {
    id: "tool-1",
    name: "mcp__simworld__take_screenshot",
    displayName: "take_screenshot",
  });
  assert.equal(events.some((event) => event.name === "tool_input"), false);
  const toolResult = events.find((event) => event.name === "tool_result").data;
  assert.deepEqual(toolResult, { toolUseId: "tool-1", isError: false });
  const loopDone = events.find((event) => event.name === "loop_done").data;
  assert.deepEqual(loopDone.builderResult, { isError: false, retryable: false, costUsd: 0 });
});

test("aggregate text-loop budget stops before another mutating round", async () => {
  const emitted = [];
  const builderCaps = [];
  const criticCaps = [];
  let builderCalls = 0;
  let criticCalls = 0;
  const reviewBudget = createReviewBudget({ limitUsd: 0.5, minimumStageUsd: 0.01 }, {});
  const result = await runSceneLoop({
    prompt: "build",
    intentSummary: "build",
    maxRounds: 5,
    reviewBudget,
    criticMaxBudgetUsd: 0.5,
    builderRunner: async ({ maxBudgetUsd }) => {
      builderCalls += 1;
      builderCaps.push(maxBudgetUsd);
      return { isError: false, costUsd: 0.3 };
    },
    criticRunner: async ({ maxBudgetUsd }) => {
      criticCalls += 1;
      criticCaps.push(maxBudgetUsd);
      return {
        status: "NEEDS_IMPROVEMENT",
        issues: ["adjust placement"],
        suggestions: ["move the chair"],
        cost_usd: 0.2,
      };
    },
    emit: (name, data) => emitted.push({ name, data }),
  });

  assert.equal(builderCalls, 1);
  assert.equal(criticCalls, 1);
  assert.deepEqual(builderCaps, [0.5]);
  assert.deepEqual(criticCaps, [0.2]);
  assert.equal(result.reason, "budget_exhausted");
  assert.equal(result.rounds, 1);
  assert.equal(result.failureReason, "budget_exhausted");
  assert.equal(result.error.code, "REVIEW_BUDGET_EXHAUSTED");
  assert.equal(result.budget.spent_usd, 0.5);
  assert.equal(result.budget.remaining_usd, 0);
  assert.equal(emitted.filter((event) => event.name === "round_start").length, 1);
  assert.equal(emitted.find((event) => event.name === "builder_done").data.budget.spent_usd, 0.3);
  assert.equal(emitted.find((event) => event.name === "critic_verdict").data.budget.spent_usd, 0.5);
});

test("parallel text-loop runs keep aggregate budgets isolated", async () => {
  const run = (limitUsd, builderCost, criticCost) => runSceneLoop({
    prompt: "build",
    intentSummary: "build",
    maxRounds: 1,
    reviewBudget: createReviewBudget({ limitUsd, minimumStageUsd: 0.01 }, {}),
    criticMaxBudgetUsd: 0.5,
    builderRunner: async () => ({ isError: false, costUsd: builderCost }),
    criticRunner: async () => ({
      status: "PASS",
      issues: [],
      suggestions: [],
      cost_usd: criticCost,
    }),
  });
  const [left, right] = await Promise.all([
    run(0.4, 0.1, 0.05),
    run(0.8, 0.2, 0.1),
  ]);
  assert.deepEqual(
    [left.budget.limit_usd, left.budget.spent_usd, left.budget.remaining_usd],
    [0.4, 0.15, 0.25],
  );
  assert.deepEqual(
    [right.budget.limit_usd, right.budget.spent_usd, right.budget.remaining_usd],
    [0.8, 0.3, 0.5],
  );
});

test("aggregate visual-loop budget includes capture-free stage caps and stops cleanly", async () => {
  let builderCalls = 0;
  let captureCalls = 0;
  let criticCalls = 0;
  let receivedCriticCap = null;
  const result = await runVisualSceneLoop({
    prompt: "build",
    intentSummary: "build",
    maxRounds: 4,
    reviewBudget: createReviewBudget({ limitUsd: 0.25, minimumStageUsd: 0.01 }, {}),
    criticMaxBudgetUsd: 0.5,
    builderRunner: async ({ maxBudgetUsd }) => {
      builderCalls += 1;
      assert.equal(maxBudgetUsd, 0.25);
      return { isError: false, costUsd: 0.15 };
    },
    captureRunner: async () => {
      captureCalls += 1;
      return [{ name: "current", path: "/tmp/evidence.png" }];
    },
    criticRunner: async ({ screenshots, maxBudgetUsd }) => {
      criticCalls += 1;
      receivedCriticCap = maxBudgetUsd;
      return {
        status: "NEEDS_IMPROVEMENT",
        issues: ["lighting"],
        suggestions: ["adjust"],
        screenshots,
        cost_usd: 0.1,
      };
    },
  });
  assert.equal(builderCalls, 1);
  assert.equal(captureCalls, 1);
  assert.equal(criticCalls, 1);
  assert.equal(receivedCriticCap, 0.1);
  assert.equal(result.reason, "budget_exhausted");
  assert.equal(result.rounds, 1);
  assert.equal(result.budget.spent_usd, 0.25);
});

test("visual loop uses the same fail-closed inner SSE client", async (t) => {
  let receivedBody = null;
  const port = await testServer(t, (req, res) => {
    let raw = "";
    req.setEncoding("utf8");
    req.on("data", (chunk) => { raw += chunk; });
    req.on("end", () => {
      receivedBody = JSON.parse(raw);
      res.writeHead(500, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "provider down" }));
    });
  });
  let captureCalls = 0;
  let criticCalls = 0;
  const res = mockResponse();
  await handleVisualSceneLoop({
    body: {
      message: "build",
      sessionId: "studio-v",
      conversationId: "conversation-v",
      agent: "claude",
      model: "claude-opus-4-8",
      require_real_assets: true,
      asset_degraded_mode: "disabled",
    },
  }, res, {
    STUDIO_SESSION: "studio-default",
    runId: "run-visual-500",
    accessToken: TOKEN,
    internalPort: port,
    internalChatTimeoutMs: 1000,
    env: { SCENE_LOOP_MAX_ROUNDS: "1" },
    intentStore: new Map(),
    updateIntentSummary: async () => "intent",
    captureRunner: async () => { captureCalls += 1; return []; },
    criticRunner: async () => { criticCalls += 1; return { status: "PASS", issues: [], suggestions: [] }; },
    ueBroker: { send: async () => ({}) },
  });
  const done = parseEvents(res.text()).find((event) => event.name === "done").data;
  assert.equal(done.isError, true);
  assert.equal(done.loop.reason, "builder_error");
  assert.equal(receivedBody.require_real_assets, true);
  assert.equal(receivedBody.asset_degraded_mode, "disabled");
  assert.equal(captureCalls, 0);
  assert.equal(criticCalls, 0);
});

test("scene loop rejects a malformed builder result before critic", async () => {
  let criticCalls = 0;
  const result = await runSceneLoop({
    prompt: "build",
    intentSummary: "build",
    maxRounds: 1,
    builderRunner: async () => ({}),
    criticRunner: async () => { criticCalls += 1; return { status: "PASS", issues: [], suggestions: [] }; },
  });
  assert.equal(result.reason, "builder_error");
  assert.equal(result.finalStatus, "NEEDS_IMPROVEMENT");
  assert.equal(criticCalls, 0);
});

test("critic failures expose typed error fields and never become PASS", async () => {
  const emitted = [];
  const error = Object.assign(new Error("review timed out"), {
    code: "REVIEW_TIMEOUT",
    retryable: true,
  });
  const result = await runSceneLoop({
    prompt: "build",
    intentSummary: "build",
    maxRounds: 1,
    criticProvider: "claude",
    criticModel: "claude-opus-4-8",
    builderRunner: async () => ({ isError: false }),
    criticRunner: async () => { throw error; },
    emit: (name, data) => emitted.push({ name, data }),
  });
  const verdict = emitted.find((event) => event.name === "critic_verdict").data;
  assert.equal(verdict.status, "FAIL");
  assert.equal(verdict.error, true);
  assert.equal(verdict.code, "REVIEW_TIMEOUT");
  assert.equal(verdict.message, "review timed out");
  assert.equal(verdict.errorDetails.retryable, true);
  assert.equal(verdict.provider, "claude");
  assert.equal(verdict.model, "claude-opus-4-8");
  const loopDone = emitted.find((event) => event.name === "loop_done").data;
  assert.equal(loopDone.failureReason, "critic_error");
  assert.equal(loopDone.error.code, "REVIEW_TIMEOUT");
  assert.notEqual(result.finalStatus, "PASS");
});

test("Text Review binds critic evidence to the server scope and emits only opaque refs", async () => {
  const emitted = [];
  const reference = {
    schema: "simworld-review-evidence-ref/v1",
    scope_digest: "c".repeat(64),
    evidence_id: `sha256:${"d".repeat(64)}`,
    handle: `evidence-${"e".repeat(48)}`,
  };
  await runSceneLoop({
    prompt: "build",
    intentSummary: "build",
    sessionId: "internal-session",
    scopeId: "server-owned-review-scope",
    maxRounds: 1,
    builderRunner: async () => ({ isError: false }),
    criticRunner: async (options) => {
      assert.equal(options.scopeId, "server-owned-review-scope");
      return {
        status: "PASS",
        issues: [],
        suggestions: [],
        screenshot: "/private/review-evidence/critic.png",
        screenshotRef: reference,
      };
    },
    emit: (name, data) => emitted.push({ name, data }),
  });

  const serialized = JSON.stringify(emitted);
  assert.doesNotMatch(serialized, /private\/review-evidence/);
  assert.doesNotMatch(serialized, /screenshot\/file\?path=/);
  assert.deepEqual(emitted.find((event) => event.name === "critic_verdict").data.screenshotRef, reference);
  assert.deepEqual(emitted.find((event) => event.name === "loop_done").data.latestScreenshotRef, reference);
});

test("visual critic verdict and loop_done expose review metadata", async () => {
  const emitted = [];
  const result = await runVisualSceneLoop({
    prompt: "build",
    intentSummary: "build",
    maxRounds: 1,
    builderRunner: async () => ({ isError: false }),
    captureRunner: async () => [{ name: "current", path: "/tmp/evidence.png" }],
    criticProvider: "claude",
    criticModel: "claude-opus-4-8",
    criticRunner: async ({ screenshots }) => ({
      status: "PASS",
      issues: [],
      suggestions: [],
      screenshots,
      provider: "claude",
      model: "claude-opus-4-8",
      evidence_ids: [`sha256:${"a".repeat(64)}`],
      usage: { input_tokens: 10 },
      latency_ms: 25,
    }),
    emit: (name, data) => emitted.push({ name, data }),
  });
  const verdict = emitted.find((event) => event.name === "critic_verdict").data;
  assert.equal(verdict.provider, "claude");
  assert.equal(verdict.model, "claude-opus-4-8");
  assert.deepEqual(verdict.evidence_ids, [`sha256:${"a".repeat(64)}`]);
  assert.deepEqual(verdict.usage, { input_tokens: 10 });
  assert.equal(verdict.latency_ms, 25);
  const loopDone = emitted.find((event) => event.name === "loop_done").data;
  assert.equal(loopDone.failureReason, null);
  assert.equal(loopDone.error, null);
  assert.equal(result.reason, "pass");
});

test("Visual Review loop_done summarizes builder output without exposing private fields", async () => {
  const emitted = [];
  await runVisualSceneLoop({
    prompt: "build",
    intentSummary: "build",
    maxRounds: 1,
    builderRunner: async () => ({
      isError: false,
      latestScreenshot: "/api/screenshot/file?path=%2Ftmp%2Fvisual-secret.png",
      filepath: "/home/yhliu/visual.scene",
      costUsd: 0,
    }),
    captureRunner: async () => [{ name: "current", path: "/tmp/evidence.png" }],
    criticRunner: async ({ screenshots }) => ({
      status: "PASS",
      issues: [],
      suggestions: [],
      screenshots,
    }),
    emit: (name, data) => emitted.push({ name, data }),
  });
  const serialized = JSON.stringify(emitted);
  assert.doesNotMatch(serialized, /screenshot\/file\?path=|\/tmp\/|\/home\/yhliu/);
  const loopDone = emitted.find((event) => event.name === "loop_done").data;
  assert.deepEqual(loopDone.builderResult, { isError: false, costUsd: 0 });
});

test("Visual Review treats an empty successful capture as typed VISUAL_CAPTURE_FAILED", async () => {
  const emitted = [];
  const result = await runVisualSceneLoop({
    prompt: "build",
    intentSummary: "build",
    maxRounds: 1,
    builderRunner: async () => ({ isError: false }),
    captureRunner: async () => [],
    criticRunner: async () => {
      assert.fail("critic must not run without visual evidence");
    },
    emit: (name, data) => emitted.push({ name, data }),
  });
  const verdict = emitted.find((event) => event.name === "critic_verdict").data;
  assert.equal(verdict.code, "VISUAL_CAPTURE_FAILED");
  assert.equal(verdict.errorDetails.code, "VISUAL_CAPTURE_FAILED");
  assert.match(verdict.message, /returned no evidence/);
  assert.equal(result.reason, "critic_error");
  assert.equal(result.finalStatus, "FAIL");
  const loopDone = emitted.find((event) => event.name === "loop_done").data;
  assert.equal(loopDone.finalStatus, "FAIL");
});

test("visual evidence reads only through the shared UE broker and shared provider", async (t) => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "simworld-review-loop-"));
  t.after(() => fs.rmSync(tmp, { recursive: true, force: true }));
  await certifyEvidenceRoots(tmp);
  const calls = [];
  const ueBroker = {
    async send(type, params, options) {
      calls.push({ type, params, options });
      if (type === "take_screenshot") {
        fs.writeFileSync(params.filepath, PNG_BYTES);
        return { status: "success" };
      }
      if (type === "get_actors_in_level") {
        return { result: { actors: [{ name: "Chair" }] } };
      }
      throw new Error(`Unexpected UE command: ${type}`);
    },
  };
  const shots = await multiViewScreenshot({ round: 1, destDir: tmp, ueBroker });
  t.after(() => cleanupEvidenceForPath(shots[0].path));
  assert.equal(shots.length, 1);
  assert.match(shots[0].evidenceRef.scope_digest, /^[a-f0-9]{64}$/);
  assert.equal(shots[0].evidenceRef.evidence_id, shots[0].evidenceId);
  assert.equal(calls[0].type, "take_screenshot");
  assert.equal(calls[0].options.signal, undefined);

  const actors = await getActorsSnapshot({ ueBroker });
  assert.equal(actors.result.actors.length, 1);
  assert.equal(calls[1].type, "get_actors_in_level");

  let providerInput = null;
  const verdict = await visualCritique({
    originalPrompt: "an office",
    screenshots: shots,
    model: "claude-opus-4-8",
    provider: "claude",
    ueBroker,
    reviewProvider: async (input) => {
      providerInput = input;
      return {
        status: "PASS",
        issues: [],
        suggestions: [],
        raw: "ok",
        provider: "claude",
        model: "claude-opus-4-8",
        evidence_ids: input.evidenceIds,
      };
    },
  });
  assert.equal(calls[2].type, "get_actors_in_level");
  assert.equal(providerInput.images.length, 1);
  assert.equal(providerInput.images[0].mediaType, "image/png");
  assert.deepEqual(providerInput.evidenceIds, [
    `sha256:${crypto.createHash("sha256").update(PNG_BYTES).digest("hex")}`,
  ]);
  assert.equal(verdict.status, "PASS");
  assert.equal(verdict.actorsCount, 1);
  assert.deepEqual(
    calls.map((call) => call.type),
    ["take_screenshot", "get_actors_in_level", "get_actors_in_level"],
  );
});

test("parallel Visual captures isolate colon/underscore scopes and expose no raw path as authority", async (t) => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "simworld-review-isolation-"));
  t.after(() => fs.rmSync(tmp, { recursive: true, force: true }));
  await certifyEvidenceRoots(tmp);
  const broker = (suffix) => ({
    async send(type, params) {
      assert.equal(type, "take_screenshot");
      fs.writeFileSync(params.filepath, Buffer.concat([PNG_BYTES, Buffer.from(suffix)]));
      return { status: "success" };
    },
  });
  const [colon, underscore] = await Promise.all([
    multiViewScreenshot({
      round: 1,
      destDir: tmp,
      scopeId: "review:lease",
      ueBroker: broker("colon"),
    }),
    multiViewScreenshot({
      round: 1,
      destDir: tmp,
      scopeId: "review_lease",
      ueBroker: broker("underscore"),
    }),
  ]);
  t.after(() => Promise.all([
    cleanupEvidenceForPath(colon[0].path),
    cleanupEvidenceForPath(underscore[0].path),
  ]));

  assert.notEqual(path.dirname(colon[0].path), path.dirname(underscore[0].path));
  assert.notEqual(colon[0].evidenceRef.scope_digest, underscore[0].evidenceRef.scope_digest);
  assert.notEqual(colon[0].evidenceId, underscore[0].evidenceId);
  assert.doesNotMatch(colon[0].path, /review[:_]lease/);
  assert.doesNotMatch(underscore[0].path, /review[:_]lease/);
});

test("Visual loader rejects tamper, symlink replacement, oversized files, and aggregate overflow before provider", async (t) => {
  const wrongRoot = fs.mkdtempSync(path.join(os.tmpdir(), "simworld-review-wrong-"));
  const symlinkRoot = fs.mkdtempSync(path.join(os.tmpdir(), "simworld-review-symlink-"));
  const oversizedRoot = fs.mkdtempSync(path.join(os.tmpdir(), "simworld-review-oversized-"));
  const totalRoot = fs.mkdtempSync(path.join(os.tmpdir(), "simworld-review-total-"));
  const outside = path.join(os.tmpdir(), `simworld-review-outside-${process.pid}-${Date.now()}.png`);
  t.after(() => {
    for (const root of [wrongRoot, symlinkRoot, oversizedRoot, totalRoot]) {
      fs.rmSync(root, { recursive: true, force: true });
    }
    fs.rmSync(outside, { force: true });
  });
  await certifyEvidenceRoots(wrongRoot, symlinkRoot, oversizedRoot, totalRoot);
  let providerCalls = 0;
  let actorCalls = 0;
  const ueBroker = {
    async send(type) {
      assert.equal(type, "get_actors_in_level");
      actorCalls += 1;
      return { result: { actors: [] } };
    },
  };
  const reviewProvider = async () => {
    providerCalls += 1;
    return { status: "PASS", issues: [], suggestions: [] };
  };

  const wrongRun = await createPrivateEvidenceRun({ root: wrongRoot, scopeId: "visual-wrong" });
  const wrong = await reserveEvidenceFile(wrongRun, "wrong");
  fs.writeFileSync(wrong, PNG_BYTES);
  await sealManagedScreenshot(wrong);
  fs.writeFileSync(wrong, Buffer.from("wrong-magic"));
  await assert.rejects(
    visualCritique({
      screenshots: [{ name: "wrong", path: wrong }],
      ueBroker,
      reviewProvider,
    }),
    (error) => error.code === "REVIEW_EVIDENCE_INVALID",
  );
  await cleanupPrivateEvidenceRun(wrongRun);

  fs.writeFileSync(outside, PNG_BYTES, { mode: 0o600 });
  const symlinkRun = await createPrivateEvidenceRun({ root: symlinkRoot, scopeId: "visual-symlink" });
  const symlink = await reserveEvidenceFile(symlinkRun, "symlink");
  fs.writeFileSync(symlink, PNG_BYTES);
  await sealManagedScreenshot(symlink);
  fs.unlinkSync(symlink);
  fs.symlinkSync(outside, symlink);
  await assert.rejects(
    visualCritique({
      screenshots: [{ name: "symlink", path: symlink }],
      ueBroker,
      reviewProvider,
    }),
    (error) => error.code === "REVIEW_EVIDENCE_INVALID",
  );
  await cleanupPrivateEvidenceRun(symlinkRun);
  assert.equal(fs.readFileSync(outside).equals(PNG_BYTES), true);

  await assert.rejects(
    multiViewScreenshot({
      round: 1,
      destDir: oversizedRoot,
      scopeId: "visual-oversized",
      ueBroker: {
        async send(_type, params) {
          const descriptor = fs.openSync(params.filepath, "r+");
          try {
            fs.writeSync(descriptor, PNG_BYTES, 0, PNG_BYTES.length, 0);
            fs.ftruncateSync(descriptor, 25 * 1024 * 1024 + 1);
          } finally {
            fs.closeSync(descriptor);
          }
          return { status: "success" };
        },
      },
    }),
    (error) => error.code === "REVIEW_EVIDENCE_INVALID",
  );

  const totalRun = await createPrivateEvidenceRun({ root: totalRoot, scopeId: "visual-total" });
  const totalShots = [];
  for (let index = 0; index < 3; index += 1) {
    const filepath = await reserveEvidenceFile(totalRun, `total-${index}`);
    const descriptor = fs.openSync(filepath, "r+");
    try {
      fs.writeSync(descriptor, PNG_BYTES, 0, PNG_BYTES.length, 0);
      fs.ftruncateSync(descriptor, 22 * 1024 * 1024);
    } finally {
      fs.closeSync(descriptor);
    }
    await sealManagedScreenshot(filepath);
    totalShots.push({ name: `total-${index}`, path: filepath });
  }
  await assert.rejects(
    visualCritique({ screenshots: totalShots, ueBroker, reviewProvider }),
    (error) => error.code === "REVIEW_EVIDENCE_INVALID",
  );
  await cleanupPrivateEvidenceRun(totalRun);
  assert.equal(actorCalls, 0, "unsafe evidence must fail before actor/provider work");
  assert.equal(providerCalls, 0, "unsafe evidence must fail before provider work");
});

test("Visual SSE emits opaque evidence refs without leaking managed server paths", async (t) => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "simworld-review-public-ref-"));
  t.after(() => fs.rmSync(tmp, { recursive: true, force: true }));
  await certifyEvidenceRoots(tmp);
  const emitted = [];
  const result = await runVisualSceneLoop({
    prompt: "build",
    intentSummary: "build",
    sessionId: "server-authoritative-scope",
    maxRounds: 1,
    destDir: tmp,
    ueBroker: {
      async send(type, params) {
        assert.equal(type, "take_screenshot");
        fs.writeFileSync(params.filepath, PNG_BYTES);
        return { status: "success" };
      },
    },
    builderRunner: async () => ({ isError: false }),
    criticRunner: async ({ screenshots }) => ({
      status: "PASS",
      issues: [],
      suggestions: [],
      screenshots,
      evidence_ids: screenshots.map((shot) => shot.evidenceId),
    }),
    emit: (name, data) => emitted.push({ name, data }),
  });
  t.after(() => cleanupEvidenceForPath(result.latestScreenshots[0].path));

  const serialized = JSON.stringify(emitted);
  assert.doesNotMatch(serialized, new RegExp(tmp.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  const shots = emitted.find((event) => event.name === "multi_shots").data;
  assert.equal(shots.evidence.length, 1);
  assert.match(shots.evidence[0].scope_digest, /^[a-f0-9]{64}$/);
  assert.match(shots.evidence[0].handle, /^evidence-[a-f0-9]{48}$/);
  const loopDone = emitted.find((event) => event.name === "loop_done").data;
  assert.equal(Object.hasOwn(loopDone.latestScreenshots[0], "path"), false);
});

test("visual capture aborts while waiting for the screenshot file", async (t) => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "simworld-review-abort-"));
  t.after(() => fs.rmSync(tmp, { recursive: true, force: true }));
  await certifyEvidenceRoots(tmp);
  const controller = new AbortController();
  let brokerSignal = null;
  const started = Date.now();
  const pending = multiViewScreenshot({
    round: 1,
    destDir: tmp,
    signal: controller.signal,
    ueBroker: {
      async send(_type, _params, options) {
        brokerSignal = options.signal;
        return { status: "success" };
      },
    },
  });
  setTimeout(() => controller.abort(), 20);
  await assert.rejects(pending, (error) => error.code === "INTERNAL_HTTP_ABORTED");
  assert.equal(brokerSignal, controller.signal);
  assert.ok(Date.now() - started < 500, "capture cancellation should not wait for the 45s file timeout");
});

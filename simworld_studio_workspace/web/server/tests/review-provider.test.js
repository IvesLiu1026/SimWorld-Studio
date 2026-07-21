"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { PassThrough, Writable } = require("node:stream");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  ReviewProviderError,
  createReviewProvider,
  resolveReviewConfig,
  validateReviewVerdict,
} = require("../review-provider");
const { getActors, screenshotEvidence, takeScreenshot } = require("../scene-critic");
const { LlmOneShotError, oneshotText } = require("../llm-oneshot");

const BASE_REVIEW = Object.freeze({
  provider: "claude",
  model: "claude-opus-4-8",
  systemPrompt: "Review only the supplied evidence.",
  prompt: "Evaluate this scene.",
  images: [{ mediaType: "image/png", data: Buffer.from("fake-png-evidence") }],
  evidenceIds: ["scene-shot-1"],
  timeoutMs: 500,
});

function fakeChildFactory(onInput) {
  const calls = [];
  const spawnImpl = (binary, args, options) => {
    const child = new EventEmitter();
    child.stdout = new PassThrough();
    child.stderr = new PassThrough();
    child.kills = [];
    child.kill = (signal) => {
      child.kills.push(signal);
      return true;
    };
    let input = "";
    child.stdin = new Writable({
      write(chunk, _encoding, callback) {
        input += chunk.toString();
        callback();
      },
      final(callback) {
        callback();
        queueMicrotask(() => onInput({ child, binary, args, options, input }));
      },
    });
    calls.push({ child, binary, args, options, get input() { return input; } });
    return child;
  };
  return { spawnImpl, calls };
}

function emitResult(child, structuredOutput, extras = {}) {
  child.stdout.write(`${JSON.stringify({
    type: "result",
    structured_output: structuredOutput,
    ...extras,
  })}\n`);
  child.emit("close", 0, null);
}

async function expectCode(promise, code) {
  await assert.rejects(promise, (error) => {
    assert.ok(error instanceof ReviewProviderError);
    assert.equal(error.code, code);
    return true;
  });
}

test("tool-free Claude adapter uses strict schema and keeps evidence off argv", async () => {
  const fake = fakeChildFactory(({ child }) => emitResult(child, {
    status: "PASS",
    issues: [],
    suggestions: ["Keep the current layout."],
    raw_notes: "Evidence is consistent.",
  }, { usage: { input_tokens: 123, output_tokens: 17 }, total_cost_usd: 0.0123456 }));
  let tick = 100;
  const provider = createReviewProvider({
    spawnImpl: fake.spawnImpl,
    now: () => { tick += 5; return tick; },
    env: {
      PATH: process.env.PATH,
      HOME: process.env.HOME,
      ANTHROPIC_API_KEY: "provider-key",
      CRITIC_MAX_BUDGET_USD: "0.25",
      STUDIO_ACCESS_TOKEN: "must-not-reach-child",
      POSTGRES_URL: "postgres://must-not-reach-child",
      CLAUDECODE: "nested-session-marker",
    },
  });

  const result = await provider.review(BASE_REVIEW);
  assert.equal(result.status, "PASS");
  assert.equal(result.provider, "claude");
  assert.equal(result.model, "claude-opus-4-8");
  assert.equal(result.max_budget_usd, 0.25);
  assert.deepEqual(result.evidence_ids, ["scene-shot-1"]);
  assert.deepEqual(result.usage, { input_tokens: 123, output_tokens: 17 });
  assert.equal(result.cost_usd, 0.012346);
  assert.equal(result.latency_ms, 5);

  assert.equal(fake.calls.length, 1);
  const call = fake.calls[0];
  assert.equal(call.binary, "claude");
  assert.ok(call.args.includes("--safe-mode"));
  assert.ok(call.args.includes("--no-session-persistence"));
  assert.ok(call.args.includes("--json-schema"));
  const budgetIndex = call.args.indexOf("--max-budget-usd");
  assert.notEqual(budgetIndex, -1);
  assert.equal(call.args[budgetIndex + 1], "0.25");
  const toolsIndex = call.args.indexOf("--tools");
  assert.notEqual(toolsIndex, -1);
  assert.equal(call.args[toolsIndex + 1], "");
  assert.equal(call.args.some((arg) => /dangerously|bypass/i.test(arg)), false);
  assert.equal(call.args.some((arg) => arg.includes("Evaluate this scene")), false);
  assert.match(call.input, /Evaluate this scene/);
  assert.equal(call.options.env.ANTHROPIC_API_KEY, "provider-key");
  assert.equal(call.options.env.STUDIO_ACCESS_TOKEN, undefined);
  assert.equal(call.options.env.POSTGRES_URL, undefined);
  assert.equal(call.options.env.CLAUDECODE, undefined);
});

test("provider and model selection fail closed before spawning", async () => {
  assert.equal(
    resolveReviewConfig({ provider: "claude", model: "claude-opus-4-8" }, {}).maxBudgetUsd,
    0.5,
  );
  assert.equal(
    resolveReviewConfig({ model: "claude-opus-4-8" }, { LLM_PROVIDER: "codex" }).provider,
    "claude",
    "builder provider must not implicitly select the critic provider",
  );
  assert.throws(
    () => resolveReviewConfig({ provider: "unknown", model: "claude-opus-4-8" }, {}),
    (error) => error.code === "REVIEW_PROVIDER_INVALID",
  );
  assert.throws(
    () => resolveReviewConfig({ provider: "claude", model: "opus" }, {}),
    (error) => error.code === "REVIEW_MODEL_INVALID",
  );
  assert.throws(
    () => resolveReviewConfig({ provider: "codex", model: "gpt-5.5" }, {}),
    (error) => error.code === "REVIEW_PROVIDER_UNSAFE",
  );
  assert.throws(
    () => resolveReviewConfig({ provider: "claude", model: "claude-opus-4-8", maxBudgetUsd: 0 }, {}),
    (error) => error.code === "REVIEW_BUDGET_INVALID",
  );
  const productionEnv = {
    NODE_ENV: "production",
    CRITIC_PROVIDER: "claude",
    CRITIC_MODEL: "claude-opus-4-8",
    CRITIC_MAX_BUDGET_USD: "0.05",
  };
  assert.deepEqual(resolveReviewConfig({}, productionEnv), {
    provider: "claude",
    model: "claude-opus-4-8",
    maxBudgetUsd: 0.05,
  });
  assert.throws(
    () => resolveReviewConfig({ model: "claude-sonnet-4-6" }, productionEnv),
    (error) => error.code === "REVIEW_MODEL_PIN_MISMATCH",
  );
  assert.throws(
    () => resolveReviewConfig({ maxBudgetUsd: 0.06 }, productionEnv),
    (error) => error.code === "REVIEW_BUDGET_EXCEEDS_DEPLOYMENT_CAP",
  );
  assert.throws(
    () => resolveReviewConfig({}, { ...productionEnv, CRITIC_MAX_BUDGET_USD: "" }),
    (error) => error.code === "REVIEW_PRODUCTION_PIN_MISSING",
  );

  let spawned = false;
  const provider = createReviewProvider({ spawnImpl: () => { spawned = true; } });
  await expectCode(provider.review({ ...BASE_REVIEW, provider: "openai", model: "gpt-5.5" }), "REVIEW_PROVIDER_UNSAFE");
  assert.equal(spawned, false);
});

test("strict verdict validation rejects coercion and unknown fields", () => {
  assert.throws(
    () => validateReviewVerdict({ status: "pass", issues: [], suggestions: [], raw_notes: "" }),
    (error) => error.code === "REVIEW_SCHEMA_INVALID",
  );
  assert.throws(
    () => validateReviewVerdict({
      status: "PASS", issues: [], suggestions: [], raw_notes: "", confidence: 1,
    }),
    (error) => error.code === "REVIEW_SCHEMA_INVALID",
  );
  assert.throws(
    () => validateReviewVerdict({ status: "PASS", issues: [42], suggestions: [], raw_notes: "" }),
    (error) => error.code === "REVIEW_SCHEMA_INVALID",
  );
});

test("malformed structured output is a typed schema failure", async () => {
  const fake = fakeChildFactory(({ child }) => emitResult(child, {
    status: "pass",
    issues: [],
    suggestions: [],
    raw_notes: "not enum-safe",
  }));
  const provider = createReviewProvider({ spawnImpl: fake.spawnImpl, env: {} });
  await expectCode(provider.review(BASE_REVIEW), "REVIEW_SCHEMA_INVALID");
});

test("invalid provider cost metadata fails the strict protocol", async () => {
  const fake = fakeChildFactory(({ child }) => emitResult(child, {
    status: "PASS",
    issues: [],
    suggestions: [],
    raw_notes: "valid verdict",
  }, { total_cost_usd: "not-a-number" }));
  const provider = createReviewProvider({ spawnImpl: fake.spawnImpl, env: {} });
  await expectCode(provider.review(BASE_REVIEW), "REVIEW_PROTOCOL_ERROR");
});

test("markdown or non-JSON output is rejected instead of parsed permissively", async () => {
  const fake = fakeChildFactory(({ child }) => {
    child.stdout.write(`${JSON.stringify({
      type: "result",
      result: "```json\\n{\\\"status\\\":\\\"PASS\\\",\\\"issues\\\":[],\\\"suggestions\\\":[],\\\"raw_notes\\\":\\\"\\\"}\\n```",
    })}\n`);
    child.emit("close", 0, null);
  });
  const provider = createReviewProvider({ spawnImpl: fake.spawnImpl, env: {} });
  await expectCode(provider.review(BASE_REVIEW), "REVIEW_PROTOCOL_ERROR");
});

test("provider timeout terminates the child and returns a retryable typed failure", async () => {
  const fake = fakeChildFactory(() => {});
  const provider = createReviewProvider({ spawnImpl: fake.spawnImpl, env: {} });
  await assert.rejects(provider.review({ ...BASE_REVIEW, timeoutMs: 15 }), (error) => {
    assert.equal(error.code, "REVIEW_TIMEOUT");
    assert.equal(error.retryable, true);
    return true;
  });
  assert.deepEqual(fake.calls[0].child.kills, ["SIGTERM"]);
});

test("AbortSignal terminates the child and returns REVIEW_ABORTED", async () => {
  const fake = fakeChildFactory(() => {});
  const provider = createReviewProvider({ spawnImpl: fake.spawnImpl, env: {} });
  const controller = new AbortController();
  const pending = provider.review({ ...BASE_REVIEW, signal: controller.signal });
  controller.abort();
  await expectCode(pending, "REVIEW_ABORTED");
  assert.deepEqual(fake.calls[0].child.kills, ["SIGTERM"]);
});

test("owned provider paths contain no dangerous permission bypass", () => {
  for (const filename of ["review-provider.js", "scene-critic.js", "llm-oneshot.js"]) {
    const source = fs.readFileSync(path.resolve(__dirname, `../${filename}`), "utf8");
    assert.doesNotMatch(source, /dangerously-(?:skip-permissions|bypass-approvals-and-sandbox)/);
  }
});

test("scene critic accepts managed image evidence and blocks paths outside its evidence root", (t) => {
  const managedDir = path.resolve(__dirname, "../../../tmp/screens");
  fs.mkdirSync(managedDir, { recursive: true });
  const managedPath = path.join(managedDir, `review-provider-test-${process.pid}-${Date.now()}.png`);
  fs.writeFileSync(managedPath, Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]));
  t.after(() => fs.rmSync(managedPath, { force: true }));
  const evidence = screenshotEvidence(managedPath);
  assert.equal(evidence.mediaType, "image/png");
  assert.equal(evidence.filepath, fs.realpathSync(managedPath));

  const outsidePath = path.join(os.tmpdir(), `review-provider-outside-${process.pid}-${Date.now()}.png`);
  fs.writeFileSync(outsidePath, Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]));
  t.after(() => fs.rmSync(outsidePath, { force: true }));
  assert.throws(
    () => screenshotEvidence(outsidePath),
    (error) => error.code === "REVIEW_EVIDENCE_INVALID",
  );
});

test("text critic evidence capture uses the injected process-wide UE broker", async (t) => {
  const calls = [];
  const ueBroker = {
    async send(type, params, options) {
      calls.push({ type, params, options });
      if (type === "take_screenshot") {
        fs.writeFileSync(params.filepath, Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]));
        t.after(() => fs.rmSync(params.filepath, { force: true }));
        return { status: "success" };
      }
      return { result: { actors: [] } };
    },
  };
  const [screenshot, actors] = await Promise.all([
    takeScreenshot({ ueBroker }),
    getActors({ ueBroker }),
  ]);
  assert.ok(screenshot.endsWith(".png"));
  assert.deepEqual(actors, { result: { actors: [] } });
  assert.deepEqual(calls.map((call) => call.type).sort(), ["get_actors_in_level", "take_screenshot"]);
  const screenshotCall = calls.find((call) => call.type === "take_screenshot");
  assert.deepEqual(screenshotCall.options, { timeoutMs: 30000, queueDeadlineMs: 45000 });

  const source = fs.readFileSync(path.resolve(__dirname, "../scene-critic.js"), "utf8");
  assert.doesNotMatch(source, /new net\.Socket|UNREAL_HOST|UNREAL_PORT/);
  assert.match(source, /getUeBroker\(\)/);
});

test("an already-aborted evidence capture never enters the UE broker", async () => {
  let calls = 0;
  const ueBroker = { send: async () => { calls += 1; return {}; } };
  const controller = new AbortController();
  controller.abort();
  await expectCode(getActors({ ueBroker, signal: controller.signal }), "REVIEW_ABORTED");
  assert.equal(calls, 0);
});

test("an in-flight UE evidence wait is cancelled by AbortSignal", async () => {
  let calls = 0;
  const ueBroker = {
    send: () => {
      calls += 1;
      return new Promise(() => {});
    },
  };
  const controller = new AbortController();
  const pending = getActors({ ueBroker, signal: controller.signal });
  controller.abort();
  await expectCode(pending, "REVIEW_ABORTED");
  assert.equal(calls, 1);
});

test("Claude and Codex one-shots terminate on AbortSignal with a settled typed failure", async () => {
  for (const providerName of ["claude", "codex"]) {
    const fake = fakeChildFactory(() => {});
    const controller = new AbortController();
    const pending = oneshotText("summarize this", {
      provider: providerName,
      model: providerName === "claude" ? "claude-opus-4-8" : "gpt-5.5",
      spawnImpl: fake.spawnImpl,
      timeoutMs: 500,
      signal: controller.signal,
    });
    controller.abort();
    await assert.rejects(pending, (error) => {
      assert.ok(error instanceof LlmOneShotError);
      assert.equal(error.code, "LLM_ONESHOT_ABORTED");
      assert.equal(error.provider, providerName);
      return true;
    });
    assert.deepEqual(fake.calls[0].child.kills, ["SIGTERM"]);

    // A late provider close must be ignored after the abort has settled.
    fake.calls[0].child.emit("close", 0, null);
  }
});

test("one-shot timeout kills the child and cannot resolve from a late close", async () => {
  const fake = fakeChildFactory(() => {});
  const pending = oneshotText("summarize this", {
    provider: "claude",
    model: "claude-opus-4-8",
    spawnImpl: fake.spawnImpl,
    timeoutMs: 15,
  });
  await assert.rejects(pending, (error) => {
    assert.equal(error.code, "LLM_ONESHOT_TIMEOUT");
    assert.equal(error.retryable, true);
    return true;
  });
  assert.deepEqual(fake.calls[0].child.kills, ["SIGTERM"]);
  fake.calls[0].child.emit("close", 0, null);
});

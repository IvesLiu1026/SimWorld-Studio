"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { PassThrough, Writable } = require("node:stream");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  ReviewProviderSmokeError,
  SMOKE_SYSTEM_PROMPT,
  createReviewProviderSmokeRunner,
} = require("../review-provider-smoke");
const {
  main: reviewSmokeCli,
  parseArgv,
} = require("../review-provider-smoke-cli");
const {
  createReviewSmokeReceipt,
} = require("../review-smoke-receipt");

const NOW = Date.parse("2026-07-21T08:00:00.000Z");
const SOURCE_REVISION = "a".repeat(40);
const SCENE_DIGEST = "b".repeat(64);
const PNG = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00]);

function providerResult(overrides = {}) {
  return {
    status: "PASS",
    issues: [],
    suggestions: ["Keep the verified composition."],
    raw: "sensitive-provider-prose-must-not-persist",
    provider: "claude",
    model: "claude-opus-4-8",
    usage: { input_tokens: 120, output_tokens: 18 },
    cost_usd: 0.012,
    latency_ms: 50,
    ...overrides,
  };
}

function runInput(overrides = {}) {
  return {
    reviewType: "visual",
    provider: "claude",
    model: "claude-opus-4-8",
    sourceRevision: SOURCE_REVISION,
    sceneDigestBefore: SCENE_DIGEST,
    reviewRequest: "Confirm the VISTA mmg_040 layout matches the supplied evidence.",
    images: [{ mediaType: "image/png", data: PNG }],
    cliName: "claude-code",
    cliVersion: "2.1.215",
    receiptPath: "/runtime/review-smoke.json",
    maxBudgetUsd: 0.1,
    timeoutMs: 500,
    maxInputTokens: 500,
    maxOutputTokens: 100,
    ...overrides,
  };
}

function makeRunner({ result = providerResult(), captureDigest = SCENE_DIGEST, providerReview, now } = {}) {
  const events = [];
  const writes = [];
  let clockCalls = 0;
  const runner = createReviewProviderSmokeRunner({
    providerAdapter: {
      review: providerReview || (async (options) => {
        events.push({ type: "provider", options });
        return result;
      }),
    },
    captureSceneDigestAfter: async () => {
      events.push({ type: "scene-after" });
      return captureDigest;
    },
    writeReceipt: (filename, receipt) => {
      events.push({ type: "write" });
      writes.push({ filename, receipt });
      return path.resolve(filename);
    },
    now: now || (() => NOW + (clockCalls++ * 100)),
    randomBytes: () => Buffer.alloc(8, 0x12),
  });
  return { runner, events, writes };
}

async function rejectCode(promise, code) {
  await assert.rejects(promise, (error) => {
    assert.ok(error instanceof ReviewProviderSmokeError);
    assert.equal(error.code, code);
    return true;
  });
}

test("PASS-only smoke uses the strict read-only prompt and atomically publishes a compact receipt", async () => {
  const { runner, events, writes } = makeRunner();
  const output = await runner.run(runInput());

  assert.deepEqual(events.map((event) => event.type), ["provider", "scene-after", "write"]);
  assert.equal(writes.length, 1);
  assert.equal(output.receiptPath, "/runtime/review-smoke.json");
  assert.equal(output.receipt.verdict.status, "PASS");
  assert.equal(output.receipt.scene.digest_before, SCENE_DIGEST);
  assert.equal(output.receipt.scene.digest_after, SCENE_DIGEST);
  assert.equal(output.receipt.usage.cost_usd, 0.012);
  assert.equal(JSON.stringify(output.receipt).includes("sensitive-provider-prose"), false);
  assert.equal(JSON.stringify(output.receipt).includes("mmg_040"), false);

  const providerCall = events[0].options;
  assert.equal(providerCall.systemPrompt, SMOKE_SYSTEM_PROMPT);
  assert.match(providerCall.systemPrompt, /no tools/i);
  assert.match(providerCall.systemPrompt, /must not request.*scene mutation/i);
  assert.match(providerCall.systemPrompt, /exactly one JSON object/i);
  assert.match(providerCall.prompt, /Immutable build revision: a{40}/);
  assert.match(providerCall.prompt, /Read-only scene digest before review: b{64}/);
  assert.equal(providerCall.provider, "claude");
  assert.equal(providerCall.model, "claude-opus-4-8");
  assert.equal(providerCall.maxBudgetUsd, 0.1);
  assert.equal(providerCall.timeoutMs, 500);
  assert.equal(providerCall.images.length, 1);
});

test("non-PASS, excessive usage, provider mismatch, and visual mutation never write a receipt", async (t) => {
  const cases = [
    {
      name: "non-PASS verdict",
      setup: { result: providerResult({ status: "NEEDS_IMPROVEMENT" }) },
      code: "REVIEW_SMOKE_VERDICT_FAILED",
    },
    {
      name: "excessive output usage",
      setup: { result: providerResult({ usage: { input_tokens: 10, output_tokens: 101 } }) },
      code: "REVIEW_SMOKE_USAGE_LIMIT_EXCEEDED",
    },
    {
      name: "cache-inclusive input usage",
      setup: {
        result: providerResult({
          usage: { input_tokens: 10, output_tokens: 10, cache_read_input_tokens: 491 },
        }),
      },
      code: "REVIEW_SMOKE_USAGE_LIMIT_EXCEEDED",
    },
    {
      name: "missing cost metadata",
      setup: { result: providerResult({ cost_usd: null }) },
      code: "REVIEW_SMOKE_COST_INVALID",
    },
    {
      name: "wrong provider identity",
      setup: { result: providerResult({ model: "claude-sonnet-4-6" }) },
      code: "REVIEW_SMOKE_PROVIDER_MISMATCH",
    },
    {
      name: "visual scene mutation",
      setup: { captureDigest: "c".repeat(64) },
      code: "REVIEW_SMOKE_SCENE_MUTATED",
    },
  ];

  for (const item of cases) {
    await t.test(item.name, async () => {
      const { runner, writes } = makeRunner(item.setup);
      await rejectCode(runner.run(runInput()), item.code);
      assert.equal(writes.length, 0);
    });
  }
});

test("explicit immutable inputs and post-scene evidence fail closed before provider or persistence", async () => {
  let calls = 0;
  const { runner, writes } = makeRunner({
    providerReview: async () => { calls += 1; return providerResult(); },
  });

  await rejectCode(runner.run(runInput({ sourceRevision: "main" })), "REVIEW_SMOKE_INPUT_INVALID");
  await rejectCode(runner.run(runInput({ model: "opus" })), "REVIEW_MODEL_INVALID");
  await rejectCode(runner.run(runInput({ api_key: "must-never-be-accepted" })), "REVIEW_SMOKE_INPUT_SENSITIVE");
  await rejectCode(runner.run(runInput({ sceneDigestBefore: "short" })), "REVIEW_SMOKE_SCENE_INVALID");
  assert.equal(calls, 0);
  assert.equal(writes.length, 0);

  const withoutPostCapture = createReviewProviderSmokeRunner({
    providerAdapter: { review: async () => { calls += 1; return providerResult(); } },
    writeReceipt: () => { throw new Error("must not write"); },
  });
  await rejectCode(withoutPostCapture.run(runInput()), "REVIEW_SMOKE_SCENE_INVALID");
  assert.equal(calls, 0);
});

test("provider failures are redacted and independent outer timeout aborts an injected provider", async () => {
  const secretError = new Error("provider failed with sk-ant-secret-value");
  secretError.code = "REVIEW_PROCESS_FAILED";
  secretError.retryable = true;
  const failed = makeRunner({ providerReview: async () => { throw secretError; } });
  await assert.rejects(failed.runner.run(runInput()), (error) => {
    assert.equal(error.code, "REVIEW_PROCESS_FAILED");
    assert.equal(error.retryable, true);
    assert.doesNotMatch(error.message, /sk-ant|secret-value/);
    return true;
  });
  assert.equal(failed.writes.length, 0);

  let observedSignal;
  const timed = makeRunner({
    providerReview: (options) => {
      observedSignal = options.signal;
      return new Promise(() => {});
    },
  });
  await rejectCode(timed.runner.run(runInput({ timeoutMs: 10 })), "REVIEW_SMOKE_TIMEOUT");
  assert.equal(observedSignal.aborted, true);
  assert.equal(timed.writes.length, 0);
});

test("the same wall-clock boundary covers post-review scene capture", async () => {
  let captureSignal;
  const writes = [];
  const runner = createReviewProviderSmokeRunner({
    providerAdapter: { review: async () => providerResult() },
    captureSceneDigestAfter: ({ signal }) => {
      captureSignal = signal;
      return new Promise(() => {});
    },
    writeReceipt: (...args) => { writes.push(args); },
    now: () => NOW,
  });
  await rejectCode(runner.run(runInput({ timeoutMs: 10 })), "REVIEW_SMOKE_TIMEOUT");
  assert.equal(captureSignal.aborted, true);
  assert.equal(writes.length, 0);
});

function fakeProviderProcess() {
  const calls = [];
  const spawnImpl = (binary, args, options) => {
    const child = new EventEmitter();
    child.stdout = new PassThrough();
    child.stderr = new PassThrough();
    child.kill = () => true;
    let input = "";
    child.stdin = new Writable({
      write(chunk, _encoding, callback) {
        input += chunk.toString();
        callback();
      },
      final(callback) {
        callback();
        queueMicrotask(() => {
          child.stdout.write(`${JSON.stringify({
            type: "result",
            structured_output: {
              status: "PASS",
              issues: [],
              suggestions: ["Keep the scene."],
              raw_notes: "bounded",
            },
            usage: { input_tokens: 25, output_tokens: 8 },
            total_cost_usd: 0.003,
          })}\n`);
          child.emit("close", 0, null);
        });
      },
    });
    calls.push({ binary, args, options, get input() { return input; } });
    return child;
  };
  return { calls, spawnImpl };
}

test("process runtime is dependency-injected and retains the lower adapter's tool-free contract", async () => {
  const processFake = fakeProviderProcess();
  const writes = [];
  let clockCalls = 0;
  const runner = createReviewProviderSmokeRunner({
    providerRuntime: {
      spawnImpl: processFake.spawnImpl,
      env: { PATH: process.env.PATH, ANTHROPIC_API_KEY: "injected-test-only" },
      now: () => NOW + (clockCalls++ * 5),
    },
    captureSceneDigestAfter: async () => SCENE_DIGEST,
    writeReceipt: (_filename, receipt) => { writes.push(receipt); return "/receipt.json"; },
    now: () => NOW + 100,
    randomBytes: () => Buffer.alloc(8, 0x34),
  });

  await runner.run(runInput());
  assert.equal(processFake.calls.length, 1);
  assert.equal(writes.length, 1);
  const call = processFake.calls[0];
  assert.equal(call.binary, "claude");
  assert.equal(call.args[call.args.indexOf("--tools") + 1], "");
  assert.ok(call.args.includes("--safe-mode"));
  assert.ok(call.args.includes("--no-session-persistence"));
  assert.equal(call.args.some((arg) => /dangerously|bypass/i.test(arg)), false);
  assert.equal(call.args.some((arg) => arg.includes("mmg_040")), false);
  assert.match(call.input, /mmg_040/);
});

test("CLI reads bounded evidence, emits only a compact summary, and supports runner injection", async (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "review-smoke-cli-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const promptPath = path.join(directory, "request.txt");
  const imagePath = path.join(directory, "evidence.png");
  const receiptPath = path.join(directory, "receipt.json");
  fs.writeFileSync(promptPath, "private-scene-request-must-not-print");
  fs.writeFileSync(imagePath, PNG);

  let capturedInput;
  const receipt = createReviewSmokeReceipt({
    receiptId: "review-smoke-cli-test",
    reviewType: "visual",
    provider: "claude",
    model: "claude-opus-4-8",
    cliName: "claude-code",
    cliVersion: "2.1.215",
    sourceRevision: SOURCE_REVISION,
    recordedAt: new Date(NOW).toISOString(),
    expiresAt: new Date(NOW + 60_000).toISOString(),
    sceneDigestBefore: SCENE_DIGEST,
    sceneDigestAfter: SCENE_DIGEST,
    usage: { input_tokens: 1, output_tokens: 1, cost_usd: 0.001 },
    verdict: { status: "PASS", issues: [], suggestions: [], raw_notes: "not persisted" },
  });
  const runner = {
    run: async (input) => {
      capturedInput = input;
      return { receipt, receiptPath };
    },
  };
  let stdout = "";
  let stderr = "";
  const code = await reviewSmokeCli([
    "--review-type", "visual",
    "--provider", "claude",
    "--model", "claude-opus-4-8",
    "--build-revision", SOURCE_REVISION,
    "--scene-digest-before", SCENE_DIGEST,
    "--scene-digest-after", SCENE_DIGEST,
    "--prompt-file", promptPath,
    "--image", imagePath,
    "--cli-name", "claude-code",
    "--cli-version", "2.1.215",
    "--max-budget-usd", "0.1",
    "--receipt", receiptPath,
  ], {
    runner,
    stdout: { write: (text) => { stdout += text; } },
    stderr: { write: (text) => { stderr += text; } },
  });

  assert.equal(code, 0);
  assert.equal(stderr, "");
  assert.equal(capturedInput.reviewRequest, "private-scene-request-must-not-print");
  assert.equal(capturedInput.images[0].mediaType, "image/png");
  assert.equal(capturedInput.maxBudgetUsd, 0.1);
  assert.doesNotMatch(stdout, /private-scene-request|evidence\.png|raw_notes/);
  assert.equal(JSON.parse(stdout).status, "PASS");
});

test("CLI rejects forbidden credential flags without echoing their values", async () => {
  assert.throws(
    () => parseArgv(["--api-key", "sk-ant-do-not-echo"]),
    (error) => error.code === "REVIEW_SMOKE_CLI_INVALID" && !error.message.includes("sk-ant-do-not-echo"),
  );
  let stderr = "";
  const code = await reviewSmokeCli(["--api-key", "sk-ant-do-not-echo"], {
    stdout: { write: () => {} },
    stderr: { write: (text) => { stderr += text; } },
  });
  assert.equal(code, 2);
  assert.doesNotMatch(stderr, /sk-ant|do-not-echo/);
});

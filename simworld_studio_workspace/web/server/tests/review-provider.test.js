"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { PassThrough, Writable } = require("node:stream");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  ReviewProviderError,
  createReviewProvider,
  parseClaudeCliIdentity,
  resolveReviewConfig,
  validateReviewVerdict,
} = require("../review-provider");
const {
  cleanupEvidenceForPath,
  cleanupPrivateEvidenceRun,
  createPrivateEvidenceRun,
  createReviewEvidenceReadinessProbe,
  evidenceCapacitySnapshot,
  getActors,
  managedScreenshotReference,
  reserveEvidenceFile,
  resolveManagedScreenshotReference,
  screenshotEvidence,
  sealManagedScreenshot,
  takeScreenshot,
} = require("../scene-critic");
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

const PNG_BYTES = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1]);

async function certifyEvidenceRoots(...roots) {
  const report = await createReviewEvidenceReadinessProbe({
    roots,
    deadlineMs: 1_000,
  })();
  assert.equal(report.status, "ready", JSON.stringify(report));
}

function fakeChildFactory(onInput, { emitSpawn = true } = {}) {
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
    if (emitSpawn) queueMicrotask(() => child.emit("spawn"));
    return child;
  };
  return { spawnImpl, calls };
}

function passthroughSandbox(bin, args) {
  return { cmd: bin, args };
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

test("Claude CLI identity is measured from the configured binary with a bounded safe environment", async () => {
  const calls = [];
  const provider = createReviewProvider({
    env: {
      PATH: process.env.PATH,
      HOME: process.env.HOME,
      ANTHROPIC_AUTH_TOKEN: "allowed-provider-auth",
      STUDIO_ACCESS_TOKEN: "must-not-reach-version-check",
    },
    spawnImpl(binary, args, options) {
      const child = new EventEmitter();
      child.stdout = new PassThrough();
      child.stderr = new PassThrough();
      child.kill = () => true;
      calls.push({ binary, args, options });
      queueMicrotask(() => {
        child.stdout.write("2.1.215 (Claude Code)\n");
        child.emit("close", 0, null);
      });
      return child;
    },
  });

  assert.deepEqual(await provider.inspectRuntimeIdentity(), {
    name: "claude-code",
    version: "2.1.215",
  });
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0].args, ["--version"]);
  assert.equal(calls[0].options.env.ANTHROPIC_AUTH_TOKEN, "allowed-provider-auth");
  assert.equal(calls[0].options.env.STUDIO_ACCESS_TOKEN, undefined);
  assert.throws(
    () => parseClaudeCliIdentity("2.1.215 (Claude Code)\nuntrusted-extra-line"),
    (error) => error.code === "REVIEW_CLI_IDENTITY_INVALID",
  );
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

test("scene critic accepts only sealed private evidence and resolves opaque refs in the authoritative scope", async (t) => {
  const managedRoot = fs.mkdtempSync(path.join(os.tmpdir(), "review-provider-managed-"));
  t.after(() => fs.rmSync(managedRoot, { recursive: true, force: true }));
  await certifyEvidenceRoots(managedRoot);
  const run = await createPrivateEvidenceRun({ root: managedRoot, scopeId: "lease:scope" });
  const managedPath = await reserveEvidenceFile(run, "managed");
  fs.writeFileSync(managedPath, PNG_BYTES);
  await sealManagedScreenshot(managedPath);
  t.after(() => cleanupPrivateEvidenceRun(run));

  const evidence = await screenshotEvidence(managedPath);
  assert.equal(evidence.mediaType, "image/png");
  assert.equal(evidence.filepath, managedPath);
  assert.match(evidence.evidenceId, /^sha256:[a-f0-9]{64}$/);
  assert.equal(fs.statSync(path.dirname(managedPath)).mode & 0o777, 0o700);
  assert.equal(fs.statSync(managedPath).mode & 0o777, 0o600);

  const reference = await managedScreenshotReference(managedPath);
  assert.deepEqual(reference, evidence.reference);
  assert.match(reference.scope_digest, /^[a-f0-9]{64}$/);
  assert.match(reference.handle, /^evidence-[a-f0-9]{48}$/);
  assert.equal(reference.evidence_id, evidence.evidenceId);
  const resolved = await resolveManagedScreenshotReference({
    scopeId: "lease:scope",
    evidenceId: reference.evidence_id,
    handle: reference.handle,
  });
  assert.deepEqual(resolved.data, PNG_BYTES);
  assert.equal(resolved.mediaType, "image/png");
  assert.equal(resolved.size, PNG_BYTES.length);
  assert.equal(Object.hasOwn(resolved, "filepath"), false);
  await assert.rejects(
    () => resolveManagedScreenshotReference({
      scopeId: "lease_scope",
      evidenceId: reference.evidence_id,
      handle: reference.handle,
    }),
    (error) => error.code === "REVIEW_EVIDENCE_INVALID",
  );

  const outsidePath = path.join(managedRoot, "outside.png");
  fs.writeFileSync(outsidePath, PNG_BYTES, { mode: 0o600 });
  t.after(() => fs.rmSync(outsidePath, { force: true }));
  await assert.rejects(
    () => screenshotEvidence(outsidePath),
    (error) => error.code === "REVIEW_EVIDENCE_INVALID",
  );
});

test("text critic evidence capture uses the injected process-wide UE broker", async (t) => {
  const evidenceRoot = fs.mkdtempSync(path.join(os.tmpdir(), "review-provider-broker-"));
  t.after(() => fs.rmSync(evidenceRoot, { recursive: true, force: true }));
  await certifyEvidenceRoots(evidenceRoot);
  const calls = [];
  const ueBroker = {
    async send(type, params, options) {
      calls.push({ type, params, options });
      if (type === "take_screenshot") {
        fs.writeFileSync(params.filepath, PNG_BYTES);
        t.after(() => cleanupEvidenceForPath(params.filepath));
        return { status: "success" };
      }
      return { result: { actors: [] } };
    },
  };
  const [screenshot, actors] = await Promise.all([
    takeScreenshot({ ueBroker, scopeId: "review-text-scope", evidenceRoot }),
    getActors({ ueBroker }),
  ]);
  assert.ok(screenshot.endsWith(".png"));
  assert.deepEqual(actors, { result: { actors: [] } });
  assert.deepEqual(calls.map((call) => call.type).sort(), ["get_actors_in_level", "take_screenshot"]);
  const screenshotCall = calls.find((call) => call.type === "take_screenshot");
  assert.deepEqual(screenshotCall.options, { timeoutMs: 30000, queueDeadlineMs: 45000 });
  assert.match((await managedScreenshotReference(screenshot)).handle, /^evidence-[a-f0-9]{48}$/);

  const source = fs.readFileSync(path.resolve(__dirname, "../scene-critic.js"), "utf8");
  assert.doesNotMatch(source, /new net\.Socket|UNREAL_HOST|UNREAL_PORT/);
  assert.match(source, /getUeBroker\(\)/);
});

test("concurrent scopes, including colon/underscore names, never share screenshot authority", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "review-provider-isolation-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  await certifyEvidenceRoots(root);
  const makeBroker = (bytes) => ({
    async send(type, params) {
      assert.equal(type, "take_screenshot");
      fs.writeFileSync(params.filepath, bytes);
      return { status: "success" };
    },
  });
  const leftBytes = Buffer.concat([PNG_BYTES, Buffer.from("left")]);
  const rightBytes = Buffer.concat([PNG_BYTES, Buffer.from("right")]);
  const [left, right] = await Promise.all([
    takeScreenshot({
      ueBroker: makeBroker(leftBytes),
      scopeId: "server-scope:lease",
      evidenceRoot: root,
    }),
    takeScreenshot({
      ueBroker: makeBroker(rightBytes),
      scopeId: "server-scope_lease",
      evidenceRoot: root,
    }),
  ]);
  t.after(() => Promise.all([
    cleanupEvidenceForPath(left),
    cleanupEvidenceForPath(right),
  ]));

  assert.notEqual(left, right);
  assert.notEqual(path.dirname(left), path.dirname(right));
  assert.doesNotMatch(left, /server-scope[:_]lease/);
  assert.doesNotMatch(right, /server-scope[:_]lease/);
  const leftEvidence = await screenshotEvidence(left);
  const rightEvidence = await screenshotEvidence(right);
  assert.equal(leftEvidence.evidenceId, `sha256:${crypto.createHash("sha256").update(leftBytes).digest("hex")}`);
  assert.equal(rightEvidence.evidenceId, `sha256:${crypto.createHash("sha256").update(rightBytes).digest("hex")}`);
  assert.notEqual(leftEvidence.reference.scope_digest, rightEvidence.reference.scope_digest);
  await assert.rejects(
    () => resolveManagedScreenshotReference({
      scopeId: "server-scope_lease",
      evidenceId: leftEvidence.evidenceId,
      handle: leftEvidence.reference.handle,
    }),
    (error) => error.code === "REVIEW_EVIDENCE_INVALID",
  );
});

test("symlink and regular-file replacements are rejected and whole-run tombstoned intact", async (t) => {
  const symlinkRoot = fs.mkdtempSync(path.join(os.tmpdir(), "review-provider-symlink-"));
  const replacementRoot = fs.mkdtempSync(path.join(os.tmpdir(), "review-provider-replace-"));
  const outside = path.join(os.tmpdir(), `review-provider-outside-${process.pid}-${Date.now()}.png`);
  fs.writeFileSync(outside, PNG_BYTES, { mode: 0o600 });
  t.after(() => {
    fs.rmSync(symlinkRoot, { recursive: true, force: true });
    fs.rmSync(replacementRoot, { recursive: true, force: true });
    fs.rmSync(outside, { force: true });
  });
  await certifyEvidenceRoots(symlinkRoot, replacementRoot);

  let symlinkPath = null;
  await assert.rejects(
    takeScreenshot({
      scopeId: "server-scope-symlink",
      evidenceRoot: symlinkRoot,
      ueBroker: {
        async send(_type, params) {
          symlinkPath = params.filepath;
          fs.unlinkSync(params.filepath);
          fs.symlinkSync(outside, params.filepath);
          return { status: "success" };
        },
      },
    }),
    (error) => error.code === "REVIEW_EVIDENCE_INVALID",
  );
  assert.equal(fs.readFileSync(outside).equals(PNG_BYTES), true);
  const symlinkTombstoneRoot = path.join(symlinkRoot, ".review-evidence-tombstones");
  const [symlinkTombstoneName] = fs.readdirSync(symlinkTombstoneRoot);
  const relocatedSymlink = path.join(
    symlinkTombstoneRoot,
    symlinkTombstoneName,
    path.basename(symlinkPath),
  );
  assert.equal(
    fs.lstatSync(relocatedSymlink).isSymbolicLink(),
    true,
    "cleanup must preserve a replacement inode in the tombstone",
  );

  let replacementPath = null;
  await assert.rejects(
    takeScreenshot({
      scopeId: "server-scope-replace",
      evidenceRoot: replacementRoot,
      ueBroker: {
        async send(_type, params) {
          replacementPath = params.filepath;
          const replacement = path.join(path.dirname(params.filepath), "replacement.png");
          fs.writeFileSync(replacement, PNG_BYTES, { mode: 0o600 });
          fs.renameSync(replacement, params.filepath);
          return { status: "success" };
        },
      },
    }),
    (error) => error.code === "REVIEW_EVIDENCE_INVALID",
  );
  const replacementTombstoneRoot = path.join(replacementRoot, ".review-evidence-tombstones");
  const [replacementTombstoneName] = fs.readdirSync(replacementTombstoneRoot);
  const relocatedReplacement = path.join(
    replacementTombstoneRoot,
    replacementTombstoneName,
    path.basename(replacementPath),
  );
  assert.equal(fs.readFileSync(relocatedReplacement).equals(PNG_BYTES), true);
});

test("cleanup refuses a replaced run directory and leaves the new inode untouched", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "review-provider-cleanup-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  await certifyEvidenceRoots(root);
  const run = await createPrivateEvidenceRun({ root, scopeId: "cleanup-scope" });
  const screenshot = await reserveEvidenceFile(run, "cleanup");
  fs.writeFileSync(screenshot, PNG_BYTES);
  await sealManagedScreenshot(screenshot);

  const moved = `${run.path}-moved`;
  fs.renameSync(run.path, moved);
  fs.mkdirSync(run.path, { mode: 0o700 });
  const sentinel = path.join(run.path, "sentinel.txt");
  fs.writeFileSync(sentinel, "replacement", { mode: 0o600 });

  assert.equal(await cleanupPrivateEvidenceRun(run), false);
  assert.equal(fs.readFileSync(sentinel, "utf8"), "replacement");
  assert.equal(fs.existsSync(path.join(moved, path.basename(screenshot))), true);
});

test("exclusive reservation rolls back random, open, lstat, and capacity failures", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "review-provider-faults-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  await certifyEvidenceRoots(root);
  const baseline = evidenceCapacitySnapshot();

  const originalRandomBytes = crypto.randomBytes;
  crypto.randomBytes = () => { throw new Error("injected random failure"); };
  try {
    await assert.rejects(
      () => createPrivateEvidenceRun({ root, scopeId: "fault-random" }),
      (error) => error.code === "REVIEW_EVIDENCE_UNAVAILABLE"
        && error.cause && error.cause.message === "injected random failure",
    );
  } finally {
    crypto.randomBytes = originalRandomBytes;
  }
  assert.deepEqual(evidenceCapacitySnapshot(), baseline);
  assert.deepEqual(fs.readdirSync(root), []);

  const openRun = await createPrivateEvidenceRun({ root, scopeId: "fault-open" });
  const beforeOpen = evidenceCapacitySnapshot();
  const originalOpen = fs.promises.open;
  fs.promises.open = async function injectedOpen(file, flags, ...args) {
    if ((flags & fs.constants.O_CREAT) !== 0) throw new Error("injected open failure");
    return originalOpen.call(fs.promises, file, flags, ...args);
  };
  try {
    await assert.rejects(() => reserveEvidenceFile(openRun, "open"), (error) => (
      error.code === "REVIEW_EVIDENCE_UNAVAILABLE"
    ));
  } finally {
    fs.promises.open = originalOpen;
  }
  assert.equal(evidenceCapacitySnapshot().active_files, beforeOpen.active_files);
  assert.deepEqual(fs.readdirSync(openRun.path), []);
  await cleanupPrivateEvidenceRun(openRun);

  const lstatRun = await createPrivateEvidenceRun({ root, scopeId: "fault-lstat" });
  const beforeLstat = evidenceCapacitySnapshot();
  const originalLstat = fs.promises.lstat;
  let injected = false;
  fs.promises.lstat = async function injectedLstat(file, ...args) {
    if (!injected && String(file).endsWith(".png")) {
      injected = true;
      throw new Error("injected lstat failure");
    }
    return originalLstat.call(fs.promises, file, ...args);
  };
  try {
    await assert.rejects(() => reserveEvidenceFile(lstatRun, "lstat"), (error) => (
      error.code === "REVIEW_EVIDENCE_UNAVAILABLE"
    ));
  } finally {
    fs.promises.lstat = originalLstat;
  }
  assert.equal(
    evidenceCapacitySnapshot().active_files,
    beforeLstat.active_files + 1,
    "a post-create failure keeps capacity until whole-run tombstoning",
  );
  assert.equal(fs.readdirSync(lstatRun.path).length, 1, "failed reservation is preserved for tombstoning");
  await cleanupPrivateEvidenceRun(lstatRun);

  const capacityRun = await createPrivateEvidenceRun({ root, scopeId: "fault-capacity" });
  for (let index = evidenceCapacitySnapshot().active_files; index < 512; index += 1) {
    await reserveEvidenceFile(capacityRun, `capacity-${index}`);
  }
  await assert.rejects(
    () => reserveEvidenceFile(capacityRun, "capacity-overflow"),
    (error) => error.code === "REVIEW_EVIDENCE_CAPACITY_EXHAUSTED",
  );
  await cleanupPrivateEvidenceRun(capacityRun);
  assert.deepEqual(evidenceCapacitySnapshot(), baseline);
});

test("wrong magic and oversized evidence fail before they can become provider input", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "review-provider-invalid-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  await certifyEvidenceRoots(root);

  const wrongRun = await createPrivateEvidenceRun({ root, scopeId: "wrong-magic" });
  const wrong = await reserveEvidenceFile(wrongRun, "wrong");
  fs.writeFileSync(wrong, Buffer.from("not-an-image"));
  await assert.rejects(
    () => sealManagedScreenshot(wrong),
    (error) => error.code === "REVIEW_EVIDENCE_INVALID",
  );
  await cleanupPrivateEvidenceRun(wrongRun);

  const oversizedRun = await createPrivateEvidenceRun({ root, scopeId: "oversized" });
  const oversized = await reserveEvidenceFile(oversizedRun, "oversized");
  const descriptor = fs.openSync(oversized, "r+");
  try {
    fs.writeSync(descriptor, PNG_BYTES, 0, PNG_BYTES.length, 0);
    fs.ftruncateSync(descriptor, 25 * 1024 * 1024 + 1);
  } finally {
    fs.closeSync(descriptor);
  }
  await assert.rejects(
    () => sealManagedScreenshot(oversized),
    (error) => error.code === "REVIEW_EVIDENCE_INVALID",
  );
  await cleanupPrivateEvidenceRun(oversizedRun);
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

test("Claude one-shot uses the tool-free sandbox, strips ambient secrets, and enforces an independent budget", async () => {
  const fake = fakeChildFactory(({ child }) => {
    child.stdout.write(`${JSON.stringify({
      type: "result",
      result: "bounded summary",
      usage: { input_tokens: 120, output_tokens: 8, untrusted_future_key: "not retained" },
      total_cost_usd: 0.01,
    })}\n`);
    child.emit("close", 0, null);
  });
  const sandboxCalls = [];
  const sandboxedSpawnImpl = (bin, args, _cwd, options) => {
    sandboxCalls.push({ bin, args, options });
    return { cmd: "/usr/bin/bwrap", args: ["--sandboxed", ...args] };
  };
  const env = {
    PATH: process.env.PATH,
    LANG: "C.UTF-8",
    NODE_ENV: "production",
    AGENT_SANDBOX_AUTH_ROOT: "/run/test-auth",
    STUDIO_ACCESS_TOKEN: "must-not-reach-child",
    POSTGRES_URL: "postgres://must-not-reach-child",
    QDRANT_API_KEY: "must-not-reach-child",
    ANTHROPIC_API_KEY: "must-not-reach-child",
    ANTHROPIC_AUTH_TOKEN: "must-not-reach-child",
    CLAUDE_CODE_OAUTH_TOKEN: "must-not-reach-child",
  };
  let observedAccounting = null;

  const result = await oneshotText("summarize this", {
    provider: "claude",
    model: "claude-opus-4-8",
    env,
    spawnImpl: fake.spawnImpl,
    sandboxedSpawnImpl,
    maxBudgetUsd: 0.05,
    onAccounting(value) { observedAccounting = value; },
  });

  assert.equal(result, "bounded summary");
  assert.deepEqual(observedAccounting, {
    provider: "claude",
    model: "claude-opus-4-8",
    usage: { input_tokens: 120, output_tokens: 8 },
    costUsd: 0.01,
    maxBudgetUsd: 0.05,
  });
  assert.equal(sandboxCalls.length, 1);
  assert.equal(sandboxCalls[0].options.provider, "claude");
  assert.equal(fake.calls.length, 1);
  assert.equal(fake.calls[0].binary, "/usr/bin/bwrap");
  const budgetAt = fake.calls[0].args.indexOf("--max-budget-usd");
  assert.notEqual(budgetAt, -1);
  assert.equal(fake.calls[0].args[budgetAt + 1], "0.05");
  const toolsAt = fake.calls[0].args.indexOf("--tools");
  assert.notEqual(toolsAt, -1);
  assert.equal(fake.calls[0].args[toolsAt + 1], "");
  assert.equal(fake.calls[0].options.cwd, "/");
  assert.equal(fake.calls[0].options.env.HOME, "/home/simworld-agent");
  assert.equal(fake.calls[0].options.env.CLAUDE_CONFIG_DIR, "/run/simworld-agent-auth/claude");
  for (const key of [
    "STUDIO_ACCESS_TOKEN",
    "POSTGRES_URL",
    "QDRANT_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
  ]) {
    assert.equal(fake.calls[0].options.env[key], undefined, `${key} must not reach the child`);
  }
});

test("Claude one-shot fails closed on missing or excessive usage/cost metadata", async (t) => {
  const cases = [
    {
      name: "missing usage",
      event: { total_cost_usd: 0.01 },
      code: "LLM_ONESHOT_USAGE_INVALID",
      accountingKnown: false,
    },
    {
      name: "missing cost",
      event: { usage: { input_tokens: 10, output_tokens: 2 } },
      code: "LLM_ONESHOT_COST_INVALID",
      accountingKnown: false,
    },
    {
      name: "cost exceeds budget",
      event: { usage: { input_tokens: 10, output_tokens: 2 }, total_cost_usd: 0.051 },
      code: "LLM_ONESHOT_COST_LIMIT_EXCEEDED",
      accountingKnown: true,
    },
    {
      name: "cache-inclusive input exceeds limit",
      event: {
        usage: { input_tokens: 4, output_tokens: 2, cache_read_input_tokens: 7 },
        total_cost_usd: 0.01,
      },
      maxInputTokens: 10,
      code: "LLM_ONESHOT_USAGE_LIMIT_EXCEEDED",
      accountingKnown: true,
    },
  ];

  for (const fixture of cases) {
    await t.test(fixture.name, async () => {
      const fake = fakeChildFactory(({ child }) => {
        child.stdout.write(`${JSON.stringify({
          type: "result",
          result: "summary",
          ...fixture.event,
        })}\n`);
        child.emit("close", 0, null);
      });
      const accounting = [];
      await assert.rejects(
        oneshotText("summarize this", {
          provider: "claude",
          model: "claude-opus-4-8",
          env: {},
          spawnImpl: fake.spawnImpl,
          sandboxedSpawnImpl: passthroughSandbox,
          maxBudgetUsd: 0.05,
          onAccounting(value) { accounting.push(value); },
          ...(fixture.maxInputTokens ? { maxInputTokens: fixture.maxInputTokens } : {}),
        }),
        (error) => {
          assert.ok(error instanceof LlmOneShotError);
          assert.equal(error.code, fixture.code);
          assert.equal(error.providerAttempted, true);
          assert.equal(error.accountingKnown, fixture.accountingKnown);
          return true;
        },
      );
      assert.equal(accounting.length, fixture.accountingKnown ? 1 : 0);
      if (accounting.length) assert.equal(accounting[0].costUsd, fixture.event.total_cost_usd);
    });
  }
});

test("Claude and Codex one-shots terminate when a process stream exceeds its hard byte ceiling", async (t) => {
  const cases = [
    {
      name: "Claude stdout",
      provider: "claude",
      stream: "stdout",
      bytes: 2 * 1024 * 1024 + 1,
      code: "LLM_ONESHOT_OUTPUT_LIMIT_EXCEEDED",
    },
    {
      name: "Claude stderr",
      provider: "claude",
      stream: "stderr",
      bytes: 64 * 1024 + 1,
      code: "LLM_ONESHOT_DIAGNOSTICS_LIMIT_EXCEEDED",
    },
    {
      name: "Codex stdout",
      provider: "codex",
      stream: "stdout",
      bytes: 2 * 1024 * 1024 + 1,
      code: "LLM_ONESHOT_OUTPUT_LIMIT_EXCEEDED",
    },
    {
      name: "Codex stderr",
      provider: "codex",
      stream: "stderr",
      bytes: 64 * 1024 + 1,
      code: "LLM_ONESHOT_DIAGNOSTICS_LIMIT_EXCEEDED",
    },
  ];

  for (const fixture of cases) {
    await t.test(fixture.name, async () => {
      const fake = fakeChildFactory(({ child }) => {
        child[fixture.stream].write(Buffer.alloc(fixture.bytes));
      });
      await assert.rejects(
        oneshotText("summarize this", {
          provider: fixture.provider,
          model: fixture.provider === "claude" ? "claude-opus-4-8" : "gpt-5.5",
          env: { NODE_ENV: "test" },
          spawnImpl: fake.spawnImpl,
          ...(fixture.provider === "claude" ? { sandboxedSpawnImpl: passthroughSandbox } : {}),
        }),
        (error) => {
          assert.ok(error instanceof LlmOneShotError);
          assert.equal(error.code, fixture.code);
          assert.equal(error.provider, fixture.provider);
          return true;
        },
      );
      assert.deepEqual(fake.calls[0].child.kills, ["SIGTERM"]);
      fake.calls[0].child.emit("close", null, "SIGTERM");
    });
  }
});

test("Codex one-shots fail closed before spawn for summarizer and production workloads", async (t) => {
  const cases = [
    { name: "summarizer", telemetryComponent: "summarizer", env: { NODE_ENV: "test" } },
    { name: "production retrieval", telemetryComponent: "retrieval", env: { NODE_ENV: "production" } },
  ];

  for (const fixture of cases) {
    await t.test(fixture.name, async () => {
      let spawnCalls = 0;
      await assert.rejects(
        oneshotText("summarize this", {
          provider: "codex",
          model: "gpt-5.5",
          telemetryComponent: fixture.telemetryComponent,
          env: fixture.env,
          spawnImpl() {
            spawnCalls += 1;
            throw new Error("must not spawn");
          },
        }),
        (error) => {
          assert.ok(error instanceof LlmOneShotError);
          assert.equal(error.code, "LLM_ONESHOT_PROVIDER_DISABLED");
          assert.equal(error.provider, "codex");
          assert.equal(error.providerAttempted, false);
          return true;
        },
      );
      assert.equal(spawnCalls, 0);
    });
  }
});

test("Claude one-shot distinguishes a proven asynchronous failed exec from an attempted provider process", async () => {
  const fake = fakeChildFactory(({ child }) => {
    child.emit("error", Object.assign(new Error("spawn ENOENT"), { code: "ENOENT" }));
  }, { emitSpawn: false });
  await assert.rejects(
    oneshotText("summarize this", {
      provider: "claude",
      model: "claude-opus-4-8",
      env: {},
      spawnImpl: fake.spawnImpl,
      sandboxedSpawnImpl: passthroughSandbox,
    }),
    (error) => {
      assert.ok(error instanceof LlmOneShotError);
      assert.equal(error.code, "LLM_ONESHOT_SPAWN_FAILED");
      assert.equal(error.providerAttempted, false);
      assert.equal(error.accountingKnown, false);
      return true;
    },
  );
  assert.equal(fake.calls.length, 1);
});

test("Claude one-shot commits valid terminal accounting before reporting a paid process failure", async () => {
  const fake = fakeChildFactory(({ child }) => {
    child.stdout.write(`${JSON.stringify({
      type: "result",
      is_error: true,
      result: "provider failed",
      usage: { input_tokens: 20, output_tokens: 3 },
      total_cost_usd: 0.02,
    })}\n`);
    child.emit("close", 1, null);
  });
  const accounting = [];
  await assert.rejects(
    oneshotText("summarize this", {
      provider: "claude",
      model: "claude-opus-4-8",
      env: {},
      spawnImpl: fake.spawnImpl,
      sandboxedSpawnImpl: passthroughSandbox,
      maxBudgetUsd: 0.05,
      onAccounting(value) { accounting.push(value); },
    }),
    (error) => {
      assert.ok(error instanceof LlmOneShotError);
      assert.equal(error.code, "LLM_ONESHOT_PROCESS_FAILED");
      assert.equal(error.providerAttempted, true);
      assert.equal(error.accountingKnown, true);
      return true;
    },
  );
  assert.equal(accounting.length, 1);
  assert.equal(accounting[0].costUsd, 0.02);
});

test("Claude one-shot preserves terminal accounting when abort wins the race with close", async () => {
  let markTerminalWritten;
  const terminalWritten = new Promise((resolve) => { markTerminalWritten = resolve; });
  const fake = fakeChildFactory(({ child }) => {
    child.stdout.write(JSON.stringify({
      type: "result",
      result: "completed before abort",
      usage: { input_tokens: 12, output_tokens: 2 },
      total_cost_usd: 0.015,
    }));
    markTerminalWritten();
  });
  const controller = new AbortController();
  const accounting = [];
  const pending = oneshotText("summarize this", {
    provider: "claude",
    model: "claude-opus-4-8",
    env: {},
    spawnImpl: fake.spawnImpl,
    sandboxedSpawnImpl: passthroughSandbox,
    signal: controller.signal,
    onAccounting(value) { accounting.push(value); },
  });
  await terminalWritten;
  controller.abort();
  await assert.rejects(pending, (error) => {
    assert.equal(error.code, "LLM_ONESHOT_ABORTED");
    assert.equal(error.providerAttempted, true);
    assert.equal(error.accountingKnown, true);
    return true;
  });
  assert.equal(accounting.length, 1);
  assert.equal(accounting[0].costUsd, 0.015);
  fake.calls[0].child.emit("close", null, "SIGTERM");
  assert.equal(accounting.length, 1);
});

test("Claude one-shot preserves terminal accounting when timeout wins the race with close", async () => {
  const fake = fakeChildFactory(({ child }) => {
    child.stdout.write(JSON.stringify({
      type: "result",
      result: "completed before timeout",
      usage: { input_tokens: 14, output_tokens: 3 },
      total_cost_usd: 0.016,
    }));
  });
  const accounting = [];
  const pending = oneshotText("summarize this", {
    provider: "claude",
    model: "claude-opus-4-8",
    env: {},
    spawnImpl: fake.spawnImpl,
    sandboxedSpawnImpl: passthroughSandbox,
    timeoutMs: 15,
    onAccounting(value) { accounting.push(value); },
  });
  await assert.rejects(pending, (error) => {
    assert.equal(error.code, "LLM_ONESHOT_TIMEOUT");
    assert.equal(error.providerAttempted, true);
    assert.equal(error.accountingKnown, true);
    return true;
  });
  assert.equal(accounting.length, 1);
  assert.equal(accounting[0].costUsd, 0.016);
  fake.calls[0].child.emit("close", null, "SIGTERM");
  assert.equal(accounting.length, 1);
});

test("Claude and Codex one-shots terminate on AbortSignal with a settled typed failure", async () => {
  for (const providerName of ["claude", "codex"]) {
    const fake = fakeChildFactory(() => {});
    const controller = new AbortController();
    const pending = oneshotText("summarize this", {
      provider: providerName,
      model: providerName === "claude" ? "claude-opus-4-8" : "gpt-5.5",
      spawnImpl: fake.spawnImpl,
      ...(providerName === "claude" ? { sandboxedSpawnImpl: passthroughSandbox } : {}),
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
    sandboxedSpawnImpl: passthroughSandbox,
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

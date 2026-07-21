"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  MAX_PROBE_TIMEOUT_MS,
  createReadinessRegistry,
  readinessHttpStatus,
} = require("../readiness-registry");

const FIXED_TIME = Date.parse("2026-07-14T08:00:00.000Z");

function disabledExcept(feature, policy = "required") {
  return {
    review: "disabled",
    retrieval: "disabled",
    streaming: "disabled",
    timeline: "disabled",
    [feature]: policy,
  };
}

test("liveness is process-local and never invokes dependency probes", () => {
  let calls = 0;
  const registry = createReadinessRegistry({
    featurePolicy: {
      review: "required",
      retrieval: "required",
      streaming: "required",
      timeline: "required",
    },
    probes: {
      review: () => { calls += 1; return { status: "ready" }; },
      retrieval: () => { calls += 1; return { status: "ready" }; },
      streaming: () => { calls += 1; return { status: "ready" }; },
      timeline: () => { calls += 1; return { status: "ready" }; },
    },
    revision: {
      build: "caf6d93",
      accessToken: "must-not-appear",
      nested: { connectionString: "postgresql://user:pass@db/assets", schema: 3 },
    },
    now: () => FIXED_TIME,
  });

  const report = registry.getLiveness();
  assert.equal(calls, 0);
  assert.deepEqual(report, {
    schema: "simworld-readiness/v1",
    kind: "liveness",
    status: "live",
    live: true,
    checked_at: "2026-07-14T08:00:00.000Z",
    revision: { build: "caf6d93", nested: { schema: 3 } },
  });
  assert.doesNotMatch(JSON.stringify(report), /must-not-appear|postgresql|user:pass/);
});

test("optional outages degrade observability without blocking readiness", async () => {
  let disabledProbeCalls = 0;
  const registry = createReadinessRegistry({
    featurePolicy: {
      review: "required",
      retrieval: "optional",
      streaming: "disabled",
      timeline: "optional",
    },
    probes: {
      review: async () => ({
        status: "ready",
        revision: { provider: "claude", model: "claude-opus-4-8" },
      }),
      retrieval: async () => ({
        status: "not_ready",
        revision: { snapshot_id: "assets-r7" },
        causes: [{
          code: "CATALOG_MISSING",
          message: "The versioned asset catalog is not mounted.",
          dependency: "catalog",
          retryable: false,
        }],
      }),
      streaming: () => {
        disabledProbeCalls += 1;
        return { status: "ready" };
      },
      timeline: async () => ({ status: "degraded", revision: "runtime-v1" }),
    },
    now: () => FIXED_TIME,
  });

  const report = await registry.getReadiness();
  assert.equal(disabledProbeCalls, 0);
  assert.equal(report.status, "degraded");
  assert.equal(report.ready, true);
  assert.equal(readinessHttpStatus(report), 200);
  assert.equal(report.features.review.status, "ready");
  assert.equal(report.features.review.blocking, false);
  assert.equal(report.features.retrieval.status, "not_ready");
  assert.equal(report.features.retrieval.blocking, false);
  assert.deepEqual(report.features.retrieval.revision, { snapshot_id: "assets-r7" });
  assert.equal(report.features.streaming.status, "disabled");
  assert.equal(report.features.timeline.causes[0].code, "FEATURE_DEGRADED");
  assert.deepEqual(
    report.causes.map(({ feature, code, blocking }) => ({ feature, code, blocking })),
    [
      { feature: "retrieval", code: "CATALOG_MISSING", blocking: false },
      { feature: "timeline", code: "FEATURE_DEGRADED", blocking: false },
    ],
  );
});

test("a required degraded or unavailable feature fails readiness", async () => {
  const degraded = createReadinessRegistry({
    featurePolicy: disabledExcept("review"),
    probes: { review: () => ({ status: "degraded" }) },
    now: () => FIXED_TIME,
  });
  const degradedReport = await degraded.getReadiness();
  assert.equal(degradedReport.status, "not_ready");
  assert.equal(degradedReport.ready, false);
  assert.equal(degradedReport.features.review.blocking, true);
  assert.equal(readinessHttpStatus(degradedReport), 503);

  const missing = createReadinessRegistry({
    featurePolicy: disabledExcept("retrieval"),
    now: () => FIXED_TIME,
  });
  const missingReport = await missing.getReadiness();
  assert.equal(missingReport.ready, false);
  assert.equal(missingReport.features.retrieval.causes[0].code, "PROBE_NOT_CONFIGURED");
  assert.equal(missingReport.causes[0].blocking, true);
});

test("probe output is reduced to a secret-safe public contract", async () => {
  const registry = createReadinessRegistry({
    featurePolicy: disabledExcept("retrieval"),
    probes: {
      retrieval: () => ({
        status: "not_ready",
        revision: {
          snapshot_id: "assets-r8",
          postgresUrl: "postgresql://asset-user:db-password@db.example/assets",
          access_token: "top-secret-token",
          nested: { qdrant_uri: "https://qdrant.example", schema_version: 4 },
        },
        causes: [{
          code: "POSTGRES_UNAVAILABLE",
          message: "postgresql://asset-user:db-password@db.example/assets token=top-secret-token refused",
          retryable: true,
          details: { password: "top-secret-token" },
        }],
        credentials: { token: "top-secret-token" },
      }),
    },
    revision: { build: "caf6d93", apiKey: "top-secret-token" },
    now: () => FIXED_TIME,
  });

  const report = await registry.getReadiness();
  const serialized = JSON.stringify(report);
  assert.equal(report.features.retrieval.causes[0].code, "POSTGRES_UNAVAILABLE");
  assert.equal(report.features.retrieval.causes[0].retryable, true);
  assert.deepEqual(report.features.retrieval.revision, {
    nested: { schema_version: 4 },
    snapshot_id: "assets-r8",
  });
  assert.deepEqual(report.revision, { build: "caf6d93" });
  assert.match(report.features.retrieval.causes[0].message, /\[redacted\]/);
  assert.doesNotMatch(
    serialized,
    /top-secret-token|db-password|db\.example|asset-user|credentials|postgresUrl|qdrant_uri|apiKey/,
  );
});

test("thrown probe errors never expose raw error messages", async () => {
  const registry = createReadinessRegistry({
    featurePolicy: disabledExcept("review"),
    probes: {
      review: async () => {
        throw new Error("Bearer raw-provider-token rejected by https://provider.example/private");
      },
    },
    now: () => FIXED_TIME,
  });

  const report = await registry.getReadiness();
  assert.deepEqual(report.features.review.causes, [{
    code: "PROBE_FAILED",
    message: "The readiness probe failed.",
    retryable: true,
  }]);
  assert.doesNotMatch(JSON.stringify(report), /raw-provider-token|provider\.example|private/);
});

test("each injected probe is bounded by its configured timeout", async () => {
  let probeSawAbort = false;
  const registry = createReadinessRegistry({
    featurePolicy: disabledExcept("streaming"),
    defaultTimeoutMs: 50,
    probes: {
      streaming: {
        timeoutMs: 15,
        probe: ({ signal, feature, timeoutMs }) => {
          assert.equal(feature, "streaming");
          assert.equal(timeoutMs, 15);
          return new Promise((resolve) => {
            signal.addEventListener("abort", () => {
              probeSawAbort = true;
              resolve({ status: "ready" });
            }, { once: true });
          });
        },
      },
    },
  });

  const startedAt = Date.now();
  const report = await registry.getReadiness();
  const elapsed = Date.now() - startedAt;
  assert.equal(report.features.streaming.causes[0].code, "PROBE_TIMEOUT");
  assert.equal(report.features.streaming.blocking, true);
  assert.equal(probeSawAbort, true);
  assert.ok(elapsed < 500, `probe timeout should be bounded, took ${elapsed}ms`);
});

test("a caller AbortSignal cancels in-flight probes with a structured result", async () => {
  let probeSawAbort = false;
  const controller = new AbortController();
  const registry = createReadinessRegistry({
    featurePolicy: disabledExcept("timeline"),
    probes: {
      timeline: ({ signal }) => new Promise((resolve) => {
        signal.addEventListener("abort", () => {
          probeSawAbort = true;
          resolve({ status: "ready" });
        }, { once: true });
      }),
    },
  });

  const pending = registry.getReadiness({ signal: controller.signal });
  await new Promise((resolve) => setImmediate(resolve));
  controller.abort(new Error("route closed"));
  const report = await pending;
  assert.equal(probeSawAbort, true);
  assert.equal(report.features.timeline.causes[0].code, "PROBE_ABORTED");
  assert.equal(report.ready, false);
});

test("malformed probe results fail closed", async () => {
  for (const invalidResult of [
    null,
    true,
    { ready: true },
    { status: "unknown" },
    { status: "not_ready", causes: "not-an-array" },
    { status: "ready", causes: [{ code: "UPSTREAM_ERROR" }] },
  ]) {
    const registry = createReadinessRegistry({
      featurePolicy: disabledExcept("review"),
      probes: { review: () => invalidResult },
      now: () => FIXED_TIME,
    });
    const report = await registry.getReadiness();
    assert.equal(report.ready, false);
    assert.equal(report.features.review.causes[0].code, "PROBE_RESULT_INVALID");
  }
});

test("enabled probes start concurrently", async () => {
  const started = [];
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const probes = Object.fromEntries(["review", "retrieval", "streaming", "timeline"].map((feature) => [
    feature,
    async () => {
      started.push(feature);
      await gate;
      return { status: "ready", revision: `${feature}-r1` };
    },
  ]));
  const registry = createReadinessRegistry({
    featurePolicy: {
      review: "required",
      retrieval: "required",
      streaming: "required",
      timeline: "required",
    },
    probes,
    defaultTimeoutMs: 1_000,
    now: () => FIXED_TIME,
  });

  const pending = registry.getReadiness();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(started.sort(), ["retrieval", "review", "streaming", "timeline"]);
  release();
  const report = await pending;
  assert.equal(report.status, "ready");
  assert.equal(report.ready, true);
  assert.equal(readinessHttpStatus(report), 200);
});

test("configuration validation rejects unbounded or ambiguous policy", async () => {
  assert.throws(
    () => createReadinessRegistry({ featurePolicy: { review: "best_effort" } }),
    /required, optional, or disabled/,
  );
  assert.throws(
    () => createReadinessRegistry({ featurePolicy: { unknown: "required" } }),
    /Unknown readiness feature/,
  );
  assert.throws(
    () => createReadinessRegistry({ probes: { unknown: () => ({ status: "ready" }) } }),
    /Unknown readiness probe/,
  );
  assert.throws(
    () => createReadinessRegistry({ probes: { review: { probe: true } } }),
    /must be a function/,
  );
  assert.throws(
    () => createReadinessRegistry({ defaultTimeoutMs: MAX_PROBE_TIMEOUT_MS + 1 }),
    /must be an integer/,
  );
  assert.equal(readinessHttpStatus(null), 503);

  const registry = createReadinessRegistry();
  await assert.rejects(() => registry.getReadiness({ signal: {} }), /AbortSignal/);
});

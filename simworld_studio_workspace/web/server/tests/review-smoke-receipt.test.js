"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  ReviewSmokeReceiptError,
  createReviewSmokeReceipt,
  digestReviewScene,
  readReviewSmokeReceipt,
  verifyReviewSmokeReceipt,
  writeReviewSmokeReceiptAtomic,
} = require("../review-smoke-receipt");

const NOW = Date.parse("2026-07-21T04:00:00.000Z");
const DIGEST = digestReviewScene({ actors: [{ name: "Chair", transform: { x: 1, y: 2, z: 0 } }] });

function smokeInput(overrides = {}) {
  return {
    receiptId: "smoke-20260721-001",
    reviewType: "visual",
    provider: "claude",
    model: "claude-opus-4-8",
    cliName: "claude-code",
    cliVersion: "2.1.17",
    sourceRevision: "f275794a337034b7d755e8ae26e74b0242c508f1",
    recordedAt: new Date(NOW).toISOString(),
    expiresAt: new Date(NOW + 60 * 60 * 1000).toISOString(),
    sceneDigestBefore: DIGEST,
    sceneDigestAfter: DIGEST,
    usage: { input_tokens: 321, output_tokens: 42, cost_usd: 0.019 },
    verdict: {
      status: "PASS",
      issues: [],
      suggestions: ["Keep the current layout."],
      raw_notes: "Provider prose is intentionally not persisted.",
    },
    ...overrides,
  };
}

test("fake coordinator creates a minimal versioned provider receipt without retaining provider prose", () => {
  let providerCalls = 0;
  const fakeCoordinator = ({ providerResult, before, after }) => {
    providerCalls += 1;
    return createReviewSmokeReceipt(smokeInput({
      sceneDigestBefore: digestReviewScene(before),
      sceneDigestAfter: digestReviewScene(after),
      usage: providerResult.usage,
      verdict: providerResult.verdict,
    }));
  };
  const scene = { lighting: "day", actors: [{ transform: { z: 0, x: 1 }, name: "Chair" }] };
  const receipt = fakeCoordinator({
    before: scene,
    after: { actors: [{ name: "Chair", transform: { x: 1, z: 0 } }], lighting: "day" },
    providerResult: {
      usage: { input_tokens: 12, output_tokens: 7, cost_usd: null },
      verdict: {
        status: "NEEDS_IMPROVEMENT",
        issues: ["Chair is dark."],
        suggestions: ["Increase fill light."],
        raw_notes: "Bearer must-never-be-written",
      },
    },
  });

  assert.equal(providerCalls, 1, "the fake is local and no external provider is invoked");
  assert.equal(receipt.schema, "simworld-review-smoke-receipt/v1");
  assert.deepEqual(receipt.verdict, {
    status: "NEEDS_IMPROVEMENT",
    issues_count: 1,
    suggestions_count: 1,
    schema_valid: true,
  });
  assert.equal(JSON.stringify(receipt).includes("must-never-be-written"), false);
  assert.equal(receipt.scene.digest_before, receipt.scene.digest_after);
});

test("atomic receipt persistence round-trips a validated 0600 file", (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "simworld-review-smoke-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const file = path.join(directory, "nested", "receipt.json");
  const receipt = createReviewSmokeReceipt(smokeInput());

  assert.equal(writeReviewSmokeReceiptAtomic(file, receipt), path.resolve(file));
  assert.deepEqual(readReviewSmokeReceipt(file), receipt);
  assert.equal(fs.statSync(file).mode & 0o777, 0o600);
  assert.deepEqual(fs.readdirSync(path.dirname(file)), ["receipt.json"]);
});

test("receipt input rejects secret fields and persisted receipts reject credential-like values", () => {
  assert.throws(
    () => createReviewSmokeReceipt({ ...smokeInput(), api_key: "sk-ant-secret" }),
    (error) => error instanceof ReviewSmokeReceiptError && error.code === "REVIEW_SMOKE_RECEIPT_SENSITIVE",
  );
  assert.throws(
    () => createReviewSmokeReceipt(smokeInput({ sourceRevision: "sk-ant-secret-material" })),
    (error) => error instanceof ReviewSmokeReceiptError && error.code === "REVIEW_SMOKE_RECEIPT_SENSITIVE",
  );
});

test("Visual receipts fail closed when the read-only scene digest changes", () => {
  assert.throws(
    () => createReviewSmokeReceipt(smokeInput({ sceneDigestAfter: "b".repeat(64) })),
    (error) => error instanceof ReviewSmokeReceiptError && error.code === "REVIEW_SMOKE_SCENE_MUTATED",
  );
  const textReceipt = createReviewSmokeReceipt(smokeInput({
    reviewType: "text",
    sceneDigestAfter: "b".repeat(64),
  }));
  assert.equal(textReceipt.review_type, "text");
});

test("receipt verification binds provider, model, revision, type, and expiry", () => {
  const receipt = createReviewSmokeReceipt(smokeInput());
  assert.equal(verifyReviewSmokeReceipt(receipt, {
    provider: "claude",
    model: "claude-opus-4-8",
    sourceRevision: "f275794a337034b7d755e8ae26e74b0242c508f1",
    reviewType: "visual",
    now: NOW + 1,
  }), receipt);

  for (const expectations of [
    { provider: "other" },
    { model: "claude-opus-4-9" },
    { sourceRevision: "different" },
    { reviewType: "text" },
  ]) {
    assert.throws(
      () => verifyReviewSmokeReceipt(receipt, { ...expectations, now: NOW + 1 }),
      (error) => error.code === "REVIEW_SMOKE_RECEIPT_MISMATCH",
    );
  }
  assert.throws(
    () => verifyReviewSmokeReceipt(receipt, { now: NOW + 60 * 60 * 1000 }),
    (error) => error.code === "REVIEW_SMOKE_RECEIPT_EXPIRED",
  );
});

test("scene digest is deterministic for object key order and rejects non-JSON values", () => {
  assert.equal(
    digestReviewScene({ b: 2, a: { z: true, x: null } }),
    digestReviewScene({ a: { x: null, z: true }, b: 2 }),
  );
  assert.throws(() => digestReviewScene({ invalid: undefined }), /not JSON serializable/);
  const cycle = {};
  cycle.self = cycle;
  assert.throws(() => digestReviewScene(cycle), /cycle/);
});

import assert from "node:assert/strict";
import test from "node:test";

import {
  buildChatStopPayload,
  mergeReviewAccounting,
  mergeReviewEvidence,
  normalizeLoopMode,
  reviewEvidenceUrls,
} from "../../src/features/chat/chatRuntime.js";

test("normalizes persisted review modes to a safe value", () => {
  assert.equal(normalizeLoopMode("vanilla"), "vanilla");
  assert.equal(normalizeLoopMode("text_loop"), "text_loop");
  assert.equal(normalizeLoopMode("visual_loop"), "visual_loop");
  assert.equal(normalizeLoopMode("unexpected"), "vanilla");
  assert.equal(normalizeLoopMode(null), "vanilla");
});

test("normalizes and deduplicates scoped opaque visual evidence", () => {
  const reference = {
    schema: "simworld-review-evidence-ref/v1",
    scope_digest: "a".repeat(64),
    evidence_id: `sha256:${"b".repeat(64)}`,
    handle: `evidence-${"c".repeat(48)}`,
  };
  const urls = reviewEvidenceUrls({
    screenshotRef: reference,
    screenshotRefs: [reference],
    screenshotUrls: ["/api/screenshot/file?path=%2Ftmp%2Fmust-not-leak.png"],
    paths: ["/tmp/must-not-leak.png"],
  }, "/api", "conversation-a");

  assert.deepEqual(urls, [
    `/api/review-evidence/${reference.handle}?evidenceId=sha256%3A${"b".repeat(64)}&conversationId=conversation-a`,
  ]);
});

test("merges multi-shot and verdict evidence without duplicate cards", () => {
  const front = {
    schema: "simworld-review-evidence-ref/v1",
    scope_digest: "a".repeat(64),
    evidence_id: `sha256:${"b".repeat(64)}`,
    handle: `evidence-${"c".repeat(48)}`,
  };
  const side = {
    ...front,
    evidence_id: `sha256:${"d".repeat(64)}`,
    handle: `evidence-${"e".repeat(48)}`,
  };
  const shots = mergeReviewEvidence([], {
    round: 2,
    evidence: [front, side],
  }, "/api", "conversation-a");
  const merged = mergeReviewEvidence(shots, {
    round: 2,
    screenshotRefs: [front, side],
  }, "/api", "conversation-a");

  assert.deepEqual(merged, [
    {
      round: 2,
      url: `/api/review-evidence/${front.handle}?evidenceId=sha256%3A${"b".repeat(64)}&conversationId=conversation-a`,
    },
    {
      round: 2,
      url: `/api/review-evidence/${side.handle}?evidenceId=sha256%3A${"d".repeat(64)}&conversationId=conversation-a`,
    },
  ]);
});

test("builds a stop payload from the exact active run identity", () => {
  assert.deepEqual(
    buildChatStopPayload({
      conversationId: "conversation-7",
      runId: "run-9",
      sessionId: "session-request-key",
    }, "newer-sse-session"),
    {
      conversationId: "conversation-7",
      runId: "run-9",
      sessionId: "session-request-key",
    },
  );
  assert.deepEqual(buildChatStopPayload(null, null), { sessionId: "_global" });
});

test("merges authoritative review budget snapshots without double-counting events", () => {
  const builder = mergeReviewAccounting(null, {
    cost_usd: 0.12,
    budget: {
      limit_usd: 2,
      spent_usd: 0.12,
      remaining_usd: 1.88,
      exhausted: false,
      stages: {
        builder: { cost_usd: 0.12 },
        critic: { cost_usd: 0 },
      },
    },
  }, "builder");
  const critic = mergeReviewAccounting(builder, {
    costUsd: 0.03,
    review_budget: {
      limitUsd: 2,
      spentUsd: 0.15,
      remainingUsd: 1.85,
      exhausted: false,
      stages: {
        builder: { costUsd: 0.12 },
        critic: { costUsd: 0.03 },
      },
    },
  }, "critic");
  const done = mergeReviewAccounting(critic, {
    review: {
      budget: {
        limit_usd: 2,
        spent_usd: 0.15,
        remaining_usd: 1.85,
        exhausted: false,
        stages: {
          builder: { cost_usd: 0.12 },
          critic: { cost_usd: 0.03 },
        },
      },
    },
  }, "run");

  assert.deepEqual(done, {
    limitUsd: 2,
    spentUsd: 0.15,
    remainingUsd: 1.85,
    builderCostUsd: 0.12,
    criticCostUsd: 0.03,
    exhausted: false,
  });
  assert.deepEqual(mergeReviewAccounting(done, { cost_usd: 0.15 }, "run"), done);
});

test("review accounting ignores missing, negative, and non-numeric values", () => {
  const current = { limitUsd: 1, spentUsd: 0.2, remainingUsd: 0.8 };
  assert.deepEqual(mergeReviewAccounting(current, {
    cost_usd: -1,
    budget: {
      limit_usd: "2",
      spent_usd: Number.NaN,
      remaining_usd: -0.1,
    },
  }, "critic"), current);
  assert.equal(mergeReviewAccounting(null, {}, "run"), null);
});

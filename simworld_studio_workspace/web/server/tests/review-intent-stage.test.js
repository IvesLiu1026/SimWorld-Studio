"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const { createReviewBudget } = require("../review-budget");
const {
  deterministicIntentFallback,
  runReviewIntentStage,
  summarizerMaxBudget,
} = require("../review-intent-stage");

test("summarizer is capped by aggregate remaining budget and records exact cost", async () => {
  const budget = createReviewBudget({ limitUsd: 0.03, minimumStageUsd: 0.01 }, {});
  const result = await runReviewIntentStage({
    priorSummary: "prior",
    newPrompt: "new",
    model: "claude-opus-4-8",
    provider: "claude",
    env: { LLM_ONESHOT_MAX_BUDGET_USD: "0.05" },
    reviewBudget: budget,
    async updateIntentSummary({ maxBudgetUsd, onAccounting }) {
      assert.equal(maxBudgetUsd, 0.03);
      onAccounting({
        provider: "claude",
        model: "claude-opus-4-8",
        usage: { input_tokens: 10, output_tokens: 4 },
        costUsd: 0.012345,
      });
      return "updated intent";
    },
  });

  assert.equal(result.status, "updated");
  assert.equal(result.summary, "updated intent");
  assert.equal(result.budget.spent_usd, 0.012345);
  assert.deepEqual(result.budget.stages.summarizer, {
    observations: 1,
    cost_usd: 0.012345,
    usage: {
      input_tokens: 10,
      output_tokens: 4,
      cache_creation_input_tokens: 0,
      cache_read_input_tokens: 0,
    },
  });
});

test("summarizer budget floors its CLI cap so it cannot exceed aggregate remaining money", () => {
  const budget = createReviewBudget({ limitUsd: 0.019999, minimumStageUsd: 0.01 }, {});
  assert.equal(summarizerMaxBudget(budget, { LLM_ONESHOT_MAX_BUDGET_USD: "0.05" }), 0.0199);
  const operatorBounded = createReviewBudget({ limitUsd: 1, minimumStageUsd: 0.01 }, {});
  assert.equal(
    summarizerMaxBudget(operatorBounded, { LLM_ONESHOT_MAX_BUDGET_USD: "0.050051" }),
    0.05,
  );
});

test("only a proven pre-provider spawn failure may use deterministic fallback", async () => {
  const budget = createReviewBudget({ limitUsd: 1 }, {});
  const result = await runReviewIntentStage({
    priorSummary: "prior",
    newPrompt: "new",
    reviewBudget: budget,
    async updateIntentSummary() {
      throw Object.assign(new Error("sandbox auth is unavailable"), {
        code: "LLM_ONESHOT_SPAWN_FAILED",
        providerAttempted: false,
      });
    },
  });

  assert.equal(result.status, "fallback");
  assert.equal(result.summary, deterministicIntentFallback("prior", "new"));
  assert.equal(result.budget.spent_usd, 0);
  assert.equal(result.budget.stages.summarizer.observations, 0);
});

test("unknown accounting after a possible provider attempt fails before builder work", async () => {
  const budget = createReviewBudget({ limitUsd: 1 }, {});
  const result = await runReviewIntentStage({
    newPrompt: "new",
    reviewBudget: budget,
    async updateIntentSummary() {
      throw Object.assign(new Error("provider stream ended"), {
        code: "LLM_ONESHOT_PROTOCOL_ERROR",
        providerAttempted: true,
        accountingKnown: false,
      });
    },
  });

  assert.equal(result.status, "failed");
  assert.equal(result.reason, "builder_error");
  assert.equal(result.error.code, "LLM_ONESHOT_PROTOCOL_ERROR");
  assert.equal(result.budget.spent_usd, 0);
});

test("known paid overspend remains in the aggregate snapshot before fail-closed termination", async () => {
  const budget = createReviewBudget({ limitUsd: 0.05 }, {});
  const result = await runReviewIntentStage({
    newPrompt: "new",
    reviewBudget: budget,
    async updateIntentSummary({ onAccounting }) {
      onAccounting({
        provider: "claude",
        model: "claude-opus-4-8",
        usage: { input_tokens: 20, output_tokens: 3 },
        costUsd: 0.06,
      });
      throw Object.assign(new Error("provider exceeded cap"), {
        code: "LLM_ONESHOT_COST_LIMIT_EXCEEDED",
        providerAttempted: true,
        accountingKnown: true,
      });
    },
  });

  assert.equal(result.status, "failed");
  assert.equal(result.reason, "builder_error");
  assert.equal(result.error.code, "LLM_ONESHOT_COST_LIMIT_EXCEEDED");
  assert.equal(result.budget.spent_usd, 0.06);
  assert.equal(result.budget.remaining_usd, 0);
  assert.deepEqual(result.budget.stages.summarizer.usage, {
    input_tokens: 20,
    output_tokens: 3,
    cache_creation_input_tokens: 0,
    cache_read_input_tokens: 0,
  });
});

test("a successful-looking summarizer without accounting fails closed", async () => {
  const budget = createReviewBudget({ limitUsd: 1 }, {});
  const result = await runReviewIntentStage({
    newPrompt: "new",
    reviewBudget: budget,
    updateIntentSummary: async () => "unaccounted summary",
  });

  assert.equal(result.status, "failed");
  assert.equal(result.reason, "builder_error");
  assert.equal(result.error.code, "REVIEW_SUMMARIZER_ACCOUNTING_MISSING");
});

test("summarizer accounting rejects missing usage atomically", async () => {
  const budget = createReviewBudget({ limitUsd: 1 }, {});
  const result = await runReviewIntentStage({
    newPrompt: "new",
    reviewBudget: budget,
    async updateIntentSummary({ onAccounting }) {
      onAccounting({ provider: "claude", model: "claude-opus-4-8", costUsd: 0.01 });
      return "summary";
    },
  });

  assert.equal(result.status, "failed");
  assert.equal(result.reason, "builder_error");
  assert.equal(result.budget.spent_usd, 0);
  assert.equal(result.budget.stages.summarizer.observations, 0);
});

test("summarizer accounting rejects malformed usage atomically", async () => {
  const budget = createReviewBudget({ limitUsd: 1 }, {});
  const result = await runReviewIntentStage({
    newPrompt: "new",
    reviewBudget: budget,
    async updateIntentSummary({ onAccounting }) {
      onAccounting({
        provider: "claude",
        model: "claude-opus-4-8",
        usage: { input_tokens: "2", output_tokens: 1 },
        costUsd: 0.01,
      });
      return "summary";
    },
  });

  assert.equal(result.status, "failed");
  assert.equal(result.reason, "builder_error");
  assert.equal(result.budget.spent_usd, 0);
  assert.equal(result.budget.stages.summarizer.observations, 0);
});

test("summarizer cost that leaves no builder minimum fails at round zero without persisting success", async () => {
  const budget = createReviewBudget({ limitUsd: 0.01, minimumStageUsd: 0.01 }, {});
  const result = await runReviewIntentStage({
    newPrompt: "new",
    reviewBudget: budget,
    async updateIntentSummary({ onAccounting }) {
      onAccounting({
        provider: "claude",
        model: "claude-opus-4-8",
        usage: { input_tokens: 2, output_tokens: 1 },
        costUsd: 0.01,
      });
      return "summary";
    },
  });

  assert.equal(result.status, "failed");
  assert.equal(result.reason, "budget_exhausted");
  assert.equal(result.budget.spent_usd, 0.01);
  assert.equal(result.budget.stages.summarizer.observations, 1);
});

test("an exhausted aggregate budget stops before summarizer invocation", async () => {
  const budget = createReviewBudget({ limitUsd: 0.01, minimumStageUsd: 0.01 }, {});
  budget.recordObservedCost({ stage: "builder", round: 1, costUsd: 0.01 });
  let calls = 0;
  const result = await runReviewIntentStage({
    newPrompt: "new",
    reviewBudget: budget,
    updateIntentSummary: async () => { calls += 1; return "must not run"; },
  });

  assert.equal(result.status, "failed");
  assert.equal(result.reason, "budget_exhausted");
  assert.equal(calls, 0);
});

test("an aborted Review reports cancellation before invoking summarizer", async () => {
  const budget = createReviewBudget({ limitUsd: 1 }, {});
  const controller = new AbortController();
  controller.abort();
  let calls = 0;
  const result = await runReviewIntentStage({
    newPrompt: "new",
    reviewBudget: budget,
    signal: controller.signal,
    updateIntentSummary: async () => { calls += 1; return "must not run"; },
  });

  assert.equal(result.status, "failed");
  assert.equal(result.reason, "cancelled");
  assert.equal(calls, 0);
});

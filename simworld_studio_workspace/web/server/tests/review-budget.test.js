"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const {
  ReviewBudgetError,
  ReviewBudgetExhaustedError,
  createReviewBudget,
  createRequestReviewBudget,
  resolveReviewBudgetConfig,
  resolveBuilderStageBudget,
  resolveReviewBudgetLimit,
} = require("../review-budget");

test("budget config resolves explicit values, environment values, and safe defaults", () => {
  assert.deepEqual(resolveReviewBudgetConfig({}, {}), {
    limitUsd: 2,
    minimumStageUsd: 0.01,
  });
  assert.deepEqual(resolveReviewBudgetConfig({}, {
    REVIEW_RUN_MAX_BUDGET_USD: "3.125",
    REVIEW_STAGE_MIN_BUDGET_USD: "0.2",
  }), {
    limitUsd: 3.125,
    minimumStageUsd: 0.2,
  });
  assert.equal(
    resolveReviewBudgetLimit({ limitUsd: 1.75 }, { REVIEW_RUN_MAX_BUDGET_USD: "9" }),
    1.75,
  );
  assert.equal(resolveReviewBudgetLimit("4.5", {}), 4.5);
});

test("budget config rejects non-finite and out-of-range USD limits", () => {
  for (const limitUsd of [0, 0.009, 100.01, "not-money", Infinity]) {
    assert.throws(
      () => resolveReviewBudgetConfig({ limitUsd }, {}),
      (error) => error instanceof ReviewBudgetError && error.code === "REVIEW_BUDGET_INVALID",
    );
  }
  assert.throws(
    () => resolveReviewBudgetConfig({ minimumStageUsd: 0 }, {}),
    (error) => error.code === "REVIEW_BUDGET_INVALID",
  );
});

test("request budgets can lower but never raise the operator ceiling", () => {
  const env = {
    REVIEW_RUN_MAX_BUDGET_USD: "2",
    REVIEW_STAGE_MIN_BUDGET_USD: "0.02",
  };
  assert.deepEqual(createRequestReviewBudget({}, env).snapshot(), {
    limit_usd: 2,
    spent_usd: 0,
    remaining_usd: 2,
    exhausted: false,
    minimum_stage_usd: 0.02,
    stages: {
      summarizer: {
        observations: 0,
        cost_usd: 0,
        usage: {
          input_tokens: 0,
          output_tokens: 0,
          cache_creation_input_tokens: 0,
          cache_read_input_tokens: 0,
        },
      },
      builder: { observations: 0, cost_usd: 0 },
      critic: { observations: 0, cost_usd: 0 },
    },
    rounds: [],
  });
  assert.equal(createRequestReviewBudget({ reviewBudgetUsd: 0.75 }, env).limitUsd, 0.75);
  assert.equal(createRequestReviewBudget({ review_budget_usd: "1.25" }, env).limitUsd, 1.25);
  assert.throws(
    () => createRequestReviewBudget({ reviewBudgetUsd: 2.01 }, env),
    (error) => error.code === "REVIEW_BUDGET_INVALID",
  );
});

test("builder stage budget is bounded by its server-side ceiling", () => {
  assert.equal(resolveBuilderStageBudget({}, {}), 2);
  assert.equal(resolveBuilderStageBudget({}, { REVIEW_RUN_MAX_BUDGET_USD: "3" }), 3);
  assert.equal(resolveBuilderStageBudget({}, {
    REVIEW_RUN_MAX_BUDGET_USD: "3",
    BUILDER_MAX_BUDGET_USD: "0.8",
  }), 0.8);
  assert.equal(resolveBuilderStageBudget({ maxBudgetUsd: 0.35 }, {
    BUILDER_MAX_BUDGET_USD: "0.8",
  }), 0.35);
  assert.throws(
    () => resolveBuilderStageBudget({ max_budget_usd: 0.81 }, { BUILDER_MAX_BUDGET_USD: "0.8" }),
    (error) => error.code === "REVIEW_BUDGET_INVALID",
  );
});

test("observed summarizer, builder, and critic costs aggregate exactly across rounds", () => {
  const budget = createReviewBudget({ limitUsd: 1, minimumStageUsd: 0.05 }, {});
  budget.recordObservedCost({
    stage: "summarizer",
    costUsd: 0.01,
    usage: {
      input_tokens: 10,
      output_tokens: 4,
      cache_creation_input_tokens: 2,
      cache_read_input_tokens: 3,
      untrusted_future_key: "not retained",
    },
  });
  budget.recordObservedCost({ stage: "builder", round: 2, costUsd: 0.100001 });
  budget.recordObservedCost({ stage: "critic", round: 1, costUsd: 0.2 });
  budget.recordObservedCost("builder", 0.099999, { round: 1 });
  budget.recordObservedCost({ stage: "critic", round: 2, costUsd: 0.05 });

  assert.equal(budget.limitUsd, 1);
  assert.equal(budget.spentUsd, 0.46);
  assert.equal(budget.remainingUsd, 0.54);
  assert.equal(budget.exhausted, false);
  assert.deepEqual(budget.snapshot(), {
    limit_usd: 1,
    spent_usd: 0.46,
    remaining_usd: 0.54,
    exhausted: false,
    minimum_stage_usd: 0.05,
    stages: {
      summarizer: {
        observations: 1,
        cost_usd: 0.01,
        usage: {
          input_tokens: 10,
          output_tokens: 4,
          cache_creation_input_tokens: 2,
          cache_read_input_tokens: 3,
        },
      },
      builder: { observations: 2, cost_usd: 0.2 },
      critic: { observations: 2, cost_usd: 0.25 },
    },
    rounds: [
      { round: 1, builder_cost_usd: 0.099999, critic_cost_usd: 0.2, total_cost_usd: 0.299999 },
      { round: 2, builder_cost_usd: 0.100001, critic_cost_usd: 0.05, total_cost_usd: 0.150001 },
    ],
  });
});

test("stage predicate fails before work when its minimum exceeds remaining budget", () => {
  const budget = createReviewBudget({ limitUsd: 0.5, minimumStageUsd: 0.1 }, {});
  budget.recordObservedCost({ stage: "builder", round: 1, costUsd: 0.35 });

  assert.equal(budget.canStartStage({ stage: "critic", round: 1 }), true);
  assert.equal(budget.canStartStage({ stage: "critic", round: 1, minimumUsd: 0.16 }), false);
  assert.throws(
    () => budget.assertCanStartStage({ stage: "critic", round: 1, minimumUsd: 0.16 }),
    (error) => {
      assert.ok(error instanceof ReviewBudgetExhaustedError);
      assert.ok(error instanceof ReviewBudgetError);
      assert.equal(error.code, "REVIEW_BUDGET_EXHAUSTED");
      assert.equal(error.stage, "critic");
      assert.equal(error.round, 1);
      assert.equal(error.minimumUsd, 0.16);
      assert.equal(error.budget.remaining_usd, 0.15);
      assert.deepEqual(error.toJSON(), {
        name: "ReviewBudgetExhaustedError",
        code: "REVIEW_BUDGET_EXHAUSTED",
        message: "Review run budget cannot start critic stage: $0.160000 minimum required",
        retryable: false,
        stage: "critic",
        round: 1,
        minimum_usd: 0.16,
        budget: error.budget,
      });
      return true;
    },
  );
});

test("snapshots and errors never retain caller metadata or secret-bearing results", () => {
  const secret = "postgres://admin:password@private/database";
  const budget = createReviewBudget({ limitUsd: 0.1, secret }, {});
  const first = budget.recordObservedCost({
    stage: "builder",
    round: 1,
    costUsd: 0.1,
    providerResult: { apiKey: secret },
  });

  assert.equal(JSON.stringify(first).includes(secret), false);
  assert.equal(budget.canStartStage({ stage: "critic", round: 1 }), false);
  assert.throws(
    () => budget.assertCanStartStage({ stage: "critic", round: 1 }),
    (error) => {
      assert.equal(JSON.stringify(error.toJSON()).includes(secret), false);
      return error.code === "REVIEW_BUDGET_EXHAUSTED";
    },
  );
});

test("accounting inputs are validated without mutating prior totals", () => {
  const budget = createReviewBudget({ limitUsd: 1 }, {});
  budget.recordObservedCost({ stage: "builder", round: 1, costUsd: 0.2 });
  const before = budget.snapshot();

  for (const request of [
    { stage: "retriever", round: 1, costUsd: 0.1 },
    {
      stage: "summarizer",
      round: 1,
      costUsd: 0.1,
      usage: { input_tokens: 1, output_tokens: 1 },
    },
    { stage: "summarizer", costUsd: 0.1, usage: null },
    { stage: "critic", round: 0, costUsd: 0.1 },
    { stage: "critic", round: 1, costUsd: -0.1 },
    { stage: "critic", round: 1, costUsd: Number.NaN },
  ]) {
    assert.throws(
      () => budget.recordObservedCost(request),
      (error) => error.code === "REVIEW_BUDGET_INVALID",
    );
  }
  assert.deepEqual(budget.snapshot(), before);
});

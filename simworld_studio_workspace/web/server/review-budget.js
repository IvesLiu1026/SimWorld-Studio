"use strict";

// Aggregate, per-run accounting for the builder/critic review loop. Money is
// stored as integer micro-dollars so repeated rounds do not accumulate binary
// floating-point drift.
const DEFAULT_REVIEW_RUN_BUDGET_USD = 2;
const MIN_REVIEW_RUN_BUDGET_USD = 0.01;
const MAX_REVIEW_RUN_BUDGET_USD = 100;
const DEFAULT_STAGE_MINIMUM_USD = 0.01;
const USD_SCALE = 1_000_000;
const STAGES = Object.freeze(["builder", "critic"]);

class ReviewBudgetError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = code === "REVIEW_BUDGET_EXHAUSTED"
      ? "ReviewBudgetExhaustedError"
      : "ReviewBudgetError";
    this.code = code;
    this.retryable = false;
    this.stage = details.stage || null;
    this.round = details.round == null ? null : details.round;
    this.minimumUsd = details.minimumUsd == null ? null : details.minimumUsd;
    this.budget = details.budget || null;
  }

  toJSON() {
    return {
      name: this.name,
      code: this.code,
      message: this.message,
      retryable: this.retryable,
      stage: this.stage,
      round: this.round,
      minimum_usd: this.minimumUsd,
      budget: this.budget,
    };
  }
}

class ReviewBudgetExhaustedError extends ReviewBudgetError {
  constructor({ stage, round, minimumUsd, budget }) {
    super(
      "REVIEW_BUDGET_EXHAUSTED",
      `Review run budget cannot start ${stage} stage: $${minimumUsd.toFixed(6)} minimum required`,
      { stage, round, minimumUsd, budget },
    );
  }
}

function invalid(message) {
  return new ReviewBudgetError("REVIEW_BUDGET_INVALID", message);
}

function usdToMicros(value, {
  field,
  defaultValue,
  minimum = 0,
  maximum = MAX_REVIEW_RUN_BUDGET_USD,
} = {}) {
  const raw = value == null || value === "" ? defaultValue : value;
  const amount = Number(raw);
  if (!Number.isFinite(amount) || amount < minimum || amount > maximum) {
    throw invalid(`${field || "USD amount"} must be between ${minimum} and ${maximum} USD`);
  }
  return Math.round(amount * USD_SCALE);
}

function microsToUsd(value) {
  return Math.round(value) / USD_SCALE;
}

function resolveReviewBudgetConfig(options = {}, env = process.env) {
  const source = options && typeof options === "object" ? options : { limitUsd: options };
  const environment = env && typeof env === "object" ? env : {};
  const limitValue = source.limitUsd !== undefined
    ? source.limitUsd
    : (source.maxBudgetUsd !== undefined
      ? source.maxBudgetUsd
      : environment.REVIEW_RUN_MAX_BUDGET_USD);
  const minimumValue = source.minimumStageUsd !== undefined
    ? source.minimumStageUsd
    : (source.minStageUsd !== undefined
      ? source.minStageUsd
      : environment.REVIEW_STAGE_MIN_BUDGET_USD);

  return Object.freeze({
    limitUsd: microsToUsd(usdToMicros(limitValue, {
      field: "Review run budget",
      defaultValue: DEFAULT_REVIEW_RUN_BUDGET_USD,
      minimum: MIN_REVIEW_RUN_BUDGET_USD,
    })),
    minimumStageUsd: microsToUsd(usdToMicros(minimumValue, {
      field: "Review stage minimum",
      defaultValue: DEFAULT_STAGE_MINIMUM_USD,
      minimum: MIN_REVIEW_RUN_BUDGET_USD,
    })),
  });
}

function resolveReviewBudgetLimit(options = {}, env = process.env) {
  return resolveReviewBudgetConfig(options, env).limitUsd;
}

function normalizeStage(value) {
  const stage = String(value || "").trim().toLowerCase();
  if (!STAGES.includes(stage)) {
    throw invalid(`Review budget stage must be one of: ${STAGES.join(", ")}`);
  }
  return stage;
}

function normalizeRound(value) {
  if (value == null) return null;
  const round = Number(value);
  if (!Number.isSafeInteger(round) || round < 1 || round > 10_000) {
    throw invalid("Review budget round must be an integer between 1 and 10000");
  }
  return round;
}

function normalizeStageRequest(value, minimumUsd) {
  if (value && typeof value === "object") {
    return {
      stage: normalizeStage(value.stage),
      round: normalizeRound(value.round),
      minimumUsd: value.minimumUsd,
    };
  }
  return {
    stage: normalizeStage(value),
    round: null,
    minimumUsd,
  };
}

class ReviewBudget {
  constructor(options = {}, env = process.env) {
    const config = resolveReviewBudgetConfig(options, env);
    this._limitMicros = usdToMicros(config.limitUsd, {
      field: "Review run budget",
      minimum: MIN_REVIEW_RUN_BUDGET_USD,
    });
    this._minimumStageMicros = usdToMicros(config.minimumStageUsd, {
      field: "Review stage minimum",
      minimum: MIN_REVIEW_RUN_BUDGET_USD,
    });
    this._stageMicros = { builder: 0, critic: 0 };
    this._stageObservations = { builder: 0, critic: 0 };
    this._rounds = new Map();
  }

  get limitUsd() {
    return microsToUsd(this._limitMicros);
  }

  get spentUsd() {
    return microsToUsd(this._spentMicros());
  }

  get remainingUsd() {
    return microsToUsd(this._remainingMicros());
  }

  get exhausted() {
    return this._remainingMicros() < this._minimumStageMicros;
  }

  recordObservedCost(value, costUsd, metadata = {}) {
    const request = value && typeof value === "object"
      ? value
      : { stage: value, costUsd, ...metadata };
    const stage = normalizeStage(request.stage);
    const round = normalizeRound(request.round);
    const micros = usdToMicros(request.costUsd, {
      field: `Observed ${stage} cost`,
      minimum: 0,
      maximum: MAX_REVIEW_RUN_BUDGET_USD,
    });

    this._stageMicros[stage] += micros;
    this._stageObservations[stage] += 1;
    if (round != null) {
      const entry = this._rounds.get(round) || { builder: 0, critic: 0 };
      entry[stage] += micros;
      this._rounds.set(round, entry);
    }
    return this.snapshot();
  }

  canStartStage(value, minimumUsd) {
    const request = normalizeStageRequest(value, minimumUsd);
    const minimumMicros = usdToMicros(request.minimumUsd, {
      field: "Review stage minimum",
      defaultValue: microsToUsd(this._minimumStageMicros),
      minimum: MIN_REVIEW_RUN_BUDGET_USD,
    });
    return this._remainingMicros() >= minimumMicros;
  }

  assertCanStartStage(value, minimumUsd) {
    const request = normalizeStageRequest(value, minimumUsd);
    const minimumMicros = usdToMicros(request.minimumUsd, {
      field: "Review stage minimum",
      defaultValue: microsToUsd(this._minimumStageMicros),
      minimum: MIN_REVIEW_RUN_BUDGET_USD,
    });
    if (this._remainingMicros() < minimumMicros) {
      throw new ReviewBudgetExhaustedError({
        stage: request.stage,
        round: request.round,
        minimumUsd: microsToUsd(minimumMicros),
        budget: this.snapshot(),
      });
    }
    return this.snapshot();
  }

  _spentMicros() {
    return this._stageMicros.builder + this._stageMicros.critic;
  }

  _remainingMicros() {
    return Math.max(0, this._limitMicros - this._spentMicros());
  }

  snapshot() {
    const spent = this._spentMicros();
    const remaining = this._remainingMicros();
    const rounds = Array.from(this._rounds.entries())
      .sort(([left], [right]) => left - right)
      .map(([round, costs]) => Object.freeze({
        round,
        builder_cost_usd: microsToUsd(costs.builder),
        critic_cost_usd: microsToUsd(costs.critic),
        total_cost_usd: microsToUsd(costs.builder + costs.critic),
      }));
    return Object.freeze({
      limit_usd: microsToUsd(this._limitMicros),
      spent_usd: microsToUsd(spent),
      remaining_usd: microsToUsd(remaining),
      exhausted: remaining < this._minimumStageMicros,
      minimum_stage_usd: microsToUsd(this._minimumStageMicros),
      stages: Object.freeze({
        builder: Object.freeze({
          observations: this._stageObservations.builder,
          cost_usd: microsToUsd(this._stageMicros.builder),
        }),
        critic: Object.freeze({
          observations: this._stageObservations.critic,
          cost_usd: microsToUsd(this._stageMicros.critic),
        }),
      }),
      rounds: Object.freeze(rounds),
    });
  }
}

function createReviewBudget(options = {}, env = process.env) {
  return new ReviewBudget(options, env);
}

// A request may lower its run budget, but it may never raise the operator's
// REVIEW_RUN_MAX_BUDGET_USD ceiling. Keeping this policy here avoids subtle
// differences between Text and Visual handlers.
function createRequestReviewBudget(request = {}, env = process.env) {
  const serverConfig = resolveReviewBudgetConfig({}, env);
  const requestedValue = request && request.reviewBudgetUsd !== undefined
    ? request.reviewBudgetUsd
    : (request && request.review_budget_usd !== undefined ? request.review_budget_usd : undefined);
  if (requestedValue === undefined || requestedValue === null || requestedValue === "") {
    return new ReviewBudget(serverConfig, {});
  }
  const requestedConfig = resolveReviewBudgetConfig({
    limitUsd: requestedValue,
    minimumStageUsd: serverConfig.minimumStageUsd,
  }, {});
  if (requestedConfig.limitUsd > serverConfig.limitUsd) {
    throw invalid(
      `Requested review budget may not exceed the server limit of ${serverConfig.limitUsd} USD`,
    );
  }
  return new ReviewBudget(requestedConfig, {});
}

function resolveBuilderStageBudget(request = {}, env = process.env) {
  const ceilingValue = env.BUILDER_MAX_BUDGET_USD !== undefined
    ? env.BUILDER_MAX_BUDGET_USD
    : env.REVIEW_RUN_MAX_BUDGET_USD;
  const ceiling = resolveReviewBudgetConfig({ limitUsd: ceilingValue }, {}).limitUsd;
  const requestedValue = request && request.maxBudgetUsd !== undefined
    ? request.maxBudgetUsd
    : (request && request.max_budget_usd !== undefined ? request.max_budget_usd : undefined);
  if (requestedValue === undefined || requestedValue === null || requestedValue === "") return ceiling;
  const requested = resolveReviewBudgetConfig({ limitUsd: requestedValue }, {}).limitUsd;
  if (requested > ceiling) {
    throw invalid(`Requested builder budget may not exceed the server limit of ${ceiling} USD`);
  }
  return requested;
}

module.exports = {
  DEFAULT_REVIEW_RUN_BUDGET_USD,
  DEFAULT_STAGE_MINIMUM_USD,
  MAX_REVIEW_RUN_BUDGET_USD,
  MIN_REVIEW_RUN_BUDGET_USD,
  ReviewBudget,
  ReviewBudgetError,
  ReviewBudgetExhaustedError,
  createReviewBudget,
  createRequestReviewBudget,
  resolveBuilderStageBudget,
  resolveReviewBudgetConfig,
  resolveReviewBudgetLimit,
};

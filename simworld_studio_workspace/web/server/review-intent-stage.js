"use strict";

const {
  DEFAULT_ONESHOT_MAX_BUDGET_USD,
  resolveOneShotBudget,
} = require("./llm-oneshot");
const { updateIntentSummary: defaultUpdateIntentSummary } = require("./intent-summarizer");

const MAX_BUDGET_DECIMALS = 4;
const BUDGET_SCALE = 10 ** MAX_BUDGET_DECIMALS;

class ReviewIntentStageError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = "ReviewIntentStageError";
    this.code = code;
    this.retryable = Boolean(details.retryable);
    this.providerAttempted = details.providerAttempted === true;
    this.accountingKnown = details.accountingKnown === true;
  }
}

function deterministicIntentFallback(priorSummary, newPrompt) {
  const prior = String(priorSummary || "").trim();
  const prompt = String(newPrompt || "").trim();
  return (prior ? `${prior}\n\nNEW: ` : "") + prompt;
}

function summarizerMaxBudget(reviewBudget, env = process.env) {
  if (!reviewBudget || typeof reviewBudget.assertCanStartStage !== "function") {
    throw new ReviewIntentStageError(
      "REVIEW_BUDGET_REQUIRED",
      "Intent summarizer requires the aggregate Review budget",
    );
  }
  reviewBudget.assertCanStartStage({ stage: "summarizer" });
  const runtimeEnv = env && typeof env === "object" ? env : {};
  const operatorCap = resolveOneShotBudget(
    runtimeEnv.LLM_ONESHOT_MAX_BUDGET_USD == null
      ? DEFAULT_ONESHOT_MAX_BUDGET_USD
      : runtimeEnv.LLM_ONESHOT_MAX_BUDGET_USD,
  );
  const bounded = Math.min(operatorCap, reviewBudget.remainingUsd);
  const floored = Math.floor((bounded + Number.EPSILON) * BUDGET_SCALE) / BUDGET_SCALE;
  if (floored < 0.01) {
    reviewBudget.assertCanStartStage({ stage: "summarizer", minimumUsd: 0.01 });
  }
  return floored;
}

function publicError(error, fallbackCode, fallbackMessage) {
  const code = error && typeof error.code === "string" && /^[A-Z0-9_]{2,80}$/.test(error.code)
    ? error.code
    : fallbackCode;
  return Object.freeze({
    code,
    message: fallbackMessage,
    retryable: Boolean(error && error.retryable),
  });
}

async function runReviewIntentStage({
  priorSummary,
  newPrompt,
  model,
  provider,
  timeoutMs,
  signal,
  env,
  reviewBudget,
  updateIntentSummary = defaultUpdateIntentSummary,
}) {
  let maxBudgetUsd;
  let accounting = null;
  let accountingCalls = 0;
  try {
    if (signal && signal.aborted) {
      throw new ReviewIntentStageError(
        "REVIEW_ABORTED",
        "Review run was cancelled before intent summarization",
      );
    }
    maxBudgetUsd = summarizerMaxBudget(reviewBudget, env);
    const summary = String(await updateIntentSummary({
      priorSummary,
      newPrompt,
      model,
      provider,
      timeoutMs,
      signal,
      env,
      maxBudgetUsd,
      onAccounting(observed) {
        accountingCalls += 1;
        if (accountingCalls !== 1) {
          throw new ReviewIntentStageError(
            "REVIEW_SUMMARIZER_ACCOUNTING_DUPLICATE",
            "Intent summarizer returned duplicate accounting",
            { providerAttempted: true },
          );
        }
        const costUsd = observed && observed.costUsd;
        if (typeof costUsd !== "number" || !Number.isFinite(costUsd) || costUsd < 0) {
          throw new ReviewIntentStageError(
            "REVIEW_SUMMARIZER_COST_INVALID",
            "Intent summarizer returned invalid aggregate cost metadata",
            { providerAttempted: true },
          );
        }
        const budgetSnapshot = reviewBudget.recordObservedCost({
          stage: "summarizer",
          costUsd,
          usage: observed.usage,
        });
        accounting = Object.freeze({
          provider: observed.provider || provider || null,
          model: observed.model || model || null,
          usage: budgetSnapshot.stages.summarizer.usage,
          costUsd,
          maxBudgetUsd,
        });
      },
    }) || "").trim();
    if (signal && signal.aborted) {
      throw new ReviewIntentStageError(
        "REVIEW_ABORTED",
        "Review run was cancelled after intent summarization",
        { providerAttempted: true, accountingKnown: accounting !== null },
      );
    }
    if (!accounting || accountingCalls !== 1) {
      throw new ReviewIntentStageError(
        "REVIEW_SUMMARIZER_ACCOUNTING_MISSING",
        "Intent summarizer returned without aggregate accounting",
        { providerAttempted: true },
      );
    }
    if (!summary) {
      throw new ReviewIntentStageError(
        "REVIEW_SUMMARIZER_EMPTY",
        "Intent summarizer returned an empty summary",
        { providerAttempted: true, accountingKnown: true },
      );
    }
    reviewBudget.assertCanStartStage({ stage: "builder", round: 1 });
    return Object.freeze({
      status: "updated",
      summary,
      fallback: false,
      maxBudgetUsd,
      accounting,
      budget: reviewBudget.snapshot(),
    });
  } catch (error) {
    const aborted = Boolean(signal && signal.aborted)
      || ["LLM_ONESHOT_ABORTED", "REVIEW_ABORTED"].includes(error && error.code);
    if (aborted) {
      return Object.freeze({
        status: "failed",
        reason: "cancelled",
        error: publicError(error, "REVIEW_ABORTED", "Review run was cancelled before the builder started"),
        budget: reviewBudget && reviewBudget.snapshot ? reviewBudget.snapshot() : null,
      });
    }
    if (error && error.code === "REVIEW_BUDGET_EXHAUSTED") {
      return Object.freeze({
        status: "failed",
        reason: "budget_exhausted",
        error: publicError(error, "REVIEW_BUDGET_EXHAUSTED", "Review budget cannot fund intent summarization"),
        budget: reviewBudget.snapshot(),
      });
    }
    if (error && error.providerAttempted === false && error.code === "LLM_ONESHOT_SPAWN_FAILED") {
      return Object.freeze({
        status: "fallback",
        summary: deterministicIntentFallback(priorSummary, newPrompt),
        fallback: true,
        fallbackCode: error.code,
        maxBudgetUsd,
        accounting: null,
        budget: reviewBudget.snapshot(),
      });
    }
    return Object.freeze({
      status: "failed",
      reason: "builder_error",
      error: publicError(
        error,
        "REVIEW_SUMMARIZER_FAILED",
        "Intent summarizer failed before the builder could start",
      ),
      budget: reviewBudget && reviewBudget.snapshot ? reviewBudget.snapshot() : null,
    });
  }
}

module.exports = {
  ReviewIntentStageError,
  deterministicIntentFallback,
  runReviewIntentStage,
  summarizerMaxBudget,
};

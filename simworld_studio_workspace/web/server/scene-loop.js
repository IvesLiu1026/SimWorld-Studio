"use strict";
// Build↔Critic orchestrator. Wraps the framework iteration around the existing builder.
// The caller (index.js /api/chat) injects:
//   - builderRunner(roundInput): runs ONE builder turn (spawn claude, stream tool/text events) and resolves
//     with {isError, ...}. The runner is responsible for the per-turn SSE events (tool_start, text, etc.).
//   - emit(eventName, payload): emits SSE events on the active chat stream.
// This module only adds the loop-level events: round_start, builder_done, critic_verdict, loop_done.
const { critique } = require("./scene-critic");
const { getUeBroker } = require("./unreal-bridge");
const { createRequestReviewBudget } = require("./review-budget");
const { validateMaxBudgetUsd } = require("./review-provider");
const { runReviewIntentStage } = require("./review-intent-stage");
const {
  requestInternalSse,
  requireStudioAccessToken,
  resolveReviewContract,
  throwIfAborted,
} = require("./internal-http");
const {
  createInnerReviewRelay,
  sanitizePublicString,
  summarizeBuilderResult,
  wrapPublicReviewEmitter,
} = require("./review-public-events");

// Format the critic's verdict into a "USER FEEDBACK" string that the builder's existing refine prompt
// already knows how to consume ("modify what exists, don't start from scratch").
function formatFeedback({ issues, suggestions }) {
  const i = (issues || []).filter(Boolean);
  const s = (suggestions || []).filter(Boolean);
  const parts = ["The critic reviewed the current scene and flagged the following:"];
  parts.push(i.length ? "Issues:\n" + i.map((x) => "- " + x).join("\n") : "Issues: (none specified)");
  if (s.length) parts.push("Suggested fixes:\n" + s.map((x) => "- " + x).join("\n"));
  parts.push("Please refine the existing scene to address these. Do NOT start from scratch — modify what already exists.");
  return parts.join("\n\n");
}

function failureDetails(error, fallbackCode, fallbackMessage) {
  const rawCode = error && (error.code || error.errorCode);
  const code = typeof rawCode === "string" && /^[A-Z0-9_]{2,80}$/.test(rawCode)
    ? rawCode
    : fallbackCode;
  const rawMessage = error && (error.message || error.error);
  const message = sanitizePublicString(rawMessage || fallbackMessage || "Review operation failed", 500);
  return { code, message, retryable: Boolean(error && error.retryable) };
}

function reportedCost(value, stage) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    const error = new Error(`${stage} did not return valid numeric cost metadata`);
    error.code = "REVIEW_COST_INVALID";
    error.retryable = false;
    throw error;
  }
  return value;
}

function budgetSnapshot(reviewBudget) {
  return reviewBudget ? reviewBudget.snapshot() : null;
}

async function runSceneLoop({
  prompt,
  intentSummary,
  sessionId,
  scopeId,
  maxRounds = 5,
  criticModel,
  criticTimeoutMs = 120000,
  criticProvider,
  criticMaxBudgetUsd,
  criticRunner = critique,
  builderRunner,
  emit,
  signal,
  ueBroker,
  evidenceAdmission,
  reviewBudget,
}) {
  if (typeof builderRunner !== "function") throw new Error("builderRunner is required");
  if (typeof criticRunner !== "function") throw new Error("criticRunner is required");
  emit = wrapPublicReviewEmitter(emit);
  let lastStatus = "NEEDS_IMPROVEMENT";
  let lastIssues = [];
  let lastSuggestions = [];
  let lastScreenshotRef = null;
  let lastBuilderResult = null;
  let lastError = null;
  let reason = "max_iterations";
  let actualRound = 0;

  for (let round = 1; round <= maxRounds; round++) {
    if (signal && signal.aborted) {
      reason = "cancelled";
      lastError = failureDetails({ code: "REVIEW_ABORTED" }, "REVIEW_ABORTED", "Review run was cancelled");
      break;
    }
    actualRound = round;
    if (emit) emit("round_start", { round, max: maxRounds });

    let builderMaxBudgetUsd;
    try {
      if (reviewBudget) {
        reviewBudget.assertCanStartStage({ stage: "builder", round });
        builderMaxBudgetUsd = reviewBudget.remainingUsd;
      }
    } catch (e) {
      lastError = failureDetails(e, "REVIEW_BUDGET_EXHAUSTED", "Review run budget is exhausted");
      if (emit) emit("builder_done", {
        round,
        isError: true,
        error: lastError,
        cost_usd: null,
        budget: budgetSnapshot(reviewBudget),
      });
      reason = "budget_exhausted";
      break;
    }

    // ---- Builder turn ----
    const roundInput = {
      prompt,
      intentSummary,
      feedback: round === 1 ? null : formatFeedback({ issues: lastIssues, suggestions: lastSuggestions }),
      round,
      maxRounds,
      signal,
      maxBudgetUsd: builderMaxBudgetUsd,
    };
    let builderResult;
    try {
      builderResult = await builderRunner(roundInput);
    } catch (e) {
      lastError = failureDetails(e, "BUILDER_REQUEST_FAILED", "Builder request failed");
      if (emit) emit("builder_done", {
        round,
        isError: true,
        error: lastError,
        cost_usd: null,
        budget: budgetSnapshot(reviewBudget),
      });
      reason = signal && signal.aborted ? "cancelled" : "builder_error";
      break;
    }
    lastBuilderResult = summarizeBuilderResult(builderResult);
    // A builder result without an explicit boolean success marker is malformed and unsafe to review.
    let builderErr = !builderResult || builderResult.isError !== false;
    let builderFailure = builderErr
      ? failureDetails(
        builderResult,
        builderResult && builderResult.isError === true ? "BUILDER_REPORTED_ERROR" : "BUILDER_RESULT_INVALID",
        builderResult && builderResult.isError === true
          ? "Inner builder reported a failure"
          : "Inner builder returned a malformed result",
      )
      : null;
    let builderCostUsd = null;
    if (reviewBudget && !builderErr) {
      try {
        builderCostUsd = reportedCost(builderResult.costUsd, "Builder");
        reviewBudget.recordObservedCost({ stage: "builder", round, costUsd: builderCostUsd });
      } catch (e) {
        builderErr = true;
        builderFailure = failureDetails(e, "REVIEW_COST_INVALID", "Builder cost metadata is invalid");
      }
    } else if (builderResult && typeof builderResult.costUsd === "number" && Number.isFinite(builderResult.costUsd)) {
      builderCostUsd = builderResult.costUsd;
    }
    if (emit) emit("builder_done", {
      round,
      isError: builderErr,
      ...(builderFailure ? { error: builderFailure } : {}),
      cost_usd: builderCostUsd,
      budget: budgetSnapshot(reviewBudget),
    });
    if (builderErr) {
      lastError = builderFailure;
      reason = "builder_error";
      break;
    }

    // ---- Critic call ----
    let critic;
    let criticStageBudgetUsd = criticMaxBudgetUsd;
    try {
      throwIfAborted(signal, "Scene critic");
      if (reviewBudget) {
        reviewBudget.assertCanStartStage({ stage: "critic", round });
        criticStageBudgetUsd = Math.min(
          validateMaxBudgetUsd(criticMaxBudgetUsd),
          reviewBudget.remainingUsd,
        );
      }
      critic = await criticRunner({
        originalPrompt: intentSummary || prompt,
        model: criticModel,
        provider: criticProvider,
        timeoutMs: criticTimeoutMs,
        maxBudgetUsd: criticStageBudgetUsd,
        signal,
        ueBroker,
        scopeId: scopeId || sessionId,
        evidenceAdmission,
      });
      if (
        !critic
        || !["PASS", "NEEDS_IMPROVEMENT", "FAIL"].includes(critic.status)
        || !Array.isArray(critic.issues)
        || !Array.isArray(critic.suggestions)
      ) {
        throw new Error("Critic returned an invalid verdict");
      }
      if (reviewBudget) {
        const criticCostUsd = reportedCost(critic.cost_usd, "Critic");
        reviewBudget.recordObservedCost({ stage: "critic", round, costUsd: criticCostUsd });
      }
    } catch (e) {
      lastError = failureDetails(e, "CRITIC_FAILED", "Scene critic failed");
      if (emit) emit("critic_verdict", {
        round,
        status: "FAIL",
        issues: [`Critic error: ${lastError.message}`],
        suggestions: [],
        provider: criticProvider || null,
        model: criticModel || null,
        evidence_ids: [],
        usage: null,
        latency_ms: null,
        error: true,
        code: lastError.code,
        message: lastError.message,
        errorDetails: lastError,
        cost_usd: null,
        budget: budgetSnapshot(reviewBudget),
      });
      reason = signal && signal.aborted
        ? "cancelled"
        : (lastError.code === "REVIEW_BUDGET_EXHAUSTED" ? "budget_exhausted" : "critic_error");
      break;
    }
    lastStatus = critic.status;
    lastIssues = critic.issues || [];
    lastSuggestions = critic.suggestions || [];
    lastScreenshotRef = critic.screenshotRef || null;
    if (emit) emit("critic_verdict", {
      round,
      status: critic.status,
      issues: lastIssues,
      suggestions: lastSuggestions,
      screenshotRef: lastScreenshotRef,
      actorsCount: critic.actorsCount || 0,
      provider: critic.provider || criticProvider || null,
      model: critic.model || criticModel || null,
      evidence_ids: Array.isArray(critic.evidence_ids) ? critic.evidence_ids : [],
      usage: critic.usage || null,
      latency_ms: Number.isFinite(critic.latency_ms) ? critic.latency_ms : null,
      cost_usd: typeof critic.cost_usd === "number" ? critic.cost_usd : null,
      budget: budgetSnapshot(reviewBudget),
    });

    // ---- Stop conditions ----
    if (critic.status === "PASS") { reason = "pass"; break; }
    if (round >= maxRounds) { reason = "max_iterations"; break; }
    if (reviewBudget && !reviewBudget.canStartStage({ stage: "builder", round: round + 1 })) {
      try {
        reviewBudget.assertCanStartStage({ stage: "builder", round: round + 1 });
      } catch (e) {
        lastError = failureDetails(e, "REVIEW_BUDGET_EXHAUSTED", "Review run budget is exhausted");
      }
      reason = "budget_exhausted";
      break;
    }
    // otherwise continue with critic feedback as the next round's input
  }

  const payload = {
    finalStatus: lastStatus,
    rounds: actualRound,
    reason,
    issues: lastIssues,
    suggestions: lastSuggestions,
    latestScreenshot: null,
    latestScreenshotRef: lastScreenshotRef,
    builderResult: lastBuilderResult, // for the caller to thread into the final `done` event
    failureReason: ["builder_error", "critic_error", "budget_exhausted", "cancelled"].includes(reason) ? reason : null,
    error: lastError,
    budget: budgetSnapshot(reviewBudget),
  };
  if (emit) emit("loop_done", payload);
  return payload;
}

// ── /api/chat loop handler ──────────────────────────────────────────────────
// Acts as an SSE relay: invokes /api/chat (with useLoop:false to avoid recursion) once per round,
// pipes inner events (tool_start, text, screenshot, ...) through to the client, and inserts the
// orchestrator's loop-level events (round_start, builder_done, critic_verdict, loop_done).
// This sidesteps any refactor of the existing /api/chat builder spawn — it's a clean additive layer.
async function handleSceneLoop(req, res, deps) {
  const updateIntentSummary = deps.updateIntentSummary || require("./intent-summarizer").updateIntentSummary;
  const requestBody = req.body || {};
  const {
    message,
    sessionId,
    conversationId,
    skills,
    feedback: userFeedback,
    assetMode,
    assetRetrievalMode,
  } = requestBody;
  const assetPolicyFields = Object.fromEntries([
    "require_real_assets",
    "requireRealAssets",
    "asset_degraded_mode",
    "assetDegradedMode",
    "allow_degraded_assets",
    "allowDegradedAssets",
  ].filter((key) => Object.prototype.hasOwnProperty.call(requestBody, key)).map((key) => [key, requestBody[key]]));
  if (!message) { res.status(400).json({ error: "message required" }); return; }

  let contract;
  let accessToken;
  let reviewBudget;
  let criticMaxBudgetUsd;
  try {
    contract = resolveReviewContract(req.body || {}, deps.env || process.env);
    reviewBudget = createRequestReviewBudget(req.body || {}, deps.env || process.env);
    criticMaxBudgetUsd = validateMaxBudgetUsd((deps.env || process.env).CRITIC_MAX_BUDGET_USD);
    accessToken = deps.accessToken == null
      ? requireStudioAccessToken(deps.env || process.env)
      : requireStudioAccessToken({ STUDIO_ACCESS_TOKEN: deps.accessToken });
  } catch (error) {
    res.status(error && error.code === "INTERNAL_AUTH_UNAVAILABLE" ? 503 : 400).json({
      error: String(error && error.message || error),
      code: error && error.code || "INVALID_REVIEW_CONTRACT",
    });
    return;
  }

  res.setHeader("Content-Type", "text/event-stream");
  res.setHeader("Cache-Control", "no-cache");
  res.setHeader("Connection", "keep-alive");
  res.setHeader("X-Accel-Buffering", "no");
  res.flushHeaders();
  const emit = wrapPublicReviewEmitter((name, data) => {
    if (!res.writableEnded) res.write(`event: ${name}\ndata: ${JSON.stringify(data || {})}\n\n`);
  });
  const ping = setInterval(() => { if (!res.writableEnded) res.write(`: ping\n\n`); }, 5000);

  const STUDIO_SESSION = String(sessionId || deps.STUDIO_SESSION);
  // Public conversation/session ids remain UI metadata. In trusted-proxy mode
  // the coordinator supplies a server-derived lease-bound scope for all
  // cancellation, intent, subprocess, and inner-call state.
  const scopeId = String(deps.scopeId || conversationId || STUDIO_SESSION);
  const internalSessionId = String(deps.internalSessionId || STUDIO_SESSION);
  const internalConversationId = String(deps.internalConversationId || conversationId || scopeId);
  const runId = String(deps.runId || (req.body && req.body.runId) || scopeId);
  const port = deps.internalPort || parseInt((deps.env || process.env).PORT || "3002", 10);
  const internalTimeoutMs = deps.internalChatTimeoutMs
    || parseInt((deps.env || process.env).INTERNAL_CHAT_TIMEOUT_MS || "1800000", 10);
  const log = deps.logToFile || (() => {});

  emit("run_start", {
    runId,
    sessionId: STUDIO_SESSION,
    conversationId: conversationId || scopeId,
    mode: "text_loop",
  });

  // 1. Update rolling user-intent summary (recency-wins on contradictions).
  emit("intent_start", {});
  const prior = (deps.intentStore && deps.intentStore.get(scopeId)) || "";
  const intentStage = await runReviewIntentStage({
    priorSummary: prior,
    newPrompt: message,
    model: contract.summarizerModel,
    provider: contract.summarizerProvider,
    timeoutMs: parseInt((deps.env || process.env).SUMMARIZER_TIMEOUT_MS || "60000", 10),
    signal: deps.signal,
    env: deps.env || process.env,
    reviewBudget,
    updateIntentSummary,
  });
  if (intentStage.status === "failed") {
    log("loop", `intent summarizer stopped Review (${intentStage.error.code})`);
    emit("intent_error", { error: intentStage.error, budget: intentStage.budget });
    const failedResult = {
      finalStatus: "FAIL",
      rounds: 0,
      reason: intentStage.reason,
      issues: [],
      suggestions: [],
      latestScreenshot: null,
      latestScreenshotRef: null,
      builderResult: null,
      failureReason: intentStage.reason,
      error: intentStage.error,
      budget: intentStage.budget,
    };
    emit("loop_done", failedResult);
    clearInterval(ping);
    emit("done", {
      sessionId: STUDIO_SESSION,
      conversationId: conversationId || scopeId,
      runId,
      isError: true,
      loop: {
        reason: failedResult.reason,
        rounds: 0,
        finalStatus: "FAIL",
        budget: failedResult.budget,
      },
      failureReason: failedResult.failureReason,
      error: failedResult.error,
      review: {
        builderAgent: contract.builderAgent,
        builderModel: contract.builderModel,
        criticProvider: contract.criticProvider,
        criticModel: contract.criticModel,
        budget: failedResult.budget,
      },
      budget: failedResult.budget,
      latestScreenshot: null,
      latestScreenshotRef: null,
    });
    res.end();
    return;
  }
  const intentSummary = intentStage.summary;
  if (!intentStage.fallback && deps.intentStore) deps.intentStore.set(scopeId, intentSummary);
  emit("intent_updated", {
    summary: intentSummary,
    ...(intentStage.fallback ? { fallback: true } : {}),
    budget: intentStage.budget,
  });
  log("loop", intentStage.fallback
    ? "intent provider unavailable before start — using deterministic fallback"
    : "intent summary updated (" + intentSummary.length + " chars)");

  // 2. Per-round builder runner: POST /api/chat (useLoop:false) and relay SSE events.
  async function builderRunner({ prompt, intentSummary, feedback, round, maxBudgetUsd }) {
    const combinedPrompt =
      `USER INTENT (cumulative across all prior prompts in this session):\n${intentSummary}\n\n` +
      `CURRENT TURN INSTRUCTION:\n${prompt}`;
    // Round 1: use any user-supplied feedback. Round 2+: critic feedback overrides.
    const combinedFeedback = feedback || userFeedback || undefined;
    const body = {
      message: combinedPrompt,
      sessionId: internalSessionId,
      conversationId: internalConversationId,
      runId,
      skills: skills || [],
      feedback: combinedFeedback,
      useLoop: false, // force the inner call to take the existing single-turn path
      dynamicSkills: false,
      agent: contract.builderAgent,
      ...(contract.builderModel ? { model: contract.builderModel } : {}),
      ...(Number.isFinite(maxBudgetUsd) ? { maxBudgetUsd } : {}),
      ...(assetMode ? { assetMode } : {}),
      ...(assetRetrievalMode ? { assetRetrievalMode } : {}),
      ...assetPolicyFields,
    };
    const result = await (deps.requestInternalSse || requestInternalSse)({
      port,
      path: "/api/chat",
      body,
      token: accessToken,
      timeoutMs: internalTimeoutMs,
      signal: deps.signal,
      onEvent: createInnerReviewRelay(emit),
    });
    return {
      isError: result.done.isError,
      error: result.done.error || null,
      errorCode: result.done.errorCode || result.done.code || null,
      retryable: Boolean(result.done.retryable),
      costUsd: result.done.costUsd,
    };
  }

  // 3. Run the loop.
  const result = await runSceneLoop({
    prompt: message, intentSummary, sessionId: scopeId,
    scopeId,
    maxRounds: parseInt((deps.env || process.env).SCENE_LOOP_MAX_ROUNDS || "5", 10),
    criticModel: contract.criticModel,
    criticProvider: contract.criticProvider,
    criticTimeoutMs: parseInt((deps.env || process.env).CRITIC_TIMEOUT_MS || "120000", 10),
    criticMaxBudgetUsd,
    criticRunner: deps.criticRunner || critique,
    builderRunner,
    emit,
    signal: deps.signal,
    ueBroker: deps.ueBroker || getUeBroker(),
    evidenceAdmission: deps.evidenceAdmission,
    reviewBudget,
  });

  // 4. Final SSE done event (closes the stream).
  clearInterval(ping);
  const fatalReasons = new Set(["builder_error", "critic_error", "budget_exhausted", "cancelled"]);
  emit("done", {
    sessionId: STUDIO_SESSION,
    conversationId: conversationId || scopeId,
    runId,
    isError: fatalReasons.has(result.reason),
    loop: { reason: result.reason, rounds: result.rounds, finalStatus: result.finalStatus, budget: result.budget },
    failureReason: result.failureReason,
    error: result.error,
    review: {
      builderAgent: contract.builderAgent,
      builderModel: contract.builderModel,
      criticProvider: contract.criticProvider,
      criticModel: contract.criticModel,
      budget: result.budget,
    },
    budget: result.budget,
    latestScreenshot: null,
    latestScreenshotRef: result.latestScreenshotRef || null,
  });
  res.end();
}

module.exports = { runSceneLoop, formatFeedback, handleSceneLoop };

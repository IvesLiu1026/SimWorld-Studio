"use strict";
// VISUAL scene-critic loop. Per round it captures read-only visual evidence, asks a VLM critic for
// a verdict, and threads the verdict + evidence path to the next builder round. Capture must never
// modify actors, lighting, ground, or the editor camera merely to improve the review image.
const path = require("path");
const { getUeBroker } = require("./unreal-bridge");
const {
  cleanupPrivateEvidenceRun,
  createPrivateEvidenceRun,
  openReviewEvidenceAdmission,
  managedEvidenceStat,
  reserveEvidenceFile,
  retainPrivateEvidenceRun,
  screenshotEvidence,
  sealManagedScreenshot,
} = require("./scene-critic");
const { createRequestReviewBudget } = require("./review-budget");
const { review: reviewWithProvider, validateMaxBudgetUsd } = require("./review-provider");
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

const ARENA_ROOT = path.resolve(__dirname, "..", "..");
const VISUAL_DIR = path.join(ARENA_ROOT, "tmp", "review-evidence", "visual");
const MAX_VISUAL_SCREENSHOTS = 8;
const MAX_VISUAL_EVIDENCE_BYTES = 64 * 1024 * 1024;

const CRITIC_SYSTEM_PROMPT_MULTI = `You are a 3D scene verification expert for SimWorld Studio (Unreal Engine 5).
You will be shown one or more read-only screenshots of the SAME scene. Use all provided visual evidence and the actor snapshot to evaluate the scene.

Evaluate:
1. Completeness: Are all requested objects present? (Cross-check across views — something hidden in one view may be visible in another.)
2. Placement: Are objects in good positions? Spatial arrangement looks right?
3. Scale: Do objects look appropriately sized relative to each other across all views?
4. Realism: Does the scene match the original request?
5. Issues: Any obvious problems (floating, buried, misaligned, upside-down, dark)?
6. Layout: Is the spatial arrangement clear and intentional? Are there obvious gaps or clusters?

Return the strict review verdict object requested by the response schema:
- status: PASS, NEEDS_IMPROVEMENT, or FAIL
- issues: specific problems (empty array when none)
- suggestions: specific actionable improvements
- raw_notes: concise supporting notes`;

// ── Read-only visual capture ────────────────────────────────────────────────
// Multi-angle capture used to reposition the editor camera and rebuild lighting/ground. That made
// the review itself mutate the scene. Until UE exposes snapshot-only named cameras, Visual Review
// captures the current viewport once through the process-wide UE broker.
const VIEW_CONFIGS = [
  { name: "current", kind: "current_viewport" },
];

function abortableDelay(delayMs, signal) {
  throwIfAborted(signal, "Visual capture");
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      if (signal) signal.removeEventListener("abort", onAbort);
      resolve();
    }, delayMs);
    const onAbort = () => {
      clearTimeout(timer);
      if (signal) signal.removeEventListener("abort", onAbort);
      try { throwIfAborted(signal, "Visual capture"); } catch (error) { reject(error); }
    };
    if (signal) {
      signal.addEventListener("abort", onAbort, { once: true });
      if (signal.aborted) onAbort();
    }
  });
}

async function waitForFileStable(file, timeoutMs = 45000, signal) {
  const start = process.hrtime.bigint();
  let last = -1;
  let same = 0;
  while (Number(process.hrtime.bigint() - start) / 1e6 < timeoutMs) {
    throwIfAborted(signal, "Visual capture");
    const st = await managedEvidenceStat(file, { allowEmpty: true, signal });
    if (st.size > 0) {
      if (st.size === last) same += 1;
      else { last = st.size; same = 0; }
      if (same >= 2) return true;
    }
    await abortableDelay(500, signal);
  }
  return false;
}

async function multiViewScreenshot({
  round,
  destDir,
  evidenceRun,
  evidenceAdmission,
  scopeId,
  ueBroker,
  signal,
}) {
  throwIfAborted(signal, "Visual capture");
  const broker = ueBroker || getUeBroker();
  if (!broker || typeof broker.send !== "function") throw new Error("Shared UE broker is unavailable");
  const ownsRun = !evidenceRun && !evidenceAdmission;
  const run = evidenceRun || (evidenceAdmission
    ? await openReviewEvidenceAdmission(evidenceAdmission, { signal })
    : await createPrivateEvidenceRun({
      root: destDir || VISUAL_DIR,
      scopeId,
      ueBroker: broker,
      signal,
    }));
  try {
    const view = VIEW_CONFIGS[0];
    const roundNumber = Number(round);
    if (!Number.isSafeInteger(roundNumber) || roundNumber < 1 || roundNumber > 100) {
      throw new Error("Visual capture round is invalid");
    }
    const filepath = await reserveEvidenceFile(
      run,
      `round${roundNumber}-${view.name}`,
      { signal },
    );
    await broker.send(
      "take_screenshot",
      { filepath },
      { timeoutMs: 30000, queueDeadlineMs: 45000, signal },
    );
    const stable = await waitForFileStable(filepath, 45000, signal);
    if (!stable) throw new Error("Read-only viewport screenshot was not created");
    throwIfAborted(signal, "Visual capture");
    const evidence = await sealManagedScreenshot(filepath, { signal });
    return [{
      name: view.name,
      path: filepath,
      evidenceId: evidence.evidenceId,
      evidenceRef: evidence.reference,
    }];
  } catch (error) {
    // Preserve the capture failure if best-effort cleanup also fails. Once an
    // abort has reached this transport boundary, expose the stable transport
    // code rather than an evidence-probe implementation detail.
    if (ownsRun) {
      try {
        await cleanupPrivateEvidenceRun(run);
      } catch (_cleanupError) {
        // The original failure remains authoritative.
      }
    }
    throwIfAborted(signal, "Visual capture");
    throw error;
  }
}

// ── Multi-image critic ─────────────────────────────────────────────────────
async function getActorsSnapshot({ ueBroker, signal } = {}) {
  if (signal && signal.aborted) throw new Error("Visual actor snapshot aborted");
  const broker = ueBroker || getUeBroker();
  if (!broker || typeof broker.send !== "function") throw new Error("Shared UE broker is unavailable");
  const reply = await broker.send(
    "get_actors_in_level",
    {},
    { timeoutMs: 15000, queueDeadlineMs: 30000, signal },
  );
  if (signal && signal.aborted) throw new Error("Visual actor snapshot aborted");
  if (!reply || typeof reply !== "object") throw new Error("UE actor snapshot was malformed");
  return reply;
}

async function visualCritique({
  originalPrompt,
  screenshots,
  model,
  maxBudgetUsd,
  timeoutMs = 180000,
  provider,
  runner,
  ueBroker,
  signal,
  reviewProvider = reviewWithProvider,
}) {
  const validScreens = (screenshots || []).filter((shot) => shot && shot.path);
  if (!validScreens.length || validScreens.length !== (screenshots || []).length
      || validScreens.length > MAX_VISUAL_SCREENSHOTS) {
    throw new Error("Visual review has no complete managed screenshot set");
  }
  const evidence = [];
  let totalEvidenceBytes = 0;
  for (const shot of validScreens) {
    const item = await screenshotEvidence(shot.path, { signal });
    totalEvidenceBytes += item.data.length;
    if (totalEvidenceBytes > MAX_VISUAL_EVIDENCE_BYTES) {
      const error = new Error("Visual review evidence exceeds the total byte limit");
      error.code = "REVIEW_EVIDENCE_INVALID";
      throw error;
    }
    evidence.push(item);
  }
  const actors = await getActorsSnapshot({ ueBroker, signal });
  const evidenceIds = evidence.map((item) => item.evidenceId);
  const images = evidence.map((item) => ({ mediaType: item.mediaType, data: item.data }));
  const prompt = [
    `Review ${validScreens.length} read-only screenshot(s) of the same current Unreal scene.`,
    originalPrompt ? `Original scene request: "${originalPrompt}"` : "",
    "Evidence labels:",
    ...validScreens.map((shot, index) => `- image ${index + 1}: ${shot.name}`),
    "Current actors in the scene:",
    JSON.stringify(actors, null, 2),
    "Return only the strict review verdict.",
  ].filter(Boolean).join("\n\n");
  const verdict = await reviewProvider({
    provider: provider || runner || "claude",
    model,
    maxBudgetUsd,
    systemPrompt: CRITIC_SYSTEM_PROMPT_MULTI,
    prompt,
    images,
    evidenceIds,
    timeoutMs,
    signal,
  });
  const actorsCount = (actors && actors.result && actors.result.actors && actors.result.actors.length) || 0;
  return { ...verdict, screenshots: validScreens, actorsCount };
}

// ── Build the feedback string sent to the next round's builder ─────────────
// The VLM critic consumes managed image bytes. The builder receives only the
// resulting text until an audited opaque byte transport is installed; private
// host paths must never cross into the sandboxed builder process.
function formatVisualFeedback({ issues, suggestions, screenshots }) {
  const i = (issues || []).filter(Boolean);
  const s = (suggestions || []).filter(Boolean);
  const parts = ["The critic reviewed the current scene evidence and flagged the following:"];
  parts.push(i.length ? "Issues:\n" + i.map((x) => "- " + x).join("\n") : "Issues: (none specified)");
  if (s.length) parts.push("Suggested fixes:\n" + s.map((x) => "- " + x).join("\n"));
  if (screenshots && screenshots.length) {
    parts.push(
      `The visual critic inspected ${screenshots.length} server-managed scene capture(s). ` +
      "Their private files are intentionally unavailable to the builder. " +
      "Use the grounded issues and suggestions above to refine the existing scene. " +
      "Do NOT start from scratch — modify what already exists."
    );
  } else {
    parts.push("Please refine the existing scene to address these. Do NOT start from scratch — modify what already exists.");
  }
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

// ── Loop orchestrator ──────────────────────────────────────────────────────
async function runVisualSceneLoop({
  prompt,
  intentSummary,
  sessionId,
  maxRounds = 5,
  criticModel,
  criticTimeoutMs = 180000,
  criticProvider,
  criticMaxBudgetUsd,
  criticRunner = visualCritique,
  captureRunner = multiViewScreenshot,
  builderRunner,
  emit,
  destDir,
  evidenceRun,
  evidenceAdmission,
  ueBroker,
  signal,
  reviewBudget,
}) {
  if (typeof builderRunner !== "function") throw new Error("builderRunner is required");
  if (typeof criticRunner !== "function") throw new Error("criticRunner is required");
  if (typeof captureRunner !== "function") throw new Error("captureRunner is required");
  emit = wrapPublicReviewEmitter(emit);
  let lastStatus = "NEEDS_IMPROVEMENT";
  let lastIssues = [];
  let lastSuggestions = [];
  let lastScreenshots = [];
  let lastBuilderResult = null;
  let lastError = null;
  let reason = "max_iterations";
  let actualRound = 0;
  let managedRun = evidenceRun || null;
  let ownsManagedRun = false;
  let retainedManagedRun = false;

  try {
  for (let round = 1; round <= maxRounds; round++) {
    if (signal && signal.aborted) {
      reason = "cancelled";
      lastError = failureDetails({ code: "REVIEW_ABORTED" }, "REVIEW_ABORTED", "Review run was cancelled");
      break;
    }
    actualRound = round;
    if (emit) emit("round_start", { round, max: maxRounds, mode: "visual_loop" });

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
      feedback: round === 1
        ? null
        : formatVisualFeedback({ issues: lastIssues, suggestions: lastSuggestions, screenshots: lastScreenshots }),
      visualFeedbackImages: [],
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

    // ---- Multi-view capture ----
    let shots;
    let captureError = null;
    try {
      throwIfAborted(signal, "Visual capture");
      if (!managedRun && captureRunner === multiViewScreenshot) {
        managedRun = evidenceAdmission
          ? await openReviewEvidenceAdmission(evidenceAdmission, { signal })
          : await createPrivateEvidenceRun({
            root: destDir || VISUAL_DIR,
            scopeId: sessionId,
            ueBroker: ueBroker || getUeBroker(),
            signal,
          });
        ownsManagedRun = !evidenceAdmission;
      }
      shots = await captureRunner({
        round,
        destDir,
        evidenceRun: managedRun,
        evidenceAdmission,
        scopeId: sessionId,
        ueBroker,
        signal,
      });
      if (!Array.isArray(shots)) shots = [];
    } catch (e) {
      shots = [];
      captureError = String(e && e.message || e);
      lastError = failureDetails(e, "VISUAL_CAPTURE_FAILED", "Read-only visual capture failed");
    }
    if (emit) emit("multi_shots", {
      round,
      count: shots.length,
      evidence: shots.map((shot) => shot.evidenceRef).filter(Boolean),
    });
    if (!shots.length) {
      lastStatus = "FAIL";
      if (!lastError) {
        lastError = failureDetails(
          { code: "VISUAL_CAPTURE_FAILED", message: "Read-only visual capture returned no evidence" },
          "VISUAL_CAPTURE_FAILED",
          "Read-only visual capture returned no evidence",
        );
      }
      if (emit) emit("critic_verdict", {
        round,
        status: "FAIL",
        issues: [`Read-only visual capture failed${captureError ? `: ${captureError}` : ""}`],
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
      reason = signal && signal.aborted ? "cancelled" : "critic_error";
      break;
    }

    // ---- Visual critic ----
    let critic;
    let criticStageBudgetUsd = criticMaxBudgetUsd;
    try {
      throwIfAborted(signal, "Visual critic");
      if (reviewBudget) {
        reviewBudget.assertCanStartStage({ stage: "critic", round });
        criticStageBudgetUsd = Math.min(
          validateMaxBudgetUsd(criticMaxBudgetUsd),
          reviewBudget.remainingUsd,
        );
      }
      critic = await criticRunner({
        originalPrompt: intentSummary || prompt,
        screenshots: shots,
        model: criticModel,
        provider: criticProvider,
        timeoutMs: criticTimeoutMs,
        maxBudgetUsd: criticStageBudgetUsd,
        ueBroker,
        signal,
      });
      if (
        !critic
        || !["PASS", "NEEDS_IMPROVEMENT", "FAIL"].includes(critic.status)
        || !Array.isArray(critic.issues)
        || !Array.isArray(critic.suggestions)
      ) {
        throw new Error("Visual critic returned an invalid verdict");
      }
      if (reviewBudget) {
        const criticCostUsd = reportedCost(critic.cost_usd, "Critic");
        reviewBudget.recordObservedCost({ stage: "critic", round, costUsd: criticCostUsd });
      }
    } catch (e) {
      lastError = failureDetails(e, "CRITIC_FAILED", "Visual critic failed");
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
    lastScreenshots = critic.screenshots || shots;
    if (emit) emit("critic_verdict", {
      round,
      status: critic.status,
      issues: lastIssues,
      suggestions: lastSuggestions,
      screenshotRefs: lastScreenshots.map((shot) => shot.evidenceRef).filter(Boolean),
      actorsCount: critic.actorsCount || 0,
      provider: critic.provider || criticProvider || null,
      model: critic.model || criticModel || null,
      evidence_ids: Array.isArray(critic.evidence_ids) ? critic.evidence_ids : [],
      usage: critic.usage || null,
      latency_ms: Number.isFinite(critic.latency_ms) ? critic.latency_ms : null,
      cost_usd: typeof critic.cost_usd === "number" ? critic.cost_usd : null,
      budget: budgetSnapshot(reviewBudget),
    });

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
  }

  const payload = {
    finalStatus: lastStatus,
    rounds: actualRound,
    reason,
    issues: lastIssues,
    suggestions: lastSuggestions,
    latestScreenshots: lastScreenshots,
    builderResult: lastBuilderResult,
    mode: "visual_loop",
    failureReason: ["builder_error", "critic_error", "budget_exhausted", "cancelled"].includes(reason) ? reason : null,
    error: lastError,
    budget: budgetSnapshot(reviewBudget),
  };
  if (emit) emit("loop_done", {
    ...payload,
    latestScreenshots: lastScreenshots.map((shot) => ({
      name: shot.name,
      evidenceId: shot.evidenceId || null,
      evidenceRef: shot.evidenceRef || null,
    })),
  });
  if (managedRun && ownsManagedRun
      && lastScreenshots.some((shot) => shot && shot.evidenceRef)) {
    await retainPrivateEvidenceRun(managedRun);
    retainedManagedRun = true;
  }
  return payload;
  } finally {
    if (managedRun && ownsManagedRun && !retainedManagedRun) {
      await cleanupPrivateEvidenceRun(managedRun);
    }
  }
}

// ── /api/chat handler for visual_loop mode ────────────────────────────────
async function handleVisualSceneLoop(req, res, deps) {
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
  // Public ids are display metadata only. The production coordinator injects
  // a server-derived active-lease scope for cancellation and all inner state.
  const scopeId = String(deps.scopeId || conversationId || STUDIO_SESSION);
  const internalSessionId = String(deps.internalSessionId || STUDIO_SESSION);
  const internalConversationId = String(deps.internalConversationId || conversationId || scopeId);
  const runId = String(deps.runId || (req.body && req.body.runId) || scopeId);
  const port = deps.internalPort || parseInt((deps.env || process.env).PORT || "3004", 10);
  const internalTimeoutMs = deps.internalChatTimeoutMs
    || parseInt((deps.env || process.env).INTERNAL_CHAT_TIMEOUT_MS || "1800000", 10);
  const log = deps.logToFile || (() => {});

  emit("run_start", {
    runId,
    sessionId: STUDIO_SESSION,
    conversationId: conversationId || scopeId,
    mode: "visual_loop",
  });

  emit("intent_start", {});
  const prior = (deps.intentStore && deps.intentStore.get(scopeId)) || "";
  let intentSummary = prior;
  try {
    throwIfAborted(deps.signal, "Intent summarizer");
    intentSummary = await updateIntentSummary({
      priorSummary: prior, newPrompt: message,
      model: contract.summarizerModel,
      provider: contract.summarizerProvider,
      timeoutMs: parseInt((deps.env || process.env).SUMMARIZER_TIMEOUT_MS || "60000", 10),
      signal: deps.signal,
    });
    throwIfAborted(deps.signal, "Intent summarizer");
    if (deps.intentStore) deps.intentStore.set(scopeId, intentSummary);
    emit("intent_updated", { summary: intentSummary });
    log("vloop", "intent summary updated (" + intentSummary.length + " chars)");
  } catch (e) {
    intentSummary = (prior ? prior + "\n\nNEW: " : "") + message;
    log("vloop", "summarizer failed (" + e.message + ") — using fallback intent");
    emit("intent_updated", { summary: intentSummary, fallback: true });
  }

  async function builderRunner({ prompt, intentSummary, feedback, visualFeedbackImages, round, maxBudgetUsd }) {
    const combinedPrompt =
      `USER INTENT (cumulative across all prior prompts in this session):\n${intentSummary}\n\n` +
      `CURRENT TURN INSTRUCTION:\n${prompt}`;
    const combinedFeedback = feedback || userFeedback || undefined;
    const body = {
      message: combinedPrompt,
      sessionId: internalSessionId,
      conversationId: internalConversationId,
      runId,
      skills: skills || [],
      feedback: combinedFeedback,
      visualFeedbackImages: Array.isArray(visualFeedbackImages) ? visualFeedbackImages : [],
      useLoop: false, // route to the existing single-turn path
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

  const result = await runVisualSceneLoop({
    prompt: message,
    intentSummary,
    sessionId: scopeId,
    maxRounds: parseInt((deps.env || process.env).SCENE_LOOP_MAX_ROUNDS || "5", 10),
    criticModel: contract.criticModel,
    criticProvider: contract.criticProvider,
    criticTimeoutMs: parseInt((deps.env || process.env).CRITIC_TIMEOUT_MS || "180000", 10),
    criticMaxBudgetUsd,
    criticRunner: deps.criticRunner || visualCritique,
    captureRunner: deps.captureRunner || multiViewScreenshot,
    builderRunner,
    emit,
    ueBroker: deps.ueBroker || getUeBroker(),
    signal: deps.signal,
    evidenceAdmission: deps.evidenceAdmission,
    reviewBudget,
  });

  clearInterval(ping);
  const fatalReasons = new Set(["builder_error", "critic_error", "budget_exhausted", "cancelled"]);
  emit("done", {
    sessionId: STUDIO_SESSION,
    conversationId: conversationId || scopeId,
    runId,
    isError: fatalReasons.has(result.reason),
    loop: {
      reason: result.reason,
      rounds: result.rounds,
      finalStatus: result.finalStatus,
      mode: "visual_loop",
      budget: result.budget,
    },
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
    latestScreenshotRef: (result.latestScreenshots && result.latestScreenshots[0])
      ? result.latestScreenshots[0].evidenceRef || null
      : null,
  });
  res.end();
}

module.exports = {
  runVisualSceneLoop,
  formatVisualFeedback,
  visualCritique,
  multiViewScreenshot,
  getActorsSnapshot,
  handleVisualSceneLoop,
  VIEW_CONFIGS,
  VISUAL_DIR,
};

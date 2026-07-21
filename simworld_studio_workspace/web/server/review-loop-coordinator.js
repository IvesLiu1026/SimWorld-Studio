"use strict";

const crypto = require("node:crypto");

const { ReviewRunRegistry } = require("./review-run-registry");
const { resolveReviewContract } = require("./internal-http");
const { createRequestReviewBudget } = require("./review-budget");
const { boundedCanonicalJson, normalizeReviewSceneBinding } = require("./review-scene-binding");

const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/;
const TRUSTED_PROFILES = new Set(["trusted_proxy", "trusted-proxy", "public_webrtc", "public-webrtc"]);
const REQUEST_CONTEXT = Symbol("simworld.reviewRequestContext");
const MAX_HELD_TERMINAL_BYTES = 64 * 1024;
const MAX_REVIEW_MESSAGE_BYTES = 1024 * 1024;
const MAX_REVIEW_AUXILIARY_BYTES = 256 * 1024;
const REVIEW_INPUT_CONTRACT_SCHEMA = "simworld-review-input-contract/v1";
const REVIEW_FATAL_REASONS = new Set(["builder_error", "critic_error", "budget_exhausted"]);
const REVIEW_REASONS = new Set([
  "pass", "max_iterations", "builder_error", "critic_error",
  "budget_exhausted", "cancelled", "handler_error",
]);
const REVIEW_VERDICTS = new Set(["PASS", "FAIL", "NEEDS_IMPROVEMENT", "UNKNOWN"]);

class ReviewLoopCoordinatorError extends Error {
  constructor(code, message, statusCode = 400) {
    super(message);
    this.name = "ReviewLoopCoordinatorError";
    this.code = code;
    this.statusCode = statusCode;
  }
}

function fail(code, message, statusCode) {
  throw new ReviewLoopCoordinatorError(code, message, statusCode);
}

function normalizeReviewMode(value) {
  const mode = String(value || "").trim().toLowerCase();
  if (["vanilla", "off", "none"].includes(mode)) return "vanilla";
  if (["text_loop", "loop", "text"].includes(mode)) return "text_loop";
  if (["visual_loop", "visual", "multi_view"].includes(mode)) return "visual_loop";
  return null;
}

function safeId(value, field, { fallback = null } = {}) {
  const id = String(value || "").trim();
  if (!id && fallback !== null) return fallback;
  if (!SAFE_ID.test(id)) fail("REVIEW_SCOPE_INVALID", `${field} is invalid`, 400);
  return id;
}

function requestValue(request, field) {
  const body = request && request.body && typeof request.body === "object" ? request.body : {};
  const query = request && request.query && typeof request.query === "object" ? request.query : {};
  return body[field] !== undefined ? body[field] : query[field];
}

function boundedInputString(value, field, { required = false, maxBytes = MAX_REVIEW_AUXILIARY_BYTES } = {}) {
  if (value === undefined || value === null || value === "") {
    if (required) fail("REVIEW_INPUT_INVALID", `${field} is required`, 400);
    return null;
  }
  if (typeof value !== "string" || (required && value.trim().length === 0)
      || Buffer.byteLength(value, "utf8") > maxBytes) {
    fail("REVIEW_INPUT_INVALID", `${field} is invalid`, 400);
  }
  return value;
}

function reviewInputDigests({ request, mode, scope, dependencies = {} } = {}) {
  const body = request && request.body && typeof request.body === "object" && !Array.isArray(request.body)
    ? request.body : {};
  const env = dependencies.env && typeof dependencies.env === "object"
    ? dependencies.env : process.env;
  const message = boundedInputString(body.message, "message", {
    required: true,
    maxBytes: MAX_REVIEW_MESSAGE_BYTES,
  });
  const feedback = boundedInputString(body.feedback, "feedback");
  const focus = boundedInputString(body.focus, "focus");
  const rawSkills = body.skills === undefined || body.skills === null ? [] : body.skills;
  if (!Array.isArray(rawSkills) || rawSkills.length > 64
      || rawSkills.some((value) => typeof value !== "string" || !SAFE_ID.test(value))) {
    fail("REVIEW_INPUT_INVALID", "skills are invalid", 400);
  }
  const intentValue = dependencies.intentStore && typeof dependencies.intentStore.get === "function"
    ? dependencies.intentStore.get(scope.scopeId) : "";
  const priorIntent = boundedInputString(intentValue || "", "prior intent", {
    maxBytes: MAX_REVIEW_MESSAGE_BYTES,
  }) || "";
  let contract;
  let budget;
  try {
    contract = resolveReviewContract(body, env);
    budget = createRequestReviewBudget(body, env).snapshot();
  } catch (error) {
    fail(
      error && error.code || "REVIEW_INPUT_INVALID",
      String(error && error.message || "Review input contract is invalid"),
      400,
    );
  }
  const optionalPolicy = {};
  for (const key of [
    "assetMode", "assetRetrievalMode", "require_real_assets", "requireRealAssets",
    "asset_degraded_mode", "assetDegradedMode", "allow_degraded_assets", "allowDegradedAssets",
    "skillSelectionMode",
  ]) {
    if (Object.prototype.hasOwnProperty.call(body, key)) optionalPolicy[key] = body[key];
  }
  let canonical;
  try {
    canonical = boundedCanonicalJson({
      schema: REVIEW_INPUT_CONTRACT_SCHEMA,
      mode,
      message,
      feedback,
      focus,
      skills: rawSkills,
      resolved_contract: {
        builder_agent: contract.builderAgent,
        builder_model: contract.builderModel || null,
        critic_provider: contract.criticProvider,
        critic_model: contract.criticModel,
        summarizer_provider: contract.summarizerProvider,
        summarizer_model: contract.summarizerModel,
      },
      budget: {
        limit_usd: budget.limit_usd,
        minimum_stage_usd: budget.minimum_stage_usd,
      },
      policy: optionalPolicy,
    });
  } catch (_error) {
    fail("REVIEW_INPUT_INVALID", "Review input contract is invalid", 400);
  }
  const requestDigest = crypto.createHash("sha256").update(canonical, "utf8").digest("hex");
  const executionCanonical = boundedCanonicalJson({
    schema: "simworld-review-execution-input/v1",
    request_digest: requestDigest,
    prior_intent: priorIntent,
  });
  return Object.freeze({
    requestDigest,
    inputDigest: crypto.createHash("sha256").update(executionCanonical, "utf8").digest("hex"),
  });
}

function activeLeaseIdentity(identity) {
  if (!identity || typeof identity !== "object" || Array.isArray(identity)) return null;
  const ownerId = String(identity.ownerId || "");
  const sessionId = String(identity.sessionId || "");
  const leaseId = String(identity.leaseId || "");
  const slotId = Number(identity.slotId);
  const mcpPort = Number(identity.mcpPort);
  if (!ownerId || !sessionId || !leaseId
      || !Number.isSafeInteger(slotId) || slotId < 0 || slotId > 65535
      || !Number.isSafeInteger(mcpPort) || mcpPort < 1 || mcpPort > 65535) {
    return null;
  }
  return Object.freeze({ ownerId, sessionId, slotId, leaseId, mcpPort });
}

function activeLeaseAuthority(identity) {
  const lease = activeLeaseIdentity(identity);
  return lease
    ? [lease.ownerId, lease.sessionId, lease.slotId, lease.leaseId, lease.mcpPort]
    : null;
}

function scopeWithActiveLease(scope, activeLease, journalAccess) {
  if (activeLease) Object.defineProperty(scope, "activeLease", {
    value: activeLease,
    configurable: false,
    enumerable: false,
    writable: false,
  });
  Object.defineProperty(scope, "journalAccess", {
    value: Object.freeze(journalAccess),
    configurable: false,
    enumerable: false,
    writable: false,
  });
  return Object.freeze(scope);
}

function createReviewScopeResolver({
  transportProfile = "loopback",
  resolveActiveSession,
  loopbackSessionId,
} = {}) {
  const requireActiveLease = TRUSTED_PROFILES.has(String(transportProfile || "").trim().toLowerCase());
  const loopbackAuthority = safeId(loopbackSessionId, "loopbackSessionId", { fallback: "studio-loopback" });
  if (requireActiveLease && typeof resolveActiveSession !== "function") {
    throw new TypeError("trusted-proxy review coordination requires resolveActiveSession");
  }

  return function resolveReviewScope(request) {
    const conversationId = safeId(
      requestValue(request, "conversationId"),
      "conversationId",
      { fallback: "default" },
    );
    let identity = null;
    // Loopback remains a deterministic single-user development scope. Only a
    // trusted/public profile promotes the server-validated lease to authority.
    if (requireActiveLease && typeof resolveActiveSession === "function") {
      try { identity = resolveActiveSession(request); } catch (_error) { identity = null; }
    }
    const activeLease = activeLeaseIdentity(identity);
    const leaseAuthority = activeLeaseAuthority(activeLease);
    if (requireActiveLease && !leaseAuthority) {
      fail(
        "REVIEW_ACTIVE_SESSION_REQUIRED",
        "An active Studio streaming lease is required for Review.",
        401,
      );
    }
    const authority = leaseAuthority || ["loopback", loopbackAuthority];
    const digest = crypto
      .createHash("sha256")
      .update("simworld/review-scope/v1\0")
      .update(JSON.stringify([authority, conversationId]))
      .digest("hex");
    return scopeWithActiveLease({
      scopeId: `review-${digest}`,
      conversationId,
      leaseBound: Boolean(leaseAuthority),
    }, activeLease, activeLease
      ? { ownerId: activeLease.ownerId, sessionId: activeLease.sessionId }
      : { ownerId: loopbackAuthority, sessionId: loopbackAuthority });
  };
}

function explicitModeForRequest(request) {
  const body = request && request.body && typeof request.body === "object" ? request.body : {};
  if (body.loopMode !== undefined) {
    const explicit = normalizeReviewMode(body.loopMode);
    if (!explicit) fail("REVIEW_MODE_INVALID", "loopMode is invalid", 400);
    return explicit;
  }
  if (body.useLoop === false) return "vanilla";
  if (body.useLoop === true) return "text_loop";
  return null;
}

function modeForRequest(request, defaultMode, scope) {
  const explicit = explicitModeForRequest(request);
  if (explicit) return explicit;
  const configured = typeof defaultMode === "function"
    ? defaultMode({ request, scope })
    : defaultMode;
  return normalizeReviewMode(configured) || "vanilla";
}

function createScopedReviewModeStore({
  transportProfile = "loopback",
  defaultMode = "vanilla",
  maxEntries = 1024,
} = {}) {
  const trusted = TRUSTED_PROFILES.has(String(transportProfile || "").trim().toLowerCase());
  const configuredDefault = normalizeReviewMode(defaultMode);
  if (!configuredDefault) throw new TypeError("defaultMode must be a valid Review mode");
  if (!Number.isSafeInteger(maxEntries) || maxEntries < 1 || maxEntries > 10000) {
    throw new TypeError("maxEntries must be an integer between 1 and 10000");
  }
  const scopedModes = new Map();
  let loopbackMode = configuredDefault;

  function requireScope(scope) {
    if (!trusted) return null;
    if (!scope || scope.leaseBound !== true || !SAFE_ID.test(String(scope.scopeId || ""))) {
      fail("REVIEW_ACTIVE_SESSION_REQUIRED", "An active Studio streaming lease is required for Review.", 401);
    }
    return scope.scopeId;
  }

  function get(scope) {
    if (!trusted) return loopbackMode;
    const scopeId = requireScope(scope);
    return scopedModes.get(scopeId) || configuredDefault;
  }

  function set(scope, value) {
    const mode = normalizeReviewMode(value);
    if (!mode) fail("REVIEW_MODE_INVALID", "mode is invalid", 400);
    if (!trusted) {
      loopbackMode = mode;
      return mode;
    }
    const scopeId = requireScope(scope);
    scopedModes.delete(scopeId);
    scopedModes.set(scopeId, mode);
    while (scopedModes.size > maxEntries) scopedModes.delete(scopedModes.keys().next().value);
    return mode;
  }

  function remove(scope) {
    if (!trusted) return false;
    return scopedModes.delete(requireScope(scope));
  }

  return Object.freeze({
    defaultMode: configuredDefault,
    get,
    set,
    delete: remove,
    get size() { return scopedModes.size; },
  });
}

function createLeaseBoundReviewBroker({ scope, resolveUeBroker } = {}) {
  const activeLease = scope && scope.activeLease;
  if (!scope || scope.leaseBound !== true || !activeLease) {
    fail("REVIEW_ACTIVE_SESSION_REQUIRED", "An active Studio streaming lease is required for Review.", 401);
  }
  if (typeof resolveUeBroker !== "function") {
    throw new TypeError("resolveUeBroker is required for lease-bound Review");
  }
  const selected = resolveUeBroker(activeLease);
  if (!selected || typeof selected.send !== "function" || Number(selected.port) !== activeLease.mcpPort) {
    fail("REVIEW_BROKER_LEASE_INVALID", "The active Studio lease has no matching UE broker.", 409);
  }

  return Object.freeze({
    port: activeLease.mcpPort,
    async send(...args) {
      const current = resolveUeBroker(activeLease);
      if (current !== selected || !current || typeof current.send !== "function"
          || Number(current.port) !== activeLease.mcpPort) {
        fail("REVIEW_BROKER_LEASE_INVALID", "The active Studio lease is no longer valid.", 409);
      }
      const response = await current.send(...args);
      const confirmed = resolveUeBroker(activeLease);
      if (confirmed !== selected || !confirmed || typeof confirmed.send !== "function"
          || Number(confirmed.port) !== activeLease.mcpPort) {
        fail("REVIEW_BROKER_LEASE_INVALID", "The active Studio lease was revoked during the UE operation.", 409);
      }
      return response;
    },
  });
}

function setRequestContext(request, context) {
  Object.defineProperty(request, REQUEST_CONTEXT, {
    value: Object.freeze(context),
    configurable: false,
    enumerable: false,
    writable: false,
  });
}

function getReviewRequestContext(request) {
  return request && request[REQUEST_CONTEXT] || null;
}

function parseSseFrame(chunk) {
  const text = Buffer.isBuffer(chunk) ? chunk.toString("utf8") : String(chunk || "");
  const eventLine = text.split(/\r?\n/).find((entry) => entry.startsWith("event:"));
  const event = eventLine ? eventLine.slice(6).trim().toLowerCase() : "";
  const line = text.split(/\r?\n/).find((entry) => entry.startsWith("data:"));
  if (!line) return null;
  try {
    return Object.freeze({ event, payload: JSON.parse(line.slice(5).trim()) });
  } catch (_error) {
    return null;
  }
}

function terminalSummaryFromFrame(chunk) {
  const frame = parseSseFrame(chunk);
  if (!frame || !["done", "loop_done"].includes(frame.event)) return null;
  const payload = frame.payload && typeof frame.payload === "object" && !Array.isArray(frame.payload)
    ? frame.payload : null;
  if (!payload || (frame.event === "done" && typeof payload.isError !== "boolean")) return null;
  const loop = frame.event === "loop_done"
    ? payload
    : (payload && typeof payload.loop === "object" && !Array.isArray(payload.loop)
      ? payload.loop : null);
  if (!loop) return null;
  const rawReason = String(loop.reason || payload.failureReason || "").trim().toLowerCase();
  if (!REVIEW_REASONS.has(rawReason)) return null;
  const rawVerdict = String(loop.finalStatus || "").trim().toUpperCase();
  if (!REVIEW_VERDICTS.has(rawVerdict)) return null;
  const rounds = Number(loop.rounds);
  if (!Number.isSafeInteger(rounds) || rounds < 0 || rounds > 100) return null;
  const errorCode = payload.error && typeof payload.error === "object"
    ? payload.error.code : payload.code;
  return Object.freeze({
    outcome: rawReason === "cancelled" ? "cancelled" : (REVIEW_FATAL_REASONS.has(rawReason) || rawReason === "handler_error" || payload.isError === true ? "failed" : "completed"),
    reason: rawReason,
    finalVerdict: rawVerdict,
    rounds,
    errorCode: typeof errorCode === "string" ? errorCode : null,
  });
}

function sseEventName(chunk) {
  const text = Buffer.isBuffer(chunk) ? chunk.toString("utf8") : String(chunk || "");
  const eventLine = text.split(/\r?\n/).find((entry) => entry.startsWith("event:"));
  return eventLine ? eventLine.slice(6).trim().toLowerCase() : "";
}

function sameTerminalSummary(left, right) {
  return Boolean(left && right
    && left.outcome === right.outcome
    && left.reason === right.reason
    && left.finalVerdict === right.finalVerdict
    && left.rounds === right.rounds
    && left.errorCode === right.errorCode);
}

function reviewEvidenceFromFrame(chunk, prior = null) {
  const frame = parseSseFrame(chunk);
  if (!frame) return prior;
  const payload = frame.payload && typeof frame.payload === "object" && !Array.isArray(frame.payload)
    ? frame.payload : {};
  const review = frame.event === "done" && payload.review
      && typeof payload.review === "object" && !Array.isArray(payload.review)
    ? payload.review : {};
  const providerValue = frame.event === "critic_verdict"
    ? payload.provider : (review.criticProvider || review.provider);
  const modelValue = frame.event === "critic_verdict"
    ? payload.model : (review.criticModel || review.model);
  const provider = SAFE_ID.test(String(providerValue || "").trim())
    ? String(providerValue).trim() : (prior && prior.provider || null);
  const model = SAFE_ID.test(String(modelValue || "").trim())
    ? String(modelValue).trim() : (prior && prior.model || null);
  const rawEvidence = frame.event === "critic_verdict" && Array.isArray(payload.evidence_ids)
    ? payload.evidence_ids : null;
  const evidenceIds = rawEvidence
    ? [...new Set(rawEvidence
      .map((value) => String(value || "").trim().toLowerCase())
      .filter((value) => /^sha256:[a-f0-9]{64}$/.test(value)))].sort()
    : (prior && prior.evidenceIds || []);
  if (!provider && !model && evidenceIds.length === 0) return prior;
  return Object.freeze({ provider, model, evidenceIds: Object.freeze(evidenceIds) });
}

function createTerminalResponseGate(response, identity = {}) {
  if (!response || typeof response.write !== "function" || typeof response.end !== "function") {
    throw new TypeError("Review response must expose write and end");
  }
  const boundIdentity = Object.freeze({
    sessionId: SAFE_ID.test(String(identity.sessionId || "")) ? String(identity.sessionId) : null,
    conversationId: SAFE_ID.test(String(identity.conversationId || ""))
      ? String(identity.conversationId) : null,
    runId: SAFE_ID.test(String(identity.runId || "")) ? String(identity.runId) : null,
  });
  const originalWrite = response.write;
  const originalEnd = response.end;
  const heldWrites = [];
  let heldBytes = 0;
  let heldEnd = null;
  let pendingText = "";
  let summary = null;
  let reviewEvidence = null;
  let restored = false;
  let holding = false;
  let compromised = false;
  let terminalInvalid = false;
  let explicitTerminal = false;
  let announcedPass = false;

  function overflow() {
    compromised = true;
    holding = true;
    heldWrites.length = 0;
    heldBytes = 0;
    heldEnd = null;
    pendingText = "";
    summary = null;
    const error = new Error("Review terminal response exceeds its safety limit");
    error.code = "REVIEW_TERMINAL_TOO_LARGE";
    throw error;
  }

  function hold(chunk) {
    const bytes = Buffer.byteLength(chunk, "utf8");
    if (heldBytes + bytes > MAX_HELD_TERMINAL_BYTES) overflow();
    heldBytes += bytes;
    heldWrites.push(chunk);
  }

  function processFrame(frame) {
    const parsedFrame = parseSseFrame(frame);
    const parsedSummary = terminalSummaryFromFrame(frame);
    const eventName = parsedFrame ? parsedFrame.event : sseEventName(frame);
    reviewEvidence = reviewEvidenceFromFrame(frame, reviewEvidence);
    const announcesPass = Boolean(parsedFrame && parsedFrame.event === "critic_verdict"
      && String(parsedFrame.payload && parsedFrame.payload.status || "").trim().toUpperCase() === "PASS");
    if (announcesPass) announcedPass = true;
    if (["done", "loop_done"].includes(eventName)) {
      explicitTerminal = true;
      holding = true;
      if (!parsedSummary) terminalInvalid = true;
      else if (summary && !sameTerminalSummary(summary, parsedSummary)) terminalInvalid = true;
      else if (!summary) summary = parsedSummary;
    }
    const identityPayload = parsedFrame && parsedFrame.payload
        && typeof parsedFrame.payload === "object" && !Array.isArray(parsedFrame.payload)
      ? parsedFrame.payload : {};
    const boundFrame = parsedFrame && ["run_start", "done", "loop_done"].includes(parsedFrame.event)
      ? `event: ${parsedFrame.event}\ndata: ${JSON.stringify({
        ...identityPayload,
        sessionId: boundIdentity.sessionId,
        conversationId: boundIdentity.conversationId,
        runId: boundIdentity.runId,
      })}\n\n`
      : frame;
    if (announcesPass) holding = true;
    if (holding) hold(boundFrame);
    else originalWrite.call(response, boundFrame);
  }

  function drainCompleteFrames() {
    while (pendingText) {
      const boundary = /\r?\n\r?\n/.exec(pendingText);
      if (!boundary) break;
      const end = boundary.index + boundary[0].length;
      const frame = pendingText.slice(0, end);
      pendingText = pendingText.slice(end);
      processFrame(frame);
    }
    if (Buffer.byteLength(pendingText, "utf8") > MAX_HELD_TERMINAL_BYTES) overflow();
  }

  response.write = function gatedWrite(chunk, encoding, callback) {
    const accepted = typeof encoding === "function" ? encoding : callback;
    if (compromised) {
      if (typeof accepted === "function") queueMicrotask(accepted);
      return true;
    }
    pendingText += Buffer.isBuffer(chunk) ? chunk.toString("utf8") : String(chunk || "");
    drainCompleteFrames();
    if (typeof accepted === "function") queueMicrotask(accepted);
    return true;
  };
  response.end = function gatedEnd(chunk, encoding, callback) {
    const accepted = typeof chunk === "function"
      ? chunk
      : (typeof encoding === "function" ? encoding : callback);
    const body = typeof chunk === "function" ? null : chunk;
    if (!compromised && body !== undefined && body !== null) {
      response.write(body);
    }
    if (!compromised && pendingText) {
      // A terminal fragment without an SSE frame boundary is never an
      // authoritative terminal, even if its JSON happens to parse.
      terminalInvalid = true;
      holding = true;
      hold(pendingText);
      pendingText = "";
    }
    if (typeof accepted === "function") queueMicrotask(accepted);
    heldEnd = [];
    return this;
  };

  function restore() {
    if (restored) return;
    restored = true;
    response.write = originalWrite;
    response.end = originalEnd;
  }

  return Object.freeze({
    get summary() { return summary; },
    get reviewEvidence() { return reviewEvidence; },
    get compromised() { return compromised; },
    validateTerminal() {
      const passTerminal = Boolean(summary
        && summary.outcome === "completed"
        && summary.reason === "pass"
        && summary.finalVerdict === "PASS");
      const passEvidence = Boolean(reviewEvidence
        && reviewEvidence.provider
        && reviewEvidence.model
        && Array.isArray(reviewEvidence.evidenceIds)
        && reviewEvidence.evidenceIds.length > 0);
      const valid = !compromised && !terminalInvalid && explicitTerminal && Boolean(summary)
        && announcedPass === passTerminal
        && (!passTerminal || passEvidence);
      return Object.freeze({
        valid,
        code: valid ? null : "REVIEW_TERMINAL_INVALID",
        announcedPass,
        explicitTerminal,
        passTerminal,
      });
    },
    release() {
      restore();
      for (const chunk of heldWrites) originalWrite.call(response, chunk);
      if (heldEnd) originalEnd.call(response, ...heldEnd);
    },
    failClosed({
      code = "ARTIFACT_JOURNAL_UNAVAILABLE",
      message = "Durable artifact journal is unavailable.",
      runId = boundIdentity.runId,
    } = {}) {
      restore();
      const payload = {
        sessionId: boundIdentity.sessionId,
        conversationId: boundIdentity.conversationId,
        runId,
        isError: true,
        cancelled: false,
        error: message,
        code,
      };
      if (response.headersSent) {
        originalWrite.call(response, `event: done\ndata: ${JSON.stringify(payload)}\n\n`);
        originalEnd.call(response);
      } else if (typeof response.status === "function" && typeof response.json === "function") {
        response.status(503).json({
          error: payload.error,
          code: payload.code,
          sessionId: payload.sessionId,
          conversationId: payload.conversationId,
          runId: payload.runId,
        });
      } else {
        originalEnd.call(response, JSON.stringify({
          error: payload.error,
          code: payload.code,
          sessionId: payload.sessionId,
          conversationId: payload.conversationId,
          runId: payload.runId,
        }));
      }
    },
  });
}

function createReviewLoopCoordinator({
  registry = new ReviewRunRegistry(),
  transportProfile,
  resolveActiveSession,
  isActiveSessionBinding,
  loopbackSessionId,
  defaultMode = "vanilla",
  textHandler,
  visualHandler,
  artifactRecorder = null,
  captureReviewBinding = null,
  mutationArbiter = null,
  handlerDependencies = () => ({}),
  logger = () => {},
} = {}) {
  if (!registry || typeof registry.start !== "function" || typeof registry.cancel !== "function"
      || typeof registry.complete !== "function") {
    throw new TypeError("review coordinator requires a ReviewRunRegistry-compatible registry");
  }
  if (typeof textHandler !== "function" || typeof visualHandler !== "function") {
    throw new TypeError("review coordinator requires Text and Visual handlers");
  }
  if (typeof handlerDependencies !== "function") {
    throw new TypeError("handlerDependencies must be a function");
  }
  if (artifactRecorder !== null && artifactRecorder !== undefined
      && (typeof artifactRecorder.prepareReviewTerminal !== "function"
        || typeof artifactRecorder.ensureReviewTerminal !== "function")) {
    throw new TypeError("artifactRecorder must expose prepareReviewTerminal and ensureReviewTerminal");
  }
  if (mutationArbiter !== null && mutationArbiter !== undefined
      && (typeof mutationArbiter.acquire !== "function" || typeof mutationArbiter.isHeld !== "function")) {
    throw new TypeError("mutationArbiter must expose acquire and isHeld");
  }
  const bindingCapture = typeof captureReviewBinding === "function"
    ? captureReviewBinding
    : (artifactRecorder && typeof artifactRecorder.captureReviewBinding === "function"
      ? artifactRecorder.captureReviewBinding.bind(artifactRecorder)
      : null);
  const resolveScope = createReviewScopeResolver({
    transportProfile,
    resolveActiveSession,
    loopbackSessionId,
  });
  const trusted = TRUSTED_PROFILES.has(String(transportProfile || "").trim().toLowerCase());
  if (trusted && typeof isActiveSessionBinding !== "function") {
    throw new TypeError("trusted-proxy review coordination requires isActiveSessionBinding");
  }
  const activeRunScopes = new Map();
  const activeRunKey = (scopeId, runId) => `${scopeId}\0${runId}`;

  async function captureBinding({ request, scope, mode, run, phase }) {
    if (typeof bindingCapture !== "function") {
      fail("REVIEW_SCENE_BINDING_UNAVAILABLE", "Review scene binding capture is unavailable.", 503);
    }
    try {
      const value = normalizeReviewSceneBinding(await bindingCapture({
        request, scope, mode, run, phase, signal: run && run.signal,
      }), `review.${phase}Binding`);
      if (value.scope_id !== scope.scopeId) {
        fail("REVIEW_SCENE_BINDING_INVALID", "Review scene binding does not match the active scope.", 409);
      }
      return value;
    } catch (error) {
      if (error instanceof ReviewLoopCoordinatorError) throw error;
      fail("REVIEW_SCENE_BINDING_UNAVAILABLE", "Review scene binding capture failed.", 503);
    }
  }

  async function handleChat(request, response, next) {
    let mode;
    let scope = null;
    try {
      const explicitMode = explicitModeForRequest(request);
      if (explicitMode === "vanilla") return next();
      scope = resolveScope(request);
      mode = explicitMode || modeForRequest(request, defaultMode, scope);
    } catch (error) {
      return response.status(error.statusCode || 400).json({ code: error.code, error: error.message });
    }
    if (mode === "vanilla") return next();

    let run = null;
    let reviewTicket = null;
    let responseGate = null;
    let terminal = null;
    let bindingBefore = null;
    let journalFailed = false;
    let recoveryBlocked = false;
    let recoveredTerminal = false;
    let sceneBindingFailed = false;
    let terminalGateFailed = false;
    let resolvedHandlerDependencies = null;
    let requestDigest = null;
    let inputDigest = null;
    let mutationToken = null;
    let requestedReviewRunId = null;
    let providerExecutionState = "proven_not_started";
    const durableReview = Boolean(artifactRecorder && artifactRecorder.enabled !== false);
    try {
      const requestedRunId = requestValue(request, "runId");
      if (durableReview
          && (requestedRunId === undefined || requestedRunId === null || requestedRunId === "")) {
        fail(
          "REVIEW_RUN_ID_REQUIRED",
          "A stable client Review run ID is required for durable execution.",
          400,
        );
      }
      const safeRequestedRunId = requestedRunId === undefined || requestedRunId === null || requestedRunId === ""
        ? null
        : safeId(requestedRunId, "runId");
      requestedReviewRunId = safeRequestedRunId;
      resolvedHandlerDependencies = handlerDependencies({ request, mode, scope, run: null }) || {};
      if (!resolvedHandlerDependencies || typeof resolvedHandlerDependencies !== "object"
          || Array.isArray(resolvedHandlerDependencies)) {
        throw new TypeError("handlerDependencies must return an object");
      }
      ({ requestDigest, inputDigest } = reviewInputDigests({
        request,
        mode,
        scope,
        dependencies: resolvedHandlerDependencies,
      }));
      // From this point onward, a conflict may belong to an already-running
      // invocation with the same client ID. Never authorize a new paid run
      // merely because this request has not reached its own handler yet.
      providerExecutionState = "unknown";
      if (mutationArbiter) {
        const mutationIdentity = scope.leaseBound
          ? scope.activeLease
          : (resolvedHandlerDependencies.ueBroker
              && Number.isSafeInteger(Number(resolvedHandlerDependencies.ueBroker.port))
            ? { mcpPort: Number(resolvedHandlerDependencies.ueBroker.port) }
            : { loopback: true });
        try {
          mutationToken = mutationArbiter.acquire(mutationIdentity, {
            kind: "review",
            operationId: safeRequestedRunId,
          });
        } catch (error) {
          if (error && error.code === "RUNTIME_MUTATION_SLOT_BUSY") {
            fail("REVIEW_SLOT_BUSY", "This Studio slot already has an active runtime mutation.", 409);
          }
          throw error;
        }
      }
      run = registry.start({
        scopeId: scope.scopeId,
        ...(safeRequestedRunId ? { runId: safeRequestedRunId } : {}),
      });
      if (scope.leaseBound) {
        activeRunScopes.set(activeRunKey(scope.scopeId, run.runId), { scope, mutationToken });
      }
      if (durableReview) {
        bindingBefore = await captureBinding({ request, scope, mode, run, phase: "before" });
      }
      responseGate = createTerminalResponseGate(response, {
        sessionId: scope.scopeId,
        conversationId: scope.conversationId,
        runId: run.runId,
      });
      if (durableReview) {
        try {
          reviewTicket = await artifactRecorder.prepareReviewTerminal({
            scope, run, mode, binding: bindingBefore, requestDigest, inputDigest,
          });
        } catch (error) {
          journalFailed = true;
          throw error;
        }
        if (!reviewTicket || reviewTicket.disabled === true) {
          journalFailed = true;
          throw new ReviewLoopCoordinatorError(
            "ARTIFACT_JOURNAL_UNAVAILABLE",
            "Durable artifact journal is unavailable.",
            503,
          );
        }
        if (reviewTicket.created === false && reviewTicket.state === "prepared") {
          recoveryBlocked = true;
          fail(
            "REVIEW_TERMINAL_RECOVERY_REQUIRED",
            "This Review run has an unresolved prior provider attempt.",
            409,
          );
        }
        if (reviewTicket.created === false
            && ["terminal_pending", "published"].includes(reviewTicket.state)) {
          terminal = reviewTicket.terminal;
          recoveredTerminal = true;
          providerExecutionState = "attempted";
          if (!terminal || typeof terminal !== "object") {
            journalFailed = true;
            throw new ReviewLoopCoordinatorError(
              "ARTIFACT_JOURNAL_CORRUPT",
              "Durable Review recovery data is invalid.",
              503,
            );
          }
          const payload = {
            sessionId: scope.scopeId,
            conversationId: scope.conversationId,
            runId: run.runId,
            isError: terminal.outcome === "failed",
            cancelled: terminal.outcome === "cancelled",
            recovered: true,
            code: terminal.errorCode,
            loop: {
              reason: terminal.reason,
              rounds: terminal.rounds,
              finalStatus: terminal.finalVerdict,
              mode,
            },
          };
          response.write(`event: done\ndata: ${JSON.stringify(payload)}\n\n`);
          response.end();
        } else {
          const handler = mode === "visual_loop" ? visualHandler : textHandler;
          if (mutationToken) mutationToken.invalidateScene();
          providerExecutionState = "attempted";
          await handler(request, response, {
            ...resolvedHandlerDependencies,
            scopeId: scope.scopeId,
            internalSessionId: scope.scopeId,
            internalConversationId: scope.scopeId,
            runId: run.runId,
            signal: run.signal,
          });
        }
      } else {
        const handler = mode === "visual_loop" ? visualHandler : textHandler;
        if (mutationToken) mutationToken.invalidateScene();
        providerExecutionState = "attempted";
        await handler(request, response, {
          ...resolvedHandlerDependencies,
          scopeId: scope.scopeId,
          internalSessionId: scope.scopeId,
          internalConversationId: scope.scopeId,
          runId: run.runId,
          signal: run.signal,
        });
      }
    } catch (error) {
      const aborted = Boolean(run && run.signal.aborted) || (error && error.name === "AbortError");
      const code = aborted
        ? "REVIEW_RUN_CANCELLED"
        : (error && /^[A-Z][A-Z0-9_]{1,79}$/.test(String(error.code || ""))
          ? error.code
          : "REVIEW_LOOP_ERROR");
      const statusCode = aborted ? 409 : (error.statusCode || (code === "REVIEW_RUN_CONFLICT" ? 409 : 500));
      const message = aborted ? "Review run cancelled" : (error instanceof ReviewLoopCoordinatorError
        ? error.message
        : "Review loop failed");
      const provenPreProviderFailure = Boolean(
        providerExecutionState === "proven_not_started"
        && scope
        && requestedReviewRunId,
      );
      terminal = {
        outcome: aborted ? "cancelled" : "failed",
        reason: aborted ? "cancelled" : "handler_error",
        finalVerdict: "UNKNOWN",
        rounds: 0,
        errorCode: code,
      };
      try { logger("review-coordinator", `${mode} ${code}`); } catch (_error) {}
      if (!response.headersSent) {
        response.status(statusCode).json({
          error: message,
          code,
          sessionId: scope ? scope.scopeId : null,
          conversationId: scope ? scope.conversationId : null,
          runId: run && run.runId || requestedReviewRunId,
          ...(provenPreProviderFailure ? { providerAttempted: false } : {}),
        });
      } else if (!response.writableEnded) {
        const payload = {
          sessionId: scope ? scope.scopeId : null,
          conversationId: scope ? scope.conversationId : null,
          runId: run && run.runId || requestedReviewRunId,
          isError: true,
          cancelled: aborted,
          error: message,
          code,
          ...(provenPreProviderFailure ? { providerAttempted: false } : {}),
          loop: {
            reason: terminal.reason,
            rounds: terminal.rounds,
            finalStatus: terminal.finalVerdict,
            mode,
          },
        };
        try { response.write(`event: done\ndata: ${JSON.stringify(payload)}\n\n`); } catch (_error) {}
        try { response.end(); } catch (_error) {}
      }
    } finally {
      if (run && scope) {
        const handlerHttpFailed = Number(response && response.statusCode) >= 400;
        const gateSummary = responseGate && responseGate.summary;
        const gateEvidence = responseGate && responseGate.reviewEvidence;
        const gateValidation = recoveredTerminal || handlerHttpFailed || !responseGate
          ? { valid: true }
          : responseGate.validateTerminal();
        terminal = recoveredTerminal
          ? terminal
          : (handlerHttpFailed
            ? (terminal || {
              outcome: "failed",
              reason: "handler_error",
              finalVerdict: "UNKNOWN",
              rounds: 0,
              errorCode: "REVIEW_HANDLER_HTTP_ERROR",
            })
            : (gateSummary || terminal || {
              outcome: "completed",
              reason: "handler_completed",
              finalVerdict: "UNKNOWN",
              rounds: 0,
              errorCode: null,
            }));
        if (!gateValidation.valid) {
          terminalGateFailed = true;
          terminal = {
            outcome: "failed",
            reason: "handler_error",
            finalVerdict: "UNKNOWN",
            rounds: terminal && Number.isSafeInteger(terminal.rounds) ? terminal.rounds : 0,
            errorCode: terminal && terminal.outcome === "failed" && terminal.errorCode
              ? terminal.errorCode : "REVIEW_TERMINAL_INVALID",
          };
        }
        if (durableReview && reviewTicket && !recoveryBlocked && !recoveredTerminal) {
          try {
            const bindingAfter = await captureBinding({ request, scope, mode, run, phase: "after" });
            terminal = {
              ...terminal,
              provider: gateEvidence && gateEvidence.provider,
              model: gateEvidence && gateEvidence.model,
              evidenceIds: gateEvidence && gateEvidence.evidenceIds || [],
              bindingAfter,
            };
          } catch (_bindingError) {
            sceneBindingFailed = true;
            terminal = {
              outcome: "failed",
              reason: "handler_error",
              finalVerdict: "UNKNOWN",
              rounds: terminal.rounds || 0,
              errorCode: "REVIEW_SCENE_BINDING_UNAVAILABLE",
              provider: gateEvidence && gateEvidence.provider,
              model: gateEvidence && gateEvidence.model,
              evidenceIds: gateEvidence && gateEvidence.evidenceIds || [],
              bindingAfter: null,
            };
          }
        }
        try {
          if (durableReview && reviewTicket && !recoveryBlocked) {
            await artifactRecorder.ensureReviewTerminal({ ticket: reviewTicket, terminal });
          }
        } catch (_journalError) {
          journalFailed = true;
          try { logger("review-coordinator", `${mode} ARTIFACT_JOURNAL_UNAVAILABLE`); } catch (_error) {}
        }
        activeRunScopes.delete(activeRunKey(scope.scopeId, run.runId));
        registry.complete({ scopeId: scope.scopeId, runId: run.runId });
        if (responseGate) {
          if (terminalGateFailed) responseGate.failClosed({
            code: "REVIEW_TERMINAL_INVALID",
            message: "Review did not produce one consistent authoritative terminal.",
            runId: run.runId,
          });
          else if (responseGate.compromised) responseGate.failClosed({
            code: "REVIEW_TERMINAL_GATE_FAILED",
            message: "Review terminal response could not be delivered safely.",
            runId: run.runId,
          });
          else if (journalFailed) responseGate.failClosed({ runId: run.runId });
          else if (sceneBindingFailed) responseGate.failClosed({
            code: "REVIEW_SCENE_BINDING_UNAVAILABLE",
            message: "Review scene binding capture failed.",
            runId: run.runId,
          });
          else responseGate.release();
        }
      }
      if (mutationToken) mutationToken.release();
    }
  }

  function bindVanillaChat(request, response, next) {
    if (!trusted) return next();
    let scope = null;
    let internal = false;
    let runId = null;
    let activeEntry = null;
    try {
      try {
        scope = resolveScope(request);
      } catch (error) {
        if (!error || error.code !== "REVIEW_ACTIVE_SESSION_REQUIRED") throw error;
        const body = request && request.body && typeof request.body === "object" ? request.body : {};
        if (body.useLoop !== false) throw error;
        const scopeId = safeId(body.sessionId, "sessionId");
        if (safeId(body.conversationId, "conversationId") !== scopeId) throw error;
        runId = safeId(body.runId, "runId");
        const record = registry.get({ scopeId, runId });
        activeEntry = activeRunScopes.get(activeRunKey(scopeId, runId));
        const activeScope = activeEntry && activeEntry.scope;
        let stillActive = false;
        try {
          stillActive = Boolean(activeScope && isActiveSessionBinding(activeScope.activeLease));
        } catch (_revalidationError) {
          stillActive = false;
        }
        if (!record || record.signal.aborted || !activeScope || !stillActive) throw error;
        scope = activeScope;
        internal = true;
      }
      const body = request && request.body && typeof request.body === "object" ? request.body : {};
      request.body = { ...body, sessionId: scope.scopeId, conversationId: scope.scopeId };
      setRequestContext(request, {
        scopeId: scope.scopeId,
        runId,
        internal,
        activeLease: scope.activeLease,
        mutationToken: internal && activeEntry ? activeEntry.mutationToken : null,
      });
      return next();
    } catch (error) {
      return response.status(error.statusCode || 401).json({
        code: error.code || "REVIEW_ACTIVE_SESSION_REQUIRED",
        error: error.message || "An active Studio streaming lease is required.",
      });
    }
  }

  function cancel(request, reason = "user_stop") {
    const scope = resolveScope(request);
    const requestedRunId = requestValue(request, "runId");
    const runId = requestedRunId === undefined || requestedRunId === null || requestedRunId === ""
      ? undefined
      : safeId(requestedRunId, "runId");
    const record = registry.cancel({ scopeId: scope.scopeId, runId, reason });
    return Object.freeze({ scope, record });
  }

  return Object.freeze({
    registry,
    resolveScope,
    handleChat,
    bindVanillaChat,
    cancel,
  });
}

module.exports = {
  ReviewLoopCoordinatorError,
  createLeaseBoundReviewBroker,
  createReviewLoopCoordinator,
  createReviewScopeResolver,
  createScopedReviewModeStore,
  createTerminalResponseGate,
  getReviewRequestContext,
  normalizeReviewMode,
};

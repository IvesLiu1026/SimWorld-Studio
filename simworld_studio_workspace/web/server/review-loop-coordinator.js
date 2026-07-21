"use strict";

const crypto = require("node:crypto");

const { ReviewRunRegistry } = require("./review-run-registry");

const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/;
const TRUSTED_PROFILES = new Set(["trusted_proxy", "trusted-proxy", "public_webrtc", "public-webrtc"]);
const REQUEST_CONTEXT = Symbol("simworld.reviewRequestContext");
const MAX_HELD_TERMINAL_BYTES = 64 * 1024;
const REVIEW_FATAL_REASONS = new Set(["builder_error", "critic_error", "budget_exhausted"]);
const REVIEW_REASONS = new Set([
  "pass", "max_iterations", "builder_error", "critic_error",
  "budget_exhausted", "cancelled",
]);
const REVIEW_VERDICTS = new Set(["PASS", "FAIL", "NEEDS_IMPROVEMENT"]);

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
      return current.send(...args);
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
    ? frame.payload : {};
  const loop = frame.event === "loop_done"
    ? payload
    : (payload && typeof payload.loop === "object" && !Array.isArray(payload.loop)
      ? payload.loop : {});
  const rawReason = String(loop.reason || payload.failureReason || "").trim().toLowerCase();
  const reason = REVIEW_REASONS.has(rawReason) ? rawReason : null;
  const rawVerdict = String(loop.finalStatus || "").trim().toUpperCase();
  const finalVerdict = REVIEW_VERDICTS.has(rawVerdict) ? rawVerdict : "UNKNOWN";
  const rounds = Number(loop.rounds);
  const errorCode = payload.error && typeof payload.error === "object"
    ? payload.error.code : payload.code;
  return Object.freeze({
    outcome: reason === "cancelled" ? "cancelled" : (REVIEW_FATAL_REASONS.has(reason) || payload.isError === true ? "failed" : "completed"),
    reason: reason || (payload.isError === true ? "handler_error" : "handler_completed"),
    finalVerdict,
    rounds: Number.isSafeInteger(rounds) && rounds >= 0 && rounds <= 100 ? rounds : 0,
    errorCode: typeof errorCode === "string" ? errorCode : null,
  });
}

function createTerminalResponseGate(response) {
  if (!response || typeof response.write !== "function" || typeof response.end !== "function") {
    throw new TypeError("Review response must expose write and end");
  }
  const originalWrite = response.write;
  const originalEnd = response.end;
  const heldWrites = [];
  let heldBytes = 0;
  let heldEnd = null;
  let pendingText = "";
  let summary = null;
  let restored = false;
  let holding = false;
  let compromised = false;

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
    const announcesPass = Boolean(parsedFrame && parsedFrame.event === "critic_verdict"
      && String(parsedFrame.payload && parsedFrame.payload.status || "").trim().toUpperCase() === "PASS");
    if (parsedSummary || announcesPass) holding = true;
    if (parsedSummary) summary = parsedSummary;
    if (holding) hold(frame);
    else originalWrite.call(response, frame);
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
      const parsed = terminalSummaryFromFrame(pendingText);
      if (parsed) summary = parsed;
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
    get compromised() { return compromised; },
    release() {
      restore();
      for (const chunk of heldWrites) originalWrite.call(response, chunk);
      if (heldEnd) originalEnd.call(response, ...heldEnd);
    },
    failClosed({
      code = "ARTIFACT_JOURNAL_UNAVAILABLE",
      message = "Durable artifact journal is unavailable.",
      runId = null,
    } = {}) {
      restore();
      const payload = {
        sessionId: null,
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
        response.status(503).json({ error: payload.error, code: payload.code, runId: payload.runId });
      } else {
        originalEnd.call(response, JSON.stringify({ error: payload.error, code: payload.code }));
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
    let journalFailed = false;
    let recoveryBlocked = false;
    const durableReview = Boolean(artifactRecorder && artifactRecorder.enabled !== false);
    try {
      const requestedRunId = requestValue(request, "runId");
      run = registry.start({
        scopeId: scope.scopeId,
        ...(requestedRunId === undefined || requestedRunId === null || requestedRunId === ""
          ? {}
          : { runId: safeId(requestedRunId, "runId") }),
      });
      if (scope.leaseBound) activeRunScopes.set(activeRunKey(scope.scopeId, run.runId), scope);
      responseGate = createTerminalResponseGate(response);
      if (durableReview) {
        try {
          reviewTicket = await artifactRecorder.prepareReviewTerminal({ scope, run, mode });
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
          await handler(request, response, {
            ...handlerDependencies({ request, mode, scope, run }),
            scopeId: scope.scopeId,
            internalSessionId: scope.scopeId,
            internalConversationId: scope.scopeId,
            runId: run.runId,
            signal: run.signal,
          });
        }
      } else {
        const handler = mode === "visual_loop" ? visualHandler : textHandler;
        await handler(request, response, {
          ...handlerDependencies({ request, mode, scope, run }),
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
      terminal = {
        outcome: aborted ? "cancelled" : "failed",
        reason: aborted ? "cancelled" : "handler_error",
        finalVerdict: "UNKNOWN",
        rounds: 0,
        errorCode: code,
      };
      try { logger("review-coordinator", `${mode} ${code}`); } catch (_error) {}
      if (!response.headersSent) {
        response.status(statusCode).json({ error: message, code, runId: run && run.runId });
      } else if (!response.writableEnded) {
        const payload = {
          sessionId: scope ? scope.scopeId : null,
          runId: run && run.runId,
          isError: true,
          cancelled: aborted,
          error: message,
          code,
        };
        try { response.write(`event: done\ndata: ${JSON.stringify(payload)}\n\n`); } catch (_error) {}
        try { response.end(); } catch (_error) {}
      }
    } finally {
      if (run && scope) {
        const handlerHttpFailed = Number(response && response.statusCode) >= 400;
        const gateSummary = responseGate && responseGate.summary;
        terminal = handlerHttpFailed
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
          });
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
          if (responseGate.compromised) responseGate.failClosed({
            code: "REVIEW_TERMINAL_GATE_FAILED",
            message: "Review terminal response could not be delivered safely.",
            runId: run.runId,
          });
          else if (journalFailed) responseGate.failClosed({ runId: run.runId });
          else responseGate.release();
        }
      }
    }
  }

  function bindVanillaChat(request, response, next) {
    if (!trusted) return next();
    let scope = null;
    let internal = false;
    let runId = null;
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
        const activeScope = activeRunScopes.get(activeRunKey(scopeId, runId));
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

"use strict";

const crypto = require("node:crypto");

const { ReviewRunRegistry } = require("./review-run-registry");

const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/;
const TRUSTED_PROFILES = new Set(["trusted_proxy", "trusted-proxy", "public_webrtc", "public-webrtc"]);

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

function activeLeaseAuthority(identity) {
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
  return [ownerId, sessionId, slotId, leaseId, mcpPort];
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
    const leaseAuthority = activeLeaseAuthority(identity);
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
    return Object.freeze({
      scopeId: `review-${digest}`,
      conversationId,
      leaseBound: Boolean(leaseAuthority),
    });
  };
}

function modeForRequest(request, defaultMode) {
  const body = request && request.body && typeof request.body === "object" ? request.body : {};
  if (body.loopMode !== undefined) {
    const explicit = normalizeReviewMode(body.loopMode);
    if (!explicit) fail("REVIEW_MODE_INVALID", "loopMode is invalid", 400);
    return explicit;
  }
  if (body.useLoop === false) return "vanilla";
  if (body.useLoop === true) return "text_loop";
  return normalizeReviewMode(typeof defaultMode === "function" ? defaultMode() : defaultMode) || "vanilla";
}

function createReviewLoopCoordinator({
  registry = new ReviewRunRegistry(),
  transportProfile,
  resolveActiveSession,
  loopbackSessionId,
  defaultMode = "vanilla",
  textHandler,
  visualHandler,
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
  const resolveScope = createReviewScopeResolver({
    transportProfile,
    resolveActiveSession,
    loopbackSessionId,
  });

  async function handleChat(request, response, next) {
    let mode;
    try {
      mode = modeForRequest(request, defaultMode);
    } catch (error) {
      return response.status(error.statusCode || 400).json({ code: error.code, error: error.message });
    }
    if (mode === "vanilla") return next();

    let scope;
    let run = null;
    try {
      scope = resolveScope(request);
      const requestedRunId = requestValue(request, "runId");
      run = registry.start({
        scopeId: scope.scopeId,
        ...(requestedRunId === undefined || requestedRunId === null || requestedRunId === ""
          ? {}
          : { runId: safeId(requestedRunId, "runId") }),
      });
      const handler = mode === "visual_loop" ? visualHandler : textHandler;
      await handler(request, response, {
        ...handlerDependencies({ request, mode, scope, run }),
        scopeId: scope.scopeId,
        internalSessionId: scope.scopeId,
        internalConversationId: scope.scopeId,
        runId: run.runId,
        signal: run.signal,
      });
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
      if (run && scope) registry.complete({ scopeId: scope.scopeId, runId: run.runId });
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
    cancel,
  });
}

module.exports = {
  ReviewLoopCoordinatorError,
  createReviewLoopCoordinator,
  createReviewScopeResolver,
  normalizeReviewMode,
};

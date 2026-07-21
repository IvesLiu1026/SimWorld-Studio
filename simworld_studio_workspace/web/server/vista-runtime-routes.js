"use strict";

const express = require("express");

const { VistaRuntimeError, hasStrictlyEmptyBody } = require("./vista-runtime-broker");

const PUBLIC_MESSAGES = Object.freeze({
  VISTA_EMPTY_BODY_REQUIRED: "Request body must be empty.",
  VISTA_RUNTIME_IDENTITY_INVALID: "An active Studio runtime lease is required.",
  VISTA_RUNTIME_LEASE_REVOKED: "The Studio runtime lease expired.",
  VISTA_RUNTIME_QUARANTINED: "Runtime ownership must be reconciled before control can continue.",
  VISTA_SCENE_PROOF_REQUIRED: "Build and verify the scene before starting simulation.",
  VISTA_SCENE_PROOF_INVALID: "The verified scene proof is invalid or stale.",
  VISTA_STOP_IN_PROGRESS: "Simulation Stop is still in progress.",
  VISTA_SETUP_SUPERSEDED: "Simulation Start was superseded by Stop.",
  VISTA_STOP_SUPERSEDED: "Simulation Stop ownership changed before confirmation.",
  VISTA_RUNTIME_BUSY: "The UE runtime is busy.",
  VISTA_RUNTIME_UNAVAILABLE: "The UE runtime is unavailable.",
  VISTA_RUNTIME_CONFIRMATION_TIMEOUT: "UE did not confirm the requested runtime transition.",
  VISTA_RUNTIME_PROTOCOL_ERROR: "UE returned an invalid runtime response.",
  VISTA_PAWN_CLASS_MISMATCH: "The live UE pawn does not match the verified VISTA pawn.",
  VISTA_RUNTIME_ABORTED: "The runtime operation was cancelled.",
});

function publicError(rawError) {
  const error = rawError instanceof VistaRuntimeError
    ? rawError
    : new VistaRuntimeError("VISTA_RUNTIME_INTERNAL", "VISTA runtime request failed", { status: 500 });
  const code = Object.prototype.hasOwnProperty.call(PUBLIC_MESSAGES, error.code)
    ? error.code
    : "VISTA_RUNTIME_INTERNAL";
  return {
    status: Number.isInteger(error.status) ? error.status : 500,
    headers: Number.isSafeInteger(error.retryAfterMs) && error.retryAfterMs > 0
      ? { "Retry-After": String(Math.max(1, Math.ceil(error.retryAfterMs / 1000))) }
      : {},
    body: {
      code,
      error: PUBLIC_MESSAGES[code] || "VISTA runtime request failed.",
      retryable: error.retryable === true,
      ...(Number.isSafeInteger(error.retryAfterMs) && error.retryAfterMs > 0
        ? { retry_after_ms: error.retryAfterMs }
        : {}),
    },
  };
}

function createAbortSignal(req, res) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  req.once("aborted", abort);
  res.once("close", () => {
    if (!res.writableEnded) abort();
  });
  return controller.signal;
}

function createVistaRuntimeRouter({ registry } = {}) {
  if (!registry || typeof registry.exactIdentityFromRequest !== "function"
      || typeof registry.startForIdentity !== "function"
      || typeof registry.stateForIdentity !== "function"
      || typeof registry.stopForIdentity !== "function") {
    throw new TypeError("VISTA runtime controller registry is required");
  }
  const router = express.Router();
  router.use((_req, res, next) => {
    res.set("Cache-Control", "no-store");
    next();
  });

  function sendError(res, error) {
    const result = publicError(error);
    for (const [name, value] of Object.entries(result.headers)) res.set(name, value);
    return res.status(result.status).json(result.body);
  }

  function emptyPost(operation) {
    return async (req, res) => {
      if (!hasStrictlyEmptyBody(req.body, req.headers)) {
        return sendError(res, new VistaRuntimeError(
          "VISTA_EMPTY_BODY_REQUIRED",
          "Request body must be empty",
          { status: 400 },
        ));
      }
      try {
        const identity = await registry.exactIdentityFromRequest(req);
        const result = await registry[operation](identity, { signal: createAbortSignal(req, res) });
        return res.status(200).json(result);
      } catch (error) {
        return sendError(res, error);
      }
    };
  }

  router.post("/setup_vista_play_mode", emptyPost("startForIdentity"));
  router.post("/stop_vista_play_mode", emptyPost("stopForIdentity"));
  router.get("/get_vista_state", async (req, res) => {
    try {
      const identity = await registry.exactIdentityFromRequest(req);
      const result = await registry.stateForIdentity(identity, {
        signal: createAbortSignal(req, res),
      });
      return res.status(200).json(result);
    } catch (error) {
      return sendError(res, error);
    }
  });
  return router;
}

module.exports = {
  createVistaRuntimeRouter,
  publicError,
};

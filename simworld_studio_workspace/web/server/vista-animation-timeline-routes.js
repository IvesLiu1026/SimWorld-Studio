"use strict";

const express = require("express");

const PUBLIC_MESSAGES = Object.freeze({
  ANIMATION_ACCESS_INVALID: "An active Studio animation session is required.",
  ANIMATION_RUNTIME_UNAVAILABLE: "The trusted VISTA animation runtime is unavailable.",
  ANIMATION_RUNTIME_NOT_READY: "The trusted VISTA animation runtime is not ready.",
  ANIMATION_RUNTIME_READINESS_STALE: "The VISTA animation runtime readiness proof expired.",
  ANIMATION_RUNTIME_REVISION_MISMATCH: "The VISTA animation runtime no longer matches this scene session.",
  ANIMATION_SCENE_BUILD_REQUIRED: "Build and verify the exact VISTA scene before starting its timeline.",
  ANIMATION_SCENE_BUILD_INVALID: "The verified VISTA scene build is incomplete or stale.",
  ANIMATION_PLAN_STALE: "The confirmed Scene BuildPlan is no longer current.",
  ANIMATION_PREFLIGHT_NOT_FOUND: "Run animation preflight again for this active Studio session.",
  ANIMATION_PREFLIGHT_EXPIRED: "Animation preflight expired; run it again.",
  ANIMATION_PREFLIGHT_STALE: "Animation preflight is stale; run it again.",
  ANIMATION_PREFLIGHT_BLOCKED: "Animation preflight did not verify every timeline event.",
  ANIMATION_CONFIRMATION_REQUIRED: "Confirm the exact animation preflight and Scene BuildPlan.",
  ANIMATION_SLOT_BUSY: "This Studio slot already has an active animation timeline.",
  ANIMATION_RUN_NOT_FOUND: "Animation run was not found.",
  ANIMATION_RUN_IDENTITY_STALE: "Only the exact active Studio lease may stop this animation run.",
  ANIMATION_REPLAY_NOT_READY: "Only a terminal animation run can be replayed.",
  ANIMATION_REPLAY_REVISION_MISMATCH: "Replay requires the exact terminal animation revision.",
  ANIMATION_PREFLIGHT_CONSUMED: "Replay requires a fresh exact animation preflight.",
  ANIMATION_STORAGE_UNAVAILABLE: "Animation run storage is unavailable.",
});

function publicError(error) {
  const rawCode = error && error.code;
  const code = typeof rawCode === "string" && /^(?:ANIMATION|TIMELINE|SCENE_BUILD|VISTA)_[A-Z0-9_]{2,120}$/.test(rawCode)
    ? rawCode
    : "ANIMATION_TIMELINE_INTERNAL";
  return {
    error: PUBLIC_MESSAGES[code] || (code === "ANIMATION_TIMELINE_INTERNAL"
      ? "VISTA animation timeline failed."
      : "VISTA animation timeline request failed."),
    code,
    retryable: Boolean(error && error.retryable),
  };
}

function errorStatus(error, body) {
  const status = [error && error.status, error && error.statusCode]
    .find((value) => Number.isInteger(value) && value >= 400 && value <= 599);
  if (status) return status;
  if (body.code.endsWith("_NOT_FOUND")) return 404;
  if (body.code.endsWith("_STALE") || body.code.endsWith("_MISMATCH")
      || body.code.endsWith("_BUSY") || body.code.endsWith("_BLOCKED")) return 409;
  if (body.code.endsWith("_UNAVAILABLE") || body.code === "ANIMATION_ACCESS_INVALID") return 503;
  if (body.code === "ANIMATION_TIMELINE_INTERNAL") return 500;
  return 400;
}

function buildAbortContext(req, res, identity) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  req.once("aborted", abort);
  res.once("close", () => {
    if (!res.writableEnded) abort();
  });
  return { ...identity, signal: controller.signal };
}

function createVistaAnimationTimelineRouter({ service, resolveIdentity } = {}) {
  if (!service || typeof service.preflight !== "function" || typeof service.start !== "function"
      || typeof service.status !== "function" || typeof service.stop !== "function"
      || typeof service.replay !== "function") {
    throw new TypeError("VISTA animation timeline service is required");
  }
  if (typeof resolveIdentity !== "function") {
    throw new TypeError("VISTA animation timeline resolveIdentity is required");
  }
  const router = express.Router();

  router.use((_req, res, next) => {
    res.set("Cache-Control", "no-store");
    next();
  });

  function requestContext(req, res, { abortOnDisconnect = false } = {}) {
    const identity = resolveIdentity(req) || {};
    if (typeof identity.ownerId !== "string" || !identity.ownerId
        || typeof identity.sessionId !== "string" || !identity.sessionId
        || typeof identity.leaseId !== "string" || !identity.leaseId
        || !Number.isSafeInteger(identity.slotId)
        || !Number.isSafeInteger(identity.mcpPort)) {
      const error = new Error("Server-side VISTA animation identity is unavailable");
      error.code = "ANIMATION_ACCESS_INVALID";
      error.status = 503;
      error.retryable = true;
      throw error;
    }
    const context = {
      ownerId: identity.ownerId,
      sessionId: identity.sessionId,
      leaseId: identity.leaseId,
      slotId: identity.slotId,
      mcpPort: identity.mcpPort,
    };
    return abortOnDisconnect ? buildAbortContext(req, res, context) : context;
  }

  function handler(responseStatus, invoke, options = {}) {
    return async (req, res) => {
      try {
        const context = requestContext(req, res, options);
        const result = await invoke(service, req, context);
        res.status(responseStatus).json(result);
      } catch (error) {
        const body = publicError(error);
        res.status(errorStatus(error, body)).json(body);
      }
    };
  }

  router.post("/:runId/animation/preflight", handler(
    200,
    (timelineService, req, context) => timelineService.preflight(String(req.params.runId || ""), req.body || {}, context),
    { abortOnDisconnect: true },
  ));
  router.post("/:runId/animation/start", handler(
    202,
    (timelineService, req, context) => timelineService.start(String(req.params.runId || ""), req.body || {}, context),
  ));
  router.get("/:runId/animation/runs/:animationRunId", handler(
    200,
    (timelineService, req, context) => timelineService.status(
      String(req.params.runId || ""),
      String(req.params.animationRunId || ""),
      context,
    ),
  ));
  router.post("/:runId/animation/runs/:animationRunId/stop", handler(
    202,
    (timelineService, req, context) => timelineService.stop(
      String(req.params.runId || ""),
      String(req.params.animationRunId || ""),
      req.body || {},
      context,
    ),
  ));
  router.post("/:runId/animation/runs/:animationRunId/replay", handler(
    202,
    (timelineService, req, context) => timelineService.replay(
      String(req.params.runId || ""),
      String(req.params.animationRunId || ""),
      req.body || {},
      context,
    ),
  ));

  return router;
}

module.exports = {
  createVistaAnimationTimelineRouter,
  errorStatus,
  publicError,
};

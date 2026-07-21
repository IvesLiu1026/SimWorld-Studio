"use strict";

const express = require("express");

const { RUN_ID_PATTERN, sanitizePublicMessage } = require("./vista-import-routes");

function publicError(error) {
  const code = error && typeof error.code === "string" && /^[A-Z0-9_]{3,100}$/.test(error.code)
    ? error.code
    : "SCENE_BUILD_INTERNAL";
  const publicResult = publicExecutionResult(error && error.result);
  return {
    error: code === "SCENE_BUILD_INTERNAL"
      ? "VISTA scene build failed"
      : sanitizePublicMessage(error && error.message || "VISTA scene build failed"),
    code,
    retryable: Boolean(error && error.retryable),
    ...(publicResult ? { result: publicResult } : {}),
  };
}

function publicExecutionResult(result) {
  if (!result || typeof result !== "object" || Array.isArray(result)
      || result.schema !== "vista-scene-build-result/v1"
      || result.status !== "failed"
      || typeof result.plan_id !== "string"
      || typeof result.scene_id !== "string") return null;
  const rollback = result.rollback && typeof result.rollback === "object" && !Array.isArray(result.rollback)
    ? result.rollback
    : {};
  return {
    schema: "vista-scene-build-result/v1",
    plan_id: result.plan_id,
    scene_id: result.scene_id,
    status: "failed",
    mutation_count: Number.isSafeInteger(result.mutation_count) && result.mutation_count >= 0
      ? result.mutation_count
      : 0,
    rollback: {
      state: new Set(["not_required", "completed", "partial"]).has(rollback.state)
        ? rollback.state
        : "partial",
      deleted_actor_names: Array.isArray(rollback.deleted_actor_names)
        ? rollback.deleted_actor_names.filter((value) => typeof value === "string").slice(0, 512)
        : [],
      restored_player_start: rollback.restored_player_start === true,
      failure_count: Array.isArray(rollback.failures) ? Math.min(rollback.failures.length, 512) : 0,
    },
  };
}

function errorStatus(error, body) {
  const candidate = [error && error.status, error && error.statusCode]
    .find((value) => Number.isInteger(value) && value >= 400 && value <= 599);
  if (candidate) return candidate;
  if (body.code === "SCENE_BUILD_ACCESS_DENIED") return 403;
  if (body.code === "SCENE_BUILD_PROFILE_UNAVAILABLE") return 404;
  if (body.code.endsWith("_UNAVAILABLE")) return 503;
  if (body.code.endsWith("_CONFLICT") || body.code.endsWith("_STALE")) return 409;
  return body.code === "SCENE_BUILD_INTERNAL" ? 500 : 400;
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

function createVistaSceneBuildRouter({ service, ownerId, sessionId, resolveIdentity } = {}) {
  if (!service || typeof service.plan !== "function" || typeof service.status !== "function"
      || typeof service.preflight !== "function" || typeof service.start !== "function") {
    throw new TypeError("VISTA scene build service with plan, status, preflight, and start is required");
  }
  const identityResolver = typeof resolveIdentity === "function"
    ? resolveIdentity
    : () => ({ ownerId, sessionId });
  const router = express.Router();

  router.use((_req, res, next) => {
    res.set("Cache-Control", "no-store");
    next();
  });

  function requestContext(req, res, { abortOnDisconnect = true } = {}) {
    const identity = identityResolver(req) || {};
    if (!identity.ownerId || !identity.sessionId || !identity.leaseId
        || !Number.isSafeInteger(identity.slotId)
        || !Number.isSafeInteger(identity.mcpPort)) {
      const error = new Error("Server-side scene build identity is unavailable");
      error.code = "SCENE_BUILD_RUNTIME_UNAVAILABLE";
      error.status = 503;
      throw error;
    }
    const context = {
      ownerId: String(identity.ownerId),
      sessionId: String(identity.sessionId),
      ...(Number.isSafeInteger(identity.slotId) ? { slotId: identity.slotId } : {}),
      ...(typeof identity.leaseId === "string" ? { leaseId: identity.leaseId } : {}),
      ...(Number.isSafeInteger(identity.mcpPort) ? { mcpPort: identity.mcpPort } : {}),
    };
    return abortOnDisconnect ? buildAbortContext(req, res, context) : context;
  }

  function runId(req) {
    const value = String(req.params.runId || "");
    if (!RUN_ID_PATTERN.test(value)) {
      const error = new Error("Import run id is invalid");
      error.code = "SCENE_BUILD_IMPORT_ID_INVALID";
      error.status = 400;
      throw error;
    }
    return value;
  }

  function handler(operation, responseStatus = 200, input = (req) => req.body || {}, options = {}) {
    return async (req, res) => {
      try {
        const result = await service[operation](runId(req), input(req), requestContext(req, res, options));
        res.status(responseStatus).json(result);
      } catch (error) {
        const body = publicError(error);
        res.status(errorStatus(error, body)).json(body);
      }
    };
  }

  router.get("/:runId/build", handler("status", 200, (req) => (
    req.query.profile_id === undefined ? {} : { profile_id: String(req.query.profile_id) }
  )));
  router.post("/:runId/build/plan", handler("plan"));
  router.post("/:runId/build/preflight", handler("preflight"));
  router.post("/:runId/build/execute", handler("start", 202, (req) => req.body || {}, {
    abortOnDisconnect: false,
  }));

  return router;
}

module.exports = {
  createVistaSceneBuildRouter,
  errorStatus,
  publicError,
  publicExecutionResult,
};

"use strict";

const express = require("express");

const PUBLIC_MESSAGES = Object.freeze({
  VISTA_WORLD_ACCESS_INVALID: "An active Studio world session is required.",
  VISTA_WORLD_INPUT_INVALID: "The VISTA world request is invalid.",
  VISTA_WORLD_ACTION_UNSUPPORTED: "The requested world action is not supported.",
  VISTA_WORLD_NOT_FOUND: "The requested world resource was not found.",
  VISTA_WORLD_REVISION_NOT_FOUND: "The requested world revision was not found.",
  VISTA_WORLD_SESSION_NOT_FOUND: "The requested world session was not found.",
  VISTA_WORLD_SESSION_STALE: "The world session belongs to a stale Studio lease.",
  VISTA_WORLD_GENERATION_STALE: "The world session generation is stale.",
  VISTA_WORLD_EVENT_INCOMPATIBLE: "The event is incompatible with this world revision.",
  VISTA_WORLD_COMPILER_UNAVAILABLE: "The trusted VISTA world compiler is unavailable.",
  VISTA_WORLD_RUNTIME_UNAVAILABLE: "The typed Unreal world runtime is unavailable.",
  VISTA_WORLD_PROTOCOL_ERROR: "The typed Unreal world response is invalid.",
  VISTA_WORLD_CATALOG_INVALID: "The trusted VISTA world catalog is invalid.",
});

function publicError(error) {
  const raw = error && error.code;
  const code = typeof raw === "string" && Object.prototype.hasOwnProperty.call(PUBLIC_MESSAGES, raw)
    ? raw
    : "VISTA_WORLD_INTERNAL";
  return {
    error: PUBLIC_MESSAGES[code] || "VISTA world request failed.",
    code,
    retryable: Boolean(error && error.retryable),
  };
}

function errorStatus(error, body) {
  const status = [error && error.status, error && error.statusCode]
    .find((value) => Number.isInteger(value) && value >= 400 && value <= 599);
  if (status) return status;
  if (body.code.endsWith("_NOT_FOUND")) return 404;
  if (body.code.endsWith("_STALE") || body.code.endsWith("_INCOMPATIBLE")) return 409;
  if (body.code.endsWith("_UNAVAILABLE") || body.code === "VISTA_WORLD_ACCESS_INVALID") return 503;
  if (body.code === "VISTA_WORLD_INTERNAL" || body.code === "VISTA_WORLD_CATALOG_INVALID") return 500;
  return 400;
}

function abortContext(req, res, identity) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  req.once("aborted", abort);
  res.once("close", () => {
    if (!res.writableEnded) abort();
  });
  return { ...identity, signal: controller.signal };
}

function createVistaWorldRouter({ service, resolveIdentity } = {}) {
  const methods = ["compile", "revision", "createSession", "status", "action", "startEvent", "resetEvent"];
  if (!service || methods.some((method) => typeof service[method] !== "function")) {
    throw new TypeError("VISTA world service is required");
  }
  if (typeof resolveIdentity !== "function") {
    throw new TypeError("VISTA world resolveIdentity is required");
  }
  const router = express.Router();
  router.use((_req, res, next) => {
    res.set("Cache-Control", "no-store");
    next();
  });

  function context(req, res, abortOnDisconnect) {
    const identity = resolveIdentity(req) || {};
    return abortOnDisconnect ? abortContext(req, res, identity) : identity;
  }

  function handler(status, invoke, { abortOnDisconnect = false } = {}) {
    return async (req, res) => {
      try {
        const result = await invoke(service, req, context(req, res, abortOnDisconnect));
        res.status(status).json(result);
      } catch (error) {
        const body = publicError(error);
        res.status(errorStatus(error, body)).json(body);
      }
    };
  }

  router.post("/compile", handler(200, (world, req, identity) => world.compile(req.body || {}, identity), { abortOnDisconnect: true }));
  router.get("/revisions/:revision", handler(200, (world, req, identity) => world.revision(String(req.params.revision || ""), identity), { abortOnDisconnect: true }));
  router.post("/sessions", handler(201, (world, req, identity) => world.createSession(req.body || {}, identity)));
  router.get("/sessions/:sessionId", handler(200, (world, req, identity) => world.status(String(req.params.sessionId || ""), identity), { abortOnDisconnect: true }));
  router.post("/sessions/:sessionId/actions", handler(200, (world, req, identity) => world.action(String(req.params.sessionId || ""), req.body || {}, identity)));
  router.post("/sessions/:sessionId/events/:eventId/start", handler(200, (world, req, identity) => world.startEvent(String(req.params.sessionId || ""), String(req.params.eventId || ""), req.body || {}, identity)));
  router.post("/sessions/:sessionId/events/reset", handler(200, (world, req, identity) => world.resetEvent(String(req.params.sessionId || ""), req.body || {}, identity)));
  return router;
}

module.exports = {
  createVistaWorldRouter,
  errorStatus,
  publicError,
};

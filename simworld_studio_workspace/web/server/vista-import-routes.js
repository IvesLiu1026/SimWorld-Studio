"use strict";

const express = require("express");

const RUN_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;

function sanitizePublicMessage(value) {
  return String(value || "")
    .replace(/\b([a-z][a-z0-9+.-]*:\/\/)[^\s/@:]+(?::[^\s/@]*)?@/gi, "$1[redacted]@")
    .replace(/([?&](?:token|key|secret|password|signature|credential)=)[^&\s]+/gi, "$1[redacted]")
    .replace(/(?:\/[A-Za-z0-9._-]+){3,}/g, "[server-path]")
    .slice(0, 500);
}

function safeError(error) {
  const rawCode = error && error.code;
  const code = typeof rawCode === "string" && /^[A-Z0-9_]{2,80}$/.test(rawCode)
    ? rawCode
    : "VISTA_IMPORT_INTERNAL";
  const publicMessage = code === "VISTA_IMPORT_INTERNAL"
    ? "VISTA import failed"
    : sanitizePublicMessage(error && error.message || "VISTA import failed");
  return {
    error: publicMessage,
    code,
    retryable: Boolean(error && error.retryable),
  };
}

function importHttpStatus(code, error) {
  if (error && Number.isInteger(error.statusCode) && error.statusCode >= 400 && error.statusCode <= 599) {
    return error.statusCode;
  }
  if (code === "VISTA_IMPORT_NOT_FOUND") return 404;
  if (code === "VISTA_IMPORT_FORBIDDEN" || code === "VISTA_IMPORT_ACCESS_DENIED") return 403;
  if (code === "VISTA_IMPORT_CONFLICT") return 409;
  if (code === "VISTA_IMPORT_UNAVAILABLE"
    || code === "VISTA_DATASET_UNAVAILABLE"
    || code === "VISTA_IMPORT_STORAGE_UNAVAILABLE") return 503;
  if (code === "VISTA_IMPORT_INTERNAL") return 500;
  return 400;
}

function createVistaImportRouter({ service, ownerId, sessionId, resolveIdentity } = {}) {
  if (!service || typeof service.preview !== "function"
    || typeof service.commit !== "function" || typeof service.status !== "function") {
    throw new TypeError("VISTA import service with preview, commit, and status is required");
  }
  const identityResolver = typeof resolveIdentity === "function"
    ? resolveIdentity
    : () => ({ ownerId, sessionId });
  const router = express.Router();

  router.use((_req, res, next) => {
    res.set("Cache-Control", "no-store");
    next();
  });

  router.post("/preview", async (req, res) => {
    try {
      const result = await service.preview(req.body || {});
      res.json(result);
    } catch (error) {
      const body = safeError(error);
      res.status(importHttpStatus(body.code, error)).json(body);
    }
  });

  router.post("/", async (req, res) => {
    try {
      const identity = identityResolver(req) || {};
      if (!identity.ownerId || !identity.sessionId) {
        const error = new Error("Server-side import identity is unavailable");
        error.code = "VISTA_IMPORT_UNAVAILABLE";
        throw error;
      }
      const result = await service.commit(req.body || {}, {
        ownerId: String(identity.ownerId),
        sessionId: String(identity.sessionId),
      });
      res.status(result && result.created === false ? 200 : 201).json(result);
    } catch (error) {
      const body = safeError(error);
      res.status(importHttpStatus(body.code, error)).json(body);
    }
  });

  router.get("/:runId", async (req, res) => {
    try {
      const runId = String(req.params.runId || "");
      if (!RUN_ID_PATTERN.test(runId)) {
        const error = new Error("Import run id is invalid");
        error.code = "VISTA_IMPORT_INVALID";
        throw error;
      }
      const identity = identityResolver(req) || {};
      if (!identity.ownerId || !identity.sessionId) {
        const error = new Error("Server-side import identity is unavailable");
        error.code = "VISTA_IMPORT_UNAVAILABLE";
        throw error;
      }
      res.json(await service.status(runId, {
        ownerId: String(identity.ownerId),
        sessionId: String(identity.sessionId),
      }));
    } catch (error) {
      const body = safeError(error);
      res.status(importHttpStatus(body.code, error)).json(body);
    }
  });

  return router;
}

module.exports = {
  RUN_ID_PATTERN,
  createVistaImportRouter,
  importHttpStatus,
  sanitizePublicMessage,
  safeError,
};

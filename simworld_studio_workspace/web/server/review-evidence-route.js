"use strict";

function createReviewEvidenceHandler({ resolveScope, resolveReference } = {}) {
  if (typeof resolveScope !== "function" || typeof resolveReference !== "function") {
    throw new TypeError("review evidence route requires scope and reference resolvers");
  }
  return async function reviewEvidenceHandler(request, response) {
    const abortController = new AbortController();
    const onAborted = () => abortController.abort();
    if (request && typeof request.once === "function") request.once("aborted", onAborted);
    try {
      const queryKeys = Object.keys(request.query || {});
      if (queryKeys.some((key) => !["evidenceId", "conversationId"].includes(key))
          || !queryKeys.includes("evidenceId")
          || !queryKeys.includes("conversationId")) {
        return response.status(400).json({ error: "Invalid evidence request" });
      }
      const initialScope = resolveScope(request);
      const evidence = await resolveReference({
        scopeId: initialScope.scopeId,
        evidenceId: request.query && request.query.evidenceId,
        handle: request.params && request.params.handle,
        signal: abortController.signal,
      });
      const confirmedScope = resolveScope(request);
      if (confirmedScope.scopeId !== initialScope.scopeId) {
        return response.status(409).json({ error: "Review scope changed" });
      }
      if (!evidence || !Buffer.isBuffer(evidence.data)
          || !["image/png", "image/jpeg"].includes(evidence.mediaType)
          || evidence.size !== evidence.data.length) {
        throw new Error("invalid evidence response");
      }
      response.setHeader("Content-Type", evidence.mediaType);
      response.setHeader("Content-Length", String(evidence.size));
      response.setHeader("Cache-Control", "private, no-store, max-age=0");
      response.setHeader("Pragma", "no-cache");
      response.setHeader("X-Content-Type-Options", "nosniff");
      response.setHeader("Accept-Ranges", "none");
      return response.end(evidence.data);
    } catch (error) {
      if (error && error.code === "REVIEW_ACTIVE_SESSION_REQUIRED") {
        return response.status(error.statusCode || 401).json({ error: error.message, code: error.code });
      }
      return response.status(404).json({
        error: "Review evidence is unavailable",
        code: "REVIEW_EVIDENCE_UNAVAILABLE",
      });
    } finally {
      if (request && typeof request.removeListener === "function") {
        request.removeListener("aborted", onAborted);
      }
    }
  };
}

module.exports = { createReviewEvidenceHandler };

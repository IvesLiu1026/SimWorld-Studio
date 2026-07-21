"use strict";

const { getReviewRequestContext } = require("./review-loop-coordinator");

function createRuntimeMutationMiddleware({ arbiter, resolveContext = getReviewRequestContext } = {}) {
  if (!arbiter || typeof arbiter.acquire !== "function" || typeof arbiter.isHeld !== "function") {
    throw new TypeError("runtime mutation middleware requires an arbiter");
  }
  if (typeof resolveContext !== "function") throw new TypeError("resolveContext must be a function");
  return function runtimeMutationMiddleware(request, response, next) {
    const context = resolveContext(request);
    if (!context || !context.activeLease) return next();
    if (context.mutationToken) {
      if (!arbiter.isHeld(context.mutationToken)) {
        return response.status(409).json({
          code: "BUILDER_MUTATION_TOKEN_STALE",
          error: "The parent Review no longer owns this Unreal runtime.",
        });
      }
      return next();
    }

    let token;
    try {
      token = arbiter.acquire(context.activeLease, {
        kind: "builder_chat",
        operationId: context.runId,
      });
      token.invalidateScene();
    } catch (error) {
      if (error && error.code === "RUNTIME_MUTATION_SLOT_BUSY") {
        return response.status(409).json({
          code: "BUILDER_SLOT_BUSY",
          error: "This Studio slot already has an active runtime mutation.",
        });
      }
      return response.status(error.statusCode || 500).json({
        code: error.code || "BUILDER_MUTATION_LOCK_FAILED",
        error: "The Unreal runtime mutation lock is unavailable.",
      });
    }

    let released = false;
    const release = () => {
      if (released) return;
      released = true;
      try { token.release(); } catch { /* fail closed on the next acquisition */ }
    };
    response.once("finish", release);
    response.once("close", release);
    try {
      return next();
    } catch (error) {
      release();
      throw error;
    }
  };
}

module.exports = { createRuntimeMutationMiddleware };

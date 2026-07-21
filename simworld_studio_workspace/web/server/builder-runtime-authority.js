"use strict";

const crypto = require("node:crypto");

const { getReviewRequestContext } = require("./review-loop-coordinator");

const BUILDER_RUNTIME = Symbol("simworld.builderRuntime");
const TRUSTED_PROFILES = new Set(["trusted_proxy", "trusted-proxy", "public_webrtc", "public-webrtc"]);

function buildBuilderChildEnv(baseEnv = process.env, runtime = null) {
  const env = { ...baseEnv };
  if (!runtime || !runtime.capability) return env;
  for (const key of [
    "STUDIO_ACCESS_TOKEN",
    "STUDIO_ACCESS_TOKEN_FILE",
    "SIMWORLD_MCP_CONFIG",
    "UNREAL_HOST",
    "UNREAL_PORT",
    "UCV_HOST",
    "UCV_PORT",
    "UNREALCV_PORT",
  ]) delete env[key];
  env.SIMWORLD_INTERNAL_RUN_CAPABILITY = runtime.capability;
  env.SIMWORLD_INTERNAL_RUN_ID = runtime.runId;
  env.SIMWORLD_INTERNAL_CAPABILITY_REQUIRED = "1";
  env.SIMWORLD_BROKER_HOST = "127.0.0.1";
  env.PORT = String(runtime.serverPort);
  return env;
}

function getBuilderRuntime(request) {
  return request && request[BUILDER_RUNTIME] || null;
}

function attachBuilderRuntimeProcess(runtime, child) {
  if (runtime && typeof runtime.attachProcess === "function") runtime.attachProcess(child);
  return child;
}

function createBuilderRuntimeAuthority({
  transportProfile = "loopback",
  registry,
  mcpConfigPath,
  serverPort,
} = {}) {
  const trusted = TRUSTED_PROFILES.has(String(transportProfile || "").trim().toLowerCase());
  if (!trusted) {
    return Object.freeze({
      bind(_request, _response, next) { return next(); },
      get: getBuilderRuntime,
    });
  }
  if (!registry || typeof registry.issue !== "function" || typeof registry.attachProcess !== "function") {
    throw new TypeError("trusted builder runtime requires an internal capability registry");
  }
  if (typeof mcpConfigPath !== "string" || !mcpConfigPath) {
    throw new TypeError("trusted builder runtime requires a brokered MCP config path");
  }
  const port = Number(serverPort);
  if (!Number.isSafeInteger(port) || port < 1 || port > 65535) {
    throw new TypeError("trusted builder runtime requires a valid server port");
  }

  function bind(request, response, next) {
    const context = getReviewRequestContext(request);
    if (!context || !context.activeLease || !context.scopeId) {
      return response.status(401).json({
        code: "BUILDER_ACTIVE_SESSION_REQUIRED",
        error: "An active Studio streaming lease is required for the builder.",
      });
    }
    try {
      const issued = registry.issue({
        identity: context.activeLease,
        runId: context.runId || crypto.randomUUID(),
        scopeId: context.scopeId,
      });
      const runtime = Object.freeze({
        ...issued,
        activeLease: context.activeLease,
        mcpConfigPath,
        serverPort: port,
        unrealPort: null,
        attachProcess(child) { registry.attachProcess(issued.capability, child); },
        revoke(reason = "builder_revoked") { return registry.revoke(issued.capability, reason); },
      });
      Object.defineProperty(request, BUILDER_RUNTIME, {
        value: runtime,
        configurable: false,
        enumerable: false,
        writable: false,
      });
      return next();
    } catch (error) {
      return response.status(error.statusCode || 409).json({
        code: error.code || "BUILDER_RUNTIME_UNAVAILABLE",
        error: error.message || "The builder runtime is unavailable.",
      });
    }
  }

  return Object.freeze({ bind, get: getBuilderRuntime });
}

module.exports = {
  BUILDER_RUNTIME,
  attachBuilderRuntimeProcess,
  buildBuilderChildEnv,
  createBuilderRuntimeAuthority,
  getBuilderRuntime,
};

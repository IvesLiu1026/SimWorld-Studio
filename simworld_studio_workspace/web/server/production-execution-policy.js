"use strict";

const { isInternalCapabilityCandidate } = require("./internal-run-capability");

const PRODUCTION_SAFETY_VALUES = new Set(["1", "true", "enforced"]);
const DISABLED_SAFETY_VALUES = new Set(["", "0", "false"]);
const PRODUCTION_MCP_BLOCKED_TOOLS = new Set([
  "execute_python_script",
  "read_job_log",
]);
const PRODUCTION_NLP_MUTATION_TOOLS = new Set([
  "spawn_blueprint_actor",
  "spawn_actor",
  "delete_actor",
  "delete_all_spawned",
  "set_actor_transform",
  "setup_environment",
  "spawn_agent",
  "agent_stop",
  "agent_rotate",
  "agent_action",
  "set_camera",
  "set_actor_color",
  "save_scene_as",
]);
const PRODUCTION_INTERNAL_UE_READ_TYPES = new Set([
  "find_actors_by_name",
  "get_actors_in_level",
]);
const ASSET_CATEGORIES = new Set([
  "agricultural_props", "barriers_and_fencing", "building_pieces", "buildings",
  "camping_outdoor", "carts_and_vendors", "decor_and_landmarks", "furniture_indoor",
  "ground_and_road", "indoor_clutter", "industrial_goods", "lighting",
  "litter_and_debris", "market_goods", "medieval_fantasy_props", "nature_terrain",
  "pipes_tanks_infra", "religious_ritual", "sci_fi_props", "seating", "signage",
  "tools_equipment", "vegetation", "vehicles", "waste_and_bins", "winter_snow_props",
]);

function productionExecutionLocked(env = process.env) {
  if (String(env.NODE_ENV || "").trim().toLowerCase() === "production") return true;

  const raw = String(env.SIMWORLD_PRODUCTION_SAFETY || "").trim().toLowerCase();
  if (PRODUCTION_SAFETY_VALUES.has(raw)) return true;
  if (DISABLED_SAFETY_VALUES.has(raw)) return false;
  throw new Error("SIMWORLD_PRODUCTION_SAFETY must be 1, true, enforced, 0, or false");
}

function productionMcpToolDecision(name, env = process.env) {
  const toolName = String(name || "");
  if (toolName === "verify_scene") {
    return Object.freeze({
      allowed: false,
      code: "REVIEW_COORDINATOR_REQUIRED",
      message: "verify_scene is retired; use the coordinator-managed Text or Visual Review mode.",
    });
  }
  if (!productionExecutionLocked(env)) return Object.freeze({ allowed: true });
  if (PRODUCTION_NLP_MUTATION_TOOLS.has(toolName)) {
    return Object.freeze({
      allowed: false,
      code: "NLP_TYPED_MUTATION_UNAVAILABLE",
      message: "Free-form NLP scene mutation is unavailable until a typed mutation adapter is installed.",
    });
  }
  if (PRODUCTION_MCP_BLOCKED_TOOLS.has(toolName)) {
    return Object.freeze({
      allowed: false,
      code: "GENERIC_UE_EXECUTION_DISABLED",
      message: "Generic Unreal execution is unavailable in production",
    });
  }
  return Object.freeze({ allowed: true });
}

function productionMcpToolAllowed(name, env = process.env) {
  return productionMcpToolDecision(name, env).allowed;
}

function normalizedRequestPath(req) {
  const raw = String((req && (req.path || req.url)) || "").split("?", 1)[0];
  return raw.length > 1 ? raw.replace(/\/+$/, "") : raw;
}

function isPlainObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function hasOnlyKeys(value, allowed) {
  return isPlainObject(value) && Object.keys(value).every((key) => allowed.has(key));
}

function internalUeReadAllowed(body) {
  if (!isPlainObject(body) || !hasOnlyKeys(body, new Set(["type", "params", "timeoutMs"]))) return false;
  if (!PRODUCTION_INTERNAL_UE_READ_TYPES.has(body.type)) return false;
  if (body.timeoutMs !== undefined && (!Number.isFinite(body.timeoutMs) || body.timeoutMs < 1 || body.timeoutMs > 120000)) {
    return false;
  }

  const params = body.params === undefined ? {} : body.params;
  if (body.type === "get_actors_in_level") return hasOnlyKeys(params, new Set());
  return hasOnlyKeys(params, new Set(["pattern"])) &&
    typeof params.pattern === "string" &&
    params.pattern.length > 0 &&
    params.pattern.length <= 256 &&
    !/[\u0000-\u001f\u007f]/.test(params.pattern);
}

function internalUcvReadAllowed(body) {
  if (!isPlainObject(body) || !hasOnlyKeys(body, new Set(["cmd", "timeoutMs", "retries", "queueDeadlineMs"]))) {
    return false;
  }
  if (typeof body.cmd !== "string" || body.cmd.length > 512 || !/^vget\s+\/[A-Za-z0-9_./ -]+$/.test(body.cmd)) {
    return false;
  }
  for (const key of ["timeoutMs", "retries", "queueDeadlineMs"]) {
    if (body[key] !== undefined && (!Number.isFinite(body[key]) || body[key] < 0 || body[key] > 120000)) return false;
  }
  return true;
}

function internalAssetSearchAllowed(body) {
  if (!isPlainObject(body) || !hasOnlyKeys(body, new Set(["query", "category", "k"]))) return false;
  const query = typeof body.query === "string" ? body.query.trim() : "";
  if (!query || query.length > 1000 || /[\u0000-\u001f\u007f]/.test(query)) return false;
  if (body.category !== undefined && !ASSET_CATEGORIES.has(body.category)) return false;
  return body.k === undefined || (Number.isSafeInteger(body.k) && body.k >= 1 && body.k <= 40);
}

function fixedCameraOperationAllowed(body) {
  if (!isPlainObject(body) || !hasOnlyKeys(body, new Set(["cmd", "args"]))) return false;
  const args = body.args === undefined ? [] : body.args;
  if (!Array.isArray(args)) return false;
  if (body.cmd === "get_camera") return args.length === 0;
  return body.cmd === "set_camera" && args.length === 6 && args.every(
    (value) => typeof value === "number" && Number.isFinite(value) && Math.abs(value) <= 1000000,
  );
}

function denyGenericExecution(res) {
  return res.status(403).json({
    code: "GENERIC_UE_EXECUTION_DISABLED",
    error: "Generic Unreal execution is unavailable in production",
  });
}

function internalCapabilityOperationPolicy({ channel, body } = {}, env = process.env) {
  if (!productionExecutionLocked(env)) return Object.freeze({ allowed: true });
  const allowed = channel === "ue"
    ? internalUeReadAllowed(body)
    : channel === "ucv"
      ? internalUcvReadAllowed(body)
      : channel === "assets" && internalAssetSearchAllowed(body);
  if (allowed) return Object.freeze({ allowed: true });
  return Object.freeze({
    allowed: false,
    code: "NLP_TYPED_MUTATION_UNAVAILABLE",
    message: "Free-form NLP scene mutation is unavailable until a typed mutation adapter is installed.",
    statusCode: 403,
  });
}

function createProductionExecutionGuard({ env = process.env } = {}) {
  const locked = productionExecutionLocked(env);
  return function productionExecutionGuard(req, res, next) {
    if (!locked || String(req.method || "GET").toUpperCase() !== "POST") return next();

    const requestPath = normalizedRequestPath(req);
    if (requestPath === "/api/ue-command") return denyGenericExecution(res);
    if (requestPath === "/api/camera" && !fixedCameraOperationAllowed(req.body)) {
      return denyGenericExecution(res);
    }
    if (isInternalCapabilityCandidate(req)) return next();
    if (requestPath === "/api/internal/ue" && !internalUeReadAllowed(req.body)) {
      return denyGenericExecution(res);
    }
    if (requestPath === "/api/internal/ucv" && !internalUcvReadAllowed(req.body)) {
      return denyGenericExecution(res);
    }
    return next();
  };
}

module.exports = {
  createProductionExecutionGuard,
  fixedCameraOperationAllowed,
  internalAssetSearchAllowed,
  internalUcvReadAllowed,
  internalUeReadAllowed,
  internalCapabilityOperationPolicy,
  productionExecutionLocked,
  productionMcpToolAllowed,
  productionMcpToolDecision,
};

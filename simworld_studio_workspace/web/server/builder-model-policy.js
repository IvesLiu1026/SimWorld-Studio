"use strict";

const DEFAULT_CLAUDE_BUILDER_MODEL = "claude-opus-4-8";
const DEFAULT_BUILDER_AGENT = "claude";

class BuilderModelPolicyError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "BuilderModelPolicyError";
    this.code = code;
  }
}

function normalizeModel(value, label) {
  const model = String(value || "").trim();
  if (!model || model.length > 128 || /[\u0000-\u001f\u007f]/.test(model)) {
    throw new BuilderModelPolicyError(
      "BUILDER_MODEL_INVALID",
      `${label} must be a non-empty model identifier`,
    );
  }
  return model;
}

function resolveClaudeBuilderModel(requestedModel, env = process.env) {
  const production = String(env.NODE_ENV || "").trim().toLowerCase() === "production";
  const deploymentModel = normalizeModel(
    env.BUILDER_MODEL || env.CLAUDE_MODEL || DEFAULT_CLAUDE_BUILDER_MODEL,
    "Claude builder deployment model",
  );

  if (production) {
    if (deploymentModel !== DEFAULT_CLAUDE_BUILDER_MODEL) {
      throw new BuilderModelPolicyError(
        "BUILDER_MODEL_PIN_MISMATCH",
        `Production Claude builder must be pinned to ${DEFAULT_CLAUDE_BUILDER_MODEL}`,
      );
    }
    return deploymentModel;
  }

  return requestedModel == null || String(requestedModel).trim() === ""
    ? deploymentModel
    : normalizeModel(requestedModel, "Requested Claude builder model");
}

function resolveBuilderAgent(requestedAgent, env = process.env) {
  const production = String(env.NODE_ENV || "").trim().toLowerCase() === "production";
  if (production) return DEFAULT_BUILDER_AGENT;
  const agent = String(requestedAgent || DEFAULT_BUILDER_AGENT).trim().toLowerCase();
  if (!/^[a-z][a-z0-9_-]{0,31}$/.test(agent)) {
    throw new BuilderModelPolicyError(
      "BUILDER_AGENT_INVALID",
      "Requested builder agent is invalid",
    );
  }
  return agent;
}

function resolveCodingAgentRegistry(registry, env = process.env) {
  const source = registry && typeof registry === "object" && !Array.isArray(registry)
    ? registry
    : {};
  const sourceAgents = source.agents && typeof source.agents === "object" && !Array.isArray(source.agents)
    ? source.agents
    : {};
  const production = String(env.NODE_ENV || "").trim().toLowerCase() === "production";
  const out = {};

  for (const [id, entry] of Object.entries(sourceAgents)) {
    if (!/^[a-z][a-z0-9_-]{0,31}$/.test(id) || !entry || typeof entry !== "object") continue;
    if (production && id !== DEFAULT_BUILDER_AGENT) continue;
    if (production) {
      const model = resolveClaudeBuilderModel(null, env);
      out[id] = {
        label: String(entry.label || id),
        defaultModel: model,
        models: [model],
      };
    } else {
      out[id] = {
        label: String(entry.label || id),
        defaultModel: String(entry.defaultModel || ""),
        models: Array.isArray(entry.models) ? entry.models.map(String) : [],
      };
    }
  }

  if (production && !out[DEFAULT_BUILDER_AGENT]) {
    const model = resolveClaudeBuilderModel(null, env);
    out[DEFAULT_BUILDER_AGENT] = {
      label: "Claude Code",
      defaultModel: model,
      models: [model],
    };
  }

  return {
    default: production
      ? DEFAULT_BUILDER_AGENT
      : resolveBuilderAgent(source.default, env),
    agents: out,
  };
}

module.exports = {
  BuilderModelPolicyError,
  DEFAULT_BUILDER_AGENT,
  DEFAULT_CLAUDE_BUILDER_MODEL,
  resolveBuilderAgent,
  resolveClaudeBuilderModel,
  resolveCodingAgentRegistry,
};

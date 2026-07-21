"use strict";

const DEFAULT_CLAUDE_BUILDER_MODEL = "claude-opus-4-8";

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

module.exports = {
  BuilderModelPolicyError,
  DEFAULT_CLAUDE_BUILDER_MODEL,
  resolveClaudeBuilderModel,
};

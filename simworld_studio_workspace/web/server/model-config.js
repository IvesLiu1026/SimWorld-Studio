"use strict";
// Single source of truth for which model a request uses, so the scene-coder and the
// asset-retrieval LLM calls always resolve to the SAME model. Mirrors the vanilla
// /api/chat claude path exactly: req.body.model -> CLAUDE_MODEL env -> "" (CLI default).
// Import this in BOTH the coder spawn and the retrieval module; change the model in one
// place (UI dropdown / CLAUDE_MODEL) and both move together.
function resolveModel(reqBody) {
  const b = reqBody || {};
  return String(b.model || process.env.CLAUDE_MODEL || "").trim();
}
module.exports = { resolveModel };

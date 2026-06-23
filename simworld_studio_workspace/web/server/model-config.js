"use strict";
// Single source of truth for which model a request uses, so the scene-coder and the
// asset-retrieval LLM calls resolve consistently.
function resolveModel(reqBody) {
  const b = reqBody || {};
  const runner = String(b.runner || process.env.LLM_PROVIDER || "").trim().toLowerCase();
  if (["codex", "gpt", "gpt-5.5", "gpt5.5", "openai"].includes(runner)) {
    const requested = String(b.model || "").trim();
    if (requested && !/^claude-/i.test(requested)) return requested;
    return String(process.env.CODEX_MODEL || "gpt-5.5").trim();
  }
  return String(b.model || process.env.CLAUDE_MODEL || "").trim();
}
module.exports = { resolveModel };

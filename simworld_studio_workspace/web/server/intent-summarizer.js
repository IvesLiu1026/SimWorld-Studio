"use strict";
// Rolling per-session user-intent summary with recency-wins on contradictions.
// Each new user prompt fires a small one-shot LLM call that takes the prior summary +
// the new prompt and returns an updated summary. If the new prompt contradicts prior intent,
// the new prompt wins. Stays concise — ~1-2 short paragraphs OR up to ~8 bullets.
const { oneshotText, normalizeProvider, codexModel } = require("./llm-oneshot");

const SUMMARIZER_SYSTEM_PROMPT = `You maintain a brief rolling SUMMARY of what the user wants for a 3D scene being built incrementally.

You will be given the PRIOR summary (may be empty) and the user's NEW prompt. Produce the UPDATED summary that:
- Captures the CUMULATIVE intent across all prompts.
- If the NEW prompt CONTRADICTS the prior summary, the NEW prompt WINS (recency-wins). Drop anything the user has revoked or overridden.
- Keeps it CONCISE: 1-2 short paragraphs OR up to ~8 bullet points. No preamble, no commentary, no explanation of what you changed.
- Output ONLY the updated summary text, nothing else.`;

async function updateIntentSummary({
  priorSummary,
  newPrompt,
  model,
  timeoutMs = 60000,
  provider,
  runner,
  signal,
  env,
  maxBudgetUsd,
  maxInputTokens,
  maxOutputTokens,
  onAccounting,
}) {
  const prior = String(priorSummary || "").trim() || "(no prior — this is the first prompt)";
  const prompt =
    SUMMARIZER_SYSTEM_PROMPT + "\n\n" +
    "PRIOR SUMMARY:\n" + prior + "\n\n" +
    "NEW USER PROMPT:\n" + String(newPrompt || "").trim() + "\n\n" +
    "Output the updated rolling summary (only the summary text):";
  const runtimeEnv = env && typeof env === "object" ? env : process.env;
  const selectedProvider = normalizeProvider(provider || runner || runtimeEnv.LLM_PROVIDER) || "claude";
  const selectedModel = selectedProvider === "codex" ? codexModel(model) : model;
  const summary = String(await oneshotText(prompt, {
    provider: selectedProvider,
    model: selectedModel,
    timeoutMs,
    signal,
    env: runtimeEnv,
    maxBudgetUsd,
    maxInputTokens,
    maxOutputTokens,
    onAccounting,
    reasoningEffort: selectedProvider === "codex" ? (runtimeEnv.SUMMARIZER_REASONING_EFFORT || "high") : undefined,
    telemetryComponent: "summarizer",
  }) || "").trim();
  if (!summary) throw new Error("summarizer returned empty summary");
  return summary;
}

module.exports = { updateIntentSummary, SUMMARIZER_SYSTEM_PROMPT };

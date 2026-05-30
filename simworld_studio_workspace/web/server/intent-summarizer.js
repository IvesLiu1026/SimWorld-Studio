"use strict";
// Rolling per-session user-intent summary with recency-wins on contradictions.
// Each new user prompt fires a small claude -p (Sonnet by default) that takes the prior summary +
// the new prompt and returns an updated summary. If the new prompt contradicts prior intent,
// the new prompt wins. Stays concise — ~1-2 short paragraphs OR up to ~8 bullets.
const { spawn } = require("child_process");
const path = require("path");

const NL = String.fromCharCode(10);

const SUMMARIZER_SYSTEM_PROMPT = `You maintain a brief rolling SUMMARY of what the user wants for a 3D scene being built incrementally.

You will be given the PRIOR summary (may be empty) and the user's NEW prompt. Produce the UPDATED summary that:
- Captures the CUMULATIVE intent across all prompts.
- If the NEW prompt CONTRADICTS the prior summary, the NEW prompt WINS (recency-wins). Drop anything the user has revoked or overridden.
- Keeps it CONCISE: 1-2 short paragraphs OR up to ~8 bullet points. No preamble, no commentary, no explanation of what you changed.
- Output ONLY the updated summary text, nothing else.`;

async function updateIntentSummary({ priorSummary, newPrompt, model, timeoutMs = 60000 }) {
  const prior = String(priorSummary || "").trim() || "(no prior — this is the first prompt)";
  const prompt =
    "PRIOR SUMMARY:\n" + prior + "\n\n" +
    "NEW USER PROMPT:\n" + String(newPrompt || "").trim() + "\n\n" +
    "Output the updated rolling summary (only the summary text):";

  const CLAUDE = process.env.CLAUDE_BIN || "claude";
  const args = ["-p", prompt, "--output-format", "json", "--dangerously-skip-permissions", "--append-system-prompt", SUMMARIZER_SYSTEM_PROMPT];
  if (model) args.push("--model", model);

  return new Promise((resolve, reject) => {
    const env = { ...process.env };
    Object.keys(env).forEach((k) => { if (k.startsWith("CLAUDE")) delete env[k]; });
    const p = spawn(CLAUDE, args, { stdio: ["ignore", "pipe", "pipe"], cwd: path.resolve(__dirname, ".."), env });
    const timer = setTimeout(() => { try { p.kill("SIGTERM"); } catch (_e) {} reject(new Error("summarizer timed out")); }, timeoutMs);
    let out = "", err = "";
    p.stdout.on("data", (d) => { out += d.toString(); });
    p.stderr.on("data", (d) => { err += d.toString(); });
    p.on("error", (e) => { clearTimeout(timer); reject(e); });
    p.on("close", (code) => {
      clearTimeout(timer);
      if (code !== 0) return reject(new Error(`summarizer exited ${code}: ${err.slice(0, 200)}`));
      let parsed;
      try { parsed = JSON.parse(out); } catch { return reject(new Error("summarizer non-JSON output: " + out.slice(0, 200))); }
      if (parsed.is_error) return reject(new Error("summarizer claude error: " + (parsed.error || JSON.stringify(parsed).slice(0, 200))));
      const summary = String(parsed.result || "").trim();
      if (!summary) return reject(new Error("summarizer returned empty summary"));
      resolve(summary);
    });
  });
}

module.exports = { updateIntentSummary, SUMMARIZER_SYSTEM_PROMPT };

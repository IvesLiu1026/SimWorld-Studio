"use strict";
// Lightweight one-shot LLM call via the claude CLI (no MCP, no tools) — reuses the box's
// OAuth login and honors --model, so retrieval follows the same model as the coder.
// Mirrors the known-good spawn/stream-json parsing pattern in skill-maker.js.
const { spawn } = require("child_process");
const path = require("path");
const NL = String.fromCharCode(10);

function oneshotText(prompt, opts) {
  const o = opts || {};
  const claudeBin = String(o.claudeBin || process.env.CLAUDE_BIN || "claude");
  const model = o.model == null || o.model === "" ? null : String(o.model);
  const timeoutMs = Math.max(10000, Number(o.timeoutMs || 120000));
  const args = ["-p", String(prompt || ""), "--output-format", "stream-json",
    "--include-partial-messages", "--verbose", "--dangerously-skip-permissions"];
  if (model) args.push("--model", model);
  return new Promise((resolve, reject) => {
    const env = { ...process.env };
    Object.keys(env).forEach(k => { if (k.startsWith("CLAUDE")) delete env[k]; });
    const proc = spawn(claudeBin, args, {
      cwd: o.cwd || path.resolve(__dirname, ".."), env, stdio: ["ignore", "pipe", "pipe"],
    });
    let outBuf = "", errBuf = "", assistantText = "", resultText = "", isErr = false;
    const timer = setTimeout(() => { try { proc.kill("SIGTERM"); } catch {} reject(new Error("oneshot timed out")); }, timeoutMs);
    function handle(line) {
      let e; try { e = JSON.parse(String(line || "").trim()); } catch { return; }
      if (!e) return;
      if (e.type === "assistant") {
        const blocks = e.message && Array.isArray(e.message.content) ? e.message.content : [];
        for (const b of blocks) if (b.type === "text" && b.text) assistantText += String(b.text);
      } else if (e.type === "result") {
        isErr = Boolean(e.is_error || e.subtype === "error_during_turn");
        if (typeof e.result === "string") resultText += e.result;
      }
    }
    proc.stdout.on("data", c => { outBuf += c.toString(); const ls = outBuf.split(NL); outBuf = ls.pop() || ""; for (const l of ls) if (l.trim()) handle(l); });
    proc.stderr.on("data", c => { errBuf += c.toString(); });
    proc.on("error", err => { clearTimeout(timer); reject(err); });
    proc.on("close", code => {
      clearTimeout(timer); if (outBuf.trim()) handle(outBuf);
      const raw = (resultText.trim() || assistantText.trim());
      if (isErr || code !== 0) return reject(new Error(`oneshot exited ${code}: ${errBuf.slice(0, 200)}`));
      resolve(raw);
    });
  });
}

// Extract a JSON value from model text (tolerates prose / ``` fences / leading commentary).
function extractJSON(raw) {
  const t = String(raw || "").trim();
  const fence = t.match(/```(?:json)?\s*([\s\S]*?)```/i);
  const body = (fence ? fence[1] : t).trim();
  try { return JSON.parse(body); } catch {}
  for (const [open, close] of [["{", "}"], ["[", "]"]]) {
    const i = body.indexOf(open), j = body.lastIndexOf(close);
    if (i >= 0 && j > i) { try { return JSON.parse(body.slice(i, j + 1)); } catch {} }
  }
  throw new Error("no JSON found in model output: " + body.slice(0, 200));
}

async function oneshotJSON(prompt, opts) {
  return extractJSON(await oneshotText(prompt, opts));
}

module.exports = { oneshotText, oneshotJSON, extractJSON };

"use strict";
// Lightweight one-shot LLM calls for retrieval/routing helpers.
// Default stays Claude for normal Studio use, but experiment runs can force Codex/GPT by
// passing { provider: "codex" } or setting LLM_PROVIDER=codex.
const { spawn } = require("child_process");
const path = require("path");
const telemetry = require("./telemetry");
const NL = String.fromCharCode(10);

function normalizeProvider(v) {
  const s = String(v || "").trim().toLowerCase();
  if (!s) return null;
  if (["codex", "gpt", "gpt-5.5", "gpt5.5", "openai"].includes(s)) return "codex";
  if (["claude", "anthropic"].includes(s)) return "claude";
  return null;
}

function providerFromOpts(o) {
  return normalizeProvider(o.provider || o.runner || process.env.LLM_ONESHOT_PROVIDER || process.env.LLM_PROVIDER) || "claude";
}

function parseCodexJsonl(stdout) {
  const trace = { thread_id: null, usage: null, last_agent_text: "", raw_event_count: 0 };
  for (const line of String(stdout || "").split(/\r?\n/)) {
    if (!line.trim()) continue;
    let event;
    try { event = JSON.parse(line); } catch { continue; }
    trace.raw_event_count += 1;
    if (event.type === "thread.started") {
      trace.thread_id = event.thread_id || (event.payload && event.payload.thread_id) || null;
    } else if (event.type === "turn.completed") {
      trace.usage = event.usage || (event.payload && event.payload.usage) || null;
    } else if (event.type === "item.completed") {
      const item = event.item || event.payload || {};
      if (item.type === "agent_message" || item.type === "message") {
        const text = item.text || (Array.isArray(item.content)
          ? item.content.filter(c => c && (c.type === "output_text" || c.type === "text")).map(c => c.text || "").join("")
          : "");
        if (text) trace.last_agent_text = text;
      }
    }
  }
  return trace;
}

function oneshotTextClaude(prompt, opts) {
  const o = opts || {};
  const claudeBin = String(o.claudeBin || process.env.CLAUDE_BIN || "claude");
  const model = o.model == null || o.model === "" ? null : String(o.model);
  const timeoutMs = Math.max(10000, Number(o.timeoutMs || process.env.LLM_ONESHOT_TIMEOUT_MS || 120000));
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

function codexModel(model) {
  const m = String(model || "").trim();
  if (m && !/^claude-/i.test(m)) return m;
  return String(process.env.CODEX_MODEL || "gpt-5.5");
}

function oneshotTextCodex(prompt, opts) {
  const o = opts || {};
  const codexBin = String(o.codexBin || process.env.CODEX_BIN || "codex");
  const model = codexModel(o.model);
  const timeoutMs = Math.max(10000, Number(o.timeoutMs || process.env.LLM_ONESHOT_TIMEOUT_MS || 120000));
  const cwd = o.cwd || process.env.CODEX_ONESHOT_CWD || "/tmp";
  const t0 = Date.now();
  const args = [
    "exec",
    "--json",
    "--skip-git-repo-check",
    "--dangerously-bypass-approvals-and-sandbox",
    "-C", cwd,
    "-m", model,
    ...(o.reasoningEffort ? ["-c", `model_reasoning_effort=${o.reasoningEffort}`] : []),
    "-",
  ];
  return new Promise((resolve, reject) => {
    const env = { ...process.env, NO_COLOR: "1" };
    Object.keys(env).forEach(k => { if (k.startsWith("CLAUDE")) delete env[k]; });
    const proc = spawn(codexBin, args, { cwd, env, stdio: ["pipe", "pipe", "pipe"] });
    let stdout = "", stderr = "";
    const timer = setTimeout(() => { try { proc.kill("SIGTERM"); } catch {} reject(new Error("codex oneshot timed out")); }, timeoutMs);
    try {
      proc.stdin.write(String(prompt || ""));
      proc.stdin.end();
    } catch (e) {
      clearTimeout(timer);
      reject(e);
      return;
    }
    proc.stdout.on("data", c => { stdout += c.toString(); });
    proc.stderr.on("data", c => { stderr += c.toString(); });
    proc.on("error", err => { clearTimeout(timer); reject(err); });
    proc.on("close", code => {
      clearTimeout(timer);
      const trace = parseCodexJsonl(stdout);
      try { telemetry.record({ component: o.telemetryComponent || "oneshot", model, reasoning: o.reasoningEffort || null, durationMs: Date.now() - t0, usage: telemetry.normUsage(trace.usage) }); } catch (_e) {}
      const raw = String(trace.last_agent_text || "").trim();
      if (code !== 0) return reject(new Error(`codex oneshot exited ${code}: ${stderr.slice(0, 500)}`));
      if (!raw) return reject(new Error(`codex oneshot returned empty output: ${stderr.slice(0, 500)}`));
      resolve(raw);
    });
  });
}

function oneshotText(prompt, opts) {
  const o = opts || {};
  if (providerFromOpts(o) === "codex") return oneshotTextCodex(prompt, o);
  return oneshotTextClaude(prompt, o);
}

// Extract a JSON value from model text (tolerates prose / ``` fences / leading commentary).
// Best-effort repair of a TRUNCATED JSON value (model hit its output-token cap mid-object): close a
// dangling string, drop the trailing incomplete fragment, and close open braces/brackets in order.
// Only ever used after normal parsing has already failed, so any success is strictly a win.
function _closeTruncatedJSON(body) {
  const start = body.search(/[{[]/);
  if (start < 0) return null;
  let s = body.slice(start);
  const stack = []; let inStr = false, esc = false;
  for (let k = 0; k < s.length; k++) {
    const c = s[k];
    if (inStr) { if (esc) esc = false; else if (c === "\\") esc = true; else if (c === '"') inStr = false; continue; }
    if (c === '"') inStr = true;
    else if (c === "{") stack.push("}");
    else if (c === "[") stack.push("]");
    else if (c === "}" || c === "]") stack.pop();
  }
  if (inStr) s = s.replace(/"[^"]*$/, "");                       // drop a dangling unterminated string
  s = s.replace(/[,\s]*$/, "");                                  // trailing comma/space
  s = s.replace(/,?\s*"[^"]*"\s*:\s*$/, "");                     // dangling "key": whose value was truncated
  s = s.replace(/,\s*"[^"]*"\s*$/, "");                          // dangling ,"key" truncated before its colon
  s = s.replace(/[,\s]*$/, "");
  for (let k = stack.length - 1; k >= 0; k--) s += stack[k];
  return s;
}

function extractJSON(raw) {
  const t = String(raw || "").trim();
  const fence = t.match(/```(?:json)?\s*([\s\S]*?)```/i);
  const body = (fence ? fence[1] : t).trim();
  try { return JSON.parse(body); } catch {}
  for (const [open, close] of [["{", "}"], ["[", "]"]]) {
    const i = body.indexOf(open), j = body.lastIndexOf(close);
    if (i >= 0 && j > i) { try { return JSON.parse(body.slice(i, j + 1)); } catch {} }
  }
  const repaired = _closeTruncatedJSON(body);                    // truncation fallback
  if (repaired) { try { return JSON.parse(repaired); } catch {} }
  throw new Error("no JSON found in model output: " + body.slice(0, 200));
}

async function oneshotJSON(prompt, opts) {
  return extractJSON(await oneshotText(prompt, opts));
}

module.exports = {
  oneshotText,
  oneshotJSON,
  extractJSON,
  normalizeProvider,
  providerFromOpts,
  parseCodexJsonl,
  codexModel,
};

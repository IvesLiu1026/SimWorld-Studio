"use strict";
// Lightweight one-shot LLM calls for retrieval/routing helpers.
// Default stays Claude for normal Studio use, but experiment runs can force Codex/GPT by
// passing { provider: "codex" } or setting LLM_PROVIDER=codex.
const { spawn } = require("child_process");
const path = require("path");
const telemetry = require("./telemetry");
const NL = String.fromCharCode(10);

class LlmOneShotError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = "LlmOneShotError";
    this.code = code;
    this.retryable = Boolean(details.retryable);
    this.provider = details.provider || null;
    this.model = details.model || null;
    if (details.cause) this.cause = details.cause;
  }
}

function oneShotError(code, message, details) {
  return new LlmOneShotError(code, message, details);
}

function oneShotTimeout(value) {
  const timeoutMs = Number(value == null || value === "" ? 120000 : value);
  if (!Number.isFinite(timeoutMs) || timeoutMs < 1 || timeoutMs > 1800000) {
    throw oneShotError("LLM_ONESHOT_CONFIG_INVALID", "One-shot timeout must be between 1 and 1800000 milliseconds");
  }
  return Math.floor(timeoutMs);
}

function normalizeProvider(v) {
  const s = String(v || "").trim().toLowerCase();
  if (!s) return null;
  if (["codex", "gpt", "gpt-5.5", "gpt5.5", "openai"].includes(s)) return "codex";
  if (["claude", "anthropic"].includes(s)) return "claude";
  return null;
}

function providerFromOpts(o) {
  const raw = o.provider || o.runner || process.env.LLM_ONESHOT_PROVIDER || process.env.LLM_PROVIDER;
  if (!raw) return "claude";
  const provider = normalizeProvider(raw);
  if (!provider) {
    throw oneShotError("LLM_PROVIDER_INVALID", `Unsupported one-shot provider: ${String(raw).trim()}`);
  }
  return provider;
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
  const timeoutMs = oneShotTimeout(o.timeoutMs == null ? process.env.LLM_ONESHOT_TIMEOUT_MS : o.timeoutMs);
  const signal = o.signal;
  const args = [
    "--print",
    "--input-format", "text",
    "--output-format", "stream-json",
    "--include-partial-messages",
    "--verbose",
    "--safe-mode",
    "--disable-slash-commands",
    "--tools", "",
    "--permission-mode", "dontAsk",
    "--strict-mcp-config",
    "--mcp-config", "{}",
    "--no-session-persistence",
  ];
  if (model) args.push("--model", model);
  return new Promise((resolve, reject) => {
    if (signal && signal.aborted) {
      reject(oneShotError("LLM_ONESHOT_ABORTED", "Claude one-shot was aborted before start", { provider: "claude", model }));
      return;
    }
    const env = { ...process.env };
    Object.keys(env).forEach(k => { if (k.startsWith("CLAUDE")) delete env[k]; });
    let proc;
    try {
      proc = (o.spawnImpl || spawn)(claudeBin, args, {
        cwd: o.cwd || path.resolve(__dirname, ".."), env, stdio: ["pipe", "pipe", "pipe"],
      });
    } catch (cause) {
      reject(oneShotError("LLM_ONESHOT_SPAWN_FAILED", "Claude one-shot could not be started", {
        provider: "claude", model, cause,
      }));
      return;
    }
    let outBuf = "", errBuf = "", assistantText = "", resultText = "", isErr = false;
    let settled = false;
    let timer = null;
    let hardKillTimer = null;
    const cleanup = () => {
      if (timer) clearTimeout(timer);
      if (signal) signal.removeEventListener("abort", onAbort);
    };
    const terminate = () => {
      try { proc.kill("SIGTERM"); } catch (_error) {}
      hardKillTimer = setTimeout(() => { try { proc.kill("SIGKILL"); } catch (_error) {} }, 1000);
      if (hardKillTimer.unref) hardKillTimer.unref();
    };
    const fail = (error, terminateChild = false) => {
      if (settled) return;
      settled = true;
      if (terminateChild) terminate();
      cleanup();
      reject(error);
    };
    const succeed = (value) => {
      if (settled) return;
      settled = true;
      cleanup();
      resolve(value);
    };
    const onAbort = () => fail(oneShotError(
      "LLM_ONESHOT_ABORTED",
      "Claude one-shot was aborted",
      { provider: "claude", model },
    ), true);
    timer = setTimeout(() => fail(oneShotError(
      "LLM_ONESHOT_TIMEOUT",
      "Claude one-shot timed out",
      { provider: "claude", model, retryable: true },
    ), true), timeoutMs);
    if (signal) signal.addEventListener("abort", onAbort, { once: true });
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
    proc.on("error", cause => fail(oneShotError("LLM_ONESHOT_SPAWN_FAILED", "Claude one-shot process failed", {
      provider: "claude", model, cause,
    })));
    proc.stdin.on("error", cause => fail(oneShotError("LLM_ONESHOT_INPUT_FAILED", "Claude one-shot input failed", {
      provider: "claude", model, cause,
    }), true));
    proc.on("close", code => {
      if (settled) return;
      if (outBuf.trim()) handle(outBuf);
      const raw = (resultText.trim() || assistantText.trim());
      if (isErr || code !== 0) {
        fail(oneShotError("LLM_ONESHOT_PROCESS_FAILED", `Claude one-shot exited unsuccessfully (${code})`, {
          provider: "claude", model, retryable: true,
        }));
        return;
      }
      if (!raw) {
        fail(oneShotError("LLM_ONESHOT_EMPTY_OUTPUT", "Claude one-shot returned empty output", {
          provider: "claude", model,
        }));
        return;
      }
      succeed(raw);
    });
    try {
      proc.stdin.end(String(prompt || ""));
    } catch (cause) {
      fail(oneShotError("LLM_ONESHOT_INPUT_FAILED", "Claude one-shot input could not be sent", {
        provider: "claude", model, cause,
      }), true);
    }
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
  const timeoutMs = oneShotTimeout(o.timeoutMs == null ? process.env.LLM_ONESHOT_TIMEOUT_MS : o.timeoutMs);
  const signal = o.signal;
  const cwd = o.cwd || process.env.CODEX_ONESHOT_CWD || "/tmp";
  const t0 = Date.now();
  const args = [
    "exec",
    "--json",
    "--skip-git-repo-check",
    "--sandbox", "read-only",
    "--ephemeral",
    "--ignore-user-config",
    "-C", cwd,
    "-m", model,
    ...(o.reasoningEffort ? ["-c", `model_reasoning_effort=${o.reasoningEffort}`] : []),
    "-",
  ];
  return new Promise((resolve, reject) => {
    if (signal && signal.aborted) {
      reject(oneShotError("LLM_ONESHOT_ABORTED", "Codex one-shot was aborted before start", { provider: "codex", model }));
      return;
    }
    const env = { ...process.env, NO_COLOR: "1" };
    Object.keys(env).forEach(k => { if (k.startsWith("CLAUDE")) delete env[k]; });
    let proc;
    try {
      proc = (o.spawnImpl || spawn)(codexBin, args, { cwd, env, stdio: ["pipe", "pipe", "pipe"] });
    } catch (cause) {
      reject(oneShotError("LLM_ONESHOT_SPAWN_FAILED", "Codex one-shot could not be started", {
        provider: "codex", model, cause,
      }));
      return;
    }
    let stdout = "", stderr = "";
    let settled = false;
    let timer = null;
    let hardKillTimer = null;
    const cleanup = () => {
      if (timer) clearTimeout(timer);
      if (signal) signal.removeEventListener("abort", onAbort);
    };
    const terminate = () => {
      try { proc.kill("SIGTERM"); } catch (_error) {}
      hardKillTimer = setTimeout(() => { try { proc.kill("SIGKILL"); } catch (_error) {} }, 1000);
      if (hardKillTimer.unref) hardKillTimer.unref();
    };
    const fail = (error, terminateChild = false) => {
      if (settled) return;
      settled = true;
      if (terminateChild) terminate();
      cleanup();
      reject(error);
    };
    const succeed = (value) => {
      if (settled) return;
      settled = true;
      cleanup();
      resolve(value);
    };
    const onAbort = () => fail(oneShotError(
      "LLM_ONESHOT_ABORTED",
      "Codex one-shot was aborted",
      { provider: "codex", model },
    ), true);
    timer = setTimeout(() => fail(oneShotError(
      "LLM_ONESHOT_TIMEOUT",
      "Codex one-shot timed out",
      { provider: "codex", model, retryable: true },
    ), true), timeoutMs);
    if (signal) signal.addEventListener("abort", onAbort, { once: true });
    proc.stdout.on("data", c => { stdout += c.toString(); });
    proc.stderr.on("data", c => { stderr += c.toString(); });
    proc.on("error", cause => fail(oneShotError("LLM_ONESHOT_SPAWN_FAILED", "Codex one-shot process failed", {
      provider: "codex", model, cause,
    })));
    proc.stdin.on("error", cause => fail(oneShotError("LLM_ONESHOT_INPUT_FAILED", "Codex one-shot input failed", {
      provider: "codex", model, cause,
    }), true));
    proc.on("close", code => {
      if (settled) return;
      const trace = parseCodexJsonl(stdout);
      try { telemetry.record({ component: o.telemetryComponent || "oneshot", model, reasoning: o.reasoningEffort || null, durationMs: Date.now() - t0, usage: telemetry.normUsage(trace.usage) }); } catch (_e) {}
      const raw = String(trace.last_agent_text || "").trim();
      if (code !== 0) {
        fail(oneShotError("LLM_ONESHOT_PROCESS_FAILED", `Codex one-shot exited unsuccessfully (${code})`, {
          provider: "codex", model, retryable: true,
        }));
        return;
      }
      if (!raw) {
        fail(oneShotError("LLM_ONESHOT_EMPTY_OUTPUT", "Codex one-shot returned empty output", {
          provider: "codex", model,
        }));
        return;
      }
      succeed(raw);
    });
    try {
      proc.stdin.end(String(prompt || ""));
    } catch (cause) {
      fail(oneShotError("LLM_ONESHOT_INPUT_FAILED", "Codex one-shot input could not be sent", {
        provider: "codex", model, cause,
      }), true);
    }
  });
}

function oneshotText(prompt, opts) {
  const o = opts || {};
  if (providerFromOpts(o) === "codex") return oneshotTextCodex(prompt, o);
  return oneshotTextClaude(prompt, o);
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

module.exports = {
  LlmOneShotError,
  oneshotText,
  oneshotJSON,
  extractJSON,
  normalizeProvider,
  providerFromOpts,
  parseCodexJsonl,
  codexModel,
};

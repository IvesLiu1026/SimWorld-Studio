"use strict";
// Lightweight one-shot LLM calls for retrieval/routing helpers.
// Default stays Claude for normal Studio use. Non-production retrieval experiments may
// force Codex/GPT, but production and summarizer calls reject that unisolated runner.
const { spawn } = require("child_process");
const telemetry = require("./telemetry");
const {
  assertSafeToolFreeClaudeArgv,
  buildClaudeToolFreeSafetyArgs,
  buildMinimalToolFreeEnv,
} = require("./builder-process-policy");
const { sandboxedSpawn: defaultSandboxedSpawn } = require("./agent-sandbox");
const NL = String.fromCharCode(10);

const DEFAULT_ONESHOT_MAX_BUDGET_USD = 0.05;
const MAX_ONESHOT_MAX_BUDGET_USD = 1;
const DEFAULT_ONESHOT_MAX_INPUT_TOKENS = 50_000;
const DEFAULT_ONESHOT_MAX_OUTPUT_TOKENS = 2_048;
const MAX_ONESHOT_STDOUT_BYTES = 2 * 1024 * 1024;
const MAX_ONESHOT_STDERR_BYTES = 64 * 1024;

class LlmOneShotError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = "LlmOneShotError";
    this.code = code;
    this.retryable = Boolean(details.retryable);
    this.provider = details.provider || null;
    this.model = details.model || null;
    this.providerAttempted = details.providerAttempted === true;
    this.accountingKnown = details.accountingKnown === true;
    if (details.cause) this.cause = details.cause;
  }
}

function oneShotError(code, message, details) {
  return new LlmOneShotError(code, message, details);
}

function streamChunkBytes(chunk) {
  return Buffer.isBuffer(chunk) ? chunk.length : Buffer.byteLength(String(chunk));
}

function oneShotTimeout(value) {
  const timeoutMs = Number(value == null || value === "" ? 120000 : value);
  if (!Number.isFinite(timeoutMs) || timeoutMs < 1 || timeoutMs > 1800000) {
    throw oneShotError("LLM_ONESHOT_CONFIG_INVALID", "One-shot timeout must be between 1 and 1800000 milliseconds");
  }
  return Math.floor(timeoutMs);
}

function oneShotBudget(value) {
  const amount = Number(value == null || value === "" ? DEFAULT_ONESHOT_MAX_BUDGET_USD : value);
  if (!Number.isFinite(amount) || amount < 0.01 || amount > MAX_ONESHOT_MAX_BUDGET_USD) {
    throw oneShotError(
      "LLM_ONESHOT_BUDGET_INVALID",
      `One-shot max budget must be between 0.01 and ${MAX_ONESHOT_MAX_BUDGET_USD.toFixed(2)} USD`,
    );
  }
  // This value becomes a hard CLI cap. Floor rather than round so a value such
  // as 0.050051 can never authorize more than the operator configured.
  return Math.floor((amount + Number.EPSILON) * 10_000) / 10_000;
}

function oneShotTokenLimit(value, { field, defaultValue, maximum }) {
  const amount = Number(value == null || value === "" ? defaultValue : value);
  if (!Number.isSafeInteger(amount) || amount < 1 || amount > maximum) {
    throw oneShotError("LLM_ONESHOT_USAGE_LIMIT_INVALID", `${field} is outside the allowed range`);
  }
  return amount;
}

function usageInteger(usage, field) {
  const value = usage && usage[field];
  if (!Number.isSafeInteger(value) || value < 0) {
    throw oneShotError("LLM_ONESHOT_USAGE_INVALID", `One-shot usage.${field} is missing or invalid`);
  }
  return value;
}

function normalizeClaudeUsageCost({ usage, costUsd }) {
  if (!usage || typeof usage !== "object" || Array.isArray(usage)) {
    throw oneShotError("LLM_ONESHOT_USAGE_INVALID", "Claude one-shot returned no usage metadata");
  }
  const inputTokens = usageInteger(usage, "input_tokens");
  const outputTokens = usageInteger(usage, "output_tokens");
  let totalInputTokens = inputTokens;
  const normalizedUsage = {
    input_tokens: inputTokens,
    output_tokens: outputTokens,
  };
  for (const field of ["cache_creation_input_tokens", "cache_read_input_tokens"]) {
    if (usage[field] !== undefined) {
      const value = usageInteger(usage, field);
      totalInputTokens += value;
      normalizedUsage[field] = value;
    }
  }
  const cost = costUsd === null || costUsd === undefined || costUsd === "" ? NaN : Number(costUsd);
  if (!Number.isFinite(cost) || cost < 0) {
    throw oneShotError("LLM_ONESHOT_COST_INVALID", "Claude one-shot returned no valid cost metadata");
  }
  return Object.freeze({
    usage: Object.freeze(normalizedUsage),
    costUsd: Math.round(cost * 1_000_000) / 1_000_000,
    totalInputTokens,
    outputTokens,
  });
}

function assertClaudeUsageCostLimits(accounting, limits) {
  if (!Number.isSafeInteger(accounting.totalInputTokens)
      || accounting.totalInputTokens > limits.maxInputTokens
      || accounting.outputTokens > limits.maxOutputTokens) {
    throw oneShotError(
      "LLM_ONESHOT_USAGE_LIMIT_EXCEEDED",
      "Claude one-shot token usage exceeded its independent limit",
    );
  }
  const cost = accounting.costUsd;
  if (cost > limits.maxBudgetUsd) {
    throw oneShotError(
      "LLM_ONESHOT_COST_LIMIT_EXCEEDED",
      "Claude one-shot cost exceeded its independent budget",
    );
  }
  return Object.freeze({
    usage: accounting.usage,
    costUsd: accounting.costUsd,
  });
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
  const startedAt = Date.now();
  const baseEnv = o.env && typeof o.env === "object" ? o.env : process.env;
  const claudeBin = String(o.claudeBin || baseEnv.CLAUDE_BIN || "claude");
  const model = o.model == null || o.model === "" ? null : String(o.model);
  const timeoutMs = oneShotTimeout(o.timeoutMs == null ? baseEnv.LLM_ONESHOT_TIMEOUT_MS : o.timeoutMs);
  const maxBudgetUsd = oneShotBudget(
    o.maxBudgetUsd == null ? baseEnv.LLM_ONESHOT_MAX_BUDGET_USD : o.maxBudgetUsd,
  );
  const maxInputTokens = oneShotTokenLimit(
    o.maxInputTokens == null ? baseEnv.LLM_ONESHOT_MAX_INPUT_TOKENS : o.maxInputTokens,
    { field: "One-shot max input tokens", defaultValue: DEFAULT_ONESHOT_MAX_INPUT_TOKENS, maximum: 1_000_000 },
  );
  const maxOutputTokens = oneShotTokenLimit(
    o.maxOutputTokens == null ? baseEnv.LLM_ONESHOT_MAX_OUTPUT_TOKENS : o.maxOutputTokens,
    { field: "One-shot max output tokens", defaultValue: DEFAULT_ONESHOT_MAX_OUTPUT_TOKENS, maximum: 100_000 },
  );
  const signal = o.signal;
  if (o.onAccounting != null && typeof o.onAccounting !== "function") {
    throw oneShotError(
      "LLM_ONESHOT_CONFIG_INVALID",
      "One-shot accounting callback must be a function",
      { provider: "claude", model },
    );
  }
  const args = [
    "-p",
    "--input-format", "text",
    "--output-format", "stream-json",
    "--include-partial-messages",
    "--verbose",
    ...buildClaudeToolFreeSafetyArgs(),
  ];
  if (model) args.push("--model", model);
  // The shared validator intentionally accepts only the exact tool-free policy.
  // Budget is validated above and appended only after that policy is sealed.
  assertSafeToolFreeClaudeArgv(args);
  args.push("--max-budget-usd", String(maxBudgetUsd));
  return new Promise((resolve, reject) => {
    if (signal && signal.aborted) {
      reject(oneShotError("LLM_ONESHOT_ABORTED", "Claude one-shot was aborted before start", { provider: "claude", model }));
      return;
    }
    let env;
    let proc;
    try {
      env = buildMinimalToolFreeEnv(baseEnv, { provider: "claude" });
      const sandbox = (o.sandboxedSpawnImpl || defaultSandboxedSpawn)(claudeBin, args, null, {
        env: baseEnv,
        provider: "claude",
      });
      proc = (o.spawnImpl || spawn)(sandbox.cmd, sandbox.args, {
        cwd: "/", env, stdio: ["pipe", "pipe", "pipe"],
      });
    } catch (cause) {
      reject(oneShotError("LLM_ONESHOT_SPAWN_FAILED", "Claude one-shot could not be started", {
        provider: "claude", model, cause,
      }));
      return;
    }
    let outBuf = "", assistantText = "", resultText = "", isErr = false;
    let stdoutBytes = 0, stderrBytes = 0;
    let resultEvents = 0, resultUsage = null, resultCostUsd = null;
    let accountingKnown = false;
    let accountingCommitAttempted = false;
    let committedAccounting = null;
    let accountingCommitError = null;
    let providerStarted = false;
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
    function commitTerminalAccounting({ strict = false } = {}) {
      if (accountingCommitAttempted) {
        if (strict && accountingCommitError) throw accountingCommitError;
        return committedAccounting;
      }
      if (resultEvents !== 1) return null;
      accountingCommitAttempted = true;
      try {
        const accounting = normalizeClaudeUsageCost({ usage: resultUsage, costUsd: resultCostUsd });
        if (o.onAccounting) {
          o.onAccounting(Object.freeze({
            provider: "claude",
            model,
            usage: accounting.usage,
            costUsd: accounting.costUsd,
            maxBudgetUsd,
          }));
        }
        committedAccounting = accounting;
        accountingKnown = true;
        return accounting;
      } catch (cause) {
        accountingCommitError = cause instanceof LlmOneShotError
          ? cause
          : oneShotError(
            "LLM_ONESHOT_ACCOUNTING_FAILED",
            "Claude one-shot accounting could not be committed",
            { provider: "claude", model, cause },
          );
        if (strict) throw accountingCommitError;
        return null;
      }
    }
    function flushBufferedRecord() {
      const buffered = outBuf.trim();
      if (!buffered) return;
      outBuf = "";
      handle(buffered);
    }
    const fail = (error, terminateChild = false) => {
      if (settled) return;
      // A complete terminal result may arrive just before cancellation,
      // timeout, or another process failure. The CLI also permits its final
      // JSON record to omit a trailing newline, so parse the buffered tail
      // before committing verified accounting exactly once.
      flushBufferedRecord();
      commitTerminalAccounting();
      settled = true;
      if (terminateChild) terminate();
      cleanup();
      if (error instanceof LlmOneShotError) {
        error.providerAttempted = providerStarted || accountingKnown || resultEvents > 0 || stdoutBytes > 0;
        error.accountingKnown = accountingKnown;
      }
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
    proc.once("spawn", () => { providerStarted = true; });
    function handle(line) {
      let e; try { e = JSON.parse(String(line || "").trim()); } catch { return; }
      if (!e) return;
      if (e.type === "assistant") {
        const blocks = e.message && Array.isArray(e.message.content) ? e.message.content : [];
        for (const b of blocks) if (b.type === "text" && b.text) assistantText += String(b.text);
      } else if (e.type === "result") {
        resultEvents += 1;
        isErr = Boolean(e.is_error || e.subtype === "error_during_turn");
        if (typeof e.result === "string") resultText += e.result;
        resultUsage = e.usage;
        resultCostUsd = e.total_cost_usd !== undefined ? e.total_cost_usd
          : (e.totalCostUsd !== undefined ? e.totalCostUsd : e.cost_usd);
      }
    }
    proc.stdout.on("data", c => {
      if (settled) return;
      stdoutBytes += streamChunkBytes(c);
      if (stdoutBytes > MAX_ONESHOT_STDOUT_BYTES) {
        fail(oneShotError(
          "LLM_ONESHOT_OUTPUT_LIMIT_EXCEEDED",
          "Claude one-shot output exceeded its hard byte limit",
          { provider: "claude", model },
        ), true);
        return;
      }
      outBuf += c.toString();
      const ls = outBuf.split(NL);
      outBuf = ls.pop() || "";
      for (const l of ls) if (l.trim()) handle(l);
    });
    proc.stderr.on("data", c => {
      if (settled) return;
      stderrBytes += streamChunkBytes(c);
      if (stderrBytes > MAX_ONESHOT_STDERR_BYTES) {
        fail(oneShotError(
          "LLM_ONESHOT_DIAGNOSTICS_LIMIT_EXCEEDED",
          "Claude one-shot diagnostics exceeded their hard byte limit",
          { provider: "claude", model },
        ), true);
        return;
      }
    });
    proc.on("error", cause => fail(oneShotError("LLM_ONESHOT_SPAWN_FAILED", "Claude one-shot process failed", {
      provider: "claude", model, cause,
    })));
    proc.stdin.on("error", cause => fail(oneShotError(
      providerStarted ? "LLM_ONESHOT_INPUT_FAILED" : "LLM_ONESHOT_SPAWN_FAILED",
      providerStarted ? "Claude one-shot input failed" : "Claude one-shot could not be started",
      { provider: "claude", model, cause },
    ), true));
    proc.on("close", code => {
      if (settled) return;
      flushBufferedRecord();
      const raw = (resultText.trim() || assistantText.trim());
      if (resultEvents !== 1) {
        fail(oneShotError(
          "LLM_ONESHOT_PROTOCOL_ERROR",
          "Claude one-shot must return exactly one terminal result event",
          { provider: "claude", model },
        ));
        return;
      }
      let accounting;
      try {
        accounting = commitTerminalAccounting({ strict: true });
      } catch (error) {
        fail(error);
        return;
      }
      try {
        assertClaudeUsageCostLimits(accounting, { maxBudgetUsd, maxInputTokens, maxOutputTokens });
      } catch (error) {
        fail(error);
        return;
      }
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
      try {
        telemetry.record({
          component: o.telemetryComponent || "oneshot",
          model,
          durationMs: Math.max(0, Date.now() - startedAt),
          usage: telemetry.normUsage(accounting.usage),
          costUsd: accounting.costUsd,
        });
      } catch (_error) {}
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
    let stdout = "";
    let stdoutBytes = 0, stderrBytes = 0;
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
    proc.stdout.on("data", c => {
      if (settled) return;
      stdoutBytes += streamChunkBytes(c);
      if (stdoutBytes > MAX_ONESHOT_STDOUT_BYTES) {
        fail(oneShotError(
          "LLM_ONESHOT_OUTPUT_LIMIT_EXCEEDED",
          "Codex one-shot output exceeded its hard byte limit",
          { provider: "codex", model },
        ), true);
        return;
      }
      stdout += c.toString();
    });
    proc.stderr.on("data", c => {
      if (settled) return;
      stderrBytes += streamChunkBytes(c);
      if (stderrBytes > MAX_ONESHOT_STDERR_BYTES) {
        fail(oneShotError(
          "LLM_ONESHOT_DIAGNOSTICS_LIMIT_EXCEEDED",
          "Codex one-shot diagnostics exceeded their hard byte limit",
          { provider: "codex", model },
        ), true);
        return;
      }
    });
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
  if (providerFromOpts(o) === "codex") {
    const runtimeEnv = o.env && typeof o.env === "object" ? o.env : process.env;
    const component = String(o.telemetryComponent || "").trim().toLowerCase();
    const isProduction = [process.env.NODE_ENV, runtimeEnv.NODE_ENV]
      .some(value => String(value || "").trim().toLowerCase() === "production");
    if (isProduction || component === "summarizer") {
      return Promise.reject(oneShotError(
        "LLM_ONESHOT_PROVIDER_DISABLED",
        "Codex one-shots are disabled for production and summarizer workloads until they have an isolated, budgeted runner",
        { provider: "codex", model: codexModel(o.model) },
      ));
    }
    return oneshotTextCodex(prompt, o);
  }
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
  DEFAULT_ONESHOT_MAX_BUDGET_USD,
  LlmOneShotError,
  MAX_ONESHOT_MAX_BUDGET_USD,
  oneshotText,
  oneshotJSON,
  extractJSON,
  normalizeProvider,
  providerFromOpts,
  parseCodexJsonl,
  codexModel,
  resolveOneShotBudget: oneShotBudget,
};

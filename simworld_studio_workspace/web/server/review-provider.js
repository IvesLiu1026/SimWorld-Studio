"use strict";

// Tool-free, strict-schema provider adapter for scene review. The adapter keeps
// scene evidence on stdin (never argv), strips unrelated service credentials
// from the child environment, and rejects providers that cannot disable their
// host tools.
const { spawn: defaultSpawn } = require("child_process");
const os = require("os");

const REVIEW_STATUSES = Object.freeze(["PASS", "NEEDS_IMPROVEMENT", "FAIL"]);
const REVIEW_VERDICT_SCHEMA = Object.freeze({
  type: "object",
  additionalProperties: false,
  properties: {
    status: { type: "string", enum: REVIEW_STATUSES },
    issues: {
      type: "array",
      maxItems: 50,
      items: { type: "string", minLength: 1, maxLength: 1000 },
    },
    suggestions: {
      type: "array",
      maxItems: 50,
      items: { type: "string", minLength: 1, maxLength: 1000 },
    },
    raw_notes: { type: "string", maxLength: 8000 },
  },
  required: ["status", "issues", "suggestions", "raw_notes"],
});

const CLAUDE_MODEL_PATTERNS = Object.freeze([
  /^claude-(?:opus|sonnet|haiku)-[a-z0-9]+(?:-[a-z0-9]+)*$/,
  /^claude-[0-9]+(?:-[0-9]+)*-(?:opus|sonnet|haiku)(?:-[a-z0-9]+)*$/,
]);
const SAFE_ENV_KEYS = Object.freeze([
  "HOME", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR",
  "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
  "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
  "CLAUDE_CONFIG_DIR", "ANTHROPIC_BASE_URL",
  "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
  "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
]);
const MAX_STDOUT_BYTES = 2 * 1024 * 1024;
const MAX_STDERR_BYTES = 64 * 1024;
const MAX_CLI_IDENTITY_BYTES = 16 * 1024;
const DEFAULT_CLI_IDENTITY_TIMEOUT_MS = 5_000;
const MAX_CLI_IDENTITY_TIMEOUT_MS = 30_000;
const CLAUDE_CLI_VERSION = /^([0-9]+(?:\.[0-9]+){2}(?:[-+][A-Za-z0-9.-]+)?) \(Claude Code\)$/;

class ReviewProviderError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = "ReviewProviderError";
    this.code = code;
    this.retryable = Boolean(details.retryable);
    this.provider = details.provider || null;
    this.model = details.model || null;
    if (details.cause) this.cause = details.cause;
  }

  toJSON() {
    return {
      name: this.name,
      code: this.code,
      message: this.message,
      retryable: this.retryable,
      provider: this.provider,
      model: this.model,
    };
  }
}

function reviewError(code, message, details) {
  return new ReviewProviderError(code, message, details);
}

function normalizeReviewProvider(value) {
  const raw = String(value || "").trim().toLowerCase();
  if (raw === "claude" || raw === "anthropic") return "claude";
  if (["codex", "openai", "gpt", "gpt-5.5", "gpt5.5"].includes(raw)) return "codex";
  throw reviewError("REVIEW_PROVIDER_INVALID", `Unsupported review provider: ${raw || "(empty)"}`);
}

function validateClaudeModel(model) {
  const value = String(model || "").trim().toLowerCase();
  if (!value || !CLAUDE_MODEL_PATTERNS.some((pattern) => pattern.test(value))) {
    throw reviewError(
      "REVIEW_MODEL_INVALID",
      "Review model must be an explicit, versioned Claude model id",
      { provider: "claude", model: value || null },
    );
  }
  return value;
}

function validateMaxBudgetUsd(value) {
  const amount = Number(value == null || value === "" ? 0.5 : value);
  if (!Number.isFinite(amount) || amount < 0.01 || amount > 10) {
    throw reviewError(
      "REVIEW_BUDGET_INVALID",
      "Review maxBudgetUsd must be between 0.01 and 10.00",
      { provider: "claude" },
    );
  }
  return Math.round(amount * 10000) / 10000;
}

function resolveReviewConfig(options = {}, env = process.env) {
  // Review selection is intentionally independent from the builder's generic
  // LLM_PROVIDER. A Codex builder must not silently turn the critic into a
  // host-capable Codex CLI review process.
  const production = String(env.NODE_ENV || "").trim().toLowerCase() === "production";
  let providerValue = options.provider || options.runner || env.CRITIC_PROVIDER || "claude";
  let modelValue = options.model || env.CRITIC_MODEL || env.CLAUDE_MODEL;
  let budgetValue = options.maxBudgetUsd == null ? env.CRITIC_MAX_BUDGET_USD : options.maxBudgetUsd;
  if (production) {
    if (!env.CRITIC_PROVIDER || !env.CRITIC_MODEL || !env.CRITIC_MAX_BUDGET_USD) {
      throw reviewError(
        "REVIEW_PRODUCTION_PIN_MISSING",
        "Production review requires pinned provider, model, and budget configuration",
      );
    }
    const pinnedProvider = normalizeReviewProvider(env.CRITIC_PROVIDER);
    const pinnedModel = validateClaudeModel(env.CRITIC_MODEL);
    const pinnedBudget = validateMaxBudgetUsd(env.CRITIC_MAX_BUDGET_USD);
    if ((options.provider || options.runner)
        && normalizeReviewProvider(options.provider || options.runner) !== pinnedProvider) {
      throw reviewError("REVIEW_PROVIDER_PIN_MISMATCH", "Requested review provider does not match the deployment pin");
    }
    if (options.model && validateClaudeModel(options.model) !== pinnedModel) {
      throw reviewError("REVIEW_MODEL_PIN_MISMATCH", "Requested review model does not match the deployment pin");
    }
    if (options.maxBudgetUsd != null && validateMaxBudgetUsd(options.maxBudgetUsd) > pinnedBudget) {
      throw reviewError("REVIEW_BUDGET_EXCEEDS_DEPLOYMENT_CAP", "Requested review budget exceeds the deployment cap");
    }
    providerValue = pinnedProvider;
    modelValue = pinnedModel;
    budgetValue = options.maxBudgetUsd == null ? pinnedBudget : options.maxBudgetUsd;
  }
  const provider = normalizeReviewProvider(providerValue);
  if (provider === "codex") {
    throw reviewError(
      "REVIEW_PROVIDER_UNSAFE",
      "Codex CLI review is disabled because this runtime cannot disable its host-capable tools",
      { provider },
    );
  }

  return {
    provider,
    model: validateClaudeModel(modelValue),
    maxBudgetUsd: validateMaxBudgetUsd(budgetValue),
  };
}

function validateStringArray(value, field) {
  if (!Array.isArray(value) || value.length > 50) {
    throw reviewError("REVIEW_SCHEMA_INVALID", `${field} must be an array with at most 50 items`);
  }
  return value.map((item) => {
    if (typeof item !== "string") {
      throw reviewError("REVIEW_SCHEMA_INVALID", `${field} must contain only strings`);
    }
    const normalized = item.trim();
    if (!normalized || normalized.length > 1000) {
      throw reviewError("REVIEW_SCHEMA_INVALID", `${field} contains an empty or oversized item`);
    }
    return normalized;
  });
}

function validateReviewVerdict(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw reviewError("REVIEW_SCHEMA_INVALID", "Review verdict must be an object");
  }
  const allowed = new Set(["status", "issues", "suggestions", "raw_notes"]);
  const extra = Object.keys(value).filter((key) => !allowed.has(key));
  if (extra.length) {
    throw reviewError("REVIEW_SCHEMA_INVALID", `Review verdict contains unknown fields: ${extra.join(", ")}`);
  }
  if (!REVIEW_STATUSES.includes(value.status)) {
    throw reviewError("REVIEW_SCHEMA_INVALID", "Review verdict status is invalid");
  }
  if (typeof value.raw_notes !== "string" || value.raw_notes.length > 8000) {
    throw reviewError("REVIEW_SCHEMA_INVALID", "raw_notes must be a string of at most 8000 characters");
  }
  return {
    status: value.status,
    issues: validateStringArray(value.issues, "issues"),
    suggestions: validateStringArray(value.suggestions, "suggestions"),
    raw_notes: value.raw_notes,
  };
}

function buildSafeProviderEnv(source = process.env) {
  const env = {};
  for (const key of SAFE_ENV_KEYS) {
    if (source[key] != null && source[key] !== "") env[key] = String(source[key]);
  }
  env.NO_COLOR = "1";
  env.CLAUDE_CODE_SAFE_MODE = "1";
  return env;
}

function parseClaudeCliIdentity(stdout) {
  const lines = String(stdout || "").trim().split(/\r?\n/).filter(Boolean);
  const match = lines.length === 1 ? lines[0].match(CLAUDE_CLI_VERSION) : null;
  if (!match) {
    throw reviewError(
      "REVIEW_CLI_IDENTITY_INVALID",
      "Claude CLI returned an invalid version identity",
      { provider: "claude" },
    );
  }
  return Object.freeze({ name: "claude-code", version: match[1] });
}

function inspectClaudeCliIdentity({
  spawnImpl = defaultSpawn,
  claudeBin = "claude",
  env = process.env,
  timeoutMs = DEFAULT_CLI_IDENTITY_TIMEOUT_MS,
  signal,
} = {}) {
  const deadlineMs = Number(timeoutMs);
  if (!Number.isFinite(deadlineMs) || deadlineMs < 1 || deadlineMs > MAX_CLI_IDENTITY_TIMEOUT_MS) {
    return Promise.reject(reviewError(
      "REVIEW_CLI_INSPECTION_CONFIG_INVALID",
      "Claude CLI identity timeout is invalid",
      { provider: "claude" },
    ));
  }
  if (signal && signal.aborted) {
    return Promise.reject(reviewError(
      "REVIEW_ABORTED",
      "Review was aborted before Claude CLI identity inspection",
      { provider: "claude" },
    ));
  }

  return new Promise((resolve, reject) => {
    let child;
    let stdout = "";
    let diagnosticsBytes = 0;
    let settled = false;
    let hardKillTimer = null;
    const removeAbortListener = () => {
      if (signal) signal.removeEventListener("abort", onAbort);
    };
    const settle = (fn, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeoutTimer);
      removeAbortListener();
      fn(value);
    };
    const terminate = () => {
      try { child && child.kill("SIGTERM"); } catch (_error) {}
      hardKillTimer = setTimeout(() => {
        try { child && child.kill("SIGKILL"); } catch (_error) {}
      }, 1_000);
      if (hardKillTimer.unref) hardKillTimer.unref();
    };
    const fail = (error, terminateChild = false) => {
      if (settled) return;
      if (terminateChild) terminate();
      settle(reject, error);
    };
    const onAbort = () => fail(reviewError(
      "REVIEW_ABORTED",
      "Review was aborted during Claude CLI identity inspection",
      { provider: "claude" },
    ), true);
    const timeoutTimer = setTimeout(() => fail(reviewError(
      "REVIEW_CLI_INSPECTION_TIMEOUT",
      "Claude CLI identity inspection timed out",
      { provider: "claude", retryable: true },
    ), true), Math.floor(deadlineMs));
    if (timeoutTimer.unref) timeoutTimer.unref();

    if (signal) signal.addEventListener("abort", onAbort, { once: true });
    try {
      child = spawnImpl(claudeBin, ["--version"], {
        cwd: os.tmpdir(),
        env: buildSafeProviderEnv(env),
        stdio: ["ignore", "pipe", "pipe"],
      });
    } catch (cause) {
      fail(reviewError(
        "REVIEW_CLI_INSPECTION_FAILED",
        "Claude CLI identity inspection could not be started",
        { provider: "claude", cause },
      ));
      return;
    }

    child.stdout.on("data", (chunk) => {
      if (settled) return;
      stdout += chunk.toString();
      if (Buffer.byteLength(stdout) > MAX_CLI_IDENTITY_BYTES) {
        fail(reviewError(
          "REVIEW_CLI_IDENTITY_INVALID",
          "Claude CLI version output exceeded its size limit",
          { provider: "claude" },
        ), true);
      }
    });
    child.stderr.on("data", (chunk) => {
      if (settled) return;
      diagnosticsBytes += chunk.length;
      if (diagnosticsBytes > MAX_CLI_IDENTITY_BYTES) {
        fail(reviewError(
          "REVIEW_CLI_IDENTITY_INVALID",
          "Claude CLI diagnostics exceeded their size limit",
          { provider: "claude" },
        ), true);
      }
    });
    child.on("error", (cause) => fail(reviewError(
      "REVIEW_CLI_INSPECTION_FAILED",
      "Claude CLI identity process failed to start",
      { provider: "claude", cause },
    )));
    child.on("close", (code, childSignal) => {
      if (settled) return;
      if (code !== 0) {
        fail(reviewError(
          "REVIEW_CLI_INSPECTION_FAILED",
          `Claude CLI identity process exited unsuccessfully (${code == null ? childSignal || "unknown" : code})`,
          { provider: "claude", retryable: true },
        ));
        return;
      }
      try {
        settle(resolve, parseClaudeCliIdentity(stdout));
      } catch (error) {
        settle(reject, error);
      }
    });
  });
}

function parseExactJson(value) {
  if (value && typeof value === "object") return value;
  const text = String(value || "").trim();
  if (!text) throw reviewError("REVIEW_PROTOCOL_ERROR", "Review provider returned no structured output");
  try {
    return JSON.parse(text);
  } catch (_error) {
    throw reviewError("REVIEW_PROTOCOL_ERROR", "Review provider output was not exact JSON");
  }
}

function parseClaudeReviewOutput(stdout) {
  const assistantParts = [];
  let resultEvent = null;
  for (const line of String(stdout || "").split(/\r?\n/)) {
    if (!line.trim()) continue;
    let event;
    try {
      event = JSON.parse(line);
    } catch (_error) {
      throw reviewError("REVIEW_PROTOCOL_ERROR", "Review provider emitted a non-JSON protocol line");
    }
    if (event.type === "assistant") {
      const blocks = event.message && Array.isArray(event.message.content) ? event.message.content : [];
      for (const block of blocks) {
        if (block && block.type === "text" && typeof block.text === "string") assistantParts.push(block.text);
      }
    } else if (event.type === "result") {
      if (resultEvent) throw reviewError("REVIEW_PROTOCOL_ERROR", "Review provider emitted multiple result events");
      resultEvent = event;
    }
  }
  if (!resultEvent) throw reviewError("REVIEW_PROTOCOL_ERROR", "Review provider emitted no result event");
  if (resultEvent.is_error || resultEvent.subtype === "error_during_turn") {
    throw reviewError("REVIEW_PROCESS_FAILED", "Review provider reported an error", { retryable: true });
  }
  const structured = resultEvent.structured_output != null
    ? resultEvent.structured_output
    : (resultEvent.structuredOutput != null ? resultEvent.structuredOutput : null);
  const rawValue = structured != null ? structured : (resultEvent.result || assistantParts.join(""));
  const rawCost = resultEvent.total_cost_usd !== undefined
    ? resultEvent.total_cost_usd
    : (resultEvent.totalCostUsd !== undefined ? resultEvent.totalCostUsd : resultEvent.cost_usd);
  const costUsd = rawCost === undefined || rawCost === null || rawCost === "" ? null : Number(rawCost);
  if (costUsd !== null && (!Number.isFinite(costUsd) || costUsd < 0 || costUsd > 100)) {
    throw reviewError("REVIEW_PROTOCOL_ERROR", "Review provider returned an invalid cost value");
  }
  return {
    verdict: validateReviewVerdict(parseExactJson(rawValue)),
    usage: resultEvent.usage && typeof resultEvent.usage === "object" ? resultEvent.usage : null,
    costUsd: costUsd === null ? null : Math.round(costUsd * 1e6) / 1e6,
  };
}

function normalizeTimeout(value) {
  const timeoutMs = Number(value == null ? 120000 : value);
  if (!Number.isFinite(timeoutMs) || timeoutMs < 1 || timeoutMs > 300000) {
    throw reviewError("REVIEW_CONFIG_INVALID", "Review timeout must be between 1 and 300000 milliseconds");
  }
  return Math.floor(timeoutMs);
}

function normalizeImages(images) {
  if (!Array.isArray(images) || images.length < 1 || images.length > 8) {
    throw reviewError("REVIEW_EVIDENCE_INVALID", "Review requires between one and eight images");
  }
  return images.map((image) => {
    const mediaType = String(image && image.mediaType || "").trim().toLowerCase();
    if (mediaType !== "image/png" && mediaType !== "image/jpeg") {
      throw reviewError("REVIEW_EVIDENCE_INVALID", "Review image media type must be image/png or image/jpeg");
    }
    const data = Buffer.isBuffer(image && image.data)
      ? image.data.toString("base64")
      : String(image && image.data || "").trim();
    if (!data || !/^[A-Za-z0-9+/]+={0,2}$/.test(data)) {
      throw reviewError("REVIEW_EVIDENCE_INVALID", "Review image data must be base64 encoded");
    }
    return { mediaType, data };
  });
}

function normalizeEvidenceIds(value) {
  if (value == null) return [];
  if (!Array.isArray(value) || value.length > 20) {
    throw reviewError("REVIEW_EVIDENCE_INVALID", "evidenceIds must be an array with at most 20 entries");
  }
  return value.map((item) => {
    const id = String(item || "").trim();
    if (!id || id.length > 512) throw reviewError("REVIEW_EVIDENCE_INVALID", "evidenceIds contains an invalid id");
    return id;
  });
}

function createReviewProvider(runtime = {}) {
  const spawnImpl = runtime.spawnImpl || defaultSpawn;
  const envSource = runtime.env || process.env;
  const now = runtime.now || Date.now;

  function inspectRuntimeIdentity(options = {}) {
    return inspectClaudeCliIdentity({
      spawnImpl,
      claudeBin: runtime.claudeBin || envSource.CLAUDE_BIN || "claude",
      env: envSource,
      timeoutMs: options.timeoutMs,
      signal: options.signal,
    });
  }

  async function review(options = {}) {
    const config = resolveReviewConfig(options, envSource);
    const timeoutMs = normalizeTimeout(options.timeoutMs);
    const signal = options.signal;
    if (signal && signal.aborted) {
      throw reviewError("REVIEW_ABORTED", "Review was aborted before provider start", {
        provider: config.provider, model: config.model,
      });
    }
    const prompt = String(options.prompt || "").trim();
    const systemPrompt = String(options.systemPrompt || "").trim();
    if (!prompt || !systemPrompt) {
      throw reviewError("REVIEW_CONFIG_INVALID", "Review prompt and system prompt are required", config);
    }
    const images = normalizeImages(options.images);
    const evidenceIds = normalizeEvidenceIds(options.evidenceIds);
    const content = images.map((image) => ({
      type: "image",
      source: { type: "base64", media_type: image.mediaType, data: image.data },
    }));
    content.push({ type: "text", text: prompt });
    const payload = JSON.stringify({ type: "user", message: { role: "user", content } }) + "\n";
    const args = [
      "--print",
      "--input-format", "stream-json",
      "--output-format", "stream-json",
      "--verbose",
      "--safe-mode",
      "--disable-slash-commands",
      "--tools", "",
      "--permission-mode", "dontAsk",
      "--strict-mcp-config",
      "--mcp-config", "{}",
      "--no-session-persistence",
      "--json-schema", JSON.stringify(REVIEW_VERDICT_SCHEMA),
      "--system-prompt", systemPrompt,
      "--model", config.model,
      "--max-budget-usd", String(config.maxBudgetUsd),
    ];
    const startedAt = now();

    return new Promise((resolve, reject) => {
      let child;
      try {
        child = spawnImpl(runtime.claudeBin || envSource.CLAUDE_BIN || "claude", args, {
          cwd: os.tmpdir(),
          env: buildSafeProviderEnv(envSource),
          stdio: ["pipe", "pipe", "pipe"],
        });
      } catch (cause) {
        reject(reviewError("REVIEW_SPAWN_FAILED", "Review provider could not be started", { ...config, cause }));
        return;
      }

      let settled = false;
      let stdout = "";
      let stderrBytes = 0;
      let hardKillTimer = null;
      const removeAbortListener = () => {
        if (signal) signal.removeEventListener("abort", onAbort);
      };
      const settle = (fn, value) => {
        if (settled) return;
        settled = true;
        clearTimeout(timeoutTimer);
        removeAbortListener();
        fn(value);
      };
      const terminate = () => {
        try { child.kill("SIGTERM"); } catch (_error) {}
        hardKillTimer = setTimeout(() => {
          try { child.kill("SIGKILL"); } catch (_error) {}
        }, 1000);
        if (hardKillTimer.unref) hardKillTimer.unref();
      };
      const fail = (error, terminateChild = false) => {
        if (settled) return;
        if (terminateChild) terminate();
        settle(reject, error);
      };
      const onAbort = () => fail(reviewError("REVIEW_ABORTED", "Review was aborted", config), true);
      const timeoutTimer = setTimeout(() => {
        fail(reviewError("REVIEW_TIMEOUT", "Review provider timed out", { ...config, retryable: true }), true);
      }, timeoutMs);
      if (signal) signal.addEventListener("abort", onAbort, { once: true });

      child.stdout.on("data", (chunk) => {
        if (settled) return;
        stdout += chunk.toString();
        if (Buffer.byteLength(stdout) > MAX_STDOUT_BYTES) {
          fail(reviewError("REVIEW_PROTOCOL_ERROR", "Review provider output exceeded its size limit", config), true);
        }
      });
      child.stderr.on("data", (chunk) => {
        if (settled) return;
        stderrBytes += chunk.length;
        if (stderrBytes > MAX_STDERR_BYTES) {
          fail(reviewError("REVIEW_PROTOCOL_ERROR", "Review provider diagnostics exceeded their size limit", config), true);
        }
      });
      child.on("error", (cause) => {
        fail(reviewError("REVIEW_SPAWN_FAILED", "Review provider process failed to start", { ...config, cause }));
      });
      child.stdin.on("error", (cause) => {
        fail(reviewError("REVIEW_PROTOCOL_ERROR", "Review provider input stream failed", { ...config, cause }), true);
      });
      child.on("close", (code, childSignal) => {
        if (settled) return;
        if (code !== 0) {
          fail(reviewError(
            "REVIEW_PROCESS_FAILED",
            `Review provider exited unsuccessfully (${code == null ? childSignal || "unknown" : code})`,
            { ...config, retryable: true },
          ));
          return;
        }
        try {
          const parsed = parseClaudeReviewOutput(stdout);
          settle(resolve, {
            status: parsed.verdict.status,
            issues: parsed.verdict.issues,
            suggestions: parsed.verdict.suggestions,
            raw: parsed.verdict.raw_notes,
            provider: config.provider,
            model: config.model,
            max_budget_usd: config.maxBudgetUsd,
            evidence_ids: evidenceIds,
            usage: parsed.usage,
            cost_usd: parsed.costUsd,
            latency_ms: Math.max(0, now() - startedAt),
          });
        } catch (error) {
          const typed = error instanceof ReviewProviderError
            ? error
            : reviewError("REVIEW_PROTOCOL_ERROR", "Review provider output could not be parsed", { ...config, cause: error });
          typed.provider = typed.provider || config.provider;
          typed.model = typed.model || config.model;
          settle(reject, typed);
        }
      });

      try {
        child.stdin.end(payload);
      } catch (cause) {
        fail(reviewError("REVIEW_PROTOCOL_ERROR", "Review provider input could not be sent", { ...config, cause }), true);
      }
    });
  }

  return Object.freeze({ inspectRuntimeIdentity, review });
}

const defaultProvider = createReviewProvider();

module.exports = {
  REVIEW_STATUSES,
  REVIEW_VERDICT_SCHEMA,
  ReviewProviderError,
  normalizeReviewProvider,
  resolveReviewConfig,
  validateMaxBudgetUsd,
  validateReviewVerdict,
  buildSafeProviderEnv,
  inspectClaudeCliIdentity,
  parseClaudeCliIdentity,
  parseClaudeReviewOutput,
  createReviewProvider,
  review: defaultProvider.review,
};

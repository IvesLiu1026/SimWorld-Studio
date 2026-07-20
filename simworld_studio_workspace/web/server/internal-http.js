"use strict";

const http = require("http");
const { resolveReviewConfig } = require("./review-provider");

const MIN_ACCESS_TOKEN_LENGTH = 32;
const DEFAULT_TIMEOUT_MS = 30 * 60 * 1000;
const MAX_SSE_BUFFER_BYTES = 2 * 1024 * 1024;

class InternalHttpError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = "InternalHttpError";
    this.code = code;
    this.statusCode = details.statusCode || null;
    this.retryable = Boolean(details.retryable);
  }
}

function requireStudioAccessToken(env = process.env) {
  const token = String((env && env.STUDIO_ACCESS_TOKEN) || "").trim();
  if (token.length < MIN_ACCESS_TOKEN_LENGTH) {
    throw new InternalHttpError(
      "INTERNAL_AUTH_UNAVAILABLE",
      `STUDIO_ACCESS_TOKEN must be at least ${MIN_ACCESS_TOKEN_LENGTH} characters for internal requests`,
    );
  }
  return token;
}

function throwIfAborted(signal, operation = "Review run") {
  if (signal && signal.aborted) {
    throw new InternalHttpError("INTERNAL_HTTP_ABORTED", `${operation} was aborted`);
  }
}

function optionalString(value, field) {
  if (value == null || value === "") return null;
  if (typeof value !== "string") {
    throw new InternalHttpError("INVALID_REVIEW_CONTRACT", `${field} must be a string`);
  }
  const normalized = value.trim();
  if (!normalized || normalized.length > 200 || /[\x00-\x1f\x7f]/.test(normalized)) {
    throw new InternalHttpError("INVALID_REVIEW_CONTRACT", `${field} is invalid`);
  }
  return normalized;
}

function normalizeReviewProvider(value) {
  const provider = String(value || "").trim().toLowerCase();
  if (["claude", "anthropic"].includes(provider)) return "claude";
  if (["codex", "openai", "gpt", "gpt-5.5", "gpt5.5"].includes(provider)) return "codex";
  return null;
}

function defaultReviewModel(provider, env) {
  return provider === "codex"
    ? String(env.CODEX_MODEL || "gpt-5.5")
    : String(env.CLAUDE_MODEL || "claude-opus-4-8");
}

function assertReviewModelCompatible(provider, model, field) {
  if (!model) return;
  if (provider === "codex" && /^claude(?:-|$)/i.test(model)) {
    throw new InternalHttpError(
      "INVALID_REVIEW_CONTRACT",
      `${field} '${model}' is not compatible with the Codex review provider`,
    );
  }
  if (provider === "claude" && /^(?:gpt(?:-|$)|o\d(?:-|$))/i.test(model)) {
    throw new InternalHttpError(
      "INVALID_REVIEW_CONTRACT",
      `${field} '${model}' is not compatible with the Claude review provider`,
    );
  }
}

/**
 * Resolve the public review request into one unambiguous builder/reviewer contract.
 * `runner` and `model` remain accepted as backwards-compatible aliases, but conflicting
 * canonical and legacy fields fail before any builder/UE mutation starts.
 */
function resolveReviewContract(body = {}, env = process.env) {
  const canonicalAgent = optionalString(body.agent, "agent");
  const legacyRunner = optionalString(body.runner, "runner");
  if (canonicalAgent && legacyRunner && canonicalAgent !== legacyRunner) {
    throw new InternalHttpError(
      "INVALID_REVIEW_CONTRACT",
      "agent and deprecated runner fields must identify the same builder",
    );
  }
  const builderAgent = String(canonicalAgent || legacyRunner || "claude").toLowerCase();
  const supportedAgents = new Set(["claude", "codex", "opencode", "gemini", "cursor", "grok"]);
  if (!supportedAgents.has(builderAgent)) {
    throw new InternalHttpError("INVALID_REVIEW_CONTRACT", `Unsupported builder agent '${builderAgent}'`);
  }

  const canonicalBuilderModel = optionalString(body.builderModel, "builderModel");
  const legacyModel = optionalString(body.model, "model");
  if (canonicalBuilderModel && legacyModel && canonicalBuilderModel !== legacyModel) {
    throw new InternalHttpError(
      "INVALID_REVIEW_CONTRACT",
      "builderModel and model fields must identify the same model",
    );
  }
  const builderModel = canonicalBuilderModel || legacyModel;

  const builderReviewProvider = normalizeReviewProvider(builderAgent);
  const configuredProvider = normalizeReviewProvider(body.criticProvider)
    || normalizeReviewProvider(env.CRITIC_PROVIDER);
  // Review is intentionally independent from the builder backend. The only current adapter that
  // can disable all host tools is Claude; Codex may still be used as the builder.
  const criticProvider = configuredProvider || "claude";
  if (body.criticProvider && !normalizeReviewProvider(body.criticProvider)) {
    throw new InternalHttpError(
      "INVALID_REVIEW_CONTRACT",
      `Unsupported criticProvider '${String(body.criticProvider)}'`,
    );
  }
  if (env.CRITIC_PROVIDER && !normalizeReviewProvider(env.CRITIC_PROVIDER)) {
    throw new InternalHttpError(
      "INVALID_REVIEW_CONTRACT",
      `Unsupported CRITIC_PROVIDER '${String(env.CRITIC_PROVIDER)}'`,
    );
  }
  if (criticProvider === "codex") {
    throw new InternalHttpError(
      "REVIEW_PROVIDER_UNSAFE",
      "Codex critic is disabled because this runtime cannot disable its host-capable tools",
    );
  }
  const requestedCriticModel = optionalString(body.criticModel, "criticModel")
    || optionalString(env.CRITIC_MODEL, "CRITIC_MODEL")
    || (criticProvider === builderReviewProvider ? builderModel : null)
    || defaultReviewModel(criticProvider, env);
  assertReviewModelCompatible(criticProvider, requestedCriticModel, "criticModel");
  let criticModel;
  try {
    criticModel = resolveReviewConfig(
      { provider: criticProvider, model: requestedCriticModel },
      env,
    ).model;
  } catch (error) {
    throw new InternalHttpError(
      error && error.code || "INVALID_REVIEW_CONTRACT",
      String(error && error.message || error),
      { retryable: Boolean(error && error.retryable) },
    );
  }

  const requestedSummarizerProvider = body.summarizerProvider == null
    ? null
    : normalizeReviewProvider(body.summarizerProvider);
  if (body.summarizerProvider != null && !requestedSummarizerProvider) {
    throw new InternalHttpError(
      "INVALID_REVIEW_CONTRACT",
      `Unsupported summarizerProvider '${String(body.summarizerProvider)}'`,
    );
  }
  if (env.SUMMARIZER_PROVIDER && !normalizeReviewProvider(env.SUMMARIZER_PROVIDER)) {
    throw new InternalHttpError(
      "INVALID_REVIEW_CONTRACT",
      `Unsupported SUMMARIZER_PROVIDER '${String(env.SUMMARIZER_PROVIDER)}'`,
    );
  }
  const summarizerProvider = requestedSummarizerProvider
    || normalizeReviewProvider(env.SUMMARIZER_PROVIDER)
    || criticProvider;
  const summarizerModel = optionalString(body.summarizerModel, "summarizerModel")
    || optionalString(env.SUMMARIZER_MODEL, "SUMMARIZER_MODEL")
    || (summarizerProvider === builderReviewProvider ? builderModel : null)
    || defaultReviewModel(summarizerProvider, env);
  assertReviewModelCompatible(summarizerProvider, summarizerModel, "summarizerModel");

  return {
    builderAgent,
    builderModel,
    criticProvider,
    criticModel,
    summarizerProvider,
    summarizerModel,
  };
}

function parseSseFrame(frame) {
  let eventName = null;
  const dataLines = [];
  for (const rawLine of String(frame || "").split(/\r?\n/)) {
    if (!rawLine || rawLine.startsWith(":")) continue;
    const colon = rawLine.indexOf(":");
    const field = colon < 0 ? rawLine : rawLine.slice(0, colon);
    let value = colon < 0 ? "" : rawLine.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") eventName = value.trim();
    else if (field === "data") dataLines.push(value);
  }
  if (!eventName || !dataLines.length) {
    throw new InternalHttpError(
      "INTERNAL_SSE_MALFORMED",
      "Internal SSE frame must include explicit event and data fields",
    );
  }
  let data;
  try {
    data = JSON.parse(dataLines.join("\n"));
  } catch (_error) {
    throw new InternalHttpError("INTERNAL_SSE_MALFORMED", `Internal SSE event '${eventName}' has invalid JSON`);
  }
  return { name: eventName, data };
}

/**
 * Authenticated fail-closed JSON -> SSE request for Studio loopback services.
 * Resolves only after a single well-formed terminal `done` event and response EOF.
 */
function requestInternalSse({
  port,
  path = "/api/chat",
  body,
  token,
  timeoutMs = DEFAULT_TIMEOUT_MS,
  signal,
  onEvent,
  requestImpl = http.request,
}) {
  const accessToken = token == null
    ? requireStudioAccessToken()
    : requireStudioAccessToken({ STUDIO_ACCESS_TOKEN: token });
  const parsedPort = Number(port);
  if (!Number.isInteger(parsedPort) || parsedPort < 1 || parsedPort > 65535) {
    return Promise.reject(new InternalHttpError("INTERNAL_HTTP_CONFIG", "Internal HTTP port is invalid"));
  }
  const deadlineMs = Number(timeoutMs);
  if (!Number.isFinite(deadlineMs) || deadlineMs < 1) {
    return Promise.reject(new InternalHttpError("INTERNAL_HTTP_CONFIG", "Internal HTTP timeout is invalid"));
  }
  const encodedBody = JSON.stringify(body || {});

  return new Promise((resolve, reject) => {
    let request = null;
    let response = null;
    let buffer = "";
    let donePayload = null;
    let settled = false;

    const cleanup = () => {
      clearTimeout(timer);
      if (signal) signal.removeEventListener("abort", onAbort);
    };
    const fail = (error) => {
      if (settled) return;
      settled = true;
      cleanup();
      try { response && response.destroy(); } catch (_error) {}
      try { request && request.destroy(); } catch (_error) {}
      reject(error instanceof InternalHttpError
        ? error
        : new InternalHttpError("INTERNAL_HTTP_ERROR", String(error && error.message || error), { retryable: true }));
    };
    const succeed = () => {
      if (settled) return;
      settled = true;
      cleanup();
      resolve({ done: donePayload });
    };
    const onAbort = () => fail(new InternalHttpError("INTERNAL_HTTP_ABORTED", "Internal SSE request was aborted"));
    const timer = setTimeout(() => {
      fail(new InternalHttpError(
        "INTERNAL_HTTP_TIMEOUT",
        `Internal SSE request timed out after ${deadlineMs}ms`,
        { retryable: true },
      ));
    }, deadlineMs);
    if (timer.unref) timer.unref();

    if (signal) {
      if (signal.aborted) { onAbort(); return; }
      signal.addEventListener("abort", onAbort, { once: true });
    }

    const options = {
      host: "127.0.0.1",
      port: parsedPort,
      path,
      method: "POST",
      headers: {
        Authorization: `Bearer ${accessToken}`,
        "Content-Type": "application/json",
        "Content-Length": Buffer.byteLength(encodedBody),
        Accept: "text/event-stream",
        Connection: "close",
      },
    };

    try {
      request = requestImpl(options, (inner) => {
        response = inner;
        const statusCode = Number(inner.statusCode || 0);
        if (statusCode < 200 || statusCode >= 300) {
          inner.resume();
          fail(new InternalHttpError(
            "INTERNAL_HTTP_STATUS",
            `Internal SSE request returned HTTP ${statusCode || "unknown"}`,
            { statusCode, retryable: statusCode === 429 || statusCode >= 500 },
          ));
          return;
        }
        const contentType = String(inner.headers["content-type"] || "").toLowerCase();
        if (!contentType.startsWith("text/event-stream")) {
          inner.resume();
          fail(new InternalHttpError(
            "INTERNAL_HTTP_CONTENT_TYPE",
            "Internal chat response is not text/event-stream",
          ));
          return;
        }

        inner.setEncoding("utf8");
        inner.on("data", (chunk) => {
          if (settled) return;
          buffer += chunk;
          if (Buffer.byteLength(buffer) > MAX_SSE_BUFFER_BYTES) {
            fail(new InternalHttpError("INTERNAL_SSE_TOO_LARGE", "Internal SSE frame exceeded the buffer limit"));
            return;
          }
          while (!settled) {
            const separator = buffer.match(/\r?\n\r?\n/);
            if (!separator || separator.index == null) break;
            const frame = buffer.slice(0, separator.index);
            buffer = buffer.slice(separator.index + separator[0].length);
            if (!frame.trim() || frame.trimStart().startsWith(":")) continue;
            let parsed;
            try { parsed = parseSseFrame(frame); } catch (error) { fail(error); break; }
            if (donePayload) {
              fail(new InternalHttpError("INTERNAL_SSE_MALFORMED", "Internal SSE emitted an event after done"));
              break;
            }
            if (parsed.name === "done") {
              if (!parsed.data || typeof parsed.data !== "object" || typeof parsed.data.isError !== "boolean") {
                fail(new InternalHttpError(
                  "INTERNAL_SSE_MALFORMED",
                  "Internal SSE done event must contain boolean isError",
                ));
                break;
              }
              donePayload = parsed.data;
            } else {
              try { if (onEvent) onEvent(parsed.name, parsed.data); }
              catch (error) { fail(error); break; }
            }
          }
        });
        inner.on("aborted", () => fail(new InternalHttpError(
          "INTERNAL_HTTP_ABORTED",
          "Internal SSE response was aborted",
          { retryable: true },
        )));
        inner.on("error", (error) => fail(error));
        inner.on("end", () => {
          if (settled) return;
          if (buffer.trim()) {
            fail(new InternalHttpError("INTERNAL_SSE_MALFORMED", "Internal SSE response ended with a partial frame"));
            return;
          }
          if (!donePayload) {
            fail(new InternalHttpError("INTERNAL_SSE_MISSING_DONE", "Internal SSE response ended without done"));
            return;
          }
          succeed();
        });
      });
    } catch (error) {
      fail(error);
      return;
    }
    request.on("error", (error) => fail(error));
    request.end(encodedBody);
  });
}

module.exports = {
  InternalHttpError,
  parseSseFrame,
  requestInternalSse,
  requireStudioAccessToken,
  resolveReviewContract,
  throwIfAborted,
};

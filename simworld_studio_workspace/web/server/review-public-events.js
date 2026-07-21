"use strict";

// Review streams cross a trust boundary: the inner builder runs with server-side
// filesystem access, while the outer stream is consumed by a browser. Keep this
// module deliberately allowlist based. The recursive scrubber is only a second
// line of defence for coordinator-owned events.
const FORBIDDEN_KEYS = new Set([
  "cwd",
  "directory",
  "file",
  "filepath",
  "file_path",
  "latestScreenshot",
  "latest_screenshot",
  "logPath",
  "log_path",
  "path",
  "screenshot",
]);

const SCREENSHOT_ROUTE = /\/api\/screenshot\/file\?path=[^\s"'<>]*/gi;
const FILE_URL = /file:\/\/[^\s"'<>]*/gi;
const WINDOWS_HOST_PATH = /\b[A-Za-z]:\\(?:[^\s"'<>\\]+\\)*[^\s"'<>]*/g;
const ENCODED_ABSOLUTE_PATH = /%2f(?:[^\s"'<>%]|%(?!2f))*?(?:%2f(?:[^\s"'<>%]|%(?!2f))*)+/gi;
const HTTPS_URL_CANDIDATE = /https:\/\/[^\s"'`<>]+/giu;
// HTTPS tokens are isolated before this expression runs. That lets repeated
// leading slashes remain fail-closed POSIX paths without corrupting valid URLs.
const UNIX_ABSOLUTE_PATH = /(^|[^A-Za-z0-9_])\/+[^\s"'`<>),\]};]*/gm;

function redactUnixAbsolutePaths(value) {
  return value.replace(
    UNIX_ABSOLUTE_PATH,
    (_match, prefix) => `${prefix}[review evidence hidden]`,
  );
}

function isPreservableHttpsUrl(value) {
  try {
    const parsed = new URL(value);
    return parsed.protocol === "https:" && Boolean(parsed.hostname);
  } catch (_error) {
    return false;
  }
}

function redactUnixPathsOutsideHttps(value) {
  const parts = [];
  let cursor = 0;
  for (const match of value.matchAll(HTTPS_URL_CANDIDATE)) {
    parts.push(redactUnixAbsolutePaths(value.slice(cursor, match.index)));
    parts.push(
      isPreservableHttpsUrl(match[0])
        ? match[0]
        : redactUnixAbsolutePaths(match[0]),
    );
    cursor = match.index + match[0].length;
  }
  parts.push(redactUnixAbsolutePaths(value.slice(cursor)));
  return parts.join("");
}

function sanitizePublicString(value, maxLength = 4_000) {
  const scrubbed = String(value == null ? "" : value)
    .slice(0, maxLength)
    .replace(SCREENSHOT_ROUTE, "[review evidence hidden]")
    .replace(FILE_URL, "[review evidence hidden]")
    .replace(WINDOWS_HOST_PATH, "[review evidence hidden]")
    .replace(ENCODED_ABSOLUTE_PATH, "[review evidence hidden]");
  return redactUnixPathsOutsideHttps(scrubbed);
}

function sanitizePublicReviewData(value, depth = 0, seen = new WeakSet()) {
  if (value == null || typeof value === "boolean") return value;
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "string") return sanitizePublicString(value);
  if (typeof value !== "object" || depth > 10) return null;
  if (seen.has(value)) return null;
  seen.add(value);
  if (Array.isArray(value)) {
    const output = value.slice(0, 256).map((entry) => sanitizePublicReviewData(entry, depth + 1, seen));
    seen.delete(value);
    return output;
  }
  const output = {};
  for (const [key, entry] of Object.entries(value).slice(0, 256)) {
    if (FORBIDDEN_KEYS.has(key)) continue;
    const sanitized = sanitizePublicReviewData(entry, depth + 1, seen);
    if (sanitized !== undefined) output[key] = sanitized;
  }
  seen.delete(value);
  return output;
}

function wrapPublicReviewEmitter(emit) {
  if (typeof emit !== "function") return null;
  return (name, data) => emit(name, sanitizePublicReviewData(data || {}));
}

function pickString(data, key, maxLength = 2_000) {
  return typeof data?.[key] === "string"
    ? sanitizePublicString(data[key], maxLength)
    : undefined;
}

function pickBoolean(data, key) {
  return typeof data?.[key] === "boolean" ? data[key] : undefined;
}

function compactObject(entries) {
  return Object.fromEntries(entries.filter(([, value]) => value !== undefined));
}

function sanitizeInnerReviewEvent(name, data) {
  switch (name) {
    case "text":
      return compactObject([["delta", pickString(data, "delta", 16_000)]]);
    case "tool_start":
      return compactObject([
        ["id", pickString(data, "id", 256)],
        ["name", pickString(data, "name", 256)],
        ["displayName", pickString(data, "displayName", 256)],
      ]);
    case "tool_input":
      // Incremental tool input is an unbounded fragment of a privileged
      // builder payload.  A tool_details event already carries the safe tool
      // identity, so there is no public value in relaying this event.
      return null;
    case "tool_details":
      // Tool input is intentionally omitted: it is an unbounded, tool-specific
      // object and can carry a privileged builder's output paths.
      return compactObject([
        ["id", pickString(data, "id", 256)],
        ["name", pickString(data, "name", 256)],
        ["displayName", pickString(data, "displayName", 256)],
      ]);
    case "tool_result":
      return compactObject([
        ["toolUseId", pickString(data, "toolUseId", 256)],
        ["isError", pickBoolean(data, "isError")],
      ]);
    case "retrieval":
      return compactObject([
        ["phase", pickString(data, "phase", 64)],
        ["status", pickString(data, "status", 64)],
        ["mode", pickString(data, "mode", 128)],
        ["degraded_mode", pickString(data, "degraded_mode", 128)],
        ["snapshot_revision", pickString(data, "snapshot_revision", 256)],
        ["reason", pickString(data, "reason", 512)],
        ["code", pickString(data, "code", 128)],
        ["message", pickString(data, "message", 2_000)],
      ]);
    case "skill_selection_start":
      return {};
    case "skill_selection_error":
      return compactObject([["message", pickString(data, "message", 2_000)]]);
    case "skill_selection_done": {
      const selectedSkills = Array.isArray(data?.selectedSkills)
        ? data.selectedSkills
          .filter((value) => typeof value === "string")
          .slice(0, 64)
          .map((value) => sanitizePublicString(value, 256))
        : undefined;
      return compactObject([
        ["mode", pickString(data, "mode", 64)],
        ["selectedSkills", selectedSkills],
      ]);
    }
    default:
      // system, screenshot, verifier_* and future events stay private until
      // their browser-facing schema is explicitly reviewed here.
      return null;
  }
}

function createInnerReviewRelay(emit) {
  if (typeof emit !== "function") return () => {};
  return (name, data) => {
    const sanitized = sanitizeInnerReviewEvent(name, data || {});
    if (sanitized !== null) emit(name, sanitized);
  };
}

function summarizeBuilderResult(result) {
  if (!result || typeof result !== "object") return null;
  const rawCode = result.errorCode || result.code;
  const rawError = result.error && typeof result.error === "object"
    ? result.error.message || result.error.error
    : result.error;
  return compactObject([
    ["isError", typeof result.isError === "boolean" ? result.isError : undefined],
    ["errorCode", typeof rawCode === "string" && /^[A-Z0-9_]{2,80}$/.test(rawCode) ? rawCode : undefined],
    ["error", typeof rawError === "string" ? sanitizePublicString(rawError, 500) : undefined],
    ["retryable", typeof result.retryable === "boolean" ? result.retryable : undefined],
    ["costUsd", typeof result.costUsd === "number" && Number.isFinite(result.costUsd) ? result.costUsd : undefined],
  ]);
}

module.exports = {
  createInnerReviewRelay,
  sanitizePublicReviewData,
  sanitizePublicString,
  summarizeBuilderResult,
  wrapPublicReviewEmitter,
};

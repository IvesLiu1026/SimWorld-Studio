"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const LOOPBACK_BIND_HOSTS = new Set(["127.0.0.1", "::1"]);
const LOOPBACK_REQUEST_HOSTS = new Set(["127.0.0.1", "::1", "localhost"]);
const MODEL_MODES = new Set(["off", "mock", "live"]);

function resolveBindHost(env = process.env) {
  const host = String(env.STUDIO_HOST || "127.0.0.1").trim();
  if (!LOOPBACK_BIND_HOSTS.has(host)) {
    throw new Error(`STUDIO_HOST must be a numeric loopback address, got: ${host}`);
  }
  return host;
}

function resolveModelMode(env = process.env) {
  const mode = String(env.STUDIO_MODEL_MODE || "off").trim().toLowerCase();
  if (!MODEL_MODES.has(mode)) {
    throw new Error(`STUDIO_MODEL_MODE must be off, mock, or live; got: ${mode}`);
  }
  return mode;
}

function codingAgentsEnabled(env = process.env) {
  const value = env.STUDIO_CODING_AGENTS_ENABLED;
  if (value === undefined || value === "" || value === "0" || value === "false") return false;
  if (value === "1" || value === "true") return true;
  throw new Error("STUDIO_CODING_AGENTS_ENABLED must be 1, true, 0, or false");
}

function resolveAccessToken(env = process.env) {
  const token = String(env.STUDIO_ACCESS_TOKEN || "");
  if (token.length < 32) throw new Error("STUDIO_ACCESS_TOKEN must contain at least 32 characters");
  return token;
}

function tokensEqual(left, right) {
  const leftBuffer = Buffer.from(String(left || ""));
  const rightBuffer = Buffer.from(String(right || ""));
  return leftBuffer.length === rightBuffer.length && crypto.timingSafeEqual(leftBuffer, rightBuffer);
}

function cookieValue(header, name) {
  for (const item of String(header || "").split(";")) {
    const separator = item.indexOf("=");
    if (separator < 0) continue;
    if (item.slice(0, separator).trim() === name) {
      return decodeURIComponent(item.slice(separator + 1).trim());
    }
  }
  return "";
}

function createAccessGuard(accessToken) {
  resolveAccessToken({ STUDIO_ACCESS_TOKEN: accessToken });
  return function accessGuard(req, res, next) {
    const authorization = String(req.headers.authorization || "");
    const bearer = authorization.startsWith("Bearer ") ? authorization.slice(7) : "";
    const cookie = cookieValue(req.headers.cookie, "vista_studio_access");
    if (tokensEqual(bearer, accessToken) || tokensEqual(cookie, accessToken)) return next();

    const queryToken = req.query && req.query.token;
    if (req.method === "GET" && typeof queryToken === "string" && tokensEqual(queryToken, accessToken)) {
      res.setHeader(
        "Set-Cookie",
        `vista_studio_access=${encodeURIComponent(accessToken)}; HttpOnly; SameSite=Strict; Path=/`,
      );
      const redirect = new URL(req.originalUrl || req.url, "http://localhost");
      redirect.searchParams.delete("token");
      return res.redirect(302, `${redirect.pathname}${redirect.search}`);
    }
    return res.status(401).json({ code: "STUDIO_AUTH_REQUIRED", error: "Studio access token required" });
  };
}

function resolveContainedFile(candidate, roots) {
  if (typeof candidate !== "string" || candidate.length === 0) return null;
  try {
    const realCandidate = fs.realpathSync(candidate);
    if (!fs.statSync(realCandidate).isFile()) return null;
    for (const root of roots) {
      const realRoot = fs.realpathSync(root);
      const relative = path.relative(realRoot, realCandidate);
      if (relative && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative)) {
        return realCandidate;
      }
    }
  } catch {
    return null;
  }
  return null;
}

function parseHttpAuthority(value) {
  try {
    const url = new URL(`http://${value}`);
    if (url.username || url.password || url.pathname !== "/" || url.search || url.hash) return null;
    return url;
  } catch {
    return null;
  }
}

function requestIsLoopback(req) {
  const host = parseHttpAuthority(req && req.headers && req.headers.host);
  if (!host || !LOOPBACK_REQUEST_HOSTS.has(host.hostname.replace(/^\[|\]$/g, ""))) return false;

  const originValue = req && req.headers && req.headers.origin;
  if (!originValue) return true;
  try {
    const origin = new URL(originValue);
    const originHostname = origin.hostname.replace(/^\[|\]$/g, "");
    return (origin.protocol === "http:" || origin.protocol === "https:") &&
      !origin.username && !origin.password &&
      LOOPBACK_REQUEST_HOSTS.has(originHostname) &&
      origin.host.toLowerCase() === host.host.toLowerCase();
  } catch {
    return false;
  }
}

function requestLoopbackGuard(req, res, next) {
  if (requestIsLoopback(req)) return next();
  return res.status(403).json({
    code: "LOOPBACK_REQUEST_REQUIRED",
    error: "Studio accepts only same-origin loopback requests",
  });
}

const MODEL_ENDPOINTS = [
  { kind: "coding", method: "POST", path: /^\/api\/chat$/ },
  { kind: "coding", method: "POST", path: /^\/api\/skills\/select$/ },
  { kind: "coding", method: "POST", path: /^\/api\/agent-chat$/ },
  { kind: "coding", method: "POST", path: /^\/api\/agent-broadcast$/ },
  { kind: "coding", method: "POST", path: /^\/api\/arena\/run$/ },
  { kind: "coding", method: "POST", path: /^\/api\/arena\/battles\/[^/]+\/run$/ },
  { kind: "model", method: "POST", path: /^\/api\/vlm-score$/ },
  { kind: "workload", method: "POST", path: /^\/api\/training\/start$/ },
];
const OFF_MODE_SAFE_MUTATIONS = [
  { method: "POST", path: /^\/api\/session\/(acquire|heartbeat|release)$/ },
  { method: "POST", path: /^\/api\/chat-stop$/ },
  { method: "POST", path: /^\/api\/agent-stop(-all)?$/ },
  { method: "POST", path: /^\/api\/demo\/stop$/ },
];

function classifyModelEndpoint(method, requestPath) {
  const normalizedMethod = String(method || "GET").toUpperCase();
  const normalizedPath = String(requestPath || "").split("?", 1)[0];
  const match = MODEL_ENDPOINTS.find(
    (route) => route.method === normalizedMethod && route.path.test(normalizedPath),
  );
  return match ? match.kind : null;
}

function disabled(res, reason = "Model and agent calls are disabled") {
  return res.status(503).json({ code: "MODEL_CALLS_DISABLED", error: reason });
}

function createModelGate({ mode = "off", allowCodingAgents = false, isMockReady = () => false } = {}) {
  if (!MODEL_MODES.has(mode)) throw new Error(`Invalid model mode: ${mode}`);

  return function modelGate(req, res, next) {
    const method = String(req.method || "GET").toUpperCase();
    const requestPath = String(req.path || req.url || "").split("?", 1)[0];

    if (method === "POST" && requestPath === "/api/scene-loop" && mode !== "live") {
      const requested = req.body && req.body.mode;
      const disablesLoop = requested === "vanilla" || requested === "off" ||
        (requested === undefined && req.body && req.body.enabled === false);
      return disablesLoop ? next() : disabled(res, "Scene-loop model calls are disabled");
    }
    if (method === "POST" && requestPath === "/api/dynamic-skills" && mode !== "live") {
      return req.body && req.body.enabled === false
        ? next()
        : disabled(res, "Dynamic skill generation is disabled");
    }

    if (mode !== "live") {
      const readOnly = method === "GET" || method === "HEAD" || method === "OPTIONS";
      const safeMutation = OFF_MODE_SAFE_MUTATIONS.some(
        (route) => route.method === method && route.path.test(requestPath),
      );
      if (readOnly || safeMutation) return next();
      if (mode === "mock" && requestPath === "/api/chat" && isMockReady()) {
        req.body = req.body || {};
        req.body.loopMode = "vanilla";
        req.body.useLoop = false;
        req.body.skillSelectionMode = "manual";
        return next();
      }
      return disabled(res, "Studio mutations and model calls are disabled");
    }

    const kind = classifyModelEndpoint(method, requestPath);
    if (!kind) return next();
    if (kind === "coding" && !allowCodingAgents) {
      return disabled(res, "Coding-agent execution requires an explicit security opt-in");
    }
    return next();
  };
}

module.exports = {
  classifyModelEndpoint,
  codingAgentsEnabled,
  createAccessGuard,
  createModelGate,
  requestIsLoopback,
  requestLoopbackGuard,
  resolveAccessToken,
  resolveBindHost,
  resolveContainedFile,
  resolveModelMode,
};

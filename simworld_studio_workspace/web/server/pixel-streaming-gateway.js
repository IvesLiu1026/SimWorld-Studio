"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const net = require("node:net");
const {
  assertTransportRequest,
  resolveTransportProfile,
} = require("./pixel-streaming-config");
const {
  createPixelStreamingEndpointRegistry,
} = require("./pixel-streaming-endpoint-registry");

const SESSION_COOKIE = "vista_stream_session";
const PRINCIPAL_COOKIE = "vista_browser_principal";
const SESSION_TOKEN_PATTERN = /^[a-f0-9]{64}$/;
const PRINCIPAL_SUBJECT_PATTERN = /^[A-Za-z0-9_-]{43}$/;
const PRINCIPAL_SIGNATURE_PATTERN = /^[A-Za-z0-9_-]{43}$/;
const OWNER_ID_PATTERN = /^browser-[a-f0-9]{64}$/;
const LEASE_ID_PATTERN = /^[A-Za-z0-9_-]{16,128}$/;
const ENDPOINT_ID_PATTERN = /^ps1_[A-Za-z0-9_-]{43}$/;
const MIN_ENDPOINT_TTL_MS = 5 * 60 * 1000;
const MAX_ENDPOINT_TTL_MS = 24 * 60 * 60 * 1000;
const DEFAULT_SESSION_TTL_MS = 30 * 60 * 1000;
const DEFAULT_SESSION_HARD_MAX_MS = 60 * 60 * 1000;
const DEFAULT_PRINCIPAL_TTL_MS = 30 * 24 * 60 * 60 * 1000;
const MIN_PRINCIPAL_TTL_MS = 24 * 60 * 60 * 1000;
const MAX_PRINCIPAL_TTL_MS = 365 * 24 * 60 * 60 * 1000;
const PRINCIPAL_CLOCK_SKEW_MS = 5 * 60 * 1000;
const MAX_UPGRADE_HEADER_BYTES = 16 * 1024;

class PixelStreamingGatewayError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "PixelStreamingGatewayError";
    this.code = code;
  }
}

function fail(code, message) {
  throw new PixelStreamingGatewayError(code, message);
}

function positiveInteger(value, fallback, minimum, maximum, field) {
  const parsed = value === undefined || value === "" ? fallback : Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < minimum || parsed > maximum) {
    fail("PIXEL_STREAMING_GATEWAY_CONFIG_INVALID", `${field} is outside the supported range`);
  }
  return parsed;
}

function exactCookieValue(header, name) {
  let found = null;
  for (const item of String(header || "").split(";")) {
    const separator = item.indexOf("=");
    if (separator < 0) continue;
    if (item.slice(0, separator).trim() !== name) continue;
    if (found !== null) return "";
    try {
      found = decodeURIComponent(item.slice(separator + 1).trim());
    } catch {
      return "";
    }
  }
  return found || "";
}

function sessionTokenFromRequest(request) {
  const token = exactCookieValue(request && request.headers && request.headers.cookie, SESSION_COOKIE);
  return SESSION_TOKEN_PATTERN.test(token) ? token : "";
}

function serializeSessionCookie(token, transport, { clear = false, maxAgeMs } = {}) {
  if (!transport || !transport.cookie) {
    fail("PIXEL_STREAMING_GATEWAY_CONFIG_INVALID", "A resolved transport profile is required");
  }
  if (!clear && !SESSION_TOKEN_PATTERN.test(String(token || ""))) {
    fail("PIXEL_STREAMING_SESSION_COOKIE_INVALID", "Session cookie token is invalid");
  }
  const parts = [
    `${SESSION_COOKIE}=${clear ? "" : encodeURIComponent(token)}`,
    "Path=/",
    "HttpOnly",
    `SameSite=${transport.cookie.sameSite}`,
  ];
  if (transport.cookie.secure) parts.push("Secure");
  if (clear) {
    parts.push("Max-Age=0", "Expires=Thu, 01 Jan 1970 00:00:00 GMT");
  } else {
    const seconds = Math.max(1, Math.floor(maxAgeMs / 1000));
    parts.push(`Max-Age=${seconds}`);
  }
  return parts.join("; ");
}

function serializePrincipalCookie(value, transport, { clear = false, maxAgeMs } = {}) {
  if (!transport || !transport.cookie) {
    fail("PIXEL_STREAMING_GATEWAY_CONFIG_INVALID", "A resolved transport profile is required");
  }
  if (!clear && (typeof value !== "string" || value.length < 1 || value.length > 512 ||
      /[^A-Za-z0-9._-]/.test(value))) {
    fail("PIXEL_STREAMING_PRINCIPAL_COOKIE_INVALID", "Principal cookie value is invalid");
  }
  const parts = [
    `${PRINCIPAL_COOKIE}=${clear ? "" : encodeURIComponent(value)}`,
    "Path=/",
    "HttpOnly",
    `SameSite=${transport.cookie.sameSite}`,
  ];
  if (transport.cookie.secure) parts.push("Secure");
  if (clear) {
    parts.push("Max-Age=0", "Expires=Thu, 01 Jan 1970 00:00:00 GMT");
  } else {
    const seconds = Math.max(1, Math.floor(maxAgeMs / 1000));
    parts.push(`Max-Age=${seconds}`);
  }
  return parts.join("; ");
}

function appendSetCookie(response, value) {
  let existing;
  if (response && typeof response.getHeader === "function") {
    existing = response.getHeader("Set-Cookie");
  } else if (response && response.headers) {
    existing = response.headers["set-cookie"];
  }
  const values = existing === undefined
    ? []
    : Array.isArray(existing) ? existing.slice() : [String(existing)];
  values.push(value);
  response.setHeader("Set-Cookie", values);
}

function readSecretFile(candidate) {
  if (typeof candidate !== "string" || !candidate.trim()) return "";
  const value = fs.readFileSync(candidate.trim(), "utf8");
  if (value.endsWith("\n")) return value.slice(0, -1);
  return value;
}

function resolveEndpointHmacKey(env, transport) {
  const direct = String(env.STUDIO_PIXEL_STREAMING_HMAC_KEY || "");
  const fromFile = direct ? "" : readSecretFile(env.STUDIO_PIXEL_STREAMING_HMAC_KEY_FILE);
  const configured = direct || fromFile;
  if (Buffer.byteLength(configured) >= 32) return configured;

  if (configured) {
    fail(
      "PIXEL_STREAMING_ENDPOINT_SECRET_INVALID",
      "STUDIO_PIXEL_STREAMING_HMAC_KEY must contain at least 32 bytes",
    );
  }
  if (transport.profile === "trusted_proxy") {
    fail(
      "PIXEL_STREAMING_ENDPOINT_SECRET_REQUIRED",
      "trusted_proxy requires STUDIO_PIXEL_STREAMING_HMAC_KEY or its secret file",
    );
  }

  const localAccessToken = String(env.STUDIO_ACCESS_TOKEN || "");
  if (Buffer.byteLength(localAccessToken) < 32) {
    fail(
      "PIXEL_STREAMING_ENDPOINT_SECRET_REQUIRED",
      "Loopback streaming requires a strong Studio access token or an endpoint HMAC key",
    );
  }
  return crypto
    .createHmac("sha256", localAccessToken)
    .update("simworld/pixel-streaming-endpoint/loopback/v1")
    .digest();
}

function derivePrincipalHmacKey(endpointHmacKey) {
  return crypto
    .createHmac("sha256", endpointHmacKey)
    .update("simworld/browser-principal/signing/v1")
    .digest();
}

function principalOwnerId(subject, principalHmacKey) {
  return `browser-${crypto
    .createHmac("sha256", principalHmacKey)
    .update("simworld/browser-principal/owner/v1\0")
    .update(subject)
    .digest("hex")}`;
}

function signPrincipalPayload(payload, principalHmacKey) {
  return crypto.createHmac("sha256", principalHmacKey).update(payload).digest("base64url");
}

function sameSignature(left, right) {
  if (!PRINCIPAL_SIGNATURE_PATTERN.test(String(left || "")) ||
      !PRINCIPAL_SIGNATURE_PATTERN.test(String(right || ""))) {
    return false;
  }
  const leftBytes = Buffer.from(left, "ascii");
  const rightBytes = Buffer.from(right, "ascii");
  return crypto.timingSafeEqual(leftBytes, rightBytes);
}

function createPrincipal(subject, principalHmacKey, now, ttlMs) {
  if (!PRINCIPAL_SUBJECT_PATTERN.test(String(subject || "")) ||
      !Number.isSafeInteger(now) || now < 0 ||
      !Number.isSafeInteger(ttlMs) || ttlMs < MIN_PRINCIPAL_TTL_MS || ttlMs > MAX_PRINCIPAL_TTL_MS ||
      !Number.isSafeInteger(now + ttlMs)) {
    fail("PIXEL_STREAMING_PRINCIPAL_INVALID", "Browser principal cannot be issued");
  }
  const expiresAt = now + ttlMs;
  const payload = `bp1.${subject}.${now}.${expiresAt}`;
  return Object.freeze({
    subject,
    ownerId: principalOwnerId(subject, principalHmacKey),
    issuedAt: now,
    expiresAt,
    value: `${payload}.${signPrincipalPayload(payload, principalHmacKey)}`,
  });
}

function parsePrincipal(value, principalHmacKey, now) {
  if (typeof value !== "string" || value.length < 1 || value.length > 512 ||
      !Number.isSafeInteger(now) || now < 0) {
    return null;
  }
  const parts = value.split(".");
  if (parts.length !== 5 || parts[0] !== "bp1" ||
      !PRINCIPAL_SUBJECT_PATTERN.test(parts[1]) ||
      !/^\d{1,16}$/.test(parts[2]) || !/^\d{1,16}$/.test(parts[3]) ||
      !PRINCIPAL_SIGNATURE_PATTERN.test(parts[4])) {
    return null;
  }
  const issuedAt = Number(parts[2]);
  const expiresAt = Number(parts[3]);
  if (!Number.isSafeInteger(issuedAt) || !Number.isSafeInteger(expiresAt) ||
      issuedAt < 0 || expiresAt <= issuedAt ||
      expiresAt - issuedAt > MAX_PRINCIPAL_TTL_MS ||
      issuedAt > now + PRINCIPAL_CLOCK_SKEW_MS || expiresAt <= now) {
    return null;
  }
  const payload = parts.slice(0, 4).join(".");
  if (!sameSignature(parts[4], signPrincipalPayload(payload, principalHmacKey))) return null;
  return Object.freeze({
    subject: parts[1],
    ownerId: principalOwnerId(parts[1], principalHmacKey),
    issuedAt,
    expiresAt,
    value,
  });
}

function validSessionRecord(record, token, ownerId) {
  return Boolean(record && typeof record === "object" &&
    SESSION_TOKEN_PATTERN.test(String(token || "")) && record.token === token &&
    OWNER_ID_PATTERN.test(String(ownerId || "")) && record.userId === ownerId &&
    Number.isSafeInteger(record.slotId) && record.slotId >= 0 && record.slotId <= 65535 &&
    LEASE_ID_PATTERN.test(String(record.leaseId || "")));
}

function hashedSessionId(record, principalHmacKey) {
  return `session-${crypto
    .createHmac("sha256", principalHmacKey)
    .update("simworld/active-session/id/v1\0")
    .update(record.token)
    .update("\0")
    .update(record.leaseId)
    .digest("hex")}`;
}

function publicSession(record, manager, sessionTtlMs) {
  return Object.freeze({
    schema: "studio-session/v2",
    slotId: record.slotId,
    totalSlots: manager.totalSlots,
    freeSlots: manager.freeSlots,
    sessionTtlMs,
    dev: false,
  });
}

function websocketScheme(transport) {
  return transport.profile === "trusted_proxy" ? "wss" : "ws";
}

function isStreamingEndpointPath(pathname, pathPrefix) {
  return typeof pathname === "string" && typeof pathPrefix === "string" &&
    pathname.startsWith(`${pathPrefix}/`) &&
    ENDPOINT_ID_PATTERN.test(pathname.slice(pathPrefix.length + 1));
}

function headerValue(request, name, maximumLength = 4096) {
  const value = request && request.headers && request.headers[name];
  if (typeof value !== "string" || !value || value.length > maximumLength || /[\r\n]/.test(value)) {
    return "";
  }
  return value;
}

function validUpgradeRequest(request) {
  const upgrade = headerValue(request, "upgrade", 64).toLowerCase();
  const connection = headerValue(request, "connection", 256)
    .toLowerCase()
    .split(",")
    .map((value) => value.trim());
  return request.method === "GET" && upgrade === "websocket" && connection.includes("upgrade") &&
    /^[A-Za-z0-9+/]{22}==$/.test(headerValue(request, "sec-websocket-key", 64)) &&
    headerValue(request, "sec-websocket-version", 16) === "13";
}

function upstreamUpgradeRequest(request, upstream) {
  const lines = [
    "GET / HTTP/1.1",
    `Host: ${upstream.hostname}:${upstream.port}`,
    "Connection: Upgrade",
    "Upgrade: websocket",
    `Sec-WebSocket-Key: ${headerValue(request, "sec-websocket-key", 64)}`,
    "Sec-WebSocket-Version: 13",
  ];
  for (const [source, destination, maximumLength] of [
    ["origin", "Origin", 2048],
    ["sec-websocket-protocol", "Sec-WebSocket-Protocol", 2048],
    ["sec-websocket-extensions", "Sec-WebSocket-Extensions", 4096],
  ]) {
    const value = headerValue(request, source, maximumLength);
    if (value) lines.push(`${destination}: ${value}`);
  }
  const serialized = `${lines.join("\r\n")}\r\n\r\n`;
  if (Buffer.byteLength(serialized) > MAX_UPGRADE_HEADER_BYTES) {
    fail("PIXEL_STREAMING_UPGRADE_REJECTED", "WebSocket upgrade headers are too large");
  }
  return serialized;
}

function rejectUpgrade(socket, status = 404) {
  if (!socket || socket.destroyed) return;
  const reason = status === 401 ? "Unauthorized" : status === 400 ? "Bad Request" : "Not Found";
  socket.end(
    `HTTP/1.1 ${status} ${reason}\r\n` +
    "Connection: close\r\n" +
    "Cache-Control: no-store\r\n" +
    "Content-Length: 0\r\n\r\n",
  );
}

function createStudioStreamingRuntime({
  env = process.env,
  sessionManager,
  webRtcFps = 60,
  logger = () => {},
  registryOptions = {},
} = {}) {
  if (!sessionManager || typeof sessionManager.acquire !== "function" ||
      typeof sessionManager.touch !== "function" || typeof sessionManager.release !== "function") {
    fail("PIXEL_STREAMING_GATEWAY_CONFIG_INVALID", "A session manager is required");
  }
  if (webRtcFps !== 30 && webRtcFps !== 60) {
    fail("PIXEL_STREAMING_GATEWAY_CONFIG_INVALID", "webRtcFps must be 30 or 60");
  }

  const transport = resolveTransportProfile(env);
  const sessionTtlMs = positiveInteger(
    env.SESSION_TTL_MS,
    DEFAULT_SESSION_TTL_MS,
    10_000,
    MAX_ENDPOINT_TTL_MS,
    "SESSION_TTL_MS",
  );
  const sessionHardMaxMs = positiveInteger(
    env.SESSION_HARD_MAX_MS,
    DEFAULT_SESSION_HARD_MAX_MS,
    sessionTtlMs,
    MAX_ENDPOINT_TTL_MS,
    "SESSION_HARD_MAX_MS",
  );
  const endpointTtlMs = positiveInteger(
    env.STUDIO_PIXEL_STREAMING_ENDPOINT_TTL_MS,
    Math.max(MIN_ENDPOINT_TTL_MS, sessionHardMaxMs),
    Math.max(MIN_ENDPOINT_TTL_MS, sessionHardMaxMs),
    MAX_ENDPOINT_TTL_MS,
    "STUDIO_PIXEL_STREAMING_ENDPOINT_TTL_MS",
  );
  const principalTtlMs = positiveInteger(
    env.STUDIO_BROWSER_PRINCIPAL_TTL_MS,
    DEFAULT_PRINCIPAL_TTL_MS,
    MIN_PRINCIPAL_TTL_MS,
    MAX_PRINCIPAL_TTL_MS,
    "STUDIO_BROWSER_PRINCIPAL_TTL_MS",
  );
  const endpointHmacKey = resolveEndpointHmacKey(env, transport);
  const principalHmacKey = derivePrincipalHmacKey(endpointHmacKey);
  const proxyContext = Object.freeze({ capability: "pixel-streaming-loopback-proxy/v1" });
  const registry = createPixelStreamingEndpointRegistry({
    endpointSecrets: { endpointHmacKey },
    trustedProxyContext: proxyContext,
    ttlMs: endpointTtlMs,
    pathPrefix: transport.streamingPathPrefix,
    ...registryOptions,
  });
  const endpointBySessionToken = new Map();

  function currentTime() {
    const now = Date.now();
    if (!Number.isSafeInteger(now) || now < 0) {
      fail("PIXEL_STREAMING_PRINCIPAL_CLOCK_INVALID", "Browser principal clock is invalid");
    }
    return now;
  }

  function principalFromRequest(request) {
    const value = exactCookieValue(
      request && request.headers && request.headers.cookie,
      PRINCIPAL_COOKIE,
    );
    return parsePrincipal(value, principalHmacKey, currentTime());
  }

  function issuePrincipal(subject = crypto.randomBytes(32).toString("base64url")) {
    return createPrincipal(subject, principalHmacKey, currentTime(), principalTtlMs);
  }

  function contextForRecord(record, token, principal) {
    if (!principal || !validSessionRecord(record, token, principal.ownerId)) return null;
    const binding = Object.freeze({
      ownerId: principal.ownerId,
      sessionId: hashedSessionId(record, principalHmacKey),
      slotId: record.slotId,
      leaseId: record.leaseId,
    });
    return Object.freeze({ record, principal, binding });
  }

  function activeContextForRequest(request) {
    const principal = principalFromRequest(request);
    const token = sessionTokenFromRequest(request);
    if (!principal || !token) return null;
    const record = sessionManager.touch(token);
    return contextForRecord(record, token, principal);
  }

  function resolveActiveSession(request) {
    const context = activeContextForRequest(request);
    if (!context || context.record.mcpReady !== true) return null;
    const mcpPort = context.record.uePorts && context.record.uePorts.mcpPort;
    if (!Number.isSafeInteger(mcpPort) || mcpPort < 1 || mcpPort > 65535) return null;
    if (Number.isSafeInteger(sessionManager.totalSlots) &&
        (context.record.slotId < 0 || context.record.slotId >= sessionManager.totalSlots)) {
      return null;
    }
    return Object.freeze({ ...context.binding, mcpPort });
  }

  function rememberEndpoint(context, endpoint) {
    endpointBySessionToken.set(context.record.token, Object.freeze({
      endpoint,
      binding: context.binding,
    }));
  }

  function revokeToken(token) {
    const remembered = endpointBySessionToken.get(token);
    endpointBySessionToken.delete(token);
    if (!remembered) return;
    try {
      registry.revoke(remembered.endpoint.path, remembered.binding);
    } catch {
      // Expiry and an explicit release are equivalent from the public side.
    }
  }

  function setPrincipalCookie(response, principal) {
    appendSetCookie(
      response,
      serializePrincipalCookie(principal.value, transport, { maxAgeMs: principalTtlMs }),
    );
  }

  function setSessionCookie(response, token) {
    appendSetCookie(
      response,
      serializeSessionCookie(token, transport, { maxAgeMs: sessionHardMaxMs }),
    );
  }

  function clearSessionCookie(response) {
    appendSetCookie(response, serializeSessionCookie("", transport, { clear: true }));
  }

  async function acquireSession(request, response) {
    let principal = principalFromRequest(request);
    let context = activeContextForRequest(request);
    try {
      if (!context) {
        if (!principal) principal = issuePrincipal();
        const record = await sessionManager.acquire(principal.ownerId);
        context = contextForRecord(record, record && record.token, principal);
      }
      if (!context) throw new Error("Session record does not meet the streaming identity contract");
      principal = issuePrincipal(principal.subject);
      setPrincipalCookie(response, principal);
      setSessionCookie(response, context.record.token);
      response.setHeader("Cache-Control", "no-store");
      return response.json(publicSession(context.record, sessionManager, sessionTtlMs));
    } catch (error) {
      const publicCode = /^[A-Z][A-Z0-9_]{0,63}$/.test(String(error.code || ""))
        ? error.code
        : "UNAVAILABLE";
      logger("streaming", `${publicCode}: session acquisition failed`);
      return response.status(503).json({
        error: "Studio session is unavailable",
        code: publicCode,
        queueLength: sessionManager.queueLength,
      });
    }
  }

  function heartbeatSession(request, response) {
    const context = activeContextForRequest(request);
    if (!context) {
      clearSessionCookie(response);
      return response.status(401).json({ code: "SESSION_EXPIRED", error: "Session expired or invalid" });
    }
    const principal = issuePrincipal(context.principal.subject);
    setPrincipalCookie(response, principal);
    setSessionCookie(response, context.record.token);
    response.setHeader("Cache-Control", "no-store");
    return response.json({
      schema: "studio-session-heartbeat/v2",
      ok: true,
      slotId: context.record.slotId,
      idleMs: 0,
      sessionTtlMs,
    });
  }

  function releaseSession(request, response) {
    const context = activeContextForRequest(request);
    if (context) {
      revokeToken(context.record.token);
      sessionManager.release(context.record.token);
    }
    clearSessionCookie(response);
    response.setHeader("Cache-Control", "no-store");
    return response.json({ ok: true });
  }

  function issueEndpoint(request, response) {
    const context = activeContextForRequest(request);
    if (!context) {
      clearSessionCookie(response);
      return response.status(401).json({
        code: "STREAMING_SESSION_REQUIRED",
        error: "An active Studio session is required",
      });
    }
    const cirrusHttpPort = Number(context.record.uePorts && context.record.uePorts.cirrusHttp);
    try {
      const endpoint = registry.acquire({ ...context.binding, cirrusHttpPort });
      rememberEndpoint(context, endpoint);
      response.setHeader("Cache-Control", "no-store");
      return response.json({
        schema: "pixel-streaming-endpoint/v1",
        path: endpoint.path,
        expiresAt: endpoint.expiresAt,
        webRtcFps,
      });
    } catch (error) {
      logger("streaming", `${error.code || "STREAMING_ENDPOINT_UNAVAILABLE"}: ${error.message}`);
      return response.status(503).json({
        code: error.code || "STREAMING_ENDPOINT_UNAVAILABLE",
        error: "Pixel Streaming endpoint is unavailable",
      });
    }
  }

  function handleUpgrade(request, socket, head = Buffer.alloc(0)) {
    let parsed;
    try {
      parsed = new URL(request.url, "http://pixel-streaming.invalid");
      if (parsed.search || parsed.hash ||
          !isStreamingEndpointPath(parsed.pathname, transport.streamingPathPrefix)) {
        return rejectUpgrade(socket, 404);
      }
      assertTransportRequest(request, transport, { requireOrigin: true });
      if (!validUpgradeRequest(request)) return rejectUpgrade(socket, 400);
    } catch {
      return rejectUpgrade(socket, 404);
    }

    const context = activeContextForRequest(request);
    if (!context) return rejectUpgrade(socket, 401);

    let upstream;
    let serializedRequest;
    try {
      upstream = registry.resolveForProxy(parsed.pathname, context.binding, proxyContext);
      serializedRequest = upstreamUpgradeRequest(request, upstream);
    } catch {
      return rejectUpgrade(socket, 404);
    }

    const upstreamSocket = net.createConnection({ host: upstream.hostname, port: upstream.port });
    let connected = false;
    const closeBoth = () => {
      if (!socket.destroyed) socket.destroy();
      if (!upstreamSocket.destroyed) upstreamSocket.destroy();
    };
    upstreamSocket.setTimeout(10_000, closeBoth);
    upstreamSocket.once("connect", () => {
      connected = true;
      upstreamSocket.setTimeout(0);
      upstreamSocket.write(serializedRequest);
      if (head && head.length) upstreamSocket.write(head);
      socket.pipe(upstreamSocket).pipe(socket);
    });
    upstreamSocket.once("error", () => {
      if (!connected) rejectUpgrade(socket, 404);
      else closeBoth();
    });
    socket.once("error", closeBoth);
  }

  const releasedListener = ({ token }) => revokeToken(token);
  if (typeof sessionManager.on === "function") sessionManager.on("released", releasedListener);

  return Object.freeze({
    transport,
    registry,
    acquireSession,
    heartbeatSession,
    releaseSession,
    issueEndpoint,
    resolveActiveSession,
    handleUpgrade,
    attach(server) {
      if (!server || typeof server.on !== "function") {
        fail("PIXEL_STREAMING_GATEWAY_CONFIG_INVALID", "An HTTP server is required");
      }
      server.on("upgrade", handleUpgrade);
      return server;
    },
    destroy() {
      if (typeof sessionManager.off === "function") sessionManager.off("released", releasedListener);
      endpointBySessionToken.clear();
    },
    playerSignallingUrl(pathname, host) {
      if (!isStreamingEndpointPath(pathname, transport.streamingPathPrefix)) return null;
      if (transport.profile === "trusted_proxy") {
        return `${websocketScheme(transport)}://${transport.publicHost}${pathname}`;
      }
      if (typeof host !== "string" || !host) return null;
      return `${websocketScheme(transport)}://${host}${pathname}`;
    },
  });
}

module.exports = {
  ENDPOINT_ID_PATTERN,
  PixelStreamingGatewayError,
  PRINCIPAL_COOKIE,
  SESSION_COOKIE,
  createStudioStreamingRuntime,
  exactCookieValue,
  isStreamingEndpointPath,
  serializePrincipalCookie,
  serializeSessionCookie,
  sessionTokenFromRequest,
  upstreamUpgradeRequest,
  validUpgradeRequest,
};

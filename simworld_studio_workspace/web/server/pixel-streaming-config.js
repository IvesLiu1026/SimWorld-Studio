"use strict";

const crypto = require("node:crypto");
const net = require("node:net");

const TRANSPORT_PROFILES = new Set(["loopback", "trusted_proxy"]);
const ICE_TRANSPORT_POLICIES = new Set(["all", "relay"]);
const LOOPBACK_ADDRESSES = new Set(["127.0.0.1", "::1"]);
const LOOPBACK_REQUEST_HOSTS = new Set(["127.0.0.1", "::1", "localhost"]);
const DEFAULT_STREAMING_PATH_PREFIX = "/pixel-stream/session";
const ENDPOINT_ID_PATTERN = /^ps1_[A-Za-z0-9_-]{43}$/;
const PLACEHOLDER_SECRET_PATTERN = /^(?:change[_-]?me(?:[_-].*)?|password|secret|turn[_-]?password)$/i;

class PixelStreamingConfigError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "PixelStreamingConfigError";
    this.code = code;
  }
}

function fail(code, message) {
  throw new PixelStreamingConfigError(code, message);
}

function nonEmptyString(value, field, maximumLength = 256) {
  if (typeof value !== "string" || value.length === 0 || value.length > maximumLength ||
      value.trim() !== value || /[\u0000-\u001f\u007f]/.test(value)) {
    fail("PIXEL_STREAMING_CONFIG_INVALID", `${field} must be a non-empty bounded string`);
  }
  return value;
}

function assertKnownKeys(value, allowed, field) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    fail("PIXEL_STREAMING_CONFIG_INVALID", `${field} must be an object`);
  }
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) {
      fail("PIXEL_STREAMING_CONFIG_INVALID", `${field} contains an unsupported field`);
    }
  }
}

function normalizeLoopbackAddress(value) {
  if (typeof value !== "string") return null;
  let address = value.trim().toLowerCase();
  if (address.startsWith("::ffff:")) address = address.slice(7);
  if (net.isIP(address) === 0 || !LOOPBACK_ADDRESSES.has(address)) return null;
  return address;
}

function isLoopbackRequestHostname(value) {
  return typeof value === "string" &&
    LOOPBACK_REQUEST_HOSTS.has(value.replace(/^\[|\]$/g, "").toLowerCase());
}

function normalizePathPrefix(value = DEFAULT_STREAMING_PATH_PREFIX) {
  if (typeof value !== "string" || value.length < 2 || value.length > 128 ||
      !value.startsWith("/") || value.startsWith("//") || value.endsWith("/") ||
      value.includes("?") || value.includes("#") || value.includes("\\") ||
      /%(?:2e|2f|5c)/i.test(value)) {
    fail("PIXEL_STREAMING_PATH_INVALID", "Streaming path prefix must be a fixed same-origin path");
  }
  const segments = value.slice(1).split("/");
  if (segments.some((segment) => !/^[A-Za-z0-9_-]+$/.test(segment) || segment === "." || segment === "..")) {
    fail("PIXEL_STREAMING_PATH_INVALID", "Streaming path prefix contains an invalid segment");
  }
  return value;
}

function parsePublicOrigin(value) {
  if (typeof value !== "string" || !value || value.trim() !== value) {
    fail("PIXEL_STREAMING_PUBLIC_ORIGIN_REQUIRED", "trusted_proxy requires STUDIO_PUBLIC_ORIGIN");
  }
  let origin;
  try {
    origin = new URL(value);
  } catch {
    fail("PIXEL_STREAMING_PUBLIC_ORIGIN_INVALID", "STUDIO_PUBLIC_ORIGIN must be an absolute HTTPS origin");
  }
  if (origin.protocol !== "https:" || origin.username || origin.password ||
      origin.pathname !== "/" || origin.search || origin.hash || origin.origin !== value) {
    fail("PIXEL_STREAMING_PUBLIC_ORIGIN_INVALID", "STUDIO_PUBLIC_ORIGIN must be an exact HTTPS origin");
  }
  return origin;
}

function parseTrustedProxyAddresses(value) {
  if (typeof value !== "string" || !value.trim()) {
    fail("PIXEL_STREAMING_TRUSTED_PROXY_REQUIRED", "trusted_proxy requires STUDIO_TRUSTED_PROXY");
  }
  const result = [];
  for (const item of value.split(",")) {
    const normalized = normalizeLoopbackAddress(item);
    if (!normalized) {
      fail(
        "PIXEL_STREAMING_TRUSTED_PROXY_INVALID",
        "STUDIO_TRUSTED_PROXY accepts only explicit numeric loopback addresses",
      );
    }
    if (!result.includes(normalized)) result.push(normalized);
  }
  return Object.freeze(result.sort());
}

function resolveTransportProfile(env = process.env) {
  const profile = String(env.STUDIO_TRANSPORT_PROFILE || "loopback").trim().toLowerCase();
  if (!TRANSPORT_PROFILES.has(profile)) {
    fail(
      "PIXEL_STREAMING_TRANSPORT_PROFILE_INVALID",
      "STUDIO_TRANSPORT_PROFILE must be loopback or trusted_proxy",
    );
  }
  const pathPrefix = normalizePathPrefix(
    env.STUDIO_PIXEL_STREAMING_PATH_PREFIX || DEFAULT_STREAMING_PATH_PREFIX,
  );

  if (profile === "loopback") {
    if (String(env.STUDIO_PUBLIC_ORIGIN || "").trim() ||
        String(env.STUDIO_TRUSTED_PROXY || "").trim()) {
      fail(
        "PIXEL_STREAMING_TRANSPORT_PROFILE_CONFLICT",
        "Public origin and trusted proxy settings require the trusted_proxy profile",
      );
    }
    return Object.freeze({
      profile,
      publicOrigin: null,
      publicHost: null,
      trustedProxyAddresses: Object.freeze([]),
      forwardedHeaders: false,
      streamingPathPrefix: pathPrefix,
      cookie: Object.freeze({ httpOnly: true, sameSite: "Strict", secure: false }),
    });
  }

  const publicOrigin = parsePublicOrigin(env.STUDIO_PUBLIC_ORIGIN);
  return Object.freeze({
    profile,
    publicOrigin: publicOrigin.origin,
    publicHost: publicOrigin.host,
    trustedProxyAddresses: parseTrustedProxyAddresses(env.STUDIO_TRUSTED_PROXY),
    forwardedHeaders: true,
    streamingPathPrefix: pathPrefix,
    cookie: Object.freeze({ httpOnly: true, sameSite: "Strict", secure: true }),
  });
}

function singleHeader(headers, name) {
  const raw = headers && headers[name];
  if (raw === undefined) return "";
  if (Array.isArray(raw)) {
    if (raw.length !== 1) fail("PIXEL_STREAMING_REQUEST_REJECTED", "Ambiguous forwarded headers");
    return String(raw[0]);
  }
  const value = String(raw);
  if (value.includes(",") || /[\r\n]/.test(value)) {
    fail("PIXEL_STREAMING_REQUEST_REJECTED", "Ambiguous forwarded headers");
  }
  return value;
}

function parseHttpAuthority(value) {
  if (!value) return null;
  try {
    const url = new URL(`http://${value}`);
    if (url.username || url.password || url.pathname !== "/" || url.search || url.hash) return null;
    return url;
  } catch {
    return null;
  }
}

function remoteAddressOf(request) {
  return request && request.socket && request.socket.remoteAddress ||
    request && request.connection && request.connection.remoteAddress || "";
}

function assertTransportRequest(request, transport, { requireOrigin = false } = {}) {
  if (!transport || !TRANSPORT_PROFILES.has(transport.profile)) {
    fail("PIXEL_STREAMING_CONFIG_INVALID", "A resolved transport profile is required");
  }
  const headers = request && request.headers || {};
  const host = singleHeader(headers, "host");
  const originValue = singleHeader(headers, "origin");
  const remoteAddress = normalizeLoopbackAddress(remoteAddressOf(request));
  if (!host || !remoteAddress) {
    fail("PIXEL_STREAMING_REQUEST_REJECTED", "Request source or authority is not allowed");
  }

  if (transport.profile === "loopback") {
    const authority = parseHttpAuthority(host);
    if (!authority || !isLoopbackRequestHostname(authority.hostname)) {
      fail("PIXEL_STREAMING_REQUEST_REJECTED", "Loopback transport requires a loopback authority");
    }
    for (const header of ["x-forwarded-proto", "x-forwarded-host", "x-forwarded-port"]) {
      if (singleHeader(headers, header)) {
        fail("PIXEL_STREAMING_REQUEST_REJECTED", "Forwarded headers are disabled in loopback mode");
      }
    }
    if (requireOrigin && !originValue) {
      fail("PIXEL_STREAMING_REQUEST_REJECTED", "Origin is required for this request");
    }
    if (originValue) {
      let origin;
      try {
        origin = new URL(originValue);
      } catch {
        fail("PIXEL_STREAMING_REQUEST_REJECTED", "Request origin is invalid");
      }
      if ((origin.protocol !== "http:" && origin.protocol !== "https:") ||
          origin.host.toLowerCase() !== authority.host.toLowerCase() ||
          !isLoopbackRequestHostname(origin.hostname)) {
        fail("PIXEL_STREAMING_REQUEST_REJECTED", "Request origin does not match the loopback authority");
      }
    }
    return Object.freeze({ profile: transport.profile, origin: originValue || null });
  }

  if (!transport.trustedProxyAddresses.includes(remoteAddress)) {
    fail("PIXEL_STREAMING_REQUEST_REJECTED", "Request did not arrive from a trusted proxy");
  }
  const forwardedProto = singleHeader(headers, "x-forwarded-proto");
  const forwardedHost = singleHeader(headers, "x-forwarded-host");
  const forwardedPort = singleHeader(headers, "x-forwarded-port");
  if (host.toLowerCase() !== transport.publicHost.toLowerCase() ||
      forwardedProto !== "https" ||
      forwardedHost.toLowerCase() !== transport.publicHost.toLowerCase()) {
    fail("PIXEL_STREAMING_REQUEST_REJECTED", "Forwarded authority does not match STUDIO_PUBLIC_ORIGIN");
  }
  const publicPort = new URL(transport.publicOrigin).port || "443";
  if (forwardedPort && forwardedPort !== publicPort) {
    fail("PIXEL_STREAMING_REQUEST_REJECTED", "Forwarded port does not match STUDIO_PUBLIC_ORIGIN");
  }
  if (requireOrigin && !originValue) {
    fail("PIXEL_STREAMING_REQUEST_REJECTED", "Origin is required for this request");
  }
  if (originValue && originValue !== transport.publicOrigin) {
    fail("PIXEL_STREAMING_REQUEST_REJECTED", "Request origin does not match STUDIO_PUBLIC_ORIGIN");
  }
  return Object.freeze({ profile: transport.profile, origin: transport.publicOrigin });
}

function validPort(value, field) {
  if (!Number.isSafeInteger(value) || value < 1 || value > 65535) {
    fail("PIXEL_STREAMING_PORT_INVALID", `${field} must be a valid TCP port`);
  }
  return value;
}

function buildPixelStreamingCsp(transport, { signalingPort = null } = {}) {
  if (!transport || !TRANSPORT_PROFILES.has(transport.profile)) {
    fail("PIXEL_STREAMING_CONFIG_INVALID", "A resolved transport profile is required");
  }
  let connectSources;
  if (transport.profile === "trusted_proxy") {
    const origin = new URL(transport.publicOrigin);
    connectSources = ["'self'", `wss://${origin.host}`];
  } else {
    const port = signalingPort === null ? "*" : String(validPort(signalingPort, "signalingPort"));
    connectSources = [
      "'self'",
      `ws://127.0.0.1:${port}`,
      `ws://localhost:${port}`,
      `ws://[::1]:${port}`,
      `wss://127.0.0.1:${port}`,
      `wss://localhost:${port}`,
      `wss://[::1]:${port}`,
    ];
  }
  const directives = [
    "default-src 'self'",
    "base-uri 'none'",
    "object-src 'none'",
    "frame-ancestors 'self'",
    "frame-src 'self'",
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline'",
    "font-src 'self' data:",
    "img-src 'self' blob: data:",
    "media-src 'self' blob:",
    "worker-src 'self' blob:",
    `connect-src ${connectSources.join(" ")}`,
  ];
  if (transport.profile === "trusted_proxy") directives.push("upgrade-insecure-requests");
  return directives.join("; ");
}

function secretValue(secrets, name, minimumBytes, { textual = false } = {}) {
  if (!secrets || typeof secrets !== "object" || Array.isArray(secrets)) {
    fail("PIXEL_STREAMING_SECRET_REQUIRED", "Required streaming secret is not available");
  }
  const raw = secrets[name];
  if (textual && typeof raw !== "string") {
    fail("PIXEL_STREAMING_SECRET_INVALID", "Required streaming secret does not meet policy");
  }
  const buffer = Buffer.isBuffer(raw) ? raw : Buffer.from(typeof raw === "string" ? raw : "");
  if (buffer.length < minimumBytes || buffer.length > 4096 || buffer.includes(0) ||
      (textual && /[\u0000-\u001f\u007f]/.test(raw))) {
    fail("PIXEL_STREAMING_SECRET_INVALID", "Required streaming secret does not meet policy");
  }
  const asString = buffer.toString("utf8");
  if (PLACEHOLDER_SECRET_PATTERN.test(asString)) {
    fail("PIXEL_STREAMING_SECRET_INVALID", "Placeholder streaming credentials are forbidden");
  }
  return { buffer, string: asString };
}

function endpointBinding(value, field) {
  if ((typeof value !== "string" && typeof value !== "number") ||
      String(value).length === 0 || String(value).length > 256 ||
      /[\u0000-\u001f\u007f]/.test(String(value))) {
    fail("PIXEL_STREAMING_ENDPOINT_BINDING_INVALID", `${field} is required for endpoint binding`);
  }
  return String(value);
}

function endpointDigest({ sessionId, slotId, leaseId }, secrets) {
  const secret = secretValue(secrets, "endpointHmacKey", 32).buffer;
  const payload = JSON.stringify([
    "simworld-pixel-streaming-endpoint/v1",
    endpointBinding(sessionId, "sessionId"),
    endpointBinding(slotId, "slotId"),
    endpointBinding(leaseId, "leaseId"),
  ]);
  return crypto.createHmac("sha256", secret).update(payload).digest("base64url");
}

function createOpaqueStreamingEndpoint(bindings, secrets) {
  assertKnownKeys(
    bindings,
    new Set(["sessionId", "slotId", "leaseId", "pathPrefix"]),
    "endpoint bindings",
  );
  const endpointId = `ps1_${endpointDigest(bindings, secrets)}`;
  const pathPrefix = normalizePathPrefix(bindings.pathPrefix || DEFAULT_STREAMING_PATH_PREFIX);
  return Object.freeze({ endpointId, path: `${pathPrefix}/${endpointId}` });
}

function verifyOpaqueStreamingEndpoint(endpointId, bindings, secrets) {
  if (typeof endpointId !== "string" || !ENDPOINT_ID_PATTERN.test(endpointId)) return false;
  const expected = `ps1_${endpointDigest(bindings, secrets)}`;
  const left = Buffer.from(endpointId);
  const right = Buffer.from(expected);
  return left.length === right.length && crypto.timingSafeEqual(left, right);
}

function normalizeUrlList(value, kind) {
  if (!Array.isArray(value) || value.length === 0 || value.length > 16) {
    fail("PIXEL_STREAMING_ICE_INVALID", `${kind} URLs must be a non-empty bounded array`);
  }
  const protocols = kind === "STUN" ? new Set(["stun", "stuns"]) : new Set(["turn", "turns"]);
  const normalized = [];
  for (const item of value) {
    if (typeof item !== "string" || item.length === 0 || item.length > 2048 ||
        item.trim() !== item || /[\s@#]/.test(item)) {
      fail("PIXEL_STREAMING_ICE_INVALID", `${kind} URL is invalid`);
    }
    const match = item.match(/^([a-z]+):([^?]+)(?:\?([^#]+))?$/i);
    if (!match || !protocols.has(match[1].toLowerCase()) || !validIceAuthority(match[2])) {
      fail("PIXEL_STREAMING_ICE_INVALID", `${kind} URL is invalid`);
    }
    if (kind === "STUN" && match[3]) {
      fail("PIXEL_STREAMING_ICE_INVALID", "STUN URLs may not contain query parameters");
    }
    if (kind === "TURN" && match[3] && !/^transport=(?:udp|tcp)$/i.test(match[3])) {
      fail("PIXEL_STREAMING_ICE_INVALID", "TURN URL has an unsupported transport parameter");
    }
    const canonical = `${match[1].toLowerCase()}:${match[2].toLowerCase()}${match[3] ? `?${match[3].toLowerCase()}` : ""}`;
    if (!normalized.includes(canonical)) normalized.push(canonical);
  }
  return normalized.sort();
}

function validIceAuthority(value) {
  if (typeof value !== "string" || value.length === 0 || value.length > 320 || value.includes("/")) {
    return false;
  }
  let host;
  let port = "";
  if (value.startsWith("[")) {
    const match = value.match(/^\[([^\]]+)\](?::([0-9]{1,5}))?$/);
    if (!match || net.isIP(match[1]) !== 6) return false;
    [, host, port = ""] = match;
  } else {
    const match = value.match(/^([^:]+)(?::([0-9]{1,5}))?$/);
    if (!match) return false;
    [, host, port = ""] = match;
    const hostname = /^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$/;
    if (net.isIP(host) !== 4 && !hostname.test(host)) return false;
  }
  if (port && (Number(port) < 1 || Number(port) > 65535)) return false;
  return Boolean(host);
}

function buildPeerConnectionOptions(ice, secrets) {
  assertKnownKeys(
    ice,
    new Set(["stunUrls", "turnUrls", "turnUsername", "transportPolicy"]),
    "ICE configuration",
  );
  const stunUrls = normalizeUrlList(ice.stunUrls, "STUN");
  const turnUrls = normalizeUrlList(ice.turnUrls, "TURN");
  const turnUsername = nonEmptyString(ice.turnUsername, "turnUsername", 256);
  const turnCredential = secretValue(secrets, "turnCredential", 16, { textual: true }).string;
  const transportPolicy = String(ice.transportPolicy || "all").trim().toLowerCase();
  if (!ICE_TRANSPORT_POLICIES.has(transportPolicy)) {
    fail("PIXEL_STREAMING_ICE_INVALID", "ICE transport policy must be all or relay");
  }
  return {
    iceServers: [
      { urls: stunUrls },
      {
        urls: turnUrls,
        username: turnUsername,
        credential: turnCredential,
        credentialType: "password",
      },
    ],
    iceTransportPolicy: transportPolicy,
  };
}

function buildCirrusConfig(options, secrets) {
  assertKnownKeys(
    options,
    new Set(["transportProfile", "httpPort", "streamerPort", "sfuPort", "useFrontend", "ice"]),
    "Cirrus configuration",
  );
  const transportProfile = String(options.transportProfile || "loopback").trim().toLowerCase();
  if (!TRANSPORT_PROFILES.has(transportProfile)) {
    fail("PIXEL_STREAMING_TRANSPORT_PROFILE_INVALID", "Cirrus transport profile is invalid");
  }
  const httpPort = validPort(options.httpPort, "HttpPort");
  const streamerPort = validPort(options.streamerPort, "StreamerPort");
  const sfuPort = options.sfuPort === undefined || options.sfuPort === null
    ? null
    : validPort(options.sfuPort, "SFUPort");
  const ports = [httpPort, streamerPort, ...(sfuPort === null ? [] : [sfuPort])];
  if (new Set(ports).size !== ports.length) {
    fail("PIXEL_STREAMING_PORT_COLLISION", "Cirrus HttpPort, StreamerPort, and SFUPort must be distinct");
  }
  if (options.useFrontend !== undefined && typeof options.useFrontend !== "boolean") {
    fail("PIXEL_STREAMING_CONFIG_INVALID", "useFrontend must be a boolean");
  }

  const config = {
    UseFrontend: options.useFrontend === undefined
      ? transportProfile === "loopback"
      : options.useFrontend,
    UseMatchmaker: false,
    BindAddress: "127.0.0.1",
    HttpPort: httpPort,
    StreamerPort: streamerPort,
  };
  if (sfuPort !== null) config.SFUPort = sfuPort;
  // The pinned UE 5.3 Cirrus parser expects this field as serialized JSON.
  config.peerConnectionOptions = JSON.stringify(buildPeerConnectionOptions(options.ice, secrets));
  return config;
}

function redactCirrusConfig(config) {
  if (!config || typeof config !== "object" || Array.isArray(config)) return null;
  const redacted = {};
  for (const key of [
    "UseFrontend",
    "UseMatchmaker",
    "BindAddress",
    "HttpPort",
    "StreamerPort",
    "SFUPort",
  ]) {
    if (Object.prototype.hasOwnProperty.call(config, key)) redacted[key] = config[key];
  }
  try {
    const peer = typeof config.peerConnectionOptions === "string"
      ? JSON.parse(config.peerConnectionOptions)
      : config.peerConnectionOptions;
    const safePeer = {
      iceServers: Array.isArray(peer && peer.iceServers)
        ? peer.iceServers.map((server) => {
          if (!server || typeof server !== "object") return server;
          const safeServer = {};
          for (const key of ["urls", "username", "credentialType"]) {
            if (Object.prototype.hasOwnProperty.call(server, key)) safeServer[key] = server[key];
          }
          if (Object.prototype.hasOwnProperty.call(server, "credential")) {
            safeServer.credential = "[REDACTED]";
          }
          return safeServer;
        })
        : [],
    };
    if (peer && Object.prototype.hasOwnProperty.call(peer, "iceTransportPolicy")) {
      safePeer.iceTransportPolicy = peer.iceTransportPolicy;
    }
    redacted.peerConnectionOptions = JSON.stringify(safePeer);
  } catch {
    redacted.peerConnectionOptions = "[INVALID REDACTED]";
  }
  return redacted;
}

module.exports = {
  DEFAULT_STREAMING_PATH_PREFIX,
  PixelStreamingConfigError,
  assertTransportRequest,
  buildCirrusConfig,
  buildPeerConnectionOptions,
  buildPixelStreamingCsp,
  createOpaqueStreamingEndpoint,
  normalizePathPrefix,
  redactCirrusConfig,
  resolveTransportProfile,
  verifyOpaqueStreamingEndpoint,
};

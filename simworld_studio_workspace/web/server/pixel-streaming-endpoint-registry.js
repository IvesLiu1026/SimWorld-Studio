"use strict";

const crypto = require("node:crypto");
const {
  DEFAULT_STREAMING_PATH_PREFIX,
  createOpaqueStreamingEndpoint,
  normalizePathPrefix,
} = require("./pixel-streaming-config");

const DEFAULT_TTL_MS = 5 * 60 * 1000;
const DEFAULT_MAX_ENTRIES = 256;
const MAX_TTL_MS = 24 * 60 * 60 * 1000;
const MAX_ENTRIES = 10000;
const NONCE_PATTERN = /^[A-Za-z0-9_-]{16,128}$/;

class PixelStreamingEndpointRegistryError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "PixelStreamingEndpointRegistryError";
    this.code = code;
  }
}

function fail(code, message) {
  throw new PixelStreamingEndpointRegistryError(code, message);
}

function assertKnownKeys(value, allowed, field) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    fail("PIXEL_STREAMING_ENDPOINT_INPUT_INVALID", `${field} must be an object`);
  }
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) {
      fail("PIXEL_STREAMING_ENDPOINT_INPUT_INVALID", `${field} contains an unsupported field`);
    }
  }
}

function boundedString(value, field) {
  if (typeof value !== "string" || value.length === 0 || value.length > 256 ||
      value.trim() !== value || /[\u0000-\u001f\u007f]/.test(value)) {
    fail("PIXEL_STREAMING_ENDPOINT_INPUT_INVALID", `${field} must be a non-empty bounded string`);
  }
  return value;
}

function slotIdValue(value) {
  if (!Number.isSafeInteger(value) || value < 0 || value > 65535) {
    fail("PIXEL_STREAMING_ENDPOINT_INPUT_INVALID", "slotId must be a bounded non-negative integer");
  }
  return value;
}

function portValue(value) {
  if (!Number.isSafeInteger(value) || value < 1 || value > 65535) {
    fail("PIXEL_STREAMING_ENDPOINT_INPUT_INVALID", "cirrusHttpPort must be a valid TCP port");
  }
  return value;
}

function positiveInteger(value, maximum, field) {
  if (!Number.isSafeInteger(value) || value < 1 || value > maximum) {
    fail("PIXEL_STREAMING_ENDPOINT_CONFIG_INVALID", `${field} is outside the supported range`);
  }
  return value;
}

function validateClock(clock) {
  if (typeof clock !== "function") {
    fail("PIXEL_STREAMING_ENDPOINT_CONFIG_INVALID", "clock must be a function");
  }
  return clock;
}

function validateNonceFactory(nonceFactory) {
  if (typeof nonceFactory !== "function") {
    fail("PIXEL_STREAMING_ENDPOINT_CONFIG_INVALID", "nonceFactory must be a function");
  }
  return nonceFactory;
}

function validateProxyContext(context) {
  if ((typeof context !== "object" || context === null) && typeof context !== "function") {
    fail(
      "PIXEL_STREAMING_ENDPOINT_CONFIG_INVALID",
      "trustedProxyContext must be an in-process object capability",
    );
  }
  return context;
}

function copyAndValidateEndpointSecrets(endpointSecrets, pathPrefix) {
  if (!endpointSecrets || typeof endpointSecrets !== "object" || Array.isArray(endpointSecrets)) {
    fail("PIXEL_STREAMING_ENDPOINT_SECRET_INVALID", "Endpoint signing secret is unavailable");
  }
  const raw = endpointSecrets.endpointHmacKey;
  const copied = Buffer.isBuffer(raw) ? Buffer.from(raw) : typeof raw === "string" ? raw : "";
  try {
    createOpaqueStreamingEndpoint({
      sessionId: "registry-preflight-session",
      slotId: 0,
      leaseId: "registry-preflight-lease",
      pathPrefix,
    }, { endpointHmacKey: copied });
  } catch {
    fail("PIXEL_STREAMING_ENDPOINT_SECRET_INVALID", "Endpoint signing secret does not meet policy");
  }
  return Object.freeze({ endpointHmacKey: copied });
}

function bindingFromInput(input, { includePort }) {
  const allowed = new Set(["ownerId", "sessionId", "slotId", "leaseId"]);
  if (includePort) allowed.add("cirrusHttpPort");
  assertKnownKeys(input, allowed, includePort ? "endpoint acquisition" : "endpoint authorization");
  const binding = {
    ownerId: boundedString(input.ownerId, "ownerId"),
    sessionId: boundedString(input.sessionId, "sessionId"),
    slotId: slotIdValue(input.slotId),
    leaseId: boundedString(input.leaseId, "leaseId"),
  };
  if (includePort) binding.cirrusHttpPort = portValue(input.cirrusHttpPort);
  return binding;
}

function sessionKey(binding) {
  return JSON.stringify([binding.ownerId, binding.sessionId]);
}

function sameBinding(record, binding) {
  return record.ownerId === binding.ownerId &&
    record.sessionId === binding.sessionId &&
    record.slotId === binding.slotId &&
    record.leaseId === binding.leaseId;
}

function publicView(record) {
  return Object.freeze({ path: record.path, expiresAt: record.expiresAt });
}

function revokedView() {
  return Object.freeze({ revoked: true });
}

class PixelStreamingEndpointRegistry {
  #byPath = new Map();
  #pathBySession = new Map();
  #clock;
  #endpointSecrets;
  #lastNow = -1;
  #maxEntries;
  #nonceFactory;
  #pathPrefix;
  #trustedProxyContext;
  #ttlMs;

  constructor(options = {}) {
    assertKnownKeys(
      options,
      new Set([
        "endpointSecrets",
        "trustedProxyContext",
        "clock",
        "nonceFactory",
        "ttlMs",
        "maxEntries",
        "pathPrefix",
      ]),
      "registry options",
    );
    try {
      this.#pathPrefix = normalizePathPrefix(options.pathPrefix ?? DEFAULT_STREAMING_PATH_PREFIX);
    } catch {
      fail("PIXEL_STREAMING_ENDPOINT_CONFIG_INVALID", "pathPrefix must be a fixed same-origin path");
    }
    this.#clock = validateClock(options.clock === undefined ? Date.now : options.clock);
    this.#nonceFactory = validateNonceFactory(
      options.nonceFactory === undefined
        ? (() => crypto.randomBytes(18).toString("base64url"))
        : options.nonceFactory,
    );
    this.#ttlMs = positiveInteger(options.ttlMs ?? DEFAULT_TTL_MS, MAX_TTL_MS, "ttlMs");
    this.#maxEntries = positiveInteger(
      options.maxEntries ?? DEFAULT_MAX_ENTRIES,
      MAX_ENTRIES,
      "maxEntries",
    );
    this.#trustedProxyContext = validateProxyContext(options.trustedProxyContext);
    this.#endpointSecrets = copyAndValidateEndpointSecrets(options.endpointSecrets, this.#pathPrefix);
    this.#now();
  }

  get size() {
    this.#pruneExpired(this.#now());
    return this.#byPath.size;
  }

  acquire(input) {
    const binding = bindingFromInput(input, { includePort: true });
    const now = this.#now();
    this.#pruneExpired(now);

    const key = sessionKey(binding);
    const existingPath = this.#pathBySession.get(key);
    if (existingPath) {
      const existing = this.#byPath.get(existingPath);
      if (existing && sameBinding(existing, binding) &&
          existing.cirrusHttpPort === binding.cirrusHttpPort) {
        return publicView(existing);
      }
      fail(
        "PIXEL_STREAMING_ENDPOINT_BINDING_CONFLICT",
        "The authenticated session already has a different streaming binding",
      );
    }
    if (this.#byPath.size >= this.#maxEntries) {
      fail("PIXEL_STREAMING_ENDPOINT_CAPACITY", "Streaming endpoint capacity has been reached");
    }

    const record = this.#createRecord(binding, now);
    this.#insert(record);
    return publicView(record);
  }

  resolve(path, authorization) {
    const binding = bindingFromInput(authorization, { includePort: false });
    const record = this.#authorizedRecord(path, binding, this.#now());
    return publicView(record);
  }

  resolveForProxy(path, authorization, internalContext) {
    if (internalContext !== this.#trustedProxyContext) {
      fail(
        "PIXEL_STREAMING_PROXY_CONTEXT_REQUIRED",
        "A trusted in-process proxy context is required",
      );
    }
    const binding = bindingFromInput(authorization, { includePort: false });
    const record = this.#authorizedRecord(path, binding, this.#now());
    return Object.freeze({
      listener: "cirrus_http",
      hostname: "127.0.0.1",
      port: record.cirrusHttpPort,
      expiresAt: record.expiresAt,
    });
  }

  rotate(path, authorization, options = {}) {
    assertKnownKeys(options, new Set(["cirrusHttpPort"]), "endpoint rotation options");
    const binding = bindingFromInput(authorization, { includePort: false });
    const now = this.#now();
    const current = this.#authorizedRecord(path, binding, now);
    const nextBinding = {
      ...binding,
      cirrusHttpPort: options.cirrusHttpPort === undefined
        ? current.cirrusHttpPort
        : portValue(options.cirrusHttpPort),
    };
    const replacement = this.#createRecord(nextBinding, now, current.path);
    this.#delete(current);
    this.#insert(replacement);
    return publicView(replacement);
  }

  revoke(path, authorization) {
    const binding = bindingFromInput(authorization, { includePort: false });
    const record = this.#authorizedRecord(path, binding, this.#now());
    this.#delete(record);
    return revokedView();
  }

  sweepExpired() {
    return this.#pruneExpired(this.#now());
  }

  #authorizedRecord(path, binding, now) {
    this.#pruneExpired(now);
    if (typeof path !== "string" || path.length < 1 || path.length > 256 ||
        !path.startsWith(`${this.#pathPrefix}/`) || path.includes("?") || path.includes("#") ||
        path.includes("\\") || path.startsWith("//")) {
      fail("PIXEL_STREAMING_ENDPOINT_UNAVAILABLE", "Streaming endpoint is unavailable");
    }
    const record = this.#byPath.get(path);
    if (!record || !sameBinding(record, binding)) {
      fail("PIXEL_STREAMING_ENDPOINT_UNAVAILABLE", "Streaming endpoint is unavailable");
    }
    return record;
  }

  #createRecord(binding, now, excludedPath = null) {
    for (let attempt = 0; attempt < 4; attempt += 1) {
      let nonce;
      try {
        nonce = this.#nonceFactory();
      } catch {
        fail("PIXEL_STREAMING_ENDPOINT_NONCE_INVALID", "Endpoint nonce source failed policy");
      }
      if (typeof nonce !== "string" || !NONCE_PATTERN.test(nonce)) {
        fail("PIXEL_STREAMING_ENDPOINT_NONCE_INVALID", "Endpoint nonce source failed policy");
      }
      let endpoint;
      try {
        endpoint = createOpaqueStreamingEndpoint({
          sessionId: JSON.stringify([binding.ownerId, binding.sessionId]),
          slotId: binding.slotId,
          leaseId: JSON.stringify([binding.leaseId, nonce]),
          pathPrefix: this.#pathPrefix,
        }, this.#endpointSecrets);
      } catch {
        fail("PIXEL_STREAMING_ENDPOINT_SECRET_INVALID", "Endpoint signing operation failed");
      }
      if (endpoint.path !== excludedPath && !this.#byPath.has(endpoint.path)) {
        const expiresAt = now + this.#ttlMs;
        if (!Number.isSafeInteger(expiresAt)) {
          fail("PIXEL_STREAMING_ENDPOINT_CLOCK_INVALID", "Endpoint expiry is outside the safe range");
        }
        return Object.freeze({
          path: endpoint.path,
          ownerId: binding.ownerId,
          sessionId: binding.sessionId,
          slotId: binding.slotId,
          leaseId: binding.leaseId,
          cirrusHttpPort: binding.cirrusHttpPort,
          issuedAt: now,
          expiresAt,
        });
      }
    }
    fail("PIXEL_STREAMING_ENDPOINT_COLLISION", "Unable to allocate a unique streaming endpoint");
  }

  #insert(record) {
    this.#byPath.set(record.path, record);
    this.#pathBySession.set(sessionKey(record), record.path);
  }

  #delete(record) {
    this.#byPath.delete(record.path);
    const key = sessionKey(record);
    if (this.#pathBySession.get(key) === record.path) this.#pathBySession.delete(key);
  }

  #pruneExpired(now) {
    let removed = 0;
    for (const record of this.#byPath.values()) {
      if (now >= record.expiresAt) {
        this.#delete(record);
        removed += 1;
      }
    }
    return removed;
  }

  #now() {
    let value;
    try {
      value = this.#clock();
    } catch {
      fail("PIXEL_STREAMING_ENDPOINT_CLOCK_INVALID", "clock failed to return a valid value");
    }
    if (!Number.isSafeInteger(value) || value < 0) {
      fail("PIXEL_STREAMING_ENDPOINT_CLOCK_INVALID", "clock must return a non-negative safe integer");
    }
    if (value < this.#lastNow) {
      fail("PIXEL_STREAMING_ENDPOINT_CLOCK_INVALID", "clock must not move backwards");
    }
    this.#lastNow = value;
    return value;
  }
}

function createPixelStreamingEndpointRegistry(options) {
  return new PixelStreamingEndpointRegistry(options);
}

module.exports = {
  DEFAULT_MAX_ENTRIES,
  DEFAULT_TTL_MS,
  PixelStreamingEndpointRegistry,
  PixelStreamingEndpointRegistryError,
  createPixelStreamingEndpointRegistry,
};

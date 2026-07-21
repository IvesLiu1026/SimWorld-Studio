"use strict";

const crypto = require("node:crypto");

const CAPABILITY_PATTERN = /^[A-Za-z0-9_-]{43}$/;
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/;
const CHANNELS = new Set(["ue", "ucv"]);

class InternalRunCapabilityError extends Error {
  constructor(code, message, statusCode = 401) {
    super(message);
    this.name = "InternalRunCapabilityError";
    this.code = code;
    this.statusCode = statusCode;
  }
}

function fail(code, message, statusCode) {
  throw new InternalRunCapabilityError(code, message, statusCode);
}

function normalizeDuration(value, fallback, field, maximum) {
  const duration = value === undefined ? fallback : Number(value);
  if (!Number.isSafeInteger(duration) || duration < 1 || duration > maximum) {
    throw new TypeError(`${field} must be a positive integer no greater than ${maximum}`);
  }
  return duration;
}

function normalizeIdentity(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const identity = {
    ownerId: String(value.ownerId || ""),
    sessionId: String(value.sessionId || ""),
    slotId: Number(value.slotId),
    leaseId: String(value.leaseId || ""),
    mcpPort: Number(value.mcpPort),
  };
  if (!SAFE_ID.test(identity.ownerId) || !SAFE_ID.test(identity.sessionId)
      || !SAFE_ID.test(identity.leaseId)
      || !Number.isSafeInteger(identity.slotId) || identity.slotId < 0 || identity.slotId > 1023
      || !Number.isSafeInteger(identity.mcpPort) || identity.mcpPort < 1 || identity.mcpPort > 65535) {
    return null;
  }
  return Object.freeze(identity);
}

function normalizeId(value, field) {
  const id = String(value || "").trim();
  if (!SAFE_ID.test(id)) throw new TypeError(`${field} is invalid`);
  return id;
}

function capabilityDigest(capability) {
  return crypto.createHash("sha256").update("simworld/internal-run/v1\0").update(capability).digest("hex");
}

function capabilityHeader(request) {
  const headers = request && request.headers || {};
  return String(headers["x-simworld-run-capability"] || "").trim();
}

function runIdHeader(request) {
  const headers = request && request.headers || {};
  return String(headers["x-simworld-run-id"] || "").trim();
}

function isInternalCapabilityCandidate(request) {
  const method = String(request && request.method || "").toUpperCase();
  const requestPath = String(request && (request.path || request.url) || "").split("?", 1)[0].replace(/\/+$/, "");
  return method === "POST"
    && (requestPath === "/api/internal/ue" || requestPath === "/api/internal/ucv")
    && CAPABILITY_PATTERN.test(capabilityHeader(request))
    && SAFE_ID.test(runIdHeader(request));
}

function createInternalRunCapabilityRegistry({
  now = Date.now,
  randomBytes = crypto.randomBytes,
  setTimeoutFn = setTimeout,
  clearTimeoutFn = clearTimeout,
  idleTtlMs,
  hardTtlMs,
  maxEntries = 1024,
  isActiveSessionBinding,
  resolveUeBroker,
  resolveUcvBroker,
  operationPolicy = () => ({ allowed: true }),
} = {}) {
  if (typeof now !== "function" || typeof randomBytes !== "function"
      || typeof setTimeoutFn !== "function" || typeof clearTimeoutFn !== "function") {
    throw new TypeError("clock, random, and timer dependencies must be functions");
  }
  if (typeof isActiveSessionBinding !== "function"
      || typeof resolveUeBroker !== "function" || typeof resolveUcvBroker !== "function") {
    throw new TypeError("active lease and broker resolvers are required");
  }
  if (typeof operationPolicy !== "function") throw new TypeError("operationPolicy must be a function");
  const idleTtl = normalizeDuration(idleTtlMs, 2 * 60 * 1000, "idleTtlMs", 30 * 60 * 1000);
  const hardTtl = normalizeDuration(hardTtlMs, 30 * 60 * 1000, "hardTtlMs", 60 * 60 * 1000);
  if (hardTtl < idleTtl) throw new TypeError("hardTtlMs must be greater than or equal to idleTtlMs");
  if (!Number.isSafeInteger(maxEntries) || maxEntries < 1 || maxEntries > 10000) {
    throw new TypeError("maxEntries must be an integer between 1 and 10000");
  }

  const records = new Map();
  const digestsByScope = new Map();

  function readNow() {
    const value = Number(now());
    if (!Number.isFinite(value) || value < 0) throw new TypeError("now() must return a finite timestamp");
    return value;
  }

  function removeDigest(digest, reason = "revoked") {
    const record = records.get(digest);
    if (!record) return false;
    records.delete(digest);
    if (record.expiryTimer) {
      clearTimeoutFn(record.expiryTimer);
      record.expiryTimer = null;
    }
    const scoped = digestsByScope.get(record.scopeId);
    if (scoped) {
      scoped.delete(digest);
      if (scoped.size === 0) digestsByScope.delete(record.scopeId);
    }
    if (!record.cleaned) {
      record.cleaned = true;
      try { if (record.cleanup) record.cleanup(reason); } catch (_error) {}
    }
    return true;
  }

  function scheduleExpiry(record) {
    if (record.expiryTimer) clearTimeoutFn(record.expiryTimer);
    const delay = Math.max(1, Math.min(record.idleExpiresAt, record.hardExpiresAt) - readNow());
    record.expiryTimer = setTimeoutFn(() => {
      record.expiryTimer = null;
      const at = readNow();
      if (at >= record.idleExpiresAt || at >= record.hardExpiresAt) {
        removeDigest(record.digest, "expired");
      } else {
        scheduleExpiry(record);
      }
    }, delay);
    if (record.expiryTimer && typeof record.expiryTimer.unref === "function") record.expiryTimer.unref();
  }

  function sweep(at = readNow()) {
    for (const [digest, record] of records) {
      if (at >= record.idleExpiresAt || at >= record.hardExpiresAt) removeDigest(digest, "expired");
    }
  }

  function issue({ identity, runId, scopeId, cleanup } = {}) {
    const activeLease = normalizeIdentity(identity);
    const safeRunId = normalizeId(runId, "runId");
    const safeScopeId = normalizeId(scopeId, "scopeId");
    if (!activeLease) throw new TypeError("identity is invalid");
    if (cleanup !== undefined && typeof cleanup !== "function") throw new TypeError("cleanup must be a function");
    sweep();
    let active = false;
    try { active = isActiveSessionBinding(activeLease) === true; } catch (_error) { active = false; }
    if (!active) fail("INTERNAL_RUN_LEASE_INVALID", "The Studio run lease is not active.", 409);
    const ueBroker = resolveUeBroker(activeLease);
    const ucvBroker = resolveUcvBroker(activeLease);
    if (!ueBroker || typeof ueBroker.send !== "function" || Number(ueBroker.port) !== activeLease.mcpPort
        || !ucvBroker || typeof ucvBroker.send !== "function") {
      fail("INTERNAL_RUN_BROKER_INVALID", "The Studio run has no exact lease broker.", 409);
    }
    const bytes = randomBytes(32);
    if (!Buffer.isBuffer(bytes) || bytes.length !== 32) throw new TypeError("randomBytes must return 32 bytes");
    const capability = bytes.toString("base64url");
    if (!CAPABILITY_PATTERN.test(capability)) throw new TypeError("generated capability is invalid");
    const digest = capabilityDigest(capability);
    if (records.has(digest)) throw new TypeError("generated capability collided");
    const issuedAt = readNow();
    const record = {
      digest,
      identity: activeLease,
      runId: safeRunId,
      scopeId: safeScopeId,
      issuedAt,
      idleExpiresAt: issuedAt + idleTtl,
      hardExpiresAt: issuedAt + hardTtl,
      ueBroker,
      ucvBroker,
      cleanup,
      cleaned: false,
      expiryTimer: null,
    };
    records.set(digest, record);
    if (!digestsByScope.has(safeScopeId)) digestsByScope.set(safeScopeId, new Set());
    digestsByScope.get(safeScopeId).add(digest);
    scheduleExpiry(record);
    while (records.size > maxEntries) removeDigest(records.keys().next().value, "capacity");
    return Object.freeze({
      capability,
      runId: safeRunId,
      scopeId: safeScopeId,
      idleExpiresAt: record.idleExpiresAt,
      hardExpiresAt: record.hardExpiresAt,
    });
  }

  function authorize({ capability, runId, channel, body } = {}) {
    const candidate = String(capability || "").trim();
    const safeRunId = String(runId || "").trim();
    if (!CAPABILITY_PATTERN.test(candidate) || !SAFE_ID.test(safeRunId) || !CHANNELS.has(channel)) {
      fail("INTERNAL_RUN_CAPABILITY_INVALID", "Internal run capability is invalid.", 401);
    }
    const at = readNow();
    sweep(at);
    const digest = capabilityDigest(candidate);
    const record = records.get(digest);
    if (!record || record.runId !== safeRunId) {
      fail("INTERNAL_RUN_CAPABILITY_INVALID", "Internal run capability is invalid.", 401);
    }
    let active = false;
    try { active = isActiveSessionBinding(record.identity) === true; } catch (_error) { active = false; }
    if (!active) {
      removeDigest(digest, "lease_revoked");
      fail("INTERNAL_RUN_LEASE_INVALID", "The Studio run lease is no longer active.", 409);
    }
    const resolver = channel === "ue" ? resolveUeBroker : resolveUcvBroker;
    const selected = channel === "ue" ? record.ueBroker : record.ucvBroker;
    const current = resolver(record.identity);
    if (!current || current !== selected || typeof current.send !== "function"
        || (channel === "ue" && Number(current.port) !== record.identity.mcpPort)) {
      removeDigest(digest, "broker_drift");
      fail("INTERNAL_RUN_BROKER_INVALID", "The Studio run broker is no longer valid.", 409);
    }
    const decision = operationPolicy({ channel, body, identity: record.identity, runId: record.runId });
    if (!decision || decision.allowed !== true) {
      fail(
        decision && decision.code || "INTERNAL_RUN_OPERATION_DENIED",
        decision && decision.message || "This internal run operation is not allowed.",
        decision && decision.statusCode || 403,
      );
    }
    record.idleExpiresAt = Math.min(record.hardExpiresAt, at + idleTtl);
    scheduleExpiry(record);
    return Object.freeze({
      broker: current,
      identity: record.identity,
      runId: record.runId,
      scopeId: record.scopeId,
      idleExpiresAt: record.idleExpiresAt,
      hardExpiresAt: record.hardExpiresAt,
    });
  }

  function revoke(capability, reason = "revoked") {
    const candidate = String(capability || "").trim();
    return CAPABILITY_PATTERN.test(candidate) && removeDigest(capabilityDigest(candidate), reason);
  }

  function revokeScope({ scopeId, runId } = {}, reason = "scope_revoked") {
    const safeScopeId = String(scopeId || "").trim();
    const safeRunId = runId == null || runId === "" ? null : String(runId).trim();
    if (!SAFE_ID.test(safeScopeId) || (safeRunId !== null && !SAFE_ID.test(safeRunId))) return 0;
    const scoped = [...(digestsByScope.get(safeScopeId) || [])];
    let revoked = 0;
    for (const digest of scoped) {
      const record = records.get(digest);
      if (record && (safeRunId === null || record.runId === safeRunId) && removeDigest(digest, reason)) revoked += 1;
    }
    return revoked;
  }

  function attachProcess(capability, child) {
    const candidate = String(capability || "").trim();
    const digest = CAPABILITY_PATTERN.test(candidate) ? capabilityDigest(candidate) : "";
    if (!records.has(digest)) throw new TypeError("capability is not active");
    if (!child || typeof child.once !== "function") throw new TypeError("child process must support once()");
    if (child.exitCode !== undefined && child.exitCode !== null) {
      removeDigest(digest, "process_exit");
      return;
    }
    child.once("exit", () => removeDigest(digest, "process_exit"));
    child.once("error", () => removeDigest(digest, "process_error"));
  }

  return Object.freeze({
    issue,
    authorize,
    revoke,
    revokeScope,
    attachProcess,
    sweep,
    get size() { return records.size; },
  });
}

module.exports = {
  CAPABILITY_PATTERN,
  InternalRunCapabilityError,
  capabilityHeader,
  createInternalRunCapabilityRegistry,
  isInternalCapabilityCandidate,
  runIdHeader,
};

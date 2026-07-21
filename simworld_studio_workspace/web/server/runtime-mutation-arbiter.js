"use strict";

const crypto = require("node:crypto");

const SAFE_KIND_RE = /^[a-z][a-z0-9_-]{0,63}$/;

class RuntimeMutationArbiterError extends Error {
  constructor(code, message, { statusCode = 409, retryable = false } = {}) {
    super(message);
    this.name = "RuntimeMutationArbiterError";
    this.code = code;
    this.statusCode = statusCode;
    this.status = statusCode;
    this.retryable = retryable;
  }
}

function positivePort(value) {
  const port = Number(value);
  return Number.isSafeInteger(port) && port >= 1 && port <= 65535 ? port : null;
}

function nonnegativeSlot(value) {
  const slot = Number(value);
  return Number.isSafeInteger(slot) && slot >= 0 && slot <= 1_000_000 ? slot : null;
}

function runtimeKey(identity = {}) {
  if (!identity || typeof identity !== "object" || Array.isArray(identity)) {
    throw new TypeError("runtime mutation identity must be an object");
  }
  const port = positivePort(identity.mcpPort ?? identity.mcp_port ?? identity.port);
  if (port !== null) return `mcp:${port}`;
  const slot = nonnegativeSlot(identity.slotId ?? identity.slot_id);
  if (slot !== null) return `slot:${slot}`;
  if (identity.loopback === true) return "loopback:default";
  throw new TypeError("runtime mutation identity requires mcpPort, slotId, or loopback=true");
}

function safeKind(value) {
  const kind = String(value || "").trim().toLowerCase();
  if (!SAFE_KIND_RE.test(kind)) throw new TypeError("runtime mutation kind is invalid");
  return kind;
}

function createRuntimeMutationArbiter({ randomUUID = () => crypto.randomUUID() } = {}) {
  const active = new Map();
  const epochs = new Map();

  function currentEpoch(identity) {
    return epochs.get(runtimeKey(identity)) || 0;
  }

  function acquire(identity, { kind = "runtime_mutation", operationId = null } = {}) {
    const key = runtimeKey(identity);
    const normalizedKind = safeKind(kind);
    const existing = active.get(key);
    if (existing) {
      throw new RuntimeMutationArbiterError(
        "RUNTIME_MUTATION_SLOT_BUSY",
        "This Unreal runtime already has an active mutation.",
        { statusCode: 409, retryable: true },
      );
    }
    const id = String(randomUUID() || "").trim();
    if (!id || id.length > 200) throw new TypeError("runtime mutation token source is invalid");
    const state = {
      id,
      key,
      kind: normalizedKind,
      operationId: operationId === null ? null : String(operationId),
      epoch: epochs.get(key) || 0,
      invalidated: false,
      released: false,
    };
    active.set(key, state);

    const token = Object.freeze({
      id,
      key,
      kind: normalizedKind,
      get epoch() { return state.epoch; },
      get invalidated() { return state.invalidated; },
      get released() { return state.released; },
      invalidateScene() {
        if (state.released || active.get(key) !== state) {
          throw new RuntimeMutationArbiterError(
            "RUNTIME_MUTATION_TOKEN_STALE",
            "The Unreal runtime mutation token is no longer active.",
            { statusCode: 409 },
          );
        }
        if (!state.invalidated) {
          state.epoch = (epochs.get(key) || 0) + 1;
          epochs.set(key, state.epoch);
          state.invalidated = true;
        }
        return state.epoch;
      },
      release() {
        if (state.released) return false;
        if (active.get(key) !== state) {
          throw new RuntimeMutationArbiterError(
            "RUNTIME_MUTATION_TOKEN_STALE",
            "The Unreal runtime mutation token does not own this runtime.",
            { statusCode: 409 },
          );
        }
        active.delete(key);
        state.released = true;
        return true;
      },
    });
    state.token = token;
    return token;
  }

  function isHeld(token) {
    return Boolean(token && typeof token === "object" && !token.released
      && active.get(token.key) && active.get(token.key).token === token);
  }

  return Object.freeze({
    acquire,
    currentEpoch,
    isHeld,
    get activeCount() { return active.size; },
  });
}

module.exports = {
  RuntimeMutationArbiterError,
  createRuntimeMutationArbiter,
  runtimeKey,
};

"use strict";

const TOOL_NAME = "vista_world_action";
const OPERATIONS = new Set(["interaction", "npc_queue", "event"]);

function createVistaWorldUeAdapter({ resolveUeBroker, timeoutMs = 15_000 } = {}) {
  if (typeof resolveUeBroker !== "function") {
    throw new TypeError("VISTA world UE broker resolver is required");
  }
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1_000 || timeoutMs > 60_000) {
    throw new TypeError("VISTA world UE timeout is invalid");
  }
  return Object.freeze({
    async send(payload, context) {
      if (!payload || typeof payload !== "object" || Array.isArray(payload)
          || !OPERATIONS.has(payload.operation)) {
        throw Object.assign(new Error("Typed VISTA world payload is invalid"), {
          code: "VISTA_WORLD_TRANSPORT_INPUT_INVALID",
        });
      }
      const broker = resolveUeBroker(context);
      if (!broker || typeof broker.send !== "function") {
        throw Object.assign(new Error("Typed VISTA world broker is unavailable"), {
          code: "VISTA_WORLD_TRANSPORT_UNAVAILABLE",
        });
      }
      return broker.send(TOOL_NAME, payload, {
        timeoutMs,
        queueDeadlineMs: timeoutMs * 2,
        ...(context && context.signal instanceof AbortSignal ? { signal: context.signal } : {}),
      });
    },
  });
}

module.exports = {
  OPERATIONS,
  TOOL_NAME,
  createVistaWorldUeAdapter,
};

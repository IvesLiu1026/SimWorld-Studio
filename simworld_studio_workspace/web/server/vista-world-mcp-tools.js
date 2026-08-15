"use strict";

const crypto = require("node:crypto");

const {
  INTERACTION_AFFORDANCES,
  NPC_ACTION_TYPES,
  normalizeInteraction,
  normalizeNpcQueue,
} = require("./vista-world-service");

const DEFAULT_REVISION = "vista_playable_home_r1";
const VISTA_WORLD_TRANSPORT_TIMEOUT_MS = 15_000;
const MAX_GENERATION = 2_147_483_647;
const REVISION_RE = /^[a-z0-9][a-z0-9._-]{0,79}$/;
const EVENT_ID_RE = /^[a-z0-9][a-z0-9._-]{0,79}$/;
const COMMAND_ID_RE = /^vwc-[a-f0-9]{24}$/;
const ACTION_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/;
const SEMANTIC_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._/-]{0,239}$/;
const RESPONSE_CODE_RE = /^[A-Z][A-Z0-9_]{0,79}$/;
const SUCCESS_STATUSES = new Set(["success", "completed", "accepted"]);
const VERIFIED_EVENT_IDS = Object.freeze([
  "mmg_001",
  "mmg_013",
  "mmg_021",
  "mmg_040",
  "mmg_044",
  "mmg_045",
  "mmg_070",
]);
const VERIFIED_EVENT_ID_SET = new Set(VERIFIED_EVENT_IDS);

const TOOL_NAMES = Object.freeze({
  status: "vista_world_status",
  interact: "vista_world_interact",
  npcQueue: "vista_world_npc_queue",
  eventStart: "vista_world_event_start",
  eventReset: "vista_world_event_reset",
});

class VistaWorldMcpError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "VistaWorldMcpError";
    this.code = code;
  }
}

function fail(code, message) {
  throw new VistaWorldMcpError(code, message);
}

function isPlainObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function hasOwn(value, key) {
  return Object.prototype.hasOwnProperty.call(value, key);
}

function exactObject(value, allowed, required, label) {
  if (!isPlainObject(value)) fail("VISTA_WORLD_INPUT_INVALID", `${label} must be an object`);
  const keys = Object.keys(value);
  if (keys.some((key) => !allowed.has(key)) || [...required].some((key) => !hasOwn(value, key))) {
    fail("VISTA_WORLD_INPUT_INVALID", `${label} has an invalid shape`);
  }
  return value;
}

function validGeneration(value) {
  return Number.isSafeInteger(value) && value >= 0 && value <= MAX_GENERATION;
}

function requireGeneration(value) {
  if (!validGeneration(value)) fail("VISTA_WORLD_INPUT_INVALID", "session_generation is invalid");
  return value;
}

function requirePattern(value, pattern, field) {
  if (typeof value !== "string" || !pattern.test(value)) {
    fail("VISTA_WORLD_INPUT_INVALID", `${field} is invalid`);
  }
  return value;
}

function configuredRevision(env = process.env) {
  const revision = String(env.VISTA_WORLD_REVISION || DEFAULT_REVISION).trim();
  if (!REVISION_RE.test(revision)) {
    throw new TypeError("VISTA_WORLD_REVISION is invalid");
  }
  return revision;
}

function requireExpectedRevision(value, revision) {
  requirePattern(value, REVISION_RE, "expected_revision");
  if (value !== revision) {
    fail("VISTA_WORLD_REVISION_STALE", "expected_revision does not match this runtime");
  }
  return value;
}

function normalizeInitialGeneration(env = process.env) {
  const raw = env.VISTA_WORLD_INITIAL_GENERATION;
  if (raw === undefined || raw === "") return 0;
  if (!/^(?:0|[1-9][0-9]{0,9})$/.test(String(raw))) {
    throw new TypeError("VISTA_WORLD_INITIAL_GENERATION is invalid");
  }
  const generation = Number(raw);
  if (!validGeneration(generation)) throw new TypeError("VISTA_WORLD_INITIAL_GENERATION is invalid");
  return generation;
}

function randomCommandId(randomBytes = crypto.randomBytes) {
  const bytes = randomBytes(12);
  if (!Buffer.isBuffer(bytes) || bytes.length !== 12) {
    throw new TypeError("randomBytes must return 12 bytes");
  }
  return `vwc-${bytes.toString("hex")}`;
}

function randomSessionId(randomBytes = crypto.randomBytes) {
  const bytes = randomBytes(12);
  if (!Buffer.isBuffer(bytes) || bytes.length !== 12) {
    throw new TypeError("randomBytes must return 12 bytes");
  }
  return `vws-${bytes.toString("hex")}`;
}

function validLocation(value) {
  return Array.isArray(value) && value.length === 3 && value.every(
    (component) => typeof component === "number" && Number.isFinite(component)
      && Math.abs(component) <= 10_000_000,
  );
}

function validNpcAction(action, seenActionIds) {
  const allowed = new Set([
    "action_id", "type", "target_semantic_id", "target_location_cm",
    "duration_sec", "timeout_sec", "speech",
  ]);
  if (!isPlainObject(action) || !Object.keys(action).every((key) => allowed.has(key))
      || !hasOwn(action, "action_id") || !hasOwn(action, "type")
      || !ACTION_ID_RE.test(String(action.action_id || ""))
      || seenActionIds.has(action.action_id)
      || !NPC_ACTION_TYPES.has(action.type)) {
    return false;
  }
  seenActionIds.add(action.action_id);
  if (action.target_semantic_id !== undefined
      && !SEMANTIC_ID_RE.test(String(action.target_semantic_id || ""))) return false;
  if (action.target_location_cm !== undefined && !validLocation(action.target_location_cm)) return false;
  for (const field of ["duration_sec", "timeout_sec"]) {
    if (action[field] !== undefined && (typeof action[field] !== "number"
        || !Number.isFinite(action[field]) || action[field] < 0 || action[field] > 300)) return false;
  }
  return action.speech === undefined || (
    typeof action.speech === "string" && action.speech.length <= 500
      && !/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(action.speech)
  );
}

function isVistaWorldWirePayloadAllowed(payload, revision = DEFAULT_REVISION) {
  if (!isPlainObject(payload) || !REVISION_RE.test(revision)
      || !COMMAND_ID_RE.test(String(payload.command_id || ""))
      || payload.expected_revision !== revision
      || !validGeneration(payload.session_generation)) return false;

  if (payload.operation === "interaction") {
    const required = new Set([
      "operation", "command_id", "expected_revision", "session_generation",
      "requester_semantic_id", "target_semantic_id", "affordance",
    ]);
    const allowed = new Set([...required, "placement_anchor_semantic_id"]);
    return Object.keys(payload).every((key) => allowed.has(key))
      && [...required].every((key) => hasOwn(payload, key))
      && SEMANTIC_ID_RE.test(String(payload.requester_semantic_id || ""))
      && SEMANTIC_ID_RE.test(String(payload.target_semantic_id || ""))
      && INTERACTION_AFFORDANCES.has(payload.affordance)
      && (payload.placement_anchor_semantic_id === undefined
        || SEMANTIC_ID_RE.test(String(payload.placement_anchor_semantic_id || "")));
  }

  if (payload.operation === "npc_queue") {
    const allowed = new Set([
      "operation", "command_id", "expected_revision", "session_generation",
      "npc_semantic_id", "replace", "actions",
    ]);
    const seen = new Set();
    return Object.keys(payload).every((key) => allowed.has(key))
      && [...allowed].every((key) => hasOwn(payload, key))
      && SEMANTIC_ID_RE.test(String(payload.npc_semantic_id || ""))
      && payload.replace === true
      && Array.isArray(payload.actions) && payload.actions.length >= 1 && payload.actions.length <= 32
      && payload.actions.every((action) => validNpcAction(action, seen));
  }

  if (payload.operation === "event") {
    const required = new Set([
      "operation", "command_id", "expected_revision", "session_generation", "event_operation",
    ]);
    const allowed = new Set([...required, "event_id"]);
    if (!Object.keys(payload).every((key) => allowed.has(key))
        || ![...required].every((key) => hasOwn(payload, key))) return false;
    if (payload.event_operation === "reset_event") return payload.event_id === undefined;
    return payload.event_operation === "start_event"
      && EVENT_ID_RE.test(String(payload.event_id || ""))
      && VERIFIED_EVENT_ID_SET.has(payload.event_id);
  }
  return false;
}

function internalVistaWorldMutationAllowed(body, env = process.env) {
  if (!isPlainObject(body)
      || !Object.keys(body).every((key) => new Set(["type", "params", "timeoutMs"]).has(key))
      || body.type !== "vista_world_action"
      || body.timeoutMs !== VISTA_WORLD_TRANSPORT_TIMEOUT_MS) return false;
  let revision;
  try {
    revision = configuredRevision(env);
  } catch {
    return false;
  }
  return isVistaWorldWirePayloadAllowed(body.params, revision);
}

function normalizeInteractionArguments(args, revision, generation) {
  exactObject(
    args,
    new Set([
      "expected_revision", "session_generation", "requester_semantic_id",
      "target_semantic_id", "affordance", "placement_anchor_semantic_id",
    ]),
    new Set([
      "expected_revision", "session_generation", "requester_semantic_id",
      "target_semantic_id", "affordance",
    ]),
    "VISTA world interaction",
  );
  requireExpectedRevision(args.expected_revision, revision);
  if (requireGeneration(args.session_generation) !== generation) {
    fail("VISTA_WORLD_GENERATION_STALE", "session_generation is stale");
  }
  return normalizeInteraction({
    kind: "interaction",
    generation: args.session_generation,
    requester_semantic_id: args.requester_semantic_id,
    target_semantic_id: args.target_semantic_id,
    affordance: args.affordance,
    ...(args.placement_anchor_semantic_id === undefined ? {} : {
      placement_anchor_semantic_id: args.placement_anchor_semantic_id,
    }),
  }, { revision });
}

function normalizeNpcQueueArguments(args, revision, generation) {
  exactObject(
    args,
    new Set(["expected_revision", "session_generation", "npc_semantic_id", "replace", "actions"]),
    new Set(["expected_revision", "session_generation", "npc_semantic_id", "replace", "actions"]),
    "VISTA world NPC queue",
  );
  requireExpectedRevision(args.expected_revision, revision);
  if (requireGeneration(args.session_generation) !== generation) {
    fail("VISTA_WORLD_GENERATION_STALE", "session_generation is stale");
  }
  const seen = new Set();
  if (!Array.isArray(args.actions) || !args.actions.every((action) => validNpcAction(action, seen))) {
    fail("VISTA_WORLD_INPUT_INVALID", "NPC actions are invalid or contain duplicate action IDs");
  }
  return normalizeNpcQueue({
    kind: "npc_queue",
    generation: args.session_generation,
    npc_semantic_id: args.npc_semantic_id,
    replace: args.replace,
    actions: args.actions,
  }, { revision });
}

function normalizeEventArguments(args, revision, generation, operation, randomBytes) {
  const start = operation === "start_event";
  exactObject(
    args,
    new Set(start
      ? ["expected_revision", "session_generation", "event_id"]
      : ["expected_revision", "session_generation"]),
    new Set(start
      ? ["expected_revision", "session_generation", "event_id"]
      : ["expected_revision", "session_generation"]),
    `VISTA world ${operation}`,
  );
  requireExpectedRevision(args.expected_revision, revision);
  if (requireGeneration(args.session_generation) !== generation) {
    fail("VISTA_WORLD_GENERATION_STALE", "session_generation is stale");
  }
  if (start && (!EVENT_ID_RE.test(String(args.event_id || "")) || !VERIFIED_EVENT_ID_SET.has(args.event_id))) {
    fail("VISTA_WORLD_EVENT_NOT_VERIFIED", "event_id is not a verified Playable Home fixture");
  }
  return {
    operation: "event",
    command_id: randomCommandId(randomBytes),
    expected_revision: revision,
    session_generation: generation,
    event_operation: operation,
    ...(start ? { event_id: args.event_id } : {}),
  };
}

function validateRuntimeResponse(response, payload) {
  const allowed = new Set([
    "command_id", "status", "code", "session_generation", "target_semantic_id", "state",
  ]);
  let serialized;
  try {
    serialized = JSON.stringify(response);
  } catch {
    fail("VISTA_WORLD_PROTOCOL_ERROR", "Typed Unreal world response is invalid");
  }
  if (!isPlainObject(response) || Buffer.byteLength(serialized || "", "utf8") > 64 * 1024
      || !Object.keys(response).every((key) => allowed.has(key))
      || response.command_id !== payload.command_id
      || ![...SUCCESS_STATUSES, "error"].includes(response.status)
      || !RESPONSE_CODE_RE.test(String(response.code || ""))) {
    fail("VISTA_WORLD_PROTOCOL_ERROR", "Typed Unreal world response is invalid");
  }
  if (response.session_generation !== undefined && !validGeneration(response.session_generation)) {
    fail("VISTA_WORLD_PROTOCOL_ERROR", "Typed Unreal world generation is invalid");
  }
  if (response.target_semantic_id !== undefined
      && !SEMANTIC_ID_RE.test(String(response.target_semantic_id || ""))) {
    fail("VISTA_WORLD_PROTOCOL_ERROR", "Typed Unreal world target is invalid");
  }
  if (response.state !== undefined && !isPlainObject(response.state)) {
    fail("VISTA_WORLD_PROTOCOL_ERROR", "Typed Unreal world state is invalid");
  }
  if (SUCCESS_STATUSES.has(response.status)
      && response.session_generation !== payload.session_generation + 1) {
    fail("VISTA_WORLD_PROTOCOL_ERROR", "Typed Unreal world generation did not advance exactly once");
  }
  return response;
}

function toolDefinitions(revision) {
  const revisionProperty = {
    type: "string",
    enum: [revision],
    description: "Exact running Playable Home revision. Call vista_world_status first.",
  };
  const generationProperty = {
    type: "integer",
    minimum: 0,
    maximum: MAX_GENERATION,
    description: "Exact generation returned by vista_world_status or the preceding typed command.",
  };
  const semanticProperty = {
    type: "string",
    pattern: SEMANTIC_ID_RE.source,
    maxLength: 240,
  };
  const actionSchema = {
    type: "object",
    additionalProperties: false,
    properties: {
      action_id: { type: "string", pattern: ACTION_ID_RE.source, maxLength: 80 },
      type: { type: "string", enum: [...NPC_ACTION_TYPES] },
      target_semantic_id: semanticProperty,
      target_location_cm: {
        type: "array", minItems: 3, maxItems: 3,
        items: { type: "number", minimum: -10_000_000, maximum: 10_000_000 },
      },
      duration_sec: { type: "number", minimum: 0, maximum: 300 },
      timeout_sec: { type: "number", minimum: 0, maximum: 300 },
      speech: { type: "string", maxLength: 500 },
    },
    required: ["action_id", "type"],
  };
  return Object.freeze([
    {
      name: TOOL_NAMES.status,
      description: "Read this MCP process's typed VISTA Playable Home session binding, generation, and active event. This is local state and performs no mutation.",
      inputSchema: {
        type: "object", additionalProperties: false,
        properties: { expected_revision: revisionProperty },
        required: ["expected_revision"],
      },
    },
    {
      name: TOOL_NAMES.interact,
      description: "Apply one allowlisted interaction to a stable semantic entity in the running Playable Home. Never sends Python, object paths, or transport options.",
      inputSchema: {
        type: "object", additionalProperties: false,
        properties: {
          expected_revision: revisionProperty,
          session_generation: generationProperty,
          requester_semantic_id: semanticProperty,
          target_semantic_id: semanticProperty,
          affordance: { type: "string", enum: [...INTERACTION_AFFORDANCES] },
          placement_anchor_semantic_id: semanticProperty,
        },
        required: [
          "expected_revision", "session_generation", "requester_semantic_id",
          "target_semantic_id", "affordance",
        ],
      },
    },
    {
      name: TOOL_NAMES.npcQueue,
      description: "Replace one Playable Home NPC's bounded action queue with 1-32 typed actions.",
      inputSchema: {
        type: "object", additionalProperties: false,
        properties: {
          expected_revision: revisionProperty,
          session_generation: generationProperty,
          npc_semantic_id: semanticProperty,
          replace: { type: "boolean", const: true },
          actions: { type: "array", minItems: 1, maxItems: 32, items: actionSchema },
        },
        required: ["expected_revision", "session_generation", "npc_semantic_id", "replace", "actions"],
      },
    },
    {
      name: TOOL_NAMES.eventStart,
      description: "Start one verified VISTA event overlay in the persistent Playable Home.",
      inputSchema: {
        type: "object", additionalProperties: false,
        properties: {
          expected_revision: revisionProperty,
          session_generation: generationProperty,
          event_id: { type: "string", enum: VERIFIED_EVENT_IDS },
        },
        required: ["expected_revision", "session_generation", "event_id"],
      },
    },
    {
      name: TOOL_NAMES.eventReset,
      description: "Reset the active VISTA event overlay to the clean Playable Home baseline.",
      inputSchema: {
        type: "object", additionalProperties: false,
        properties: {
          expected_revision: revisionProperty,
          session_generation: generationProperty,
        },
        required: ["expected_revision", "session_generation"],
      },
    },
  ]);
}

function createVistaWorldMcpTools({ sendTyped, env = process.env, randomBytes = crypto.randomBytes } = {}) {
  if (typeof sendTyped !== "function") throw new TypeError("sendTyped is required");
  const revision = configuredRevision(env);
  const sessionId = randomSessionId(randomBytes);
  const state = {
    generation: normalizeInitialGeneration(env),
    activeEvent: null,
    inFlight: false,
  };

  function status(args) {
    exactObject(args, new Set(["expected_revision"]), new Set(["expected_revision"]), "VISTA world status");
    requireExpectedRevision(args.expected_revision, revision);
    return {
      schema: "simworld.vista.playable-world-session/v1",
      session_id: sessionId,
      revision,
      generation: state.generation,
      active_event: state.activeEvent,
      status: state.inFlight ? "busy" : "bound",
    };
  }

  async function dispatch(payload, activeEventAfterSuccess) {
    if (state.inFlight) fail("VISTA_WORLD_BUSY", "A typed VISTA world command is already in flight");
    if (!isVistaWorldWirePayloadAllowed(payload, revision)) {
      fail("VISTA_WORLD_INPUT_INVALID", "Typed VISTA world payload failed the wire contract");
    }
    state.inFlight = true;
    try {
      const response = validateRuntimeResponse(await sendTyped(payload), payload);
      if (SUCCESS_STATUSES.has(response.status)) {
        state.generation = response.session_generation;
        if (activeEventAfterSuccess !== undefined) state.activeEvent = activeEventAfterSuccess;
      } else if (validGeneration(response.session_generation)
          && response.session_generation >= state.generation) {
        // A newly-created MCP process can start behind an already-running UE
        // world. A typed error may safely disclose the authoritative generation
        // so a later explicit command can use it. This helper itself never
        // retries a mutation.
        state.generation = response.session_generation;
      }
      return {
        schema: "simworld.vista.playable-world-command-result/v1",
        session_id: sessionId,
        generation: state.generation,
        command_id: payload.command_id,
        status: response.status,
        code: response.code,
        ...(typeof response.target_semantic_id === "string"
          ? { target_semantic_id: response.target_semantic_id } : {}),
        ...(isPlainObject(response.state) ? { state: response.state } : {}),
        ...(activeEventAfterSuccess === undefined ? {} : { active_event: state.activeEvent }),
      };
    } finally {
      state.inFlight = false;
    }
  }

  const handlers = Object.freeze({
    [TOOL_NAMES.status]: status,
    [TOOL_NAMES.interact]: (args) => dispatch(
      normalizeInteractionArguments(args, revision, state.generation),
      undefined,
    ),
    [TOOL_NAMES.npcQueue]: (args) => dispatch(
      normalizeNpcQueueArguments(args, revision, state.generation),
      undefined,
    ),
    [TOOL_NAMES.eventStart]: (args) => {
      const payload = normalizeEventArguments(
        args, revision, state.generation, "start_event", randomBytes,
      );
      return dispatch(payload, payload.event_id);
    },
    [TOOL_NAMES.eventReset]: (args) => dispatch(
      normalizeEventArguments(args, revision, state.generation, "reset_event", randomBytes),
      null,
    ),
  });

  return Object.freeze({
    revision,
    toolDefinitions: toolDefinitions(revision),
    handlers,
  });
}

module.exports = {
  DEFAULT_REVISION,
  MAX_GENERATION,
  TOOL_NAMES,
  VERIFIED_EVENT_IDS,
  VISTA_WORLD_TRANSPORT_TIMEOUT_MS,
  VistaWorldMcpError,
  configuredRevision,
  createVistaWorldMcpTools,
  internalVistaWorldMutationAllowed,
  isVistaWorldWirePayloadAllowed,
};

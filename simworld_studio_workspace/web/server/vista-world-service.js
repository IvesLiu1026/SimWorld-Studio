"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const SESSION_ID_RE = /^vws-[a-f0-9]{24}$/;
const COMMAND_ID_RE = /^vwc-[a-f0-9]{24}$/;
const REVISION_RE = /^[a-z0-9][a-z0-9._-]{0,79}$/;
const EVENT_ID_RE = /^[a-z0-9][a-z0-9._-]{0,79}$/;
const SEMANTIC_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._/-]{0,239}$/;
const INTERACTION_AFFORDANCES = new Set([
  "open", "close", "pick_up", "drop", "place", "toggle", "sit", "inspect",
]);
const NPC_ACTION_TYPES = new Set([
  "navigate_to", "look_at", "pick_up", "place", "open_door", "close_door",
  "sit", "wait", "speak",
]);

class VistaWorldError extends Error {
  constructor(code, message, { status = 400, retryable = false, generation } = {}) {
    super(message);
    this.name = "VistaWorldError";
    this.code = code;
    this.status = status;
    this.retryable = retryable;
    if (Number.isSafeInteger(generation) && generation >= 0) {
      this.generation = generation;
    }
  }
}

function randomId(prefix) {
  return `${prefix}-${crypto.randomBytes(12).toString("hex")}`;
}

function exactObject(value, allowed, required, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new VistaWorldError("VISTA_WORLD_INPUT_INVALID", `${label} must be an object`);
  }
  const keys = Object.keys(value);
  const unknown = keys.filter((key) => !allowed.has(key));
  const missing = [...required].filter((key) => !Object.prototype.hasOwnProperty.call(value, key));
  if (unknown.length || missing.length) {
    throw new VistaWorldError("VISTA_WORLD_INPUT_INVALID", `${label} has an invalid shape`);
  }
  return value;
}

function requireString(value, pattern, label) {
  if (typeof value !== "string" || !pattern.test(value)) {
    throw new VistaWorldError("VISTA_WORLD_INPUT_INVALID", `${label} is invalid`);
  }
  return value;
}

function requireGeneration(value) {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new VistaWorldError("VISTA_WORLD_INPUT_INVALID", "generation is invalid");
  }
  return value;
}

function normalizeIdentity(identity) {
  if (!identity || typeof identity !== "object"
      || typeof identity.ownerId !== "string" || !identity.ownerId
      || typeof identity.sessionId !== "string" || !identity.sessionId
      || typeof identity.leaseId !== "string" || !identity.leaseId
      || !Number.isSafeInteger(identity.slotId)
      || !Number.isSafeInteger(identity.mcpPort)) {
    throw new VistaWorldError(
      "VISTA_WORLD_ACCESS_INVALID",
      "An active Studio world session is required",
      { status: 503, retryable: true },
    );
  }
  return Object.freeze({
    ownerId: identity.ownerId,
    sessionId: identity.sessionId,
    leaseId: identity.leaseId,
    slotId: identity.slotId,
    mcpPort: identity.mcpPort,
    ...(identity.signal instanceof AbortSignal ? { signal: identity.signal } : {}),
  });
}

function sameIdentity(left, right) {
  return left.ownerId === right.ownerId
    && left.sessionId === right.sessionId
    && left.leaseId === right.leaseId
    && left.slotId === right.slotId
    && left.mcpPort === right.mcpPort;
}

function safeJsonFile(root, relative, label) {
  const candidate = path.resolve(root, relative);
  if (candidate !== root && !candidate.startsWith(root + path.sep)) {
    throw new VistaWorldError("VISTA_WORLD_CATALOG_INVALID", `${label} escaped the catalog`, { status: 500 });
  }
  let metadata;
  try {
    metadata = fs.lstatSync(candidate);
  } catch {
    throw new VistaWorldError("VISTA_WORLD_NOT_FOUND", `${label} was not found`, { status: 404 });
  }
  if (!metadata.isFile() || metadata.isSymbolicLink() || metadata.size > 4 * 1024 * 1024) {
    throw new VistaWorldError("VISTA_WORLD_CATALOG_INVALID", `${label} is not an accepted file`, { status: 500 });
  }
  try {
    const payload = JSON.parse(fs.readFileSync(candidate, "utf8"));
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) throw new Error("shape");
    return payload;
  } catch (error) {
    if (error instanceof VistaWorldError) throw error;
    throw new VistaWorldError("VISTA_WORLD_CATALOG_INVALID", `${label} is invalid JSON`, { status: 500 });
  }
}

function createVistaWorldFileCatalog({ root } = {}) {
  const resolvedRoot = path.resolve(String(root || ""));
  if (!path.isAbsolute(String(root || ""))) {
    throw new TypeError("VISTA world catalog root must be absolute");
  }
  return Object.freeze({
    revision(revision) {
      requireString(revision, REVISION_RE, "revision");
      const house = safeJsonFile(resolvedRoot, "house.json", "house revision");
      const observed = String(house.revision || house.house_revision || "");
      if (observed !== revision) {
        throw new VistaWorldError("VISTA_WORLD_REVISION_NOT_FOUND", "World revision was not found", { status: 404 });
      }
      return house;
    },
    event(eventId, revision) {
      requireString(eventId, EVENT_ID_RE, "event id");
      const event = safeJsonFile(resolvedRoot, path.join("events", `${eventId}.json`), "event");
      const compatible = String(
        event.compatible_revision
        || event.house_revision
        || (event.compatible_house && event.compatible_house.revision)
        || "",
      );
      if (compatible !== revision) {
        throw new VistaWorldError("VISTA_WORLD_EVENT_INCOMPATIBLE", "Event is incompatible with this world revision", { status: 409 });
      }
      return event;
    },
  });
}

function publicRevision(house) {
  const rooms = Array.isArray(house.rooms) ? house.rooms : [];
  const portals = Array.isArray(house.portals) ? house.portals : [];
  const entities = Array.isArray(house.entities) ? house.entities : [];
  return {
    schema: "simworld.vista.playable-world-revision/v1",
    house_id: String(house.house_id || ""),
    revision: String(house.revision || house.house_revision || ""),
    content_digest: typeof house.content_digest === "string" ? house.content_digest : null,
    room_ids: rooms.map((room) => String(room.id || room.room_id || "")).filter(Boolean),
    portal_count: portals.length,
    entity_count: entities.length,
  };
}

function normalizeInteraction(body, session) {
  exactObject(
    body,
    new Set([
      "kind", "generation", "requester_semantic_id", "target_semantic_id",
      "affordance", "placement_anchor_semantic_id",
    ]),
    new Set(["kind", "generation", "requester_semantic_id", "target_semantic_id", "affordance"]),
    "interaction",
  );
  if (body.kind !== "interaction" || !INTERACTION_AFFORDANCES.has(body.affordance)) {
    throw new VistaWorldError("VISTA_WORLD_ACTION_UNSUPPORTED", "Interaction is not allowlisted");
  }
  return {
    operation: "interaction",
    command_id: randomId("vwc"),
    expected_revision: session.revision,
    session_generation: requireGeneration(body.generation),
    requester_semantic_id: requireString(body.requester_semantic_id, SEMANTIC_ID_RE, "requester id"),
    target_semantic_id: requireString(body.target_semantic_id, SEMANTIC_ID_RE, "target id"),
    affordance: body.affordance,
    ...(body.placement_anchor_semantic_id === undefined ? {} : {
      placement_anchor_semantic_id: requireString(
        body.placement_anchor_semantic_id,
        SEMANTIC_ID_RE,
        "placement anchor id",
      ),
    }),
  };
}

function normalizeNpcAction(action, index) {
  exactObject(
    action,
    new Set([
      "action_id", "type", "target_semantic_id", "target_location_cm",
      "duration_sec", "timeout_sec", "speech",
    ]),
    new Set(["action_id", "type"]),
    `NPC action ${index}`,
  );
  requireString(action.action_id, /^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/, "action id");
  if (!NPC_ACTION_TYPES.has(action.type)) {
    throw new VistaWorldError("VISTA_WORLD_ACTION_UNSUPPORTED", "NPC action is not allowlisted");
  }
  const normalized = { action_id: action.action_id, type: action.type };
  if (action.target_semantic_id !== undefined) {
    normalized.target_semantic_id = requireString(action.target_semantic_id, SEMANTIC_ID_RE, "target id");
  }
  if (action.target_location_cm !== undefined) {
    if (!Array.isArray(action.target_location_cm) || action.target_location_cm.length !== 3
        || action.target_location_cm.some((value) => !Number.isFinite(value) || Math.abs(value) > 10_000_000)) {
      throw new VistaWorldError("VISTA_WORLD_INPUT_INVALID", "NPC target location is invalid");
    }
    normalized.target_location_cm = [...action.target_location_cm];
  }
  for (const field of ["duration_sec", "timeout_sec"]) {
    if (action[field] !== undefined) {
      if (!Number.isFinite(action[field]) || action[field] < 0 || action[field] > 300) {
        throw new VistaWorldError("VISTA_WORLD_INPUT_INVALID", `${field} is invalid`);
      }
      normalized[field] = action[field];
    }
  }
  if (action.speech !== undefined) {
    if (typeof action.speech !== "string" || action.speech.length > 500) {
      throw new VistaWorldError("VISTA_WORLD_INPUT_INVALID", "NPC speech is invalid");
    }
    normalized.speech = action.speech;
  }
  return normalized;
}

function normalizeNpcQueue(body, session) {
  exactObject(
    body,
    new Set(["kind", "generation", "npc_semantic_id", "replace", "actions"]),
    new Set(["kind", "generation", "npc_semantic_id", "replace", "actions"]),
    "NPC queue",
  );
  if (body.kind !== "npc_queue" || body.replace !== true
      || !Array.isArray(body.actions) || body.actions.length < 1 || body.actions.length > 32) {
    throw new VistaWorldError("VISTA_WORLD_INPUT_INVALID", "NPC queue is invalid");
  }
  return {
    operation: "npc_queue",
    command_id: randomId("vwc"),
    expected_revision: session.revision,
    session_generation: requireGeneration(body.generation),
    npc_semantic_id: requireString(body.npc_semantic_id, SEMANTIC_ID_RE, "NPC id"),
    replace: true,
    actions: body.actions.map(normalizeNpcAction),
  };
}

function runtimeFailure(result, generation) {
  const runtimeCode = typeof result.code === "string" ? result.code : "RUNTIME_ACTION_FAILED";
  const mappings = new Map([
    ["SESSION_GENERATION_MISMATCH", ["VISTA_WORLD_GENERATION_STALE", 409, false]],
    ["REVISION_MISMATCH", ["VISTA_WORLD_SESSION_STALE", 409, false]],
    ["REQUESTER_NOT_FOUND", ["VISTA_WORLD_NOT_FOUND", 404, false]],
    ["TARGET_NOT_INTERACTABLE", ["VISTA_WORLD_NOT_FOUND", 404, false]],
    ["PLACEMENT_ANCHOR_NOT_FOUND", ["VISTA_WORLD_NOT_FOUND", 404, false]],
    ["NPC_CONTROLLER_NOT_FOUND", ["VISTA_WORLD_NOT_FOUND", 404, false]],
    ["AFFORDANCE_UNSUPPORTED", ["VISTA_WORLD_ACTION_UNSUPPORTED", 400, false]],
    ["EVENT_OPERATION_UNSUPPORTED", ["VISTA_WORLD_ACTION_UNSUPPORTED", 400, false]],
    ["OPERATION_UNSUPPORTED", ["VISTA_WORLD_ACTION_UNSUPPORTED", 400, false]],
    ["DISPATCH_TIMEOUT", ["VISTA_WORLD_RUNTIME_UNAVAILABLE", 503, true]],
    ["DISPATCH_FAILED", ["VISTA_WORLD_RUNTIME_UNAVAILABLE", 503, true]],
    ["RUNTIME_UNAVAILABLE", ["VISTA_WORLD_RUNTIME_UNAVAILABLE", 503, true]],
  ]);
  const [code, status, retryable] = mappings.get(runtimeCode)
    || ["VISTA_WORLD_ACTION_FAILED", 409, false];
  const error = new VistaWorldError(
    code,
    "Typed Unreal world runtime rejected the action",
    { status, retryable, generation },
  );
  // Retain the bounded runtime code for server-side diagnostics. Routes expose
  // only the stable public code and authoritative generation.
  error.runtimeCode = runtimeCode;
  return error;
}

function createVistaWorldService({ catalog, transport, compiler = null, now = () => Date.now() } = {}) {
  if (!catalog || typeof catalog.revision !== "function" || typeof catalog.event !== "function") {
    throw new TypeError("VISTA world catalog is required");
  }
  if (!transport || typeof transport.send !== "function") {
    throw new TypeError("VISTA world typed transport is required");
  }
  const sessions = new Map();

  function sessionFor(sessionId, identity) {
    requireString(sessionId, SESSION_ID_RE, "session id");
    const session = sessions.get(sessionId);
    if (!session) {
      throw new VistaWorldError("VISTA_WORLD_SESSION_NOT_FOUND", "World session was not found", { status: 404 });
    }
    if (!sameIdentity(session.identity, identity)) {
      throw new VistaWorldError("VISTA_WORLD_SESSION_STALE", "World session belongs to another active lease", { status: 409 });
    }
    return session;
  }

  function assertGeneration(session, generation) {
    if (requireGeneration(generation) !== session.generation) {
      throw new VistaWorldError("VISTA_WORLD_GENERATION_STALE", "World session generation is stale", { status: 409 });
    }
  }

  async function sendAndAdvance(session, payload, context) {
    let result;
    try {
      result = await transport.send(payload, context);
    } catch (rawError) {
      const error = new VistaWorldError(
        "VISTA_WORLD_RUNTIME_UNAVAILABLE",
        "Typed Unreal world runtime is unavailable",
        { status: 503, retryable: true },
      );
      error.cause = rawError;
      throw error;
    }
    const acceptedStatuses = new Set(["completed", "accepted", "success", "error"]);
    if (!result || typeof result !== "object" || Array.isArray(result)
        || result.command_id !== payload.command_id
        || !acceptedStatuses.has(String(result.status))) {
      throw new VistaWorldError("VISTA_WORLD_PROTOCOL_ERROR", "Typed Unreal world response is invalid", { status: 502 });
    }
    if (String(result.status) === "error") {
      const runtimeCode = typeof result.code === "string" ? result.code : "";
      const infrastructureFailure = new Set([
        "DISPATCH_TIMEOUT", "DISPATCH_FAILED", "RUNTIME_UNAVAILABLE",
      ]).has(runtimeCode);
      if (!Number.isSafeInteger(result.session_generation)
          || result.session_generation < session.generation) {
        if (infrastructureFailure && result.session_generation === undefined) {
          throw runtimeFailure(result, session.generation);
        }
        throw new VistaWorldError(
          "VISTA_WORLD_PROTOCOL_ERROR",
          "Typed Unreal world error generation is invalid",
          { status: 502, generation: session.generation },
        );
      }
      session.generation = result.session_generation;
      session.updatedAt = now();
      throw runtimeFailure(result, session.generation);
    }
    if (!Number.isSafeInteger(result.session_generation)
        || result.session_generation !== session.generation + 1) {
      throw new VistaWorldError(
        "VISTA_WORLD_PROTOCOL_ERROR",
        "Typed Unreal world response generation is invalid",
        { status: 502, generation: session.generation },
      );
    }
    session.generation = result.session_generation;
    session.updatedAt = now();
    return {
      schema: "simworld.vista.playable-world-command-result/v1",
      session_id: session.id,
      generation: session.generation,
      command_id: payload.command_id,
      status: String(result.status),
      code: typeof result.code === "string" ? result.code : "VISTA_WORLD_OK",
      ...(typeof result.target_semantic_id === "string" ? { target_semantic_id: result.target_semantic_id } : {}),
      ...(result.state && typeof result.state === "object" && !Array.isArray(result.state) ? { state: result.state } : {}),
    };
  }

  return Object.freeze({
    async compile(body, rawIdentity) {
      const identity = normalizeIdentity(rawIdentity);
      exactObject(body, new Set(["revision"]), new Set(["revision"]), "compile request");
      const revision = requireString(body.revision, REVISION_RE, "revision");
      const house = catalog.revision(revision);
      if (!compiler || typeof compiler.compile !== "function") {
        throw new VistaWorldError(
          "VISTA_WORLD_COMPILER_UNAVAILABLE",
          "VISTA world compiler is unavailable",
          { status: 503, retryable: true },
        );
      }
      return compiler.compile(house, identity);
    },

    async revision(revision, rawIdentity) {
      normalizeIdentity(rawIdentity);
      return publicRevision(catalog.revision(requireString(revision, REVISION_RE, "revision")));
    },

    async createSession(body, rawIdentity) {
      const identity = normalizeIdentity(rawIdentity);
      exactObject(body, new Set(["revision"]), new Set(["revision"]), "session request");
      const revision = requireString(body.revision, REVISION_RE, "revision");
      const house = catalog.revision(revision);
      const id = randomId("vws");
      const session = {
        id,
        revision,
        houseId: String(house.house_id || ""),
        identity,
        generation: 0,
        activeEvent: null,
        createdAt: now(),
        updatedAt: now(),
      };
      sessions.set(id, session);
      return {
        schema: "simworld.vista.playable-world-session/v1",
        session_id: id,
        revision,
        generation: 0,
        active_event: null,
        status: "bound",
      };
    },

    async status(sessionId, rawIdentity) {
      const identity = normalizeIdentity(rawIdentity);
      const session = sessionFor(sessionId, identity);
      return {
        schema: "simworld.vista.playable-world-session/v1",
        session_id: session.id,
        revision: session.revision,
        generation: session.generation,
        active_event: session.activeEvent,
        status: "bound",
      };
    },

    async action(sessionId, body, rawIdentity) {
      const identity = normalizeIdentity(rawIdentity);
      const session = sessionFor(sessionId, identity);
      const payload = body && body.kind === "npc_queue"
        ? normalizeNpcQueue(body, session)
        : normalizeInteraction(body, session);
      assertGeneration(session, payload.session_generation);
      return sendAndAdvance(session, payload, identity);
    },

    async startEvent(sessionId, eventId, body, rawIdentity) {
      const identity = normalizeIdentity(rawIdentity);
      const session = sessionFor(sessionId, identity);
      exactObject(body, new Set(["generation"]), new Set(["generation"]), "event start");
      assertGeneration(session, body.generation);
      const event = catalog.event(requireString(eventId, EVENT_ID_RE, "event id"), session.revision);
      const payload = {
        operation: "event",
        command_id: randomId("vwc"),
        expected_revision: session.revision,
        session_generation: session.generation,
        event_operation: "start_event",
        event_id: String(event.event_id || eventId),
      };
      const result = await sendAndAdvance(session, payload, identity);
      session.activeEvent = payload.event_id;
      return { ...result, active_event: session.activeEvent };
    },

    async resetEvent(sessionId, body, rawIdentity) {
      const identity = normalizeIdentity(rawIdentity);
      const session = sessionFor(sessionId, identity);
      exactObject(body, new Set(["generation"]), new Set(["generation"]), "event reset");
      assertGeneration(session, body.generation);
      const payload = {
        operation: "event",
        command_id: randomId("vwc"),
        expected_revision: session.revision,
        session_generation: session.generation,
        event_operation: "reset_event",
      };
      const result = await sendAndAdvance(session, payload, identity);
      session.activeEvent = null;
      return { ...result, active_event: null };
    },
  });
}

module.exports = {
  COMMAND_ID_RE,
  INTERACTION_AFFORDANCES,
  NPC_ACTION_TYPES,
  SESSION_ID_RE,
  VistaWorldError,
  createVistaWorldFileCatalog,
  createVistaWorldService,
  normalizeIdentity,
  normalizeInteraction,
  normalizeNpcQueue,
  publicRevision,
};

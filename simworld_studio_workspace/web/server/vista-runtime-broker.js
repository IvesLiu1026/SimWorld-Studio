"use strict";

const crypto = require("node:crypto");

const VISTA_SETUP_ROUTE = "/api/vista/setup_vista_play_mode";
const VISTA_STOP_ROUTE = "/api/vista/stop_vista_play_mode";
const VISTA_STATE_ROUTE = "/api/vista/get_vista_state";
const VISTA_SETUP_SCHEMA = "vista-runtime-setup/v1";
const VISTA_STOP_SCHEMA = "vista-runtime-stop/v1";
const VISTA_STATE_SCHEMA = "vista-runtime-state/v1";
const VISTA_SETUP_MARKER = "VISTA_SETUP_V1";
const VISTA_STOP_MARKER = "VISTA_STOP_V1";
const VISTA_STATE_MARKER = "VISTA_STATE_V1";
const FIXED_NONCE_PLACEHOLDER = "VISTA_SERVER_NONCE_PLACEHOLDER_V1";
const VISTA_GAME_MODE_CLASS =
  "/Game/Human_Avatar/DefaultCharacter/ThirdPerson/Blueprints/" +
  "BP_ThirdPersonGameMode.BP_ThirdPersonGameMode_C";
const VISTA_PAWN_CLASS =
  "/Game/Human_Avatar/DefaultCharacter/ThirdPerson/Blueprints/" +
  "BP_ThirdPersonCharacter.BP_ThirdPersonCharacter_C";

const DEFAULT_SETUP_GRACE_MS = 30_000;
const DEFAULT_STATE_CACHE_MS = 500;
const SETUP_TIMEOUT_MS = 30_000;
const STOP_TIMEOUT_MS = 15_000;
const STATE_TIMEOUT_MS = 15_000;

const STATE_LIMITS = Object.freeze({
  location: 10_000_000,
  rotation: 360,
  velocity: 100_000,
  engineTime: 315_360_000,
});

// These scripts are fixed source artifacts. No request field is interpolated into
// either payload and neither payload saves content or starts PIE/SIE.
const FIXED_SETUP_SCRIPT = [
  "import json",
  "import unreal",
  `GAME_MODE_CLASS = ${JSON.stringify(VISTA_GAME_MODE_CLASS)}`,
  `PAWN_CLASS = ${JSON.stringify(VISTA_PAWN_CLASS)}`,
  "game_mode_class = unreal.load_class(None, GAME_MODE_CLASS)",
  "pawn_class = unreal.load_class(None, PAWN_CLASS)",
  "if game_mode_class is None or pawn_class is None:",
  "    raise RuntimeError('fixed VISTA classes are unavailable')",
  "game_mode_default = unreal.get_default_object(game_mode_class)",
  "default_pawn_class = game_mode_default.get_editor_property('default_pawn_class')",
  "if default_pawn_class is None or default_pawn_class.get_path_name() != PAWN_CLASS:",
  "    raise RuntimeError('fixed VISTA game mode has an unexpected default pawn')",
  "editor_subsystem = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)",
  "editor_world = editor_subsystem.get_editor_world()",
  "if editor_world is None:",
  "    raise RuntimeError('editor world is unavailable')",
  "world_settings = editor_world.get_world_settings()",
  "if world_settings is None:",
  "    raise RuntimeError('world settings are unavailable')",
  "world_settings.set_editor_property('default_game_mode', game_mode_class)",
  "applied_game_mode = world_settings.get_editor_property('default_game_mode')",
  "if applied_game_mode is None or applied_game_mode.get_path_name() != GAME_MODE_CLASS:",
  "    raise RuntimeError('fixed VISTA game mode override was not applied')",
  "actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)",
  "player_starts = [actor for actor in actor_subsystem.get_all_level_actors() if isinstance(actor, unreal.PlayerStart)]",
  "if not player_starts:",
  "    raise RuntimeError('PlayerStart is unavailable')",
  "payload = {",
  `    'schema': ${JSON.stringify(VISTA_SETUP_SCHEMA)},`,
  "    'phase': 'prepared',",
  "    'prepared': True,",
  "    'game_mode_class': applied_game_mode.get_path_name(),",
  "    'pawn_class': pawn_class.get_path_name(),",
  "    'default_pawn_class': default_pawn_class.get_path_name(),",
  "    'player_start_count': len(player_starts),",
  "}",
  `print(${JSON.stringify(`${VISTA_SETUP_MARKER}:${FIXED_NONCE_PLACEHOLDER}:`)} + json.dumps(payload, separators=(',', ':'), allow_nan=False))`,
].join("\n");

const FIXED_STATE_SCRIPT = [
  "import json",
  "import unreal",
  `PAWN_CLASS = ${JSON.stringify(VISTA_PAWN_CLASS)}`,
  "editor_subsystem = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)",
  "level_editor_subsystem = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)",
  "if level_editor_subsystem is None:",
  "    raise RuntimeError('level editor subsystem is unavailable')",
  "pie_active = bool(level_editor_subsystem.is_in_play_in_editor())",
  "game_world = editor_subsystem.get_game_world()",
  "if not pie_active:",
  "    payload = {",
  `        'schema': ${JSON.stringify(VISTA_STATE_SCHEMA)},`,
  "        'pie': False,",
  "        'possessed': False,",
  "        'pawn_class': None,",
  "        'location': None,",
  "        'rotation': None,",
  "        'velocity': None,",
  "        'on_ground': None,",
  "        'engine_time': None,",
  "    }",
  "else:",
  "    if game_world is None:",
  "        raise RuntimeError('PIE game world is not ready')",
  "    controller = unreal.GameplayStatics.get_player_controller(game_world, 0)",
  "    if controller is None:",
  "        raise RuntimeError('player zero controller is unavailable')",
  "    pawn = controller.get_pawn()",
  "    if pawn is None:",
  "        raise RuntimeError('player zero pawn is not possessed')",
  "    location = pawn.get_actor_location()",
  "    rotation = pawn.get_actor_rotation()",
  "    velocity = pawn.get_velocity()",
  "    movement = pawn.get_component_by_class(unreal.CharacterMovementComponent)",
  "    if movement is None:",
  "        raise RuntimeError('character movement component is unavailable')",
  "    payload = {",
  `        'schema': ${JSON.stringify(VISTA_STATE_SCHEMA)},`,
  "        'pie': True,",
  "        'possessed': True,",
  "        'pawn_class': pawn.get_class().get_path_name(),",
  "        'location': [float(location.x), float(location.y), float(location.z)],",
  "        'rotation': [float(rotation.pitch), float(rotation.yaw), float(rotation.roll)],",
  "        'velocity': [float(velocity.x), float(velocity.y), float(velocity.z)],",
  "        'on_ground': bool(movement.is_moving_on_ground()),",
  "        'engine_time': float(unreal.GameplayStatics.get_time_seconds(game_world)),",
  "    }",
  `print(${JSON.stringify(`${VISTA_STATE_MARKER}:${FIXED_NONCE_PLACEHOLDER}:`)} + json.dumps(payload, separators=(',', ':'), allow_nan=False))`,
].join("\n");

const FIXED_STOP_SCRIPT = [
  "import json",
  "import unreal",
  "level_editor_subsystem = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)",
  "if level_editor_subsystem is None:",
  "    raise RuntimeError('level editor subsystem is unavailable')",
  "was_playing = bool(level_editor_subsystem.is_in_play_in_editor())",
  "if was_playing:",
  "    level_editor_subsystem.editor_request_end_play()",
  "payload = {",
  `    'schema': ${JSON.stringify(VISTA_STOP_SCHEMA)},`,
  "    'phase': 'stop_requested',",
  "    'stop_requested': True,",
  "    'was_playing': was_playing,",
  "}",
  `print(${JSON.stringify(`${VISTA_STOP_MARKER}:${FIXED_NONCE_PLACEHOLDER}:`)} + json.dumps(payload, separators=(',', ':'), allow_nan=False))`,
].join("\n");

function defaultNonceFactory() {
  return crypto.randomBytes(16).toString("hex");
}

function bindFixedNonce(script, marker, nonce) {
  if (!/^[a-f0-9]{32}$/.test(nonce)) {
    throw new TypeError("VISTA server nonce must be 128-bit lowercase hex");
  }
  const placeholder = `${marker}:${FIXED_NONCE_PLACEHOLDER}`;
  if (script.split(placeholder).length !== 2) {
    throw new TypeError("fixed VISTA script must contain exactly one nonce placeholder");
  }
  return script.replace(placeholder, `${marker}:${nonce}`);
}

class VistaProtocolError extends Error {
  constructor(code) {
    super(code);
    this.name = "VistaProtocolError";
    this.code = code;
  }
}

function isPlainObject(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function hasStrictlyEmptyBody(body, headers = {}) {
  // Express leaves a zero-byte POST undefined; `{}` is the only JSON body that
  // is semantically empty. For an unparsed content type, prove that no bytes
  // arrived instead of trusting an undefined body from express.json().
  if (body === undefined) {
    const entries = headers && typeof headers === "object" ? Object.entries(headers) : [];
    const normalized = Object.fromEntries(entries.map(([key, value]) => [key.toLowerCase(), value]));
    if (normalized["transfer-encoding"] !== undefined) return false;
    const contentLength = normalized["content-length"];
    return contentLength === undefined || contentLength === "0" || contentLength === 0;
  }
  return isPlainObject(body) && Object.keys(body).length === 0;
}

function hasExactKeys(value, expectedKeys) {
  if (!isPlainObject(value) || Object.keys(value).length !== expectedKeys.length) return false;
  return expectedKeys.every((key) => Object.prototype.hasOwnProperty.call(value, key));
}

function pythonLogs(rawReply) {
  const logs = rawReply && rawReply.result && rawReply.result.python_logs;
  if (!Array.isArray(logs) || !logs.every((line) => typeof line === "string")) {
    throw new VistaProtocolError("VISTA_MARKER_MISSING");
  }
  return logs;
}

function extractSingleMarker(rawReply, marker) {
  const prefix = `${marker}:`;
  const matches = [];
  for (const line of pythonLogs(rawReply)) {
    let offset = 0;
    while (offset <= line.length) {
      const index = line.indexOf(prefix, offset);
      if (index < 0) break;
      matches.push(line.slice(index + prefix.length).trim());
      offset = index + prefix.length;
    }
  }
  if (matches.length !== 1) {
    throw new VistaProtocolError(matches.length === 0 ? "VISTA_MARKER_MISSING" : "VISTA_MARKER_DUPLICATE");
  }
  try {
    const payload = JSON.parse(matches[0]);
    if (!isPlainObject(payload)) throw new Error("marker payload must be an object");
    return payload;
  } catch {
    throw new VistaProtocolError("VISTA_MARKER_MALFORMED");
  }
}

function validateSetupPayload(payload) {
  if (
    !hasExactKeys(payload, [
      "schema",
      "phase",
      "prepared",
      "game_mode_class",
      "pawn_class",
      "default_pawn_class",
      "player_start_count",
    ]) ||
    payload.schema !== VISTA_SETUP_SCHEMA ||
    payload.phase !== "prepared" ||
    payload.prepared !== true ||
    payload.game_mode_class !== VISTA_GAME_MODE_CLASS ||
    payload.pawn_class !== VISTA_PAWN_CLASS ||
    payload.default_pawn_class !== VISTA_PAWN_CLASS ||
    !Number.isSafeInteger(payload.player_start_count) ||
    payload.player_start_count < 1 ||
    payload.player_start_count > 10_000
  ) {
    throw new VistaProtocolError("VISTA_SETUP_INVALID");
  }
  return Object.freeze({
    schema: VISTA_SETUP_SCHEMA,
    phase: "prepared",
    prepared: true,
    game_mode_class: VISTA_GAME_MODE_CLASS,
    pawn_class: VISTA_PAWN_CLASS,
    player_start_present: true,
    requires_operator_play: true,
  });
}

function validateStopPayload(payload) {
  if (
    !hasExactKeys(payload, ["schema", "phase", "stop_requested", "was_playing"]) ||
    payload.schema !== VISTA_STOP_SCHEMA ||
    payload.phase !== "stop_requested" ||
    payload.stop_requested !== true ||
    typeof payload.was_playing !== "boolean"
  ) {
    throw new VistaProtocolError("VISTA_STOP_INVALID");
  }
  return Object.freeze({
    schema: VISTA_STOP_SCHEMA,
    phase: "stop_requested",
    stop_requested: true,
    was_playing: payload.was_playing,
    retry_after_ms: DEFAULT_STATE_CACHE_MS,
  });
}

function validateTriple(value, bound) {
  if (
    !Array.isArray(value) ||
    value.length !== 3 ||
    !value.every((item) => Number.isFinite(item) && Math.abs(item) <= bound)
  ) {
    throw new VistaProtocolError("VISTA_STATE_VECTOR_INVALID");
  }
  return value.map((item) => Number(item));
}

function validateStatePayload(payload) {
  if (!hasExactKeys(payload, [
    "schema",
    "pie",
    "possessed",
    "pawn_class",
    "location",
    "rotation",
    "velocity",
    "on_ground",
    "engine_time",
  ])) {
    throw new VistaProtocolError("VISTA_STATE_SHAPE_INVALID");
  }
  if (payload.schema !== VISTA_STATE_SCHEMA) {
    throw new VistaProtocolError("VISTA_STATE_NOT_READY");
  }
  if (payload.pie === false && payload.possessed === false) {
    if (
      payload.pawn_class !== null ||
      payload.location !== null ||
      payload.rotation !== null ||
      payload.velocity !== null ||
      payload.on_ground !== null ||
      payload.engine_time !== null
    ) {
      throw new VistaProtocolError("VISTA_STATE_STOPPED_INVALID");
    }
    return Object.freeze({
      schema: VISTA_STATE_SCHEMA,
      pie: false,
      possessed: false,
      pawn_class: null,
      location: null,
      rotation: null,
      velocity: null,
      on_ground: null,
      engine_time: null,
    });
  }
  if (payload.pie !== true || payload.possessed !== true) {
    throw new VistaProtocolError("VISTA_STATE_NOT_READY");
  }
  if (payload.pawn_class !== VISTA_PAWN_CLASS) {
    throw new VistaProtocolError("VISTA_PAWN_CLASS_MISMATCH");
  }
  const location = validateTriple(payload.location, STATE_LIMITS.location);
  const rotation = validateTriple(payload.rotation, STATE_LIMITS.rotation);
  const velocity = validateTriple(payload.velocity, STATE_LIMITS.velocity);
  if (typeof payload.on_ground !== "boolean") {
    throw new VistaProtocolError("VISTA_STATE_GROUND_INVALID");
  }
  if (
    !Number.isFinite(payload.engine_time) ||
    payload.engine_time < 0 ||
    payload.engine_time > STATE_LIMITS.engineTime
  ) {
    throw new VistaProtocolError("VISTA_STATE_TIME_INVALID");
  }
  return Object.freeze({
    schema: VISTA_STATE_SCHEMA,
    pie: true,
    possessed: true,
    pawn_class: VISTA_PAWN_CLASS,
    location,
    rotation,
    velocity,
    on_ground: payload.on_ground,
    engine_time: Number(payload.engine_time),
  });
}

function protocolFailure(error) {
  const code = error instanceof VistaProtocolError && error.code === "VISTA_PAWN_CLASS_MISMATCH"
    ? "VISTA_PAWN_CLASS_MISMATCH"
    : "VISTA_RUNTIME_PROTOCOL_ERROR";
  return {
    status: 502,
    body: {
      code,
      error: "VISTA runtime returned an invalid fixed response",
    },
  };
}

function transportFailure(error) {
  const retryAfterMs = error && Number.isFinite(error.retryAfterMs) && error.retryAfterMs > 0
    ? Math.min(Math.ceil(error.retryAfterMs), 60_000)
    : null;
  if (retryAfterMs !== null) {
    return {
      status: 429,
      headers: { "Retry-After": String(Math.max(1, Math.ceil(retryAfterMs / 1000))) },
      body: { code: "VISTA_RUNTIME_BUSY", error: "VISTA runtime is busy", retry_after_ms: retryAfterMs },
    };
  }
  return {
    status: 503,
    body: { code: "VISTA_RUNTIME_UNAVAILABLE", error: "VISTA runtime is unavailable" },
  };
}

function createVistaRuntimeBroker({
  ueBroker,
  now = () => Date.now(),
  nonceFactory = defaultNonceFactory,
  initialRuntimePhase = "unknown",
  setupGraceMs = DEFAULT_SETUP_GRACE_MS,
  stateCacheMs = DEFAULT_STATE_CACHE_MS,
} = {}) {
  if (!ueBroker || typeof ueBroker.send !== "function") {
    throw new TypeError("createVistaRuntimeBroker requires a UE broker");
  }
  if (!Number.isFinite(setupGraceMs) || setupGraceMs < 0) {
    throw new TypeError("setupGraceMs must be a non-negative finite number");
  }
  if (!Number.isFinite(stateCacheMs) || stateCacheMs < 500) {
    throw new TypeError("stateCacheMs must be at least 500ms");
  }
  if (typeof nonceFactory !== "function") {
    throw new TypeError("nonceFactory must be a function");
  }
  if (initialRuntimePhase !== "unknown" && initialRuntimePhase !== "stopped") {
    throw new TypeError("initialRuntimePhase must be unknown or stopped");
  }

  let setupInFlight = null;
  let setupCache = null;
  let stateProbeAfter = 0;
  let stopInFlight = null;
  let stateInFlight = null;
  let stateCache = null;
  let controlGeneration = 0;
  let runtimePhase = initialRuntimePhase;

  function sendResult(res, result) {
    for (const [name, value] of Object.entries(result.headers || {})) res.set(name, value);
    return res.status(result.status).json(result.body);
  }

  async function executeFixedScript(scriptTemplate, marker, timeoutMs, validatePayload) {
    let lastError = null;
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const nonce = nonceFactory();
      const script = bindFixedNonce(scriptTemplate, marker, nonce);
      try {
        const rawReply = await ueBroker.send(
          "execute_python_script",
          { script },
          { timeoutMs, queueDeadlineMs: timeoutMs * 2 },
        );
        return validatePayload(extractSingleMarker(rawReply, `${marker}:${nonce}`));
      } catch (error) {
        lastError = error;
        if (!(error instanceof VistaProtocolError) || error.code !== "VISTA_MARKER_MISSING") {
          throw error;
        }
      }
    }
    throw lastError;
  }

  async function executeSetup(generation) {
    try {
      const body = {
        ...await executeFixedScript(
          FIXED_SETUP_SCRIPT,
          VISTA_SETUP_MARKER,
          SETUP_TIMEOUT_MS,
          validateSetupPayload,
        ),
        retry_after_ms: setupGraceMs,
        state_probe_grace_ms: setupGraceMs,
      };
      if (generation !== controlGeneration) {
        return {
          status: 409,
          body: { code: "VISTA_SETUP_SUPERSEDED", error: "VISTA setup was superseded by Stop" },
        };
      }
      stateProbeAfter = now() + setupGraceMs;
      setupCache = { body, expiresAt: stateProbeAfter };
      stateCache = null;
      return { status: 200, body };
    } catch (error) {
      return error instanceof VistaProtocolError ? protocolFailure(error) : transportFailure(error);
    }
  }

  function getSetupResult() {
    const currentTime = now();
    if (setupCache && currentTime < setupCache.expiresAt) {
      return Promise.resolve({ status: 200, body: setupCache.body });
    }
    if (setupInFlight) return setupInFlight;
    controlGeneration += 1;
    stateCache = null;
    stateInFlight = null;
    const generation = controlGeneration;
    const tracked = executeSetup(generation).finally(() => {
      if (setupInFlight === tracked) setupInFlight = null;
    });
    setupInFlight = tracked;
    return setupInFlight;
  }

  async function executeStop() {
    try {
      return {
        status: 200,
        body: await executeFixedScript(
          FIXED_STOP_SCRIPT,
          VISTA_STOP_MARKER,
          STOP_TIMEOUT_MS,
          validateStopPayload,
        ),
      };
    } catch (error) {
      return error instanceof VistaProtocolError ? protocolFailure(error) : transportFailure(error);
    }
  }

  function getStopResult() {
    // A stop request must be able to clear the setup grace even if the stream
    // disconnected or UE lost the success marker after executing the script.
    if (stopInFlight) {
      stateProbeAfter = 0;
      setupCache = null;
      stateCache = null;
      return stopInFlight;
    }
    controlGeneration += 1;
    runtimePhase = "stopping";
    stateProbeAfter = 0;
    setupCache = null;
    stateCache = null;
    stateInFlight = null;
    stopInFlight = executeStop().finally(() => {
      stopInFlight = null;
    });
    return stopInFlight;
  }

  async function executeStateRead(generation) {
    let result;
    try {
      const body = await executeFixedScript(
        FIXED_STATE_SCRIPT,
        VISTA_STATE_MARKER,
        STATE_TIMEOUT_MS,
        validateStatePayload,
      );
      if (generation === controlGeneration) {
        if (body.pie === false) {
          runtimePhase = "stopped";
          stateProbeAfter = 0;
          setupCache = null;
        } else if (runtimePhase !== "stopping") {
          runtimePhase = "live";
        }
      }
      result = {
        status: 200,
        headers: { "Cache-Control": "no-store" },
        body,
      };
    } catch (error) {
      result = error instanceof VistaProtocolError ? protocolFailure(error) : transportFailure(error);
      result.headers = { ...(result.headers || {}), "Cache-Control": "no-store" };
    }
    if (generation === controlGeneration) {
      stateCache = { result, expiresAt: now() + stateCacheMs };
    }
    return result;
  }

  function getStateResult() {
    const currentTime = now();
    if (setupInFlight) {
      return Promise.resolve({
        status: 425,
        headers: { "Cache-Control": "no-store", "Retry-After": "1" },
        body: {
          code: "VISTA_SETUP_IN_PROGRESS",
          error: "VISTA setup is still in progress",
          retry_after_ms: 1_000,
        },
      });
    }
    if (stopInFlight) {
      return Promise.resolve({
        status: 425,
        headers: { "Cache-Control": "no-store", "Retry-After": "1" },
        body: {
          code: "VISTA_STOP_IN_PROGRESS",
          error: "VISTA Stop is still in progress",
          retry_after_ms: 1_000,
        },
      });
    }
    if (currentTime < stateProbeAfter) {
      const retryAfterMs = Math.max(1, Math.ceil(stateProbeAfter - currentTime));
      return Promise.resolve({
        status: 425,
        headers: {
          "Cache-Control": "no-store",
          "Retry-After": String(Math.max(1, Math.ceil(retryAfterMs / 1000))),
        },
        body: {
          code: "VISTA_PLAY_START_GRACE",
          error: "Start PIE through the viewport before requesting state",
          retry_after_ms: retryAfterMs,
        },
      });
    }
    if (stateCache && currentTime < stateCache.expiresAt) return Promise.resolve(stateCache.result);
    if (stateInFlight) return stateInFlight;
    const generation = controlGeneration;
    const tracked = executeStateRead(generation).finally(() => {
      if (stateInFlight === tracked) stateInFlight = null;
    });
    stateInFlight = tracked;
    return stateInFlight;
  }

  async function setupVistaPlayMode(req, res) {
    if (!hasStrictlyEmptyBody(req.body, req.headers)) {
      return sendResult(res, {
        status: 400,
        body: { code: "VISTA_EMPTY_BODY_REQUIRED", error: "Request body must be empty" },
      });
    }
    if (runtimePhase === "unknown") {
      return sendResult(res, {
        status: 428,
        body: {
          code: "VISTA_STATE_RECONCILIATION_REQUIRED",
          error: "Read the fixed VISTA state before claiming Play",
          retry_after_ms: DEFAULT_STATE_CACHE_MS,
        },
      });
    }
    if (runtimePhase === "stopping" || stopInFlight) {
      return sendResult(res, {
        status: 425,
        body: {
          code: "VISTA_STOP_IN_PROGRESS",
          error: "VISTA Stop must be confirmed before another Play",
          retry_after_ms: DEFAULT_STATE_CACHE_MS,
        },
      });
    }
    if (runtimePhase !== "stopped") {
      return sendResult(res, {
        status: 409,
        body: {
          code: "VISTA_PLAY_LEASE_HELD",
          error: "Another VISTA operator has already claimed Play",
          retry_after_ms: DEFAULT_STATE_CACHE_MS,
        },
      });
    }
    runtimePhase = "preparing";
    const result = await getSetupResult();
    if (result.status !== 200) {
      if (runtimePhase === "preparing") runtimePhase = "stopped";
      return sendResult(res, result);
    }
    if (runtimePhase !== "preparing") {
      return sendResult(res, {
        status: 409,
        body: {
          code: "VISTA_SETUP_SUPERSEDED",
          error: "VISTA setup was superseded by another control transition",
          retry_after_ms: DEFAULT_STATE_CACHE_MS,
        },
      });
    }
    runtimePhase = "play_pending";
    return sendResult(res, {
      ...result,
      body: { ...result.body, play_lease_granted: true },
    });
  }

  async function stopVistaPlayMode(req, res) {
    if (!hasStrictlyEmptyBody(req.body, req.headers)) {
      return sendResult(res, {
        status: 400,
        body: { code: "VISTA_EMPTY_BODY_REQUIRED", error: "Request body must be empty" },
      });
    }
    return sendResult(res, await getStopResult());
  }

  async function getVistaState(_req, res) {
    return sendResult(res, await getStateResult());
  }

  return Object.freeze({ getVistaState, setupVistaPlayMode, stopVistaPlayMode });
}

module.exports = {
  DEFAULT_SETUP_GRACE_MS,
  DEFAULT_STATE_CACHE_MS,
  FIXED_NONCE_PLACEHOLDER,
  FIXED_SETUP_SCRIPT,
  FIXED_STATE_SCRIPT,
  FIXED_STOP_SCRIPT,
  STATE_LIMITS,
  VISTA_GAME_MODE_CLASS,
  VISTA_PAWN_CLASS,
  VISTA_SETUP_MARKER,
  VISTA_SETUP_ROUTE,
  VISTA_SETUP_SCHEMA,
  VISTA_STOP_MARKER,
  VISTA_STOP_ROUTE,
  VISTA_STOP_SCHEMA,
  VISTA_STATE_MARKER,
  VISTA_STATE_ROUTE,
  VISTA_STATE_SCHEMA,
  VistaProtocolError,
  bindFixedNonce,
  createVistaRuntimeBroker,
  extractSingleMarker,
  hasStrictlyEmptyBody,
  validateSetupPayload,
  validateStopPayload,
  validateStatePayload,
};

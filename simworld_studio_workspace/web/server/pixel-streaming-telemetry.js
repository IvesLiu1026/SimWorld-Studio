"use strict";

const crypto = require("node:crypto");

const TELEMETRY_SCHEMA = "pixel-streaming-client-telemetry/v1";
const STATUS_SCHEMA = "pixel-streaming-session-status/v1";
const SUMMARY_SCHEMA = "pixel-streaming-telemetry-summary/v1";
const SHA256 = /^[a-f0-9]{64}$/;
const CONNECTION_ID = /^[a-f0-9]{32}$/;
const CONNECTION_STATES = new Set(["closed", "connected", "connecting", "disconnected", "failed", "new"]);
const ICE_CONNECTION_STATES = new Set(["checking", "closed", "completed", "connected", "disconnected", "failed", "new"]);
const ICE_GATHERING_STATES = new Set(["complete", "gathering", "new"]);
const CANDIDATE_TYPES = new Set(["host", "prflx", "relay", "srflx"]);
const CANDIDATE_PROTOCOLS = new Set(["tcp", "udp"]);
const TURN_TRANSPORTS = new Set(["tcp", "tls", "udp"]);
const ALLOWED_REPORT_KEYS = new Set([
  "schema",
  "connection_id",
  "sequence",
  "connection_state",
  "ice_connection_state",
  "ice_gathering_state",
  "data_channel_open",
  "video",
  "selected_candidate",
]);

class PixelStreamingTelemetryError extends Error {
  constructor(code, message, statusCode = 400) {
    super(message);
    this.name = "PixelStreamingTelemetryError";
    this.code = code;
    this.statusCode = statusCode;
  }
}

function fail(code, message, statusCode) {
  throw new PixelStreamingTelemetryError(code, message, statusCode);
}

function exactObject(value, allowed, field) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    fail("PIXEL_STREAMING_TELEMETRY_INVALID", `${field} must be an object`);
  }
  const unknown = Object.keys(value).find((key) => !allowed.has(key));
  if (unknown) fail("PIXEL_STREAMING_TELEMETRY_INVALID", `${field} contains an unsupported field`);
  return value;
}

function boundedInteger(value, field, min = 0, max = 1_000_000_000) {
  if (!Number.isSafeInteger(value) || value < min || value > max) {
    fail("PIXEL_STREAMING_TELEMETRY_INVALID", `${field} must be a bounded integer`);
  }
  return value;
}

function enumValue(value, allowed, field) {
  if (typeof value !== "string" || !allowed.has(value)) {
    fail("PIXEL_STREAMING_TELEMETRY_INVALID", `${field} is invalid`);
  }
  return value;
}

function validateCandidate(value) {
  if (value === null) return null;
  exactObject(value, new Set([
    "type",
    "protocol",
    "turn_transport",
    "candidate_fingerprint",
    "address_redacted",
  ]), "selected_candidate");
  for (const required of ["type", "protocol", "turn_transport", "candidate_fingerprint", "address_redacted"]) {
    if (!Object.hasOwn(value, required)) {
      fail("PIXEL_STREAMING_TELEMETRY_INVALID", `selected_candidate.${required} is required`);
    }
  }
  const type = enumValue(value.type, CANDIDATE_TYPES, "selected_candidate.type");
  const protocol = enumValue(value.protocol, CANDIDATE_PROTOCOLS, "selected_candidate.protocol");
  if (value.address_redacted !== true) {
    fail("PIXEL_STREAMING_TELEMETRY_SENSITIVE", "selected candidate addresses must be redacted");
  }
  if (typeof value.candidate_fingerprint !== "string" || !SHA256.test(value.candidate_fingerprint)) {
    fail("PIXEL_STREAMING_TELEMETRY_INVALID", "selected_candidate.candidate_fingerprint is invalid");
  }
  let turnTransport = null;
  if (type === "relay") {
    turnTransport = enumValue(value.turn_transport, TURN_TRANSPORTS, "selected_candidate.turn_transport");
    if (protocol === "udp" && turnTransport !== "udp") {
      fail("PIXEL_STREAMING_TELEMETRY_INVALID", "UDP relay candidates must use UDP TURN");
    }
    if (protocol === "tcp" && !["tcp", "tls"].includes(turnTransport)) {
      fail("PIXEL_STREAMING_TELEMETRY_INVALID", "TCP relay candidates must use TCP or TLS TURN");
    }
  } else if (value.turn_transport !== null) {
    fail("PIXEL_STREAMING_TELEMETRY_INVALID", "non-relay candidates cannot declare TURN transport");
  }
  return Object.freeze({
    type,
    protocol,
    turn_transport: turnTransport,
    candidate_fingerprint: value.candidate_fingerprint,
    address_redacted: true,
  });
}

function validateReport(value) {
  exactObject(value, ALLOWED_REPORT_KEYS, "telemetry");
  for (const required of ALLOWED_REPORT_KEYS) {
    if (!Object.hasOwn(value, required)) {
      fail("PIXEL_STREAMING_TELEMETRY_INVALID", `telemetry.${required} is required`);
    }
  }
  if (value.schema !== TELEMETRY_SCHEMA) {
    fail("PIXEL_STREAMING_TELEMETRY_INVALID", "telemetry schema is unsupported");
  }
  if (typeof value.connection_id !== "string" || !CONNECTION_ID.test(value.connection_id)) {
    fail("PIXEL_STREAMING_TELEMETRY_INVALID", "connection_id is invalid");
  }
  boundedInteger(value.sequence, "sequence");
  const connectionState = enumValue(value.connection_state, CONNECTION_STATES, "connection_state");
  const iceConnectionState = enumValue(value.ice_connection_state, ICE_CONNECTION_STATES, "ice_connection_state");
  const iceGatheringState = enumValue(value.ice_gathering_state, ICE_GATHERING_STATES, "ice_gathering_state");
  if (typeof value.data_channel_open !== "boolean") {
    fail("PIXEL_STREAMING_TELEMETRY_INVALID", "data_channel_open must be boolean");
  }
  exactObject(value.video, new Set(["decoded_frames", "frames_advancing"]), "video");
  if (!Object.hasOwn(value.video, "decoded_frames") || !Object.hasOwn(value.video, "frames_advancing")) {
    fail("PIXEL_STREAMING_TELEMETRY_INVALID", "video fields are required");
  }
  boundedInteger(value.video.decoded_frames, "video.decoded_frames");
  if (typeof value.video.frames_advancing !== "boolean") {
    fail("PIXEL_STREAMING_TELEMETRY_INVALID", "video.frames_advancing must be boolean");
  }
  return Object.freeze({
    schema: TELEMETRY_SCHEMA,
    connection_id: value.connection_id,
    sequence: value.sequence,
    connection_state: connectionState,
    ice_connection_state: iceConnectionState,
    ice_gathering_state: iceGatheringState,
    data_channel_open: value.data_channel_open,
    video: Object.freeze({
      decoded_frames: value.video.decoded_frames,
      frames_advancing: value.video.frames_advancing,
    }),
    selected_candidate: validateCandidate(value.selected_candidate),
  });
}

function identityKey(identity) {
  if (!identity || typeof identity !== "object" || Array.isArray(identity)) {
    fail("PIXEL_STREAMING_TELEMETRY_IDENTITY_INVALID", "active session identity is required", 401);
  }
  const fields = [identity.ownerId, identity.sessionId, identity.slotId, identity.leaseId];
  if (fields.some((value) => value === undefined || value === null || value === "")) {
    fail("PIXEL_STREAMING_TELEMETRY_IDENTITY_INVALID", "active session identity is incomplete", 401);
  }
  return crypto
    .createHash("sha256")
    .update("simworld/pixel-streaming-telemetry/session/v1\0")
    .update(JSON.stringify(fields))
    .digest("hex");
}

function positiveInteger(value, fallback, min, max, field) {
  const number = value === undefined ? fallback : Number(value);
  if (!Number.isSafeInteger(number) || number < min || number > max) {
    throw new TypeError(`${field} must be an integer between ${min} and ${max}`);
  }
  return number;
}

function createPixelStreamingTelemetryRegistry({
  clock = () => Date.now(),
  ttlMs = 30_000,
  maxSessions = 256,
  requireRelay = false,
} = {}) {
  const telemetryTtlMs = positiveInteger(ttlMs, 30_000, 5_000, 300_000, "ttlMs");
  const capacity = positiveInteger(maxSessions, 256, 1, 10_000, "maxSessions");
  if (typeof clock !== "function") throw new TypeError("clock must be a function");
  const records = new Map();

  function now() {
    const value = Number(clock());
    if (!Number.isSafeInteger(value) || value < 0) {
      fail("PIXEL_STREAMING_TELEMETRY_CLOCK_INVALID", "telemetry clock is invalid", 500);
    }
    return value;
  }

  function purge(current = now()) {
    for (const [key, record] of records) {
      if (current - record.receivedAt > telemetryTtlMs) records.delete(key);
    }
  }

  function isReady(report) {
    const candidate = report.selected_candidate;
    return report.connection_state === "connected"
      && ["connected", "completed"].includes(report.ice_connection_state)
      && report.ice_gathering_state === "complete"
      && report.data_channel_open === true
      && report.video.decoded_frames > 0
      && report.video.frames_advancing === true
      && Boolean(candidate)
      && (!requireRelay || candidate.type === "relay");
  }

  function record(identity, value) {
    const key = identityKey(identity);
    const report = validateReport(value);
    const current = now();
    purge(current);
    const previous = records.get(key);
    if (previous && previous.report.connection_id === report.connection_id
        && report.sequence <= previous.report.sequence) {
      fail("PIXEL_STREAMING_TELEMETRY_REPLAYED", "telemetry sequence must increase", 409);
    }
    if (!previous && records.size >= capacity) {
      const oldest = [...records.entries()].sort((left, right) => left[1].receivedAt - right[1].receivedAt)[0];
      if (oldest) records.delete(oldest[0]);
    }
    records.set(key, Object.freeze({ report, receivedAt: current }));
    return status(identity);
  }

  function status(identity) {
    const key = identityKey(identity);
    const current = now();
    purge(current);
    const record = records.get(key);
    if (!record) {
      return Object.freeze({
        schema: STATUS_SCHEMA,
        status: "not_observed",
        ready: false,
        require_relay: Boolean(requireRelay),
      });
    }
    const { report } = record;
    return Object.freeze({
      schema: STATUS_SCHEMA,
      status: isReady(report) ? "ready" : "connecting",
      ready: isReady(report),
      require_relay: Boolean(requireRelay),
      age_ms: current - record.receivedAt,
      connection_state: report.connection_state,
      ice_connection_state: report.ice_connection_state,
      ice_gathering_state: report.ice_gathering_state,
      data_channel_open: report.data_channel_open,
      video: report.video,
      selected_candidate: report.selected_candidate && Object.freeze({
        type: report.selected_candidate.type,
        protocol: report.selected_candidate.protocol,
        turn_transport: report.selected_candidate.turn_transport,
        address_redacted: true,
      }),
    });
  }

  function remove(identity) {
    try { return records.delete(identityKey(identity)); } catch (_error) { return false; }
  }

  function summary() {
    const current = now();
    purge(current);
    let readySessions = 0;
    let relaySessions = 0;
    for (const record of records.values()) {
      if (isReady(record.report)) readySessions += 1;
      if (record.report.selected_candidate && record.report.selected_candidate.type === "relay") relaySessions += 1;
    }
    return Object.freeze({
      schema: SUMMARY_SCHEMA,
      active_reports: records.size,
      ready_sessions: readySessions,
      relay_sessions: relaySessions,
      require_relay: Boolean(requireRelay),
      ttl_ms: telemetryTtlMs,
    });
  }

  return Object.freeze({
    record,
    status,
    remove,
    summary,
    clear: () => records.clear(),
  });
}

module.exports = {
  PixelStreamingTelemetryError,
  STATUS_SCHEMA,
  SUMMARY_SCHEMA,
  TELEMETRY_SCHEMA,
  createPixelStreamingTelemetryRegistry,
  validateReport,
};

"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const net = require("node:net");
const path = require("node:path");

const WEBRTC_PROBE_RESULTS_SCHEMA = "simworld-webrtc-probe-results/v1";
const WEBRTC_READINESS_RECEIPT_SCHEMA = "simworld-webrtc-readiness-receipt/v1";
const MAX_PROBE_BYTES = 256 * 1024;
const MAX_RECEIPT_BYTES = 64 * 1024;
const MAX_RECEIPT_TTL_MS = 24 * 60 * 60 * 1000;
const MAX_CLOCK_SKEW_MS = 5 * 60 * 1000;
const MIN_EXTERNAL_SESSION_MS = 12 * 60 * 1000;
const MIN_TURN_CREDENTIAL_TTL_MS = 5 * 60 * 1000;
const MAX_TURN_CREDENTIAL_TTL_MS = 24 * 60 * 60 * 1000;
const REQUIRED_TURN_TRANSPORTS = Object.freeze(["tcp", "tls", "udp"]);
const TURN_TRANSPORTS = new Set(REQUIRED_TURN_TRANSPORTS);
const NETWORK_CLASSES = new Set([
  "external_other",
  "mobile_hotspot",
  "residential_nat",
  "restricted_network",
]);
const TLS_VERSIONS = new Set(["TLSv1.2", "TLSv1.3"]);
const SHA256 = /^[a-f0-9]{64}$/;
const GIT_REVISION = /^[a-f0-9]{40}$/;
const SAFE_TOKEN = /^[A-Za-z0-9][A-Za-z0-9._:@/+~-]{0,159}$/;
const PUBLIC_DNS = /^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$/;
const PRIVATE_DNS_SUFFIX = /(?:^|\.)(?:home|internal|intranet|lan|local|localhost)$/i;
const SENSITIVE_KEY = /(?:^|_)(?:access_?token|address|api_?key|authorization|bearer|candidate_?sdp|cookie|credential_?value|host(?:name)?|ice_?(?:password|username)|ip|password|port|private_?key|raw_?port|secret|session_?token|turn_?username|url|username)(?:$|_)/i;
const CREDENTIAL_VALUE = /(?:\b(?:Bearer|Basic)\s+\S+|\b(?:sk-ant-|sk-proj-|gh[oprsu]_|xox[baprs]-)[A-Za-z0-9_-]+|-----BEGIN [A-Z ]*PRIVATE KEY-----|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})/i;
const PRIVATE_OR_LOOPBACK = /(?:\blocalhost\b|\b0\.0\.0\.0\b|\b127(?:\.\d{1,3}){3}\b|\b10(?:\.\d{1,3}){3}\b|\b192\.168(?:\.\d{1,3}){2}\b|\b172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}\b|\b169\.254(?:\.\d{1,3}){2}\b|(?:^|[^a-f0-9])(?:::1|f[cd][a-f0-9]{2}:|fe80:))/i;
const RAW_ENDPOINT = /(?:\bcandidate:|\bps1_[A-Za-z0-9_-]{16,}|\b(?:wss?|https?|turns?|stun):\/\/[^\s]+:\d{2,5}\b)/i;

class WebRtcReadinessError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = "WebRtcReadinessError";
    this.code = code;
    this.retryable = details.retryable === true;
    this.details = Object.freeze(details.field ? { field: details.field } : {});
  }
}

function fail(code, message, field, retryable = false) {
  throw new WebRtcReadinessError(code, message, { field, retryable });
}

function isPlainObject(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function assertObject(value, field, allowed, required = allowed) {
  if (!isPlainObject(value)) {
    fail("WEBRTC_EVIDENCE_INVALID", `${field} must be an object`, field);
  }
  const allowedSet = new Set(allowed);
  const unknown = Object.keys(value).find((key) => !allowedSet.has(key));
  if (unknown) {
    fail(
      SENSITIVE_KEY.test(unknown) ? "WEBRTC_EVIDENCE_SENSITIVE" : "WEBRTC_EVIDENCE_INVALID",
      `${field} contains a forbidden field`,
      field,
    );
  }
  const missing = required.find((key) => !Object.prototype.hasOwnProperty.call(value, key));
  if (missing) {
    fail("WEBRTC_EVIDENCE_INVALID", `${field} is missing a required field`, `${field}.${missing}`);
  }
  return value;
}

function assertNoSensitiveString(value, field) {
  if (CREDENTIAL_VALUE.test(value)) {
    fail("WEBRTC_EVIDENCE_SENSITIVE", `${field} contains credential-like material`, field);
  }
  if (PRIVATE_OR_LOOPBACK.test(value)) {
    fail("WEBRTC_EVIDENCE_LEAKAGE", `${field} contains a private or loopback address`, field);
  }
  if (RAW_ENDPOINT.test(value)) {
    fail("WEBRTC_EVIDENCE_LEAKAGE", `${field} contains a raw endpoint or opaque session capability`, field);
  }
}

function safeToken(value, field, max = 160) {
  if (typeof value !== "string" || value.length > max || !SAFE_TOKEN.test(value)) {
    fail("WEBRTC_EVIDENCE_INVALID", `${field} is invalid`, field);
  }
  assertNoSensitiveString(value, field);
  return value;
}

function sha256(value, field) {
  if (typeof value !== "string" || !SHA256.test(value)) {
    fail("WEBRTC_EVIDENCE_INVALID", `${field} must be a lowercase SHA-256 digest`, field);
  }
  return value;
}

function gitRevision(value, field) {
  if (typeof value !== "string" || !GIT_REVISION.test(value)) {
    fail("WEBRTC_EVIDENCE_INVALID", `${field} must be an exact 40-character Git revision`, field);
  }
  return value;
}

function timestamp(value, field) {
  if (typeof value !== "string" || value.length > 40) {
    fail("WEBRTC_EVIDENCE_INVALID", `${field} must be a canonical ISO-8601 timestamp`, field);
  }
  const milliseconds = Date.parse(value);
  if (!Number.isFinite(milliseconds) || new Date(milliseconds).toISOString() !== value) {
    fail("WEBRTC_EVIDENCE_INVALID", `${field} must be a canonical ISO-8601 timestamp`, field);
  }
  return milliseconds;
}

function boundedInteger(value, field, { min = 0, max = 1_000_000_000 } = {}) {
  if (!Number.isSafeInteger(value) || value < min || value > max) {
    fail("WEBRTC_EVIDENCE_INVALID", `${field} must be a bounded integer`, field);
  }
  return value;
}

function exact(value, expected, field, message = `${field} does not meet the readiness requirement`) {
  if (value !== expected) fail("WEBRTC_EVIDENCE_NOT_READY", message, field, true);
  return value;
}

function validatePublicOrigin(value, field = "public_endpoint.origin") {
  if (typeof value !== "string" || value.length > 256) {
    fail("WEBRTC_EVIDENCE_INVALID", `${field} must be a canonical HTTPS origin`, field);
  }
  if (CREDENTIAL_VALUE.test(value)) {
    fail("WEBRTC_EVIDENCE_SENSITIVE", `${field} contains credential-like material`, field);
  }
  let parsed;
  try {
    parsed = new URL(value);
  } catch (_error) {
    fail("WEBRTC_EVIDENCE_INVALID", `${field} must be a canonical HTTPS origin`, field);
  }
  if (
    parsed.protocol !== "https:"
    || parsed.username
    || parsed.password
    || parsed.pathname !== "/"
    || parsed.search
    || parsed.hash
    || parsed.origin !== value
    || parsed.port
    || net.isIP(parsed.hostname)
    || !PUBLIC_DNS.test(parsed.hostname)
    || PRIVATE_DNS_SUFFIX.test(parsed.hostname)
  ) {
    fail("WEBRTC_EVIDENCE_LEAKAGE", `${field} must be a canonical public-DNS HTTPS origin without a raw port`, field);
  }
  return value;
}

function canonicalize(value, seen = new Set()) {
  if (value === null || typeof value === "boolean" || typeof value === "string") return JSON.stringify(value);
  if (typeof value === "number") {
    if (!Number.isFinite(value)) fail("WEBRTC_EVIDENCE_INVALID", "Evidence contains a non-finite number");
    return JSON.stringify(value);
  }
  if (typeof value !== "object" || value === undefined) {
    fail("WEBRTC_EVIDENCE_INVALID", "Evidence is not JSON serializable");
  }
  if (seen.has(value)) fail("WEBRTC_EVIDENCE_INVALID", "Evidence contains a cycle");
  seen.add(value);
  let serialized;
  if (Array.isArray(value)) {
    serialized = `[${value.map((entry) => canonicalize(entry, seen)).join(",")}]`;
  } else {
    serialized = `{${Object.keys(value).sort().map((key) => (
      `${JSON.stringify(key)}:${canonicalize(value[key], seen)}`
    )).join(",")}}`;
  }
  seen.delete(value);
  return serialized;
}

function digestJson(value) {
  return crypto.createHash("sha256").update(canonicalize(value)).digest("hex");
}

function validatePublicEndpoint(endpoint) {
  assertObject(endpoint, "public_endpoint", [
    "origin",
    "certificate_sha256",
    "wss_certificate_sha256",
    "https_status",
    "wss_status",
    "tls_version",
    "certificate_valid",
    "certificate_dns_match",
    "mixed_content",
    "wss_upgraded",
    "same_origin",
    "opaque_path",
  ]);
  validatePublicOrigin(endpoint.origin);
  sha256(endpoint.certificate_sha256, "public_endpoint.certificate_sha256");
  sha256(endpoint.wss_certificate_sha256, "public_endpoint.wss_certificate_sha256");
  if (endpoint.wss_certificate_sha256 !== endpoint.certificate_sha256) {
    fail("WEBRTC_EVIDENCE_NOT_READY", "HTTPS and WSS certificate fingerprints do not match", "public_endpoint.wss_certificate_sha256", true);
  }
  exact(endpoint.https_status, 200, "public_endpoint.https_status");
  exact(endpoint.wss_status, 101, "public_endpoint.wss_status");
  if (!TLS_VERSIONS.has(endpoint.tls_version)) {
    fail("WEBRTC_EVIDENCE_NOT_READY", "TLS 1.2 or newer is required", "public_endpoint.tls_version", true);
  }
  exact(endpoint.certificate_valid, true, "public_endpoint.certificate_valid");
  exact(endpoint.certificate_dns_match, true, "public_endpoint.certificate_dns_match");
  exact(endpoint.mixed_content, false, "public_endpoint.mixed_content");
  exact(endpoint.wss_upgraded, true, "public_endpoint.wss_upgraded");
  exact(endpoint.same_origin, true, "public_endpoint.same_origin");
  exact(endpoint.opaque_path, true, "public_endpoint.opaque_path");
}

function validateCirrus(cirrus) {
  assertObject(cirrus, "cirrus", [
    "player_signalling_reachable",
    "streamer_registered",
    "http_listener_loopback_only",
    "streamer_listener_loopback_only",
  ]);
  for (const field of Object.keys(cirrus)) exact(cirrus[field], true, `cirrus.${field}`);
}

function validateSecurity(security) {
  assertObject(security, "security", [
    "unauthenticated_denied",
    "cross_session_denied",
    "raw_control_ports_unreachable",
    "host_candidates_absent",
    "private_addresses_absent",
    "credentials_absent",
    "tokens_absent",
  ]);
  for (const field of Object.keys(security)) exact(security[field], true, `security.${field}`);
}

function validateCredential(credential, session, field) {
  assertObject(credential, field, ["scheme", "redacted", "issued_at", "expires_at", "ttl_seconds"]);
  exact(credential.scheme, "turn_rest_hmac", `${field}.scheme`);
  exact(credential.redacted, true, `${field}.redacted`);
  const issuedAt = timestamp(credential.issued_at, `${field}.issued_at`);
  const expiresAt = timestamp(credential.expires_at, `${field}.expires_at`);
  const ttlMs = expiresAt - issuedAt;
  if (ttlMs < MIN_TURN_CREDENTIAL_TTL_MS || ttlMs > MAX_TURN_CREDENTIAL_TTL_MS) {
    fail("WEBRTC_EVIDENCE_NOT_READY", "TURN credential TTL must be short-lived and bounded", `${field}.expires_at`, true);
  }
  boundedInteger(credential.ttl_seconds, `${field}.ttl_seconds`, { min: 300, max: 86_400 });
  if (credential.ttl_seconds !== ttlMs / 1000) {
    fail("WEBRTC_EVIDENCE_INVALID", "TURN credential TTL does not match its timestamps", `${field}.ttl_seconds`);
  }
  if (issuedAt > session.startedAt || expiresAt <= session.completedAt) {
    fail("WEBRTC_EVIDENCE_NOT_READY", "TURN credential was not valid for the full external session", field, true);
  }
}

function validateCandidate(candidate, turnTransport, field) {
  assertObject(candidate, field, [
    "type",
    "protocol",
    "turn_transport",
    "candidate_fingerprint",
    "turn_endpoint_matched",
    "address_redacted",
  ]);
  exact(candidate.type, "relay", `${field}.type`);
  const expectedProtocol = turnTransport === "udp" ? "udp" : "tcp";
  exact(candidate.protocol, expectedProtocol, `${field}.protocol`);
  exact(candidate.turn_transport, turnTransport, `${field}.turn_transport`);
  sha256(candidate.candidate_fingerprint, `${field}.candidate_fingerprint`);
  exact(candidate.turn_endpoint_matched, true, `${field}.turn_endpoint_matched`);
  exact(candidate.address_redacted, true, `${field}.address_redacted`);
}

function validateMedia(media, elapsedMs, field) {
  assertObject(media, field, ["decoded_frames", "first_frame_ms", "sustained_seconds"]);
  boundedInteger(media.decoded_frames, `${field}.decoded_frames`, { min: 1 });
  boundedInteger(media.first_frame_ms, `${field}.first_frame_ms`, { min: 0, max: 60_000 });
  boundedInteger(media.sustained_seconds, `${field}.sustained_seconds`, { min: 720, max: 86_400 });
  if (media.sustained_seconds * 1000 > elapsedMs + 5_000) {
    fail("WEBRTC_EVIDENCE_INVALID", "Media duration exceeds the external session duration", `${field}.sustained_seconds`);
  }
}

function validateInputRoundTrip(input, field) {
  assertObject(input, field, ["data_channel_open", "messages_sent", "messages_acknowledged", "max_round_trip_ms"]);
  exact(input.data_channel_open, true, `${field}.data_channel_open`);
  boundedInteger(input.messages_sent, `${field}.messages_sent`, { min: 1, max: 1_000_000 });
  boundedInteger(input.messages_acknowledged, `${field}.messages_acknowledged`, { min: 1, max: 1_000_000 });
  if (input.messages_acknowledged !== input.messages_sent) {
    fail("WEBRTC_EVIDENCE_NOT_READY", "Every recorded input probe must be acknowledged", `${field}.messages_acknowledged`, true);
  }
  boundedInteger(input.max_round_trip_ms, `${field}.max_round_trip_ms`, { min: 0, max: 5_000 });
}

function validateReconnect(reconnect, field) {
  assertObject(reconnect, field, ["attempted", "successful", "recovery_ms"]);
  exact(reconnect.attempted, true, `${field}.attempted`);
  exact(reconnect.successful, true, `${field}.successful`);
  boundedInteger(reconnect.recovery_ms, `${field}.recovery_ms`, { min: 0, max: 60_000 });
}

function validateLeakage(leakage, field) {
  assertObject(leakage, field, [
    "host_candidate_exposed",
    "private_address_exposed",
    "raw_port_exposed",
    "credential_exposed",
    "token_exposed",
  ]);
  for (const name of Object.keys(leakage)) exact(leakage[name], false, `${field}.${name}`);
}

function validateExternalSession(session, index, recordedAt) {
  const field = `sessions[${index}]`;
  assertObject(session, field, [
    "test_id",
    "network_fingerprint",
    "network_class",
    "turn_transport",
    "ice_policy",
    "ice_gathering_state",
    "started_at",
    "completed_at",
    "credential",
    "selected_candidate",
    "media",
    "input",
    "reconnect",
    "leakage",
  ]);
  safeToken(session.test_id, `${field}.test_id`);
  sha256(session.network_fingerprint, `${field}.network_fingerprint`);
  if (!NETWORK_CLASSES.has(session.network_class)) {
    fail("WEBRTC_EVIDENCE_INVALID", "External network class is invalid", `${field}.network_class`);
  }
  if (!TURN_TRANSPORTS.has(session.turn_transport)) {
    fail("WEBRTC_EVIDENCE_INVALID", "TURN transport must be udp, tcp, or tls", `${field}.turn_transport`);
  }
  exact(session.ice_policy, "relay", `${field}.ice_policy`);
  exact(session.ice_gathering_state, "complete", `${field}.ice_gathering_state`);
  const startedAt = timestamp(session.started_at, `${field}.started_at`);
  const completedAt = timestamp(session.completed_at, `${field}.completed_at`);
  const elapsedMs = completedAt - startedAt;
  if (elapsedMs < MIN_EXTERNAL_SESSION_MS || elapsedMs > MAX_RECEIPT_TTL_MS) {
    fail("WEBRTC_EVIDENCE_NOT_READY", "External media/input sessions must run for at least 12 minutes", `${field}.completed_at`, true);
  }
  if (completedAt > recordedAt + MAX_CLOCK_SKEW_MS || recordedAt - startedAt > MAX_RECEIPT_TTL_MS) {
    fail("WEBRTC_EVIDENCE_INVALID", "External session timestamps are outside the receipt evidence window", `${field}.completed_at`);
  }
  const temporal = { startedAt, completedAt };
  validateCredential(session.credential, temporal, `${field}.credential`);
  validateCandidate(session.selected_candidate, session.turn_transport, `${field}.selected_candidate`);
  validateMedia(session.media, elapsedMs, `${field}.media`);
  validateInputRoundTrip(session.input, `${field}.input`);
  validateReconnect(session.reconnect, `${field}.reconnect`);
  validateLeakage(session.leakage, `${field}.leakage`);
  return { elapsedMs };
}

function validateMatrix(sessions) {
  if (!Array.isArray(sessions) || sessions.length < 6 || sessions.length > 24) {
    fail("WEBRTC_EVIDENCE_NOT_READY", "At least six bounded external TURN sessions are required", "probe.sessions", true);
  }
  const networks = new Map();
  const testIds = new Set();
  for (const [index, session] of sessions.entries()) {
    if (testIds.has(session.test_id)) {
      fail("WEBRTC_EVIDENCE_INVALID", "External session test_id values must be unique", `sessions[${index}].test_id`);
    }
    testIds.add(session.test_id);
    const current = networks.get(session.network_fingerprint) || {
      networkClass: session.network_class,
      transports: new Set(),
    };
    if (current.networkClass !== session.network_class) {
      fail("WEBRTC_EVIDENCE_INVALID", "A network fingerprint maps to multiple network classes", `sessions[${index}].network_class`);
    }
    if (current.transports.has(session.turn_transport)) {
      fail("WEBRTC_EVIDENCE_INVALID", "A network/transport evidence pair is duplicated", `sessions[${index}].turn_transport`);
    }
    current.transports.add(session.turn_transport);
    networks.set(session.network_fingerprint, current);
  }
  if (networks.size < 2 || new Set([...networks.values()].map((item) => item.networkClass)).size < 2) {
    fail("WEBRTC_EVIDENCE_NOT_READY", "At least two distinct external network classes are required", "probe.sessions", true);
  }
  for (const { transports } of networks.values()) {
    for (const required of REQUIRED_TURN_TRANSPORTS) {
      if (!transports.has(required)) {
        fail("WEBRTC_EVIDENCE_NOT_READY", "Each external network must cover UDP, TCP, and TLS TURN", "probe.sessions", true);
      }
    }
  }
  return networks.size;
}

function validateWebRtcProbeResults(probe) {
  assertObject(probe, "probe", [
    "schema",
    "probe_id",
    "build_revision",
    "deployment_fingerprint",
    "recorded_at",
    "expires_at",
    "public_endpoint",
    "cirrus",
    "security",
    "sessions",
  ]);
  if (probe.schema !== WEBRTC_PROBE_RESULTS_SCHEMA) {
    fail("WEBRTC_EVIDENCE_INVALID", "Unsupported WebRTC probe-results schema", "probe.schema");
  }
  safeToken(probe.probe_id, "probe.probe_id");
  gitRevision(probe.build_revision, "probe.build_revision");
  sha256(probe.deployment_fingerprint, "probe.deployment_fingerprint");
  const recordedAt = timestamp(probe.recorded_at, "probe.recorded_at");
  const expiresAt = timestamp(probe.expires_at, "probe.expires_at");
  if (expiresAt <= recordedAt || expiresAt - recordedAt > MAX_RECEIPT_TTL_MS) {
    fail("WEBRTC_EVIDENCE_INVALID", "Probe expiry must be after recording and no more than 24 hours later", "probe.expires_at");
  }
  validatePublicEndpoint(probe.public_endpoint);
  validateCirrus(probe.cirrus);
  validateSecurity(probe.security);
  if (!Array.isArray(probe.sessions)) {
    fail("WEBRTC_EVIDENCE_INVALID", "probe.sessions must be an array", "probe.sessions");
  }
  if (probe.sessions.length < 6 || probe.sessions.length > 24) {
    fail("WEBRTC_EVIDENCE_NOT_READY", "At least six bounded external TURN sessions are required", "probe.sessions", true);
  }
  const sessionMeta = probe.sessions.map((session, index) => validateExternalSession(session, index, recordedAt));
  validateMatrix(probe.sessions);
  return Object.freeze({
    recordedAt,
    expiresAt,
    minimumSessionMs: Math.min(...sessionMeta.map((item) => item.elapsedMs)),
  });
}

function readNow(now) {
  const value = typeof now === "function" ? now() : now;
  const milliseconds = value instanceof Date ? value.getTime() : Number(value);
  if (!Number.isFinite(milliseconds)) {
    fail("WEBRTC_EVIDENCE_INVALID", "Verification time is invalid", "now");
  }
  return milliseconds;
}

function validateExpectations(options, { nowRequired = false } = {}) {
  assertObject(
    options,
    "expectations",
    ["buildRevision", "deploymentFingerprint", "publicOrigin", "certificateSha256", "now"],
    ["buildRevision", "deploymentFingerprint", "publicOrigin"],
  );
  gitRevision(options.buildRevision, "expectations.buildRevision");
  sha256(options.deploymentFingerprint, "expectations.deploymentFingerprint");
  validatePublicOrigin(options.publicOrigin, "expectations.publicOrigin");
  if (options.certificateSha256 !== undefined) {
    sha256(options.certificateSha256, "expectations.certificateSha256");
  }
  if (nowRequired && options.now === undefined) {
    fail("WEBRTC_EVIDENCE_INVALID", "Verification time is required", "expectations.now");
  }
}

function enforceExpectations(value, options, recordedAt, expiresAt) {
  const expected = [
    ["build_revision", options.buildRevision],
    ["deployment_fingerprint", options.deploymentFingerprint],
  ];
  for (const [field, expectation] of expected) {
    if (value[field] !== expectation) {
      fail("WEBRTC_EVIDENCE_MISMATCH", `${field} does not match the running deployment`, field);
    }
  }
  if (value.public_endpoint.origin !== options.publicOrigin) {
    fail("WEBRTC_EVIDENCE_MISMATCH", "Public origin does not match the running deployment", "public_endpoint.origin");
  }
  if (
    options.certificateSha256 !== undefined
    && value.public_endpoint.certificate_sha256 !== options.certificateSha256
  ) {
    fail("WEBRTC_EVIDENCE_MISMATCH", "Certificate fingerprint does not match the deployment expectation", "public_endpoint.certificate_sha256");
  }
  const nowMs = readNow(options.now === undefined ? Date.now() : options.now);
  if (recordedAt > nowMs + MAX_CLOCK_SKEW_MS) {
    fail("WEBRTC_EVIDENCE_NOT_YET_VALID", "WebRTC evidence timestamp is in the future", "recorded_at");
  }
  if (expiresAt <= nowMs) {
    fail("WEBRTC_EVIDENCE_EXPIRED", "WebRTC evidence has expired", "expires_at", true);
  }
}

function verifyWebRtcProbeResults(probe, options = {}) {
  validateExpectations(options);
  const timing = validateWebRtcProbeResults(probe);
  enforceExpectations(probe, options, timing.recordedAt, timing.expiresAt);
  return probe;
}

function createWebRtcReadinessReceipt(probe, options = {}) {
  verifyWebRtcProbeResults(probe, options);
  const networks = new Set(probe.sessions.map((session) => session.network_fingerprint));
  const receipt = {
    schema: WEBRTC_READINESS_RECEIPT_SCHEMA,
    receipt_id: probe.probe_id,
    outcome: "ready",
    build_revision: probe.build_revision,
    deployment_fingerprint: probe.deployment_fingerprint,
    recorded_at: probe.recorded_at,
    expires_at: probe.expires_at,
    public_endpoint: {
      origin: probe.public_endpoint.origin,
      certificate_sha256: probe.public_endpoint.certificate_sha256,
      tls_version: probe.public_endpoint.tls_version,
      https_status: 200,
      wss_status: 101,
    },
    transport: {
      cirrus_reachable: true,
      streamer_registered: true,
      forced_relay: true,
      networks_tested: networks.size,
      sessions_tested: probe.sessions.length,
      turn_transports: [...REQUIRED_TURN_TRANSPORTS],
      minimum_session_seconds: Math.floor(Math.min(...probe.sessions.map((session) => (
        Date.parse(session.completed_at) - Date.parse(session.started_at)
      ))) / 1000),
      max_input_round_trip_ms: Math.max(...probe.sessions.map((session) => session.input.max_round_trip_ms)),
    },
    security: {
      no_mixed_content: true,
      no_host_candidates: true,
      no_private_addresses: true,
      no_raw_ports: true,
      no_credentials: true,
      no_tokens: true,
      unauthenticated_denied: true,
      cross_session_denied: true,
    },
    evidence: {
      probe_schema: WEBRTC_PROBE_RESULTS_SCHEMA,
      probe_id: probe.probe_id,
      probe_sha256: digestJson(probe),
    },
  };
  validateWebRtcReadinessReceipt(receipt);
  return Object.freeze(receipt);
}

function validateWebRtcReadinessReceipt(receipt) {
  assertObject(receipt, "receipt", [
    "schema",
    "receipt_id",
    "outcome",
    "build_revision",
    "deployment_fingerprint",
    "recorded_at",
    "expires_at",
    "public_endpoint",
    "transport",
    "security",
    "evidence",
  ]);
  if (receipt.schema !== WEBRTC_READINESS_RECEIPT_SCHEMA) {
    fail("WEBRTC_EVIDENCE_INVALID", "Unsupported WebRTC readiness receipt schema", "receipt.schema");
  }
  safeToken(receipt.receipt_id, "receipt.receipt_id");
  exact(receipt.outcome, "ready", "receipt.outcome");
  gitRevision(receipt.build_revision, "receipt.build_revision");
  sha256(receipt.deployment_fingerprint, "receipt.deployment_fingerprint");
  const recordedAt = timestamp(receipt.recorded_at, "receipt.recorded_at");
  const expiresAt = timestamp(receipt.expires_at, "receipt.expires_at");
  if (expiresAt <= recordedAt || expiresAt - recordedAt > MAX_RECEIPT_TTL_MS) {
    fail("WEBRTC_EVIDENCE_INVALID", "Receipt expiry must be after recording and no more than 24 hours later", "receipt.expires_at");
  }
  assertObject(receipt.public_endpoint, "receipt.public_endpoint", [
    "origin", "certificate_sha256", "tls_version", "https_status", "wss_status",
  ]);
  validatePublicOrigin(receipt.public_endpoint.origin, "receipt.public_endpoint.origin");
  sha256(receipt.public_endpoint.certificate_sha256, "receipt.public_endpoint.certificate_sha256");
  if (!TLS_VERSIONS.has(receipt.public_endpoint.tls_version)) {
    fail("WEBRTC_EVIDENCE_NOT_READY", "Receipt TLS version is not production-ready", "receipt.public_endpoint.tls_version", true);
  }
  exact(receipt.public_endpoint.https_status, 200, "receipt.public_endpoint.https_status");
  exact(receipt.public_endpoint.wss_status, 101, "receipt.public_endpoint.wss_status");

  assertObject(receipt.transport, "receipt.transport", [
    "cirrus_reachable",
    "streamer_registered",
    "forced_relay",
    "networks_tested",
    "sessions_tested",
    "turn_transports",
    "minimum_session_seconds",
    "max_input_round_trip_ms",
  ]);
  for (const field of ["cirrus_reachable", "streamer_registered", "forced_relay"]) {
    exact(receipt.transport[field], true, `receipt.transport.${field}`);
  }
  boundedInteger(receipt.transport.networks_tested, "receipt.transport.networks_tested", { min: 2, max: 8 });
  boundedInteger(receipt.transport.sessions_tested, "receipt.transport.sessions_tested", { min: 6, max: 24 });
  if (receipt.transport.sessions_tested !== receipt.transport.networks_tested * REQUIRED_TURN_TRANSPORTS.length) {
    fail("WEBRTC_EVIDENCE_INVALID", "Receipt session count does not match its network/transport matrix", "receipt.transport.sessions_tested");
  }
  if (
    !Array.isArray(receipt.transport.turn_transports)
    || receipt.transport.turn_transports.length !== REQUIRED_TURN_TRANSPORTS.length
    || receipt.transport.turn_transports.some((value, index) => value !== REQUIRED_TURN_TRANSPORTS[index])
  ) {
    fail("WEBRTC_EVIDENCE_NOT_READY", "Receipt must cover TCP, TLS, and UDP TURN", "receipt.transport.turn_transports", true);
  }
  boundedInteger(receipt.transport.minimum_session_seconds, "receipt.transport.minimum_session_seconds", { min: 720, max: 86_400 });
  boundedInteger(receipt.transport.max_input_round_trip_ms, "receipt.transport.max_input_round_trip_ms", { min: 0, max: 5_000 });

  assertObject(receipt.security, "receipt.security", [
    "no_mixed_content",
    "no_host_candidates",
    "no_private_addresses",
    "no_raw_ports",
    "no_credentials",
    "no_tokens",
    "unauthenticated_denied",
    "cross_session_denied",
  ]);
  for (const field of Object.keys(receipt.security)) exact(receipt.security[field], true, `receipt.security.${field}`);

  assertObject(receipt.evidence, "receipt.evidence", ["probe_schema", "probe_id", "probe_sha256"]);
  exact(receipt.evidence.probe_schema, WEBRTC_PROBE_RESULTS_SCHEMA, "receipt.evidence.probe_schema");
  safeToken(receipt.evidence.probe_id, "receipt.evidence.probe_id");
  if (receipt.evidence.probe_id !== receipt.receipt_id) {
    fail("WEBRTC_EVIDENCE_INVALID", "Receipt and probe identifiers do not match", "receipt.evidence.probe_id");
  }
  sha256(receipt.evidence.probe_sha256, "receipt.evidence.probe_sha256");
  return Object.freeze({ recordedAt, expiresAt });
}

function verifyWebRtcReadinessReceipt(receipt, options = {}) {
  validateExpectations(options);
  const timing = validateWebRtcReadinessReceipt(receipt);
  enforceExpectations(receipt, options, timing.recordedAt, timing.expiresAt);
  return receipt;
}

function validateFilePath(file, code) {
  if (typeof file !== "string" || !file.trim() || file.includes("\0")) {
    fail(code, "WebRTC evidence path is invalid", "path");
  }
  return path.resolve(file);
}

function writeWebRtcReadinessReceiptAtomic(file, receipt, { fsImpl = fs } = {}) {
  validateWebRtcReadinessReceipt(receipt);
  const target = validateFilePath(file, "WEBRTC_RECEIPT_PATH_INVALID");
  const directory = path.dirname(target);
  fsImpl.mkdirSync(directory, { recursive: true, mode: 0o700 });
  const temporary = path.join(directory, `.${path.basename(target)}.${process.pid}.${crypto.randomBytes(8).toString("hex")}.tmp`);
  let descriptor;
  try {
    descriptor = fsImpl.openSync(temporary, "wx", 0o600);
    fsImpl.writeFileSync(descriptor, `${JSON.stringify(receipt, null, 2)}\n`, "utf8");
    if (typeof fsImpl.fsyncSync === "function") fsImpl.fsyncSync(descriptor);
    fsImpl.closeSync(descriptor);
    descriptor = undefined;
    fsImpl.renameSync(temporary, target);
    if (typeof fsImpl.chmodSync === "function") fsImpl.chmodSync(target, 0o600);
    return target;
  } catch (_cause) {
    try { if (descriptor !== undefined) fsImpl.closeSync(descriptor); } catch (_error) {}
    try { fsImpl.rmSync(temporary, { force: true }); } catch (_error) {}
    fail("WEBRTC_RECEIPT_WRITE_FAILED", "WebRTC readiness receipt could not be saved atomically", "path", true);
  }
}

function parseBoundedJsonFile(file, { fsImpl = fs, maxBytes, unavailableCode, invalidCode } = {}) {
  const target = validateFilePath(file, invalidCode);
  let bytes;
  let descriptor;
  try {
    if (typeof fsImpl.openSync === "function" && typeof fsImpl.fstatSync === "function"
        && typeof fsImpl.closeSync === "function") {
      descriptor = fsImpl.openSync(target, fs.constants.O_RDONLY | Number(fs.constants.O_NOFOLLOW || 0));
      const before = fsImpl.fstatSync(descriptor);
      if (!before.isFile() || before.size < 2 || before.size > maxBytes) {
        fail(invalidCode, "WebRTC evidence file has an invalid size", "path");
      }
      bytes = fsImpl.readFileSync(descriptor);
      const after = fsImpl.fstatSync(descriptor);
      if (after.size !== before.size || after.mtimeMs !== before.mtimeMs || after.ctimeMs !== before.ctimeMs) {
        fail(invalidCode, "WebRTC evidence file changed while it was being read", "path");
      }
    } else {
      bytes = fsImpl.readFileSync(target);
    }
  } catch (_cause) {
    if (_cause instanceof WebRtcReadinessError) throw _cause;
    fail(unavailableCode, "WebRTC evidence file is unavailable", "path", true);
  } finally {
    try { if (descriptor !== undefined) fsImpl.closeSync(descriptor); } catch (_error) {}
  }
  if (!Buffer.isBuffer(bytes)) bytes = Buffer.from(String(bytes));
  if (bytes.length < 2 || bytes.length > maxBytes) {
    fail(invalidCode, "WebRTC evidence file has an invalid size", "path");
  }
  try {
    return JSON.parse(bytes.toString("utf8"));
  } catch (_cause) {
    fail(invalidCode, "WebRTC evidence file is not valid JSON", "path");
  }
}

function readWebRtcProbeResults(file, options = {}) {
  const probe = parseBoundedJsonFile(file, {
    ...options,
    maxBytes: MAX_PROBE_BYTES,
    unavailableCode: "WEBRTC_PROBE_UNAVAILABLE",
    invalidCode: "WEBRTC_EVIDENCE_INVALID",
  });
  validateWebRtcProbeResults(probe);
  return probe;
}

function readWebRtcReadinessReceipt(file, options = {}) {
  const receipt = parseBoundedJsonFile(file, {
    ...options,
    maxBytes: MAX_RECEIPT_BYTES,
    unavailableCode: "WEBRTC_RECEIPT_UNAVAILABLE",
    invalidCode: "WEBRTC_EVIDENCE_INVALID",
  });
  validateWebRtcReadinessReceipt(receipt);
  return receipt;
}

module.exports = {
  MAX_PROBE_BYTES,
  MAX_RECEIPT_TTL_MS,
  MIN_EXTERNAL_SESSION_MS,
  REQUIRED_TURN_TRANSPORTS,
  WEBRTC_PROBE_RESULTS_SCHEMA,
  WEBRTC_READINESS_RECEIPT_SCHEMA,
  WebRtcReadinessError,
  createWebRtcReadinessReceipt,
  digestJson,
  readWebRtcProbeResults,
  readWebRtcReadinessReceipt,
  validateWebRtcProbeResults,
  validateWebRtcReadinessReceipt,
  verifyWebRtcProbeResults,
  verifyWebRtcReadinessReceipt,
  writeWebRtcReadinessReceiptAtomic,
};

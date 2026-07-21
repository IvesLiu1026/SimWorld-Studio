"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { runCli } = require("../webrtc-readiness-cli");
const {
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
} = require("../webrtc-readiness-receipt");

const NOW = Date.parse("2026-07-21T04:00:00.000Z");
const BUILD_REVISION = "a".repeat(40);
const DEPLOYMENT_FINGERPRINT = "b".repeat(64);
const CERTIFICATE_FINGERPRINT = "c".repeat(64);
const PUBLIC_ORIGIN = "https://studio.example.edu";

function fingerprint(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
}

function session(network, networkClass, turnTransport, ordinal) {
  const startedAt = NOW - 15 * 60 * 1000;
  const completedAt = startedAt + 12 * 60 * 1000;
  const credentialIssuedAt = startedAt - 5 * 60 * 1000;
  const credentialExpiresAt = credentialIssuedAt + 2 * 60 * 60 * 1000;
  return {
    test_id: `${networkClass}-${turnTransport}-${ordinal}`,
    network_fingerprint: fingerprint(network),
    network_class: networkClass,
    turn_transport: turnTransport,
    ice_policy: "relay",
    ice_gathering_state: "complete",
    started_at: new Date(startedAt).toISOString(),
    completed_at: new Date(completedAt).toISOString(),
    credential: {
      scheme: "turn_rest_hmac",
      redacted: true,
      issued_at: new Date(credentialIssuedAt).toISOString(),
      expires_at: new Date(credentialExpiresAt).toISOString(),
      ttl_seconds: 7200,
    },
    selected_candidate: {
      type: "relay",
      protocol: turnTransport === "udp" ? "udp" : "tcp",
      turn_transport: turnTransport,
      candidate_fingerprint: fingerprint(`${network}:${turnTransport}:candidate`),
      turn_endpoint_matched: true,
      address_redacted: true,
    },
    media: {
      decoded_frames: 43_200,
      first_frame_ms: 850,
      sustained_seconds: 720,
    },
    input: {
      data_channel_open: true,
      messages_sent: 12,
      messages_acknowledged: 12,
      max_round_trip_ms: 93 + ordinal,
    },
    reconnect: {
      attempted: true,
      successful: true,
      recovery_ms: 1_200 + ordinal,
    },
    leakage: {
      host_candidate_exposed: false,
      private_address_exposed: false,
      raw_port_exposed: false,
      credential_exposed: false,
      token_exposed: false,
    },
  };
}

function probeFixture() {
  const sessions = [];
  let ordinal = 0;
  for (const [network, networkClass] of [
    ["external-network-a", "residential_nat"],
    ["external-network-b", "mobile_hotspot"],
  ]) {
    for (const transport of ["udp", "tcp", "tls"]) {
      sessions.push(session(network, networkClass, transport, ordinal));
      ordinal += 1;
    }
  }
  return {
    schema: "simworld-webrtc-probe-results/v1",
    probe_id: "webrtc-prod-20260721-001",
    build_revision: BUILD_REVISION,
    deployment_fingerprint: DEPLOYMENT_FINGERPRINT,
    recorded_at: new Date(NOW).toISOString(),
    expires_at: new Date(NOW + 60 * 60 * 1000).toISOString(),
    public_endpoint: {
      origin: PUBLIC_ORIGIN,
      certificate_sha256: CERTIFICATE_FINGERPRINT,
      wss_certificate_sha256: CERTIFICATE_FINGERPRINT,
      https_status: 200,
      wss_status: 101,
      tls_version: "TLSv1.3",
      certificate_valid: true,
      certificate_dns_match: true,
      mixed_content: false,
      wss_upgraded: true,
      same_origin: true,
      opaque_path: true,
    },
    cirrus: {
      player_signalling_reachable: true,
      streamer_registered: true,
      http_listener_loopback_only: true,
      streamer_listener_loopback_only: true,
    },
    security: {
      unauthenticated_denied: true,
      cross_session_denied: true,
      raw_control_ports_unreachable: true,
      host_candidates_absent: true,
      private_addresses_absent: true,
      credentials_absent: true,
      tokens_absent: true,
    },
    sessions,
  };
}

function expectations(overrides = {}) {
  return {
    buildRevision: BUILD_REVISION,
    deploymentFingerprint: DEPLOYMENT_FINGERPRINT,
    publicOrigin: PUBLIC_ORIGIN,
    certificateSha256: CERTIFICATE_FINGERPRINT,
    now: NOW + 1,
    ...overrides,
  };
}

function clone(value) {
  return structuredClone(value);
}

test("a complete two-network forced-relay matrix creates a compact deployment-bound receipt", () => {
  const probe = probeFixture();
  const timing = validateWebRtcProbeResults(probe);
  assert.equal(timing.minimumSessionMs, 12 * 60 * 1000);
  assert.equal(verifyWebRtcProbeResults(probe, expectations()), probe);

  const receipt = createWebRtcReadinessReceipt(probe, expectations());
  assert.equal(receipt.schema, "simworld-webrtc-readiness-receipt/v1");
  assert.equal(receipt.outcome, "ready");
  assert.equal(receipt.build_revision, BUILD_REVISION);
  assert.equal(receipt.deployment_fingerprint, DEPLOYMENT_FINGERPRINT);
  assert.equal(receipt.transport.networks_tested, 2);
  assert.equal(receipt.transport.sessions_tested, 6);
  assert.deepEqual(receipt.transport.turn_transports, ["tcp", "tls", "udp"]);
  assert.equal(receipt.transport.minimum_session_seconds, 720);
  assert.equal(receipt.transport.forced_relay, true);
  assert.equal(receipt.security.no_raw_ports, true);
  assert.equal(receipt.evidence.probe_sha256, digestJson(probe));

  const serialized = JSON.stringify(receipt);
  assert.doesNotMatch(serialized, /"selected_candidate"|"credential"|"network_fingerprint"|"started_at"|"completed_at"|ps1_/i);
  assert.equal(verifyWebRtcReadinessReceipt(receipt, expectations()), receipt);
});

test("HTTPS, WSS, Cirrus, ICE, media, input, reconnect, and isolation evidence all fail closed", () => {
  const mutations = [
    (probe) => { probe.public_endpoint.https_status = 503; },
    (probe) => { probe.public_endpoint.wss_upgraded = false; },
    (probe) => { probe.public_endpoint.wss_certificate_sha256 = "d".repeat(64); },
    (probe) => { probe.cirrus.player_signalling_reachable = false; },
    (probe) => { probe.cirrus.streamer_registered = false; },
    (probe) => { probe.security.cross_session_denied = false; },
    (probe) => { probe.security.raw_control_ports_unreachable = false; },
    (probe) => { probe.sessions[0].ice_gathering_state = "gathering"; },
    (probe) => { probe.sessions[0].selected_candidate.type = "host"; },
    (probe) => { probe.sessions[0].media.decoded_frames = 0; },
    (probe) => { probe.sessions[0].input.data_channel_open = false; },
    (probe) => { probe.sessions[0].input.messages_acknowledged = 11; },
    (probe) => { probe.sessions[0].reconnect.successful = false; },
    (probe) => { probe.sessions[0].leakage.host_candidate_exposed = true; },
  ];
  for (const mutate of mutations) {
    const probe = probeFixture();
    mutate(probe);
    assert.throws(
      () => createWebRtcReadinessReceipt(probe, expectations()),
      (error) => error instanceof WebRtcReadinessError,
    );
  }
});

test("the verifier requires every TURN transport on two distinct external network classes", () => {
  const incomplete = probeFixture();
  incomplete.sessions.pop();
  assert.throws(
    () => validateWebRtcProbeResults(incomplete),
    (error) => error.code === "WEBRTC_EVIDENCE_NOT_READY" && error.details.field === "probe.sessions",
  );

  const duplicate = probeFixture();
  duplicate.sessions[5] = clone(duplicate.sessions[4]);
  duplicate.sessions[5].test_id = "mobile-hotspot-duplicate";
  assert.throws(
    () => validateWebRtcProbeResults(duplicate),
    (error) => error.code === "WEBRTC_EVIDENCE_INVALID" && /turn_transport/.test(error.details.field),
  );

  const oneClass = probeFixture();
  for (const item of oneClass.sessions.slice(3)) item.network_class = "residential_nat";
  assert.throws(
    () => validateWebRtcProbeResults(oneClass),
    (error) => error.code === "WEBRTC_EVIDENCE_NOT_READY" && error.details.field === "probe.sessions",
  );
});

test("TURN credentials must remain redacted, short lived, and valid for the full probe", () => {
  const cases = [
    (probe) => { probe.sessions[0].credential.redacted = false; },
    (probe) => { probe.sessions[0].credential.expires_at = probe.sessions[0].started_at; },
    (probe) => { probe.sessions[0].credential.ttl_seconds = 7199; },
    (probe) => { probe.sessions[0].credential.username = "1720000000:session"; },
  ];
  for (const mutate of cases) {
    const probe = probeFixture();
    mutate(probe);
    assert.throws(() => validateWebRtcProbeResults(probe), WebRtcReadinessError);
  }
});

test("host, private address, raw endpoint, raw port, and token fields are rejected rather than redacted", () => {
  const sensitiveMutations = [
    (probe) => { probe.sessions[0].selected_candidate.address = "10.0.0.8:49160"; },
    (probe) => { probe.sessions[0].selected_candidate.raw_port = 49160; },
    (probe) => { probe.sessions[0].session_token = "must-never-be-stored"; },
    (probe) => { probe.sessions[0].credential.password = "must-never-be-stored"; },
  ];
  for (const mutate of sensitiveMutations) {
    const probe = probeFixture();
    mutate(probe);
    assert.throws(
      () => validateWebRtcProbeResults(probe),
      (error) => error.code === "WEBRTC_EVIDENCE_SENSITIVE",
    );
  }

  const secretAsUnknownKey = probeFixture();
  secretAsUnknownKey["sk-ant-must-never-be-echoed"] = true;
  assert.throws(
    () => validateWebRtcProbeResults(secretAsUnknownKey),
    (error) => {
      assert.doesNotMatch(JSON.stringify({
        code: error.code,
        message: error.message,
        details: error.details,
      }), /must-never-be-echoed/);
      return true;
    },
  );

  for (const testId of [
    "sk-ant-must-never-be-stored",
    "wss://public.example.edu:8500/pixel-stream/session/ps1_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
  ]) {
    const probe = probeFixture();
    probe.sessions[0].test_id = testId;
    assert.throws(
      () => validateWebRtcProbeResults(probe),
      (error) => ["WEBRTC_EVIDENCE_SENSITIVE", "WEBRTC_EVIDENCE_LEAKAGE"].includes(error.code),
    );
  }

  for (const origin of [
    "http://studio.example.edu",
    "https://127.0.0.1",
    "https://studio.internal",
    "https://studio.example.edu:8500",
    "https://user:password@studio.example.edu",
  ]) {
    const probe = probeFixture();
    probe.public_endpoint.origin = origin;
    assert.throws(
      () => validateWebRtcProbeResults(probe),
      (error) => error.code === "WEBRTC_EVIDENCE_LEAKAGE",
    );
  }
});

test("receipt creation and verification bind current expiry, deployment fingerprint, build, origin, and certificate", () => {
  const probe = probeFixture();
  for (const option of [
    { buildRevision: "d".repeat(40) },
    { deploymentFingerprint: "d".repeat(64) },
    { publicOrigin: "https://other.example.edu" },
    { certificateSha256: "d".repeat(64) },
  ]) {
    assert.throws(
      () => createWebRtcReadinessReceipt(probe, expectations(option)),
      (error) => error.code === "WEBRTC_EVIDENCE_MISMATCH",
    );
  }
  assert.throws(
    () => createWebRtcReadinessReceipt(probe, expectations({ now: NOW + 60 * 60 * 1000 })),
    (error) => error.code === "WEBRTC_EVIDENCE_EXPIRED",
  );
  assert.throws(
    () => createWebRtcReadinessReceipt(probe, expectations({ now: NOW - 10 * 60 * 1000 })),
    (error) => error.code === "WEBRTC_EVIDENCE_NOT_YET_VALID",
  );

  const receipt = createWebRtcReadinessReceipt(probe, expectations());
  assert.throws(
    () => verifyWebRtcReadinessReceipt(receipt, expectations({ now: NOW + 60 * 60 * 1000 })),
    (error) => error.code === "WEBRTC_EVIDENCE_EXPIRED",
  );
  const inconsistent = clone(receipt);
  inconsistent.transport.networks_tested = 3;
  assert.throws(
    () => validateWebRtcReadinessReceipt(inconsistent),
    (error) => error.code === "WEBRTC_EVIDENCE_INVALID" && /sessions_tested/.test(error.details.field),
  );
  assert.throws(
    () => verifyWebRtcReadinessReceipt(receipt, {}),
    (error) => error.code === "WEBRTC_EVIDENCE_INVALID" && /buildRevision/.test(error.details.field),
  );
});

test("probe digest is deterministic while invalid JSON values are rejected", () => {
  assert.equal(digestJson({ b: 2, a: { z: true, x: null } }), digestJson({ a: { x: null, z: true }, b: 2 }));
  assert.throws(() => digestJson({ invalid: undefined }), /JSON serializable/);
  const cycle = {};
  cycle.self = cycle;
  assert.throws(() => digestJson(cycle), /cycle/);
});

test("bounded file readers and atomic 0600 receipt persistence round trip validated evidence", (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "simworld-webrtc-readiness-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const probeFile = path.join(directory, "probe.json");
  const receiptFile = path.join(directory, "nested", "receipt.json");
  const probe = probeFixture();
  fs.writeFileSync(probeFile, `${JSON.stringify(probe)}\n`, { mode: 0o600 });
  assert.deepEqual(readWebRtcProbeResults(probeFile), probe);

  const receipt = createWebRtcReadinessReceipt(probe, expectations());
  assert.equal(writeWebRtcReadinessReceiptAtomic(receiptFile, receipt), path.resolve(receiptFile));
  assert.deepEqual(readWebRtcReadinessReceipt(receiptFile), receipt);
  assert.equal(fs.statSync(receiptFile).mode & 0o777, 0o600);
  assert.deepEqual(fs.readdirSync(path.dirname(receiptFile)), ["receipt.json"]);

  fs.writeFileSync(probeFile, "x".repeat(256 * 1024 + 1), { mode: 0o600 });
  assert.throws(
    () => readWebRtcProbeResults(probeFile),
    (error) => error.code === "WEBRTC_EVIDENCE_INVALID",
  );
});

test("offline CLI consumes only redacted files, requires deployment expectations, and never starts a service", (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "simworld-webrtc-cli-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const probeFile = path.join(directory, "probes.json");
  const receiptFile = path.join(directory, "receipt.json");
  fs.writeFileSync(probeFile, `${JSON.stringify(probeFixture())}\n`, { mode: 0o600 });

  let stdout = "";
  let stderr = "";
  const status = runCli({
    argv: [
      "--input", probeFile,
      "--expect-build-revision", BUILD_REVISION,
      "--expect-deployment-fingerprint", DEPLOYMENT_FINGERPRINT,
      "--expect-origin", PUBLIC_ORIGIN,
      "--expect-certificate-fingerprint", CERTIFICATE_FINGERPRINT,
      "--output", receiptFile,
    ],
    stdout: { write: (value) => { stdout += value; } },
    stderr: { write: (value) => { stderr += value; } },
    now: NOW + 1,
  });
  assert.equal(status, 0);
  assert.equal(stderr, "");
  const summary = JSON.parse(stdout);
  assert.equal(summary.ready, true);
  assert.match(summary.receipt_sha256, /^[a-f0-9]{64}$/);
  assert.equal(summary.output, path.resolve(receiptFile));
  assert.equal(fs.statSync(receiptFile).mode & 0o777, 0o600);
  validateWebRtcReadinessReceipt(JSON.parse(fs.readFileSync(receiptFile, "utf8")));

  stdout = "";
  stderr = "";
  const missingExpectation = runCli({
    argv: ["--input", probeFile],
    stdout: { write: (value) => { stdout += value; } },
    stderr: { write: (value) => { stderr += value; } },
    now: NOW + 1,
  });
  assert.equal(missingExpectation, 2);
  assert.equal(stdout, "");
  assert.equal(JSON.parse(stderr).code, "WEBRTC_CLI_USAGE");
});

test("checked-in schemas describe strict success-only probe and receipt contracts", () => {
  const schemas = path.join(__dirname, "..", "schemas");
  const probeSchema = JSON.parse(fs.readFileSync(path.join(schemas, "simworld-webrtc-probe-results-v1.schema.json"), "utf8"));
  const receiptSchema = JSON.parse(fs.readFileSync(path.join(schemas, "simworld-webrtc-readiness-receipt-v1.schema.json"), "utf8"));
  assert.equal(probeSchema.additionalProperties, false);
  assert.equal(probeSchema.properties.schema.const, "simworld-webrtc-probe-results/v1");
  assert.equal(probeSchema.properties.sessions.minItems, 6);
  assert.equal(receiptSchema.additionalProperties, false);
  assert.equal(receiptSchema.properties.schema.const, "simworld-webrtc-readiness-receipt/v1");
  assert.equal(receiptSchema.properties.outcome.const, "ready");
});

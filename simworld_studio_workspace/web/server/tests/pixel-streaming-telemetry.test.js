"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const {
  createPixelStreamingTelemetryRegistry,
  validateReport,
} = require("../pixel-streaming-telemetry");

const IDENTITY_A = {
  ownerId: "owner-a",
  sessionId: `session-${"a".repeat(64)}`,
  slotId: 1,
  leaseId: "lease-a",
};
const IDENTITY_B = {
  ownerId: "owner-b",
  sessionId: `session-${"b".repeat(64)}`,
  slotId: 2,
  leaseId: "lease-b",
};

function report(overrides = {}) {
  return {
    schema: "pixel-streaming-client-telemetry/v1",
    connection_id: "1".repeat(32),
    sequence: 1,
    connection_state: "connected",
    ice_connection_state: "completed",
    ice_gathering_state: "complete",
    data_channel_open: true,
    video: { decoded_frames: 720, frames_advancing: true },
    selected_candidate: {
      type: "relay",
      protocol: "tcp",
      turn_transport: "tls",
      candidate_fingerprint: "c".repeat(64),
      address_redacted: true,
    },
    ...overrides,
  };
}

test("selected-candidate reports retain only redacted enums and bounded metrics", () => {
  const validated = validateReport(report());
  assert.equal(validated.selected_candidate.type, "relay");
  assert.equal(validated.selected_candidate.turn_transport, "tls");
  assert.equal(JSON.stringify(validated).includes("address"), true);
  assert.equal(JSON.stringify(validated).includes("turn.example"), false);

  for (const sensitive of [
    { candidate_sdp: "candidate:1 1 udp 1 10.0.0.1 49160 typ relay" },
    { address: "10.0.0.1" },
    { raw_port: 49160 },
    { credential: "turn-password" },
    { turn_url: "turns:turn.example.test:5349" },
  ]) {
    assert.throws(
      () => validateReport({ ...report(), selected_candidate: { ...report().selected_candidate, ...sensitive } }),
      (error) => error.code === "PIXEL_STREAMING_TELEMETRY_INVALID",
    );
  }
  assert.throws(
    () => validateReport({ ...report(), selected_candidate: { ...report().selected_candidate, address_redacted: false } }),
    (error) => error.code === "PIXEL_STREAMING_TELEMETRY_SENSITIVE",
  );
});

test("registry binds telemetry to a lease, rejects replay, and expires stale evidence", () => {
  let now = 10_000;
  const registry = createPixelStreamingTelemetryRegistry({
    clock: () => now,
    ttlMs: 5_000,
    requireRelay: true,
  });
  const ready = registry.record(IDENTITY_A, report());
  assert.equal(ready.ready, true);
  assert.equal(ready.selected_candidate.type, "relay");
  assert.equal(ready.selected_candidate.address_redacted, true);
  assert.equal(registry.status(IDENTITY_B).status, "not_observed");
  assert.deepEqual(registry.summary(), {
    schema: "pixel-streaming-telemetry-summary/v1",
    active_reports: 1,
    ready_sessions: 1,
    relay_sessions: 1,
    require_relay: true,
    ttl_ms: 5_000,
  });
  assert.throws(
    () => registry.record(IDENTITY_A, report()),
    (error) => error.code === "PIXEL_STREAMING_TELEMETRY_REPLAYED" && error.statusCode === 409,
  );
  registry.record(IDENTITY_A, report({ sequence: 2, connection_state: "disconnected" }));
  assert.equal(registry.status(IDENTITY_A).ready, false);
  now += 5_001;
  assert.equal(registry.status(IDENTITY_A).status, "not_observed");
  assert.equal(registry.summary().active_reports, 0);
});

test("forced-relay policy never treats direct candidates as ready", () => {
  const direct = report({
    selected_candidate: {
      type: "srflx",
      protocol: "udp",
      turn_transport: null,
      candidate_fingerprint: "d".repeat(64),
      address_redacted: true,
    },
  });
  const relayRegistry = createPixelStreamingTelemetryRegistry({ requireRelay: true });
  assert.equal(relayRegistry.record(IDENTITY_A, direct).ready, false);
  const normalRegistry = createPixelStreamingTelemetryRegistry({ requireRelay: false });
  assert.equal(normalRegistry.record(IDENTITY_A, direct).ready, true);
  assert.throws(
    () => validateReport(report({
      selected_candidate: { ...direct.selected_candidate, turn_transport: "udp" },
    })),
    /non-relay candidates/,
  );
});

test("connection epochs may rotate while sequence is monotonic within an epoch", () => {
  const registry = createPixelStreamingTelemetryRegistry();
  registry.record(IDENTITY_A, report({ sequence: 10 }));
  registry.record(IDENTITY_A, report({ connection_id: "2".repeat(32), sequence: 0 }));
  assert.equal(registry.status(IDENTITY_A).ready, true);
  assert.throws(
    () => registry.record(IDENTITY_A, report({ connection_id: "2".repeat(32), sequence: 0 })),
    /sequence must increase/,
  );
});

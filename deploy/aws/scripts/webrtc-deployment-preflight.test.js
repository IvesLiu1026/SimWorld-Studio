"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const {
  evaluateSnapshot,
  parseSsListeners,
  SNAPSHOT_SCHEMA,
} = require("./webrtc-deployment-preflight");

function healthySnapshot(phase = "runtime") {
  return {
    schema: SNAPSHOT_SCHEMA,
    phase,
    captured_at: "2026-07-21T00:00:00.000Z",
    platform: "linux",
    effective_uid: 0,
    effective_gid: 0,
    environment: {
      studio_public_origin: "https://studio.example.edu",
      studio_host: "studio.example.edu",
      turn_public_host: "turn.example.edu",
      turn_external_ip: "8.8.8.8",
      runtime_uid: 1234,
      runtime_gid: 1234,
      secret_gid: 1234,
      turn_config_gid: 1235,
      build_revision: "a".repeat(40),
      pool_size: 3,
      port_stride: 2,
      base_mcp_port: 55559,
      base_cirrus_http: 8585,
      base_cirrus_ws: 8586,
      base_cirrus_sfu: 8989,
      base_ucv_port: 9017,
    },
    executables: {
      nginx: "/usr/sbin/nginx",
      turnserver: "/usr/bin/turnserver",
      openssl: "/usr/bin/openssl",
      certbot: "/usr/bin/certbot",
      ss: "/usr/bin/ss",
    },
    accounts: {
      simworld: { name: "simworld", uid: 1234, gid: 1234 },
      simworld_group: { name: "simworld", gid: 1234 },
      turnserver_group: { name: "turnserver", gid: 1235 },
    },
    release: {
      root: "/opt/simworld-studio",
      stat: { uid: 0, gid: 0, mode: 0o755, directory: true },
      git_revision: "a".repeat(40),
      git_status_available: true,
      dirty: false,
    },
    dns: {
      studio: ["203.0.113.10"],
      turn: ["8.8.8.8"],
      skipped: false,
    },
    listeners: [
      { protocol: "tcp", address: "0.0.0.0", port: 80, process_name: "nginx", pid: 10 },
      { protocol: "tcp", address: "0.0.0.0", port: 443, process_name: "nginx", pid: 10 },
      { protocol: "tcp", address: "0.0.0.0", port: 3478, process_name: "turnserver", pid: 20 },
      { protocol: "udp", address: "0.0.0.0", port: 3478, process_name: "turnserver", pid: 20 },
      { protocol: "tcp", address: "0.0.0.0", port: 5349, process_name: "turnserver", pid: 20 },
      { protocol: "tcp", address: "127.0.0.1", port: 3002, process_name: "node", pid: 30 },
      { protocol: "tcp", address: "127.0.0.1", port: 8585, process_name: "node", pid: 31 },
      { protocol: "tcp", address: "::1", port: 55559, process_name: "UnrealEditor", pid: 32 },
    ],
    listener_error: null,
  };
}

test("runtime preflight accepts reviewed public ingress/TURN and loopback control listeners", () => {
  const report = evaluateSnapshot(healthySnapshot());
  assert.equal(report.ready, true);
  assert.equal(report.host_runtime_ready, true);
  assert.equal(report.production_ready, false);
  assert.deepEqual(report.blocked_checks, []);
  assert.deepEqual(report.external_gates, [
    "administrator_dns_tls_firewall_approval",
    "forced_udp_tcp_tls_relay_matrix",
    "interactive_video_input_reconnect_evidence",
    "signed_webrtc_readiness_receipt",
  ]);
});

test("runtime preflight fails closed for a public control listener", () => {
  const snapshot = healthySnapshot();
  snapshot.listeners.push({
    protocol: "tcp",
    address: "0.0.0.0",
    port: 55561,
    process_name: "UnrealEditor",
    pid: 99,
  });
  const report = evaluateSnapshot(snapshot);
  assert.equal(report.production_ready, false);
  assert.equal(report.host_runtime_ready, false);
  assert.ok(report.blocked_checks.includes("control_plane_loopback"));
});

test("runtime preflight requires root listener attribution and expected owners", () => {
  const snapshot = healthySnapshot();
  snapshot.effective_uid = 1234;
  snapshot.listeners.find((entry) => entry.port === 443).process_name = null;
  snapshot.listeners.find((entry) => entry.protocol === "udp" && entry.port === 3478).process_name = "dnsmasq";
  const report = evaluateSnapshot(snapshot);
  assert.ok(report.blocked_checks.includes("listener_visibility"));
  assert.ok(report.blocked_checks.includes("public_https_ingress"));
  assert.ok(report.blocked_checks.includes("public_turn_listeners"));
});

test("provision preflight makes existing ingress and TURN ownership explicit", () => {
  const report = evaluateSnapshot(healthySnapshot("provision"));
  assert.equal(report.ready, false);
  assert.equal(report.production_ready, false);
  assert.ok(report.blocked_checks.includes("ingress_ownership_decision"));
  assert.ok(report.blocked_checks.includes("turn_ports_available"));
});

test("identity, DNS, package, and revision omissions are blockers", () => {
  const snapshot = healthySnapshot();
  snapshot.environment.runtime_uid = null;
  snapshot.environment.turn_external_ip = "127.0.0.1";
  snapshot.environment.build_revision = "dev";
  snapshot.executables.certbot = null;
  snapshot.dns.turn = [];
  const report = evaluateSnapshot(snapshot);
  assert.ok(report.blocked_checks.includes("required_packages"));
  assert.ok(report.blocked_checks.includes("numeric_identity"));
  assert.ok(report.blocked_checks.includes("deployment_identity"));
  assert.ok(report.blocked_checks.includes("public_dns"));
});

test("release preflight binds the environment revision to a clean root-owned checkout", () => {
  const snapshot = healthySnapshot();
  snapshot.release.stat.uid = 1234;
  snapshot.release.stat.mode = 0o775;
  snapshot.release.git_revision = "b".repeat(40);
  snapshot.release.dirty = true;
  const report = evaluateSnapshot(snapshot);
  assert.ok(report.blocked_checks.includes("sealed_release"));
  const releaseCheck = report.checks.find((entry) => entry.id === "sealed_release");
  assert.deepEqual(releaseCheck.evidence.issues, [
    "release_root_not_root_owned",
    "release_root_group_or_world_writable",
    "release_revision_mismatch",
    "release_tree_dirty",
  ]);
});

test("ss parser retains only bounded listener identity, not command arguments", () => {
  const listeners = parseSsListeners([
    'tcp LISTEN 0 511 0.0.0.0:443 0.0.0.0:* users:(("nginx",pid=123,fd=6)) --secret=do-not-copy',
    'udp UNCONN 0 0 [::]:3478 [::]:* users:(("turnserver",pid=456,fd=8))',
    'garbage line',
  ].join("\n"));
  assert.deepEqual(listeners, [
    { protocol: "tcp", address: "0.0.0.0", port: 443, process_name: "nginx", pid: 123 },
    { protocol: "udp", address: "::", port: 3478, process_name: "turnserver", pid: 456 },
  ]);
  assert.doesNotMatch(JSON.stringify(listeners), /do-not-copy/);
});

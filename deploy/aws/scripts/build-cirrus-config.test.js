"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFileSync, spawnSync } = require("node:child_process");

const SCRIPT = path.join(__dirname, "build-cirrus-config.js");

test("Cirrus deployment config is loopback-only and uses an ephemeral coturn REST credential", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "simworld-cirrus-config-"));
  try {
    const secret = "turn-shared-secret-test-material-".repeat(2);
    const secretFile = path.join(root, "turn.secret");
    const outputFile = path.join(root, "cirrus.json");
    fs.writeFileSync(secretFile, `${secret}\n`, { mode: 0o600 });
    const stdout = execFileSync(process.execPath, [
      SCRIPT,
      "--output", outputFile,
      "--http", "8585",
      "--streamer", "8586",
      "--sfu", "8989",
      "--session", "slot-2",
    ], {
      encoding: "utf8",
      env: {
        ...process.env,
        TURN_PUBLIC_HOST: "turn.example.test",
        TURN_SHARED_SECRET_FILE: secretFile,
        TURN_CREDENTIAL_TTL_SECONDS: "3600",
        SESSION_HARD_MAX_MS: "1800000",
        TURN_CREDENTIAL_RECONNECT_GRACE_SECONDS: "300",
      },
    });
    const config = JSON.parse(fs.readFileSync(outputFile, "utf8"));
    assert.equal(config.UseFrontend, false);
    assert.equal(config.UseMatchmaker, false);
    assert.equal(config.BindAddress, "127.0.0.1");
    assert.equal(config.HttpPort, 8585);
    assert.equal(config.StreamerPort, 8586);
    assert.equal(config.SFUPort, 8989);
    const peer = JSON.parse(config.peerConnectionOptions);
    assert.equal(peer.iceTransportPolicy, "relay");
    assert.deepEqual(peer.iceServers[0].urls, ["stun:turn.example.test:3478"]);
    assert.deepEqual(peer.iceServers[1].urls, [
      "turn:turn.example.test:3478?transport=tcp",
      "turn:turn.example.test:3478?transport=udp",
      "turns:turn.example.test:5349?transport=tcp",
    ]);
    assert.match(peer.iceServers[1].username, /^\d+:slot-2$/);
    assert.equal(
      peer.iceServers[1].credential,
      crypto.createHmac("sha1", secret).update(peer.iceServers[1].username).digest("base64"),
    );
    assert.equal(fs.statSync(outputFile).mode & 0o777, 0o600);
    assert.doesNotMatch(stdout, new RegExp(secret));
    assert.doesNotMatch(stdout, new RegExp(peer.iceServers[1].credential.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
    const report = JSON.parse(stdout);
    assert.equal(report.sessionHardMaxMs, 1800000);
    assert.equal(report.reconnectGraceSeconds, 300);
    assert.equal(report.config.BindAddress, "127.0.0.1");
    assert.match(JSON.parse(report.config.peerConnectionOptions).iceServers[1].credential, /REDACTED/);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("Cirrus deployment config rejects credentials that expire before session reconnect", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "simworld-cirrus-lifetime-"));
  try {
    const secretFile = path.join(root, "turn.secret");
    fs.writeFileSync(secretFile, `${"turn-lifetime-secret-material-".repeat(2)}\n`, { mode: 0o600 });
    const result = spawnSync(process.execPath, [
      SCRIPT,
      "--output", path.join(root, "cirrus.json"),
      "--http", "8585",
      "--streamer", "8586",
      "--sfu", "8989",
      "--session", "slot-2",
    ], {
      encoding: "utf8",
      env: {
        ...process.env,
        TURN_PUBLIC_HOST: "turn.example.test",
        TURN_SHARED_SECRET_FILE: secretFile,
        TURN_CREDENTIAL_TTL_SECONDS: "3600",
        SESSION_HARD_MAX_MS: "3600000",
        TURN_CREDENTIAL_RECONNECT_GRACE_SECONDS: "300",
      },
    });
    assert.equal(result.status, 2);
    assert.match(result.stderr, /must cover SESSION_HARD_MAX_MS plus reconnect grace/);
    assert.equal(fs.existsSync(path.join(root, "cirrus.json")), false);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("rotating the shared secret produces a distinct redacted credential artifact", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "simworld-cirrus-rotation-"));
  try {
    const credentials = [];
    const outputs = [];
    for (const generation of ["alpha", "bravo"]) {
      const secret = `turn-rotation-${generation}-secret-material-`.repeat(2);
      const secretFile = path.join(root, `${generation}.secret`);
      const output = path.join(root, `${generation}.json`);
      fs.writeFileSync(secretFile, `${secret}\n`, { mode: 0o600 });
      const stdout = execFileSync(process.execPath, [
        SCRIPT,
        "--output", output,
        "--http", "8585",
        "--streamer", "8586",
        "--sfu", "8989",
        "--session", "slot-rotation",
      ], {
        encoding: "utf8",
        env: {
          ...process.env,
          TURN_PUBLIC_HOST: "turn.example.test",
          TURN_SHARED_SECRET_FILE: secretFile,
          TURN_CREDENTIAL_TTL_SECONDS: "7200",
          SESSION_HARD_MAX_MS: "3600000",
          TURN_CREDENTIAL_RECONNECT_GRACE_SECONDS: "300",
        },
      });
      const peer = JSON.parse(JSON.parse(fs.readFileSync(output, "utf8")).peerConnectionOptions);
      credentials.push(peer.iceServers[1].credential);
      outputs.push(stdout);
      assert.doesNotMatch(stdout, new RegExp(secret));
      assert.doesNotMatch(stdout, new RegExp(peer.iceServers[1].credential.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
      assert.match(JSON.parse(JSON.parse(stdout).config.peerConnectionOptions).iceServers[1].credential, /REDACTED/);
    }
    assert.notEqual(credentials[0], credentials[1]);
    assert.notEqual(outputs[0], "");
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("Cirrus deployment config fails closed without DNS or secret-file inputs", () => {
  const result = spawnSync(process.execPath, [
    SCRIPT,
    "--output", "/tmp/unused-cirrus.json",
    "--http", "8585",
    "--streamer", "8586",
    "--sfu", "8989",
    "--session", "slot-2",
  ], {
    encoding: "utf8",
    env: { PATH: process.env.PATH || "" },
  });
  assert.equal(result.status, 2);
  assert.match(result.stderr, /TURN_PUBLIC_HOST/);
});

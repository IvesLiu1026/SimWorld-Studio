"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFileSync, spawnSync } = require("node:child_process");

const SCRIPT = path.join(__dirname, "materialize-coturn-config.js");
const TEMPLATE = path.join(__dirname, "../templates/coturn.conf");

test("coturn materializer injects secret/DNS/IP atomically without printing credentials", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "simworld-coturn-"));
  try {
    const secret = "coturn-materializer-secret-".repeat(2);
    const secretFile = path.join(root, "turn.secret");
    const output = path.join(root, "turnserver.conf");
    fs.writeFileSync(secretFile, `${secret}\n`, { mode: 0o600 });
    const stdout = execFileSync(process.execPath, [
      SCRIPT,
      "--template", TEMPLATE,
      "--output", output,
    ], {
      encoding: "utf8",
      env: {
        ...process.env,
        TURN_PUBLIC_HOST: "turn.example.test",
        TURN_EXTERNAL_IP: "203.0.113.20",
        TURN_PRIVATE_IP: "10.0.0.20",
        TURN_SHARED_SECRET_FILE: secretFile,
      },
    });
    const config = fs.readFileSync(output, "utf8");
    assert.match(config, new RegExp(`static-auth-secret=${secret}`));
    assert.match(config, /realm=turn\.example\.test/);
    assert.match(config, /external-ip=203\.0\.113\.20\/10\.0\.0\.20/);
    assert.match(config, /tls-listening-port=5349/);
    assert.match(config, /min-port=49160\nmax-port=49200/);
    assert.doesNotMatch(config, /CHANGE_ME|PUBLIC_IP|simworld\.your-lab\.edu/);
    assert.doesNotMatch(stdout, new RegExp(secret));
    assert.equal(fs.statSync(output).mode & 0o777, 0o600);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("coturn materializer rejects missing external identity before writing", () => {
  const result = spawnSync(process.execPath, [
    SCRIPT,
    "--template", TEMPLATE,
    "--output", "/tmp/unused-turnserver.conf",
  ], { encoding: "utf8", env: { PATH: process.env.PATH || "" } });
  assert.equal(result.status, 2);
  assert.match(result.stderr, /TURN_PUBLIC_HOST/);
});

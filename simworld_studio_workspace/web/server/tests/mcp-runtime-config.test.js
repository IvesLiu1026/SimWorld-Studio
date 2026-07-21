"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  buildBrokeredMcpRuntimeConfig,
  buildMcpRuntimeConfig,
  ensureBrokeredMcpRuntimeConfig,
  ensureMcpRuntimeConfig,
} = require("../mcp-runtime-config");

test("brokered MCP config is shared, secret-free, and contains no direct UE routing", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "simworld-mcp-brokered-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const baseDir = path.join(root, "web", "server");
  fs.mkdirSync(baseDir, { recursive: true });
  const env = {
    PORT: "3443",
    UNREAL_PORT: "65530",
    STUDIO_ACCESS_TOKEN: "root-secret",
    STUDIO_ACCESS_TOKEN_FILE: "/run/secrets/studio",
    SIMWORLD_INTERNAL_RUN_CAPABILITY: "run-secret",
  };
  const expected = buildBrokeredMcpRuntimeConfig(env, baseDir);
  const result = ensureBrokeredMcpRuntimeConfig({ env, baseDir });
  assert.deepEqual(result.config, expected);
  assert.deepEqual(result.config.mcpServers.simworld.env, {
    SIMWORLD_BROKER_HOST: "127.0.0.1",
    PORT: "3443",
    SIMWORLD_INTERNAL_CAPABILITY_REQUIRED: "1",
  });
  const serialized = fs.readFileSync(result.path, "utf8");
  for (const forbidden of ["root-secret", "/run/secrets", "run-secret", "UNREAL_PORT", "65530"]) {
    assert.equal(serialized.includes(forbidden), false);
  }
  assert.equal(fs.statSync(result.path).mode & 0o777, 0o600);
});

test("runtime MCP config pins this checkout without serializing inherited secrets", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "simworld-mcp-runtime-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const baseDir = path.join(root, "web", "server");
  fs.mkdirSync(baseDir, { recursive: true });
  const target = path.join(root, "state", "mcp.json");
  const env = {
    PORT: "3022",
    UNREAL_HOST: "127.0.0.1",
    UNREAL_PORT: "55570",
    SIMWORLD_MCP_CONFIG: target,
    POSTGRES_URL: "postgresql://user:secret@db/assets",
    STUDIO_ACCESS_TOKEN: "top-secret-token",
    ASSET_LIVE_AUDIT_RECEIPT_SHA256: "a".repeat(64),
  };

  const result = ensureMcpRuntimeConfig({ env, baseDir });
  const saved = fs.readFileSync(target, "utf8");
  const parsed = JSON.parse(saved);
  assert.equal(result.path, target);
  assert.equal(parsed.mcpServers.simworld.command, process.execPath);
  assert.deepEqual(parsed.mcpServers.simworld.args, [path.join(baseDir, "mcp-server.js")]);
  assert.deepEqual(parsed.mcpServers.simworld.env, {
    UNREAL_HOST: "127.0.0.1",
    UNREAL_PORT: "55570",
  });
  assert.equal(saved.includes("secret"), false);
  assert.equal(fs.statSync(target).mode & 0o777, 0o600);
});

test("runtime MCP config refuses relative targets and invalid ports", () => {
  assert.throws(
    () => ensureMcpRuntimeConfig({ env: { SIMWORLD_MCP_CONFIG: "relative.json" } }),
    /absolute path/,
  );
  assert.throws(
    () => buildMcpRuntimeConfig({ UNREAL_PORT: "70000" }),
    /valid TCP port/,
  );
});

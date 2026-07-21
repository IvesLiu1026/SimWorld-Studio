"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const {
  createBuilderRuntimeAuthority,
  getBuilderRuntime,
} = require("../builder-runtime-authority");
const { createReviewLoopCoordinator } = require("../review-loop-coordinator");

const IDENTITY = Object.freeze({
  ownerId: "browser-owner",
  sessionId: "stream-session",
  slotId: 1,
  leaseId: "lease-identifier-1234",
  mcpPort: 55561,
});

test("trusted builder authority is minted from the server review context, never caller routing fields", () => {
  const issued = [];
  const attached = [];
  const registry = {
    issue(input) {
      issued.push(input);
      return {
        capability: "c".repeat(43),
        runId: input.runId,
        scopeId: input.scopeId,
        idleExpiresAt: 10,
        hardExpiresAt: 20,
      };
    },
    attachProcess(capability, child) { attached.push({ capability, child }); },
    revoke() { return true; },
  };
  const coordinator = createReviewLoopCoordinator({
    transportProfile: "trusted_proxy",
    resolveActiveSession: () => IDENTITY,
    isActiveSessionBinding: () => true,
    textHandler: async () => {},
    visualHandler: async () => {},
  });
  const authority = createBuilderRuntimeAuthority({
    transportProfile: "trusted_proxy",
    registry,
    mcpConfigPath: "/srv/simworld/.runtime/mcp-brokered-3443.json",
    serverPort: 3443,
  });
  const request = {
    body: {
      conversationId: "conversation-1",
      sessionId: "caller-session",
      runId: "caller-run",
      UNREAL_PORT: 1,
      MCP_CONFIG: "/tmp/caller.json",
    },
    query: {},
  };
  const response = {
    status() { assert.fail("trusted binding should succeed"); },
    json() { assert.fail("trusted binding should succeed"); },
  };
  let completed = false;
  coordinator.bindVanillaChat(request, response, () => {
    authority.bind(request, response, () => { completed = true; });
  });
  assert.equal(completed, true);
  assert.equal(issued.length, 1);
  assert.deepEqual(issued[0].identity, IDENTITY);
  assert.notEqual(issued[0].runId, "caller-run");
  assert.match(issued[0].runId, /^[0-9a-f-]{36}$/);
  const runtime = getBuilderRuntime(request);
  assert.equal(runtime.mcpConfigPath, "/srv/simworld/.runtime/mcp-brokered-3443.json");
  assert.equal(runtime.unrealPort, null);
  assert.deepEqual(runtime.activeLease, IDENTITY);
  const child = {};
  runtime.attachProcess(child);
  assert.deepEqual(attached, [{ capability: "c".repeat(43), child }]);
});

test("every builder runner and inline Claude use the same capability authority", () => {
  const serverDir = path.resolve(__dirname, "..");
  for (const filename of [
    "codex-runner.js",
    "gemini-runner.js",
    "opencode-runner.js",
    "cursor-runner.js",
    "grok-runner.js",
  ]) {
    const source = fs.readFileSync(path.join(serverDir, filename), "utf8");
    assert.match(source, /buildBuilderChildEnv\(process\.env, BUILDER_RUNTIME\)/, filename);
    assert.match(source, /attachBuilderRuntimeProcess\(BUILDER_RUNTIME, proc\)/, filename);
    assert.match(source, /BUILDER_RUNTIME \? BUILDER_RUNTIME\.scopeId/, filename);
  }
  const index = fs.readFileSync(path.join(serverDir, "index.js"), "utf8");
  const bind = index.indexOf('app.post("/api/chat",_builderRuntimeAuthority.bind)');
  const legacy = index.indexOf('app.post("/api/chat",async');
  assert.ok(bind > 0 && bind < legacy);
  assert.match(index, /buildBuilderChildEnv\(process\.env,_builderRuntime\)/);
  assert.match(index, /_builderRuntime\.attachProcess\(g\)/);
  assert.doesNotMatch(index, /BUILDER_RUNTIME:s\.body/);
});

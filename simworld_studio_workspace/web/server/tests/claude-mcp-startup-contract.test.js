"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");
const test = require("node:test");

const {
  assertSafeBuilderArgv,
  buildClaudeSafetyArgs,
  buildMinimalBuilderEnv,
} = require("../builder-process-policy");
const {
  findExecutable,
  REPO_ROOT,
  SANDBOX_CAPABILITY,
  sandboxedSpawn,
} = require("../agent-sandbox");

test("real Claude offline startup discovers exactly the scoped SimWorld MCP server", {
  skip: !SANDBOX_CAPABILITY.verified || !findExecutable("claude", process.env),
  timeout: 20000,
}, async (t) => {
  const configPath = path.join(REPO_ROOT, `.claude-mcp-contract-${process.pid}-${Date.now()}.json`);
  fs.writeFileSync(configPath, JSON.stringify({
    mcpServers: {
      simworld: {
        command: process.execPath,
        args: [path.resolve(__dirname, "../mcp-server.js")],
        env: {
          SIMWORLD_BROKER_HOST: "127.0.0.1",
          PORT: "9",
          SIMWORLD_INTERNAL_CAPABILITY_REQUIRED: "1",
        },
      },
    },
  }), { mode: 0o600 });
  t.after(() => fs.rmSync(configPath, { force: true }));

  const args = [
    "-p",
    "--input-format", "text",
    "--output-format", "stream-json",
    "--include-partial-messages",
    "--verbose",
    "--mcp-config", configPath,
    ...buildClaudeSafetyArgs(),
  ];
  assertSafeBuilderArgv("claude", args);
  assert.equal(args.includes("--safe-mode"), false);
  const sandbox = sandboxedSpawn("claude", args, null, {
    env: process.env,
    provider: "claude",
    authMount: null,
  });
  const isolatedArgs = [...sandbox.args];
  const shareNet = isolatedArgs.indexOf("--share-net");
  assert.ok(shareNet >= 0);
  isolatedArgs.splice(shareNet, 1);
  const env = buildMinimalBuilderEnv({ NODE_ENV: "development" }, {
    capability: "c".repeat(43),
    runId: "offline-contract-run",
    serverPort: 9,
  }, { provider: "claude" });

  const child = spawn(sandbox.cmd, isolatedArgs, {
    cwd: "/",
    env,
    stdio: ["pipe", "pipe", "pipe"],
  });
  t.after(() => { if (!child.killed) child.kill("SIGTERM"); });
  let stdout = "";
  let stderr = "";
  const init = new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`Timed out waiting for Claude init: ${stderr}`)), 15000);
    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString();
      const lines = stdout.split("\n");
      stdout = lines.pop() || "";
      for (const line of lines) {
        try {
          const event = JSON.parse(line);
          if (event.type === "system" && event.subtype === "init") {
            clearTimeout(timer);
            resolve(event);
          }
        } catch {}
      }
    });
    child.stderr.on("data", (chunk) => { stderr += chunk.toString(); });
    child.once("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.once("exit", (code) => {
      if (code !== null && !stdout.includes('"subtype":"init"')) {
        clearTimeout(timer);
        reject(new Error(`Claude exited before init (${code}): ${stderr}`));
      }
    });
  });
  child.stdin.end("offline MCP startup contract");
  const event = await init;
  child.kill("SIGTERM");
  assert.deepEqual(event.mcp_servers.map((server) => server.name), ["simworld"]);
  assert.equal(event.mcp_servers[0].status === "pending" || event.mcp_servers[0].status === "connected", true);
  assert.doesNotMatch(stderr, /POSTGRES_URL|QDRANT_API_KEY|EMBED_SERVICE_TOKEN|STUDIO_ACCESS_TOKEN/);
});

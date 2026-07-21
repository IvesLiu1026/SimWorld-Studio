"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");
const test = require("node:test");

const MCP_SERVER = path.resolve(__dirname, "../mcp-server.js");

function listen(server) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      server.removeListener("error", reject);
      resolve(server.address().port);
    });
  });
}

function waitFor(predicate, timeoutMs = 5000) {
  const started = Date.now();
  return new Promise((resolve, reject) => {
    const poll = () => {
      const value = predicate();
      if (value) return resolve(value);
      if (Date.now() - started >= timeoutMs) return reject(new Error("Timed out waiting for MCP response"));
      setTimeout(poll, 20);
    };
    poll();
  });
}

test("search_assets uses only the lease broker while DB/vector/embed secrets stay in the main process", async (t) => {
  const capability = "a".repeat(43);
  const runId = "asset-run-1";
  const requests = [];
  const broker = http.createServer((req, res) => {
    let body = "";
    req.setEncoding("utf8");
    req.on("data", (chunk) => { body += chunk; });
    req.on("end", () => {
      requests.push({
        path: req.url,
        capability: req.headers["x-simworld-run-capability"] || "",
        runId: req.headers["x-simworld-run-id"] || "",
        authorization: req.headers.authorization || "",
        body: JSON.parse(body),
      });
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({
        ok: true,
        result: {
          retrieval: { source: "qdrant" },
          assets: [{
            name: "Wooden Market Stall",
            path: "/Game/VISTA/Props/SM_Market_Stall.SM_Market_Stall",
            spawnTool: "spawn_actor",
            category: "carts_and_vendors",
            setting: "market",
            dims: { width: 2.1, depth: 1.4, height: 2.2 },
            desc: "verified wooden stall",
            asset_snapshot_revision: "snapshot-verified",
          }],
        },
      }));
    });
  });
  const port = await listen(broker);
  t.after(() => new Promise((resolve) => broker.close(resolve)));

  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "simworld-asset-broker-"));
  const learnedTools = path.join(temporary, "learned-tools.json");
  fs.writeFileSync(learnedTools, "[]\n", "utf8");
  t.after(() => fs.rmSync(temporary, { recursive: true, force: true }));

  const childEnv = {
    PATH: "/usr/bin:/bin",
    HOME: "/home/simworld-agent",
    TMPDIR: "/tmp",
    NODE_ENV: "production",
    SIMWORLD_PRODUCTION_SAFETY: "1",
    ASSET_REQUIRE_REAL_ASSETS: "1",
    SIMWORLD_BROKER_HOST: "127.0.0.1",
    PORT: String(port),
    SIMWORLD_INTERNAL_RUN_CAPABILITY: capability,
    SIMWORLD_INTERNAL_RUN_ID: runId,
    SIMWORLD_INTERNAL_CAPABILITY_REQUIRED: "1",
    LEARNED_TOOLS_FILE: learnedTools,
  };
  assert.equal(childEnv.POSTGRES_URL, undefined);
  assert.equal(childEnv.QDRANT_API_KEY, undefined);
  assert.equal(childEnv.EMBED_SERVICE_TOKEN, undefined);
  assert.equal(childEnv.STUDIO_ACCESS_TOKEN, undefined);

  const proc = spawn(process.execPath, [MCP_SERVER], {
    env: childEnv,
    stdio: ["pipe", "pipe", "pipe"],
  });
  t.after(() => { if (!proc.killed) proc.kill("SIGTERM"); });
  const responses = new Map();
  let stdout = "";
  let stderr = "";
  proc.stdout.on("data", (chunk) => {
    stdout += chunk.toString();
    const lines = stdout.split("\n");
    stdout = lines.pop() || "";
    for (const line of lines) {
      try {
        const response = JSON.parse(line);
        if (Object.prototype.hasOwnProperty.call(response, "id")) responses.set(response.id, response);
      } catch {}
    }
  });
  proc.stderr.on("data", (chunk) => { stderr += chunk.toString(); });
  proc.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", id: 1, method: "initialize", params: {} })}\n`);
  proc.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized", params: {} })}\n`);
  proc.stdin.write(`${JSON.stringify({
    jsonrpc: "2.0",
    id: 2,
    method: "tools/call",
    params: {
      name: "search_assets",
      arguments: { query: "傳統市場木製攤位", category: "carts_and_vendors", k: 5 },
    },
  })}\n`);

  await waitFor(() => responses.has(2));
  assert.equal(proc.exitCode, null, stderr);
  assert.equal(responses.get(2).result.isError, false, stderr);
  const payload = JSON.parse(responses.get(2).result.content[0].text);
  assert.equal(payload.status, "success");
  assert.equal(payload.count, 1);
  assert.equal(payload.retrieval.source, "qdrant");
  assert.equal(payload.assets[0].path, "/Game/VISTA/Props/SM_Market_Stall.SM_Market_Stall");
  assert.deepEqual(requests, [{
    path: "/api/internal/assets",
    capability,
    runId,
    authorization: "",
    body: { query: "傳統市場木製攤位", category: "carts_and_vendors", k: 5 },
  }]);
  assert.doesNotMatch(stderr, new RegExp(capability));
});

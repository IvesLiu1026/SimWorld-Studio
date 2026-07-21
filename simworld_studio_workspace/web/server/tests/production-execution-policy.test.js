"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");
const {
  createProductionExecutionGuard,
  internalCapabilityOperationPolicy,
  productionExecutionLocked,
  productionMcpToolAllowed,
  productionMcpToolDecision,
} = require("../production-execution-policy");
const {
  buildPanelAgentRuleLines,
  buildSceneAgentRuntimeAppendix,
} = require("../agent-runtime-policy");

const SERVER_PATH = path.resolve(__dirname, "../mcp-server.js");
const JOB_ROOT = path.resolve(__dirname, "../../../tmp/jobs");
const TOKEN = "focused-production-lockdown-token-0123456789abcdef";

function invokeGuard(env, requestPath, body, method = "POST") {
  let nextCalled = false;
  let statusCode = null;
  let responseBody = null;
  createProductionExecutionGuard({ env })(
    { method, path: requestPath, url: requestPath, body },
    {
      status(code) { statusCode = code; return this; },
      json(value) { responseBody = value; return this; },
    },
    () => { nextCalled = true; },
  );
  return { nextCalled, responseBody, statusCode };
}

function startMcp(extraEnv = {}) {
  const proc = spawn(process.execPath, [SERVER_PATH], {
    env: {
      ...process.env,
      STUDIO_ACCESS_TOKEN: TOKEN,
      UNREAL_HOST: "127.0.0.1",
      UNREAL_PORT: "65530",
      ...extraEnv,
    },
    stdio: ["pipe", "pipe", "pipe"],
  });
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
  const send = (payload) => proc.stdin.write(`${JSON.stringify(payload)}\n`);
  const waitFor = (ids, timeoutMs = 5000) => new Promise((resolve, reject) => {
    const started = Date.now();
    const poll = () => {
      if (ids.every((id) => responses.has(id))) return resolve();
      if (proc.exitCode !== null) return reject(new Error(`MCP exited ${proc.exitCode}: ${stderr}`));
      if (Date.now() - started >= timeoutMs) return reject(new Error(`Timed out waiting for MCP: ${stderr}`));
      setTimeout(poll, 20);
    };
    poll();
  });
  return { proc, responses, send, waitFor, stderr: () => stderr };
}

test("production HTTP guard rejects generic UE execution without reflecting caller content", () => {
  const env = {
    NODE_ENV: "production",
    SIMWORLD_ALLOW_UNSAFE_UE_EXECUTION: "true",
  };
  const secret = "do-not-log-or-reflect-this-script";
  const cases = [
    ["/api/ue-command", { command: `py ${secret}` }],
    ["/api/ue-command/?retry=1", { command: secret }],
    ["/api/camera", { cmd: "set_camera", args: [0, 0, `0); ${secret}`, 0, 0, 0] }],
    ["/api/internal/ue", { type: "execute_python_script", params: { script: secret } }],
    ["/api/internal/ue", { type: "execute_console_command", params: { command: secret } }],
    ["/api/internal/ue", { type: "get_actors_in_level", params: { script: secret } }],
    ["/api/internal/ucv", { cmd: `vset /object/Pedestrian_1/location ${secret}` }],
  ];

  for (const [requestPath, body] of cases) {
    const result = invokeGuard(env, requestPath, body);
    assert.equal(result.nextCalled, false, requestPath);
    assert.equal(result.statusCode, 403, requestPath);
    assert.equal(result.responseBody.code, "GENERIC_UE_EXECUTION_DISABLED", requestPath);
    assert.doesNotMatch(JSON.stringify(result.responseBody), new RegExp(secret), requestPath);
  }
});

test("production guard preserves read-only broker calls and fixed VISTA Content API routes", () => {
  const env = { NODE_ENV: "production" };
  const allowed = [
    ["/api/internal/ue", { type: "get_actors_in_level", params: {} }],
    ["/api/internal/ue", { type: "find_actors_by_name", params: { pattern: "VISTA_*" } }],
    ["/api/internal/ucv", { cmd: "vget /objects", timeoutMs: 5000 }],
    ["/api/camera", { cmd: "set_camera", args: [0, 0, 1200, -20, 90, 0] }],
    ["/api/camera", { cmd: "get_camera", args: [] }],
    ["/api/vista/imports/mmg_040/build", { plan_id: "plan-fixed", confirm: true }],
    ["/api/vista/imports/mmg_040/animation/start", { program_id: "fixed-program", confirm: true }],
  ];
  for (const [requestPath, body] of allowed) {
    assert.equal(invokeGuard(env, requestPath, body).nextCalled, true, requestPath);
  }
});

test("development behavior remains compatible while the explicit safety gate is fail closed", () => {
  assert.equal(invokeGuard({ NODE_ENV: "development" }, "/api/ue-command", { command: "stat fps" }).nextCalled, true);
  assert.equal(productionExecutionLocked({ NODE_ENV: "development", SIMWORLD_PRODUCTION_SAFETY: "1" }), true);
  assert.equal(productionExecutionLocked({ NODE_ENV: "production", SIMWORLD_PRODUCTION_SAFETY: "0" }), true);
  assert.equal(
    productionMcpToolAllowed("execute_python_script", { NODE_ENV: "development", SIMWORLD_PRODUCTION_SAFETY: "1" }),
    false,
  );
  assert.equal(
    productionMcpToolAllowed("get_actors_in_level", { NODE_ENV: "development", SIMWORLD_PRODUCTION_SAFETY: "1" }),
    true,
  );
  assert.throws(
    () => productionExecutionLocked({ NODE_ENV: "development", SIMWORLD_PRODUCTION_SAFETY: "maybe" }),
    /SIMWORLD_PRODUCTION_SAFETY/,
  );
});

test("production run capabilities reach route authentication but free-form mutations have one typed denial", () => {
  const env = { NODE_ENV: "production" };
  assert.deepEqual(productionMcpToolDecision("spawn_actor", env), {
    allowed: false,
    code: "NLP_TYPED_MUTATION_UNAVAILABLE",
    message: "Free-form NLP scene mutation is unavailable until a typed mutation adapter is installed.",
  });
  assert.equal(productionMcpToolDecision("save_scene_as", env).code, "NLP_TYPED_MUTATION_UNAVAILABLE");
  assert.equal(internalCapabilityOperationPolicy({
    channel: "ue",
    body: { type: "get_actors_in_level", params: {} },
  }, env).allowed, true);
  assert.equal(internalCapabilityOperationPolicy({
    channel: "ue",
    body: { type: "spawn_actor", params: { name: "Cube" } },
  }, env).code, "NLP_TYPED_MUTATION_UNAVAILABLE");

  let nextCalled = false;
  createProductionExecutionGuard({ env })({
    method: "POST",
    path: "/api/internal/ue",
    headers: {
      "x-simworld-run-capability": "c".repeat(43),
      "x-simworld-run-id": "run-1",
    },
    body: { type: "spawn_actor", params: {} },
  }, {}, () => { nextCalled = true; });
  assert.equal(nextCalled, true, "capability-shaped requests must be authenticated and policy-checked by the route");
});

test("production agent policy removes arbitrary execution guidance", () => {
  const unsafeFlag = { SIMWORLD_ALLOW_UNSAFE_UE_EXECUTION: "true" };
  const production = buildSceneAgentRuntimeAppendix({ env: { NODE_ENV: "production", ...unsafeFlag } });
  assert.match(production, /Arbitrary execute_python_script.*disabled/);
  assert.match(production, /fixed VISTA Content API workflow/);
  assert.doesNotMatch(production, /Use small, verifiable execute_python_script batches/);
  assert.doesNotMatch(production, /batch_python_scripts/);

  const panel = buildPanelAgentRuleLines({ agentName: "Pedestrian_1", env: { NODE_ENV: "production" } }).join("\n");
  assert.match(panel, /PRODUCTION UE EXECUTION SAFETY/);
  assert.doesNotMatch(panel, /batch_python_scripts/);

  const development = buildSceneAgentRuntimeAppendix({ env: { NODE_ENV: "development" } });
  assert.match(development, /Use small, verifiable execute_python_script batches/);
  assert.match(development, /batch_python_scripts/);
});

test("production MCP hides and rejects arbitrary Python and job-log tools", async (t) => {
  const secret = "production-mcp-secret-content";
  fs.mkdirSync(JOB_ROOT, { recursive: true });
  const secretLog = path.join(JOB_ROOT, `lockdown-${process.pid}-${Date.now()}.log`);
  fs.writeFileSync(secretLog, `${secret}\n`, "utf8");
  t.after(() => fs.rmSync(secretLog, { force: true }));

  const mcp = startMcp({
    NODE_ENV: "production",
    SIMWORLD_ALLOW_UNSAFE_UE_EXECUTION: "true",
  });
  t.after(() => { if (!mcp.proc.killed) mcp.proc.kill("SIGTERM"); });

  mcp.send({ jsonrpc: "2.0", id: 1, method: "initialize", params: {} });
  mcp.send({ jsonrpc: "2.0", method: "notifications/initialized", params: {} });
  mcp.send({ jsonrpc: "2.0", id: 2, method: "tools/list", params: {} });
  mcp.send({
    jsonrpc: "2.0",
    id: 3,
    method: "tools/call",
    params: { name: "execute_python_script", arguments: { script: secret } },
  });
  mcp.send({
    jsonrpc: "2.0",
    id: 4,
    method: "tools/call",
    params: { name: "read_job_log", arguments: { log_path: secretLog } },
  });
  mcp.send({
    jsonrpc: "2.0",
    id: 5,
    method: "tools/call",
    params: { name: "spawn_actor", arguments: { name: "Cube", static_mesh: "/Engine/BasicShapes/Cube.Cube", location: [0, 0, 0] } },
  });
  mcp.send({
    jsonrpc: "2.0",
    id: 6,
    method: "tools/call",
    params: { name: "save_scene_as", arguments: { name: "BlockedScene" } },
  });
  await mcp.waitFor([2, 3, 4, 5, 6]);

  const names = mcp.responses.get(2).result.tools.map((tool) => tool.name);
  assert.equal(names.includes("execute_python_script"), false);
  assert.equal(names.includes("read_job_log"), false);
  assert.equal(names.includes("spawn_actor"), false);
  assert.equal(names.includes("save_scene_as"), false);
  for (const id of [3, 4]) {
    const response = mcp.responses.get(id);
    assert.equal(response.result.isError, true);
    assert.match(response.result.content[0].text, /GENERIC_UE_EXECUTION_DISABLED/);
    assert.doesNotMatch(JSON.stringify(response), new RegExp(secret));
  }
  assert.match(mcp.responses.get(5).result.content[0].text, /NLP_TYPED_MUTATION_UNAVAILABLE/);
  assert.match(mcp.responses.get(6).result.content[0].text, /NLP_TYPED_MUTATION_UNAVAILABLE/);
  assert.doesNotMatch(mcp.stderr(), new RegExp(secret));
});

test("development MCP continues to expose and read background job logs", async (t) => {
  const secret = "development-job-log-compatibility";
  fs.mkdirSync(JOB_ROOT, { recursive: true });
  const secretLog = path.join(JOB_ROOT, `compat-${process.pid}-${Date.now()}.log`);
  fs.writeFileSync(secretLog, `[DONE]\n${secret}\n`, "utf8");
  t.after(() => fs.rmSync(secretLog, { force: true }));

  const mcp = startMcp({ NODE_ENV: "development", SIMWORLD_PRODUCTION_SAFETY: "0" });
  t.after(() => { if (!mcp.proc.killed) mcp.proc.kill("SIGTERM"); });
  mcp.send({ jsonrpc: "2.0", id: 1, method: "initialize", params: {} });
  mcp.send({ jsonrpc: "2.0", id: 2, method: "tools/list", params: {} });
  mcp.send({
    jsonrpc: "2.0",
    id: 3,
    method: "tools/call",
    params: { name: "read_job_log", arguments: { log_path: secretLog } },
  });
  await mcp.waitFor([2, 3]);

  const names = mcp.responses.get(2).result.tools.map((tool) => tool.name);
  assert.equal(names.includes("execute_python_script"), true);
  assert.equal(names.includes("read_job_log"), true);
  assert.equal(mcp.responses.get(3).result.isError, false, mcp.stderr());
  assert.match(mcp.responses.get(3).result.content[0].text, new RegExp(secret));
});

test("index installs the execution guard before request-body logging", () => {
  const source = fs.readFileSync(path.resolve(__dirname, "../index.js"), "utf8");
  const guardIndex = source.indexOf("app.use(createProductionExecutionGuard({env:process.env}))");
  const bodyLogIndex = source.indexOf("bodyForLog=");
  assert.notEqual(guardIndex, -1);
  assert.notEqual(bodyLogIndex, -1);
  assert.ok(guardIndex < bodyLogIndex, "generic execution must be denied before request bodies are logged");
});

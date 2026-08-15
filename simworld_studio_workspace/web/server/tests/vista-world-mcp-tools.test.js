"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { spawn } = require("node:child_process");

const {
  TOOL_NAMES,
  VERIFIED_EVENT_IDS,
  VISTA_WORLD_TRANSPORT_TIMEOUT_MS,
  createVistaWorldMcpTools,
  internalVistaWorldMutationAllowed,
  isVistaWorldWirePayloadAllowed,
} = require("../vista-world-mcp-tools");
const {
  createProductionExecutionGuard,
  internalCapabilityOperationPolicy,
  productionMcpToolAllowed,
} = require("../production-execution-policy");

const REVISION = "vista_playable_home_r1";
const PLAYER = "home.r1/player.01";
const DOOR = "home.r1/portal.entry_hall-living_room.01";
const NPC = "home.r1/entity.npc.01";

function success(payload, extra = {}) {
  return {
    command_id: payload.command_id,
    status: "success",
    code: "VISTA_WORLD_OK",
    session_generation: payload.session_generation + 1,
    ...extra,
  };
}

function statusArgs() {
  return { expected_revision: REVISION };
}

function interactionArgs(generation = 0) {
  return {
    expected_revision: REVISION,
    session_generation: generation,
    requester_semantic_id: PLAYER,
    target_semantic_id: DOOR,
    affordance: "open",
  };
}

test("typed tool definitions stay production-visible and reject extra schema fields", () => {
  const runtime = createVistaWorldMcpTools({ sendTyped: async (payload) => success(payload) });
  assert.deepEqual(runtime.toolDefinitions.map((tool) => tool.name), Object.values(TOOL_NAMES));
  for (const definition of runtime.toolDefinitions) {
    assert.equal(definition.inputSchema.additionalProperties, false);
    assert.equal(productionMcpToolAllowed(definition.name, { NODE_ENV: "production" }), true);
  }
  const event = runtime.toolDefinitions.find((tool) => tool.name === TOOL_NAMES.eventStart);
  assert.deepEqual(event.inputSchema.properties.event_id.enum, VERIFIED_EVENT_IDS);
});

test("interaction sends one exact vista_world_action payload and advances generation", async () => {
  const calls = [];
  const runtime = createVistaWorldMcpTools({
    sendTyped: async (payload) => {
      calls.push(payload);
      return success(payload, {
        target_semantic_id: payload.target_semantic_id,
        state: { semantic_id: payload.target_semantic_id, values: { open: "true" } },
      });
    },
  });

  assert.equal(runtime.handlers[TOOL_NAMES.status](statusArgs()).generation, 0);
  const result = await runtime.handlers[TOOL_NAMES.interact](interactionArgs());
  assert.equal(result.generation, 1);
  assert.equal(result.status, "success");
  assert.equal("active_event" in result, false);
  assert.equal(calls.length, 1);
  assert.deepEqual(Object.keys(calls[0]).sort(), [
    "affordance", "command_id", "expected_revision", "operation",
    "requester_semantic_id", "session_generation", "target_semantic_id",
  ]);
  assert.equal(calls[0].operation, "interaction");
  assert.match(calls[0].command_id, /^vwc-[a-f0-9]{24}$/);
  assert.equal(JSON.stringify(calls[0]).includes("python"), false);

  await assert.rejects(
    async () => runtime.handlers[TOOL_NAMES.interact](interactionArgs(0)),
    { code: "VISTA_WORLD_GENERATION_STALE" },
  );
  assert.equal(calls.length, 1, "stale commands must fail before transport");
});

test("runtime errors resynchronize generation without replaying a mutation", async () => {
  const calls = [];
  const runtime = createVistaWorldMcpTools({
    sendTyped: async (payload) => {
      calls.push(payload);
      return {
        command_id: payload.command_id,
        status: "error",
        code: "GENERATION_STALE",
        session_generation: 7,
      };
    },
  });
  const result = await runtime.handlers[TOOL_NAMES.interact](interactionArgs(0));
  assert.equal(result.status, "error");
  assert.equal(result.generation, 7);
  assert.equal(calls.length, 1);
  assert.equal(runtime.handlers[TOOL_NAMES.status](statusArgs()).generation, 7);
});

test("only one generation-changing command may be in flight", async () => {
  let release;
  const runtime = createVistaWorldMcpTools({
    sendTyped: (payload) => new Promise((resolve) => { release = () => resolve(success(payload)); }),
  });
  const first = runtime.handlers[TOOL_NAMES.interact](interactionArgs(0));
  await assert.rejects(
    runtime.handlers[TOOL_NAMES.interact](interactionArgs(0)),
    { code: "VISTA_WORLD_BUSY" },
  );
  release();
  assert.equal((await first).generation, 1);
});

test("NPC queues reject duplicate IDs, executable fields, and stale revisions before transport", async () => {
  const calls = [];
  const runtime = createVistaWorldMcpTools({
    sendTyped: async (payload) => { calls.push(payload); return success(payload); },
  });
  const valid = {
    expected_revision: REVISION,
    session_generation: 0,
    npc_semantic_id: NPC,
    replace: true,
    actions: [
      { action_id: "walk-kitchen", type: "navigate_to", target_semantic_id: "home.r1/room.kitchen_dining" },
      { action_id: "wait-one", type: "wait", duration_sec: 1, timeout_sec: 2 },
    ],
  };
  const result = await runtime.handlers[TOOL_NAMES.npcQueue](valid);
  assert.equal(result.generation, 1);
  assert.equal(calls[0].operation, "npc_queue");
  assert.equal(calls[0].replace, true);

  const fresh = createVistaWorldMcpTools({
    sendTyped: async (payload) => { calls.push(payload); return success(payload); },
  });
  for (const invalid of [
    { ...valid, actions: [{ action_id: "same", type: "wait" }, { action_id: "same", type: "wait" }] },
    { ...valid, actions: [{ action_id: "bad", type: "wait", script: "open('/secret')" }] },
    { ...valid, expected_revision: "other_revision" },
    { ...valid, command: "execute_python_script" },
  ]) {
    await assert.rejects(async () => fresh.handlers[TOOL_NAMES.npcQueue](invalid));
  }
  assert.equal(calls.length, 1, "invalid NPC queues must never reach transport");
});

test("verified event start and reset update only successful local overlay state", async () => {
  const calls = [];
  const runtime = createVistaWorldMcpTools({
    sendTyped: async (payload) => { calls.push(payload); return success(payload); },
  });
  const started = await runtime.handlers[TOOL_NAMES.eventStart]({
    expected_revision: REVISION,
    session_generation: 0,
    event_id: "mmg_044",
  });
  assert.equal(started.active_event, "mmg_044");
  assert.equal(calls[0].event_operation, "start_event");
  const reset = await runtime.handlers[TOOL_NAMES.eventReset]({
    expected_revision: REVISION,
    session_generation: 1,
  });
  assert.equal(reset.active_event, null);
  assert.equal(calls[1].event_operation, "reset_event");
  assert.equal("event_id" in calls[1], false);

  const fresh = createVistaWorldMcpTools({ sendTyped: async (payload) => success(payload) });
  await assert.rejects(
    async () => fresh.handlers[TOOL_NAMES.eventStart]({
      expected_revision: REVISION,
      session_generation: 0,
      event_id: "private_oracle_event",
    }),
    { code: "VISTA_WORLD_EVENT_NOT_VERIFIED" },
  );
});

test("production internal bridge accepts only exact typed world envelopes", () => {
  const payloadPromise = [];
  const capture = createVistaWorldMcpTools({
    sendTyped: async (payload) => { payloadPromise.push(payload); return success(payload); },
  });
  return capture.handlers[TOOL_NAMES.interact](interactionArgs()).then(() => {
    const payload = payloadPromise[0];
    const body = {
      type: "vista_world_action",
      params: payload,
      timeoutMs: VISTA_WORLD_TRANSPORT_TIMEOUT_MS,
    };
    assert.equal(isVistaWorldWirePayloadAllowed(payload, REVISION), true);
    assert.equal(internalVistaWorldMutationAllowed(body, { VISTA_WORLD_REVISION: REVISION }), true);
    assert.equal(internalCapabilityOperationPolicy({ channel: "ue", body }, {
      NODE_ENV: "production",
      VISTA_WORLD_REVISION: REVISION,
    }).allowed, true);
    let nextCalled = false;
    createProductionExecutionGuard({
      env: { NODE_ENV: "production", VISTA_WORLD_REVISION: REVISION },
    })({ method: "POST", path: "/api/internal/ue", url: "/api/internal/ue", body }, {
      status() { return this; },
      json() { return this; },
    }, () => { nextCalled = true; });
    assert.equal(nextCalled, true, "the loopback bearer path must accept the exact typed envelope");

    for (const invalid of [
      { ...body, timeoutMs: 60_000 },
      { ...body, host: "attacker.invalid" },
      { ...body, type: "execute_python_script" },
      { ...body, params: { ...payload, script: "open('/secret')" } },
      { ...body, params: { ...payload, expected_revision: "other_revision" } },
      { ...body, params: { ...payload, command_id: "caller-selected" } },
    ]) {
      assert.equal(internalVistaWorldMutationAllowed(invalid, { VISTA_WORLD_REVISION: REVISION }), false);
      assert.equal(internalCapabilityOperationPolicy({ channel: "ue", body: invalid }, {
        NODE_ENV: "production",
        VISTA_WORLD_REVISION: REVISION,
      }).allowed, false);
    }
  });
});

function startMcp(port, extraEnv = {}) {
  const serverPath = path.resolve(__dirname, "../mcp-server.js");
  const proc = spawn(process.execPath, [serverPath], {
    env: {
      ...process.env,
      NODE_ENV: "production",
      STUDIO_ACCESS_TOKEN: "typed-world-mcp-test-token-0123456789abcdef",
      SIMWORLD_BROKER_HOST: "127.0.0.1",
      PORT: String(port),
      VISTA_WORLD_REVISION: REVISION,
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
  const send = (value) => proc.stdin.write(`${JSON.stringify(value)}\n`);
  const waitFor = (ids, timeoutMs = 5_000) => new Promise((resolve, reject) => {
    const started = Date.now();
    const poll = () => {
      if (ids.every((id) => responses.has(id))) return resolve();
      if (proc.exitCode !== null) return reject(new Error(`MCP exited ${proc.exitCode}: ${stderr}`));
      if (Date.now() - started > timeoutMs) return reject(new Error(`MCP timeout: ${stderr}`));
      setTimeout(poll, 20);
    };
    poll();
  });
  return { proc, responses, send, waitFor, stderr: () => stderr };
}

test("production MCP lists typed world tools and sends no caller-controlled transport fields", async (t) => {
  const requests = [];
  const broker = http.createServer((request, response) => {
    let body = "";
    request.on("data", (chunk) => { body += chunk.toString(); });
    request.on("end", () => {
      const parsed = JSON.parse(body);
      requests.push({ url: request.url, headers: request.headers, body: parsed });
      response.setHeader("Content-Type", "application/json");
      response.end(JSON.stringify({ ok: true, result: success(parsed.params) }));
    });
  });
  await new Promise((resolve) => broker.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise((resolve) => broker.close(resolve)));

  const mcp = startMcp(broker.address().port);
  t.after(() => { if (!mcp.proc.killed) mcp.proc.kill("SIGTERM"); });
  mcp.send({ jsonrpc: "2.0", id: 1, method: "initialize", params: {} });
  mcp.send({ jsonrpc: "2.0", id: 2, method: "tools/list", params: {} });
  mcp.send({
    jsonrpc: "2.0",
    id: 3,
    method: "tools/call",
    params: { name: TOOL_NAMES.interact, arguments: interactionArgs(0) },
  });
  await mcp.waitFor([2, 3]);

  const names = mcp.responses.get(2).result.tools.map((tool) => tool.name);
  for (const name of Object.values(TOOL_NAMES)) assert.equal(names.includes(name), true, name);
  assert.equal(names.includes("execute_python_script"), false);
  assert.equal(mcp.responses.get(3).result.isError, false, mcp.stderr());
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, "/api/internal/ue");
  assert.deepEqual(Object.keys(requests[0].body).sort(), ["params", "timeoutMs", "type"]);
  assert.equal(requests[0].body.type, "vista_world_action");
  assert.equal(requests[0].body.timeoutMs, VISTA_WORLD_TRANSPORT_TIMEOUT_MS);
  assert.equal(JSON.stringify(requests[0].body).includes("script"), false);
  assert.match(String(requests[0].headers.authorization || ""), /^Bearer /);

  mcp.send({
    jsonrpc: "2.0",
    id: 4,
    method: "tools/call",
    params: {
      name: TOOL_NAMES.interact,
      arguments: { ...interactionArgs(1), script: "open('/secret')" },
    },
  });
  await mcp.waitFor([4]);
  assert.equal(mcp.responses.get(4).result.isError, true);
  assert.equal(requests.length, 1, "invalid caller fields must never reach the broker");
  assert.doesNotMatch(JSON.stringify(mcp.responses.get(4)), /open\('\/secret'\)/);
});

test("internal broker pins mutation transport to one attempt and a bounded response", () => {
  const source = fs.readFileSync(path.resolve(__dirname, "../index.js"), "utf8");
  assert.match(source, /type==="vista_world_action"/);
  assert.match(source, /\{timeoutMs:15000,queueDeadlineMs:30000,maxAttempts:1,maxResponseBytes:64\*1024\}/);
  assert.doesNotMatch(
    source,
    /type==="vista_world_action"[\s\S]{0,240}maxAttempts:\s*(?:2|3|timeoutMs)/,
  );
});

'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');

const MCP_SERVER = path.resolve(__dirname, '../mcp-server.js');
const TOKEN = 'test-studio-access-token-0123456789abcdef';

function listen(server) {
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      server.removeListener('error', reject);
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
      if (Date.now() - started >= timeoutMs) return reject(new Error('Timed out waiting for MCP response'));
      setTimeout(poll, 20);
    };
    poll();
  });
}

test('MCP UE and UCV broker requests propagate the Studio bearer token', async (t) => {
  const requests = [];
  const broker = http.createServer((req, res) => {
    let body = '';
    req.setEncoding('utf8');
    req.on('data', (chunk) => { body += chunk; });
    req.on('end', () => {
      requests.push({
        path: req.url,
        authorization: req.headers.authorization || '',
        body: body ? JSON.parse(body) : null,
      });
      res.setHeader('Content-Type', 'application/json');
      res.end(JSON.stringify({ ok: true, result: { actors: [] } }));
    });
  });
  const port = await listen(broker);
  t.after(() => new Promise((resolve) => broker.close(resolve)));

  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'simworld-internal-auth-'));
  const learnedTools = path.join(tmp, 'learned_tools.json');
  fs.writeFileSync(learnedTools, '[]\n', 'utf8');
  t.after(() => fs.rmSync(tmp, { recursive: true, force: true }));

  const proc = spawn(process.execPath, [MCP_SERVER], {
    env: {
      ...process.env,
      LEARNED_TOOLS_FILE: learnedTools,
      PORT: String(port),
      SIMWORLD_BROKER_HOST: '127.0.0.1',
      STUDIO_ACCESS_TOKEN: TOKEN,
    },
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  t.after(() => { if (!proc.killed) proc.kill('SIGTERM'); });

  const responses = new Map();
  let stdout = '';
  let stderr = '';
  proc.stdout.on('data', (chunk) => {
    stdout += chunk.toString();
    const lines = stdout.split('\n');
    stdout = lines.pop() || '';
    for (const line of lines) {
      try {
        const response = JSON.parse(line);
        if (Object.prototype.hasOwnProperty.call(response, 'id')) responses.set(response.id, response);
      } catch {}
    }
  });
  proc.stderr.on('data', (chunk) => { stderr += chunk.toString(); });

  const send = (payload) => proc.stdin.write(`${JSON.stringify(payload)}\n`);
  send({ jsonrpc: '2.0', id: 1, method: 'initialize', params: {} });
  send({ jsonrpc: '2.0', method: 'notifications/initialized', params: {} });
  send({
    jsonrpc: '2.0',
    id: 2,
    method: 'tools/call',
    params: { name: 'get_actors_in_level', arguments: {} },
  });
  send({
    jsonrpc: '2.0',
    id: 3,
    method: 'tools/call',
    params: { name: 'agent_stop', arguments: { agent_name: 'TestAgent', agent_type: 'pedestrian' } },
  });

  await waitFor(() => responses.has(2) && responses.has(3));
  assert.equal(responses.get(2).result.isError, false, stderr);
  assert.equal(responses.get(3).result.isError, false, stderr);

  const ueRequest = requests.find((request) => request.path === '/api/internal/ue');
  const ucvRequest = requests.find((request) => request.path === '/api/internal/ucv');
  assert.ok(ueRequest, 'expected an internal UE broker request');
  assert.ok(ucvRequest, 'expected an internal UCV broker request');
  assert.equal(ueRequest.authorization, `Bearer ${TOKEN}`);
  assert.equal(ucvRequest.authorization, `Bearer ${TOKEN}`);
  assert.equal(ueRequest.body.type, 'get_actors_in_level');

  const source = fs.readFileSync(MCP_SERVER, 'utf8');
  assert.doesNotMatch(
    source,
    /\/api\/verifier-update|_notifyBackend/,
    'the MCP verifier must return evidence through its tool result instead of a ghost callback',
  );
  assert.doesNotMatch(stderr, new RegExp(TOKEN), 'the bearer token must not be logged to stderr');
});

test('MCP capability mode sends only run authority and never the root Studio bearer', async (t) => {
  const capability = 'c'.repeat(43);
  const runId = 'run-capability-1';
  const requests = [];
  const broker = http.createServer((req, res) => {
    let body = '';
    req.setEncoding('utf8');
    req.on('data', (chunk) => { body += chunk; });
    req.on('end', () => {
      requests.push({
        authorization: req.headers.authorization || '',
        capability: req.headers['x-simworld-run-capability'] || '',
        runId: req.headers['x-simworld-run-id'] || '',
        body: JSON.parse(body),
      });
      res.setHeader('Content-Type', 'application/json');
      res.end(JSON.stringify({ ok: true, result: { actors: [] } }));
    });
  });
  const port = await listen(broker);
  t.after(() => new Promise((resolve) => broker.close(resolve)));

  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'simworld-internal-capability-'));
  const learnedTools = path.join(tmp, 'learned_tools.json');
  fs.writeFileSync(learnedTools, '[]\n', 'utf8');
  t.after(() => fs.rmSync(tmp, { recursive: true, force: true }));
  const proc = spawn(process.execPath, [MCP_SERVER], {
    env: {
      ...process.env,
      LEARNED_TOOLS_FILE: learnedTools,
      PORT: String(port),
      SIMWORLD_BROKER_HOST: '127.0.0.1',
      SIMWORLD_INTERNAL_RUN_CAPABILITY: capability,
      SIMWORLD_INTERNAL_RUN_ID: runId,
      SIMWORLD_INTERNAL_CAPABILITY_REQUIRED: '1',
      STUDIO_ACCESS_TOKEN: TOKEN,
    },
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  t.after(() => { if (!proc.killed) proc.kill('SIGTERM'); });
  const responses = new Map();
  let stdout = '';
  let stderr = '';
  proc.stdout.on('data', (chunk) => {
    stdout += chunk.toString();
    const lines = stdout.split('\n');
    stdout = lines.pop() || '';
    for (const line of lines) {
      try {
        const response = JSON.parse(line);
        if (Object.prototype.hasOwnProperty.call(response, 'id')) responses.set(response.id, response);
      } catch {}
    }
  });
  proc.stderr.on('data', (chunk) => { stderr += chunk.toString(); });
  proc.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'initialize', params: {} })}\n`);
  proc.stdin.write(`${JSON.stringify({
    jsonrpc: '2.0', id: 2, method: 'tools/call',
    params: { name: 'get_actors_in_level', arguments: {} },
  })}\n`);
  await waitFor(() => responses.has(2));
  assert.equal(responses.get(2).result.isError, false, stderr);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].authorization, '');
  assert.equal(requests[0].capability, capability);
  assert.equal(requests[0].runId, runId);
  assert.doesNotMatch(stderr, new RegExp(capability));
  assert.doesNotMatch(stderr, new RegExp(TOKEN));
});

test('MCP broker calls fail closed before the network when the token is missing', async (t) => {
  let requestCount = 0;
  const broker = http.createServer((_req, res) => {
    requestCount += 1;
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({ ok: true, result: { actors: [] } }));
  });
  const port = await listen(broker);
  t.after(() => new Promise((resolve) => broker.close(resolve)));

  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'simworld-internal-auth-missing-'));
  const learnedTools = path.join(tmp, 'learned_tools.json');
  fs.writeFileSync(learnedTools, '[]\n', 'utf8');
  t.after(() => fs.rmSync(tmp, { recursive: true, force: true }));

  const env = {
    ...process.env,
    LEARNED_TOOLS_FILE: learnedTools,
    PORT: String(port),
    SIMWORLD_BROKER_HOST: '127.0.0.1',
  };
  delete env.STUDIO_ACCESS_TOKEN;
  const proc = spawn(process.execPath, [MCP_SERVER], {
    env,
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  t.after(() => { if (!proc.killed) proc.kill('SIGTERM'); });

  const responses = new Map();
  let stdout = '';
  proc.stdout.on('data', (chunk) => {
    stdout += chunk.toString();
    const lines = stdout.split('\n');
    stdout = lines.pop() || '';
    for (const line of lines) {
      try {
        const response = JSON.parse(line);
        if (Object.prototype.hasOwnProperty.call(response, 'id')) responses.set(response.id, response);
      } catch {}
    }
  });

  proc.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'initialize', params: {} })}\n`);
  proc.stdin.write(`${JSON.stringify({
    jsonrpc: '2.0',
    id: 2,
    method: 'tools/call',
    params: { name: 'get_actors_in_level', arguments: {} },
  })}\n`);

  await waitFor(() => responses.has(2));
  assert.equal(responses.get(2).result.isError, true);
  assert.match(
    responses.get(2).result.content[0].text,
    /STUDIO_ACCESS_TOKEN is unavailable to the SimWorld MCP subprocess/,
  );
  assert.equal(requestCount, 0, 'missing credentials must not send an unauthenticated broker request');
});

test('scene review loops authenticate their internal chat requests', () => {
  for (const filename of ['scene-loop.js', 'scene-loop-visual.js']) {
    const source = fs.readFileSync(path.resolve(__dirname, `../${filename}`), 'utf8');
    assert.match(source, /requestInternalSse/);
  }
  const internalClient = fs.readFileSync(path.resolve(__dirname, '../internal-http.js'), 'utf8');
  assert.match(internalClient, /Authorization: `Bearer \$\{accessToken\}`/);
  assert.match(internalClient, /requireStudioAccessToken/);
});

test('scene checks resolve both Unreal object names and actor labels', () => {
  const source = fs.readFileSync(MCP_SERVER, 'utf8');
  assert.match(source, /nm = a\.get_name\(\)[\s\S]{0,100}by_label\[nm\] = a/);
  assert.match(source, /lbl = a\.get_actor_label\(\)[\s\S]{0,100}by_label\[lbl\] = a/);
  assert.match(source, /other\['top_z'\] > a\['bot_z'\] \+ TOUCH_TOL/);
  assert.doesNotMatch(source, /other\['top_z'\] >= a\['bot_z'\] - 1/);
});

test('safe scene presentation tools are registered without exposing arbitrary code', () => {
  const source = fs.readFileSync(MCP_SERVER, 'utf8');
  assert.match(source, /name:"set_camera"/);
  assert.match(source, /TOOL_HANDLERS\.set_camera=toolSetCamera/);
  assert.match(source, /name:"set_actor_color"/);
  assert.match(source, /TOOL_HANDLERS\.set_actor_color=toolSetActorColor/);
  assert.match(source, /BasicShapeMaterial\.BasicShapeMaterial/);
  assert.match(source, /create_dynamic_material_instance\(0, base\)/);
});

test('legacy MCP scene verification is not advertised and cannot bypass Review coordination', () => {
  const source = fs.readFileSync(MCP_SERVER, 'utf8');
  assert.doesNotMatch(source, /name:"verify_scene"/);
  assert.doesNotMatch(source, /verify_scene:toolVerifyScene/);
  assert.doesNotMatch(source, /const\{runCritic\}=require\("\.\/scene-critic"\)/);
  assert.doesNotMatch(source, /toolVerifyScene/);
  assert.doesNotMatch(source, /dangerously-(?:skip-permissions|bypass-approvals-and-sandbox)/);
});

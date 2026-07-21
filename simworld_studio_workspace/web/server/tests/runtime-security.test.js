"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const {
  codingAgentsEnabled,
  createAccessGuard,
  createLoopbackBrowserHeaders,
  createModelGate,
  createTransportBrowserHeaders,
  createTransportRequestGuard,
  requestIsLoopback,
  resolveAccessToken,
  resolveBindHost,
  resolveContainedFile,
  resolveModelMode,
  resolveVistaDemoFps,
  setLoopbackBrowserHeaders,
} = require("../runtime-security");
const { resolveTransportProfile } = require("../pixel-streaming-config");

const ACCESS_TOKEN = "t".repeat(43);

test("bind and mode defaults fail closed", () => {
  assert.equal(resolveBindHost({}), "127.0.0.1");
  assert.equal(resolveBindHost({ STUDIO_HOST: "::1" }), "::1");
  for (const host of ["0.0.0.0", "::", "localhost", "example.com"]) {
    assert.throws(() => resolveBindHost({ STUDIO_HOST: host }), /numeric loopback/);
  }
  assert.equal(resolveModelMode({}), "off");
  assert.equal(resolveModelMode({ STUDIO_MODEL_MODE: "mock" }), "mock");
  assert.throws(() => resolveModelMode({ STUDIO_MODEL_MODE: "auto" }), /off, mock, or live/);
  assert.equal(codingAgentsEnabled({}), false);
  assert.equal(codingAgentsEnabled({ STUDIO_CODING_AGENTS_ENABLED: "1" }), true);
  assert.throws(() => codingAgentsEnabled({ STUDIO_CODING_AGENTS_ENABLED: "yes" }));
  assert.equal(resolveVistaDemoFps({}), 60);
  assert.equal(resolveVistaDemoFps({ VISTA_DEMO_ENABLED: "1", VISTA_DEMO_FPS: "30" }), 30);
  assert.equal(resolveVistaDemoFps({ VISTA_DEMO_ENABLED: "1", VISTA_DEMO_FPS: "60" }), 60);
  assert.throws(
    () => resolveVistaDemoFps({ VISTA_DEMO_ENABLED: "1", VISTA_DEMO_FPS: "45" }),
    /30 or 60/,
  );
});

test("browser headers prohibit external web origins and referrer leakage", () => {
  const headers = {};
  let nextCalled = false;
  setLoopbackBrowserHeaders({}, {
    set(name, value) { headers[name] = value; return this; },
  }, () => { nextCalled = true; });
  assert.equal(nextCalled, true);
  assert.equal(headers["Referrer-Policy"], "no-referrer");
  assert.equal(headers["X-Content-Type-Options"], "nosniff");
  assert.match(headers["Content-Security-Policy"], /default-src 'self'/);
  assert.match(headers["Content-Security-Policy"], /connect-src 'self' ws:\/\/127\.0\.0\.1:\*/);
  assert.doesNotMatch(headers["Content-Security-Policy"], /https:\/\/\*/);
  assert.doesNotMatch(headers["Content-Security-Policy"], /fonts\.googleapis|googleapis\.com/);

  const strictHeaders = {};
  createLoopbackBrowserHeaders({ signalingPort: 8585 })({}, {
    set(name, value) { strictHeaders[name] = value; return this; },
  }, () => {});
  assert.match(strictHeaders["Content-Security-Policy"], /ws:\/\/127\.0\.0\.1:8585/);
  assert.doesNotMatch(strictHeaders["Content-Security-Policy"], /127\.0\.0\.1:\*/);
  assert.throws(() => createLoopbackBrowserHeaders({ signalingPort: 0 }), /valid TCP port/);
});

test("access token guard supports bearer and one-time cookie bootstrap", () => {
  assert.throws(() => resolveAccessToken({}), /at least 32/);
  assert.equal(resolveAccessToken({ STUDIO_ACCESS_TOKEN: ACCESS_TOKEN }), ACCESS_TOKEN);
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "studio-access-token-"));
  try {
    const filename = path.join(temporary, "access.token");
    fs.writeFileSync(filename, `${ACCESS_TOKEN}\n`, { mode: 0o600 });
    assert.equal(resolveAccessToken({ STUDIO_ACCESS_TOKEN_FILE: filename }), ACCESS_TOKEN);
    assert.throws(
      () => resolveAccessToken({ STUDIO_ACCESS_TOKEN: ACCESS_TOKEN, STUDIO_ACCESS_TOKEN_FILE: filename }),
      /only one/,
    );
  } finally {
    fs.rmSync(temporary, { recursive: true, force: true });
  }
  const guard = createAccessGuard(ACCESS_TOKEN);
  let nextCalled = false;
  guard(
    { method: "GET", headers: { authorization: `Bearer ${ACCESS_TOKEN}` }, query: {} },
    {},
    () => { nextCalled = true; },
  );
  assert.equal(nextCalled, true);

  let cookie = "";
  let location = "";
  guard(
    {
      method: "GET",
      headers: {},
      query: { token: ACCESS_TOKEN },
      originalUrl: `/?token=${ACCESS_TOKEN}`,
      url: `/?token=${ACCESS_TOKEN}`,
    },
    {
      setHeader(name, value) { if (name === "Set-Cookie") cookie = value; },
      redirect(status, value) { assert.equal(status, 302); location = value; },
    },
    () => assert.fail("query bootstrap should redirect"),
  );
  assert.match(cookie, /HttpOnly; SameSite=Strict/);
  assert.equal(location, "/");
  assert.doesNotMatch(location, /token/);

  const trusted = resolveTransportProfile({
    STUDIO_TRANSPORT_PROFILE: "trusted_proxy",
    STUDIO_PUBLIC_ORIGIN: "https://studio.example.test",
    STUDIO_TRUSTED_PROXY: "127.0.0.1",
  });
  const publicGuard = createAccessGuard(ACCESS_TOKEN, { transport: trusted });
  let publicCookie = "";
  publicGuard(
    {
      method: "GET",
      headers: {},
      query: { token: ACCESS_TOKEN },
      originalUrl: `/?token=${ACCESS_TOKEN}`,
      url: `/?token=${ACCESS_TOKEN}`,
    },
    {
      setHeader(name, value) { if (name === "Set-Cookie") publicCookie = value; },
      redirect() {},
    },
    () => assert.fail("query bootstrap should redirect"),
  );
  assert.match(publicCookie, /; Secure$/);
});

test("transport middleware admits only the resolved loopback or trusted proxy authority", () => {
  const loopback = resolveTransportProfile({});
  const loopbackHeaders = {};
  let nextCalled = false;
  createTransportBrowserHeaders(loopback, { signalingPort: 8585 })({}, {
    set(name, value) { loopbackHeaders[name] = value; return this; },
  }, () => { nextCalled = true; });
  assert.equal(nextCalled, true);
  assert.match(loopbackHeaders["Content-Security-Policy"], /ws:\/\/127\.0\.0\.1:8585/);
  assert.equal(loopbackHeaders["Cross-Origin-Resource-Policy"], "same-origin");

  const trusted = resolveTransportProfile({
    STUDIO_TRANSPORT_PROFILE: "trusted_proxy",
    STUDIO_PUBLIC_ORIGIN: "https://studio.example.test",
    STUDIO_TRUSTED_PROXY: "127.0.0.1",
  });
  const publicHeaders = {};
  createTransportBrowserHeaders(trusted)({}, {
    set(name, value) { publicHeaders[name] = value; return this; },
  }, () => {});
  assert.match(publicHeaders["Content-Security-Policy"], /wss:\/\/studio\.example\.test/);
  assert.doesNotMatch(publicHeaders["Content-Security-Policy"], /localhost|127\.0\.0\.1/);

  const request = {
    headers: {
      host: "studio.example.test",
      "x-forwarded-proto": "https",
      "x-forwarded-host": "studio.example.test",
      "x-forwarded-port": "443",
    },
    socket: { remoteAddress: "127.0.0.1" },
  };
  let allowed = false;
  createTransportRequestGuard(trusted)(request, {}, () => { allowed = true; });
  assert.equal(allowed, true);

  let status = null;
  let payload = null;
  createTransportRequestGuard(trusted)(
    { ...request, socket: { remoteAddress: "10.0.0.5" } },
    {
      status(value) { status = value; return this; },
      json(value) { payload = value; return this; },
    },
    () => assert.fail("untrusted source must be rejected"),
  );
  assert.equal(status, 403);
  assert.equal(payload.code, "TRANSPORT_REQUEST_REJECTED");
});

test("screenshot files must resolve inside an approved realpath root", () => {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "studio-security-"));
  try {
    const root = path.join(temporary, "screens");
    fs.mkdirSync(root);
    const inside = path.join(root, "frame.png");
    const outside = path.join(temporary, "secret.txt");
    fs.writeFileSync(inside, "image");
    fs.writeFileSync(outside, "secret");
    const escape = path.join(root, "escape.png");
    fs.symlinkSync(outside, escape);
    assert.equal(resolveContainedFile(inside, [root]), fs.realpathSync(inside));
    assert.equal(resolveContainedFile(outside, [root]), null);
    assert.equal(resolveContainedFile(escape, [root]), null);
  } finally {
    fs.rmSync(temporary, { recursive: true, force: true });
  }
});

test("request guard accepts only matching loopback Host and Origin", () => {
  const request = (host, origin) => ({ headers: { host, ...(origin ? { origin } : {}) } });
  assert.equal(requestIsLoopback(request("127.0.0.1:3002")), true);
  assert.equal(requestIsLoopback(request("localhost:3002", "http://localhost:3002")), true);
  assert.equal(requestIsLoopback(request("[::1]:3002", "https://[::1]:3002")), true);
  assert.equal(requestIsLoopback(request("example.com:3002")), false);
  assert.equal(requestIsLoopback(request("0.0.0.0:3002")), false);
  assert.equal(requestIsLoopback(request("localhost:3002", "http://localhost:4000")), false);
  assert.equal(requestIsLoopback(request("localhost:3002", "http://127.0.0.1:3002")), false);
  assert.equal(requestIsLoopback(request("localhost:3002", "https://example.com")), false);
});

function invokeGate(options, path, body = {}, method = "POST") {
  let nextCalled = false;
  let statusCode = null;
  let responseBody = null;
  const req = { method, path, url: path, body };
  createModelGate(options)(
    req,
    {
      status(code) { statusCode = code; return this; },
      json(value) { responseBody = value; return this; },
    },
    () => { nextCalled = true; },
  );
  return { nextCalled, statusCode, responseBody, requestBody: req.body };
}

test("off mode blocks every external model or workload endpoint", () => {
  const paths = [
    "/api/chat",
    "/api/skills/select",
    "/api/agent-chat",
    "/api/agent-broadcast",
    "/api/arena/run",
    "/api/arena/battles/b1/run",
    "/api/vlm-score",
    "/api/training/start",
  ];
  for (const path of paths) {
    const result = invokeGate({ mode: "off" }, path);
    assert.equal(result.nextCalled, false, path);
    assert.equal(result.statusCode, 503, path);
    assert.equal(result.responseBody.code, "MODEL_CALLS_DISABLED", path);
  }
  for (const path of ["/api/ue-command", "/api/camera", "/api/tasks/generate", "/api/unknown"]) {
    const result = invokeGate({ mode: "off" }, path);
    assert.equal(result.nextCalled, false, path);
    assert.equal(result.statusCode, 503, path);
  }
  assert.equal(invokeGate({ mode: "off" }, "/api/health", {}, "GET").nextCalled, true);
  assert.equal(invokeGate({ mode: "off" }, "/api/session/heartbeat").nextCalled, true);
});

test("model-off admits only the exact fixed VISTA setup and stop mutations", () => {
  for (const mode of ["off", "mock"]) {
    for (const route of [
      "/api/vista/setup_vista_play_mode",
      "/api/vista/stop_vista_play_mode",
    ]) {
      assert.equal(invokeGate({ mode }, route, {}).nextCalled, true, `${mode} ${route}`);
    }
  }
  for (const path of [
    "/api/vista/setup_vista_play_mode/",
    "/api/vista/setup_vista_play_mode_extra",
    "/api/vista/stop_vista_play_mode/",
    "/api/vista/stop_vista_play_mode_extra",
    "/api/vista/command",
    "/api/vista/execute_python_script",
    "/api/internal/ue",
  ]) {
    const result = invokeGate({ mode: "off" }, path);
    assert.equal(result.nextCalled, false, path);
    assert.equal(result.statusCode, 503, path);
    assert.equal(result.responseBody.code, "MODEL_CALLS_DISABLED", path);
  }
  assert.equal(
    invokeGate({ mode: "off" }, "/api/vista/get_vista_state", {}, "GET").nextCalled,
    true,
  );
});

test("VISTA demo mode blocks GET routes that mutate UE or bypass the shared broker", () => {
  for (const path of [
    "/api/agent-camera/Pedestrian_1",
    "/api/agent-camera/Pedestrian_1/",
    "/api/saved-maps",
    "/api/saved-maps/",
    "/api/saved-maps/ReviewedMap/download",
    "/api/asset-ls",
    "/api/asset-ls/",
  ]) {
    const result = invokeGate({ mode: "off", demoMode: true }, path, {}, "GET");
    assert.equal(result.nextCalled, false, path);
    assert.equal(result.statusCode, 503, path);
    assert.equal(result.responseBody.code, "MODEL_CALLS_DISABLED", path);
  }
  assert.equal(
    invokeGate({ mode: "off", demoMode: true }, "/api/vista/get_vista_state", {}, "GET").nextCalled,
    true,
  );
  assert.equal(
    invokeGate({ mode: "off", demoMode: false }, "/api/saved-maps", {}, "GET").nextCalled,
    true,
  );
});

test("mock mode allows only ready chat and forces a non-agent path", () => {
  assert.equal(invokeGate({ mode: "mock" }, "/api/chat").nextCalled, false);
  const result = invokeGate(
    { mode: "mock", isMockReady: () => true },
    "/api/chat",
    { loopMode: "visual_loop", useLoop: true, skillSelectionMode: "auto" },
  );
  assert.equal(result.nextCalled, true);
  assert.equal(result.requestBody.loopMode, "vanilla");
  assert.equal(result.requestBody.useLoop, false);
  assert.equal(result.requestBody.skillSelectionMode, "manual");
  assert.equal(invokeGate({ mode: "mock", isMockReady: () => true }, "/api/vlm-score").nextCalled, false);
});

test("off and mock modes permit only disabling loop features", () => {
  assert.equal(invokeGate({ mode: "off" }, "/api/scene-loop", { mode: "vanilla" }).nextCalled, true);
  assert.equal(invokeGate({ mode: "off" }, "/api/scene-loop", { mode: "visual_loop" }).nextCalled, false);
  assert.equal(invokeGate({ mode: "mock" }, "/api/dynamic-skills", { enabled: false }).nextCalled, true);
  assert.equal(invokeGate({ mode: "mock" }, "/api/dynamic-skills", { enabled: true }).nextCalled, false);
});

test("live mode still requires a separate coding-agent opt-in", () => {
  assert.equal(invokeGate({ mode: "live" }, "/api/chat").nextCalled, false);
  assert.equal(invokeGate({ mode: "live", allowCodingAgents: true }, "/api/chat").nextCalled, true);
  assert.equal(invokeGate({ mode: "live" }, "/api/vlm-score").nextCalled, true);
});

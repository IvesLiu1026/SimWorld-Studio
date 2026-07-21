"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const http = require("node:http");
const net = require("node:net");
const {
  PixelStreamingGatewayError,
  SESSION_COOKIE,
  createStudioStreamingRuntime,
  exactCookieValue,
  serializeSessionCookie,
  sessionTokenFromRequest,
  upstreamUpgradeRequest,
  validUpgradeRequest,
} = require("../pixel-streaming-gateway");
const { resolveTransportProfile } = require("../pixel-streaming-config");

const ACCESS_TOKEN = "studio-access-token-for-gateway-tests-".repeat(2);
const SESSION_TOKEN = "a".repeat(64);

class FakeSessionManager extends EventEmitter {
  constructor(cirrusHttpPort = 8585) {
    super();
    this.totalSlots = 4;
    this.freeSlots = 3;
    this.queueLength = 0;
    this.record = {
      token: SESSION_TOKEN,
      leaseId: "lease-gateway-test",
      userId: "browser-gateway-test",
      slotId: 1,
      uePorts: { cirrusHttp: cirrusHttpPort },
      lastActivity: Date.now(),
    };
    this.active = false;
  }

  async acquire(userId) {
    this.record.userId = userId;
    this.active = true;
    this.freeSlots = 3;
    return this.record;
  }

  touch(token) {
    if (!this.active || token !== this.record.token) return null;
    this.record.lastActivity = Date.now();
    return this.record;
  }

  release(token) {
    if (!this.active || token !== this.record.token) return;
    this.active = false;
    this.freeSlots = 4;
    this.emit("released", { token, slotId: this.record.slotId, reason: "released" });
  }
}

function env(overrides = {}) {
  return {
    STUDIO_ACCESS_TOKEN: ACCESS_TOKEN,
    SESSION_TTL_MS: "1800000",
    SESSION_HARD_MAX_MS: "3600000",
    ...overrides,
  };
}

function responseHarness() {
  return {
    statusCode: 200,
    headers: {},
    body: null,
    setHeader(name, value) { this.headers[name.toLowerCase()] = value; return this; },
    status(value) { this.statusCode = value; return this; },
    json(value) { this.body = value; return this; },
  };
}

function cookieRequest(cookie = "") {
  return { headers: cookie ? { cookie } : {} };
}

function listen(server) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => resolve(server.address().port));
  });
}

function close(server) {
  return new Promise((resolve) => server.close(resolve));
}

function rawUpgrade(port, path, cookie) {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection({ host: "127.0.0.1", port });
    let response = "";
    socket.setTimeout(5_000, () => socket.destroy(new Error("upgrade timed out")));
    socket.once("connect", () => {
      socket.write([
        `GET ${path} HTTP/1.1`,
        `Host: 127.0.0.1:${port}`,
        `Origin: http://127.0.0.1:${port}`,
        "Connection: Upgrade",
        "Upgrade: websocket",
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==",
        "Sec-WebSocket-Version: 13",
        `Cookie: ${cookie}`,
        "",
        "",
      ].join("\r\n"));
    });
    socket.on("data", (chunk) => {
      response += chunk.toString("latin1");
      if (response.includes("\r\n\r\n")) {
        socket.destroy();
        resolve(response);
      }
    });
    socket.once("error", reject);
    socket.once("end", () => resolve(response));
  });
}

test("session cookies are exact, HttpOnly, profile-aware, and ambiguity fails closed", () => {
  const loopback = resolveTransportProfile({});
  const trusted = resolveTransportProfile({
    STUDIO_TRANSPORT_PROFILE: "trusted_proxy",
    STUDIO_PUBLIC_ORIGIN: "https://studio.example.test",
    STUDIO_TRUSTED_PROXY: "127.0.0.1",
  });
  const localCookie = serializeSessionCookie(SESSION_TOKEN, loopback, { maxAgeMs: 60_000 });
  const publicCookie = serializeSessionCookie(SESSION_TOKEN, trusted, { maxAgeMs: 60_000 });
  assert.match(localCookie, /^vista_stream_session=[a-f0-9]{64}; Path=\/; HttpOnly; SameSite=Strict; Max-Age=60$/);
  assert.doesNotMatch(localCookie, /Secure/);
  assert.match(publicCookie, /; Secure; Max-Age=60$/);
  assert.equal(exactCookieValue(`${SESSION_COOKIE}=${SESSION_TOKEN}`, SESSION_COOKIE), SESSION_TOKEN);
  assert.equal(
    exactCookieValue(`${SESSION_COOKIE}=${SESSION_TOKEN}; ${SESSION_COOKIE}=${SESSION_TOKEN}`, SESSION_COOKIE),
    "",
  );
  assert.equal(sessionTokenFromRequest(cookieRequest(`${SESSION_COOKIE}=${SESSION_TOKEN}`)), SESSION_TOKEN);
  assert.equal(sessionTokenFromRequest(cookieRequest(`${SESSION_COOKIE}=not-a-token`)), "");
});

test("trusted proxy startup requires an independent endpoint HMAC secret", () => {
  const manager = new FakeSessionManager();
  assert.throws(
    () => createStudioStreamingRuntime({
      env: env({
        STUDIO_TRANSPORT_PROFILE: "trusted_proxy",
        STUDIO_PUBLIC_ORIGIN: "https://studio.example.test",
        STUDIO_TRUSTED_PROXY: "127.0.0.1",
      }),
      sessionManager: manager,
    }),
    (error) => error instanceof PixelStreamingGatewayError &&
      error.code === "PIXEL_STREAMING_ENDPOINT_SECRET_REQUIRED",
  );
});

test("session and endpoint HTTP contracts expose neither bearer token nor raw UE ports", async () => {
  const manager = new FakeSessionManager(8595);
  const runtime = createStudioStreamingRuntime({ env: env(), sessionManager: manager, webRtcFps: 30 });
  try {
    const acquired = responseHarness();
    await runtime.acquireSession(cookieRequest(), acquired);
    assert.equal(acquired.statusCode, 200);
    assert.equal(acquired.body.schema, "studio-session/v2");
    assert.equal(acquired.body.slotId, 1);
    assert.equal(Object.hasOwn(acquired.body, "token"), false);
    assert.equal(Object.hasOwn(acquired.body, "uePorts"), false);
    const cookie = acquired.headers["set-cookie"].split(";", 1)[0];

    const endpoint = responseHarness();
    runtime.issueEndpoint(cookieRequest(cookie), endpoint);
    assert.equal(endpoint.statusCode, 200);
    assert.equal(endpoint.body.schema, "pixel-streaming-endpoint/v1");
    assert.match(endpoint.body.path, /^\/pixel-stream\/session\/ps1_[A-Za-z0-9_-]{43}$/);
    assert.equal(endpoint.body.webRtcFps, 30);
    assert.equal(Object.hasOwn(endpoint.body, "port"), false);
    assert.equal(Object.hasOwn(endpoint.body, "url"), false);
    assert.doesNotMatch(JSON.stringify(endpoint.body), /8595|127\.0\.0\.1|browser-gateway-test/);

    const heartbeat = responseHarness();
    runtime.heartbeatSession(cookieRequest(cookie), heartbeat);
    assert.equal(heartbeat.body.schema, "studio-session-heartbeat/v2");
    assert.equal(heartbeat.body.ok, true);

    const released = responseHarness();
    runtime.releaseSession(cookieRequest(cookie), released);
    assert.equal(released.body.ok, true);
    assert.match(released.headers["set-cookie"], /Max-Age=0/);

    const unavailable = responseHarness();
    runtime.issueEndpoint(cookieRequest(cookie), unavailable);
    assert.equal(unavailable.statusCode, 401);
  } finally {
    runtime.destroy();
  }
});

test("upgrade validation preserves only the WebSocket allowlist", () => {
  const request = {
    method: "GET",
    headers: {
      connection: "keep-alive, Upgrade",
      upgrade: "websocket",
      "sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ==",
      "sec-websocket-version": "13",
      "sec-websocket-protocol": "binary",
      origin: "https://studio.example.test",
      cookie: "studio-secret=must-not-pass",
      authorization: "Bearer must-not-pass",
      "x-forwarded-for": "203.0.113.2",
    },
  };
  assert.equal(validUpgradeRequest(request), true);
  const serialized = upstreamUpgradeRequest(request, { hostname: "127.0.0.1", port: 8595 });
  assert.match(serialized, /^GET \/ HTTP\/1\.1\r\nHost: 127\.0\.0\.1:8595/m);
  assert.match(serialized, /Origin: https:\/\/studio\.example\.test/);
  assert.match(serialized, /Sec-WebSocket-Protocol: binary/);
  assert.doesNotMatch(serialized, /Cookie|Authorization|X-Forwarded|must-not-pass|203\.0\.113\.2/i);
});

test("opaque upgrade proxies only the authenticated session to Cirrus HttpPort", async () => {
  let upstreamRequest = "";
  let upstreamConnections = 0;
  const upstreamServer = net.createServer((socket) => {
    upstreamConnections += 1;
    socket.on("data", (chunk) => {
      upstreamRequest += chunk.toString("latin1");
      if (upstreamRequest.includes("\r\n\r\n")) {
        socket.write(
          "HTTP/1.1 101 Switching Protocols\r\n" +
          "Connection: Upgrade\r\n" +
          "Upgrade: websocket\r\n\r\n",
        );
      }
    });
  });
  const upstreamPort = await listen(upstreamServer);
  const manager = new FakeSessionManager(upstreamPort);
  manager.active = true;
  const runtime = createStudioStreamingRuntime({ env: env(), sessionManager: manager });
  const endpointResponse = responseHarness();
  runtime.issueEndpoint(
    cookieRequest(`${SESSION_COOKIE}=${SESSION_TOKEN}`),
    endpointResponse,
  );

  const frontendServer = http.createServer((_request, response) => response.end("not-upgrade"));
  runtime.attach(frontendServer);
  const frontendPort = await listen(frontendServer);
  try {
    const accepted = await rawUpgrade(
      frontendPort,
      endpointResponse.body.path,
      `${SESSION_COOKIE}=${SESSION_TOKEN}`,
    );
    assert.match(accepted, /^HTTP\/1\.1 101 Switching Protocols/);
    assert.equal(upstreamConnections, 1);
    assert.match(upstreamRequest, new RegExp(`^GET / HTTP/1\\.1\\r\\nHost: 127\\.0\\.0\\.1:${upstreamPort}`, "m"));
    assert.doesNotMatch(upstreamRequest, /Cookie|X-Forwarded|attacker\.invalid/i);

    const rejected = await rawUpgrade(
      frontendPort,
      endpointResponse.body.path,
      `${SESSION_COOKIE}=${"b".repeat(64)}`,
    );
    assert.match(rejected, /^HTTP\/1\.1 401 Unauthorized/);
    assert.equal(upstreamConnections, 1);

    const wrongPath = await rawUpgrade(
      frontendPort,
      "/pixel-stream/session/ps1_BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
      `${SESSION_COOKIE}=${SESSION_TOKEN}`,
    );
    assert.match(wrongPath, /^HTTP\/1\.1 404 Not Found/);
    assert.equal(upstreamConnections, 1);
  } finally {
    runtime.destroy();
    await close(frontendServer);
    await close(upstreamServer);
  }
});

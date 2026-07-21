"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const http = require("node:http");
const net = require("node:net");
const {
  PixelStreamingGatewayError,
  PRINCIPAL_COOKIE,
  SESSION_COOKIE,
  createStudioStreamingRuntime,
  exactCookieValue,
  serializePrincipalCookie,
  serializeSessionCookie,
  sessionTokenFromRequest,
  upstreamUpgradeRequest,
  validUpgradeRequest,
} = require("../pixel-streaming-gateway");
const { resolveTransportProfile } = require("../pixel-streaming-config");

const ACCESS_TOKEN = "studio-access-token-for-gateway-tests-".repeat(2);
const SESSION_TOKEN = "a".repeat(64);

class FakeSessionManager extends EventEmitter {
  constructor(cirrusHttpPort = 8585, { mcpPort = 55561, token = SESSION_TOKEN } = {}) {
    super();
    this.totalSlots = 4;
    this.freeSlots = 3;
    this.queueLength = 0;
    this.acquireUserIds = [];
    this.record = {
      token,
      leaseId: "lease-gateway-test",
      userId: null,
      slotId: 1,
      uePorts: { cirrusHttp: cirrusHttpPort, mcpPort },
      mcpReady: true,
      lastActivity: Date.now(),
    };
    this.active = false;
  }

  async acquire(userId) {
    this.acquireUserIds.push(userId);
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
    getHeader(name) { return this.headers[name.toLowerCase()]; },
    status(value) { this.statusCode = value; return this; },
    json(value) { this.body = value; return this; },
  };
}

function cookieRequest(cookie = "") {
  return { headers: cookie ? { cookie } : {} };
}

function setCookieValues(response) {
  const value = response.headers["set-cookie"];
  if (value === undefined) return [];
  return Array.isArray(value) ? value : [value];
}

function cookiePair(response, name) {
  const serialized = setCookieValues(response).find((value) => value.startsWith(`${name}=`));
  return serialized ? serialized.split(";", 1)[0] : "";
}

function cookieHeader(...pairs) {
  return pairs.filter(Boolean).join("; ");
}

async function acquireCookies(runtime, request = cookieRequest()) {
  const response = responseHarness();
  await runtime.acquireSession(request, response);
  return {
    response,
    principal: cookiePair(response, PRINCIPAL_COOKIE),
    session: cookiePair(response, SESSION_COOKIE),
  };
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

test("session and principal cookies are HttpOnly, persistent, profile-aware, and exact", () => {
  const loopback = resolveTransportProfile({});
  const trusted = resolveTransportProfile({
    STUDIO_TRANSPORT_PROFILE: "trusted_proxy",
    STUDIO_PUBLIC_ORIGIN: "https://studio.example.test",
    STUDIO_TRUSTED_PROXY: "127.0.0.1",
  });
  const localCookie = serializeSessionCookie(SESSION_TOKEN, loopback, { maxAgeMs: 60_000 });
  const publicCookie = serializeSessionCookie(SESSION_TOKEN, trusted, { maxAgeMs: 60_000 });
  const principalValue = `bp1.${"A".repeat(43)}.1000.2000.${"B".repeat(43)}`;
  const localPrincipal = serializePrincipalCookie(principalValue, loopback, { maxAgeMs: 86_400_000 });
  const publicPrincipal = serializePrincipalCookie(principalValue, trusted, { maxAgeMs: 86_400_000 });
  assert.match(localCookie, /^vista_stream_session=[a-f0-9]{64}; Path=\/; HttpOnly; SameSite=Strict; Max-Age=60$/);
  assert.doesNotMatch(localCookie, /Secure/);
  assert.match(publicCookie, /; Secure; Max-Age=60$/);
  assert.match(
    localPrincipal,
    /^vista_browser_principal=bp1\.[A-Za-z0-9._-]+; Path=\/; HttpOnly; SameSite=Strict; Max-Age=86400$/,
  );
  assert.doesNotMatch(localPrincipal, /Secure/);
  assert.match(publicPrincipal, /; Secure; Max-Age=86400$/);
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
    const { response: acquired, principal, session } = await acquireCookies(runtime);
    assert.equal(acquired.statusCode, 200);
    assert.equal(acquired.body.schema, "studio-session/v2");
    assert.equal(acquired.body.slotId, 1);
    assert.equal(Object.hasOwn(acquired.body, "token"), false);
    assert.equal(Object.hasOwn(acquired.body, "uePorts"), false);
    assert.equal(setCookieValues(acquired).length, 2);
    assert.match(principal, /^vista_browser_principal=bp1\./);
    assert.match(session, /^vista_stream_session=[a-f0-9]{64}$/);
    assert.ok(setCookieValues(acquired).every((value) => /; HttpOnly; SameSite=Strict;/.test(value)));
    const cookies = cookieHeader(principal, session);
    assert.doesNotMatch(
      JSON.stringify(acquired.body),
      /vista_browser_principal|vista_stream_session|browser-|lease-gateway-test|55561|8595/,
    );

    const endpoint = responseHarness();
    runtime.issueEndpoint(cookieRequest(cookies), endpoint);
    assert.equal(endpoint.statusCode, 200);
    assert.equal(endpoint.body.schema, "pixel-streaming-endpoint/v1");
    assert.match(endpoint.body.path, /^\/pixel-stream\/session\/ps1_[A-Za-z0-9_-]{43}$/);
    assert.equal(endpoint.body.webRtcFps, 30);
    assert.equal(Object.hasOwn(endpoint.body, "port"), false);
    assert.equal(Object.hasOwn(endpoint.body, "url"), false);
    assert.doesNotMatch(JSON.stringify(endpoint.body), /8595|127\.0\.0\.1|browser-gateway-test/);

    const heartbeat = responseHarness();
    runtime.heartbeatSession(cookieRequest(cookies), heartbeat);
    assert.equal(heartbeat.body.schema, "studio-session-heartbeat/v2");
    assert.equal(heartbeat.body.ok, true);
    assert.equal(setCookieValues(heartbeat).length, 2);

    const released = responseHarness();
    runtime.releaseSession(cookieRequest(cookies), released);
    assert.equal(released.body.ok, true);
    assert.equal(setCookieValues(released).length, 1);
    assert.match(setCookieValues(released)[0], /^vista_stream_session=.*Max-Age=0/);
    assert.doesNotMatch(setCookieValues(released)[0], /vista_browser_principal/);

    const unavailable = responseHarness();
    runtime.issueEndpoint(cookieRequest(cookies), unavailable);
    assert.equal(unavailable.statusCode, 401);
  } finally {
    runtime.destroy();
  }
});

test("session acquisition failures keep internal tokens and ports out of HTTP responses", async () => {
  const manager = new FakeSessionManager();
  manager.acquire = async () => {
    const error = new Error(`failed ${SESSION_TOKEN} on 127.0.0.1:55561`);
    error.code = `PORT_${SESSION_TOKEN}`;
    throw error;
  };
  const logged = [];
  const runtime = createStudioStreamingRuntime({
    env: env(),
    sessionManager: manager,
    logger: (...items) => logged.push(items.join(" ")),
  });
  try {
    const acquired = await acquireCookies(runtime);
    assert.equal(acquired.response.statusCode, 503);
    assert.deepEqual(acquired.response.body, {
      error: "Studio session is unavailable",
      code: "UNAVAILABLE",
      queueLength: 0,
    });
    assert.doesNotMatch(JSON.stringify(acquired.response.body), /55561|127\.0\.0\.1|a{64}/);
    assert.match(logged.join("\n"), /UNAVAILABLE: session acquisition failed/);
    assert.doesNotMatch(logged.join("\n"), /55561|127\.0\.0\.1|a{64}/);
    assert.equal(setCookieValues(acquired.response).length, 0);
  } finally {
    runtime.destroy();
  }
});

test("signed browser principal keeps a stable owner across runtime restart", async () => {
  const firstManager = new FakeSessionManager();
  const first = createStudioStreamingRuntime({ env: env(), sessionManager: firstManager });
  let principal;
  let firstOwner;
  try {
    const acquired = await acquireCookies(first);
    principal = acquired.principal;
    firstOwner = firstManager.record.userId;
    assert.match(firstOwner, /^browser-[a-f0-9]{64}$/);
    assert.equal(first.resolveActiveSession(cookieRequest(principal)), null);
  } finally {
    first.destroy();
  }

  const secondManager = new FakeSessionManager(8585, { token: "b".repeat(64) });
  const second = createStudioStreamingRuntime({ env: env(), sessionManager: secondManager });
  try {
    const acquired = await acquireCookies(second, cookieRequest(principal));
    assert.equal(acquired.response.statusCode, 200);
    assert.deepEqual(secondManager.acquireUserIds, [firstOwner]);
    assert.equal(secondManager.record.userId, firstOwner);
    assert.doesNotMatch(JSON.stringify(acquired.response.body), /browser-|vista_browser_principal|55561/);

    const resolved = second.resolveActiveSession(cookieRequest(cookieHeader(
      acquired.principal,
      acquired.session,
    )));
    assert.deepEqual(resolved, {
      ownerId: firstOwner,
      sessionId: resolved.sessionId,
      slotId: 1,
      leaseId: "lease-gateway-test",
      mcpPort: 55561,
    });
    assert.match(resolved.sessionId, /^session-[a-f0-9]{64}$/);
    assert.notEqual(resolved.sessionId, secondManager.record.token);
    assert.equal(Object.isFrozen(resolved), true);
    assert.doesNotMatch(JSON.stringify(resolved), new RegExp(secondManager.record.token));
  } finally {
    second.destroy();
  }
});

test("active-session resolver fails closed on cookie, owner, lease, readiness, slot, and port boundaries", async () => {
  const manager = new FakeSessionManager();
  const runtime = createStudioStreamingRuntime({ env: env(), sessionManager: manager });
  try {
    const acquired = await acquireCookies(runtime);
    const validCookies = cookieHeader(acquired.principal, acquired.session);
    const originalOwner = manager.record.userId;
    const originalLease = manager.record.leaseId;
    const originalSlot = manager.record.slotId;
    const originalMcpPort = manager.record.uePorts.mcpPort;
    const principalValue = acquired.principal.slice(acquired.principal.indexOf("=") + 1);
    const last = principalValue.at(-1);
    const tamperedPrincipal = `${PRINCIPAL_COOKIE}=${principalValue.slice(0, -1)}${last === "A" ? "B" : "A"}`;

    assert.ok(runtime.resolveActiveSession(cookieRequest(validCookies)));
    assert.equal(runtime.resolveActiveSession(cookieRequest(acquired.principal)), null);
    assert.equal(runtime.resolveActiveSession(cookieRequest(acquired.session)), null);
    assert.equal(
      runtime.resolveActiveSession(cookieRequest(cookieHeader(
        acquired.principal,
        acquired.principal,
        acquired.session,
      ))),
      null,
    );
    assert.equal(
      runtime.resolveActiveSession(cookieRequest(cookieHeader(tamperedPrincipal, acquired.session))),
      null,
    );
    assert.equal(
      runtime.resolveActiveSession(cookieRequest(cookieHeader(
        acquired.principal,
        `${SESSION_COOKIE}=${"c".repeat(64)}`,
      ))),
      null,
    );

    manager.record.userId = `browser-${"d".repeat(64)}`;
    assert.equal(runtime.resolveActiveSession(cookieRequest(validCookies)), null);
    manager.record.userId = originalOwner;

    manager.record.leaseId = "short";
    assert.equal(runtime.resolveActiveSession(cookieRequest(validCookies)), null);
    manager.record.leaseId = originalLease;

    manager.record.mcpReady = false;
    assert.equal(runtime.resolveActiveSession(cookieRequest(validCookies)), null);
    manager.record.mcpReady = true;

    for (const invalidPort of [0, 65536, "55561", null]) {
      manager.record.uePorts.mcpPort = invalidPort;
      assert.equal(runtime.resolveActiveSession(cookieRequest(validCookies)), null);
    }
    manager.record.uePorts.mcpPort = originalMcpPort;

    manager.record.slotId = manager.totalSlots;
    assert.equal(runtime.resolveActiveSession(cookieRequest(validCookies)), null);
    manager.record.slotId = originalSlot;
    assert.ok(runtime.resolveActiveSession(cookieRequest(validCookies)));
  } finally {
    runtime.destroy();
  }
});

test("principal signatures are scoped to the configured persistent HMAC key", async () => {
  const firstEnv = env({ STUDIO_PIXEL_STREAMING_HMAC_KEY: "gateway-hmac-key-alpha-".repeat(3) });
  const secondEnv = env({ STUDIO_PIXEL_STREAMING_HMAC_KEY: "gateway-hmac-key-bravo-".repeat(3) });
  const firstManager = new FakeSessionManager();
  const first = createStudioStreamingRuntime({ env: firstEnv, sessionManager: firstManager });
  let firstPrincipal;
  let firstOwner;
  try {
    const acquired = await acquireCookies(first);
    firstPrincipal = acquired.principal;
    firstOwner = firstManager.record.userId;
  } finally {
    first.destroy();
  }

  const secondManager = new FakeSessionManager(8585, { token: "e".repeat(64) });
  const second = createStudioStreamingRuntime({ env: secondEnv, sessionManager: secondManager });
  try {
    const acquired = await acquireCookies(second, cookieRequest(firstPrincipal));
    assert.equal(acquired.response.statusCode, 200);
    assert.notEqual(secondManager.record.userId, firstOwner);
    assert.notEqual(acquired.principal, firstPrincipal);
  } finally {
    second.destroy();
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
  const runtime = createStudioStreamingRuntime({ env: env(), sessionManager: manager });
  const acquired = await acquireCookies(runtime);
  const validCookies = cookieHeader(acquired.principal, acquired.session);
  const endpointResponse = responseHarness();
  runtime.issueEndpoint(cookieRequest(validCookies), endpointResponse);

  const frontendServer = http.createServer((_request, response) => response.end("not-upgrade"));
  runtime.attach(frontendServer);
  const frontendPort = await listen(frontendServer);
  try {
    const accepted = await rawUpgrade(
      frontendPort,
      endpointResponse.body.path,
      validCookies,
    );
    assert.match(accepted, /^HTTP\/1\.1 101 Switching Protocols/);
    assert.equal(upstreamConnections, 1);
    assert.match(upstreamRequest, new RegExp(`^GET / HTTP/1\\.1\\r\\nHost: 127\\.0\\.0\\.1:${upstreamPort}`, "m"));
    assert.doesNotMatch(upstreamRequest, /Cookie|X-Forwarded|attacker\.invalid/i);

    const rejected = await rawUpgrade(
      frontendPort,
      endpointResponse.body.path,
      cookieHeader(acquired.principal, `${SESSION_COOKIE}=${"b".repeat(64)}`),
    );
    assert.match(rejected, /^HTTP\/1\.1 401 Unauthorized/);
    assert.equal(upstreamConnections, 1);

    const missingPrincipal = await rawUpgrade(
      frontendPort,
      endpointResponse.body.path,
      acquired.session,
    );
    assert.match(missingPrincipal, /^HTTP\/1\.1 401 Unauthorized/);
    assert.equal(upstreamConnections, 1);

    const wrongPath = await rawUpgrade(
      frontendPort,
      "/pixel-stream/session/ps1_BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
      validCookies,
    );
    assert.match(wrongPath, /^HTTP\/1\.1 404 Not Found/);
    assert.equal(upstreamConnections, 1);
  } finally {
    runtime.destroy();
    await close(frontendServer);
    await close(upstreamServer);
  }
});

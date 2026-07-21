"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  PixelStreamingConfigError,
  assertTransportRequest,
  buildCirrusConfig,
  buildPixelStreamingCsp,
  createOpaqueStreamingEndpoint,
  createTurnRestCredentials,
  normalizePathPrefix,
  redactCirrusConfig,
  resolveTransportProfile,
  verifyOpaqueStreamingEndpoint,
} = require("../pixel-streaming-config");

const ENDPOINT_SECRETS = { endpointHmacKey: "endpoint-hmac-key-".repeat(3) };
const TURN_CREDENTIAL = "turn-credential-from-secret-provider-2026";
const CIRRUS_SECRETS = { turnCredential: TURN_CREDENTIAL };
const ICE = {
  stunUrls: ["stun:stun2.example.test:3478", "stun:stun1.example.test:3478"],
  turnUrls: [
    "turns:turn.example.test:5349?transport=tcp",
    "turn:turn.example.test:3478?transport=udp",
  ],
  turnUsername: "1784000000:studio-session",
  transportPolicy: "relay",
};

function trustedProfile(overrides = {}) {
  return resolveTransportProfile({
    STUDIO_TRANSPORT_PROFILE: "trusted_proxy",
    STUDIO_PUBLIC_ORIGIN: "https://studio.example.test",
    STUDIO_TRUSTED_PROXY: "127.0.0.1,::1",
    ...overrides,
  });
}

function request(headers, remoteAddress = "127.0.0.1") {
  return { headers, socket: { remoteAddress } };
}

function captureError(fn) {
  try {
    fn();
  } catch (error) {
    assert.ok(error instanceof PixelStreamingConfigError);
    assert.match(error.code, /^PIXEL_STREAMING_/);
    return error;
  }
  assert.fail("Expected PixelStreamingConfigError");
}

test("transport profiles default to loopback and reject mixed public settings", () => {
  const loopback = resolveTransportProfile({});
  assert.equal(loopback.profile, "loopback");
  assert.equal(loopback.publicOrigin, null);
  assert.equal(loopback.forwardedHeaders, false);
  assert.deepEqual(loopback.trustedProxyAddresses, []);
  assert.deepEqual(loopback.cookie, { httpOnly: true, sameSite: "Strict", secure: false });
  assert.equal(loopback.streamingPathPrefix, "/pixel-stream/session");
  assert.equal(Object.isFrozen(loopback), true);

  assert.throws(
    () => resolveTransportProfile({ STUDIO_TRANSPORT_PROFILE: "public" }),
    /loopback or trusted_proxy/,
  );
  assert.throws(
    () => resolveTransportProfile({ STUDIO_PUBLIC_ORIGIN: "https://studio.example.test" }),
    /trusted_proxy profile/,
  );
  assert.throws(
    () => resolveTransportProfile({ STUDIO_TRUSTED_PROXY: "127.0.0.1" }),
    /trusted_proxy profile/,
  );
});

test("trusted_proxy requires one canonical HTTPS origin and explicit local proxy addresses", () => {
  const profile = trustedProfile({ STUDIO_TRUSTED_PROXY: "::ffff:127.0.0.1,::1,127.0.0.1" });
  assert.equal(profile.profile, "trusted_proxy");
  assert.equal(profile.publicOrigin, "https://studio.example.test");
  assert.equal(profile.publicHost, "studio.example.test");
  assert.deepEqual(profile.trustedProxyAddresses, ["127.0.0.1", "::1"]);
  assert.equal(profile.forwardedHeaders, true);
  assert.deepEqual(profile.cookie, { httpOnly: true, sameSite: "Strict", secure: true });

  for (const origin of [
    "http://studio.example.test",
    "https://studio.example.test/",
    "https://studio.example.test/player",
    "https://user:password@studio.example.test",
    "https://studio.example.test?slot=1",
  ]) {
    assert.throws(
      () => trustedProfile({ STUDIO_PUBLIC_ORIGIN: origin }),
      /HTTPS origin|exact HTTPS origin/,
      origin,
    );
  }
  for (const proxy of ["", "localhost", "0.0.0.0", "10.0.0.1", "127.0.0.1/32", "*"]) {
    assert.throws(
      () => trustedProfile({ STUDIO_TRUSTED_PROXY: proxy }),
      /STUDIO_TRUSTED_PROXY/,
      proxy,
    );
  }
});

test("loopback requests reject forwarded headers, remote clients, and origin mismatches", () => {
  const profile = resolveTransportProfile({});
  assert.deepEqual(
    assertTransportRequest(
      request({ host: "127.0.0.1:3022", origin: "http://127.0.0.1:3022" }, "::ffff:127.0.0.1"),
      profile,
      { requireOrigin: true },
    ),
    { profile: "loopback", origin: "http://127.0.0.1:3022" },
  );
  assert.throws(
    () => assertTransportRequest(request({ host: "127.0.0.1:3022" }, "10.0.0.8"), profile),
    /not allowed/,
  );
  assert.throws(
    () => assertTransportRequest(request({ host: "studio.example.test" }), profile),
    /loopback authority/,
  );
  assert.throws(
    () => assertTransportRequest(
      request({ host: "localhost:3022", origin: "http://localhost:4000" }),
      profile,
    ),
    /does not match/,
  );
  assert.throws(
    () => assertTransportRequest(
      request({ host: "localhost:3022", "x-forwarded-proto": "https" }),
      profile,
    ),
    /Forwarded headers are disabled/,
  );
});

test("trusted_proxy validates proxy source and fixed forwarded authority", () => {
  const profile = trustedProfile();
  const headers = {
    host: "studio.example.test",
    origin: "https://studio.example.test",
    "x-forwarded-proto": "https",
    "x-forwarded-host": "studio.example.test",
    "x-forwarded-port": "443",
  };
  assert.deepEqual(
    assertTransportRequest(request(headers), profile, { requireOrigin: true }),
    { profile: "trusted_proxy", origin: "https://studio.example.test" },
  );

  const rejected = [
    request(headers, "10.0.0.9"),
    request({ ...headers, host: "attacker.example.test" }),
    request({ ...headers, origin: "https://attacker.example.test" }),
    request({ ...headers, "x-forwarded-proto": "http" }),
    request({ ...headers, "x-forwarded-host": "attacker.example.test" }),
    request({ ...headers, "x-forwarded-port": "8443" }),
    request({ ...headers, "x-forwarded-proto": "https,http" }),
  ];
  for (const candidate of rejected) {
    assert.throws(
      () => assertTransportRequest(candidate, profile, { requireOrigin: true }),
      PixelStreamingConfigError,
    );
  }
  assert.throws(
    () => assertTransportRequest(request({ ...headers, origin: undefined }), profile, { requireOrigin: true }),
    /Origin is required/,
  );
});

test("CSP is profile-specific and public mode contains no loopback or raw Cirrus ports", () => {
  const loopbackCsp = buildPixelStreamingCsp(resolveTransportProfile({}), { signalingPort: 8595 });
  assert.match(loopbackCsp, /ws:\/\/127\.0\.0\.1:8595/);
  assert.match(loopbackCsp, /wss:\/\/\[::1\]:8595/);

  const publicCsp = buildPixelStreamingCsp(trustedProfile());
  assert.match(publicCsp, /connect-src 'self' wss:\/\/studio\.example\.test/);
  assert.match(publicCsp, /upgrade-insecure-requests/);
  assert.doesNotMatch(publicCsp, /127\.0\.0\.1|localhost|8595|8596|ws:\/\//);
  assert.throws(
    () => buildPixelStreamingCsp(resolveTransportProfile({}), { signalingPort: 0 }),
    /valid TCP port/,
  );
});

test("opaque endpoint is same-origin, session-bound, deterministic, and non-descriptive", () => {
  const bindings = { sessionId: "session-user-alice", slotId: 17, leaseId: "lease-001" };
  const endpoint = createOpaqueStreamingEndpoint(bindings, ENDPOINT_SECRETS);
  assert.match(endpoint.endpointId, /^ps1_[A-Za-z0-9_-]{43}$/);
  assert.equal(endpoint.path, `/pixel-stream/session/${endpoint.endpointId}`);
  assert.equal(endpoint.path.startsWith("/"), true);
  assert.doesNotMatch(endpoint.path, /:|\/\/|session-user-alice|lease-001/);
  assert.deepEqual(createOpaqueStreamingEndpoint(bindings, ENDPOINT_SECRETS), endpoint);
  assert.equal(verifyOpaqueStreamingEndpoint(endpoint.endpointId, bindings, ENDPOINT_SECRETS), true);
  assert.equal(
    verifyOpaqueStreamingEndpoint(endpoint.endpointId, { ...bindings, slotId: 18 }, ENDPOINT_SECRETS),
    false,
  );
  assert.notEqual(
    createOpaqueStreamingEndpoint({ ...bindings, leaseId: "lease-002" }, ENDPOINT_SECRETS).endpointId,
    endpoint.endpointId,
  );
  assert.equal(verifyOpaqueStreamingEndpoint("not-an-endpoint", bindings, ENDPOINT_SECRETS), false);
});

test("opaque endpoint rejects weak secrets, missing bindings, and URL-like prefixes", () => {
  const bindings = { sessionId: "s1", slotId: 1, leaseId: "l1" };
  assert.throws(
    () => createOpaqueStreamingEndpoint(bindings, { endpointHmacKey: "too-short" }),
    /does not meet policy/,
  );
  assert.throws(
    () => createOpaqueStreamingEndpoint({ ...bindings, sessionId: "" }, ENDPOINT_SECRETS),
    /sessionId is required/,
  );
  for (const prefix of [
    "https://studio.example.test/pixel",
    "//studio.example.test/pixel",
    "/pixel/../admin",
    "/pixel/%2fadmin",
    "/pixel/session?slot=1",
    "/pixel/session/",
  ]) {
    assert.throws(() => normalizePathPrefix(prefix), /Streaming path prefix/);
  }
});

test("Cirrus builder fixes listeners to loopback and serializes ICE for the pinned parser", () => {
  const config = buildCirrusConfig({
    transportProfile: "trusted_proxy",
    httpPort: 8595,
    streamerPort: 8596,
    sfuPort: 8899,
    ice: ICE,
  }, CIRRUS_SECRETS);
  assert.deepEqual(
    {
      UseFrontend: config.UseFrontend,
      UseMatchmaker: config.UseMatchmaker,
      BindAddress: config.BindAddress,
      HttpPort: config.HttpPort,
      StreamerPort: config.StreamerPort,
      SFUPort: config.SFUPort,
    },
    {
      UseFrontend: false,
      UseMatchmaker: false,
      BindAddress: "127.0.0.1",
      HttpPort: 8595,
      StreamerPort: 8596,
      SFUPort: 8899,
    },
  );
  assert.equal(typeof config.peerConnectionOptions, "string");
  const peer = JSON.parse(config.peerConnectionOptions);
  assert.equal(peer.iceTransportPolicy, "relay");
  assert.deepEqual(peer.iceServers[0], {
    urls: ["stun:stun1.example.test:3478", "stun:stun2.example.test:3478"],
  });
  assert.deepEqual(peer.iceServers[1], {
    urls: [
      "turn:turn.example.test:3478?transport=udp",
      "turns:turn.example.test:5349?transport=tcp",
    ],
    username: "1784000000:studio-session",
    credential: TURN_CREDENTIAL,
    credentialType: "password",
  });

  const loopback = buildCirrusConfig({
    httpPort: 8687,
    streamerPort: 8688,
    ice: { ...ICE, transportPolicy: "all" },
  }, CIRRUS_SECRETS);
  assert.equal(loopback.UseFrontend, true);
  assert.equal(JSON.parse(loopback.peerConnectionOptions).iceTransportPolicy, "all");
});

test("TURN REST credentials are short-lived, session-bound, and compatible with coturn auth-secret", () => {
  const sharedSecret = "turn-rest-shared-secret-material-".repeat(2);
  const first = createTurnRestCredentials({
    sessionId: "slot-3-lease-a",
    ttlSeconds: 3600,
    nowSeconds: 1_800_000_000,
  }, { turnSharedSecret: sharedSecret });
  assert.deepEqual(first, {
    username: "1800003600:slot-3-lease-a",
    credential: require("node:crypto")
      .createHmac("sha1", sharedSecret)
      .update("1800003600:slot-3-lease-a")
      .digest("base64"),
    expiresAt: 1_800_003_600_000,
  });
  assert.equal(Object.isFrozen(first), true);
  const second = createTurnRestCredentials({
    sessionId: "slot-3-lease-b",
    ttlSeconds: 3600,
    nowSeconds: 1_800_000_000,
  }, { turnSharedSecret: sharedSecret });
  assert.notEqual(second.credential, first.credential);
  assert.throws(
    () => createTurnRestCredentials({ sessionId: "slot/3", nowSeconds: 1_800_000_000 }, { turnSharedSecret: sharedSecret }),
    /unsupported characters/,
  );
  assert.throws(
    () => createTurnRestCredentials({ sessionId: "slot-3", ttlSeconds: 30, nowSeconds: 1_800_000_000 }, { turnSharedSecret: sharedSecret }),
    /between 300 and 86400/,
  );
  assert.throws(
    () => createTurnRestCredentials({ sessionId: "slot-3", nowSeconds: 1_800_000_000 }, { turnSharedSecret: "weak" }),
    /does not meet policy/,
  );
});

test("Cirrus builder rejects unsafe overrides, port collisions, and incomplete ICE", () => {
  const valid = { httpPort: 8595, streamerPort: 8596, ice: ICE };
  assert.throws(
    () => buildCirrusConfig({ ...valid, BindAddress: "0.0.0.0" }, CIRRUS_SECRETS),
    /unsupported field/,
  );
  for (const invalid of [0, 65536, 1.5, "8595"]) {
    assert.throws(
      () => buildCirrusConfig({ ...valid, httpPort: invalid }, CIRRUS_SECRETS),
      /valid TCP port/,
    );
  }
  assert.throws(
    () => buildCirrusConfig({ ...valid, streamerPort: 8595 }, CIRRUS_SECRETS),
    /must be distinct/,
  );
  assert.throws(
    () => buildCirrusConfig({ ...valid, ice: { ...ICE, stunUrls: [] } }, CIRRUS_SECRETS),
    /STUN URLs/,
  );
  assert.throws(
    () => buildCirrusConfig({ ...valid, ice: { ...ICE, turnUrls: ["stun:not-turn.example"] } }, CIRRUS_SECRETS),
    /TURN URL is invalid/,
  );
  for (const turnUrl of [
    "turn:user@turn.example.test:3478",
    "turn:turn.example.test:not-a-port",
    "turn:turn.example.test:65536",
    "turn:bad_host.example.test:3478",
  ]) {
    assert.throws(
      () => buildCirrusConfig({ ...valid, ice: { ...ICE, turnUrls: [turnUrl] } }, CIRRUS_SECRETS),
      /TURN URL is invalid/,
    );
  }
  assert.throws(
    () => buildCirrusConfig({ ...valid, ice: { ...ICE, transportPolicy: "public" } }, CIRRUS_SECRETS),
    /all or relay/,
  );
});

test("ICE validation errors and redacted config never reveal TURN credentials", () => {
  const credentialWithNul = `private-${"x".repeat(32)}\u0000-never-log`;
  const error = captureError(() => buildCirrusConfig({
    httpPort: 8595,
    streamerPort: 8596,
    ice: ICE,
  }, { turnCredential: credentialWithNul }));
  const errorText = JSON.stringify({ name: error.name, code: error.code, message: error.message });
  assert.doesNotMatch(errorText, /private-|never-log/);

  const config = buildCirrusConfig({
    httpPort: 8595,
    streamerPort: 8596,
    ice: ICE,
  }, CIRRUS_SECRETS);
  const safe = redactCirrusConfig(config);
  assert.doesNotMatch(JSON.stringify(safe), new RegExp(TURN_CREDENTIAL));
  assert.match(safe.peerConnectionOptions, /\[REDACTED\]/);

  const malformed = redactCirrusConfig({
    HttpPort: 8595,
    unexpectedSecret: TURN_CREDENTIAL,
    peerConnectionOptions: `not-json-${TURN_CREDENTIAL}`,
  });
  assert.equal(malformed.peerConnectionOptions, "[INVALID REDACTED]");
  assert.doesNotMatch(JSON.stringify(malformed), new RegExp(TURN_CREDENTIAL));

  assert.throws(
    () => buildCirrusConfig({
      httpPort: 8595,
      streamerPort: 8596,
      ice: ICE,
    }, { turnCredential: "CHANGE_ME_static_password" }),
    /Placeholder streaming credentials/,
  );
});

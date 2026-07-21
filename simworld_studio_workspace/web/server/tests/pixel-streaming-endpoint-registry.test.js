"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  PixelStreamingEndpointRegistry,
  PixelStreamingEndpointRegistryError,
} = require("../pixel-streaming-endpoint-registry");

const ENDPOINT_SECRETS = { endpointHmacKey: "endpoint-registry-signing-key-".repeat(2) };
const BASE_BINDING = Object.freeze({
  ownerId: "owner-alpha",
  sessionId: "session-alpha",
  slotId: 3,
  leaseId: "lease-alpha",
});

function createHarness(overrides = {}) {
  let now = overrides.initialNow ?? 1_000_000;
  let nonce = 0;
  const trustedProxyContext = Object.freeze({ capability: "test-only" });
  const registry = new PixelStreamingEndpointRegistry({
    endpointSecrets: ENDPOINT_SECRETS,
    trustedProxyContext,
    ttlMs: 5_000,
    maxEntries: 8,
    clock: () => now,
    nonceFactory: () => `nonce_${String(++nonce).padStart(16, "0")}`,
    ...overrides.options,
  });
  return {
    registry,
    trustedProxyContext,
    now: () => now,
    advance(milliseconds) { now += milliseconds; },
    setNow(value) { now = value; },
  };
}

function acquire(registry, binding = BASE_BINDING, cirrusHttpPort = 8595) {
  return registry.acquire({ ...binding, cirrusHttpPort });
}

function expectRegistryError(fn, expectedCode) {
  try {
    fn();
  } catch (error) {
    assert.ok(error instanceof PixelStreamingEndpointRegistryError);
    assert.equal(error.code, expectedCode);
    return error;
  }
  assert.fail(`Expected ${expectedCode}`);
}

test("acquire returns only an opaque same-origin path and is idempotent for one exact binding", () => {
  const { registry, now } = createHarness();
  const endpoint = acquire(registry);
  assert.deepEqual(Object.keys(endpoint).sort(), ["expiresAt", "path"]);
  assert.equal(Object.isFrozen(endpoint), true);
  assert.match(endpoint.path, /^\/pixel-stream\/session\/ps1_[A-Za-z0-9_-]{43}$/);
  assert.equal(endpoint.expiresAt, now() + 5_000);
  assert.doesNotMatch(endpoint.path, /owner-alpha|session-alpha|lease-alpha|127\.0\.0\.1|8595/);
  assert.deepEqual(acquire(registry), endpoint);
  assert.equal(registry.size, 1);

  expectRegistryError(
    () => acquire(registry, BASE_BINDING, 8695),
    "PIXEL_STREAMING_ENDPOINT_BINDING_CONFLICT",
  );
  expectRegistryError(
    () => acquire(registry, { ...BASE_BINDING, slotId: 4 }),
    "PIXEL_STREAMING_ENDPOINT_BINDING_CONFLICT",
  );
  expectRegistryError(
    () => acquire(registry, { ...BASE_BINDING, leaseId: "lease-rebound" }),
    "PIXEL_STREAMING_ENDPOINT_BINDING_CONFLICT",
  );
});

test("resolve requires exact owner, session, slot, and lease authorization", () => {
  const { registry } = createHarness();
  const endpoint = acquire(registry);
  assert.deepEqual(registry.resolve(endpoint.path, BASE_BINDING), endpoint);

  for (const authorization of [
    { ...BASE_BINDING, ownerId: "owner-bravo" },
    { ...BASE_BINDING, sessionId: "session-bravo" },
    { ...BASE_BINDING, slotId: 4 },
    { ...BASE_BINDING, leaseId: "lease-bravo" },
  ]) {
    const error = expectRegistryError(
      () => registry.resolve(endpoint.path, authorization),
      "PIXEL_STREAMING_ENDPOINT_UNAVAILABLE",
    );
    const errorText = JSON.stringify({ code: error.code, message: error.message });
    for (const value of Object.values(authorization)) {
      if (typeof value === "string") assert.equal(errorText.includes(value), false);
    }
  }

  expectRegistryError(
    () => registry.resolve("/pixel-stream/session/ps1_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", BASE_BINDING),
    "PIXEL_STREAMING_ENDPOINT_UNAVAILABLE",
  );
  for (const path of [
    "https://studio.example.test/pixel-stream/session/token",
    "//studio.example.test/pixel-stream/session/token",
    `${endpoint.path}?slot=3`,
    `${endpoint.path}#fragment`,
  ]) {
    expectRegistryError(
      () => registry.resolve(path, BASE_BINDING),
      "PIXEL_STREAMING_ENDPOINT_UNAVAILABLE",
    );
  }
  assert.deepEqual(registry.resolve(endpoint.path, BASE_BINDING), endpoint);
});

test("opaque endpoint signing includes owner identity even when every other binding is equal", () => {
  const first = createHarness();
  const second = createHarness();
  const endpointA = acquire(first.registry);
  const endpointB = acquire(second.registry, { ...BASE_BINDING, ownerId: "owner-bravo" });
  assert.notEqual(endpointA.path, endpointB.path);
});

test("TTL expiry is exact, lazily pruned, and reacquisition never resurrects an old path", () => {
  const harness = createHarness();
  const first = acquire(harness.registry);
  harness.advance(4_999);
  assert.deepEqual(harness.registry.resolve(first.path, BASE_BINDING), first);
  harness.advance(1);
  assert.equal(harness.registry.sweepExpired(), 1);
  assert.equal(harness.registry.size, 0);
  expectRegistryError(
    () => harness.registry.resolve(first.path, BASE_BINDING),
    "PIXEL_STREAMING_ENDPOINT_UNAVAILABLE",
  );

  const second = acquire(harness.registry);
  assert.notEqual(second.path, first.path);
  assert.equal(second.expiresAt, harness.now() + 5_000);
});

test("rotate atomically invalidates the old path and can update only the internal HttpPort", () => {
  const harness = createHarness();
  const first = acquire(harness.registry);
  harness.advance(1_000);

  expectRegistryError(
    () => harness.registry.rotate(first.path, { ...BASE_BINDING, slotId: 9 }),
    "PIXEL_STREAMING_ENDPOINT_UNAVAILABLE",
  );
  assert.deepEqual(harness.registry.resolve(first.path, BASE_BINDING), first);

  const second = harness.registry.rotate(first.path, BASE_BINDING, { cirrusHttpPort: 8795 });
  assert.notEqual(second.path, first.path);
  assert.equal(second.expiresAt, harness.now() + 5_000);
  expectRegistryError(
    () => harness.registry.resolve(first.path, BASE_BINDING),
    "PIXEL_STREAMING_ENDPOINT_UNAVAILABLE",
  );
  assert.deepEqual(harness.registry.resolve(second.path, BASE_BINDING), second);
  const upstream = harness.registry.resolveForProxy(
    second.path,
    BASE_BINDING,
    harness.trustedProxyContext,
  );
  assert.equal(upstream.port, 8795);
});

test("failed rotation leaves the existing endpoint valid", () => {
  const trustedProxyContext = Object.freeze({});
  const fixedNonce = () => "nonce_that_never_changes_0001";
  const registry = new PixelStreamingEndpointRegistry({
    endpointSecrets: ENDPOINT_SECRETS,
    trustedProxyContext,
    ttlMs: 5_000,
    maxEntries: 2,
    clock: () => 1000,
    nonceFactory: fixedNonce,
  });
  const first = acquire(registry);
  expectRegistryError(
    () => registry.rotate(first.path, BASE_BINDING),
    "PIXEL_STREAMING_ENDPOINT_COLLISION",
  );
  assert.deepEqual(registry.resolve(first.path, BASE_BINDING), first);
});

test("revoke requires exact authorization and removes both path and session indexes", () => {
  const { registry } = createHarness();
  const endpoint = acquire(registry);
  expectRegistryError(
    () => registry.revoke(endpoint.path, { ...BASE_BINDING, leaseId: "other-lease" }),
    "PIXEL_STREAMING_ENDPOINT_UNAVAILABLE",
  );
  assert.deepEqual(registry.resolve(endpoint.path, BASE_BINDING), endpoint);

  const result = registry.revoke(endpoint.path, BASE_BINDING);
  assert.deepEqual(result, { revoked: true });
  assert.equal(Object.isFrozen(result), true);
  assert.equal(registry.size, 0);
  expectRegistryError(
    () => registry.revoke(endpoint.path, BASE_BINDING),
    "PIXEL_STREAMING_ENDPOINT_UNAVAILABLE",
  );

  const replacement = acquire(registry);
  assert.notEqual(replacement.path, endpoint.path);
});

test("only the trusted in-process proxy capability can resolve the loopback Cirrus HttpPort", () => {
  const harness = createHarness();
  const endpoint = acquire(harness.registry, BASE_BINDING, 8595);
  assert.deepEqual(Object.keys(harness.registry.resolve(endpoint.path, BASE_BINDING)).sort(), [
    "expiresAt",
    "path",
  ]);

  expectRegistryError(
    () => harness.registry.resolveForProxy(endpoint.path, BASE_BINDING, Object.freeze({})),
    "PIXEL_STREAMING_PROXY_CONTEXT_REQUIRED",
  );
  expectRegistryError(
    () => harness.registry.resolveForProxy(
      endpoint.path,
      { ...BASE_BINDING, slotId: 99 },
      harness.trustedProxyContext,
    ),
    "PIXEL_STREAMING_ENDPOINT_UNAVAILABLE",
  );

  const upstream = harness.registry.resolveForProxy(
    endpoint.path,
    BASE_BINDING,
    harness.trustedProxyContext,
  );
  assert.deepEqual(upstream, {
    listener: "cirrus_http",
    hostname: "127.0.0.1",
    port: 8595,
    expiresAt: endpoint.expiresAt,
  });
  assert.equal(Object.isFrozen(upstream), true);
  assert.equal(Object.prototype.hasOwnProperty.call(upstream, "credential"), false);
  assert.equal(Object.prototype.hasOwnProperty.call(upstream, "streamerPort"), false);
});

test("bounded capacity fails closed and expired entries free capacity", () => {
  const harness = createHarness({ options: { maxEntries: 2, ttlMs: 100 } });
  const secondBinding = {
    ownerId: "owner-bravo",
    sessionId: "session-bravo",
    slotId: 4,
    leaseId: "lease-bravo",
  };
  const thirdBinding = {
    ownerId: "owner-charlie",
    sessionId: "session-charlie",
    slotId: 5,
    leaseId: "lease-charlie",
  };
  acquire(harness.registry);
  acquire(harness.registry, secondBinding, 8695);
  assert.equal(harness.registry.size, 2);
  expectRegistryError(
    () => acquire(harness.registry, thirdBinding, 8795),
    "PIXEL_STREAMING_ENDPOINT_CAPACITY",
  );
  assert.equal(harness.registry.size, 2);

  harness.advance(100);
  const third = acquire(harness.registry, thirdBinding, 8795);
  assert.equal(harness.registry.size, 1);
  assert.match(third.path, /^\/pixel-stream\/session\/ps1_/);
});

test("configuration and acquisition inputs are strict and never echo secret values", () => {
  const trustedProxyContext = Object.freeze({});
  const sensitivePrefix = "do-not-print-this-endpoint-secret";
  const sensitive = `${sensitivePrefix}\u0000invalid`;
  const secretError = expectRegistryError(
    () => new PixelStreamingEndpointRegistry({
      endpointSecrets: { endpointHmacKey: sensitive },
      trustedProxyContext,
    }),
    "PIXEL_STREAMING_ENDPOINT_SECRET_INVALID",
  );
  assert.equal(JSON.stringify(secretError).includes(sensitivePrefix), false);
  assert.equal(secretError.message.includes(sensitivePrefix), false);

  const invalidOptions = [
    { endpointSecrets: ENDPOINT_SECRETS, trustedProxyContext, ttlMs: 0 },
    { endpointSecrets: ENDPOINT_SECRETS, trustedProxyContext, maxEntries: 0 },
    { endpointSecrets: ENDPOINT_SECRETS, trustedProxyContext: "not-a-capability" },
    { endpointSecrets: ENDPOINT_SECRETS, trustedProxyContext, clock: null },
    { endpointSecrets: ENDPOINT_SECRETS, trustedProxyContext, nonceFactory: null },
    { endpointSecrets: ENDPOINT_SECRETS, trustedProxyContext, pathPrefix: "https://example.test/stream" },
    { endpointSecrets: ENDPOINT_SECRETS, trustedProxyContext, pathPrefix: "" },
  ];
  for (const options of invalidOptions) {
    assert.throws(() => new PixelStreamingEndpointRegistry(options), PixelStreamingEndpointRegistryError);
  }

  const harness = createHarness();
  for (const input of [
    { ...BASE_BINDING, cirrusHttpPort: 0 },
    { ...BASE_BINDING, cirrusHttpPort: 8595, slotId: -1 },
    { ...BASE_BINDING, cirrusHttpPort: 8595, ownerId: "" },
    { ...BASE_BINDING, cirrusHttpPort: 8595, rawPort: 8596 },
  ]) {
    assert.throws(() => harness.registry.acquire(input), PixelStreamingEndpointRegistryError);
  }
});

test("clock and nonce provider failures are wrapped without leaking provider errors", () => {
  const trustedProxyContext = Object.freeze({});
  const clockSecret = "clock-provider-private-value";
  const clockError = expectRegistryError(
    () => new PixelStreamingEndpointRegistry({
      endpointSecrets: ENDPOINT_SECRETS,
      trustedProxyContext,
      clock: () => { throw new Error(clockSecret); },
    }),
    "PIXEL_STREAMING_ENDPOINT_CLOCK_INVALID",
  );
  assert.equal(clockError.message.includes(clockSecret), false);

  const nonceSecret = "nonce-provider-private-value";
  const registry = new PixelStreamingEndpointRegistry({
    endpointSecrets: ENDPOINT_SECRETS,
    trustedProxyContext,
    clock: () => 1000,
    nonceFactory: () => { throw new Error(nonceSecret); },
  });
  const nonceError = expectRegistryError(
    () => acquire(registry),
    "PIXEL_STREAMING_ENDPOINT_NONCE_INVALID",
  );
  assert.equal(nonceError.message.includes(nonceSecret), false);
  assert.equal(registry.size, 0);

  let now = 2000;
  const backwardsClockRegistry = new PixelStreamingEndpointRegistry({
    endpointSecrets: ENDPOINT_SECRETS,
    trustedProxyContext,
    clock: () => now,
  });
  acquire(backwardsClockRegistry);
  now = 1999;
  expectRegistryError(
    () => backwardsClockRegistry.resolve(
      "/pixel-stream/session/ps1_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
      BASE_BINDING,
    ),
    "PIXEL_STREAMING_ENDPOINT_CLOCK_INVALID",
  );
});

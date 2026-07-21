"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const {
  ACTION_DEFINITIONS,
  ANIMATION_CONTENT_PROFILE_SCHEMA,
  digest,
} = require("../vista-animation-contract");
const {
  ANIMATION_UE_MARKER_SCHEMA,
} = require("../vista-animation-ue-adapter");
const {
  ANIMATION_UE_CAPABILITY_OPERATION_FINGERPRINT,
  ANIMATION_UE_CAPABILITY_OPERATION_ID,
  ANIMATION_UE_CAPABILITY_PROBE_SCHEMA,
  ANIMATION_UE_CAPABILITY_SCHEMA,
  ANIMATION_UE_PLUGIN_API_SCHEMA,
  ANIMATION_UE_PLUGIN_ARTIFACT_SCHEMA,
  ANIMATION_UE_PLUGIN_NAME,
  EXPECTED_PLUGIN_SOURCE_FILES,
  OPERATION_SET,
  SECURITY_POLICY,
  VistaAnimationUeReadinessError,
  createVistaAnimationUeReadinessProbe,
  inspectVistaAnimationUePluginSource,
} = require("../vista-animation-ue-readiness");
const capabilityProbeSchema = require("../schemas/vista-animation-ue-capability-probe-v1.schema.json");
const capabilitySchema = require("../schemas/vista-animation-ue-capability-v1.schema.json");

const NONCE = "a".repeat(32);
const CONTENT_DIGEST = "b".repeat(64);
const BINARY_DIGEST = "c".repeat(64);

function makeProfile() {
  return {
    schema: ANIMATION_CONTENT_PROFILE_SCHEMA,
    profile_id: "vista_hands_ik_v1",
    revision: "vista_hands_ik_2026_r1",
    content_revision: "simworld-content-2026.07.21-r1",
    content_digest: CONTENT_DIGEST,
    pawn_class_path: "/Game/VISTA/Characters/BP_VistaFirstPerson.BP_VistaFirstPerson_C",
    skeleton_path: "/Game/VISTA/Characters/SK_VistaHuman.SK_VistaHuman",
    verification: {
      status: "verified",
      receipt_id: "ue-content-receipt:2026-07-21:001",
      verified_at: "2026-07-21T00:00:00.000Z",
    },
    actions: [{
      action: "brace",
      adapter_id: ACTION_DEFINITIONS.brace.adapter_id,
      version: "1.0.0",
      bridge_action_id: ACTION_DEFINITIONS.brace.bridge_action_id,
      implementation_asset: "/Game/VISTA/Animations/ABP_brace.ABP_brace",
      completion_signal: "vista_brace_complete",
      timeout_ms: 5000,
    }],
  };
}

function makeArtifact(overrides = {}) {
  return {
    schema: ANIMATION_UE_PLUGIN_ARTIFACT_SCHEMA,
    plugin_name: ANIMATION_UE_PLUGIN_NAME,
    plugin_version: "1.0.0",
    plugin_build_id: "vista-animation-linux-ue5.3-build001",
    binary_sha256: BINARY_DIGEST,
    engine_version: "5.3.2",
    target_platform: "linux-x86_64",
    api_schema: ANIMATION_UE_PLUGIN_API_SCHEMA,
    ...overrides,
  };
}

function makeSlotBinding(overrides = {}) {
  return {
    owner_id: "owner:ives",
    session_id: "session:animation001",
    slot_id: "slot:ue-primary",
    scene_revision: "mmg_040@aaaaaaaaaaaaaaaa",
    ...overrides,
  };
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function responseFor(request, mutate = () => {}) {
  const response = {
    schema: ANIMATION_UE_CAPABILITY_SCHEMA,
    status: "ready",
    operation_id: ANIMATION_UE_CAPABILITY_OPERATION_ID,
    operation_fingerprint: ANIMATION_UE_CAPABILITY_OPERATION_FINGERPRINT,
    nonce_marker: clone(request.nonce_marker),
    challenge_digest: request.challenge_digest,
    slot_binding: clone(request.slot_binding),
    plugin_artifact: makeArtifact(),
    process_instance_id: "ue-process:boot001",
    content_proof: clone(request.content_proof),
    security: clone(SECURITY_POLICY),
    operation_allowlist_digest: OPERATION_SET.operation_allowlist_digest,
    operations: clone(OPERATION_SET.operations),
  };
  mutate(response);
  return JSON.stringify(response);
}

function makeTransport({ mutate, fail, hang = false } = {}) {
  const calls = [];
  return {
    calls,
    async probeAnimationContentApi(requestJson, options) {
      calls.push({ requestJson, options });
      if (hang) return new Promise(() => {});
      if (fail) throw fail;
      return responseFor(JSON.parse(requestJson), mutate);
    },
  };
}

function makeProbe(transport, overrides = {}) {
  return createVistaAnimationUeReadinessProbe({
    transport,
    expectedArtifact: makeArtifact(),
    contentProfile: makeProfile(),
    slotBinding: makeSlotBinding(),
    nonceFactory: () => NONCE,
    clock: () => Date.parse("2026-07-21T12:00:00.000Z"),
    ...overrides,
  });
}

test("capability JSON schema pins the exact live operation contract", () => {
  assert.equal(capabilityProbeSchema.properties.schema.const, ANIMATION_UE_CAPABILITY_PROBE_SCHEMA);
  assert.equal(capabilityProbeSchema.additionalProperties, false);
  assert.equal(
    capabilityProbeSchema.properties.operation_fingerprint.const,
    ANIMATION_UE_CAPABILITY_OPERATION_FINGERPRINT,
  );
  assert.equal(
    capabilityProbeSchema.properties.operation_allowlist_digest.const,
    OPERATION_SET.operation_allowlist_digest,
  );
  assert.equal(capabilitySchema.properties.schema.const, ANIMATION_UE_CAPABILITY_SCHEMA);
  assert.equal(
    capabilitySchema.properties.operation_fingerprint.const,
    ANIMATION_UE_CAPABILITY_OPERATION_FINGERPRINT,
  );
  assert.equal(
    capabilitySchema.properties.operation_allowlist_digest.const,
    OPERATION_SET.operation_allowlist_digest,
  );
  assert.equal(capabilitySchema.properties.operations.minItems, 7);
  assert.equal(capabilitySchema.properties.operations.maxItems, 7);
  assert.deepEqual(
    capabilitySchema.properties.operations.prefixItems.map((entry) => entry.const),
    OPERATION_SET.operations,
  );
  assert.equal(capabilitySchema.additionalProperties, false);
  assert.equal(capabilitySchema.$defs.pluginArtifact.additionalProperties, false);
  assert.equal(capabilitySchema.$defs.slotBinding.additionalProperties, false);
});

test("the checked-out Studio repository does not contain a compilable plugin source tree", () => {
  const repositoryRoot = path.resolve(__dirname, "../../../..");
  const audit = inspectVistaAnimationUePluginSource(repositoryRoot);

  assert.equal(audit.schema, "vista-animation-ue-source-audit/v1");
  assert.equal(audit.plugin_name, ANIMATION_UE_PLUGIN_NAME);
  assert.equal(audit.source_tree_complete, false);
  assert.deepEqual(audit.expected_files, EXPECTED_PLUGIN_SOURCE_FILES);
  assert.deepEqual(audit.present_files, []);
  assert.deepEqual(audit.missing_files, EXPECTED_PLUGIN_SOURCE_FILES);
});

test("source audit requires all fixed regular files and rejects symlink-only evidence", () => {
  const complete = inspectVistaAnimationUePluginSource("/ue-project", {
    fsImpl: {
      lstatSync() {
        return { isFile: () => true, isSymbolicLink: () => false };
      },
    },
  });
  assert.equal(complete.source_tree_complete, true);
  assert.deepEqual(complete.missing_files, []);

  const symlinked = inspectVistaAnimationUePluginSource("/ue-project", {
    fsImpl: {
      lstatSync() {
        return { isFile: () => true, isSymbolicLink: () => true };
      },
    },
  });
  assert.equal(symlinked.source_tree_complete, false);
  assert.deepEqual(symlinked.missing_files, EXPECTED_PLUGIN_SOURCE_FILES);
});

test("a live plugin challenge proves artifact, slot, content, allowlist, and no-retry policy", async () => {
  const transport = makeTransport();
  const result = await makeProbe(transport)();

  assert.equal(result.status, "ready");
  assert.deepEqual(result.causes, []);
  assert.equal(result.revision.verification, "live_plugin_challenge");
  assert.equal(result.revision.process_instance_id, "ue-process:boot001");
  assert.equal(result.revision.binary_sha256, BINARY_DIGEST);
  assert.equal(transport.calls.length, 1);

  const { requestJson, options } = transport.calls[0];
  const request = JSON.parse(requestJson);
  assert.equal(request.schema, ANIMATION_UE_CAPABILITY_PROBE_SCHEMA);
  assert.equal(request.operation_id, ANIMATION_UE_CAPABILITY_OPERATION_ID);
  assert.equal(request.nonce_marker.schema, ANIMATION_UE_MARKER_SCHEMA);
  assert.equal(request.nonce_marker.nonce, NONCE);
  const unsigned = clone(request);
  delete unsigned.challenge_digest;
  assert.equal(request.challenge_digest, digest(unsigned));
  const slotIdentity = clone(request.slot_binding);
  delete slotIdentity.binding_digest;
  assert.equal(request.slot_binding.binding_digest, digest(slotIdentity));
  assert.equal(request.operation_allowlist_digest, OPERATION_SET.operation_allowlist_digest);
  assert.equal(options.mutation, false);
  assert.equal(options.maxAttempts, 1);
  assert.equal(options.challengeDigest, request.challenge_digest);
  assert.equal(options.slotBindingDigest, request.slot_binding.binding_digest);
  assert.equal(options.timeoutMs, options.queueDeadlineMs);
  assert.equal(options.signal.aborted, false);

  assert.equal(requestJson.includes("/Game/"), false);
  assert.equal(/python|execute[_-]?console|\bvbp\b|\.py\b/i.test(requestJson), false);
  assert.equal(Object.hasOwn(request, "operations"), false);
  assert.equal(Object.hasOwn(request, "expected_plugin_artifact"), false);
  assert.equal(Object.hasOwn(request, "script"), false);
  assert.equal(Object.hasOwn(request, "asset_path"), false);
  assert.equal(OPERATION_SET.operations.filter((entry) => entry.mutation).length, 4);
  assert.ok(OPERATION_SET.operations.filter((entry) => entry.mutation).every((entry) => entry.max_attempts === 1));
});

test("generic MCP, Python, and invoke-only transports cannot satisfy readiness", async () => {
  const genericTransports = [
    null,
    { send: async () => "{}" },
    { execute_python_script: async () => "{}" },
    { invokeAnimationContentApi: async () => "{}" },
  ];

  for (const transport of genericTransports) {
    const result = await makeProbe(transport)();
    assert.equal(result.status, "not_ready");
    assert.equal(result.causes[0].code, "ANIMATION_UE_PLUGIN_TRANSPORT_MISSING");
  }
});

test("legacy timeline environment flags are not accepted as capability evidence", () => {
  assert.throws(
    () => createVistaAnimationUeReadinessProbe({
      transport: null,
      expectedArtifact: makeArtifact(),
      contentProfile: makeProfile(),
      slotBinding: makeSlotBinding(),
      env: { TIMELINE_AUTOMATION_VERIFIED: "true" },
    }),
    (error) => error instanceof VistaAnimationUeReadinessError
      && error.code === "ANIMATION_UE_PLUGIN_CONFIG_INVALID",
  );
});

const mismatchCases = [
  {
    name: "unknown response field",
    code: "ANIMATION_UE_PLUGIN_PROTOCOL_INVALID",
    mutate: (response) => { response.python = "print('unsafe')"; },
  },
  {
    name: "stale nonce",
    code: "ANIMATION_UE_PLUGIN_CORRELATION_MISMATCH",
    mutate: (response) => { response.nonce_marker.nonce = "d".repeat(32); },
  },
  {
    name: "stale challenge digest",
    code: "ANIMATION_UE_PLUGIN_CORRELATION_MISMATCH",
    mutate: (response) => { response.challenge_digest = "d".repeat(64); },
  },
  {
    name: "different plugin binary",
    code: "ANIMATION_UE_PLUGIN_ARTIFACT_MISMATCH",
    mutate: (response) => { response.plugin_artifact.binary_sha256 = "d".repeat(64); },
  },
  {
    name: "different slot",
    code: "ANIMATION_UE_PLUGIN_SLOT_MISMATCH",
    mutate: (response) => { response.slot_binding.slot_id = "slot:other"; },
  },
  {
    name: "different content",
    code: "ANIMATION_UE_PLUGIN_CONTENT_MISMATCH",
    mutate: (response) => { response.content_proof.content_digest = "d".repeat(64); },
  },
  {
    name: "caller Python enabled",
    code: "ANIMATION_UE_PLUGIN_SECURITY_POLICY_MISMATCH",
    mutate: (response) => { response.security.caller_python = true; },
  },
  {
    name: "mutation retries enabled",
    code: "ANIMATION_UE_PLUGIN_SECURITY_POLICY_MISMATCH",
    mutate: (response) => { response.security.mutation_max_attempts = 2; },
  },
  {
    name: "operation omitted",
    code: "ANIMATION_UE_PLUGIN_OPERATION_MISMATCH",
    mutate: (response) => { response.operations.pop(); },
  },
  {
    name: "operation fingerprint changed",
    code: "ANIMATION_UE_PLUGIN_OPERATION_MISMATCH",
    mutate: (response) => { response.operations[0].operation_fingerprint = "d".repeat(64); },
  },
];

for (const entry of mismatchCases) {
  test(`capability probe fails closed for ${entry.name}`, async () => {
    const result = await makeProbe(makeTransport({ mutate: entry.mutate }))();
    assert.equal(result.status, "not_ready");
    assert.equal(result.causes[0].code, entry.code);
    assert.equal(result.causes[0].dependency, "vista_animation_ue_plugin");
    assert.equal(JSON.stringify(result).includes("print('unsafe')"), false);
  });
}

test("invalid and oversized capability responses fail closed", async () => {
  const invalid = makeProbe({ probeAnimationContentApi: async () => "not-json" });
  const invalidResult = await invalid();
  assert.equal(invalidResult.status, "not_ready");
  assert.equal(invalidResult.causes[0].code, "ANIMATION_UE_PLUGIN_PROTOCOL_INVALID");

  const oversized = makeProbe({
    probeAnimationContentApi: async () => `{"padding":"${"x".repeat(131_073)}"}`,
  }, { nonceFactory: () => "e".repeat(32) });
  const oversizedResult = await oversized();
  assert.equal(oversizedResult.status, "not_ready");
  assert.equal(oversizedResult.causes[0].code, "ANIMATION_UE_PLUGIN_PROTOCOL_INVALID");
});

test("transport failures are sanitized and do not expose credentials", async () => {
  const result = await makeProbe(makeTransport({
    fail: new Error("postgres://user:password@internal/db token=secret"),
  }))();
  assert.equal(result.status, "not_ready");
  assert.equal(result.causes[0].code, "ANIMATION_UE_PLUGIN_PROBE_UNAVAILABLE");
  assert.equal(JSON.stringify(result).includes("password"), false);
  assert.equal(JSON.stringify(result).includes("secret"), false);
});

test("timeout remains bounded even when a transport ignores abort", async () => {
  const started = Date.now();
  const result = await makeProbe(makeTransport({ hang: true }), {
    timeoutMs: 10,
  })();
  assert.equal(result.status, "not_ready");
  assert.equal(result.causes[0].code, "ANIMATION_UE_PLUGIN_PROBE_TIMEOUT");
  assert.ok(Date.now() - started < 500);
});

test("caller cancellation remains bounded even when a transport ignores abort", async () => {
  const controller = new AbortController();
  const probe = makeProbe(makeTransport({ hang: true }), { timeoutMs: 1000 });
  const pending = probe({ signal: controller.signal });
  setImmediate(() => controller.abort());
  const result = await pending;
  assert.equal(result.status, "not_ready");
  assert.equal(result.causes[0].code, "ANIMATION_UE_PLUGIN_PROBE_ABORTED");
});

test("nonce reuse is rejected instead of treating a replay as a fresh capability", async () => {
  const transport = makeTransport();
  const probe = makeProbe(transport);
  assert.equal((await probe()).status, "ready");
  const replay = await probe();
  assert.equal(replay.status, "not_ready");
  assert.equal(replay.causes[0].code, "ANIMATION_UE_PLUGIN_NONCE_INVALID");
  assert.equal(transport.calls.length, 1);
});

test("artifact, slot, profile, and timeout configuration are exact and bounded", () => {
  assert.throws(
    () => makeProbe(null, { expectedArtifact: makeArtifact({ arbitrary_path: "/tmp/plugin" }) }),
    (error) => error.code === "ANIMATION_UE_PLUGIN_CONFIG_INVALID",
  );
  assert.throws(
    () => makeProbe(null, { slotBinding: makeSlotBinding({ lease_token: "secret" }) }),
    (error) => error.code === "ANIMATION_UE_PLUGIN_CONFIG_INVALID",
  );
  assert.throws(
    () => makeProbe(null, { timeoutMs: 10_001 }),
    (error) => error.code === "ANIMATION_UE_PLUGIN_CONFIG_INVALID",
  );
  assert.throws(
    () => makeProbe(null, { contentProfile: { schema: ANIMATION_CONTENT_PROFILE_SCHEMA } }),
    (error) => error.code === "ANIMATION_UE_PLUGIN_CONFIG_INVALID",
  );
});

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const {
  ACTION_DEFINITIONS,
  ANIMATION_CONTENT_PROFILE_SCHEMA,
  ANIMATION_PREFLIGHT_REQUEST_SCHEMA,
  ANIMATION_PREFLIGHT_RESPONSE_SCHEMA,
} = require("../vista-animation-contract");
const {
  RELEASE_REQUEST_SCHEMA,
  RELEASE_RESPONSE_SCHEMA,
  RESTORE_REQUEST_SCHEMA,
  RESTORE_RESPONSE_SCHEMA,
  SNAPSHOT_REQUEST_SCHEMA,
  SNAPSHOT_RESPONSE_SCHEMA,
  START_REQUEST_SCHEMA,
  START_RESPONSE_SCHEMA,
  STOP_REQUEST_SCHEMA,
  STOP_RESPONSE_SCHEMA,
  WAIT_REQUEST_SCHEMA,
  WAIT_RESPONSE_SCHEMA,
  createVistaAnimationRuntime,
} = require("../vista-animation-runtime");
const { BINDINGS_SCHEMA } = require("../vista-timeline-compiler");
const {
  ANIMATION_UE_MARKER_SCHEMA,
  ANIMATION_UE_OPERATION_ALLOWLIST,
  ANIMATION_UE_REQUEST_SCHEMA,
  ANIMATION_UE_RESPONSE_SCHEMA,
  VistaAnimationUeAdapterError,
  createVistaAnimationUeAdapter,
} = require("../vista-animation-ue-adapter");

const CONTENT_DIGEST = "b".repeat(64);
const STATE_DIGEST = "c".repeat(64);
const PROFILE_ID = "vista_hands_ik_v1";
const PROFILE_REVISION = "vista_hands_ik_2026_r1";
const CONTENT_REVISION = "simworld-content-2026.07.21-r1";
const PREFLIGHT_ID = `vap-${"d".repeat(24)}`;
const RUN_ID = "animation_run_001";
const EVENT_ID = "beat-0001";
const ACTOR_ID = "actor_camera_wearer";
const TARGET_ID = "target_rolling_chair";
const ACTION = "brace";
const BRIDGE_ACTION_ID = ACTION_DEFINITIONS[ACTION].bridge_action_id;
const SNAPSHOT_ID = `snapshot:${EVENT_ID}`;
const HANDLE_ID = `handle:${EVENT_ID}`;

function makeProfile() {
  return {
    schema: ANIMATION_CONTENT_PROFILE_SCHEMA,
    profile_id: PROFILE_ID,
    revision: PROFILE_REVISION,
    content_revision: CONTENT_REVISION,
    content_digest: CONTENT_DIGEST,
    pawn_class_path: "/Game/VISTA/Characters/BP_VistaFirstPerson.BP_VistaFirstPerson_C",
    skeleton_path: "/Game/VISTA/Characters/SK_VistaHuman.SK_VistaHuman",
    verification: {
      status: "verified",
      receipt_id: "ue-content-receipt:2026-07-21:001",
      verified_at: "2026-07-21T00:00:00.000Z",
    },
    actions: [{
      action: ACTION,
      adapter_id: ACTION_DEFINITIONS[ACTION].adapter_id,
      version: "1.0.0",
      bridge_action_id: BRIDGE_ACTION_ID,
      implementation_asset: "/Game/VISTA/Animations/ABP_brace.ABP_brace",
      completion_signal: "vista_brace_complete",
      timeout_ms: 5000,
    }],
  };
}

function makePreflightRequest(overrides = {}) {
  return {
    schema: ANIMATION_PREFLIGHT_REQUEST_SCHEMA,
    scene_revision: "mmg_040@aaaaaaaaaaaaaaaa",
    profile_id: PROFILE_ID,
    profile_revision: PROFILE_REVISION,
    content_revision: CONTENT_REVISION,
    content_digest: CONTENT_DIGEST,
    pawn_class_path: "/Game/VISTA/Characters/BP_VistaFirstPerson.BP_VistaFirstPerson_C",
    skeleton_path: "/Game/VISTA/Characters/SK_VistaHuman.SK_VistaHuman",
    requested_actions: [ACTION],
    actor_binding_ids: [ACTOR_ID],
    target_binding_ids: [TARGET_ID],
    ...overrides,
  };
}

function makeSnapshotRequest(overrides = {}) {
  return {
    schema: SNAPSHOT_REQUEST_SCHEMA,
    preflight_id: PREFLIGHT_ID,
    run_id: RUN_ID,
    event_id: EVENT_ID,
    action: ACTION,
    actor_binding_id: ACTOR_ID,
    target_binding_id: TARGET_ID,
    ...overrides,
  };
}

function makeStartRequest(overrides = {}) {
  return {
    schema: START_REQUEST_SCHEMA,
    preflight_id: PREFLIGHT_ID,
    run_id: RUN_ID,
    event_id: EVENT_ID,
    bridge_action_id: BRIDGE_ACTION_ID,
    actor_binding_id: ACTOR_ID,
    target_binding_id: TARGET_ID,
    parameters: { duration_sec: 2, hand: "both" },
    ...overrides,
  };
}

function makeWaitRequest(overrides = {}) {
  return {
    schema: WAIT_REQUEST_SCHEMA,
    preflight_id: PREFLIGHT_ID,
    run_id: RUN_ID,
    event_id: EVENT_ID,
    action_handle: HANDLE_ID,
    ...overrides,
  };
}

function makeStopRequest(overrides = {}) {
  return {
    schema: STOP_REQUEST_SCHEMA,
    preflight_id: PREFLIGHT_ID,
    run_id: RUN_ID,
    event_id: EVENT_ID,
    action_handle: HANDLE_ID,
    reason: "timeout",
    ...overrides,
  };
}

function makeReleaseRequest(overrides = {}) {
  return {
    schema: RELEASE_REQUEST_SCHEMA,
    preflight_id: PREFLIGHT_ID,
    run_id: RUN_ID,
    event_id: EVENT_ID,
    action_handle: HANDLE_ID,
    ...overrides,
  };
}

function makeRestoreRequest(overrides = {}) {
  return {
    schema: RESTORE_REQUEST_SCHEMA,
    preflight_id: PREFLIGHT_ID,
    run_id: RUN_ID,
    event_id: EVENT_ID,
    snapshot_id: SNAPSHOT_ID,
    state_digest: STATE_DIGEST,
    ...overrides,
  };
}

function defaultPayload(envelope) {
  const request = envelope.request;
  switch (envelope.operation_id) {
    case "vista.animation.preflight.v1":
      return {
        schema: ANIMATION_PREFLIGHT_RESPONSE_SCHEMA,
        scene_revision: request.scene_revision,
        profile_revision: request.profile_revision,
        content_revision: request.content_revision,
        content_digest: request.content_digest,
        ready: true,
        actors: request.actor_binding_ids.map((bindingId) => ({
          binding_id: bindingId,
          available: true,
          class_matches: true,
          skeleton_matches: true,
          capabilities: ["upper_body_ik"],
          anchor_kinds: [],
        })),
        targets: request.target_binding_ids.map((bindingId) => ({
          binding_id: bindingId,
          available: true,
          class_matches: true,
          skeleton_matches: true,
          capabilities: ["hand_contact_target"],
          anchor_kinds: ["hand_contact"],
        })),
        actions: request.requested_actions.map((action) => ({
          action,
          bridge_action_id: ACTION_DEFINITIONS[action].bridge_action_id,
          available: true,
          implementation_matches: true,
          completion_signal_available: true,
        })),
      };
    case "vista.animation.snapshot.v1":
      return {
        schema: SNAPSHOT_RESPONSE_SCHEMA,
        status: "captured",
        snapshot_id: SNAPSHOT_ID,
        action: request.action,
        actor_binding_id: request.actor_binding_id,
        target_binding_id: request.target_binding_id,
        engine_time: 1,
        state_digest: STATE_DIGEST,
      };
    case "vista.animation.start.v1":
      return {
        schema: START_RESPONSE_SCHEMA,
        status: "started",
        action_handle: HANDLE_ID,
        bridge_action_id: request.bridge_action_id,
        actor_binding_id: request.actor_binding_id,
        target_binding_id: request.target_binding_id,
        engine_time: 1.1,
      };
    case "vista.animation.wait.v1":
      return {
        schema: WAIT_RESPONSE_SCHEMA,
        status: "completed",
        action_handle: request.action_handle,
        completion_signal: "vista_brace_complete",
        engine_time: 1.5,
        evidence_ids: [`broker:${EVENT_ID}`],
      };
    case "vista.animation.stop.v1":
      return {
        schema: STOP_RESPONSE_SCHEMA,
        status: "stopped",
        action_handle: request.action_handle,
        engine_time: 1.6,
      };
    case "vista.animation.release.v1":
      return {
        schema: RELEASE_RESPONSE_SCHEMA,
        status: "released",
        action_handle: request.action_handle,
      };
    case "vista.animation.restore.v1":
      return {
        schema: RESTORE_RESPONSE_SCHEMA,
        status: "restored",
        snapshot_id: request.snapshot_id,
        state_digest: request.state_digest,
        engine_time: 1.7,
      };
    default:
      throw new Error(`unexpected operation ${envelope.operation_id}`);
  }
}

function makeTransport({ mutateEnvelope, mutatePayload, throwOperation } = {}) {
  const calls = [];
  return {
    calls,
    async invokeAnimationContentApi(requestJson, options) {
      const requestEnvelope = JSON.parse(requestJson);
      calls.push({ requestJson, envelope: requestEnvelope, options });
      if (throwOperation === requestEnvelope.operation_id) {
        const error = new Error("transport leaked private-token-value");
        error.code = "UE_PRIVATE_FAILURE";
        throw error;
      }
      const payload = mutatePayload
        ? mutatePayload(requestEnvelope.operation_id, defaultPayload(requestEnvelope), requestEnvelope)
        : defaultPayload(requestEnvelope);
      const response = {
        schema: ANIMATION_UE_RESPONSE_SCHEMA,
        operation_id: requestEnvelope.operation_id,
        operation_fingerprint: requestEnvelope.operation_fingerprint,
        invocation_id: requestEnvelope.invocation_id,
        request_digest: requestEnvelope.request_digest,
        nonce_marker: requestEnvelope.nonce_marker,
        payload,
      };
      return JSON.stringify(mutateEnvelope ? mutateEnvelope(response, requestEnvelope) : response);
    },
  };
}

function makeNonceFactory(valueFactory = (sequence) => sequence.toString(16).padStart(32, "0")) {
  let sequence = 0;
  return () => valueFactory(++sequence);
}

function makeAdapter(transport, nonceFactory = makeNonceFactory()) {
  return createVistaAnimationUeAdapter({
    transport,
    contentProfile: makeProfile(),
    nonceFactory,
  });
}

async function preflight(adapter) {
  return adapter.preflightAnimation(makePreflightRequest());
}

async function snapshot(adapter) {
  return adapter.snapshotAnimationState(makeSnapshotRequest());
}

async function start(adapter) {
  return adapter.startAnimationAction(makeStartRequest());
}

test("fixed UE operation allowlist is closed and pins every mutation to maxAttempts one", () => {
  assert.deepEqual(Object.keys(ANIMATION_UE_OPERATION_ALLOWLIST).sort(), [
    "preflightAnimation",
    "releaseAnimationAction",
    "restoreAnimationState",
    "snapshotAnimationState",
    "startAnimationAction",
    "stopAnimationAction",
    "waitAnimationAction",
  ]);
  assert.equal(Object.isFrozen(ANIMATION_UE_OPERATION_ALLOWLIST), true);
  for (const operation of Object.values(ANIMATION_UE_OPERATION_ALLOWLIST)) {
    assert.match(operation.operation_id, /^vista\.animation\.[a-z]+\.v1$/);
    assert.match(operation.operation_fingerprint, /^[a-f0-9]{64}$/);
    assert.equal(operation.operation_fingerprint.length, 64);
    assert.equal(operation.max_attempts, operation.mutation ? 1 : 2);
  }
  assert.throws(
    () => createVistaAnimationUeAdapter({ transport: { send() {} }, contentProfile: makeProfile() }),
    (error) => error instanceof VistaAnimationUeAdapterError && error.code === "ANIMATION_UE_CONFIG_INVALID",
  );
});

test("preflight sends a JSON-fed nonce marker and no public UE content or execution path", async () => {
  const transport = makeTransport();
  const adapter = makeAdapter(transport);
  const response = await preflight(adapter);
  assert.equal(response.ready, true);
  assert.equal(transport.calls.length, 1);
  const call = transport.calls[0];
  assert.equal(call.envelope.schema, ANIMATION_UE_REQUEST_SCHEMA);
  assert.equal(call.envelope.nonce_marker.schema, ANIMATION_UE_MARKER_SCHEMA);
  assert.match(call.envelope.nonce_marker.nonce, /^[a-f0-9]{32}$/);
  assert.equal(call.envelope.operation_id, "vista.animation.preflight.v1");
  assert.equal(
    call.envelope.operation_fingerprint,
    ANIMATION_UE_OPERATION_ALLOWLIST.preflightAnimation.operation_fingerprint,
  );
  assert.equal(call.options.operationId, call.envelope.operation_id);
  assert.equal(call.options.operationFingerprint, call.envelope.operation_fingerprint);
  assert.equal(call.options.maxAttempts, 2);
  assert.equal(call.options.mutation, false);
  assert.equal(call.requestJson.includes("/Game/"), false);
  assert.equal(call.requestJson.includes("pawn_class_path"), false);
  assert.equal(call.requestJson.includes("skeleton_path"), false);
  assert.equal(call.requestJson.includes("implementation_asset"), false);
  assert.equal(call.requestJson.includes("execute_python_script"), false);
  assert.equal(call.requestJson.includes("montage_path"), false);
  assert.deepEqual(Object.keys(call.envelope.content_proof).sort(), [
    "content_digest",
    "content_revision",
    "profile_id",
    "profile_revision",
    "schema",
    "verification_receipt_id",
  ]);
});

test("adapter is directly compatible with the animation runtime preflight broker contract", async () => {
  const transport = makeTransport();
  const adapter = makeAdapter(transport);
  const evidence = (kind, assertion) => async () => ({
    evidence_id: `evidence:${kind}:0001`,
    artifact_ref: `animation/test/${kind}.json`,
    sha256: "e".repeat(64),
    assertion,
  });
  const runtime = createVistaAnimationRuntime({
    broker: adapter,
    contentProfile: makeProfile(),
    evidenceHooks: {
      pose_snapshot: evidence("pose", null),
      interaction_state: evidence("interaction", "pass"),
      screenshot: evidence("screenshot", null),
      scene_validation: evidence("scene", "pass"),
    },
  });
  const prepared = await runtime.preflight({
    sceneRevision: "mmg_040@aaaaaaaaaaaaaaaa",
    bindings: {
      schema: BINDINGS_SCHEMA,
      revision: "mmg040_animation_bindings_v1",
      actors: [{
        source_id: "camera_wearer",
        binding_id: ACTOR_ID,
        kind: "player",
        capabilities: ["upper_body_ik"],
      }],
      entities: [{
        source_id: "rolling_chair",
        binding_id: TARGET_ID,
        kind: "prop",
        capabilities: ["hand_contact_target"],
      }],
    },
    requestedActions: [ACTION],
  });
  assert.equal(prepared.artifact.start_allowed, true);
  assert.deepEqual(prepared.artifact.supported_actions, [ACTION]);
  assert.equal(prepared.capabilityRegistry.adapters[0].adapter_id, ACTION_DEFINITIONS[ACTION].adapter_id);
  assert.equal(transport.calls.length, 1);
  assert.equal(transport.calls[0].options.signal, undefined);
});

test("snapshot, start, wait, stop, release, restore enforce the exact lifecycle and broker options", async () => {
  const transport = makeTransport();
  const adapter = makeAdapter(transport);
  await preflight(adapter);
  assert.equal((await snapshot(adapter)).snapshot_id, SNAPSHOT_ID);
  assert.equal((await start(adapter)).action_handle, HANDLE_ID);
  assert.equal((await adapter.waitAnimationAction(makeWaitRequest())).completion_signal, "vista_brace_complete");
  assert.equal((await adapter.stopAnimationAction(makeStopRequest())).status, "stopped");
  assert.equal((await adapter.releaseAnimationAction(makeReleaseRequest())).status, "released");
  assert.equal((await adapter.restoreAnimationState(makeRestoreRequest())).status, "restored");

  assert.deepEqual(transport.calls.map((call) => call.envelope.operation_id), [
    "vista.animation.preflight.v1",
    "vista.animation.snapshot.v1",
    "vista.animation.start.v1",
    "vista.animation.wait.v1",
    "vista.animation.stop.v1",
    "vista.animation.release.v1",
    "vista.animation.restore.v1",
  ]);
  const mutations = transport.calls.filter((call) => call.options.mutation);
  assert.deepEqual(mutations.map((call) => call.envelope.operation_id), [
    "vista.animation.start.v1",
    "vista.animation.stop.v1",
    "vista.animation.release.v1",
    "vista.animation.restore.v1",
  ]);
  assert.equal(mutations.every((call) => call.options.maxAttempts === 1), true);
  assert.equal(transport.calls.filter((call) => !call.options.mutation).every((call) => call.options.maxAttempts === 2), true);
  assert.equal(new Set(transport.calls.map((call) => call.envelope.nonce_marker.nonce)).size, transport.calls.length);
  assert.equal(new Set(transport.calls.map((call) => call.envelope.invocation_id)).size, transport.calls.length);
  assert.equal(transport.calls.every((call) => call.requestJson.includes("/Game/") === false), true);
});

test("arbitrary Python, vbp, montage parameters, bridge ids, and request fields are rejected before transport", async () => {
  const transport = makeTransport();
  const adapter = makeAdapter(transport);
  await assert.rejects(
    adapter.preflightAnimation({ ...makePreflightRequest(), python: "print('unsafe')" }),
    (error) => error.code === "ANIMATION_UE_REQUEST_INVALID",
  );
  assert.equal(transport.calls.length, 0);

  await preflight(adapter);
  await snapshot(adapter);
  const callsBefore = transport.calls.length;
  await assert.rejects(
    adapter.startAnimationAction(makeStartRequest({ bridge_action_id: "vbp_arbitrary_command" })),
    (error) => error.code === "ANIMATION_UE_OPERATION_FORBIDDEN",
  );
  await assert.rejects(
    adapter.startAnimationAction(makeStartRequest({ parameters: { duration_sec: 2, hand: "both", montage_path: "/Game/Private" } })),
    (error) => error.code === "ANIMATION_UE_REQUEST_INVALID",
  );
  assert.equal(transport.calls.length, callsBefore);
});

test("nonce marker, operation identity, request digest, and response shape must echo exactly", async (t) => {
  const cases = [
    ["nonce", (response) => ({ ...response, nonce_marker: { ...response.nonce_marker, nonce: "f".repeat(32) } })],
    ["operation id", (response) => ({ ...response, operation_id: "vista.animation.snapshot.v1" })],
    ["fingerprint", (response) => ({ ...response, operation_fingerprint: "0".repeat(64) })],
    ["invocation id", (response) => ({ ...response, invocation_id: `${response.invocation_id}x` })],
    ["request digest", (response) => ({ ...response, request_digest: "0".repeat(64) })],
    ["extra field", (response) => ({ ...response, raw_logs: "private-token-value" })],
  ];
  for (const [name, mutateEnvelope] of cases) {
    await t.test(name, async () => {
      const transport = makeTransport({ mutateEnvelope });
      const adapter = makeAdapter(transport);
      await assert.rejects(
        preflight(adapter),
        (error) => error instanceof VistaAnimationUeAdapterError
          && error.code === "ANIMATION_UE_PROTOCOL_INVALID"
          && error.message.includes("private-token-value") === false,
      );
      assert.equal(transport.calls.length, 1);
    });
  }
});

test("every operation rejects malformed or mismatched payloads with exact validation", async (t) => {
  const cases = [
    {
      name: "preflight extra",
      operation: "vista.animation.preflight.v1",
      mutate: (payload) => ({ ...payload, raw_secret: "private-token-value" }),
      code: "ANIMATION_UE_PREFLIGHT_PROTOCOL_INVALID",
      drive: async (adapter) => preflight(adapter),
    },
    {
      name: "snapshot actor mismatch",
      operation: "vista.animation.snapshot.v1",
      mutate: (payload) => ({ ...payload, actor_binding_id: "actor_wrong" }),
      code: "ANIMATION_UE_SNAPSHOT_PROTOCOL_INVALID",
      drive: async (adapter) => { await preflight(adapter); return snapshot(adapter); },
    },
    {
      name: "start bridge mismatch",
      operation: "vista.animation.start.v1",
      mutate: (payload) => ({ ...payload, bridge_action_id: "vista_look_at_v1" }),
      code: "ANIMATION_UE_START_PROTOCOL_INVALID",
      drive: async (adapter) => { await preflight(adapter); await snapshot(adapter); return start(adapter); },
    },
    {
      name: "wait completion mismatch",
      operation: "vista.animation.wait.v1",
      mutate: (payload) => ({ ...payload, completion_signal: "wrong_signal" }),
      code: "ANIMATION_UE_WAIT_PROTOCOL_INVALID",
      drive: async (adapter) => { await preflight(adapter); await snapshot(adapter); await start(adapter); return adapter.waitAnimationAction(makeWaitRequest()); },
    },
    {
      name: "stop handle mismatch",
      operation: "vista.animation.stop.v1",
      mutate: (payload) => ({ ...payload, action_handle: "handle:wrong" }),
      code: "ANIMATION_UE_STOP_PROTOCOL_INVALID",
      drive: async (adapter) => { await preflight(adapter); await snapshot(adapter); await start(adapter); return adapter.stopAnimationAction(makeStopRequest()); },
    },
    {
      name: "release extra",
      operation: "vista.animation.release.v1",
      mutate: (payload) => ({ ...payload, raw_secret: "private-token-value" }),
      code: "ANIMATION_UE_RELEASE_PROTOCOL_INVALID",
      drive: async (adapter) => {
        await preflight(adapter);
        await snapshot(adapter);
        await start(adapter);
        await adapter.stopAnimationAction(makeStopRequest());
        return adapter.releaseAnimationAction(makeReleaseRequest());
      },
    },
    {
      name: "restore digest mismatch",
      operation: "vista.animation.restore.v1",
      mutate: (payload) => ({ ...payload, state_digest: "e".repeat(64) }),
      code: "ANIMATION_UE_RESTORE_PROTOCOL_INVALID",
      drive: async (adapter) => { await preflight(adapter); await snapshot(adapter); return adapter.restoreAnimationState(makeRestoreRequest()); },
    },
  ];
  for (const entry of cases) {
    await t.test(entry.name, async () => {
      const transport = makeTransport({
        mutatePayload(operation, payload) {
          return operation === entry.operation ? entry.mutate(payload) : payload;
        },
      });
      const adapter = makeAdapter(transport);
      const operationContract = Object.values(ANIMATION_UE_OPERATION_ALLOWLIST)
        .find((operation) => operation.operation_id === entry.operation);
      await assert.rejects(
        entry.drive(adapter),
        (error) => error instanceof VistaAnimationUeAdapterError
          && error.code === entry.code
          && error.status === 502
          && (operationContract.mutation ? error.details.outcome_unknown === true : true)
          && error.message.includes("private-token-value") === false,
      );
    });
  }
});

test("a failed mutation has unknown outcome, is sent once, and cannot be replayed", async () => {
  const transport = makeTransport({ throwOperation: "vista.animation.start.v1" });
  const adapter = makeAdapter(transport);
  await preflight(adapter);
  await snapshot(adapter);
  await assert.rejects(
    start(adapter),
    (error) => error.code === "ANIMATION_UE_MUTATION_OUTCOME_UNKNOWN"
      && error.retryable === false
      && error.message.includes("private-token-value") === false,
  );
  const startCalls = () => transport.calls.filter((call) => call.envelope.operation_id === "vista.animation.start.v1");
  assert.equal(startCalls().length, 1);
  assert.equal(startCalls()[0].options.maxAttempts, 1);
  await assert.rejects(start(adapter), (error) => error.code === "ANIMATION_UE_SNAPSHOT_REQUIRED");
  assert.equal(startCalls().length, 1);
});

test("invalid, throwing, and repeated nonce sources fail closed without a second transport call", async () => {
  for (const nonceFactory of [
    () => "not-a-nonce",
    () => { throw new Error("private nonce secret"); },
  ]) {
    const transport = makeTransport();
    const adapter = makeAdapter(transport, nonceFactory);
    await assert.rejects(
      preflight(adapter),
      (error) => error.code === "ANIMATION_UE_NONCE_INVALID" && error.message.includes("private nonce secret") === false,
    );
    assert.equal(transport.calls.length, 0);
  }

  const transport = makeTransport();
  const repeated = "a".repeat(32);
  const adapter = makeAdapter(transport, () => repeated);
  await preflight(adapter);
  await assert.rejects(preflight(adapter), (error) => error.code === "ANIMATION_UE_NONCE_INVALID");
  assert.equal(transport.calls.length, 1);
});

test("cross-preflight handles and restore before release are rejected before mutation", async () => {
  const transport = makeTransport();
  const adapter = makeAdapter(transport);
  await preflight(adapter);
  await snapshot(adapter);
  await start(adapter);
  const callsBefore = transport.calls.length;
  await assert.rejects(
    adapter.waitAnimationAction(makeWaitRequest({ preflight_id: `vap-${"e".repeat(24)}` })),
    (error) => error.code === "ANIMATION_UE_PREFLIGHT_MISMATCH",
  );
  await assert.rejects(
    adapter.restoreAnimationState(makeRestoreRequest()),
    (error) => error.code === "ANIMATION_UE_RELEASE_REQUIRED",
  );
  assert.equal(transport.calls.length, callsBefore);
});

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const {
  ACTION_DEFINITIONS,
  ANIMATION_CONTENT_PROFILE_SCHEMA,
  ANIMATION_PREFLIGHT_RESPONSE_SCHEMA,
} = require("../vista-animation-contract");
const {
  RELEASE_RESPONSE_SCHEMA,
  RESTORE_RESPONSE_SCHEMA,
  SNAPSHOT_RESPONSE_SCHEMA,
  START_RESPONSE_SCHEMA,
  STOP_RESPONSE_SCHEMA,
  WAIT_RESPONSE_SCHEMA,
  createVistaAnimationRuntime,
} = require("../vista-animation-runtime");
const {
  AGENT_ACTION_AUTHORIZATION_SCHEMA,
  LEGACY_HUMANOID_PAWN,
  SERVER_MAPPINGS,
  VistaAnimationAgentActionRegistryError,
  auditVistaAgentActionSource,
  createVistaAnimationAgentActionPolicy,
} = require("../vista-animation-agent-action-registry");
const { BINDINGS_SCHEMA } = require("../vista-timeline-compiler");

const SERVER_ROOT = path.resolve(__dirname, "..");
const SCENE_REVISION = "mmg_040@agent-action-registry";
const CONTENT_DIGEST = "b".repeat(64);
const STATE_DIGEST = "c".repeat(64);

function makeProfile(actions = ["pause"], overrides = {}) {
  return {
    schema: ANIMATION_CONTENT_PROFILE_SCHEMA,
    profile_id: "vista_legacy_agent_action_v1",
    revision: "vista_legacy_agent_action_2026_r1",
    content_revision: "simworld-content-2026.07.21-r1",
    content_digest: CONTENT_DIGEST,
    pawn_class_path: LEGACY_HUMANOID_PAWN,
    skeleton_path: "/Game/TrafficSystem/Pedestrian/SK_Base_User_Agent.SK_Base_User_Agent",
    verification: {
      status: "verified",
      receipt_id: "ue-content-receipt:2026-07-21:legacy-agent-action",
      verified_at: "2026-07-21T00:00:00.000Z",
    },
    actions: actions.map((action) => ({
      action,
      adapter_id: ACTION_DEFINITIONS[action].adapter_id,
      version: "1.0.0",
      bridge_action_id: ACTION_DEFINITIONS[action].bridge_action_id,
      implementation_asset: action === "pause"
        ? LEGACY_HUMANOID_PAWN
        : `/Game/VISTA/Animations/ABP_${action}.ABP_${action}`,
      completion_signal: `vista_${action}_complete`,
      timeout_ms: 2500,
    })),
    ...overrides,
  };
}

function makeBindings(capabilities = ["hold_pose"]) {
  return {
    schema: BINDINGS_SCHEMA,
    revision: "legacy_agent_action_bindings_v1",
    actors: [{
      source_id: "camera_wearer",
      binding_id: "actor_camera_wearer",
      kind: "player",
      capabilities,
    }],
    entities: [],
  };
}

function makeBroker({ capabilities = ["hold_pose"] } = {}) {
  const log = [];
  return {
    log,
    async preflightAnimation(request) {
      log.push(["preflight", request]);
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
          capabilities,
          anchor_kinds: [],
        })),
        targets: [],
        actions: request.requested_actions.map((action) => ({
          action,
          bridge_action_id: ACTION_DEFINITIONS[action].bridge_action_id,
          available: true,
          implementation_matches: true,
          completion_signal_available: true,
        })),
      };
    },
    async snapshotAnimationState(request) {
      log.push(["snapshot", request]);
      return {
        schema: SNAPSHOT_RESPONSE_SCHEMA,
        status: "captured",
        snapshot_id: `snapshot:${request.event_id}`,
        action: request.action,
        actor_binding_id: request.actor_binding_id,
        target_binding_id: request.target_binding_id,
        engine_time: 1,
        state_digest: STATE_DIGEST,
      };
    },
    async startAnimationAction(request) {
      log.push(["start", request]);
      return {
        schema: START_RESPONSE_SCHEMA,
        status: "started",
        action_handle: `handle:${request.event_id}`,
        bridge_action_id: request.bridge_action_id,
        actor_binding_id: request.actor_binding_id,
        target_binding_id: request.target_binding_id,
        engine_time: 1.1,
      };
    },
    async waitAnimationAction(request) {
      log.push(["wait", request]);
      return {
        schema: WAIT_RESPONSE_SCHEMA,
        status: "completed",
        action_handle: request.action_handle,
        completion_signal: "vista_pause_complete",
        engine_time: 1.5,
        evidence_ids: [`broker:${request.event_id}`],
      };
    },
    async stopAnimationAction(request) {
      log.push(["stop", request]);
      return {
        schema: STOP_RESPONSE_SCHEMA,
        status: "stopped",
        action_handle: request.action_handle,
        engine_time: 1.6,
      };
    },
    async releaseAnimationAction(request) {
      log.push(["release", request]);
      return {
        schema: RELEASE_RESPONSE_SCHEMA,
        status: "released",
        action_handle: request.action_handle,
      };
    },
    async restoreAnimationState(request) {
      log.push(["restore", request]);
      return {
        schema: RESTORE_RESPONSE_SCHEMA,
        status: "restored",
        snapshot_id: request.snapshot_id,
        state_digest: request.state_digest,
        engine_time: 1.7,
      };
    },
  };
}

function evidenceHooks() {
  let sequence = 0;
  const make = (kind, assertion) => async (context) => {
    sequence += 1;
    return {
      evidence_id: `evidence:${kind}:${sequence}`,
      artifact_ref: `animation/${context.run_id}/${kind}-${sequence}.json`,
      sha256: String(sequence % 10).repeat(64),
      assertion,
    };
  };
  return {
    pose_snapshot: make("pose_snapshot", null),
    interaction_state: make("interaction_state", null),
    screenshot: make("screenshot", null),
    scene_validation: make("scene_validation", "pass"),
  };
}

function createRuntime({ profile = makeProfile(), capabilities = ["hold_pose"] } = {}) {
  const broker = makeBroker({ capabilities });
  const runtime = createVistaAnimationRuntime({
    broker,
    contentProfile: profile,
    evidenceHooks: evidenceHooks(),
    clock: () => "2026-07-21T00:00:00.000Z",
  });
  return { broker, runtime };
}

function authorization(profile, requestedActions) {
  return {
    schema: AGENT_ACTION_AUTHORIZATION_SCHEMA,
    profile_id: profile.profile_id,
    profile_revision: profile.revision,
    content_digest: profile.content_digest,
    requested_actions: requestedActions,
  };
}

function lifecycleContext(reason) {
  return {
    run_id: "animation_run_legacy_001",
    timeline_id: `vtl-${"a".repeat(24)}`,
    scene_revision: SCENE_REVISION,
    event_id: "beat-0001",
    action: "pause",
    actor_binding_id: "actor_camera_wearer",
    target_binding_id: null,
    attempt: 1,
    planned_sec: 0,
    parameters: { duration_sec: 1 },
    ...(reason ? { reason } : {}),
  };
}

test("bundled agent_action audit exposes only the pinned pause candidate mapping", () => {
  const audit = auditVistaAgentActionSource();
  assert.equal(audit.ready, true);
  assert.deepEqual(audit.issues, []);
  assert.deepEqual(audit.supported_mappings, [{
    semantic_action: "pause",
    source_agent_type: "humanoid",
    source_action: "stop_action",
    adapter_id: ACTION_DEFINITIONS.pause.adapter_id,
    bridge_action_id: ACTION_DEFINITIONS.pause.bridge_action_id,
    mapping_fingerprint: SERVER_MAPPINGS.pause.mapping_fingerprint,
  }]);
  assert.equal(Object.isFrozen(audit), true);
  assert.equal(Object.isFrozen(SERVER_MAPPINGS.pause), true);
});

test("legacy pause becomes a typed adapter only after profile and live capability proof", async () => {
  const { broker, runtime } = createRuntime();
  const prepared = await runtime.preflight({
    sceneRevision: SCENE_REVISION,
    bindings: makeBindings(),
    requestedActions: ["pause"],
  });
  assert.equal(prepared.artifact.start_allowed, true);
  assert.match(prepared.artifact.registry_revision, /^anim_[a-f0-9]{16}$/);
  assert.deepEqual(prepared.capabilityRegistry.adapters.map((entry) => entry.action), ["pause"]);
  const adapter = prepared.capabilityRegistry.adapters[0];
  assert.equal(adapter.adapter_id, ACTION_DEFINITIONS.pause.adapter_id);
  assert.equal(adapter.timeout_ms, 2500);
  assert.deepEqual(
    ["precondition", "execute", "completion", "timeout", "cancel", "cleanup"].map((name) => typeof adapter[name]),
    ["function", "function", "function", "function", "function", "function"],
  );
  assert.deepEqual(broker.log.map(([operation]) => operation), ["preflight"]);
});

test("drag, brace, lift_foot, and fall are rejected before even read-only UE preflight", async () => {
  for (const action of ["drag", "brace", "lift_foot", "fall"]) {
    const { broker, runtime } = createRuntime({ profile: makeProfile(["pause", action]) });
    await assert.rejects(
      runtime.preflight({
        sceneRevision: SCENE_REVISION,
        bindings: makeBindings(),
        requestedActions: [action],
      }),
      (error) => (
        error instanceof VistaAnimationAgentActionRegistryError
        && error.code === "ANIMATION_AGENT_ACTION_UNSUPPORTED"
        && error.details.unsupported.includes(action)
      ),
    );
    assert.deepEqual(broker.log, []);
  }
});

test("authorization rejects caller-authored command, code, and asset path fields", () => {
  const profile = makeProfile();
  const policy = createVistaAnimationAgentActionPolicy({ contentProfile: profile });
  assert.deepEqual(policy.authorize(authorization(profile, ["pause"])).requested_actions, ["pause"]);
  for (const injected of [
    { command: "vbp Actor StopAction" },
    { python: "import unreal" },
    { implementation_path: "/Game/Private/Injected.Injected_C" },
  ]) {
    assert.throws(
      () => policy.authorize({ ...authorization(profile, ["pause"]), ...injected }),
      (error) => error.code === "ANIMATION_AGENT_ACTION_INPUT_INVALID",
    );
  }
});

test("profile and actor capability mismatches fail closed", async () => {
  const wrongProfile = makeProfile(["pause"]);
  wrongProfile.actions[0].implementation_asset = "/Game/VISTA/Animations/ABP_pause.ABP_pause";
  assert.throws(
    () => createVistaAnimationAgentActionPolicy({ contentProfile: wrongProfile }),
    (error) => error.code === "ANIMATION_AGENT_ACTION_PROFILE_MISMATCH",
  );

  const { broker, runtime } = createRuntime({ capabilities: [] });
  await assert.rejects(
    runtime.preflight({
      sceneRevision: SCENE_REVISION,
      bindings: makeBindings([]),
      requestedActions: ["pause"],
    }),
    (error) => error.code === "ANIMATION_AGENT_ACTION_CAPABILITY_MISMATCH",
  );
  assert.deepEqual(broker.log.map(([operation]) => operation), ["preflight"]);
});

test("timeout performs typed stop and cleanup releases then restores the exact snapshot", async () => {
  const { broker, runtime } = createRuntime();
  const prepared = await runtime.preflight({
    sceneRevision: SCENE_REVISION,
    bindings: makeBindings(),
    requestedActions: ["pause"],
  });
  const adapter = prepared.capabilityRegistry.adapters[0];
  const context = lifecycleContext();
  await adapter.precondition(context);
  await adapter.execute(context);
  const timeout = await adapter.timeout(context);
  assert.equal(timeout.stopped, true);
  const cleanup = await adapter.cleanup(lifecycleContext("timed_out"));
  assert.deepEqual(cleanup, { cleaned: true, released: true, restored: true });
  assert.deepEqual(
    broker.log.map(([operation]) => operation),
    ["preflight", "snapshot", "start", "stop", "release", "restore"],
  );
  const stopRequest = broker.log.find(([operation]) => operation === "stop")[1];
  const releaseRequest = broker.log.find(([operation]) => operation === "release")[1];
  const restoreRequest = broker.log.find(([operation]) => operation === "restore")[1];
  assert.equal(stopRequest.reason, "timeout");
  assert.equal(releaseRequest.action_handle, "handle:beat-0001");
  assert.equal(restoreRequest.snapshot_id, "snapshot:beat-0001");
  assert.equal(restoreRequest.state_digest, STATE_DIGEST);
});

test("normal completion requires the pinned signal and cleanup only releases transient controls", async () => {
  const { broker, runtime } = createRuntime();
  const prepared = await runtime.preflight({
    sceneRevision: SCENE_REVISION,
    bindings: makeBindings(),
    requestedActions: ["pause"],
  });
  const adapter = prepared.capabilityRegistry.adapters[0];
  const context = lifecycleContext();
  await adapter.precondition(context);
  await adapter.execute(context);
  const completed = await adapter.completion(context);
  assert.equal(completed.completed, true);
  assert.equal(completed.completion_signal, "vista_pause_complete");
  const cleanup = await adapter.cleanup(lifecycleContext("completed"));
  assert.deepEqual(cleanup, { cleaned: true, released: true, restored: false });
  assert.deepEqual(
    broker.log.map(([operation]) => operation),
    ["preflight", "snapshot", "start", "wait", "release"],
  );
});

test("source parity keeps legacy commands outside the VISTA adapter execution path", () => {
  const registrySource = fs.readFileSync(path.join(SERVER_ROOT, "agent-registry.json"), "utf8");
  const mcpSource = fs.readFileSync(path.join(SERVER_ROOT, "mcp-server.js"), "utf8");
  const adapterSource = fs.readFileSync(path.join(SERVER_ROOT, "vista-animation-agent-action-registry.js"), "utf8");
  const runtimeSource = fs.readFileSync(path.join(SERVER_ROOT, "vista-animation-runtime.js"), "utf8");
  assert.match(registrySource, /"stop_action"\s*:\s*\{\s*"cmd"\s*:\s*"StopAction"/);
  assert.match(mcpSource, /let cmdStr=`vbp \$\{agent_name\} \$\{actionDef\.cmd\}`/);
  assert.doesNotMatch(adapterSource, /ucvCommand|cmdStr|execute_python_script|invokeAnimationContentApi|`vbp /);
  const authorizeIndex = runtimeSource.indexOf("agentActionPolicy.authorize({");
  const preflightIndex = runtimeSource.indexOf('callBroker(broker, "preflightAnimation"');
  assert.ok(authorizeIndex >= 0 && preflightIndex > authorizeIndex);
});

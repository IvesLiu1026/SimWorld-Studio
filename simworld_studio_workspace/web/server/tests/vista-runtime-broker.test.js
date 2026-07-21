"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const {
  FIXED_CLEANUP_SCRIPT,
  FIXED_SETUP_SCRIPT,
  FIXED_STATE_SCRIPT,
  FIXED_STOP_SCRIPT,
  VISTA_CLEANUP_MARKER,
  VISTA_CLEANUP_SCHEMA,
  VISTA_GAME_MODE_CLASS,
  VISTA_PAWN_CLASS,
  VISTA_SCENE_PROOF_SCHEMA,
  VISTA_SETUP_MARKER,
  VISTA_SETUP_SCHEMA,
  VISTA_STATE_MARKER,
  VISTA_STATE_SCHEMA,
  VISTA_STOP_MARKER,
  VISTA_STOP_SCHEMA,
  bindFixedRuntimeScript,
  createVistaRuntimeBroker,
  createVistaRuntimeControllerRegistry,
  extractSingleMarker,
  hasStrictlyEmptyBody,
  normalizeSceneProof,
  runtimeBindingDigest,
  sceneManifestDigest,
} = require("../vista-runtime-broker");

const NONCE = "a".repeat(32);
const DIGESTS = Object.freeze({
  asset: "b".repeat(64),
  semantic: "c".repeat(64),
  material: "d".repeat(64),
  evidence: "e".repeat(64),
  surface: "6".repeat(64),
});

function identity(overrides = {}) {
  return {
    ownerId: "owner-runtime",
    sessionId: "session-runtime",
    slotId: 1,
    leaseId: "lease-runtime-00000001",
    mcpPort: 55561,
    ...overrides,
  };
}

function actorManifest(name = "VISTA_Floor") {
  return [{
    actor_name: name,
    fingerprint: `vsa-${"f".repeat(24)}`,
    operation_id: `vso-${"2".repeat(24)}`,
    object_guid: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
  }];
}

function proof(overrides = {}) {
  const { actorName, ...proofOverrides } = overrides;
  const manifest = actorManifest(actorName);
  return {
    schema: VISTA_SCENE_PROOF_SCHEMA,
    plan_id: `vsp-${"1".repeat(24)}`,
    scene_id: "mmg_040_a07",
    actor_manifest_digest: sceneManifestDigest(manifest),
    actor_count: manifest.length,
    content_revision: "content-r1",
    verification_revision: "verification-r1",
    asset_evidence_digest: DIGESTS.asset,
    semantic_binding_digest: DIGESTS.semantic,
    material_pbr_evidence_digest: DIGESTS.material,
    evidence_bundle_digest: DIGESTS.evidence,
    live_surface_digest: DIGESTS.surface,
    start_allowed: true,
    ...proofOverrides,
  };
}

function stoppedState() {
  return {
    schema: VISTA_STATE_SCHEMA,
    pie: false,
    possessed: false,
    pawn_class: null,
    location: null,
    rotation: null,
    velocity: null,
    on_ground: null,
    engine_time: null,
  };
}

function liveState(time = 1) {
  return {
    schema: VISTA_STATE_SCHEMA,
    pie: true,
    possessed: true,
    pawn_class: VISTA_PAWN_CLASS,
    location: [0, 0, 96],
    rotation: [0, 90, 0],
    velocity: [0, 0, 0],
    on_ground: true,
    engine_time: time,
  };
}

function markerFromScript(script, marker, payload) {
  const match = script.match(new RegExp(`${marker}:([a-f0-9]{32}):`));
  assert.ok(match, `missing bound ${marker} nonce`);
  return { result: { python_logs: [`LogPython: ${marker}:${match[1]}:${JSON.stringify(payload)}`] } };
}

function setupPayload(sceneProof, overrides = {}) {
  return {
    schema: VISTA_SETUP_SCHEMA,
    phase: "play_requested",
    play_requested: true,
    was_playing: false,
    game_mode_class: VISTA_GAME_MODE_CLASS,
    pawn_class: VISTA_PAWN_CLASS,
    default_pawn_class: VISTA_PAWN_CLASS,
    player_start_count: 1,
    scene_actor_count: sceneProof.actor_count,
    surface_actor_count: 1,
    scene_manifest_digest: sceneProof.actor_manifest_digest,
    ...overrides,
  };
}

function stopPayload(overrides = {}) {
  return {
    schema: VISTA_STOP_SCHEMA,
    phase: "stop_requested",
    stop_requested: true,
    was_playing: true,
    ...overrides,
  };
}

function cleanupPayload(overrides = {}) {
  return {
    schema: VISTA_CLEANUP_SCHEMA,
    phase: "cleaned",
    binding_removed: true,
    ...overrides,
  };
}

function scriptedBroker(sceneProof, states, options = {}) {
  const calls = [];
  return {
    calls,
    async send(type, params, sendOptions) {
      calls.push({ type, params, options: sendOptions });
      assert.equal(type, "execute_python_script");
      assert.deepEqual(Object.keys(params), ["script"]);
      assert.equal(await sendOptions.preSendAuthorize(), true);
      if (params.script.includes(VISTA_SETUP_MARKER)) {
        if (options.setupError) throw options.setupError;
        return markerFromScript(params.script, VISTA_SETUP_MARKER, setupPayload(sceneProof));
      }
      if (params.script.includes(VISTA_STOP_MARKER)) {
        if (options.stopError) throw options.stopError;
        return markerFromScript(params.script, VISTA_STOP_MARKER, stopPayload());
      }
      if (params.script.includes(VISTA_CLEANUP_MARKER)) {
        return markerFromScript(params.script, VISTA_CLEANUP_MARKER, cleanupPayload());
      }
      assert.ok(params.script.includes(VISTA_STATE_MARKER));
      const state = states.length > 1 ? states.shift() : states[0];
      return markerFromScript(params.script, VISTA_STATE_MARKER, state);
    },
  };
}

test("fixed source starts/stops PIE without toolbar coordinates, caller code, or fallback geometry", () => {
  assert.match(FIXED_SETUP_SCRIPT, /editor_request_begin_play\(\)/);
  assert.match(FIXED_SETUP_SCRIPT, /hasattr\(level_editor_subsystem, 'editor_request_begin_play'\)/);
  assert.match(FIXED_SETUP_SCRIPT, /scene_manifest_digest/);
  assert.match(FIXED_SETUP_SCRIPT, /surface_count < 1/);
  assert.match(FIXED_SETUP_SCRIPT, /live_surface_digest != EXPECTED_SURFACE/);
  assert.match(FIXED_SETUP_SCRIPT, /content-revision\.json/);
  assert.match(FIXED_SETUP_SCRIPT, /isinstance\(material, unreal\.MaterialInterface\)/);
  assert.match(FIXED_STATE_SCRIPT, /live_surface_digest != EXPECTED_SURFACE/);
  assert.match(FIXED_SETUP_SCRIPT, /SIMWORLD_VISTA_RUNTIME_V2_BINDING=/);
  assert.match(FIXED_SETUP_SCRIPT, /binding_tags != \[expected_binding_tag\]/);
  assert.match(FIXED_STOP_SCRIPT, /editor_request_end_play\(\)/);
  assert.match(FIXED_STOP_SCRIPT, /refusing to reconcile tags owned by another Studio lease or scene/);
  assert.match(FIXED_CLEANUP_SCRIPT, /tag not in \[BINDING_TAG, SCENE_TAG\]/);
  for (const source of [FIXED_SETUP_SCRIPT, FIXED_STATE_SCRIPT, FIXED_STOP_SCRIPT, FIXED_CLEANUP_SCRIPT]) {
    assert.doesNotMatch(source, /486|78|spawn_actor|spawn_actor_from|BasicShapes|Cube\.Cube|save_(?:asset|dirty)/i);
  }
  assert.doesNotMatch(FIXED_SETUP_SCRIPT, /set_editor_property\('default_game_mode'/);
  assert.equal(hasStrictlyEmptyBody(undefined), true);
  assert.equal(hasStrictlyEmptyBody({}), true);
  assert.equal(hasStrictlyEmptyBody(undefined, { "content-length": "1" }), false);
  assert.equal(hasStrictlyEmptyBody({ script: "caller" }), false);
});

test("fixed script binding replaces only server nonce, lease digest, and scene digest", () => {
  const id = identity();
  const sceneProof = proof();
  const bound = bindFixedRuntimeScript(
    FIXED_SETUP_SCRIPT,
    VISTA_SETUP_MARKER,
    NONCE,
    runtimeBindingDigest(id),
    sceneProof.actor_manifest_digest,
    sceneProof.live_surface_digest,
  );
  assert.match(bound, new RegExp(`${VISTA_SETUP_MARKER}:${NONCE}:`));
  assert.match(bound, new RegExp(runtimeBindingDigest(id)));
  assert.match(bound, new RegExp(sceneProof.actor_manifest_digest));
  assert.match(bound, new RegExp(sceneProof.live_surface_digest));
  assert.doesNotMatch(bound, /PLACEHOLDER_V2/);
  assert.throws(() => bindFixedRuntimeScript(
    `${FIXED_SETUP_SCRIPT}\n${FIXED_SETUP_SCRIPT}`,
    VISTA_SETUP_MARKER,
    NONCE,
    runtimeBindingDigest(id),
    sceneProof.actor_manifest_digest,
    sceneProof.live_surface_digest,
  ), /exactly one/);
});

test("one lease-bound controller performs backend Start, state confirmation, Stop, and exact cleanup", async () => {
  const id = identity();
  const sceneProof = proof();
  let active = true;
  const ue = scriptedBroker(sceneProof, [stoppedState(), liveState(), stoppedState()]);
  const controller = createVistaRuntimeBroker({
    identity: id,
    sceneProof,
    resolveUeBroker: () => ue,
    isActiveSessionBinding: (candidate) => active && runtimeBindingDigest(candidate) === runtimeBindingDigest(id),
    nonceFactory: () => NONCE,
    delay: async () => {},
    pollIntervalMs: 1,
  });
  const started = await controller.start();
  assert.deepEqual(started, {
    schema: VISTA_SETUP_SCHEMA,
    phase: "live",
    prepared: true,
    play_requested: true,
    already_playing: false,
    pie: true,
    possessed: true,
    game_mode_class: VISTA_GAME_MODE_CLASS,
    pawn_class: VISTA_PAWN_CLASS,
    player_start_present: true,
    scene_proof_digest: sceneProof.actor_manifest_digest,
    play_lease_granted: true,
  });
  assert.equal(ue.calls.find((call) => call.params.script.includes(VISTA_SETUP_MARKER)).options.maxAttempts, 1);
  const stopped = await controller.stop();
  assert.deepEqual(stopped, {
    schema: VISTA_STOP_SCHEMA,
    phase: "stopped",
    stop_requested: true,
    was_playing: true,
    confirmed_stopped: true,
    ended_pie: true,
    binding_cleaned: true,
  });
  assert.equal(ue.calls.find((call) => call.params.script.includes(VISTA_STOP_MARKER)).options.maxAttempts, 1);
  assert.equal(ue.calls.find((call) => call.params.script.includes(VISTA_CLEANUP_MARKER)).options.maxAttempts, 2);
  active = false;
  await assert.rejects(controller.state({ force: true }), (error) => error.code === "VISTA_RUNTIME_LEASE_REVOKED");
});

test("lost mutation receipts never fabricate ended_pie and recover only through exact state", async () => {
  const id = identity();
  const sceneProof = proof();
  const lost = new Error("response lost");
  const ue = scriptedBroker(sceneProof, [stoppedState(), liveState(), stoppedState()], {
    setupError: lost,
    stopError: lost,
  });
  const controller = createVistaRuntimeBroker({
    identity: id,
    sceneProof,
    resolveUeBroker: () => ue,
    isActiveSessionBinding: () => true,
    nonceFactory: () => NONCE,
    delay: async () => {},
    pollIntervalMs: 1,
  });
  const started = await controller.start();
  assert.equal(started.phase, "live");
  assert.equal(started.play_requested, false);
  const stopped = await controller.stop();
  assert.equal(stopped.confirmed_stopped, true);
  assert.equal(stopped.was_playing, null);
  assert.equal(stopped.ended_pie, false);
});

test("authoritative Stop cannot overtake an unsettled backend Start mutation", async () => {
  const id = identity();
  const sceneProof = proof();
  let releaseSetup;
  let setupEntered;
  const entered = new Promise((resolve) => { setupEntered = resolve; });
  const setupBarrier = new Promise((resolve) => { releaseSetup = resolve; });
  const states = [stoppedState(), liveState(), stoppedState()];
  const calls = [];
  const ue = {
    calls,
    async send(_type, params, options) {
      calls.push(params.script);
      assert.equal(await options.preSendAuthorize(), true);
      if (params.script.includes(VISTA_SETUP_MARKER)) {
        setupEntered();
        await setupBarrier;
        return markerFromScript(params.script, VISTA_SETUP_MARKER, setupPayload(sceneProof));
      }
      if (params.script.includes(VISTA_STOP_MARKER)) {
        return markerFromScript(params.script, VISTA_STOP_MARKER, stopPayload());
      }
      if (params.script.includes(VISTA_CLEANUP_MARKER)) {
        return markerFromScript(params.script, VISTA_CLEANUP_MARKER, cleanupPayload());
      }
      return markerFromScript(params.script, VISTA_STATE_MARKER, states.shift() || stoppedState());
    },
  };
  const controller = createVistaRuntimeBroker({
    identity: id,
    sceneProof,
    resolveUeBroker: () => ue,
    isActiveSessionBinding: () => true,
    nonceFactory: () => NONCE,
    delay: async () => {},
    pollIntervalMs: 1,
  });
  const starting = controller.start();
  await entered;
  const stopping = controller.stop();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(calls.filter((script) => script.includes(VISTA_STOP_MARKER)).length, 0);
  releaseSetup();
  await assert.rejects(starting, (error) => error.code === "VISTA_SETUP_SUPERSEDED");
  const stopped = await stopping;
  assert.equal(stopped.confirmed_stopped, true);
  assert.equal(calls.filter((script) => script.includes(VISTA_STOP_MARKER)).length, 1);
});

test("registry isolates two sessions/two slots and quarantines an old same-slot lease", async () => {
  const first = identity({ sessionId: "session-one", leaseId: "lease-runtime-00000001", slotId: 1, mcpPort: 55561 });
  const second = identity({ sessionId: "session-two", leaseId: "lease-runtime-00000002", slotId: 2, mcpPort: 55563 });
  const replacement = identity({ sessionId: "session-three", leaseId: "lease-runtime-00000003", slotId: 1, mcpPort: 55561 });
  const proofs = new Map([[runtimeBindingDigest(first), proof({ actorName: "Floor_One" })], [runtimeBindingDigest(second), proof({ actorName: "Floor_Two" })], [runtimeBindingDigest(replacement), proof({ actorName: "Floor_Three" })]]);
  const brokers = new Map([
    [runtimeBindingDigest(first), scriptedBroker(proofs.get(runtimeBindingDigest(first)), [stoppedState(), liveState()])],
    [runtimeBindingDigest(second), scriptedBroker(proofs.get(runtimeBindingDigest(second)), [stoppedState(), liveState()])],
    [runtimeBindingDigest(replacement), scriptedBroker(proofs.get(runtimeBindingDigest(replacement)), [stoppedState()])],
  ]);
  const active = new Set(brokers.keys());
  const registry = createVistaRuntimeControllerRegistry({
    resolveIdentity: (request) => request.identity,
    resolveUeBroker: (candidate) => brokers.get(runtimeBindingDigest(candidate)),
    isActiveSessionBinding: (candidate) => active.has(runtimeBindingDigest(candidate)),
    resolveSceneProof: (candidate) => proofs.get(runtimeBindingDigest(candidate)),
    nonceFactory: () => NONCE,
    delay: async () => {},
    pollIntervalMs: 1,
  });
  const [one, two] = await Promise.all([
    registry.startForIdentity(first),
    registry.startForIdentity(second),
  ]);
  assert.equal(one.scene_proof_digest, proofs.get(runtimeBindingDigest(first)).actor_manifest_digest);
  assert.equal(two.scene_proof_digest, proofs.get(runtimeBindingDigest(second)).actor_manifest_digest);
  const oldController = registry.controllerFor(first);
  await registry.stateForIdentity(replacement, { force: true });
  await assert.rejects(oldController.stop(), (error) => error.code === "VISTA_RUNTIME_QUARANTINED");
  assert.notEqual(runtimeBindingDigest(first), runtimeBindingDigest(replacement));
});

test("physical slot ownership is exclusive across owners as well as sessions", async () => {
  const first = identity({ ownerId: "owner-one", sessionId: "session-one" });
  const replacement = identity({
    ownerId: "owner-two",
    sessionId: "session-two",
    leaseId: "lease-runtime-00000002",
  });
  const firstProof = proof({ actorName: "Floor_One" });
  const replacementProof = proof({ actorName: "Floor_Two" });
  const brokers = new Map([
    [runtimeBindingDigest(first), scriptedBroker(firstProof, [stoppedState()])],
    [runtimeBindingDigest(replacement), scriptedBroker(replacementProof, [stoppedState()])],
  ]);
  const registry = createVistaRuntimeControllerRegistry({
    resolveIdentity: (request) => request.identity,
    resolveUeBroker: (candidate) => brokers.get(runtimeBindingDigest(candidate)),
    isActiveSessionBinding: () => true,
    resolveSceneProof: (candidate) => (
      runtimeBindingDigest(candidate) === runtimeBindingDigest(first) ? firstProof : replacementProof
    ),
    nonceFactory: () => NONCE,
  });
  const oldController = registry.controllerFor(first);
  registry.controllerFor(replacement);
  await assert.rejects(oldController.readState({ force: true }), (error) => error.code === "VISTA_RUNTIME_QUARANTINED");
});

test("queued/retried sends revalidate the exact lease before every dispatch", async () => {
  const id = identity();
  const sceneProof = proof();
  let active = true;
  let authorizationChecks = 0;
  const broker = {
    async send(_type, _params, options) {
      authorizationChecks += 1;
      assert.equal(await options.preSendAuthorize(), true);
      active = false;
      authorizationChecks += 1;
      assert.equal(await options.preSendAuthorize(), false);
      const error = new Error("authorization expired");
      error.code = "UE_COMMAND_AUTHORIZATION_EXPIRED";
      throw error;
    },
  };
  const controller = createVistaRuntimeBroker({
    identity: id,
    sceneProof,
    resolveUeBroker: () => broker,
    isActiveSessionBinding: () => active,
    nonceFactory: () => NONCE,
  });
  await assert.rejects(controller.state({ force: true }), (error) => error.code === "VISTA_RUNTIME_LEASE_REVOKED");
  assert.equal(authorizationChecks, 2);
});

test("scene proof and marker contracts reject degraded, malformed, duplicate, or extra data", () => {
  assert.throws(
    () => normalizeSceneProof({ ...proof(), start_allowed: false }),
    (error) => error.code === "VISTA_SCENE_PROOF_INVALID",
  );
  assert.throws(
    () => normalizeSceneProof({ ...proof(), material_pbr_evidence_digest: "x" }),
    (error) => error.code === "VISTA_SCENE_PROOF_INVALID",
  );
  assert.throws(
    () => normalizeSceneProof({ ...proof(), live_surface_digest: "x" }),
    (error) => error.code === "VISTA_SCENE_PROOF_INVALID",
  );
  const valid = liveState();
  assert.throws(() => extractSingleMarker({ result: { python_logs: [] } }, VISTA_STATE_MARKER), /VISTA_MARKER_MISSING/);
  assert.throws(() => extractSingleMarker({ result: { python_logs: [
    `${VISTA_STATE_MARKER}:${JSON.stringify(valid)}`,
    `${VISTA_STATE_MARKER}:${JSON.stringify(valid)}`,
  ] } }, VISTA_STATE_MARKER), /VISTA_MARKER_DUPLICATE/);
});

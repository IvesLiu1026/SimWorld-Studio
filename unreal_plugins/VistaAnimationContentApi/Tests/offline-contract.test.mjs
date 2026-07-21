import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import {
  chmodSync,
  cpSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";
import { spawnSync } from "node:child_process";
import test from "node:test";

const here = path.dirname(fileURLToPath(import.meta.url));
const pluginRoot = path.resolve(here, "..");
const repositoryRoot = path.resolve(pluginRoot, "../..");
const require = createRequire(import.meta.url);
const readiness = require(path.join(repositoryRoot, "simworld_studio_workspace/web/server/vista-animation-ue-readiness.js"));
const adapter = require(path.join(repositoryRoot, "simworld_studio_workspace/web/server/vista-animation-ue-adapter.js"));
const animationContract = require(path.join(repositoryRoot, "simworld_studio_workspace/web/server/vista-animation-contract.js"));

const contract = JSON.parse(readFileSync(path.join(pluginRoot, "Contract/vista-animation-content-api-v1.json"), "utf8"));
const subsystemSource = readFileSync(path.join(
  pluginRoot,
  "Source/VistaAnimationContentApi/Private/VistaAnimationContentApiSubsystem.cpp",
), "utf8");
const subsystemHeader = readFileSync(path.join(
  pluginRoot,
  "Source/VistaAnimationContentApi/Public/VistaAnimationContentApiSubsystem.h",
), "utf8");
const driverHeader = readFileSync(path.join(
  pluginRoot,
  "Source/VistaAnimationContentApi/Public/VistaAnimationContentDriver.h",
), "utf8");
const mmg040DriverHeader = readFileSync(path.join(
  pluginRoot,
  "Source/VistaAnimationContentApi/Public/VistaMmg040ContentDriver.h",
), "utf8");
const mmg040DriverSource = readFileSync(path.join(
  pluginRoot,
  "Source/VistaAnimationContentApi/Private/VistaMmg040ContentDriver.cpp",
), "utf8");
const strictJsonSource = readFileSync(path.join(
  pluginRoot,
  "Source/VistaAnimationContentApi/Private/VistaAnimationStrictJson.cpp",
), "utf8");

test("plugin descriptor and minimum source inventory are complete", () => {
  const descriptor = JSON.parse(readFileSync(path.join(pluginRoot, "VistaAnimationContentApi.uplugin"), "utf8"));
  assert.equal(descriptor.FileVersion, 3);
  assert.equal(descriptor.VersionName, "1.1.0");
  assert.equal(descriptor.CanContainContent, false);
  assert.deepEqual(descriptor.Modules, [{
    Name: "VistaAnimationContentApi",
    Type: "Runtime",
    LoadingPhase: "Default",
  }]);
  const includes = subsystemHeader.split("\n").filter((line) => line.startsWith("#include"));
  assert.equal(includes.at(-1), '#include "VistaAnimationContentApiSubsystem.generated.h"');

  const temporaryProject = mkdtempSync(path.join(os.tmpdir(), "vista-animation-source-audit-"));
  cpSync(pluginRoot, path.join(temporaryProject, "Plugins/VistaAnimationContentApi"), { recursive: true });
  const audit = readiness.inspectVistaAnimationUePluginSource(temporaryProject);
  assert.equal(audit.source_tree_complete, true);
  assert.deepEqual(audit.missing_files, []);
  for (const relative of [
    "Source/VistaAnimationContentApi/Public/VistaMmg040ContentDriver.h",
    "Source/VistaAnimationContentApi/Private/VistaMmg040ContentDriver.cpp",
    "ContentProfiles/vista-mmg040-project-profile-source-v1.json",
    "Contract/vista-animation-project-profile-source-v1.schema.json",
    "Contract/vista-animation-content-inspection-receipt-v1.schema.json",
  ]) assert.equal(existsSync(path.join(pluginRoot, relative)), true, `missing ${relative}`);
});

test("subsystem binds every trusted config to the driver's sealed profile receipt", () => {
  assert.match(driverHeader, /virtual bool ValidateTrustedProfile\(/);
  const validationIndex = subsystemSource.indexOf("InDriver->ValidateTrustedProfile");
  const configuredIndex = subsystemSource.indexOf("Config = InConfig");
  assert.ok(validationIndex > 0);
  assert.ok(configuredIndex > validationIndex, "profile validation must run before configuration is accepted");
  assert.match(subsystemSource, /ANIMATION_DRIVER_PROFILE_UNVERIFIED/);
});

test("mmg_040 concrete driver pins content and dispatches only typed project actions", () => {
  const sourceContractPath = path.join(
    pluginRoot,
    "ContentProfiles/vista-mmg040-project-profile-source-v1.json",
  );
  const sourceContractBytes = readFileSync(sourceContractPath);
  const sourceContract = JSON.parse(sourceContractBytes);
  const sourceContractSha = createHash("sha256").update(sourceContractBytes).digest("hex");
  const joinedCppLiterals = mmg040DriverSource.replace(/"\s*"/g, "");
  assert.ok(mmg040DriverSource.includes(sourceContractSha));
  for (const asset of sourceContract.assets) {
    assert.ok(joinedCppLiterals.includes(asset.object_path), `missing C++ pin ${asset.object_path}`);
    for (const notify of asset.required_notifies) {
      assert.ok(mmg040DriverSource.includes(notify), `missing C++ notify ${notify}`);
    }
  }
  for (const action of sourceContract.actions) {
    assert.ok(mmg040DriverSource.includes(action.adapter_id));
    assert.ok(mmg040DriverSource.includes(action.bridge_action_id));
    assert.ok(mmg040DriverSource.includes(action.completion_signal));
    for (const capability of [
      ...action.actor_capabilities,
      ...action.target_capabilities,
      ...action.anchor_kinds,
      ...action.live_checks,
    ]) assert.ok(mmg040DriverSource.includes(capability), `missing C++ action pin ${capability}`);
  }
  const enumNames = {
    look_at: "LookAt", brace: "Brace", drag: "Drag", lift_foot: "LiftFoot",
    pause: "Pause", fall: "Fall", recover: "Recover",
  };
  const actionSection = mmg040DriverSource.slice(
    mmg040DriverSource.indexOf("FVistaMmg040ContentDriver::PinnedActions()"),
    mmg040DriverSource.indexOf("FVistaMmg040ContentDriver::FVistaMmg040ContentDriver("),
  );
  for (const [index, action] of sourceContract.actions.entries()) {
    const marker = `{EVistaAnimationAction::${enumNames[action.action]},`;
    const start = actionSection.indexOf(marker);
    const next = index + 1 < sourceContract.actions.length
      ? actionSection.indexOf(`{EVistaAnimationAction::${enumNames[sourceContract.actions[index + 1].action]},`, start + marker.length)
      : actionSection.length;
    assert.ok(start >= 0 && next > start, `missing grouped C++ pin for ${action.action}`);
    const block = actionSection.slice(start, next);
    for (const value of [
      action.action, action.adapter_id, action.bridge_action_id, action.completion_signal,
      ...action.actor_capabilities, ...action.target_capabilities, ...action.anchor_kinds,
      ...action.live_checks,
    ]) assert.ok(block.includes(value), `${action.action} C++ block missing ${value}`);
    assert.ok(block.includes(String(action.timeout_ms)), `${action.action} C++ block missing timeout`);
    for (const value of Object.values(action.parameter_contract).filter((entry) => entry !== null)) {
      assert.ok(block.includes(String(value)), `${action.action} C++ block missing parameter ${value}`);
    }
  }
  for (const method of [
    "StartLookAt", "StartBrace", "StartDrag", "StartLiftFoot", "StartPause", "StartFall", "StartRecover",
  ]) {
    assert.match(mmg040DriverHeader, new RegExp(`virtual bool ${method}\\(`));
    assert.match(mmg040DriverSource, new RegExp(`Backend->${method}\\(`));
  }
  for (const forbidden of [
    "execute_python_script", "ExecutePythonCommand", "ExecuteConsoleCommand", "GEngine->Exec",
    "ProcessEvent(", "StaticLoadObject", "LoadObject<", "ExecuteCommand(", "vbp ",
  ]) {
    assert.equal(mmg040DriverSource.includes(forbidden), false, `forbidden driver primitive: ${forbidden}`);
  }
  assert.match(mmg040DriverSource, /Backend->InspectPinnedAsset\(/);
  assert.match(mmg040DriverSource, /ANIMATION_MMG040_PINNED_ASSET_MISSING/);
  assert.match(mmg040DriverSource, /ANIMATION_MMG040_NOTIFY_MISMATCH/);
  assert.match(mmg040DriverSource, /ANIMATION_MMG040_CONTENT_DIGEST_MISMATCH/);
  assert.match(mmg040DriverSource, /MatchesRuntimeParameters\(Parameters, Pin->Parameters\)/);
  assert.match(mmg040DriverSource, /ANIMATION_MMG040_PARAMETERS_UNVERIFIED/);
  assert.match(mmg040DriverSource, /ContainsAll\(Binding\.Capabilities, Pin\.TargetCapabilities\)/);
  assert.match(mmg040DriverSource, /ContainsAll\(Binding\.AnchorKinds, Pin\.AnchorKinds\)/);
});

test("failed mmg_040 starts are contained by driver-owned handles, rollback, and quarantine", () => {
  const backendOutput = mmg040DriverHeader.match(
    /struct FVistaMmg040BackendStartOutput \{([\s\S]*?)\n\};/,
  );
  assert.ok(backendOutput, "typed backend start output is missing");
  assert.match(backendOutput[1], /double EngineTimeSec/);
  assert.doesNotMatch(backendOutput[1], /ActionHandle/);

  for (const method of [
    "StartLookAt", "StartBrace", "StartDrag", "StartLiftFoot", "StartPause", "StartFall", "StartRecover",
  ]) {
    const declaration = mmg040DriverHeader.match(
      new RegExp(`virtual bool ${method}\\(([\\s\\S]*?)\\) = 0;`),
    );
    assert.ok(declaration, `missing backend declaration ${method}`);
    assert.match(declaration[1], /const FString &ActionHandle/);
    assert.match(declaration[1], /FVistaMmg040BackendStartOutput &Output/);
    assert.ok(
      declaration[1].indexOf("ActionHandle") < declaration[1].indexOf("FVistaAnimationActionParameters"),
      `${method} must receive the reserved handle before parameters`,
    );
  }

  const startBody = mmg040DriverSource.slice(
    mmg040DriverSource.indexOf("bool FVistaMmg040ContentDriver::Start("),
    mmg040DriverSource.indexOf("bool FVistaMmg040ContentDriver::Wait("),
  );
  assert.match(mmg040DriverSource, /HandleNamespace\(\s*FGuid::NewGuid\(\)\.ToString\(EGuidFormats::Digits\)\.ToLower\(\)\)/);
  assert.match(startBody, /NextHandleSequence > MaxActionHandlesPerDriver/);
  assert.match(startBody, /ANIMATION_MMG040_ACTION_HANDLE_BUDGET_EXHAUSTED/);
  assert.match(startBody, /FString::FromInt\(static_cast<int32>\(NextHandleSequence\+\+\)\)/);
  assert.match(startBody, /ActiveHandles\.Add\(ActionHandle, Action\)/);
  for (const method of [
    "StartLookAt", "StartBrace", "StartDrag", "StartLiftFoot", "StartPause", "StartFall", "StartRecover",
  ]) assert.match(startBody, new RegExp(`Backend->${method}\\([\\s\\S]*?ActionHandle`));

  const backendFalse = startBody.match(/if \(!bStarted\) \{([\s\S]*?)\n  \}/);
  assert.ok(backendFalse, "backend false-return containment is missing");
  assert.match(backendFalse[1], /ActiveHandles\.Remove\(ActionHandle\)/);
  assert.match(backendFalse[1], /ANIMATION_MMG040_START_REJECTED/);
  assert.match(mmg040DriverHeader, /Returning false MUST be side-effect-free/);

  assert.match(startBody, /Backend->RollbackFailedStart\(Action, ActionHandle, RollbackError\)/);
  assert.match(startBody, /ANIMATION_MMG040_START_OUTPUT_INVALID/);
  assert.match(startBody, /bMutationQuarantined = true/);
  assert.match(startBody, /bPreflightReady = false/);
  assert.match(startBody, /ANIMATION_MMG040_MUTATION_OUTCOME_UNKNOWN/);
  assert.ok(
    startBody.indexOf("ANIMATION_MMG040_MUTATION_QUARANTINED") <
      startBody.indexOf("IsPreflightBindingAllowed"),
    "quarantine must be reported before a stale preflight error",
  );

  const waitBody = mmg040DriverSource.slice(
    mmg040DriverSource.indexOf("bool FVistaMmg040ContentDriver::Wait("),
    mmg040DriverSource.indexOf("bool FVistaMmg040ContentDriver::Stop("),
  );
  assert.match(waitBody, /ActiveHandles\.Find\(ActionHandle\)/);
  assert.doesNotMatch(waitBody, /bPreflightReady|bMutationQuarantined/);
});

test("mmg_040 evidence capture preserves the exact preflight action-target pair", () => {
  const preflightBody = mmg040DriverSource.slice(
    mmg040DriverSource.indexOf("bool FVistaMmg040ContentDriver::Preflight("),
    mmg040DriverSource.indexOf("bool FVistaMmg040ContentDriver::IsPreflightBindingAllowed("),
  );
  const evidenceBody = mmg040DriverSource.slice(
    mmg040DriverSource.indexOf("bool FVistaMmg040ContentDriver::CaptureEvidence("),
  );
  assert.match(preflightBody, /ReadyActionTargetPairs = MoveTemp\(VerifiedActionTargetPairs\)/);
  assert.match(evidenceBody, /bTargetRequired/);
  assert.match(
    evidenceBody,
    /ReadyActionTargetPairs\.Contains\(\s*ActionTargetKey\(\s*Action,\s*Input\.TargetBindingId\.GetValue\(\)\)\)/,
  );
  assert.match(evidenceBody, /ANIMATION_MMG040_EVIDENCE_UNVERIFIED/);
  assert.ok(
    evidenceBody.indexOf("ReadyActionTargetPairs.Contains") <
      evidenceBody.indexOf("Backend->CaptureEvidence"),
    "backend evidence capture must follow exact action-target authorization",
  );
});

test("completion cannot be synthesized from a timer or trusted config string", () => {
  assert.match(driverHeader, /FString ObservedCompletionSignal;/);
  assert.match(driverHeader, /FString CompletionEvidenceId;/);
  assert.match(driverHeader, /FString CompletionEvidenceSha256;/);
  assert.match(mmg040DriverSource, /Output\.ObservedCompletionSignal != Pin->CompletionSignal/);
  assert.match(mmg040DriverSource, /SeenEvidence\.Contains\(Output\.CompletionEvidenceId\)/);
  assert.match(mmg040DriverSource, /ANIMATION_MMG040_COMPLETION_SIGNAL_MISMATCH/);
  assert.match(mmg040DriverSource, /ANIMATION_MMG040_COMPLETION_EVIDENCE_INVALID/);
  assert.match(subsystemSource, /Output\.ObservedCompletionSignal != CompletionSignal/);
  assert.match(subsystemSource, /Output\.EvidenceIds\.Contains\(Output\.CompletionEvidenceId\)/);
  assert.match(subsystemSource, /Quote\(Output\.ObservedCompletionSignal\)/);
  assert.doesNotMatch(subsystemSource, /Quote\(CompletionSignal\), Output\.EngineTimeSec/);
  assert.match(subsystemSource, /Action\.AdapterId != Fixed->BridgeActionId/);
  assert.match(subsystemSource, /Action\.BridgeActionId != Fixed->BridgeActionId/);
  assert.match(subsystemSource, /Action\.TimeoutMs < 100/);
});

test("compiled receipt validation preserves exact provenance and live coverage", () => {
  for (const literal of [
    "vista-animation-content-inspection-receipt/v1",
    "gym_citynav",
    "5.3.2",
    "verified",
    "ue53_disposable_live_inspection_v1",
    "pawn_spawnable",
    "generated_class_matches",
    "skeletal_mesh_matches",
    "anim_blueprint_matches",
    "no_redirectors",
    "dependency_closure",
    "disposable_pie",
    "scene_zero_diff",
  ]) assert.ok(mmg040DriverSource.includes(literal), `missing compiled receipt pin ${literal}`);
  assert.match(mmg040DriverSource, /SameStringSet\(Candidate\.PassedChecks, RequiredReceiptChecks\(\)\)/);
  assert.match(mmg040DriverSource, /SameStringSet\(Observed\.ObservedLiveChecks, Pin->LiveChecks\)/);
  assert.match(mmg040DriverSource, /SameParameters\(Observed\.VerifiedParameters, Pin->Parameters\)/);
  assert.doesNotMatch(mmg040DriverHeader, /bAllRequiredChecksPassed/);
});

test("portable manifest exactly matches server capability, operation, action, and security contracts", () => {
  assert.equal(contract.schema, readiness.ANIMATION_UE_PLUGIN_API_SCHEMA);
  assert.equal(contract.capability.request_schema, readiness.ANIMATION_UE_CAPABILITY_REQUEST_SCHEMA);
  assert.equal(contract.capability.response_schema, readiness.ANIMATION_UE_CAPABILITY_RESPONSE_SCHEMA);
  assert.equal(contract.capability.operation_id, readiness.ANIMATION_UE_CAPABILITY_OPERATION_ID);
  assert.equal(contract.capability.operation_fingerprint, readiness.ANIMATION_UE_CAPABILITY_OPERATION_FINGERPRINT);
  assert.equal(contract.operation_allowlist_digest, readiness.OPERATION_SET.operation_allowlist_digest);
  assert.deepEqual(contract.operations, readiness.OPERATION_SET.operations);
  assert.deepEqual(contract.security, readiness.SECURITY_POLICY);

  const expectedActions = Object.entries(animationContract.ACTION_DEFINITIONS).map(([action, definition]) => ({
    action,
    bridge_action_id: definition.bridge_action_id,
    target_policy: definition.target_policy,
  }));
  assert.deepEqual(contract.actions, expectedActions);
  assert.deepEqual(
    contract.operations.map((operation) => operation.operation_fingerprint).sort(),
    Object.values(adapter.ANIMATION_UE_OPERATION_ALLOWLIST).map((operation) => operation.operation_fingerprint).sort(),
  );
});

test("portable manifest reserves exactly the four dedicated transport commands", () => {
  assert.deepEqual(contract.reserved_commands, [
    {
      command_type: "vista_animation_capabilities",
      request_schema: "vista-animation-ue-capability-probe/v1",
      response_schema: "vista-animation-ue-capability/v1",
      retry_policy: "single_attempt",
    },
    {
      command_type: "vista_animation_content_api",
      request_schema: "vista-animation-ue-request/v1",
      response_schema: "vista-animation-ue-response/v1",
      retry_policy: "operation_bound",
    },
    {
      command_type: "vista_animation_engine_time",
      request_schema: "vista-animation-engine-time-request/v1",
      response_schema: "vista-animation-engine-time-response/v1",
      retry_policy: "single_attempt",
    },
    {
      command_type: "vista_animation_evidence_capture",
      request_schema: "vista-animation-evidence-capture-request/v1",
      response_schema: "vista-animation-evidence-capture-response/v1",
      retry_policy: "single_attempt",
    },
  ]);
  assert.deepEqual(contract.engine_time, {
    clock: "ue_process_monotonic",
    request_keys: ["schema", "run_id", "timeline_id", "event_id", "slot_binding", "request_digest"],
    response_keys: ["schema", "run_id", "timeline_id", "event_id", "slot_binding", "request_digest", "engine_time_sec"],
    slot_binding_keys: ["owner_id", "session_id", "slot_id", "scene_revision"],
    slot_binding_required: true,
    request_digest_required: true,
  });
  assert.deepEqual(contract.evidence_capture.request_keys, [
    "schema", "kind", "slot_binding", "context", "context_digest",
  ]);
  assert.deepEqual(contract.evidence_capture.response_keys, [
    "schema", "kind", "context_digest", "evidence",
  ]);
  assert.deepEqual(contract.evidence_capture.slot_binding_keys, [
    "owner_id", "session_id", "slot_id", "scene_revision",
  ]);
  assert.deepEqual(contract.evidence_capture.context_keys, [
    "schema", "run_id", "timeline_id", "scene_revision", "event_id", "action",
    "actor_binding_id", "target_binding_id", "planned_sec", "at_frame", "attempt",
    "phase", "snapshot_id", "action_handle",
  ]);
  assert.deepEqual(contract.evidence_capture.evidence_keys, [
    "evidence_id", "artifact_ref", "sha256", "assertion",
  ]);
  assert.deepEqual(contract.evidence_capture.kinds, [
    "pose_snapshot",
    "interaction_state",
    "screenshot",
    "scene_validation",
  ]);
  assert.equal(contract.evidence_capture.driver_descriptor_required, true);
  for (const key of ["caller_paths", "caller_classes", "caller_functions", "caller_scripts"]) {
    assert.equal(contract.evidence_capture[key], false);
  }
});

test("compiled source pins every contract identifier and has no generic execution primitive", () => {
  const requiredLiterals = [
    contract.capability.operation_id,
    contract.capability.operation_fingerprint,
    contract.operation_allowlist_digest,
    ...contract.operations.flatMap((operation) => [
      operation.operation_id,
      operation.operation_fingerprint,
      operation.request_schema,
      operation.response_schema,
    ]),
    ...contract.actions.flatMap((action) => [action.action, action.bridge_action_id]),
    ...contract.reserved_commands.flatMap((command) => [
      command.command_type,
      command.request_schema,
      command.response_schema,
    ]),
  ];
  for (const literal of requiredLiterals) assert.ok(subsystemSource.includes(literal), `missing C++ literal ${literal}`);
  for (const forbidden of [
    "ExecutePythonCommand",
    "IPythonScriptPlugin",
    "ExecuteConsoleCommand",
    "GEngine->Exec",
    "StaticLoadObject",
    "LoadObject<",
    "ProcessEvent(",
    "ExecuteCommand(",
  ]) assert.equal(subsystemSource.includes(forbidden), false, `generic execution primitive found: ${forbidden}`);
  assert.match(strictJsonSource, /JSON_DUPLICATE_KEY/);
  assert.match(strictJsonSource, /MaxDepth = 16/);
  assert.match(subsystemSource, /MaxReplayEntries = 4096/);
  assert.match(subsystemSource, /ANIMATION_MUTATION_OUTCOME_UNKNOWN/);
});

test("dispatcher, engine clock, and evidence driver remain fixed and fail closed", () => {
  const dispatchCommands = [...subsystemSource.matchAll(
    /CommandType == TEXT\("(vista_animation_[a-z_]+)"\)/g,
  )].map((match) => match[1]);
  assert.deepEqual(dispatchCommands, contract.reserved_commands.map((entry) => entry.command_type));
  assert.match(subsystemSource, /EVistaAnimationFixedDispatchResult::RejectedUnknownCommand/);
  assert.match(subsystemSource, /FPlatformTime::Seconds\(\) - ProcessMonotonicOriginSec/);
  assert.match(subsystemSource, /ValidateRuntimeSlotBinding\(\*SlotBinding, SlotCanonical\)/);
  assert.match(subsystemSource, /Sha256HexUtf8\(DigestInput\) != RequestDigest/);
  assert.match(subsystemSource, /Sha256HexUtf8\(DigestInput\) != ContextDigest/);
  assert.match(subsystemSource, /LocalDriver->CaptureEvidence\(Input, Output, DriverError\)/);
  assert.match(subsystemSource, /IsSafeArtifactRef\(Output\.ArtifactRef\)/);
  assert.match(subsystemSource, /IsLowerHex\(Output\.Sha256, 64\)/);
  assert.match(subsystemSource, /LastPreflight\.RequestedActions\.Contains\(\s*EvidenceAction->Name\)/);
  assert.doesNotMatch(subsystemSource, /LastPreflight\.RequestedActions\.Contains\(\s*Input\.Action\.GetValue\(\)\)/);
  assert.equal(/Output\.Assertion\s*=(?!=)/.test(subsystemSource), false, "plugin must not synthesize evidence assertions");

  const inputBlock = driverHeader.match(
    /struct FVistaAnimationEvidenceCaptureInput \{([\s\S]*?)\n\};/,
  );
  assert.ok(inputBlock, "typed evidence input is missing");
  for (const forbiddenField of ["Path", "Class", "Function", "Script", "Command"]) {
    assert.equal(
      new RegExp(`FString\\s+\\w*${forbiddenField}\\w*`, "i").test(inputBlock[1]),
      false,
      `caller-controlled ${forbiddenField} field is forbidden`,
    );
  }
  assert.match(driverHeader, /virtual bool CaptureEvidence\(/);
});

test("server accepts a capability response with the portable fixed contract", () => {
  const slotIdentity = {
    schema: readiness.ANIMATION_UE_SLOT_BINDING_SCHEMA,
    owner_id: "owner-test",
    session_id: "session-test",
    slot_id: "slot-test",
    scene_revision: "scene-test",
  };
  const slotBinding = {
    ...slotIdentity,
    binding_digest: animationContract.digest(slotIdentity),
  };
  const contentProof = {
    schema: adapter.ANIMATION_UE_CONTENT_PROOF_SCHEMA,
    profile_id: "vista_mmg040",
    profile_revision: "profile_r1",
    content_revision: "content-r1",
    content_digest: "1".repeat(64),
    verification_receipt_id: "receipt-test",
  };
  const pluginArtifact = {
    schema: readiness.ANIMATION_UE_PLUGIN_ARTIFACT_SCHEMA,
    plugin_name: readiness.ANIMATION_UE_PLUGIN_NAME,
    plugin_version: "1.1.0",
    plugin_build_id: "offline-test-build",
    binary_sha256: "2".repeat(64),
    engine_version: "5.3.2",
    target_platform: "linux-x86_64",
    api_schema: readiness.ANIMATION_UE_PLUGIN_API_SCHEMA,
  };
  const response = {
    schema: readiness.ANIMATION_UE_CAPABILITY_RESPONSE_SCHEMA,
    status: "ready",
    operation_id: readiness.ANIMATION_UE_CAPABILITY_OPERATION_ID,
    operation_fingerprint: readiness.ANIMATION_UE_CAPABILITY_OPERATION_FINGERPRINT,
    nonce_marker: { schema: adapter.ANIMATION_UE_MARKER_SCHEMA, nonce: "3".repeat(32) },
    challenge_digest: "4".repeat(64),
    slot_binding: slotBinding,
    plugin_artifact: pluginArtifact,
    process_instance_id: "vapi-offline-test",
    content_proof: contentProof,
    security: contract.security,
    operation_allowlist_digest: contract.operation_allowlist_digest,
    operations: contract.operations,
  };
  const validated = readiness.validateCapabilityResponse(JSON.stringify(response), {
    nonce: "3".repeat(32),
    challengeDigest: "4".repeat(64),
    slotBinding,
    pluginArtifact,
    contentProof,
  });
  assert.equal(validated.operation_allowlist_digest, contract.operation_allowlist_digest);
});

test("artifact manifest helper hashes a regular binary and emits the exact schema", () => {
  const temporary = mkdtempSync(path.join(os.tmpdir(), "vista-animation-artifact-"));
  const binary = path.join(temporary, "libUnrealEditor-VistaAnimationContentApi.so");
  writeFileSync(binary, "offline-test-binary\n", { mode: 0o600 });
  const result = spawnSync(process.execPath, [
    path.join(pluginRoot, "Scripts/create-artifact-manifest.mjs"),
    "--binary", binary,
    "--build-id", "offline-test-build",
    "--engine-version", "5.3.2",
    "--target-platform", "linux-x86_64",
  ], { encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr);
  const manifest = JSON.parse(result.stdout);
  assert.deepEqual(manifest, {
    schema: readiness.ANIMATION_UE_PLUGIN_ARTIFACT_SCHEMA,
    plugin_name: readiness.ANIMATION_UE_PLUGIN_NAME,
    plugin_version: "1.1.0",
    plugin_build_id: "offline-test-build",
    binary_sha256: createHash("sha256").update("offline-test-binary\n").digest("hex"),
    engine_version: "5.3.2",
    target_platform: "linux-x86_64",
    api_schema: readiness.ANIMATION_UE_PLUGIN_API_SCHEMA,
  });

  const link = path.join(temporary, "linked.so");
  symlinkSync(binary, link);
  const rejected = spawnSync(process.execPath, [
    path.join(pluginRoot, "Scripts/create-artifact-manifest.mjs"),
    "--binary", link,
    "--build-id", "offline-test-build",
    "--engine-version", "5.3.2",
    "--target-platform", "linux-x86_64",
  ], { encoding: "utf8" });
  assert.notEqual(rejected.status, 0);
});

test("scripts use non-mutating dry runs and explicit install creates the audited tree", () => {
  for (const script of ["install-plugin.sh", "build-plugin.sh"]) {
    const syntax = spawnSync("sh", ["-n", path.join(pluginRoot, "Scripts", script)], { encoding: "utf8" });
    assert.equal(syntax.status, 0, syntax.stderr);
  }
  const project = mkdtempSync(path.join(os.tmpdir(), "vista-animation-project-"));
  writeFileSync(path.join(project, "Disposable.uproject"), "{}\n", { mode: 0o600 });
  const dryRun = spawnSync("sh", [
    path.join(pluginRoot, "Scripts/install-plugin.sh"),
    "--project-root", project,
  ], { encoding: "utf8" });
  assert.equal(dryRun.status, 0, dryRun.stderr);
  assert.match(dryRun.stdout, /dry run only/);
  assert.equal(existsSync(path.join(project, "Plugins")), false);

  const install = spawnSync("sh", [
    path.join(pluginRoot, "Scripts/install-plugin.sh"),
    "--project-root", project,
    "--apply",
  ], { encoding: "utf8" });
  assert.equal(install.status, 0, install.stderr);
  assert.equal(readiness.inspectVistaAnimationUePluginSource(project).source_tree_complete, true);

  const engine = path.join(project, "FakeEngine");
  const runUat = path.join(engine, "Engine/Build/BatchFiles/RunUAT.sh");
  mkdirSync(path.dirname(runUat), { recursive: true });
  writeFileSync(runUat, "#!/bin/sh\nexit 99\n", { mode: 0o700 });
  chmodSync(runUat, 0o700);
  const buildDryRun = spawnSync("sh", [
    path.join(pluginRoot, "Scripts/build-plugin.sh"),
    "--engine-root", engine,
    "--output", path.join(project, "PackageOutput"),
    "--platform", "Linux",
  ], {
    encoding: "utf8",
    env: { ...process.env, VISTA_ANIMATION_PLUGIN_BUILD_ID: "offline-test-build" },
  });
  assert.equal(buildDryRun.status, 0, buildDryRun.stderr);
  assert.match(buildDryRun.stdout, /dry run only/);
  assert.equal(existsSync(path.join(project, "PackageOutput")), false);
});

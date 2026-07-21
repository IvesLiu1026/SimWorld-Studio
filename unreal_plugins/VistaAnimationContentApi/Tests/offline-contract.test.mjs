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
const strictJsonSource = readFileSync(path.join(
  pluginRoot,
  "Source/VistaAnimationContentApi/Private/VistaAnimationStrictJson.cpp",
), "utf8");

test("plugin descriptor and minimum source inventory are complete", () => {
  const descriptor = JSON.parse(readFileSync(path.join(pluginRoot, "VistaAnimationContentApi.uplugin"), "utf8"));
  assert.equal(descriptor.FileVersion, 3);
  assert.equal(descriptor.VersionName, "1.0.0");
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
    "vista_animation_capabilities",
    "vista_animation_content_api",
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
    plugin_version: "1.0.0",
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
    plugin_version: "1.0.0",
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

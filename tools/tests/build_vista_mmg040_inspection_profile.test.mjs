import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  chmodSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import {
  ContentProfileContractError,
  computeContentDigest,
  parseStrictJson,
  sha256Bytes,
  validateInspectionReceipt,
  validateSourceContract,
} from "../../unreal_plugins/VistaAnimationContentApi/Scripts/prepare-content-profile.mjs";
import {
  INSPECTION_PROFILE_SCHEMA,
  Mmg040InspectionProfileError,
  PINNED_CANDIDATE_SOURCE_SHA256,
  buildInspectionProfile,
  validateCandidateSource,
  validateInspectionProfile,
} from "../build_vista_mmg040_inspection_profile.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const repositoryRoot = path.resolve(here, "../..");
const contractPath = path.join(
  repositoryRoot,
  "unreal_plugins/VistaAnimationContentApi/ContentProfiles/vista-mmg040-project-profile-source-v1.json",
);
const candidatesPath = path.join(
  repositoryRoot,
  "tools/assets/vista_mmg_040_gym_citynav_candidate_sources_v1.json",
);
const schemaPath = path.join(repositoryRoot, "tools/vista_mmg040_inspection_profile_schema.json");
const scriptPath = path.join(repositoryRoot, "tools/build_vista_mmg040_inspection_profile.mjs");
const contractBytes = readFileSync(contractPath);
const contractSha256 = sha256Bytes(contractBytes);
const contract = validateSourceContract(parseStrictJson(contractBytes), contractSha256);
const candidateBytes = readFileSync(candidatesPath);
const candidateSha256 = sha256Bytes(candidateBytes);
const candidates = validateCandidateSource(parseStrictJson(candidateBytes), candidateSha256, contract);

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function digest(label) {
  return createHash("sha256").update(label).digest("hex");
}

function makeReceipt() {
  const receipt = {
    schema: "vista-animation-content-inspection-receipt/v1",
    source_contract_sha256: contractSha256,
    profile_id: contract.profile_id,
    profile_revision: contract.profile_revision,
    content_revision: "gym-citynav-mmg040-content-r1",
    content_digest: "0".repeat(64),
    project: {
      project_name: contract.target.project_name,
      engine_version: contract.target.engine_version,
      project_revision: "gym-citynav-project-r1",
      project_descriptor_sha256: digest("gym-citynav.uproject"),
    },
    verification: {
      status: "verified",
      receipt_id: "mmg040-live-inspection-r1",
      verified_at: "2026-07-21T08:00:00Z",
      operator_id: "content-owner-test",
      method: contract.receipt_requirements.method,
    },
    assets: contract.assets.map((asset) => ({
      asset_id: asset.asset_id,
      object_path: asset.object_path,
      observed_class: asset.expected_class,
      package_sha256: digest(`package:${asset.asset_id}`),
      loaded: true,
      skeleton_asset_id: asset.skeleton_asset_id,
      notify_names: [...asset.required_notifies],
      root_motion_enabled: asset.root_motion_policy === "required",
    })),
    actions: contract.actions.map((action) => ({
      action: action.action,
      implementation_asset_id: action.implementation_asset_id,
      completion_signal: action.completion_signal,
      implementation_matches: true,
      skeleton_matches: true,
      completion_signal_observed: true,
      behavior_evidence_sha256: digest(`behavior:${action.action}`),
      ik_contact_verified: ["brace", "drag", "lift_foot"].includes(action.action),
      root_motion_verified: ["drag", "fall", "recover"].includes(action.action),
      collision_verified: action.action === "fall",
      recovery_alignment_verified: action.action === "recover",
      observed_live_checks: [...action.live_checks],
      verified_parameters: clone(action.parameter_contract),
    })),
    checks: Object.fromEntries(contract.receipt_requirements.required_checks.map((key) => [key, true])),
  };
  receipt.content_digest = computeContentDigest(contractSha256, receipt);
  return receipt;
}

function hasKey(value, searchedKey) {
  if (Array.isArray(value)) return value.some((item) => hasKey(item, searchedKey));
  if (value === null || typeof value !== "object") return false;
  return Object.entries(value).some(([key, item]) => key === searchedKey || hasKey(item, searchedKey));
}

function expectProfileCode(callback, code) {
  assert.throws(callback, (error) => {
    assert.ok(error instanceof Mmg040InspectionProfileError);
    assert.equal(error.code, code);
    return true;
  });
}

test("candidate source is byte-pinned to the official archive and filename-only evidence", () => {
  assert.equal(candidateSha256, PINNED_CANDIDATE_SOURCE_SHA256);
  assert.equal(candidates.source_binding.archive_sha256,
    "806e869ad1c65b298f05a39854b28e4188bb50817f539744451849e054990e2f");
  assert.equal(candidates.source_binding.project_revision,
    "source-patch:51426e97354477dca1635217455e644e9ca98976");
  assert.equal(candidates.filesystem_inventory_observation.package_file_count, 2937);
  assert.equal(candidates.filesystem_inventory_observation.count_semantics,
    "uasset_plus_umap_not_catalog_count");
  assert.equal(candidates.filesystem_inventory_observation.asset_registry_bundle, null);
  assert.equal(hasKey(candidates, "asset_count"), false);
  assert.ok(candidates.scene_object_candidates.every((entry) => entry.possible_target_asset_ids.length === 0));
  assert.ok(candidates.animation_source_candidates.every((entry) => !entry.filesystem_locator.startsWith("/Game/")));
  assert.ok(candidates.animation_source_candidates.every((entry) =>
    entry.locator_kind === "content_relative_prefix" ? entry.filesystem_locator.endsWith("/") : entry.filesystem_locator.endsWith(".uasset")));
  expectProfileCode(
    () => validateCandidateSource(candidates, "0".repeat(64), contract),
    "ANIMATION_MMG040_CANDIDATE_SOURCE_MISMATCH",
  );
});

test("candidate mode derives all statuses and remains blocked without a live receipt", () => {
  const profile = buildInspectionProfile(contract, candidates, candidateSha256);
  assert.equal(validateInspectionProfile(profile, contract), profile);
  assert.equal(profile.schema, INSPECTION_PROFILE_SCHEMA);
  assert.equal(profile.verification_status, "candidate_unverified");
  assert.equal(profile.start_allowed, false);
  assert.equal(profile.runtime_ready, false);
  assert.equal(profile.source_lineage_status, "candidate_unverified");
  assert.equal(profile.live_inspection, null);
  assert.equal(profile.derived_targets.length, 13);
  assert.deepEqual(profile.derived_targets.map((entry) => entry.asset_id), contract.assets.map((entry) => entry.asset_id));
  assert.ok(profile.derived_targets.every((entry) =>
    entry.content_status === "not_authored_or_unverified" &&
    entry.verification_status === "candidate_unverified" &&
    entry.source_lineage_status === "candidate_unverified" &&
    entry.authoring_required === true &&
    entry.live_receipt_evidence === null));
  assert.ok([...profile.scene_object_candidates, ...profile.animation_source_candidates]
    .every((entry) => entry.candidate_status === "candidate_unverified"));
  assert.equal(hasKey(profile, "asset_count"), false);
  assert.equal(profile.proof_boundary.runtime_capability_challenge_verified, false);
  assert.equal(profile.proof_boundary.executable_profile_emitted, false);
});

test("schema fixes the candidate/live split, thirteen targets, and false runtime gates", () => {
  const schema = JSON.parse(readFileSync(schemaPath, "utf8"));
  assert.equal(schema.properties.schema.const, INSPECTION_PROFILE_SCHEMA);
  assert.equal(schema.properties.start_allowed.const, false);
  assert.equal(schema.properties.runtime_ready.const, false);
  assert.equal(schema.properties.source_lineage_status.const, "candidate_unverified");
  assert.equal(schema.properties.derived_targets.minItems, 13);
  assert.equal(schema.properties.derived_targets.maxItems, 13);
  assert.equal(schema.allOf[0].if.properties.verification_status.const, "candidate_unverified");
  assert.equal(schema.allOf[0].then.properties.live_inspection.type, "null");
  assert.equal(schema.allOf[0].else.properties.live_inspection.$ref, "#/$defs/liveInspection");
  assert.equal(hasKey(schema, "asset_count"), false);
});

test("source binding drift, object-path claims, and upgraded evidence stages fail closed", () => {
  const bindingDrift = clone(candidates);
  bindingDrift.source_binding.archive_sha256 = "f".repeat(64);
  expectProfileCode(
    () => validateCandidateSource(bindingDrift, candidateSha256, contract),
    "ANIMATION_MMG040_SOURCE_BINDING_MISMATCH",
  );

  const objectPath = clone(candidates);
  objectPath.animation_source_candidates[0].filesystem_locator = "/Game/Human_Avatar/BP_Human_Base";
  expectProfileCode(
    () => validateCandidateSource(objectPath, candidateSha256, contract),
    "ANIMATION_MMG040_CANDIDATE_SOURCE_INVALID",
  );

  const upgradedStage = clone(candidates);
  upgradedStage.filesystem_inventory_observation.evidence_stage = "asset_registry_verified";
  expectProfileCode(
    () => validateCandidateSource(upgradedStage, candidateSha256, contract),
    "ANIMATION_MMG040_SOURCE_BINDING_MISMATCH",
  );
});

test("static assets.json files are exclusions and can never be supplied as proof", () => {
  const enabledCatalog = clone(candidates);
  enabledCatalog.excluded_unbound_catalog_claims[0].allowed_as_evidence = true;
  expectProfileCode(
    () => validateCandidateSource(enabledCatalog, candidateSha256, contract),
    "ANIMATION_MMG040_STATIC_CATALOG_PROOF_FORBIDDEN",
  );

  const catalogLocator = clone(candidates);
  catalogLocator.scene_object_candidates[0].filesystem_locator =
    "simworld_studio_workspace/web/server/assets.json";
  expectProfileCode(
    () => validateCandidateSource(catalogLocator, candidateSha256, contract),
    "ANIMATION_MMG040_CANDIDATE_SOURCE_INVALID",
  );

  for (const excluded of candidates.excluded_unbound_catalog_claims) {
    assert.equal(excluded.allowed_as_evidence, false);
    assert.equal(excluded.reason, "static_catalog_not_bound_to_archive_revision");
  }
});

test("live mode accepts only the existing exact receipt validator and still does not claim runtime readiness", () => {
  const receipt = makeReceipt();
  assert.equal(validateInspectionReceipt(contract, contractSha256, receipt), receipt);
  const receiptSha256 = sha256Bytes(Buffer.from(`${JSON.stringify(receipt, null, 2)}\n`));
  const profile = buildInspectionProfile(contract, candidates, candidateSha256, receipt, receiptSha256);
  assert.equal(profile.verification_status, "live_verified");
  assert.equal(profile.start_allowed, false);
  assert.equal(profile.runtime_ready, false);
  assert.equal(profile.source_lineage_status, "candidate_unverified");
  assert.equal(profile.live_inspection.receipt_sha256, receiptSha256);
  assert.equal(profile.live_inspection.engine_version, "5.3.2");
  assert.equal(profile.live_inspection.content_revision, receipt.content_revision);
  assert.equal(profile.live_inspection.content_digest, receipt.content_digest);
  assert.equal(profile.live_inspection.verified_target_total, 13);
  assert.equal(profile.live_inspection.verified_action_total, 7);
  assert.ok(profile.derived_targets.every((entry) =>
    entry.verification_status === "live_verified" &&
    entry.content_status === "live_inspection_verified" &&
    entry.source_lineage_status === "candidate_unverified" &&
    entry.live_receipt_evidence.loaded === true));
  assert.ok(profile.animation_source_candidates.every((entry) => entry.candidate_status === "candidate_unverified"));
  assert.equal(profile.proof_boundary.runtime_capability_challenge_verified, false);
  assert.equal(profile.proof_boundary.executable_profile_emitted, false);
  assert.equal(hasKey(profile, "asset_count"), false);

  const wrongEngine = makeReceipt();
  wrongEngine.project.engine_version = "5.7.3";
  assert.throws(
    () => buildInspectionProfile(contract, candidates, candidateSha256, wrongEngine, receiptSha256),
    (error) => error instanceof ContentProfileContractError && error.code === "ANIMATION_MMG040_RECEIPT_MISMATCH",
  );
  const unverified = makeReceipt();
  unverified.verification.status = "candidate";
  assert.throws(
    () => buildInspectionProfile(contract, candidates, candidateSha256, unverified, receiptSha256),
    (error) => error instanceof ContentProfileContractError && error.code === "ANIMATION_MMG040_RECEIPT_UNVERIFIED",
  );
});

test("CLI derives mode from receipt presence and rejects caller-authored status or catalog inputs", (t) => {
  const candidateResult = spawnSync(process.execPath, [
    scriptPath,
    "--contract", contractPath,
    "--candidates", candidatesPath,
  ], { encoding: "utf8" });
  assert.equal(candidateResult.status, 0, candidateResult.stderr);
  const candidateProfile = JSON.parse(candidateResult.stdout);
  assert.equal(candidateProfile.verification_status, "candidate_unverified");
  assert.equal(candidateProfile.start_allowed, false);
  assert.equal(candidateProfile.runtime_ready, false);

  for (const [flag, value] of [
    ["--status", "live_verified"],
    ["--catalog", path.join(repositoryRoot, "simworld_studio_workspace/web/server/assets.json")],
  ]) {
    const rejected = spawnSync(process.execPath, [
      scriptPath,
      "--contract", contractPath,
      "--candidates", candidatesPath,
      flag, value,
    ], { encoding: "utf8" });
    assert.equal(rejected.status, 2);
    assert.match(rejected.stderr, /ANIMATION_MMG040_ARGUMENTS_INVALID/);
  }

  const temporary = mkdtempSync(path.join(os.homedir(), ".mmg040-inspection-test-"));
  t.after(() => rmSync(temporary, { recursive: true, force: true }));
  chmodSync(temporary, 0o700);
  const receipt = makeReceipt();
  const receiptPath = path.join(temporary, "receipt.json");
  writeFileSync(receiptPath, `${JSON.stringify(receipt, null, 2)}\n`, { mode: 0o600 });
  const liveResult = spawnSync(process.execPath, [
    scriptPath,
    "--contract", contractPath,
    "--candidates", candidatesPath,
    "--receipt", receiptPath,
  ], { encoding: "utf8" });
  assert.equal(liveResult.status, 0, liveResult.stderr);
  const liveProfile = JSON.parse(liveResult.stdout);
  assert.equal(liveProfile.verification_status, "live_verified");
  assert.equal(liveProfile.start_allowed, false);
  assert.equal(liveProfile.runtime_ready, false);
  assert.equal(liveProfile.source_lineage_status, "candidate_unverified");
  assert.equal(liveProfile.live_inspection.receipt_sha256, sha256Bytes(readFileSync(receiptPath)));
});

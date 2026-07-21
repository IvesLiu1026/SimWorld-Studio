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
import * as inspectionProfileModule from "../build_vista_mmg040_inspection_profile.mjs";

const {
  CANDIDATE_SOURCE_SET_ID,
  INSPECTION_PROFILE_SCHEMA,
  Mmg040InspectionProfileError,
  PINNED_CANDIDATE_SOURCE_SHA256,
  buildInspectionProfile,
  loadInspectionProfileBasis,
  loadInspectionReceiptBasis,
  validateInspectionProfile,
} = inspectionProfileModule;

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
const candidates = parseStrictJson(candidateBytes);
const candidateBasis = loadInspectionProfileBasis({ contractPath, candidatesPath });

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

function protectedDirectory(t) {
  const temporary = mkdtempSync(path.join(os.homedir(), ".mmg040-inspection-test-"));
  t.after(() => rmSync(temporary, { recursive: true, force: true }));
  chmodSync(temporary, 0o700);
  return temporary;
}

function writeProtectedJson(directory, name, value) {
  const target = path.join(directory, name);
  writeFileSync(target, `${JSON.stringify(value, null, 2)}\n`, { mode: 0o600 });
  chmodSync(target, 0o600);
  return target;
}

test("candidate source is byte-pinned to the official archive and filename-only evidence", () => {
  assert.equal(candidateSha256, PINNED_CANDIDATE_SOURCE_SHA256);
  assert.equal(candidates.source_set_id, CANDIDATE_SOURCE_SET_ID);
  assert.equal(candidates.source_binding.archive_sha256,
    "806e869ad1c65b298f05a39854b28e4188bb50817f539744451849e054990e2f");
  assert.equal(candidates.source_binding.project_revision,
    "source-patch:51426e97354477dca1635217455e644e9ca98976");
  assert.equal(candidates.filesystem_inventory_observation.package_file_count, 2937);
  assert.equal(candidates.filesystem_inventory_observation.count_semantics,
    "uasset_plus_umap_not_catalog_count");
  assert.equal(candidates.filesystem_inventory_observation.asset_registry_bundle, null);
  assert.equal(candidates.filesystem_inventory_observation.audit_document.sha256,
    "9769049b9aaddccfbcf40453f65e42ea44fdf05b7c599a27011774d5394f2c59");
  assert.equal(hasKey(candidates, "asset_count"), false);
  assert.ok(candidates.scene_object_candidates.every((entry) => entry.possible_target_asset_ids.length === 0));
  const sceneCandidates = new Map(candidates.scene_object_candidates
    .map((entry) => [entry.candidate_id, entry]));
  assert.equal(sceneCandidates.get("scene_sm_seat_table_01a").filesystem_locator,
    "Camping_Pack/Props/Seat_Table_01/Meshes/SM_SeatTable_01a.uasset");
  assert.ok(sceneCandidates.get("scene_sm_seat_table_01a").possible_roles.includes("stable_step_stool"));
  assert.equal(sceneCandidates.get("scene_sm_industrial_static_cart_1").filesystem_locator,
    "Industrial_Carts/Meshes/SM_Industrial_Carts_Static_Carts_1.uasset");
  assert.equal(sceneCandidates.get("scene_sm_industrial_service_cart_8").filesystem_locator,
    "Industrial_Carts/Meshes/SM_Industrial_Carts_Service_Carts_8.uasset");
  for (const candidateId of [
    "scene_sm_seat_table_01a",
    "scene_sm_industrial_static_cart_1",
    "scene_sm_industrial_service_cart_8",
  ]) {
    assert.ok(sceneCandidates.get(candidateId).unresolved_checks.includes("visual_role_match"));
  }
  assert.ok(candidates.animation_source_candidates.every((entry) => !entry.filesystem_locator.startsWith("/Game/")));
  assert.ok(candidates.animation_source_candidates.every((entry) =>
    entry.locator_kind === "content_relative_prefix" ? entry.filesystem_locator.endsWith("/") : entry.filesystem_locator.endsWith(".uasset")));
  assert.equal(inspectionProfileModule.validateCandidateSource, undefined);
  assert.equal(inspectionProfileModule.validateInspectionProfileShape, undefined);
  expectProfileCode(
    () => buildInspectionProfile(contract, candidates, candidateSha256),
    "ANIMATION_MMG040_INSPECTION_BASIS_INVALID",
  );
});

test("candidate mode derives all statuses and remains blocked without a live receipt", () => {
  const profile = buildInspectionProfile(candidateBasis);
  assert.equal(validateInspectionProfile(profile, candidateBasis), profile);
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

test("opaque basis cannot be copied, property-mutated, or replaced with parsed caller data", () => {
  assert.equal(Object.isFrozen(candidateBasis), true);
  assert.equal(Object.getPrototypeOf(candidateBasis), null);
  assert.deepEqual(Object.keys(candidateBasis), []);
  assert.throws(() => {
    candidateBasis.contract = contract;
  }, TypeError);
  expectProfileCode(
    () => buildInspectionProfile({ ...candidateBasis }),
    "ANIMATION_MMG040_INSPECTION_BASIS_INVALID",
  );

  const callerContract = clone(contract);
  const callerCandidates = clone(candidates);
  callerContract.assets[0].object_path = "/Game/Caller/Injected.Injected_C";
  callerCandidates.source_binding.archive_sha256 = "0".repeat(64);
  const profile = buildInspectionProfile(candidateBasis);
  assert.equal(profile.derived_targets[0].target_object_path, contract.assets[0].object_path);
  assert.equal(profile.source_binding.archive_sha256, candidates.source_binding.archive_sha256);
  expectProfileCode(
    () => validateInspectionProfile(profile, contract),
    "ANIMATION_MMG040_INSPECTION_BASIS_INVALID",
  );
});

test("schema fixes digests, both candidate/live branches, locator kinds, and receipt authority", () => {
  const schema = JSON.parse(readFileSync(schemaPath, "utf8"));
  assert.equal(schema.properties.schema.const, INSPECTION_PROFILE_SCHEMA);
  assert.match(schema.$comment, /schema.*not.*proof|not.*proof.*schema/i);
  assert.equal(schema.properties.source_contract_sha256.const, contractSha256);
  assert.equal(schema.properties.candidate_source_sha256.const, candidateSha256);
  assert.equal(schema.properties.excluded_unbound_catalog_claims.uniqueItems, true);
  assert.equal(schema.properties.start_allowed.const, false);
  assert.equal(schema.properties.runtime_ready.const, false);
  assert.equal(schema.properties.source_lineage_status.const, "candidate_unverified");
  assert.equal(schema.properties.derived_targets.minItems, 13);
  assert.equal(schema.properties.derived_targets.maxItems, 13);
  assert.equal(schema.allOf[0].if.properties.verification_status.const, "candidate_unverified");
  assert.equal(schema.allOf[0].then.properties.live_inspection.type, "null");
  assert.equal(schema.allOf[1].if.properties.verification_status.const, "live_verified");
  assert.equal(schema.allOf[1].then.properties.live_inspection.$ref, "#/$defs/liveInspection");
  assert.equal(schema.$defs.candidate.allOf.length, 2);
  assert.equal(schema.$defs.candidate.allOf[0].then.properties.filesystem_locator.pattern, "\\.uasset$");
  assert.equal(schema.$defs.candidate.allOf[1].then.properties.filesystem_locator.pattern, "/$");
  assert.deepEqual(schema.$defs.candidate.properties.filesystem_locator.not.anyOf[0].enum, [
    "simworld_studio_workspace/web/server/assets.json",
    "packaging/simworld_arena/server/assets.json",
  ]);
  assert.equal(schema.$defs.candidate.properties.filesystem_locator.not.anyOf[1].pattern, "^Game/");
  assert.equal(schema.$defs.candidate.properties.filesystem_locator.not.anyOf[2].pattern, "\\.\\.");
  assert.equal(schema.$defs.liveInspection.properties.verified_at.pattern,
    "^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(?:\\.\\d{3})?Z$");
  assert.match(schema.$defs.liveInspection.$comment, /receipt verifier|sealed.*basis/i);
  assert.equal(hasKey(schema, "asset_count"), false);
});

test("source binding drift, object-path claims, and upgraded evidence stages fail closed", () => {
  const bindingDrift = buildInspectionProfile(candidateBasis);
  bindingDrift.source_binding.archive_sha256 = "f".repeat(64);
  expectProfileCode(
    () => validateInspectionProfile(bindingDrift, candidateBasis),
    "ANIMATION_MMG040_SOURCE_BINDING_MISMATCH",
  );

  const objectPath = buildInspectionProfile(candidateBasis);
  objectPath.animation_source_candidates[0].filesystem_locator = "/Game/Human_Avatar/BP_Human_Base";
  expectProfileCode(
    () => validateInspectionProfile(objectPath, candidateBasis),
    "ANIMATION_MMG040_CANDIDATE_SOURCE_INVALID",
  );

  for (const locator of ["Game/Foo.uasset", "foo/../bar.uasset"]) {
    const invalidLocator = buildInspectionProfile(candidateBasis);
    invalidLocator.animation_source_candidates[0].filesystem_locator = locator;
    expectProfileCode(
      () => validateInspectionProfile(invalidLocator, candidateBasis),
      "ANIMATION_MMG040_CANDIDATE_SOURCE_INVALID",
    );
  }

  const upgradedStage = buildInspectionProfile(candidateBasis);
  upgradedStage.filesystem_inventory_observation.evidence_stage = "asset_registry_verified";
  expectProfileCode(
    () => validateInspectionProfile(upgradedStage, candidateBasis),
    "ANIMATION_MMG040_SOURCE_BINDING_MISMATCH",
  );
});

test("static assets.json files are exclusions and can never be supplied as proof", () => {
  const enabledCatalog = buildInspectionProfile(candidateBasis);
  enabledCatalog.excluded_unbound_catalog_claims[0].allowed_as_evidence = true;
  expectProfileCode(
    () => validateInspectionProfile(enabledCatalog, candidateBasis),
    "ANIMATION_MMG040_STATIC_CATALOG_PROOF_FORBIDDEN",
  );

  const catalogLocator = buildInspectionProfile(candidateBasis);
  catalogLocator.scene_object_candidates[0].filesystem_locator =
    "simworld_studio_workspace/web/server/assets.json";
  expectProfileCode(
    () => validateInspectionProfile(catalogLocator, candidateBasis),
    "ANIMATION_MMG040_CANDIDATE_SOURCE_INVALID",
  );

  for (const excluded of candidates.excluded_unbound_catalog_claims) {
    assert.equal(excluded.allowed_as_evidence, false);
    assert.equal(excluded.reason, "static_catalog_not_bound_to_archive_revision");
  }
});

test("live mode requires sealed exact receipt bytes and still does not claim runtime readiness", (t) => {
  const temporary = protectedDirectory(t);
  const receipt = makeReceipt();
  assert.equal(validateInspectionReceipt(contract, contractSha256, receipt), receipt);
  const receiptPath = writeProtectedJson(temporary, "receipt.json", receipt);
  const receiptSha256 = sha256Bytes(readFileSync(receiptPath));
  const liveBasis = loadInspectionReceiptBasis(candidateBasis, { receiptPath });

  receipt.verification.verified_at = "caller-mutated-after-seal";
  receipt.assets[0].package_sha256 = "f".repeat(64);
  const profile = buildInspectionProfile(liveBasis);
  assert.equal(validateInspectionProfile(profile, liveBasis), profile);
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
  const wrongEnginePath = writeProtectedJson(temporary, "wrong-engine.json", wrongEngine);
  assert.throws(
    () => loadInspectionReceiptBasis(candidateBasis, { receiptPath: wrongEnginePath }),
    (error) => error instanceof ContentProfileContractError && error.code === "ANIMATION_MMG040_RECEIPT_MISMATCH",
  );
  const unverified = makeReceipt();
  unverified.verification.status = "candidate";
  const unverifiedPath = writeProtectedJson(temporary, "unverified.json", unverified);
  assert.throws(
    () => loadInspectionReceiptBasis(candidateBasis, { receiptPath: unverifiedPath }),
    (error) => error instanceof ContentProfileContractError && error.code === "ANIMATION_MMG040_RECEIPT_UNVERIFIED",
  );
});

test("authoritative validation rejects a forged live upgrade and a caller-supplied receipt digest", (t) => {
  const temporary = protectedDirectory(t);
  const candidateProfile = buildInspectionProfile(candidateBasis);
  const receipt = makeReceipt();
  const receiptPath = writeProtectedJson(temporary, "receipt.json", receipt);
  const liveBasis = loadInspectionReceiptBasis(candidateBasis, { receiptPath });
  const liveProfile = buildInspectionProfile(liveBasis);

  expectProfileCode(
    () => validateInspectionProfile(liveProfile, candidateBasis),
    "ANIMATION_MMG040_LIVE_BASIS_REQUIRED",
  );

  const forgedDigest = clone(liveProfile);
  forgedDigest.live_inspection.receipt_sha256 = "a".repeat(64);
  expectProfileCode(
    () => validateInspectionProfile(forgedDigest, liveBasis),
    "ANIMATION_MMG040_RECEIPT_HASH_MISMATCH",
  );

  const malformedTimestamp = clone(liveProfile);
  malformedTimestamp.live_inspection.verified_at = "2026-07-21 08:00:00";
  expectProfileCode(
    () => validateInspectionProfile(malformedTimestamp, liveBasis),
    "ANIMATION_MMG040_INSPECTION_PROFILE_INVALID",
  );

  const impossibleTimestamp = makeReceipt();
  impossibleTimestamp.verification.verified_at = "2026-02-31T08:00:00Z";
  const impossibleTimestampPath = writeProtectedJson(temporary, "impossible-timestamp.json", impossibleTimestamp);
  expectProfileCode(
    () => loadInspectionReceiptBasis(candidateBasis, { receiptPath: impossibleTimestampPath }),
    "ANIMATION_MMG040_INSPECTION_PROFILE_INVALID",
  );

  candidateProfile.verification_status = "live_verified";
  expectProfileCode(
    () => validateInspectionProfile(candidateProfile, candidateBasis),
    "ANIMATION_MMG040_LIVE_BASIS_REQUIRED",
  );
});

test("secure candidate loader rejects changed bytes instead of trusting a caller digest", (t) => {
  const temporary = protectedDirectory(t);
  const changedCandidates = clone(candidates);
  changedCandidates.source_binding.archive_sha256 = "f".repeat(64);
  const changedPath = writeProtectedJson(temporary, "changed-candidates.json", changedCandidates);
  assert.throws(
    () => loadInspectionProfileBasis({ contractPath, candidatesPath: changedPath }),
    (error) => error instanceof Mmg040InspectionProfileError &&
      error.code === "ANIMATION_MMG040_CANDIDATE_SOURCE_MISMATCH",
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

  const temporary = protectedDirectory(t);
  const receipt = makeReceipt();
  const receiptPath = writeProtectedJson(temporary, "receipt.json", receipt);
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

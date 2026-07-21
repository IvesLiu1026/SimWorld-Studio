#!/usr/bin/env node

import path from "node:path";
import { pathToFileURL } from "node:url";

import {
  canonicalize,
  ContentProfileContractError,
  PINNED_SOURCE_CONTRACT_SHA256,
  parseStrictJson,
  readSecureJson,
  sha256Bytes,
  validateInspectionReceipt,
  validateSourceContract,
} from "../unreal_plugins/VistaAnimationContentApi/Scripts/prepare-content-profile.mjs";

export const INSPECTION_PROFILE_SCHEMA = "vista-mmg040-inspection-profile/v1";
export const CANDIDATE_SOURCE_SCHEMA = "vista-mmg040-candidate-sources/v1";
export const PINNED_CANDIDATE_SOURCE_SHA256 = "fc03861ddb92f71e23efa6794f98145455b22fdfef52e96b41fe11a299bf7c95";

const SAFE_ID = /^[a-z][a-z0-9_]{0,119}$/;
const SAFE_CHECK = /^[a-z][a-z0-9_]{0,119}$/;
const SHA256 = /^[a-f0-9]{64}$/;
const ISO_UTC = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$/;
const CONTENT_RELATIVE = /^[A-Za-z0-9_+.-]+(?:\/[A-Za-z0-9_+.-]+)*\/?$/;
const PROFILE_ID = "vista_mmg040";
const PROFILE_REVISION = "mmg040_project_content_r1";
const SAMPLE_ID = "mmg_040";
const SOURCE_LINEAGE_STATUS = "candidate_unverified";

const OFFICIAL_SOURCE_BINDING = Object.freeze({
  repository: "SimWorld-AI/SimWorld-Studio",
  dataset_revision: "26bdd2ca18f06ab455023b0a602ede60b3afb243",
  archive_name: "SimWorld-Studio-Minimal.tar.gz",
  archive_size_bytes: 15170703068,
  archive_sha256: "806e869ad1c65b298f05a39854b28e4188bb50817f539744451849e054990e2f",
  project_name: "gym_citynav",
  project_patch: "51426e97354477dca1635217455e644e9ca98976",
  project_revision: "source-patch:51426e97354477dca1635217455e644e9ca98976",
  content_revision: "sha256:806e869ad1c65b298f05a39854b28e4188bb50817f539744451849e054990e2f",
});

const OFFICIAL_FILESYSTEM_OBSERVATION = Object.freeze({
  evidence_stage: "filesystem_filename_only",
  package_file_count: 2937,
  count_semantics: "uasset_plus_umap_not_catalog_count",
  audit_document: Object.freeze({
    path: "docs/specs/production-readiness/evidence/2026-07-21-official-minimal-content-audit.md",
    sha256: "e591be51470c2e4a4c390916f3cead810409daedbdeae84e7e28ffc5dac7015f",
  }),
  asset_registry_bundle: null,
});

const EXCLUDED_STATIC_CATALOGS = Object.freeze([
  "simworld_studio_workspace/web/server/assets.json",
  "packaging/simworld_arena/server/assets.json",
]);

const PROOF_BOUNDARY = Object.freeze({
  filesystem_only_candidates_not_registry_proof: true,
  static_catalogs_excluded: true,
  semantic_snapshot_verified: false,
  runtime_capability_challenge_verified: false,
  executable_profile_emitted: false,
});

// An inspection basis is intentionally property-free. Authoritative builders
// recover protected raw bytes only through this module-owned WeakMap, recompute
// every digest, and run the exact source/receipt validators again immediately
// before producing or authorizing an output profile.
const INSPECTION_BASIS_STATES = new WeakMap();

export class Mmg040InspectionProfileError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "Mmg040InspectionProfileError";
    this.code = code;
  }
}

function fail(code, message) {
  throw new Mmg040InspectionProfileError(code, message);
}

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function exactKeys(value, keys, label) {
  if (!isObject(value)) fail("ANIMATION_MMG040_CANDIDATE_SOURCE_INVALID", `${label} must be an object`);
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length || expected.some((key, index) => key !== actual[index])) {
    fail("ANIMATION_MMG040_CANDIDATE_SOURCE_INVALID", `${label} has an invalid shape`);
  }
}

function requireString(value, pattern, label) {
  if (typeof value !== "string" || !pattern.test(value)) {
    fail("ANIMATION_MMG040_CANDIDATE_SOURCE_INVALID", `${label} is invalid`);
  }
  return value;
}

function isExactUtcTimestamp(value) {
  if (typeof value !== "string" || !ISO_UTC.test(value)) return false;
  const milliseconds = Date.parse(value);
  if (Number.isNaN(milliseconds)) return false;
  const normalized = new Date(milliseconds).toISOString();
  return value.includes(".") ? normalized === value : normalized.replace(".000Z", "Z") === value;
}

function validateExactReceipt(contract, contractSha256, receipt) {
  const validated = validateInspectionReceipt(contract, contractSha256, receipt);
  if (!isExactUtcTimestamp(validated.verification.verified_at)) {
    fail("ANIMATION_MMG040_INSPECTION_PROFILE_INVALID", "receipt verified_at must be an exact valid UTC timestamp");
  }
  return validated;
}

function requireUniqueStrings(value, pattern, label, { allowEmpty = false } = {}) {
  if (!Array.isArray(value) || (!allowEmpty && value.length === 0) ||
      value.some((item) => typeof item !== "string" || !pattern.test(item)) ||
      new Set(value).size !== value.length) {
    fail("ANIMATION_MMG040_CANDIDATE_SOURCE_INVALID", `${label} must contain unique valid strings`);
  }
  return [...value];
}

function sameScalarObject(actual, expected, label) {
  exactKeys(actual, Object.keys(expected), label);
  for (const [key, value] of Object.entries(expected)) {
    if (actual[key] !== value) {
      fail("ANIMATION_MMG040_SOURCE_BINDING_MISMATCH", `${label}.${key} does not match the official source`);
    }
  }
}

function rejectAssetCountField(value, label = "document") {
  if (Array.isArray(value)) {
    value.forEach((item, index) => rejectAssetCountField(item, `${label}[${index}]`));
    return;
  }
  if (!isObject(value)) return;
  for (const [key, item] of Object.entries(value)) {
    if (key === "asset_count") {
      fail("ANIMATION_MMG040_CANDIDATE_SOURCE_INVALID", `${label} must not call the filesystem observation asset_count`);
    }
    rejectAssetCountField(item, `${label}.${key}`);
  }
}

function validateFilesystemObservation(value) {
  exactKeys(value, [
    "evidence_stage",
    "package_file_count",
    "count_semantics",
    "audit_document",
    "asset_registry_bundle",
  ], "filesystem_inventory_observation");
  if (value.evidence_stage !== OFFICIAL_FILESYSTEM_OBSERVATION.evidence_stage ||
      value.package_file_count !== OFFICIAL_FILESYSTEM_OBSERVATION.package_file_count ||
      value.count_semantics !== OFFICIAL_FILESYSTEM_OBSERVATION.count_semantics ||
      value.asset_registry_bundle !== null) {
    fail("ANIMATION_MMG040_SOURCE_BINDING_MISMATCH", "filesystem observation is not the pinned filename-only audit");
  }
  sameScalarObject(value.audit_document, OFFICIAL_FILESYSTEM_OBSERVATION.audit_document, "audit_document");
}

function validateExcludedCatalogs(value) {
  if (!Array.isArray(value) || value.length !== EXCLUDED_STATIC_CATALOGS.length) {
    fail("ANIMATION_MMG040_CANDIDATE_SOURCE_INVALID", "both unbound static catalogs must be excluded");
  }
  const paths = new Set();
  for (const [index, entry] of value.entries()) {
    exactKeys(entry, ["path", "reason", "allowed_as_evidence"], `excluded_unbound_catalog_claims[${index}]`);
    if (!EXCLUDED_STATIC_CATALOGS.includes(entry.path) || paths.has(entry.path) ||
        entry.reason !== "static_catalog_not_bound_to_archive_revision" || entry.allowed_as_evidence !== false) {
      fail("ANIMATION_MMG040_STATIC_CATALOG_PROOF_FORBIDDEN", "static assets.json files cannot be candidate evidence");
    }
    paths.add(entry.path);
  }
  if (paths.size !== EXCLUDED_STATIC_CATALOGS.length) {
    fail("ANIMATION_MMG040_STATIC_CATALOG_PROOF_FORBIDDEN", "static catalog exclusion set is incomplete");
  }
}

function validateCandidate(candidate, label, contractAssetIds) {
  exactKeys(candidate, [
    "candidate_id",
    "source_scope_id",
    "locator_kind",
    "filesystem_locator",
    "possible_roles",
    "possible_target_asset_ids",
    "unresolved_checks",
  ], label);
  requireString(candidate.candidate_id, SAFE_ID, `${label}.candidate_id`);
  if (candidate.source_scope_id !== "official_minimal_content") {
    fail("ANIMATION_MMG040_SOURCE_BINDING_MISMATCH", `${label}.source_scope_id is invalid`);
  }
  if (!["content_relative_file", "content_relative_prefix"].includes(candidate.locator_kind)) {
    fail("ANIMATION_MMG040_CANDIDATE_SOURCE_INVALID", `${label}.locator_kind is invalid`);
  }
  requireString(candidate.filesystem_locator, CONTENT_RELATIVE, `${label}.filesystem_locator`);
  if (candidate.filesystem_locator.startsWith("/") || candidate.filesystem_locator.includes("..") ||
      candidate.filesystem_locator.includes("//") || candidate.filesystem_locator.includes("\\") ||
      candidate.filesystem_locator.startsWith("Game/") || candidate.filesystem_locator.includes("://")) {
    fail("ANIMATION_MMG040_CANDIDATE_SOURCE_INVALID", `${label}.filesystem_locator must remain Content-relative`);
  }
  if (candidate.locator_kind === "content_relative_file" && !candidate.filesystem_locator.endsWith(".uasset")) {
    fail("ANIMATION_MMG040_CANDIDATE_SOURCE_INVALID", `${label} file locator must end in .uasset`);
  }
  if (candidate.locator_kind === "content_relative_prefix" && !candidate.filesystem_locator.endsWith("/")) {
    fail("ANIMATION_MMG040_CANDIDATE_SOURCE_INVALID", `${label} prefix locator must end in /`);
  }
  requireUniqueStrings(candidate.possible_roles, SAFE_CHECK, `${label}.possible_roles`);
  const targetIds = requireUniqueStrings(
    candidate.possible_target_asset_ids,
    SAFE_ID,
    `${label}.possible_target_asset_ids`,
    { allowEmpty: true },
  );
  if (targetIds.some((assetId) => !contractAssetIds.has(assetId))) {
    fail("ANIMATION_MMG040_CANDIDATE_SOURCE_INVALID", `${label} references an unknown derived target`);
  }
  requireUniqueStrings(candidate.unresolved_checks, SAFE_CHECK, `${label}.unresolved_checks`);
  return candidate;
}

function validateCandidateSourceDocument(source, rawSha256, contract) {
  rejectAssetCountField(source);
  exactKeys(source, [
    "schema",
    "source_set_id",
    "profile_id",
    "profile_revision",
    "sample_id",
    "source_binding",
    "filesystem_inventory_observation",
    "excluded_unbound_catalog_claims",
    "scene_object_candidates",
    "animation_source_candidates",
    "target_source_links",
  ], "candidate source");
  if (rawSha256 !== PINNED_CANDIDATE_SOURCE_SHA256) {
    fail("ANIMATION_MMG040_CANDIDATE_SOURCE_MISMATCH", "candidate source bytes do not match the compiled pin");
  }
  if (source.schema !== CANDIDATE_SOURCE_SCHEMA || source.source_set_id !== "official-minimal-gym-citynav-filename-audit-r1" ||
      source.profile_id !== PROFILE_ID || source.profile_revision !== PROFILE_REVISION || source.sample_id !== SAMPLE_ID) {
    fail("ANIMATION_MMG040_CANDIDATE_SOURCE_MISMATCH", "candidate source identity is invalid");
  }
  sameScalarObject(source.source_binding, OFFICIAL_SOURCE_BINDING, "source_binding");
  if (contract.profile_id !== source.profile_id || contract.profile_revision !== source.profile_revision ||
      contract.sample_id !== source.sample_id || contract.target.project_name !== source.source_binding.project_name ||
      contract.source_provenance.repository !== source.source_binding.repository ||
      contract.source_provenance.dataset_revision !== source.source_binding.dataset_revision ||
      contract.source_provenance.archive_name !== source.source_binding.archive_name ||
      contract.source_provenance.archive_sha256 !== source.source_binding.archive_sha256 ||
      contract.source_provenance.project_patch !== source.source_binding.project_patch) {
    fail("ANIMATION_MMG040_SOURCE_BINDING_MISMATCH", "candidate source does not match the pinned project profile contract");
  }
  validateFilesystemObservation(source.filesystem_inventory_observation);
  validateExcludedCatalogs(source.excluded_unbound_catalog_claims);

  if (!Array.isArray(source.scene_object_candidates) || source.scene_object_candidates.length === 0 ||
      !Array.isArray(source.animation_source_candidates) || source.animation_source_candidates.length === 0) {
    fail("ANIMATION_MMG040_CANDIDATE_SOURCE_INVALID", "scene and animation candidate lists are required");
  }
  const contractAssetIds = new Set(contract.assets.map((asset) => asset.asset_id));
  const candidateIds = new Set();
  const animationCandidateIds = new Set();
  for (const [groupName, candidates] of [
    ["scene_object_candidates", source.scene_object_candidates],
    ["animation_source_candidates", source.animation_source_candidates],
  ]) {
    for (const [index, candidate] of candidates.entries()) {
      validateCandidate(candidate, `${groupName}[${index}]`, contractAssetIds);
      if (candidateIds.has(candidate.candidate_id)) {
        fail("ANIMATION_MMG040_CANDIDATE_SOURCE_INVALID", "candidate IDs must be globally unique");
      }
      candidateIds.add(candidate.candidate_id);
      if (groupName === "scene_object_candidates") {
        if (candidate.possible_target_asset_ids.length !== 0) {
          fail("ANIMATION_MMG040_CANDIDATE_SOURCE_INVALID", "scene candidates cannot stand in for project-owned animation targets");
        }
      } else {
        animationCandidateIds.add(candidate.candidate_id);
      }
    }
  }

  if (!Array.isArray(source.target_source_links) || source.target_source_links.length !== contract.assets.length) {
    fail("ANIMATION_MMG040_CANDIDATE_SOURCE_INVALID", "target_source_links must cover exactly thirteen contract targets");
  }
  const links = new Map();
  for (const [index, link] of source.target_source_links.entries()) {
    exactKeys(link, ["target_asset_id", "source_candidate_ids", "authoring_required", "required_live_checks"], `target_source_links[${index}]`);
    if (link.target_asset_id !== contract.assets[index].asset_id || links.has(link.target_asset_id) ||
        link.authoring_required !== true) {
      fail("ANIMATION_MMG040_CANDIDATE_SOURCE_INVALID", "target source links must preserve the exact contract target order");
    }
    const sourceIds = requireUniqueStrings(link.source_candidate_ids, SAFE_ID, `target_source_links[${index}].source_candidate_ids`, { allowEmpty: true });
    if (sourceIds.some((candidateId) => !animationCandidateIds.has(candidateId))) {
      fail("ANIMATION_MMG040_CANDIDATE_SOURCE_INVALID", "target source links may reference only animation candidates");
    }
    requireUniqueStrings(link.required_live_checks, SAFE_CHECK, `target_source_links[${index}].required_live_checks`);
    links.set(link.target_asset_id, link);
  }
  for (const candidate of source.animation_source_candidates) {
    for (const targetId of candidate.possible_target_asset_ids) {
      if (!links.get(targetId).source_candidate_ids.includes(candidate.candidate_id)) {
        fail("ANIMATION_MMG040_CANDIDATE_SOURCE_INVALID", "candidate-to-target links are not bidirectionally complete");
      }
    }
  }
  for (const link of links.values()) {
    for (const candidateId of link.source_candidate_ids) {
      const candidate = source.animation_source_candidates.find((entry) => entry.candidate_id === candidateId);
      if (!candidate.possible_target_asset_ids.includes(link.target_asset_id)) {
        fail("ANIMATION_MMG040_CANDIDATE_SOURCE_INVALID", "target-to-candidate links are not bidirectionally complete");
      }
    }
  }
  return source;
}

function outputCandidate(candidate) {
  return {
    ...candidate,
    candidate_status: "candidate_unverified",
  };
}

function exactBasisOptions(value, keys, label) {
  if (!isObject(value)) {
    fail("ANIMATION_MMG040_INSPECTION_BASIS_INVALID", `${label} must be an object`);
  }
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length || expected.some((key, index) => key !== actual[index])) {
    fail("ANIMATION_MMG040_INSPECTION_BASIS_INVALID", `${label} has an invalid shape`);
  }
  const copied = Object.create(null);
  for (const key of keys) {
    const pathValue = value[key];
    if (typeof pathValue !== "string" || pathValue.length === 0) {
      fail("ANIMATION_MMG040_INSPECTION_BASIS_INVALID", `${label}.${key} must be a path string`);
    }
    copied[key] = pathValue;
  }
  return copied;
}

function sealInspectionBasis({
  contractBytes,
  contractSha256,
  candidateBytes,
  candidateSha256,
  receiptBytes = null,
  receiptSha256 = null,
}) {
  const basis = Object.freeze(Object.create(null));
  INSPECTION_BASIS_STATES.set(basis, Object.freeze({
    contractBytes: Buffer.from(contractBytes),
    contractSha256,
    candidateBytes: Buffer.from(candidateBytes),
    candidateSha256,
    receiptBytes: receiptBytes === null ? null : Buffer.from(receiptBytes),
    receiptSha256,
  }));
  return basis;
}

function requireInspectionBasis(basis) {
  const state = INSPECTION_BASIS_STATES.get(basis);
  if (!state) {
    fail(
      "ANIMATION_MMG040_INSPECTION_BASIS_INVALID",
      "an opaque inspection basis created by this module is required",
    );
  }
  return state;
}

function materializeInspectionBasis(basis) {
  const state = requireInspectionBasis(basis);
  const contractBytes = Buffer.from(state.contractBytes);
  const candidateBytes = Buffer.from(state.candidateBytes);
  const contractSha256 = sha256Bytes(contractBytes);
  const candidateSha256 = sha256Bytes(candidateBytes);
  if (contractSha256 !== state.contractSha256 || contractSha256 !== PINNED_SOURCE_CONTRACT_SHA256) {
    fail("ANIMATION_MMG040_INSPECTION_BASIS_MISMATCH", "sealed source contract bytes no longer match their pin");
  }
  if (candidateSha256 !== state.candidateSha256 || candidateSha256 !== PINNED_CANDIDATE_SOURCE_SHA256) {
    fail("ANIMATION_MMG040_INSPECTION_BASIS_MISMATCH", "sealed candidate source bytes no longer match their pin");
  }

  const contract = validateSourceContract(parseStrictJson(contractBytes), contractSha256);
  const candidates = validateCandidateSourceDocument(
    parseStrictJson(candidateBytes),
    candidateSha256,
    contract,
  );
  let receipt = null;
  let receiptSha256 = null;
  if (state.receiptBytes !== null) {
    const receiptBytes = Buffer.from(state.receiptBytes);
    receiptSha256 = sha256Bytes(receiptBytes);
    if (receiptSha256 !== state.receiptSha256) {
      fail("ANIMATION_MMG040_INSPECTION_BASIS_MISMATCH", "sealed inspection receipt bytes no longer match their hash");
    }
    receipt = validateExactReceipt(
      contract,
      contractSha256,
      parseStrictJson(receiptBytes),
    );
  } else if (state.receiptSha256 !== null) {
    fail("ANIMATION_MMG040_INSPECTION_BASIS_MISMATCH", "sealed receipt hash exists without receipt bytes");
  }
  return { contract, candidates, candidateSha256, receipt, receiptSha256 };
}

export function loadInspectionProfileBasis(options) {
  if (arguments.length !== 1) {
    fail("ANIMATION_MMG040_INSPECTION_BASIS_INVALID", "profile basis loader accepts one options object");
  }
  const paths = exactBasisOptions(options, ["contractPath", "candidatesPath"], "profile basis options");
  const source = readSecureJson(paths.contractPath, "source contract");
  const contract = validateSourceContract(source.value, source.sha256);
  const candidateInput = readSecureJson(paths.candidatesPath, "candidate source");
  validateCandidateSourceDocument(candidateInput.value, candidateInput.sha256, contract);
  return sealInspectionBasis({
    contractBytes: source.bytes,
    contractSha256: source.sha256,
    candidateBytes: candidateInput.bytes,
    candidateSha256: candidateInput.sha256,
  });
}

export function loadInspectionReceiptBasis(candidateBasis, options) {
  if (arguments.length !== 2) {
    fail("ANIMATION_MMG040_INSPECTION_BASIS_INVALID", "receipt basis loader accepts a basis and one options object");
  }
  const paths = exactBasisOptions(options, ["receiptPath"], "receipt basis options");
  const state = requireInspectionBasis(candidateBasis);
  if (state.receiptBytes !== null) {
    fail("ANIMATION_MMG040_INSPECTION_BASIS_INVALID", "receipt basis must be derived from a candidate-only basis");
  }
  const materialized = materializeInspectionBasis(candidateBasis);
  const receiptInput = readSecureJson(paths.receiptPath, "inspection receipt");
  validateExactReceipt(
    materialized.contract,
    PINNED_SOURCE_CONTRACT_SHA256,
    receiptInput.value,
  );
  return sealInspectionBasis({
    contractBytes: state.contractBytes,
    contractSha256: state.contractSha256,
    candidateBytes: state.candidateBytes,
    candidateSha256: state.candidateSha256,
    receiptBytes: receiptInput.bytes,
    receiptSha256: receiptInput.sha256,
  });
}

function buildDerivedTargets(contract, candidates, receipt) {
  const links = new Map(candidates.target_source_links.map((entry) => [entry.target_asset_id, entry]));
  const liveAssets = receipt ? new Map(receipt.assets.map((asset) => [asset.asset_id, asset])) : null;
  return contract.assets.map((asset) => {
    const link = links.get(asset.asset_id);
    const liveAsset = liveAssets?.get(asset.asset_id) ?? null;
    return {
      asset_id: asset.asset_id,
      role: asset.role,
      target_object_path: asset.object_path,
      expected_class: asset.expected_class,
      source_candidate_ids: [...link.source_candidate_ids],
      authoring_required: true,
      required_live_checks: [...link.required_live_checks],
      content_status: receipt ? "live_inspection_verified" : "not_authored_or_unverified",
      verification_status: receipt ? "live_verified" : "candidate_unverified",
      source_lineage_status: SOURCE_LINEAGE_STATUS,
      live_receipt_evidence: liveAsset ? {
        package_sha256: liveAsset.package_sha256,
        loaded: liveAsset.loaded,
        observed_class: liveAsset.observed_class,
        observed_object_path: liveAsset.object_path,
      } : null,
    };
  });
}

function buildLiveInspection(receipt, receiptSha256) {
  if (!receipt) return null;
  return {
    schema: receipt.schema,
    receipt_sha256: receiptSha256,
    receipt_id: receipt.verification.receipt_id,
    verified_at: receipt.verification.verified_at,
    operator_id: receipt.verification.operator_id,
    method: receipt.verification.method,
    engine_version: receipt.project.engine_version,
    project_revision: receipt.project.project_revision,
    project_descriptor_sha256: receipt.project.project_descriptor_sha256,
    content_revision: receipt.content_revision,
    content_digest: receipt.content_digest,
    verified_target_total: receipt.assets.length,
    verified_action_total: receipt.actions.length,
  };
}

function createInspectionProfile({ contract, candidates, candidateSha256, receipt, receiptSha256 }) {
  const profile = {
    schema: INSPECTION_PROFILE_SCHEMA,
    profile_id: contract.profile_id,
    profile_revision: contract.profile_revision,
    sample_id: contract.sample_id,
    source_contract_sha256: PINNED_SOURCE_CONTRACT_SHA256,
    candidate_source_sha256: candidateSha256,
    verification_status: receipt ? "live_verified" : "candidate_unverified",
    start_allowed: false,
    runtime_ready: false,
    source_lineage_status: SOURCE_LINEAGE_STATUS,
    source_binding: { ...candidates.source_binding },
    filesystem_inventory_observation: {
      ...candidates.filesystem_inventory_observation,
      audit_document: { ...candidates.filesystem_inventory_observation.audit_document },
    },
    excluded_unbound_catalog_claims: candidates.excluded_unbound_catalog_claims.map((entry) => ({ ...entry })),
    scene_object_candidates: candidates.scene_object_candidates.map(outputCandidate),
    animation_source_candidates: candidates.animation_source_candidates.map(outputCandidate),
    derived_targets: buildDerivedTargets(contract, candidates, receipt),
    live_inspection: buildLiveInspection(receipt, receiptSha256),
    proof_boundary: { ...PROOF_BOUNDARY },
  };
  return profile;
}

function validateOutputCandidate(candidate, label, contractAssetIds) {
  exactKeys(candidate, [
    "candidate_id", "source_scope_id", "locator_kind", "filesystem_locator", "possible_roles",
    "possible_target_asset_ids", "unresolved_checks", "candidate_status",
  ], label);
  if (candidate.candidate_status !== "candidate_unverified") {
    fail("ANIMATION_MMG040_INSPECTION_PROFILE_INVALID", `${label} cannot claim verification`);
  }
  const sourceShape = { ...candidate };
  delete sourceShape.candidate_status;
  validateCandidate(sourceShape, label, contractAssetIds);
}

function validateLiveInspection(value) {
  exactKeys(value, [
    "schema", "receipt_sha256", "receipt_id", "verified_at", "operator_id", "method", "engine_version",
    "project_revision", "project_descriptor_sha256", "content_revision", "content_digest",
    "verified_target_total", "verified_action_total",
  ], "live_inspection");
  if (value.schema !== "vista-animation-content-inspection-receipt/v1" ||
      value.method !== "ue53_disposable_live_inspection_v1" || value.engine_version !== "5.3.2" ||
      value.verified_target_total !== 13 || value.verified_action_total !== 7) {
    fail("ANIMATION_MMG040_INSPECTION_PROFILE_INVALID", "live inspection identity or coverage is invalid");
  }
  for (const [field, valueToCheck] of [
    ["receipt_sha256", value.receipt_sha256],
    ["project_descriptor_sha256", value.project_descriptor_sha256],
    ["content_digest", value.content_digest],
  ]) requireString(valueToCheck, SHA256, `live_inspection.${field}`);
  if (!isExactUtcTimestamp(value.verified_at)) {
    fail("ANIMATION_MMG040_INSPECTION_PROFILE_INVALID", "live_inspection.verified_at must be an exact UTC timestamp");
  }
  for (const field of ["receipt_id", "operator_id", "project_revision", "content_revision"]) {
    if (typeof value[field] !== "string" || value[field].length === 0 || value[field].length > 160) {
      fail("ANIMATION_MMG040_INSPECTION_PROFILE_INVALID", `live_inspection.${field} is invalid`);
    }
  }
}

function validateInspectionProfileShape(profile, contract) {
  rejectAssetCountField(profile, "inspection profile");
  exactKeys(profile, [
    "schema", "profile_id", "profile_revision", "sample_id", "source_contract_sha256",
    "candidate_source_sha256", "verification_status", "start_allowed", "runtime_ready",
    "source_lineage_status", "source_binding", "filesystem_inventory_observation",
    "excluded_unbound_catalog_claims", "scene_object_candidates", "animation_source_candidates",
    "derived_targets", "live_inspection", "proof_boundary",
  ], "inspection profile");
  if (profile.schema !== INSPECTION_PROFILE_SCHEMA || profile.profile_id !== PROFILE_ID ||
      profile.profile_revision !== PROFILE_REVISION || profile.sample_id !== SAMPLE_ID ||
      profile.source_contract_sha256 !== PINNED_SOURCE_CONTRACT_SHA256 ||
      profile.candidate_source_sha256 !== PINNED_CANDIDATE_SOURCE_SHA256 ||
      !["candidate_unverified", "live_verified"].includes(profile.verification_status) ||
      profile.start_allowed !== false || profile.runtime_ready !== false ||
      profile.source_lineage_status !== SOURCE_LINEAGE_STATUS) {
    fail("ANIMATION_MMG040_INSPECTION_PROFILE_INVALID", "inspection profile identity or readiness is invalid");
  }
  sameScalarObject(profile.source_binding, OFFICIAL_SOURCE_BINDING, "source_binding");
  validateFilesystemObservation(profile.filesystem_inventory_observation);
  validateExcludedCatalogs(profile.excluded_unbound_catalog_claims);
  const contractAssetIds = new Set(contract.assets.map((asset) => asset.asset_id));
  if (!Array.isArray(profile.scene_object_candidates) || !Array.isArray(profile.animation_source_candidates)) {
    fail("ANIMATION_MMG040_INSPECTION_PROFILE_INVALID", "candidate output groups are invalid");
  }
  profile.scene_object_candidates.forEach((candidate, index) => validateOutputCandidate(candidate, `scene_object_candidates[${index}]`, contractAssetIds));
  profile.animation_source_candidates.forEach((candidate, index) => validateOutputCandidate(candidate, `animation_source_candidates[${index}]`, contractAssetIds));
  if (!Array.isArray(profile.derived_targets) || profile.derived_targets.length !== contract.assets.length) {
    fail("ANIMATION_MMG040_INSPECTION_PROFILE_INVALID", "derived target coverage is invalid");
  }
  const live = profile.verification_status === "live_verified";
  for (const [index, target] of profile.derived_targets.entries()) {
    exactKeys(target, [
      "asset_id", "role", "target_object_path", "expected_class", "source_candidate_ids",
      "authoring_required", "required_live_checks", "content_status", "verification_status",
      "source_lineage_status", "live_receipt_evidence",
    ], `derived_targets[${index}]`);
    const expected = contract.assets[index];
    if (target.asset_id !== expected.asset_id || target.role !== expected.role ||
        target.target_object_path !== expected.object_path || target.expected_class !== expected.expected_class ||
        target.authoring_required !== true || target.source_lineage_status !== SOURCE_LINEAGE_STATUS ||
        target.verification_status !== (live ? "live_verified" : "candidate_unverified") ||
        target.content_status !== (live ? "live_inspection_verified" : "not_authored_or_unverified")) {
      fail("ANIMATION_MMG040_INSPECTION_PROFILE_INVALID", `derived target '${expected.asset_id}' is invalid`);
    }
    requireUniqueStrings(target.source_candidate_ids, SAFE_ID, `derived_targets[${index}].source_candidate_ids`, { allowEmpty: true });
    requireUniqueStrings(target.required_live_checks, SAFE_CHECK, `derived_targets[${index}].required_live_checks`);
    if (!live && target.live_receipt_evidence !== null) {
      fail("ANIMATION_MMG040_INSPECTION_PROFILE_INVALID", "candidate profile cannot contain live receipt evidence");
    }
    if (live) {
      exactKeys(target.live_receipt_evidence, ["package_sha256", "loaded", "observed_class", "observed_object_path"], `derived_targets[${index}].live_receipt_evidence`);
      if (target.live_receipt_evidence.loaded !== true ||
          target.live_receipt_evidence.observed_class !== expected.expected_class ||
          target.live_receipt_evidence.observed_object_path !== expected.object_path) {
        fail("ANIMATION_MMG040_INSPECTION_PROFILE_INVALID", "live target evidence does not match the contract");
      }
      requireString(target.live_receipt_evidence.package_sha256, SHA256, `derived_targets[${index}].package_sha256`);
    }
  }
  if (live) {
    if (!isObject(profile.live_inspection)) fail("ANIMATION_MMG040_INSPECTION_PROFILE_INVALID", "live profile requires an inspection receipt reference");
    validateLiveInspection(profile.live_inspection);
  } else if (profile.live_inspection !== null) {
    fail("ANIMATION_MMG040_INSPECTION_PROFILE_INVALID", "candidate profile cannot claim live inspection");
  }
  sameScalarObject(profile.proof_boundary, PROOF_BOUNDARY, "proof_boundary");
  return profile;
}

export function buildInspectionProfile(basis) {
  if (arguments.length !== 1) {
    fail("ANIMATION_MMG040_INSPECTION_BASIS_INVALID", "profile builder accepts exactly one opaque inspection basis");
  }
  const materialized = materializeInspectionBasis(basis);
  const profile = createInspectionProfile(materialized);
  return validateInspectionProfileShape(profile, materialized.contract);
}

export function validateInspectionProfile(profile, basis) {
  if (arguments.length !== 2) {
    fail("ANIMATION_MMG040_INSPECTION_BASIS_INVALID", "profile validation requires an exact opaque inspection basis");
  }
  const materialized = materializeInspectionBasis(basis);
  const claimsLive = isObject(profile) && profile.verification_status === "live_verified";
  if (claimsLive && materialized.receipt === null) {
    fail("ANIMATION_MMG040_LIVE_BASIS_REQUIRED", "live profile validation requires a sealed verified receipt basis");
  }
  validateInspectionProfileShape(profile, materialized.contract);
  if (claimsLive && profile.live_inspection.receipt_sha256 !== materialized.receiptSha256) {
    fail("ANIMATION_MMG040_RECEIPT_HASH_MISMATCH", "live profile receipt hash does not match the sealed receipt bytes");
  }
  const expected = validateInspectionProfileShape(
    createInspectionProfile(materialized),
    materialized.contract,
  );
  if (canonicalize(profile) !== canonicalize(expected)) {
    fail("ANIMATION_MMG040_INSPECTION_BASIS_MISMATCH", "inspection profile does not exactly match its sealed basis");
  }
  return profile;
}

function parseArgs(argv) {
  const output = new Map();
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index];
    const value = argv[index + 1];
    if (!key?.startsWith("--") || value === undefined || output.has(key.slice(2))) {
      fail("ANIMATION_MMG040_ARGUMENTS_INVALID", "arguments must be unique --name value pairs");
    }
    output.set(key.slice(2), value);
  }
  for (const key of output.keys()) {
    if (!["contract", "candidates", "receipt"].includes(key)) {
      fail("ANIMATION_MMG040_ARGUMENTS_INVALID", `unknown argument --${key}`);
    }
  }
  if (!output.has("contract") || !output.has("candidates")) {
    fail("ANIMATION_MMG040_ARGUMENTS_INVALID", "--contract and --candidates are required");
  }
  return output;
}

export function main(argv) {
  const args = parseArgs(argv);
  let basis = loadInspectionProfileBasis({
    contractPath: args.get("contract"),
    candidatesPath: args.get("candidates"),
  });
  if (args.has("receipt")) {
    basis = loadInspectionReceiptBasis(basis, { receiptPath: args.get("receipt") });
  }
  const profile = buildInspectionProfile(basis);
  process.stdout.write(`${JSON.stringify(profile, null, 2)}\n`);
  return 0;
}

const invokedPath = process.argv[1] ? pathToFileURL(path.resolve(process.argv[1])).href : "";
if (import.meta.url === invokedPath) {
  try {
    process.exitCode = main(process.argv.slice(2));
  } catch (error) {
    if (error instanceof Mmg040InspectionProfileError || error instanceof ContentProfileContractError) {
      process.stderr.write(`${error.code}: ${error.message}\n`);
    } else {
      process.stderr.write("ANIMATION_MMG040_INSPECTION_PROFILE_FAILED\n");
    }
    process.exitCode = 2;
  }
}

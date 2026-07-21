#!/usr/bin/env node

import { createHash } from "node:crypto";
import {
  closeSync,
  constants,
  fstatSync,
  lstatSync,
  openSync,
  readFileSync,
  realpathSync,
} from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

export const PINNED_SOURCE_CONTRACT_SHA256 = "1b0aa6e48d251cb8dbeac4f34528ca8fa6084fb330fc2d150ef341f630528b1c";

const SOURCE_SCHEMA = "vista-animation-project-profile-source/v1";
const RECEIPT_SCHEMA = "vista-animation-content-inspection-receipt/v1";
const PROFILE_SCHEMA = "vista-animation-content-profile/v1";
const BINDING_SCHEMA = "vista-animation-content-binding/v1";
const SAFE_ID = /^[a-z][a-z0-9_-]{0,119}$/;
const OPAQUE_ID = /^[A-Za-z0-9][A-Za-z0-9._:@-]{0,159}$/;
const SHA256 = /^[a-f0-9]{64}$/;
const OBJECT_PATH = /^\/Game\/[A-Za-z0-9_./-]{1,500}$/;
const CLASS_PATH = /^\/Script\/[A-Za-z0-9_./-]{1,250}$/;
const ISO_UTC = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$/;
const MAX_JSON_BYTES = 2 * 1024 * 1024;

const ACTION_IDENTITIES = Object.freeze({
  look_at: ["vista_look_at_v1", "required"],
  brace: ["vista_brace_ik_v1", "required"],
  drag: ["vista_drag_ik_v1", "required"],
  lift_foot: ["vista_lift_foot_ik_v1", "required"],
  pause: ["vista_pause_pose_v1", "optional"],
  fall: ["vista_fall_montage_v1", "forbidden"],
  recover: ["vista_recover_montage_v1", "forbidden"],
});

const ACTION_PARAMETER_CONTRACTS = Object.freeze({
  look_at: { duration_sec: 1, distance_cm: null, height_cm: null, hand: null, foot: null, direction: null },
  brace: { duration_sec: 2, distance_cm: null, height_cm: null, hand: "both", foot: null, direction: null },
  drag: { duration_sec: 2, distance_cm: 120, height_cm: null, hand: "right", foot: null, direction: null },
  lift_foot: { duration_sec: 2, distance_cm: null, height_cm: 35, hand: null, foot: "left", direction: null },
  pause: { duration_sec: 3, distance_cm: null, height_cm: null, hand: null, foot: null, direction: null },
  fall: { duration_sec: null, distance_cm: null, height_cm: null, hand: null, foot: null, direction: "forward" },
  recover: { duration_sec: null, distance_cm: null, height_cm: null, hand: null, foot: null, direction: "forward" },
});

const REQUIRED_CHECKS = Object.freeze([
  "pawn_spawnable",
  "generated_class_matches",
  "skeletal_mesh_matches",
  "anim_blueprint_matches",
  "no_redirectors",
  "dependency_closure",
  "disposable_pie",
  "scene_zero_diff",
]);

export class ContentProfileContractError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "ContentProfileContractError";
    this.code = code;
  }
}

function fail(code, message) {
  throw new ContentProfileContractError(code, message);
}

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function exactKeys(value, keys, label) {
  if (!isObject(value)) fail("ANIMATION_MMG040_RECEIPT_INVALID", `${label} must be an object`);
  const expected = [...keys].sort();
  const actual = Object.keys(value).sort();
  if (expected.length !== actual.length || expected.some((key, index) => key !== actual[index])) {
    fail("ANIMATION_MMG040_RECEIPT_INVALID", `${label} has an invalid shape`);
  }
}

function requireString(value, pattern, label) {
  if (typeof value !== "string" || !pattern.test(value)) {
    fail("ANIMATION_MMG040_RECEIPT_INVALID", `${label} is invalid`);
  }
  return value;
}

function requireUniqueStrings(value, pattern, label) {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string" || !pattern.test(item))) {
    fail("ANIMATION_MMG040_RECEIPT_INVALID", `${label} must be a string array`);
  }
  if (new Set(value).size !== value.length) {
    fail("ANIMATION_MMG040_RECEIPT_INVALID", `${label} contains duplicates`);
  }
  return [...value];
}

function sameStringSet(actual, expected) {
  if (actual.length !== expected.length) return false;
  const left = [...actual].sort();
  const right = [...expected].sort();
  return left.every((value, index) => value === right[index]);
}

function validateExactParameters(value, label) {
  exactKeys(value, ["duration_sec", "distance_cm", "height_cm", "hand", "foot", "direction"], label);
  for (const [key, maximum] of [["duration_sec", 60], ["distance_cm", 500], ["height_cm", 150]]) {
    if (value[key] !== null && (!Number.isInteger(value[key]) || value[key] < 1 || value[key] > maximum)) {
      fail("ANIMATION_MMG040_RECEIPT_INVALID", `${label}.${key} is invalid`);
    }
  }
  if (value.hand !== null && !["left", "right", "both"].includes(value.hand)) {
    fail("ANIMATION_MMG040_RECEIPT_INVALID", `${label}.hand is invalid`);
  }
  if (value.foot !== null && !["left", "right"].includes(value.foot)) {
    fail("ANIMATION_MMG040_RECEIPT_INVALID", `${label}.foot is invalid`);
  }
  if (value.direction !== null && !["forward", "backward", "left", "right"].includes(value.direction)) {
    fail("ANIMATION_MMG040_RECEIPT_INVALID", `${label}.direction is invalid`);
  }
  return value;
}

function compareAscii(left, right) {
  return left < right ? -1 : left > right ? 1 : 0;
}

export function canonicalize(value) {
  if (value === null || typeof value === "boolean" || typeof value === "string") return JSON.stringify(value);
  if (typeof value === "number") {
    if (!Number.isFinite(value)) fail("ANIMATION_MMG040_JSON_INVALID", "JSON number is not finite");
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(canonicalize).join(",")}]`;
  if (!isObject(value)) fail("ANIMATION_MMG040_JSON_INVALID", "Unsupported JSON value");
  return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalize(value[key])}`).join(",")}}`;
}

export function sha256Bytes(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

class StrictJsonParser {
  constructor(text) {
    this.text = text;
    this.index = 0;
    this.nodes = 0;
  }

  parse() {
    const value = this.value(0);
    this.space();
    if (this.index !== this.text.length) this.error("trailing input");
    return value;
  }

  error(reason) {
    fail("ANIMATION_MMG040_JSON_INVALID", `Strict JSON parse failed at byte ${this.index}: ${reason}`);
  }

  space() {
    while (" \t\r\n".includes(this.text[this.index] ?? "\u0000")) this.index += 1;
  }

  value(depth) {
    if (depth > 32 || ++this.nodes > 10000) this.error("document bounds exceeded");
    this.space();
    const token = this.text[this.index];
    if (token === "{") return this.object(depth + 1);
    if (token === "[") return this.array(depth + 1);
    if (token === '"') return this.string();
    if (token === "t" && this.text.slice(this.index, this.index + 4) === "true") {
      this.index += 4;
      return true;
    }
    if (token === "f" && this.text.slice(this.index, this.index + 5) === "false") {
      this.index += 5;
      return false;
    }
    if (token === "n" && this.text.slice(this.index, this.index + 4) === "null") {
      this.index += 4;
      return null;
    }
    return this.number();
  }

  object(depth) {
    this.index += 1;
    const output = Object.create(null);
    const keys = new Set();
    this.space();
    if (this.text[this.index] === "}") {
      this.index += 1;
      return output;
    }
    while (true) {
      this.space();
      if (this.text[this.index] !== '"') this.error("object key expected");
      const key = this.string();
      if (keys.has(key)) fail("ANIMATION_MMG040_JSON_DUPLICATE_KEY", `Duplicate JSON key '${key}'`);
      keys.add(key);
      this.space();
      if (this.text[this.index] !== ":") this.error("colon expected");
      this.index += 1;
      output[key] = this.value(depth);
      this.space();
      const token = this.text[this.index++];
      if (token === "}") return output;
      if (token !== ",") this.error("comma or object end expected");
    }
  }

  array(depth) {
    this.index += 1;
    const output = [];
    this.space();
    if (this.text[this.index] === "]") {
      this.index += 1;
      return output;
    }
    while (true) {
      output.push(this.value(depth));
      this.space();
      const token = this.text[this.index++];
      if (token === "]") return output;
      if (token !== ",") this.error("comma or array end expected");
    }
  }

  string() {
    const start = this.index;
    this.index += 1;
    while (this.index < this.text.length) {
      const code = this.text.charCodeAt(this.index);
      if (code === 0x22) {
        this.index += 1;
        try {
          return JSON.parse(this.text.slice(start, this.index));
        } catch {
          this.error("invalid string escape");
        }
      }
      if (code < 0x20) this.error("unescaped control character");
      if (code === 0x5c) {
        this.index += 1;
        if (this.text[this.index] === "u") {
          if (!/^[0-9a-fA-F]{4}$/.test(this.text.slice(this.index + 1, this.index + 5))) this.error("invalid unicode escape");
          this.index += 4;
        } else if (!/["\\/bfnrt]/.test(this.text[this.index] ?? "")) {
          this.error("invalid escape");
        }
      }
      this.index += 1;
    }
    this.error("unterminated string");
  }

  number() {
    const match = /^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/.exec(this.text.slice(this.index));
    if (!match) this.error("value expected");
    this.index += match[0].length;
    const value = Number(match[0]);
    if (!Number.isFinite(value)) this.error("number out of range");
    return value;
  }
}

export function parseStrictJson(bytes) {
  let text;
  if (Buffer.isBuffer(bytes)) {
    try {
      text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    } catch {
      fail("ANIMATION_MMG040_JSON_INVALID", "JSON document is not valid UTF-8");
    }
  } else {
    text = String(bytes);
  }
  if (Buffer.byteLength(text, "utf8") > MAX_JSON_BYTES || text.includes("\u0000")) {
    fail("ANIMATION_MMG040_JSON_INVALID", "JSON document is oversized or contains NUL");
  }
  return new StrictJsonParser(text).parse();
}

function pathPrefixes(absolutePath) {
  const parsed = path.parse(absolutePath);
  const parts = absolutePath.slice(parsed.root.length).split(path.sep).filter(Boolean);
  const prefixes = [];
  let cursor = parsed.root;
  for (const part of parts) {
    cursor = path.join(cursor, part);
    prefixes.push(cursor);
  }
  return prefixes;
}

export function readSecureJson(absolutePath, label) {
  if (!path.isAbsolute(absolutePath) || path.normalize(absolutePath) !== absolutePath) {
    fail("ANIMATION_MMG040_RECEIPT_PATH_INVALID", `${label} path must be absolute and normalized`);
  }
  const prefixes = pathPrefixes(absolutePath);
  const uid = typeof process.getuid === "function" ? process.getuid() : -1;
  for (const [index, prefix] of prefixes.entries()) {
    const status = lstatSync(prefix);
    if (status.isSymbolicLink()) fail("ANIMATION_MMG040_RECEIPT_PATH_INVALID", `${label} path contains a symlink`);
    if (index < prefixes.length - 1) {
      if (!status.isDirectory() || (status.uid !== 0 && status.uid !== uid) || (status.mode & 0o022) !== 0) {
        fail("ANIMATION_MMG040_RECEIPT_PATH_INVALID", `${label} parent is not integrity protected`);
      }
    }
  }
  if (realpathSync(absolutePath) !== absolutePath) {
    fail("ANIMATION_MMG040_RECEIPT_PATH_INVALID", `${label} path is not canonical`);
  }
  const before = lstatSync(absolutePath);
  if (!before.isFile() || before.nlink !== 1 || (before.uid !== 0 && before.uid !== uid) || (before.mode & 0o022) !== 0) {
    fail("ANIMATION_MMG040_RECEIPT_PATH_INVALID", `${label} must be a protected owner/root regular file`);
  }
  if (before.size <= 0 || before.size > MAX_JSON_BYTES) {
    fail("ANIMATION_MMG040_RECEIPT_PATH_INVALID", `${label} size is invalid`);
  }
  if (constants.O_NOFOLLOW === undefined) fail("ANIMATION_MMG040_RECEIPT_PATH_INVALID", "O_NOFOLLOW is unavailable");
  const descriptor = openSync(absolutePath, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const opened = fstatSync(descriptor);
    if (!opened.isFile() || opened.dev !== before.dev || opened.ino !== before.ino || opened.size !== before.size ||
        opened.nlink !== 1 || (opened.uid !== 0 && opened.uid !== uid) || (opened.mode & 0o022) !== 0) {
      fail("ANIMATION_MMG040_RECEIPT_PATH_INVALID", `${label} changed before read`);
    }
    const bytes = readFileSync(descriptor);
    const after = fstatSync(descriptor);
    if (after.dev !== opened.dev || after.ino !== opened.ino || after.size !== opened.size ||
        after.mtimeMs !== opened.mtimeMs || after.ctimeMs !== opened.ctimeMs || after.nlink !== opened.nlink ||
        after.uid !== opened.uid || after.mode !== opened.mode) {
      fail("ANIMATION_MMG040_RECEIPT_PATH_INVALID", `${label} changed during read`);
    }
    return { bytes, value: parseStrictJson(bytes), sha256: sha256Bytes(bytes) };
  } finally {
    closeSync(descriptor);
  }
}

export function validateSourceContract(contract, rawSha256) {
  exactKeys(contract, [
    "schema", "profile_id", "profile_revision", "sample_id", "target",
    "source_provenance", "current_readiness", "assets", "actions", "receipt_requirements",
  ], "source contract");
  if (rawSha256 !== PINNED_SOURCE_CONTRACT_SHA256) {
    fail("ANIMATION_MMG040_SOURCE_CONTRACT_MISMATCH", "Source contract bytes do not match the compiled pin");
  }
  if (contract.schema !== SOURCE_SCHEMA || contract.profile_id !== "vista_mmg040" ||
      contract.profile_revision !== "mmg040_project_content_r1" || contract.sample_id !== "mmg_040") {
    fail("ANIMATION_MMG040_SOURCE_CONTRACT_MISMATCH", "Source contract identity is invalid");
  }
  exactKeys(contract.target, ["project_name", "engine_version", "content_namespace"], "source contract target");
  if (contract.target.project_name !== "gym_citynav" || contract.target.engine_version !== "5.3.2" ||
      contract.target.content_namespace !== "/Game/VISTA/MMG040/") {
    fail("ANIMATION_MMG040_SOURCE_CONTRACT_MISMATCH", "Source contract target is invalid");
  }
  exactKeys(contract.current_readiness, ["ready", "reason_codes"], "current_readiness");
  if (contract.current_readiness.ready !== false) fail("ANIMATION_MMG040_SOURCE_CONTRACT_MISMATCH", "Unchecked source contract cannot be ready");
  requireUniqueStrings(contract.current_readiness.reason_codes, /^ANIMATION_[A-Z0-9_]{1,119}$/, "current_readiness.reason_codes");
  if (!Array.isArray(contract.assets) || contract.assets.length !== 13) fail("ANIMATION_MMG040_SOURCE_CONTRACT_MISMATCH", "Exactly thirteen pinned assets are required");
  const assetIds = new Set();
  for (const [index, asset] of contract.assets.entries()) {
    exactKeys(asset, ["asset_id", "role", "object_path", "expected_class", "skeleton_asset_id", "required_notifies", "root_motion_policy"], `assets[${index}]`);
    requireString(asset.asset_id, SAFE_ID, `assets[${index}].asset_id`);
    if (assetIds.has(asset.asset_id)) fail("ANIMATION_MMG040_SOURCE_CONTRACT_MISMATCH", "Pinned asset IDs must be unique");
    assetIds.add(asset.asset_id);
    requireString(asset.object_path, OBJECT_PATH, `assets[${index}].object_path`);
    if (!asset.object_path.startsWith(contract.target.content_namespace) || asset.object_path.includes("..") || asset.object_path.includes("//")) {
      fail("ANIMATION_MMG040_SOURCE_CONTRACT_MISMATCH", "Pinned asset escaped the project-owned namespace");
    }
    requireString(asset.expected_class, CLASS_PATH, `assets[${index}].expected_class`);
    if (asset.skeleton_asset_id !== null) requireString(asset.skeleton_asset_id, SAFE_ID, `assets[${index}].skeleton_asset_id`);
    requireUniqueStrings(asset.required_notifies, SAFE_ID, `assets[${index}].required_notifies`);
    if (!["required", "forbidden", "not_applicable"].includes(asset.root_motion_policy)) {
      fail("ANIMATION_MMG040_SOURCE_CONTRACT_MISMATCH", "Pinned root-motion policy is invalid");
    }
  }
  for (const asset of contract.assets) {
    if (asset.skeleton_asset_id !== null && !assetIds.has(asset.skeleton_asset_id)) {
      fail("ANIMATION_MMG040_SOURCE_CONTRACT_MISMATCH", "Pinned skeleton reference is missing");
    }
  }
  if (!Array.isArray(contract.actions) || contract.actions.length !== 7) fail("ANIMATION_MMG040_SOURCE_CONTRACT_MISMATCH", "Exactly seven fixed actions are required");
  const actions = new Set();
  for (const [index, action] of contract.actions.entries()) {
    exactKeys(action, [
      "action", "adapter_id", "bridge_action_id", "implementation_asset_id", "supporting_asset_ids",
      "completion_signal", "timeout_ms", "target_policy", "actor_capabilities", "target_capabilities",
      "anchor_kinds", "parameter_contract", "live_checks",
    ], `actions[${index}]`);
    const identity = ACTION_IDENTITIES[action.action];
    if (!identity || actions.has(action.action) || action.adapter_id !== identity[0] ||
        action.bridge_action_id !== identity[0] || action.target_policy !== identity[1]) {
      fail("ANIMATION_MMG040_SOURCE_CONTRACT_MISMATCH", "Fixed action identity is invalid");
    }
    actions.add(action.action);
    if (!assetIds.has(action.implementation_asset_id) || action.supporting_asset_ids.some((id) => !assetIds.has(id))) {
      fail("ANIMATION_MMG040_SOURCE_CONTRACT_MISMATCH", "Action references an unknown pinned asset");
    }
    requireUniqueStrings(action.supporting_asset_ids, SAFE_ID, `actions[${index}].supporting_asset_ids`);
    requireString(action.completion_signal, SAFE_ID, `actions[${index}].completion_signal`);
    requireUniqueStrings(action.actor_capabilities, SAFE_ID, `actions[${index}].actor_capabilities`);
    requireUniqueStrings(action.target_capabilities, SAFE_ID, `actions[${index}].target_capabilities`);
    requireUniqueStrings(action.anchor_kinds, SAFE_ID, `actions[${index}].anchor_kinds`);
    requireUniqueStrings(action.live_checks, SAFE_ID, `actions[${index}].live_checks`);
    validateExactParameters(action.parameter_contract, `actions[${index}].parameter_contract`);
    if (canonicalize(action.parameter_contract) !== canonicalize(ACTION_PARAMETER_CONTRACTS[action.action])) {
      fail("ANIMATION_MMG040_SOURCE_CONTRACT_MISMATCH", "Fixed action parameter contract does not match the server defaults");
    }
    if (!Number.isInteger(action.timeout_ms) || action.timeout_ms < 100 || action.timeout_ms > 60000) {
      fail("ANIMATION_MMG040_SOURCE_CONTRACT_MISMATCH", "Action timeout is invalid");
    }
  }
  if (!sameStringSet([...actions], Object.keys(ACTION_IDENTITIES))) fail("ANIMATION_MMG040_SOURCE_CONTRACT_MISMATCH", "Fixed action set is incomplete");
  exactKeys(contract.receipt_requirements, ["schema", "method", "required_checks"], "receipt_requirements");
  if (contract.receipt_requirements.schema !== RECEIPT_SCHEMA ||
      contract.receipt_requirements.method !== "ue53_disposable_live_inspection_v1" ||
      !sameStringSet(contract.receipt_requirements.required_checks, REQUIRED_CHECKS)) {
    fail("ANIMATION_MMG040_SOURCE_CONTRACT_MISMATCH", "Receipt requirements are invalid");
  }
  return contract;
}

function contentBinding(contractSha256, receipt) {
  return {
    schema: BINDING_SCHEMA,
    source_contract_sha256: contractSha256,
    content_revision: receipt.content_revision,
    project_revision: receipt.project.project_revision,
    project_descriptor_sha256: receipt.project.project_descriptor_sha256,
    assets: receipt.assets
      .map(({ asset_id, package_sha256 }) => ({ asset_id, package_sha256 }))
      .sort((left, right) => compareAscii(left.asset_id, right.asset_id)),
    actions: receipt.actions
      .map(({ action, behavior_evidence_sha256, observed_live_checks, verified_parameters }) => ({
        action,
        behavior_evidence_sha256,
        observed_live_checks: [...observed_live_checks].sort(compareAscii),
        verified_parameters,
      }))
      .sort((left, right) => compareAscii(left.action, right.action)),
  };
}

export function computeContentDigest(contractSha256, receipt) {
  return sha256Bytes(Buffer.from(canonicalize(contentBinding(contractSha256, receipt)), "utf8"));
}

export function validateInspectionReceipt(
  contract,
  contractSha256,
  receipt,
  { requireContentDigest = true } = {},
) {
  exactKeys(receipt, [
    "schema", "source_contract_sha256", "profile_id", "profile_revision", "content_revision",
    "content_digest", "project", "verification", "assets", "actions", "checks",
  ], "inspection receipt");
  if (receipt.schema !== RECEIPT_SCHEMA || receipt.source_contract_sha256 !== contractSha256 ||
      receipt.profile_id !== contract.profile_id || receipt.profile_revision !== contract.profile_revision) {
    fail("ANIMATION_MMG040_RECEIPT_MISMATCH", "Inspection receipt identity does not match the pinned source contract");
  }
  requireString(receipt.content_revision, OPAQUE_ID, "content_revision");
  requireString(receipt.content_digest, SHA256, "content_digest");
  exactKeys(receipt.project, ["project_name", "engine_version", "project_revision", "project_descriptor_sha256"], "project");
  if (receipt.project.project_name !== contract.target.project_name || receipt.project.engine_version !== contract.target.engine_version) {
    fail("ANIMATION_MMG040_RECEIPT_MISMATCH", "Inspection receipt project does not match the target");
  }
  requireString(receipt.project.project_revision, OPAQUE_ID, "project.project_revision");
  requireString(receipt.project.project_descriptor_sha256, SHA256, "project.project_descriptor_sha256");
  exactKeys(receipt.verification, ["status", "receipt_id", "verified_at", "operator_id", "method"], "verification");
  if (receipt.verification.status !== "verified" || receipt.verification.method !== contract.receipt_requirements.method) {
    fail("ANIMATION_MMG040_RECEIPT_UNVERIFIED", "Inspection receipt is not verified with the required method");
  }
  requireString(receipt.verification.receipt_id, OPAQUE_ID, "verification.receipt_id");
  requireString(receipt.verification.operator_id, OPAQUE_ID, "verification.operator_id");
  if (typeof receipt.verification.verified_at !== "string" || !ISO_UTC.test(receipt.verification.verified_at) || Number.isNaN(Date.parse(receipt.verification.verified_at))) {
    fail("ANIMATION_MMG040_RECEIPT_INVALID", "verification.verified_at must be an exact UTC timestamp");
  }
  exactKeys(receipt.checks, REQUIRED_CHECKS, "checks");
  for (const check of REQUIRED_CHECKS) if (receipt.checks[check] !== true) fail("ANIMATION_MMG040_LIVE_CHECK_FAILED", `Required live check '${check}' did not pass`);

  if (!Array.isArray(receipt.assets) || receipt.assets.length !== contract.assets.length) {
    fail("ANIMATION_MMG040_ASSET_RECEIPT_INCOMPLETE", "Inspection receipt does not cover every pinned asset");
  }
  const receiptAssets = new Map();
  for (const [index, asset] of receipt.assets.entries()) {
    exactKeys(asset, ["asset_id", "object_path", "observed_class", "package_sha256", "loaded", "skeleton_asset_id", "notify_names", "root_motion_enabled"], `receipt.assets[${index}]`);
    requireString(asset.asset_id, SAFE_ID, `receipt.assets[${index}].asset_id`);
    if (receiptAssets.has(asset.asset_id)) fail("ANIMATION_MMG040_ASSET_RECEIPT_INCOMPLETE", "Duplicate asset receipt");
    receiptAssets.set(asset.asset_id, asset);
  }
  for (const expected of contract.assets) {
    const observed = receiptAssets.get(expected.asset_id);
    if (!observed || observed.loaded !== true || observed.object_path !== expected.object_path ||
        observed.observed_class !== expected.expected_class || observed.skeleton_asset_id !== expected.skeleton_asset_id) {
      fail("ANIMATION_MMG040_PINNED_ASSET_MISMATCH", `Pinned asset '${expected.asset_id}' did not match live inspection`);
    }
    requireString(observed.object_path, OBJECT_PATH, `asset ${expected.asset_id}.object_path`);
    requireString(observed.observed_class, CLASS_PATH, `asset ${expected.asset_id}.observed_class`);
    requireString(observed.package_sha256, SHA256, `asset ${expected.asset_id}.package_sha256`);
    const notifies = requireUniqueStrings(observed.notify_names, SAFE_ID, `asset ${expected.asset_id}.notify_names`);
    if (!sameStringSet(notifies, expected.required_notifies)) {
      fail("ANIMATION_MMG040_NOTIFY_MISMATCH", `Pinned asset '${expected.asset_id}' has an invalid notify set`);
    }
    const expectedRootMotion = expected.root_motion_policy === "required";
    if (typeof observed.root_motion_enabled !== "boolean" || observed.root_motion_enabled !== expectedRootMotion) {
      fail("ANIMATION_MMG040_ROOT_MOTION_MISMATCH", `Pinned asset '${expected.asset_id}' has invalid root motion`);
    }
  }

  if (!Array.isArray(receipt.actions) || receipt.actions.length !== contract.actions.length) {
    fail("ANIMATION_MMG040_ACTION_RECEIPT_INCOMPLETE", "Inspection receipt does not cover every fixed action");
  }
  const receiptActions = new Map();
  for (const [index, action] of receipt.actions.entries()) {
    exactKeys(action, [
      "action", "implementation_asset_id", "completion_signal", "implementation_matches", "skeleton_matches",
      "completion_signal_observed", "behavior_evidence_sha256", "ik_contact_verified", "root_motion_verified",
      "collision_verified", "recovery_alignment_verified", "observed_live_checks", "verified_parameters",
    ], `receipt.actions[${index}]`);
    if (!ACTION_IDENTITIES[action.action] || receiptActions.has(action.action)) {
      fail("ANIMATION_MMG040_ACTION_RECEIPT_INCOMPLETE", "Inspection receipt has an unknown or duplicate action");
    }
    receiptActions.set(action.action, action);
  }
  for (const expected of contract.actions) {
    const observed = receiptActions.get(expected.action);
    if (!observed || observed.implementation_asset_id !== expected.implementation_asset_id ||
        observed.completion_signal !== expected.completion_signal || observed.implementation_matches !== true ||
        observed.skeleton_matches !== true || observed.completion_signal_observed !== true) {
      fail("ANIMATION_MMG040_ACTION_RECEIPT_MISMATCH", `Fixed action '${expected.action}' is not live verified`);
    }
    requireString(observed.behavior_evidence_sha256, SHA256, `action ${expected.action}.behavior_evidence_sha256`);
    const observedLiveChecks = requireUniqueStrings(
      observed.observed_live_checks,
      SAFE_ID,
      `action ${expected.action}.observed_live_checks`,
    );
    validateExactParameters(observed.verified_parameters, `action ${expected.action}.verified_parameters`);
    if (!sameStringSet(observedLiveChecks, expected.live_checks) ||
        canonicalize(observed.verified_parameters) !== canonicalize(expected.parameter_contract)) {
      fail("ANIMATION_MMG040_BEHAVIOR_RECEIPT_MISMATCH", `Fixed action '${expected.action}' does not cover the exact live checks and parameter contract`);
    }
    const requirements = {
      ik_contact_verified: ["brace", "drag", "lift_foot"].includes(expected.action),
      root_motion_verified: ["drag", "fall", "recover"].includes(expected.action),
      collision_verified: expected.action === "fall",
      recovery_alignment_verified: expected.action === "recover",
    };
    for (const [field, required] of Object.entries(requirements)) {
      if (typeof observed[field] !== "boolean" || observed[field] !== required) {
        fail("ANIMATION_MMG040_BEHAVIOR_RECEIPT_MISMATCH", `Fixed action '${expected.action}' has invalid ${field}`);
      }
    }
  }
  const computedDigest = computeContentDigest(contractSha256, receipt);
  if (requireContentDigest && receipt.content_digest !== computedDigest) {
    fail("ANIMATION_MMG040_CONTENT_DIGEST_MISMATCH", "Inspection receipt content digest is not canonical");
  }
  return receipt;
}

export function buildServerContentProfile(contract, receipt) {
  const assets = new Map(contract.assets.map((asset) => [asset.asset_id, asset]));
  return {
    schema: PROFILE_SCHEMA,
    profile_id: contract.profile_id,
    revision: contract.profile_revision,
    content_revision: receipt.content_revision,
    content_digest: receipt.content_digest,
    pawn_class_path: assets.get("pawn_class").object_path,
    skeleton_path: assets.get("skeleton").object_path,
    verification: {
      status: "verified",
      receipt_id: receipt.verification.receipt_id,
      verified_at: receipt.verification.verified_at,
    },
    actions: contract.actions.map((action) => ({
      action: action.action,
      adapter_id: action.adapter_id,
      version: "1.0.0",
      bridge_action_id: action.bridge_action_id,
      implementation_asset: assets.get(action.implementation_asset_id).object_path,
      completion_signal: action.completion_signal,
      timeout_ms: action.timeout_ms,
    })),
  };
}

export function buildBlockedPreflight(contract, contractSha256) {
  return {
    schema: "vista-animation-content-profile-preflight/v1",
    profile_id: contract.profile_id,
    profile_revision: contract.profile_revision,
    source_contract_sha256: contractSha256,
    ready: false,
    start_allowed: false,
    reason_codes: [...contract.current_readiness.reason_codes],
  };
}

function parseArgs(argv) {
  const output = new Map();
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index];
    const value = argv[index + 1];
    if (!key?.startsWith("--") || value === undefined || output.has(key.slice(2))) {
      fail("ANIMATION_MMG040_ARGUMENTS_INVALID", "Arguments must be unique --name value pairs");
    }
    output.set(key.slice(2), value);
  }
  for (const key of output.keys()) {
    if (key !== "contract" && key !== "receipt" && key !== "mode") fail("ANIMATION_MMG040_ARGUMENTS_INVALID", `Unknown argument --${key}`);
  }
  if (!output.has("contract")) fail("ANIMATION_MMG040_ARGUMENTS_INVALID", "--contract is required");
  if (output.has("mode") && !["preflight", "digest", "profile"].includes(output.get("mode"))) {
    fail("ANIMATION_MMG040_ARGUMENTS_INVALID", "--mode must be preflight, digest, or profile");
  }
  return output;
}

export function main(argv) {
  const args = parseArgs(argv);
  const source = readSecureJson(args.get("contract"), "source contract");
  const contract = validateSourceContract(source.value, source.sha256);
  const mode = args.get("mode") ?? (args.has("receipt") ? "profile" : "preflight");
  if (mode === "preflight") {
    if (args.has("receipt")) fail("ANIMATION_MMG040_ARGUMENTS_INVALID", "preflight mode does not accept --receipt");
    process.stdout.write(`${JSON.stringify(buildBlockedPreflight(contract, source.sha256), null, 2)}\n`);
    return 3;
  }
  if (!args.has("receipt")) fail("ANIMATION_MMG040_ARGUMENTS_INVALID", `${mode} mode requires --receipt`);
  const receipt = readSecureJson(args.get("receipt"), "inspection receipt");
  if (mode === "digest") {
    validateInspectionReceipt(contract, source.sha256, receipt.value, { requireContentDigest: false });
    process.stdout.write(`${JSON.stringify({
      schema: "vista-animation-content-digest/v1",
      source_contract_sha256: source.sha256,
      content_revision: receipt.value.content_revision,
      content_digest: computeContentDigest(source.sha256, receipt.value),
      ready: false,
      start_allowed: false,
    }, null, 2)}\n`);
    return 0;
  }
  validateInspectionReceipt(contract, source.sha256, receipt.value);
  process.stdout.write(`${JSON.stringify(buildServerContentProfile(contract, receipt.value), null, 2)}\n`);
  return 0;
}

const invokedPath = process.argv[1] ? pathToFileURL(path.resolve(process.argv[1])).href : "";
if (import.meta.url === invokedPath) {
  try {
    process.exitCode = main(process.argv.slice(2));
  } catch (error) {
    if (error instanceof ContentProfileContractError) {
      process.stderr.write(`${error.code}: ${error.message}\n`);
      process.exitCode = 2;
    } else {
      process.stderr.write("ANIMATION_MMG040_PROFILE_PREPARATION_FAILED\n");
      process.exitCode = 2;
    }
  }
}

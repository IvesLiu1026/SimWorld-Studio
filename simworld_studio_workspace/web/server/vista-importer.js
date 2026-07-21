"use strict";

// Fail-closed adapter for small, operator-curated VISTA source bundles.  The
// public request selects an allowlisted revision/sample/attempt; it never
// supplies a filesystem path.  This module deliberately performs no network,
// database, model, or Unreal work.

const crypto = require("crypto");
const fs = require("fs");
const path = require("path");
const YAML = require("yaml");

const IMPORTER_NAME = "vista-scene-importer";
const IMPORTER_VERSION = "1.1.0";
const SOURCE_SCHEMA = "vista-import-source/v1";
const DIALOGUE_SCHEMA = "vista-dialogue-no-oracle/v1";
const MEDIA_SCHEMA = "vista-media-reference/v1";
const SCENE_SCHEMA = "vista-simworld-scene/v1";
const EVALUATION_SCHEMA = "vista-evaluation-input/v1";
const SHA256_RE = /^[a-f0-9]{64}$/;
const SAFE_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const SAFE_SOURCE_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:@-]{0,511}$/;
const MAX_MANIFEST_BYTES = 1024 * 1024;
const MAX_SOURCE_BYTES = 8 * 1024 * 1024;
const MAX_TREE_NODES = 20_000;
const MAX_TREE_DEPTH = 40;

const TOP_LEVEL_SCENE_KEYS = Object.freeze([
  "schema", "scene_id", "profile", "privilege", "provenance", "source",
  "duration_sec", "environment", "camera", "entities", "relations",
  "timeline", "dialogue", "unresolved",
]);

const UNSUPPORTED_RUNTIME_ACTIONS = new Set([
  "brace", "drag", "hesitate", "lift_foot", "unresolved_action",
]);

const BRACE_ACTION_RE = /\b(?:brace|bracing)/;
const LIFT_FOOT_ACTION_RE = /\b(?:lift|raise)\b[^.]{0,80}\bfoot\b|\bfoot\b[^.]{0,80}\b(?:lift|raise)/;

const ORACLE_KEY_RE = /^(?:oracle(?:_|$)|ground[_-]?truth|target(?:_|$)|target_label|label|answer|review[_-]?note|review[_-]?decision|visible[_-]?evidence|dialogue[_-]?evidence|assist[_-]?steps?|intervention(?:_|$)|issue[_-]?summary|prediction(?:_|$)|seed(?:_|$))/i;

class VistaImportError extends Error {
  constructor(code, message, details = {}) {
    super(String(message || "VISTA import failed"));
    this.name = "VistaImportError";
    this.code = String(code || "VISTA_IMPORT_FAILED");
    this.details = details && typeof details === "object" ? { ...details } : {};
    this.retryable = details.retryable === true;
    if (details.cause !== undefined) {
      Object.defineProperty(this, "cause", { value: details.cause, configurable: true });
      delete this.details.cause;
    }
  }
}

function fail(code, message, details) {
  throw new VistaImportError(code, message, details);
}

function isPlainObject(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function requireObject(value, pointer, code = "VISTA_SCHEMA_INVALID") {
  if (!isPlainObject(value)) fail(code, `${pointer} must be an object`, { pointer });
  return value;
}

function exactKeys(value, allowed, required, pointer, code = "VISTA_SCHEMA_INVALID") {
  requireObject(value, pointer, code);
  const allowedSet = new Set(allowed);
  const unknown = Object.keys(value).filter((key) => !allowedSet.has(key));
  if (unknown.length) {
    fail(code, `${pointer} contains unknown fields`, { pointer, fields: unknown.sort() });
  }
  const missing = required.filter((key) => !Object.prototype.hasOwnProperty.call(value, key));
  if (missing.length) {
    fail(code, `${pointer} is missing required fields`, { pointer, fields: missing });
  }
}

function requireString(value, pointer, { max = 16_384, allowEmpty = false, pattern } = {}) {
  if (typeof value !== "string") fail("VISTA_SCHEMA_INVALID", `${pointer} must be a string`, { pointer });
  const normalized = value.trim();
  if ((!allowEmpty && !normalized) || normalized.length > max || (pattern && !pattern.test(normalized))) {
    fail("VISTA_SCHEMA_INVALID", `${pointer} is invalid`, { pointer });
  }
  return normalized;
}

function requireFiniteNumber(value, pointer, { min = -Infinity, max = Infinity } = {}) {
  const number = Number(value);
  if (!Number.isFinite(number) || number < min || number > max) {
    fail("VISTA_SCHEMA_INVALID", `${pointer} must be a finite number in range`, { pointer });
  }
  return number;
}

function canonicalize(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalize).join(",")}]`;
  if (isPlainObject(value)) {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalize(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function sha256(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
}

function safeRelativePath(value, pointer = "file.path") {
  const candidate = requireString(value, pointer, { max: 512 });
  if (
    candidate.includes("\0")
    || candidate.includes("\\")
    || path.posix.isAbsolute(candidate)
    || path.win32.isAbsolute(candidate)
  ) {
    fail("VISTA_PATH_INVALID", `${pointer} must be a safe relative POSIX path`, { pointer });
  }
  const segments = candidate.split("/");
  if (segments.some((part) => !part || part === "." || part === "..") || path.posix.normalize(candidate) !== candidate) {
    fail("VISTA_PATH_INVALID", `${pointer} must not escape or alias the curated bundle`, { pointer });
  }
  return candidate;
}

function resolveContained(root, relative, pointer) {
  const target = path.resolve(root, safeRelativePath(relative, pointer));
  const prefix = root.endsWith(path.sep) ? root : `${root}${path.sep}`;
  if (target !== root && !target.startsWith(prefix)) {
    fail("VISTA_PATH_INVALID", `${pointer} escapes the curated bundle`, { pointer });
  }
  return target;
}

function guardDataTree(value, pointer = "$", depth = 0, state = { nodes: 0 }) {
  state.nodes += 1;
  if (state.nodes > MAX_TREE_NODES || depth > MAX_TREE_DEPTH) {
    fail("VISTA_SOURCE_TOO_COMPLEX", "Curated source exceeds structural limits", { pointer });
  }
  if (Array.isArray(value)) {
    value.forEach((item, index) => guardDataTree(item, `${pointer}/${index}`, depth + 1, state));
    return;
  }
  if (value && typeof value === "object") {
    if (!isPlainObject(value)) fail("VISTA_SCHEMA_INVALID", `${pointer} contains an unsupported value`, { pointer });
    for (const [key, item] of Object.entries(value)) {
      if (key === "__proto__" || key === "prototype" || key === "constructor") {
        fail("VISTA_SCHEMA_INVALID", `${pointer} contains a forbidden key`, { pointer });
      }
      guardDataTree(item, `${pointer}/${key}`, depth + 1, state);
    }
  }
}

function parseJson(buffer, label) {
  try {
    const value = JSON.parse(Buffer.isBuffer(buffer) ? buffer.toString("utf8") : String(buffer));
    guardDataTree(value);
    return value;
  } catch (error) {
    if (error instanceof VistaImportError) throw error;
    fail("VISTA_JSON_INVALID", `${label} is not valid JSON`, { cause: error });
  }
}

function parseYaml(buffer, yamlImpl = YAML) {
  try {
    const source = Buffer.isBuffer(buffer) ? buffer.toString("utf8") : String(buffer);
    const document = yamlImpl.parseDocument(source, {
      maxAliasCount: 32,
      prettyErrors: false,
      uniqueKeys: true,
    });
    if (document.errors && document.errors.length) throw document.errors[0];
    const value = document.toJSON();
    guardDataTree(value);
    return value;
  } catch (error) {
    if (error instanceof VistaImportError) throw error;
    fail("VISTA_YAML_INVALID", "render_script is not safe, valid YAML", { cause: error });
  }
}

function parseTimestampToken(token) {
  const normalized = String(token || "").trim().toLowerCase();
  if (/^\d+(?:\.\d+)?s$/.test(normalized)) return Number(normalized.slice(0, -1));
  if (/^\d+(?:\.\d+)?$/.test(normalized)) return Number(normalized);
  const parts = normalized.split(":");
  if (parts.length !== 2 && parts.length !== 3) return NaN;
  if (!parts.every((part) => /^\d+(?:\.\d+)?$/.test(part))) return NaN;
  const numbers = parts.map(Number);
  if (numbers.slice(1).some((part) => part >= 60)) return NaN;
  return parts.length === 2
    ? numbers[0] * 60 + numbers[1]
    : numbers[0] * 3600 + numbers[1] * 60 + numbers[2];
}

function parseTimestampedAction(value) {
  if (typeof value !== "string" || !value.trim()) {
    fail("VISTA_ACTION_INVALID", "Timestamped action must be a non-empty string");
  }
  const text = value.trim();
  const bracketed = text.match(/^\[\s*([^\]]+)\s*\]\s*([\s\S]+)$/);
  const secondsForm = bracketed ? null : text.match(/^(\d+(?:\.\d+)?)\s*s(?:ec(?:onds?)?)?\s*[:\-]\s*([\s\S]+)$/i);
  if (!bracketed && !secondsForm) {
    fail("VISTA_ACTION_INVALID", "Action must begin with [MM:SS], [HH:MM:SS], or seconds", { value: text.slice(0, 80) });
  }
  const atSec = parseTimestampToken(bracketed ? bracketed[1] : secondsForm[1]);
  const description = String(bracketed ? bracketed[2] : secondsForm[2]).trim();
  if (!Number.isFinite(atSec) || atSec < 0 || !description || description.length > 32_000) {
    fail("VISTA_ACTION_INVALID", "Timestamped action contains an invalid time or description");
  }
  return Object.freeze({
    at_sec: Math.round(atSec * 1000) / 1000,
    description,
  });
}

function validateManifest(manifest, expectedRevision) {
  exactKeys(
    manifest,
    ["schema", "bundle_id", "dataset_revision", "profile", "privilege", "sample", "files"],
    ["schema", "bundle_id", "dataset_revision", "profile", "privilege", "sample", "files"],
    "manifest",
    "VISTA_MANIFEST_INVALID",
  );
  if (manifest.schema !== SOURCE_SCHEMA || manifest.profile !== "reconstruction") {
    fail("VISTA_MANIFEST_INVALID", "Unsupported curated source schema or profile");
  }
  requireString(manifest.bundle_id, "manifest.bundle_id", { max: 160, pattern: SAFE_SOURCE_ID_RE });
  const revision = requireString(manifest.dataset_revision, "manifest.dataset_revision", { pattern: SAFE_ID_RE });
  if (revision !== expectedRevision) {
    fail("VISTA_IDENTITY_MISMATCH", "Registry revision does not match the source manifest");
  }
  exactKeys(
    manifest.privilege,
    ["classification", "evaluation_input_allowed", "allowed_consumers"],
    ["classification", "evaluation_input_allowed", "allowed_consumers"],
    "manifest.privilege",
    "VISTA_MANIFEST_INVALID",
  );
  if (
    manifest.privilege.classification !== "reconstruction_source"
    || manifest.privilege.evaluation_input_allowed !== false
    || !Array.isArray(manifest.privilege.allowed_consumers)
    || !manifest.privilege.allowed_consumers.length
    || !manifest.privilege.allowed_consumers.includes("vista_importer")
    || new Set(manifest.privilege.allowed_consumers).size !== manifest.privilege.allowed_consumers.length
    || manifest.privilege.allowed_consumers.some((value) => !new Set(["vista_importer", "scene_normalizer", "evaluation_exporter"]).has(value))
  ) {
    fail("VISTA_MANIFEST_INVALID", "Manifest privilege boundary is invalid");
  }

  exactKeys(
    manifest.sample,
    ["visual_id", "case_scope", "scenario_type", "duration_sec", "attempt", "source_row_id"],
    ["visual_id", "case_scope", "scenario_type", "duration_sec", "attempt", "source_row_id"],
    "manifest.sample",
    "VISTA_MANIFEST_INVALID",
  );
  for (const field of ["visual_id", "case_scope", "scenario_type"]) {
    requireString(manifest.sample[field], `manifest.sample.${field}`, { max: 160, pattern: SAFE_ID_RE });
  }
  requireString(manifest.sample.source_row_id, "manifest.sample.source_row_id", { max: 512, pattern: SAFE_SOURCE_ID_RE });
  requireFiniteNumber(manifest.sample.duration_sec, "manifest.sample.duration_sec", { min: 0.001, max: 3600 });
  exactKeys(
    manifest.sample.attempt,
    ["provider", "index", "selected"],
    ["provider", "index", "selected"],
    "manifest.sample.attempt",
    "VISTA_MANIFEST_INVALID",
  );
  requireString(manifest.sample.attempt.provider, "manifest.sample.attempt.provider", { pattern: SAFE_ID_RE });
  const attemptIndex = requireFiniteNumber(manifest.sample.attempt.index, "manifest.sample.attempt.index", { min: 1, max: 1_000_000 });
  if (!Number.isInteger(attemptIndex) || manifest.sample.attempt.selected !== true) {
    fail("VISTA_ATTEMPT_INVALID", "Manifest attempt must be an integer selected attempt");
  }

  if (!Array.isArray(manifest.files) || manifest.files.length !== 3) {
    fail("VISTA_MANIFEST_INVALID", "Manifest must declare exactly the curated render, no-oracle dialogue, and media descriptor files");
  }
  const roles = new Set();
  const paths = new Set();
  manifest.files.forEach((file, index) => {
    const pointer = `manifest.files[${index}]`;
    exactKeys(
      file,
      ["path", "role", "media_type", "sha256", "bytes", "required", "privilege"],
      ["path", "role", "media_type", "sha256", "bytes", "required", "privilege"],
      pointer,
      "VISTA_MANIFEST_INVALID",
    );
    const role = requireString(file.role, `${pointer}.role`);
    if (!new Set(["render_script", "dialogue_no_oracle", "media_descriptor"]).has(role) || roles.has(role)) {
      fail("VISTA_MANIFEST_INVALID", "Manifest file roles must be unique and allowlisted", { pointer });
    }
    roles.add(role);
    const relative = safeRelativePath(file.path, `${pointer}.path`);
    if (paths.has(relative)) fail("VISTA_MANIFEST_INVALID", "Manifest file paths must be unique", { pointer });
    paths.add(relative);
    requireString(file.media_type, `${pointer}.media_type`, { max: 128 });
    if (!SHA256_RE.test(String(file.sha256 || ""))) fail("VISTA_MANIFEST_INVALID", `${pointer}.sha256 is invalid`, { pointer });
    const bytes = requireFiniteNumber(file.bytes, `${pointer}.bytes`, { min: 1, max: MAX_SOURCE_BYTES });
    if (!Number.isInteger(bytes) || file.required !== true) fail("VISTA_MANIFEST_INVALID", `${pointer} must be a required bounded file`, { pointer });
    const expectedPrivilege = role === "dialogue_no_oracle" ? "evaluation_safe" : "reconstruction_only";
    if (file.privilege !== expectedPrivilege) fail("VISTA_MANIFEST_INVALID", `${pointer}.privilege is invalid`, { pointer });
  });
  if (!roles.has("render_script") || !roles.has("dialogue_no_oracle") || !roles.has("media_descriptor")) {
    fail("VISTA_MANIFEST_INVALID", "Manifest is missing a required curated source role");
  }
  return manifest;
}

function validateRequest(request) {
  exactKeys(
    request,
    ["datasetRevision", "sampleId", "attempt", "scenarioType"],
    ["datasetRevision", "sampleId", "attempt"],
    "request",
    "VISTA_REQUEST_INVALID",
  );
  const datasetRevision = requireString(request.datasetRevision, "request.datasetRevision", { pattern: SAFE_ID_RE });
  const sampleId = requireString(request.sampleId, "request.sampleId", { pattern: SAFE_ID_RE });
  const attempt = Number(request.attempt);
  if (!Number.isInteger(attempt) || attempt < 1 || attempt > 1_000_000) {
    fail("VISTA_REQUEST_INVALID", "request.attempt must be a positive integer");
  }
  const scenarioType = request.scenarioType === undefined
    ? undefined
    : requireString(request.scenarioType, "request.scenarioType", { pattern: SAFE_ID_RE });
  return Object.freeze({ datasetRevision, sampleId, attempt, ...(scenarioType ? { scenarioType } : {}) });
}

function validateRequestIdentity(request, manifest) {
  const sample = manifest.sample;
  if (request.sampleId !== sample.visual_id) fail("VISTA_IDENTITY_MISMATCH", "Requested sample does not match the curated manifest");
  if (request.attempt !== sample.attempt.index) fail("VISTA_ATTEMPT_INVALID", "Requested attempt is not the selected curated attempt");
  if (sample.attempt.selected !== true) fail("VISTA_ATTEMPT_INVALID", "Requested attempt has not been selected for import");
  if (request.scenarioType && request.scenarioType !== sample.scenario_type) {
    fail("VISTA_IDENTITY_MISMATCH", "Requested scenario type does not match the curated manifest");
  }
}

function assertNoOracleFields(value, pointer = "dialogue") {
  if (Array.isArray(value)) {
    value.forEach((item, index) => assertNoOracleFields(item, `${pointer}/${index}`));
    return;
  }
  if (!value || typeof value !== "object") return;
  for (const [key, item] of Object.entries(value)) {
    if (ORACLE_KEY_RE.test(key)) {
      fail("VISTA_ORACLE_LEAKAGE", "No-oracle dialogue contains a restricted field", { pointer: `${pointer}/${key}` });
    }
    assertNoOracleFields(item, `${pointer}/${key}`);
  }
}

function validateDialogue(dialogue, manifest) {
  exactKeys(
    dialogue,
    ["schema", "profile", "privilege", "source", "turns"],
    ["schema", "profile", "privilege", "source", "turns"],
    "dialogue",
    "VISTA_DIALOGUE_INVALID",
  );
  assertNoOracleFields(dialogue);
  if (dialogue.schema !== DIALOGUE_SCHEMA || dialogue.profile !== "evaluation_safe") {
    fail("VISTA_DIALOGUE_INVALID", "Dialogue must use the no-oracle evaluation-safe profile");
  }
  exactKeys(dialogue.privilege, ["classification", "evaluation_input_allowed"], ["classification", "evaluation_input_allowed"], "dialogue.privilege", "VISTA_DIALOGUE_INVALID");
  if (dialogue.privilege.classification !== "evaluation_safe" || dialogue.privilege.evaluation_input_allowed !== true) {
    fail("VISTA_DIALOGUE_INVALID", "Dialogue privilege boundary is invalid");
  }
  exactKeys(dialogue.source, ["dataset_revision", "source_row_id", "visual_id", "case_scope", "scenario_type", "attempt"], ["dataset_revision", "source_row_id", "visual_id", "case_scope", "scenario_type", "attempt"], "dialogue.source", "VISTA_DIALOGUE_INVALID");
  const sample = manifest.sample;
  for (const [field, expected] of [["dataset_revision", manifest.dataset_revision], ["source_row_id", sample.source_row_id], ["visual_id", sample.visual_id], ["case_scope", sample.case_scope], ["scenario_type", sample.scenario_type]]) {
    if (dialogue.source[field] !== expected) fail("VISTA_IDENTITY_MISMATCH", `Dialogue ${field} does not match the selected sample`);
  }
  exactKeys(dialogue.source.attempt, ["provider", "index", "selected"], ["provider", "index", "selected"], "dialogue.source.attempt", "VISTA_DIALOGUE_INVALID");
  if (dialogue.source.attempt.provider !== sample.attempt.provider || dialogue.source.attempt.index !== sample.attempt.index || dialogue.source.attempt.selected !== true) {
    fail("VISTA_IDENTITY_MISMATCH", "Dialogue attempt does not match the selected sample");
  }
  if (!Array.isArray(dialogue.turns) || dialogue.turns.length > 200) {
    fail("VISTA_DIALOGUE_INVALID", "Dialogue turns must be a bounded array");
  }
  const turnIds = new Set();
  const turns = dialogue.turns.map((turn, index) => {
    const pointer = `dialogue.turns[${index}]`;
    exactKeys(turn, ["turn_id", "role", "speaker", "text"], ["turn_id", "role", "speaker", "text"], pointer, "VISTA_DIALOGUE_INVALID");
    const turnId = Number(turn.turn_id);
    if (!Number.isInteger(turnId) || turnId < 1 || turnIds.has(turnId)) fail("VISTA_DIALOGUE_INVALID", "Dialogue turn ids must be positive integers and unique", { pointer });
    turnIds.add(turnId);
    const role = requireString(turn.role, `${pointer}.role`, { max: 64 });
    const speaker = requireString(turn.speaker, `${pointer}.speaker`, { max: 64 });
    if (role !== "context" || !new Set(["other_person", "user"]).has(speaker)) {
      fail("VISTA_DIALOGUE_INVALID", "Dialogue role or speaker is not allowlisted", { pointer });
    }
    return Object.freeze({
      turn_id: turnId,
      role,
      speaker,
      text: requireString(turn.text, `${pointer}.text`, { max: 8000 }),
      source_pointer: `/turns/${index}`,
      privilege: "evaluation_safe",
    });
  });
  return Object.freeze(turns);
}

function validateMediaDescriptor(descriptor, manifest) {
  exactKeys(descriptor, ["schema", "profile", "privilege", "source", "media"], ["schema", "profile", "privilege", "source", "media"], "media_descriptor", "VISTA_MEDIA_INVALID");
  if (descriptor.schema !== MEDIA_SCHEMA || descriptor.profile !== "reconstruction") {
    fail("VISTA_MEDIA_INVALID", "Media descriptor schema or profile is invalid");
  }
  exactKeys(descriptor.privilege, ["classification", "evaluation_input_allowed"], ["classification", "evaluation_input_allowed"], "media_descriptor.privilege", "VISTA_MEDIA_INVALID");
  if (descriptor.privilege.classification !== "reconstruction_source" || descriptor.privilege.evaluation_input_allowed !== false) {
    fail("VISTA_MEDIA_INVALID", "Media descriptor privilege boundary is invalid");
  }
  exactKeys(descriptor.source, ["dataset_revision", "source_row_id", "visual_id", "case_scope", "scenario_type", "attempt"], ["dataset_revision", "source_row_id", "visual_id", "case_scope", "scenario_type", "attempt"], "media_descriptor.source", "VISTA_MEDIA_INVALID");
  const sample = manifest.sample;
  for (const [field, expected] of [["dataset_revision", manifest.dataset_revision], ["source_row_id", sample.source_row_id], ["visual_id", sample.visual_id], ["case_scope", sample.case_scope], ["scenario_type", sample.scenario_type]]) {
    if (descriptor.source[field] !== expected) fail("VISTA_IDENTITY_MISMATCH", `Media ${field} does not match the selected sample`);
  }
  exactKeys(descriptor.source.attempt, ["provider", "index", "selected"], ["provider", "index", "selected"], "media_descriptor.source.attempt", "VISTA_MEDIA_INVALID");
  if (descriptor.source.attempt.provider !== sample.attempt.provider || descriptor.source.attempt.index !== sample.attempt.index || descriptor.source.attempt.selected !== true) {
    fail("VISTA_IDENTITY_MISMATCH", "Media attempt does not match the selected sample");
  }
  const media = descriptor.media;
  exactKeys(media, ["media_id", "role", "logical_ref", "media_type", "sha256", "bytes", "duration_sec", "width", "height", "bundled", "integrity_status", "privilege"], ["media_id", "role", "logical_ref", "media_type", "sha256", "bytes", "duration_sec", "width", "height", "bundled", "integrity_status", "privilege"], "media_descriptor.media", "VISTA_MEDIA_INVALID");
  requireString(media.media_id, "media_descriptor.media.media_id", { max: 160, pattern: SAFE_SOURCE_ID_RE });
  safeRelativePath(media.logical_ref, "media_descriptor.media.logical_ref");
  const duration = requireFiniteNumber(media.duration_sec, "media_descriptor.media.duration_sec", { min: 0.001, max: 3600 });
  const bytes = requireFiniteNumber(media.bytes, "media_descriptor.media.bytes", { min: 1, max: Number.MAX_SAFE_INTEGER });
  const width = requireFiniteNumber(media.width, "media_descriptor.media.width", { min: 1, max: 16384 });
  const height = requireFiniteNumber(media.height, "media_descriptor.media.height", { min: 1, max: 16384 });
  if (media.role !== "reference_video" || media.media_type !== "video/mp4" || !SHA256_RE.test(String(media.sha256 || ""))
    || !Number.isSafeInteger(bytes) || !Number.isInteger(width) || !Number.isInteger(height)
    || media.bundled !== false || media.integrity_status !== "recorded_checksum" || media.privilege !== "reconstruction_only") {
    fail("VISTA_MEDIA_INVALID", "Media reference contract is invalid");
  }
  if (Math.abs(duration - Number(sample.duration_sec)) > 0.001) {
    fail("VISTA_DURATION_INVALID", "Media and selected sample durations do not match", {
      manifest_duration_sec: Number(sample.duration_sec),
      media_duration_sec: duration,
    });
  }
  return Object.freeze({
    media_id: media.media_id,
    role: media.role,
    logical_ref: media.logical_ref,
    media_type: media.media_type,
    sha256: media.sha256,
    bytes,
    duration_sec: duration,
    width,
    height,
    bundled: false,
    integrity_status: "recorded_checksum",
    privilege: "reconstruction_only",
  });
}

function validateRenderScript(script, expectedDuration) {
  requireObject(script, "render_script", "VISTA_YAML_INVALID");
  const metadata = requireObject(script.Global_Metadata, "render_script.Global_Metadata", "VISTA_YAML_INVALID");
  const camera = requireObject(script.Camera_Continuity, "render_script.Camera_Continuity", "VISTA_YAML_INVALID");
  const scene = requireObject(script.Scene, "render_script.Scene", "VISTA_YAML_INVALID");
  const duration = requireFiniteNumber(metadata.Duration_sec, "render_script.Global_Metadata.Duration_sec", { min: 0.001, max: 3600 });
  if (Math.abs(duration - expectedDuration) > 0.001) {
    fail("VISTA_DURATION_INVALID", "Verified manifest and render_script durations do not match", {
      manifest_duration_sec: expectedDuration,
      script_duration_sec: duration,
    });
  }
  requireString(metadata.Environment, "render_script.Global_Metadata.Environment", { max: 32_000 });
  requireString(metadata.Lighting, "render_script.Global_Metadata.Lighting", { max: 8000 });
  const perspective = requireString(metadata.Perspective, "render_script.Global_Metadata.Perspective", { max: 256 });
  if (!/(?:first[-_ ]person|egocentric)/i.test(perspective) || /third[-_ ]person/i.test(perspective)) {
    fail("VISTA_CAMERA_INVALID", "VISTA reconstruction requires a verified first-person camera contract");
  }
  for (const key of ["Position", "Height", "Angle", "Motion", "Framing"]) {
    requireString(camera[key], `render_script.Camera_Continuity.${key}`, { max: 32_000 });
  }
  if (!Array.isArray(scene.Key_Visual_Elements) || !scene.Key_Visual_Elements.length || scene.Key_Visual_Elements.length > 200) {
    fail("VISTA_YAML_INVALID", "render_script.Scene.Key_Visual_Elements must be a non-empty bounded array");
  }
  scene.Key_Visual_Elements.forEach((item, index) => requireString(item, `render_script.Scene.Key_Visual_Elements[${index}]`, { max: 8000 }));
  if (!Array.isArray(scene.Actions) || !scene.Actions.length || scene.Actions.length > 500) {
    fail("VISTA_YAML_INVALID", "render_script.Scene.Actions must be a non-empty bounded array");
  }
  if (!Array.isArray(scene.Dialogue)) fail("VISTA_YAML_INVALID", "render_script.Scene.Dialogue must be an array");
  if (scene.Dialogue.length) {
    fail("VISTA_DIALOGUE_INVALID", "Embedded render dialogue is not allowed; use the verified no-oracle join");
  }
  const parsedActions = scene.Actions.map(parseTimestampedAction);
  let previous = -1;
  const seen = new Set();
  parsedActions.forEach((action, index) => {
    if (action.at_sec > duration) fail("VISTA_TIMELINE_INVALID", "Action timestamp exceeds scene duration", { index, at_sec: action.at_sec });
    if (action.at_sec <= previous || seen.has(action.at_sec)) {
      fail("VISTA_TIMELINE_INVALID", "Action timestamps must be strictly ordered and unique", { index, at_sec: action.at_sec });
    }
    seen.add(action.at_sec);
    previous = action.at_sec;
  });
  return { metadata, camera, scene, duration, parsedActions };
}

function slug(value, fallback) {
  const normalized = String(value || "")
    .normalize("NFKD")
    .replace(/[^A-Za-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .toLowerCase()
    .slice(0, 56);
  const safe = normalized || fallback;
  return /^[a-z]/.test(safe) ? safe : `entity_${safe}`;
}

function uniqueId(base, used) {
  let value = base;
  let suffix = 2;
  while (used.has(value)) value = `${base}_${suffix++}`;
  used.add(value);
  return value;
}

function inferAction(description) {
  const text = description.toLowerCase();
  if (BRACE_ACTION_RE.test(text)) return "brace";
  if (/\b(?:drag|pull)/.test(text)) return "drag";
  if (LIFT_FOOT_ACTION_RE.test(text)) return "lift_foot";
  if (/\b(?:pause|wait|stop|hold|hesitat)/.test(text)) return "pause";
  if (/\b(?:look|gaze|glance|view)\b/.test(text)) return "look_at";
  if (/\b(?:pick up|pickup|grasp|take)\b/.test(text)) return "pick_up";
  if (/\b(?:drop|release|put down)\b/.test(text)) return "drop";
  if (/\b(?:walk|approach|move|step toward)\b/.test(text)) return "move_to";
  return "unresolved_action";
}

function inferActionSequence(description) {
  const text = description.toLowerCase();
  const primary = inferAction(description);
  if (primary === "brace" && BRACE_ACTION_RE.test(text) && LIFT_FOOT_ACTION_RE.test(text)) {
    return Object.freeze(["brace", "lift_foot"]);
  }
  return Object.freeze([primary]);
}

function inferTarget(description, entities) {
  const text = description.toLowerCase();
  const priority = ["box", "chair", "ladder", "stool", "cabinet", "shelf"];
  for (const term of priority) {
    if (!text.includes(term)) continue;
    const match = entities.find((entity) => entity.semantic_query.toLowerCase().includes(term));
    if (match) return match.id;
  }
  return null;
}

function normalizeEntities(scene) {
  const used = new Set();
  return scene.Key_Visual_Elements.map((query, index) => {
    const semanticQuery = String(query).trim();
    if (/^(?:cube|basic geometry|primitive cube)$/i.test(semanticQuery)) {
      fail("VISTA_ASSET_FALLBACK_FORBIDDEN", "Basic geometry is not a valid VISTA semantic entity", { index });
    }
    const id = uniqueId(slug(semanticQuery, `entity_${String(index + 1).padStart(3, "0")}`), used);
    return Object.freeze({
      id,
      semantic_query: semanticQuery,
      required: true,
      asset_binding: null,
      source_pointer: `/Scene/Key_Visual_Elements/${index}`,
      privilege: "reconstruction_only",
    });
  });
}

function normalizeCamera(metadata, camera) {
  const forbidden = [];
  const addConstraints = (items) => {
    if (!Array.isArray(items)) return;
    for (const item of items) {
      const value = String(item || "").trim();
      if (value && !forbidden.includes(value)) forbidden.push(value);
    }
  };
  addConstraints(metadata.Viewpoint_Contract && metadata.Viewpoint_Contract.Forbidden_Views);
  addConstraints(camera.Forbidden_Camera_Behaviors);
  return Object.freeze({
    perspective: "first_person",
    position: String(camera.Position).trim(),
    height: String(camera.Height).trim(),
    angle: String(camera.Angle).trim(),
    motion: String(camera.Motion).trim(),
    framing: String(camera.Framing).trim(),
    constraints: Object.freeze(forbidden),
  });
}

function sourceRowId(manifest, dialogueRaw) {
  if (manifest.sample.source_row_id) return String(manifest.sample.source_row_id);
  if (dialogueRaw.source && dialogueRaw.source.source_row_id) return String(dialogueRaw.source.source_row_id);
  const sample = manifest.sample;
  return `${sample.visual_id}::${sample.case_scope}::${sample.scenario_type}::${sample.attempt.provider}::attempt_${String(sample.attempt.index).padStart(3, "0")}`;
}

function normalizeScene({ manifest, verifiedFiles, script, dialogue, dialogueRaw, media, importerVersion, sourceChecksum }) {
  const checked = validateRenderScript(script, Number(manifest.sample.duration_sec));
  const entities = normalizeEntities(checked.scene);
  const unresolved = entities.map((entity) => Object.freeze({
    mapping_id: `unresolved-asset-${entity.id}`,
    kind: "asset",
    source_pointer: entity.source_pointer,
    reason_code: "no_asset_match",
    message: `Semantic asset resolution has not selected a verified real asset for entity '${entity.id}'`,
    blocking: entity.required === true,
    candidates: Object.freeze([]),
  }));
  const timeline = checked.parsedActions.flatMap((parsed, index) => {
    const actions = inferActionSequence(parsed.description);
    const compound = actions.length > 1;
    const beat = String(index + 1).padStart(4, "0");
    return actions.map((action) => {
      const eventId = compound ? `beat-${beat}-${action}` : `beat-${beat}`;
      const sourcePointer = compound ? `/Scene/Actions/${index}/${action}` : `/Scene/Actions/${index}`;
      if (UNSUPPORTED_RUNTIME_ACTIONS.has(action)) {
        unresolved.push(Object.freeze({
          mapping_id: `unresolved-action-${eventId.slice("beat-".length)}`,
          kind: "action",
          source_pointer: sourcePointer,
          reason_code: "unsupported_action",
          message: `Action '${action}' requires an explicit verified runtime adapter`,
          blocking: true,
          candidates: Object.freeze([]),
        }));
      }
      return Object.freeze({
        event_id: eventId,
        at_sec: parsed.at_sec,
        action,
        actor_id: "camera_wearer",
        target_id: inferTarget(parsed.description, entities),
        parameters: Object.freeze({}),
        description: parsed.description,
        source_pointer: sourcePointer,
        privilege: "reconstruction_only",
      });
    });
  });
  const files = [...verifiedFiles]
    .sort((a, b) => a.role.localeCompare(b.role) || a.path.localeCompare(b.path))
    .map((file) => Object.freeze({
      role: file.role,
      path: file.path,
      media_type: file.media_type,
      sha256: file.sha256,
      bytes: file.bytes,
      required: true,
      privilege: file.privilege,
    }));
  const sample = manifest.sample;
  const sceneSpec = {
    schema: SCENE_SCHEMA,
    scene_id: `${sample.visual_id}@${sourceChecksum.slice(0, 16)}`,
    profile: "reconstruction",
    privilege: Object.freeze({
      profile: "reconstruction",
      classification: "reconstruction_only",
      evaluation_input_allowed: false,
      field_labels: Object.freeze({
        render_script: "reconstruction_only",
        environment: "reconstruction_only",
        timeline: "reconstruction_only",
        dialogue: "evaluation_safe",
      }),
    }),
    provenance: Object.freeze({
      importer_name: IMPORTER_NAME,
      importer_version: importerVersion,
      source_checksum: sourceChecksum,
      bundle_id: manifest.bundle_id,
    }),
    source: Object.freeze({
      profile: "reconstruction",
      classification: "reconstruction_source",
      dataset_revision: manifest.dataset_revision,
      source_row_id: sourceRowId(manifest, dialogueRaw),
      visual_id: sample.visual_id,
      case_scope: sample.case_scope,
      scenario_type: sample.scenario_type,
      attempt: Object.freeze({ provider: sample.attempt.provider, index: sample.attempt.index, selected: true }),
      files: Object.freeze(files),
      media,
      source_checksum: sourceChecksum,
    }),
    duration_sec: checked.duration,
    environment: Object.freeze({
      description: String(checked.metadata.Environment).trim(),
      lighting: String(checked.metadata.Lighting).trim(),
    }),
    camera: normalizeCamera(checked.metadata, checked.camera),
    entities: Object.freeze(entities),
    relations: Object.freeze([]),
    timeline: Object.freeze(timeline),
    dialogue,
    unresolved: Object.freeze(unresolved),
  };
  return Object.freeze(validateSceneSpec(sceneSpec));
}

function validateAssetBinding(binding, pointer, expectedSnapshot) {
  exactKeys(binding, ["snapshot_id", "asset_id", "ue_path", "confidence"], ["snapshot_id", "asset_id", "ue_path", "confidence"], pointer, "VISTA_SCENE_INVALID");
  const snapshotId = requireString(binding.snapshot_id, `${pointer}.snapshot_id`, { max: 160, pattern: SAFE_SOURCE_ID_RE });
  requireString(binding.asset_id, `${pointer}.asset_id`, { max: 160, pattern: SAFE_SOURCE_ID_RE });
  const uePath = requireString(binding.ue_path, `${pointer}.ue_path`, { max: 512 });
  const confidence = requireFiniteNumber(binding.confidence, `${pointer}.confidence`, { min: 0, max: 1 });
  if (expectedSnapshot && snapshotId !== expectedSnapshot) fail("VISTA_SCENE_INVALID", "Asset binding snapshot is inconsistent", { pointer });
  if (!/^\/Game\/[A-Za-z0-9_./-]+$/.test(uePath) || uePath.includes("..") || /\/(?:SM_)?(?:Cube|Plane|Sphere|Cylinder|Cone)(?:\.|$)/i.test(uePath)) {
    fail("VISTA_SCENE_INVALID", "Asset binding must reference a real /Game asset, not basic geometry", { pointer });
  }
  return { snapshot_id: snapshotId, asset_id: binding.asset_id, ue_path: uePath, confidence };
}

function sameAssetBinding(left, right) {
  return Boolean(left && right
    && left.snapshot_id === right.snapshot_id
    && left.asset_id === right.asset_id
    && left.ue_path === right.ue_path
    && left.confidence === right.confidence);
}

function validateAssetResolution(resolution, entity, pointer) {
  exactKeys(resolution, ["schema", "snapshot_id", "query", "min_confidence", "candidates", "selected_binding", "selected_by", "manual_override"], ["schema", "snapshot_id", "query", "min_confidence", "candidates", "selected_binding", "selected_by", "manual_override"], pointer, "VISTA_SCENE_INVALID");
  if (resolution.schema !== "vista-asset-resolution/v1") fail("VISTA_SCENE_INVALID", "Asset resolution schema is invalid", { pointer });
  const snapshotId = requireString(resolution.snapshot_id, `${pointer}.snapshot_id`, { max: 160, pattern: SAFE_SOURCE_ID_RE });
  const query = requireString(resolution.query, `${pointer}.query`, { max: 1000 });
  const minConfidence = requireFiniteNumber(resolution.min_confidence, `${pointer}.min_confidence`, { min: 0, max: 1 });
  if (query !== entity.semantic_query || !Array.isArray(resolution.candidates) || resolution.candidates.length > 40) {
    fail("VISTA_SCENE_INVALID", "Asset resolution does not match its entity query", { pointer });
  }
  const candidateKeys = new Set();
  let previous = null;
  resolution.candidates.forEach((candidate, index) => {
    const candidatePointer = `${pointer}.candidates[${index}]`;
    exactKeys(candidate, ["rank", "snapshot_id", "asset_id", "ue_path", "confidence", "origin"], ["rank", "snapshot_id", "asset_id", "ue_path", "confidence", "origin"], candidatePointer, "VISTA_SCENE_INVALID");
    if (candidate.rank !== index + 1 || !new Set(["search", "manual_override"]).has(candidate.origin)) {
      fail("VISTA_SCENE_INVALID", "Asset candidates must have stable ranks and origins", { pointer: candidatePointer });
    }
    const normalized = validateAssetBinding({
      snapshot_id: candidate.snapshot_id,
      asset_id: candidate.asset_id,
      ue_path: candidate.ue_path,
      confidence: candidate.confidence,
    }, candidatePointer, snapshotId);
    const key = `${normalized.asset_id}\u0000${normalized.ue_path}`;
    if (candidateKeys.has(key)) fail("VISTA_SCENE_INVALID", "Asset candidates must be unique", { pointer: candidatePointer });
    candidateKeys.add(key);
    if (previous && (normalized.confidence > previous.confidence
      || (normalized.confidence === previous.confidence && normalized.asset_id < previous.asset_id))) {
      fail("VISTA_SCENE_INVALID", "Asset candidates must be deterministically ordered", { pointer: candidatePointer });
    }
    previous = normalized;
  });

  const selected = resolution.selected_binding === null
    ? null
    : validateAssetBinding(resolution.selected_binding, `${pointer}.selected_binding`, snapshotId);
  if (selected === null) {
    if (resolution.selected_by !== null || resolution.manual_override !== null || entity.asset_binding !== null) {
      fail("VISTA_SCENE_INVALID", "Unselected asset resolution contains a binding or override", { pointer });
    }
    return;
  }
  if (!new Set(["automatic", "manual_override"]).has(resolution.selected_by)) {
    fail("VISTA_SCENE_INVALID", "Selected asset resolution is missing its selection mode", { pointer });
  }
  const entityBinding = validateAssetBinding(entity.asset_binding, `${pointer}.entity_asset_binding`, snapshotId);
  if (!sameAssetBinding(selected, entityBinding)) fail("VISTA_SCENE_INVALID", "Selected and entity asset bindings differ", { pointer });
  if (resolution.selected_by === "automatic") {
    if (resolution.manual_override !== null || selected.confidence < minConfidence
      || !resolution.candidates.some((candidate) => sameAssetBinding(candidate, selected))) {
      fail("VISTA_SCENE_INVALID", "Automatic asset selection is inconsistent with its candidates", { pointer });
    }
    return;
  }
  const override = resolution.manual_override;
  exactKeys(override, ["confirmed", "reason", "snapshot_id", "asset_id", "ue_path", "confidence"], ["confirmed", "reason", "snapshot_id", "asset_id", "ue_path", "confidence"], `${pointer}.manual_override`, "VISTA_SCENE_INVALID");
  if (override.confirmed !== true) fail("VISTA_SCENE_INVALID", "Manual asset override is not confirmed", { pointer });
  requireString(override.reason, `${pointer}.manual_override.reason`, { max: 500 });
  const normalizedOverride = validateAssetBinding({
    snapshot_id: override.snapshot_id,
    asset_id: override.asset_id,
    ue_path: override.ue_path,
    confidence: override.confidence,
  }, `${pointer}.manual_override`, snapshotId);
  if (!sameAssetBinding(selected, normalizedOverride)) fail("VISTA_SCENE_INVALID", "Manual override and selected binding differ", { pointer });
}

function validateSceneSpec(scene) {
  exactKeys(scene, TOP_LEVEL_SCENE_KEYS, TOP_LEVEL_SCENE_KEYS, "scene", "VISTA_SCENE_INVALID");
  if (scene.schema !== SCENE_SCHEMA || scene.profile !== "reconstruction") fail("VISTA_SCENE_INVALID", "Unsupported SceneSpec schema or profile");
  const sceneId = requireString(scene.scene_id, "scene.scene_id", { max: 256 });

  exactKeys(scene.privilege, ["profile", "classification", "evaluation_input_allowed", "field_labels"], ["profile", "classification", "evaluation_input_allowed", "field_labels"], "scene.privilege", "VISTA_SCENE_INVALID");
  if (scene.privilege.profile !== "reconstruction" || scene.privilege.classification !== "reconstruction_only" || scene.privilege.evaluation_input_allowed !== false) {
    fail("VISTA_SCENE_INVALID", "Scene privilege boundary is invalid");
  }
  exactKeys(scene.privilege.field_labels, ["render_script", "environment", "timeline", "dialogue"], ["render_script", "environment", "timeline", "dialogue"], "scene.privilege.field_labels", "VISTA_SCENE_INVALID");
  if (scene.privilege.field_labels.render_script !== "reconstruction_only" || scene.privilege.field_labels.environment !== "reconstruction_only" || scene.privilege.field_labels.timeline !== "reconstruction_only" || scene.privilege.field_labels.dialogue !== "evaluation_safe") {
    fail("VISTA_SCENE_INVALID", "Scene field privilege labels are invalid");
  }

  exactKeys(scene.provenance, ["importer_name", "importer_version", "source_checksum", "bundle_id"], ["importer_name", "importer_version", "source_checksum", "bundle_id"], "scene.provenance", "VISTA_SCENE_INVALID");
  if (scene.provenance.importer_name !== IMPORTER_NAME || !SHA256_RE.test(String(scene.provenance.source_checksum || ""))) fail("VISTA_SCENE_INVALID", "Scene provenance is invalid");
  requireString(scene.provenance.importer_version, "scene.provenance.importer_version", { max: 64 });
  requireString(scene.provenance.bundle_id, "scene.provenance.bundle_id", { max: 160, pattern: SAFE_SOURCE_ID_RE });

  exactKeys(scene.source, ["profile", "classification", "dataset_revision", "source_row_id", "visual_id", "case_scope", "scenario_type", "attempt", "files", "media", "source_checksum"], ["profile", "classification", "dataset_revision", "source_row_id", "visual_id", "case_scope", "scenario_type", "attempt", "files", "media", "source_checksum"], "scene.source", "VISTA_SCENE_INVALID");
  if (scene.source.profile !== "reconstruction" || scene.source.classification !== "reconstruction_source" || scene.source.source_checksum !== scene.provenance.source_checksum) fail("VISTA_SCENE_INVALID", "Scene source provenance is inconsistent");
  for (const field of ["dataset_revision", "visual_id", "case_scope", "scenario_type"]) requireString(scene.source[field], `scene.source.${field}`, { max: 160, pattern: SAFE_ID_RE });
  requireString(scene.source.source_row_id, "scene.source.source_row_id", { max: 512 });
  if (!sceneId.startsWith(`${scene.source.visual_id}@`) || !/^[A-Za-z0-9._-]+@[a-f0-9]{12,64}$/.test(sceneId)) fail("VISTA_SCENE_INVALID", "scene_id does not match source identity");
  exactKeys(scene.source.attempt, ["provider", "index", "selected"], ["provider", "index", "selected"], "scene.source.attempt", "VISTA_SCENE_INVALID");
  requireString(scene.source.attempt.provider, "scene.source.attempt.provider", { pattern: SAFE_ID_RE });
  if (!Number.isInteger(scene.source.attempt.index) || scene.source.attempt.index < 1 || scene.source.attempt.selected !== true) fail("VISTA_SCENE_INVALID", "Scene source attempt is invalid");
  if (!Array.isArray(scene.source.files) || scene.source.files.length !== 3) fail("VISTA_SCENE_INVALID", "Scene must retain the three verified source references");
  scene.source.files.forEach((file, index) => {
    exactKeys(file, ["role", "path", "media_type", "sha256", "bytes", "required", "privilege"], ["role", "path", "media_type", "sha256", "bytes", "required", "privilege"], `scene.source.files[${index}]`, "VISTA_SCENE_INVALID");
    safeRelativePath(file.path, `scene.source.files[${index}].path`);
    if (!SHA256_RE.test(file.sha256) || !Number.isInteger(file.bytes) || file.bytes < 1 || file.required !== true) fail("VISTA_SCENE_INVALID", "Scene source file evidence is invalid", { index });
  });
  const sourceMedia = scene.source.media;
  exactKeys(sourceMedia, ["media_id", "role", "logical_ref", "media_type", "sha256", "bytes", "duration_sec", "width", "height", "bundled", "integrity_status", "privilege"], ["media_id", "role", "logical_ref", "media_type", "sha256", "bytes", "duration_sec", "width", "height", "bundled", "integrity_status", "privilege"], "scene.source.media", "VISTA_SCENE_INVALID");
  safeRelativePath(sourceMedia.logical_ref, "scene.source.media.logical_ref");
  if (sourceMedia.role !== "reference_video" || sourceMedia.media_type !== "video/mp4" || !SHA256_RE.test(String(sourceMedia.sha256 || ""))
    || !Number.isSafeInteger(sourceMedia.bytes) || sourceMedia.bytes < 1 || !Number.isInteger(sourceMedia.width) || !Number.isInteger(sourceMedia.height)
    || sourceMedia.bundled !== false || sourceMedia.integrity_status !== "recorded_checksum" || sourceMedia.privilege !== "reconstruction_only") {
    fail("VISTA_SCENE_INVALID", "Scene media reference is invalid");
  }

  const duration = requireFiniteNumber(scene.duration_sec, "scene.duration_sec", { min: 0.001, max: 3600 });
  if (Math.abs(Number(sourceMedia.duration_sec) - duration) > 0.001) fail("VISTA_SCENE_INVALID", "Scene and media durations differ");
  exactKeys(scene.environment, ["description", "lighting"], ["description", "lighting"], "scene.environment", "VISTA_SCENE_INVALID");
  requireString(scene.environment.description, "scene.environment.description", { max: 32_000 });
  requireString(scene.environment.lighting, "scene.environment.lighting", { max: 8000 });
  exactKeys(scene.camera, ["perspective", "position", "height", "angle", "motion", "framing", "constraints"], ["perspective", "position", "height", "angle", "motion", "framing", "constraints"], "scene.camera", "VISTA_SCENE_INVALID");
  if (scene.camera.perspective !== "first_person") fail("VISTA_SCENE_INVALID", "Scene camera must be first-person");
  for (const field of ["position", "height", "angle", "motion", "framing"]) requireString(scene.camera[field], `scene.camera.${field}`, { max: 32_000 });
  if (!Array.isArray(scene.camera.constraints) || scene.camera.constraints.some((item) => typeof item !== "string" || !item.trim())) fail("VISTA_SCENE_INVALID", "Scene camera constraints are invalid");

  if (!Array.isArray(scene.entities) || !scene.entities.length || scene.entities.length > 200) fail("VISTA_SCENE_INVALID", "Scene entities are invalid");
  const entityIds = new Set();
  scene.entities.forEach((entity, index) => {
    const pointer = `scene.entities[${index}]`;
    exactKeys(entity, ["id", "semantic_query", "required", "asset_binding", "asset_resolution", "source_pointer", "privilege"], ["id", "semantic_query", "required", "asset_binding", "source_pointer", "privilege"], pointer, "VISTA_SCENE_INVALID");
    const id = requireString(entity.id, `${pointer}.id`, { pattern: SAFE_ID_RE });
    if (entityIds.has(id)) fail("VISTA_SCENE_INVALID", "Entity ids must be unique", { pointer });
    entityIds.add(id);
    const query = requireString(entity.semantic_query, `${pointer}.semantic_query`, { max: 8000 });
    if (/^(?:cube|basic geometry|primitive cube)$/i.test(query) || entity.required !== true || entity.privilege !== "reconstruction_only") fail("VISTA_SCENE_INVALID", "Entity violates fail-closed asset or privilege policy", { pointer });
    if (entity.asset_resolution === undefined) {
      if (entity.asset_binding !== null) fail("VISTA_SCENE_INVALID", "Unresolved entity cannot contain an asset binding", { pointer });
    } else {
      validateAssetResolution(entity.asset_resolution, entity, `${pointer}.asset_resolution`);
    }
    requireString(entity.source_pointer, `${pointer}.source_pointer`, { max: 512 });
  });

  if (!Array.isArray(scene.relations)) fail("VISTA_SCENE_INVALID", "Scene relations must be an array");
  if (scene.relations.length) fail("VISTA_SCENE_INVALID", "This importer version does not emit unverified inferred relations");
  if (!Array.isArray(scene.timeline) || !scene.timeline.length || scene.timeline.length > 500) fail("VISTA_SCENE_INVALID", "Scene timeline is invalid");
  const eventIds = new Set();
  let previous = -1;
  scene.timeline.forEach((event, index) => {
    const pointer = `scene.timeline[${index}]`;
    exactKeys(event, ["event_id", "at_sec", "action", "actor_id", "target_id", "parameters", "description", "source_pointer", "privilege"], ["event_id", "at_sec", "action", "actor_id", "target_id", "parameters", "description", "source_pointer", "privilege"], pointer, "VISTA_SCENE_INVALID");
    const eventId = requireString(event.event_id, `${pointer}.event_id`, { pattern: SAFE_ID_RE });
    if (eventIds.has(eventId)) fail("VISTA_SCENE_INVALID", "Timeline event ids must be unique", { pointer });
    eventIds.add(eventId);
    const atSec = requireFiniteNumber(event.at_sec, `${pointer}.at_sec`, { min: 0, max: duration });
    if (atSec < previous) fail("VISTA_SCENE_INVALID", "Timeline timestamps must be nondecreasing", { pointer });
    previous = atSec;
    requireString(event.action, `${pointer}.action`, { pattern: SAFE_ID_RE });
    requireString(event.actor_id, `${pointer}.actor_id`, { pattern: SAFE_ID_RE });
    if (event.target_id !== null && !entityIds.has(event.target_id)) fail("VISTA_SCENE_INVALID", "Timeline target does not reference a scene entity", { pointer });
    requireObject(event.parameters, `${pointer}.parameters`, "VISTA_SCENE_INVALID");
    requireString(event.description, `${pointer}.description`, { max: 32_000 });
    requireString(event.source_pointer, `${pointer}.source_pointer`, { max: 512 });
    if (event.privilege !== "reconstruction_only") fail("VISTA_SCENE_INVALID", "Timeline privilege is invalid", { pointer });
  });

  validateNormalizedDialogue(scene.dialogue);
  if (!Array.isArray(scene.unresolved) || scene.unresolved.length > 500) fail("VISTA_SCENE_INVALID", "Scene unresolved mappings are invalid");
  scene.unresolved.forEach((item, index) => {
    const pointer = `scene.unresolved[${index}]`;
    exactKeys(item, ["mapping_id", "kind", "source_pointer", "reason_code", "message", "blocking", "candidates"], ["mapping_id", "kind", "source_pointer", "reason_code", "message", "blocking", "candidates"], pointer, "VISTA_SCENE_INVALID");
    requireString(item.mapping_id, `${pointer}.mapping_id`, { pattern: SAFE_ID_RE });
    requireString(item.kind, `${pointer}.kind`, { pattern: SAFE_ID_RE });
    requireString(item.source_pointer, `${pointer}.source_pointer`, { max: 512 });
    requireString(item.reason_code, `${pointer}.reason_code`, { pattern: SAFE_ID_RE });
    requireString(item.message, `${pointer}.message`, { max: 8000 });
    if (typeof item.blocking !== "boolean" || !Array.isArray(item.candidates)) fail("VISTA_SCENE_INVALID", "Unresolved mapping shape is invalid", { pointer });
  });
  const unresolvedAssetIds = new Set(scene.unresolved
    .filter((item) => item.kind === "asset" && item.mapping_id.startsWith("unresolved-asset-"))
    .map((item) => item.mapping_id.slice("unresolved-asset-".length)));
  scene.entities.forEach((entity) => {
    if (entity.asset_resolution === undefined) return;
    const unresolved = unresolvedAssetIds.has(entity.id);
    if ((entity.asset_binding === null) !== unresolved) {
      fail("VISTA_SCENE_INVALID", "Resolved asset state and unresolved mappings are inconsistent", { entity_id: entity.id });
    }
  });
  return scene;
}

function validateNormalizedDialogue(dialogue) {
  if (!Array.isArray(dialogue)) fail("VISTA_SCENE_INVALID", "Scene dialogue must be an array");
  const ids = new Set();
  dialogue.forEach((turn, index) => {
    const pointer = `scene.dialogue[${index}]`;
    exactKeys(turn, ["turn_id", "role", "speaker", "text", "source_pointer", "privilege"], ["turn_id", "role", "speaker", "text", "source_pointer", "privilege"], pointer, "VISTA_SCENE_INVALID");
    const id = Number(turn.turn_id);
    if (!Number.isInteger(id) || id < 1 || ids.has(id) || turn.role !== "context" || !new Set(["other_person", "user"]).has(turn.speaker) || turn.privilege !== "evaluation_safe") fail("VISTA_SCENE_INVALID", "Scene dialogue turn is invalid", { pointer });
    ids.add(id);
    requireString(turn.text, `${pointer}.text`, { max: 8000 });
    requireString(turn.source_pointer, `${pointer}.source_pointer`, { max: 512 });
  });
  assertNoOracleFields(dialogue, "scene/dialogue");
}

function exportEvaluationSafeScene(scene) {
  validateSceneSpec(scene);
  const output = {
    schema: EVALUATION_SCHEMA,
    scene_id: scene.scene_id,
    profile: "evaluation_safe",
    privilege: {
      profile: "evaluation_safe",
      classification: "evaluation_safe",
      evaluation_input_allowed: true,
      field_labels: { dialogue: "evaluation_safe" },
    },
    provenance: {
      importer_name: scene.provenance.importer_name,
      importer_version: scene.provenance.importer_version,
      source_checksum: scene.provenance.source_checksum,
      bundle_id: scene.provenance.bundle_id,
    },
    source: {
      dataset_revision: scene.source.dataset_revision,
      source_row_id: scene.source.source_row_id,
      visual_id: scene.source.visual_id,
      case_scope: scene.source.case_scope,
      scenario_type: scene.source.scenario_type,
      attempt: {
        provider: scene.source.attempt.provider,
        index: scene.source.attempt.index,
        selected: true,
      },
    },
    duration_sec: scene.duration_sec,
    dialogue: scene.dialogue.map((turn) => ({
        turn_id: turn.turn_id,
        role: turn.role,
        speaker: turn.speaker,
        text: turn.text,
        source_pointer: turn.source_pointer,
        privilege: "evaluation_safe",
      })),
  };
  assertNoOracleFields(output);
  return output;
}

function normalizeRegistryEntry(entry, revision) {
  if (typeof entry === "string") return { root: entry, manifestPath: "manifest.json" };
  if (!isPlainObject(entry)) fail("VISTA_REVISION_NOT_ALLOWED", "Dataset revision is not configured", { dataset_revision: revision });
  const root = entry.root || entry.bundleRoot;
  if (typeof root !== "string" || !root.trim()) fail("VISTA_REGISTRY_INVALID", "Configured bundle root is invalid", { dataset_revision: revision });
  const manifestPath = entry.manifestPath || entry.manifest || "manifest.json";
  if (typeof manifestPath !== "string") fail("VISTA_REGISTRY_INVALID", "Configured manifest path is invalid", { dataset_revision: revision });
  return {
    root,
    manifestPath,
  };
}

function registryLookup(registry, revision) {
  if (typeof registry === "function") return registry(revision);
  if (registry instanceof Map) return registry.get(revision);
  if (isPlainObject(registry)) return registry[revision];
  fail("VISTA_REGISTRY_INVALID", "VISTA importer requires an operator-configured revision registry");
}

function createVistaImporter(options = {}) {
  const registry = options.registry || options.bundleRegistry;
  if (!registry) fail("VISTA_REGISTRY_INVALID", "VISTA importer requires an operator-configured revision registry");
  const readFile = options.readFile || fs.promises.readFile.bind(fs.promises);
  const realpath = options.realpath || fs.promises.realpath.bind(fs.promises);
  const stat = options.stat || fs.promises.stat.bind(fs.promises);
  const yamlImpl = options.yaml || YAML;
  const assetResolver = options.assetResolver || null;
  if (assetResolver && typeof assetResolver.resolve !== "function") {
    fail("VISTA_CONFIG_INVALID", "options.assetResolver must expose resolve(scene)");
  }
  const importerVersion = requireString(options.importerVersion || IMPORTER_VERSION, "options.importerVersion", { max: 64 });
  const maxManifestBytes = Number(options.maxManifestBytes || MAX_MANIFEST_BYTES);
  const maxSourceBytes = Number(options.maxSourceBytes || MAX_SOURCE_BYTES);
  if (!Number.isInteger(maxManifestBytes) || maxManifestBytes < 1024 || !Number.isInteger(maxSourceBytes) || maxSourceBytes < 1024) {
    fail("VISTA_CONFIG_INVALID", "Importer byte limits are invalid");
  }

  async function resolveEntry(revision) {
    let raw;
    try {
      raw = await registryLookup(registry, revision);
    } catch (error) {
      if (error instanceof VistaImportError) throw error;
      fail("VISTA_REGISTRY_INVALID", "Dataset registry lookup failed", { dataset_revision: revision, cause: error });
    }
    if (!raw) fail("VISTA_REVISION_NOT_ALLOWED", "Dataset revision is not allowlisted", { dataset_revision: revision });
    return normalizeRegistryEntry(raw, revision);
  }

  async function safeRoot(entry) {
    try {
      const configured = path.resolve(entry.root);
      const actual = await realpath(configured);
      const metadata = await stat(actual);
      if (!metadata.isDirectory()) fail("VISTA_REGISTRY_INVALID", "Configured bundle root is not a directory");
      return actual;
    } catch (error) {
      if (error instanceof VistaImportError) throw error;
      fail("VISTA_SOURCE_UNAVAILABLE", "Configured curated bundle is unavailable", { cause: error });
    }
  }

  async function readContained(root, relative, limit, pointer) {
    const configured = resolveContained(root, relative, pointer);
    try {
      const actual = await realpath(configured);
      const prefix = root.endsWith(path.sep) ? root : `${root}${path.sep}`;
      if (actual !== root && !actual.startsWith(prefix)) fail("VISTA_PATH_INVALID", `${pointer} resolves outside the curated bundle`, { pointer });
      const metadata = await stat(actual);
      if (!metadata.isFile() || metadata.size < 1 || metadata.size > limit) fail("VISTA_SOURCE_SIZE_INVALID", `${pointer} is not a bounded regular file`, { pointer });
      const value = await readFile(actual);
      const buffer = Buffer.isBuffer(value) ? value : Buffer.from(value);
      if (buffer.length !== metadata.size || buffer.length > limit) fail("VISTA_SOURCE_SIZE_INVALID", `${pointer} changed while being read`, { pointer });
      return buffer;
    } catch (error) {
      if (error instanceof VistaImportError) throw error;
      fail("VISTA_SOURCE_UNAVAILABLE", `${pointer} could not be read`, { pointer, cause: error });
    }
  }

  async function loadBundle(requestInput) {
    const request = validateRequest(requestInput);
    const entry = await resolveEntry(request.datasetRevision);
    const root = await safeRoot(entry);
    const manifestRelative = safeRelativePath(entry.manifestPath, "registry.manifestPath");
    const manifestBuffer = await readContained(root, manifestRelative, maxManifestBytes, "manifest");
    const manifest = validateManifest(parseJson(manifestBuffer, "manifest"), request.datasetRevision);
    validateRequestIdentity(request, manifest);

    const verifiedFiles = [];
    const contents = new Map();
    for (const declared of manifest.files) {
      if (declared.bytes > maxSourceBytes) fail("VISTA_SOURCE_SIZE_INVALID", "Declared source exceeds configured importer limit", { role: declared.role });
      const buffer = await readContained(root, declared.path, maxSourceBytes, `manifest file '${declared.role}'`);
      const digest = sha256(buffer);
      if (buffer.length !== declared.bytes || digest !== declared.sha256) {
        fail("VISTA_CHECKSUM_MISMATCH", "Curated source checksum or size does not match the manifest", { role: declared.role });
      }
      verifiedFiles.push({ ...declared });
      contents.set(declared.role, buffer);
    }

    const sourceChecksum = sha256(canonicalize({
      schema: SOURCE_SCHEMA,
      bundle_id: manifest.bundle_id,
      dataset_revision: manifest.dataset_revision,
      sample: manifest.sample,
      manifest_sha256: sha256(manifestBuffer),
      files: verifiedFiles
        .map((file) => ({ role: file.role, path: file.path, sha256: file.sha256, bytes: file.bytes, privilege: file.privilege }))
        .sort((a, b) => a.role.localeCompare(b.role) || a.path.localeCompare(b.path)),
    }));
    const dialogueRaw = parseJson(contents.get("dialogue_no_oracle"), "no-oracle dialogue");
    const dialogue = validateDialogue(dialogueRaw, manifest);
    const mediaRaw = contents.has("media_descriptor")
      ? parseJson(contents.get("media_descriptor"), "media descriptor")
      : null;
    if (!mediaRaw) fail("VISTA_MEDIA_INVALID", "A verified media descriptor is required for VISTA reconstruction");
    const media = validateMediaDescriptor(mediaRaw, manifest);
    const script = parseYaml(contents.get("render_script"), yamlImpl);
    return { request, manifest, verifiedFiles, script, dialogue, dialogueRaw, media, sourceChecksum };
  }

  async function preview(request) {
    const bundle = await loadBundle(request);
    const scene = normalizeScene({ ...bundle, importerVersion });
    if (!assetResolver) return scene;
    try {
      return validateSceneSpec(await assetResolver.resolve(scene));
    } catch (error) {
      if (error instanceof VistaImportError) throw error;
      const resolverCode = error && typeof error.code === "string" && /^VISTA_[A-Z0-9_]+$/.test(error.code)
        ? error.code
        : "VISTA_ASSET_RESOLUTION_FAILED";
      fail(resolverCode, "VISTA semantic asset resolution failed", {
        retryable: Boolean(error && error.retryable),
        cause: error,
      });
    }
  }

  async function validateBundle(request) {
    const scene = await preview(request);
    return Object.freeze({
      valid: true,
      schema: SOURCE_SCHEMA,
      dataset_revision: scene.source.dataset_revision,
      sample_id: scene.source.visual_id,
      attempt: scene.source.attempt.index,
      source_checksum: scene.source.source_checksum,
    });
  }

  return Object.freeze({
    version: importerVersion,
    preview,
    importSample: preview,
    validateBundle,
    exportEvaluationSafe: exportEvaluationSafeScene,
  });
}

module.exports = {
  createVistaImporter,
  parseTimestampedAction,
  validateSceneSpec,
  exportEvaluationSafeScene,
  VistaImportError,
};

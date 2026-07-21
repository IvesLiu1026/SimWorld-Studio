"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const { validateVistaSceneLayoutProfile } = require("./vista-scene-build-plan");
const { createVistaSceneBuildService } = require("./vista-scene-build-service");

const MAX_REGISTRY_BYTES = 64 * 1024;
const MAX_PROFILE_BYTES = 2 * 1024 * 1024;
const PROFILE_ID_PATTERN = /^[a-z0-9][a-z0-9._-]{0,79}$/;
const SHA256_PATTERN = /^[a-f0-9]{64}$/;

function isPlainObject(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function flag(value) {
  return /^(?:1|true|yes|on)$/i.test(String(value || "").trim());
}

function safeAbsolutePath(value, label) {
  const text = typeof value === "string" ? value.trim() : "";
  if (!text || !path.isAbsolute(text)) throw new TypeError(`${label} must be an absolute path`);
  const resolved = path.resolve(text);
  if (resolved === path.parse(resolved).root) throw new TypeError(`${label} cannot be a filesystem root`);
  return resolved;
}

function normalizeEntry(profileId, entry, production) {
  if (!PROFILE_ID_PATTERN.test(profileId) || !isPlainObject(entry)) {
    throw new TypeError("VISTA_SCENE_LAYOUT_REGISTRY_JSON contains an invalid entry");
  }
  const allowed = new Set(["file", "sha256"]);
  if (Object.keys(entry).some((key) => !allowed.has(key))) {
    throw new TypeError(`VISTA_SCENE_LAYOUT_REGISTRY_JSON.${profileId} contains an unsupported field`);
  }
  const file = safeAbsolutePath(entry.file, `VISTA_SCENE_LAYOUT_REGISTRY_JSON.${profileId}.file`);
  const checksum = typeof entry.sha256 === "string" ? entry.sha256.trim().toLowerCase() : "";
  if (!SHA256_PATTERN.test(checksum)) {
    throw new TypeError(`VISTA_SCENE_LAYOUT_REGISTRY_JSON.${profileId}.sha256 must be a SHA-256 digest`);
  }
  if (production && /(?:^|[._-])(?:test|fixture|demo)(?:$|[._-])/i.test(profileId)) {
    throw new TypeError("Test/demo scene layout profiles are not allowed in production");
  }
  return { file, sha256: checksum };
}

function parseLayoutRegistryJson(raw, { production = false } = {}) {
  const text = String(raw || "").trim();
  if (!text) return {};
  if (Buffer.byteLength(text, "utf8") > MAX_REGISTRY_BYTES) {
    throw new TypeError("VISTA_SCENE_LAYOUT_REGISTRY_JSON exceeds its size limit");
  }
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (_error) {
    throw new TypeError("VISTA_SCENE_LAYOUT_REGISTRY_JSON must be valid JSON");
  }
  if (!isPlainObject(parsed)) throw new TypeError("VISTA_SCENE_LAYOUT_REGISTRY_JSON must be an object");
  return Object.fromEntries(Object.entries(parsed).map(([profileId, entry]) => [
    profileId,
    normalizeEntry(profileId, entry, production),
  ]));
}

function readPinnedJson(entry, profileId, production) {
  const descriptor = fs.openSync(entry.file, fs.constants.O_RDONLY | Number(fs.constants.O_NOFOLLOW || 0));
  try {
    const stat = fs.fstatSync(descriptor);
    if (!stat.isFile() || stat.size < 2 || stat.size > MAX_PROFILE_BYTES) {
      throw new TypeError(`Scene layout profile '${profileId}' is not a bounded regular file`);
    }
    const raw = fs.readFileSync(descriptor, "utf8");
    const actual = crypto.createHash("sha256").update(raw, "utf8").digest("hex");
    if (actual !== entry.sha256) throw new TypeError(`Scene layout profile '${profileId}' checksum does not match`);
    let profile;
    try {
      profile = JSON.parse(raw);
    } catch (_error) {
      throw new TypeError(`Scene layout profile '${profileId}' is not valid JSON`);
    }
    if (!isPlainObject(profile) || profile.profile_id !== profileId) {
      throw new TypeError(`Scene layout profile '${profileId}' identity does not match its registry key`);
    }
    const validated = validateVistaSceneLayoutProfile(profile);
    if (production && [validated.asset_snapshot_id, validated.content_revision, validated.verification_revision]
      .some((value) => /(?:^|[._-])(?:test|fixture|demo)(?:$|[._-])/i.test(value))) {
      throw new TypeError(`Scene layout profile '${profileId}' contains a test/demo revision`);
    }
    return validated;
  } finally {
    fs.closeSync(descriptor);
  }
}

function resolveVistaSceneBuildConfig(env = process.env, options = {}) {
  const baseDir = path.resolve(options.baseDir || __dirname);
  const production = String(env.NODE_ENV || "").trim().toLowerCase() === "production";
  const entries = parseLayoutRegistryJson(env.VISTA_SCENE_LAYOUT_REGISTRY_JSON, { production });
  const explicitProfileId = String(env.VISTA_SCENE_LAYOUT_PROFILE_ID || "").trim();
  const explicitFile = String(env.VISTA_SCENE_LAYOUT_PROFILE_FILE || "").trim();
  const explicitSha = String(env.VISTA_SCENE_LAYOUT_PROFILE_SHA256 || "").trim();
  if (explicitProfileId || explicitFile || explicitSha) {
    if (!explicitProfileId || !explicitFile || !explicitSha) {
      throw new TypeError("VISTA_SCENE_LAYOUT_PROFILE_ID, FILE, and SHA256 must be set together");
    }
    entries[explicitProfileId] = normalizeEntry(explicitProfileId, {
      file: explicitFile,
      sha256: explicitSha,
    }, production);
  }

  const explicitFixture = flag(env.VISTA_SCENE_BUILD_ENABLE_GOLDEN_FIXTURE);
  if (production && explicitFixture) throw new TypeError("VISTA_SCENE_BUILD_ENABLE_GOLDEN_FIXTURE is not allowed in production");
  const fixtureEnabled = !production && (explicitFixture || flag(env.VISTA_DEMO_ENABLED));
  if (fixtureEnabled && !entries.mmg_040_static_office_v1) {
    const fixtureFile = path.join(baseDir, "tests", "fixtures", "vista", "mmg_040", "build-layout.v1.json");
    const raw = fs.readFileSync(fixtureFile, "utf8");
    entries.mmg_040_static_office_v1 = {
      file: fixtureFile,
      sha256: crypto.createHash("sha256").update(raw, "utf8").digest("hex"),
    };
  }
  const layoutProfiles = Object.fromEntries(Object.entries(entries).sort(([left], [right]) => left.localeCompare(right))
    .map(([profileId, entry]) => [profileId, readPinnedJson(entry, profileId, production)]));
  const recordRoot = env.VISTA_SCENE_BUILD_RECORD_ROOT
    ? safeAbsolutePath(env.VISTA_SCENE_BUILD_RECORD_ROOT, "VISTA_SCENE_BUILD_RECORD_ROOT")
    : path.resolve(options.defaultRecordRoot || path.join(baseDir, "..", ".runtime", "vista-scene-builds"));
  return Object.freeze({
    fixtureEnabled,
    layoutProfiles: Object.freeze(layoutProfiles),
    profileIds: Object.freeze(Object.keys(layoutProfiles)),
    recordRoot,
  });
}

function createVistaSceneBuildRuntime(options = {}) {
  if (!options.importService) throw new TypeError("createVistaSceneBuildRuntime requires importService");
  const config = options.config || resolveVistaSceneBuildConfig(options.env || process.env, {
    baseDir: options.baseDir || __dirname,
    defaultRecordRoot: options.defaultRecordRoot,
  });
  const service = options.service || createVistaSceneBuildService({
    importService: options.importService,
    layoutProfiles: config.layoutProfiles,
    recordRoot: config.recordRoot,
    executor: options.executor || null,
  });
  return Object.freeze({ config, service });
}

module.exports = {
  createVistaSceneBuildRuntime,
  parseLayoutRegistryJson,
  resolveVistaSceneBuildConfig,
};

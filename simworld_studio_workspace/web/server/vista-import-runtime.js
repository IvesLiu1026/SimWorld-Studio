"use strict";

const os = require("node:os");
const path = require("node:path");

const { createVistaImporter } = require("./vista-importer");
const { createVistaImportService } = require("./vista-import-service");

const REVISION_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const ENV_REGISTRY_MAX_BYTES = 64 * 1024;

function isPlainObject(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function flag(value) {
  return /^(?:1|true|yes|on)$/i.test(String(value || "").trim());
}

function safeManifestPath(value, label) {
  const candidate = String(value || "manifest.json").trim();
  if (!candidate
    || candidate.length > 240
    || candidate.includes("\\")
    || path.posix.isAbsolute(candidate)
    || path.win32.isAbsolute(candidate)
    || candidate.split("/").some((part) => !part || part === "." || part === "..")) {
    throw new TypeError(`${label} must be a safe relative manifest path`);
  }
  return candidate;
}

function safeBundleRoot(value, label) {
  if (typeof value !== "string" || !value.trim()) {
    throw new TypeError(`${label} must be a configured absolute directory`);
  }
  const root = path.resolve(value.trim());
  if (!path.isAbsolute(value.trim()) || root === path.parse(root).root) {
    throw new TypeError(`${label} must be a configured absolute directory`);
  }
  return root;
}

function normalizeRegistryEntry(revision, entry, label) {
  if (!REVISION_PATTERN.test(revision)) throw new TypeError(`${label} contains an invalid dataset revision`);
  if (typeof entry === "string") {
    return Object.freeze({ root: safeBundleRoot(entry, `${label}.${revision}`), manifestPath: "manifest.json" });
  }
  if (!isPlainObject(entry)) throw new TypeError(`${label}.${revision} must be an object`);
  const allowed = new Set(["root", "bundleRoot", "manifestPath", "manifest"]);
  if (Object.keys(entry).some((key) => !allowed.has(key))) {
    throw new TypeError(`${label}.${revision} contains an unsupported field`);
  }
  return Object.freeze({
    root: safeBundleRoot(entry.root || entry.bundleRoot, `${label}.${revision}.root`),
    manifestPath: safeManifestPath(entry.manifestPath || entry.manifest, `${label}.${revision}.manifestPath`),
  });
}

function parseRegistryJson(raw) {
  const text = String(raw || "").trim();
  if (!text) return {};
  if (Buffer.byteLength(text, "utf8") > ENV_REGISTRY_MAX_BYTES) {
    throw new TypeError("VISTA_IMPORT_REGISTRY_JSON exceeds its size limit");
  }
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (_error) {
    throw new TypeError("VISTA_IMPORT_REGISTRY_JSON must be valid JSON");
  }
  if (!isPlainObject(parsed)) throw new TypeError("VISTA_IMPORT_REGISTRY_JSON must be an object");
  const registry = Object.create(null);
  for (const [revision, entry] of Object.entries(parsed)) {
    registry[revision] = normalizeRegistryEntry(revision, entry, "VISTA_IMPORT_REGISTRY_JSON");
  }
  return registry;
}

function defaultStateRoot(env, baseDir) {
  if (env.XDG_STATE_HOME) return path.resolve(env.XDG_STATE_HOME);
  if (env.HOME) return path.join(path.resolve(env.HOME), ".local", "state");
  const home = os.homedir();
  if (home && home !== path.parse(home).root) return path.join(home, ".local", "state");
  return path.resolve(baseDir, "../.runtime");
}

function resolveVistaImportConfig(env = process.env, options = {}) {
  const baseDir = path.resolve(options.baseDir || __dirname);
  const production = String(env.NODE_ENV || "").trim().toLowerCase() === "production";
  const explicitGoldenFixture = flag(env.VISTA_IMPORT_ENABLE_GOLDEN_FIXTURE);
  const enableGoldenFixture = explicitGoldenFixture || (!production && flag(env.VISTA_DEMO_ENABLED));
  const registry = parseRegistryJson(env.VISTA_IMPORT_REGISTRY_JSON);
  const simpleRevision = String(env.VISTA_IMPORT_DATASET_REVISION || "").trim();
  const simpleRoot = String(env.VISTA_IMPORT_BUNDLE_ROOT || "").trim();
  if (simpleRevision || simpleRoot) {
    if (!simpleRevision || !simpleRoot) {
      throw new TypeError("VISTA_IMPORT_DATASET_REVISION and VISTA_IMPORT_BUNDLE_ROOT must be set together");
    }
    registry[simpleRevision] = normalizeRegistryEntry(simpleRevision, {
      root: simpleRoot,
      manifestPath: env.VISTA_IMPORT_MANIFEST || "manifest.json",
    }, "VISTA_IMPORT_BUNDLE_ROOT");
  }

  if (enableGoldenFixture) {
    if (production) {
      throw new TypeError("VISTA_IMPORT_ENABLE_GOLDEN_FIXTURE is not allowed in production");
    }
    const revision = "round1_reviewed_latest";
    if (!registry[revision]) {
      registry[revision] = Object.freeze({
        root: path.join(baseDir, "tests", "fixtures", "vista", "mmg_040"),
        manifestPath: "manifest.json",
      });
    }
  }

  const artifactRoot = env.VISTA_IMPORT_ARTIFACT_ROOT
    ? safeBundleRoot(env.VISTA_IMPORT_ARTIFACT_ROOT, "VISTA_IMPORT_ARTIFACT_ROOT")
    : path.join(defaultStateRoot(env, baseDir), "simworld-studio", "vista-imports");
  return Object.freeze({
    registry: Object.freeze(registry),
    artifactRoot,
    configuredRevisions: Object.freeze(Object.keys(registry).sort()),
    fixtureEnabled: enableGoldenFixture,
  });
}

function createVistaImportRuntime(options = {}) {
  const config = options.config || resolveVistaImportConfig(options.env || process.env, {
    baseDir: options.baseDir || __dirname,
  });
  const importer = options.importer || createVistaImporter({
    registry: config.registry,
    ...(options.assetResolver ? { assetResolver: options.assetResolver } : {}),
  });
  const service = options.service || createVistaImportService({
    importer,
    artifactRoot: config.artifactRoot,
    artifactRecorder: options.artifactRecorder || null,
  });
  return Object.freeze({ config, importer, service });
}

module.exports = {
  createVistaImportRuntime,
  parseRegistryJson,
  resolveVistaImportConfig,
};

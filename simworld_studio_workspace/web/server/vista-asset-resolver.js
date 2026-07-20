"use strict";

// Pure SceneSpec resolver. The injected searchAssets function receives
// { query, k, snapshot_id, entity_id, signal } and returns either an array or
// { snapshot_id, candidates }. A configured snapshot_id is authoritative; any
// snapshot evidence returned by the provider must match it exactly.

const RESOLUTION_SCHEMA = "vista-asset-resolution/v1";
const DEFAULT_TIMEOUT_MS = 1_500;
const DEFAULT_TOTAL_TIMEOUT_MS = 10_000;
const DEFAULT_MAX_CANDIDATES = 8;
const DEFAULT_MIN_CONFIDENCE = 0.7;
const MAX_ENTITIES = 200;
const MAX_SEARCH_RESPONSE_CANDIDATES = 200;
const SAFE_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:@-]{0,159}$/;
const ENTITY_ID_RE = /^[a-z][a-z0-9_]{0,79}$/;
const UE_PATH_RE = /^\/Game\/[A-Za-z0-9_-]+(?:\/[A-Za-z0-9_-]+)*(?:\.[A-Za-z0-9_-]+)?$/;

class VistaAssetResolverError extends Error {
  constructor(code, message, { status = 400, retryable = false, details = {} } = {}) {
    super(message);
    this.name = "VistaAssetResolverError";
    this.code = code;
    this.status = status;
    this.retryable = Boolean(retryable);
    this.details = Object.freeze({ ...details });
  }

  toJSON() {
    return {
      code: this.code,
      message: this.message,
      retryable: this.retryable,
      details: this.details,
    };
  }
}

function fail(code, message, options) {
  throw new VistaAssetResolverError(code, message, options);
}

function boundedInteger(value, fallback, { min, max, field }) {
  const resolved = value === undefined ? fallback : value;
  if (!Number.isInteger(resolved) || resolved < min || resolved > max) {
    fail("VISTA_ASSET_RESOLVER_CONFIG_INVALID", `${field} is outside its allowed range`, {
      details: { field },
    });
  }
  return resolved;
}

function boundedConfidence(value, fallback, field) {
  const resolved = value === undefined ? fallback : value;
  if (typeof resolved !== "number" || !Number.isFinite(resolved) || resolved < 0 || resolved > 1) {
    fail("VISTA_ASSET_RESOLVER_CONFIG_INVALID", `${field} must be a number from 0 to 1`, {
      details: { field },
    });
  }
  return resolved;
}

function requireOverrideConfidence(value, field) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0 || value > 1) {
    fail("VISTA_ASSET_OVERRIDE_INVALID", "Manual override confidence must be a number from 0 to 1", {
      details: { field },
    });
  }
  return value;
}

function requireSafeId(value, field, pattern = SAFE_ID_RE) {
  const normalized = String(value === undefined || value === null ? "" : value).trim();
  if (!pattern.test(normalized)) {
    fail("VISTA_ASSET_RESOLUTION_INPUT_INVALID", `${field} is invalid`, {
      details: { field },
    });
  }
  return normalized;
}

function requireString(value, field, maxLength) {
  const normalized = typeof value === "string" ? value.trim() : "";
  if (!normalized || normalized.length > maxLength) {
    fail("VISTA_ASSET_RESOLUTION_INPUT_INVALID", `${field} is invalid`, {
      details: { field },
    });
  }
  return normalized;
}

function normalizeUePath(value, field) {
  const normalized = typeof value === "string" ? value.trim() : "";
  if (!UE_PATH_RE.test(normalized) || normalized.includes("..")) {
    fail("VISTA_ASSET_RESOLUTION_INPUT_INVALID", `${field} is not a valid /Game asset path`, {
      details: { field },
    });
  }
  return normalized;
}

function assetLeaf(uePath) {
  const pathLeaf = uePath.split("/").pop() || "";
  return (pathLeaf.split(".")[0] || "").toLowerCase();
}

function isBasicGeometry(candidate) {
  const uePath = String(candidate && (candidate.ue_path || candidate.path || candidate.unreal_asset_path) || "").trim();
  const lowerPath = uePath.toLowerCase();
  const leaf = assetLeaf(uePath);
  const type = String(candidate && (candidate.asset_type || candidate.type) || "").toLowerCase();
  const explicit = candidate && (candidate.is_basic_geometry === true || candidate.basic_geometry === true);
  if (explicit || type === "primitive" || type === "basic_geometry") return true;
  if (lowerPath.startsWith("/engine/basicshapes/") || lowerPath.includes("/basicshapes/")) return true;
  return /^(?:sm_)?(?:cube|plane|sphere|cylinder|cone)$/.test(leaf);
}

function validateSceneInput(scene) {
  if (!scene || typeof scene !== "object" || Array.isArray(scene)) {
    fail("VISTA_ASSET_RESOLUTION_INPUT_INVALID", "SceneSpec must be an object");
  }
  if (scene.schema !== "vista-simworld-scene/v1") {
    fail("VISTA_ASSET_RESOLUTION_INPUT_INVALID", "Unsupported SceneSpec schema", {
      details: { field: "schema" },
    });
  }
  if (!Array.isArray(scene.entities) || scene.entities.length === 0 || scene.entities.length > MAX_ENTITIES) {
    fail("VISTA_ASSET_RESOLUTION_INPUT_INVALID", "SceneSpec entities are invalid", {
      details: { field: "entities" },
    });
  }
  if (!Array.isArray(scene.unresolved)) {
    fail("VISTA_ASSET_RESOLUTION_INPUT_INVALID", "SceneSpec unresolved must be an array", {
      details: { field: "unresolved" },
    });
  }

  const ids = new Set();
  const entities = scene.entities.map((entity, index) => {
    if (!entity || typeof entity !== "object" || Array.isArray(entity)) {
      fail("VISTA_ASSET_RESOLUTION_INPUT_INVALID", "SceneSpec entity is invalid", {
        details: { field: `entities[${index}]` },
      });
    }
    const id = requireSafeId(entity.id, `entities[${index}].id`, ENTITY_ID_RE);
    if (ids.has(id)) {
      fail("VISTA_ASSET_RESOLUTION_INPUT_INVALID", "SceneSpec entity ids must be unique", {
        details: { entity_id: id },
      });
    }
    ids.add(id);
    const query = requireString(entity.semantic_query, `entities[${index}].semantic_query`, 1_000);
    const sourcePointer = requireString(entity.source_pointer, `entities[${index}].source_pointer`, 512);
    if (entity.required !== true && entity.required !== false) {
      fail("VISTA_ASSET_RESOLUTION_INPUT_INVALID", "Entity required must be boolean", {
        details: { entity_id: id },
      });
    }
    return { entity, id, query, sourcePointer };
  });
  return { entities, ids };
}

function normalizeManualOverrides(value, entityIds, snapshotId) {
  if (value === undefined || value === null) return new Map();
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    fail("VISTA_ASSET_OVERRIDE_INVALID", "manualOverrides must be an object keyed by entity id");
  }
  const overrides = new Map();
  for (const [rawEntityId, raw] of Object.entries(value)) {
    const entityId = requireSafeId(rawEntityId, "manualOverrides entity id", ENTITY_ID_RE);
    if (!entityIds.has(entityId)) {
      fail("VISTA_ASSET_OVERRIDE_INVALID", "Manual override references an unknown entity", {
        details: { entity_id: entityId },
      });
    }
    if (!raw || typeof raw !== "object" || Array.isArray(raw) || raw.confirmed !== true) {
      fail("VISTA_ASSET_OVERRIDE_INVALID", "Manual override must be explicitly confirmed", {
        details: { entity_id: entityId },
      });
    }
    const allowedKeys = new Set(["confirmed", "reason", "snapshot_id", "asset_id", "ue_path", "confidence"]);
    const extraKey = Object.keys(raw).find((key) => !allowedKeys.has(key));
    if (extraKey) {
      fail("VISTA_ASSET_OVERRIDE_INVALID", "Manual override contains an unsupported field", {
        details: { entity_id: entityId, field: extraKey },
      });
    }
    const overrideSnapshot = raw.snapshot_id === undefined
      ? snapshotId
      : requireSafeId(raw.snapshot_id, `manualOverrides.${entityId}.snapshot_id`);
    if (overrideSnapshot !== snapshotId) {
      fail("VISTA_ASSET_SNAPSHOT_MISMATCH", "Manual override asset snapshot does not match the resolver snapshot", {
        details: { entity_id: entityId, expected_snapshot_id: snapshotId },
      });
    }
    const candidate = {
      asset_id: requireSafeId(raw.asset_id, `manualOverrides.${entityId}.asset_id`),
      ue_path: normalizeUePath(raw.ue_path, `manualOverrides.${entityId}.ue_path`),
      confidence: requireOverrideConfidence(raw.confidence, `manualOverrides.${entityId}.confidence`),
    };
    if (isBasicGeometry(candidate)) {
      fail("VISTA_ASSET_BASIC_GEOMETRY_FORBIDDEN", "Basic geometry cannot be used as a VISTA asset override", {
        details: { entity_id: entityId },
      });
    }
    overrides.set(entityId, Object.freeze({
      confirmed: true,
      reason: requireString(raw.reason, `manualOverrides.${entityId}.reason`, 500),
      snapshot_id: snapshotId,
      ...candidate,
    }));
  }
  return overrides;
}

function reportedSnapshotId(raw) {
  if (!raw || (typeof raw !== "object" && !Array.isArray(raw))) return null;
  const retrieval = raw.retrieval && typeof raw.retrieval === "object" ? raw.retrieval : null;
  return raw.snapshot_id
    || raw.snapshot_revision
    || (retrieval && (retrieval.snapshot_id || retrieval.snapshot_revision))
    || null;
}

function candidateList(raw) {
  if (Array.isArray(raw)) return raw;
  if (!raw || typeof raw !== "object") return null;
  if (Array.isArray(raw.candidates)) return raw.candidates;
  if (Array.isArray(raw.assets)) return raw.assets;
  if (Array.isArray(raw.results)) return raw.results;
  return null;
}

function responseInvalid(entityId, field) {
  fail("VISTA_ASSET_SEARCH_RESPONSE_INVALID", "Semantic asset search returned an invalid response", {
    status: 502,
    retryable: false,
    details: { entity_id: entityId, field },
  });
}

function normalizeCandidate(raw, entityId, snapshotId) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) responseInvalid(entityId, "candidate");
  const candidateSnapshot = raw.snapshot_id || raw.snapshot_revision || snapshotId;
  if (candidateSnapshot !== snapshotId) {
    fail("VISTA_ASSET_SNAPSHOT_MISMATCH", "Search candidate asset snapshot does not match the resolver snapshot", {
      status: 409,
      details: { entity_id: entityId, expected_snapshot_id: snapshotId },
    });
  }
  let assetId;
  let uePath;
  try {
    assetId = requireSafeId(raw.asset_id === undefined ? raw.id : raw.asset_id, "candidate.asset_id");
    uePath = normalizeUePath(
      raw.ue_path === undefined ? (raw.path === undefined ? raw.unreal_asset_path : raw.path) : raw.ue_path,
      "candidate.ue_path",
    );
  } catch (error) {
    if (error instanceof VistaAssetResolverError) responseInvalid(entityId, error.details.field || "candidate");
    throw error;
  }
  const confidence = raw.confidence === undefined ? raw.score : raw.confidence;
  if (typeof confidence !== "number" || !Number.isFinite(confidence) || confidence < 0 || confidence > 1) {
    responseInvalid(entityId, "candidate.confidence");
  }
  return {
    snapshot_id: snapshotId,
    asset_id: assetId,
    ue_path: uePath,
    confidence,
    origin: "search",
    isBasic: isBasicGeometry({ ...raw, ue_path: uePath }),
  };
}

function compareCandidates(a, b) {
  return b.confidence - a.confidence
    || (a.asset_id < b.asset_id ? -1 : a.asset_id > b.asset_id ? 1 : 0)
    || (a.ue_path < b.ue_path ? -1 : a.ue_path > b.ue_path ? 1 : 0);
}

function normalizeSearchResponse(raw, entityId, snapshotId, maxCandidates) {
  const responseSnapshot = reportedSnapshotId(raw);
  if (responseSnapshot !== null && responseSnapshot !== snapshotId) {
    fail("VISTA_ASSET_SNAPSHOT_MISMATCH", "Semantic asset search used a different asset snapshot", {
      status: 409,
      details: { entity_id: entityId, expected_snapshot_id: snapshotId },
    });
  }
  const list = candidateList(raw);
  if (!list) responseInvalid(entityId, "candidates");
  if (list.length > MAX_SEARCH_RESPONSE_CANDIDATES) responseInvalid(entityId, "candidates.length");

  const unique = new Map();
  for (const item of list) {
    const candidate = normalizeCandidate(item, entityId, snapshotId);
    if (candidate.isBasic) continue;
    delete candidate.isBasic;
    const key = `${candidate.asset_id}\u0000${candidate.ue_path}`;
    const existing = unique.get(key);
    if (!existing || compareCandidates(candidate, existing) < 0) unique.set(key, candidate);
  }
  return [...unique.values()]
    .sort(compareCandidates)
    .slice(0, maxCandidates)
    .map((candidate, index) => Object.freeze({ rank: index + 1, ...candidate }));
}

function safeUpstreamCode(error) {
  const code = error && typeof error.code === "string" ? error.code : "";
  return /^[A-Z][A-Z0-9_]{0,79}$/.test(code) ? code : undefined;
}

function searchFailure(error, entityId) {
  return new VistaAssetResolverError(
    "VISTA_ASSET_SEARCH_FAILED",
    "Semantic asset search failed",
    {
      status: 503,
      retryable: error && typeof error.retryable === "boolean" ? error.retryable : true,
      details: {
        entity_id: entityId,
        ...(safeUpstreamCode(error) ? { upstream_code: safeUpstreamCode(error) } : {}),
      },
    },
  );
}

function runBoundedSearch(searchAssets, request, { parentSignal, timeoutMs, entityId }) {
  if (parentSignal && parentSignal.aborted) {
    return Promise.reject(new VistaAssetResolverError(
      "VISTA_ASSET_RESOLUTION_ABORTED",
      "Asset resolution was cancelled",
      { status: 499, retryable: false, details: { entity_id: entityId } },
    ));
  }

  const controller = new AbortController();
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (callback, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (parentSignal) parentSignal.removeEventListener("abort", onAbort);
      callback(value);
    };
    const onAbort = () => {
      controller.abort();
      finish(reject, new VistaAssetResolverError(
        "VISTA_ASSET_RESOLUTION_ABORTED",
        "Asset resolution was cancelled",
        { status: 499, retryable: false, details: { entity_id: entityId } },
      ));
    };
    const timer = setTimeout(() => {
      controller.abort();
      finish(reject, new VistaAssetResolverError(
        "VISTA_ASSET_RESOLUTION_TIMEOUT",
        "Semantic asset search exceeded its bounded deadline",
        { status: 504, retryable: true, details: { entity_id: entityId } },
      ));
    }, timeoutMs);
    if (parentSignal) parentSignal.addEventListener("abort", onAbort, { once: true });

    Promise.resolve()
      .then(() => (settled ? undefined : searchAssets({ ...request, signal: controller.signal })))
      .then((value) => finish(resolve, value))
      .catch((error) => finish(reject, searchFailure(error, entityId)));
  });
}

function bindingFrom(candidate, snapshotId) {
  return Object.freeze({
    snapshot_id: snapshotId,
    asset_id: candidate.asset_id,
    ue_path: candidate.ue_path,
    confidence: candidate.confidence,
  });
}

function candidateFromOverride(override) {
  return Object.freeze({
    rank: 0,
    snapshot_id: override.snapshot_id,
    asset_id: override.asset_id,
    ue_path: override.ue_path,
    confidence: override.confidence,
    origin: "manual_override",
  });
}

function includeOverrideCandidate(candidates, override, maxCandidates) {
  const matching = candidates.find((candidate) => (
    candidate.asset_id === override.asset_id && candidate.ue_path === override.ue_path
  ));
  if (matching) return candidates;
  const withOverride = [...candidates, candidateFromOverride(override)]
    .sort(compareCandidates)
    .slice(0, maxCandidates)
    .map((candidate, index) => Object.freeze({ ...candidate, rank: index + 1 }));
  if (withOverride.some((candidate) => (
    candidate.asset_id === override.asset_id && candidate.ue_path === override.ue_path
  ))) return withOverride;
  return [candidateFromOverride(override), ...withOverride.slice(0, Math.max(0, maxCandidates - 1))]
    .map((candidate, index) => Object.freeze({ ...candidate, rank: index + 1 }));
}

function resolutionRecord({ query, snapshotId, candidates, selectedBinding, selectedBy, manualOverride, minConfidence }) {
  return Object.freeze({
    schema: RESOLUTION_SCHEMA,
    snapshot_id: snapshotId,
    query,
    min_confidence: minConfidence,
    candidates: Object.freeze(candidates),
    selected_binding: selectedBinding,
    selected_by: selectedBy,
    manual_override: manualOverride,
  });
}

function noMatchMapping(entity, candidates, minConfidence) {
  const lowConfidence = candidates.length > 0;
  return Object.freeze({
    mapping_id: `unresolved-asset-${entity.id}`,
    kind: "asset",
    source_pointer: entity.sourcePointer,
    reason_code: "no_asset_match",
    message: lowConfidence
      ? `No real asset met the minimum confidence ${minConfidence.toFixed(3)} for entity '${entity.id}'`
      : `No real asset match was returned for entity '${entity.id}'`,
    blocking: entity.entity.required === true,
    candidates: Object.freeze([...new Set(candidates.map((candidate) => candidate.asset_id))]),
  });
}

function isResolverOwnedUnresolved(item, entityIds) {
  if (!item || typeof item !== "object" || item.kind !== "asset") return false;
  if (typeof item.mapping_id !== "string" || !item.mapping_id.startsWith("unresolved-asset-")) return false;
  return entityIds.has(item.mapping_id.slice("unresolved-asset-".length));
}

function createVistaAssetResolver({
  searchAssets,
  snapshotId,
  timeoutMs = DEFAULT_TIMEOUT_MS,
  totalTimeoutMs = DEFAULT_TOTAL_TIMEOUT_MS,
  maxCandidates = DEFAULT_MAX_CANDIDATES,
  minConfidence = DEFAULT_MIN_CONFIDENCE,
} = {}) {
  if (typeof searchAssets !== "function") {
    fail("VISTA_ASSET_RESOLVER_CONFIG_INVALID", "searchAssets must be an injected function", {
      details: { field: "searchAssets" },
    });
  }
  const fixedSnapshotId = requireSafeId(snapshotId, "snapshotId");
  const fixedTimeoutMs = boundedInteger(timeoutMs, DEFAULT_TIMEOUT_MS, {
    min: 10,
    max: 30_000,
    field: "timeoutMs",
  });
  const fixedTotalTimeoutMs = boundedInteger(totalTimeoutMs, DEFAULT_TOTAL_TIMEOUT_MS, {
    min: 10,
    max: 120_000,
    field: "totalTimeoutMs",
  });
  const fixedMaxCandidates = boundedInteger(maxCandidates, DEFAULT_MAX_CANDIDATES, {
    min: 1,
    max: 40,
    field: "maxCandidates",
  });
  const fixedMinConfidence = boundedConfidence(minConfidence, DEFAULT_MIN_CONFIDENCE, "minConfidence");

  async function resolve(scene, { signal, manualOverrides } = {}) {
    if (signal !== undefined && (!signal || typeof signal.aborted !== "boolean" || typeof signal.addEventListener !== "function")) {
      fail("VISTA_ASSET_RESOLUTION_INPUT_INVALID", "signal must be an AbortSignal", {
        details: { field: "signal" },
      });
    }
    const validated = validateSceneInput(scene);
    const overrides = normalizeManualOverrides(manualOverrides, validated.ids, fixedSnapshotId);
    const startedAt = Date.now();
    const resolvedEntities = [];
    const generatedUnresolved = [];

    for (const entity of validated.entities) {
      if (signal && signal.aborted) {
        fail("VISTA_ASSET_RESOLUTION_ABORTED", "Asset resolution was cancelled", {
          status: 499,
          details: { entity_id: entity.id },
        });
      }
      const remainingMs = fixedTotalTimeoutMs - (Date.now() - startedAt);
      if (remainingMs <= 0) {
        fail("VISTA_ASSET_RESOLUTION_TIMEOUT", "Asset resolution exceeded its total bounded deadline", {
          status: 504,
          retryable: true,
          details: { entity_id: entity.id },
        });
      }
      const raw = await runBoundedSearch(searchAssets, {
        query: entity.query,
        k: fixedMaxCandidates,
        snapshot_id: fixedSnapshotId,
        entity_id: entity.id,
      }, {
        parentSignal: signal,
        timeoutMs: Math.min(fixedTimeoutMs, remainingMs),
        entityId: entity.id,
      });
      let candidates = normalizeSearchResponse(raw, entity.id, fixedSnapshotId, fixedMaxCandidates);
      const override = overrides.get(entity.id) || null;
      if (override) candidates = includeOverrideCandidate(candidates, override, fixedMaxCandidates);

      let selectedBinding = null;
      let selectedBy = null;
      if (override) {
        selectedBinding = bindingFrom(override, fixedSnapshotId);
        selectedBy = "manual_override";
      } else if (candidates.length && candidates[0].confidence >= fixedMinConfidence) {
        selectedBinding = bindingFrom(candidates[0], fixedSnapshotId);
        selectedBy = "automatic";
      }
      if (!selectedBinding) generatedUnresolved.push(noMatchMapping(entity, candidates, fixedMinConfidence));

      resolvedEntities.push(Object.freeze({
        ...entity.entity,
        asset_binding: selectedBinding,
        asset_resolution: resolutionRecord({
          query: entity.query,
          snapshotId: fixedSnapshotId,
          candidates,
          selectedBinding,
          selectedBy,
          manualOverride: override,
          minConfidence: fixedMinConfidence,
        }),
      }));
    }

    const retainedUnresolved = scene.unresolved.filter((item) => !isResolverOwnedUnresolved(item, validated.ids));
    return Object.freeze({
      ...scene,
      entities: Object.freeze(resolvedEntities),
      unresolved: Object.freeze([...retainedUnresolved, ...generatedUnresolved]),
    });
  }

  return Object.freeze({
    schema: RESOLUTION_SCHEMA,
    snapshot_id: fixedSnapshotId,
    resolve,
  });
}

async function resolveVistaAssets(scene, options = {}) {
  const { manualOverrides, signal, ...resolverOptions } = options;
  return createVistaAssetResolver(resolverOptions).resolve(scene, { manualOverrides, signal });
}

module.exports = {
  RESOLUTION_SCHEMA,
  VistaAssetResolverError,
  createVistaAssetResolver,
  isBasicGeometry,
  resolveVistaAssets,
};

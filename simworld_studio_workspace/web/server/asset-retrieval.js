"use strict";
// Asset-retrieval component for SimWorld outdoor scene generation.
// Narrow-then-rank over the offline asset DB (/data/siddhant/asset_db):
//   stage 1  route     -> pick the categories relevant to the scene
//   stage 2  select     -> per category (run in PARALLEL), pick the assets that fit the scene
//   stage 3  aggregate  -> dedupe + finalize a coherent, ordered palette
// Output: a prompt block listing the chosen assets with their EXACT spawn path + spawn tool,
// appended to the coder's system prompt.
//
// mode "baseline_full" skips routing/selection and lists ALL assets through the SAME formatter,
// so the A/B isolates retrieval (identical asset universe, with vs without filtering).
//
// No hard caps anywhere — every stage lets the model decide how many assets fit the scene.
const fs = require("fs");
const path = require("path");
const { oneshotJSON } = require("./llm-oneshot");
const {
  AssetDependencyError,
  AssetRetrievalUnavailableError,
  serializeDependencyCause,
} = require("./asset-retrieval-db");

const DEFAULT_DATA_ROOT = process.env.XDG_DATA_HOME
  || (process.env.HOME ? path.join(process.env.HOME, ".local", "share") : path.resolve(__dirname, "..", ".runtime"));
const ASSET_DB_DIR = process.env.ASSET_DB_DIR || path.join(DEFAULT_DATA_ROOT, "simworld-studio", "asset-db");
const PREFILTER_TOP_K = parseInt(process.env.PREFILTER_TOP_K || "150", 10);
const FULL_FALLBACK_MAX = parseInt(process.env.ASSET_FULL_FALLBACK_MAX || "0", 10);

const SETTING_VALUES = new Set([
  "modern_urban", "industrial", "suburban_residential", "commercial_retail",
  "nature_rural", "coastal_harbor", "medieval", "fantasy_gothic",
  "ancient_temple", "middle_eastern", "east_asian", "winter", "sci_fi",
  "indoor", "generic",
]);

// Decide which MCP tool spawns this asset: Blueprints -> spawn_blueprint_actor, static meshes -> spawn_actor.
// asset_type is authoritative (set by the indexer from the real UE object class), so trust it first.
// NOTE: some Blueprints are SM_-named (e.g. /Game/bp_city_props/SM_hydrant_main is a Blueprint) — the
// leaf name is NOT a reliable signal, asset_type is. Prefix is only a fallback when asset_type is absent.
function _spawnTool(rec) {
  const t = String(rec && rec.technical && rec.technical.asset_type || "").trim();
  if (t === "Blueprint") return "spawn_blueprint_actor";
  if (t === "StaticMesh") return "spawn_actor";
  const p = String(rec && rec.technical && rec.technical.unreal_asset_path || "");
  const leaf = p.split("/").pop().split(".")[0];
  if (/^bp_/i.test(leaf) || /\/blueprints\//i.test(p)) return "spawn_blueprint_actor";
  return "spawn_actor";
}

let _cache = null;
const _retrievalCache = new Map();  // scene-text -> result; so loop rounds reuse one retrieval per scene

function _truthy(v) {
  return /^(1|true|yes|on)$/i.test(String(v || ""));
}

// Env-gated (ASSET_DROP_JUNK) filter: drop assets that should never enter a buildable
// palette — VFX blueprints (e.g. BP_fountain_on, whose water particles float and wreck
// framing), sky/weather/atmosphere blueprints, and absurdly oversized background/terrain
// (sky domes up to millions of metres). Default OFF so committed behavior is unchanged.
const _DROP_JUNK = _truthy(process.env.ASSET_DROP_JUNK || "");
function _isJunkAsset(a) {
  const p = String((a && a.path) || "").toLowerCase();
  const n = String((a && a.name) || "").toLowerCase();
  if (/\/vfx\//.test(p)) return true;
  if (/(skydome|skysphere|sky_sphere|skybox|infinitysky|infinityweather|infinityprecip|precipitation|backgroundisland|background_[abc]\b)/.test(p)) return true;
  if (/(sky dome|sky sphere|skybox|atmospheric sky|volumetric sky|distant island|background (cliff|island)|infinity (sky|lightning))/.test(n)) return true;
  const d = a && a.dims;
  if (d) { const mx = Math.max(Number(d.width) || 0, Number(d.depth) || 0, Number(d.height) || 0); if (mx > 300) return true; }
  return false;
}

function _legacyPrefilterDefault() {
  return _truthy(process.env.ASSET_PREFILTER);
}

function _normalizeAssetModeValue(v) {
  const s = String(v || "").trim().toLowerCase();
  if (!s) return null;
  if (["off", "none", "no", "false", "0", "disabled"].includes(s)) return "off";
  if (["db", "qdrant", "prefilter", "vector"].includes(s)) return "db";
  if (["hybrid", "seed", "both", "seed_plus_tools"].includes(s)) return "hybrid";
  if (["file", "catalog", "retrieval_file", "llm"].includes(s)) return "file";
  if (["baseline_full", "full", "all", "full_list"].includes(s)) return "baseline_full";
  if (s === "retrieval") return _legacyPrefilterDefault() ? "db" : "file";
  return null;
}

function resolveAssetMode(bodyOrMode) {
  if (typeof bodyOrMode === "string") {
    return _normalizeAssetModeValue(bodyOrMode) || "off";
  }
  const body = bodyOrMode || {};
  const explicit = _normalizeAssetModeValue(body.assetRetrievalMode) || _normalizeAssetModeValue(body.assetMode);
  if (explicit) return explicit;
  const envMode = _normalizeAssetModeValue(process.env.ASSET_RETRIEVAL_MODE);
  if (envMode) return envMode;
  return "hybrid";
}

class AssetRetrievalPolicyError extends Error {
  constructor(message, details) {
    super(message);
    this.name = "AssetRetrievalPolicyError";
    this.code = "ASSET_RETRIEVAL_POLICY_INVALID";
    this.details = details || {};
  }
}

function _optionalBoolean(value) {
  if (typeof value === "boolean") return value;
  if (typeof value === "number" && (value === 0 || value === 1)) return value === 1;
  const s = String(value === undefined || value === null ? "" : value).trim().toLowerCase();
  if (["1", "true", "yes", "on"].includes(s)) return true;
  if (["0", "false", "no", "off"].includes(s)) return false;
  return undefined;
}

function _normalizeDegradedMode(value) {
  const s = String(value === undefined || value === null ? "" : value).trim().toLowerCase();
  if (!s || ["off", "none", "disabled", "false", "0"].includes(s)) return "disabled";
  if (["basic", "basic_geometry", "geometry", "whitebox", "true", "1"].includes(s)) return "basic_geometry";
  return null;
}

function resolveAssetPolicy(bodyOrMode) {
  const body = bodyOrMode && typeof bodyOrMode === "object" ? bodyOrMode : {};
  const requestModeRaw = typeof bodyOrMode === "string"
    ? bodyOrMode
    : (body.assetRetrievalMode !== undefined ? body.assetRetrievalMode : body.assetMode);
  const configuredModeRaw = requestModeRaw !== undefined && String(requestModeRaw).trim()
    ? requestModeRaw
    : process.env.ASSET_RETRIEVAL_MODE;
  if (configuredModeRaw !== undefined && String(configuredModeRaw).trim() && !_normalizeAssetModeValue(configuredModeRaw)) {
    throw new AssetRetrievalPolicyError("unsupported asset retrieval mode", {
      mode: String(configuredModeRaw),
    });
  }
  const mode = resolveAssetMode(bodyOrMode);
  const requestRequireRaw = body.require_real_assets !== undefined ? body.require_real_assets : body.requireRealAssets;
  const requestRequire = _optionalBoolean(requestRequireRaw);
  if (requestRequireRaw !== undefined && requestRequire === undefined) {
    throw new AssetRetrievalPolicyError("require_real_assets must be a boolean", {
      require_real_assets: String(requestRequireRaw),
    });
  }
  const envRequireRaw = process.env.ASSET_REQUIRE_REAL_ASSETS !== undefined
    ? process.env.ASSET_REQUIRE_REAL_ASSETS
    : process.env.REQUIRE_REAL_ASSETS;
  const envRequire = _optionalBoolean(envRequireRaw);
  if (envRequireRaw !== undefined && envRequire === undefined) {
    throw new AssetRetrievalPolicyError("configured require_real_assets must be a boolean", {});
  }
  const requireRealAssets = requestRequire !== undefined ? requestRequire : Boolean(envRequire);

  const degradedRaw = body.asset_degraded_mode !== undefined ? body.asset_degraded_mode
    : body.assetDegradedMode !== undefined ? body.assetDegradedMode
      : body.asset_fallback_mode !== undefined ? body.asset_fallback_mode
        : body.assetFallbackMode !== undefined ? body.assetFallbackMode
          : process.env.ASSET_DEGRADED_MODE;
  const allowDegradedValue = body.allow_degraded_assets !== undefined
    ? body.allow_degraded_assets
    : body.allowDegradedAssets;
  const allowDegradedRaw = _optionalBoolean(allowDegradedValue);
  if (allowDegradedValue !== undefined && allowDegradedRaw === undefined) {
    throw new AssetRetrievalPolicyError("allow_degraded_assets must be a boolean", {
      allow_degraded_assets: String(allowDegradedValue),
    });
  }
  let degradedMode = _normalizeDegradedMode(degradedRaw);
  if (degradedMode === null) {
    throw new AssetRetrievalPolicyError("unsupported asset degraded mode", {
      degraded_mode: String(degradedRaw),
    });
  }
  if ((degradedRaw === undefined || degradedRaw === null || String(degradedRaw).trim() === "") && allowDegradedRaw === true) {
    degradedMode = "basic_geometry";
  }
  if (allowDegradedRaw === false) degradedMode = "disabled";

  if (requireRealAssets && mode === "off") {
    throw new AssetRetrievalPolicyError("require_real_assets cannot be combined with asset retrieval mode off", {
      mode,
      require_real_assets: true,
    });
  }
  if (requireRealAssets && degradedMode !== "disabled") {
    throw new AssetRetrievalPolicyError("require_real_assets cannot be combined with basic geometry fallback", {
      mode,
      require_real_assets: true,
      degraded_mode: degradedMode,
    });
  }

  return Object.freeze({
    policy_version: 1,
    mode,
    require_real_assets: requireRealAssets,
    degraded_mode: degradedMode,
    allow_degraded_assets: degradedMode === "basic_geometry" && !requireRealAssets,
  });
}

function _asAssetPolicy(input) {
  if (input && input.policy_version === 1 && typeof input.mode === "string") return input;
  return resolveAssetPolicy(input);
}

function _redactDependencyMessage(value) {
  return String(value || "asset dependency failed")
    .replace(/\b(postgres(?:ql)?|https?):\/\/[^\s/@]+:[^\s/@]+@/gi, "$1://[redacted]@")
    .replace(/\b(token|password|secret|api[_-]?key)=([^\s&]+)/gi, "$1=[redacted]");
}

function normalizeAssetRetrievalError(error, context) {
  if (error instanceof AssetRetrievalUnavailableError) return error;
  const operation = context && context.operation || "asset_retrieval";
  const cause = error instanceof AssetDependencyError
    ? error
    : new AssetDependencyError(
      context && context.dependency || "retrieval",
      error && error.code && String(error.code).startsWith("ASSET_")
        ? error.code
        : "ASSET_DEPENDENCY_UNAVAILABLE",
      _redactDependencyMessage(error && error.message || error),
      { operation, retryable: Boolean(error && error.retryable), cause: error },
    );
  return new AssetRetrievalUnavailableError(
    context && context.message || "asset retrieval is unavailable",
    { operation, causes: [cause] },
  );
}

function serializeAssetRetrievalError(error) {
  const typed = normalizeAssetRetrievalError(error);
  return {
    code: typed.code,
    message: _redactDependencyMessage(typed.message),
    retryable: Boolean(typed.retryable),
    causes: typed.causes.map((cause) => {
      const serialized = serializeDependencyCause(cause);
      return { ...serialized, message: _redactDependencyMessage(serialized.message) };
    }),
  };
}

function assetFailureDecision(error, policyInput) {
  const policy = _asAssetPolicy(policyInput);
  const typed = normalizeAssetRetrievalError(error);
  const reason = serializeAssetRetrievalError(typed);
  if (policy.allow_degraded_assets) {
    return {
      action: "degrade",
      policy,
      metadata: {
        status: "degraded",
        mode: policy.mode,
        degraded_mode: policy.degraded_mode,
        require_real_assets: false,
        reason,
      },
    };
  }
  return {
    action: "block",
    policy,
    error: typed,
    metadata: {
      status: "blocked",
      mode: policy.mode,
      degraded_mode: "disabled",
      require_real_assets: policy.require_real_assets,
      reason,
    },
  };
}

function _usePrefilter(opts) {
  if (opts && typeof opts.usePrefilter === "boolean") return opts.usePrefilter;
  return _legacyPrefilterDefault();
}

function _cleanSettings(values) {
  const out = [];
  for (const v of Array.isArray(values) ? values : []) {
    const s = String(v || "").trim();
    if (SETTING_VALUES.has(s) && !out.includes(s)) out.push(s);
  }
  return out;
}

function _cleanTerms(values, max) {
  return (Array.isArray(values) ? values : [])
    .map(v => String(v || "").trim())
    .filter(Boolean)
    .slice(0, max || 8);
}

function _normalizePlan(raw, scene) {
  const p = raw && raw.plan ? raw.plan : (raw || {});
  return {
    semantic_query: String(p.semantic_query || scene || "").trim(),
    primary_settings: _cleanSettings(p.primary_settings),
    hard_exclude_settings: _cleanSettings(p.hard_exclude_settings),
    must_terms: _cleanTerms(p.must_terms, 8),
    avoid_terms: _cleanTerms(p.avoid_terms, 6),
  };
}

function getAssetSnapshotRevision(db, opts) {
  const o = opts || {};
  return String(
    o.snapshotRevision
      || o.assetRevision
      || process.env.ASSET_SNAPSHOT_REVISION
      || process.env.ASSET_CATALOG_REVISION
      || (db && db.revision)
      || "unversioned",
  );
}

let _verifiedAssetRuntimeConfig = null;
function withVerifiedAssetRuntimeOptions(opts) {
  const provided = opts || {};
  if (provided.assertLiveAuditFresh !== undefined || !_truthy(process.env.VISTA_ASSET_RESOLUTION_ENABLED)) {
    return provided;
  }
  if (!_verifiedAssetRuntimeConfig) {
    _verifiedAssetRuntimeConfig = require("./vista-asset-runtime")
      .resolveVistaAssetRuntimeConfig(process.env);
  }
  if (!_verifiedAssetRuntimeConfig.enabled) return provided;
  return {
    ...provided,
    snapshotRevision: _verifiedAssetRuntimeConfig.snapshotId,
    assetSnapshotRevision: _verifiedAssetRuntimeConfig.snapshotId,
    assertLiveAuditFresh: _verifiedAssetRuntimeConfig.assertLiveAuditFresh,
    postgresUrl: _verifiedAssetRuntimeConfig.postgresUrl,
  };
}

function buildRetrievalCacheKey(scene, opts, revision) {
  const usePrefilter = _usePrefilter(opts);
  return JSON.stringify({
    scene: String(scene || ""),
    model: String((opts && opts.model) || ""),
    prefilter: usePrefilter,
    topK: PREFILTER_TOP_K,
    collection: String((opts && opts.collection) || process.env.QDRANT_COLLECTION || "assets"),
    embedVersion: String((opts && opts.embedVersion) || process.env.EMBED_VERSION || "bge-large-en-v1.5-bm25-v1"),
    snapshotRevision: String(revision || getAssetSnapshotRevision(null, opts)),
  });
}

function loadDB() {
  const indexPath = path.join(ASSET_DB_DIR, "category_index.json");
  const stat = fs.statSync(indexPath);
  const configuredRevision = process.env.ASSET_SNAPSHOT_REVISION || process.env.ASSET_CATALOG_REVISION || "";
  const sourceKey = JSON.stringify({
    dir: ASSET_DB_DIR,
    configuredRevision,
    size: stat.size,
    mtimeMs: Math.trunc(stat.mtimeMs),
  });
  if (_cache && _cache.sourceKey === sourceKey) return _cache;
  const idx = JSON.parse(fs.readFileSync(indexPath, "utf-8"));
  if (!idx || !Array.isArray(idx.categories)) throw new Error("asset category index has no categories array");
  const assets = new Map();
  const byCat = new Map();
  for (const cat of idx.categories) {
    const list = [];
    for (const a of cat.assets) {
      const fp = path.join(ASSET_DB_DIR, "catalog", cat.id, a.asset_id + ".json");
      let rec; try { rec = JSON.parse(fs.readFileSync(fp, "utf-8")); } catch { continue; }
      const sem = rec.semantic || {}, geo = rec.geometry || {}, tech = rec.technical || {};
      const compact = {
        id: rec.identity.asset_id,
        name: rec.identity.name,
        category: cat.id,
        subcategory: rec.identity.subcategory || "",
        desc: sem.short_description || "",
        tags: (sem.tags || []).slice(0, 8),
        sceneTypes: (sem.scene_types || []).slice(0, 5),
        setting: sem.setting || "generic",
        dims: geo.dimensions_m || null,
        path: tech.unreal_asset_path || "",
        assetType: tech.asset_type || "",
        spawnTool: _spawnTool(rec),
      };
      if (_DROP_JUNK && _isJunkAsset(compact)) continue;
      assets.set(compact.id, compact);
      list.push(compact);
    }
    byCat.set(cat.id, { id: cat.id, description: cat.description, count: list.length, assets: list });
  }
  if (!assets.size) throw new Error("asset DB empty at " + ASSET_DB_DIR);
  const declaredRevision = idx.snapshot_revision
    || idx.snapshotRevision
    || idx.revision
    || idx.checksum
    || (idx.manifest && (idx.manifest.revision || idx.manifest.checksum));
  const revision = String(
    configuredRevision
      || declaredRevision
      || `category-index:${stat.size}:${Math.trunc(stat.mtimeMs)}`,
  );
  if (_cache && (_cache.revision !== revision || _cache.sourceKey !== sourceKey)) _retrievalCache.clear();
  _cache = {
    categories: idx.categories.map(c => ({ id: c.id, description: c.description, count: c.count })),
    byCat, assets,
    revision,
    sourceKey,
  };
  return _cache;
}

// ── stage 1: category routing ────────────────────────────────────────────────
async function routeCategories(scene, db, opts) {
  const cats = db.categories.map(c => `- ${c.id} (${c.count}): ${c.description}`).join("\n");
  const settings = [...SETTING_VALUES].join(", ");
  const prompt = [
    "You are the CATEGORY ROUTER for a 3D scene asset retriever spanning many settings (modern urban, industrial, suburban, retail, rural/nature, harbor, medieval, fantasy/gothic, temple, middle-eastern, east-asian, winter, sci-fi, indoor).",
    "Given a scene description and a list of asset categories, choose EVERY category that could plausibly contribute assets to this scene. Be inclusive about plausibly-relevant categories, but skip categories clearly irrelevant to this scene.",
    "Do NOT limit the number of categories — pick as many as genuinely fit.",
    "",
    "Also produce a retrieval plan for DB/vector prefiltering:",
    "- semantic_query: concise search-optimized description with key nouns, adjectives, style and genre words.",
    "- primary_settings: 1-3 setting enum values that best match the scene.",
    "- hard_exclude_settings: setting enum values that are clearly wrong for the scene.",
    "- must_terms: 3-8 key object/concept terms that good assets should match.",
    "- avoid_terms: 2-6 wrong-genre terms to avoid.",
    `Setting enum values: ${settings}`,
    "",
    "SCENE:", scene, "",
    "CATEGORIES  (id (asset_count): description):", cats, "",
    'Output ONLY JSON, no prose: {"categories":[{"id":"<category id>","emphasis":"primary|secondary","reason":"<short>"}],"semantic_query":"<search query>","primary_settings":["<setting>"],"hard_exclude_settings":["<setting>"],"must_terms":["<term>"],"avoid_terms":["<term>"]}',
  ].join("\n");
  const out = await oneshotJSON(prompt, opts);
  const valid = new Set(db.categories.map(c => c.id));
  const seen = new Set(), uniq = [];
  const rawCats = Array.isArray(out) ? out : (Array.isArray(out.categories) ? out.categories : []);
  for (const c of rawCats) {
    if (c && valid.has(c.id) && !seen.has(c.id)) { seen.add(c.id); uniq.push(c); }
  }
  return { categories: uniq, plan: _normalizePlan(out, scene) };
}

// ── stage 2: per-category selection (one call per category, run in parallel) ──
async function selectInCategory(scene, cat, emphasis, opts) {
  const perCatCap = Math.max(1, Number(process.env.ASSET_SELECT_PER_CAT_CAP || 15));
  const items = cat.assets.map(a =>
    `- ${a.id} | ${a.name} | ${a.subcategory} | ${a.setting} | ${a.desc} | tags: ${a.tags.join(",")}`
  ).join("\n");
  const prompt = [
    `You are selecting assets from ONE category ("${cat.id}") for a 3D scene.`,
    `This category was flagged as ${emphasis || "relevant"} for the scene.`,
    "Pick the assets that genuinely fit this scene's setting, era, mood and function. EXCLUDE assets that belong to a different setting/genre (e.g. sci-fi consoles in a medieval market, ornate temple decor in an industrial yard, or snow props in a desert bazaar). Use the per-asset 'setting' tag as a strong signal, but trust the description/tags when a 'generic' prop genuinely fits.",
    `Select the BEST-FITTING, DISTINCT assets only — avoid near-duplicates (do NOT pick 20 near-identical trash cans / lamps / fences; pick the few best of each kind). Aim for roughly the ${perCatCap} most relevant or fewer. If none fit, return an empty list.`,
    "",
    "SCENE:", scene, "",
    `CATEGORY "${cat.id}" — ${cat.description}`,
    "ASSETS  (id | name | subcategory | setting | description | tags):",
    items, "",
    'Output ONLY JSON, no prose: {"selected":[{"id":"<asset id>","reason":"<short>"}]}',
  ].join("\n");
  const out = await oneshotJSON(prompt, opts);
  const valid = new Set(cat.assets.map(a => a.id));
  return (out.selected || []).filter(s => s && valid.has(s.id)).slice(0, perCatCap);
}

// ── stage 3: aggregation / final curation ────────────────────────────────────
async function aggregate(scene, picked, db, opts) {
  const finalCap = Math.max(5, Number(process.env.ASSET_FINAL_CAP || 70));
  const lines = picked.map(p => {
    const a = db.assets.get(p.id);
    return `- ${a.id} | ${a.category} | ${a.name} | ${a.desc}`;
  }).join("\n");
  const prompt = [
    "You are the final CURATOR assembling a coherent asset palette for a 3D OUTDOOR scene.",
    "From the candidate assets below (already pre-filtered per category), produce the FINAL palette:",
    "- Drop anything off-theme or stylistically inconsistent with the rest of the set.",
    "- Keep the set coherent (consistent era / mood / setting).",
    "- ALWAYS keep at least one large ground/floor/paving/grass/snow surface (category ground_and_road) as the base ground — NEVER drop every ground asset, or the scene has no floor to stand on.",
    "- Order by importance: ground/base surface and primary structures & backdrop first, then mid-ground, then small accents / clutter.",
    `Keep a FOCUSED, coherent palette of DISTINCT assets — drop near-duplicates. Aim for roughly ${finalCap} or fewer (a rich, lively scene needs more variety; a sparse scene fewer), ordered by importance.`,
    "",
    "SCENE:", scene, "",
    "CANDIDATES  (id | category | name | description):", lines, "",
    'Output ONLY JSON, no prose: {"scene_rationale":"<1-2 sentences>","final":["<asset id>","<asset id>"]}  (final = the ordered list of asset ids to keep)',
  ].join("\n");
  const out = await oneshotJSON(prompt, { ...opts, timeoutMs: Math.max(Number(opts.timeoutMs) || 0, 300000) });
  const valid = new Set(picked.map(p => p.id));
  let ids = (out.final || []).map(f => (typeof f === "string" ? f : (f && f.id))).filter(id => valid.has(id)).slice(0, finalCap);
  // ground guarantee: if the curator dropped every ground/floor asset, re-inject the
  // best 1-2 ground candidates so the scene always has a base surface to lay.
  const isGround = id => { const a = db.assets.get(id); return a && a.category === "ground_and_road"; };
  if (!ids.some(isGround)) {
    const groundCandidates = picked.filter(p => isGround(p.id)).map(p => p.id);
    if (groundCandidates.length) ids = [...groundCandidates.slice(0, 2), ...ids.filter(id => !groundCandidates.includes(id))].slice(0, finalCap);
  }
  return { rationale: out.scene_rationale || "", final: ids.map(id => ({ id })) };
}

function _dependencyCausesFromErrors(errors) {
  const causes = [];
  for (const error of errors || []) {
    const nested = Array.isArray(error && error.causes)
      ? error.causes
      : (Array.isArray(error && error.details && error.details.causes) ? error.details.causes : []);
    if (nested.length) {
      causes.push(...nested);
    } else {
      causes.push(new AssetDependencyError(
        error && error.dependency || "retrieval",
        error && error.code || "ASSET_DEPENDENCY_UNAVAILABLE",
        _redactDependencyMessage(error && error.message || error),
        { operation: error && error.operation || "prefilter", retryable: Boolean(error && error.retryable) },
      ));
    }
  }
  return causes;
}

// ── orchestrator ─────────────────────────────────────────────────────────────
async function retrieve(scene, opts) {
  const o = Object.assign({ reasoningEffort: process.env.ASSET_RETRIEVAL_REASONING_EFFORT || "medium", telemetryComponent: "retrieval" }, opts || {});
  if (o.assertLiveAuditFresh !== undefined) {
    if (typeof o.assertLiveAuditFresh !== "function") throw new TypeError("assertLiveAuditFresh must be a function");
    // Validate before consulting the prompt cache so an expired live audit
    // receipt cannot keep serving a previously cached palette.
    o.assertLiveAuditFresh();
  }
  const log = o.log || (() => {});
  const usePrefilter = _usePrefilter(o);
  const db = loadDB();
  const revision = getAssetSnapshotRevision(db, o);
  const cacheKey = buildRetrievalCacheKey(scene, o, revision);
  if (_retrievalCache.has(cacheKey)) { log("retrieval cache HIT (reusing this scene's prior result)"); return _retrievalCache.get(cacheKey); }
  const trace = { revision, routed: [], plan: {}, perCategory: {}, prefilter: {}, prefilterTelemetry: {} };
  const prefilterFailures = [];

  const routeResult = await routeCategories(scene, db, o);
  const routed = Array.isArray(routeResult) ? routeResult : (routeResult.categories || []);
  const plan = _normalizePlan(routeResult, scene);
  // ── ground guarantee ────────────────────────────────────────────────────────
  // Always route a ground/surface category so the palette has a base floor to lay.
  // Without this, db-mode scenes can end up with no ground asset and render on the
  // bare default grey plane (the off arm always finds a ground by browsing live UE).
  {
    const force = ["ground_and_road"];
    const wantsWinter = /\b(snow|snowy|winter|ice|icy|frost|frozen)\b/i.test(scene) ||
      (plan.primary_settings || []).includes("winter");
    if (wantsWinter && db.byCat.get("winter_snow_props")) force.push("winter_snow_props");
    for (const id of force) {
      if (!db.byCat.get(id)) continue;
      const existing = routed.find(x => x.id === id);
      if (existing) existing.emphasis = "primary";        // bump so selection is generous
      else routed.push({ id, emphasis: "primary", reason: "ground-guarantee (forced)" });
    }
  }
  trace.routed = routed;
  trace.plan = plan;
  log("routed: " + routed.map(r => `${r.id}(${r.emphasis})`).join(", "));
  if (usePrefilter) log(`prefilter enabled: topK=${PREFILTER_TOP_K}`);

  const results = await Promise.all(routed.map(async r => {
    const cat = db.byCat.get(r.id);
    if (!cat) return [];
    let candidates = cat;
    if (usePrefilter) {
      try {
        const { prefilterCategory } = require("./asset-retrieval-db");
        const pref = await prefilterCategory(r.id, plan, {
          ...o,
          topK: PREFILTER_TOP_K,
          log,
          assetSnapshotRevision: revision,
        });
        if (pref.retrieval) trace.prefilterTelemetry[r.id] = pref.retrieval;
        const seenIds = new Set();
        const compact = [];
        for (const p of pref) {
          if (!p || !p.id || seenIds.has(p.id)) continue;
          const a = db.assets.get(p.id);
          if (a) { seenIds.add(p.id); compact.push(a); }
        }
        if (!compact.length) {
          throw new AssetDependencyError(
            "catalog",
            "ASSET_SNAPSHOT_REVISION_MISMATCH",
            `prefilter returned assets outside the loaded catalog revision for ${r.id}`,
            { operation: "join_prefilter_catalog", retryable: false },
          );
        }
        candidates = { ...cat, count: compact.length, assets: compact };
        trace.prefilter[r.id] = compact.map(a => a.id);
        log(`prefilter ${r.id}: ${compact.length} candidates (from ${cat.count})`);
      } catch (e) {
        const errorSummary = serializeAssetRetrievalError(e);
        if (FULL_FALLBACK_MAX > 0 && cat.count <= FULL_FALLBACK_MAX) {
          trace.prefilter[r.id] = { fallback: "full_category", error: errorSummary };
          log(`prefilter ${r.id} FAILED (${errorSummary.code}); using bounded full-list fallback ${cat.count}/${FULL_FALLBACK_MAX}`);
          candidates = cat;
        } else {
          // Non-fatal: one off-theme/empty category (e.g. router pulled in winter_snow_props
          // for a non-winter scene, then excluded the winter setting → 0 candidates) must NOT
          // abort the whole retrieval. Skip just this category. A true infra outage fails every
          // category → empty palette → buildPromptBlock throws, so real failures still surface.
          trace.prefilter[r.id] = { skipped: true, error: errorSummary };
          prefilterFailures.push(e);
          log(`prefilter ${r.id} FAILED (${errorSummary.code}); skipping this category`);
          return [];
        }
      }
    }
    try {
      const sel = await selectInCategory(scene, candidates, r.emphasis, o);
      trace.perCategory[r.id] = sel.map(s => s.id);
      log(`select ${r.id}: ${sel.length}/${candidates.assets.length} candidates (category total ${cat.count}) -> ${sel.map(s => s.id).join(",")}`);
      return sel.map(s => ({ ...s, category: r.id }));
    } catch (e) { log(`select ${r.id} FAILED: ${e.message}`); return []; }
  }));

  const seen = new Set(), picked = [];
  for (const p of results.flat()) if (!seen.has(p.id)) { seen.add(p.id); picked.push(p); }
  log(`picked total: ${picked.length}`);
  if (!picked.length && prefilterFailures.length) {
    throw new AssetRetrievalUnavailableError("asset prefilter dependencies are unavailable", {
      operation: "retrieve",
      causes: _dependencyCausesFromErrors(prefilterFailures),
    });
  }
  if (!picked.length) return { final: [], rationale: "", trace, assets: [], revision };

  let rationale = "", final;
  try {
    const agg = await aggregate(scene, picked, db, o);
    rationale = agg.rationale; final = agg.final;
    if (!final.length) throw new Error("aggregate returned empty");
  } catch (e) {
    log(`aggregate FAILED (${e.message}) — falling back to stage-2 union`);
    final = picked.map(p => ({ id: p.id }));
    rationale = "(aggregate unavailable; used per-category selections)";
  }
  log(`final: ${final.length} -> ${final.map(f => f.id).join(",")}`);
  const assets = final.map(f => ({ ...db.assets.get(f.id), role: f.role }));
  const result = { final, rationale, trace, assets, revision };
  _retrievalCache.set(cacheKey, result);
  return result;
}

// ── prompt formatting ────────────────────────────────────────────────────────
function _fmtDims(d) {
  if (!d) return "?";
  const r = x => (Math.round(Number(x) * 10) / 10);
  return `${r(d.width)}×${r(d.depth)}×${r(d.height)}m`;
}

function formatAssetsForPrompt(assets) {
  const byCat = new Map();
  for (const a of assets) { if (!byCat.has(a.category)) byCat.set(a.category, []); byCat.get(a.category).push(a); }
  const lines = [];
  for (const [cat, list] of byCat) {
    lines.push(`### ${cat} (${list.length})`);
    for (const a of list) {
      const role = a.role ? ` — ${a.role}` : "";
      lines.push(`- ${a.name}${role}: ${a.desc} · ${a.spawnTool} "${a.path}" · ~${_fmtDims(a.dims)}`);
    }
  }
  return lines.join("\n");
}

// Curated, verified ground-surface MATERIALS (object paths confirmed to load in this
// UE project). Used as a fallback to carpet large areas when no full-coverage ground
// MESH fits the setting (e.g. a snow field or sand lot). Applied to a tiled grid of
// flat planes. Meshes (with baked UVs) are preferred; materials are the safety net.
const _GROUND_MATERIALS = [
  { surface: "grass / lawn", path: "/Game/SuburbNeighborhoodHousePack/Materials/MI_Floor_Grass.MI_Floor_Grass" },
  { surface: "grass lawn (large-scale)", path: "/Game/midmanhattan/Materials/KB3D_MIM_GrassLawn.KB3D_MIM_GrassLawn" },
  { surface: "snow", path: "/Game/Village/Materials/MI_Snow01.MI_Snow01" },
  { surface: "sand", path: "/Game/Downtown_West/Materials/Ground_Shared/MI_Sand_Ground_A.MI_Sand_Ground_A" },
  { surface: "dirt / gravel", path: "/Game/ModularBuildingSet/materials/Ground_Rubble/ground_gravel_dirt.ground_gravel_dirt" },
  { surface: "gravel path", path: "/Game/EnglishCollege/Materials/Pathways/M_Gravel.M_Gravel" },
  { surface: "cobblestone", path: "/Game/UrbanDistrict/Environment/Cobble_01/mi_Cobble_01_01.mi_Cobble_01_01" },
  { surface: "asphalt", path: "/Game/CityDatabase/materials/M_Asphalt_Master_Inst.M_Asphalt_Master_Inst" },
  { surface: "concrete", path: "/Game/UrbanDistrict/Environment/GroundConcrete_01/mi_GroundConcrete_01_01.mi_GroundConcrete_01_01" },
];

function _groundMaterialsBlock() {
  const lines = _GROUND_MATERIALS.map(m => `- ${m.surface}: ${m.path}`);
  return [
    "### GROUND SURFACE MATERIALS (fallback floor — use if no ground/floor MESH in the palette fits the setting)",
    ...lines,
  ].join("\n");
}

const _HOWTO = 'This is a CURATED PALETTE of assets selected for this scene — build the scene from it. Spawn each by its EXACT full path shown: spawn_blueprint_actor for Blueprints, spawn_actor for static meshes (as labelled). Dimensions are width×depth×height in metres (UE: 1 m = 100 units) — use them for spacing/overlaps.\n\nGROUND FIRST (do this before any props): carpet the WHOLE ~100 m × 100 m floor with a base ground matched to the scene, so nothing sits on the bare default grey plane. PREFER tiling the ground/floor MESHES in the palette (category ground_and_road — grass tiles, park-walkway slabs, snowy-road tiles, plaza/stone-floor pieces have baked UVs and tile cleanly): repeat them edge-to-edge across the full 100 m at z≈0. If no palette ground MESH fits the setting, instead spawn a grid of flat base planes (/Engine/BasicShapes/Plane, scaled ~4–8 m each, tiled to cover 100×100 m at z≈0) and apply the best-matching GROUND SURFACE MATERIAL listed below via StaticMeshComponent.set_material(0, material) so the texture tiles instead of stretching. NEVER use ocean/sea/water as the floor (water only as a separate edge feature). Then place EVERY object ON the ground, upright.\n\nNow build a FULL ~100 m × 100 m (≈10000×10000 UE units, X/Y roughly -5000..+5000), DENSE, lived-in scene: LEAD with the large/structural assets in the palette (whole buildings, houses, stalls, walls, big trees, large set-pieces like fountains/cranes/gates) as the backbone and REUSE them in rows and clusters DISTRIBUTED across the WHOLE 100×100 m (something roughly every 8–12 m, aim ~80–150+ assets total, NOT clustered in one corner); add smaller props only as light dressing, never the bulk. Use execute_python_script to place many instances efficiently. Aim for a busy, instantly-recognizable ~100 m place — dense and organized around a clear focal point with natural variation, NOT a sparse handful and NOT a random pile.';

// HYBRID mode HOWTO: the retrieved palette is a non-exclusive SEED, and the builder is
// told it ALSO has the search_assets tool to pull more relevant assets on demand. This
// keeps db's relevance prior while removing the over-constraint / missing-category /
// junk failure modes — the model can always fetch the right ground or fill any gap.
const _HOWTO_HYBRID = 'The list below is a SEED PALETTE — a high-relevance STARTING SET retrieved for this scene. Lead with it, but you are NOT limited to it.\n\nYou ALSO have a search_assets tool: call it any time to pull MORE relevant real assets from the full ~16k-asset library — give it a natural-language query (e.g. "snow covered ground", "leafy park trees", "fruit market stall", "stone temple gate") and it returns real assets with exact spawn paths + dimensions. USE IT for: (a) the correct GROUND for this setting (always search e.g. "grass lawn ground" / "snow ground tiles" / "cobblestone" if the seed lacks a fitting ground MESH), (b) any category the seed is missing, and (c) extra variety so the scene is not repetitive. NEVER skip or fake a needed asset because it is not in the seed — search_assets it (or browse a specific pack via list_assets / execute_python_script EditorAssetLibrary.list_assets). Spawn Blueprints with spawn_blueprint_actor and static meshes with spawn_actor, by EXACT path. Dimensions are width×depth×height in metres (1 m = 100 units).\n\nGROUND FIRST (before any props): carpet the WHOLE ~100 m × 100 m floor with a base ground matched to the scene so nothing sits on bare default grey. PREFER tiling ground/floor MESHES (grass tiles, park-walkway slabs, snowy-road tiles, plaza/stone-floor pieces — from the seed or from search_assets) edge-to-edge across the full 100 m at z≈0. If no ground MESH fits, spawn a grid of flat planes (/Engine/BasicShapes/Plane, ~4–8 m each, tiled to cover 100×100 m at z≈0) and apply the best-matching GROUND SURFACE MATERIAL below via StaticMeshComponent.set_material(0, material). NEVER use ocean/sea/water as the floor. Then place EVERY object ON the ground, upright.\n\nNow build a FULL ~100 m × 100 m (≈10000×10000 UE units, X/Y roughly -5000..+5000), DENSE, lived-in scene: LEAD with large/structural assets (whole buildings, houses, stalls, walls, big trees, large set-pieces) as the backbone and REUSE them in rows and clusters DISTRIBUTED across the WHOLE 100×100 m (something roughly every 8–12 m, aim ~80–150+ assets total, NOT clustered in one corner); add smaller props only as light dressing. Use execute_python_script to place many instances efficiently. Aim for a busy, instantly-recognizable ~100 m place — dense and organized around a clear focal point with natural variation.';

// Public entry used by /api/chat. Returns a system-prompt block (string), or "" if nothing.
async function buildPromptBlock(scene, mode, opts) {
  const resolvedMode = resolveAssetMode(mode);
  if (resolvedMode === "off") return "";
  const db = loadDB();
  if (resolvedMode === "hybrid") {
    const r = await retrieve(scene, { ...(opts || {}), usePrefilter: true });
    const seed = (r.assets || []).filter(a => !_isJunkAsset(a));
    if (!seed.length) {
      throw new AssetRetrievalUnavailableError("asset retrieval produced an empty seed palette", {
        operation: "build_prompt",
        causes: [new AssetDependencyError(
          "catalog",
          "ASSET_RETRIEVAL_EMPTY",
          "no buildable real assets remained after retrieval",
          { operation: "filter_seed", retryable: false },
        )],
      });
    }
    const body = formatAssetsForPrompt(seed);
    return [
      "## SEED ASSET PALETTE FOR THIS SCENE (high-relevance starting set — NOT an exclusive list)",
      r.rationale ? ("Scene rationale: " + r.rationale) : "",
      _HOWTO_HYBRID, "", body, "", _groundMaterialsBlock(),
    ].filter(Boolean).join("\n");
  }
  if (resolvedMode === "baseline_full") {
    const body = formatAssetsForPrompt([...db.assets.values()]);
    return ["## AVAILABLE ASSET PALETTE (build the scene using these curated assets)", _HOWTO, "", body, "", _groundMaterialsBlock()].join("\n");
  }
  const r = await retrieve(scene, { ...(opts || {}), usePrefilter: resolvedMode === "db" });
  if (!r.assets.length) {
    throw new AssetRetrievalUnavailableError("asset retrieval produced an empty palette", {
      operation: "build_prompt",
      causes: [new AssetDependencyError(
        "catalog",
        "ASSET_RETRIEVAL_EMPTY",
        "no buildable real assets were selected",
        { operation: "select_assets", retryable: false },
      )],
    });
  }
  const body = formatAssetsForPrompt(r.assets);
  return [
    "## RETRIEVED ASSETS FOR THIS SCENE (curated specifically for your prompt)",
    r.rationale ? ("Scene rationale: " + r.rationale) : "",
    _HOWTO, "", body, "", _groundMaterialsBlock(),
  ].filter(Boolean).join("\n");
}

async function buildPromptBlockWithPolicy(scene, policyInput, opts) {
  const policy = _asAssetPolicy(policyInput);
  const runtimeOptions = policy.mode === "off" ? (opts || {}) : withVerifiedAssetRuntimeOptions(opts);
  const revision = () => getAssetSnapshotRevision(_cache, runtimeOptions);
  if (policy.mode === "off") {
    return {
      promptBlock: "",
      metadata: {
        status: "off",
        mode: "off",
        require_real_assets: false,
        degraded_mode: "disabled",
        snapshot_revision: revision(),
      },
    };
  }
  try {
    const promptBlock = await buildPromptBlock(scene, policy.mode, runtimeOptions);
    return {
      promptBlock,
      metadata: {
        status: "ready",
        mode: policy.mode,
        require_real_assets: policy.require_real_assets,
        degraded_mode: "disabled",
        snapshot_revision: revision(),
      },
    };
  } catch (error) {
    const decision = assetFailureDecision(error, policy);
    const metadata = { ...decision.metadata, snapshot_revision: revision() };
    if (decision.action === "degrade") return { promptBlock: "", metadata };
    decision.error.retrieval = metadata;
    throw decision.error;
  }
}

module.exports = {
  AssetRetrievalPolicyError,
  assetFailureDecision,
  buildPromptBlock,
  buildPromptBlockWithPolicy,
  buildRetrievalCacheKey,
  formatAssetsForPrompt,
  getAssetSnapshotRevision,
  loadDB,
  normalizeAssetRetrievalError,
  resolveAssetMode,
  resolveAssetPolicy,
  retrieve,
  serializeAssetRetrievalError,
  withVerifiedAssetRuntimeOptions,
};

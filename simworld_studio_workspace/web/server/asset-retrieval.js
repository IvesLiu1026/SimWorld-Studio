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

const ASSET_DB_DIR = process.env.ASSET_DB_DIR || "/data/siddhant/asset_db";
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

function _legacyPrefilterDefault() {
  return _truthy(process.env.ASSET_PREFILTER);
}

function _normalizeAssetModeValue(v) {
  const s = String(v || "").trim().toLowerCase();
  if (!s) return null;
  if (["off", "none", "no", "false", "0", "disabled"].includes(s)) return "off";
  if (["db", "qdrant", "prefilter", "vector", "hybrid"].includes(s)) return "db";
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
  return "db";
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

function _retrievalCacheKey(scene, opts) {
  const usePrefilter = _usePrefilter(opts);
  return JSON.stringify({
    scene: String(scene || ""),
    model: String((opts && opts.model) || ""),
    prefilter: usePrefilter,
    topK: PREFILTER_TOP_K,
    collection: process.env.QDRANT_COLLECTION || "assets",
    embedVersion: process.env.EMBED_VERSION || "bge-large-en-v1.5-bm25-v1",
  });
}

function loadDB() {
  if (_cache) return _cache;
  const idx = JSON.parse(fs.readFileSync(path.join(ASSET_DB_DIR, "category_index.json"), "utf-8"));
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
      assets.set(compact.id, compact);
      list.push(compact);
    }
    byCat.set(cat.id, { id: cat.id, description: cat.description, count: list.length, assets: list });
  }
  if (!assets.size) throw new Error("asset DB empty at " + ASSET_DB_DIR);
  _cache = {
    categories: idx.categories.map(c => ({ id: c.id, description: c.description, count: c.count })),
    byCat, assets,
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
  const items = cat.assets.map(a =>
    `- ${a.id} | ${a.name} | ${a.subcategory} | ${a.setting} | ${a.desc} | tags: ${a.tags.join(",")}`
  ).join("\n");
  const prompt = [
    `You are selecting assets from ONE category ("${cat.id}") for a 3D scene.`,
    `This category was flagged as ${emphasis || "relevant"} for the scene.`,
    "Pick the assets that genuinely fit this scene's setting, era, mood and function. EXCLUDE assets that belong to a different setting/genre (e.g. sci-fi consoles in a medieval market, ornate temple decor in an industrial yard, or snow props in a desert bazaar). Use the per-asset 'setting' tag as a strong signal, but trust the description/tags when a 'generic' prop genuinely fits.",
    "Do NOT limit the count — include EVERY asset that fits and exclude every asset that does not. If none fit, return an empty list.",
    "",
    "SCENE:", scene, "",
    `CATEGORY "${cat.id}" — ${cat.description}`,
    "ASSETS  (id | name | subcategory | setting | description | tags):",
    items, "",
    'Output ONLY JSON, no prose: {"selected":[{"id":"<asset id>","reason":"<short>"}]}',
  ].join("\n");
  const out = await oneshotJSON(prompt, opts);
  const valid = new Set(cat.assets.map(a => a.id));
  return (out.selected || []).filter(s => s && valid.has(s.id));
}

// ── stage 3: aggregation / final curation ────────────────────────────────────
async function aggregate(scene, picked, db, opts) {
  const lines = picked.map(p => {
    const a = db.assets.get(p.id);
    return `- ${a.id} | ${a.category} | ${a.name} | ${a.desc}`;
  }).join("\n");
  const prompt = [
    "You are the final CURATOR assembling a coherent asset palette for a 3D OUTDOOR scene.",
    "From the candidate assets below (already pre-filtered per category), produce the FINAL palette:",
    "- Drop anything off-theme or stylistically inconsistent with the rest of the set.",
    "- Keep the set coherent (consistent era / mood / setting).",
    "- Order by importance: primary structures & backdrop first, then mid-ground, then small accents / clutter.",
    "Do NOT impose a fixed size — keep exactly as many distinct assets as the scene genuinely needs (a rich, lively scene needs many types; a sparse scene needs few).",
    "",
    "SCENE:", scene, "",
    "CANDIDATES  (id | category | name | description):", lines, "",
    'Output ONLY JSON, no prose: {"scene_rationale":"<1-2 sentences>","final":["<asset id>","<asset id>"]}  (final = the ordered list of asset ids to keep)',
  ].join("\n");
  const out = await oneshotJSON(prompt, { ...opts, timeoutMs: Math.max(Number(opts.timeoutMs) || 0, 300000) });
  const valid = new Set(picked.map(p => p.id));
  const ids = (out.final || []).map(f => (typeof f === "string" ? f : (f && f.id))).filter(id => valid.has(id));
  return { rationale: out.scene_rationale || "", final: ids.map(id => ({ id })) };
}

// ── orchestrator ─────────────────────────────────────────────────────────────
async function retrieve(scene, opts) {
  const o = opts || {};
  const log = o.log || (() => {});
  const usePrefilter = _usePrefilter(o);
  const cacheKey = _retrievalCacheKey(scene, o);
  if (_retrievalCache.has(cacheKey)) { log("retrieval cache HIT (reusing this scene's prior result)"); return _retrievalCache.get(cacheKey); }
  const db = loadDB();
  const trace = { routed: [], plan: {}, perCategory: {}, prefilter: {} };

  const routeResult = await routeCategories(scene, db, o);
  const routed = Array.isArray(routeResult) ? routeResult : (routeResult.categories || []);
  const plan = _normalizePlan(routeResult, scene);
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
        const pref = await prefilterCategory(r.id, plan, { ...o, topK: PREFILTER_TOP_K, log });
        const seenIds = new Set();
        const compact = [];
        for (const p of pref) {
          if (!p || !p.id || seenIds.has(p.id)) continue;
          const a = db.assets.get(p.id);
          if (a) { seenIds.add(p.id); compact.push(a); }
        }
        if (!compact.length) throw new Error(`prefilter returned no in-memory assets for ${r.id}`);
        candidates = { ...cat, count: compact.length, assets: compact };
        trace.prefilter[r.id] = compact.map(a => a.id);
        log(`prefilter ${r.id}: ${compact.length} candidates (from ${cat.count})`);
      } catch (e) {
        if (FULL_FALLBACK_MAX > 0 && cat.count <= FULL_FALLBACK_MAX) {
          trace.prefilter[r.id] = { fallback: "full_category", error: e.message };
          log(`prefilter ${r.id} FAILED (${e.message}); using bounded full-list fallback ${cat.count}/${FULL_FALLBACK_MAX}`);
          candidates = cat;
        } else {
          throw e;
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
  if (!picked.length) return { final: [], rationale: "", trace, assets: [] };

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
  const result = { final, rationale, trace, assets };
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

const _HOWTO = 'These are the ONLY assets to use for this scene. IGNORE the generic CityDatabase / BP_Building_* lists and list_assets discovery mentioned earlier in this prompt — build the scene exclusively from the palette below. Spawn each asset by its EXACT full path shown: use spawn_blueprint_actor for Blueprints and spawn_actor for static meshes (as labelled per item). Do NOT spawn any asset whose path is not in this list. You MAY place multiple instances of an asset for a fuller scene. Dimensions are width×depth×height in metres (UE uses cm: 1 m = 100 units) — use them for spacing and to avoid overlaps.';

// Public entry used by /api/chat. Returns a system-prompt block (string), or "" if nothing.
async function buildPromptBlock(scene, mode, opts) {
  const resolvedMode = resolveAssetMode(mode);
  if (resolvedMode === "off") return "";
  const db = loadDB();
  if (resolvedMode === "baseline_full") {
    const body = formatAssetsForPrompt([...db.assets.values()]);
    return ["## AVAILABLE ASSET PALETTE (build the scene using these curated assets)", _HOWTO, "", body].join("\n");
  }
  const r = await retrieve(scene, { ...(opts || {}), usePrefilter: resolvedMode === "db" });
  if (!r.assets.length) {
    throw new Error("asset retrieval produced an empty palette");
  }
  const body = formatAssetsForPrompt(r.assets);
  return [
    "## RETRIEVED ASSETS FOR THIS SCENE (curated specifically for your prompt)",
    r.rationale ? ("Scene rationale: " + r.rationale) : "",
    _HOWTO, "", body,
  ].filter(Boolean).join("\n");
}

module.exports = { loadDB, retrieve, buildPromptBlock, formatAssetsForPrompt, resolveAssetMode };

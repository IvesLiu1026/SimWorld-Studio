"use strict";

const TOP_K = Math.max(1, parseInt(process.env.PREFILTER_TOP_K || "150", 10));
const COLLECTION = process.env.QDRANT_COLLECTION || "assets";
const QDRANT_URL = process.env.QDRANT_URL || "http://127.0.0.1:6333";
const EMBED_SERVICE_URL = process.env.EMBED_SERVICE_URL || "http://127.0.0.1:7777";
const DEFAULT_DEPENDENCY_TIMEOUT_MS = _positiveInt(process.env.ASSET_RETRIEVAL_TIMEOUT_MS, 5000);

const DEPENDENCY_TIMEOUT_ENV = Object.freeze({
  embedding: "ASSET_EMBED_TIMEOUT_MS",
  qdrant: "ASSET_QDRANT_TIMEOUT_MS",
  postgres: "ASSET_POSTGRES_TIMEOUT_MS",
});

const VALID_SETTINGS = new Set([
  "modern_urban", "industrial", "suburban_residential", "commercial_retail",
  "nature_rural", "coastal_harbor", "medieval", "fantasy_gothic",
  "ancient_temple", "middle_eastern", "east_asian", "winter", "sci_fi",
  "indoor", "generic",
]);

const SETTING_COMPAT = {
  medieval: {
    boost: ["medieval", "fantasy_gothic", "nature_rural", "generic"],
    allow: ["ancient_temple", "middle_eastern", "east_asian", "winter", "coastal_harbor"],
    exclude: ["sci_fi", "modern_urban", "industrial", "suburban_residential", "commercial_retail"],
  },
  fantasy_gothic: {
    boost: ["fantasy_gothic", "medieval", "generic"],
    allow: ["ancient_temple", "winter", "nature_rural", "middle_eastern"],
    exclude: ["sci_fi", "modern_urban", "industrial", "suburban_residential", "commercial_retail"],
  },
  ancient_temple: {
    boost: ["ancient_temple", "middle_eastern", "east_asian", "generic"],
    allow: ["medieval", "fantasy_gothic", "nature_rural", "coastal_harbor"],
    exclude: ["sci_fi", "modern_urban", "industrial", "winter"],
  },
  middle_eastern: {
    boost: ["middle_eastern", "ancient_temple", "generic"],
    allow: ["medieval", "east_asian", "nature_rural", "coastal_harbor", "commercial_retail"],
    exclude: ["sci_fi", "modern_urban", "industrial", "winter"],
  },
  east_asian: {
    boost: ["east_asian", "middle_eastern", "ancient_temple", "generic"],
    allow: ["medieval", "nature_rural", "coastal_harbor"],
    exclude: ["sci_fi", "modern_urban", "industrial"],
  },
  modern_urban: {
    boost: ["modern_urban", "industrial", "commercial_retail", "suburban_residential", "generic"],
    allow: ["coastal_harbor", "indoor"],
    exclude: ["medieval", "fantasy_gothic", "ancient_temple", "middle_eastern", "east_asian", "sci_fi", "winter"],
  },
  industrial: {
    boost: ["industrial", "modern_urban", "generic"],
    allow: ["coastal_harbor", "suburban_residential"],
    exclude: ["medieval", "fantasy_gothic", "ancient_temple", "middle_eastern", "east_asian", "sci_fi"],
  },
  suburban_residential: {
    boost: ["suburban_residential", "modern_urban", "commercial_retail", "generic"],
    allow: ["nature_rural", "indoor"],
    exclude: ["medieval", "fantasy_gothic", "sci_fi", "ancient_temple"],
  },
  commercial_retail: {
    boost: ["commercial_retail", "modern_urban", "suburban_residential", "generic"],
    allow: ["coastal_harbor", "indoor"],
    exclude: ["medieval", "fantasy_gothic", "sci_fi", "ancient_temple"],
  },
  coastal_harbor: {
    boost: ["coastal_harbor", "industrial", "modern_urban", "generic"],
    allow: ["nature_rural", "medieval", "commercial_retail"],
    exclude: ["sci_fi", "fantasy_gothic", "ancient_temple", "east_asian"],
  },
  nature_rural: {
    boost: ["nature_rural", "generic"],
    allow: ["medieval", "coastal_harbor", "suburban_residential", "winter"],
    exclude: ["sci_fi", "modern_urban", "industrial", "fantasy_gothic"],
  },
  winter: {
    boost: ["winter", "nature_rural", "generic"],
    allow: ["medieval", "fantasy_gothic", "suburban_residential"],
    exclude: ["sci_fi", "modern_urban", "industrial"],
  },
  sci_fi: {
    boost: ["sci_fi", "generic"],
    allow: ["industrial", "modern_urban", "indoor"],
    exclude: ["medieval", "fantasy_gothic", "ancient_temple", "middle_eastern", "east_asian", "nature_rural"],
  },
  indoor: {
    boost: ["indoor", "generic"],
    allow: ["modern_urban", "commercial_retail", "suburban_residential", "sci_fi"],
    exclude: [],
  },
  generic: { boost: ["generic"], allow: ["*"], exclude: [] },
};

class AssetDependencyError extends Error {
  constructor(dependency, code, message, details) {
    super(message);
    this.name = "AssetDependencyError";
    this.dependency = String(dependency || "unknown");
    this.code = String(code || "ASSET_DEPENDENCY_UNAVAILABLE");
    const d = details || {};
    this.operation = d.operation ? String(d.operation) : undefined;
    this.retryable = Boolean(d.retryable);
    this.timeoutMs = Number.isFinite(Number(d.timeoutMs)) ? Number(d.timeoutMs) : undefined;
    this.status = Number.isFinite(Number(d.status)) ? Number(d.status) : undefined;
    if (d.cause !== undefined) {
      Object.defineProperty(this, "cause", { value: d.cause, configurable: true });
    }
  }
}

function redactDependencyMessage(value) {
  return String(value || "asset dependency failed")
    .replace(/\b(postgres(?:ql)?|https?):\/\/[^\s/@]+:[^\s/@]+@/gi, "$1://[redacted]@")
    .replace(/\b(token|password|secret|api[_-]?key)=([^\s&]+)/gi, "$1=[redacted]");
}

function serializeDependencyCause(error) {
  const e = normalizeDependencyError(error, error && error.dependency, error && error.operation);
  return {
    dependency: e.dependency,
    code: e.code,
    message: redactDependencyMessage(e.message),
    retryable: e.retryable,
    ...(e.operation ? { operation: e.operation } : {}),
    ...(e.timeoutMs !== undefined ? { timeout_ms: e.timeoutMs } : {}),
    ...(e.status !== undefined ? { status: e.status } : {}),
  };
}

function attachRetrievalTelemetry(results, { source, causes = [] } = {}) {
  const assets = Array.isArray(results) ? results : [];
  const serializedCauses = causes.map(serializeDependencyCause);
  const telemetry = Object.freeze({
    schema: "asset-retrieval-telemetry/v1",
    status: serializedCauses.length ? "degraded" : "ready",
    source: source || "unknown",
    fallback_used: source === "postgres",
    causes: Object.freeze(serializedCauses),
  });
  Object.defineProperty(assets, "retrieval", {
    value: telemetry,
    configurable: false,
    enumerable: false,
    writable: false,
  });
  return assets;
}

class AssetRetrievalUnavailableError extends Error {
  constructor(message, details) {
    super(message || "asset retrieval is unavailable");
    this.name = "AssetRetrievalUnavailableError";
    this.code = "ASSET_RETRIEVAL_UNAVAILABLE";
    const d = details || {};
    this.operation = d.operation ? String(d.operation) : undefined;
    this.category = d.category ? String(d.category) : undefined;
    this.causes = (Array.isArray(d.causes) ? d.causes : [])
      .map((cause) => normalizeDependencyError(cause, cause && cause.dependency, cause && cause.operation));
    const failureCauses = this.causes.filter((cause) => cause.code !== "ASSET_DEPENDENCY_NO_RESULTS");
    this.retryable = failureCauses.length > 0 && failureCauses.every((cause) => cause.retryable);
    this.details = {
      ...(this.operation ? { operation: this.operation } : {}),
      ...(this.category ? { category: this.category } : {}),
      causes: this.causes.map(serializeDependencyCause),
      retryable: this.retryable,
    };
  }
}

class RetrievalPrefilterError extends AssetRetrievalUnavailableError {
  constructor(message, details) {
    super(message, { ...(details || {}), operation: "prefilter_category" });
    this.name = "RetrievalPrefilterError";
  }
}

function _positiveInt(value, fallback) {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : fallback;
}

function dependencyTimeoutMs(dependency, opts) {
  const o = opts || {};
  const optionName = `${dependency}TimeoutMs`;
  const envName = DEPENDENCY_TIMEOUT_ENV[dependency];
  return _positiveInt(
    o[optionName],
    _positiveInt(o.timeoutMs, _positiveInt(envName && process.env[envName], DEFAULT_DEPENDENCY_TIMEOUT_MS)),
  );
}

function normalizeDependencyError(error, dependency, operation) {
  if (error instanceof AssetDependencyError) return error;
  if (error && error.dependency && error.code && error.message) {
    return new AssetDependencyError(error.dependency, error.code, error.message, {
      operation: error.operation || operation,
      retryable: error.retryable,
      timeoutMs: error.timeout_ms !== undefined ? error.timeout_ms : error.timeoutMs,
      status: error.status,
      cause: error,
    });
  }
  const status = Number(error && (error.status || error.statusCode));
  const retryable = status === 408 || status === 429 || status >= 500 || !Number.isFinite(status);
  return new AssetDependencyError(
    dependency || "unknown",
    "ASSET_DEPENDENCY_UNAVAILABLE",
    String(error && error.message || error || `${dependency || "asset"} dependency failed`),
    { operation, retryable, ...(Number.isFinite(status) ? { status } : {}), cause: error },
  );
}

function dependencyNoResults(dependency, operation) {
  return new AssetDependencyError(
    dependency,
    "ASSET_DEPENDENCY_NO_RESULTS",
    `${dependency} returned no matching assets`,
    { operation, retryable: false },
  );
}

function runDependency(dependency, operation, opts, task) {
  const o = opts || {};
  const timeoutMs = dependencyTimeoutMs(dependency, o);
  const parentSignal = o.signal;
  const controller = new AbortController();

  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (fn, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (parentSignal) parentSignal.removeEventListener("abort", onAbort);
      fn(value);
    };
    const onAbort = () => {
      controller.abort(parentSignal && parentSignal.reason);
      finish(reject, new AssetDependencyError(
        dependency,
        "ASSET_DEPENDENCY_ABORTED",
        `${dependency} ${operation} was aborted`,
        { operation, retryable: false },
      ));
    };
    const timer = setTimeout(() => {
      controller.abort(new Error(`${dependency} ${operation} timed out`));
      finish(reject, new AssetDependencyError(
        dependency,
        "ASSET_DEPENDENCY_TIMEOUT",
        `${dependency} ${operation} timed out after ${timeoutMs}ms`,
        { operation, retryable: true, timeoutMs },
      ));
    }, timeoutMs);

    if (parentSignal && parentSignal.aborted) {
      onAbort();
      return;
    }
    if (parentSignal) parentSignal.addEventListener("abort", onAbort, { once: true });

    Promise.resolve()
      .then(() => task(controller.signal, timeoutMs))
      .then(
        (value) => finish(resolve, value),
        (error) => finish(reject, normalizeDependencyError(error, dependency, operation)),
      );
  });
}

let _qdrant = null;
let _pool = null;

function cleanSettings(values) {
  const out = [];
  for (const v of Array.isArray(values) ? values : []) {
    const s = String(v || "").trim();
    if (VALID_SETTINGS.has(s) && !out.includes(s)) out.push(s);
  }
  return out;
}

function cleanTerms(values) {
  return (Array.isArray(values) ? values : [])
    .map(v => String(v || "").trim().toLowerCase())
    .filter(Boolean)
    .slice(0, 12);
}

function preferredSettings(plan) {
  const primary = cleanSettings(plan && plan.primary_settings);
  const compat = SETTING_COMPAT[primary[0]] || {};
  return cleanSettings([...primary, ...(compat.boost || [])]);
}

function excludedSettings(plan) {
  const primary = cleanSettings(plan && plan.primary_settings);
  const compat = SETTING_COMPAT[primary[0]] || {};
  const excluded = cleanSettings([...(compat.exclude || []), ...cleanSettings(plan && plan.hard_exclude_settings)]);
  return excluded.filter(s => !primary.includes(s));
}

function qdrant(opts) {
  if (opts && opts.qdrantClient) return opts.qdrantClient;
  if (!_qdrant) {
    const { QdrantClient } = require("@qdrant/js-client-rest");
    _qdrant = new QdrantClient({
      url: (opts && opts.qdrantUrl) || QDRANT_URL,
      checkCompatibility: false,
      timeout: dependencyTimeoutMs("qdrant", opts),
    });
  }
  return _qdrant;
}

function pgPool(opts) {
  if (opts && opts.pgPool) return opts.pgPool;
  if (!_pool) {
    if (!process.env.POSTGRES_URL) {
      throw new AssetDependencyError(
        "postgres",
        "ASSET_DEPENDENCY_CONFIG_MISSING",
        "POSTGRES_URL is not set",
        { operation: "connect", retryable: false },
      );
    }
    const { Pool } = require("pg");
    _pool = new Pool({ connectionString: process.env.POSTGRES_URL });
  }
  return _pool;
}

async function embedQuery(text, opts) {
  const o = opts || {};
  const fetchImpl = o.fetchImpl || globalThis.fetch;
  if (typeof fetchImpl !== "function") {
    throw new AssetDependencyError(
      "embedding",
      "ASSET_DEPENDENCY_CONFIG_MISSING",
      "embedding fetch client is unavailable",
      { operation: "embed", retryable: false },
    );
  }
  const serviceUrl = String(o.embedServiceUrl || EMBED_SERVICE_URL).replace(/\/$/, "");
  return runDependency("embedding", "embed", o, async (signal) => {
    const resp = await fetchImpl(`${serviceUrl}/embed`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ texts: [String(text || "").trim() || "asset"] }),
      signal,
    });
    if (!resp || !resp.ok) {
      const status = Number(resp && resp.status);
      throw new AssetDependencyError(
        "embedding",
        "ASSET_DEPENDENCY_HTTP_ERROR",
        `embed service returned HTTP ${Number.isFinite(status) ? status : "error"}`,
        {
          operation: "embed",
          retryable: status === 408 || status === 429 || status >= 500,
          ...(Number.isFinite(status) ? { status } : {}),
        },
      );
    }
    let data;
    try {
      data = await resp.json();
    } catch (error) {
      throw new AssetDependencyError(
        "embedding",
        "ASSET_DEPENDENCY_INVALID_RESPONSE",
        "embed service returned invalid JSON",
        { operation: "embed", retryable: false, cause: error },
      );
    }
    if (!Array.isArray(data.dense) || !Array.isArray(data.dense[0]) || data.dense[0].length === 0) {
      throw new AssetDependencyError(
        "embedding",
        "ASSET_DEPENDENCY_INVALID_RESPONSE",
        "embed service returned no dense vector",
        { operation: "embed", retryable: false },
      );
    }
    return { dense: data.dense[0], sparse: data.sparse && data.sparse[0] };
  });
}

function buildQdrantFilter(category, plan, assetSnapshotRevision) {
  const exclude = excludedSettings(plan);
  const filter = {
    must: [{ key: "category", match: { value: category } }],
  };
  if (assetSnapshotRevision) {
    filter.must.push({ key: "asset_snapshot_revision", match: { value: assetSnapshotRevision } });
  }
  if (exclude.length) {
    filter.must_not = [{ key: "setting", match: { any: exclude } }];
  }
  return filter;
}

function queryTextForPlan(plan) {
  return [
    plan && plan.semantic_query,
    ...cleanTerms(plan && plan.must_terms),
    ...preferredSettings(plan),
  ].filter(Boolean).join(" ");
}

function resultPoints(result) {
  if (Array.isArray(result)) return result;
  if (Array.isArray(result && result.points)) return result.points;
  if (Array.isArray(result && result.result)) return result.result;
  return [];
}

function compactFromPayload(payload, score) {
  const p = payload || {};
  return {
    id: p.asset_id,
    name: p.name,
    category: p.category,
    subcategory: p.subcategory || "",
    desc: p.short_description || "",
    tags: (p.tags || []).slice(0, 8),
    sceneTypes: (p.scene_types || []).slice(0, 5),
    setting: p.setting || "generic",
    dims: p.width_m != null ? { width: p.width_m, depth: p.depth_m, height: p.height_m } : null,
    path: p.unreal_asset_path || "",
    assetType: p.asset_type || "",
    spawnTool: p.asset_type === "Blueprint" ? "spawn_blueprint_actor" : "spawn_actor",
    asset_snapshot_revision: p.asset_snapshot_revision || "",
    _score: score,
  };
}

async function qdrantPrefilterCategory(category, plan, opts) {
  const topK = Math.max(1, parseInt((opts && opts.topK) || TOP_K, 10));
  const { dense, sparse } = await embedQuery(queryTextForPlan(plan), opts);
  const assetSnapshotRevision = String(opts && opts.assetSnapshotRevision || "").trim() || null;
  const filter = buildQdrantFilter(category, plan, assetSnapshotRevision);
  const prefetch = [
    { query: dense, using: "text_dense", filter, limit: topK },
  ];
  if (sparse && Array.isArray(sparse.indices) && sparse.indices.length) {
    prefetch.push({ query: sparse, using: "text_sparse", filter, limit: topK });
  }
  const collection = (opts && opts.collection) || COLLECTION;
  const result = await runDependency("qdrant", "query", opts, () => qdrant(opts).query(collection, {
    prefetch,
    query: { fusion: "rrf" },
    limit: topK,
    with_payload: true,
  }));
  return resultPoints(result).map(r => compactFromPayload(r.payload, r.score))
    .filter(a => a.id && (!assetSnapshotRevision || a.asset_snapshot_revision === assetSnapshotRevision));
}

async function postgresFallbackCategory(category, plan, opts) {
  const topK = Math.max(1, parseInt((opts && opts.topK) || TOP_K, 10));
  const queryText = queryTextForPlan(plan) || category;
  const preferred = preferredSettings(plan);
  const excluded = excludedSettings(plan);
  const terms = cleanTerms([...(plan && plan.must_terms || []), ...(plan && plan.semantic_query || "").split(/\s+/)]);
  const assetSnapshotRevision = String(opts && opts.assetSnapshotRevision || "").trim() || null;
  const sql = `
    WITH q AS (SELECT websearch_to_tsquery('english', $2) AS query)
    SELECT
      asset_id, name, category, subcategory, short_description, tags, scene_types,
      setting, width_m, depth_m, height_m, unreal_asset_path, asset_type,
      asset_snapshot_revision,
      ts_rank_cd(search_tsv, q.query, 32) AS text_rank,
      CASE WHEN setting = ANY($4::text[]) THEN 1 ELSE 0 END AS setting_rank,
      CASE WHEN scene_types && $5::text[] THEN 1 ELSE 0 END AS scene_rank,
      CASE WHEN tags && $5::text[] THEN 1 ELSE 0 END AS tag_rank
    FROM assets, q
    WHERE category = $1
      AND NOT (setting = ANY($3::text[]))
      AND ($6::text IS NULL OR asset_snapshot_revision = $6)
    ORDER BY
      text_rank DESC,
      setting_rank DESC,
      scene_rank DESC,
      tag_rank DESC,
      name ASC
    LIMIT $7
  `;
  const values = [category, queryText, excluded, preferred, terms, assetSnapshotRevision, topK];
  const pool = pgPool(opts);
  const res = await runDependency("postgres", "query", opts, (signal, timeoutMs) => pool.query({
    text: sql,
    values,
    query_timeout: timeoutMs,
    signal,
  }));
  if (!res || !Array.isArray(res.rows)) {
    throw new AssetDependencyError(
      "postgres",
      "ASSET_DEPENDENCY_INVALID_RESPONSE",
      "postgres returned an invalid rows payload",
      { operation: "query", retryable: false },
    );
  }
  return res.rows.map(row => compactFromPayload({
    asset_id: row.asset_id,
    name: row.name,
    category: row.category,
    subcategory: row.subcategory,
    short_description: row.short_description,
    tags: row.tags,
    scene_types: row.scene_types,
    setting: row.setting,
    width_m: row.width_m,
    depth_m: row.depth_m,
    height_m: row.height_m,
    unreal_asset_path: row.unreal_asset_path,
    asset_type: row.asset_type,
    asset_snapshot_revision: row.asset_snapshot_revision,
  }, Number(row.text_rank || 0)));
}

async function prefilterCategory(category, plan, opts) {
  const log = opts && opts.log || (() => {});
  const causes = [];
  try {
    const qdrantResults = await qdrantPrefilterCategory(category, plan, opts);
    if (qdrantResults.length) return attachRetrievalTelemetry(qdrantResults, { source: "qdrant" });
    causes.push(dependencyNoResults("qdrant", "query"));
    log(`prefilter ${category}: qdrant returned 0 candidates; trying postgres fallback`);
  } catch (e) {
    const cause = normalizeDependencyError(e, "qdrant", "query");
    causes.push(cause);
    log(`prefilter ${category}: ${cause.dependency} failed (${cause.code}); trying postgres fallback`);
  }

  try {
    const pgResults = await postgresFallbackCategory(category, plan, opts);
    if (pgResults.length) return attachRetrievalTelemetry(pgResults, { source: "postgres", causes });
    causes.push(dependencyNoResults("postgres", "query"));
  } catch (e) {
    causes.push(normalizeDependencyError(e, "postgres", "query"));
  }

  throw new RetrievalPrefilterError(
    `asset prefilter failed for category "${category}"`,
    { category, causes },
  );
}

// Free-text semantic search over the WHOLE library (hybrid dense+sparse RRF), with an
// optional category filter and NO setting exclusion — the caller's query drives relevance.
// Powers the search_assets MCP tool so the builder can PULL relevant assets on demand
// (filling gaps / adding variety) instead of being limited to the pushed seed palette.
async function searchAssets({ query, category, k } = {}, opts) {
  const o = opts || {};
  const limit = Math.max(1, Math.min(parseInt(k, 10) || 12, 40));
  const text = String(query || "").trim() || "asset";
  const cat = category && String(category).trim() ? String(category).trim() : null;
  const assetSnapshotRevision = String(o.assetSnapshotRevision || "").trim() || null;
  const must = [];
  if (cat) must.push({ key: "category", match: { value: cat } });
  if (assetSnapshotRevision) must.push({ key: "asset_snapshot_revision", match: { value: assetSnapshotRevision } });
  const filter = must.length ? { must } : undefined;
  const causes = [];
  try {
    const { dense, sparse } = await embedQuery(text, o);
    const prefetch = [{ query: dense, using: "text_dense", limit: limit * 4, ...(filter ? { filter } : {}) }];
    if (sparse && Array.isArray(sparse.indices) && sparse.indices.length) {
      prefetch.push({ query: sparse, using: "text_sparse", limit: limit * 4, ...(filter ? { filter } : {}) });
    }
    const collection = o.collection || COLLECTION;
    const result = await runDependency("qdrant", "query", o, () => qdrant(o).query(collection, {
      prefetch,
      query: { fusion: "rrf" },
      limit,
      with_payload: true,
    }));
    const out = resultPoints(result).map(r => compactFromPayload(r.payload, r.score))
      .filter(a => a.id && a.path && (!assetSnapshotRevision || a.asset_snapshot_revision === assetSnapshotRevision));
    if (out.length) return attachRetrievalTelemetry(out, { source: "qdrant" });
    causes.push(dependencyNoResults("qdrant", "query"));
  } catch (e) {
    causes.push(normalizeDependencyError(e, "qdrant", "query"));
  }
  // postgres full-text fallback (qdrant down / empty)
  const sql = `
    WITH q AS (SELECT websearch_to_tsquery('english', $1) AS query)
    SELECT asset_id, name, category, subcategory, short_description, tags, scene_types,
           setting, width_m, depth_m, height_m, unreal_asset_path, asset_type,
           asset_snapshot_revision,
           ts_rank_cd(search_tsv, q.query, 32) AS text_rank
    FROM assets, q
    WHERE ($2::text IS NULL OR category = $2)
      AND ($3::text IS NULL OR asset_snapshot_revision = $3)
    ORDER BY text_rank DESC, name ASC
    LIMIT $4`;
  try {
    const pool = pgPool(o);
    const values = [text, cat, assetSnapshotRevision, limit];
    const res = await runDependency("postgres", "query", o, (signal, timeoutMs) => pool.query({
      text: sql,
      values,
      query_timeout: timeoutMs,
      signal,
    }));
    if (!res || !Array.isArray(res.rows)) {
      throw new AssetDependencyError(
        "postgres",
        "ASSET_DEPENDENCY_INVALID_RESPONSE",
        "postgres returned an invalid rows payload",
        { operation: "query", retryable: false },
      );
    }
    const assets = res.rows.map(row => compactFromPayload({
      asset_id: row.asset_id, name: row.name, category: row.category, subcategory: row.subcategory,
      short_description: row.short_description, tags: row.tags, scene_types: row.scene_types, setting: row.setting,
      width_m: row.width_m, depth_m: row.depth_m, height_m: row.height_m,
      unreal_asset_path: row.unreal_asset_path, asset_type: row.asset_type,
      asset_snapshot_revision: row.asset_snapshot_revision,
    }, Number(row.text_rank || 0))).filter(a => a.id && a.path);
    return attachRetrievalTelemetry(assets, { source: "postgres", causes });
  } catch (e) {
    causes.push(normalizeDependencyError(e, "postgres", "query"));
    throw new AssetRetrievalUnavailableError("semantic asset search is unavailable", {
      operation: "search_assets",
      causes,
    });
  }
}

module.exports = {
  SETTING_COMPAT,
  VALID_SETTINGS,
  AssetDependencyError,
  AssetRetrievalUnavailableError,
  RetrievalPrefilterError,
  attachRetrievalTelemetry,
  buildQdrantFilter,
  dependencyTimeoutMs,
  embedQuery,
  excludedSettings,
  normalizeDependencyError,
  postgresFallbackCategory,
  preferredSettings,
  prefilterCategory,
  qdrantPrefilterCategory,
  searchAssets,
  serializeDependencyCause,
};

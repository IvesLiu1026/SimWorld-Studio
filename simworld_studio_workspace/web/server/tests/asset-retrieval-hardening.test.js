"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const retrieval = require("../asset-retrieval");
const retrievalDb = require("../asset-retrieval-db");

const PLAN = Object.freeze({
  semantic_query: "black wheeled office chair",
  primary_settings: ["indoor"],
  hard_exclude_settings: [],
  must_terms: ["chair", "wheels"],
});

function embedOk() {
  return {
    ok: true,
    status: 200,
    json: async () => ({ dense: [[0.1, 0.2]], sparse: [{ indices: [1], values: [0.5] }] }),
  };
}

function qdrantPoint(id = "chair-1", snapshotRevision = "") {
  return {
    score: 0.92,
    payload: {
      asset_id: id,
      name: "Office Chair",
      category: "furniture",
      short_description: "Black wheeled office chair",
      tags: ["chair", "office"],
      scene_types: ["office"],
      setting: "indoor",
      width_m: 0.6,
      depth_m: 0.6,
      height_m: 1.1,
      unreal_asset_path: "/Game/Office/SM_Chair.SM_Chair",
      asset_type: "StaticMesh",
      asset_snapshot_revision: snapshotRevision,
    },
  };
}

function postgresRow(id = "chair-pg", snapshotRevision = "") {
  return {
    asset_id: id,
    name: "Database Chair",
    category: "furniture",
    subcategory: "chair",
    short_description: "Chair returned by full-text fallback",
    tags: ["chair"],
    scene_types: ["office"],
    setting: "indoor",
    width_m: 0.6,
    depth_m: 0.6,
    height_m: 1.0,
    unreal_asset_path: "/Game/Office/SM_Chair_PG.SM_Chair_PG",
    asset_type: "StaticMesh",
    asset_snapshot_revision: snapshotRevision,
    text_rank: 0.8,
    setting_rank: 1,
  };
}

async function withEnv(overrides, fn) {
  const before = {};
  for (const [key, value] of Object.entries(overrides)) {
    before[key] = process.env[key];
    if (value === undefined) delete process.env[key];
    else process.env[key] = String(value);
  }
  try {
    return await fn();
  } finally {
    for (const [key, value] of Object.entries(before)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
}

test("documented default resolves to hybrid without a truthy request/env pre-gate", async () => {
  await withEnv({
    ASSET_RETRIEVAL_MODE: undefined,
    ASSET_REQUIRE_REAL_ASSETS: undefined,
    REQUIRE_REAL_ASSETS: undefined,
    ASSET_DEGRADED_MODE: undefined,
  }, async () => {
    assert.equal(retrieval.resolveAssetMode({}), "hybrid");
    assert.deepEqual(retrieval.resolveAssetPolicy({}), {
      policy_version: 1,
      mode: "hybrid",
      require_real_assets: false,
      degraded_mode: "disabled",
      allow_degraded_assets: false,
    });
  });
});

test("asset fallback is fail-closed unless basic geometry is explicitly requested", () => {
  const outage = new retrievalDb.AssetRetrievalUnavailableError("retrieval unavailable", {
    operation: "search_assets",
    causes: [new retrievalDb.AssetDependencyError(
      "postgres",
      "ASSET_DEPENDENCY_UNAVAILABLE",
      "postgres offline",
      { operation: "query", retryable: true },
    )],
  });

  const defaultDecision = retrieval.assetFailureDecision(outage, { assetRetrievalMode: "hybrid" });
  assert.equal(defaultDecision.action, "block");
  assert.equal(defaultDecision.metadata.status, "blocked");

  const requiredDecision = retrieval.assetFailureDecision(outage, {
    assetRetrievalMode: "hybrid",
    require_real_assets: true,
  });
  assert.equal(requiredDecision.action, "block");
  assert.equal(requiredDecision.metadata.require_real_assets, true);

  const degradedDecision = retrieval.assetFailureDecision(outage, {
    assetRetrievalMode: "hybrid",
    asset_fallback_mode: "basic_geometry",
  });
  assert.equal(degradedDecision.action, "degrade");
  assert.equal(degradedDecision.metadata.degraded_mode, "basic_geometry");
  assert.equal(degradedDecision.metadata.reason.causes[0].dependency, "postgres");

  assert.throws(
    () => retrieval.resolveAssetPolicy({ assetRetrievalMode: "off", require_real_assets: true }),
    (error) => error.code === "ASSET_RETRIEVAL_POLICY_INVALID",
  );
  assert.throws(
    () => retrieval.resolveAssetPolicy({ require_real_assets: true, assetFallbackMode: "basic_geometry" }),
    (error) => error.code === "ASSET_RETRIEVAL_POLICY_INVALID",
  );
  assert.throws(
    () => retrieval.resolveAssetPolicy({ assetRetrievalMode: "typo-mode" }),
    (error) => error.code === "ASSET_RETRIEVAL_POLICY_INVALID",
  );
});

test("policy-aware prompt API represents an explicit off mode without touching dependencies", async () => {
  const result = await retrieval.buildPromptBlockWithPolicy("scene", { assetRetrievalMode: "off" });
  assert.equal(result.promptBlock, "");
  assert.equal(result.metadata.status, "off");
  assert.equal(result.metadata.mode, "off");
});

test("serialized dependency metadata redacts credentials", () => {
  const serialized = retrieval.serializeAssetRetrievalError(
    new Error("postgresql://alice:supersecret@db.internal/assets token=abc123"),
  );
  const text = JSON.stringify(serialized);
  assert.equal(text.includes("supersecret"), false);
  assert.equal(text.includes("abc123"), false);
  assert.equal(text.includes("[redacted]"), true);
});

test("retrieval cache keys include snapshot and embedding revisions", () => {
  const base = { usePrefilter: true, embedVersion: "embed-v1", collection: "assets-v1" };
  const first = retrieval.buildRetrievalCacheKey("office", base, "snapshot-a");
  const second = retrieval.buildRetrievalCacheKey("office", base, "snapshot-b");
  const third = retrieval.buildRetrievalCacheKey("office", { ...base, embedVersion: "embed-v2" }, "snapshot-a");
  assert.notEqual(first, second);
  assert.notEqual(first, third);
});

test("search_assets succeeds from Qdrant while Postgres is unavailable", async () => {
  let postgresCalled = false;
  const assets = await retrievalDb.searchAssets({ query: "office chair", category: "furniture" }, {
    fetchImpl: async () => embedOk(),
    qdrantClient: { query: async () => [qdrantPoint()] },
    pgPool: { query: async () => { postgresCalled = true; throw new Error("postgres offline"); } },
    timeoutMs: 100,
  });
  assert.equal(postgresCalled, false);
  assert.equal(assets.length, 1);
  assert.equal(assets[0].path, "/Game/Office/SM_Chair.SM_Chair");
  assert.deepEqual(assets.retrieval, {
    schema: "asset-retrieval-telemetry/v1",
    status: "ready",
    source: "qdrant",
    fallback_used: false,
    causes: [],
  });
});

test("Qdrant and PostgreSQL queries are both constrained to the exact asset snapshot revision", async () => {
  const snapshotRevision = "assets-2026-07-21-r1";
  let qdrantRequest;
  const qdrantAssets = await retrievalDb.searchAssets({ query: "office chair", category: "furniture" }, {
    assetSnapshotRevision: snapshotRevision,
    fetchImpl: async () => embedOk(),
    qdrantClient: {
      query: async (_collection, request) => {
        qdrantRequest = request;
        return [qdrantPoint("chair-revision", snapshotRevision)];
      },
    },
    pgPool: { query: async () => { throw new Error("postgres should not be called"); } },
    timeoutMs: 100,
  });
  assert.equal(qdrantAssets[0].asset_snapshot_revision, snapshotRevision);
  assert.equal(qdrantRequest.prefetch.length, 2);
  for (const query of qdrantRequest.prefetch) {
    assert.deepEqual(query.filter.must, [
      { key: "category", match: { value: "furniture" } },
      { key: "asset_snapshot_revision", match: { value: snapshotRevision } },
    ]);
  }

  let postgresRequest;
  const postgresAssets = await retrievalDb.searchAssets({ query: "office chair" }, {
    assetSnapshotRevision: snapshotRevision,
    fetchImpl: async () => { throw new Error("embedding unavailable"); },
    pgPool: {
      query: async (request) => {
        postgresRequest = request;
        return { rows: [postgresRow("chair-pg-revision", snapshotRevision)] };
      },
    },
    timeoutMs: 100,
  });
  assert.equal(postgresAssets[0].asset_snapshot_revision, snapshotRevision);
  assert.equal(postgresRequest.values[2], snapshotRevision);
  assert.match(postgresRequest.text, /asset_snapshot_revision = \$3/);
});

test("semantic operations reject an expired live-audit gate before any dependency call", async () => {
  let dependencyCalls = 0;
  let freshnessChecks = 0;
  const expired = () => {
    freshnessChecks += 1;
    const error = new Error("asset live audit expired");
    error.code = "ASSET_LIVE_AUDIT_EXPIRED";
    throw error;
  };
  const options = {
    assertLiveAuditFresh: expired,
    fetchImpl: async () => { dependencyCalls += 1; return embedOk(); },
    qdrantClient: { query: async () => { dependencyCalls += 1; return []; } },
    pgPool: { query: async () => { dependencyCalls += 1; return { rows: [] }; } },
    timeoutMs: 100,
  };

  await assert.rejects(
    retrievalDb.searchAssets({ query: "office chair" }, options),
    (error) => error.code === "ASSET_LIVE_AUDIT_EXPIRED",
  );
  await assert.rejects(
    retrievalDb.prefilterCategory("furniture", PLAN, options),
    (error) => error.code === "ASSET_LIVE_AUDIT_EXPIRED",
  );
  assert.equal(freshnessChecks, 2);
  assert.equal(dependencyCalls, 0);
});

test("search_assets preserves a representative Chinese query for multilingual embedding", async () => {
  let embeddedText = null;
  let authorization = null;
  const assets = await retrievalDb.searchAssets({ query: "黑色有輪子的辦公椅" }, {
    fetchImpl: async (_url, options) => {
      embeddedText = JSON.parse(options.body).texts[0];
      authorization = options.headers.Authorization;
      return embedOk();
    },
    embedServiceToken: "runtime-embedding-bearer-token".padEnd(40, "x"),
    qdrantClient: { query: async () => [qdrantPoint("chair-zh")] },
    pgPool: { query: async () => { throw new Error("postgres should not be called"); } },
    timeoutMs: 100,
  });
  assert.equal(embeddedText, "黑色有輪子的辦公椅");
  assert.equal(authorization, `Bearer ${"runtime-embedding-bearer-token".padEnd(40, "x")}`);
  assert.equal(assets[0].id, "chair-zh");
  assert.equal(assets.retrieval.source, "qdrant");
});

test("search_assets falls back to Postgres when embedding is unavailable", async () => {
  let qdrantCalled = false;
  const assets = await retrievalDb.searchAssets({ query: "office chair" }, {
    fetchImpl: async () => { throw new Error("https://user:secret@embed.internal offline token=abc123"); },
    qdrantClient: { query: async () => { qdrantCalled = true; return []; } },
    pgPool: { query: async () => ({ rows: [postgresRow()] }) },
    timeoutMs: 100,
  });
  assert.equal(qdrantCalled, false);
  assert.equal(assets.length, 1);
  assert.equal(assets[0].id, "chair-pg");
  assert.equal(assets.retrieval.status, "degraded");
  assert.equal(assets.retrieval.source, "postgres");
  assert.equal(assets.retrieval.fallback_used, true);
  assert.equal(assets.retrieval.causes[0].dependency, "embedding");
  assert.equal(JSON.stringify(assets.retrieval).includes("secret"), false);
  assert.equal(JSON.stringify(assets.retrieval).includes("abc123"), false);
});

test("search_assets falls back to Postgres when Qdrant is unavailable", async () => {
  const assets = await retrievalDb.searchAssets({ query: "office chair" }, {
    fetchImpl: async () => embedOk(),
    qdrantClient: { query: async () => { throw new Error("qdrant offline"); } },
    pgPool: { query: async () => ({ rows: [postgresRow("qdrant-fallback")] }) },
    timeoutMs: 100,
  });
  assert.equal(assets.length, 1);
  assert.equal(assets[0].id, "qdrant-fallback");
  assert.equal(assets.retrieval.status, "degraded");
  assert.equal(assets.retrieval.source, "postgres");
  assert.equal(assets.retrieval.causes[0].dependency, "qdrant");
});

test("search_assets preserves embedding and Postgres root causes when all paths fail", async () => {
  await assert.rejects(
    retrievalDb.searchAssets({ query: "office chair" }, {
      fetchImpl: async () => { throw new Error("embed offline"); },
      qdrantClient: { query: async () => { throw new Error("must not run"); } },
      pgPool: { query: async () => { throw new Error("postgres offline"); } },
      timeoutMs: 100,
    }),
    (error) => {
      assert.equal(error.code, "ASSET_RETRIEVAL_UNAVAILABLE");
      assert.deepEqual(error.details.causes.map((cause) => cause.dependency), ["embedding", "postgres"]);
      assert.equal(error.details.causes.every((cause) => cause.code), true);
      return true;
    },
  );
});

test("search_assets reports an unavailable fallback after a healthy empty Qdrant result", async () => {
  await assert.rejects(
    retrievalDb.searchAssets({ query: "unmatched asset" }, {
      fetchImpl: async () => embedOk(),
      qdrantClient: { query: async () => [] },
      pgPool: { query: async () => { throw new Error("postgres offline"); } },
      timeoutMs: 100,
    }),
    (error) => {
      assert.equal(error.code, "ASSET_RETRIEVAL_UNAVAILABLE");
      assert.deepEqual(error.details.causes.map((cause) => cause.code), [
        "ASSET_DEPENDENCY_NO_RESULTS",
        "ASSET_DEPENDENCY_UNAVAILABLE",
      ]);
      assert.equal(error.retryable, true);
      return true;
    },
  );
});

test("prefilter preserves Qdrant and Postgres root causes", async () => {
  await assert.rejects(
    retrievalDb.prefilterCategory("furniture", PLAN, {
      fetchImpl: async () => embedOk(),
      qdrantClient: { query: async () => { throw new Error("qdrant offline"); } },
      pgPool: { query: async () => { throw new Error("postgres offline"); } },
      timeoutMs: 100,
    }),
    (error) => {
      assert.equal(error.code, "ASSET_RETRIEVAL_UNAVAILABLE");
      assert.deepEqual(error.details.causes.map((cause) => cause.dependency), ["qdrant", "postgres"]);
      return true;
    },
  );
});

test("embedding, Qdrant, and Postgres operations each have bounded timeouts", async () => {
  const never = () => new Promise(() => {});
  const started = Date.now();

  await assert.rejects(
    retrievalDb.embedQuery("chair", { fetchImpl: never, embeddingTimeoutMs: 20 }),
    (error) => error.dependency === "embedding" && error.code === "ASSET_DEPENDENCY_TIMEOUT" && error.timeoutMs === 20,
  );

  await assert.rejects(
    retrievalDb.qdrantPrefilterCategory("furniture", PLAN, {
      fetchImpl: async () => embedOk(),
      qdrantClient: { query: never },
      qdrantTimeoutMs: 20,
      embeddingTimeoutMs: 100,
    }),
    (error) => error.dependency === "qdrant" && error.code === "ASSET_DEPENDENCY_TIMEOUT" && error.timeoutMs === 20,
  );

  await assert.rejects(
    retrievalDb.postgresFallbackCategory("furniture", PLAN, {
      pgPool: { query: never },
      postgresTimeoutMs: 20,
    }),
    (error) => error.dependency === "postgres" && error.code === "ASSET_DEPENDENCY_TIMEOUT" && error.timeoutMs === 20,
  );

  assert.ok(Date.now() - started < 1000, "dependency timeouts must return within a bounded deadline");
});

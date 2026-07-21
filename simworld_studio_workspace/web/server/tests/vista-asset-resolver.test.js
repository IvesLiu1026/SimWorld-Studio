"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const {
  VistaAssetResolverError,
  createVistaAssetResolver,
  isBasicGeometry,
  resolveVistaAssets,
} = require("../vista-asset-resolver");

const SNAPSHOT_ID = "ue-content-vista-2026-07-14";

function entity(id, semanticQuery) {
  return {
    id,
    semantic_query: semanticQuery,
    required: true,
    asset_binding: null,
    source_pointer: `/Scene/Key_Visual_Elements/${id}`,
    privilege: "reconstruction_only",
  };
}

function scene(entities = [entity("chair", "black wheeled office chair")]) {
  return {
    schema: "vista-simworld-scene/v1",
    scene_id: "mmg_040@0123456789abcdef",
    profile: "reconstruction",
    source: { dataset_revision: "round1_reviewed_latest" },
    duration_sec: 12,
    environment: { description: "office", lighting: "neutral" },
    camera: { perspective: "first_person" },
    entities,
    relations: [],
    timeline: [],
    dialogue: [],
    unresolved: [{
      mapping_id: "unresolved-action-0001",
      kind: "action",
      source_pointer: "/Scene/Actions/0",
      reason_code: "unsupported_action",
      message: "Needs a verified adapter",
      blocking: true,
      candidates: [],
    }],
    provenance: { importer_version: "1.0.0" },
  };
}

function candidate(assetId, uePath, confidence, extra = {}) {
  return { asset_id: assetId, ue_path: uePath, confidence, ...extra };
}

test("selects real assets deterministically while preserving input and provenance", async () => {
  const input = scene([
    entity("chair", "black wheeled office chair"),
    entity("cabinet", "wooden office cabinet"),
  ]);
  const before = JSON.parse(JSON.stringify(input));
  const requests = [];
  const resolver = createVistaAssetResolver({
    snapshotId: SNAPSHOT_ID,
    minConfidence: 0.75,
    maxCandidates: 4,
    searchAssets: async (request) => {
      requests.push(request);
      assert.equal(request.signal instanceof AbortSignal, true);
      if (request.entity_id === "chair") {
        return {
          snapshot_id: SNAPSHOT_ID,
          candidates: [
            candidate("chair-z", "/Game/Office/SM_Chair_Z.SM_Chair_Z", 0.8),
            candidate("chair-a", "/Game/Office/SM_Chair_A.SM_Chair_A", 0.8),
            candidate("chair-a", "/Game/Office/SM_Chair_A.SM_Chair_A", 0.91),
          ],
        };
      }
      return {
        snapshot_revision: SNAPSHOT_ID,
        assets: [candidate("cabinet-1", "/Game/Office/SM_Cabinet.SM_Cabinet", 0.88)],
      };
    },
  });

  const output = await resolver.resolve(input);

  assert.deepEqual(input, before);
  assert.notEqual(output, input);
  assert.notEqual(output.entities[0], input.entities[0]);
  assert.deepEqual(requests.map(({ query, k, snapshot_id, entity_id }) => ({
    query, k, snapshot_id, entity_id,
  })), [
    {
      query: "black wheeled office chair",
      k: 4,
      snapshot_id: SNAPSHOT_ID,
      entity_id: "chair",
    },
    {
      query: "wooden office cabinet",
      k: 4,
      snapshot_id: SNAPSHOT_ID,
      entity_id: "cabinet",
    },
  ]);
  assert.deepEqual(output.entities[0].asset_binding, {
    snapshot_id: SNAPSHOT_ID,
    asset_id: "chair-a",
    ue_path: "/Game/Office/SM_Chair_A.SM_Chair_A",
    confidence: 0.91,
  });
  assert.deepEqual(output.entities[0].asset_resolution.candidates.map((item) => ({
    rank: item.rank,
    asset_id: item.asset_id,
    confidence: item.confidence,
  })), [
    { rank: 1, asset_id: "chair-a", confidence: 0.91 },
    { rank: 2, asset_id: "chair-z", confidence: 0.8 },
  ]);
  assert.equal(output.entities[0].asset_resolution.query, "black wheeled office chair");
  assert.equal(output.entities[0].asset_resolution.snapshot_id, SNAPSHOT_ID);
  assert.equal(output.entities[0].asset_resolution.selected_by, "automatic");
  assert.deepEqual(output.entities[1].asset_binding, {
    snapshot_id: SNAPSHOT_ID,
    asset_id: "cabinet-1",
    ue_path: "/Game/Office/SM_Cabinet.SM_Cabinet",
    confidence: 0.88,
  });
  assert.deepEqual(output.unresolved, input.unresolved);
});

test("filters basic geometry and leaves low-confidence entities unresolved without a Cube fallback", async () => {
  const resolver = createVistaAssetResolver({
    snapshotId: SNAPSHOT_ID,
    minConfidence: 0.7,
    searchAssets: async () => ({
      snapshot_id: SNAPSHOT_ID,
      candidates: [
        candidate("engine-cube", "/Game/Props/SM_Cube.SM_Cube", 0.99),
        candidate("chair-low", "/Game/Office/SM_Chair.SM_Chair", 0.69),
      ],
    }),
  });

  const output = await resolver.resolve(scene());
  const resolvedEntity = output.entities[0];

  assert.equal(resolvedEntity.asset_binding, null);
  assert.deepEqual(resolvedEntity.asset_resolution.candidates.map((item) => item.asset_id), ["chair-low"]);
  assert.equal(JSON.stringify(resolvedEntity).includes("SM_Cube"), false);
  assert.equal(output.unresolved.length, 2);
  assert.deepEqual(output.unresolved[1], {
    mapping_id: "unresolved-asset-chair",
    kind: "asset",
    source_pointer: "/Scene/Key_Visual_Elements/chair",
    reason_code: "no_asset_match",
    message: "No real asset met the minimum confidence 0.700 for entity 'chair'",
    blocking: true,
    candidates: ["chair-low"],
  });
});

test("an empty search result creates a stable no_asset_match record", async () => {
  const resolver = createVistaAssetResolver({
    snapshotId: SNAPSHOT_ID,
    searchAssets: async () => ({ snapshot_id: SNAPSHOT_ID, candidates: [] }),
  });

  const first = await resolver.resolve(scene());
  const second = await resolver.resolve(first);

  assert.deepEqual(second, first);
  assert.equal(second.entities[0].asset_binding, null);
  assert.equal(second.unresolved.filter((item) => item.mapping_id === "unresolved-asset-chair").length, 1);
  assert.equal(second.unresolved[1].reason_code, "no_asset_match");
  assert.deepEqual(second.unresolved[1].candidates, []);
});

test("a confirmed manual override is explicit and wins without pretending to be an automatic match", async () => {
  const resolver = createVistaAssetResolver({
    snapshotId: SNAPSHOT_ID,
    minConfidence: 0.9,
    maxCandidates: 2,
    searchAssets: async () => ({
      snapshot_id: SNAPSHOT_ID,
      candidates: [candidate("chair-search", "/Game/Office/SM_SearchChair.SM_SearchChair", 0.95)],
    }),
  });

  const output = await resolver.resolve(scene(), {
    manualOverrides: {
      chair: {
        confirmed: true,
        reason: "Verified by the asset curator against the current UE content snapshot",
        snapshot_id: SNAPSHOT_ID,
        asset_id: "chair-curated",
        ue_path: "/Game/Office/SM_CuratedChair.SM_CuratedChair",
        confidence: 0.4,
      },
    },
  });
  const resolvedEntity = output.entities[0];

  assert.deepEqual(resolvedEntity.asset_binding, {
    snapshot_id: SNAPSHOT_ID,
    asset_id: "chair-curated",
    ue_path: "/Game/Office/SM_CuratedChair.SM_CuratedChair",
    confidence: 0.4,
  });
  assert.equal(resolvedEntity.asset_resolution.selected_by, "manual_override");
  assert.equal(resolvedEntity.asset_resolution.manual_override.confirmed, true);
  assert.equal(resolvedEntity.asset_resolution.manual_override.reason.startsWith("Verified by"), true);
  assert.equal(
    resolvedEntity.asset_resolution.candidates.some((item) => item.asset_id === "chair-curated"),
    true,
  );
  assert.equal(output.unresolved.some((item) => item.kind === "asset"), false);
});

test("rejects implicit, unknown, or basic-geometry manual overrides before search", async (t) => {
  let calls = 0;
  const resolver = createVistaAssetResolver({
    snapshotId: SNAPSHOT_ID,
    searchAssets: async () => { calls += 1; return []; },
  });
  const validShape = {
    confirmed: true,
    reason: "Curator selected this exact content asset",
    asset_id: "chair-1",
    ue_path: "/Game/Office/SM_Chair.SM_Chair",
    confidence: 0.8,
  };

  await t.test("confirmation is mandatory", async () => {
    await assert.rejects(
      resolver.resolve(scene(), { manualOverrides: { chair: { ...validShape, confirmed: false } } }),
      (error) => error.code === "VISTA_ASSET_OVERRIDE_INVALID",
    );
  });
  await t.test("unknown entity is rejected", async () => {
    await assert.rejects(
      resolver.resolve(scene(), { manualOverrides: { desk: validShape } }),
      (error) => error.code === "VISTA_ASSET_OVERRIDE_INVALID",
    );
  });
  await t.test("Cube override is forbidden", async () => {
    await assert.rejects(
      resolver.resolve(scene(), {
        manualOverrides: {
          chair: { ...validShape, asset_id: "cube", ue_path: "/Game/Props/SM_Cube.SM_Cube" },
        },
      }),
      (error) => error.code === "VISTA_ASSET_BASIC_GEOMETRY_FORBIDDEN",
    );
  });
  assert.equal(calls, 0);
});

test("fails closed when response, candidate, or override snapshots do not match", async (t) => {
  await t.test("response snapshot", async () => {
    const resolver = createVistaAssetResolver({
      snapshotId: SNAPSHOT_ID,
      searchAssets: async () => ({ snapshot_id: "other-snapshot", candidates: [] }),
    });
    await assert.rejects(
      resolver.resolve(scene()),
      (error) => error.code === "VISTA_ASSET_SNAPSHOT_MISMATCH" && error.status === 409,
    );
  });

  await t.test("candidate snapshot", async () => {
    const resolver = createVistaAssetResolver({
      snapshotId: SNAPSHOT_ID,
      searchAssets: async () => ({
        snapshot_id: SNAPSHOT_ID,
        candidates: [candidate(
          "chair-1",
          "/Game/Office/SM_Chair.SM_Chair",
          0.9,
          { snapshot_id: "other-snapshot" },
        )],
      }),
    });
    await assert.rejects(
      resolver.resolve(scene()),
      (error) => error.code === "VISTA_ASSET_SNAPSHOT_MISMATCH" && error.status === 409,
    );
  });

  await t.test("override snapshot", async () => {
    const resolver = createVistaAssetResolver({
      snapshotId: SNAPSHOT_ID,
      searchAssets: async () => [],
    });
    await assert.rejects(
      resolver.resolve(scene(), {
        manualOverrides: {
          chair: {
            confirmed: true,
            reason: "Curator selection",
            snapshot_id: "other-snapshot",
            asset_id: "chair-1",
            ue_path: "/Game/Office/SM_Chair.SM_Chair",
            confidence: 0.9,
          },
        },
      }),
      (error) => error.code === "VISTA_ASSET_SNAPSHOT_MISMATCH",
    );
  });
});

test("bounds a search that ignores AbortSignal and aborts its child signal", async () => {
  let childAborted = false;
  const resolver = createVistaAssetResolver({
    snapshotId: SNAPSHOT_ID,
    timeoutMs: 15,
    totalTimeoutMs: 100,
    searchAssets: ({ signal }) => {
      signal.addEventListener("abort", () => { childAborted = true; }, { once: true });
      return new Promise(() => {});
    },
  });

  await assert.rejects(
    resolver.resolve(scene()),
    (error) => error instanceof VistaAssetResolverError
      && error.code === "VISTA_ASSET_RESOLUTION_TIMEOUT"
      && error.status === 504
      && error.retryable === true,
  );
  assert.equal(childAborted, true);
});

test("honors a caller AbortSignal without invoking search when already cancelled", async () => {
  let calls = 0;
  const resolver = createVistaAssetResolver({
    snapshotId: SNAPSHOT_ID,
    searchAssets: async () => { calls += 1; return []; },
  });
  const controller = new AbortController();
  controller.abort();

  await assert.rejects(
    resolver.resolve(scene(), { signal: controller.signal }),
    (error) => error.code === "VISTA_ASSET_RESOLUTION_ABORTED" && error.retryable === false,
  );
  assert.equal(calls, 0);
});

test("propagates cancellation to an in-flight semantic search", async () => {
  const controller = new AbortController();
  let childAborted = false;
  let markStarted;
  const started = new Promise((resolve) => { markStarted = resolve; });
  const resolver = createVistaAssetResolver({
    snapshotId: SNAPSHOT_ID,
    timeoutMs: 1_000,
    searchAssets: ({ signal }) => new Promise((_resolve, reject) => {
      markStarted();
      signal.addEventListener("abort", () => {
        childAborted = true;
        reject(Object.assign(new Error("cancelled by resolver"), { code: "ABORT_ERR" }));
      }, { once: true });
    }),
  });
  const pending = resolver.resolve(scene(), { signal: controller.signal });
  await started;
  controller.abort();

  await assert.rejects(
    pending,
    (error) => error.code === "VISTA_ASSET_RESOLUTION_ABORTED" && error.status === 499,
  );
  assert.equal(childAborted, true);
});

test("redacts upstream failures into typed safe errors", async () => {
  const resolver = createVistaAssetResolver({
    snapshotId: SNAPSHOT_ID,
    searchAssets: async () => {
      const error = new Error("postgresql://alice:supersecret@db.internal/assets token=abc123");
      error.code = "ASSET_RETRIEVAL_UNAVAILABLE";
      error.retryable = true;
      throw error;
    },
  });

  await assert.rejects(resolver.resolve(scene()), (error) => {
    const serialized = JSON.stringify(error);
    assert.equal(error.code, "VISTA_ASSET_SEARCH_FAILED");
    assert.equal(error.status, 503);
    assert.equal(error.details.upstream_code, "ASSET_RETRIEVAL_UNAVAILABLE");
    assert.equal(serialized.includes("supersecret"), false);
    assert.equal(serialized.includes("abc123"), false);
    assert.equal(serialized.includes("db.internal"), false);
    return true;
  });
});

test("rejects malformed provider candidates instead of silently binding them", async () => {
  const resolver = createVistaAssetResolver({
    snapshotId: SNAPSHOT_ID,
    searchAssets: async () => ({
      snapshot_id: SNAPSHOT_ID,
      candidates: [{ asset_id: "chair-1", ue_path: "https://example.invalid/chair", confidence: 0.9 }],
    }),
  });

  await assert.rejects(
    resolver.resolve(scene()),
    (error) => error.code === "VISTA_ASSET_SEARCH_RESPONSE_INVALID"
      && error.status === 502
      && error.details.field === "candidate.ue_path",
  );
});

test("convenience API and basic-geometry classifier expose the integration contract", async () => {
  const output = await resolveVistaAssets(scene(), {
    snapshotId: SNAPSHOT_ID,
    searchAssets: async () => [candidate("chair-1", "/Game/Office/SM_Chair.SM_Chair", 0.9)],
  });

  assert.equal(output.entities[0].asset_binding.asset_id, "chair-1");
  assert.equal(isBasicGeometry({ ue_path: "/Engine/BasicShapes/Cube.Cube" }), true);
  assert.equal(isBasicGeometry({ ue_path: "/Game/Office/SM_Chair.SM_Chair" }), false);
});

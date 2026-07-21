"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const {
  createReviewSceneBinding,
  normalizeReviewSceneBinding,
  sameReviewSceneBinding,
} = require("../review-scene-binding");

function scope() {
  return {
    scopeId: `review-${"a".repeat(64)}`,
    activeLease: { leaseId: "lease-secret", slotId: 3 },
  };
}

test("Review scene bindings are deterministic, lease-digested, and VISTA-lineage aware", () => {
  const lineage = {
    kind: "vista-scene-build",
    artifact_id: `vsp-${"b".repeat(24)}`,
    revision: `vsj-${"c".repeat(24)}`,
    content_digest: "d".repeat(64),
    scene_id: "mmg_040@attempt-7",
  };
  const first = createReviewSceneBinding({
    scope: scope(),
    snapshot: { result: { actors: [{ name: "chair", x: 1 }], count: 1 } },
    sceneBuildLineage: lineage,
  });
  const reordered = createReviewSceneBinding({
    scope: scope(),
    snapshot: { result: { count: 1, actors: [{ x: 1, name: "chair" }] } },
    sceneBuildLineage: lineage,
  });
  assert.equal(sameReviewSceneBinding(first, reordered), true);
  assert.equal(first.scene_revision, lineage.scene_id);
  assert.match(first.lease_id_sha256, /^[a-f0-9]{64}$/);
  assert.doesNotMatch(JSON.stringify(first), /lease-secret/);
});

test("generic bindings pin their exact snapshot and reject drift or malformed lineage", () => {
  const first = createReviewSceneBinding({ scope: { scopeId: "review-loopback" }, snapshot: { actors: [] } });
  const changed = createReviewSceneBinding({ scope: { scopeId: "review-loopback" }, snapshot: { actors: [{ id: 1 }] } });
  assert.equal(first.slot_id, null);
  assert.equal(first.scene_revision, `snapshot:${first.scene_snapshot_digest}`);
  assert.equal(sameReviewSceneBinding(first, changed), false);
  assert.throws(() => normalizeReviewSceneBinding({ ...first, scene_revision: "snapshot:" + "0".repeat(64) }));
});

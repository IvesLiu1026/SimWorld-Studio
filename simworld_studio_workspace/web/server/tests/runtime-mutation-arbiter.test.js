"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const { createRuntimeMutationArbiter, runtimeKey } = require("../runtime-mutation-arbiter");

const SLOT = Object.freeze({ ownerId: "owner-a", slotId: 1, mcpPort: 55561 });

test("one MCP runtime admits one mutation across owners and operation kinds", () => {
  const arbiter = createRuntimeMutationArbiter({ randomUUID: () => "token-a" });
  const review = arbiter.acquire(SLOT, { kind: "review", operationId: "review-a" });

  assert.equal(runtimeKey(SLOT), "mcp:55561");
  assert.equal(arbiter.isHeld(review), true);
  assert.throws(
    () => arbiter.acquire({ ownerId: "other", slotId: 99, mcpPort: 55561 }, { kind: "scene_build" }),
    (error) => error.code === "RUNTIME_MUTATION_SLOT_BUSY" && error.statusCode === 409,
  );
  assert.equal(review.release(), true);
  assert.equal(review.release(), false);
  assert.equal(arbiter.activeCount, 0);
});

test("scene invalidation advances one monotonic epoch while a read-only lock does not", () => {
  let next = 0;
  const arbiter = createRuntimeMutationArbiter({ randomUUID: () => `token-${++next}` });
  const animation = arbiter.acquire(SLOT, { kind: "timeline" });
  assert.equal(animation.epoch, 0);
  animation.release();
  assert.equal(arbiter.currentEpoch(SLOT), 0);

  const build = arbiter.acquire(SLOT, { kind: "scene_build" });
  assert.equal(build.invalidateScene(), 1);
  assert.equal(build.invalidateScene(), 1);
  assert.equal(arbiter.currentEpoch(SLOT), 1);
  build.release();

  const review = arbiter.acquire(SLOT, { kind: "review" });
  assert.equal(review.invalidateScene(), 2);
  review.release();
  assert.equal(arbiter.currentEpoch(SLOT), 2);
});

test("a stale token cannot invalidate a runtime after release", () => {
  const arbiter = createRuntimeMutationArbiter({ randomUUID: () => "token-a" });
  const token = arbiter.acquire({ loopback: true }, { kind: "review" });
  token.release();
  assert.throws(
    () => token.invalidateScene(),
    (error) => error.code === "RUNTIME_MUTATION_TOKEN_STALE",
  );
});

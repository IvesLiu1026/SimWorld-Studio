"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");

const { createRuntimeMutationArbiter } = require("../runtime-mutation-arbiter");
const { createRuntimeMutationMiddleware } = require("../runtime-mutation-middleware");

function responseHarness() {
  const response = new EventEmitter();
  response.statusCode = 200;
  response.status = (code) => { response.statusCode = code; return response; };
  response.json = (body) => { response.body = body; return response; };
  return response;
}

test("trusted vanilla builder owns and invalidates the slot until its response closes", () => {
  const arbiter = createRuntimeMutationArbiter({ randomUUID: () => "builder-token" });
  const context = { activeLease: { mcpPort: 55561 }, runId: "builder-run", mutationToken: null };
  const middleware = createRuntimeMutationMiddleware({ arbiter, resolveContext: () => context });
  const response = responseHarness();
  let called = 0;
  middleware({}, response, () => { called += 1; });

  assert.equal(called, 1);
  assert.equal(arbiter.activeCount, 1);
  assert.equal(arbiter.currentEpoch(context.activeLease), 1);
  response.emit("finish");
  response.emit("close");
  assert.equal(arbiter.activeCount, 0);
});

test("nested Review builder reuses the held parent token and a competing builder gets 409", () => {
  let nextToken = 0;
  const arbiter = createRuntimeMutationArbiter({ randomUUID: () => `token-${++nextToken}` });
  const activeLease = { mcpPort: 55561 };
  const parent = arbiter.acquire(activeLease, { kind: "review" });
  parent.invalidateScene();
  let context = { activeLease, runId: "review-run", mutationToken: parent };
  const middleware = createRuntimeMutationMiddleware({ arbiter, resolveContext: () => context });
  const inheritedResponse = responseHarness();
  let called = false;
  middleware({}, inheritedResponse, () => { called = true; });
  assert.equal(called, true);
  assert.equal(arbiter.activeCount, 1);

  context = { activeLease, runId: "other-run", mutationToken: null };
  const rejected = responseHarness();
  middleware({}, rejected, () => assert.fail("busy builder must not run"));
  assert.equal(rejected.statusCode, 409);
  assert.equal(rejected.body.code, "BUILDER_SLOT_BUSY");
  parent.release();
});

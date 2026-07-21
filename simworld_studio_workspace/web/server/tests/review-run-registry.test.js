"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { ReviewRunRegistry } = require("../review-run-registry");

test("review runs are isolated by scope and cancellable by exact run id", () => {
  let next = 0;
  const registry = new ReviewRunRegistry({ randomUUID: () => `run-${++next}` });
  const first = registry.start({ scopeId: "conversation-a" });
  const second = registry.start({ scopeId: "conversation-b" });

  assert.equal(registry.cancel({ scopeId: "conversation-a", runId: second.runId }), null);
  assert.equal(first.signal.aborted, false);
  assert.equal(second.signal.aborted, false);

  const cancelled = registry.cancel({ scopeId: "conversation-a", runId: first.runId });
  assert.equal(cancelled.runId, first.runId);
  assert.equal(first.signal.aborted, true);
  assert.equal(first.signal.reason.name, "AbortError");
  assert.equal(first.signal.reason.code, "REVIEW_RUN_CANCELLED");
  assert.equal(second.signal.aborted, false);
});

test("starting a new run only supersedes the previous run in the same scope", () => {
  let next = 0;
  const registry = new ReviewRunRegistry({ randomUUID: () => `run-${++next}` });
  const oldRun = registry.start({ scopeId: "conversation-a" });
  const otherRun = registry.start({ scopeId: "conversation-b" });
  const newRun = registry.start({ scopeId: "conversation-a" });

  assert.equal(oldRun.signal.aborted, true);
  assert.equal(oldRun.signal.reason.message, "superseded");
  assert.equal(otherRun.signal.aborted, false);
  assert.equal(newRun.signal.aborted, false);
  assert.equal(registry.get({ scopeId: "conversation-a" }).runId, newRun.runId);
});

test("completion removes only the matching current run", () => {
  const registry = new ReviewRunRegistry({ randomUUID: () => "run-1" });
  registry.start({ scopeId: "conversation-a" });

  assert.equal(registry.complete({ scopeId: "conversation-b", runId: "run-1" }), false);
  assert.equal(registry.size, 1);
  assert.equal(registry.complete({ scopeId: "conversation-a", runId: "run-1" }), true);
  assert.equal(registry.size, 0);
  assert.equal(registry.get({ scopeId: "conversation-a" }), null);
});

test("caller-supplied run ids are unique within a scope but isolated across owners", () => {
  const registry = new ReviewRunRegistry();
  const first = registry.start({ scopeId: "conversation-a", runId: "client-run" });
  const second = registry.start({ scopeId: "conversation-b", runId: "client-run" });

  assert.equal(registry.get({ scopeId: "conversation-a", runId: "client-run" }), first);
  assert.equal(registry.get({ scopeId: "conversation-b", runId: "client-run" }), second);
  assert.equal(registry.size, 2);

  assert.throws(
    () => registry.start({ scopeId: "conversation-a", runId: "client-run" }),
    (error) => error && error.code === "REVIEW_RUN_CONFLICT",
  );
});

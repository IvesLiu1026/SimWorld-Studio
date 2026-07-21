"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const { createReviewEvidenceHandler } = require("../review-evidence-route");

function responseHarness() {
  return {
    statusCode: 200,
    headers: {},
    status(code) { this.statusCode = code; return this; },
    json(body) { this.body = body; return this; },
    setHeader(name, value) { this.headers[name.toLowerCase()] = value; },
    end(body) { this.body = body; return this; },
  };
}

test("opaque Review evidence returns already-verified bytes with no-store headers", async () => {
  const bytes = Buffer.from([0x89, 0x50, 0x4e, 0x47]);
  const calls = [];
  const handler = createReviewEvidenceHandler({
    resolveScope() { return { scopeId: "owner:lease" }; },
    resolveReference(input) {
      calls.push(input);
      return { data: bytes, size: bytes.length, mediaType: "image/png" };
    },
  });
  const response = responseHarness();
  await handler({
    query: { evidenceId: `sha256:${"a".repeat(64)}`, conversationId: "conversation-a" },
    params: { handle: `evidence-${"b".repeat(48)}` },
  }, response);

  assert.equal(response.statusCode, 200);
  assert.equal(response.body, bytes);
  assert.equal(response.headers["content-length"], String(bytes.length));
  assert.equal(response.headers["content-type"], "image/png");
  assert.equal(response.headers["cache-control"], "private, no-store, max-age=0");
  assert.equal(response.headers["x-content-type-options"], "nosniff");
  assert.equal(Object.hasOwn(calls[0], "filepath"), false);
});

test("cross-scope, expired, and scope-changed references fail without serving bytes", async () => {
  const expired = createReviewEvidenceHandler({
    resolveScope() { return { scopeId: "owner-a" }; },
    resolveReference() { throw Object.assign(new Error("expired"), { code: "REVIEW_EVIDENCE_UNAVAILABLE" }); },
  });
  const expiredResponse = responseHarness();
  await expired({
    query: { evidenceId: `sha256:${"a".repeat(64)}`, conversationId: "conversation-a" },
    params: { handle: `evidence-${"b".repeat(48)}` },
  }, expiredResponse);
  assert.equal(expiredResponse.statusCode, 404);
  assert.equal(Buffer.isBuffer(expiredResponse.body), false);

  let scope = "owner-a";
  const changed = createReviewEvidenceHandler({
    resolveScope() { const selected = scope; scope = "owner-b"; return { scopeId: selected }; },
    resolveReference() { return { data: Buffer.from("png"), size: 3, mediaType: "image/png" }; },
  });
  const changedResponse = responseHarness();
  await changed({
    query: { evidenceId: `sha256:${"a".repeat(64)}`, conversationId: "conversation-a" },
    params: { handle: `evidence-${"b".repeat(48)}` },
  }, changedResponse);
  assert.equal(changedResponse.statusCode, 409);
  assert.equal(Buffer.isBuffer(changedResponse.body), false);
});

test("evidence route requires the explicit conversation scope and rejects extra query keys", async () => {
  let resolveCalls = 0;
  const handler = createReviewEvidenceHandler({
    resolveScope() { resolveCalls += 1; return { scopeId: "owner-a" }; },
    resolveReference() { throw new Error("must not run"); },
  });
  for (const query of [
    { evidenceId: `sha256:${"a".repeat(64)}` },
    { conversationId: "conversation-a" },
    {
      evidenceId: `sha256:${"a".repeat(64)}`,
      conversationId: "conversation-a",
      path: "/private/file.png",
    },
  ]) {
    const response = responseHarness();
    await handler({ query, params: { handle: `evidence-${"b".repeat(48)}` } }, response);
    assert.equal(response.statusCode, 400);
  }
  assert.equal(resolveCalls, 0);
});

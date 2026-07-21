import test from "node:test";
import assert from "node:assert/strict";

import { sendChat } from "./appApi.js";

const encoder = new TextEncoder();

function streamingResponse(reader) {
  return {
    ok: true,
    status: 200,
    body: { getReader: () => reader },
  };
}

test("sendChat rejects a done fragment at EOF without a blank delimiter", async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  let reads = 0;
  const reader = {
    async read() {
      reads += 1;
      if (reads === 1) {
        return {
          done: false,
          value: encoder.encode('event: done\ndata: {"isError":false,"runId":"review-client-12345678"}'),
        };
      }
      return { done: true, value: undefined };
    },
    async cancel() {},
  };
  globalThis.fetch = async () => streamingResponse(reader);
  const events = [];

  await assert.rejects(
    sendChat("build", null, (event) => events.push(event), undefined, {
      loopMode: "text_loop",
      runId: "review-client-12345678",
    }),
    (error) => error.code === "REVIEW_TRANSPORT_AMBIGUOUS" && error.retryable === true,
  );

  assert.equal(reads, 2);
  assert.deepEqual(events, []);
});

test("sendChat returns immediately after done and ignores a later transport reset", async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  let reads = 0;
  const reader = {
    async read() {
      reads += 1;
      if (reads === 1) {
        return {
          done: false,
          value: encoder.encode('event: done\ndata: {"isError":false,"runId":"review-client-12345678","conversationId":"conversation-a"}\n\n'),
        };
      }
      throw new Error("simulated reset after terminal");
    },
    async cancel() {},
  };
  globalThis.fetch = async () => streamingResponse(reader);
  const events = [];

  await sendChat("build", null, (event) => events.push(event), undefined, {
    loopMode: "visual_loop",
    runId: "review-client-12345678",
    conversationId: "conversation-a",
  });

  assert.equal(reads, 1);
  assert.equal(events[0].type, "done");
});

test("sendChat awaits terminal processing before accepting done", async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  let releaseTerminal;
  let terminalStarted;
  const terminalStartedPromise = new Promise((resolve) => { terminalStarted = resolve; });
  const terminalGate = new Promise((resolve) => { releaseTerminal = resolve; });
  let reads = 0;
  const reader = {
    async read() {
      reads += 1;
      return reads === 1
        ? {
            done: false,
            value: encoder.encode('event: done\ndata: {"isError":false,"runId":"review-client-12345678","conversationId":"conversation-a"}\n\n'),
          }
        : { done: true, value: undefined };
    },
    async cancel() {},
  };
  globalThis.fetch = async () => streamingResponse(reader);

  let deliverySettled = false;
  const delivery = sendChat("build", null, async () => {
    terminalStarted();
    await terminalGate;
  }, undefined, {
    loopMode: "text_loop",
    runId: "review-client-12345678",
    conversationId: "conversation-a",
  });
  delivery.finally(() => { deliverySettled = true; });

  await terminalStartedPromise;
  await Promise.resolve();
  assert.equal(deliverySettled, false);
  releaseTerminal();
  await delivery;
  assert.equal(deliverySettled, true);
  assert.equal(reads, 1);
});

test("sendChat preserves explicit pre-provider and request identity metadata on HTTP failures", async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  globalThis.fetch = async () => ({
    ok: false,
    status: 400,
    body: null,
    async json() {
      return {
        code: "REVIEW_CONFIG_INVALID",
        error: "invalid provider configuration",
        runId: "review-client-12345678",
        conversationId: "conversation-a",
        providerAttempted: false,
      };
    },
  });

  await assert.rejects(
    sendChat("build", null, () => {}, undefined, {
      loopMode: "text_loop",
      runId: "review-client-12345678",
      conversationId: "conversation-a",
    }),
    (error) => error.code === "REVIEW_CONFIG_INVALID"
      && error.runId === "review-client-12345678"
      && error.conversationId === "conversation-a"
      && error.providerAttempted === false,
  );
});

test("sendChat fails closed on a malformed terminal payload", async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  let reads = 0;
  const reader = {
    async read() {
      reads += 1;
      return reads === 1
        ? { done: false, value: encoder.encode('event: done\ndata: {"runId":"review-client-12345678"}\n\n') }
        : { done: true, value: undefined };
    },
    async cancel() {},
  };
  globalThis.fetch = async () => streamingResponse(reader);

  await assert.rejects(
    sendChat("build", null, () => {}, undefined, {
      loopMode: "text_loop",
      runId: "review-client-12345678",
    }),
    (error) => error.code === "CHAT_SSE_PROTOCOL_INVALID" && error.retryable === true,
  );
});

test("sendChat cancels and rejects a delimiter-free SSE buffer above the byte cap", async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  let cancelled = 0;
  let reads = 0;
  const reader = {
    async read() {
      reads += 1;
      return reads === 1
        ? { done: false, value: encoder.encode(`data: ${"x".repeat(1024 * 1024 + 1)}`) }
        : { done: true, value: undefined };
    },
    async cancel() { cancelled += 1; },
  };
  globalThis.fetch = async () => streamingResponse(reader);

  await assert.rejects(
    sendChat("build", null, () => {}, undefined, {
      loopMode: "text_loop",
      runId: "review-client-12345678",
      conversationId: "conversation-a",
    }),
    (error) => error.code === "CHAT_SSE_PROTOCOL_INVALID" && error.retryable === true,
  );
  assert.equal(reads, 1);
  assert.ok(cancelled >= 1);
});

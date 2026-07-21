import test from "node:test";
import assert from "node:assert/strict";

import {
  assertReviewRunEvent,
  claimPendingReviewExclusive,
  clearPreProviderReviewExclusive,
  clearTerminalReviewExclusive,
  markPendingReviewTerminalExclusive,
  pendingReviewRecordExists,
  readPendingReview,
  reviewFailureDisposition,
  reviewEvidenceUrls,
  reviewTerminalDisposition,
} from "./chatRuntime.js";

function memoryStorage() {
  const values = new Map();
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
    values,
  };
}

function exclusiveLocks(onRequest) {
  return {
    request: async (name, options, callback) => {
      onRequest?.(name, options);
      return callback();
    },
  };
}

function pendingEnvelope(overrides = {}) {
  const runId = overrides.runId || "review-client-12345678";
  const conversationId = overrides.conversationId || "conversation-a";
  return {
    schema: "simworld-review-pending/v2",
    conversationId,
    userMessageId: "user-a",
    assistantId: "assistant-a",
    createdAt: 1,
    request: {
      runId,
      prompt: "build a scene",
      sessionId: null,
      options: {
        loopMode: "visual_loop",
        runId,
        conversationId,
      },
    },
    ...overrides,
  };
}

function legacyPendingEnvelope({
  runId = "review-client-12345678",
  conversationId = "conversation-a",
  generation = 4,
  ownerId = "review-owner-legacy1234",
} = {}) {
  return {
    schema: "simworld-review-pending/v1",
    conversationId,
    userMessageId: "user-a",
    assistantId: "assistant-a",
    createdAt: 1,
    request: {
      runId,
      prompt: "build a scene",
      sessionId: null,
      options: {
        loopMode: "visual_loop",
        runId,
        conversationId,
      },
    },
    generation,
    ownerId,
  };
}

test("review evidence refs become scoped opaque URLs without using server paths", () => {
  const reference = {
    schema: "simworld-review-evidence-ref/v1",
    scope_digest: "a".repeat(64),
    evidence_id: `sha256:${"b".repeat(64)}`,
    handle: `evidence-${"c".repeat(48)}`,
  };
  assert.deepEqual(reviewEvidenceUrls({
    screenshotRef: reference,
    paths: ["/private/must-not-leak.png"],
  }, "/api", "conversation-a"), [
    `/api/review-evidence/${reference.handle}?evidenceId=sha256%3A${"b".repeat(64)}&conversationId=conversation-a`,
  ]);
});

test("malformed evidence refs are ignored", () => {
  assert.deepEqual(reviewEvidenceUrls({
    evidence: [{
      schema: "simworld-review-evidence-ref/v1",
      evidence_id: "sha256:bad",
      handle: "../../etc/passwd",
    }],
  }, "/api", "conversation-a"), []);
});

test("pending Review claim, terminal receipt, and clear share one lock and owner-generation CAS", async () => {
  const storage = memoryStorage();
  const lockCalls = [];
  const lockManager = exclusiveLocks((name, options) => lockCalls.push({ name, options }));
  const runtime = { storage, lockManager, requireLock: true };
  const envelope = pendingEnvelope();
  const created = await claimPendingReviewExclusive(envelope, {
    ownerId: "review-owner-aaaaaaaa",
  }, runtime);

  assert.deepEqual(created, {
    ...envelope,
    generation: 1,
    ownerId: "review-owner-aaaaaaaa",
    state: "claimed",
  });
  assert.equal(pendingReviewRecordExists(storage), true);
  assert.deepEqual(readPendingReview(storage), created);

  const claimed = await claimPendingReviewExclusive(envelope, {
    ownerId: "review-owner-bbbbbbbb",
    expectedGeneration: created.generation,
  }, runtime);
  assert.equal(claimed.generation, 2);
  assert.equal(claimed.ownerId, "review-owner-bbbbbbbb");

  const terminalReceived = await markPendingReviewTerminalExclusive({
    runId: claimed.request.runId,
    conversationId: claimed.conversationId,
    generation: claimed.generation,
    ownerId: claimed.ownerId,
    terminal: {
      isError: false,
      cancelled: false,
      recovered: false,
      code: null,
      providerAttempted: true,
    },
  }, runtime);
  assert.equal(terminalReceived.state, "terminal_received");
  assert.equal(terminalReceived.generation, 3);
  assert.match(terminalReceived.terminalReceipt.receiptId, /^review-terminal-/);

  await assert.rejects(
    clearTerminalReviewExclusive({
      runId: terminalReceived.request.runId,
      conversationId: terminalReceived.conversationId,
      generation: claimed.generation,
      ownerId: "review-owner-aaaaaaaa",
      receiptId: terminalReceived.terminalReceipt.receiptId,
    }, runtime),
    (error) => error.code === "REVIEW_PENDING_CAS_MISMATCH",
  );
  assert.deepEqual(readPendingReview(storage), terminalReceived);

  assert.equal(await clearTerminalReviewExclusive({
    runId: terminalReceived.request.runId,
    conversationId: terminalReceived.conversationId,
    generation: terminalReceived.generation,
    ownerId: terminalReceived.ownerId,
    receiptId: terminalReceived.terminalReceipt.receiptId,
  }, runtime), true);
  assert.equal(readPendingReview(storage), null);
  assert.equal(lockCalls.length, 5);
  assert.ok(lockCalls.every(({ name, options }) => (
    name === "simworld.review.pending.v2" && options.mode === "exclusive"
  )));
});

test("terminal receipt survives a crash window and reload can only claim the same run", async () => {
  const storage = memoryStorage();
  const runtime = { storage, lockManager: exclusiveLocks(), requireLock: true };
  const envelope = pendingEnvelope();
  const created = await claimPendingReviewExclusive(envelope, {
    ownerId: "review-owner-aaaaaaaa",
  }, runtime);
  const received = await markPendingReviewTerminalExclusive({
    runId: created.request.runId,
    conversationId: created.conversationId,
    generation: created.generation,
    ownerId: created.ownerId,
    terminal: {
      isError: true,
      cancelled: false,
      recovered: false,
      code: "REVIEW_PROVIDER_ERROR",
      providerAttempted: true,
    },
  }, runtime);

  assert.throws(() => { throw new Error("simulated render crash"); }, /simulated render crash/);
  assert.deepEqual(readPendingReview(storage), received);

  const reloadedClaim = await claimPendingReviewExclusive(envelope, {
    ownerId: "review-owner-bbbbbbbb",
    expectedGeneration: received.generation,
  }, runtime);
  assert.equal(reloadedClaim.request.runId, envelope.request.runId);
  assert.equal(reloadedClaim.state, "terminal_received");
  assert.deepEqual(reloadedClaim.terminalReceipt, received.terminalReceipt);
  await assert.rejects(
    claimPendingReviewExclusive(pendingEnvelope({ runId: "review-client-87654321" }), {
      ownerId: "review-owner-cccccccc",
    }, runtime),
    (error) => error.code === "REVIEW_PENDING_CONFLICT",
  );
});

test("a valid v1 reload projects to claimed v2 and migrates with the same ID under CAS", async () => {
  const storage = memoryStorage();
  const runtime = { storage, lockManager: exclusiveLocks(), requireLock: true };
  const legacy = legacyPendingEnvelope();
  storage.values.set("simworld.review.pending.v1", JSON.stringify(legacy));

  const reloaded = readPendingReview(storage);
  assert.deepEqual(reloaded, {
    ...legacy,
    schema: "simworld-review-pending/v2",
    state: "claimed",
  });
  assert.equal(storage.getItem("simworld.review.pending.v2"), null);

  const migrated = await claimPendingReviewExclusive(pendingEnvelope(), {
    ownerId: "review-owner-migrated1234",
    expectedGeneration: legacy.generation,
  }, runtime);
  assert.equal(migrated.schema, "simworld-review-pending/v2");
  assert.equal(migrated.state, "claimed");
  assert.equal(migrated.generation, legacy.generation + 1);
  assert.equal(migrated.ownerId, "review-owner-migrated1234");
  assert.equal(migrated.request.runId, legacy.request.runId);
  assert.deepEqual(migrated.request, legacy.request);
  assert.equal(migrated.userMessageId, legacy.userMessageId);
  assert.equal(migrated.assistantId, legacy.assistantId);
  assert.equal(storage.getItem("simworld.review.pending.v1"), null);
  assert.deepEqual(readPendingReview(storage), migrated);
});

test("a crash after the v2 migration write leaves an exact dual-key pair recoverable", async () => {
  const backing = memoryStorage();
  const legacy = legacyPendingEnvelope({ generation: 8 });
  backing.values.set("simworld.review.pending.v1", JSON.stringify(legacy));
  let crashBeforeLegacyRemoval = true;
  const storage = {
    getItem: backing.getItem,
    setItem: backing.setItem,
    removeItem: (key) => {
      if (key === "simworld.review.pending.v1" && crashBeforeLegacyRemoval) {
        throw new Error("simulated crash before legacy removal");
      }
      backing.removeItem(key);
    },
  };
  const runtime = { storage, lockManager: exclusiveLocks(), requireLock: true };

  await assert.rejects(
    claimPendingReviewExclusive(pendingEnvelope(), {
      ownerId: "review-owner-migrated1234",
      expectedGeneration: legacy.generation,
    }, runtime),
    /simulated crash before legacy removal/,
  );
  assert.notEqual(storage.getItem("simworld.review.pending.v1"), null);
  assert.notEqual(storage.getItem("simworld.review.pending.v2"), null);
  const afterCrash = readPendingReview(storage);
  assert.equal(afterCrash.request.runId, legacy.request.runId);
  assert.equal(afterCrash.generation, legacy.generation + 1);
  assert.equal(afterCrash.ownerId, "review-owner-migrated1234");
  const legacyRawAfterCrash = storage.getItem("simworld.review.pending.v1");
  const currentRawAfterCrash = storage.getItem("simworld.review.pending.v2");
  await assert.rejects(
    markPendingReviewTerminalExclusive({
      runId: afterCrash.request.runId,
      conversationId: afterCrash.conversationId,
      generation: afterCrash.generation,
      ownerId: afterCrash.ownerId,
      terminal: {
        isError: false,
        cancelled: false,
        recovered: false,
        code: null,
        providerAttempted: true,
      },
    }, runtime),
    (error) => error.code === "REVIEW_PENDING_CAS_MISMATCH",
  );
  assert.equal(storage.getItem("simworld.review.pending.v1"), legacyRawAfterCrash);
  assert.equal(storage.getItem("simworld.review.pending.v2"), currentRawAfterCrash);

  crashBeforeLegacyRemoval = false;
  const recovered = await claimPendingReviewExclusive(pendingEnvelope(), {
    ownerId: "review-owner-reloaded1234",
    expectedGeneration: afterCrash.generation,
  }, runtime);
  assert.equal(recovered.request.runId, legacy.request.runId);
  assert.equal(recovered.generation, afterCrash.generation + 1);
  assert.equal(recovered.ownerId, "review-owner-reloaded1234");
  assert.equal(storage.getItem("simworld.review.pending.v1"), null);
  assert.deepEqual(readPendingReview(storage), recovered);
});

test("invalid or conflicting dual-key recovery state is preserved without deletion", async () => {
  const storage = memoryStorage();
  const runtime = { storage, lockManager: exclusiveLocks(), requireLock: true };
  const current = await claimPendingReviewExclusive(pendingEnvelope(), {
    ownerId: "review-owner-current1234",
  }, runtime);
  const invalidLegacy = {
    ...legacyPendingEnvelope(),
    generation: undefined,
  };
  storage.values.set("simworld.review.pending.v1", JSON.stringify(invalidLegacy));
  const currentRaw = storage.getItem("simworld.review.pending.v2");
  const invalidLegacyRaw = storage.getItem("simworld.review.pending.v1");
  assert.equal(readPendingReview(storage), null);
  await assert.rejects(
    claimPendingReviewExclusive(pendingEnvelope(), {
      ownerId: "review-owner-reloaded1234",
      expectedGeneration: current.generation,
    }, runtime),
    (error) => error.code === "REVIEW_PENDING_INVALID",
  );
  assert.equal(storage.getItem("simworld.review.pending.v2"), currentRaw);
  assert.equal(storage.getItem("simworld.review.pending.v1"), invalidLegacyRaw);

  const conflictingLegacy = legacyPendingEnvelope({
    runId: "review-client-87654321",
    conversationId: "conversation-b",
    generation: current.generation,
  });
  storage.values.set("simworld.review.pending.v1", JSON.stringify(conflictingLegacy));
  const conflictingLegacyRaw = storage.getItem("simworld.review.pending.v1");
  assert.equal(readPendingReview(storage), null);
  await assert.rejects(
    claimPendingReviewExclusive(pendingEnvelope(), {
      ownerId: "review-owner-reloaded1234",
      expectedGeneration: current.generation,
    }, runtime),
    (error) => error.code === "REVIEW_PENDING_CONFLICT",
  );
  assert.equal(storage.getItem("simworld.review.pending.v2"), currentRaw);
  assert.equal(storage.getItem("simworld.review.pending.v1"), conflictingLegacyRaw);
});

test("only an exact identity-bound pre-provider failure can clear a claimed Review", async () => {
  const storage = memoryStorage();
  const runtime = { storage, lockManager: exclusiveLocks(), requireLock: true };
  const created = await claimPendingReviewExclusive(pendingEnvelope(), {
    ownerId: "review-owner-aaaaaaaa",
  }, runtime);
  const identity = {
    runId: created.request.runId,
    conversationId: created.conversationId,
    generation: created.generation,
    ownerId: created.ownerId,
  };
  await assert.rejects(
    clearPreProviderReviewExclusive({ ...identity, providerAttempted: true }, runtime),
    (error) => error.code === "REVIEW_PENDING_INVALID",
  );
  assert.deepEqual(readPendingReview(storage), created);
  assert.equal(await clearPreProviderReviewExclusive({
    ...identity,
    providerAttempted: false,
  }, runtime), true);
  assert.equal(readPendingReview(storage), null);
});

test("unverifiable browser storage blocks Review dispatch", async () => {
  const storage = {
    getItem: () => null,
    setItem: () => {},
    removeItem: () => {},
  };
  await assert.rejects(
    claimPendingReviewExclusive(pendingEnvelope(), { ownerId: "review-owner-aaaaaaaa" }, {
      storage,
      lockManager: exclusiveLocks(),
      requireLock: true,
    }),
    /could not be verified/,
  );
});

test("pending Review claims reject stale generations and preserve the original retry envelope", async () => {
  const storage = memoryStorage();
  const runtime = { storage, lockManager: exclusiveLocks(), requireLock: true };
  const envelope = pendingEnvelope();
  const created = await claimPendingReviewExclusive(envelope, {
    ownerId: "review-owner-aaaaaaaa",
  }, runtime);
  const retryCandidate = {
    ...envelope,
    userMessageId: "user-retry",
    assistantId: "assistant-retry",
    createdAt: 2,
  };
  const claimed = await claimPendingReviewExclusive(retryCandidate, {
    ownerId: "review-owner-bbbbbbbb",
    expectedGeneration: created.generation,
  }, runtime);
  assert.equal(claimed.userMessageId, envelope.userMessageId);
  assert.equal(claimed.assistantId, envelope.assistantId);
  assert.equal(claimed.createdAt, envelope.createdAt);
  assert.equal(claimed.generation, 2);

  await assert.rejects(
    claimPendingReviewExclusive(retryCandidate, {
      ownerId: "review-owner-cccccccc",
      expectedGeneration: created.generation,
    }, runtime),
    (error) => error.code === "REVIEW_PENDING_CAS_MISMATCH",
  );
  assert.deepEqual(readPendingReview(storage), claimed);

  await assert.rejects(
    claimPendingReviewExclusive(pendingEnvelope({
      runId: "review-client-87654321",
    }), { ownerId: "review-owner-cccccccc" }, runtime),
    (error) => error.code === "REVIEW_PENDING_CONFLICT",
  );
});

test("production Review fails closed without Web Locks", async () => {
  const storage = memoryStorage();
  await assert.rejects(
    claimPendingReviewExclusive(pendingEnvelope(), { ownerId: "review-owner-aaaaaaaa" }, {
      storage,
      lockManager: null,
      requireLock: true,
    }),
    (error) => error.code === "REVIEW_WEB_LOCK_UNAVAILABLE",
  );
  assert.equal(pendingReviewRecordExists(storage), false);
});

test("corrupt or downgraded pending envelopes fail closed", async () => {
  const storage = memoryStorage();
  const invalid = {
    schema: "simworld-review-pending/v1",
    conversationId: "conversation-a",
    userMessageId: "user-a",
    assistantId: "assistant-a",
    createdAt: 1,
    request: {
      runId: "review-client-12345678",
      prompt: "build",
      sessionId: null,
      options: {
        loopMode: "vanilla",
        runId: "review-client-other",
        conversationId: "conversation-b",
      },
    },
  };
  storage.values.set("simworld.review.pending.v2", JSON.stringify(invalid));
  assert.equal(readPendingReview(storage), null);
  assert.equal(pendingReviewRecordExists(storage), true);
  await assert.rejects(
    claimPendingReviewExclusive(invalid, { ownerId: "review-owner-aaaaaaaa" }, {
      storage,
      lockManager: exclusiveLocks(),
      requireLock: true,
    }),
    (error) => error.code === "REVIEW_PENDING_INVALID",
  );
});

test("Review SSE run identity is required on start and terminal events", () => {
  const runId = "review-client-12345678";
  assert.equal(assertReviewRunEvent({ type: "round_start", data: {} }, runId), null);
  assert.equal(assertReviewRunEvent({
    type: "run_start",
    data: { runId, conversationId: "conversation-a" },
  }, runId, "conversation-a"), runId);
  assert.throws(
    () => assertReviewRunEvent({ type: "done", data: {} }, runId),
    (error) => error.code === "REVIEW_RUN_ID_MISMATCH" && error.retryable === true,
  );
  assert.throws(
    () => assertReviewRunEvent({ type: "done", data: { runId } }, runId, "conversation-a"),
    (error) => error.code === "REVIEW_RUN_ID_MISMATCH",
  );
  assert.throws(
    () => assertReviewRunEvent({
      type: "run_start",
      data: { runId },
    }, runId, "conversation-a"),
    (error) => error.code === "REVIEW_RUN_ID_MISMATCH",
  );
  assert.throws(
    () => assertReviewRunEvent({ type: "done", data: { runId: "review-client-other" } }, runId),
    (error) => error.code === "REVIEW_RUN_ID_MISMATCH",
  );
  assert.throws(
    () => assertReviewRunEvent({
      type: "run_start",
      data: { runId, conversationId: "conversation-b" },
    }, runId, "conversation-a"),
    (error) => error.code === "REVIEW_RUN_ID_MISMATCH",
  );
});

test("Review recovery actions distinguish ambiguity, manual recovery, and immutable terminals", () => {
  assert.deepEqual(reviewFailureDisposition({
    hasReview: true,
    authoritativeDone: false,
    code: "REVIEW_RUN_CONFLICT",
    status: 409,
  }), {
    keepPending: true,
    sameIdRetry: true,
    startNew: false,
    recoveryBlocked: false,
  });
  assert.deepEqual(reviewFailureDisposition({
    hasReview: true,
    authoritativeDone: false,
    code: "CHAT_HTTP_ERROR",
    status: 502,
  }), {
    keepPending: true,
    sameIdRetry: true,
    startNew: false,
    recoveryBlocked: false,
  });
  assert.deepEqual(reviewFailureDisposition({
    hasReview: true,
    authoritativeDone: false,
    code: "REVIEW_CONFIG_INVALID",
    status: 400,
    providerAttempted: false,
    identityBound: true,
  }), {
    keepPending: false,
    sameIdRetry: false,
    startNew: true,
    recoveryBlocked: false,
  });
  assert.deepEqual(reviewFailureDisposition({
    hasReview: true,
    authoritativeDone: false,
    code: "REVIEW_CONFIG_INVALID",
    status: 400,
    providerAttempted: false,
    identityBound: false,
  }), {
    keepPending: true,
    sameIdRetry: true,
    startNew: false,
    recoveryBlocked: false,
  });
  assert.deepEqual(reviewFailureDisposition({
    hasReview: true,
    authoritativeDone: false,
    code: "REVIEW_TERMINAL_RECOVERY_REQUIRED",
    status: 409,
  }), {
    keepPending: true,
    sameIdRetry: false,
    startNew: false,
    recoveryBlocked: true,
  });
  assert.deepEqual(reviewTerminalDisposition({
    isError: false,
    cancelled: true,
    recovered: true,
    code: "REVIEW_RUN_CANCELLED",
  }), {
    keepPending: false,
    sameIdRetry: false,
    startNew: true,
    recoveryBlocked: false,
  });
  assert.deepEqual(reviewTerminalDisposition({
    isError: true,
    cancelled: false,
    recovered: false,
    code: "ARTIFACT_JOURNAL_UNAVAILABLE",
  }), {
    keepPending: true,
    sameIdRetry: true,
    startNew: false,
    recoveryBlocked: false,
  });
  assert.deepEqual(reviewTerminalDisposition({
    isError: true,
    cancelled: false,
    recovered: false,
    code: "REVIEW_PROVIDER_ERROR",
  }), {
    keepPending: false,
    sameIdRetry: false,
    startNew: false,
    recoveryBlocked: false,
  });
  assert.deepEqual(reviewTerminalDisposition({
    isError: true,
    cancelled: false,
    recovered: false,
    code: "REVIEW_CONFIG_INVALID",
    providerAttempted: false,
    identityBound: true,
  }), {
    keepPending: false,
    sameIdRetry: false,
    startNew: true,
    recoveryBlocked: false,
  });
});

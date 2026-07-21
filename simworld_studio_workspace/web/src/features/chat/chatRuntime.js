const SCENE_TOOLS = new Set([
  "spawn_blueprint_actor",
  "spawn_actor",
  "spawn_agent",
  "delete_actor",
  "delete_all_spawned",
  "setup_environment",
  "set_actor_transform",
  "execute_python_script",
]);

const LOOP_MODES = new Set(["vanilla", "text_loop", "visual_loop"]);
const REVIEW_LOOP_MODES = new Set(["text_loop", "visual_loop"]);
const SAFE_REVIEW_SCOPE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/;
const SAFE_MESSAGE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/;
const SAFE_REVIEW_OWNER_ID = /^review-owner-[A-Za-z0-9-]{8,180}$/;
const SAFE_REVIEW_RECEIPT_ID = /^review-terminal-[A-Za-z0-9-]{8,180}$/;
const SAFE_REVIEW_ERROR_CODE = /^[A-Z][A-Z0-9_]{1,79}$/;
const REVIEW_PENDING_STATES = new Set(["claimed", "terminal_received"]);
const SAME_ID_RETRY_CODES = new Set([
  "ARTIFACT_JOURNAL_UNAVAILABLE",
  "ARTIFACT_JOURNAL_WRITE_FAILED",
  "REVIEW_TERMINAL_PENDING",
  "REVIEW_TRANSPORT_AMBIGUOUS",
  "REVIEW_RUN_CONFLICT",
  "REVIEW_RUN_ID_MISMATCH",
  "CHAT_SSE_PROTOCOL_INVALID",
  "REVIEW_PENDING_CLEAR_FAILED",
  "REVIEW_PENDING_CAS_MISMATCH",
]);
export const REVIEW_PENDING_STORAGE_KEY = "simworld.review.pending.v2";
const LEGACY_REVIEW_PENDING_STORAGE_KEY = "simworld.review.pending.v1";
const MAX_PENDING_REVIEW_BYTES = 32 * 1024;
const LEGACY_PENDING_FIELDS = new Set([
  "schema",
  "conversationId",
  "userMessageId",
  "assistantId",
  "createdAt",
  "request",
  "generation",
  "ownerId",
]);
const V2_CLAIMED_PENDING_FIELDS = new Set([
  ...LEGACY_PENDING_FIELDS,
  "state",
]);

let messageCounter = 0;

export function generateMessageId() {
  messageCounter += 1;
  return `msg-${messageCounter}-${Date.now()}`;
}

export function buildWelcomeMessage() {
  return {
    id: generateMessageId(),
    role: "assistant",
    content: `**Scene workspace ready**

Define the environment using layout, asset, scale, lighting, and camera requirements. Operations and validation results will be recorded below.

Example specifications:
- *Residential block with six houses, tree-lined streets, and a 6 m clear route.*
- *Downtown intersection with defined setbacks and pedestrian crossings.*
- *Public park with benches, perimeter trees, and late-afternoon lighting.*`,
    timestamp: Date.now(),
  };
}

export function normalizeLoopMode(value) {
  return LOOP_MODES.has(value) ? value : "vanilla";
}

function isPlainRecord(value) {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function validTerminalReceipt(receipt, envelope) {
  if (!isPlainRecord(receipt)
      || receipt.schema !== "simworld-review-terminal-receipt/v1"
      || !SAFE_REVIEW_RECEIPT_ID.test(String(receipt.receiptId || ""))
      || receipt.runId !== envelope?.request?.runId
      || receipt.conversationId !== envelope?.conversationId
      || !Number.isFinite(receipt.receivedAt) || receipt.receivedAt <= 0
      || typeof receipt.isError !== "boolean"
      || typeof receipt.cancelled !== "boolean"
      || typeof receipt.recovered !== "boolean"
      || (receipt.code !== null && !SAFE_REVIEW_ERROR_CODE.test(String(receipt.code || "")))
      || (receipt.providerAttempted !== null
        && typeof receipt.providerAttempted !== "boolean")) return false;
  return true;
}

function validPendingReviewEnvelope(value, { requireClaim = true } = {}) {
  const request = value?.request;
  const options = request?.options;
  const runId = String(request?.runId || "");
  if (!isPlainRecord(value) || value.schema !== "simworld-review-pending/v2"
      || !SAFE_REVIEW_SCOPE_ID.test(String(value.conversationId || ""))
      || !SAFE_MESSAGE_ID.test(String(value.userMessageId || ""))
      || !SAFE_MESSAGE_ID.test(String(value.assistantId || ""))
      || !Number.isFinite(value.createdAt) || value.createdAt <= 0
      || !isPlainRecord(request)
      || !/^review-client-[A-Za-z0-9-]{8,180}$/.test(runId)
      || typeof request.prompt !== "string" || !request.prompt.trim()
      || request.prompt.length > 24 * 1024
      || (request.sessionId !== null && request.sessionId !== undefined
        && !SAFE_REVIEW_SCOPE_ID.test(String(request.sessionId)))
      || !isPlainRecord(options)
      || !REVIEW_LOOP_MODES.has(options.loopMode)
      || options.runId !== runId
      || options.conversationId !== value.conversationId
      || (requireClaim && (!Number.isSafeInteger(value.generation) || value.generation < 1
        || !SAFE_REVIEW_OWNER_ID.test(String(value.ownerId || ""))
        || !REVIEW_PENDING_STATES.has(value.state)
        || (value.state === "claimed" && value.terminalReceipt !== undefined)
        || (value.state === "terminal_received"
          && !validTerminalReceipt(value.terminalReceipt, value))))) return false;
  return true;
}

function parsePendingReview(raw) {
  if (typeof raw !== "string" || !raw
      || new TextEncoder().encode(raw).length > MAX_PENDING_REVIEW_BYTES) return null;
  let value;
  try { value = JSON.parse(raw); } catch { return null; }
  return validPendingReviewEnvelope(value) ? value : null;
}

function projectLegacyPendingReview(value) {
  return {
    schema: "simworld-review-pending/v2",
    conversationId: value.conversationId,
    userMessageId: value.userMessageId,
    assistantId: value.assistantId,
    createdAt: value.createdAt,
    request: value.request,
    generation: value.generation,
    ownerId: value.ownerId,
    state: "claimed",
  };
}

function validLegacyPendingReviewEnvelope(value) {
  if (!isPlainRecord(value)
      || value.schema !== "simworld-review-pending/v1"
      || Object.keys(value).some((field) => !LEGACY_PENDING_FIELDS.has(field))) return false;
  return validPendingReviewEnvelope(projectLegacyPendingReview(value));
}

function parseLegacyPendingReview(raw) {
  if (typeof raw !== "string" || !raw
      || new TextEncoder().encode(raw).length > MAX_PENDING_REVIEW_BYTES) return null;
  let value;
  try { value = JSON.parse(raw); } catch { return null; }
  return validLegacyPendingReviewEnvelope(value) ? value : null;
}

function sameReviewRequest(left, right) {
  try { return JSON.stringify(left) === JSON.stringify(right); } catch { return false; }
}

function samePendingReviewPayload(left, right) {
  return left.conversationId === right.conversationId
    && left.userMessageId === right.userMessageId
    && left.assistantId === right.assistantId
    && left.createdAt === right.createdAt
    && sameReviewRequest(left.request, right.request);
}

function compatibleLegacyAndV2(legacy, current) {
  if (!legacy || !current || current.state !== "claimed"
      || current.terminalReceipt !== undefined
      || Object.keys(current).some((field) => !V2_CLAIMED_PENDING_FIELDS.has(field))
      || !samePendingReviewPayload(legacy, current)) return false;
  const exactProjection = current.generation === legacy.generation
    && current.ownerId === legacy.ownerId;
  const claimedMigration = legacy.generation < Number.MAX_SAFE_INTEGER
    && current.generation === legacy.generation + 1;
  return exactProjection || claimedMigration;
}

export function readPendingReview(storage = globalThis.localStorage) {
  try {
    const currentRaw = storage.getItem(REVIEW_PENDING_STORAGE_KEY);
    const legacyRaw = storage.getItem(LEGACY_REVIEW_PENDING_STORAGE_KEY);
    const current = currentRaw === null ? null : parsePendingReview(currentRaw);
    const legacy = legacyRaw === null ? null : parseLegacyPendingReview(legacyRaw);
    if ((currentRaw !== null && !current) || (legacyRaw !== null && !legacy)) return null;
    if (current && legacy) return compatibleLegacyAndV2(legacy, current) ? current : null;
    if (current) return current;
    return legacy ? projectLegacyPendingReview(legacy) : null;
  } catch {
    return null;
  }
}

export function pendingReviewRecordExists(storage = globalThis.localStorage) {
  try {
    return storage.getItem(REVIEW_PENDING_STORAGE_KEY) !== null
      || storage.getItem(LEGACY_REVIEW_PENDING_STORAGE_KEY) !== null;
  } catch {
    return true;
  }
}

function pendingReviewError(code, message) {
  const error = new Error(message);
  error.code = code;
  error.retryable = true;
  return error;
}

function pendingReviewCandidate(value) {
  const candidate = isPlainRecord(value) ? { ...value } : value;
  if (isPlainRecord(candidate)) {
    delete candidate.generation;
    delete candidate.ownerId;
    delete candidate.state;
    delete candidate.terminalReceipt;
  }
  const serialized = JSON.stringify(candidate);
  if (new TextEncoder().encode(serialized).length > MAX_PENDING_REVIEW_BYTES) {
    throw new Error("Review recovery envelope exceeds the browser storage limit");
  }
  if (!validPendingReviewEnvelope(candidate, { requireClaim: false })) {
    throw pendingReviewError("REVIEW_PENDING_INVALID", "Review recovery envelope is invalid");
  }
  return candidate;
}

function resolvedReviewLockRuntime(runtime = {}) {
  const hasStorage = Object.prototype.hasOwnProperty.call(runtime, "storage");
  const hasLockManager = Object.prototype.hasOwnProperty.call(runtime, "lockManager");
  return {
    storage: hasStorage ? runtime.storage : globalThis.localStorage,
    lockManager: hasLockManager ? runtime.lockManager : globalThis.navigator?.locks,
    requireLock: runtime.requireLock === true,
  };
}

async function withPendingReviewLock(runtime, operation) {
  const { lockManager, requireLock } = runtime;
  if (!lockManager || typeof lockManager.request !== "function") {
    if (requireLock) {
      throw pendingReviewError(
        "REVIEW_WEB_LOCK_UNAVAILABLE",
        "Review requires browser Web Locks in this deployment",
      );
    }
    return operation();
  }
  return lockManager.request(
    REVIEW_PENDING_STORAGE_KEY,
    { mode: "exclusive" },
    operation,
  );
}

export function createReviewOwnerId() {
  const uuid = typeof globalThis.crypto?.randomUUID === "function"
    ? globalThis.crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 14)}`;
  return `review-owner-${uuid}`;
}

function createReviewTerminalReceiptId() {
  const uuid = typeof globalThis.crypto?.randomUUID === "function"
    ? globalThis.crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 14)}`;
  return `review-terminal-${uuid}`;
}

export function productionReviewRequiresWebLocks() {
  return import.meta.env?.PROD === true;
}

export async function claimPendingReviewExclusive(
  value,
  { ownerId, expectedGeneration = null } = {},
  runtimeOptions = {},
) {
  if (!SAFE_REVIEW_OWNER_ID.test(String(ownerId || ""))) {
    throw pendingReviewError("REVIEW_PENDING_INVALID", "Review recovery owner is invalid");
  }
  const candidate = pendingReviewCandidate(value);
  const runtime = resolvedReviewLockRuntime(runtimeOptions);
  return withPendingReviewLock(runtime, () => {
    const existingRaw = runtime.storage.getItem(REVIEW_PENDING_STORAGE_KEY);
    const legacyRaw = runtime.storage.getItem(LEGACY_REVIEW_PENDING_STORAGE_KEY);
    const existing = existingRaw === null ? null : parsePendingReview(existingRaw);
    const legacy = legacyRaw === null ? null : parseLegacyPendingReview(legacyRaw);
    if (existingRaw !== null && !existing) {
      throw pendingReviewError(
        "REVIEW_PENDING_INVALID",
        "An invalid Review recovery record already exists and was preserved",
      );
    }
    if (legacyRaw !== null && !legacy) {
      throw pendingReviewError(
        "REVIEW_PENDING_INVALID",
        "An invalid legacy Review recovery record exists and was preserved",
      );
    }
    if (existing && legacy && !compatibleLegacyAndV2(legacy, existing)) {
      throw pendingReviewError(
        "REVIEW_PENDING_CONFLICT",
        "Legacy and current Review recovery records conflict and were preserved",
      );
    }

    const recoverable = existing || (legacy ? projectLegacyPendingReview(legacy) : null);
    let claimed;
    if (recoverable) {
      if (recoverable.request.runId !== candidate.request.runId) {
        throw pendingReviewError(
          "REVIEW_PENDING_CONFLICT",
          "Another Review run still requires recovery",
        );
      }
      if (recoverable.conversationId !== candidate.conversationId
          || !sameReviewRequest(recoverable.request, candidate.request)) {
        throw pendingReviewError(
          "REVIEW_PENDING_MISMATCH",
          "The pending Review request does not match this retry",
        );
      }
      if (!Number.isSafeInteger(expectedGeneration)
          || expectedGeneration !== recoverable.generation
          || recoverable.generation >= Number.MAX_SAFE_INTEGER) {
        throw pendingReviewError(
          "REVIEW_PENDING_CAS_MISMATCH",
          "The pending Review claim changed in another browser context",
        );
      }
      claimed = {
        ...recoverable,
        generation: recoverable.generation + 1,
        ownerId,
      };
    } else {
      if (expectedGeneration !== null && expectedGeneration !== undefined) {
        throw pendingReviewError(
          "REVIEW_PENDING_CAS_MISMATCH",
          "The pending Review claim no longer exists",
        );
      }
      claimed = { ...candidate, generation: 1, ownerId, state: "claimed" };
    }

    if (legacy && existing) {
      // A previous migration already made v2 durable but crashed before
      // deleting v1. Reconcile that exact pair before taking a newer claim so
      // another crash cannot create an unverifiable two-generation gap.
      removeAndVerifyLegacyPending(runtime.storage);
    }

    const verified = writeAndVerifyPending(runtime.storage, claimed);
    if (verified.request.runId !== candidate.request.runId
        || verified.conversationId !== candidate.conversationId) {
      throw pendingReviewError("REVIEW_PENDING_INVALID", "Review recovery envelope is invalid");
    }
    if (legacy && !existing) removeAndVerifyLegacyPending(runtime.storage);
    return verified;
  });
}

function validReviewCasIdentity({ runId, conversationId, generation, ownerId } = {}) {
  return SAFE_REVIEW_SCOPE_ID.test(String(conversationId || ""))
    && /^review-client-[A-Za-z0-9-]{8,180}$/.test(String(runId || ""))
    && Number.isSafeInteger(generation) && generation >= 1
    && SAFE_REVIEW_OWNER_ID.test(String(ownerId || ""));
}

function assertMatchingPendingReview(existing, identity) {
  if (existing.request.runId !== identity.runId
      || existing.conversationId !== identity.conversationId
      || existing.generation !== identity.generation
      || existing.ownerId !== identity.ownerId) {
    throw pendingReviewError(
      "REVIEW_PENDING_CAS_MISMATCH",
      "The pending Review claim changed in another browser context",
    );
  }
}

function readPendingForMutation(storage) {
  const existingRaw = storage.getItem(REVIEW_PENDING_STORAGE_KEY);
  const legacyRaw = storage.getItem(LEGACY_REVIEW_PENDING_STORAGE_KEY);
  const existing = parsePendingReview(existingRaw);
  if (!existing) {
    throw pendingReviewError(
      existingRaw !== null ? "REVIEW_PENDING_INVALID" : "REVIEW_PENDING_CAS_MISMATCH",
      existingRaw !== null
        ? "The Review recovery record is invalid and was preserved"
        : "The pending Review claim no longer exists",
    );
  }
  if (legacyRaw !== null) {
    const legacy = parseLegacyPendingReview(legacyRaw);
    const compatible = legacy && compatibleLegacyAndV2(legacy, existing);
    throw pendingReviewError(
      compatible ? "REVIEW_PENDING_CAS_MISMATCH" : "REVIEW_PENDING_CONFLICT",
      compatible
        ? "Legacy Review recovery must be reconciled by an exact same-ID claim first"
        : "Legacy and current Review recovery records conflict and were preserved",
    );
  }
  return existing;
}

function writeAndVerifyPending(storage, value) {
  const serialized = JSON.stringify(value);
  if (new TextEncoder().encode(serialized).length > MAX_PENDING_REVIEW_BYTES) {
    throw pendingReviewError(
      "REVIEW_PENDING_WRITE_FAILED",
      "Review recovery envelope exceeds the browser storage limit",
    );
  }
  storage.setItem(REVIEW_PENDING_STORAGE_KEY, serialized);
  if (storage.getItem(REVIEW_PENDING_STORAGE_KEY) !== serialized) {
    throw pendingReviewError(
      "REVIEW_PENDING_WRITE_FAILED",
      "Review recovery envelope could not be verified",
    );
  }
  const verified = readPendingReview(storage);
  if (!verified || verified.generation !== value.generation
      || verified.ownerId !== value.ownerId || verified.state !== value.state) {
    throw pendingReviewError("REVIEW_PENDING_INVALID", "Review recovery envelope is invalid");
  }
  return verified;
}

function removeAndVerifyLegacyPending(storage) {
  storage.removeItem(LEGACY_REVIEW_PENDING_STORAGE_KEY);
  if (storage.getItem(LEGACY_REVIEW_PENDING_STORAGE_KEY) !== null) {
    throw pendingReviewError(
      "REVIEW_PENDING_CLEAR_FAILED",
      "Legacy Review recovery envelope could not be cleared after migration",
    );
  }
  return true;
}

export async function markPendingReviewTerminalExclusive(
  { runId, conversationId, generation, ownerId, terminal } = {},
  runtimeOptions = {},
) {
  if (!validReviewCasIdentity({ runId, conversationId, generation, ownerId })
      || !isPlainRecord(terminal) || typeof terminal.isError !== "boolean"
      || typeof terminal.cancelled !== "boolean" || typeof terminal.recovered !== "boolean"
      || (terminal.code !== null && !SAFE_REVIEW_ERROR_CODE.test(String(terminal.code || "")))
      || (terminal.providerAttempted !== null && terminal.providerAttempted !== undefined
        && typeof terminal.providerAttempted !== "boolean")) {
    throw pendingReviewError("REVIEW_PENDING_INVALID", "Review terminal receipt is invalid");
  }
  const runtime = resolvedReviewLockRuntime(runtimeOptions);
  return withPendingReviewLock(runtime, () => {
    const existing = readPendingForMutation(runtime.storage);
    assertMatchingPendingReview(existing, { runId, conversationId, generation, ownerId });
    if (existing.generation >= Number.MAX_SAFE_INTEGER) {
      throw pendingReviewError("REVIEW_PENDING_CAS_MISMATCH", "Review generation is exhausted");
    }
    const next = {
      ...existing,
      generation: existing.generation + 1,
      state: "terminal_received",
      terminalReceipt: {
        schema: "simworld-review-terminal-receipt/v1",
        receiptId: createReviewTerminalReceiptId(),
        runId,
        conversationId,
        receivedAt: Date.now(),
        isError: terminal.isError,
        cancelled: terminal.cancelled,
        recovered: terminal.recovered,
        code: terminal.code === null ? null : String(terminal.code),
        providerAttempted: typeof terminal.providerAttempted === "boolean"
          ? terminal.providerAttempted : null,
      },
    };
    return writeAndVerifyPending(runtime.storage, next);
  });
}

function removeAndVerifyPending(storage) {
  storage.removeItem(REVIEW_PENDING_STORAGE_KEY);
  if (storage.getItem(REVIEW_PENDING_STORAGE_KEY) !== null) {
    throw pendingReviewError(
      "REVIEW_PENDING_CLEAR_FAILED",
      "Review recovery envelope could not be cleared",
    );
  }
  return true;
}

export async function clearTerminalReviewExclusive(
  { runId, conversationId, generation, ownerId, receiptId } = {},
  runtimeOptions = {},
) {
  if (!validReviewCasIdentity({ runId, conversationId, generation, ownerId })
      || !SAFE_REVIEW_RECEIPT_ID.test(String(receiptId || ""))) {
    throw pendingReviewError("REVIEW_PENDING_INVALID", "Review terminal clear identity is invalid");
  }
  const runtime = resolvedReviewLockRuntime(runtimeOptions);
  return withPendingReviewLock(runtime, () => {
    const existing = readPendingForMutation(runtime.storage);
    assertMatchingPendingReview(existing, { runId, conversationId, generation, ownerId });
    if (existing.state !== "terminal_received"
        || existing.terminalReceipt.receiptId !== receiptId) {
      throw pendingReviewError(
        "REVIEW_PENDING_CAS_MISMATCH",
        "The Review terminal receipt changed before it could be cleared",
      );
    }
    return removeAndVerifyPending(runtime.storage);
  });
}

export async function clearPreProviderReviewExclusive(
  { runId, conversationId, generation, ownerId, providerAttempted } = {},
  runtimeOptions = {},
) {
  if (!validReviewCasIdentity({ runId, conversationId, generation, ownerId })
      || providerAttempted !== false) {
    throw pendingReviewError("REVIEW_PENDING_INVALID", "Pre-provider Review clear is invalid");
  }
  const runtime = resolvedReviewLockRuntime(runtimeOptions);
  return withPendingReviewLock(runtime, () => {
    const existing = readPendingForMutation(runtime.storage);
    assertMatchingPendingReview(existing, { runId, conversationId, generation, ownerId });
    if (existing.state !== "claimed") {
      throw pendingReviewError(
        "REVIEW_PENDING_CAS_MISMATCH",
        "A received Review terminal cannot be cleared as a pre-provider failure",
      );
    }
    return removeAndVerifyPending(runtime.storage);
  });
}

export function reviewEvidenceUrls(data, apiBase = "/api", conversationId) {
  const payload = data && typeof data === "object" ? data : {};
  const scope = String(conversationId || "");
  if (!SAFE_REVIEW_SCOPE_ID.test(scope)) return [];
  const evidencePrefix = `${String(apiBase || "/api").replace(/\/$/, "")}/review-evidence`;
  const urls = [];

  const references = [
    payload.screenshotRef,
    payload.latestScreenshotRef,
    ...(Array.isArray(payload.screenshotRefs) ? payload.screenshotRefs : []),
    ...(Array.isArray(payload.evidence) ? payload.evidence : []),
    ...(Array.isArray(payload.latestScreenshots)
      ? payload.latestScreenshots.map((item) => item?.evidenceRef || item)
      : []),
  ];
  for (const reference of references) {
    if (!reference || typeof reference !== "object"
        || reference.schema !== "simworld-review-evidence-ref/v1"
        || !/^[a-f0-9]{64}$/.test(String(reference.scope_digest || ""))
        || !/^evidence-[a-f0-9]{48}$/.test(String(reference.handle || ""))
        || !/^sha256:[a-f0-9]{64}$/.test(String(reference.evidence_id || ""))) continue;
    const query = new URLSearchParams({
      evidenceId: reference.evidence_id,
      conversationId: scope,
    });
    urls.push(`${evidencePrefix}/${encodeURIComponent(reference.handle)}?${query.toString()}`);
  }
  return [...new Set(urls)];
}

export function mergeReviewEvidence(current, data, apiBase = "/api", conversationId) {
  const existing = Array.isArray(current) ? current : [];
  const hasRound = data?.round !== null && data?.round !== undefined;
  const round = hasRound && Number.isFinite(Number(data.round)) ? Number(data.round) : null;
  const next = reviewEvidenceUrls(data, apiBase, conversationId).map((url) => ({ round, url }));
  const seen = new Set(existing.map((item) => item?.url).filter(Boolean));
  return [
    ...existing,
    ...next.filter((item) => {
      if (seen.has(item.url)) return false;
      seen.add(item.url);
      return true;
    }),
  ];
}

export function assertReviewRunEvent(event, expectedRunId, expectedConversationId = null) {
  if (!expectedRunId) return null;
  const eventRunId = event?.data?.runId || event?.data?.run_id || null;
  const eventConversationId = event?.data?.conversationId || event?.data?.conversation_id || null;
  const requiresIdentity = event?.type === "run_start" || event?.type === "done";
  if ((eventRunId && eventRunId !== expectedRunId)
      || (requiresIdentity && !eventRunId)
      || (requiresIdentity && expectedConversationId
        && eventConversationId !== expectedConversationId)
      || (eventConversationId && expectedConversationId
        && eventConversationId !== expectedConversationId)) {
    const error = new Error("Review response did not match the persisted request ID");
    error.code = "REVIEW_RUN_ID_MISMATCH";
    error.retryable = true;
    throw error;
  }
  return eventRunId;
}

export function reviewTerminalDisposition({
  isError,
  cancelled,
  recovered,
  code,
  providerAttempted,
  identityBound = true,
} = {}) {
  const explicitPreProviderFailure = Boolean(
    isError === true && providerAttempted === false && identityBound === true,
  );
  const sameIdRetry = Boolean(
    isError === true
    && recovered !== true
    && cancelled !== true
    && !explicitPreProviderFailure
    && SAME_ID_RETRY_CODES.has(String(code || "")),
  );
  return Object.freeze({
    keepPending: sameIdRetry,
    sameIdRetry,
    startNew: Boolean(cancelled === true || explicitPreProviderFailure),
    recoveryBlocked: false,
  });
}

export function reviewFailureDisposition({
  hasReview,
  authoritativeDone,
  code,
  status,
  name,
  providerAttempted,
  identityBound = false,
} = {}) {
  if (!hasReview) {
    return Object.freeze({
      keepPending: false,
      sameIdRetry: false,
      startNew: false,
      recoveryBlocked: false,
    });
  }
  const recoveryBlocked = code === "REVIEW_TERMINAL_RECOVERY_REQUIRED";
  const explicitPreProviderFailure = Boolean(
    providerAttempted === false && identityBound === true,
  );
  const sameIdRetry = Boolean(
    !recoveryBlocked && !explicitPreProviderFailure,
  );
  const keepPending = recoveryBlocked || sameIdRetry;
  return Object.freeze({
    keepPending,
    sameIdRetry,
    startNew: !keepPending,
    recoveryBlocked,
  });
}

function nonNegativeNumber(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

// Cost events are repeated as a run progresses. Treat the server's latest
// aggregate snapshot as authoritative; never add event values in the browser.
export function mergeReviewAccounting(current, data, stage) {
  const payload = data && typeof data === "object" ? data : {};
  const existing = current && typeof current === "object" ? current : {};
  const budget = payload.budget
    || payload.reviewBudget
    || payload.review_budget
    || payload.loop?.budget
    || payload.review?.budget;
  const next = { ...existing };

  if (budget && typeof budget === "object") {
    const values = {
      limitUsd: nonNegativeNumber(budget.limit_usd ?? budget.limitUsd),
      spentUsd: nonNegativeNumber(budget.spent_usd ?? budget.spentUsd),
      remainingUsd: nonNegativeNumber(budget.remaining_usd ?? budget.remainingUsd),
      builderCostUsd: nonNegativeNumber(
        budget.stages?.builder?.cost_usd ?? budget.stages?.builder?.costUsd,
      ),
      criticCostUsd: nonNegativeNumber(
        budget.stages?.critic?.cost_usd ?? budget.stages?.critic?.costUsd,
      ),
    };
    for (const [key, value] of Object.entries(values)) {
      if (value !== null) next[key] = value;
    }
    if (typeof budget.exhausted === "boolean") next.exhausted = budget.exhausted;
  }

  const eventCost = nonNegativeNumber(payload.cost_usd ?? payload.costUsd);
  if (eventCost !== null && stage === "builder") next.builderCostUsd = eventCost;
  if (eventCost !== null && stage === "critic") next.criticCostUsd = eventCost;

  return Object.keys(next).length ? next : null;
}

export function buildChatStopPayload(activeRun, fallbackSessionId) {
  const payload = {
    sessionId: activeRun?.sessionId || fallbackSessionId || "_global",
  };
  if (activeRun?.runId) payload.runId = activeRun.runId;
  if (activeRun?.conversationId) payload.conversationId = activeRun.conversationId;
  return payload;
}

export function turnChangedScene(msg) {
  return (msg?.toolCalls || []).some((toolCall) => {
    const name = toolCall.displayName || (toolCall.name || "").replace(/^mcp__\w+__/, "");
    return SCENE_TOOLS.has(name) && toolCall.status !== "error";
  });
}

export function screenshotPathFromUrl(url) {
  if (!url || typeof url !== "string") return null;
  try {
    return new URLSearchParams(url.split("?")[1] || "").get("path");
  } catch {
    return null;
  }
}

export function getUniqueTextAppend(existingContent, rawDelta) {
  const existing = existingContent || "";
  const incoming = rawDelta || "";
  if (!incoming) return "";

  if (!existing) return incoming;
  if (incoming.startsWith(existing)) return incoming.slice(existing.length);
  if (existing.endsWith(incoming)) return "";

  const trimmedIncoming = incoming.trim();
  if (trimmedIncoming && existing.trimEnd().endsWith(trimmedIncoming)) return "";

  return incoming;
}

export function appendTextDeltaToMessage(message, rawDelta) {
  const appendText = getUniqueTextAppend(message.content, rawDelta);
  if (!appendText) return { changed: false, message };

  const blocks = message.blocks || [];
  const lastBlock = blocks[blocks.length - 1];
  const nextMessage = {
    ...message,
    content: (message.content || "") + appendText,
  };

  if (lastBlock?.type === "text") {
    nextMessage.blocks = [
      ...blocks.slice(0, -1),
      { ...lastBlock, content: lastBlock.content + appendText },
    ];
  } else {
    nextMessage.blocks = [...blocks, { type: "text", content: appendText }];
  }

  return { changed: true, message: nextMessage };
}

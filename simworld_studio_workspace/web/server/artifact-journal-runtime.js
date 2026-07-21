"use strict";

const crypto = require("node:crypto");
const fsConstants = require("node:fs").constants;
const fs = require("node:fs/promises");
const path = require("node:path");

const {
  ArtifactRevisionJournalError,
  HARD_LIMITS,
  createArtifactRevisionJournal,
} = require("./artifact-revision-journal");
const {
  normalizeReviewSceneBinding,
  reviewSceneBindingDigest,
  sameReviewSceneBinding,
} = require("./review-scene-binding");

const RUNTIME_SCHEMA = "simworld-artifact-journal-runtime/v1";
const DEFAULT_RETENTION_DAYS = 365;
const MAX_RETENTION_DAYS = 3650;
const SAFE_LABEL_RE = /^[A-Za-z0-9][A-Za-z0-9._:@-]{0,159}$/;
const SHA256_RE = /^[a-f0-9]{64}$/;
const TERMINAL_BUILD_STATES = new Set(["succeeded", "already_applied", "failed"]);
const TERMINAL_TIMELINE_STATES = new Set(["completed", "failed", "cancelled"]);
const REVIEW_MODES = new Set(["text_loop", "visual_loop"]);
const REVIEW_OUTCOMES = new Set(["completed", "failed", "cancelled"]);
const REVIEW_REASONS = new Set([
  "pass", "max_iterations", "builder_error", "critic_error",
  "budget_exhausted", "cancelled", "handler_completed", "handler_error",
]);
const REVIEW_VERDICTS = new Set(["PASS", "FAIL", "NEEDS_IMPROVEMENT", "UNKNOWN"]);
const REVIEW_OUTBOX_SCHEMA = "simworld-review-terminal-outbox/v3";
const REVIEW_OUTBOX_TICKET_SCHEMA = "simworld-review-terminal-outbox-ticket/v3";
const REVIEW_ABANDONMENT_SCHEMA = "simworld-review-abandonment/v1";
const REVIEW_OUTBOX_STATES = new Set(["prepared", "terminal_pending", "published", "abandoned"]);
const REVIEW_ACTIVE_OUTBOX_STATES = new Set(["prepared", "terminal_pending"]);
const REVIEW_ABANDONMENT_REASONS = new Set([
  "provider_not_started",
  "provider_result_unrecoverable",
  "provider_charge_reconciled_no_result",
]);
const MAX_REVIEW_OUTBOX_BYTES = 32 * 1024;
const MAX_REVIEW_OUTBOX_RECORDS = 10_000;
const DEFAULT_REVIEW_OUTBOX_RECORDS_PER_OWNER = 256;
const REVIEW_OUTBOX_RECORD_RE = /^([a-f0-9]{64})\.json$/;

class ArtifactJournalRuntimeError extends Error {
  constructor(code, message, { cause, retryable = false } = {}) {
    super(message);
    this.name = "ArtifactJournalRuntimeError";
    this.code = code;
    this.statusCode = 503;
    this.status = 503;
    this.retryable = retryable;
    if (cause) this.cause = cause;
  }
}

function isPlainObject(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function canonicalJson(value) {
  if (value === null || typeof value === "boolean" || typeof value === "string") {
    return JSON.stringify(value);
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new TypeError("Artifact journal summary contains a non-finite number");
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (!isPlainObject(value)) throw new TypeError("Artifact journal summary must be JSON data");
  return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
}

function digestJson(value) {
  return crypto.createHash("sha256").update(canonicalJson(value), "utf8").digest("hex");
}

function persistedRecordDigest(value, field) {
  let persisted;
  try {
    persisted = JSON.parse(JSON.stringify(value));
  } catch (error) {
    throw new TypeError(`${field} is not JSON-persistable`, { cause: error });
  }
  return digestJson(persisted);
}

function safeLabel(value, field) {
  const label = typeof value === "string" ? value.trim() : "";
  if (!SAFE_LABEL_RE.test(label)) throw new TypeError(`${field} is invalid`);
  return label;
}

function safeDigest(value, field) {
  const digest = typeof value === "string"
    ? value.trim().toLowerCase().replace(/^sha256:/, "")
    : "";
  if (!SHA256_RE.test(digest)) throw new TypeError(`${field} is invalid`);
  return digest;
}

function safeInteger(value, field, max = Number.MAX_SAFE_INTEGER) {
  if (!Number.isSafeInteger(value) || value < 0 || value > max) {
    throw new TypeError(`${field} is invalid`);
  }
  return value;
}

function safeOptionalCode(value) {
  const code = typeof value === "string" ? value.trim().toUpperCase() : "";
  return /^[A-Z][A-Z0-9_]{1,79}$/.test(code) ? code : null;
}

function isoTimestamp(value, field) {
  const text = typeof value === "string" ? value.trim() : "";
  const parsed = Date.parse(text);
  if (!text || !Number.isFinite(parsed) || new Date(parsed).toISOString() !== text) {
    throw new TypeError(`${field} is invalid`);
  }
  return text;
}

function boolFlag(value, field) {
  const text = String(value == null ? "" : value).trim().toLowerCase();
  if (["1", "true", "yes", "on"].includes(text)) return true;
  if (["0", "false", "no", "off"].includes(text)) return false;
  throw new TypeError(`${field} must be a boolean flag`);
}

function resolveReviewAbandonmentAuthorization(env = process.env) {
  const operatorText = String(env.VISTA_REVIEW_ABANDON_OPERATOR_ID || "").trim();
  const digestText = String(env.VISTA_REVIEW_ABANDON_TOKEN_SHA256 || "").trim().toLowerCase();
  if (!operatorText && !digestText) {
    return Object.freeze({ enabled: false, operatorId: null, tokenDigest: null, configError: null });
  }
  try {
    const operatorId = safeLabel(operatorText, "VISTA_REVIEW_ABANDON_OPERATOR_ID");
    const tokenDigest = safeDigest(digestText, "VISTA_REVIEW_ABANDON_TOKEN_SHA256");
    return Object.freeze({ enabled: true, operatorId, tokenDigest, configError: null });
  } catch (_error) {
    return Object.freeze({
      enabled: false,
      operatorId: null,
      tokenDigest: null,
      configError: "ARTIFACT_REVIEW_ABANDONMENT_CONFIG_INVALID",
    });
  }
}

function resolveArtifactJournalConfig(env = process.env) {
  const production = String(env.NODE_ENV || "").trim().toLowerCase() === "production";
  const rootText = String(env.VISTA_ARTIFACT_JOURNAL_ROOT || "").trim();
  let enabled = production || Boolean(rootText);
  let configError = null;

  if (env.VISTA_ARTIFACT_JOURNAL_ENABLED !== undefined
      && String(env.VISTA_ARTIFACT_JOURNAL_ENABLED).trim() !== "") {
    try {
      const requestedEnabled = boolFlag(
        env.VISTA_ARTIFACT_JOURNAL_ENABLED,
        "VISTA_ARTIFACT_JOURNAL_ENABLED",
      );
      if (production && !requestedEnabled) {
        configError = "ARTIFACT_JOURNAL_REQUIRED";
      } else {
        enabled = requestedEnabled;
      }
    } catch (_error) {
      configError = "ARTIFACT_JOURNAL_CONFIG_INVALID";
    }
  }
  if (production && !enabled) configError = "ARTIFACT_JOURNAL_REQUIRED";

  let root = null;
  if (enabled) {
    const normalized = rootText ? path.posix.normalize(rootText) : "";
    if (!rootText || !path.posix.isAbsolute(rootText) || normalized !== rootText
        || rootText === "/" || rootText.includes("\\")) {
      configError = configError || "ARTIFACT_JOURNAL_ROOT_INVALID";
    } else {
      root = rootText;
    }
  }

  const retentionRaw = String(env.VISTA_ARTIFACT_JOURNAL_RETENTION_DAYS || DEFAULT_RETENTION_DAYS).trim();
  const retentionDays = Number(retentionRaw);
  if (!Number.isSafeInteger(retentionDays) || retentionDays < 1 || retentionDays > MAX_RETENTION_DAYS) {
    configError = configError || "ARTIFACT_JOURNAL_RETENTION_INVALID";
  }

  return Object.freeze({
    schema: RUNTIME_SCHEMA,
    production,
    required: production,
    enabled,
    root,
    retentionDays: Number.isSafeInteger(retentionDays) ? retentionDays : DEFAULT_RETENTION_DAYS,
    configError,
  });
}

function expiresAt(createdAt, retentionDays) {
  const created = Date.parse(isoTimestamp(createdAt, "createdAt"));
  return new Date(created + retentionDays * 24 * 60 * 60 * 1000).toISOString();
}

function deterministicIdempotency(kind, artifactId, revision) {
  return `idem:${digestJson([kind, artifactId, revision])}`;
}

function correlation(kind, value) {
  return `${kind}:${digestJson([kind, value]).slice(0, 40)}`;
}

function countArray(value) {
  return Array.isArray(value) ? Math.min(value.length, 1_000_000) : 0;
}

function normalizeAccess(access, field = "access") {
  if (!isPlainObject(access)) throw new TypeError(`${field} is invalid`);
  return Object.freeze({
    ownerId: safeLabel(access.owner_id || access.ownerId, `${field}.ownerId`),
    sessionId: safeLabel(access.session_id || access.last_session_id || access.sessionId, `${field}.sessionId`),
  });
}

function importSummary(artifact) {
  if (!isPlainObject(artifact) || artifact.status !== "committed" || !isPlainObject(artifact.idempotency)
      || !isPlainObject(artifact.access) || !isPlainObject(artifact.scene_spec)) {
    throw new TypeError("Committed import artifact is invalid");
  }
  const artifactId = safeLabel(artifact.artifact_id, "artifact.artifact_id");
  const sceneId = safeLabel(artifact.scene_id, "artifact.scene_id");
  const profile = safeLabel(artifact.profile, "artifact.profile");
  const sourceChecksum = safeDigest(artifact.idempotency.source_checksum, "artifact.source_checksum");
  const importerVersion = safeLabel(artifact.idempotency.importer_version, "artifact.importer_version");
  const recordDigest = persistedRecordDigest(artifact, "artifact");
  return Object.freeze({
    schema: "simworld-vista-import-terminal/v1",
    terminal_status: "committed",
    artifact_id: artifactId,
    scene_id: sceneId,
    profile,
    source_checksum: sourceChecksum,
    importer_version: importerVersion,
    record_digest: recordDigest,
    entity_count: countArray(artifact.scene_spec.entities),
    timeline_event_count: countArray(artifact.scene_spec.timeline),
    evaluation_safe_included: Object.prototype.hasOwnProperty.call(artifact, "evaluation_safe"),
  });
}

function importJournalIdentity(artifact) {
  const summary = importSummary(artifact);
  const revision = safeLabel(artifact.artifact_revision, "artifact.artifact_revision");
  return { summary, revision, contentDigest: summary.record_digest };
}

function sceneBuildSummary(record) {
  if (!isPlainObject(record) || !TERMINAL_BUILD_STATES.has(record.status)
      || !isPlainObject(record.operation) || !isPlainObject(record.access)
      || !isPlainObject(record.result)) {
    throw new TypeError("Terminal scene build record is invalid");
  }
  const result = record.result;
  const rollback = isPlainObject(result.rollback) ? result.rollback : {};
  const recordDigest = persistedRecordDigest(record, "record");
  return Object.freeze({
    schema: "simworld-vista-scene-build-terminal/v1",
    terminal_status: record.status,
    plan_id: safeLabel(record.plan_id, "record.plan_id"),
    import_artifact_id: safeLabel(record.import_artifact_id, "record.import_artifact_id"),
    profile_id: safeLabel(record.profile_id, "record.profile_id"),
    operation_id: safeLabel(record.operation.operation_id, "record.operation.operation_id"),
    record_digest: recordDigest,
    scene_id: safeLabel(result.scene_id, "record.result.scene_id"),
    mutation_count: safeInteger(Number(result.mutation_count || 0), "record.result.mutation_count"),
    actor_count: countArray(result.actor_manifest),
    evidence_count: countArray(result.evidence),
    rollback_state: safeLabel(rollback.state || "not_required", "record.result.rollback.state"),
    rollback_failure_count: countArray(rollback.failures),
    failure_code: safeOptionalCode(result.error && result.error.code),
  });
}

function sceneBuildJournalLineage(record) {
  const summary = sceneBuildSummary(record);
  return Object.freeze({
    kind: "vista-scene-build",
    artifact_id: summary.plan_id,
    revision: safeLabel(record.operation.operation_id, "record.operation.operation_id"),
    content_digest: summary.record_digest,
  });
}

function eventStateCounts(run) {
  const counts = { completed: 0, failed: 0, pending: 0, skipped: 0, cancelled: 0, other: 0 };
  const events = isPlainObject(run) && Array.isArray(run.events) ? run.events : [];
  for (const event of events.slice(0, 100_000)) {
    const state = String(event && (event.state || event.status) || "").toLowerCase();
    if (Object.prototype.hasOwnProperty.call(counts, state)) counts[state] += 1;
    else counts.other += 1;
  }
  return counts;
}

function timelineSummary(record) {
  if (!isPlainObject(record) || !TERMINAL_TIMELINE_STATES.has(record.status)
      || !isPlainObject(record.identity) || !isPlainObject(record.access)
      || !isPlainObject(record.runtime) || !isPlainObject(record.operation)) {
    throw new TypeError("Terminal animation record is invalid");
  }
  const run = isPlainObject(record.run) ? record.run : null;
  const cleanup = run && isPlainObject(run.cleanup) ? run.cleanup : {};
  const recordDigest = persistedRecordDigest(record, "record");
  return Object.freeze({
    schema: "simworld-vista-animation-terminal/v1",
    terminal_status: record.status,
    run_id: safeLabel(record.run_id, "record.run_id"),
    record_digest: recordDigest,
    import_artifact_id: safeLabel(record.identity.import_artifact_id, "record.identity.import_artifact_id"),
    plan_id: safeLabel(record.identity.plan_id, "record.identity.plan_id"),
    scene_id: safeLabel(record.identity.scene_id, "record.identity.scene_id"),
    preflight_id: safeLabel(record.identity.preflight_id, "record.identity.preflight_id"),
    timeline_id: safeLabel(record.identity.timeline_id, "record.identity.timeline_id"),
    program_id: safeLabel(record.identity.program_id, "record.identity.program_id"),
    scene_build_operation_id: record.identity.scene_build_operation_id
      ? safeLabel(record.identity.scene_build_operation_id, "record.identity.scene_build_operation_id") : null,
    scene_build_content_digest: safeDigest(
      record.identity.scene_build_content_digest,
      "record.identity.scene_build_content_digest",
    ),
    replay_of: record.operation.replay_of ? safeLabel(record.operation.replay_of, "record.operation.replay_of") : null,
    runtime_profile_revision: safeLabel(record.runtime.profile_revision, "record.runtime.profile_revision"),
    runtime_content_digest: safeDigest(record.runtime.content_digest, "record.runtime.content_digest"),
    event_state_counts: eventStateCounts(run),
    confirmed_stopped: cleanup.confirmed_stopped === true,
    ended_pie: cleanup.ended_pie === true,
    error_code: safeOptionalCode(record.error && record.error.code),
  });
}

function safeOptionalLabel(value, field) {
  if (value === null || value === undefined || value === "") return null;
  return safeLabel(value, field);
}

function normalizeReviewEvidenceIds(value) {
  if (value === undefined || value === null) return Object.freeze([]);
  if (!Array.isArray(value) || value.length > 20) {
    throw new TypeError("Review evidence ids are invalid");
  }
  const ids = value.map((entry, index) => (
    `sha256:${safeDigest(entry, `review.evidenceIds[${index}]`)}`
  ));
  const unique = [...new Set(ids)].sort();
  if (unique.length !== ids.length) throw new TypeError("Review evidence ids contain duplicates");
  return Object.freeze(unique);
}

function normalizeReviewTerminal(value) {
  if (!isPlainObject(value)) throw new TypeError("Review terminal summary is invalid");
  const outcome = String(value.outcome || "").trim().toLowerCase();
  const reason = String(value.reason || (outcome === "completed" ? "handler_completed" : "handler_error")).trim().toLowerCase();
  const verdict = String(value.finalVerdict || "UNKNOWN").trim().toUpperCase();
  if (!REVIEW_OUTCOMES.has(outcome) || !REVIEW_REASONS.has(reason) || !REVIEW_VERDICTS.has(verdict)) {
    throw new TypeError("Review terminal summary contains an invalid enum");
  }
  const provider = safeOptionalLabel(value.provider, "review.provider");
  const model = safeOptionalLabel(value.model, "review.model");
  const evidenceIds = normalizeReviewEvidenceIds(value.evidenceIds || value.evidence_ids);
  const bindingAfterValue = value.bindingAfter === undefined ? value.binding_after : value.bindingAfter;
  const bindingAfter = bindingAfterValue === null || bindingAfterValue === undefined
    ? null : normalizeReviewSceneBinding(bindingAfterValue, "review.bindingAfter");
  const errorCode = safeOptionalCode(value.errorCode || value.error_code);
  if ((provider === null) !== (model === null)) {
    throw new TypeError("Review provider and model must be recorded together");
  }
  if (verdict === "PASS" && (outcome !== "completed" || reason !== "pass"
      || provider === null || evidenceIds.length < 1 || bindingAfter === null)) {
    throw new TypeError("A passing Review terminal lacks provider, evidence, or scene binding");
  }
  if (bindingAfter === null
      && !(outcome === "failed" && errorCode === "REVIEW_SCENE_BINDING_UNAVAILABLE")) {
    throw new TypeError("Review terminal lacks its post-execution scene binding");
  }
  return Object.freeze({
    outcome,
    reason,
    final_verdict: verdict,
    rounds: safeInteger(Number(value.rounds || 0), "review.rounds", 100),
    error_code: errorCode,
    provider,
    model,
    evidence_ids: evidenceIds,
    binding_after: bindingAfter,
  });
}

function storedReviewTerminal(value) {
  if (!isPlainObject(value)) throw new TypeError("Stored Review terminal summary is invalid");
  const expected = [
    "binding_after", "error_code", "evidence_ids", "final_verdict", "model",
    "outcome", "provider", "reason", "rounds",
  ];
  const keys = Object.keys(value).sort();
  if (keys.length !== expected.length || keys.some((key, index) => key !== expected[index])) {
    throw new TypeError("Stored Review terminal summary is invalid");
  }
  return normalizeReviewTerminal({
    outcome: value.outcome,
    reason: value.reason,
    finalVerdict: value.final_verdict,
    rounds: value.rounds,
    errorCode: value.error_code,
    provider: value.provider,
    model: value.model,
    evidenceIds: value.evidence_ids,
    bindingAfter: value.binding_after,
  });
}

function publicReviewTerminal(value) {
  if (value === null) return null;
  const normalized = storedReviewTerminal(value);
  return Object.freeze({
    outcome: normalized.outcome,
    reason: normalized.reason,
    finalVerdict: normalized.final_verdict,
    rounds: normalized.rounds,
    errorCode: normalized.error_code,
    provider: normalized.provider,
    model: normalized.model,
    evidenceIds: normalized.evidence_ids,
    bindingAfter: normalized.binding_after,
  });
}

function reviewTerminalProjection(record) {
  return Object.freeze({
    schema: record.schema,
    lookup_digest: record.lookup_digest,
    owner_id: record.owner_id,
    session_id: record.session_id,
    mode: record.mode,
    request_digest: record.request_digest,
    input_digest: record.input_digest,
    journal_run_id: record.journal_run_id,
    started_at_ms: record.started_at_ms,
    created_at: record.created_at,
    binding_before: record.binding_before,
    terminal: record.terminal,
  });
}

function reviewTerminalDigest(record) {
  return digestJson(reviewTerminalProjection(record));
}

function assertReviewBindingContinuity(before, after) {
  if (!after) return;
  if (before.scope_id !== after.scope_id || before.slot_id !== after.slot_id
      || before.lease_id_sha256 !== after.lease_id_sha256
      || canonicalJson(before.scene_build_lineage) !== canonicalJson(after.scene_build_lineage)) {
    throw new TypeError("Review scene binding changed authority or VISTA lineage");
  }
}

function normalizeReviewAbandonmentDecision(value, operatorId, startedAtMs, nowText) {
  if (!isPlainObject(value)) throw new TypeError("Review abandonment decision is invalid");
  const expected = ["decidedAt", "decisionId", "evidenceDigest", "reasonCode"].sort();
  const keys = Object.keys(value).sort();
  if (keys.length !== expected.length || keys.some((key, index) => key !== expected[index])) {
    throw new TypeError("Review abandonment decision is invalid");
  }
  const decisionId = safeLabel(value.decisionId, "decision.decisionId");
  const decidedAt = isoTimestamp(value.decidedAt, "decision.decidedAt");
  const reasonCode = String(value.reasonCode || "").trim().toLowerCase();
  const evidenceDigest = safeDigest(value.evidenceDigest, "decision.evidenceDigest");
  const decidedAtMs = Date.parse(decidedAt);
  const nowMs = Date.parse(nowText);
  if (!REVIEW_ABANDONMENT_REASONS.has(reasonCode)
      || decidedAtMs < startedAtMs || decidedAtMs > nowMs + 5 * 60 * 1000) {
    throw new TypeError("Review abandonment decision is invalid");
  }
  return Object.freeze({
    schema: REVIEW_ABANDONMENT_SCHEMA,
    decision_id: decisionId,
    decided_at: decidedAt,
    operator_id: operatorId,
    reason_code: reasonCode,
    evidence_digest: evidenceDigest,
  });
}

function storedReviewAbandonment(value) {
  if (!isPlainObject(value)) throw new TypeError("Stored Review abandonment is invalid");
  const expected = [
    "decided_at", "decision_id", "evidence_digest", "operator_id", "reason_code", "schema",
  ].sort();
  const keys = Object.keys(value).sort();
  if (keys.length !== expected.length || keys.some((key, index) => key !== expected[index])
      || value.schema !== REVIEW_ABANDONMENT_SCHEMA) {
    throw new TypeError("Stored Review abandonment is invalid");
  }
  const reasonCode = String(value.reason_code || "").trim().toLowerCase();
  if (!REVIEW_ABANDONMENT_REASONS.has(reasonCode)) {
    throw new TypeError("Stored Review abandonment is invalid");
  }
  return Object.freeze({
    schema: REVIEW_ABANDONMENT_SCHEMA,
    decision_id: safeLabel(value.decision_id, "abandonment.decision_id"),
    decided_at: isoTimestamp(value.decided_at, "abandonment.decided_at"),
    operator_id: safeLabel(value.operator_id, "abandonment.operator_id"),
    reason_code: reasonCode,
    evidence_digest: safeDigest(value.evidence_digest, "abandonment.evidence_digest"),
  });
}

function reviewAbandonmentDigest(record, abandonment) {
  return digestJson({
    schema: REVIEW_ABANDONMENT_SCHEMA,
    lookup_digest: record.lookup_digest,
    owner_id: record.owner_id,
    session_id: record.session_id,
    mode: record.mode,
    input_digest: record.input_digest,
    request_digest: record.request_digest,
    journal_run_id: record.journal_run_id,
    started_at_ms: record.started_at_ms,
    created_at: record.created_at,
    binding_before: record.binding_before,
    abandonment,
  });
}

function createArtifactJournalRuntime(options = {}) {
  const config = options.config || resolveArtifactJournalConfig(options.env || process.env);
  const reviewAbandonmentAuthorization = resolveReviewAbandonmentAuthorization(
    options.env || process.env,
  );
  const clock = typeof options.clock === "function" ? options.clock : () => new Date();
  const randomBytes = typeof options.randomBytes === "function" ? options.randomBytes : crypto.randomBytes;
  const maxReviewOutboxRecords = options.maxReviewOutboxRecords === undefined
    ? MAX_REVIEW_OUTBOX_RECORDS
    : safeInteger(options.maxReviewOutboxRecords, "maxReviewOutboxRecords", MAX_REVIEW_OUTBOX_RECORDS);
  if (maxReviewOutboxRecords < 1) {
    throw new TypeError("maxReviewOutboxRecords is invalid");
  }
  const maxReviewOutboxRecordsPerOwner = options.maxReviewOutboxRecordsPerOwner === undefined
    ? Math.min(DEFAULT_REVIEW_OUTBOX_RECORDS_PER_OWNER, maxReviewOutboxRecords)
    : safeInteger(
      options.maxReviewOutboxRecordsPerOwner,
      "maxReviewOutboxRecordsPerOwner",
      maxReviewOutboxRecords,
    );
  if (maxReviewOutboxRecordsPerOwner < 1) {
    throw new TypeError("maxReviewOutboxRecordsPerOwner is invalid");
  }
  const journal = options.journal || (config.enabled && !config.configError && config.root
    ? createArtifactRevisionJournal({ root: config.root, now: clock })
    : null);
  const reviewOutboxRoot = config.root ? path.join(config.root, "review-outbox") : null;
  const reviewOutboxLockPath = reviewOutboxRoot
    ? path.join(reviewOutboxRoot, ".review-outbox.lock")
    : null;
  let lastFailureCode = config.configError;
  let lastIntegrity = null;
  let integrityVerified = !config.required;
  let cachedReadiness = null;
  let verificationPromise = null;
  let failureEpoch = 0;

  function maxJournalEntries() {
    return journal && journal.limits && Number.isSafeInteger(journal.limits.maxJournalEntries)
      ? journal.limits.maxJournalEntries
      : HARD_LIMITS.maxJournalEntries;
  }

  function capacityReport(entryCount = maxJournalEntries()) {
    return {
      status: "not_ready",
      revision: {
        schema: RUNTIME_SCHEMA,
        enabled: true,
        verification: "full_hash_chain",
        entry_count: entryCount,
        max_entry_count: maxJournalEntries(),
      },
      causes: [{
        code: "ARTIFACT_JOURNAL_CAPACITY_EXHAUSTED",
        message: "The artifact journal has reached its configured entry capacity.",
        retryable: false,
        dependency: "artifact_journal",
      }],
    };
  }

  function reviewOutboxCapacityReport(recordCount = maxReviewOutboxRecords) {
    return {
      status: "not_ready",
      revision: {
        schema: RUNTIME_SCHEMA,
        enabled: true,
        verification: "full_hash_chain",
        review_outbox_record_count: recordCount,
        max_review_outbox_record_count: maxReviewOutboxRecords,
      },
      causes: [{
        code: "ARTIFACT_REVIEW_OUTBOX_CAPACITY_EXHAUSTED",
        message: "The Review terminal outbox has reached its configured record capacity.",
        retryable: false,
        dependency: "artifact_journal",
      }],
    };
  }

  function unavailable(code = lastFailureCode || "ARTIFACT_JOURNAL_UNAVAILABLE", cause) {
    const nonRetryable = new Set([
      "ARTIFACT_JOURNAL_CONFIG_INVALID",
      "ARTIFACT_JOURNAL_ROOT_INSECURE",
      "ARTIFACT_JOURNAL_CORRUPT",
      "ARTIFACT_JOURNAL_LIMIT_EXCEEDED",
      "ARTIFACT_JOURNAL_CAPACITY_EXHAUSTED",
      "ARTIFACT_REVIEW_OUTBOX_CAPACITY_EXHAUSTED",
      "ARTIFACT_REVIEW_OWNER_QUOTA_EXHAUSTED",
      "ARTIFACT_REVIEW_ABANDONMENT_CONFIG_INVALID",
      "ARTIFACT_REVIEW_ABANDONMENT_NOT_CONFIGURED",
      "ARTIFACT_REVIEW_OPERATOR_AUTH_REQUIRED",
      "ARTIFACT_REVIEW_ABANDONMENT_CONFLICT",
      "ARTIFACT_REVIEW_RUN_ABANDONED",
      "ARTIFACT_IDEMPOTENCY_CONFLICT",
    ]);
    return new ArtifactJournalRuntimeError(
      code,
      "Durable artifact journal is unavailable.",
      { cause, retryable: !nonRetryable.has(code) },
    );
  }

  function revokeIntegrity(code) {
    lastFailureCode = code || "ARTIFACT_JOURNAL_UNAVAILABLE";
    integrityVerified = false;
    cachedReadiness = null;
    failureEpoch += 1;
  }

  function currentUid() {
    return typeof process.getuid === "function" ? process.getuid() : null;
  }

  function statMode(stat) {
    return stat.mode & 0o777;
  }

  function randomHex(byteLength) {
    let bytes;
    try {
      bytes = randomBytes(byteLength);
    } catch (error) {
      throw unavailable("ARTIFACT_JOURNAL_WRITE_FAILED", error);
    }
    if (!Buffer.isBuffer(bytes) || bytes.length !== byteLength) {
      throw unavailable("ARTIFACT_JOURNAL_WRITE_FAILED");
    }
    return bytes.toString("hex");
  }

  function clockTimestamp() {
    let value;
    try {
      value = clock();
    } catch (error) {
      throw unavailable("ARTIFACT_JOURNAL_WRITE_FAILED", error);
    }
    const date = value instanceof Date ? value : new Date(value);
    if (!Number.isFinite(date.getTime())) throw unavailable("ARTIFACT_JOURNAL_WRITE_FAILED");
    return date.toISOString();
  }

  function authorizeReviewAbandonment(authorizationToken) {
    if (reviewAbandonmentAuthorization.configError) {
      throw unavailable(reviewAbandonmentAuthorization.configError);
    }
    if (!reviewAbandonmentAuthorization.enabled) {
      throw unavailable("ARTIFACT_REVIEW_ABANDONMENT_NOT_CONFIGURED");
    }
    const token = typeof authorizationToken === "string"
      ? Buffer.from(authorizationToken, "utf8")
      : null;
    if (!token || token.length < 32 || token.length > 4096) {
      throw unavailable("ARTIFACT_REVIEW_OPERATOR_AUTH_REQUIRED");
    }
    const observed = crypto.createHash("sha256").update(token).digest();
    const expected = Buffer.from(reviewAbandonmentAuthorization.tokenDigest, "hex");
    if (observed.length !== expected.length || !crypto.timingSafeEqual(observed, expected)) {
      throw unavailable("ARTIFACT_REVIEW_OPERATOR_AUTH_REQUIRED");
    }
    return reviewAbandonmentAuthorization.operatorId;
  }

  async function ensureSecureReviewOutboxRoot({ create = true } = {}) {
    if (!reviewOutboxRoot || !Number.isInteger(fsConstants.O_NOFOLLOW)
        || !Number.isInteger(fsConstants.O_DIRECTORY)) {
      throw unavailable("ARTIFACT_JOURNAL_ROOT_INSECURE");
    }
    let created = false;
    if (create) {
      try {
        await fs.mkdir(reviewOutboxRoot, { mode: 0o700, recursive: false });
        created = true;
      } catch (error) {
        if (!error || error.code !== "EEXIST") {
          throw unavailable("ARTIFACT_JOURNAL_ROOT_INSECURE", error);
        }
      }
    }
    try {
      const stat = await fs.lstat(reviewOutboxRoot);
      const resolved = await fs.realpath(reviewOutboxRoot);
      if (!stat.isDirectory() || stat.isSymbolicLink() || statMode(stat) !== 0o700
          || resolved !== reviewOutboxRoot
          || (currentUid() !== null && stat.uid !== currentUid())) {
        throw unavailable("ARTIFACT_JOURNAL_ROOT_INSECURE");
      }
      if (created) await fsyncDirectory(config.root);
    } catch (error) {
      if (error instanceof ArtifactJournalRuntimeError) throw error;
      throw unavailable("ARTIFACT_JOURNAL_ROOT_INSECURE", error);
    }
  }

  async function fsyncDirectory(directory) {
    let handle;
    try {
      handle = await fs.open(
        directory,
        fsConstants.O_RDONLY | fsConstants.O_DIRECTORY | fsConstants.O_NOFOLLOW,
      );
      await handle.sync();
    } catch (error) {
      throw unavailable("ARTIFACT_JOURNAL_WRITE_FAILED", error);
    } finally {
      if (handle) await handle.close().catch(() => {});
    }
  }

  async function acquireReviewOutboxLock() {
    for (let attempt = 0; attempt < 200; attempt += 1) {
      let handle;
      let lockStat;
      try {
        handle = await fs.open(
          reviewOutboxLockPath,
          fsConstants.O_WRONLY | fsConstants.O_CREAT | fsConstants.O_EXCL | fsConstants.O_NOFOLLOW,
          0o600,
        );
        await handle.chmod(0o600);
        lockStat = await handle.stat();
        await handle.writeFile(Buffer.from(canonicalJson({
          schema: "simworld-review-terminal-outbox-lock/v1",
          pid: process.pid,
        }), "utf8"));
        await handle.sync();
        const verifiedStat = await handle.stat();
        if (!verifiedStat.isFile() || verifiedStat.nlink !== 1 || statMode(verifiedStat) !== 0o600
            || verifiedStat.dev !== lockStat.dev || verifiedStat.ino !== lockStat.ino
            || (currentUid() !== null && verifiedStat.uid !== currentUid())) {
          throw unavailable("ARTIFACT_JOURNAL_ROOT_INSECURE");
        }
        return { handle, stat: verifiedStat };
      } catch (error) {
        if (handle) {
          await handle.close().catch(() => {});
          if (lockStat) {
            try {
              const current = await fs.lstat(reviewOutboxLockPath);
              if (current.dev === lockStat.dev && current.ino === lockStat.ino) {
                await fs.unlink(reviewOutboxLockPath);
              }
            } catch (_cleanupError) {}
          }
        }
        if (!error || error.code !== "EEXIST") {
          if (error instanceof ArtifactJournalRuntimeError) throw error;
          throw unavailable("ARTIFACT_JOURNAL_ROOT_INSECURE", error);
        }
        if (attempt + 1 < 200) {
          await new Promise((resolve) => setTimeout(resolve, 5));
        }
      }
    }
    throw unavailable("ARTIFACT_JOURNAL_BUSY");
  }

  async function releaseReviewOutboxLock(lock) {
    let releaseError = null;
    try {
      const current = await fs.lstat(reviewOutboxLockPath);
      if (!current.isFile() || current.isSymbolicLink()
          || current.dev !== lock.stat.dev || current.ino !== lock.stat.ino
          || statMode(current) !== 0o600) {
        throw unavailable("ARTIFACT_JOURNAL_ROOT_INSECURE");
      }
      await fs.unlink(reviewOutboxLockPath);
      await fsyncDirectory(reviewOutboxRoot);
    } catch (error) {
      releaseError = error instanceof ArtifactJournalRuntimeError
        ? error
        : unavailable("ARTIFACT_JOURNAL_ROOT_INSECURE", error);
    } finally {
      await lock.handle.close().catch(() => {});
    }
    if (releaseError) throw releaseError;
  }

  async function withReviewOutboxLock(operation) {
    await ensureSecureReviewOutboxRoot();
    const lock = await acquireReviewOutboxLock();
    let result;
    let operationError = null;
    try {
      result = await operation();
    } catch (error) {
      operationError = error;
    }
    try {
      await releaseReviewOutboxLock(lock);
    } catch (error) {
      if (!operationError) operationError = error;
    }
    if (operationError) throw operationError;
    return result;
  }

  function validateReviewOutboxRecord(value, expectedDigest) {
    if (!isPlainObject(value)) throw unavailable("ARTIFACT_JOURNAL_CORRUPT");
    const expectedKeys = [
      "abandonment", "abandonment_digest", "binding_before", "created_at", "input_digest",
      "journal_run_id", "lookup_digest", "mode", "owner_id", "request_digest", "schema",
      "session_id", "started_at_ms", "state", "terminal", "terminal_digest",
    ].sort();
    const keys = Object.keys(value).sort();
    if (keys.length !== expectedKeys.length
        || keys.some((key, index) => key !== expectedKeys[index])
        || value.schema !== REVIEW_OUTBOX_SCHEMA
        || safeDigest(value.lookup_digest, "outbox.lookup_digest") !== expectedDigest
        || !REVIEW_MODES.has(String(value.mode || ""))
        || !REVIEW_OUTBOX_STATES.has(String(value.state || ""))) {
      throw unavailable("ARTIFACT_JOURNAL_CORRUPT");
    }
    let ownerId;
    let sessionId;
    let journalRunId;
    let createdAt;
    let startedAtMs;
    let terminal;
    let bindingBefore;
    let terminalDigest;
    let abandonment;
    let abandonmentDigest;
    let inputDigest;
    let requestDigest;
    try {
      ownerId = safeLabel(value.owner_id, "outbox.owner_id");
      sessionId = safeLabel(value.session_id, "outbox.session_id");
      journalRunId = safeLabel(value.journal_run_id, "outbox.journal_run_id");
      createdAt = isoTimestamp(value.created_at, "outbox.created_at");
      startedAtMs = safeInteger(value.started_at_ms, "outbox.started_at_ms");
      if (!/^review-journal-[a-f0-9]{48}$/.test(journalRunId)
          || !Number.isFinite(new Date(startedAtMs).getTime())) {
        throw new TypeError("Review outbox identity is invalid");
      }
      terminal = value.terminal === null ? null : storedReviewTerminal(value.terminal);
      bindingBefore = normalizeReviewSceneBinding(value.binding_before, "outbox.binding_before");
      inputDigest = safeDigest(value.input_digest, "outbox.input_digest");
      requestDigest = safeDigest(value.request_digest, "outbox.request_digest");
      terminalDigest = value.terminal_digest === null
        ? null : safeDigest(value.terminal_digest, "outbox.terminal_digest");
      abandonment = value.abandonment === null
        ? null : storedReviewAbandonment(value.abandonment);
      abandonmentDigest = value.abandonment_digest === null
        ? null : safeDigest(value.abandonment_digest, "outbox.abandonment_digest");
      const terminalState = value.state === "terminal_pending" || value.state === "published";
      const abandonedState = value.state === "abandoned";
      if ((terminalState !== (terminal !== null && terminalDigest !== null))
          || (abandonedState !== (abandonment !== null && abandonmentDigest !== null))
          || (terminal !== null && abandonment !== null)) {
        throw new TypeError("Review outbox state is invalid");
      }
      if (terminal) {
        assertReviewBindingContinuity(bindingBefore, terminal.binding_after);
        if (reviewTerminalDigest({ ...value, binding_before: bindingBefore, terminal }) !== terminalDigest) {
          throw new TypeError("Review outbox terminal digest is invalid");
        }
      }
      if (abandonment) {
        if (Date.parse(abandonment.decided_at) < startedAtMs
            || reviewAbandonmentDigest(
              { ...value, binding_before: bindingBefore, input_digest: inputDigest, request_digest: requestDigest },
              abandonment,
            ) !== abandonmentDigest) {
          throw new TypeError("Review outbox abandonment digest is invalid");
        }
      }
    } catch (error) {
      if (error instanceof ArtifactJournalRuntimeError) throw error;
      throw unavailable("ARTIFACT_JOURNAL_CORRUPT", error);
    }
    return Object.freeze({
      schema: REVIEW_OUTBOX_SCHEMA,
      lookup_digest: expectedDigest,
      owner_id: ownerId,
      session_id: sessionId,
      mode: value.mode,
      input_digest: inputDigest,
      request_digest: requestDigest,
      journal_run_id: journalRunId,
      started_at_ms: startedAtMs,
      state: value.state,
      terminal,
      binding_before: bindingBefore,
      terminal_digest: terminalDigest,
      abandonment,
      abandonment_digest: abandonmentDigest,
      created_at: createdAt,
    });
  }

  async function readReviewOutboxRecord(lookupDigest, { allowMissing = false } = {}) {
    const checkedDigest = safeDigest(lookupDigest, "outbox.lookup_digest");
    const filename = path.join(reviewOutboxRoot, `${checkedDigest}.json`);
    let handle;
    try {
      handle = await fs.open(filename, fsConstants.O_RDONLY | fsConstants.O_NOFOLLOW);
      const before = await handle.stat();
      if (!before.isFile() || before.nlink !== 1 || statMode(before) !== 0o600
          || before.size < 2 || before.size > MAX_REVIEW_OUTBOX_BYTES
          || (currentUid() !== null && before.uid !== currentUid())) {
        throw unavailable("ARTIFACT_JOURNAL_CORRUPT");
      }
      const bytes = await handle.readFile();
      const after = await handle.stat();
      if (before.dev !== after.dev || before.ino !== after.ino || before.size !== after.size
          || bytes.length !== after.size) {
        throw unavailable("ARTIFACT_JOURNAL_CORRUPT");
      }
      let parsed;
      try {
        parsed = JSON.parse(bytes.toString("utf8"));
      } catch (error) {
        throw unavailable("ARTIFACT_JOURNAL_CORRUPT", error);
      }
      if (!bytes.equals(Buffer.from(canonicalJson(parsed), "utf8"))) {
        throw unavailable("ARTIFACT_JOURNAL_CORRUPT");
      }
      return validateReviewOutboxRecord(parsed, checkedDigest);
    } catch (error) {
      if (allowMissing && error && error.code === "ENOENT") return null;
      if (error instanceof ArtifactJournalRuntimeError) throw error;
      throw unavailable("ARTIFACT_JOURNAL_CORRUPT", error);
    } finally {
      if (handle) await handle.close().catch(() => {});
    }
  }

  async function reviewOutboxNames() {
    let entries;
    try {
      entries = await fs.readdir(reviewOutboxRoot, { withFileTypes: true });
    } catch (error) {
      throw unavailable("ARTIFACT_JOURNAL_CORRUPT", error);
    }
    const names = [];
    for (const entry of entries) {
      if (entry.name === ".review-outbox.lock" && entry.isFile() && !entry.isSymbolicLink()) continue;
      if (!entry.isFile() || entry.isSymbolicLink() || !REVIEW_OUTBOX_RECORD_RE.test(entry.name)) {
        throw unavailable("ARTIFACT_JOURNAL_CORRUPT");
      }
      names.push(entry.name);
    }
    if (names.length > maxReviewOutboxRecords) {
      throw unavailable("ARTIFACT_JOURNAL_LIMIT_EXCEEDED");
    }
    return names.sort();
  }

  async function writeReviewOutboxRecord(record, { create = false } = {}) {
    const checked = validateReviewOutboxRecord(record, record.lookup_digest);
    const bytes = Buffer.from(canonicalJson(checked), "utf8");
    if (bytes.length < 2 || bytes.length > MAX_REVIEW_OUTBOX_BYTES) {
      throw unavailable("ARTIFACT_JOURNAL_LIMIT_EXCEEDED");
    }
    const filename = path.join(reviewOutboxRoot, `${checked.lookup_digest}.json`);
    const pendingPath = path.join(
      reviewOutboxRoot,
      `${checked.lookup_digest}.${randomHex(12)}.pending`,
    );
    let handle;
    let pendingStat;
    try {
      handle = await fs.open(
        pendingPath,
        fsConstants.O_WRONLY | fsConstants.O_CREAT | fsConstants.O_EXCL | fsConstants.O_NOFOLLOW,
        0o600,
      );
      await handle.chmod(0o600);
      await handle.writeFile(bytes);
      await handle.sync();
      pendingStat = await handle.stat();
      if (!pendingStat.isFile() || pendingStat.nlink !== 1 || statMode(pendingStat) !== 0o600
          || pendingStat.size !== bytes.length
          || (currentUid() !== null && pendingStat.uid !== currentUid())) {
        throw unavailable("ARTIFACT_JOURNAL_ROOT_INSECURE");
      }
      await handle.close();
      handle = null;
      if (create) {
        await fs.link(pendingPath, filename);
        const linked = await fs.lstat(filename);
        if (!linked.isFile() || linked.isSymbolicLink()
            || linked.dev !== pendingStat.dev || linked.ino !== pendingStat.ino
            || linked.nlink !== 2 || statMode(linked) !== 0o600) {
          throw unavailable("ARTIFACT_JOURNAL_ROOT_INSECURE");
        }
      } else {
        await fs.rename(pendingPath, filename);
        const renamed = await fs.lstat(filename);
        if (!renamed.isFile() || renamed.isSymbolicLink()
            || renamed.dev !== pendingStat.dev || renamed.ino !== pendingStat.ino
            || renamed.nlink !== 1 || statMode(renamed) !== 0o600) {
          throw unavailable("ARTIFACT_JOURNAL_ROOT_INSECURE");
        }
        pendingStat = null;
      }
      if (create) await fs.unlink(pendingPath);
      pendingStat = null;
      await fsyncDirectory(reviewOutboxRoot);
    } catch (error) {
      if (handle) await handle.close().catch(() => {});
      if (error instanceof ArtifactJournalRuntimeError) throw error;
      if (error && error.code === "EEXIST") {
        throw unavailable("ARTIFACT_IDEMPOTENCY_CONFLICT", error);
      }
      throw unavailable("ARTIFACT_JOURNAL_WRITE_FAILED", error);
    } finally {
      if (pendingStat) {
        try {
          const current = await fs.lstat(pendingPath);
          if (current.dev === pendingStat.dev && current.ino === pendingStat.ino) {
            await fs.unlink(pendingPath);
          }
        } catch (_cleanupError) {}
      }
    }
    return checked;
  }

  function reviewOutboxTicket(record, created) {
    return Object.freeze({
      schema: REVIEW_OUTBOX_TICKET_SCHEMA,
      lookupDigest: record.lookup_digest,
      ownerId: record.owner_id,
      sessionId: record.session_id,
      mode: record.mode,
      inputDigest: record.input_digest,
      requestDigest: record.request_digest,
      journalRunId: record.journal_run_id,
      startedAt: record.started_at_ms,
      state: record.state,
      created: created === true,
      bindingBefore: record.binding_before,
      terminal: publicReviewTerminal(record.terminal),
    });
  }

  function validateReviewOutboxTicket(ticket) {
    if (!isPlainObject(ticket) || ticket.schema !== REVIEW_OUTBOX_TICKET_SCHEMA) {
      throw new TypeError("Review outbox ticket is invalid");
    }
    const lookupDigest = safeDigest(ticket.lookupDigest, "ticket.lookupDigest");
    const ownerId = safeLabel(ticket.ownerId, "ticket.ownerId");
    const sessionId = safeLabel(ticket.sessionId, "ticket.sessionId");
    const journalRunId = safeLabel(ticket.journalRunId, "ticket.journalRunId");
    const mode = String(ticket.mode || "");
    const inputDigest = safeDigest(ticket.inputDigest, "ticket.inputDigest");
    const requestDigest = safeDigest(ticket.requestDigest, "ticket.requestDigest");
    const bindingBefore = normalizeReviewSceneBinding(ticket.bindingBefore, "ticket.bindingBefore");
    const startedAt = safeInteger(Number(ticket.startedAt), "ticket.startedAt");
    if (!REVIEW_MODES.has(mode) || !/^review-journal-[a-f0-9]{48}$/.test(journalRunId)
        || !Number.isFinite(new Date(startedAt).getTime())) {
      throw new TypeError("Review outbox ticket is invalid");
    }
    return {
      lookupDigest, ownerId, sessionId, journalRunId, mode,
      inputDigest, requestDigest, bindingBefore, startedAt,
    };
  }

  function assertTicketMatchesRecord(ticket, record) {
    if (ticket.lookupDigest !== record.lookup_digest || ticket.ownerId !== record.owner_id
        || ticket.sessionId !== record.session_id || ticket.mode !== record.mode
        || ticket.inputDigest !== record.input_digest
        || ticket.requestDigest !== record.request_digest
        || ticket.journalRunId !== record.journal_run_id
        || ticket.startedAt !== record.started_at_ms
        || !sameReviewSceneBinding(ticket.bindingBefore, record.binding_before)) {
      throw unavailable("ARTIFACT_IDEMPOTENCY_CONFLICT");
    }
  }

  function validateReviewAbandonmentIdentity(value) {
    if (!isPlainObject(value)) throw new TypeError("Review abandonment identity is invalid");
    const expected = ["journalRunId", "lookupDigest", "ownerId", "sessionId"].sort();
    const keys = Object.keys(value).sort();
    if (keys.length !== expected.length || keys.some((key, index) => key !== expected[index])) {
      throw new TypeError("Review abandonment identity is invalid");
    }
    const identity = Object.freeze({
      lookupDigest: safeDigest(value.lookupDigest, "identity.lookupDigest"),
      ownerId: safeLabel(value.ownerId, "identity.ownerId"),
      sessionId: safeLabel(value.sessionId, "identity.sessionId"),
      journalRunId: safeLabel(value.journalRunId, "identity.journalRunId"),
    });
    if (!/^review-journal-[a-f0-9]{48}$/.test(identity.journalRunId)) {
      throw new TypeError("Review abandonment identity is invalid");
    }
    return identity;
  }

  function assertAbandonmentIdentityMatchesRecord(identity, record) {
    if (identity.lookupDigest !== record.lookup_digest || identity.ownerId !== record.owner_id
        || identity.sessionId !== record.session_id
        || identity.journalRunId !== record.journal_run_id) {
      throw unavailable("ARTIFACT_IDEMPOTENCY_CONFLICT");
    }
  }

  async function appendReviewOutboxRecord(record, appendOptions) {
    const terminal = storedReviewTerminal(record.terminal);
    const bindingBefore = normalizeReviewSceneBinding(record.binding_before, "outbox.binding_before");
    const bindingAfter = terminal.binding_after;
    const lineage = (bindingAfter && bindingAfter.scene_build_lineage)
      || bindingBefore.scene_build_lineage;
    const instanceDigest = digestJson({
      owner_id: record.owner_id,
      session_id: record.session_id,
      journal_run_id: record.journal_run_id,
      started_at_ms: record.started_at_ms,
    });
    const artifactId = `review-${instanceDigest}`;
    return append({
      kind: "vista-review",
      artifactId,
      revision: "terminal",
      ownerId: record.owner_id,
      sessionId: record.session_id,
      correlationId: `review:${instanceDigest.slice(0, 40)}`,
      idempotencyKey: deterministicIdempotency("vista-review", artifactId, "terminal"),
      expiresAt: expiresAt(new Date(record.started_at_ms).toISOString(), config.retentionDays),
      sourceLineage: lineage ? [{
        kind: lineage.kind,
        artifactId: lineage.artifact_id,
        revision: lineage.revision,
        contentDigest: lineage.content_digest,
      }] : [],
      content: Object.freeze({
        schema: "simworld-review-terminal/v2",
        mode: record.mode,
        input_digest: record.input_digest,
        request_digest: record.request_digest,
        terminal_status: terminal.outcome,
        reason: terminal.reason,
        final_verdict: terminal.final_verdict,
        rounds: terminal.rounds,
        error_code: terminal.error_code,
        provider: terminal.provider,
        model: terminal.model,
        evidence_ids: terminal.evidence_ids,
        evidence_digest: digestJson(terminal.evidence_ids),
        outbox_lookup_digest: record.lookup_digest,
        outbox_terminal_digest: safeDigest(record.terminal_digest, "outbox.terminal_digest"),
        record_digest: safeDigest(record.terminal_digest, "outbox.terminal_digest"),
        scene_binding: Object.freeze({
          before_digest: reviewSceneBindingDigest(bindingBefore),
          after_digest: bindingAfter ? reviewSceneBindingDigest(bindingAfter) : null,
          before_revision: bindingBefore.scene_revision,
          after_revision: bindingAfter ? bindingAfter.scene_revision : null,
          slot_id: bindingBefore.slot_id,
        }),
      }),
    }, appendOptions);
  }

  async function appendReviewAbandonmentRecord(record, abandonment) {
    const bindingBefore = normalizeReviewSceneBinding(record.binding_before, "outbox.binding_before");
    const checkedAbandonment = storedReviewAbandonment(abandonment);
    const abandonmentDigest = reviewAbandonmentDigest(record, checkedAbandonment);
    const instanceDigest = digestJson({
      owner_id: record.owner_id,
      journal_run_id: record.journal_run_id,
      started_at_ms: record.started_at_ms,
    });
    const artifactId = `review-abandonment-${instanceDigest}`;
    const lineage = bindingBefore.scene_build_lineage;
    return append({
      kind: "vista-review-abandonment",
      artifactId,
      revision: "abandoned",
      ownerId: record.owner_id,
      sessionId: record.session_id,
      correlationId: `review-abandonment:${instanceDigest.slice(0, 32)}`,
      idempotencyKey: deterministicIdempotency(
        "vista-review-abandonment",
        artifactId,
        "abandoned",
      ),
      expiresAt: expiresAt(checkedAbandonment.decided_at, config.retentionDays),
      sourceLineage: lineage ? [{
        kind: lineage.kind,
        artifactId: lineage.artifact_id,
        revision: lineage.revision,
        contentDigest: lineage.content_digest,
      }] : [],
      content: Object.freeze({
        schema: REVIEW_ABANDONMENT_SCHEMA,
        decision_id: checkedAbandonment.decision_id,
        operator_id: checkedAbandonment.operator_id,
        reason_code: checkedAbandonment.reason_code,
        evidence_digest: checkedAbandonment.evidence_digest,
        outbox_lookup_digest: record.lookup_digest,
        outbox_abandonment_digest: abandonmentDigest,
        request_digest: record.request_digest,
        input_digest: record.input_digest,
        binding_before_digest: reviewSceneBindingDigest(bindingBefore),
      }),
    });
  }

  async function inspectReviewOutbox() {
    return withReviewOutboxLock(async () => {
      const names = await reviewOutboxNames();
      const records = [];
      const journalIdentities = new Set();
      for (const name of names) {
        const digest = REVIEW_OUTBOX_RECORD_RE.exec(name)[1];
        const record = await readReviewOutboxRecord(digest);
        const journalIdentity = `${record.owner_id}\0${record.journal_run_id}`;
        if (journalIdentities.has(journalIdentity)) {
          throw unavailable("ARTIFACT_JOURNAL_CORRUPT");
        }
        journalIdentities.add(journalIdentity);
        records.push(record);
      }
      return records;
    });
  }

  async function reconcilePendingReviewOutboxRecord(expected) {
    return withReviewOutboxLock(async () => {
      let record = await readReviewOutboxRecord(expected.lookup_digest);
      if (record.owner_id !== expected.owner_id || record.session_id !== expected.session_id
          || record.mode !== expected.mode || record.journal_run_id !== expected.journal_run_id) {
        throw unavailable("ARTIFACT_JOURNAL_CORRUPT");
      }
      if (record.state === "published") return false;
      if (record.state !== "terminal_pending"
          || canonicalJson(storedReviewTerminal(record.terminal))
            !== canonicalJson(storedReviewTerminal(expected.terminal))) {
        throw unavailable("ARTIFACT_JOURNAL_CORRUPT");
      }
      await appendReviewOutboxRecord(record, { allowDuringVerification: true });
      record = await writeReviewOutboxRecord({ ...record, state: "published" });
      return record.state === "published";
    });
  }

  async function append(request, { allowDuringVerification = false } = {}) {
    if (!config.enabled) return Object.freeze({ disabled: true });
    if (!journal || config.configError) throw unavailable();
    if (config.required && !integrityVerified && !allowDuringVerification) {
      throw unavailable("ARTIFACT_JOURNAL_NOT_VERIFIED");
    }
    try {
      const result = await journal.append(request);
      lastFailureCode = null;
      if (result && result.revision
          && Number.isSafeInteger(result.revision.sequence)
          && result.revision.sequence >= maxJournalEntries()) {
        cachedReadiness = capacityReport(result.revision.sequence);
      }
      return result;
    } catch (error) {
      lastFailureCode = error instanceof ArtifactRevisionJournalError
        ? error.code
        : "ARTIFACT_JOURNAL_WRITE_FAILED";
      if (lastFailureCode === "ARTIFACT_JOURNAL_LIMIT_EXCEEDED") {
        cachedReadiness = capacityReport();
      } else {
        revokeIntegrity(lastFailureCode);
      }
      throw unavailable(lastFailureCode, error);
    }
  }

  const recorder = Object.freeze({
    get enabled() { return config.enabled; },
    get required() { return config.required; },

    sceneBuildLineage({ record } = {}) {
      return sceneBuildJournalLineage(record);
    },

    async ensureImportCommitted({ artifact } = {}) {
      if (!config.enabled) return Object.freeze({ disabled: true });
      const identity = importJournalIdentity(artifact);
      const access = normalizeAccess(artifact.access, "artifact.access");
      const createdAt = isoTimestamp(artifact.created_at, "artifact.created_at");
      return append({
        kind: "vista-import",
        artifactId: identity.summary.artifact_id,
        revision: identity.revision,
        ownerId: access.ownerId,
        sessionId: access.sessionId,
        correlationId: correlation("import", identity.summary.artifact_id),
        idempotencyKey: deterministicIdempotency("vista-import", identity.summary.artifact_id, identity.revision),
        expiresAt: expiresAt(createdAt, config.retentionDays),
        sourceLineage: [],
        content: identity.summary,
      });
    },

    async ensureSceneBuildTerminal({ record, importArtifact } = {}) {
      if (!config.enabled) return Object.freeze({ disabled: true });
      const summary = sceneBuildSummary(record);
      const access = normalizeAccess(record.access, "record.access");
      const lineage = sceneBuildJournalLineage(record);
      const operationId = lineage.revision;
      const imported = importJournalIdentity(importArtifact);
      return append({
        kind: "vista-scene-build",
        artifactId: summary.plan_id,
        revision: operationId,
        ownerId: access.ownerId,
        sessionId: access.sessionId,
        correlationId: correlation("scene-build", operationId),
        idempotencyKey: deterministicIdempotency("vista-scene-build", summary.plan_id, operationId),
        expiresAt: expiresAt(record.created_at, config.retentionDays),
        sourceLineage: [{
          kind: "vista-import",
          artifactId: summary.import_artifact_id,
          revision: imported.revision,
          contentDigest: imported.contentDigest,
        }],
        content: summary,
      });
    },

    async ensureTimelineTerminal({ record } = {}) {
      if (!config.enabled) return Object.freeze({ disabled: true });
      const summary = timelineSummary(record);
      const access = normalizeAccess(record.access, "record.access");
      const revision = "terminal";
      const buildDigest = safeDigest(
        record.identity.scene_build_content_digest,
        "record.identity.scene_build_content_digest",
      );
      return append({
        kind: "vista-animation-timeline",
        artifactId: summary.run_id,
        revision,
        ownerId: access.ownerId,
        sessionId: access.sessionId,
        correlationId: correlation("animation", summary.run_id),
        idempotencyKey: deterministicIdempotency("vista-animation-timeline", summary.run_id, revision),
        expiresAt: expiresAt(record.created_at, config.retentionDays),
        sourceLineage: [{
          kind: "vista-scene-build",
          artifactId: summary.plan_id,
          revision: summary.scene_build_operation_id,
          contentDigest: buildDigest,
        }],
        content: summary,
      });
    },

    async prepareReviewTerminal({ scope, run, mode, binding, requestDigest, inputDigest } = {}) {
      if (!config.enabled) return Object.freeze({ disabled: true });
      if (!scope || typeof scope !== "object" || !run || typeof run !== "object") {
        throw new TypeError("Review scope and run are required");
      }
      if (!journal || config.configError) throw unavailable();
      if (config.required && !integrityVerified) {
        throw unavailable("ARTIFACT_JOURNAL_NOT_VERIFIED");
      }
      const checkedMode = String(mode || "").trim().toLowerCase();
      if (!REVIEW_MODES.has(checkedMode)) throw new TypeError("Review mode is invalid");
      const bindingBefore = normalizeReviewSceneBinding(binding, "binding");
      const checkedInputDigest = safeDigest(inputDigest, "inputDigest");
      const checkedRequestDigest = safeDigest(requestDigest, "requestDigest");
      if (bindingBefore.scope_id !== scope.scopeId) {
        throw new TypeError("Review scene binding does not match the Review scope");
      }
      const publicRunId = safeLabel(run.runId, "run.runId");
      const startedAtMs = Number(run.startedAt);
      if (!Number.isSafeInteger(startedAtMs) || startedAtMs < 0
          || !Number.isFinite(new Date(startedAtMs).getTime())) {
        throw new TypeError("Review start time is invalid");
      }
      const accessSource = scope.journalAccess || scope.activeLease;
      if (!accessSource) throw new TypeError("Review journal authority is missing");
      const access = normalizeAccess(accessSource, "scope.journalAccess");
      const lookupDigest = digestJson([
        "simworld-review-terminal-outbox-id/v1",
        access.ownerId,
        publicRunId,
      ]);
      try {
        return await withReviewOutboxLock(async () => {
          const prior = await readReviewOutboxRecord(lookupDigest, { allowMissing: true });
          if (prior) {
            if (prior.owner_id !== access.ownerId || prior.mode !== checkedMode
                || prior.request_digest !== checkedRequestDigest) {
              throw unavailable("ARTIFACT_IDEMPOTENCY_CONFLICT");
            }
            if (prior.state === "abandoned") {
              throw unavailable("ARTIFACT_REVIEW_RUN_ABANDONED");
            }
            const expectedBinding = prior.state === "prepared"
              ? prior.binding_before
              : (prior.terminal.binding_after || prior.binding_before);
            if (!sameReviewSceneBinding(bindingBefore, expectedBinding)) {
              throw unavailable("ARTIFACT_IDEMPOTENCY_CONFLICT");
            }
            return reviewOutboxTicket(prior, false);
          }
          const names = await reviewOutboxNames();
          if (names.length >= maxReviewOutboxRecords) {
            throw unavailable("ARTIFACT_REVIEW_OUTBOX_CAPACITY_EXHAUSTED");
          }
          let ownerRecordCount = 0;
          for (const name of names) {
            const digest = REVIEW_OUTBOX_RECORD_RE.exec(name)[1];
            const existing = await readReviewOutboxRecord(digest);
            if (existing.owner_id === access.ownerId
                && REVIEW_ACTIVE_OUTBOX_STATES.has(existing.state)) {
              ownerRecordCount += 1;
            }
          }
          if (ownerRecordCount >= maxReviewOutboxRecordsPerOwner) {
            throw unavailable("ARTIFACT_REVIEW_OWNER_QUOTA_EXHAUSTED");
          }
          const record = {
            schema: REVIEW_OUTBOX_SCHEMA,
            lookup_digest: lookupDigest,
            owner_id: access.ownerId,
            session_id: access.sessionId,
            mode: checkedMode,
            input_digest: checkedInputDigest,
            request_digest: checkedRequestDigest,
            journal_run_id: `review-journal-${randomHex(24)}`,
            started_at_ms: startedAtMs,
            state: "prepared",
            terminal: null,
            binding_before: bindingBefore,
            terminal_digest: null,
            abandonment: null,
            abandonment_digest: null,
            created_at: clockTimestamp(),
          };
          const written = await writeReviewOutboxRecord(record, { create: true });
          if (names.length + 1 >= maxReviewOutboxRecords) {
            lastFailureCode = "ARTIFACT_REVIEW_OUTBOX_CAPACITY_EXHAUSTED";
            cachedReadiness = reviewOutboxCapacityReport(names.length + 1);
          }
          return reviewOutboxTicket(written, true);
        });
      } catch (error) {
        if (error instanceof ArtifactJournalRuntimeError
            && error.code === "ARTIFACT_REVIEW_OUTBOX_CAPACITY_EXHAUSTED") {
          lastFailureCode = error.code;
          cachedReadiness = reviewOutboxCapacityReport(maxReviewOutboxRecords);
        } else if (!(error instanceof ArtifactJournalRuntimeError)
            || ![
              "ARTIFACT_JOURNAL_BUSY",
              "ARTIFACT_IDEMPOTENCY_CONFLICT",
              "ARTIFACT_REVIEW_OWNER_QUOTA_EXHAUSTED",
              "ARTIFACT_REVIEW_RUN_ABANDONED",
            ].includes(error.code)) {
          revokeIntegrity(error && error.code || "ARTIFACT_JOURNAL_WRITE_FAILED");
        }
        throw error;
      }
    },

    async abandonPreparedReview({ identity, authorizationToken, decision } = {}) {
      if (!config.enabled) return Object.freeze({ disabled: true });
      if (!journal || config.configError) throw unavailable();
      if (config.required && !integrityVerified) {
        throw unavailable("ARTIFACT_JOURNAL_NOT_VERIFIED");
      }
      const operatorId = authorizeReviewAbandonment(authorizationToken);
      const checkedIdentity = validateReviewAbandonmentIdentity(identity);
      try {
        return await withReviewOutboxLock(async () => {
          let record = await readReviewOutboxRecord(checkedIdentity.lookupDigest);
          assertAbandonmentIdentityMatchesRecord(checkedIdentity, record);
          const abandonment = normalizeReviewAbandonmentDecision(
            decision,
            operatorId,
            record.started_at_ms,
            clockTimestamp(),
          );
          const abandonmentDigest = reviewAbandonmentDigest(record, abandonment);
          if (record.state === "abandoned") {
            if (record.abandonment_digest !== abandonmentDigest
                || canonicalJson(record.abandonment) !== canonicalJson(abandonment)) {
              throw unavailable("ARTIFACT_IDEMPOTENCY_CONFLICT");
            }
            return Object.freeze({
              state: record.state,
              lookupDigest: record.lookup_digest,
              journalRunId: record.journal_run_id,
              abandonmentDigest: record.abandonment_digest,
              idempotent: true,
            });
          }
          if (record.state !== "prepared") {
            throw unavailable("ARTIFACT_REVIEW_ABANDONMENT_CONFLICT");
          }
          const audit = await appendReviewAbandonmentRecord(record, abandonment);
          record = await writeReviewOutboxRecord({
            ...record,
            state: "abandoned",
            abandonment,
            abandonment_digest: abandonmentDigest,
          });
          cachedReadiness = null;
          return Object.freeze({
            state: record.state,
            lookupDigest: record.lookup_digest,
            journalRunId: record.journal_run_id,
            abandonmentDigest: record.abandonment_digest,
            idempotent: false,
            audit,
          });
        });
      } catch (error) {
        if (error instanceof ArtifactJournalRuntimeError
            && [
              "ARTIFACT_JOURNAL_BUSY",
              "ARTIFACT_IDEMPOTENCY_CONFLICT",
              "ARTIFACT_REVIEW_ABANDONMENT_CONFLICT",
            ].includes(error.code)) {
          throw error;
        }
        revokeIntegrity(error && error.code || "ARTIFACT_JOURNAL_WRITE_FAILED");
        throw error;
      }
    },

    async ensureReviewTerminal({ ticket, terminal } = {}) {
      if (!config.enabled) return Object.freeze({ disabled: true });
      if (!journal || config.configError) throw unavailable();
      if (config.required && !integrityVerified) {
        throw unavailable("ARTIFACT_JOURNAL_NOT_VERIFIED");
      }
      const checkedTicket = validateReviewOutboxTicket(ticket);
      const normalized = normalizeReviewTerminal(terminal);
      assertReviewBindingContinuity(checkedTicket.bindingBefore, normalized.binding_after);
      try {
        return await withReviewOutboxLock(async () => {
          let record = await readReviewOutboxRecord(checkedTicket.lookupDigest);
          assertTicketMatchesRecord(checkedTicket, record);
          if (record.state === "abandoned") {
            throw unavailable("ARTIFACT_REVIEW_RUN_ABANDONED");
          }
          if (record.state === "prepared") {
            const pendingRecord = {
              ...record,
              state: "terminal_pending",
              terminal: normalized,
            };
            pendingRecord.terminal_digest = reviewTerminalDigest(pendingRecord);
            record = await writeReviewOutboxRecord(pendingRecord);
          } else if (canonicalJson(storedReviewTerminal(record.terminal)) !== canonicalJson(normalized)) {
            throw unavailable("ARTIFACT_IDEMPOTENCY_CONFLICT");
          }
          const appended = await appendReviewOutboxRecord(record);
          if (record.state !== "published") {
            record = await writeReviewOutboxRecord({ ...record, state: "published" });
          }
          return Object.freeze({
            ...appended,
            outbox: Object.freeze({ state: record.state, lookupDigest: record.lookup_digest }),
          });
        });
      } catch (error) {
        if (error instanceof ArtifactJournalRuntimeError
            && error.code === "ARTIFACT_JOURNAL_LIMIT_EXCEEDED") {
          lastFailureCode = error.code;
          if (!cachedReadiness) cachedReadiness = capacityReport();
        } else if (!(error instanceof ArtifactJournalRuntimeError)
            || ![
              "ARTIFACT_JOURNAL_BUSY",
              "ARTIFACT_IDEMPOTENCY_CONFLICT",
              "ARTIFACT_REVIEW_RUN_ABANDONED",
            ].includes(error.code)) {
          revokeIntegrity(error && error.code || "ARTIFACT_JOURNAL_WRITE_FAILED");
        }
        throw error;
      }
    },
  });

  const abortedReadinessReport = () => ({
    status: "not_ready",
    revision: { schema: RUNTIME_SCHEMA, enabled: true, verification: "aborted" },
    causes: [{
      code: "ARTIFACT_JOURNAL_CHECK_ABORTED",
      message: "The artifact journal integrity check was cancelled.",
      retryable: true,
      dependency: "artifact_journal",
    }],
  });

  async function performFullVerification() {
    const verificationEpoch = failureEpoch;
    try {
      const outboxBefore = await inspectReviewOutbox();
      lastIntegrity = await journal.verifyIntegrity();
      const pendingReviews = outboxBefore.filter((record) => record.state === "terminal_pending");
      for (const record of pendingReviews) {
        await reconcilePendingReviewOutboxRecord(record);
      }
      if (pendingReviews.length > 0) lastIntegrity = await journal.verifyIntegrity();
      const outboxAfter = pendingReviews.length > 0 ? await inspectReviewOutbox() : outboxBefore;
      const outboxCounts = { prepared: 0, terminal_pending: 0, published: 0, abandoned: 0 };
      for (const record of outboxAfter) outboxCounts[record.state] += 1;
      if (failureEpoch !== verificationEpoch) {
        throw unavailable(lastFailureCode || "ARTIFACT_JOURNAL_INTEGRITY_FAILED");
      }
      integrityVerified = true;
      if (lastIntegrity.entry_count >= maxJournalEntries()) {
        lastFailureCode = "ARTIFACT_JOURNAL_CAPACITY_EXHAUSTED";
        cachedReadiness = capacityReport(lastIntegrity.entry_count);
      } else if (outboxAfter.length >= maxReviewOutboxRecords) {
        lastFailureCode = "ARTIFACT_REVIEW_OUTBOX_CAPACITY_EXHAUSTED";
        cachedReadiness = reviewOutboxCapacityReport(outboxAfter.length);
      } else {
        lastFailureCode = null;
        cachedReadiness = {
          status: "ready",
          revision: {
            schema: RUNTIME_SCHEMA,
            enabled: true,
            verification: "full_hash_chain",
            entry_count: lastIntegrity.entry_count,
            revision_count: lastIntegrity.revision_count,
            retained_revision_count: lastIntegrity.retained_revision_count,
            orphan_blob_count: lastIntegrity.orphan_blob_count,
            head_digest: lastIntegrity.head_digest,
            review_outbox: outboxCounts,
          },
          causes: [],
        };
      }
    } catch (error) {
      lastFailureCode = error instanceof ArtifactRevisionJournalError
        || error instanceof ArtifactJournalRuntimeError
        ? error.code
        : "ARTIFACT_JOURNAL_INTEGRITY_FAILED";
      integrityVerified = false;
      failureEpoch += 1;
      cachedReadiness = {
        status: "not_ready",
        revision: { schema: RUNTIME_SCHEMA, enabled: true, verification: "failed" },
        causes: [{
          code: lastFailureCode,
          message: "The artifact journal integrity chain could not be verified.",
          retryable: Boolean(error && error.retryable),
          dependency: "artifact_journal",
        }],
      };
    }
    return cachedReadiness;
  }

  async function awaitVerification(promise, signal) {
    if (!signal || typeof signal.addEventListener !== "function") return promise;
    if (signal.aborted) return abortedReadinessReport();
    return new Promise((resolve) => {
      let settled = false;
      const finish = (report) => {
        if (settled) return;
        settled = true;
        signal.removeEventListener("abort", onAbort);
        resolve(report);
      };
      const onAbort = () => finish(abortedReadinessReport());
      signal.addEventListener("abort", onAbort, { once: true });
      if (signal.aborted) onAbort();
      Promise.resolve(promise).then(finish, () => finish({
        status: "not_ready",
        revision: { schema: RUNTIME_SCHEMA, enabled: true, verification: "failed" },
        causes: [{
          code: "ARTIFACT_JOURNAL_INTEGRITY_FAILED",
          message: "The artifact journal integrity chain could not be verified.",
          retryable: false,
          dependency: "artifact_journal",
        }],
      }));
    });
  }

  async function readinessProbe({ signal, force = false } = {}) {
    if (!config.enabled) {
      return {
        status: config.required ? "not_ready" : "ready",
        revision: { schema: RUNTIME_SCHEMA, enabled: false, verification: "disabled" },
        causes: config.required ? [{
          code: "ARTIFACT_JOURNAL_REQUIRED",
          message: "Production artifact journaling is required.",
          retryable: false,
          dependency: "artifact_journal",
        }] : [],
      };
    }
    if (config.configError || !journal || !config.root) {
      return {
        status: "not_ready",
        revision: { schema: RUNTIME_SCHEMA, enabled: true, verification: "unavailable" },
        causes: [{
          code: config.configError || "ARTIFACT_JOURNAL_CONFIG_INVALID",
          message: "The artifact journal production configuration is invalid.",
          retryable: false,
          dependency: "artifact_journal",
        }],
      };
    }
    if (signal && signal.aborted) return abortedReadinessReport();
    try {
      const stat = await fs.lstat(config.root);
      const resolved = await fs.realpath(config.root);
      const currentUid = typeof process.getuid === "function" ? process.getuid() : null;
      if (!stat.isDirectory() || stat.isSymbolicLink() || (stat.mode & 0o777) !== 0o700
          || resolved !== config.root || (currentUid !== null && stat.uid !== currentUid)) {
        throw unavailable("ARTIFACT_JOURNAL_ROOT_INSECURE");
      }
      if (cachedReadiness) await ensureSecureReviewOutboxRoot({ create: false });
    } catch (error) {
      const code = error && error.code === "ENOENT"
        ? "ARTIFACT_JOURNAL_ROOT_MISSING"
        : (error && error.code) || "ARTIFACT_JOURNAL_ROOT_INSECURE";
      lastFailureCode = code;
      revokeIntegrity(code);
      return {
        status: "not_ready",
        revision: { schema: RUNTIME_SCHEMA, enabled: true, verification: "unavailable" },
        causes: [{
          code,
          message: "The artifact journal root is unavailable or insecure.",
          retryable: code === "ARTIFACT_JOURNAL_ROOT_MISSING",
          dependency: "artifact_journal",
        }],
      };
    }
    if (signal && signal.aborted) return abortedReadinessReport();
    if (cachedReadiness && force !== true) return cachedReadiness;
    if (!verificationPromise) {
      const activeVerification = performFullVerification();
      const wrappedVerification = activeVerification.finally(() => {
        if (verificationPromise === wrappedVerification) verificationPromise = null;
      });
      verificationPromise = wrappedVerification;
    }
    return awaitVerification(verificationPromise, signal);
  }

  return Object.freeze({ config, journal, recorder, readinessProbe });
}

module.exports = {
  ArtifactJournalRuntimeError,
  MAX_RETENTION_DAYS,
  RUNTIME_SCHEMA,
  createArtifactJournalRuntime,
  digestJson,
  resolveArtifactJournalConfig,
};

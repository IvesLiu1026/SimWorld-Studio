"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const REVIEW_SMOKE_RECEIPT_SCHEMA = "simworld-review-smoke-receipt/v1";
const REVIEW_TYPES = new Set(["text", "visual"]);
const VERDICT_STATUSES = new Set(["PASS", "NEEDS_IMPROVEMENT", "FAIL"]);
const SAFE_TOKEN = /^[A-Za-z0-9][A-Za-z0-9._:@/+~-]{0,239}$/;
const SHA256 = /^[a-f0-9]{64}$/;
const MAX_RECEIPT_BYTES = 64 * 1024;
const MAX_RECEIPT_TTL_MS = 24 * 60 * 60 * 1000;
const MAX_CLOCK_SKEW_MS = 5 * 60 * 1000;
const SENSITIVE_KEY = /(?:^|_)(?:api_?key|access_?token|auth|authorization|bearer|cookie|credential|password|private_?key|secret)(?:$|_)/i;
const SENSITIVE_VALUE = /(?:\bBearer\s+[A-Za-z0-9._~+/-]+=*|\b(?:sk-ant-|sk-proj-|gh[oprsu]_|xox[baprs]-)[A-Za-z0-9_-]+|-----BEGIN [A-Z ]*PRIVATE KEY-----|(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?):\/\/[^\s/@:]+:[^\s/@]+@)/i;

class ReviewSmokeReceiptError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = "ReviewSmokeReceiptError";
    this.code = code;
    this.retryable = details.retryable === true;
    this.details = Object.freeze(details.field ? { field: details.field } : {});
  }
}

function fail(code, message, field) {
  throw new ReviewSmokeReceiptError(code, message, field ? { field } : {});
}

function assertObject(value, field, allowed, required = allowed) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    fail("REVIEW_SMOKE_RECEIPT_INVALID", `${field} must be an object`, field);
  }
  const keys = Object.keys(value);
  const allowedSet = new Set(allowed);
  const unknown = keys.find((key) => !allowedSet.has(key));
  if (unknown) {
    const code = SENSITIVE_KEY.test(unknown)
      ? "REVIEW_SMOKE_RECEIPT_SENSITIVE"
      : "REVIEW_SMOKE_RECEIPT_INVALID";
    fail(code, `${field} contains a forbidden field`, `${field}.${unknown}`);
  }
  const missing = required.find((key) => !Object.prototype.hasOwnProperty.call(value, key));
  if (missing) fail("REVIEW_SMOKE_RECEIPT_INVALID", `${field} is missing a required field`, `${field}.${missing}`);
  return value;
}

function safeToken(value, field, max = 240) {
  if (typeof value !== "string" || value.length > max || !SAFE_TOKEN.test(value)) {
    fail("REVIEW_SMOKE_RECEIPT_INVALID", `${field} is invalid`, field);
  }
  if (SENSITIVE_VALUE.test(value)) {
    fail("REVIEW_SMOKE_RECEIPT_SENSITIVE", `${field} contains credential-like material`, field);
  }
  return value;
}

function timestamp(value, field) {
  if (typeof value !== "string" || value.length > 40) {
    fail("REVIEW_SMOKE_RECEIPT_INVALID", `${field} must be an ISO-8601 timestamp`, field);
  }
  const milliseconds = Date.parse(value);
  if (!Number.isFinite(milliseconds) || new Date(milliseconds).toISOString() !== value) {
    fail("REVIEW_SMOKE_RECEIPT_INVALID", `${field} must be a canonical ISO-8601 timestamp`, field);
  }
  return milliseconds;
}

function nonnegativeInteger(value, field) {
  if (!Number.isSafeInteger(value) || value < 0 || value > 1_000_000_000) {
    fail("REVIEW_SMOKE_RECEIPT_INVALID", `${field} must be a bounded non-negative integer`, field);
  }
  return value;
}

function normalizeUsage(usage) {
  assertObject(
    usage,
    "usage",
    ["input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "cost_usd"],
    ["input_tokens", "output_tokens", "cost_usd"],
  );
  const normalized = {
    input_tokens: nonnegativeInteger(usage.input_tokens, "usage.input_tokens"),
    output_tokens: nonnegativeInteger(usage.output_tokens, "usage.output_tokens"),
  };
  for (const field of ["cache_creation_input_tokens", "cache_read_input_tokens"]) {
    if (usage[field] !== undefined) normalized[field] = nonnegativeInteger(usage[field], `usage.${field}`);
  }
  if (usage.cost_usd !== null && (
    typeof usage.cost_usd !== "number"
    || !Number.isFinite(usage.cost_usd)
    || usage.cost_usd < 0
    || usage.cost_usd > 10
  )) {
    fail("REVIEW_SMOKE_RECEIPT_INVALID", "usage.cost_usd must be null or a bounded number", "usage.cost_usd");
  }
  normalized.cost_usd = usage.cost_usd === null
    ? null
    : Math.round(usage.cost_usd * 1e6) / 1e6;
  return normalized;
}

function normalizeStoredVerdict(verdict) {
  assertObject(verdict, "verdict", ["status", "issues_count", "suggestions_count", "schema_valid"]);
  if (!VERDICT_STATUSES.has(verdict.status)) {
    fail("REVIEW_SMOKE_RECEIPT_INVALID", "verdict.status is invalid", "verdict.status");
  }
  if (verdict.schema_valid !== true) {
    fail("REVIEW_SMOKE_RECEIPT_INVALID", "verdict.schema_valid must be true", "verdict.schema_valid");
  }
  return {
    status: verdict.status,
    issues_count: nonnegativeInteger(verdict.issues_count, "verdict.issues_count"),
    suggestions_count: nonnegativeInteger(verdict.suggestions_count, "verdict.suggestions_count"),
    schema_valid: true,
  };
}

function validateReviewSmokeReceipt(receipt) {
  assertObject(receipt, "receipt", [
    "schema",
    "receipt_id",
    "outcome",
    "review_type",
    "provider",
    "model",
    "cli",
    "source_revision",
    "recorded_at",
    "expires_at",
    "scene",
    "usage",
    "verdict",
  ]);
  if (receipt.schema !== REVIEW_SMOKE_RECEIPT_SCHEMA) {
    fail("REVIEW_SMOKE_RECEIPT_INVALID", "Unsupported review smoke receipt schema", "schema");
  }
  safeToken(receipt.receipt_id, "receipt_id", 160);
  if (receipt.outcome !== "success") {
    fail("REVIEW_SMOKE_RECEIPT_INVALID", "Receipt outcome must be success", "outcome");
  }
  if (!REVIEW_TYPES.has(receipt.review_type)) {
    fail("REVIEW_SMOKE_RECEIPT_INVALID", "review_type must be text or visual", "review_type");
  }
  safeToken(receipt.provider, "provider");
  safeToken(receipt.model, "model");
  assertObject(receipt.cli, "cli", ["name", "version"]);
  safeToken(receipt.cli.name, "cli.name");
  safeToken(receipt.cli.version, "cli.version");
  safeToken(receipt.source_revision, "source_revision");

  const recordedAt = timestamp(receipt.recorded_at, "recorded_at");
  const expiresAt = timestamp(receipt.expires_at, "expires_at");
  if (expiresAt <= recordedAt || expiresAt - recordedAt > MAX_RECEIPT_TTL_MS) {
    fail("REVIEW_SMOKE_RECEIPT_INVALID", "Receipt expiry must be after recording and no more than 24 hours later", "expires_at");
  }

  assertObject(receipt.scene, "scene", ["read_only", "digest_algorithm", "digest_before", "digest_after"]);
  if (receipt.scene.read_only !== true || receipt.scene.digest_algorithm !== "sha256") {
    fail("REVIEW_SMOKE_RECEIPT_INVALID", "Scene evidence must declare read-only SHA-256 digests", "scene");
  }
  for (const field of ["digest_before", "digest_after"]) {
    if (typeof receipt.scene[field] !== "string" || !SHA256.test(receipt.scene[field])) {
      fail("REVIEW_SMOKE_RECEIPT_INVALID", `scene.${field} must be a SHA-256 digest`, `scene.${field}`);
    }
  }
  if (receipt.review_type === "visual" && receipt.scene.digest_before !== receipt.scene.digest_after) {
    fail("REVIEW_SMOKE_SCENE_MUTATED", "Visual review changed the scene digest", "scene.digest_after");
  }

  normalizeUsage(receipt.usage);
  normalizeStoredVerdict(receipt.verdict);
  return receipt;
}

function compactProviderVerdict(verdict) {
  if (!verdict || typeof verdict !== "object" || Array.isArray(verdict)) {
    fail("REVIEW_SMOKE_RECEIPT_INVALID", "Provider verdict must be an object", "verdict");
  }
  if (!VERDICT_STATUSES.has(verdict.status)) {
    fail("REVIEW_SMOKE_RECEIPT_INVALID", "Provider verdict status is invalid", "verdict.status");
  }
  if (!Array.isArray(verdict.issues) || !Array.isArray(verdict.suggestions)) {
    fail("REVIEW_SMOKE_RECEIPT_INVALID", "Provider verdict arrays are required", "verdict");
  }
  return {
    status: verdict.status,
    issues_count: nonnegativeInteger(verdict.issues.length, "verdict.issues_count"),
    suggestions_count: nonnegativeInteger(verdict.suggestions.length, "verdict.suggestions_count"),
    schema_valid: true,
  };
}

function createReviewSmokeReceipt(input = {}) {
  assertObject(input, "input", [
    "receiptId",
    "reviewType",
    "provider",
    "model",
    "cliName",
    "cliVersion",
    "sourceRevision",
    "recordedAt",
    "expiresAt",
    "sceneDigestBefore",
    "sceneDigestAfter",
    "usage",
    "verdict",
  ]);
  const receipt = {
    schema: REVIEW_SMOKE_RECEIPT_SCHEMA,
    receipt_id: input.receiptId,
    outcome: "success",
    review_type: input.reviewType,
    provider: input.provider,
    model: input.model,
    cli: { name: input.cliName, version: input.cliVersion },
    source_revision: input.sourceRevision,
    recorded_at: input.recordedAt,
    expires_at: input.expiresAt,
    scene: {
      read_only: true,
      digest_algorithm: "sha256",
      digest_before: input.sceneDigestBefore,
      digest_after: input.sceneDigestAfter,
    },
    usage: normalizeUsage(input.usage),
    // Persist only verdict status and counts. Provider prose, evidence paths,
    // and raw notes are deliberately excluded from the readiness artifact.
    verdict: compactProviderVerdict(input.verdict),
  };
  validateReviewSmokeReceipt(receipt);
  return Object.freeze(receipt);
}

function canonicalJson(value, seen = new Set()) {
  if (value === null) return "null";
  if (typeof value === "string" || typeof value === "boolean") return JSON.stringify(value);
  if (typeof value === "number") {
    if (!Number.isFinite(value)) fail("REVIEW_SMOKE_SCENE_INVALID", "Scene snapshot contains a non-finite number");
    return JSON.stringify(value);
  }
  if (typeof value !== "object") fail("REVIEW_SMOKE_SCENE_INVALID", "Scene snapshot is not JSON serializable");
  if (seen.has(value)) fail("REVIEW_SMOKE_SCENE_INVALID", "Scene snapshot contains a cycle");
  seen.add(value);
  let result;
  if (Array.isArray(value)) {
    result = `[${value.map((item) => canonicalJson(item, seen)).join(",")}]`;
  } else {
    const keys = Object.keys(value).sort();
    result = `{${keys.map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key], seen)}`).join(",")}}`;
  }
  seen.delete(value);
  return result;
}

function digestReviewScene(sceneSnapshot) {
  const serialized = canonicalJson(sceneSnapshot);
  if (Buffer.byteLength(serialized) > 16 * 1024 * 1024) {
    fail("REVIEW_SMOKE_SCENE_INVALID", "Scene snapshot exceeds the digest size limit");
  }
  return crypto.createHash("sha256").update(serialized).digest("hex");
}

function writeReviewSmokeReceiptAtomic(file, receipt, { fsImpl = fs } = {}) {
  validateReviewSmokeReceipt(receipt);
  if (typeof file !== "string" || !file.trim() || file.includes("\0")) {
    fail("REVIEW_SMOKE_RECEIPT_PATH_INVALID", "Receipt path is invalid");
  }
  const target = path.resolve(file);
  const directory = path.dirname(target);
  fsImpl.mkdirSync(directory, { recursive: true, mode: 0o700 });
  const temporary = path.join(
    directory,
    `.${path.basename(target)}.${process.pid}.${crypto.randomBytes(8).toString("hex")}.tmp`,
  );
  let descriptor;
  try {
    descriptor = fsImpl.openSync(temporary, "wx", 0o600);
    fsImpl.writeFileSync(descriptor, `${JSON.stringify(receipt, null, 2)}\n`, "utf8");
    if (typeof fsImpl.fsyncSync === "function") fsImpl.fsyncSync(descriptor);
    fsImpl.closeSync(descriptor);
    descriptor = undefined;
    fsImpl.renameSync(temporary, target);
    if (typeof fsImpl.chmodSync === "function") fsImpl.chmodSync(target, 0o600);
    return target;
  } catch (cause) {
    try { if (descriptor !== undefined) fsImpl.closeSync(descriptor); } catch (_error) {}
    try { fsImpl.rmSync(temporary, { force: true }); } catch (_error) {}
    if (cause instanceof ReviewSmokeReceiptError) throw cause;
    throw new ReviewSmokeReceiptError(
      "REVIEW_SMOKE_RECEIPT_WRITE_FAILED",
      "Review smoke receipt could not be saved atomically",
      { retryable: true },
    );
  }
}

function readReviewSmokeReceipt(file, { fsImpl = fs, expectedSha256 = null } = {}) {
  let bytes;
  let descriptor;
  try {
    descriptor = fsImpl.openSync(file, fs.constants.O_RDONLY | Number(fs.constants.O_NOFOLLOW || 0));
    const before = fsImpl.fstatSync(descriptor);
    if (!before.isFile() || before.size < 2 || before.size > MAX_RECEIPT_BYTES) {
      fail("REVIEW_SMOKE_RECEIPT_INVALID", "Review smoke receipt has an invalid size");
    }
    bytes = fsImpl.readFileSync(descriptor);
    const after = fsImpl.fstatSync(descriptor);
    if (after.size !== before.size || after.mtimeMs !== before.mtimeMs || after.ctimeMs !== before.ctimeMs) {
      fail("REVIEW_SMOKE_RECEIPT_CHANGED", "Review smoke receipt changed while it was being read");
    }
  } catch (_cause) {
    if (_cause instanceof ReviewSmokeReceiptError) throw _cause;
    throw new ReviewSmokeReceiptError(
      "REVIEW_SMOKE_RECEIPT_UNAVAILABLE",
      "Review smoke receipt is unavailable",
      { retryable: true },
    );
  } finally {
    try { if (descriptor !== undefined) fsImpl.closeSync(descriptor); } catch (_error) {}
  }
  if (!Buffer.isBuffer(bytes)) bytes = Buffer.from(String(bytes));
  if (bytes.length < 2 || bytes.length > MAX_RECEIPT_BYTES) {
    fail("REVIEW_SMOKE_RECEIPT_INVALID", "Review smoke receipt has an invalid size");
  }
  if (expectedSha256 !== null) {
    if (typeof expectedSha256 !== "string" || !SHA256.test(expectedSha256)) {
      fail("REVIEW_SMOKE_RECEIPT_PIN_INVALID", "Review smoke receipt SHA-256 pin is invalid");
    }
    if (digestReviewSmokeReceiptBytes(bytes) !== expectedSha256) {
      fail("REVIEW_SMOKE_RECEIPT_DIGEST_MISMATCH", "Review smoke receipt does not match its SHA-256 pin");
    }
  }
  let receipt;
  try {
    receipt = JSON.parse(bytes.toString("utf8"));
  } catch (_cause) {
    fail("REVIEW_SMOKE_RECEIPT_INVALID", "Review smoke receipt is not valid JSON");
  }
  return validateReviewSmokeReceipt(receipt);
}

function digestReviewSmokeReceiptBytes(bytes) {
  const value = Buffer.isBuffer(bytes) ? bytes : Buffer.from(String(bytes), "utf8");
  if (value.length < 2 || value.length > MAX_RECEIPT_BYTES) {
    fail("REVIEW_SMOKE_RECEIPT_INVALID", "Review smoke receipt has an invalid size");
  }
  return crypto.createHash("sha256").update(value).digest("hex");
}

function verifyReviewSmokeReceipt(receipt, {
  provider,
  model,
  sourceRevision,
  reviewType,
  now = Date.now(),
} = {}) {
  validateReviewSmokeReceipt(receipt);
  if (receipt.verdict.status !== "PASS") {
    fail("REVIEW_SMOKE_VERDICT_FAILED", "Review provider smoke verdict did not pass", "verdict.status");
  }
  const nowMs = typeof now === "function" ? Number(now()) : Number(now);
  if (!Number.isFinite(nowMs)) fail("REVIEW_SMOKE_RECEIPT_INVALID", "Verification time is invalid");
  const recordedAt = Date.parse(receipt.recorded_at);
  const expiresAt = Date.parse(receipt.expires_at);
  if (recordedAt > nowMs + MAX_CLOCK_SKEW_MS) {
    fail("REVIEW_SMOKE_RECEIPT_NOT_YET_VALID", "Review smoke receipt timestamp is in the future");
  }
  if (expiresAt <= nowMs) {
    fail("REVIEW_SMOKE_RECEIPT_EXPIRED", "Review smoke receipt has expired");
  }
  const expectations = [
    ["provider", provider],
    ["model", model],
    ["source_revision", sourceRevision],
    ["review_type", reviewType],
  ];
  for (const [field, expected] of expectations) {
    if (expected !== undefined && expected !== null && expected !== "" && receipt[field] !== expected) {
      fail("REVIEW_SMOKE_RECEIPT_MISMATCH", `Review smoke receipt ${field} does not match the running service`, field);
    }
  }
  return receipt;
}

function resolveReviewSmokeReceiptPath(env = process.env) {
  if (env.REVIEW_SMOKE_RECEIPT_PATH) return path.resolve(String(env.REVIEW_SMOKE_RECEIPT_PATH));
  const dataRoot = env.XDG_DATA_HOME
    || (env.HOME ? path.join(env.HOME, ".local", "share") : path.resolve(__dirname, "..", ".runtime"));
  return path.join(dataRoot, "simworld-studio", "review-smoke-receipt.json");
}

module.exports = {
  MAX_RECEIPT_TTL_MS,
  REVIEW_SMOKE_RECEIPT_SCHEMA,
  ReviewSmokeReceiptError,
  createReviewSmokeReceipt,
  digestReviewSmokeReceiptBytes,
  digestReviewScene,
  readReviewSmokeReceipt,
  resolveReviewSmokeReceiptPath,
  validateReviewSmokeReceipt,
  verifyReviewSmokeReceipt,
  writeReviewSmokeReceiptAtomic,
};

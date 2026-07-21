#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const crypto = require("node:crypto");
const {
  ReviewProviderSmokeError,
  createReviewProviderSmokeRunner,
} = require("./review-provider-smoke");

const VALUE_FLAGS = new Set([
  "review-type",
  "provider",
  "model",
  "build-revision",
  "scene-digest-before",
  "scene-digest-after",
  "prompt-file",
  "image",
  "receipt",
  "receipt-id",
  "receipt-ttl-seconds",
  "cli-name",
  "cli-version",
  "max-budget-usd",
  "timeout-ms",
  "max-input-tokens",
  "max-output-tokens",
]);
const REPEATABLE_FLAGS = new Set(["image"]);
const MAX_PROMPT_FILE_BYTES = 16 * 1024;
const MAX_IMAGE_FILE_BYTES = 8 * 1024 * 1024;

const USAGE = `Usage:
  node server/review-provider-smoke-cli.js \\
    --review-type visual \\
    --provider claude \\
    --model claude-opus-4-8 \\
    --build-revision <40-or-64-character-git-object-id> \\
    --scene-digest-before <sha256> \\
    --scene-digest-after <sha256> \\
    --prompt-file <review-request.txt> \\
    --image <evidence.png> \\
    --cli-name claude-code \\
    --cli-version <version> \\
    --max-budget-usd 0.10 \\
    --timeout-ms 120000 \\
    --max-input-tokens 50000 \\
    --max-output-tokens 2048 \\
    --receipt <review-smoke-receipt.json>

Provider credentials are read only by the provider adapter from its allowlisted
environment. Do not pass credentials on this command line.`;

function cliError(code, message) {
  const error = new Error(message);
  error.code = code;
  return error;
}

function parseArgv(argv) {
  if (!Array.isArray(argv)) throw cliError("REVIEW_SMOKE_CLI_INVALID", "CLI arguments are invalid");
  if (argv.includes("--help") || argv.includes("-h")) return { help: true };
  const values = {};
  for (let index = 0; index < argv.length; index += 2) {
    const rawFlag = argv[index];
    const rawValue = argv[index + 1];
    if (typeof rawFlag !== "string" || !rawFlag.startsWith("--") || rawFlag.includes("=")) {
      throw cliError("REVIEW_SMOKE_CLI_INVALID", "Use separate --flag value arguments");
    }
    const flag = rawFlag.slice(2);
    if (!VALUE_FLAGS.has(flag)) {
      throw cliError("REVIEW_SMOKE_CLI_INVALID", "Unknown or forbidden review smoke option");
    }
    if (rawValue === undefined || String(rawValue).startsWith("--")) {
      throw cliError("REVIEW_SMOKE_CLI_INVALID", "A review smoke option is missing its value");
    }
    if (REPEATABLE_FLAGS.has(flag)) {
      if (!values[flag]) values[flag] = [];
      values[flag].push(String(rawValue));
    } else {
      if (Object.prototype.hasOwnProperty.call(values, flag)) {
        throw cliError("REVIEW_SMOKE_CLI_INVALID", "A review smoke option was provided more than once");
      }
      values[flag] = String(rawValue);
    }
  }
  return values;
}

function readBoundedFile(fsImpl, filename, maximum, label) {
  if (typeof filename !== "string" || !filename || filename.includes("\0")) {
    throw cliError("REVIEW_SMOKE_EVIDENCE_INVALID", `${label} path is invalid`);
  }
  let stat;
  try {
    stat = fsImpl.statSync(filename);
  } catch (_error) {
    throw cliError("REVIEW_SMOKE_EVIDENCE_INVALID", `${label} is unavailable`);
  }
  if (!stat.isFile() || stat.size < 1 || stat.size > maximum) {
    throw cliError("REVIEW_SMOKE_EVIDENCE_INVALID", `${label} size is invalid`);
  }
  try {
    return fsImpl.readFileSync(filename);
  } catch (_error) {
    throw cliError("REVIEW_SMOKE_EVIDENCE_INVALID", `${label} could not be read`);
  }
}

function imageMediaType(bytes) {
  if (bytes.length >= 8 && bytes.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) {
    return "image/png";
  }
  if (bytes.length >= 3 && bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff) {
    return "image/jpeg";
  }
  throw cliError("REVIEW_SMOKE_EVIDENCE_INVALID", "Review image is not a PNG or JPEG file");
}

function required(values, field) {
  const value = values[field];
  if (typeof value !== "string" || !value.trim()) {
    throw cliError("REVIEW_SMOKE_CLI_INVALID", `--${field} is required`);
  }
  return value.trim();
}

function optionalNumber(values, field) {
  if (values[field] === undefined) return undefined;
  const value = Number(values[field]);
  if (!Number.isFinite(value)) throw cliError("REVIEW_SMOKE_CLI_INVALID", `--${field} must be numeric`);
  return value;
}

function buildRunInput(values, { fsImpl = fs } = {}) {
  const promptBytes = readBoundedFile(fsImpl, required(values, "prompt-file"), MAX_PROMPT_FILE_BYTES, "Review prompt file");
  const imagePaths = values.image || [];
  if (imagePaths.length < 1 || imagePaths.length > 4) {
    throw cliError("REVIEW_SMOKE_EVIDENCE_INVALID", "One to four --image options are required");
  }
  const images = imagePaths.map((filename) => {
    const data = readBoundedFile(fsImpl, filename, MAX_IMAGE_FILE_BYTES, "Review image");
    return { mediaType: imageMediaType(data), data };
  });
  return {
    reviewType: required(values, "review-type"),
    provider: required(values, "provider"),
    model: required(values, "model"),
    sourceRevision: required(values, "build-revision"),
    sceneDigestBefore: required(values, "scene-digest-before"),
    sceneDigestAfter: required(values, "scene-digest-after"),
    reviewRequest: promptBytes.toString("utf8"),
    images,
    evidenceIds: images.map((image) => `sha256:${crypto.createHash("sha256").update(image.data).digest("hex")}`),
    receiptPath: required(values, "receipt"),
    receiptId: values["receipt-id"],
    receiptTtlMs: optionalNumber(values, "receipt-ttl-seconds") === undefined
      ? undefined
      : optionalNumber(values, "receipt-ttl-seconds") * 1_000,
    cliName: required(values, "cli-name"),
    cliVersion: required(values, "cli-version"),
    maxBudgetUsd: optionalNumber(values, "max-budget-usd"),
    timeoutMs: optionalNumber(values, "timeout-ms"),
    maxInputTokens: optionalNumber(values, "max-input-tokens"),
    maxOutputTokens: optionalNumber(values, "max-output-tokens"),
  };
}

function safeCliFailure(error) {
  const code = error && typeof error.code === "string" && /^REVIEW_[A-Z0-9_]+$/.test(error.code)
    ? error.code
    : "REVIEW_SMOKE_FAILED";
  return {
    ok: false,
    code,
    error: error instanceof ReviewProviderSmokeError
      ? error.message
      : "Review provider smoke failed before a receipt was written",
  };
}

async function main(argv = process.argv.slice(2), runtime = {}) {
  const stdout = runtime.stdout || process.stdout;
  const stderr = runtime.stderr || process.stderr;
  try {
    const values = parseArgv(argv);
    if (values.help) {
      stdout.write(`${USAGE}\n`);
      return 0;
    }
    const input = buildRunInput(values, { fsImpl: runtime.fsImpl || fs });
    const runner = runtime.runner || createReviewProviderSmokeRunner(runtime.runnerRuntime || {});
    const result = await runner.run(input);
    stdout.write(`${JSON.stringify({
      ok: true,
      schema: result.receipt.schema,
      receipt_id: result.receipt.receipt_id,
      receipt_path: result.receiptPath,
      review_type: result.receipt.review_type,
      provider: result.receipt.provider,
      model: result.receipt.model,
      source_revision: result.receipt.source_revision,
      status: result.receipt.verdict.status,
      usage: result.receipt.usage,
      expires_at: result.receipt.expires_at,
    })}\n`);
    return 0;
  } catch (error) {
    stderr.write(`${JSON.stringify(safeCliFailure(error))}\n`);
    return 2;
  }
}

if (require.main === module) {
  main().then((code) => { process.exitCode = code; });
}

module.exports = {
  USAGE,
  buildRunInput,
  imageMediaType,
  main,
  parseArgv,
  safeCliFailure,
};

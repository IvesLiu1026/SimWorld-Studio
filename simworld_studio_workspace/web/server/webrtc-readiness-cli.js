#!/usr/bin/env node
"use strict";

const path = require("node:path");
const {
  WebRtcReadinessError,
  createWebRtcReadinessReceipt,
  digestJson,
  readWebRtcProbeResults,
  writeWebRtcReadinessReceiptAtomic,
} = require("./webrtc-readiness-receipt");

const OPTIONS = new Set([
  "--input",
  "--output",
  "--expect-build-revision",
  "--expect-deployment-fingerprint",
  "--expect-origin",
  "--expect-certificate-fingerprint",
]);

function usage() {
  return [
    "Usage: node webrtc-readiness-cli.js --input <redacted-probes.json>",
    "  --expect-build-revision <40-char-git-sha>",
    "  --expect-deployment-fingerprint <sha256>",
    "  --expect-origin <https-origin>",
    "  [--expect-certificate-fingerprint <sha256>] [--output <receipt.json>]",
  ].join(" ");
}

function parseArguments(argv) {
  if (!Array.isArray(argv) || argv.length === 0 || argv.length % 2 !== 0) {
    throw new WebRtcReadinessError("WEBRTC_CLI_USAGE", usage());
  }
  const parsed = {};
  for (let index = 0; index < argv.length; index += 2) {
    const name = argv[index];
    const value = argv[index + 1];
    if (!OPTIONS.has(name) || typeof value !== "string" || !value || Object.hasOwn(parsed, name)) {
      throw new WebRtcReadinessError("WEBRTC_CLI_USAGE", usage());
    }
    parsed[name] = value;
  }
  for (const required of [
    "--input",
    "--expect-build-revision",
    "--expect-deployment-fingerprint",
    "--expect-origin",
  ]) {
    if (!parsed[required]) throw new WebRtcReadinessError("WEBRTC_CLI_USAGE", usage());
  }
  return parsed;
}

function writeJson(stream, value) {
  stream.write(`${JSON.stringify(value)}\n`);
}

function safeFailure(error) {
  if (error instanceof WebRtcReadinessError) {
    return {
      ready: false,
      code: error.code,
      message: error.message,
      ...(error.details.field ? { field: error.details.field } : {}),
    };
  }
  return {
    ready: false,
    code: "WEBRTC_VERIFIER_FAILED",
    message: "The offline WebRTC readiness verifier failed.",
  };
}

function runCli({
  argv = process.argv.slice(2),
  stdout = process.stdout,
  stderr = process.stderr,
  now = Date.now(),
} = {}) {
  try {
    const args = parseArguments(argv);
    const probe = readWebRtcProbeResults(args["--input"]);
    const receipt = createWebRtcReadinessReceipt(probe, {
      buildRevision: args["--expect-build-revision"],
      deploymentFingerprint: args["--expect-deployment-fingerprint"],
      publicOrigin: args["--expect-origin"],
      ...(args["--expect-certificate-fingerprint"]
        ? { certificateSha256: args["--expect-certificate-fingerprint"] }
        : {}),
      now,
    });
    const output = args["--output"]
      ? writeWebRtcReadinessReceiptAtomic(args["--output"], receipt)
      : null;
    writeJson(stdout, {
      ready: true,
      schema: receipt.schema,
      receipt_id: receipt.receipt_id,
      receipt_sha256: digestJson(receipt),
      ...(output ? { output: path.resolve(output) } : {}),
    });
    return 0;
  } catch (error) {
    writeJson(stderr, safeFailure(error));
    return error instanceof WebRtcReadinessError && error.code === "WEBRTC_CLI_USAGE" ? 2 : 1;
  }
}

if (require.main === module) process.exitCode = runCli();

module.exports = { parseArguments, runCli };

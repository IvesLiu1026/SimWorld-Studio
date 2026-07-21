#!/usr/bin/env node
"use strict";

const { readSecretFile, writePrivateAtomic } = require("./secure-config-files");
const {
  buildCirrusConfig,
  createTurnRestCredentials,
  redactCirrusConfig,
} = require("../../../simworld_studio_workspace/web/server/pixel-streaming-config");

function die(message) {
  process.stderr.write(`build-cirrus-config: ${message}\n`);
  process.exit(2);
}

function argumentsMap(argv) {
  const result = {};
  for (let index = 0; index < argv.length; index += 2) {
    const name = argv[index];
    const value = argv[index + 1];
    if (!/^--(?:output|http|streamer|sfu|session)$/.test(name || "") || value === undefined) {
      die("expected --output, --http, --streamer, --sfu, and --session value pairs");
    }
    if (Object.hasOwn(result, name)) die(`duplicate option ${name}`);
    result[name] = value;
  }
  for (const required of ["--output", "--http", "--streamer", "--sfu", "--session"]) {
    if (!result[required]) die(`missing ${required}`);
  }
  return result;
}

function port(value, field) {
  if (!/^\d{1,5}$/.test(value || "")) die(`${field} must be a TCP/UDP port`);
  const parsed = Number(value);
  if (parsed < 1 || parsed > 65535) die(`${field} must be a TCP/UDP port`);
  return parsed;
}

function secretFile(variable) {
  const filename = String(process.env[variable] || "").trim();
  const expectedGroupId = optionalGroupId(`${variable}_GID`);
  try {
    return {
      filename,
      value: readSecretFile(filename, { label: variable, expectedGroupId }),
    };
  }
  catch (error) { die(error.message); }
}

function optionalGroupId(variable) {
  const raw = String(process.env[variable] || "").trim();
  if (!raw) return undefined;
  if (!/^[1-9][0-9]{0,9}$/.test(raw)) die(`${variable} must be a positive non-root GID`);
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value > 0xffffffff) die(`${variable} must be a positive non-root GID`);
  return value;
}

function commaList(value) {
  return String(value || "").split(",").map((item) => item.trim()).filter(Boolean);
}

function boundedInteger(value, fallback, minimum, maximum, field) {
  const number = Number(value === undefined || value === "" ? fallback : value);
  if (!Number.isSafeInteger(number) || number < minimum || number > maximum) {
    die(`${field} must be an integer between ${minimum} and ${maximum}`);
  }
  return number;
}

const args = argumentsMap(process.argv.slice(2));
const publicHost = String(process.env.TURN_PUBLIC_HOST || "").trim();
if (!/^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$/.test(publicHost)) {
  die("TURN_PUBLIC_HOST must be a public DNS hostname");
}
const ttlSeconds = boundedInteger(
  process.env.TURN_CREDENTIAL_TTL_SECONDS,
  14400,
  300,
  86400,
  "TURN_CREDENTIAL_TTL_SECONDS",
);
const sessionHardMaxMs = boundedInteger(
  process.env.SESSION_HARD_MAX_MS,
  60 * 60 * 1000,
  10_000,
  24 * 60 * 60 * 1000,
  "SESSION_HARD_MAX_MS",
);
const reconnectGraceSeconds = boundedInteger(
  process.env.TURN_CREDENTIAL_RECONNECT_GRACE_SECONDS,
  600,
  60,
  3600,
  "TURN_CREDENTIAL_RECONNECT_GRACE_SECONDS",
);
const minimumCredentialLifetimeSeconds = Math.ceil(sessionHardMaxMs / 1000) + reconnectGraceSeconds;
if (ttlSeconds < minimumCredentialLifetimeSeconds) {
  die("TURN credential lifetime must cover SESSION_HARD_MAX_MS plus reconnect grace");
}
const turnSecret = secretFile("TURN_SHARED_SECRET_FILE");
const credentials = createTurnRestCredentials({
  sessionId: args["--session"],
  ttlSeconds,
}, { turnSharedSecret: turnSecret.value });
const stunUrls = commaList(process.env.TURN_STUN_URLS || `stun:${publicHost}:3478`);
const turnUrls = commaList(
  process.env.TURN_URLS || [
    `turn:${publicHost}:3478?transport=udp`,
    `turn:${publicHost}:3478?transport=tcp`,
    `turns:${publicHost}:5349?transport=tcp`,
  ].join(","),
);
const config = buildCirrusConfig({
  transportProfile: "trusted_proxy",
  httpPort: port(args["--http"], "--http"),
  streamerPort: port(args["--streamer"], "--streamer"),
  sfuPort: port(args["--sfu"], "--sfu"),
  useFrontend: false,
  ice: {
    stunUrls,
    turnUrls,
    turnUsername: credentials.username,
    transportPolicy: process.env.TURN_TRANSPORT_POLICY || "relay",
  },
}, { turnCredential: credentials.credential });

let output;
try {
  output = writePrivateAtomic(
    args["--output"],
    `${JSON.stringify(config, null, 2)}\n`,
    {
      label: "Cirrus output",
      forbiddenInputs: [
        { filename: turnSecret.filename, label: "TURN_SHARED_SECRET_FILE" },
      ],
    },
  );
} catch (error) {
  die(error.message);
}
process.stdout.write(`${JSON.stringify({
  ok: true,
  output,
  turnCredentialExpiresAt: credentials.expiresAt,
  reconnectGraceSeconds,
  sessionHardMaxMs,
  config: redactCirrusConfig(config),
})}\n`);

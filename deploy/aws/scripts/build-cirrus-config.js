#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const path = require("node:path");
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
  if (!path.isAbsolute(filename)) die(`${variable} must be an absolute secret-file path`);
  let value;
  try {
    value = fs.readFileSync(filename, "utf8").replace(/\r?\n$/, "");
  } catch {
    die(`${variable} could not be read`);
  }
  if (Buffer.byteLength(value) < 32) die(`${variable} must contain at least 32 bytes`);
  return value;
}

function commaList(value) {
  return String(value || "").split(",").map((item) => item.trim()).filter(Boolean);
}

function writeAtomic(filename, value) {
  const target = path.resolve(filename);
  const directory = path.dirname(target);
  const temporary = path.join(directory, `.${path.basename(target)}.${process.pid}.tmp`);
  try {
    fs.writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, {
      encoding: "utf8",
      mode: 0o600,
      flag: "wx",
    });
    fs.renameSync(temporary, target);
    fs.chmodSync(target, 0o600);
  } catch (error) {
    try { fs.unlinkSync(temporary); } catch {}
    die(`could not atomically write output: ${error.code || "write_failed"}`);
  }
}

const args = argumentsMap(process.argv.slice(2));
const publicHost = String(process.env.TURN_PUBLIC_HOST || "").trim();
if (!/^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$/.test(publicHost)) {
  die("TURN_PUBLIC_HOST must be a public DNS hostname");
}
const ttlSeconds = Number(process.env.TURN_CREDENTIAL_TTL_SECONDS || "14400");
const credentials = createTurnRestCredentials({
  sessionId: args["--session"],
  ttlSeconds,
}, { turnSharedSecret: secretFile("TURN_SHARED_SECRET_FILE") });
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

writeAtomic(args["--output"], config);
process.stdout.write(`${JSON.stringify({
  ok: true,
  output: path.resolve(args["--output"]),
  turnCredentialExpiresAt: credentials.expiresAt,
  config: redactCirrusConfig(config),
})}\n`);

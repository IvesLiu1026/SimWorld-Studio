#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const net = require("node:net");
const { readSecretFile, writePrivateAtomic } = require("./secure-config-files");

function die(message) {
  process.stderr.write(`materialize-coturn-config: ${message}\n`);
  process.exit(2);
}

function optionMap(argv) {
  const result = {};
  for (let index = 0; index < argv.length; index += 2) {
    if (!["--template", "--output"].includes(argv[index]) || argv[index + 1] === undefined) {
      die("usage: materialize-coturn-config.js --template FILE --output FILE");
    }
    result[argv[index]] = argv[index + 1];
  }
  if (!result["--template"] || !result["--output"]) die("--template and --output are required");
  return result;
}

function requiredEnvironment(name) {
  const value = String(process.env[name] || "").trim();
  if (!value) die(`${name} is required`);
  return value;
}

function optionalGroupId(name) {
  const raw = String(process.env[name] || "").trim();
  if (!raw) return undefined;
  if (!/^[1-9][0-9]{0,9}$/.test(raw)) die(`${name} must be a positive non-root GID`);
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value > 0xffffffff) die(`${name} must be a positive non-root GID`);
  return value;
}

function readFile(filename, description) {
  try { return fs.readFileSync(filename, "utf8"); }
  catch { die(`${description} could not be read`); }
}

function replaceExactlyOnce(source, needle, replacement) {
  const first = source.indexOf(needle);
  if (first < 0 || source.indexOf(needle, first + needle.length) >= 0) {
    die("coturn template does not match the reviewed placeholder contract");
  }
  return `${source.slice(0, first)}${replacement}${source.slice(first + needle.length)}`;
}

const options = optionMap(process.argv.slice(2));
const hostname = requiredEnvironment("TURN_PUBLIC_HOST");
if (!/^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$/.test(hostname)) {
  die("TURN_PUBLIC_HOST must be a DNS hostname");
}
const publicIp = requiredEnvironment("TURN_EXTERNAL_IP");
if (net.isIP(publicIp) === 0) die("TURN_EXTERNAL_IP must be a numeric IP address");
const privateIp = String(process.env.TURN_PRIVATE_IP || "").trim();
if (privateIp && net.isIP(privateIp) === 0) die("TURN_PRIVATE_IP must be a numeric IP address");
const secretFile = requiredEnvironment("TURN_SHARED_SECRET_FILE");
const secretGroupId = optionalGroupId("TURN_SHARED_SECRET_FILE_GID");
let secret;
try {
  secret = readSecretFile(secretFile, {
    label: "TURN_SHARED_SECRET_FILE",
    expectedGroupId: secretGroupId,
  });
}
catch (error) { die(error.message); }

let config = readFile(options["--template"], "coturn template");
config = replaceExactlyOnce(
  config,
  "static-auth-secret=CHANGE_ME_TO_AT_LEAST_32_RANDOM_BYTES",
  `static-auth-secret=${secret}`,
);
config = config.replaceAll("simworld.your-lab.edu", hostname);
config = replaceExactlyOnce(
  config,
  "# external-ip=PUBLIC_IP",
  `external-ip=${publicIp}${privateIp ? `/${privateIp}` : ""}`,
);
if (/CHANGE_ME|PUBLIC_IP|simworld\.your-lab\.edu/.test(config)) {
  die("unresolved coturn template placeholder remains");
}
let output;
try {
  output = writePrivateAtomic(options["--output"], config, {
    label: "coturn output",
    forbiddenInputs: [
      { filename: secretFile, label: "TURN_SHARED_SECRET_FILE" },
      { filename: options["--template"], label: "coturn template" },
    ],
    outputGroupId: optionalGroupId("TURN_CONFIG_GID"),
  });
}
catch (error) { die(error.message); }
process.stdout.write(`${JSON.stringify({ ok: true, output })}\n`);

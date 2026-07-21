#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const net = require("node:net");
const path = require("node:path");

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

function atomicWrite(filename, content) {
  const target = path.resolve(filename);
  const temporary = path.join(path.dirname(target), `.${path.basename(target)}.${process.pid}.tmp`);
  try {
    fs.writeFileSync(temporary, content, { encoding: "utf8", mode: 0o600, flag: "wx" });
    fs.renameSync(temporary, target);
    fs.chmodSync(target, 0o600);
  } catch (error) {
    try { fs.unlinkSync(temporary); } catch {}
    die(`output could not be atomically written: ${error.code || "write_failed"}`);
  }
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
const secret = readFile(secretFile, "TURN_SHARED_SECRET_FILE").replace(/\r?\n$/, "");
if (Buffer.byteLength(secret) < 32 || Buffer.byteLength(secret) > 4096 || /[\u0000-\u001f\u007f]/.test(secret)) {
  die("TURN_SHARED_SECRET_FILE does not meet policy");
}

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
atomicWrite(options["--output"], config);
process.stdout.write(`${JSON.stringify({ ok: true, output: path.resolve(options["--output"]) })}\n`);

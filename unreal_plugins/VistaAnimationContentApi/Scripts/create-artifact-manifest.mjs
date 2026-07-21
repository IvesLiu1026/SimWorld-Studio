#!/usr/bin/env node

import { createHash } from "node:crypto";
import {
  closeSync,
  constants,
  fstatSync,
  lstatSync,
  openSync,
  readFileSync,
  realpathSync,
} from "node:fs";
import path from "node:path";

function fail(message) {
  process.stderr.write(`${message}\n`);
  process.exit(2);
}

const args = new Map();
for (let index = 2; index < process.argv.length; index += 2) {
  if (!process.argv[index]?.startsWith("--") || process.argv[index + 1] === undefined) fail("arguments must be --name value pairs");
  args.set(process.argv[index].slice(2), process.argv[index + 1]);
}
const required = ["binary", "build-id", "engine-version", "target-platform"];
for (const key of required) if (!args.has(key)) fail(`missing --${key}`);
const binary = args.get("binary");
if (!path.isAbsolute(binary)) fail("--binary must be absolute");
const status = lstatSync(binary);
if (!status.isFile() || status.isSymbolicLink() || realpathSync(binary) !== binary) fail("binary must be a canonical regular file, not a symlink");
const opaque = /^[A-Za-z0-9][A-Za-z0-9._:@-]{0,159}$/;
const platform = /^[a-z0-9][a-z0-9_.-]{0,63}$/;
if (!opaque.test(args.get("build-id")) || !opaque.test(args.get("engine-version")) || !platform.test(args.get("target-platform"))) fail("artifact identity is invalid");

const descriptor = openSync(binary, constants.O_RDONLY | constants.O_NOFOLLOW);
let binaryBytes;
try {
  if (!fstatSync(descriptor).isFile()) fail("binary descriptor is not a regular file");
  binaryBytes = readFileSync(descriptor);
} finally {
  closeSync(descriptor);
}
const binarySha = createHash("sha256").update(binaryBytes).digest("hex");
process.stdout.write(`${JSON.stringify({
  schema: "vista-animation-ue-plugin-artifact/v1",
  plugin_name: "VistaAnimationContentApi",
  plugin_version: "1.2.0",
  plugin_build_id: args.get("build-id"),
  binary_sha256: binarySha,
  engine_version: args.get("engine-version"),
  target_platform: args.get("target-platform"),
  api_schema: "vista-animation-ue-content-api/v1",
}, null, 2)}\n`);

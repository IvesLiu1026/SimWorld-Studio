"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

test("source server keeps the loopback and model gates wired", () => {
  const indexPath = path.resolve(__dirname, "../index.js");
  const source = fs.readFileSync(indexPath, "utf8");
  assert.doesNotMatch(source, /require\(["']cors["']\)/);
  assert.doesNotMatch(source, /app\.use\(cors\(/);
  assert.doesNotMatch(source, /app\.listen\([^\n]*["']0\.0\.0\.0["']/);
  assert.match(source, /app\.listen\(PORT,STUDIO_HOST,/);
  assert.match(source, /app\.use\(requestLoopbackGuard\)/);
  assert.match(source, /app\.use\(createAccessGuard\(STUDIO_ACCESS_TOKEN\)\)/);
  assert.match(source, /app\.use\(createModelGate\(/);
  assert.match(source, /resolveContainedFile\(s\.query\.path,SCREENSHOT_SEARCH_DIRS\)/);
  assert.doesNotMatch(source, /sendFile\(path\.resolve\(t\)\)/);
});

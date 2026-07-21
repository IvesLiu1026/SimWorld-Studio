"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const {
  DEFAULT_CLAUDE_BUILDER_MODEL,
  resolveClaudeBuilderModel,
} = require("../builder-model-policy");

test("production Claude builder ignores a stale browser override and uses Opus 4.8", () => {
  assert.equal(
    resolveClaudeBuilderModel("claude-sonnet-4-6", {
      NODE_ENV: "production",
      CLAUDE_MODEL: "claude-opus-4-8",
    }),
    DEFAULT_CLAUDE_BUILDER_MODEL,
  );
});

test("production rejects deployment drift instead of silently running another Claude model", () => {
  assert.throws(
    () => resolveClaudeBuilderModel(null, {
      NODE_ENV: "production",
      CLAUDE_MODEL: "claude-sonnet-4-6",
    }),
    (error) => error && error.code === "BUILDER_MODEL_PIN_MISMATCH",
  );
});

test("development keeps an explicit model selector while defaulting to Opus 4.8", () => {
  assert.equal(resolveClaudeBuilderModel(null, {}), DEFAULT_CLAUDE_BUILDER_MODEL);
  assert.equal(resolveClaudeBuilderModel("claude-sonnet-4-6", {}), "claude-sonnet-4-6");
  assert.throws(() => resolveClaudeBuilderModel("bad\nmodel", {}), /non-empty model identifier/);
});

test("chat and skill-selection entry points share the deployment-aware resolver", () => {
  const source = fs.readFileSync(path.join(__dirname, "..", "index.js"), "utf8");
  assert.match(source, /resolveClaudeBuilderModel\(s\.body&&s\.body\.model,process\.env\)/);
  assert.doesNotMatch(
    source,
    /const CLAUDE_MODEL=\(\(s\.body&&s\.body\.model\)\|\|process\.env\.CLAUDE_MODEL/,
  );
});

"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const WEB_ROOT = path.resolve(__dirname, "../..");
const REPO_ROOT = path.resolve(WEB_ROOT, "../..");
const EXPECTED_MODEL = "claude-opus-4-8";

function read(relativePath) {
  return fs.readFileSync(path.join(REPO_ROOT, relativePath), "utf8");
}

test("Claude builder defaults stay pinned to Opus 4.8 across launch surfaces", () => {
  const registry = JSON.parse(read("simworld_studio_workspace/web/server/coding-agents.json"));
  assert.equal(registry.agents.claude.defaultModel, EXPECTED_MODEL);

  const files = [
    "simworld_studio_workspace/web/start.sh",
    "simworld_studio_workspace/web/server/start.sh",
    "simworld_studio_workspace/web/src/features/agents/codingAgents.js",
    "deploy/aws/docker/docker-compose.yml",
    "deploy/aws/systemd/simworld-web.service",
    "deploy/aws/templates/simworld.env",
  ];
  for (const relativePath of files) {
    assert.match(
      read(relativePath),
      new RegExp(`(?:CLAUDE_MODEL|defaultModel)[^\\n]*${EXPECTED_MODEL}`),
      relativePath,
    );
  }
});

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const SERVER_ROOT = path.resolve(__dirname, "..");
const REPO_ROOT = path.resolve(SERVER_ROOT, "../../..");

function read(relative) {
  return fs.readFileSync(path.join(REPO_ROOT, relative), "utf8");
}

test("server wires one shared journal recorder through readiness and all terminal domains", () => {
  const source = fs.readFileSync(path.join(SERVER_ROOT, "index.js"), "utf8");
  assert.match(source, /createArtifactJournalRuntime\(\{env:process\.env\}\)/);
  assert.match(source, /artifactJournalProbe:artifactJournalRuntime\.readinessProbe/);
  assert.equal((source.match(/artifactRecorder:artifactJournalRuntime\.recorder/g) || []).length, 4);
  assert.equal(source.includes('app.use("/api/artifact-journal"'), false);
  assert.ok(source.indexOf("createArtifactJournalRuntime") < source.indexOf("createStudioReadiness({"));
});

test("production launch surfaces require one persistent private journal root", () => {
  const compose = read("deploy/aws/docker/docker-compose.yml");
  const systemd = read("deploy/aws/systemd/simworld-web.service");
  const bakeAmi = read("deploy/aws/scripts/bake-ami.sh");
  for (const source of [compose, systemd]) {
    assert.match(source, /VISTA_ARTIFACT_JOURNAL_ENABLED=1/);
    assert.match(source, /VISTA_ARTIFACT_JOURNAL_ROOT=\/var\/lib\/simworld\/artifact-journal/);
    assert.match(source, /VISTA_ARTIFACT_JOURNAL_RETENTION_DAYS/);
  }
  assert.match(compose, /\/var\/lib\/simworld:\/var\/lib\/simworld/);
  assert.match(systemd, /ReadWritePaths=\/var\/lib\/simworld\b/);
  assert.match(bakeAmi, /install -d -o simworld -g simworld -m 700[\s\\]+\/var\/lib\/simworld\/artifact-journal/);
});

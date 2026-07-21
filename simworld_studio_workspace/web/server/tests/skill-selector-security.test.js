"use strict";

const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { PassThrough } = require("node:stream");
const test = require("node:test");

const { selectSkillsWithClaude } = require("../skill-selector");

function registry() {
  const skill = {
    id: "vista_scene",
    name: "VISTA Scene",
    description: "Build a bounded VISTA scene",
    tags: ["vista"],
    dependencies: [],
    source: "builtin",
    content: "Use verified scene specifications.",
  };
  return {
    list() { return [{ id: skill.id }]; },
    get(id) { return id === skill.id ? skill : null; },
  };
}

function fakeChild(onInput) {
  const child = new EventEmitter();
  child.stdin = new PassThrough();
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  child.killed = false;
  child.kill = () => { child.killed = true; };
  let input = "";
  child.stdin.setEncoding("utf8");
  child.stdin.on("data", (chunk) => { input += chunk; });
  child.stdin.on("finish", () => {
    onInput(input);
    setImmediate(() => {
      child.stdout.write(`${JSON.stringify({
        type: "result",
        result: JSON.stringify({
          selectedSkillIds: ["vista_scene"],
          reasoning: "matches the request",
        }),
      })}\n`);
      child.emit("close", 0);
    });
  });
  return child;
}

test("selector sends a leading-option prompt only over stdin inside the tool-free sandbox", async () => {
  const honeypot = "selector-host-secret-must-not-cross";
  let sandboxCall = null;
  let spawnCall = null;
  let stdinText = "";
  const result = await selectSkillsWithClaude({
    prompt: "--version",
    model: "claude-opus-4-8",
    claudeBin: "/trusted/claude",
    skillRegistry: registry(),
    env: {
      NODE_ENV: "development",
      PATH: "/trusted/bin",
      ANTHROPIC_API_KEY: honeypot,
      POSTGRES_URL: honeypot,
      HOME: `/home/${honeypot}`,
    },
    sandboxedSpawnImpl(bin, args, cwd, options) {
      sandboxCall = { bin, args, cwd, options };
      return { cmd: "/usr/bin/bwrap", args: ["--", "/opt/simworld-agent/bin/agent"] };
    },
    spawnImpl(cmd, args, options) {
      spawnCall = { cmd, args, options };
      return fakeChild((value) => { stdinText = value; });
    },
  });

  assert.deepEqual(result.selectedSkillIds, ["vista_scene"]);
  assert.equal(sandboxCall.bin, "/trusted/claude");
  assert.equal(sandboxCall.args.includes("--version"), false);
  assert.equal(sandboxCall.args.includes("--mcp-config"), false);
  assert.equal(sandboxCall.args[sandboxCall.args.indexOf("--tools") + 1], "");
  assert.ok(sandboxCall.args.includes("--safe-mode"));
  assert.equal(spawnCall.cmd, "/usr/bin/bwrap");
  assert.equal(spawnCall.options.cwd, "/");
  assert.deepEqual(spawnCall.options.stdio, ["pipe", "pipe", "pipe"]);
  assert.doesNotMatch(JSON.stringify(spawnCall.options.env), new RegExp(honeypot));
  assert.match(stdinText, /User request: --version/);
  assert.match(stdinText, /vista_scene/);
});

test("selector rejects an option-shaped model before sandbox or process launch", async () => {
  let launched = false;
  await assert.rejects(
    selectSkillsWithClaude({
      prompt: "build a room",
      model: "--version",
      skillRegistry: registry(),
      sandboxedSpawnImpl() { launched = true; },
      spawnImpl() { launched = true; },
    }),
    (error) => error && error.code === "BUILDER_CLAUDE_SELECTOR_POLICY_INCOMPLETE",
  );
  assert.equal(launched, false);
});

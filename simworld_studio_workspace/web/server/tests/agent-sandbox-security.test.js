"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { buildSandboxCommand } = require("../agent-sandbox");

const base = {
  hasBwrap: true,
  disabled: false,
  repoRoot: "/private/studio",
  bin: "/usr/bin/agent",
  args: ["run", "prompt"],
  cwd: "/private/studio/work",
};

test("sandbox construction fails closed", () => {
  assert.throws(
    () => buildSandboxCommand({ ...base, hasBwrap: false }),
    /bwrap is unavailable/,
  );
  assert.throws(
    () => buildSandboxCommand({ ...base, disabled: true }),
    /AGENT_SANDBOX=0 is forbidden/,
  );
});

test("sandbox command always makes the Studio source read-only", () => {
  const result = buildSandboxCommand(base);
  assert.equal(result.cmd, "bwrap");
  assert.equal(result.sandboxed, true);
  assert.deepEqual(result.args.slice(0, 6), [
    "--dev-bind", "/", "/", "--ro-bind", "/private/studio", "/private/studio",
  ]);
  assert.ok(result.args.includes("--die-with-parent"));
  assert.ok(result.args.includes("--chdir"));
  const separator = result.args.indexOf("--");
  assert.ok(separator > 0);
  assert.deepEqual(result.args.slice(separator + 1), ["/usr/bin/agent", "run", "prompt"]);
});

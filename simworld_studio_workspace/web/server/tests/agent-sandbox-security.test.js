"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const {
  buildSandboxCommand,
  probeSandboxCapability,
  REPO_ROOT,
  resolveAuthRoot,
  resolveProviderAuthMount,
  SANDBOX_CAPABILITY,
  sandboxedSpawn,
} = require("../agent-sandbox");

const capability = Object.freeze({
  verified: true,
  bwrapBin: "/usr/bin/bwrap",
  libraryRoots: Object.freeze(["/lib", "/lib64"]),
});

const base = {
  capability,
  disabled: false,
  repoRoot: "/private/studio",
  resolvedBin: "/opt/host/agent",
  args: ["run", "prompt"],
  runtimeExecutables: ["/opt/node/bin/node"],
  authMount: {
    source: "/private/agent-auth/claude",
    dest: "/run/simworld-agent-auth/claude",
  },
  systemReadonly: [
    { source: "/etc/hosts", dest: "/etc/hosts" },
  ],
};

test("sandbox construction fails closed without a verified namespace contract", () => {
  assert.throws(
    () => buildSandboxCommand({ ...base, capability: { verified: false } }),
    /capability probe has not passed/,
  );
  assert.throws(
    () => buildSandboxCommand({ ...base, disabled: true }),
    /AGENT_SANDBOX=0 is forbidden/,
  );
  assert.throws(
    () => buildSandboxCommand({ ...base, repoRoot: "/" }),
    /cannot be a filesystem root/,
  );
  assert.throws(
    () => buildSandboxCommand({
      ...base,
      repoRoot: "/private/studio",
      authMount: {
        source: "/private/studio/.auth/claude",
        dest: "/run/simworld-agent-auth/claude",
      },
    }),
    /auth mount must be outside the repository/,
  );
  assert.throws(
    () => buildSandboxCommand({
      ...base,
      repoRoot: "/private/agent-auth/claude/studio",
    }),
    /auth mount must be outside the repository/,
  );
});

test("sandbox starts from an empty mount namespace and exposes only exact read-only roots", () => {
  const result = buildSandboxCommand(base);
  assert.equal(result.cmd, "/usr/bin/bwrap");
  assert.equal(result.sandboxed, true);
  assert.equal(result.sandboxCwd, "/work");
  assert.ok(result.args.includes("--unshare-all"));
  assert.ok(result.args.includes("--share-net"));
  assert.ok(result.args.includes("--new-session"));
  assert.ok(result.args.includes("--die-with-parent"));
  assert.deepEqual(
    result.args.slice(result.args.indexOf("--cap-drop"), result.args.indexOf("--cap-drop") + 2),
    ["--cap-drop", "ALL"],
  );
  assert.ok(result.args.includes("--tmpfs"));
  assert.ok(result.args.includes("/home"));
  assert.ok(result.args.includes("/tmp"));
  assert.deepEqual(result.args.filter((value) => value === "--dev-bind"), []);

  const mounts = [];
  for (let index = 0; index < result.args.length; index += 1) {
    if (result.args[index] === "--ro-bind") mounts.push(result.args.slice(index + 1, index + 3));
  }
  assert.deepEqual(mounts, [
    ["/lib", "/lib"],
    ["/lib64", "/lib64"],
    ["/etc/hosts", "/etc/hosts"],
    ["/private/studio", "/private/studio"],
    ["/opt/node/bin/node", "/opt/node/bin/node"],
    ["/opt/host/agent", "/opt/simworld-agent/bin/agent"],
    ["/private/agent-auth/claude", "/run/simworld-agent-auth/claude"],
  ]);
  assert.equal(mounts.some(([source, dest]) => source === "/" || dest === "/"), false);
  const separator = result.args.indexOf("--");
  assert.ok(separator > 0);
  assert.deepEqual(result.args.slice(separator + 1), [
    "/opt/simworld-agent/bin/agent", "run", "prompt",
  ]);
});

test("sandbox capability probe requires a successful real namespace launch", () => {
  const calls = [];
  const ok = probeSandboxCapability({
    bwrapBin: "/usr/bin/bwrap",
    trueBin: "/bin/true",
    libraryRoots: ["/lib"],
    spawnSyncImpl(command, args, options) {
      calls.push({ command, args, options });
      return { status: 0, error: null };
    },
  });
  assert.equal(ok.verified, true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].command, "/usr/bin/bwrap");
  assert.ok(calls[0].args.includes("--unshare-all"));
  assert.ok(calls[0].args.includes("--ro-bind"));

  const failed = probeSandboxCapability({
    bwrapBin: "/usr/bin/bwrap",
    trueBin: "/bin/true",
    libraryRoots: ["/lib"],
    spawnSyncImpl() { return { status: 1 }; },
  });
  assert.equal(failed.verified, false);
  assert.equal(failed.code, "BUILDER_SANDBOX_PROBE_FAILED");
});

test("real namespace cannot see host secrets or write the repository", {
  skip: !SANDBOX_CAPABILITY.verified,
}, () => {
  const repoProbe = path.join(REPO_ROOT, `.sandbox-write-probe-${process.pid}`);
  const tmpProbe = `/tmp/simworld-private-probe-${process.pid}`;
  fs.rmSync(repoProbe, { force: true });
  fs.rmSync(tmpProbe, { force: true });
  const script = `
    const fs = require("node:fs");
    let repoWrite = "unexpected";
    try { fs.writeFileSync(${JSON.stringify(repoProbe)}, "blocked"); }
    catch (error) { repoWrite = error.code; }
    fs.writeFileSync(${JSON.stringify(tmpProbe)}, "private");
    process.stdout.write(JSON.stringify({
      shadow: fs.existsSync("/etc/shadow"),
      ssh: fs.existsSync("/home/yhliu/.ssh"),
      repoWrite,
      privateTmp: fs.existsSync(${JSON.stringify(tmpProbe)}),
    }));
  `;
  const sandbox = sandboxedSpawn(process.execPath, ["-e", script], null, {
    env: { NODE_ENV: "development", PATH: process.env.PATH },
    provider: "codex",
    authMount: null,
  });
  const result = spawnSync(sandbox.cmd, sandbox.args, {
    cwd: "/",
    env: {
      PATH: "/usr/bin:/bin",
      HOME: "/home/simworld-agent",
      TMPDIR: "/tmp",
    },
    encoding: "utf8",
    timeout: 10000,
  });
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), {
    shadow: false,
    ssh: false,
    repoWrite: "EROFS",
    privateTmp: true,
  });
  assert.equal(fs.existsSync(repoProbe), false);
  assert.equal(fs.existsSync(tmpProbe), false);
});

test("production auth mount is mandatory, private, owned, and never follows symlinks", (t) => {
  assert.throws(
    () => resolveAuthRoot({ NODE_ENV: "production" }),
    /required in production/,
  );
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), "simworld-agent-auth-"));
  const auth = path.join(parent, "auth");
  fs.mkdirSync(auth, { mode: 0o700 });
  const claude = path.join(auth, "claude");
  fs.mkdirSync(claude, { mode: 0o700 });
  fs.chmodSync(auth, 0o700);
  fs.chmodSync(claude, 0o700);
  t.after(() => fs.rmSync(parent, { recursive: true, force: true }));
  assert.equal(resolveAuthRoot({ NODE_ENV: "production", AGENT_SANDBOX_AUTH_ROOT: auth }), auth);
  assert.deepEqual(
    resolveProviderAuthMount({ NODE_ENV: "production", AGENT_SANDBOX_AUTH_ROOT: auth }, "claude"),
    { source: claude, dest: "/run/simworld-agent-auth/claude" },
  );
  assert.throws(
    () => resolveProviderAuthMount({ NODE_ENV: "production", AGENT_SANDBOX_AUTH_ROOT: auth }, "codex"),
    /codex.*real dedicated directory/,
  );

  fs.chmodSync(auth, 0o755);
  assert.throws(
    () => resolveAuthRoot({ NODE_ENV: "production", AGENT_SANDBOX_AUTH_ROOT: auth }),
    /mode 0700/,
  );
  fs.chmodSync(auth, 0o700);
  const link = path.join(parent, "auth-link");
  fs.symlinkSync(auth, link);
  assert.throws(
    () => resolveAuthRoot({ NODE_ENV: "production", AGENT_SANDBOX_AUTH_ROOT: link }),
    /real dedicated directory/,
  );
});

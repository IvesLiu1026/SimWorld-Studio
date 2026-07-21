"use strict";

// Fail-closed OS boundary for model-driven builder CLIs.
//
// The old wrapper used `--dev-bind / /`, which gave a compromised builder the
// complete writable host and only remounted the repository read-only. This
// wrapper starts from an empty mount namespace instead. It exposes only:
//   * the selected CLI binary and Node runtime as read-only files;
//   * dynamic-loader libraries and CA/DNS material as read-only system files;
//   * this repository read-only for the scoped stdio MCP subprocess;
//   * an optional dedicated, mode-0700 provider-auth directory read-only;
//   * private tmpfs work, HOME, /tmp, /dev, and a new /proc.
//
// Network is shared because the CLI needs its model endpoint and the scoped MCP
// subprocess needs the loopback broker. Host tools are independently disabled
// by builder-process-policy.js. Missing namespace capability or invalid mount
// configuration is a hard launch failure; there is no unsandboxed fallback.

const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const REPO_ROOT = path.resolve(__dirname, "../../..");
const BWRAP_CANDIDATES = Object.freeze(["/usr/bin/bwrap", "/bin/bwrap"]);
const SANDBOX_AGENT_BIN = "/opt/simworld-agent/bin/agent";
const SANDBOX_WORKDIR = "/work";
const SANDBOX_AUTH_DEST = "/run/simworld-agent-auth";

function findExecutable(binary, env = process.env, fsImpl = fs) {
  const requested = String(binary || "").trim();
  if (!requested || /\u0000/.test(requested)) return null;
  const candidates = path.isAbsolute(requested)
    ? [requested]
    : String(env.PATH || "/usr/bin:/bin").split(path.delimiter).filter(Boolean).map((dir) => path.join(dir, requested));
  for (const candidate of candidates) {
    try {
      const resolved = fsImpl.realpathSync(candidate);
      const stat = fsImpl.statSync(resolved);
      fsImpl.accessSync(resolved, fs.constants.X_OK);
      if (stat.isFile()) return resolved;
    } catch (_error) {}
  }
  return null;
}

function findBwrap(fsImpl = fs) {
  for (const candidate of BWRAP_CANDIDATES) {
    try {
      const stat = fsImpl.statSync(candidate);
      fsImpl.accessSync(candidate, fs.constants.X_OK);
      if (stat.isFile()) return candidate;
    } catch (_error) {}
  }
  return null;
}

function existingLibraryRoots(fsImpl = fs) {
  return ["/lib", "/lib64"].filter((candidate) => {
    try { return fsImpl.statSync(candidate).isDirectory(); } catch (_error) { return false; }
  });
}

function probeSandboxCapability(options = {}) {
  const fsImpl = options.fsImpl || fs;
  const spawnSyncImpl = options.spawnSyncImpl || spawnSync;
  const bwrapBin = options.bwrapBin || findBwrap(fsImpl);
  const trueBin = options.trueBin || findExecutable("/bin/true", process.env, fsImpl);
  const libraryRoots = options.libraryRoots || existingLibraryRoots(fsImpl);
  if (!bwrapBin || !trueBin || libraryRoots.length === 0) {
    return Object.freeze({ verified: false, code: "BUILDER_SANDBOX_DEPENDENCY_MISSING", bwrapBin });
  }
  const args = [
    "--unshare-all",
    "--share-net",
    "--die-with-parent",
    "--new-session",
    "--cap-drop", "ALL",
    "--proc", "/proc",
    "--dev", "/dev",
    "--tmpfs", "/tmp",
  ];
  for (const root of libraryRoots) args.push("--ro-bind", root, root);
  args.push("--ro-bind", trueBin, "/probe", "--", "/probe");
  let result;
  try {
    result = spawnSyncImpl(bwrapBin, args, {
      env: { PATH: "/usr/bin:/bin" },
      stdio: "ignore",
      timeout: 5000,
    });
  } catch (_error) {
    return Object.freeze({ verified: false, code: "BUILDER_SANDBOX_PROBE_FAILED", bwrapBin });
  }
  if (!result || result.status !== 0 || result.error) {
    return Object.freeze({ verified: false, code: "BUILDER_SANDBOX_PROBE_FAILED", bwrapBin });
  }
  return Object.freeze({
    verified: true,
    bwrapBin,
    libraryRoots: Object.freeze([...libraryRoots]),
  });
}

const SANDBOX_CAPABILITY = probeSandboxCapability();
const HAS_BWRAP = SANDBOX_CAPABILITY.verified;
const SANDBOX_DISABLED = process.env.AGENT_SANDBOX === "0";

function parentDirectories(target) {
  const out = [];
  let current = path.dirname(path.resolve(target));
  while (current !== path.parse(current).root) {
    out.push(current);
    current = path.dirname(current);
  }
  return out.reverse();
}

function appendDirs(args, paths) {
  const seen = new Set();
  for (const target of paths) {
    for (const directory of parentDirectories(target)) {
      if (seen.has(directory) || ["/tmp", "/home", "/proc", "/dev"].includes(directory)) continue;
      args.push("--dir", directory);
      seen.add(directory);
    }
  }
}

function pathsOverlap(left, right) {
  const a = path.resolve(String(left));
  const b = path.resolve(String(right));
  const aToB = path.relative(a, b);
  const bToA = path.relative(b, a);
  const contained = (relative) => relative === "" ||
    (!relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative));
  return contained(aToB) || contained(bToA);
}

function buildSandboxCommand({
  capability,
  disabled,
  repoRoot,
  resolvedBin,
  bin,
  args: childArgs,
  runtimeExecutables = [],
  authMount = null,
  systemReadonly = [],
}) {
  if (disabled) {
    throw new Error("Coding-agent sandbox is required; AGENT_SANDBOX=0 is forbidden");
  }
  if (!capability || !capability.verified || !capability.bwrapBin) {
    throw new Error("Coding-agent sandbox capability probe has not passed");
  }
  const agentBin = String(resolvedBin || bin || "");
  if (!path.isAbsolute(agentBin)) throw new Error("Coding-agent executable must resolve to an absolute file");
  const sourceRoot = path.resolve(String(repoRoot || ""));
  if (sourceRoot === path.parse(sourceRoot).root) throw new Error("Coding-agent repository mount cannot be a filesystem root");
  const argv = Array.isArray(childArgs) ? childArgs.map(String) : [];
  if (argv.some((value) => /\u0000/.test(value))) throw new Error("Coding-agent argv contains NUL");

  const runtimes = [...new Set(runtimeExecutables.map((value) => path.resolve(String(value))))];
  const readonly = systemReadonly.map((entry) => ({
    source: path.resolve(String(entry.source)),
    dest: path.resolve(String(entry.dest)),
  }));
  if (authMount) {
    const authSource = path.resolve(String(authMount.source || ""));
    const authDest = path.resolve(String(authMount.dest || ""));
    if (authSource === path.parse(authSource).root ||
        !new Set([`${SANDBOX_AUTH_DEST}/claude`, `${SANDBOX_AUTH_DEST}/codex`]).has(authDest)) {
      throw new Error("Coding-agent auth mount is invalid");
    }
    if (pathsOverlap(sourceRoot, authSource)) {
      throw new Error("Coding-agent auth mount must be outside the repository mount");
    }
    authMount = Object.freeze({ source: authSource, dest: authDest });
  }
  const mountTargets = [sourceRoot, SANDBOX_AGENT_BIN, ...runtimes, ...readonly.map((entry) => entry.dest)];
  if (authMount) mountTargets.push(authMount.dest);

  const bw = [
    "--unshare-all",
    "--share-net",
    "--die-with-parent",
    "--new-session",
    "--cap-drop", "ALL",
    "--proc", "/proc",
    "--dev", "/dev",
    "--tmpfs", "/tmp",
    "--tmpfs", "/home",
    "--dir", SANDBOX_WORKDIR,
  ];
  appendDirs(bw, mountTargets);
  for (const root of capability.libraryRoots || []) bw.push("--ro-bind", root, root);
  for (const entry of readonly) bw.push("--ro-bind", entry.source, entry.dest);
  bw.push("--ro-bind", sourceRoot, sourceRoot);
  for (const runtime of runtimes) bw.push("--ro-bind", runtime, runtime);
  bw.push("--ro-bind", agentBin, SANDBOX_AGENT_BIN);
  if (authMount) bw.push("--ro-bind", path.resolve(authMount.source), path.resolve(authMount.dest));
  bw.push("--chdir", SANDBOX_WORKDIR, "--", SANDBOX_AGENT_BIN, ...argv);
  return Object.freeze({
    cmd: capability.bwrapBin,
    args: Object.freeze(bw),
    sandboxed: true,
    sandboxCwd: SANDBOX_WORKDIR,
  });
}

function resolveAuthRoot(env = process.env, fsImpl = fs) {
  const raw = String(env.AGENT_SANDBOX_AUTH_ROOT || "").trim();
  if (!raw) {
    if (String(env.NODE_ENV || "").trim().toLowerCase() === "production") {
      throw new Error("AGENT_SANDBOX_AUTH_ROOT is required in production");
    }
    return null;
  }
  if (!path.isAbsolute(raw)) throw new Error("AGENT_SANDBOX_AUTH_ROOT must be absolute");
  const target = path.resolve(raw);
  let stat;
  try {
    stat = fsImpl.lstatSync(target);
    if (!stat.isDirectory() || stat.isSymbolicLink() || fsImpl.realpathSync(target) !== target) throw new Error("unsafe");
  } catch (_error) {
    throw new Error("AGENT_SANDBOX_AUTH_ROOT must be a real dedicated directory");
  }
  if ((stat.mode & 0o077) !== 0) throw new Error("AGENT_SANDBOX_AUTH_ROOT must have mode 0700 or stricter");
  if (typeof process.getuid === "function" && stat.uid !== process.getuid()) {
    throw new Error("AGENT_SANDBOX_AUTH_ROOT must be owned by the Studio service user");
  }
  return target;
}

function resolveProviderAuthMount(env = process.env, provider, fsImpl = fs) {
  const root = resolveAuthRoot(env, fsImpl);
  if (!root) return null;
  const name = String(provider || "").trim().toLowerCase();
  if (name !== "claude" && name !== "codex") {
    throw new Error("A supported builder provider is required for the auth mount");
  }
  const source = path.join(root, name);
  let stat;
  try {
    stat = fsImpl.lstatSync(source);
    if (!stat.isDirectory() || stat.isSymbolicLink() || fsImpl.realpathSync(source) !== source) throw new Error("unsafe");
  } catch (_error) {
    throw new Error(`AGENT_SANDBOX_AUTH_ROOT/${name} must be a real dedicated directory`);
  }
  if ((stat.mode & 0o077) !== 0) throw new Error(`AGENT_SANDBOX_AUTH_ROOT/${name} must have mode 0700 or stricter`);
  if (typeof process.getuid === "function" && stat.uid !== process.getuid()) {
    throw new Error(`AGENT_SANDBOX_AUTH_ROOT/${name} must be owned by the Studio service user`);
  }
  return Object.freeze({ source, dest: `${SANDBOX_AUTH_DEST}/${name}` });
}

function systemReadonlyMounts(fsImpl = fs) {
  const specs = [
    ["/etc/ssl/certs", "/etc/ssl/certs"],
    ["/etc/hosts", "/etc/hosts"],
    ["/etc/nsswitch.conf", "/etc/nsswitch.conf"],
    ["/etc/passwd", "/etc/passwd"],
    ["/etc/group", "/etc/group"],
    ["/etc/localtime", "/etc/localtime"],
  ];
  try {
    const resolv = fsImpl.realpathSync("/etc/resolv.conf");
    specs.push([resolv, "/etc/resolv.conf"]);
  } catch (_error) {}
  return specs.filter(([source]) => {
    try { fsImpl.statSync(source); return true; } catch (_error) { return false; }
  }).map(([source, dest]) => ({ source, dest }));
}

/**
 * Prepare a builder command for child_process.spawn(). The caller must pass the
 * minimal environment from builder-process-policy.js to spawn; this function
 * controls only the kernel-visible mount/namespace boundary.
 */
function sandboxedSpawn(bin, args, _cwd, options = {}) {
  const fsImpl = options.fsImpl || fs;
  const env = options.env || process.env;
  const capability = options.capability || SANDBOX_CAPABILITY;
  const resolvedBin = options.resolvedBin || findExecutable(bin, env, fsImpl);
  if (!resolvedBin) throw new Error("Coding-agent executable is unavailable");
  const nodeBin = options.nodeBin || findExecutable(process.execPath, env, fsImpl);
  if (!nodeBin) throw new Error("Coding-agent MCP Node runtime is unavailable");
  const authMount = options.authMount === undefined
    ? resolveProviderAuthMount(env, options.provider, fsImpl)
    : options.authMount;
  return buildSandboxCommand({
    capability,
    disabled: options.disabled === undefined ? SANDBOX_DISABLED : options.disabled,
    repoRoot: options.repoRoot || REPO_ROOT,
    resolvedBin,
    args,
    runtimeExecutables: options.runtimeExecutables || [nodeBin],
    authMount,
    systemReadonly: options.systemReadonly || systemReadonlyMounts(fsImpl),
  });
}

module.exports = {
  buildSandboxCommand,
  findExecutable,
  HAS_BWRAP,
  probeSandboxCapability,
  pathsOverlap,
  REPO_ROOT,
  resolveAuthRoot,
  resolveProviderAuthMount,
  SANDBOX_CAPABILITY,
  SANDBOX_DISABLED,
  SANDBOX_WORKDIR,
  sandboxedSpawn,
  systemReadonlyMounts,
};

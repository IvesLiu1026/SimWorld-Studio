"use strict";

const fs = require("node:fs");
const path = require("node:path");

const BUILDER_HOME = "/home/simworld-agent";
const BUILDER_AUTH_ROOT = "/run/simworld-agent-auth";
const MAX_MCP_CONFIG_BYTES = 64 * 1024;
const MAX_VISUAL_FEEDBACK_BYTES = 10 * 1024 * 1024;
const OPAQUE_IMAGE_REF = /^simworld-image:[A-Za-z0-9_-]{16,128}$/;

// Deliberately excludes arbitrary Python, background-job log reads, scene
// filesystem export, and the nested-provider verify_scene handler. Review is
// owned by the main-process Review coordinator so no provider secret or
// writable evidence root crosses into this child.
const SCOPED_SIMWORLD_TOOLS = Object.freeze([
  "search_assets",
  "spawn_blueprint_actor",
  "spawn_actor",
  "delete_actor",
  "delete_all_spawned",
  "get_actors_in_level",
  "find_actors_by_name",
  "set_actor_transform",
  "take_screenshot",
  "set_camera",
  "set_actor_color",
  "list_assets",
  "setup_environment",
  "check_floating",
  "check_collisions",
  "spawn_agent",
  "agent_stop",
  "agent_rotate",
  "agent_action",
  "get_agent_state",
]);

const CLAUDE_SCOPED_TOOLS = Object.freeze(
  SCOPED_SIMWORLD_TOOLS.map((name) => `mcp__simworld__${name}`),
);

const CLAUDE_HOST_TOOLS = Object.freeze([
  "Bash",
  "BashOutput",
  "KillShell",
  "Read",
  "Glob",
  "Grep",
  "Edit",
  "MultiEdit",
  "Write",
  "NotebookEdit",
  "Task",
  "TodoWrite",
  "WebFetch",
  "WebSearch",
]);

const SAFE_ENV_KEYS = Object.freeze([
  "LANG",
  "LC_ALL",
  "LC_CTYPE",
  "TZ",
  "NODE_ENV",
  "SIMWORLD_PRODUCTION_SAFETY",
  "SIMWORLD_BUILD_REVISION",
  "ASSET_REQUIRE_REAL_ASSETS",
  "REQUIRE_REAL_ASSETS",
]);

const FORBIDDEN_ARGV = new Set([
  "--dangerously-bypass-approvals-and-sandbox",
  "--dangerously-skip-permissions",
  "--allow-dangerously-skip-permissions",
  "--dangerously-bypass-hook-trust",
  "--sandbox=danger-full-access",
  "--permission-mode=bypassPermissions",
]);

class BuilderProcessPolicyError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "BuilderProcessPolicyError";
    this.code = code;
    this.statusCode = 503;
  }
}

function isProduction(env) {
  return String(env && env.NODE_ENV || "").trim().toLowerCase() === "production";
}

function safeEnvValue(value, key) {
  const text = String(value == null ? "" : value);
  if (text.length > 1024 || /[\u0000\r\n]/.test(text)) {
    throw new BuilderProcessPolicyError(
      "BUILDER_ENV_INVALID",
      `Builder environment value is invalid for ${key}`,
    );
  }
  return text;
}

function requireRuntime(runtime, production) {
  if (!runtime) {
    if (production) {
      throw new BuilderProcessPolicyError(
        "BUILDER_RUN_AUTHORITY_REQUIRED",
        "A lease-scoped builder run authority is required for every real builder",
      );
    }
    return null;
  }
  const capability = String(runtime.capability || "").trim();
  const runId = String(runtime.runId || "").trim();
  const port = Number(runtime.serverPort);
  if (!/^[A-Za-z0-9_-]{43}$/.test(capability) ||
      !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/.test(runId) ||
      !Number.isSafeInteger(port) || port < 1 || port > 65535) {
    throw new BuilderProcessPolicyError(
      "BUILDER_RUN_AUTHORITY_INVALID",
      "The lease-scoped builder run authority is invalid",
    );
  }
  return { capability, runId, port };
}

/**
 * Build a new child environment from an allowlist. This intentionally does not
 * inherit provider keys, database DSNs, cloud credentials, proxy credentials,
 * root Studio bearers, shell startup variables, or the host HOME.
 *
 * Provider authentication must come from the dedicated read-only auth mount or
 * from a deployment credential broker; it is never copied from process.env.
 */
function buildMinimalBuilderEnv(baseEnv = process.env, runtime = null, options = {}) {
  const env = buildMinimalProviderEnv(baseEnv, options);
  // Production code has one real-builder contract in every environment: an
  // active trusted/public lease and its server-minted run capability. Loopback
  // development can use reviewed mock/off mode, never a weaker child token.
  const authority = requireRuntime(runtime, true);
  if (authority) {
    env.SIMWORLD_INTERNAL_RUN_CAPABILITY = authority.capability;
    env.SIMWORLD_INTERNAL_RUN_ID = authority.runId;
    env.SIMWORLD_INTERNAL_CAPABILITY_REQUIRED = "1";
    env.SIMWORLD_BROKER_HOST = "127.0.0.1";
    env.PORT = String(authority.port);
  }
  return Object.freeze(env);
}

function buildMinimalProviderEnv(baseEnv = process.env, options = {}) {
  const provider = String(options.provider || "").trim().toLowerCase();
  if (provider !== "claude" && provider !== "codex") {
    throw new BuilderProcessPolicyError("BUILDER_PROVIDER_INVALID", "Builder provider is invalid");
  }
  const env = Object.create(null);
  env.PATH = "/usr/bin:/bin";
  env.HOME = BUILDER_HOME;
  env.TMPDIR = "/tmp";
  env.XDG_CACHE_HOME = "/tmp/xdg-cache";
  env.XDG_CONFIG_HOME = "/tmp/xdg-config";
  env.XDG_DATA_HOME = "/tmp/xdg-data";
  env.NO_COLOR = "1";
  for (const key of SAFE_ENV_KEYS) {
    if (baseEnv && baseEnv[key] != null && String(baseEnv[key]) !== "") {
      env[key] = safeEnvValue(baseEnv[key], key);
    }
  }
  if (provider === "codex") env.CODEX_HOME = `${BUILDER_AUTH_ROOT}/codex`;
  if (provider === "claude") env.CLAUDE_CONFIG_DIR = `${BUILDER_AUTH_ROOT}/claude`;
  return env;
}

/**
 * Tool-free routing calls need provider authentication but no UE lease. They
 * still receive the same fresh, secret-free environment and OS sandbox as a
 * builder. In particular, no host/provider/DB secret is inherited.
 */
function buildMinimalToolFreeEnv(baseEnv = process.env, options = {}) {
  return Object.freeze(buildMinimalProviderEnv(baseEnv, options));
}

function assertSafeBuilderArgv(provider, args) {
  const argv = Array.isArray(args) ? args.map(String) : [];
  for (const arg of argv) {
    if (FORBIDDEN_ARGV.has(arg) || /^--dangerously-/.test(arg)) {
      throw new BuilderProcessPolicyError(
        "BUILDER_DANGEROUS_ARGV",
        `Dangerous ${provider} builder option is forbidden`,
      );
    }
  }
  const genericSandboxAt = argv.indexOf("--sandbox");
  const permissionAt = argv.indexOf("--permission-mode");
  if (argv.some((arg, index) => arg === "--sandbox" && argv[index + 1] === "danger-full-access") ||
      argv.some((arg, index) => arg === "--permission-mode" && argv[index + 1] === "bypassPermissions")) {
    throw new BuilderProcessPolicyError(
      "BUILDER_DANGEROUS_ARGV",
      `Dangerous ${provider} builder option is forbidden`,
    );
  }
  if (provider === "codex") {
    const sandboxAt = argv.indexOf("--sandbox");
    if (sandboxAt < 0 || argv[sandboxAt + 1] !== "read-only" ||
        !argv.includes("--ephemeral") || !argv.includes("--ignore-user-config") ||
        !argv.includes("--ignore-rules") || !argv.includes('approval_policy="never"') ||
        !argv.includes('web_search="disabled"')) {
      throw new BuilderProcessPolicyError(
        "BUILDER_CODEX_POLICY_INCOMPLETE",
        "Codex builder read-only, ephemeral, no-approval policy is incomplete",
      );
    }
  } else if (provider === "claude") {
    const toolsAt = argv.indexOf("--tools");
    const allowedAt = argv.indexOf("--allowedTools");
    const deniedAt = argv.indexOf("--disallowedTools");
    const settingSourcesAt = argv.indexOf("--setting-sources");
    const exactTools = CLAUDE_SCOPED_TOOLS.join(",");
    const exactDenied = CLAUDE_HOST_TOOLS.join(",");
    const modelFlags = argv.reduce((count, value) => count + (value === "--model" ? 1 : 0), 0);
    const modelAt = argv.indexOf("--model");
    if (!argv.includes("--strict-mcp-config") || argv.includes("--safe-mode") ||
        !argv.includes("--no-session-persistence") || !argv.includes("--disable-slash-commands") ||
        !argv.includes("--no-chrome") ||
        settingSourcesAt < 0 || argv[settingSourcesAt + 1] !== "" ||
        permissionAt < 0 || argv[permissionAt + 1] !== "dontAsk" ||
        toolsAt < 0 || argv[toolsAt + 1] !== exactTools ||
        allowedAt < 0 || argv[allowedAt + 1] !== exactTools ||
        deniedAt < 0 || argv[deniedAt + 1] !== exactDenied || modelFlags > 1 ||
        (modelAt >= 0 && !/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/.test(argv[modelAt + 1] || ""))) {
      throw new BuilderProcessPolicyError(
        "BUILDER_CLAUDE_POLICY_INCOMPLETE",
        "Claude builder MCP-only tool policy is incomplete",
      );
    }
  } else {
    throw new BuilderProcessPolicyError("BUILDER_PROVIDER_INVALID", "Builder provider is invalid");
  }
  return Object.freeze(argv);
}

function buildCodexSafetyArgs() {
  return Object.freeze([
    "--sandbox", "read-only",
    "--ephemeral",
    "--ignore-user-config",
    "--ignore-rules",
    "-c", 'approval_policy="never"',
    "-c", 'web_search="disabled"',
  ]);
}

function buildClaudeSafetyArgs() {
  return Object.freeze([
    "--strict-mcp-config",
    "--setting-sources", "",
    "--no-session-persistence",
    "--disable-slash-commands",
    "--no-chrome",
    "--permission-mode", "dontAsk",
    "--tools", CLAUDE_SCOPED_TOOLS.join(","),
    "--allowedTools", CLAUDE_SCOPED_TOOLS.join(","),
    "--disallowedTools", CLAUDE_HOST_TOOLS.join(","),
  ]);
}

function buildClaudeToolFreeSafetyArgs() {
  return Object.freeze([
    "--strict-mcp-config",
    "--safe-mode",
    "--setting-sources", "",
    "--no-session-persistence",
    "--disable-slash-commands",
    "--no-chrome",
    "--permission-mode", "dontAsk",
    "--tools", "",
    "--allowedTools", "",
    "--disallowedTools", CLAUDE_HOST_TOOLS.join(","),
  ]);
}

function assertSafeToolFreeClaudeArgv(args) {
  const argv = Array.isArray(args) ? args.map(String) : [];
  for (const arg of argv) {
    if (FORBIDDEN_ARGV.has(arg) || /^--dangerously-/.test(arg)) {
      throw new BuilderProcessPolicyError(
        "BUILDER_DANGEROUS_ARGV",
        "Dangerous Claude selector option is forbidden",
      );
    }
  }
  const valueFlags = new Map([
    ["--input-format", (value) => value === "text"],
    ["--output-format", (value) => value === "stream-json"],
    ["--setting-sources", (value) => value === ""],
    ["--permission-mode", (value) => value === "dontAsk"],
    ["--tools", (value) => value === ""],
    ["--allowedTools", (value) => value === ""],
    ["--disallowedTools", (value) => value === CLAUDE_HOST_TOOLS.join(",")],
    ["--model", (value) => /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/.test(value)],
  ]);
  const booleanFlags = new Set([
    "-p",
    "--include-partial-messages",
    "--verbose",
    "--strict-mcp-config",
    "--safe-mode",
    "--no-session-persistence",
    "--disable-slash-commands",
    "--no-chrome",
  ]);
  const seen = new Set();
  for (let index = 0; index < argv.length; index += 1) {
    const flag = argv[index];
    if (seen.has(flag) || (!booleanFlags.has(flag) && !valueFlags.has(flag))) {
      throw new BuilderProcessPolicyError(
        "BUILDER_CLAUDE_SELECTOR_POLICY_INCOMPLETE",
        "Claude selector argv must contain only the exact tool-free policy",
      );
    }
    seen.add(flag);
    if (valueFlags.has(flag)) {
      const value = argv[index + 1];
      if (value === undefined || !valueFlags.get(flag)(value)) {
        throw new BuilderProcessPolicyError(
          "BUILDER_CLAUDE_SELECTOR_POLICY_INCOMPLETE",
          "Claude selector argv contains an invalid policy value",
        );
      }
      index += 1;
    }
  }
  const required = [
    "-p", "--input-format", "--output-format", "--include-partial-messages", "--verbose",
    "--strict-mcp-config", "--safe-mode", "--setting-sources", "--no-session-persistence",
    "--disable-slash-commands", "--no-chrome", "--permission-mode", "--tools",
    "--allowedTools", "--disallowedTools",
  ];
  if (required.some((flag) => !seen.has(flag))) {
    throw new BuilderProcessPolicyError(
      "BUILDER_CLAUDE_SELECTOR_POLICY_INCOMPLETE",
      "Claude selector tool-free policy is incomplete",
    );
  }
  return Object.freeze(argv);
}

function assertScopedMcpConfig(configPath, options = {}) {
  const fsImpl = options.fsImpl || fs;
  const target = path.resolve(String(configPath || ""));
  if (!path.isAbsolute(String(configPath || "")) || target === path.parse(target).root) {
    throw new BuilderProcessPolicyError(
      "BUILDER_MCP_CONFIG_INVALID",
      "Builder MCP config must be an absolute server-managed file",
    );
  }
  let stat;
  let raw;
  try {
    stat = fsImpl.lstatSync(target);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.size > MAX_MCP_CONFIG_BYTES) throw new Error("unsafe file");
    if (typeof fsImpl.realpathSync === "function" && fsImpl.realpathSync(target) !== target) throw new Error("symlinked path");
    raw = fsImpl.readFileSync(target, "utf8");
  } catch (_error) {
    throw new BuilderProcessPolicyError(
      "BUILDER_MCP_CONFIG_INVALID",
      "Builder MCP config is not a bounded regular server-managed file",
    );
  }
  let parsed;
  try { parsed = JSON.parse(raw); } catch (_error) {
    throw new BuilderProcessPolicyError("BUILDER_MCP_CONFIG_INVALID", "Builder MCP config is invalid JSON");
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed) ||
      Object.keys(parsed).length !== 1 || Object.keys(parsed)[0] !== "mcpServers") {
    throw new BuilderProcessPolicyError(
      "BUILDER_MCP_SCOPE_INVALID",
      "Builder MCP config must contain only the mcpServers root key",
    );
  }
  if (isProduction(options.env || process.env) &&
      (((stat.mode & 0o077) !== 0) ||
       (typeof process.getuid === "function" && stat.uid !== process.getuid()))) {
    throw new BuilderProcessPolicyError(
      "BUILDER_MCP_CONFIG_INVALID",
      "Production builder MCP config must be service-owned and mode 0600 or stricter",
    );
  }
  const servers = parsed.mcpServers;
  const names = servers && typeof servers === "object" && !Array.isArray(servers)
    ? Object.keys(servers)
    : [];
  if (names.length !== 1 || names[0] !== "simworld") {
    throw new BuilderProcessPolicyError(
      "BUILDER_MCP_SCOPE_INVALID",
      "Builder MCP config must contain exactly the scoped simworld server",
    );
  }
  const server = servers.simworld;
  if (!server || typeof server !== "object" || Array.isArray(server) ||
      Object.keys(server).sort().join(",") !== "args,command,env") {
    throw new BuilderProcessPolicyError(
      "BUILDER_MCP_SCOPE_INVALID",
      "Builder MCP server must contain only command, args, and env",
    );
  }
  const command = String(server && server.command || "");
  const args = server && server.args;
  const serverEnv = server && server.env || {};
  const expectedCommand = path.resolve(options.expectedCommand || process.execPath);
  const expectedScript = path.resolve(options.expectedScript || path.join(__dirname, "mcp-server.js"));
  const allowedEnv = new Set([
    "SIMWORLD_BROKER_HOST",
    "PORT",
    "SIMWORLD_INTERNAL_CAPABILITY_REQUIRED",
  ]);
  if (command !== expectedCommand || !Array.isArray(args) ||
      args.length !== 1 || path.resolve(String(args[0] || "")) !== expectedScript ||
      args.some((value) => typeof value !== "string" || value.length > 4096 || /\u0000/.test(value)) ||
      !serverEnv || typeof serverEnv !== "object" || Array.isArray(serverEnv) ||
      Object.keys(serverEnv).some((key) => !allowedEnv.has(key))) {
    throw new BuilderProcessPolicyError(
      "BUILDER_MCP_SCOPE_INVALID",
      "Builder MCP config contains an unscoped command, argument, or environment value",
    );
  }
  const envKeys = Object.keys(serverEnv).sort();
  const brokerKeys = ["PORT", "SIMWORLD_BROKER_HOST", "SIMWORLD_INTERNAL_CAPABILITY_REQUIRED"];
  const exactKeys = (expected) => envKeys.length === expected.length && expected.every((key, index) => key === envKeys[index]);
  const broker = exactKeys(brokerKeys) &&
    String(serverEnv.SIMWORLD_BROKER_HOST) === "127.0.0.1" &&
    String(serverEnv.SIMWORLD_INTERNAL_CAPABILITY_REQUIRED) === "1" &&
    /^[1-9][0-9]{0,4}$/.test(String(serverEnv.PORT)) && Number(serverEnv.PORT) <= 65535;
  if (!broker) {
    throw new BuilderProcessPolicyError(
      "BUILDER_MCP_SCOPE_INVALID",
      "Builder MCP config routing must be the exact lease-broker profile",
    );
  }
  return Object.freeze({
    path: target,
    command,
    args: Object.freeze(args.map(String)),
    env: Object.freeze(Object.fromEntries(Object.entries(serverEnv).map(([key, value]) => [key, String(value)]))),
  });
}

function isPng(bytes) {
  return bytes.length >= 8 && bytes.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]));
}

function isJpeg(bytes) {
  return bytes.length >= 4 && bytes[0] === 0xff && bytes[1] === 0xd8 &&
    bytes[bytes.length - 2] === 0xff && bytes[bytes.length - 1] === 0xd9;
}

/**
 * Resolve only opaque server-managed image references. No caller filesystem
 * path is ever inspected. Current CLI launchers deliberately call this without
 * a resolver, so non-empty image input fails closed until a byte-to-sandbox
 * transport is installed and reviewed.
 */
function resolveVisualFeedbackImages(value, options = {}) {
  if (value == null) return Object.freeze([]);
  if (!Array.isArray(value) || value.length > 8) {
    throw new BuilderProcessPolicyError(
      "BUILDER_VISUAL_FEEDBACK_INVALID",
      "visualFeedbackImages must be a bounded list of server-managed opaque references",
    );
  }
  if (value.length === 0) return Object.freeze([]);
  for (const ref of value) {
    if (typeof ref !== "string" || !OPAQUE_IMAGE_REF.test(ref)) {
      throw new BuilderProcessPolicyError(
        "BUILDER_VISUAL_FEEDBACK_INVALID",
        "Caller filesystem paths are forbidden for visual feedback",
      );
    }
  }
  const resolver = options.resolveOpaqueImage;
  if (typeof resolver !== "function") {
    throw new BuilderProcessPolicyError(
      "BUILDER_VISUAL_FEEDBACK_UNAVAILABLE",
      "Server-managed visual feedback transport is not installed",
    );
  }
  return Object.freeze(value.map((ref) => {
    const resolved = resolver(ref);
    if (!Buffer.isBuffer(resolved) || resolved.length < 4 ||
        resolved.length > MAX_VISUAL_FEEDBACK_BYTES || (!isPng(resolved) && !isJpeg(resolved))) {
      throw new BuilderProcessPolicyError(
        "BUILDER_VISUAL_FEEDBACK_INVALID",
        "Resolved visual feedback must be bounded validated PNG or JPEG bytes",
      );
    }
    return Object.freeze({ ref, bytes: Buffer.from(resolved) });
  }));
}

module.exports = {
  BUILDER_AUTH_ROOT,
  BUILDER_HOME,
  BuilderProcessPolicyError,
  CLAUDE_HOST_TOOLS,
  CLAUDE_SCOPED_TOOLS,
  SCOPED_SIMWORLD_TOOLS,
  assertSafeBuilderArgv,
  assertScopedMcpConfig,
  buildClaudeSafetyArgs,
  buildClaudeToolFreeSafetyArgs,
  buildCodexSafetyArgs,
  buildMinimalBuilderEnv,
  buildMinimalToolFreeEnv,
  assertSafeToolFreeClaudeArgv,
  resolveVisualFeedbackImages,
};

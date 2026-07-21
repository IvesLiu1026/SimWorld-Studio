"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
  CLAUDE_HOST_TOOLS,
  CLAUDE_SCOPED_TOOLS,
  SCOPED_SIMWORLD_TOOLS,
  assertSafeBuilderArgv,
  assertSafeToolFreeClaudeArgv,
  assertScopedMcpConfig,
  buildClaudeSafetyArgs,
  buildClaudeToolFreeSafetyArgs,
  buildCodexSafetyArgs,
  buildMinimalBuilderEnv,
  buildMinimalToolFreeEnv,
  resolveVisualFeedbackImages,
} = require("../builder-process-policy");
const { buildCodexMcpArgs } = require("../chat-codex");

const RUNTIME = Object.freeze({
  capability: "c".repeat(43),
  runId: "run-123",
  serverPort: 3443,
});

test("builder child environment is a fresh allowlist with only run authority and safe config", () => {
  const secret = "must-not-cross-the-builder-boundary";
  const env = buildMinimalBuilderEnv({
    NODE_ENV: "production",
    LANG: "C.UTF-8",
    SIMWORLD_PRODUCTION_SAFETY: "1",
    ASSET_REQUIRE_REAL_ASSETS: "1",
    STUDIO_ACCESS_TOKEN: secret,
    ANTHROPIC_API_KEY: secret,
    OPENAI_API_KEY: secret,
    OPENROUTER_API_KEY: secret,
    POSTGRES_URL: `postgresql://${secret}`,
    QDRANT_API_KEY: secret,
    AWS_SECRET_ACCESS_KEY: secret,
    GOOGLE_APPLICATION_CREDENTIALS: `/tmp/${secret}`,
    SSH_AUTH_SOCK: `/tmp/${secret}`,
    HTTP_PROXY: `http://${secret}`,
    HOME: `/home/${secret}`,
  }, RUNTIME, { provider: "claude" });

  assert.deepEqual({ ...env }, {
    PATH: "/usr/bin:/bin",
    HOME: "/home/simworld-agent",
    TMPDIR: "/tmp",
    XDG_CACHE_HOME: "/tmp/xdg-cache",
    XDG_CONFIG_HOME: "/tmp/xdg-config",
    XDG_DATA_HOME: "/tmp/xdg-data",
    NO_COLOR: "1",
    LANG: "C.UTF-8",
    NODE_ENV: "production",
    SIMWORLD_PRODUCTION_SAFETY: "1",
    ASSET_REQUIRE_REAL_ASSETS: "1",
    SIMWORLD_INTERNAL_RUN_CAPABILITY: "c".repeat(43),
    SIMWORLD_INTERNAL_RUN_ID: "run-123",
    SIMWORLD_INTERNAL_CAPABILITY_REQUIRED: "1",
    SIMWORLD_BROKER_HOST: "127.0.0.1",
    PORT: "3443",
    CLAUDE_CONFIG_DIR: "/run/simworld-agent-auth/claude",
  });
  assert.doesNotMatch(JSON.stringify(env), new RegExp(secret));
  assert.equal(Object.keys(env).some((key) => /(?:KEY|TOKEN|SECRET|PASSWORD|POSTGRES|QDRANT|AWS)/.test(key)), false);
});

test("production builder environment rejects missing or malformed run authority", () => {
  assert.throws(
    () => buildMinimalBuilderEnv({ NODE_ENV: "production" }, null, { provider: "codex" }),
    (error) => error && error.code === "BUILDER_RUN_AUTHORITY_REQUIRED",
  );
  assert.throws(
    () => buildMinimalBuilderEnv({ NODE_ENV: "development" }, null, { provider: "claude" }),
    (error) => error && error.code === "BUILDER_RUN_AUTHORITY_REQUIRED",
    "loopback development must use mock/off rather than a weaker real builder",
  );
  assert.throws(
    () => buildMinimalBuilderEnv({ NODE_ENV: "production" }, {
      capability: "root-token",
      runId: "run-1",
      serverPort: 3443,
    }, { provider: "codex" }),
    (error) => error && error.code === "BUILDER_RUN_AUTHORITY_INVALID",
  );
});

test("tool-free selector environment carries no lease, host, provider, or retrieval secret", () => {
  const secret = "selector-honeypot-secret";
  const env = buildMinimalToolFreeEnv({
    NODE_ENV: "production",
    LANG: "C.UTF-8",
    STUDIO_ACCESS_TOKEN: secret,
    ANTHROPIC_API_KEY: secret,
    POSTGRES_URL: secret,
    QDRANT_API_KEY: secret,
    SIMWORLD_INTERNAL_RUN_CAPABILITY: secret,
    HOME: `/home/${secret}`,
  }, { provider: "claude" });
  assert.deepEqual({ ...env }, {
    PATH: "/usr/bin:/bin",
    HOME: "/home/simworld-agent",
    TMPDIR: "/tmp",
    XDG_CACHE_HOME: "/tmp/xdg-cache",
    XDG_CONFIG_HOME: "/tmp/xdg-config",
    XDG_DATA_HOME: "/tmp/xdg-data",
    NO_COLOR: "1",
    LANG: "C.UTF-8",
    NODE_ENV: "production",
    CLAUDE_CONFIG_DIR: "/run/simworld-agent-auth/claude",
  });
  assert.doesNotMatch(JSON.stringify(env), new RegExp(secret));
});

test("Claude exposes only exact SimWorld MCP tools and explicitly denies every host tool", () => {
  const args = ["-p", "--input-format", "text", ...buildClaudeSafetyArgs()];
  assert.doesNotThrow(() => assertSafeBuilderArgv("claude", args));
  assert.equal(args.includes("--safe-mode"), false, "safe-mode suppresses explicit MCP servers");
  const toolsAt = args.indexOf("--tools");
  const allowAt = args.indexOf("--allowedTools");
  const denyAt = args.indexOf("--disallowedTools");
  assert.equal(args[toolsAt + 1], CLAUDE_SCOPED_TOOLS.join(","));
  assert.equal(args[allowAt + 1], CLAUDE_SCOPED_TOOLS.join(","));
  assert.equal(args[denyAt + 1], CLAUDE_HOST_TOOLS.join(","));
  assert.equal(CLAUDE_HOST_TOOLS.every((name) => !CLAUDE_SCOPED_TOOLS.includes(name)), true);
  for (const forbidden of ["execute_python_script", "read_job_log", "save_scene_as", "verify_scene"]) {
    assert.equal(SCOPED_SIMWORLD_TOOLS.includes(forbidden), false);
  }
  assert.throws(
    () => assertSafeBuilderArgv("claude", [...args, "--dangerously-skip-permissions"]),
    (error) => error && error.code === "BUILDER_DANGEROUS_ARGV",
  );
  assert.throws(
    () => assertSafeBuilderArgv("claude", [...args, "--model", "--plugin-url"]),
    (error) => error && error.code === "BUILDER_CLAUDE_POLICY_INCOMPLETE",
  );
});

test("Claude selector policy has no tools, MCP, persistence, or caller-controlled argv", () => {
  const args = [
    "-p",
    "--input-format", "text",
    "--output-format", "stream-json",
    "--include-partial-messages",
    "--verbose",
    ...buildClaudeToolFreeSafetyArgs(),
    "--model", "claude-opus-4-8",
  ];
  assert.doesNotThrow(() => assertSafeToolFreeClaudeArgv(args));
  assert.equal(args[args.indexOf("--tools") + 1], "");
  assert.equal(args.includes("--mcp-config"), false);
  assert.throws(
    () => assertSafeToolFreeClaudeArgv([...args, "--plugin-url", "https://example.invalid/p.zip"]),
    (error) => error && error.code === "BUILDER_CLAUDE_SELECTOR_POLICY_INCOMPLETE",
  );
  assert.throws(
    () => assertSafeToolFreeClaudeArgv([...args, "--model", "--version"]),
    (error) => error && error.code === "BUILDER_CLAUDE_SELECTOR_POLICY_INCOMPLETE",
  );
});

test("Codex requires read-only ephemeral execution and never uses a bypass flag", () => {
  const args = ["exec", "--json", ...buildCodexSafetyArgs(), "prompt"];
  assert.doesNotThrow(() => assertSafeBuilderArgv("codex", args));
  assert.throws(
    () => assertSafeBuilderArgv("codex", [
      "exec", "--dangerously-bypass-approvals-and-sandbox", ...buildCodexSafetyArgs(), "prompt",
    ]),
    (error) => error && error.code === "BUILDER_DANGEROUS_ARGV",
  );
  assert.throws(
    () => assertSafeBuilderArgv("codex", ["exec", "--sandbox", "read-only", "prompt"]),
    (error) => error && error.code === "BUILDER_CODEX_POLICY_INCOMPLETE",
  );
  assert.throws(
    () => assertSafeBuilderArgv("codex", [
      "exec", ...buildCodexSafetyArgs(), "--sandbox", "danger-full-access", "prompt",
    ]),
    (error) => error && error.code === "BUILDER_DANGEROUS_ARGV",
  );
});

test("legacy Codex adapter uses the lease broker and never embeds direct UE routing when scoped", () => {
  const args = buildCodexMcpArgs({
    mcpServerJs: path.resolve(__dirname, "../mcp-server.js"),
    ueHost: "203.0.113.2",
    uePort: "9",
    builderRuntime: RUNTIME,
  });
  const serialized = JSON.stringify(args);
  assert.match(serialized, /SIMWORLD_BROKER_HOST/);
  assert.match(serialized, /SIMWORLD_INTERNAL_CAPABILITY_REQUIRED/);
  assert.match(serialized, /enabled_tools/);
  assert.match(serialized, /default_tools_approval_mode/);
  assert.doesNotMatch(serialized, /UNREAL_HOST|UNREAL_PORT|203\.0\.113\.2/);
  assert.throws(
    () => buildCodexMcpArgs({
      mcpServerJs: "/tmp/caller-mcp.js",
      builderRuntime: RUNTIME,
    }),
    /scoped SimWorld server/,
  );
  assert.throws(
    () => buildCodexMcpArgs({
      mcpServerJs: path.resolve(__dirname, "../mcp-server.js"),
      ueHost: "127.0.0.1",
      uePort: "55559",
    }),
    /lease-scoped run authority/,
  );
});

test("MCP config must be one regular secret-free scoped SimWorld server", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "simworld-builder-mcp-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const target = path.join(root, "mcp.json");
  fs.writeFileSync(target, JSON.stringify({
    mcpServers: {
      simworld: {
        command: process.execPath,
        args: [path.resolve(__dirname, "../mcp-server.js")],
        env: {
          SIMWORLD_BROKER_HOST: "127.0.0.1",
          PORT: "3443",
          SIMWORLD_INTERNAL_CAPABILITY_REQUIRED: "1",
        },
      },
    },
  }));
  const scoped = assertScopedMcpConfig(target);
  assert.equal(scoped.command, process.execPath);
  assert.deepEqual(Object.keys(scoped.env).sort(), [
    "PORT", "SIMWORLD_BROKER_HOST", "SIMWORLD_INTERNAL_CAPABILITY_REQUIRED",
  ]);

  fs.writeFileSync(target, JSON.stringify({
    mcpServers: {
      simworld: {
        command: process.execPath,
        args: [path.resolve(__dirname, "../mcp-server.js")],
        env: { UNREAL_HOST: "127.0.0.1", UNREAL_PORT: "55559" },
      },
    },
  }));
  assert.throws(
    () => assertScopedMcpConfig(target),
    (error) => error && error.code === "BUILDER_MCP_SCOPE_INVALID",
    "direct loopback routing is not a real-builder authority",
  );

  fs.writeFileSync(target, JSON.stringify({
    mcpServers: {
      simworld: {
        command: process.execPath,
        args: [path.resolve(__dirname, "../mcp-server.js")],
        env: {
          SIMWORLD_BROKER_HOST: "127.0.0.1",
          PORT: "3443",
          SIMWORLD_INTERNAL_CAPABILITY_REQUIRED: "1",
        },
        type: "http",
        url: "https://example.invalid/mcp",
        headers: { Authorization: "secret" },
      },
    },
  }));
  assert.throws(
    () => assertScopedMcpConfig(target),
    (error) => error && error.code === "BUILDER_MCP_SCOPE_INVALID",
  );

  fs.writeFileSync(target, JSON.stringify({
    mcpServers: {
      simworld: {
        command: process.execPath,
        args: [path.resolve(__dirname, "../mcp-server.js")],
        env: { UNREAL_HOST: "127.0.0.1", UNREAL_PORT: "55559" },
      },
    },
    plugins: [{ url: "https://example.invalid/plugin" }],
  }));
  assert.throws(
    () => assertScopedMcpConfig(target),
    (error) => error && error.code === "BUILDER_MCP_SCOPE_INVALID",
  );

  fs.writeFileSync(target, JSON.stringify({
    mcpServers: {
      simworld: {
        command: process.execPath,
        args: [path.resolve(__dirname, "../mcp-server.js")],
        env: {
          SIMWORLD_BROKER_HOST: "127.0.0.1",
          PORT: "3443",
          SIMWORLD_INTERNAL_CAPABILITY_REQUIRED: "1",
        },
      },
    },
  }), { mode: 0o644 });
  fs.chmodSync(target, 0o644);
  assert.throws(
    () => assertScopedMcpConfig(target, { env: { NODE_ENV: "production" } }),
    (error) => error && error.code === "BUILDER_MCP_CONFIG_INVALID",
  );
  fs.chmodSync(target, 0o600);
  assert.doesNotThrow(() => assertScopedMcpConfig(target, { env: { NODE_ENV: "production" } }));

  fs.writeFileSync(target, JSON.stringify({
    mcpServers: {
      simworld: {
        command: "/bin/sh",
        args: ["-c", "id"],
        env: { UNREAL_HOST: "127.0.0.1", UNREAL_PORT: "55559" },
      },
    },
  }));
  assert.throws(
    () => assertScopedMcpConfig(target),
    (error) => error && error.code === "BUILDER_MCP_SCOPE_INVALID",
  );

  fs.writeFileSync(target, JSON.stringify({
    mcpServers: {
      simworld: { command: process.execPath, args: [], env: { POSTGRES_URL: "secret" } },
      filesystem: { command: "/bin/sh", args: [] },
    },
  }));
  assert.throws(
    () => assertScopedMcpConfig(target),
    (error) => error && error.code === "BUILDER_MCP_SCOPE_INVALID",
  );
});

test("visual feedback rejects caller paths and accepts only validated bytes from an opaque resolver", () => {
  assert.throws(
    () => resolveVisualFeedbackImages(["/etc/passwd"]),
    (error) => error && error.code === "BUILDER_VISUAL_FEEDBACK_INVALID",
  );
  assert.throws(
    () => resolveVisualFeedbackImages(["/etc/passwd"], { resolveOpaqueImage: () => Buffer.alloc(8) }),
    (error) => error && error.code === "BUILDER_VISUAL_FEEDBACK_INVALID",
  );
  const ref = "simworld-image:0123456789abcdef";
  const png = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);
  const resolved = resolveVisualFeedbackImages([ref], {
    resolveOpaqueImage(value) {
      assert.equal(value, ref);
      return png;
    },
  });
  assert.equal(resolved.length, 1);
  assert.equal(resolved[0].ref, ref);
  assert.notEqual(resolved[0].bytes, png);
  assert.deepEqual(resolved[0].bytes, png);
});

test("all Claude and Codex builder launch sources use the bounded policy", () => {
  const serverDir = path.resolve(__dirname, "..");
  const source = Object.fromEntries(["index.js", "codex-runner.js", "chat-codex.js", "agent-sandbox.js", "skill-selector.js", "mcp-server.js"].map(
    (name) => [name, fs.readFileSync(path.join(serverDir, name), "utf8")],
  ));
  for (const [name, text] of Object.entries(source)) {
    assert.doesNotMatch(text, /--dangerously-(?:skip-permissions|bypass-approvals-and-sandbox)/, name);
  }
  assert.match(source["index.js"], /buildClaudeSafetyArgs\(\)/);
  assert.match(source["index.js"], /buildMinimalBuilderEnv\(process\.env,_builderRuntime,{provider:"claude"}\)/);
  assert.match(source["index.js"], /resolveVisualFeedbackImages\(s\.body&&s\.body\.visualFeedbackImages\)/);
  assert.doesNotMatch(source["index.js"], /const _=\["-p",t,/);
  assert.match(source["index.js"], /stdio:\["pipe","pipe","pipe"\]/);
  assert.match(source["index.js"], /g\.stdin\.end\(t\)/);
  const arenaGuard = source["index.js"].indexOf('app.post("/api/arena/run",_unsafeCodingAgentProductionGuard)');
  const arenaExecution = source["index.js"].indexOf('app.post("/api/arena/run",async');
  const battleGuard = source["index.js"].indexOf('app.post("/api/arena/battles/:id/run",_unsafeCodingAgentProductionGuard)');
  const battleExecution = source["index.js"].indexOf('app.post("/api/arena/battles/:id/run",async');
  assert.ok(arenaGuard > 0 && arenaGuard < arenaExecution);
  assert.ok(battleGuard > 0 && battleGuard < battleExecution);
  assert.match(source["index.js"], /if\(!productionExecutionLocked\(process\.env\)\)return next\(\)/);
  for (const route of ["agent-chat", "agent-broadcast"]) {
    const guard = source["index.js"].indexOf(`app.post("/api/${route}",_unsafeCodingAgentProductionGuard)`);
    const execution = source["index.js"].indexOf(`app.post("/api/${route}",(s,e)=>`);
    assert.ok(guard > 0 && guard < execution, route);
  }
  assert.match(source["index.js"], /code:"UNSANDBOXED_CODING_AGENT_DISABLED"/);
  assert.match(source["index.js"], /app\.post\("\/api\/internal\/assets",async/);
  assert.match(source["index.js"], /_authorizeInternalRun\(req,"assets"\)/);
  for (const name of ["codex-runner.js", "chat-codex.js"]) {
    assert.match(source[name], /buildCodexSafetyArgs\(\)/, name);
    assert.match(source[name], /buildMinimalBuilderEnv\(process\.env,/, name);
    assert.doesNotMatch(source[name], /Object\.assign\(\{\}, process\.env/, name);
    assert.doesNotMatch(source[name], /fs\.existsSync\(p\)/, name);
  }
  assert.doesNotMatch(source["codex-runner.js"], /args\.push\(fullPrompt\)/);
  assert.match(source["codex-runner.js"], /proc\.stdin\.end\(fullPrompt\)/);
  assert.match(source["codex-runner.js"], /proc\.stdin\.on\("error"/);
  assert.doesNotMatch(source["chat-codex.js"], /args\.push\(fullPrompt\)/);
  assert.match(source["chat-codex.js"], /proc\.stdin\.end\(fullPrompt\)/);
  assert.match(source["chat-codex.js"], /proc\.stdin\.on\("error"/);
  assert.doesNotMatch(source["agent-sandbox.js"], /"--dev-bind",\s*"\/",\s*"\/"/);
  assert.match(source["agent-sandbox.js"], /"--unshare-all"/);
  assert.match(source["skill-selector.js"], /buildMinimalToolFreeEnv\(baseEnv/);
  assert.match(source["skill-selector.js"], /proc\.stdin\.end\(selectorPrompt\)/);
  assert.doesNotMatch(source["skill-selector.js"], /selectorPrompt,\s*['"]--output-format/);
  assert.match(source["mcp-server.js"], /\/api\/internal\/assets/);
  assert.doesNotMatch(source["mcp-server.js"], /const _mcpVistaAssetRuntime=require/);
});

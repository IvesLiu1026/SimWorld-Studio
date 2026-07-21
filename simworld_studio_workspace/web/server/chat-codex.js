"use strict";
// Codex-CLI based chat handler — drop-in alternative to the inline Claude chat path in index.js.
// Uses `codex exec --json -m gpt-5.5 ...` to run an OpenAI-side agent against the same MCP server
// (mcp-server.js) the Claude path uses. Emits the same SSE events the frontend + loop wrappers
// expect (system, text, tool_start, tool_input, tool_details, tool_result, screenshot, done).
//
// Stream pattern mirrors the exp9_codex example at /data/siddhant/static_scene_eval/exp9_codex/.
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");
const {
  SCOPED_SIMWORLD_TOOLS,
  assertSafeBuilderArgv,
  buildCodexSafetyArgs,
  buildMinimalBuilderEnv,
  resolveVisualFeedbackImages,
} = require("./builder-process-policy");
const { SANDBOX_WORKDIR, sandboxedSpawn } = require("./agent-sandbox");
const { attachBuilderRuntimeProcess } = require("./builder-runtime-authority");

const NL = String.fromCharCode(10);
const CODEX_BIN = process.env.CODEX_BIN || "codex";
const CODEX_MODEL = process.env.CODEX_MODEL || "gpt-5.5";
const CODEX_TIMEOUT_MS = parseInt(process.env.CODEX_TIMEOUT_MS || "900000", 10); // 15 min default
const MCP_SERVER_JS = path.resolve(__dirname, "mcp-server.js");

function tomlVal(value) {
  if (Array.isArray(value)) return `[${value.map(tomlVal).join(",")}]`;
  if (typeof value === "boolean") return value ? "true" : "false";
  return JSON.stringify(String(value));
}

// MCP config passed to Codex so it can call the same SimWorld MCP tools (spawn_blueprint_actor, etc.)
function buildCodexMcpArgs(opts) {
  const mcpServerJs = path.resolve(String(opts.mcpServerJs || ""));
  if (mcpServerJs !== MCP_SERVER_JS) {
    throw new Error("Codex builder MCP script must be the scoped SimWorld server");
  }
  const runtime = opts.builderRuntime || null;
  if (!runtime) throw new Error("Codex builder requires a lease-scoped run authority");
  const configArgs = [
    "-c", `mcp_servers.simworld.command=${tomlVal(process.execPath)}`,
    "-c", `mcp_servers.simworld.args=${tomlVal([mcpServerJs])}`,
    "-c", "mcp_servers.simworld.required=true",
    "-c", `mcp_servers.simworld.enabled_tools=${tomlVal(SCOPED_SIMWORLD_TOOLS)}`,
    "-c", 'mcp_servers.simworld.default_tools_approval_mode="approve"',
  ];
  const port = Number(runtime.serverPort);
  if (!Number.isSafeInteger(port) || port < 1 || port > 65535) {
    throw new Error("Codex builder broker port is invalid");
  }
  configArgs.push(
    "-c", 'mcp_servers.simworld.env.SIMWORLD_BROKER_HOST="127.0.0.1"',
    "-c", `mcp_servers.simworld.env.PORT=${tomlVal(port)}`,
    "-c", 'mcp_servers.simworld.env.SIMWORLD_INTERNAL_CAPABILITY_REQUIRED="1"',
  );
  return configArgs;
}

// Build the system + task prompt the same way the Claude path does (ARENA_SYSTEM_PROMPT + skills + feedback),
// then concatenate as one prompt sent over stdin to Codex.
function buildPrompt(deps, body, options) {
  let systemPrompt = options.arenaSystemPrompt || "";
  const sceneContext = (options.sceneContext || "").trim();
  if (sceneContext) systemPrompt += "\n\n" + sceneContext;
  if (body.feedback) {
    systemPrompt += `\n\n## USER FEEDBACK ON CURRENT SCENE\nThe user is providing feedback on the current scene. Modify the scene based on this feedback. Do NOT start from scratch — refine what exists.\nFeedback: ${body.feedback}`;
  }
  const assetPromptBlock = (options.assetPromptBlock || "").trim();
  if (assetPromptBlock) systemPrompt += "\n\n" + assetPromptBlock;
  return `${systemPrompt}\n\n## TASK\n${body.message}`;
}

// Parse one JSON event from Codex's stream. Codex emits one JSON object per line.
// Returns a normalized event payload or null.
function normalizeCodexEvent(line) {
  if (!line || !line.trim()) return null;
  let ev;
  try { ev = JSON.parse(line); } catch { return null; }
  const typ = ev.type;
  let payload = null;
  if (ev.payload && typeof ev.payload === "object") payload = ev.payload;
  else if (ev.item && typeof ev.item === "object") payload = ev.item;
  else payload = ev;
  return { typ, payload, raw: ev };
}

// Spawn Codex with the right MCP config + stream events into SSE.
// Mirrors the per-request side effects the Claude path has:
//   - ctxManager.addActor / removeActor / setEnvironmentReady on spawn/delete/setup_environment success
//   - latestScreenshot extraction from take_screenshot results
//   - logToFile audit lines
function spawnCodexAgent(deps, body, options, emit, onDone) {
  const sessionId = body.sessionId || options.studioSession || "_global";
  const fullPrompt = buildPrompt(deps, body, options);
  resolveVisualFeedbackImages(body.visualFeedbackImages);
  const runtime = options.builderRuntime || deps && deps.BUILDER_RUNTIME || null;
  if (!/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/.test(CODEX_MODEL)) {
    throw new Error("Codex builder model identifier is invalid");
  }
  const reasoningEffort = String(process.env.CODEX_BUILDER_REASONING_EFFORT || "high").trim();
  if (!new Set(["minimal", "low", "medium", "high", "xhigh"]).has(reasoningEffort)) {
    throw new Error("Codex builder reasoning effort is invalid");
  }
  const env = buildMinimalBuilderEnv(process.env, runtime, { provider: "codex" });

  const args = [
    "exec",
    "--json",
    "--skip-git-repo-check",
    ...buildCodexSafetyArgs(),
    "-C", SANDBOX_WORKDIR,
    "-m", CODEX_MODEL,
    "-c", `model_reasoning_effort=${reasoningEffort}`,
    ...buildCodexMcpArgs({
      mcpServerJs: options.mcpServerJs,
      uePort: options.uePort,
      ueHost: options.ueHost,
      builderRuntime: runtime,
    }),
  ];
  args.push("-");
  assertSafeBuilderArgv("codex", args);

  const log = options.logToFile || (() => {});
  log("codex", `[builder] model=${CODEX_MODEL} sessionId=${sessionId} prompt_chars=${fullPrompt.length} images=0`);

  const sandbox = sandboxedSpawn(CODEX_BIN, args, null, { env: process.env, provider: "codex" });
  const proc = spawn(sandbox.cmd, sandbox.args, {
    cwd: "/",
    env,
    stdio: ["pipe", "pipe", "pipe"],
  });
  attachBuilderRuntimeProcess(runtime, proc);
  const processMap = deps && deps._chatProcs;
  const processKey = runtime ? runtime.scopeId : sessionId;
  const clearTrackedProcess = () => {
    if (processMap && processMap.get(processKey) === proc) processMap.delete(processKey);
  };
  if (processMap && typeof processMap.get === "function" && typeof processMap.set === "function") {
    const prior = processMap.get(processKey);
    if (prior && prior !== proc && !prior.killed) {
      try { prior.kill("SIGTERM"); } catch (_error) {}
    }
    processMap.set(processKey, proc);
    proc.on("exit", clearTrackedProcess);
  }

  let stdoutBuf = "";
  let stderrBuf = "";
  let isError = false;
  let latestScreenshot = null;
  let finalText = "";
  let _usage = null;
  let completed = false;
  const _t0 = Date.now();
  const toolInputs = new Map();      // tool_use_id -> { name, input }  for ctxManager hooks
  const startedTools = new Set();
  const ctx = deps && deps.ctxManager;
  const studio = options.studioSession;

  // Watchdog: kill if no output for N seconds
  let lastOutputAt = Date.now();
  const idleTimer = setInterval(() => {
    if (Date.now() - lastOutputAt > 300000) {
      log("codex", `[builder] idle 300s, killing`);
      try { proc.kill("SIGTERM"); } catch (_e) {}
      clearInterval(idleTimer);
    }
  }, 15000);

  proc.stdout.on("data", (d) => {
    lastOutputAt = Date.now();
    stdoutBuf += d.toString();
    const lines = stdoutBuf.split(NL);
    stdoutBuf = lines.pop() || "";
    for (const line of lines) {
      handleCodexLine(line);
    }
  });
  proc.stderr.on("data", (d) => {
    lastOutputAt = Date.now();
    const s = d.toString();
    stderrBuf += s;
    log("codex_err", s.slice(0, 300));
  });

  function handleCodexLine(line) {
    const norm = normalizeCodexEvent(line);
    if (!norm) return;
    const { typ, payload } = norm;
    const ptype = payload.type || typ;

    // ── thread + turn lifecycle ──
    if (typ === "thread.started") {
      emit("system", { sessionId: studio, mcpServers: [{ name: "simworld", status: "connected" }], thread_id: payload.thread_id });
      try { if (ctx && studio) { ctx.resolveSession(studio); ctx.beginRound(studio); } } catch (_e) {}
      return;
    }
    if (typ === "turn.started" || ptype === "turn.started") return;

    // ── MCP tool call lifecycle ──
    if (ptype === "mcp_tool_call") {
      const id = payload.id || payload.call_id || `codex-${Date.now()}`;
      const server = payload.server || "";
      const tool = payload.tool || payload.name || "";
      const fullName = server && tool ? `mcp__${server}__${tool}` : (tool || "unknown");
      const displayName = tool.replace(/^mcp__[^_]+__/, "") || fullName;
      const input = payload.arguments || payload.input || {};

      if (typ === "item.started") {
        if (!startedTools.has(id)) {
          startedTools.add(id);
          emit("tool_start", { id, name: fullName, displayName });
          emit("tool_details", { id, name: fullName, displayName, input });
          // Capture for ctxManager hooks
          if (["spawn_blueprint_actor","spawn_actor","spawn_agent","delete_actor","delete_all_spawned","setup_environment"].includes(displayName)) {
            toolInputs.set(id, { name: displayName, input });
          }
          log("tool", `[codex] starting ${fullName}`);
        }
        return;
      }
      if (typ === "item.completed") {
        const result = payload.result;
        let resultText = "";
        if (result !== undefined) {
          try { resultText = typeof result === "string" ? result : JSON.stringify(result); } catch { resultText = String(result); }
        }
        const errInResult = /"status"\s*:\s*"error"/.test(resultText);
        if (errInResult) isError = true;

        // Screenshot extraction (mimics Claude path)
        const m = resultText.match(/([\/][\w\/\-._]+\.png)/);
        if (m && fs.existsSync(m[1])) {
          latestScreenshot = m[1];
          emit("screenshot", { toolUseId: id, filepath: `/api/screenshot/file?path=${encodeURIComponent(latestScreenshot)}` });
        }
        emit("tool_result", { toolUseId: id, result: resultText.slice(0, 2000), isError: errInResult });
        log("tool_result", `[codex] ${id.slice(0,8)} → ${resultText.slice(0,200)}`);

        // ctxManager hooks for scene state
        const st = toolInputs.get(id);
        if (st && !errInResult && ctx && studio) {
          try {
            if (st.name === "spawn_blueprint_actor" || st.name === "spawn_actor" || st.name === "spawn_agent") {
              const an = st.input.actor_name || st.input.agent_name || st.input.name;
              const cls = st.input.blueprint_id || st.input.static_mesh || st.input.agent_type || "";
              const cat = st.name === "spawn_agent" ? "agent" : undefined;
              if (an) ctx.addActor(studio, { name: an, cls, category: cat, location: st.input.location });
            } else if (st.name === "delete_actor") {
              ctx.removeActor(studio, st.input.name);
            } else if (st.name === "delete_all_spawned") {
              ctx.clearAllSpawned(studio);
            } else if (st.name === "setup_environment") {
              ctx.setEnvironmentReady(studio);
            }
          } catch (e) {
            log("codex_ctx", `error: ${e.message}`);
          }
          toolInputs.delete(id);
        }
        return;
      }
    }

    // ── function_call (non-MCP function call, e.g., codex internal) ──
    if (ptype === "function_call") {
      const id = payload.call_id || payload.id || `codex-fn-${Date.now()}`;
      const name = payload.name || "function_call";
      let input = payload.arguments || {};
      if (typeof input === "string") {
        try { input = JSON.parse(input); } catch { input = { raw: input }; }
      }
      if (typ === "item.completed") {
        emit("tool_start", { id, name, displayName: name });
        emit("tool_details", { id, name, displayName: name, input });
      }
      return;
    }
    if (ptype === "function_call_output") {
      const id = payload.call_id || `codex-fn-${Date.now()}`;
      const out = payload.output || "";
      emit("tool_result", { toolUseId: id, result: String(out).slice(0, 2000), isError: false });
      return;
    }

    // ── Agent messages (text output) ──
    if (ptype === "agent_message" || ptype === "message") {
      if (typ === "item.completed") {
        const text = payload.text || (Array.isArray(payload.content)
          ? payload.content.filter((c) => c && (c.type === "output_text" || c.type === "text")).map((c) => c.text || "").join("")
          : "");
        if (text) {
          finalText = text;
          emit("text", { delta: text + "\n" });
        }
      } else if (typ === "item.delta" || ptype === "output_text_delta") {
        const text = payload.text || payload.delta || "";
        if (text) emit("text", { delta: text });
      }
      return;
    }

    // ── Agent reasoning (Codex internal thinking; surface as text for visibility) ──
    if (typ === "event_msg" && payload.type === "agent_reasoning_delta") {
      // optional: surface as thinking. Skipped to keep stream clean.
      return;
    }

    // ── Errors ──
    if (ptype === "error" || ptype === "turn_failed" || typ === "error") {
      isError = true;
      log("codex_err", `event: ${JSON.stringify(payload).slice(0,300)}`);
      return;
    }

    // ── Turn completed → emit done after process exits, not here ──
    if (typ === "turn.completed") { try { _usage = payload.usage || (payload.payload && payload.payload.usage) || _usage; } catch (_e) {} return; }
  }

  proc.on("close", (code) => {
    clearInterval(idleTimer);
    if (stdoutBuf.trim()) handleCodexLine(stdoutBuf);
    const exitErr = code !== 0;
    if (exitErr) isError = true;
    try { require("./telemetry").record({ component: "builder", model: CODEX_MODEL, reasoning: process.env.CODEX_BUILDER_REASONING_EFFORT || "high", durationMs: Date.now() - _t0, usage: require("./telemetry").normUsage(_usage) }); } catch (_e) {}
    log("codex", `[builder] exited code=${code} isError=${isError} stderr_tail=${stderrBuf.slice(-200)}`);
    if (!completed) {
      completed = true;
      onDone({ isError, latestScreenshot, finalText, returnCode: code });
    }
  });
  proc.on("error", (e) => {
    clearInterval(idleTimer);
    clearTrackedProcess();
    log("codex", `[builder] proc error: ${e.message}`);
    isError = true;
    if (!completed) {
      completed = true;
      onDone({ isError, latestScreenshot, finalText, returnCode: -1, error: e.message });
    }
  });

  proc.stdin.on("error", (error) => {
    stderrBuf += `stdin: ${error.message}\n`;
    log("codex", `[builder] stdin error: ${error.message}`);
    try { proc.kill("SIGTERM"); } catch (_error) {}
  });
  try {
    proc.stdin.end(fullPrompt);
  } catch (error) {
    stderrBuf += `stdin: ${error.message}\n`;
    log("codex", `[builder] stdin write failed: ${error.message}`);
    completed = true;
    try { proc.kill("SIGTERM"); } catch (_error) {}
    throw error;
  }

  return proc;
}

// Express handler: /api/chat when {runner: 'codex'} or env LLM_PROVIDER=codex.
// SSE-streams the same event types the Claude path does, so the loop wrappers can run unchanged.
async function handleCodexChat(req, res, deps) {
  const body = req.body || {};
  const { message, sessionId, skills, feedback } = body;
  if (!message) { res.status(400).json({ error: "message required" }); return; }

  res.setHeader("Content-Type", "text/event-stream");
  res.setHeader("Cache-Control", "no-cache");
  res.setHeader("Connection", "keep-alive");
  res.setHeader("X-Accel-Buffering", "no");
  res.flushHeaders();

  const emit = (name, data) => {
    if (!res.writableEnded) res.write(`event: ${name}\ndata: ${JSON.stringify(data || {})}\n\n`);
  };
  const ping = setInterval(() => { if (!res.writableEnded) res.write(`: ping\n\n`); }, 5000);

  // Build the scene-context snippet (same as Claude path)
  let sceneContext = "";
  try {
    if (deps.ctxManager && deps.studioSession) {
      sceneContext = deps.ctxManager.renderForPrompt(deps.studioSession) || "";
    }
  } catch (_e) {}

  // Optional active-skills doc block (matches Claude path)
  let arenaPrompt = deps.arenaSystemPrompt || "";
  if (skills && skills.length > 0 && deps.skillRegistry) {
    try {
      const d = deps.skillRegistry.compose(skills);
      if (d) arenaPrompt += `\n\n## ACTIVE SKILLS (reference documentation)\n` + d;
    } catch (_e) {}
  }

  let assetPromptBlock = "";
  let assetPolicy = null;
  try {
    const assetRetrieval = require("./asset-retrieval");
    assetPolicy = assetRetrieval.resolveAssetPolicy(body);
    if (!deps.MOCK_MODE) {
      emit("retrieval", { phase: "start", mode: assetPolicy.mode });
      const model = require("./model-config").resolveModel(body);
      const result = await assetRetrieval.buildPromptBlockWithPolicy(message, assetPolicy, {
        model,
        provider: "codex",
        runner: "codex",
        log: (x) => deps.logToFile && deps.logToFile("retrieval", x),
      });
      assetPromptBlock = result.promptBlock;
      const chars = assetPromptBlock ? assetPromptBlock.length : 0;
      if (deps.logToFile) {
        deps.logToFile("retrieval", JSON.stringify({
          status: result.metadata.status,
          mode: result.metadata.mode,
          chars,
          runner: "codex",
        }));
      }
      emit("retrieval", { phase: "done", chars, ...result.metadata });
    }
  } catch (err) {
    const assetRetrieval = require("./asset-retrieval");
    const metadata = assetPolicy
      ? assetRetrieval.assetFailureDecision(err, assetPolicy).metadata
      : {
        status: "blocked",
        code: err && err.code || "ASSET_RETRIEVAL_POLICY_INVALID",
        message: "Asset retrieval policy is invalid.",
      };
    const code = metadata.reason && metadata.reason.code || metadata.code;
    if (deps.logToFile) deps.logToFile("retrieval", JSON.stringify({ status: "blocked", code }));
    emit("retrieval", { phase: "error", ...metadata });
    emit("text", { delta: `\n\nAsset retrieval blocked the build: ${code}\n` });
    emit("done", {
      sessionId: deps.studioSession,
      isError: true,
      runner: "codex",
      latestScreenshot: null,
      retrieval: metadata,
    });
    clearInterval(ping);
    res.end();
    return;
  }

  const options = {
    arenaSystemPrompt: arenaPrompt,
    sceneContext,
    assetPromptBlock,
    studioSession: deps.studioSession,
    logToFile: deps.logToFile,
    mcpServerJs: deps.mcpServerJs || path.resolve(__dirname, "mcp-server.js"),
    uePort: process.env.UNREAL_PORT || "55561",
    ueHost: process.env.UNREAL_HOST || "127.0.0.1",
    assetLibraryPath: process.env.ASSET_LIBRARY_PATH || "",
    screenshotDir: deps.screenshotDir,
    builderRuntime: deps.BUILDER_RUNTIME || null,
  };

  try {
    spawnCodexAgent(deps, body, options, emit, ({ isError, latestScreenshot, finalText, returnCode, error }) => {
    clearInterval(ping);

    // Best-effort: scan UE screenshots dir for the newest if we didn't capture one in-stream
    if (!latestScreenshot && options.screenshotDir) {
      try {
        const files = fs.readdirSync(options.screenshotDir)
          .filter((f) => f.endsWith(".png"))
          .map((f) => ({ p: path.join(options.screenshotDir, f), t: fs.statSync(path.join(options.screenshotDir, f)).mtimeMs }))
          .filter((x) => Date.now() - x.t < 1800000)
          .sort((a, b) => b.t - a.t);
        if (files.length) latestScreenshot = files[0].p;
      } catch (_e) {}
    }

    emit("done", {
      sessionId: deps.studioSession,
      isError: !!isError,
      runner: "codex",
      model: CODEX_MODEL,
      returnCode,
      error,
      latestScreenshot: latestScreenshot ? `/api/screenshot/file?path=${encodeURIComponent(latestScreenshot)}` : null,
    });
      res.end();
    });
  } catch (error) {
    clearInterval(ping);
    if (deps.logToFile) deps.logToFile("codex", `[builder] start blocked: ${error.code || error.message}`);
    emit("done", {
      sessionId: deps.studioSession,
      isError: true,
      runner: "codex",
      error: error.message,
      errorCode: error.code || "BUILDER_START_BLOCKED",
      latestScreenshot: null,
    });
    res.end();
  }
}

module.exports = { buildCodexMcpArgs, handleCodexChat, spawnCodexAgent, CODEX_MODEL };

"use strict";

// codex-runner.js — drives the OpenAI Codex CLI (`codex exec`) as a drop-in coding
// agent for /api/chat, mirroring gemini-runner.js.
//
// The frontend talks SSE to /api/chat and expects Claude-shaped events
// (system/text/tool_start/tool_details/tool_result/screenshot/done). This
// module spawns `codex exec --json ...` and translates Codex's thread-event vocabulary
// into that shape so nothing on the React side changes.
//
// Event schema (verified against codex-cli 0.133.0, `codex exec --json`):
//   {"type":"thread.started","thread_id":"..."}
//   {"type":"turn.started"}
//   {"type":"item.started"|"item.updated"|"item.completed","item":{ "id","type", ... }}
//       item.type ∈ { agent_message(text), reasoning(text), command_execution
//                     (command, aggregated_output, exit_code, status),
//                     mcp_tool_call(server, tool, arguments, result/output, status), ... }
//   {"type":"turn.completed","usage":{...}}   ← per-turn end (exec exits after)
//   {"type":"error","message":"..."}
//
// MCP: Codex reads MCP servers from $CODEX_HOME/config.toml (default ~/.codex, where
// the user's `codex login` auth lives — so we DON'T override CODEX_HOME). Instead we
// inject the simworld MCP server from web/mcp.json via repeatable `-c` config overrides,
// rewriting UNREAL_PORT to the live engine port. This guarantees the MCP tools are
// available regardless of whether ~/.codex/config.toml was pre-configured.
//
// System prompt: Codex has no --append-system-prompt flag, so we prepend the prompt to
// the user message.

const fs    = require("fs");
const path  = require("path");
const crypto= require("crypto");
const { spawn } = require("child_process");
const { stripToolPrefix } = require("./gemini-runner");
const { attachBuilderRuntimeProcess } = require("./builder-runtime-authority");
const {
  SCOPED_SIMWORLD_TOOLS,
  assertSafeBuilderArgv,
  assertScopedMcpConfig,
  buildCodexSafetyArgs,
  buildMinimalBuilderEnv,
  resolveVisualFeedbackImages,
} = require("./builder-process-policy");
const { SANDBOX_WORKDIR, sandboxedSpawn } = require("./agent-sandbox");

const CODEX_BIN = process.env.CODEX_BIN || "codex";
const CODEX_MODEL = process.env.CODEX_MODEL || ""; // empty → CLI/config default
const CODEX_IDLE_TIMEOUT_MS = parseInt(process.env.CODEX_IDLE_TIMEOUT_MS || "1800000", 10);

// ---- TOML scalar/array encoder for `-c key=value` overrides ----------------
function tomlVal(v) {
  if (Array.isArray(v)) return "[" + v.map(tomlVal).join(",") + "]";
  if (typeof v === "number") return String(v);
  if (typeof v === "boolean") return v ? "true" : "false";
  return JSON.stringify(String(v)); // valid TOML basic string (escaped)
}

// Build repeatable `-c mcp_servers.<name>...=<toml>` args mirroring web/mcp.json,
// rewriting UNREAL_PORT to the live engine port.
function buildMcpOverrideArgs(mcpConfigPath, unrealPort) {
  const scoped = assertScopedMcpConfig(mcpConfigPath);
  if (unrealPort != null) {
    throw new Error("Direct Unreal routing is forbidden for the real Codex builder");
  }
  const out = [];
  out.push("-c", `mcp_servers.simworld.command=${tomlVal(scoped.command)}`);
  out.push("-c", `mcp_servers.simworld.args=${tomlVal(scoped.args)}`);
  const env = { ...scoped.env };
  for (const [key, value] of Object.entries(env)) {
    out.push("-c", `mcp_servers.simworld.env.${key}=${tomlVal(value)}`);
  }
  out.push("-c", "mcp_servers.simworld.required=true");
  out.push("-c", `mcp_servers.simworld.enabled_tools=${tomlVal(SCOPED_SIMWORLD_TOOLS)}`);
  out.push("-c", 'mcp_servers.simworld.default_tools_approval_mode="approve"');
  return out;
}

/**
 * @param {object} args  req, res, body{message,sessionId,model,...}, systemPrompt, ctx
 *   ctx: { ctxManager, snapshotScene, STUDIO_SESSION, MCP_CONFIG, UNREAL_PORT,
 *          LOG_DIR, SCREENSHOT_DIR, _chatProcs, logToFile, MOCK_MODE }
 */
function runCodexChat({ req, res, body, systemPrompt, ctx }) {
  const { message, sessionId } = body || {};
  const model = String((body && body.model) || CODEX_MODEL || "").trim();
  if (model && !/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/.test(model)) {
    throw new Error("Codex builder model identifier is invalid");
  }
  const {
    ctxManager, snapshotScene, STUDIO_SESSION,
    MCP_CONFIG, UNREAL_PORT, LOG_DIR, SCREENSHOT_DIR,
    _chatProcs, logToFile, MOCK_MODE, BUILDER_RUNTIME,
  } = ctx;

  // Caller filesystem paths are never accepted as model image input. A future
  // server-managed byte resolver can be installed explicitly; until then any
  // non-empty visualFeedbackImages request fails before a process is spawned.
  resolveVisualFeedbackImages(body && body.visualFeedbackImages);

  if (!res.headersSent) {
    res.setHeader("Content-Type", "text/event-stream");
    res.setHeader("Cache-Control", "no-cache");
    res.setHeader("Connection", "keep-alive");
    res.setHeader("X-Accel-Buffering", "no");
    res.flushHeaders();
  }

  const emit = (type, data) => {
    if (res.writableEnded) return;
    res.write(`event: ${type}\ndata: ${JSON.stringify(data)}\n\n`);
  };

  // Codex has no system-prompt flag — prepend it to the user message.
  const fullPrompt = systemPrompt ? `${systemPrompt}\n\n---\n\n${message}` : String(message || "");

  const args = [
    "exec",
    "--json",
    "--skip-git-repo-check",
    ...buildCodexSafetyArgs(),
    "-C", SANDBOX_WORKDIR,
    ...buildMcpOverrideArgs(MCP_CONFIG, UNREAL_PORT),
  ];
  if (model) args.push("-m", model);
  // The prompt is untrusted request content. Keep it out of argv so a leading
  // dash cannot become a CLI option and it is not exposed in the process list.
  args.push("-");
  assertSafeBuilderArgv("codex", args);

  const env = buildMinimalBuilderEnv(process.env, BUILDER_RUNTIME, { provider: "codex" });

  logToFile("codex", `User: "${String(message).slice(0, 200)}" model=${model || "default"} sessionId=${sessionId || "new"}`);
  try { fs.writeFileSync(path.join(LOG_DIR, "raw_latest.jsonl"), ""); } catch {}

  // OS sandbox: empty mount namespace plus exact read-only runtime/config mounts.
  const _sb = sandboxedSpawn(CODEX_BIN, args, null, { env: process.env, provider: "codex" });
  const proc = spawn(_sb.cmd, _sb.args, {
    cwd: "/",
    env,
    stdio: ["pipe", "pipe", "pipe"],
  });
  attachBuilderRuntimeProcess(BUILDER_RUNTIME, proc);

  const procKey = BUILDER_RUNTIME ? BUILDER_RUNTIME.scopeId : (sessionId || "_global");
  const prior = _chatProcs.get(procKey);
  if (prior && !prior.killed) { try { prior.kill("SIGTERM"); } catch {} }
  _chatProcs.set(procKey, proc);
  const clearTrackedProcess = () => {
    if (_chatProcs.get(procKey) === proc) _chatProcs.delete(procKey);
  };
  proc.on("exit", clearTrackedProcess);

  // ---- per-call state ------------------------------------------------------
  let stdoutBuf      = "";
  let stderrBuf      = "";
  let session        = sessionId || null;
  let latestShot     = null;
  let sawTurnDone    = false;     // clean finish seen (turn.completed)
  let lastOutputTime = Date.now();
  const startedTools = new Set(); // item id → tool_start emitted
  const toolInputs   = new Map(); // item id → {name, input} for ctx tracking

  let knownMcpServers = [];
  try {
    const mcp = JSON.parse(fs.readFileSync(MCP_CONFIG, "utf-8"));
    knownMcpServers = Object.keys(mcp.mcpServers || {});
  } catch {}

  function sweepLatestScreenshot() {
    if (!fs.existsSync(SCREENSHOT_DIR)) return;
    try {
      const now = Date.now();
      const fresh = fs.readdirSync(SCREENSHOT_DIR)
        .filter(n => n.endsWith(".png"))
        .map(n => ({ fp: path.join(SCREENSHOT_DIR, n), t: fs.statSync(path.join(SCREENSHOT_DIR, n)).mtimeMs }))
        .filter(({ t }) => now - t < 1.8e6);
      let best = null;
      for (const u of fresh) if (!best || u.t > best.t) best = u;
      if (best) latestShot = best.fp;
    } catch {}
  }
  const latestShotUrl = () => {
    sweepLatestScreenshot();
    return latestShot ? `/api/screenshot/file?path=${encodeURIComponent(latestShot)}` : null;
  };

  // ---- ctxManager hooks (mirror Claude/Gemini paths) -----------------------
  ctxManager.resolveSession(STUDIO_SESSION);
  ctxManager.beginRound(STUDIO_SESSION);

  const CTX_TOOLS = new Set([
    "spawn_blueprint_actor", "spawn_actor", "spawn_agent",
    "delete_actor", "delete_all_spawned", "setup_environment",
  ]);

  function recordToolUseForCtx(itemId, bareName, input) {
    if (CTX_TOOLS.has(bareName)) toolInputs.set(itemId, { name: bareName, input: input || {} });
  }
  function applyCtxOnToolResult(itemId, resultText, isError) {
    const st = toolInputs.get(itemId);
    if (!st) return;
    try {
      if (!isError && session) {
        const tr = JSON.parse(resultText);
        if (tr.status === "success") {
          if (st.name === "spawn_blueprint_actor" || st.name === "spawn_actor" || st.name === "spawn_agent") {
            const an  = st.input.actor_name || st.input.agent_name || st.input.name;
            const cls = st.input.blueprint_id || st.input.static_mesh || st.input.agent_type || "";
            const cat = st.name === "spawn_agent" ? "agent" : undefined;
            ctxManager.addActor(STUDIO_SESSION, { name: an, cls, category: cat, location: st.input.location });
          } else if (st.name === "delete_actor") {
            ctxManager.removeActor(STUDIO_SESSION, st.input.name);
          } else if (st.name === "delete_all_spawned") {
            ctxManager.clearAllSpawned(STUDIO_SESSION);
          } else if (st.name === "setup_environment") {
            ctxManager.setEnvironmentReady(STUDIO_SESSION);
          }
        }
      }
    } catch (e) {
      logToFile("ctx", `codex parse error: ${e.message}`);
    }
    toolInputs.delete(itemId);
  }

  // Surface a tool result string, detect screenshots / verifier output.
  function emitToolResult(itemId, resultText, isError) {
    const shotMatch = resultText.match(/([\/][\w\/\-._]+\.png)/);
    if (shotMatch && fs.existsSync(shotMatch[1])) {
      latestShot = shotMatch[1];
      emit("screenshot", { toolUseId: itemId, filepath: `/api/screenshot/file?path=${encodeURIComponent(latestShot)}` });
    }
    emit("tool_result", { toolUseId: itemId, result: resultText.slice(0, 2000), isError });
    applyCtxOnToolResult(itemId, resultText, isError);
  }

  // ---- item handling -------------------------------------------------------
  function handleItem(phase, item) {
    if (!item || typeof item !== "object") return;
    const itemId = item.id || crypto.randomBytes(6).toString("hex");
    const itype  = item.type || item.item_type;

    if (itype === "agent_message") {
      // Emitted complete (no token streaming in this format) — surface once on completion.
      if (phase === "completed" && typeof item.text === "string" && item.text.length) {
        emit("text", { delta: item.text });
      }
      return;
    }

    if (itype === "command_execution") {
      const cmd = item.command || "shell";
      if (phase === "started" && !startedTools.has(itemId)) {
        startedTools.add(itemId);
        emit("tool_start",  { id: itemId, name: "shell", displayName: "shell" });
        emit("tool_details",{ id: itemId, name: "shell", displayName: "shell", input: { command: cmd } });
        logToFile("tool", `Starting (codex shell): ${String(cmd).slice(0, 120)}`);
      }
      if (phase === "completed") {
        if (!startedTools.has(itemId)) {
          startedTools.add(itemId);
          emit("tool_start",  { id: itemId, name: "shell", displayName: "shell" });
          emit("tool_details",{ id: itemId, name: "shell", displayName: "shell", input: { command: cmd } });
        }
        const out = (item.aggregated_output != null ? String(item.aggregated_output) : "");
        const isError = item.exit_code != null && item.exit_code !== 0;
        emit("tool_result", { toolUseId: itemId, result: out.slice(0, 2000), isError });
        logToFile("tool_result", `${itemId} exit=${item.exit_code} → ${out.slice(0, 200)}`);
      }
      return;
    }

    if (itype === "mcp_tool_call" || itype === "tool_call") {
      const server = item.server || item.server_name || "";
      const toolNm = item.tool || item.tool_name || item.name || "tool";
      const fullName = server ? `mcp__${server}__${toolNm}` : toolNm;
      const bare   = stripToolPrefix(fullName, knownMcpServers);
      let input    = item.arguments != null ? item.arguments : (item.input || {});
      if (typeof input === "string") { try { input = JSON.parse(input); } catch {} }

      if ((phase === "started" || phase === "completed") && !startedTools.has(itemId)) {
        startedTools.add(itemId);
        emit("tool_start",  { id: itemId, name: fullName, displayName: bare });
        emit("tool_details",{ id: itemId, name: fullName, displayName: bare, input });
        logToFile("tool", `Starting (codex mcp): ${fullName}`);
        recordToolUseForCtx(itemId, bare, input);
      }
      if (phase === "completed") {
        let resultText = "";
        const raw = item.result != null ? item.result : (item.output != null ? item.output : item.aggregated_output);
        if (typeof raw === "string") resultText = raw;
        else if (raw != null) resultText = JSON.stringify(raw);
        const isError = item.status === "failed" || item.status === "error" || !!item.error;
        emitToolResult(itemId, resultText, isError);
        logToFile("tool_result", `${itemId} (mcp ${bare}) → ${resultText.slice(0, 200)}`);
      }
      return;
    }

    // reasoning / todo_list / web_search / file_change → not surfaced as chat text.
  }

  // ---- top-level thread-event dispatch -------------------------------------
  function handleEvent(ev) {
    if (!ev || typeof ev !== "object") return;
    const t = ev.type;

    if (t === "thread.started" || t === "session.created") {
      if (ev.thread_id) session = ev.thread_id;
      let mcpServers = [];
      try {
        const mcp = JSON.parse(fs.readFileSync(MCP_CONFIG, "utf-8"));
        mcpServers = Object.keys(mcp.mcpServers || {}).map(name => ({ name, status: "connected" }));
      } catch {}
      emit("system", { sessionId: session, mcpServers });
      logToFile("codex", `Thread ${session}`);
      return;
    }

    if (t === "item.started")   { handleItem("started",   ev.item); return; }
    if (t === "item.updated")   { handleItem("updated",   ev.item); return; }
    if (t === "item.completed") { handleItem("completed", ev.item); return; }

    if (t === "turn.completed") {
      sawTurnDone = true;
      const usage = ev.usage || {};
      logToFile("result", JSON.stringify({ provider: "codex", usage }).slice(0, 500));
      return; // finalize on process close (exec exits after the turn)
    }

    if (t === "turn.failed" || t === "error") {
      const msg = (ev.error && ev.error.message) || ev.message || "codex error";
      logToFile("codex-err", String(msg).slice(0, 400));
      emit("text", { delta: `\n\n⚠️ ${msg}\n` });
      return;
    }
  }

  // ---- line-delimited stdout parser ----------------------------------------
  function flushLine(line) {
    line = line.trim();
    if (!line) return;
    try { fs.appendFileSync(path.join(LOG_DIR, "raw_latest.jsonl"), line + "\n"); } catch {}
    let ev;
    try { ev = JSON.parse(line); } catch { return; } // ignore non-JSON banner lines
    try { handleEvent(ev); }
    catch (e) { logToFile("codex", `event handler error: ${e.message}`); }
  }

  proc.stdout.on("data", chunk => {
    lastOutputTime = Date.now();
    stdoutBuf += chunk.toString();
    const lines = stdoutBuf.split("\n");
    stdoutBuf = lines.pop() ?? "";
    for (const line of lines) flushLine(line);
  });

  proc.stderr.on("data", chunk => {
    lastOutputTime = Date.now();
    const s = chunk.toString().trim();
    if (s) { stderrBuf += s + "\n"; logToFile("codex-stderr", s.slice(0, 300)); }
  });

  const idleTimer = setInterval(() => {
    if (Date.now() - lastOutputTime > CODEX_IDLE_TIMEOUT_MS && !sawTurnDone) {
      logToFile("codex", `Idle timeout (${CODEX_IDLE_TIMEOUT_MS / 1000}s no output), killing process`);
      clearInterval(idleTimer);
      try { proc.kill("SIGTERM"); } catch {}
    }
  }, 10000);

  let finished = false;
  function finish(isError) {
    if (finished) return;
    finished = true;
    clearInterval(idleTimer);
    const done = () => {
      emit("done", { sessionId: STUDIO_SESSION, isError, costUsd: 0, latestScreenshot: latestShotUrl() });
      try { res.end(); } catch {}
    };
    if (!MOCK_MODE) {
      snapshotScene(STUDIO_SESSION, BUILDER_RUNTIME).then(done).catch(err => {
        logToFile("ctx", `codex snapshotScene error: ${err.message}`);
        done();
      });
    } else done();
  }

  proc.on("close", code => {
    clearInterval(idleTimer);
    if (stdoutBuf.trim()) flushLine(stdoutBuf);
    logToFile("codex", `Process exited code=${code} sawTurnDone=${sawTurnDone}`);
    if (sawTurnDone) { finish(false); return; }
    if (!res.writableEnded) {
      const detail = stderrBuf.slice(0, 400).trim() || (code !== 0 ? `exit code ${code}` : "no output received");
      emit("text", { delta: `\n\n⚠️ Codex agent exited unexpectedly: ${detail}\n` });
      finish(true);
    }
  });

  proc.on("error", error => {
    clearTrackedProcess();
    stderrBuf += `process: ${error.message}\n`;
    logToFile("codex", `Process error: ${error.message}`);
    if (!res.writableEnded) {
      emit("text", { delta: `\n\n⚠️ Codex agent failed to start: ${error.message}\n` });
      finish(true);
    }
  });
  proc.stdin.on("error", error => {
    stderrBuf += `stdin: ${error.message}\n`;
    logToFile("codex", `stdin error: ${error.message}`);
    try { proc.kill("SIGTERM"); } catch {}
  });
  try {
    proc.stdin.end(fullPrompt);
  } catch (error) {
    stderrBuf += `stdin: ${error.message}\n`;
    logToFile("codex", `stdin write failed: ${error.message}`);
    try { proc.kill("SIGTERM"); } catch {}
    finish(true);
  }

  res.on("close", () => {
    if (!res.writableEnded) {
      clearInterval(idleTimer);
      try { res.end(); } catch {}
      logToFile("codex", "Browser closed SSE — codex continues in background (use /api/chat-stop to kill)");
    }
  });
}

module.exports = { buildMcpOverrideArgs, runCodexChat };

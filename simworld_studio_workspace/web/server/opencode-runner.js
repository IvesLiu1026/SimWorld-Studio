"use strict";

// opencode-runner.js — drives the OpenCode CLI (`opencode run --format json`) as a
// drop-in coding agent for /api/chat, mirroring gemini-runner.js / codex-runner.js.
//
// The frontend talks SSE to /api/chat and expects Claude-shaped events
// (system/text/tool_start/tool_details/tool_result/screenshot/done). This
// module spawns `opencode run --format json --dangerously-skip-permissions` and
// translates OpenCode's part-event vocabulary into that shape.
//
// Event schema (verified against opencode 1.15.12, `run --format json`): newline-
// delimited JSON, each line `{ type, sessionID, part }` where `part.type` is one of:
//   step-start                            → ignored
//   text   { text }                       → text delta (deduped by part.id)
//   tool   { tool, callID, state:{ status, input, output } } → tool_start/details/result
//   step-finish { cost, tokens }          → cost accounting
//
// MCP: OpenCode reads MCP servers from `opencode.json` in the working dir. We mirror
// web/mcp.json into a scratch dir's opencode.json (rewriting UNREAL_PORT to the live UE
// port) and run from there — the same auto-wiring approach as the Gemini path. Auth comes
// from the user's existing `opencode auth login` / provider env keys (e.g. OPENAI_API_KEY).
//
// System prompt: prepended to the user message (OpenCode has no system-prompt CLI flag).

const fs    = require("fs");
const os    = require("os");
const path  = require("path");
const { spawn } = require("child_process");
const { stripToolPrefix } = require("./gemini-runner");
const { attachBuilderRuntimeProcess, buildBuilderChildEnv } = require("./builder-runtime-authority");

const OPENCODE_BIN   = process.env.OPENCODE_BIN   || "opencode";
const OPENCODE_MODEL = process.env.OPENCODE_MODEL || ""; // provider/model form; empty → opencode default
const OPENCODE_IDLE_TIMEOUT_MS = parseInt(process.env.OPENCODE_IDLE_TIMEOUT_MS || "1800000", 10);

// Mirror web/mcp.json → <scratch>/opencode.json so OpenCode auto-loads the simworld MCP
// server (rewriting UNREAL_PORT to the live engine port). Returns the scratch cwd.
function ensureOpenCodeWorkspace(mcpConfigPath, unrealPort, logToFile) {
  const cwd = path.join(os.tmpdir(), "simworld-opencode");
  try {
    fs.mkdirSync(cwd, { recursive: true });
    const mcp = JSON.parse(fs.readFileSync(mcpConfigPath, "utf-8"));
    const servers = {};
    for (const [name, cfg] of Object.entries(mcp.mcpServers || {})) {
      const environment = { ...(cfg.env || {}) };
      if (unrealPort) environment.UNREAL_PORT = String(unrealPort);
      servers[name] = {
        type: "local",
        command: [cfg.command, ...(cfg.args || [])],
        enabled: true,
        environment,
      };
    }
    // SECURITY: this is a scene-generation agent, not a coding agent. Hard-deny every
    // built-in file/shell tool so it can ONLY act through the simworld MCP tools — it must
    // not read or modify Studio's source. Without this, the model uses bash/read/write as an
    // escape hatch (e.g. poking at mcp-server.js) instead of the scene tools.
    const config = {
      $schema: "https://opencode.ai/config.json",
      mcp: servers,
      permission: { bash: "deny", edit: "deny", webfetch: "deny" },
      tools: { bash: false, edit: false, write: false, read: false, grep: false, glob: false, list: false, patch: false, webfetch: false, todowrite: false, todoread: false },
    };
    fs.writeFileSync(path.join(cwd, "opencode.json"), JSON.stringify(config, null, 2));
  } catch (e) {
    logToFile && logToFile("opencode", `failed to write opencode.json: ${e.message}`);
  }
  return cwd;
}

/**
 * @param {object} args  req, res, body{message,sessionId,model,...}, systemPrompt, ctx
 *   ctx: { ctxManager, snapshotScene, STUDIO_SESSION, MCP_CONFIG, UNREAL_PORT,
 *          LOG_DIR, SCREENSHOT_DIR, _chatProcs, logToFile, MOCK_MODE }
 */
function runOpenCodeChat({ req, res, body, systemPrompt, ctx }) {
  const { message, sessionId } = body || {};
  const model = (body && body.model) || OPENCODE_MODEL;
  const {
    ctxManager, snapshotScene, STUDIO_SESSION,
    MCP_CONFIG, UNREAL_PORT, LOG_DIR, SCREENSHOT_DIR,
    _chatProcs, logToFile, MOCK_MODE, BUILDER_RUNTIME,
  } = ctx;

  const cwd = ensureOpenCodeWorkspace(MCP_CONFIG, UNREAL_PORT, logToFile);

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

  const fullPrompt = systemPrompt ? `${systemPrompt}\n\n---\n\n${message}` : String(message || "");

  // --pure = run WITHOUT external plugins/skills. Critical: without it, opencode discovers
  // the repo's `.agents/skills/simworld-mcp` skill and invokes a generic `skill`→bash path
  // instead of the real MCP tools (that's why it went poking at source). With --pure it uses
  // the simworld MCP tools directly (verified: spawn_blueprint_actor actually runs).
  const args = ["run", "--pure", "--format", "json", "--dangerously-skip-permissions"];
  if (model) args.push("--model", model);
  args.push(fullPrompt);

  const env = buildBuilderChildEnv(process.env, BUILDER_RUNTIME);
  Object.keys(env).forEach(k => { if (k.startsWith("CLAUDE")) delete env[k]; });

  logToFile("opencode", `User: "${String(message).slice(0, 200)}" model=${model || "default"} sessionId=${sessionId || "new"}`);
  try { fs.writeFileSync(path.join(LOG_DIR, "raw_latest.jsonl"), ""); } catch {}

  let proc;
  try {
    // OS sandbox: repo read-only so the agent can't modify Studio source (see agent-sandbox.js).
    const _sb = require("./agent-sandbox").sandboxedSpawn(OPENCODE_BIN, args, cwd);
    proc = spawn(_sb.cmd, _sb.args, { cwd, env, stdio: ["ignore", "pipe", "pipe"] });
    attachBuilderRuntimeProcess(BUILDER_RUNTIME, proc);
  } catch (e) {
    emit("text", { delta: `\n\n⚠️ Failed to launch OpenCode (\`${OPENCODE_BIN}\`): ${e.message}. Install the opencode CLI and set OPENCODE_BIN if needed.\n` });
    emit("done", { sessionId: STUDIO_SESSION, isError: true, latestScreenshot: null });
    try { res.end(); } catch {}
    return;
  }

  const procKey = BUILDER_RUNTIME ? BUILDER_RUNTIME.scopeId : (sessionId || "_global");
  const prior = _chatProcs.get(procKey);
  if (prior && !prior.killed) { try { prior.kill("SIGTERM"); } catch {} }
  _chatProcs.set(procKey, proc);
  proc.on("exit", () => { if (_chatProcs.get(procKey) === proc) _chatProcs.delete(procKey); });

  // ---- per-call state ------------------------------------------------------
  let stdoutBuf      = "";
  let stderrBuf      = "";
  let latestShot     = null;
  let session        = sessionId || null;
  let lastOutputTime = Date.now();
  const textEmitted  = new Map();   // text part id → chars already emitted (dedupe streaming)
  const startedTools = new Set();   // tool callID → tool_start emitted
  const toolInputs   = new Map();   // callID → {name, input} for ctx tracking

  let knownMcpServers = [];
  try {
    const mcp = JSON.parse(fs.readFileSync(MCP_CONFIG, "utf-8"));
    knownMcpServers = Object.keys(mcp.mcpServers || {});
  } catch {}

  function sweepLatestScreenshot() {
    if (!fs.existsSync(SCREENSHOT_DIR)) return;
    try {
      const now = Date.now();
      let best = null;
      for (const n of fs.readdirSync(SCREENSHOT_DIR)) {
        if (!n.endsWith(".png")) continue;
        const fp = path.join(SCREENSHOT_DIR, n);
        const t = fs.statSync(fp).mtimeMs;
        if (now - t < 1.8e6 && (!best || t > best.t)) best = { fp, t };
      }
      if (best) latestShot = best.fp;
    } catch {}
  }
  const latestShotUrl = () => {
    sweepLatestScreenshot();
    return latestShot ? `/api/screenshot/file?path=${encodeURIComponent(latestShot)}` : null;
  };

  // ---- ctxManager hooks (mirror Claude/Gemini/Codex paths) -----------------
  ctxManager.resolveSession(STUDIO_SESSION);
  ctxManager.beginRound(STUDIO_SESSION);

  const CTX_TOOLS = new Set([
    "spawn_blueprint_actor", "spawn_actor", "spawn_agent",
    "delete_actor", "delete_all_spawned", "setup_environment",
  ]);
  function recordToolUseForCtx(callId, bareName, input) {
    if (CTX_TOOLS.has(bareName)) toolInputs.set(callId, { name: bareName, input: input || {} });
  }
  function applyCtxOnToolResult(callId, resultText, isError) {
    const st = toolInputs.get(callId);
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
      logToFile("ctx", `opencode parse error: ${e.message}`);
    }
    toolInputs.delete(callId);
  }

  // ---- part handling -------------------------------------------------------
  function handlePart(part) {
    if (!part || typeof part !== "object") return;
    const ptype = part.type;

    if (ptype === "text") {
      const id = part.id || "_t";
      const full = typeof part.text === "string" ? part.text : "";
      const prev = textEmitted.get(id) || 0;
      if (full.length > prev) { emit("text", { delta: full.slice(prev) }); textEmitted.set(id, full.length); }
      return;
    }

    if (ptype === "tool") {
      const callId   = part.callID || part.id;
      const fullName = part.tool || "tool";
      const bare     = stripToolPrefix(fullName, knownMcpServers);
      const state    = part.state || {};
      const input    = state.input || {};

      if (!startedTools.has(callId)) {
        startedTools.add(callId);
        emit("tool_start",  { id: callId, name: fullName, displayName: bare });
        emit("tool_details",{ id: callId, name: fullName, displayName: bare, input });
        logToFile("tool", `Starting (opencode): ${fullName}`);
        recordToolUseForCtx(callId, bare, input);
      }

      if (state.status === "completed" || state.status === "error") {
        let resultText = "";
        if (typeof state.output === "string") resultText = state.output;
        else if (state.output != null) resultText = JSON.stringify(state.output);
        else if (state.metadata && state.metadata.output) resultText = String(state.metadata.output);
        const isError = state.status === "error";

        const shotMatch = resultText.match(/([\/][\w\/\-._]+\.png)/);
        if (shotMatch && fs.existsSync(shotMatch[1])) {
          latestShot = shotMatch[1];
          emit("screenshot", { toolUseId: callId, filepath: `/api/screenshot/file?path=${encodeURIComponent(latestShot)}` });
        }
        emit("tool_result", { toolUseId: callId, result: resultText.slice(0, 2000), isError });
        logToFile("tool_result", `${callId} (opencode ${bare}) → ${resultText.slice(0, 200)}`);
        applyCtxOnToolResult(callId, resultText, isError);

      }
      return;
    }
    // step-start / step-finish → nothing user-facing.
  }

  function handleEvent(ev) {
    if (!ev || typeof ev !== "object") return;
    if (ev.sessionID && !session) {
      session = ev.sessionID;
      let mcpServers = [];
      try {
        const mcp = JSON.parse(fs.readFileSync(MCP_CONFIG, "utf-8"));
        mcpServers = Object.keys(mcp.mcpServers || {}).map(name => ({ name, status: "connected" }));
      } catch {}
      emit("system", { sessionId: session, mcpServers });
    }
    if (ev.type === "error") {
      const msg = (ev.error && (ev.error.message || ev.error.data)) || ev.message || "opencode error";
      logToFile("opencode-err", String(msg).slice(0, 400));
      emit("text", { delta: `\n\n⚠️ ${msg}\n` });
      return;
    }
    if (ev.part) handlePart(ev.part);
  }

  function flushLine(line) {
    line = line.trim();
    if (!line) return;
    try { fs.appendFileSync(path.join(LOG_DIR, "raw_latest.jsonl"), line + "\n"); } catch {}
    let ev;
    try { ev = JSON.parse(line); } catch { return; }
    try { handleEvent(ev); }
    catch (e) { logToFile("opencode", `event handler error: ${e.message}`); }
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
    if (s) { stderrBuf += s + "\n"; logToFile("opencode-stderr", s.slice(0, 300)); }
  });

  const idleTimer = setInterval(() => {
    if (Date.now() - lastOutputTime > OPENCODE_IDLE_TIMEOUT_MS) {
      logToFile("opencode", `Idle timeout, killing process`);
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
    if (!MOCK_MODE) snapshotScene(STUDIO_SESSION, BUILDER_RUNTIME).then(done).catch(() => done());
    else done();
  }

  proc.on("error", err => {
    logToFile("opencode", `spawn error: ${err.message}`);
    if (!res.writableEnded) {
      emit("text", { delta: `\n\n⚠️ OpenCode failed to start: ${err.message}. Install the opencode CLI (and set OPENCODE_BIN if it isn't on PATH).\n` });
      finish(true);
    }
  });

  proc.on("close", code => {
    clearInterval(idleTimer);
    if (stdoutBuf.trim()) flushLine(stdoutBuf);
    logToFile("opencode", `Process exited code=${code}`);
    if (code !== 0 && !finished) {
      const detail = stderrBuf.slice(0, 400).trim() || `exit code ${code}`;
      emit("text", { delta: `\n\n⚠️ OpenCode exited: ${detail}\n` });
    }
    finish(code !== 0);
  });

  res.on("close", () => {
    if (!res.writableEnded) {
      clearInterval(idleTimer);
      try { res.end(); } catch {}
      logToFile("opencode", "Browser closed SSE — opencode continues in background (use /api/chat-stop to kill)");
    }
  });
}

module.exports = { runOpenCodeChat, ensureOpenCodeWorkspace };

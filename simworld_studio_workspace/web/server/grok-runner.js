"use strict";

// grok-runner.js — drives xAI's Grok Build CLI as a drop-in coding agent for /api/chat,
// mirroring cursor-runner.js / codex-runner.js / gemini-runner.js / opencode-runner.js.
//
// Grok Build (docs.x.ai/build) is xAI's terminal coding agent. The same CLI routes to several
// models — grok-build (default, 512K ctx) and grok-composer-2.5-fast — selected with `-m`.
//
// TRANSPORT — ACP (Agent Client Protocol), not the simple `-p` headless mode. We drive
// `grok agent --always-approve -m <model> stdio`, a newline-delimited JSON-RPC 2.0 stream.
// Why ACP: grok's `-p --output-format streaming-json` mode emits ONLY thought/text/end and
// hides every tool call, so the per-tool timeline / screenshots / verifier / scene-tracking the
// Studio UI relies on would be invisible. ACP surfaces tool calls as `session/update`
// notifications (tool_call + tool_call_update), so we get full parity with the other agents.
//
// Handshake (verified against the real grok 0.2.22 binary):
//   → initialize {protocolVersion:1, clientCapabilities:{fs,terminal:false}}
//   ← {protocolVersion, agentCapabilities, authMethods}
//   → session/new {cwd, mcpServers:[]}            (mcp auto-loads from cwd/.mcp.json, see below)
//   ← {sessionId, models:{currentModelId, availableModels}}
//   → session/prompt {sessionId, prompt:[{type:"text", text}]}
//   ← (stream of session/update notifications) … then the response: {stopReason:"end_turn", _meta}
// session/update.update.sessionUpdate ∈ {
//   agent_thought_chunk → update.content.text (reasoning; suppressed)
//   agent_message_chunk → update.content.text (assistant answer; emitted as text)
//   tool_call           → {toolCallId, title, rawInput}                       → tool_start
//   tool_call_update    → {toolCallId, kind, title, status, content[], rawOutput, locations} → tool_result on status completed/failed
//   user_message_chunk / available_commands_update / plan → ignored
// }
// `--always-approve` auto-approves all tool/MCP executions, so no client-side permission round-trip
// is needed (we still answer session/request_permission defensively). Auth: XAI_API_KEY env OR the
// cached token from `grok login` (~/.grok/auth.json). No append-system-prompt flag → the Studio
// system prompt is prepended to the prompt text (same as cursor/codex).
//
// MCP: grok loads project MCP servers from `.mcp.json` (Claude-Code `{mcpServers:{...}}` format) in
// the cwd — verified via `grok mcp doctor` (config sources: ~/.grok/config.toml, ~/.claude.json,
// project .mcp.json, grok.com; NOT `.grok/settings.json` despite the docs). We mirror web/mcp.json
// → a scratch dir's .mcp.json (rewriting UNREAL_PORT to the live engine port) and run from there.
//
// SECURITY: agent-sandbox.js wraps the process in bwrap with the repo bound READ-ONLY — the OS
// sandbox is the real guardrail, not grok's own.

const fs    = require("fs");
const os    = require("os");
const path  = require("path");
const crypto= require("crypto");
const { spawn } = require("child_process");
const { stripToolPrefix } = require("./gemini-runner");

const GROK_BIN  = process.env.GROK_BIN  || "grok";
const GROK_MODEL = process.env.GROK_MODEL || ""; // empty → CLI/account default
const GROK_IDLE_TIMEOUT_MS = parseInt(process.env.GROK_IDLE_TIMEOUT_MS || "1800000", 10);

// Mirror web/mcp.json → <scratch>/.mcp.json so grok auto-loads the simworld MCP server
// (rewriting UNREAL_PORT to the live engine port). Returns the scratch cwd.
function ensureGrokWorkspace(mcpConfigPath, unrealPort, logToFile) {
  const cwd = path.join(os.tmpdir(), "simworld-grok");
  try {
    fs.mkdirSync(cwd, { recursive: true });
    const mcp = JSON.parse(fs.readFileSync(mcpConfigPath, "utf-8"));
    const servers = {};
    for (const [name, cfg] of Object.entries(mcp.mcpServers || {})) {
      const env = { ...(cfg.env || {}) };
      if (unrealPort) env.UNREAL_PORT = String(unrealPort);
      servers[name] = { command: cfg.command, args: cfg.args || [], env };
    }
    fs.writeFileSync(path.join(cwd, ".mcp.json"), JSON.stringify({ mcpServers: servers }, null, 2));
    logToFile && logToFile("grok", `mirrored mcp.json → ${path.join(cwd, ".mcp.json")}`);
  } catch (e) {
    logToFile && logToFile("grok", `failed to mirror mcp.json: ${e.message}`);
  }
  return cwd;
}

// Flatten an ACP content value into plain text. Handles a bare string, {type:"text",text},
// {type:"content",content:{...}}, and arrays of any of those (tool_call_update.content shape).
function contentToText(c) {
  if (c == null) return "";
  if (typeof c === "string") return c;
  if (Array.isArray(c)) return c.map(contentToText).join("");
  if (typeof c.text === "string") return c.text;
  if (c.content != null) return contentToText(c.content);
  return "";
}

/**
 * @param {object} args  req, res, body{message,sessionId,model,...}, systemPrompt, ctx
 *   ctx: { ctxManager, snapshotScene, STUDIO_SESSION, MCP_CONFIG, UNREAL_PORT,
 *          LOG_DIR, SCREENSHOT_DIR, _chatProcs, logToFile, MOCK_MODE }
 */
function runGrokChat({ req, res, body, systemPrompt, ctx }) {
  const { message, sessionId } = body || {};
  const model = (body && body.model) || GROK_MODEL;
  const {
    ctxManager, snapshotScene, STUDIO_SESSION,
    MCP_CONFIG, UNREAL_PORT, LOG_DIR, SCREENSHOT_DIR,
    _chatProcs, logToFile, MOCK_MODE,
  } = ctx;

  const grokCwd = ensureGrokWorkspace(MCP_CONFIG, UNREAL_PORT, logToFile);

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

  // grok has no append-system-prompt flag — prepend it to the prompt text.
  const fullPrompt = systemPrompt ? `${systemPrompt}\n\n---\n\n${message}` : String(message || "");

  // ACP transport: `grok agent --always-approve -m <model> stdio`. SECURITY: safe because
  // agent-sandbox.js wraps this in bwrap with the repo READ-ONLY.
  const args = ["agent", "--always-approve"];
  if (model) args.push("-m", model);
  args.push("stdio");

  const env = { ...process.env };
  // Don't let Claude session env vars leak into grok. XAI_API_KEY (if set) is preserved.
  Object.keys(env).forEach(k => { if (k.startsWith("CLAUDE")) delete env[k]; });
  // The grok installer drops the binary in ~/.local/bin — prepend it so GROK_BIN resolves under tmux.
  const localBin = path.join(os.homedir(), ".local", "bin");
  env.PATH = `${localBin}:${env.PATH || ""}`;

  logToFile("grok", `User: "${String(message).slice(0, 200)}" model=${model || "default"} sessionId=${sessionId || "new"}`);
  try { fs.writeFileSync(path.join(LOG_DIR, "raw_latest.jsonl"), ""); } catch {}

  const _sb = require("./agent-sandbox").sandboxedSpawn(GROK_BIN, args, grokCwd);
  const proc = spawn(_sb.cmd, _sb.args, { cwd: grokCwd, env, stdio: ["pipe", "pipe", "pipe"] });

  const procKey = sessionId || "_global";
  const prior = _chatProcs.get(procKey);
  if (prior && !prior.killed) { try { prior.kill("SIGTERM"); } catch {} }
  _chatProcs.set(procKey, proc);
  proc.on("exit", () => { if (_chatProcs.get(procKey) === proc) _chatProcs.delete(procKey); });

  // ---- per-call state ------------------------------------------------------
  let stdoutBuf      = "";
  let stderrBuf      = "";
  let session        = sessionId || null;
  let latestShot     = null;
  let gotResult      = false;
  let lastOutputTime = Date.now();
  const startedTools = new Set(); // tool id → tool_start emitted
  const verifierTools= new Set();
  const toolInputs   = new Map(); // tool id → {name, input} for ctx tracking
  const toolNames    = new Map(); // tool id → raw tool name (from _x.ai delta chunks)

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

  // ---- ctxManager hooks (mirror Claude/Gemini/Codex/Cursor paths) ----------
  ctxManager.resolveSession(STUDIO_SESSION);
  ctxManager.beginRound(STUDIO_SESSION);

  const CTX_TOOLS = new Set([
    "spawn_blueprint_actor", "spawn_actor", "spawn_agent",
    "delete_actor", "delete_all_spawned", "setup_environment",
  ]);

  function recordToolUseForCtx(toolId, bareName, input) {
    if (CTX_TOOLS.has(bareName)) toolInputs.set(toolId, { name: bareName, input: input || {} });
  }
  function applyCtxOnToolResult(toolId, resultText, isError) {
    const st = toolInputs.get(toolId);
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
      logToFile("ctx", `grok parse error: ${e.message}`);
    }
    toolInputs.delete(toolId);
  }

  function emitToolStart(toolId, fullName, input) {
    if (startedTools.has(toolId)) return;
    startedTools.add(toolId);
    const bare = stripToolPrefix(fullName, knownMcpServers);
    emit("tool_start",  { id: toolId, name: fullName, displayName: bare });
    emit("tool_details",{ id: toolId, name: fullName, displayName: bare, input: input || {} });
    logToFile("tool", `Starting (grok): ${fullName}`);
    if (bare === "verify_scene") { verifierTools.add(toolId); emit("verifier_start", { toolUseId: toolId }); }
    recordToolUseForCtx(toolId, bare, input || {});
  }

  function emitToolResult(toolId, resultText, isError) {
    const shotMatch = resultText.match(/([\/][\w\/\-._]+\.png)/);
    if (shotMatch && fs.existsSync(shotMatch[1])) {
      latestShot = shotMatch[1];
      emit("screenshot", { toolUseId: toolId, filepath: `/api/screenshot/file?path=${encodeURIComponent(latestShot)}` });
    }
    emit("tool_result", { toolUseId: toolId, result: resultText.slice(0, 2000), isError });
    applyCtxOnToolResult(toolId, resultText, isError);
    if (verifierTools.has(toolId)) {
      let fb = "", ss = "";
      try { const r2 = JSON.parse(resultText); fb = r2.feedback || ""; ss = r2.screenshot || ""; } catch {}
      emit("verifier_result", {
        toolUseId: toolId,
        feedback: fb,
        screenshot: ss ? `/api/screenshot/file?path=${encodeURIComponent(ss)}` : "",
      });
      verifierTools.delete(toolId);
    }
  }

  // ---- ACP session/update notification → Studio SSE events -----------------
  function handleUpdate(params) {
    const u = params && params.update;
    if (!u) return;
    if (params.sessionId) session = params.sessionId;

    switch (u.sessionUpdate) {
      case "agent_thought_chunk":   // private reasoning — suppressed (matches other runners)
      case "user_message_chunk":    // echo of our own prompt
      case "available_commands_update":
      case "plan":
        return;

      case "agent_message_chunk": {
        const delta = contentToText(u.content);
        if (delta) emit("text", { delta });
        return;
      }

      case "tool_call": {
        const id   = u.toolCallId || u.tool_call_id || crypto.randomBytes(6).toString("hex");
        const name = toolNames.get(id) || u.title || "tool";
        emitToolStart(id, name, u.rawInput || u.input || {});
        return;
      }

      case "tool_call_update": {
        const id = u.toolCallId || u.tool_call_id;
        if (!id) return;
        if (!startedTools.has(id)) emitToolStart(id, toolNames.get(id) || u.title || "tool", u.rawInput || {});
        if (u.status === "completed" || u.status === "failed") {
          let text = contentToText(u.content);
          if (!text && u.rawOutput != null) text = typeof u.rawOutput === "string" ? u.rawOutput : JSON.stringify(u.rawOutput);
          emitToolResult(id, text, u.status === "failed");
        }
        return;
      }

      default:
        return; // unknown update kind — already persisted to raw_latest.jsonl
    }
  }

  // grok also emits proprietary _x.ai/session_notification events; the only one we mine is
  // tool_call_delta_chunk, which carries the precise raw tool `name` (the standard tool_call only
  // gives a human `title`). Capturing it improves MCP tool naming + ctx scene-tracking.
  function handleXaiNotification(params) {
    const u = params && params.update;
    if (!u) return;
    if (u.sessionUpdate === "tool_call_delta_chunk" && u.name) {
      const id = u.tool_call_id || u.toolCallId;
      if (id) toolNames.set(id, u.name);
    }
  }

  // ---- terminal: finalize the turn -----------------------------------------
  function finalize(stopReason, forceError) {
    if (gotResult) return;
    gotResult = true;
    clearInterval(idleTimer);
    const isError = forceError === true || (stopReason && !/^(end_turn|max_tokens|stop)$/i.test(stopReason));
    logToFile("grok", `End: stopReason=${stopReason || "?"} session=${session}`);
    logToFile("result", JSON.stringify({ provider: "grok", stopReason: stopReason || null }).slice(0, 500));
    const finish = () => {
      emit("done", { sessionId: STUDIO_SESSION, isError, costUsd: 0, latestScreenshot: latestShotUrl() });
      try { res.end(); } catch {}
      try { proc.kill("SIGTERM"); } catch {} // ACP agent is persistent — stop it after the turn
    };
    if (!MOCK_MODE) {
      snapshotScene(STUDIO_SESSION).then(finish).catch(err => {
        logToFile("ctx", `grok snapshotScene error: ${err.message}`);
        finish();
      });
    } else finish();
  }

  // ---- JSON-RPC 2.0 plumbing (newline-delimited over stdio) ----------------
  let rpcId = 0;
  const pending = new Map(); // request id → {resolve, reject}

  function rpcRequest(method, params) {
    const id = ++rpcId;
    const line = JSON.stringify({ jsonrpc: "2.0", id, method, params });
    try { proc.stdin.write(line + "\n"); } catch (e) { return Promise.reject(e); }
    return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
  }
  function rpcRespond(id, result) {
    try { proc.stdin.write(JSON.stringify({ jsonrpc: "2.0", id, result }) + "\n"); } catch {}
  }
  function rpcRespondError(id, code, msg) {
    try { proc.stdin.write(JSON.stringify({ jsonrpc: "2.0", id, error: { code, message: msg } }) + "\n"); } catch {}
  }

  // Server → client request (the agent asks us something). With --always-approve no permission
  // request should arrive, but answer it defensively; reject fs/terminal (we advertised none).
  function handleServerRequest(msg) {
    const method = msg.method || "";
    if (/permission/.test(method)) {
      const opts = (msg.params && msg.params.options) || [];
      const allow = opts.find(o => /allow/i.test(o.kind || o.optionId || "")) || opts[0];
      rpcRespond(msg.id, { outcome: allow ? { outcome: "selected", optionId: allow.optionId } : { outcome: "cancelled" } });
    } else {
      rpcRespondError(msg.id, -32601, `method not supported: ${method}`);
    }
  }

  function dispatch(msg) {
    // Response to one of our requests.
    if (msg.id != null && (msg.result !== undefined || msg.error !== undefined)) {
      const p = pending.get(msg.id);
      if (p) { pending.delete(msg.id); msg.error ? p.reject(Object.assign(new Error(msg.error.message || "rpc error"), { rpc: msg.error })) : p.resolve(msg.result); }
      return;
    }
    // Server → client request (has id + method).
    if (msg.id != null && msg.method) { handleServerRequest(msg); return; }
    // Notification (method, no id).
    if (msg.method === "session/update") { handleUpdate(msg.params); return; }
    if (msg.method === "_x.ai/session_notification") { handleXaiNotification(msg.params); return; }
    // Other _x.ai/* notifications (settings/models/mcp_initialized/prompt_complete) — ignored.
  }

  // ---- the ACP conversation ------------------------------------------------
  (async () => {
    await rpcRequest("initialize", {
      protocolVersion: 1,
      clientCapabilities: { fs: { readTextFile: false, writeTextFile: false }, terminal: false },
    });
    const sess = await rpcRequest("session/new", { cwd: grokCwd, mcpServers: [] });
    session = (sess && sess.sessionId) || session;
    // grok emits no separate init event — synthesize `system` so the UI shows the MCP indicator.
    emit("system", { sessionId: session, mcpServers: knownMcpServers.map(name => ({ name, status: "connected" })) });
    logToFile("grok", `Session ${session} | model=${(sess && sess.models && sess.models.currentModelId) || model || "?"}`);

    const result = await rpcRequest("session/prompt", { sessionId: session, prompt: [{ type: "text", text: fullPrompt }] });
    finalize((result && result.stopReason) || "end_turn");
  })().catch(err => {
    logToFile("grok-err", `ACP error: ${err.message}`);
    if (!gotResult) { emit("text", { delta: `\n\n⚠️ Grok Build error: ${err.message}\n` }); finalize(null, true); }
  });

  // ---- stdout line parser --------------------------------------------------
  function flushLine(line) {
    line = line.trim();
    if (!line) return;
    try { fs.appendFileSync(path.join(LOG_DIR, "raw_latest.jsonl"), line + "\n"); } catch {}
    let msg;
    try { msg = JSON.parse(line); } catch { return; } // ignore non-JSON banner/log lines
    try { dispatch(msg); }
    catch (e) { logToFile("grok", `dispatch error: ${e.message}`); }
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
    if (s) { stderrBuf += s + "\n"; logToFile("grok-stderr", s.slice(0, 300)); }
  });

  const idleTimer = setInterval(() => {
    if (Date.now() - lastOutputTime > GROK_IDLE_TIMEOUT_MS && !gotResult) {
      logToFile("grok", `Idle timeout (${GROK_IDLE_TIMEOUT_MS / 1000}s no output), killing process`);
      clearInterval(idleTimer);
      try { proc.kill("SIGTERM"); } catch {}
    }
  }, 10000);

  proc.on("close", code => {
    clearInterval(idleTimer);
    if (stdoutBuf.trim()) flushLine(stdoutBuf);
    logToFile("grok", `Process exited code=${code} gotResult=${gotResult}`);
    if (gotResult) return;
    if (!res.writableEnded) {
      const detail = stderrBuf.slice(0, 400).trim() || (code !== 0 ? `exit code ${code}` : "no output received");
      emit("text", { delta: `\n\n⚠️ Grok Build exited unexpectedly: ${detail}\n` });
      emit("done", { sessionId: STUDIO_SESSION, isError: true, latestScreenshot: latestShotUrl() });
      try { res.end(); } catch {}
    }
  });

  res.on("close", () => {
    if (!res.writableEnded) {
      clearInterval(idleTimer);
      try { proc.kill("SIGTERM"); } catch {}
      try { res.end(); } catch {}
      logToFile("grok", "Browser closed SSE — grok agent terminated");
    }
  });
}

module.exports = { runGrokChat };

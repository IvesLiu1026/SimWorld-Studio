"use strict";

// cursor-runner.js — drives the Cursor Agent CLI (`cursor-agent -p ... --output-format
// stream-json`) as a drop-in coding agent for /api/chat, mirroring gemini-runner.js /
// codex-runner.js / opencode-runner.js.
//
// The frontend talks SSE to /api/chat and expects Claude Code-shaped events
// (system/text/tool_start/tool_details/tool_result/screenshot/done). Cursor's
// `--output-format stream-json` is deliberately Claude-Code-compatible: newline-delimited
// JSON with `{type:"system",subtype:"init",...}`, `{type:"assistant",message:{content:[...]}}`,
// `{type:"user",message:{content:[tool_result...]}}`, and `{type:"result",...}`. Assistant
// `content` blocks carry `{type:"text",text}` and `{type:"tool_call"|"tool_use", ...}`.
// We translate that vocabulary into the Claude-shaped events the React side already renders.
//
// MCP: cursor-agent reads MCP servers from `.cursor/mcp.json` in the working dir (same
// `{mcpServers:{...}}` schema as web/mcp.json). We mirror web/mcp.json into a scratch dir's
// .cursor/mcp.json (rewriting UNREAL_PORT to the live engine port) and run from there — the
// same auto-wiring approach as the codex/opencode paths. `--force` makes cursor-agent run
// non-interactively and auto-approve tool/MCP calls (yolo equivalent). Auth comes from the
// user's existing `cursor-agent login`.
//
// System prompt: cursor-agent has no append-system-prompt flag, so we prepend it to the
// user message (same as codex/opencode).

const fs    = require("fs");
const os    = require("os");
const path  = require("path");
const crypto= require("crypto");
const { spawn } = require("child_process");
const { stripToolPrefix } = require("./gemini-runner");
const { attachBuilderRuntimeProcess, buildBuilderChildEnv } = require("./builder-runtime-authority");

const CURSOR_BIN  = process.env.CURSOR_BIN  || "cursor-agent";
const CURSOR_MODEL = process.env.CURSOR_MODEL || ""; // empty → CLI/account default
const CURSOR_IDLE_TIMEOUT_MS = parseInt(process.env.CURSOR_IDLE_TIMEOUT_MS || "1800000", 10);

// Mirror web/mcp.json → <scratch>/.cursor/mcp.json so cursor-agent auto-loads the simworld
// MCP server (rewriting UNREAL_PORT to the live engine port). Returns the scratch cwd.
function ensureCursorWorkspace(mcpConfigPath, unrealPort, logToFile) {
  const cwd = path.join(os.tmpdir(), "simworld-cursor");
  try {
    fs.mkdirSync(path.join(cwd, ".cursor"), { recursive: true });
    const mcp = JSON.parse(fs.readFileSync(mcpConfigPath, "utf-8"));
    const servers = {};
    for (const [name, cfg] of Object.entries(mcp.mcpServers || {})) {
      const env = { ...(cfg.env || {}) };
      if (unrealPort) env.UNREAL_PORT = String(unrealPort);
      servers[name] = { command: cfg.command, args: cfg.args || [], env };
    }
    fs.writeFileSync(
      path.join(cwd, ".cursor", "mcp.json"),
      JSON.stringify({ mcpServers: servers }, null, 2),
    );
    logToFile && logToFile("cursor", `mirrored mcp.json → ${path.join(cwd, ".cursor", "mcp.json")}`);
  } catch (e) {
    logToFile && logToFile("cursor", `failed to mirror mcp.json: ${e.message}`);
  }
  return cwd;
}

/**
 * @param {object} args  req, res, body{message,sessionId,model,...}, systemPrompt, ctx
 *   ctx: { ctxManager, snapshotScene, STUDIO_SESSION, MCP_CONFIG, UNREAL_PORT,
 *          LOG_DIR, SCREENSHOT_DIR, _chatProcs, logToFile, MOCK_MODE }
 */
function runCursorChat({ req, res, body, systemPrompt, ctx }) {
  const { message, sessionId } = body || {};
  const model = (body && body.model) || CURSOR_MODEL;
  const {
    ctxManager, snapshotScene, STUDIO_SESSION,
    MCP_CONFIG, UNREAL_PORT, LOG_DIR, SCREENSHOT_DIR,
    _chatProcs, logToFile, MOCK_MODE, BUILDER_RUNTIME,
  } = ctx;

  const cursorCwd = ensureCursorWorkspace(MCP_CONFIG, UNREAL_PORT, logToFile);

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

  // cursor-agent has no system-prompt flag — prepend it to the user message.
  const fullPrompt = systemPrompt ? `${systemPrompt}\n\n---\n\n${message}` : String(message || "");

  // `--print` is a BOOLEAN flag (non-interactive/headless); the prompt is a POSITIONAL arg
  // passed last — NOT `-p <prompt>`. `--force` auto-allows tool/shell calls, `--approve-mcps`
  // auto-approves MCP servers, `--trust` trusts the workspace (both required in headless mode
  // so the simworld MCP loads without an interactive prompt). SECURITY: safe because
  // agent-sandbox.js wraps this process in bwrap with the repo bound READ-ONLY — the OS
  // sandbox is the real guardrail, not cursor's. We also pass --sandbox disabled so cursor's
  // own sandbox doesn't fight bwrap.
  const args = [
    "--print",
    "--output-format", "stream-json",
    "--force",
    "--approve-mcps",
    "--trust",
    "--sandbox", "disabled",
  ];
  if (model) args.push("--model", model);
  args.push(fullPrompt); // positional prompt — must be last

  const env = buildBuilderChildEnv(process.env, BUILDER_RUNTIME);
  // Don't let Claude session env vars leak into cursor-agent.
  Object.keys(env).forEach(k => { if (k.startsWith("CLAUDE")) delete env[k]; });
  // The cursor-agent installer drops a stable symlink in ~/.local/bin, which a tmux-launched
  // server may not have on PATH. Prepend it so the default CURSOR_BIN ("cursor-agent") resolves.
  const localBin = path.join(os.homedir(), ".local", "bin");
  env.PATH = `${localBin}:${env.PATH || ""}`;

  logToFile("cursor", `User: "${String(message).slice(0, 200)}" model=${model || "default"} sessionId=${sessionId || "new"}`);
  try { fs.writeFileSync(path.join(LOG_DIR, "raw_latest.jsonl"), ""); } catch {}

  // OS sandbox: repo read-only so the agent can't modify Studio source (see agent-sandbox.js).
  const _sb = require("./agent-sandbox").sandboxedSpawn(CURSOR_BIN, args, cursorCwd);
  const proc = spawn(_sb.cmd, _sb.args, {
    cwd: cursorCwd,
    env,
    stdio: ["ignore", "pipe", "pipe"],
  });
  attachBuilderRuntimeProcess(BUILDER_RUNTIME, proc);

  const procKey = BUILDER_RUNTIME ? BUILDER_RUNTIME.scopeId : (sessionId || "_global");
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
  const toolInputs   = new Map(); // tool id → {name, input} for ctx tracking

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

  // ---- ctxManager hooks (mirror Claude/Gemini/Codex paths) -----------------
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
      logToFile("ctx", `cursor parse error: ${e.message}`);
    }
    toolInputs.delete(toolId);
  }

  function emitToolStart(toolId, fullName, input) {
    if (startedTools.has(toolId)) return;
    startedTools.add(toolId);
    const bare = stripToolPrefix(fullName, knownMcpServers);
    emit("tool_start",  { id: toolId, name: fullName, displayName: bare });
    emit("tool_details",{ id: toolId, name: fullName, displayName: bare, input: input || {} });
    logToFile("tool", `Starting (cursor): ${fullName}`);
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
  }

  // Normalize a Cursor assistant content block (text only — Cursor delivers tool calls as
  // their own top-level `tool_call` events, see handleCursorToolCall, not as content blocks).
  function handleContentBlock(blk) {
    if (!blk || typeof blk !== "object") return;
    if (blk.type === "text" && typeof blk.text === "string" && blk.text.length) {
      emit("text", { delta: blk.text });
    }
  }

  // Top-level `tool_call` events (verified against cursor-agent 2026.06.02). Shape:
  //   {type:"tool_call", subtype:"started"|"completed", call_id, tool_call:{ <kind>:{ args, result, description } }}
  // where <kind> is e.g. shellToolCall / mcpToolCall / readToolCall / writeToolCall. The
  // result lives at tool_call.<kind>.result.{success|error}; shell carries stdout/exitCode.
  function handleCursorToolCall(ev) {
    const toolId = ev.call_id || ev.id || crypto.randomBytes(6).toString("hex");
    const tc = ev.tool_call || {};
    const kind = Object.keys(tc)[0] || "tool";
    const detail = tc[kind] || {};
    const args = detail.args || detail.arguments || {};

    // Derive a display name + input depending on the tool kind.
    let fullName, input;
    if (kind === "mcpToolCall") {
      const server = detail.server || detail.serverName || args.server || "";
      const tool   = detail.tool || detail.name || detail.toolName || args.tool || args.name || "tool";
      fullName = server ? `mcp__${server}__${tool}` : tool;
      input    = args.arguments || args.input || args;
    } else if (kind === "shellToolCall") {
      fullName = "shell";
      input    = { command: args.command };
    } else {
      fullName = kind.replace(/ToolCall$/, "") || "tool"; // readToolCall → read, etc.
      input    = args;
    }

    if (!startedTools.has(toolId)) emitToolStart(toolId, fullName, input);

    if (ev.subtype === "completed") {
      const r = detail.result || {};
      let resultText = "";
      let isError = false;
      if (r.success != null) {
        const s = r.success;
        if (typeof s === "string") resultText = s;
        else if (s.stdout != null || s.stderr != null) {
          resultText = String(s.stdout || "") + (s.stderr ? `\n${s.stderr}` : "");
          if (s.exitCode != null && s.exitCode !== 0) isError = true;
        } else resultText = JSON.stringify(s);
      } else if (r.error != null) {
        isError = true;
        resultText = typeof r.error === "string" ? r.error : JSON.stringify(r.error);
      } else if (detail.result != null) {
        resultText = typeof detail.result === "string" ? detail.result : JSON.stringify(detail.result);
      }
      emitToolResult(toolId, resultText, isError);
    }
  }

  // ---- top-level stream-json event dispatch --------------------------------
  function handleEvent(ev) {
    if (!ev || typeof ev !== "object") return;
    const t = ev.type;

    if (t === "system") {
      if (ev.session_id) session = ev.session_id;
      // Prefer Cursor's enumerated MCP servers; fall back to what's in mcp.json so the
      // UI's "MCP: simworld:connected" indicator still renders.
      let mcpServers = [];
      if (Array.isArray(ev.mcp_servers) && ev.mcp_servers.length) {
        mcpServers = ev.mcp_servers.map(s =>
          typeof s === "string" ? { name: s, status: "connected" }
                                : { name: s.name, status: s.status || "connected" });
      } else {
        try {
          const mcp = JSON.parse(fs.readFileSync(MCP_CONFIG, "utf-8"));
          mcpServers = Object.keys(mcp.mcpServers || {}).map(name => ({ name, status: "connected" }));
        } catch {}
      }
      emit("system", { sessionId: session, mcpServers });
      logToFile("cursor", `Session ${session} | model=${ev.model || model || "?"}`);
      return;
    }

    if (t === "assistant") {
      if (ev.session_id) session = ev.session_id;
      const content = ev.message && ev.message.content;
      if (Array.isArray(content)) content.forEach(handleContentBlock);
      else if (typeof content === "string" && content.length) emit("text", { delta: content });
      return;
    }

    if (t === "user") {
      // Cursor emits a `user` event that ECHOES the user's own prompt as a text block, and
      // also carries tool_result blocks. Surface ONLY tool results — never the echoed text,
      // or the prompt would be re-printed into the assistant stream.
      if (ev.session_id) session = ev.session_id;
      const content = ev.message && ev.message.content;
      if (Array.isArray(content)) {
        content.forEach(blk => { if (blk && blk.type === "tool_result") handleContentBlock(blk); });
      }
      return;
    }

    // Cursor delivers every tool invocation (shell, MCP, read/write, …) as a top-level
    // `tool_call` event with subtype started/completed.
    if (t === "tool_call") { handleCursorToolCall(ev); return; }

    // `thinking` (subtype delta/completed) is the model's private reasoning — not surfaced
    // as chat text (intentionally falls through to no-op, matching the other runners).

    if (t === "result") {
      gotResult = true;
      clearInterval(idleTimer);
      const isError = ev.is_error === true || ev.subtype === "error" || ev.status === "error";
      // A non-streamed final answer may only appear here.
      if (typeof ev.result === "string" && ev.result.length && !ev.is_error) {
        // Avoid double-printing if it was already streamed: only emit when nothing streamed.
      }
      logToFile("cursor", `Result: subtype=${ev.subtype || ev.status} session=${session}`);
      logToFile("result", JSON.stringify({ provider: "cursor", subtype: ev.subtype, duration_ms: ev.duration_ms }).slice(0, 500));

      const finish = () => {
        emit("done", {
          sessionId: STUDIO_SESSION,
          isError,
          costUsd: ev.total_cost_usd || 0,
          latestScreenshot: latestShotUrl(),
        });
        try { res.end(); } catch {}
      };
      if (!MOCK_MODE) {
        snapshotScene(STUDIO_SESSION, BUILDER_RUNTIME).then(finish).catch(err => {
          logToFile("ctx", `cursor snapshotScene error: ${err.message}`);
          finish();
        });
      } else finish();
      return;
    }

    if (t === "error") {
      const msg = (ev.error && ev.error.message) || ev.message || "cursor error";
      logToFile("cursor-err", String(msg).slice(0, 400));
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
    catch (e) { logToFile("cursor", `event handler error: ${e.message}`); }
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
    if (s) { stderrBuf += s + "\n"; logToFile("cursor-stderr", s.slice(0, 300)); }
  });

  const idleTimer = setInterval(() => {
    if (Date.now() - lastOutputTime > CURSOR_IDLE_TIMEOUT_MS && !gotResult) {
      logToFile("cursor", `Idle timeout (${CURSOR_IDLE_TIMEOUT_MS / 1000}s no output), killing process`);
      clearInterval(idleTimer);
      try { proc.kill("SIGTERM"); } catch {}
    }
  }, 10000);

  proc.on("close", code => {
    clearInterval(idleTimer);
    if (stdoutBuf.trim()) flushLine(stdoutBuf);
    logToFile("cursor", `Process exited code=${code} gotResult=${gotResult}`);
    if (gotResult) return;
    if (!res.writableEnded) {
      const detail = stderrBuf.slice(0, 400).trim() || (code !== 0 ? `exit code ${code}` : "no output received");
      emit("text", { delta: `\n\n⚠️ Cursor agent exited unexpectedly: ${detail}\n` });
      emit("done", { sessionId: STUDIO_SESSION, isError: true, latestScreenshot: latestShotUrl() });
      try { res.end(); } catch {}
    }
  });

  res.on("close", () => {
    if (!res.writableEnded) {
      clearInterval(idleTimer);
      try { res.end(); } catch {}
      logToFile("cursor", "Browser closed SSE — cursor continues in background (use /api/chat-stop to kill)");
    }
  });
}

module.exports = { runCursorChat };

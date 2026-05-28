"use strict";

// gemini-runner.js — drives the Gemini CLI as a drop-in coding agent for /api/chat.
//
// The frontend talks SSE to /api/chat and expects events shaped like Claude Code's
// stream-json (system/text/tool_start/tool_details/tool_input/tool_result/screenshot/
// verifier_*/done). This module spawns `gemini -p ... --output-format stream-json
// --approval-mode yolo` and translates its event vocabulary
// (init/message/tool_use/tool_result/error/result) into that shape so nothing on the
// React side has to change.
//
// MCP wiring: the simworld MCP server lives in web/mcp.json. Gemini-cli doesn't take
// an --mcp-config flag — it reads `.gemini/settings.json` from cwd. We mirror mcp.json
// into web/.gemini/settings.json once at module load, adding `trust:true` so YOLO mode
// invokes MCP tools without prompting.
//
// System prompt: Claude takes one via --append-system-prompt. Gemini takes one via the
// GEMINI_SYSTEM_MD env var pointing at a file (REPLACE semantics, not append). For each
// call we write the prompt to a temp file under LOG_DIR and point the env var at it.

const fs    = require("fs");
const path  = require("path");
const os    = require("os");
const crypto= require("crypto");
const { spawn } = require("child_process");

const GEMINI_BIN   = process.env.GEMINI_BIN   || "gemini";
const GEMINI_MODEL = process.env.GEMINI_MODEL || ""; // empty → cli default ("auto")
const GEMINI_IDLE_TIMEOUT_MS = parseInt(process.env.GEMINI_IDLE_TIMEOUT_MS || "1800000", 10);

// ---- MCP settings mirror ---------------------------------------------------

let _settingsWritten = false;

// Mirror mcp.json → <web>/.gemini/settings.json (gemini-cli's workspace settings) and
// produce <web>/.gemini/trustedFolders.json so the workspace is trusted (gemini
// disables MCP servers in "untrusted" folders even when --skip-trust is passed at the
// command line — the trust check on MCP loading consults this file). The trust file
// is wired in via GEMINI_CLI_TRUSTED_FOLDERS_PATH so nothing in ~/.gemini is touched.
function _ensureGeminiSettings(mcpConfigPath, geminiCwd, logToFile) {
  const settingsDir = path.join(geminiCwd, ".gemini");
  const settingsPath = path.join(settingsDir, "settings.json");
  const trustPath    = path.join(settingsDir, "trustedFolders.json");
  if (_settingsWritten) return { settingsPath, trustPath };
  try {
    const mcp = JSON.parse(fs.readFileSync(mcpConfigPath, "utf-8"));
    const servers = {};
    for (const [name, cfg] of Object.entries(mcp.mcpServers || {})) {
      servers[name] = { ...cfg, trust: true };
    }
    fs.mkdirSync(settingsDir, { recursive: true });
    let prev = null;
    try { prev = JSON.parse(fs.readFileSync(settingsPath, "utf-8")); } catch {}
    // Preserve any unrelated keys the user added (themes, telemetry, etc.).
    // SECURITY: scene-gen agent — exclude the built-in shell/file tools so it can ONLY use
    // the simworld MCP tools (cannot read or modify Studio source).
    const merged = { ...(prev || {}), mcpServers: servers,
      excludeTools: ["run_shell_command", "write_file", "replace", "read_file", "read_many_files",
        "glob", "search_file_content", "list_directory", "web_fetch", "google_web_search", "save_memory"] };
    fs.writeFileSync(settingsPath, JSON.stringify(merged, null, 2));

    // Trust the workspace itself so MCP servers load. Values: TRUST_FOLDER (this dir),
    // TRUST_PARENT (this dir and descendants), DO_NOT_TRUST.
    const trustEntry = { [geminiCwd]: "TRUST_FOLDER" };
    let prevTrust = null;
    try { prevTrust = JSON.parse(fs.readFileSync(trustPath, "utf-8")); } catch {}
    const mergedTrust = { ...(prevTrust || {}), ...trustEntry };
    fs.writeFileSync(trustPath, JSON.stringify(mergedTrust, null, 2));

    logToFile && logToFile("gemini", `mirrored mcp.json → ${settingsPath} (trusted ${geminiCwd})`);
    _settingsWritten = true;
  } catch (e) {
    logToFile && logToFile("gemini", `failed to mirror mcp.json: ${e.message}`);
  }
  return { settingsPath, trustPath };
}

// ---- Tool-name prefix stripping --------------------------------------------
// Claude exposes MCP tools as `mcp__simworld__spawn_blueprint_actor` (double
// underscores). Gemini exposes them as `mcp_simworld_take_screenshot` (single
// underscores), where the tool name itself can also contain underscores (e.g.
// `spawn_blueprint_actor`) — so we can't just split on `_`. Strip the prefix only
// when the segment after `mcp_` matches a known MCP server name from mcp.json.
function stripToolPrefix(name, knownServers) {
  if (!name) return name;
  const s = String(name);
  // Claude-style: mcp__<server>__<tool>
  const m = s.match(/^mcp__[\w-]+__(.+)$/);
  if (m) return m[1];
  // Gemini-style: mcp_<server>_<tool> — only strip if <server> is a known server.
  if (Array.isArray(knownServers) && knownServers.length) {
    for (const srv of knownServers) {
      const a = `mcp_${srv}_`;
      if (s.startsWith(a)) return s.slice(a.length);
      const b = `${srv}_`;
      if (s.startsWith(b)) return s.slice(b.length);
    }
  }
  return s;
}

// ---- Main runner -----------------------------------------------------------

/**
 * @param {object} args
 *   req, res                 — Express request/response (SSE)
 *   body                     — { message, sessionId, skills, feedback }
 *   systemPrompt             — full system prompt string (same one Claude path uses)
 *   ctx                      — { ctxManager, snapshotScene, STUDIO_SESSION,
 *                                MCP_CONFIG, LOG_DIR, SCREENSHOT_DIR,
 *                                _chatProcs, logToFile, MOCK_MODE }
 */
function runGeminiChat({ req, res, body, systemPrompt, ctx }) {
  const { message, sessionId, skills, feedback } = body || {};
  // Per-request model from the UI's Model dropdown; falls back to GEMINI_MODEL env, then CLI default.
  const model = (body && body.model) || GEMINI_MODEL;
  const {
    ctxManager, snapshotScene, STUDIO_SESSION,
    MCP_CONFIG, LOG_DIR, SCREENSHOT_DIR,
    _chatProcs, logToFile, MOCK_MODE,
  } = ctx;

  const geminiCwd = path.resolve(__dirname, "..");
  const { trustPath } = _ensureGeminiSettings(MCP_CONFIG, geminiCwd, logToFile);

  // SSE headers (mirror what /api/chat sets for the Claude path). The chat handler
  // sets these before us, so guard with headersSent to stay idempotent.
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

  // Persist the system prompt to a temp file for GEMINI_SYSTEM_MD.
  let systemMdPath = null;
  try {
    const tmpDir = path.join(LOG_DIR, "gemini-system");
    fs.mkdirSync(tmpDir, { recursive: true });
    systemMdPath = path.join(
      tmpDir,
      `system-${Date.now()}-${crypto.randomBytes(4).toString("hex")}.md`,
    );
    fs.writeFileSync(systemMdPath, systemPrompt || "", "utf-8");
  } catch (e) {
    logToFile("gemini", `failed to write system prompt: ${e.message}`);
  }

  const args = [
    "-p", message,
    "--output-format", "stream-json",
    "--approval-mode", "yolo",
    // Without --skip-trust, gemini downgrades yolo → default for "untrusted"
    // workspaces, which then hangs/fails non-interactive tool approvals.
    // The flag is misnamed: it actually means "trust this workspace for this session".
    "--skip-trust",
  ];
  if (model) args.push("--model", model);

  const env = { ...process.env };
  // Don't let Claude session env vars leak into gemini.
  Object.keys(env).forEach(k => { if (k.startsWith("CLAUDE")) delete env[k]; });
  if (systemMdPath) env.GEMINI_SYSTEM_MD = systemMdPath;
  if (trustPath)    env.GEMINI_CLI_TRUSTED_FOLDERS_PATH = trustPath;

  logToFile("gemini", `User: "${String(message).slice(0, 200)}" sessionId=${sessionId || "new"}`);
  try { fs.writeFileSync(path.join(LOG_DIR, "raw_latest.jsonl"), ""); } catch {}

  const proc = spawn(GEMINI_BIN, args, {
    cwd: geminiCwd,
    env,
    stdio: ["ignore", "pipe", "pipe"],
  });

  // Replace any prior chat process for this session (matches Claude behavior).
  const procKey = sessionId || "_global";
  const prior = _chatProcs.get(procKey);
  if (prior && !prior.killed) { try { prior.kill("SIGTERM"); } catch {} }
  _chatProcs.set(procKey, proc);
  proc.on("exit", () => { if (_chatProcs.get(procKey) === proc) _chatProcs.delete(procKey); });

  // ---- per-call state ------------------------------------------------------
  let stdoutBuf       = "";
  let stderrBuf       = "";
  let session         = sessionId || null;
  let latestShot      = null;             // last screenshot path detected in tool results
  const startedTools  = new Set();        // tool_id → started (avoid duplicate tool_start)
  const verifierTools = new Set();        // tool_id of verify_scene calls
  const toolInputs    = new Map();        // tool_id → {name, input} for ctx tracking
  let gotResult       = false;
  let lastOutputTime  = Date.now();
  // Known MCP server names parsed from mcp.json once; used by stripToolPrefix
  // to disambiguate `mcp_<server>_<tool>` from tools whose names contain underscores.
  let knownMcpServers = [];
  try {
    const mcp = JSON.parse(fs.readFileSync(MCP_CONFIG, "utf-8"));
    knownMcpServers = Object.keys(mcp.mcpServers || {});
  } catch {}

  // Sweep SCREENSHOT_DIR for the freshest .png (used on done if tool results didn't surface one).
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

  // ---- ctxManager hooks (mirror Claude path) -------------------------------
  ctxManager.resolveSession(STUDIO_SESSION);
  ctxManager.beginRound(STUDIO_SESSION);

  const CTX_TOOLS = new Set([
    "spawn_blueprint_actor", "spawn_actor", "spawn_agent",
    "delete_actor", "delete_all_spawned", "setup_environment",
  ]);

  function recordToolUseForCtx(toolId, bareName, input) {
    if (CTX_TOOLS.has(bareName)) {
      toolInputs.set(toolId, { name: bareName, input: input || {} });
    }
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
      logToFile("ctx", `gemini parse error: ${e.message}`);
    }
    toolInputs.delete(toolId);
  }

  // ---- gemini stream-json event dispatch -----------------------------------
  function handleEvent(ev) {
    if (!ev || typeof ev !== "object") return;
    const t = ev.type;

    if (t === "init") {
      if (ev.session_id) session = ev.session_id;
      // Gemini doesn't enumerate MCP servers in init; surface what's in mcp.json so the
      // UI's "MCP: simworld:connected" indicator still has something to render.
      let mcpServers = [];
      try {
        const mcp = JSON.parse(fs.readFileSync(MCP_CONFIG, "utf-8"));
        mcpServers = Object.keys(mcp.mcpServers || {}).map(name => ({ name, status: "connected" }));
      } catch {}
      emit("system", { sessionId: session, mcpServers });
      logToFile("gemini", `Session ${session} | model=${ev.model || "?"}`);
      return;
    }

    if (t === "message") {
      if (ev.role === "assistant" && typeof ev.content === "string" && ev.content.length) {
        emit("text", { delta: ev.content });
      }
      return;
    }

    if (t === "tool_use") {
      const toolId   = ev.tool_id || ev.id || crypto.randomBytes(6).toString("hex");
      const fullName = ev.tool_name || ev.name || "tool";
      const bare     = stripToolPrefix(fullName, knownMcpServers);
      const input    = ev.parameters || ev.input || {};

      if (!startedTools.has(toolId)) {
        startedTools.add(toolId);
        emit("tool_start",  { id: toolId, name: fullName, displayName: bare });
        emit("tool_details",{ id: toolId, name: fullName, displayName: bare, input });
        logToFile("tool", `Starting (gemini): ${fullName}`);
        if (bare === "verify_scene") {
          verifierTools.add(toolId);
          emit("verifier_start", { toolUseId: toolId });
        }
      }
      recordToolUseForCtx(toolId, bare, input);
      return;
    }

    if (t === "tool_result") {
      const toolId = ev.tool_id || ev.id;
      const status = ev.status || (ev.error ? "error" : "success");
      const isError = status === "error";
      let resultText = "";
      if (typeof ev.output === "string") resultText = ev.output;
      else if (ev.output != null) resultText = JSON.stringify(ev.output);
      else if (ev.error && ev.error.message) resultText = String(ev.error.message);

      const shotMatch = resultText.match(/([\/][\w\/\-._]+\.png)/);
      if (shotMatch && fs.existsSync(shotMatch[1])) {
        latestShot = shotMatch[1];
        emit("screenshot", {
          toolUseId: toolId,
          filepath: `/api/screenshot/file?path=${encodeURIComponent(latestShot)}`,
        });
      }
      emit("tool_result", { toolUseId: toolId, result: resultText.slice(0, 2000), isError });
      logToFile("tool_result", `${(toolId || "").slice(0, 8)} → ${resultText.slice(0, 300)}`);

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
      return;
    }

    if (t === "error") {
      const sev = ev.severity || "error";
      const msg = ev.message || "";
      logToFile("gemini-err", `${sev}: ${msg}`.slice(0, 400));
      if (sev !== "warning") {
        emit("text", { delta: `\n\n⚠️ ${msg}\n` });
      }
      return;
    }

    if (t === "result") {
      gotResult = true;
      clearInterval(idleTimer);
      const isError = ev.status === "error";
      const stats   = ev.stats || {};
      logToFile("gemini", `Result: status=${ev.status} session=${session}`);
      logToFile("result", JSON.stringify({ provider: "gemini", status: ev.status, stats }).slice(0, 500));

      const finish = () => {
        emit("done", {
          sessionId: STUDIO_SESSION,
          isError,
          costUsd: stats.total_cost_usd || 0,
          latestScreenshot: latestShotUrl(),
        });
        try { res.end(); } catch {}
        if (systemMdPath) { try { fs.unlinkSync(systemMdPath); } catch {} }
      };
      if (!MOCK_MODE) {
        snapshotScene(STUDIO_SESSION).then(finish).catch(err => {
          logToFile("ctx", `gemini snapshotScene error: ${err.message}`);
          finish();
        });
      } else finish();
      return;
    }
  }

  // ---- line-delimited stdout parser ----------------------------------------
  function flushLine(line) {
    line = line.trim();
    if (!line) return;
    try { fs.appendFileSync(path.join(LOG_DIR, "raw_latest.jsonl"), line + "\n"); } catch {}
    let ev;
    try { ev = JSON.parse(line); } catch { return; }
    try { handleEvent(ev); }
    catch (e) { logToFile("gemini", `event handler error: ${e.message}`); }
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
    if (s) {
      stderrBuf += s + "\n";
      logToFile("gemini-stderr", s.slice(0, 300));
    }
  });

  const idleTimer = setInterval(() => {
    if (Date.now() - lastOutputTime > GEMINI_IDLE_TIMEOUT_MS && !gotResult) {
      logToFile("gemini", `Idle timeout (${GEMINI_IDLE_TIMEOUT_MS / 1000}s no output), killing process`);
      clearInterval(idleTimer);
      try { proc.kill("SIGTERM"); } catch {}
    }
  }, 10000);

  proc.on("close", code => {
    clearInterval(idleTimer);
    if (stdoutBuf.trim()) flushLine(stdoutBuf);
    logToFile("gemini", `Process exited code=${code} gotResult=${gotResult}`);
    if (gotResult) return;
    if (!res.writableEnded) {
      const detail = stderrBuf.slice(0, 400).trim() || (code !== 0 ? `exit code ${code}` : "no output received");
      emit("text", { delta: `\n\n⚠️ Gemini agent exited unexpectedly: ${detail}\n` });
      emit("done", { sessionId: STUDIO_SESSION, isError: true, latestScreenshot: latestShotUrl() });
      try { res.end(); } catch {}
    }
    if (systemMdPath) { try { fs.unlinkSync(systemMdPath); } catch {} }
  });

  res.on("close", () => {
    if (!res.writableEnded) {
      clearInterval(idleTimer);
      try { res.end(); } catch {}
      logToFile("gemini", "Browser closed SSE — gemini continues in background (use /api/chat-stop to kill)");
    }
  });
}

module.exports = { runGeminiChat, stripToolPrefix };

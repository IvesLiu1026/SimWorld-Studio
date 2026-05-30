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

const NL = String.fromCharCode(10);
const CODEX_BIN = process.env.CODEX_BIN || "codex";
const CODEX_MODEL = process.env.CODEX_MODEL || "gpt-5.5";
const CODEX_TIMEOUT_MS = parseInt(process.env.CODEX_TIMEOUT_MS || "900000", 10); // 15 min default
const CODEX_SANDBOX = process.env.CODEX_SANDBOX || "/tmp/experiment_sandbox";

// MCP config passed to Codex so it can call the same SimWorld MCP tools (spawn_blueprint_actor, etc.)
function buildCodexMcpArgs(opts) {
  const mcpServerJs = opts.mcpServerJs;
  const uePort = opts.uePort || process.env.UNREAL_PORT || "55561";
  const ueHost = opts.ueHost || process.env.UNREAL_HOST || "127.0.0.1";
  const assetLib = opts.assetLibraryPath || process.env.ASSET_LIBRARY_PATH || "";
  return [
    "-c", `mcp_servers.simworld.command="node"`,
    "-c", `mcp_servers.simworld.args=["${mcpServerJs}"]`,
    "-c",
    `mcp_servers.simworld.env={UNREAL_HOST="${ueHost}",UNREAL_PORT="${uePort}"${assetLib ? `,ASSET_LIBRARY_PATH="${assetLib}"` : ""}}`,
  ];
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

  const args = [
    "exec",
    "--json",
    "--skip-git-repo-check",
    "--dangerously-bypass-approvals-and-sandbox",
    "-C", CODEX_SANDBOX,
    "-m", CODEX_MODEL,
    ...buildCodexMcpArgs({
      mcpServerJs: options.mcpServerJs,
      uePort: options.uePort,
      ueHost: options.ueHost,
      assetLibraryPath: options.assetLibraryPath,
    }),
    "-",
  ];

  try { fs.mkdirSync(CODEX_SANDBOX, { recursive: true }); } catch (_e) {}
  const env = Object.assign({}, process.env, { NO_COLOR: "1" });
  // Don't propagate Claude-prefixed env vars (avoid auth confusion)
  Object.keys(env).forEach((k) => { if (k.startsWith("CLAUDE")) delete env[k]; });

  const log = options.logToFile || (() => {});
  log("codex", `[builder] model=${CODEX_MODEL} sessionId=${sessionId} prompt_chars=${fullPrompt.length}`);

  const proc = spawn(CODEX_BIN, args, {
    cwd: options.cwd || CODEX_SANDBOX,
    env,
    stdio: ["pipe", "pipe", "pipe"],
  });

  // Send the prompt over stdin and close
  try {
    proc.stdin.write(fullPrompt);
    proc.stdin.end();
  } catch (e) {
    log("codex", `[builder] stdin write failed: ${e.message}`);
  }

  let stdoutBuf = "";
  let stderrBuf = "";
  let isError = false;
  let latestScreenshot = null;
  let finalText = "";
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
    if (typ === "turn.completed") return;
  }

  proc.on("close", (code) => {
    clearInterval(idleTimer);
    if (stdoutBuf.trim()) handleCodexLine(stdoutBuf);
    const exitErr = code !== 0;
    if (exitErr) isError = true;
    log("codex", `[builder] exited code=${code} isError=${isError} stderr_tail=${stderrBuf.slice(-200)}`);
    onDone({ isError, latestScreenshot, finalText, returnCode: code });
  });
  proc.on("error", (e) => {
    clearInterval(idleTimer);
    log("codex", `[builder] proc error: ${e.message}`);
    isError = true;
    onDone({ isError, latestScreenshot, finalText, returnCode: -1, error: e.message });
  });

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

  const options = {
    arenaSystemPrompt: arenaPrompt,
    sceneContext,
    studioSession: deps.studioSession,
    logToFile: deps.logToFile,
    mcpServerJs: deps.mcpServerJs || path.resolve(__dirname, "mcp-server.js"),
    uePort: process.env.UNREAL_PORT || "55561",
    ueHost: process.env.UNREAL_HOST || "127.0.0.1",
    assetLibraryPath: process.env.ASSET_LIBRARY_PATH || "",
  };

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
}

module.exports = { handleCodexChat, spawnCodexAgent, CODEX_MODEL };

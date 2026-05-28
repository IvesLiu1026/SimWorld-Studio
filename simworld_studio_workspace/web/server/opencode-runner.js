"use strict";

// opencode-runner.js — drives the OpenCode CLI (`opencode run`) as a drop-in coding
// agent for /api/chat, mirroring gemini-runner.js / codex-runner.js.
//
// ⚠️ UNTESTED: authored without a local `opencode` CLI on this machine. The spawn flags
// and output handling below follow OpenCode's documented `run` interface and need a live
// verification pass once `opencode` is installed (see README → Coding Agent Backends).
// Because OpenCode's `run` command streams human-readable assistant text on stdout (no
// stable stream-json contract like Claude/Gemini/Codex expose), this translator relays
// assistant output as `text` deltas and best-effort detects screenshot paths; rich
// tool_start/tool_result framing may need refinement against real output.
//
// MCP: OpenCode reads MCP servers from an `opencode.json` (`mcp` section) in the project
// or ~/.config/opencode. The simworld MCP server must be added there for scene tools to
// work — see README. We do not inject it here (OpenCode has no CLI MCP-override flag).
//
// System prompt: prepended to the user message (OpenCode takes per-run system prompts via
// agent config / AGENTS.md, not a CLI flag).

const fs    = require("fs");
const path  = require("path");
const { spawn } = require("child_process");

const OPENCODE_BIN   = process.env.OPENCODE_BIN   || "opencode";
const OPENCODE_MODEL = process.env.OPENCODE_MODEL || ""; // empty → opencode default; form: provider/model
const OPENCODE_IDLE_TIMEOUT_MS = parseInt(process.env.OPENCODE_IDLE_TIMEOUT_MS || "1800000", 10);

/**
 * @param {object} args  req, res, body{message,sessionId,model,...}, systemPrompt, ctx
 *   ctx: { ctxManager, snapshotScene, STUDIO_SESSION, LOG_DIR, SCREENSHOT_DIR,
 *          _chatProcs, logToFile, MOCK_MODE }
 */
function runOpenCodeChat({ req, res, body, systemPrompt, ctx }) {
  const { message, sessionId } = body || {};
  const model = (body && body.model) || OPENCODE_MODEL;
  const {
    ctxManager, snapshotScene, STUDIO_SESSION,
    LOG_DIR, SCREENSHOT_DIR, _chatProcs, logToFile, MOCK_MODE,
  } = ctx;

  const cwd = path.resolve(__dirname, "..");

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

  const args = ["run"];
  if (model) args.push("--model", model);
  args.push(fullPrompt);

  const env = { ...process.env };
  Object.keys(env).forEach(k => { if (k.startsWith("CLAUDE")) delete env[k]; });

  logToFile("opencode", `User: "${String(message).slice(0, 200)}" model=${model || "default"} sessionId=${sessionId || "new"}`);

  let proc;
  try {
    proc = spawn(OPENCODE_BIN, args, { cwd, env, stdio: ["ignore", "pipe", "pipe"] });
  } catch (e) {
    emit("text", { delta: `\n\n⚠️ Failed to launch OpenCode (\`${OPENCODE_BIN}\`): ${e.message}. Install the opencode CLI and set OPENCODE_BIN if needed.\n` });
    emit("done", { sessionId: STUDIO_SESSION, isError: true, latestScreenshot: null });
    try { res.end(); } catch {}
    return;
  }

  const procKey = sessionId || "_global";
  const prior = _chatProcs.get(procKey);
  if (prior && !prior.killed) { try { prior.kill("SIGTERM"); } catch {} }
  _chatProcs.set(procKey, proc);
  proc.on("exit", () => { if (_chatProcs.get(procKey) === proc) _chatProcs.delete(procKey); });

  let latestShot = null;
  let stderrBuf  = "";
  let lastOutputTime = Date.now();

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

  ctxManager.resolveSession(STUDIO_SESSION);
  ctxManager.beginRound(STUDIO_SESSION);

  // Relay stdout as assistant text; detect any screenshot paths it prints.
  proc.stdout.on("data", chunk => {
    lastOutputTime = Date.now();
    const s = chunk.toString();
    if (s) emit("text", { delta: s });
    const m = s.match(/([\/][\w\/\-._]+\.png)/);
    if (m && fs.existsSync(m[1])) {
      latestShot = m[1];
      emit("screenshot", { filepath: `/api/screenshot/file?path=${encodeURIComponent(latestShot)}` });
    }
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
    if (!MOCK_MODE) snapshotScene(STUDIO_SESSION).then(done).catch(() => done());
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

module.exports = { runOpenCodeChat };

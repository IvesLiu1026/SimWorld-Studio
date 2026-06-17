"use strict";
// Demo-mode replay for the chat panel.
// When DEMO_MODE=1, /api/chat routes through here. Each call advances a counter
// and streams the next iter's pre-built plan (thinking text + execute_python_script
// tool calls + screenshot) through the existing SSE events the frontend already
// understands. Tool scripts are forwarded to UE for real, so the viewport's
// pixel stream stays in lockstep with the chat panel.

const fs = require("fs");
const path = require("path");
const net = require("net");

const PLANS_DIR = process.env.DEMO_PLANS_DIR || "/data/siddhant/static_scene_eval/exp9_codex/demo/plans";

let counter = 0;            // how many demo replays already served this process
let inFlight = null;        // current SSE response, if any
let stopRequested = false;

function reset() {
  counter = 0;
  stopRequested = false;
}
function status() {
  return { counter, plansDir: PLANS_DIR };
}
function requestStop() {
  stopRequested = true;
  return { ok: true };
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

// Direct UE/MCP exec — mirrors ueExecScript in index.js but doesn't depend on it.
function ueExec(unrealHost, unrealPort, script, timeoutMs = 60000) {
  return new Promise((resolve, reject) => {
    const sock = new net.Socket();
    const timer = setTimeout(() => {
      sock.destroy();
      reject(new Error("UE exec timeout after " + timeoutMs + "ms"));
    }, timeoutMs);
    let buf = "";
    sock.connect(parseInt(unrealPort), unrealHost, () => {
      sock.write(JSON.stringify({ type: "execute_python_script", params: { script } }) + "\n");
    });
    sock.on("data", d => {
      buf += d.toString();
      try {
        const r = JSON.parse(buf);
        clearTimeout(timer);
        sock.destroy();
        resolve(r);
      } catch (_) {}
    });
    sock.on("error", e => {
      clearTimeout(timer);
      reject(e);
    });
    sock.on("close", () => {
      if (buf.trim()) {
        try { resolve(JSON.parse(buf)); } catch (_) {}
      }
    });
  });
}

async function handleDemoReplay(message, sessionIdIn, res, ctx) {
  const {
    logToFile,
    STUDIO_SESSION,
    SCREENSHOT_DIR,
    UNREAL_HOST,
    UNREAL_PORT,
  } = ctx;

  // SSE headers (same as /api/chat)
  res.setHeader("Content-Type", "text/event-stream");
  res.setHeader("Cache-Control", "no-cache");
  res.setHeader("Connection", "keep-alive");
  res.setHeader("X-Accel-Buffering", "no");
  res.flushHeaders();

  let clientGone = false;
  res.on("close", () => { clientGone = true; });

  const send = (type, data) => {
    if (clientGone || res.writableEnded) return;
    try {
      res.write(`event: ${type}\ndata: ${JSON.stringify(data)}\n\n`);
    } catch (e) {
      clientGone = true;
      logToFile && logToFile("demo-replay", `SSE write error: ${e.message}`);
    }
  };

  // Resolve which iter this is. If the request hints at an explicit iter (legacy),
  // use it; otherwise advance the counter.
  let iter = counter + 1;
  // Allow override via "demo:iter=N" prefix in the message
  const m = /^demo:iter=(\d+)\b/.exec(message || "");
  if (m) {
    iter = parseInt(m[1], 10);
  }
  const planPath = path.join(PLANS_DIR, `iter_${String(iter).padStart(2, "0")}.json`);

  if (!fs.existsSync(planPath)) {
    send("text", { delta: `\n[demo] no plan found for iter ${iter} at ${planPath}\n` });
    send("done", { sessionId: STUDIO_SESSION, isError: true });
    res.end();
    return;
  }
  if (!m) counter++;

  const plan = JSON.parse(fs.readFileSync(planPath, "utf-8"));
  logToFile && logToFile("demo-replay", `start iter=${iter} steps=${plan.steps.length}`);

  // Initial system event mirrors what index.js sends for a real run
  send("system", { sessionId: STUDIO_SESSION, mcpServers: [{ name: "simworld", status: "connected" }] });

  // Pacing knobs (env-tunable so we can speed up / slow down for different recordings)
  const TEXT_DELAY = parseInt(process.env.DEMO_TEXT_DELAY_MS || "12", 10);
  const TEXT_CHUNK = parseInt(process.env.DEMO_TEXT_CHUNK || "4", 10);
  const TOOL_PRE_MS = parseInt(process.env.DEMO_TOOL_PRE_MS || "350", 10);
  const TOOL_POST_MS = parseInt(process.env.DEMO_TOOL_POST_MS || "300", 10);
  const STEP_GAP_MS = parseInt(process.env.DEMO_STEP_GAP_MS || "180", 10);

  async function streamText(text) {
    for (let i = 0; i < text.length; i += TEXT_CHUNK) {
      if (clientGone || stopRequested) return;
      send("text", { delta: text.slice(i, i + TEXT_CHUNK) });
      if (TEXT_DELAY) await sleep(TEXT_DELAY);
    }
  }

  let lastScreenshot = null;

  try {
    for (const step of plan.steps) {
      if (clientGone || stopRequested) break;
      if (step.type === "thinking" || step.type === "text") {
        await streamText(step.text + "\n\n");
        await sleep(STEP_GAP_MS);
      } else if (step.type === "tool_call") {
        const toolId = `replay_${iter}_${Math.random().toString(36).slice(2, 10)}`;
        const fullName = `mcp__simworld__${step.tool}`;
        const displayName = step.tool;
        send("tool_start", { id: toolId, name: fullName, displayName });
        await sleep(120);
        send("tool_details", { id: toolId, name: fullName, displayName, input: step.input });
        await sleep(TOOL_PRE_MS);

        let resultText = "";
        let isError = false;
        try {
          const ueRes = await ueExec(UNREAL_HOST, UNREAL_PORT, step.input.script, step.timeoutMs || 240000);
          resultText = JSON.stringify(ueRes);
          if (ueRes && ueRes.status && ueRes.status !== "success") isError = true;
        } catch (e) {
          resultText = JSON.stringify({ error: e.message });
          isError = true;
          logToFile && logToFile("demo-replay", `tool error on iter ${iter}: ${e.message}`);
        }
        send("tool_result", { toolUseId: toolId, result: resultText.slice(0, 4000), isError });
        await sleep(TOOL_POST_MS);
      } else if (step.type === "screenshot") {
        const toolId = `replay_${iter}_ss_${Date.now()}`;
        const fullName = `mcp__simworld__take_screenshot`;
        const filename = step.filename || `demo_iter_${iter}.png`;
        send("tool_start", { id: toolId, name: fullName, displayName: "take_screenshot" });
        await sleep(120);
        send("tool_details", { id: toolId, name: fullName, displayName: "take_screenshot", input: { filename } });
        await sleep(TOOL_PRE_MS);

        const screenshotScript = [
          "import unreal, os",
          `out_dir = ${JSON.stringify(SCREENSHOT_DIR)}`,
          "os.makedirs(out_dir, exist_ok=True)",
          `fp = os.path.join(out_dir, ${JSON.stringify(filename)})`,
          "unreal.AutomationLibrary.take_high_res_screenshot(1920, 1080, fp)",
          "print('SCREENSHOT_SAVED:' + fp)",
        ].join("\n");

        let ssPath = null;
        // Screenshot is best-effort — short timeout so a slow take_high_res doesn't
        // stall the whole demo. The viewport stream still shows the scene live.
        try {
          const r = await ueExec(UNREAL_HOST, UNREAL_PORT, screenshotScript, 18000);
          const logs = (r && r.result && r.result.python_logs) || [];
          for (const line of logs) {
            const idx = line.indexOf("SCREENSHOT_SAVED:");
            if (idx >= 0) ssPath = line.slice(idx + "SCREENSHOT_SAVED:".length).trim();
          }
        } catch (e) {
          logToFile && logToFile("demo-replay", `screenshot best-effort skip iter ${iter}: ${e.message}`);
        }

        // Try to find the file on disk; UE writes the high-res screenshot asynchronously.
        // Up to ~5s of polling — if the file doesn't appear by then, just show no thumbnail.
        for (let i = 0; i < 20 && ssPath && !fs.existsSync(ssPath); i++) {
          await sleep(250);
        }

        if (ssPath && fs.existsSync(ssPath)) {
          lastScreenshot = ssPath;
          send("screenshot", { toolUseId: toolId, filepath: `/api/screenshot/file?path=${encodeURIComponent(ssPath)}` });
        }
        send("tool_result", {
          toolUseId: toolId,
          result: ssPath ? `Screenshot saved: ${ssPath}` : "Screenshot taken (path unresolved)",
          isError: false,
        });
        await sleep(TOOL_POST_MS);
      } else if (step.type === "sleep") {
        await sleep(step.ms || 500);
      }
    }

    if (plan.final_text && !clientGone) {
      await sleep(300);
      await streamText(plan.final_text);
    }

    send("done", {
      sessionId: STUDIO_SESSION,
      isError: false,
      latestScreenshot: lastScreenshot ? `/api/screenshot/file?path=${encodeURIComponent(lastScreenshot)}` : null,
    });
    logToFile && logToFile("demo-replay", `done iter=${iter}`);
  } catch (err) {
    logToFile && logToFile("demo-replay", `Error: ${err.message}`);
    send("text", { delta: `\n[demo replay error: ${err.message}]\n` });
    send("done", { sessionId: STUDIO_SESSION, isError: true });
  }
  if (!res.writableEnded) res.end();
}

// Sweep yaw 0->360 inside a SINGLE python script on UE. Way faster + more
// reliable than 36 separate /api/camera HTTP roundtrips (each ~3s).
async function handle360(req, res, ctx) {
  const { UNREAL_HOST, UNREAL_PORT, logToFile } = ctx;
  const { loc = [0, 0, 250], pitch = 0, startYaw = 0, roll = 0, steps = 72, sleepMs = 150 } = req.body || {};
  const sleepS = (sleepMs || 150) / 1000;
  const script = [
    "import unreal, time",
    "vp = unreal.UnrealEditorSubsystem()",
    // Force the editor viewport into realtime mode so the pixel-stream encoder
    // actually gets a new frame after every camera move (otherwise the editor
    // idle-throttles and the WebRTC stream stays on a stale frame).
    "try: unreal.SystemLibrary.execute_console_command(None, 'Realtime 1')",
    "except Exception: pass",
    "try: unreal.SystemLibrary.execute_console_command(None, 't.MaxFPS 60')",
    "except Exception: pass",
    `loc = unreal.Vector(${loc[0]}, ${loc[1]}, ${loc[2]})`,
    `for i in range(${steps} + 1):`,
    `    yaw = ${startYaw} + (360.0 * i) / ${steps}`,
    `    rot = unreal.Rotator(pitch=${pitch}, yaw=yaw, roll=${roll})`,
    "    vp.set_level_viewport_camera_info(loc, rot)",
    "    try: unreal.EditorLevelLibrary.editor_invalidate_viewports()",
    "    except Exception: pass",
    // Spawn-and-destroy a transient note actor to force the level to be marked",
    // dirty and re-tick. Cheap; ensures pixel-stream encoder gets a fresh frame.",
    "    try:",
    "        _t = unreal.EditorLevelLibrary.spawn_actor_from_class(unreal.Note, unreal.Vector(0,0,-10000))",
    "        if _t: unreal.EditorLevelLibrary.destroy_actor(_t)",
    "    except Exception: pass",
    `    time.sleep(${sleepS})`,
    "print('360_DONE')",
  ].join("\n");
  const timeoutMs = (steps + 1) * sleepMs + 30000;
  try {
    const r = await ueExec(UNREAL_HOST, UNREAL_PORT, script, timeoutMs);
    logToFile && logToFile("demo-360", `done steps=${steps} sleep=${sleepMs}ms`);
    res.json({ ok: true, result: r });
  } catch (e) {
    logToFile && logToFile("demo-360", `error: ${e.message}`);
    res.status(500).json({ error: e.message });
  }
}

module.exports = { handleDemoReplay, handle360, reset, status, requestStop };

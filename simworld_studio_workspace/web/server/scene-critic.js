"use strict";
// VLM-based scene critic — extracted from the verify_scene MCP tool so the framework
// orchestrator can call it directly (no need to spawn a builder agent just to invoke its own tool).
// Takes a screenshot + actor list, sends to Claude-with-vision with a verification system prompt,
// returns a structured verdict: {status: PASS|NEEDS_IMPROVEMENT|FAIL, issues[], suggestions[], raw}.
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");
const net = require("net");
const { codexModel, extractJSON, normalizeProvider, parseCodexJsonl } = require("./llm-oneshot");

const NL = String.fromCharCode(10);
const UNREAL_HOST = process.env.UNREAL_HOST || "127.0.0.1";
const UNREAL_PORT = process.env.UNREAL_PORT || "55561";
const ARENA_ROOT = path.resolve(__dirname, "..", "..");
const SCREENSHOT_DIR = path.join(ARENA_ROOT, "tmp", "screens");

const CRITIC_SYSTEM_PROMPT = `You are a 3D scene verification expert for SimWorld Studio (Unreal Engine 5).
Analyze the scene screenshot and actor list, then provide concise actionable feedback.

Evaluate:
1. Completeness: Are all requested objects present?
2. Placement: Are objects in good positions? (X/Y within -9500 to 9500, not overlapping, not outside ground)
3. Scale: Do objects look appropriately sized relative to each other?
4. Realism: Does the scene match the original request?
5. Issues: Any obvious problems (floating objects above ground, buried below ground, misaligned, upside-down)?
6. Navigation/walkability: Large buildings (BP_Building_*) have a large footprint and BLOCK agent navigation if placed in the walkable area near PlayerStart. They belong as background scenery far from center (>2500 UU). Small props (hydrants, bins, cones, benches) are fine anywhere. Trees belong at the scene edge.

Format your response as:
- **Status**: PASS / NEEDS_IMPROVEMENT / FAIL
- **Issues**: (bullet list of specific problems, or "None" if PASS)
- **Suggestions**: (bullet list of specific actionable improvements the builder agent should make next)`;

// Low-level UE TCP command. Returns the parsed JSON response or null on error/timeout.
function ueCommand(type, params, timeoutMs = 30000) {
  return new Promise((resolve) => {
    const sock = new net.Socket();
    const timer = setTimeout(() => { try { sock.destroy(); } catch (_e) {} resolve(null); }, timeoutMs);
    let buf = "";
    sock.connect(parseInt(UNREAL_PORT), UNREAL_HOST, () => {
      sock.write(JSON.stringify({ type, params: params || {} }) + NL);
    });
    sock.on("data", (d) => {
      buf += d.toString();
      try { const r = JSON.parse(buf); clearTimeout(timer); sock.destroy(); resolve(r); } catch (_e) {}
    });
    sock.on("error", () => { clearTimeout(timer); try { sock.destroy(); } catch (_e) {} resolve(null); });
  });
}

async function takeScreenshot() {
  try { fs.mkdirSync(SCREENSHOT_DIR, { recursive: true }); } catch (_e) {}
  const filepath = path.join(SCREENSHOT_DIR, `critic_${Date.now()}.png`);
  await ueCommand("take_screenshot", { filepath }, 30000);
  return fs.existsSync(filepath) ? filepath : null;
}

async function getActors() {
  const r = await ueCommand("get_actors_in_level", {}, 15000);
  // mcp-server returns nested under .result; UE bridge variations possible. Normalize to whatever's there.
  return r || {};
}

// Parse the critic's free-text response into a structured verdict.
// Robust to **Markdown** vs plain labels, trailing-section-without-newline, lowercase status, extra prose.
function parseCriticFeedback(text) {
  const t = String(text || "");
  // Status — match **Status**: VAL or Status: VAL (case-insensitive on the value).
  const sm = t.match(/(?:\*\*Status\*\*|\bStatus\b)\s*:\s*([A-Za-z_]+)/i);
  let status = "NEEDS_IMPROVEMENT";
  if (sm) {
    const s = sm[1].toUpperCase();
    if (s === "PASS" || s === "NEEDS_IMPROVEMENT" || s === "FAIL") status = s;
  }
  // Locate every labelled section (Status/Issues/Suggestions) by its start position; require ':' after label.
  const labelRe = /(?:\*\*(Status|Issues|Suggestions)\*\*|\b(Status|Issues|Suggestions)\b)\s*:\s*/gi;
  const positions = []; let m;
  while ((m = labelRe.exec(t)) !== null) {
    positions.push({ label: ((m[1] || m[2]) || "").toLowerCase(), start: m.index, contentStart: m.index + m[0].length });
  }
  const sectionBody = (label) => {
    const i = positions.findIndex((p) => p.label === label.toLowerCase());
    if (i < 0) return "";
    const start = positions[i].contentStart;
    const end = i + 1 < positions.length ? positions[i + 1].start : t.length;
    return t.slice(start, end);
  };
  const toBullets = (s) => (s || "")
    .split(/\n/)
    .map((l) => l.replace(/^[\s\-•*\d.)]+/, "").trim())
    .filter((x) => x && !/^none$/i.test(x) && !/^\(none\)$/i.test(x));
  return { status, issues: toBullets(sectionBody("Issues")), suggestions: toBullets(sectionBody("Suggestions")) };
}

function criticSchemaPath() {
  const fp = path.join("/tmp", "simworld_scene_critic_schema.json");
  const schema = {
    type: "object",
    additionalProperties: false,
    properties: {
      status: { type: "string", enum: ["PASS", "NEEDS_IMPROVEMENT", "FAIL"] },
      issues: { type: "array", items: { type: "string" } },
      suggestions: { type: "array", items: { type: "string" } },
      raw_notes: { type: "string" },
    },
    required: ["status", "issues", "suggestions", "raw_notes"],
  };
  try { fs.writeFileSync(fp, JSON.stringify(schema, null, 2), "utf-8"); } catch (_e) {}
  return fp;
}

function normalizeCriticJson(out) {
  const status = ["PASS", "NEEDS_IMPROVEMENT", "FAIL"].includes(String(out && out.status || "").toUpperCase())
    ? String(out.status).toUpperCase()
    : "NEEDS_IMPROVEMENT";
  return {
    status,
    issues: Array.isArray(out && out.issues) ? out.issues.map(String).filter(Boolean) : [],
    suggestions: Array.isArray(out && out.suggestions) ? out.suggestions.map(String).filter(Boolean) : [],
    raw: out && out.raw_notes ? String(out.raw_notes) : JSON.stringify(out || {}),
  };
}

function runCriticCodex({ originalPrompt, focus, screenshot, actors, model, timeoutMs = 120000 }) {
  const schema = criticSchemaPath();
  const tmpOut = path.join("/tmp", `simworld_scene_critic_${process.pid}_${Date.now()}.json`);
  try { fs.rmSync(tmpOut, { force: true }); } catch (_e) {}
  const prompt = [
    CRITIC_SYSTEM_PROMPT,
    "",
    "Evaluate the attached screenshot and actor list for this SimWorld Studio scene.",
    originalPrompt ? `Original scene request: "${originalPrompt}"` : "",
    focus ? `Focus on: ${focus}` : "",
    "",
    "Current actors in the scene:",
    JSON.stringify(actors, null, 2),
    "",
    "Return ONLY the JSON object matching the schema. Keep issues/suggestions concise and actionable.",
  ].filter(Boolean).join("\n");
  const args = [
    "exec",
    "--json",
    "--skip-git-repo-check",
    "--dangerously-bypass-approvals-and-sandbox",
    "-m", codexModel(model),
    "-c", `model_reasoning_effort=${process.env.CRITIC_REASONING_EFFORT || "high"}`,
    "--output-schema", schema,
    "-o", tmpOut,
    "-C", "/tmp",
    "-i", screenshot,
    "-",
  ];
  return new Promise((resolve, reject) => {
    const env = { ...process.env, NO_COLOR: "1" };
    Object.keys(env).forEach(k => { if (k.startsWith("CLAUDE")) delete env[k]; });
    const p = spawn(process.env.CODEX_BIN || "codex", args, { stdio: ["pipe", "pipe", "pipe"], cwd: "/tmp", env });
    const timer = setTimeout(() => { try { p.kill("SIGTERM"); } catch (_e) {} reject(new Error("codex critic timed out")); }, timeoutMs);
    let stdout = "", stderr = "";
    try { p.stdin.write(prompt); p.stdin.end(); } catch (e) { clearTimeout(timer); reject(e); return; }
    p.stdout.on("data", d => { stdout += d.toString(); });
    p.stderr.on("data", d => { stderr += d.toString(); });
    p.on("error", e => { clearTimeout(timer); reject(e); });
    p.on("close", code => {
      clearTimeout(timer);
      if (code !== 0) return reject(new Error(`codex critic exited ${code}: ${stderr.slice(0, 400)}`));
      let out = null;
      if (fs.existsSync(tmpOut)) {
        try { out = JSON.parse(fs.readFileSync(tmpOut, "utf-8")); } catch (_e) {}
      }
      if (!out) {
        const trace = parseCodexJsonl(stdout);
        try { out = extractJSON(trace.last_agent_text || ""); } catch (_e) {}
      }
      if (!out) return reject(new Error(`codex critic produced no JSON: ${stderr.slice(0, 400)}`));
      const actorsCount = (actors && actors.result && actors.result.actors && actors.result.actors.length) || 0;
      resolve({ ...normalizeCriticJson(out), screenshot, actorsCount });
    });
  });
}

// Spawn the configured vision critic with the screenshot + text. Returns parsed verdict.
async function runCritic({ originalPrompt, focus, screenshot, actors, model, timeoutMs = 120000, provider, runner }) {
  if (!screenshot || !fs.existsSync(screenshot)) {
    return { status: "FAIL", issues: ["No screenshot available — UE may be wedged"], suggestions: [], raw: "", screenshot: null, actorsCount: 0 };
  }
  if (normalizeProvider(provider || runner || process.env.LLM_PROVIDER) === "codex") {
    return runCriticCodex({ originalPrompt, focus, screenshot, actors, model, timeoutMs });
  }
  const imgData = fs.readFileSync(screenshot);
  const isJpeg = imgData[0] === 0xff && imgData[1] === 0xd8;
  const userContent = [
    { type: "image", source: { type: "base64", media_type: isJpeg ? "image/jpeg" : "image/png", data: imgData.toString("base64") } },
    {
      type: "text",
      text:
        "Please verify this 3D scene in SimWorld Studio (Unreal Engine 5).\n\n" +
        (originalPrompt ? `Original scene request: "${originalPrompt}"\n\n` : "") +
        "Current actors in the scene:\n" + JSON.stringify(actors, null, 2) +
        (focus ? `\n\nFocus on: ${focus}` : ""),
    },
  ];

  const CLAUDE = process.env.CLAUDE_BIN || "claude";
  const args = [
    "--input-format", "stream-json",
    "--output-format", "stream-json",
    "--verbose",
    "--dangerously-skip-permissions",
    "--append-system-prompt", CRITIC_SYSTEM_PROMPT,
  ];
  if (model) args.push("--model", model);

  return new Promise((resolve, reject) => {
    const env = { ...process.env };
    Object.keys(env).forEach((k) => { if (k.startsWith("CLAUDE")) delete env[k]; });
    const p = spawn(CLAUDE, args, { stdio: ["pipe", "pipe", "pipe"], cwd: path.resolve(__dirname, ".."), env });
    const timer = setTimeout(() => { try { p.kill("SIGTERM"); } catch (_e) {} reject(new Error("critic timed out")); }, timeoutMs);
    p.stdin.write(JSON.stringify({ type: "user", message: { role: "user", content: userContent } }) + NL);
    p.stdin.end();
    let outBuf = "", errBuf = "", feedback = "", isErr = false;
    p.stdout.on("data", (d) => {
      outBuf += d.toString();
      const lines = outBuf.split(NL);
      outBuf = lines.pop() || "";
      for (const line of lines) {
        if (!line.trim()) continue;
        let ev; try { ev = JSON.parse(line); } catch { continue; }
        if (ev.type === "result") {
          if (typeof ev.result === "string" && ev.result) feedback = ev.result;
          if (ev.is_error || ev.subtype === "error_during_turn") isErr = true;
        } else if (ev.type === "assistant") {
          for (const b of (ev.message && ev.message.content) || []) {
            if (b.type === "text" && b.text) feedback += b.text;
          }
        }
      }
    });
    p.stderr.on("data", (d) => { errBuf += d.toString(); });
    p.on("error", (e) => { clearTimeout(timer); reject(e); });
    p.on("close", (code) => {
      clearTimeout(timer);
      if (isErr || code !== 0) return reject(new Error(`critic exited ${code}: ${errBuf.slice(0, 200)}`));
      const parsed = parseCriticFeedback(feedback);
      const actorsCount = (actors && actors.result && actors.result.actors && actors.result.actors.length) || 0;
      resolve({ ...parsed, raw: feedback, screenshot, actorsCount });
    });
  });
}

// Convenience: capture fresh screenshot + actors, then critique.
async function critique({ originalPrompt, focus, model, timeoutMs, provider, runner }) {
  const [screenshot, actors] = await Promise.all([takeScreenshot(), getActors()]);
  return runCritic({ originalPrompt, focus, screenshot, actors, model, timeoutMs, provider, runner });
}

module.exports = {
  critique, runCritic, takeScreenshot, getActors, parseCriticFeedback,
  CRITIC_SYSTEM_PROMPT,
};

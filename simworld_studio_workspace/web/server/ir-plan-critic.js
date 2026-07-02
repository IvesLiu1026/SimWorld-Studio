"use strict";
// ── Plan-level visual critic (fix B) ──────────────────────────────────────────
// Renders the SOLVED plan as a top-down image and asks a VLM to critique the LAYOUT (emptiness,
// grouping, focal structure, balance) — the axis our scenes lose — returning actionable plan edits.
// This is the "planner sees its own output and revises" loop the literature says is the biggest lever.
const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");
const os = require("os");
const { codexModel, extractJSON, normalizeProvider, parseCodexJsonl } = require("./llm-oneshot");

const RENDER = path.resolve(__dirname, "../../../tools/render_plan.py");
const NL = String.fromCharCode(10);

// Render the solved `placed` list to a top-down PNG; returns the path or null.
function renderPlanImage(placed, tag) {
  try {
    const base = path.join(os.tmpdir(), `irplan_${process.pid}_${Date.now()}_${tag || ""}`);
    const jf = base + ".json", pf = base + ".png";
    fs.writeFileSync(jf, JSON.stringify(placed || []));
    spawnSync("python3", [RENDER, jf, pf], { timeout: 40000 });
    try { fs.rmSync(jf, { force: true }); } catch (_e) {}
    return fs.existsSync(pf) ? pf : null;
  } catch (_e) { return null; }
}

const PLAN_CRITIC_PROMPT =
  "You are a 3D scene LAYOUT critic. You are shown a TOP-DOWN render of a planned scene — each colored " +
  "rectangle is one object's footprint (colored by category; legend at the bottom; North is up). Judge the " +
  "LAYOUT only:\n" +
  "- Emptiness/sparseness: is a large area EMPTY while another is over-crowded? A believable scene fills its space.\n" +
  "- Grouping: are related objects clustered into sensible functional zones, or scattered?\n" +
  "- Focal structure: is there a clear main/central structure, sensible zones, an entrance/axis where appropriate?\n" +
  "- Balance: obvious gaps, lopsidedness, or under-population?\n" +
  "Do NOT flag small overlaps — a deterministic solver handles collisions. Give concise, ACTIONABLE layout fixes " +
  "(what to add/where, what to regroup, how to fill empty areas). If the layout is already full and well-composed, PASS.";

function _schemaPath() {
  const fp = path.join(os.tmpdir(), "ir_plan_critic_schema.json");
  const schema = { type: "object", additionalProperties: false, properties: {
    status: { type: "string", enum: ["PASS", "NEEDS_IMPROVEMENT"] },
    issues: { type: "array", items: { type: "string" } },
    suggestions: { type: "array", items: { type: "string" } },
  }, required: ["status", "issues", "suggestions"] };
  try { fs.writeFileSync(fp, JSON.stringify(schema)); } catch (_e) {}
  return fp;
}

// VLM critique of the plan render. codex path (matches the rest of the pipeline). Returns {status,issues,suggestions}.
function critiquePlan({ scene, imagePath, intent, model, timeoutMs = 120000 }) {
  return new Promise((resolve) => {
    if (!imagePath || !fs.existsSync(imagePath)) return resolve({ status: "PASS", issues: [], suggestions: [] });
    const schema = _schemaPath();
    const out = path.join(os.tmpdir(), `ir_plan_critic_${process.pid}_${Date.now()}.json`);
    try { fs.rmSync(out, { force: true }); } catch (_e) {}
    const prompt = [PLAN_CRITIC_PROMPT, "",
      scene ? `Scene request: "${scene}"` : "",
      intent ? `Intended design: ${String(intent).slice(0, 400)}` : "",
      "Return ONLY the JSON {status, issues, suggestions}."].filter(Boolean).join("\n");
    const args = ["exec", "--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox",
      "-m", codexModel(model), "-c", `model_reasoning_effort=${process.env.IR_PLAN_CRITIC_EFFORT || "high"}`,
      "--output-schema", schema, "-o", out, "-C", os.tmpdir(), "-i", imagePath, "-"];
    const env = { ...process.env, NO_COLOR: "1" };
    Object.keys(env).forEach(k => { if (k.startsWith("CLAUDE")) delete env[k]; });
    const p = spawn(process.env.CODEX_BIN || "codex", args, { stdio: ["pipe", "pipe", "pipe"], cwd: os.tmpdir(), env });
    const timer = setTimeout(() => { try { p.kill("SIGTERM"); } catch (_e) {} resolve({ status: "PASS", issues: [], suggestions: [] }); }, timeoutMs);
    let so = "";
    try { p.stdin.write(prompt); p.stdin.end(); } catch (_e) {}
    p.stdout.on("data", d => { so += d.toString(); });
    p.on("error", () => { clearTimeout(timer); resolve({ status: "PASS", issues: [], suggestions: [] }); });
    p.on("close", () => {
      clearTimeout(timer);
      let r = null;
      if (fs.existsSync(out)) { try { r = JSON.parse(fs.readFileSync(out, "utf8")); } catch (_e) {} }
      if (!r) { try { r = extractJSON(parseCodexJsonl(so).last_agent_text || ""); } catch (_e) {} }
      if (!r || typeof r !== "object") return resolve({ status: "PASS", issues: [], suggestions: [] });
      resolve({ status: /pass/i.test(String(r.status)) ? "PASS" : "NEEDS_IMPROVEMENT",
        issues: Array.isArray(r.issues) ? r.issues.map(String).filter(Boolean) : [],
        suggestions: Array.isArray(r.suggestions) ? r.suggestions.map(String).filter(Boolean) : [] });
    });
  });
}

module.exports = { renderPlanImage, critiquePlan };

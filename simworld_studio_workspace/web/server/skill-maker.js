"use strict";
// Dynamic skill-maker: given a scene prompt + live scene state + the existing session skill library,
// asks Claude to decide which skills to REUSE / UPDATE / ADD and returns valid UE Python helper code.
// Uses a DELIMITER format (not JSON) so multi-line Python code never breaks parsing.
const { spawn } = require("child_process");
const path = require("path");
const NL = String.fromCharCode(10);

function trim(t, n) { const s = String(t || ""); return s.length <= n ? s : s.slice(0, n) + NL + "..."; }

function buildMakerPrompt(userPrompt, sceneState, existingLibrary, assetHint) {
  const lib = (existingLibrary || []).map(s => `  - ${s.signature || s.name}  [${(s.tags || []).join(",")}]  ${s.description || ""}`).join(NL) || "  (none — this is the first turn)";
  return [
    "You are a SKILL-AUTHORING agent for a UE 5.3 scene-building system.",
    "You write small, reusable Python helper functions (\"skills\") that a downstream coding agent imports and calls",
    "to build a scene, so geometry/spacing/alignment/asset-placement is computed ONCE, correctly, and reused",
    "consistently across the build and across later edit prompts.",
    "",
    "The downstream agent runs your skills inside Unreal's Python via:  from <module> import *",
    "These base helpers ALREADY exist (do NOT redefine): get_subsystem() -> unreal.EditorActorSubsystem ;",
    "delete_by_prefix(prefix) -> deletes actors whose label starts with prefix ;",
    "asset_size(path) -> (size_x, size_y, size_z) in cm, the asset's REAL measured bounds (cached).  (`import unreal, math, random` already done.)",
    "",
    "SKILL CODE RULES:",
    "- Valid Python 3, top-level `def` only. No classes, no top-level statements, no input().",
    "- Spawn blueprints: cls=unreal.load_object(None,'<path>_C'); a=S.spawn_actor_from_class(cls, unreal.Vector(x,y,z), unreal.Rotator(0,yaw,0))",
    "- Real asset paths. Known-good: road '/Game/CityDatabase/blueprints/BP_Road1.BP_Road1_C',",
    "  buildings '/Game/CityDatabase/blueprints/BP_Building_05.BP_Building_05_C' (01..88), trees 'BP_Tree2' (1..6).",
    "- CRITICAL — NEVER hardcode an asset's size/length/width/footprint/extent. Assets are large and vary",
    "  (e.g. BP_Road1 is ~18500cm long x ~5500cm wide). ALWAYS call asset_size(path) and derive ALL placement",
    "  from the measured sizes so geometry matches the REAL asset. Hardcoded dimensions are the #1 failure.",
    "- BP_Road1 ALREADY INCLUDES its sidewalks within its width. Let rl,rw = asset_size(road)[0], asset_size(road)[1].",
    "  * Tile roads END-TO-END by measured length: segment i center x = start_x + i*rl  (NOT a guessed gap; else they overlap).",
    "  * Buildings flush JUST OUTSIDE the road: building_y = rw/2.0 + asset_size(building)[1]/2.0  (both sides: +y and -y).",
    "  * Sidewalk trees just INSIDE the outer edge (on the sidewalk): tree_y = rw/2.0 * 0.92.",
    "  * Populate the FULL length: count = max(1, int(total_length / spacing_or_width)); spacing from asset_size too.",
    "- Prefer pure functions returning data (lists of (x,y,z,yaw)) AND/OR spawn helpers taking (S, ...).",
    "  Make functions GENERAL/parameterized so they survive later edits.",
    "",
    "DECISIONS, given the request + current scene + existing library:",
    "- REUSE existing skills that stay correct & useful as-is.",
    "- UPDATE existing skills that must change (resend full code).",
    "- ADD new skills the request needs. Keep the set minimal & high-leverage (placement/geometry/asset-fit).",
    "",
    "OUTPUT FORMAT — output EXACTLY this, nothing else (NO JSON, NO markdown fences):",
    "REASONING: <one short sentence>",
    "REUSE: <comma-separated existing skill names, or: none>",
    "",
    "Then ONE block per added/updated skill (multi-line code is fine between CODE: and @@END):",
    "@@SKILL <snake_name> | <add or update>",
    "SIG: <one-line call signature, e.g. spawn_tree_row(S, n=6, axis='x', spacing=600)>",
    "DESC: <one-line description>",
    "TAGS: <comma tags>",
    "CODE:",
    "def <snake_name>(...):",
    "    <body>",
    "@@END",
    "",
    `USER SCENE REQUEST: ${String(userPrompt || "").trim()}`,
    "",
    "CURRENT SCENE STATE:",
    trim(sceneState || "(empty scene)", 2500),
    "",
    "EXISTING SESSION SKILL LIBRARY (reuse/update by name):",
    lib,
    assetHint ? (NL + "AVAILABLE ASSET CATEGORIES:" + NL + trim(assetHint, 1200)) : "",
  ].filter(Boolean).join(NL);
}

// Parse the delimiter format into {reasoning, reuse[], update[], add[]}.
function parseMakerOutput(raw) {
  const text = String(raw || "");
  const reasoning = (text.match(/REASONING:\s*(.*)/) || [])[1] || "";
  const reuseLine = (text.match(/REUSE:\s*(.*)/) || [])[1] || "";
  const reuse = reuseLine.split(",").map(s => s.trim()).filter(x => x && x.toLowerCase() !== "none");
  const add = [], update = [];
  const re = /@@SKILL[ \t]+([^\n|]+?)(?:[ \t]*\|[ \t]*(\w+))?[ \t]*\n([\s\S]*?)@@END/g;
  let m;
  while ((m = re.exec(text)) !== null) {
    const name = (m[1] || "").trim();
    const action = ((m[2] || "add").trim().toLowerCase() === "update") ? "update" : "add";
    const body = m[3] || "";
    const sig = ((body.match(/SIG:\s*(.*)/) || [])[1] || (name + "()")).trim();
    const desc = ((body.match(/DESC:\s*(.*)/) || [])[1] || "").trim();
    const tags = ((body.match(/TAGS:\s*(.*)/) || [])[1] || "").split(",").map(s => s.trim()).filter(Boolean);
    const codeM = body.match(/CODE:[ \t]*\n([\s\S]*)$/);
    const code = codeM ? codeM[1].replace(/\s+$/, "") : "";
    if (!name || !code) continue;
    (action === "update" ? update : add).push({ name, signature: sig, description: desc, tags, code });
  }
  const dedup = (arr) => { const mp = new Map(); for (const s of arr) mp.set(s.name, s); return [...mp.values()]; };
  return { reasoning: reasoning.trim(), reuse: [...new Set(reuse)], update: dedup(update), add: dedup(add) };
}

async function generateSkills(options) {
  const opts = options || {};
  const prompt = String(opts.prompt || "").trim();
  const claudeBin = String(opts.claudeBin || process.env.CLAUDE_BIN || "claude");
  const timeoutMs = Math.max(10000, Number(opts.timeoutMs || 120000));
  const model = opts.model == null || opts.model === "" ? null : String(opts.model);
  if (!prompt) return { reasoning: "empty_prompt", reuse: [], update: [], add: [], rawText: "" };

  const makerPrompt = buildMakerPrompt(prompt, opts.sceneState, opts.existingLibrary, opts.assetHint);
  const args = ["-p", makerPrompt, "--output-format", "stream-json", "--include-partial-messages", "--verbose", "--dangerously-skip-permissions"];
  if (model) args.push("--model", model);

  return new Promise((resolve, reject) => {
    const env = { ...process.env };
    Object.keys(env).forEach(k => { if (k.startsWith("CLAUDE")) delete env[k]; });
    const proc = spawn(claudeBin, args, { cwd: path.resolve(__dirname, ".."), env, stdio: ["ignore", "pipe", "pipe"] });
    let outBuf = "", errBuf = "", assistantText = "", resultText = "", isErr = false;
    const timer = setTimeout(() => { try { proc.kill("SIGTERM"); } catch {} reject(new Error("skill-maker timed out")); }, timeoutMs);
    function handle(line) {
      let e; try { e = JSON.parse(String(line || "").trim()); } catch { return; }
      if (!e) return;
      if (e.type === "assistant") {
        const blocks = e.message && Array.isArray(e.message.content) ? e.message.content : [];
        for (const b of blocks) if (b.type === "text" && b.text) assistantText += String(b.text);
      } else if (e.type === "result") {
        isErr = Boolean(e.is_error || e.subtype === "error_during_turn");
        if (typeof e.result === "string") resultText += e.result;
      }
    }
    proc.stdout.on("data", c => { outBuf += c.toString(); const ls = outBuf.split(NL); outBuf = ls.pop() || ""; for (const l of ls) if (l.trim()) handle(l); });
    proc.stderr.on("data", c => { errBuf += c.toString(); });
    proc.on("error", err => { clearTimeout(timer); reject(err); });
    proc.on("close", code => {
      clearTimeout(timer); if (outBuf.trim()) handle(outBuf);
      const raw = (resultText.trim() || assistantText.trim());
      if (isErr || code !== 0) return reject(new Error(`skill-maker exited ${code}: ${errBuf.slice(0, 200)}`));
      const parsed = parseMakerOutput(raw);
      if (!parsed.add.length && !parsed.update.length && !parsed.reuse.length) {
        return reject(new Error("skill-maker produced no parseable skills: " + raw.slice(0, 200)));
      }
      resolve({ ...parsed, rawText: raw });
    });
  });
}

module.exports = { generateSkills, buildMakerPrompt, parseMakerOutput };

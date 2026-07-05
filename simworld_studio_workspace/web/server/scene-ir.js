"use strict";
// ── Scene IR orchestrator ────────────────────────────────────────────────────
// Public entry for the Intermediate-Representation stage that sits AFTER asset
// retrieval and BEFORE scene development. Pipeline:
//   retrieve assets → plan (LLM relational graph) → solve (coords + non-overlap)
//   → render ASCII → format a builder prompt block.
// Plan-once: cached per scene so loop rounds reuse one plan. Off by default
// (SCENE_IR), so when disabled this module is never invoked and behavior is unchanged.
const fs = require("fs");
const path = require("path");

function _truthy(v) { return /^(1|true|yes|on)$/i.test(String(v == null ? "" : v)); }
function _falsy(v) { return /^(0|false|no|off|none|disabled)$/i.test(String(v == null ? "" : v)); }

// Resolve IR mode from a request body (per-request override) or env SCENE_IR. Default OFF.
function resolveIRMode(bodyOrMode) {
  if (typeof bodyOrMode === "string") return _truthy(bodyOrMode) ? "on" : "off";
  const b = bodyOrMode || {};
  for (const k of ["sceneIr", "irMode", "ir", "sceneIR"]) {
    const v = b[k];
    if (v != null && v !== "") return _truthy(v) ? "on" : "off";
  }
  if (_truthy(process.env.SCENE_IR)) return "on";
  return "off";
}

// Plan-once cache (the LLM graph) is MODE-INDEPENDENT so every solver variant of a scene
// shares the IDENTICAL plan → the A/B isolates the solver. The block cache is per variant.
const _planCache = new Map();
const _cache = new Map();
const _artifactCache = new Map(); // bkey -> {ascii, intent}; surfaced to the visual-loop critic so it can judge intended-vs-built
function _planKey(scene, o) { return JSON.stringify({ scene: String(scene || ""), model: String((o && o.model) || ""), cot: resolveCot(o), rich: resolveRichAssets(o), v: 1 }); }
function _blockKey(scene, o, mode, repair) { return JSON.stringify({ scene: String(scene || ""), model: String((o && o.model) || ""), mode, repair: !!repair, cot: resolveCot(o), ascii: resolveAscii(o), rich: resolveRichAssets(o), planCritic: resolvePlanCritic(o), ground: resolveGroundPass(o), v: 2 }); }

// Solver mode: body.irSolver | env IR_SOLVER. Default "legacy" (the original scatter solver).
function resolveSolverMode(o) {
  const b = o || {};
  const s = String(b.irSolver || b.solverMode || process.env.IR_SOLVER || "").toLowerCase();
  if (s === "gentle") return "gentle";
  if (s === "structure" || s === "structured" || s === "structure_preserving") return "structure";
  return "legacy";
}
// LLM-repair toggle: body.irRepair | env IR_REPAIR. Only meaningful with the structure solver.
function resolveRepair(o) {
  const b = o || {};
  if (b.irRepair != null && b.irRepair !== "") return _truthy(b.irRepair);
  return _truthy(process.env.IR_REPAIR);
}
// CoT/reasoning planner toggle: body.irCot | env IR_COT. Default OFF (single-shot planner).
function resolveCot(o) {
  const b = o || {};
  if (b.irCot != null && b.irCot !== "") return _truthy(b.irCot);
  return _truthy(process.env.IR_COT);
}
// Include the ASCII top-down map in the builder block: body.irAscii | env IR_ASCII. Default ON.
function resolveAscii(o) {
  const b = o || {};
  if (b.irAscii != null && b.irAscii !== "") return _truthy(b.irAscii);
  if (process.env.IR_ASCII != null && process.env.IR_ASCII !== "") return _truthy(process.env.IR_ASCII);
  return true;
}
// Rich asset context: feed the retrieved desc/tags/subcategory into the planner so it isn't blind to
// what each asset is. body.irRichAssets | env IR_RICH_ASSETS. Default OFF (preserves prior behavior).
function resolveRichAssets(o) {
  const b = o || {};
  if (b.irRichAssets != null && b.irRichAssets !== "") return _truthy(b.irRichAssets);
  return _truthy(process.env.IR_RICH_ASSETS);
}
// Plan-level visual critic (fix B): render the solved plan, VLM critiques layout/density, revise, re-solve.
// body.irPlanCritic | env IR_PLAN_CRITIC. Default OFF.
function resolvePlanCritic(o) {
  const b = o || {};
  if (b.irPlanCritic != null && b.irPlanCritic !== "") return _truthy(b.irPlanCritic);
  return _truthy(process.env.IR_PLAN_CRITIC);
}
// Ground-carpet pass (fix #1): prepend a deterministic gapless themed ground carpet to the IR block.
// body.irGroundPass | env IR_GROUND_PASS. Default OFF.
function resolveGroundPass(o) {
  const b = o || {};
  if (b.irGroundPass != null && b.irGroundPass !== "") return _truthy(b.irGroundPass);
  return _truthy(process.env.IR_GROUND_PASS);
}

// Plan ONCE per (scene, model): retrieve palette + LLM plan → normalized graph. Cached so all
// solver variants reuse the same graph. Returns { graph, assets, planReport }.
async function _planOnce(scene, o, log) {
  const key = _planKey(scene, o);
  if (_planCache.has(key)) { log("ir plan cache HIT (reusing this scene's graph across solver variants)"); return _planCache.get(key); }
  const ar = o.retrieveFn ? { retrieve: o.retrieveFn } : require("./asset-retrieval");
  const planner = o.planFn ? { planScene: o.planFn } : require("./ir-planner");
  const r = await ar.retrieve(scene, Object.assign({ usePrefilter: o.usePrefilter !== false }, o));
  const assets = (r && Array.isArray(r.assets) ? r.assets : []).filter(a => a && a.id);
  if (!assets.length) { const res = { graph: null, assets: [], planReport: {} }; _planCache.set(key, res); return res; }
  const plannerOpts = Object.assign({}, o, {
    model: process.env.IR_MODEL || o.model,
    provider: process.env.IR_PROVIDER || o.provider || o.runner,
    runner: process.env.IR_PROVIDER || o.runner || o.provider,
    reasoningEffort: process.env.IR_PLANNER_REASONING_EFFORT || o.reasoningEffort || "high",
    timeoutMs: Number(process.env.IR_PLANNER_TIMEOUT_MS || o.timeoutMs || 300000),
    richAssets: resolveRichAssets(o),
  });
  const useCot = resolveCot(o) && typeof planner.planSceneCot === "function";
  if (useCot) log("ir planner: CoT/reasoning two-stage (design brief -> graph)");
  const planned = useCot
    ? await planner.planSceneCot(scene, assets, plannerOpts)
    : await planner.planScene(scene, assets, plannerOpts);
  const res = { graph: planned.graph || planned, assets, planReport: planned.report || {}, brief: (planned && planned.brief) || "" };
  _planCache.set(key, res);
  return res;
}

function _slug(s) { return String(s || "scene").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40) || "scene"; }
function _hash(s) { return require("crypto").createHash("sha1").update(String(s || "")).digest("hex").slice(0, 8); }

function _groupByAsset(list) {
  const m = new Map();
  for (const p of list) {
    const k = p.asset_id || p.path || p.name;
    if (!m.has(k)) m.set(k, { name: p.name, path: p.path, spawnTool: p.spawnTool, category: p.category, items: [] });
    m.get(k).items.push(p);
  }
  return [...m.values()];
}

function _extentM(list) {
  let mx = 0;
  for (const p of list) mx = Math.max(mx, Math.abs(p.x_m || 0), Math.abs(p.y_m || 0));
  return Math.ceil(mx);
}

// Format the IR into the system-prompt block the builder consumes.
function formatIRBlock(placed, ascii, report, graph, includeAscii) {
  const ground = placed.filter(p => p.isGround);
  const objs = placed.filter(p => !p.isGround);
  const L = [];
  L.push("## SCENE PLAN — INTERMEDIATE REPRESENTATION (build the scene to this layout)");
  L.push("A planner laid out this scene and a solver assigned COLLISION-CHECKED world coordinates "
    + "(UE centimetres; z=0 is the ground). BUILD TO THIS PLAN: spawn each listed object at its given "
    + "location. The top-down map shows the intended layout for overall context; treat the listed "
    + "coordinates as authoritative and only nudge if a specific spawn clearly fails. Use "
    + "execute_python_script to place many instances efficiently. Coordinates are in UE cm, [x, y, z]; "
    + "yaw is in UE degrees (0 = +X / East).");
  L.push("ON THE GROUND: every object's Z is 0 — spawn each resting on the ground (base at z=0). "
    + "Nothing should float above the surface; do not raise objects into the air.");
  L.push("");
  if (includeAscii !== false && ascii) {
    L.push("### TOP-DOWN MAP");
    L.push("```");
    L.push(ascii);
    L.push("```");
  }
  if (ground.length) {
    L.push("");
    L.push("### GROUND (carpet the floor first, at z=0)");
    for (const grp of _groupByAsset(ground)) {
      L.push(`- ${grp.name} · ${grp.spawnTool} "${grp.path}" — tile edge-to-edge across the build area (~${_extentM(ground) * 2} m span), ${grp.items.length} tiles planned, at z=0.`);
    }
  }
  L.push("");
  L.push(`### PLACEMENTS (${objs.length} objects — spawn at these UE-cm coordinates)`);
  for (const grp of _groupByAsset(objs)) {
    L.push(`- ${grp.name} · ${grp.spawnTool} "${grp.path}" ×${grp.items.length}:`);
    const coords = grp.items.map(it => {
      let s = `[${it.location.join(",")}]`;
      const yaw = Math.round(it.yaw_deg || 0);
      if (yaw) s += `@${yaw}`;
      if (it.scale && it.scale !== 1) s += `*${it.scale}`;
      return s;
    });
    L.push("    " + coords.join("  "));
  }
  if (report && report.overlapsRemaining > 0) {
    L.push("");
    L.push(`NOTE: ${report.overlapsRemaining} placement(s) are still tight after the non-overlap pass — space them out slightly if a spawn visibly overlaps.`);
  }
  if (report && report.missingAssets && report.missingAssets.length) {
    L.push(`NOTE: ${report.missingAssets.length} planned item(s) referenced assets not in the palette and were placed with default size; prefer a palette asset for them.`);
  }
  return L.join("\n");
}

function _persist(o, data) {
  try {
    const dir = process.env.IR_OUTPUT_DIR || path.join(path.resolve(__dirname, "../.."), "tmp", "ir");
    fs.mkdirSync(dir, { recursive: true });
    const base = _slug(data.scene) + "-" + _hash(data.scene);
    fs.writeFileSync(path.join(dir, base + ".json"), JSON.stringify({
      scene: data.scene, graph: data.graph, placed: data.placed,
      solveReport: data.solveReport, planReport: data.planReport,
    }, null, 2));
    fs.writeFileSync(path.join(dir, base + ".ascii.txt"), data.ascii);
    (o.log || (() => {}))("ir artifacts -> " + path.join(dir, base + ".json"));
    return path.join(dir, base + ".json");
  } catch (e) { (o.log || (() => {}))("ir persist failed: " + e.message); return null; }
}

// Persist top-down PLAN renders (every solve + every plan-critic round) so their evolution can be
// inspected later. Default dir: tmp/ir/plan_renders (override IR_PLAN_RENDER_DIR). Never fatal.
function _planRenderDir() { return process.env.IR_PLAN_RENDER_DIR || path.join(path.resolve(__dirname, "../.."), "tmp", "ir", "plan_renders"); }
function _keepPlanRender(srcPng, name, log) {
  try {
    if (!srcPng || !fs.existsSync(srcPng)) return null;
    const dir = _planRenderDir(); fs.mkdirSync(dir, { recursive: true });
    const dest = path.join(dir, name + ".png");
    fs.copyFileSync(srcPng, dest);
    (log || (() => {}))("ir plan render -> " + dest);
    return dest;
  } catch (e) { (log || (() => {}))("ir plan render save failed: " + (e && e.message)); return null; }
}
function _keepPlanCritique(name, crit, log) {
  try {
    const dir = _planRenderDir(); fs.mkdirSync(dir, { recursive: true });
    const txt = `status: ${crit.status}\n\nISSUES:\n` + ((crit.issues || []).map(x => "- " + x).join("\n") || "(none)")
      + `\n\nSUGGESTIONS:\n` + ((crit.suggestions || []).map(x => "- " + x).join("\n") || "(none)") + "\n";
    fs.writeFileSync(path.join(dir, name + ".critique.txt"), txt);
  } catch (_e) {}
}

// Build the IR prompt block for a scene. Returns "" on any failure or when no assets are
// available — IR is an enhancement and must NEVER break the build.
async function buildIRBlock(scene, opts) {
  const o = opts || {};
  const log = o.log || (() => {});
  const mode = resolveSolverMode(o);
  const repair = resolveRepair(o) && mode !== "legacy";
  const bkey = _blockKey(scene, o, mode, repair);
  if (_cache.has(bkey)) { log(`ir cache HIT (block mode=${mode}${repair ? "+repair" : ""})`); return _cache.get(bkey); }

  try {
    const { solve } = require("./ir-solver");
    const { renderAscii } = require("./ir-ascii");
    const planner = require("./ir-planner");

    // Plan ONCE (shared across solver variants), then solve in THIS variant's mode.
    const { graph, assets, planReport, brief } = await _planOnce(scene, o, log);
    if (!graph || !assets.length) { log("ir: no plan/palette, skipping IR"); _cache.set(bkey, ""); return ""; }
    const assetIndex = new Map(assets.map(a => [a.id, a]));

    let curGraph = graph;
    let { placed, report: solveReport } = solve(curGraph, assetIndex, { mode });
    const renderStamp = Date.now(); // groups this build's plan renders
    const rslug = _slug(scene);

    // LLM-repair loop (structure mode only): if STRUCTURAL overlaps remain, ask the LLM to
    // relocate/respace the clashing GROUPS (semantic, never coords), re-solve, accept iff better.
    if (repair) {
      const TH = Math.max(1, Number(process.env.IR_REPAIR_THRESHOLD || 1));
      const ROUNDS = Math.max(0, Number(process.env.IR_REPAIR_ROUNDS || 2));
      for (let round = 1; round <= ROUNDS && solveReport.overlapsRemaining >= TH; round++) {
        const conflicts = (solveReport.residualPairs || []).slice(0, 30);
        log(`ir repair ${round}/${ROUNDS}: ${solveReport.overlapsRemaining} structural overlap(s) → revising ${conflicts.length} group-pair(s)`);
        try {
          const rep = await planner.repairGraph(scene, curGraph, conflicts, assets, Object.assign({}, o, {
            model: process.env.IR_MODEL || o.model,
            provider: process.env.IR_PROVIDER || o.provider || o.runner,
            runner: process.env.IR_PROVIDER || o.runner || o.provider,
            reasoningEffort: process.env.IR_PLANNER_REASONING_EFFORT || o.reasoningEffort || "high",
            timeoutMs: Number(process.env.IR_PLANNER_TIMEOUT_MS || o.timeoutMs || 300000),
          }));
          const g2 = rep && rep.graph;
          if (!g2 || (!(g2.objects || []).length && !(g2.patterns || []).length)) { log("ir repair: empty revision, stopping"); break; }
          const solved2 = solve(g2, assetIndex, { mode });
          if (solved2.report.overlapsRemaining < solveReport.overlapsRemaining) {
            curGraph = g2; placed = solved2.placed; solveReport = solved2.report;
            log(`ir repair ${round}: improved → overlapsRemaining=${solveReport.overlapsRemaining}`);
          } else { log(`ir repair ${round}: no improvement (${solved2.report.overlapsRemaining}), keeping prior plan`); break; }
        } catch (e) { log("ir repair failed (non-fatal): " + (e && e.message)); break; }
      }
    }

    // Plan-level visual critic (fix B): render the solved plan → VLM critiques LAYOUT (density/grouping/
    // focal, NOT collisions) → revise the graph → re-solve. Keeps the deterministic solver.
    if (resolvePlanCritic(o) && placed.length) {
      const planCritic = require("./ir-plan-critic");
      const ROUNDS = Math.max(1, Number(process.env.IR_PLAN_CRITIC_ROUNDS || 1));
      for (let r = 1; r <= ROUNDS; r++) {
        const img = planCritic.renderPlanImage(placed, `${rslug}_r${r}`);
        _keepPlanRender(img, `${rslug}__${renderStamp}__r${r}pre__${placed.length}obj`, log); // keep this round's plan for analysis
        let crit;
        try {
          crit = await planCritic.critiquePlan({ scene, imagePath: img, intent: (brief && brief.trim()) || _planIntent(curGraph), model: process.env.IR_MODEL || o.model, timeoutMs: Number(process.env.IR_PLAN_CRITIC_TIMEOUT_MS || 120000) });
        } catch (e) { log("ir plan-critic failed (non-fatal): " + (e && e.message)); break; }
        try { if (img) fs.rmSync(img, { force: true }); } catch (_e) {}
        _keepPlanCritique(`${rslug}__${renderStamp}__r${r}pre__${placed.length}obj`, crit, log);
        log(`ir plan-critic ${r}/${ROUNDS}: ${crit.status} — ${(crit.issues || []).length} issue(s), ${(crit.suggestions || []).length} fix(es)`);
        if (crit.status === "PASS" || (!(crit.suggestions || []).length && !(crit.issues || []).length)) break;
        try {
          const rev = await planner.revisePlanForLayout(scene, curGraph, crit, assets, Object.assign({}, o, {
            model: process.env.IR_MODEL || o.model, provider: process.env.IR_PROVIDER || o.provider || o.runner,
            runner: process.env.IR_PROVIDER || o.runner || o.provider,
            reasoningEffort: process.env.IR_PLANNER_REASONING_EFFORT || o.reasoningEffort || "high",
            timeoutMs: Number(process.env.IR_PLANNER_TIMEOUT_MS || o.timeoutMs || 300000),
          }));
          if (rev && rev.graph && ((rev.graph.objects || []).length || (rev.graph.patterns || []).length)) {
            curGraph = rev.graph;
            const solved = solve(curGraph, assetIndex, { mode });
            placed = solved.placed; solveReport = solved.report;
            log(`ir plan-critic ${r}: revised -> ${(curGraph.objects || []).length} obj + ${(curGraph.patterns || []).length} pat, ${placed.length} placed`);
          } else { log("ir plan-critic: empty revision, stopping"); break; }
        } catch (e) { log("ir plan-critic revise failed (non-fatal): " + (e && e.message)); break; }
      }
    }

    if (!placed.length) { log("ir: solver produced no placements, skipping"); _cache.set(bkey, ""); return ""; }

    const ascii = renderAscii(placed, { sceneName: curGraph.scene_name || "" });
    let block = formatIRBlock(placed, ascii, solveReport, curGraph, resolveAscii(o));
    // Ground-carpet pass (fix #1): prepend a deterministic, gapless, theme-matched ground carpet so
    // scenes stop showing bare-grey / striped ground (the palette is ground-poor; this picks from the
    // full library and lays it as a guaranteed first step).
    if (resolveGroundPass(o)) {
      try {
        const irGround = require("./ir-ground");
        const db = await require("./asset-retrieval").loadDB();
        const carpetHalf = Number(process.env.IR_GROUND_CARPET_HALF_M) || (process.env.AB_EVAL_SIZE_M ? Math.round(Number(process.env.AB_EVAL_SIZE_M) / 2 + 5) : 52);
        const carpet = irGround.groundCarpetBlock(scene, db, carpetHalf);
        if (carpet) { block = carpet + "\n" + block; log("ir ground-carpet: prepended themed gapless carpet"); }
        else log("ir ground-carpet: no suitable tile found (skipped)");
      } catch (e) { log("ir ground-pass failed (non-fatal): " + (e && e.message)); }
    }
    // final top-down plan render (every IR build) for offline layout analysis
    try {
      const pc = require("./ir-plan-critic");
      const fimg = pc.renderPlanImage(placed, "final");
      _keepPlanRender(fimg, `${rslug}__${renderStamp}__final__${placed.length}obj`, log);
      try { if (fimg) fs.rmSync(fimg, { force: true }); } catch (_e) {}
    } catch (_e) {}
    _persist(o, { scene, graph: curGraph, placed, solveReport, planReport, ascii });
    _artifactCache.set(bkey, { ascii, intent: (brief && brief.trim()) ? brief.trim() : _planIntent(curGraph) });

    log(`ir[${mode}${repair ? "+repair" : ""}]: ${(curGraph.objects || []).length} obj + ${(curGraph.patterns || []).length} patterns → ${placed.length} placed `
      + `(overlapsRemaining=${solveReport.overlapsRemaining}, clamped=${solveReport.clamped}, solver=${solveReport.solver})`);
    _cache.set(bkey, block);
    return block;
  } catch (e) {
    log("ir FAILED (non-fatal, building without plan): " + (e && e.message));
    return "";
  }
}

// Short human/VLM-readable summary of the plan's intent (scene name + named zones/anchors),
// for the visual-loop critic to judge whether the built scene realizes the intended structure.
function _planIntent(g) {
  g = g || {};
  const skip = new Set(["origin", "center"]);
  const zones = Object.keys(g.anchors || {}).filter(k => !skip.has(k));
  const parts = [];
  if (g.scene_name) parts.push(String(g.scene_name).replace(/_/g, " "));
  if (zones.length) parts.push("intended zones/anchors: " + zones.slice(0, 12).join(", "));
  return parts.join("; ");
}

// Return {ascii, intent} for a scene's plan (building + caching it if needed). Used by the visual
// loop to give the critic the INTENDED top-down layout alongside the rendered screenshots.
async function getPlanArtifacts(scene, opts) {
  const o = opts || {};
  const mode = resolveSolverMode(o);
  const repair = resolveRepair(o) && mode !== "legacy";
  const bkey = _blockKey(scene, o, mode, repair);
  if (!_artifactCache.has(bkey)) { try { await buildIRBlock(scene, o); } catch (_e) {} }
  return _artifactCache.get(bkey) || { ascii: "", intent: "" };
}

module.exports = { resolveIRMode, buildIRBlock, formatIRBlock, getPlanArtifacts };

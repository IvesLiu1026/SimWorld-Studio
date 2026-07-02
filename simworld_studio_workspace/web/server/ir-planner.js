"use strict";
// ── IR planner ───────────────────────────────────────────────────────────────
// LLM stage: scene prompt + retrieved asset palette → a RELATIONAL layout graph
// (anchors + objects + patterns + constraints), then validate/normalize it so the
// solver only ever sees clean, real-asset-referencing input.
const { oneshotJSON, oneshotText } = require("./llm-oneshot");

function _num(v, d) { const n = Number(v); return Number.isFinite(n) ? n : d; }
function _clampInt(v, lo, hi) { let n = Math.floor(_num(v, lo)); if (n < lo) n = lo; if (n > hi) n = hi; return n; }
function _fmtDims(d) { if (!d) return "?"; const r = x => Math.round(Number(x) * 10) / 10; return `${r(d.width)}×${r(d.depth)}×${r(d.height)}m`; }

// Compact palette listing the planner picks asset_id values from.
function _assetLines(assets, rich) {
  return (assets || []).map(a => {
    if (!rich) return `- ${a.id} | ${a.category} | ${a.name} | ${_fmtDims(a.dims)}`;
    // "un-blind" the planner: surface the description + tags + subcategory we already retrieve
    // (asset-retrieval provides these but the base prompt drops them), so the planner knows what
    // each asset actually IS, not just its filename + bounding box.
    const cat = a.category + (a.subcategory ? `/${a.subcategory}` : "");
    const desc = a.desc ? " — " + String(a.desc).replace(/\s+/g, " ").trim().slice(0, 110) : "";
    const tags = (Array.isArray(a.tags) && a.tags.length) ? "  [" + a.tags.slice(0, 8).join(", ") + "]" : "";
    return `- ${a.id} | ${cat} | ${a.name} | ${_fmtDims(a.dims)}${desc}${tags}`;
  }).join("\n");
}

function buildPlannerPrompt(scene, assets, brief, rich) {
  return [
    "You are the SCENE LAYOUT PLANNER for a 3D scene built in Unreal Engine. Produce a STRUCTURED",
    "LAYOUT PLAN as JSON that a builder will execute verbatim. You DECIDE what goes where, using",
    "RELATIVE placement (each thing positioned relative to another) so the layout is coherent and",
    "collision-free. A deterministic solver will turn your relations into exact coordinates.",
    "",
    "SCENE:",
    scene,
    "",
    ...(brief ? ["DESIGN BRIEF (you reasoned this out first — realize it FAITHFULLY in the layout below: honor the boundary/focal/axis/symmetry/ground/zone decisions it makes):", brief, ""] : []),
    "ASSET PALETTE (use ONLY these exact asset_id values; dimensions are width×depth×height in metres):",
    _assetLines(assets, rich),
    "",
    "OUTPUT a JSON layout graph with EXACTLY this schema:",
    '{',
    '  "scene_name": "<short name>",',
    '  "anchors": { "center": [0, 0] },              // a few absolute reference points in METRES; include a focal anchor',
    '  "objects": [',
    '    { "id": "<unique>", "asset_id": "<from palette>", "relative_to": "<anchor or another object id>",',
    '      "offset": { "distance": <m>, "angle": <deg> },   // OR {"dx": <m>, "dy": <m>}',
    '      "facing": "<optional anchor/object id to orient toward>" }',
    '  ],',
    '  "patterns": [   // PREFER these for repeated elements (rows, rings, grids of one asset)',
    '    { "id_prefix": "<name>", "asset_id": "<from palette>", "kind": "line|ring|grid",',
    '      "relative_to": "<anchor/object>", "count": <n>,',
    '      "spacing": <m>, "along_angle": <deg>, "offset": {"distance": <m>, "angle": <deg>},  // line',
    '      "radius": <m>, "start_angle": <deg>,                                                 // ring',
    '      "rows": <n>, "cols": <n> }                                                           // grid',
    '  ],',
    '  "constraints": ["non_overlap", "on_ground"]',
    '}',
    "",
    "RULES:",
    "- Angles in DEGREES: 0°=+X (East), 90°=+Y (North), counter-clockwise. All distances/offsets in METRES.",
    "- Place RELATIVE in a chain: hang buildings off roads/anchors, props off buildings, trees off paths — NOT everything from one center.",
    "- COMPOSE WITH INTENT — do NOT sprinkle objects evenly across the map. Cluster related items into tight functional ZONES (market stalls packed in the square; temple halls on a central axis; container stacks in a yard; buildings lining a street) with deliberate NEGATIVE SPACE (paths, plaza, courtyard) between zones. Give each zone/anchor its OWN region so zones don't pile onto each other.",
    "- RIGHT-SIZE to the scene type and keep it COMPACT: an enclosed courtyard / square / market ≈ 40–60 m across; a street or district up to ~100 m. Do NOT push objects out toward the far corners just to fill space — a smaller, fuller scene reads far better than a large sparse one.",
    "- WITHIN a zone, place objects CLOSE but NON-OVERLAPPING — leave each its own footprint (a small gap between them), dense enough to read as full and lived-in. Use line/grid PATTERNS for BUILT repetition only (fences, columns, container rows, lamp lines, stall rows). Aim for ~100–200 placements once patterns expand.",
    "- NATURE IS NOT GRIDDED: scatter trees / plants / rocks in IRREGULAR, DENSE CLUMPS of varying spacing — NEVER a ring or grid of trees (it looks fake). A park or garden should be FULL of trees in natural clusters, not a thin perimeter ring around empty grass.",
    "- BUILD FUNCTIONAL GROUPS by chaining small props off EACH OTHER (relative_to another OBJECT, not always an anchor/center): a bench beside or under a tree, a trash bin next to that bench, lanterns along a path, crates stacked against a wall, stalls flanking a lane. Scatter several such little groups through the scene — that is what makes it feel lived-in and real.",
    "- ALWAYS tile a ground/floor asset from the palette (category ground_and_road) with a grid pattern to carpet the area at z≈0.",
    "- Reference ONLY asset_id values present in the palette above. Give every object/pattern a UNIQUE id/id_prefix.",
    "- Output ONLY the JSON object. No prose, no markdown fences.",
  ].join("\n");
}

// Validate + normalize raw planner JSON against the schema; drop entries referencing
// unknown assets, dedupe ids, coerce types, cap pattern sizes. Returns { graph, report }.
function normalizeGraph(raw, assetIndex) {
  const valid = (assetIndex && typeof assetIndex.get === "function") ? assetIndex : new Map(Object.entries(assetIndex || {}));
  const has = id => valid.has(id);
  const g = (raw && typeof raw === "object" && !Array.isArray(raw)) ? raw : {};
  const report = { droppedObjects: [], droppedPatterns: [], keptObjects: 0, keptPatterns: 0 };

  // anchors
  const anchors = {};
  if (g.anchors && typeof g.anchors === "object") {
    for (const [k, v] of Object.entries(g.anchors)) {
      if (Array.isArray(v)) anchors[k] = v[2] != null ? [_num(v[0], 0), _num(v[1], 0), _num(v[2], 0)] : [_num(v[0], 0), _num(v[1], 0)];
    }
  }
  if (!Object.keys(anchors).length) anchors.origin = [0, 0];

  const seen = new Set(Object.keys(anchors));
  const uniq = base => { let id = String(base || "obj"); let k = 1; while (seen.has(id)) id = `${base}_${k++}`; seen.add(id); return id; };

  // objects
  const objects = [];
  for (const ob of (Array.isArray(g.objects) ? g.objects : [])) {
    const aid = ob && (ob.asset_id || ob.asset);
    if (!ob || !aid || !has(aid)) { report.droppedObjects.push((ob && (ob.id || aid)) || "?"); continue; }
    const norm = { id: uniq(ob.id || aid), asset_id: aid, relative_to: String(ob.relative_to || ob.relativeTo || "origin") };
    const off = ob.offset || {};
    if (off.dx != null || off.dy != null) norm.offset = { dx: _num(off.dx, 0), dy: _num(off.dy, 0) };
    else norm.offset = { distance: _num(off.distance, 0), angle: _num(off.angle, 0) };
    if (ob.facing) norm.facing = String(ob.facing);
    if (ob.rotation_deg != null) norm.rotation_deg = _num(ob.rotation_deg, 0);
    if (ob.scale != null) norm.scale = _num(ob.scale, 1);
    objects.push(norm);
  }

  // patterns
  const MAXCOUNT = _num(process.env.IR_PATTERN_MAX, 80);
  const patterns = [];
  for (const p of (Array.isArray(g.patterns) ? g.patterns : [])) {
    const aid = p && (p.asset_id || p.asset);
    if (!p || !aid || !has(aid)) { report.droppedPatterns.push((p && (p.id_prefix || aid)) || "?"); continue; }
    const kind = ["line", "ring", "grid"].includes(String(p.kind).toLowerCase()) ? String(p.kind).toLowerCase() : "line";
    const np = { id_prefix: uniq(p.id_prefix || p.id || aid), asset_id: aid, kind, relative_to: String(p.relative_to || p.relativeTo || "origin") };
    if (p.facing) np.facing = String(p.facing);
    if (p.scale != null) np.scale = _num(p.scale, 1);
    if (p.offset && typeof p.offset === "object") np.offset = p.offset;
    if (kind === "grid") { np.rows = _clampInt(p.rows, 1, 40); np.cols = _clampInt(p.cols, 1, 40); np.spacing = _num(p.spacing, 5); }
    else if (kind === "ring") { np.count = _clampInt(p.count, 1, MAXCOUNT); np.radius = _num(p.radius, 5); np.start_angle = _num(p.start_angle, 0); }
    else { np.count = _clampInt(p.count, 1, MAXCOUNT); np.spacing = _num(p.spacing, 5); np.along_angle = _num(p.along_angle, 0); }
    patterns.push(np);
  }

  report.keptObjects = objects.length;
  report.keptPatterns = patterns.length;
  const constraints = (Array.isArray(g.constraints) && g.constraints.length) ? g.constraints.map(String) : ["non_overlap", "on_ground"];
  return { graph: { scene_name: String(g.scene_name || ""), anchors, objects, patterns, constraints }, report };
}

// LLM call → normalized graph. assets = retrieve().assets (compact records with dims).
async function planScene(scene, assets, opts) {
  const o = opts || {};
  const prompt = buildPlannerPrompt(scene, assets || [], null, o.richAssets);
  const raw = await oneshotJSON(prompt, Object.assign({ telemetryComponent: "ir_planner" }, o));
  const assetIndex = new Map((assets || []).map(a => [a.id, a]));
  const { graph, report } = normalizeGraph(raw, assetIndex);
  return { graph, report, raw };
}

// Repair prompt: hand the LLM the current plan + the GROUPS whose footprints still collide
// after the structure-preserving solve, and ask it to relocate/respace WHOLE structures
// (semantic moves, never coordinates) so the solver can then place them cleanly.
function buildRepairPrompt(scene, graph, conflicts, assets) {
  const clist = (conflicts || []).map(c => `- ${c[0]}  <->  ${c[1]}`).join("\n") || "- (general crowding in the center)";
  return [
    "You are REVISING a 3D scene LAYOUT PLAN to remove STRUCTURAL OVERLAPS. A deterministic solver",
    "placed your plan and kept every line/grid/ring rigid, but these GROUPS still overlap (their",
    "footprints collide) because the plan packs them into the same space:",
    clist,
    "",
    "Revise the JSON plan so these groups no longer overlap. ALLOWED moves (think like an architect —",
    "keep the scene ORGANIZED: aligned rows, perpendicular structures, a clear focal point, sensible zones):",
    "  • RELOCATE a whole structure — change its relative_to / offset so it sits in a clearer area",
    "  • RESPACE — increase a pattern's spacing, or a grid's spacing",
    "  • RESIZE — reduce a pattern's count if the area is genuinely too small",
    "Do NOT output world coordinates. Keep the EXACT same relational schema (anchors / objects /",
    "patterns / constraints), keep ONLY palette asset_id values, and keep ids stable where possible.",
    "",
    "SCENE:",
    scene,
    "",
    "CURRENT PLAN (revise and return the full updated graph):",
    JSON.stringify(graph),
    "",
    "ASSET PALETTE (asset_id | category | name | dims):",
    _assetLines(assets || []),
    "",
    "Output ONLY the revised JSON layout graph. No prose, no markdown fences.",
  ].join("\n");
}

// LLM call → revised, normalized graph for the structure+repair variant.
async function repairGraph(scene, graph, conflicts, assets, opts) {
  const o = opts || {};
  const prompt = buildRepairPrompt(scene, graph, conflicts || [], assets || []);
  const raw = await oneshotJSON(prompt, Object.assign({ telemetryComponent: "ir_repair" }, o));
  const assetIndex = new Map((assets || []).map(a => [a.id, a]));
  const { graph: g2, report } = normalizeGraph(raw, assetIndex);
  return { graph: g2, report, raw };
}

// ── CoT / reasoning planner (two-stage) ───────────────────────────────────────
// Stage 1: reason like an art director → a prose DESIGN BRIEF that DECIDES the composition
// per-scene (boundary? focal where? axis? symmetry-or-organic? ground materials? zones?).
// Stage 2: realize that brief as the JSON graph. Nothing is hardcoded — the LLM decides what
// applies to THIS scene (e.g. symmetry for a temple, organic for a market).
function _categorySummary(assets) {
  const m = new Map();
  for (const a of (assets || [])) { const c = (a && a.category) || "?"; m.set(c, (m.get(c) || 0) + 1); }
  return [...m.entries()].sort((a, b) => b[1] - a[1]).map(([c, n]) => `${c} (${n})`).join(", ");
}

function buildDesignBriefPrompt(scene, assets) {
  return [
    "You are an expert 3D environment ART DIRECTOR. BEFORE any coordinates, reason about how to",
    "compose this scene so it reads like a real, recognizable place.",
    "",
    "SCENE:", scene, "",
    "ASSET CATEGORIES AVAILABLE (with counts):", _categorySummary(assets), "",
    "Think step by step, then write a concise DESIGN BRIEF (prose, ~120-220 words). Decide what",
    "actually applies to THIS scene — do NOT force ideas that don't fit (e.g. formal symmetry suits",
    "a temple/palace but NOT a market, village, or harbour). Cover, only as appropriate:",
    "- Real-world reference: what IS this place and how is such a place actually laid out?",
    "- Boundary/enclosure: is there a perimeter (wall / fence / buildings-as-edge) with a gate or",
    "  opening, or is it open on some sides?",
    "- Focal element: the main structure — where does it sit (dead center, off-center, set back from",
    "  the entrance)? Is there an approach axis or path leading to it?",
    "- Symmetry vs organic: is this scene type formal/symmetric or irregular/organic? Decide and commit.",
    "- Ground & materials: what covers the floor, and where does it CHANGE (e.g. grass field + a stone",
    "  path + a stone building base; a concrete yard; sand; snow)?",
    "- Zones, density & functional groupings: where is it dense vs open; which small clusters belong",
    "  together (stalls in a row, benches under trees, crates against a wall).",
    "Be specific and decisive — this brief will directly drive the layout. Output ONLY the brief prose.",
  ].join("\n");
}

async function planSceneCot(scene, assets, opts) {
  const o = opts || {};
  let brief = "";
  try {
    brief = await oneshotText(buildDesignBriefPrompt(scene, assets || []), Object.assign({ telemetryComponent: "ir_planner_brief" }, o));
    brief = String(brief || "").trim();
  } catch (_e) { brief = ""; }
  const prompt = buildPlannerPrompt(scene, assets || [], brief, o.richAssets);
  const raw = await oneshotJSON(prompt, Object.assign({ telemetryComponent: "ir_planner" }, o));
  const assetIndex = new Map((assets || []).map(a => [a.id, a]));
  const { graph, report } = normalizeGraph(raw, assetIndex);
  return { graph, report, raw, brief };
}

// ── Plan-critic revision (fix B) ──────────────────────────────────────────────
// A VLM critic looked at a TOP-DOWN render of the solved plan and flagged LAYOUT problems
// (emptiness, poor grouping, weak focal, imbalance). Revise the relational graph to address them —
// add/regroup to fill sparse areas, strengthen structure. Collisions are NOT the concern (solver handles).
function buildLayoutRevisePrompt(scene, graph, critique, assets) {
  const issues = (critique.issues || []).map(x => "- " + x).join("\n") || "(none)";
  const sugg = (critique.suggestions || []).map(x => "- " + x).join("\n") || "(none)";
  const anchorNames = Object.keys((graph && graph.anchors) || {});
  return [
    "You are IMPROVING a 3D scene LAYOUT PLAN. A critic looked at a TOP-DOWN RENDER of the current",
    "solved plan and flagged LAYOUT problems (emptiness, weak grouping/focal, imbalance). Your job:",
    "output ADDITIONAL objects and patterns to ADD to the plan that fix these — fill sparse/empty areas,",
    "reinforce functional clusters, strengthen the focal structure, balance density. Collisions are NOT",
    "your concern (a deterministic solver handles them).",
    "",
    "CRITIC ISSUES:", issues,
    "CRITIC SUGGESTIONS:", sugg,
    "",
    "SCENE:", scene, "",
    "EXISTING PLAN (read-only context — do NOT repeat or restate these items):",
    JSON.stringify(graph),
    "",
    "ASSET PALETTE (use ONLY these exact asset_id values):",
    _assetLines(assets, true),
    "",
    "You may place items relative to EXISTING anchors by name (" + (anchorNames.slice(0, 12).join(", ") || "none") + ")",
    "or define new anchors. Output ONLY the ADDITIONS as a compact JSON object of this shape:",
    '{"anchors": {"name":[x_m,y_m], ...}, "objects": [ ...new objects... ], "patterns": [ ...new patterns... ]}',
    "Include ONLY the new items needed to fix the issues (this keeps the output small). Objects/patterns use",
    "the SAME schema as the main plan. No prose, no markdown fences, no repetition of existing items.",
  ].join("\n");
}

// Additive revise: the model returns ONLY new items to ADD (small output → no truncation of the full,
// densified graph, which is what was silently failing the revise). We merge them into the base graph.
async function revisePlanForLayout(scene, graph, critique, assets, opts) {
  const o = opts || {};
  const prompt = buildLayoutRevisePrompt(scene, graph, critique || {}, assets || []);
  const raw = await oneshotJSON(prompt, Object.assign({ telemetryComponent: "ir_plan_revise" }, o));
  const addObjs = Array.isArray(raw && raw.objects) ? raw.objects : [];
  const addPats = Array.isArray(raw && raw.patterns) ? raw.patterns : [];
  const merged = {
    scene_name: graph.scene_name,
    anchors: Object.assign({}, graph.anchors || {}, (raw && raw.anchors) || {}),
    objects: (graph.objects || []).concat(addObjs),
    patterns: (graph.patterns || []).concat(addPats),
    constraints: graph.constraints || [],
  };
  const assetIndex = new Map((assets || []).map(a => [a.id, a]));
  const { graph: g2, report } = normalizeGraph(merged, assetIndex);
  return { graph: g2, report, raw, added: { objects: addObjs.length, patterns: addPats.length } };
}

module.exports = { planScene, planSceneCot, repairGraph, revisePlanForLayout, normalizeGraph, buildPlannerPrompt, buildDesignBriefPrompt, buildRepairPrompt };

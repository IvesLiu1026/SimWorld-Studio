"use strict";
// ── Reflect agent (staged builder, Phase 4) ───────────────────────────────────
// One VLM round per tier: given the scene prompt + a PER-ITEM STATUS TABLE + the authoritative
// measureReport + the retrieved palette + annotated top/aerial images + the op ledger, the critic
// proposes TYPED ops on the persistent IR (never freeform, never raw coords) or no_change. A
// deterministic validator (normalizeOps) enforces the guardrails, then the orchestrator applies +
// gates (checkpoint → apply → re-measure → revert-on-regression). Mirrors ir-plan-critic plumbing
// (codex exec --json -i <images> --output-schema); runs SANDBOXED read-only (it needs zero tools).
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");
const os = require("os");
const { codexModel, extractJSON, parseCodexJsonl } = require("./llm-oneshot");

const OP_TYPES = new Set(["add_object", "add_pattern", "remove", "move", "swap_asset", "adjust_pattern", "rotate"]);
const PLACED_OPS = new Set(["remove", "move", "swap_asset", "adjust_pattern", "rotate"]);

// ── Deterministic validator ───────────────────────────────────────────────────
// ctx: { placedIds:Set, markNumbers:Set, palette:Set, ledger:[{op,id,attr}], frozenAttrs:Set<"id|attr">,
//        placedBudget, totalCap }. Returns { valid:[normalized ops], dropped:[{op,reason}] }.
// Rules (each evidence-backed, §3.6): placed-ops need an evidence string citing an EXISTING mark#; capped
// at placedBudget/tier and totalCap overall; ids must exist; add_* asset_ids must be in the palette; NO
// absolute coordinates anywhere; an op inverting a prior applied op on the same id+attribute is dropped
// and that attribute frozen. Invalid ops are dropped individually (never fail the whole batch).
function _opAttr(op) {
  switch (op.op) {
    case "move": return "position";
    case "swap_asset": return "asset";
    case "remove": return "exists";
    case "adjust_pattern": return "pattern";
    case "rotate": return "yaw";
    default: return null;
  }
}
function _citesMark(evidence, markNumbers) {
  const m = String(evidence || "").match(/mark\s*#?\s*(\d+)/i);
  if (!m) return false;
  return markNumbers.has(Number(m[1]));
}
function _hasAbsoluteCoords(obj) {
  // reject any absolute [x,y,z] / location / world coords; relative placement only (solver authority)
  const s = JSON.stringify(obj || {});
  if (/"location"|"world"|"absolute"|"x_cm"|"y_cm"/.test(s)) return true;
  const to = obj && obj.to;
  if (to && to.location) return true;
  return false;
}
// Coerce common model field-name drift into the canonical op shape before validation (models sometimes
// use target/actor for id, put relative_to/offset at the top level of a move, etc.).
function _coerceOp(raw) {
  const op = Object.assign({}, raw);
  if (op.id == null && (op.target != null || op.actor != null || op.mark_id != null)) op.id = op.target || op.actor || op.mark_id;
  if (op.op === "move") {
    if (!op.to || typeof op.to !== "object") op.to = {};
    if (op.to.relative_to == null && op.relative_to != null) op.to.relative_to = op.relative_to;
    if (op.to.offset == null && op.offset != null) op.to.offset = op.offset;
  }
  if (op.op === "adjust_pattern" && op.id_prefix == null && op.id != null) op.id_prefix = op.id;
  return op;
}
function normalizeOps(ops, ctx) {
  const c = ctx || {};
  const placedIds = c.placedIds || new Set();
  const markNumbers = c.markNumbers || new Set();
  const palette = c.palette || new Set();
  const frozen = c.frozenAttrs || new Set();
  const placedBudget = Number.isFinite(c.placedBudget) ? c.placedBudget : 3;
  const totalCap = Number.isFinite(c.totalCap) ? c.totalCap : 12;
  const valid = [], dropped = [];
  let placedUsed = 0;
  for (const raw of (Array.isArray(ops) ? ops : [])) {
    const op = raw && typeof raw === "object" ? _coerceOp(raw) : {};
    const drop = (reason) => dropped.push({ op, reason });
    if (!OP_TYPES.has(op.op)) { drop("unknown_op"); continue; }
    if (valid.length >= totalCap) { drop("total_cap"); continue; }
    const isPlaced = PLACED_OPS.has(op.op);
    if (isPlaced) {
      const id = op.op === "adjust_pattern" ? op.id_prefix : op.id;
      if (id == null || !placedIds.has(String(id))) { drop("unknown_id"); continue; }
      if (!_citesMark(op.evidence, markNumbers)) { drop("no_mark_evidence"); continue; }
      const attr = _opAttr(op);
      if (attr && frozen.has(String(id) + "|" + attr)) { drop("attr_frozen_anti_oscillation"); continue; }
      if (placedUsed >= placedBudget) { drop("placed_budget"); continue; }
    }
    // adds: palette-only, no absolute coords
    if (op.op === "add_object") {
      const o = op.object || {};
      const aid = o.asset_id || o.asset;
      if (!aid || !palette.has(String(aid))) { drop("add_asset_not_in_palette"); continue; }
      if (_hasAbsoluteCoords(o)) { drop("absolute_coords"); continue; }
      if (!o.relative_to) { drop("add_missing_relative_to"); continue; }
    } else if (op.op === "add_pattern") {
      const p = op.pattern || {};
      const aid = p.asset_id || p.asset;
      if (!aid || !palette.has(String(aid))) { drop("add_asset_not_in_palette"); continue; }
      if (_hasAbsoluteCoords(p)) { drop("absolute_coords"); continue; }
      if (!p.relative_to) { drop("add_missing_relative_to"); continue; }
    } else if (op.op === "move") {
      if (_hasAbsoluteCoords(op)) { drop("absolute_coords"); continue; }
      if (!op.to || !op.to.relative_to) { drop("move_missing_relative_to"); continue; }
    } else if (op.op === "swap_asset") {
      if (!op.new_asset_id || !palette.has(String(op.new_asset_id))) { drop("swap_asset_not_in_palette"); continue; }
    } else if (op.op === "adjust_pattern") {
      if (op.count == null && op.spacing == null) { drop("adjust_pattern_noop"); continue; }
    } else if (op.op === "rotate") {
      const y = op.yaw_deg != null ? op.yaw_deg : op.delta_yaw_deg;
      if (!Number.isFinite(Number(y))) { drop("rotate_no_yaw"); continue; }
    }
    valid.push(op);
    if (isPlaced) placedUsed++;
  }
  return { valid, dropped };
}

// Update the anti-oscillation freeze set from applied ops (call after a batch is applied). An id+attr
// that has now been touched by BOTH a forward and a reverse op is frozen for the rest of the build.
function updateFreeze(ledger, appliedOps, frozenAttrs) {
  for (const op of appliedOps) {
    const id = op.op === "adjust_pattern" ? op.id_prefix : op.id;
    const attr = _opAttr(op);
    if (id == null || !attr) continue;
    const key = String(id) + "|" + attr;
    const prior = ledger.filter(e => String(e.id) === String(id) && e.attr === attr).length;
    if (prior >= 1) frozenAttrs.add(key); // touched before → inverting/repeated edits freeze it
    ledger.push({ op: op.op, id: String(id), attr });
  }
  return frozenAttrs;
}

// ── Prompt ────────────────────────────────────────────────────────────────────
const REFLECT_SYSTEM =
  "You are a 3D scene BUILD critic reviewing a scene mid-construction, tier by tier. You are shown one or " +
  "more screenshots (see the VIEWS list for what each is, in order) with NUMBERED MARKS on each placed " +
  "object (green=ok, red=defect, grey=failed-to-spawn, purple=solver-blocked) and DASHED GHOST outlines " +
  "where LATER tiers' objects will go. A per-item STATUS TABLE and a MEASURE REPORT are provided and are " +
  "AUTHORITATIVE.\n" +
  "- Use the TOP-DOWN/AERIAL views for layout, grouping, and dead space; use any EYE-LEVEL views for what " +
  "they alone reveal — facing/orientation, relative scale, interpenetration, and street-level believability.\n" +
  "RULES:\n" +
  "- Collisions/floating are ALREADY handled by the deterministic solver (see the measure report) — do NOT " +
  "spend edits re-fixing overlaps or grounding, and NEVER count objects or hunt collisions in pixels. Only " +
  "escalate a collision if it is a PROMINENT, visually-obvious wrongness in a focal area.\n" +
  "- NEVER flag `scheduled(tier n)` items as missing — they are intentionally not placed yet (see ghosts).\n" +
  "- SPEND YOUR EDITS ON WHAT THE IMAGES REVEAL AND THE MEASURE REPORT CANNOT: facing/orientation (objects " +
  "or fences turned the wrong way), path/approach clarity, dead space, scale mismatch, grouping, and " +
  "street-level believability. PREFER a rotate or an ADD to pending tiers over moving/removing placed items.\n" +
  "- Every claim about a specific object MUST cite its mark number (e.g. 'mark 12').\n" +
  "- Most tiers need 0-2 edits; an EMPTY ops list is a common, correct answer. Do not invent defects.\n" +
  "- Output ONLY typed ops on the plan (relative placement, palette assets only) — never raw coordinates.\n" +
  "OP FORMATS — use these EXACT field names. Every op on a PLACED item needs an \"evidence\" string citing a mark#:\n" +
  "  {\"op\":\"add_object\",\"object\":{\"id\":\"<new-unique>\",\"asset_id\":\"<from palette>\",\"relative_to\":\"<id or anchor>\",\"offset\":{\"distance\":<m>,\"angle\":<deg>}}}\n" +
  "  {\"op\":\"add_pattern\",\"pattern\":{\"id_prefix\":\"<new>\",\"asset_id\":\"<palette>\",\"kind\":\"line|ring|grid\",\"relative_to\":\"<id/anchor>\",\"count\":<n>,\"spacing\":<m>}}\n" +
  "  {\"op\":\"remove\",\"id\":\"<id>\",\"evidence\":\"mark <n>: <why>\"}\n" +
  "  {\"op\":\"move\",\"id\":\"<id>\",\"to\":{\"relative_to\":\"<id/anchor>\",\"offset\":{\"distance\":<m>,\"angle\":<deg>}},\"evidence\":\"mark <n>: <why>\"}\n" +
  "  {\"op\":\"swap_asset\",\"id\":\"<id>\",\"new_asset_id\":\"<palette>\",\"evidence\":\"mark <n>: <why>\"}\n" +
  "  {\"op\":\"rotate\",\"id\":\"<id>\",\"yaw_deg\":<abs deg, 0=East>  (or \"delta_yaw_deg\":<relative>),\"evidence\":\"mark <n>: facing wrong\"}\n" +
  "  {\"op\":\"adjust_pattern\",\"id_prefix\":\"<prefix>\",\"count\":<n>,\"spacing\":<m>,\"evidence\":\"mark <n>: <why>\"}";

function _schema() {
  const fp = path.join(os.tmpdir(), "ir_reflect_schema.json");
  const schema = {
    type: "object", additionalProperties: false,
    properties: {
      assessment: { type: "string" },
      no_change: { type: "boolean" },
      ops: {
        type: "array",
        items: {
          type: "object", additionalProperties: true,
          properties: { op: { type: "string" }, id: { type: "string" }, id_prefix: { type: "string" },
            evidence: { type: "string" }, new_asset_id: { type: "string" },
            count: { type: "number" }, spacing: { type: "number" },
            object: { type: "object" }, pattern: { type: "object" }, to: { type: "object" } },
          required: ["op"],
        },
      },
    }, required: ["assessment", "ops", "no_change"],
  };
  try { fs.writeFileSync(fp, JSON.stringify(schema)); } catch (_e) {}
  return fp;
}

// Assemble the per-item status table (the partial-scene fix): one row per plan object/group.
function statusTable(placed, markOf, curTier, measureReport) {
  const collidingWith = (measureReport && measureReport.colliding_with) || {};
  const floating = new Set((measureReport && measureReport.floating) || []);
  const rows = [];
  for (const p of placed) {
    if (p.isGround) continue;
    const id = String(p.id);
    let status;
    if (p.tier > curTier) status = "scheduled(tier " + p.tier + ")";
    else if (p.status === "spawn_failed") status = "spawn_failed";
    else if (p.status === "solver_blocked") status = "solver_blocked";
    else if (floating.has(id)) status = "placed-defect(floating)";
    else if (collidingWith[id] && collidingWith[id].length) status = "placed-defect(colliding-with " + collidingWith[id].slice(0, 2).join(",") + ")";
    else status = "placed-ok";
    rows.push("#" + (markOf.get(id) || "?") + " | " + id + " | " + (p.name || p.asset_id) + " | " + (p.category || "") + " | tier " + p.tier + " | " + status);
  }
  return rows;
}

// Run the reflect round. Returns { assessment, ops:[raw], no_change, raw, dropped:[], modelText }.
function reflect(bundle, opts) {
  const o = opts || {};
  return new Promise((resolve) => {
    const images = (bundle.images || []).filter(p => p && fs.existsSync(p));
    const rows = bundle.statusTable || [];
    const palette = (bundle.palette || []).map(a => `${a.id} | ${a.name || ""} | ${a.category || ""} | ${a.dims || ""}`);
    const _vdesc = { top: "TOP-DOWN (near-overhead)", aerial: "AERIAL 3/4 (facades+depth)", eye_e: "EYE-LEVEL from the east, looking across the scene", eye_w: "EYE-LEVEL from the west, looking across the scene" };
    const viewsLine = (bundle.imageViews && bundle.imageViews.length)
      ? "VIEWS (in image order): " + bundle.imageViews.map((v, i) => {
        let d = _vdesc[v]; if (!d && /^eye_\d/.test(v)) d = "EYE-LEVEL local view on walkable ground (its marks are ONLY the nearby objects it sees)";
        return `#${i + 1} ${d || v}`;
      }).join("; ") : "";
    const prompt = [
      REFLECT_SYSTEM, "",
      `SCENE: ${bundle.scene}`,
      bundle.brief ? `DESIGN BRIEF: ${String(bundle.brief).slice(0, 400)}` : "",
      `TIER CONTEXT: tier ${bundle.tier} of ${bundle.tiers} just spawned. Later tiers are shown as dashed ghosts.`,
      viewsLine,
      "", "MEASURE REPORT (authoritative — do NOT recompute from pixels):",
      `  checked=${bundle.measure.checked} structural_collision_rate=${bundle.measure.structural_collision_rate} ` +
      `colliding_actors=${(bundle.measure.structural_collision_actors || []).length} floating=${(bundle.measure.floating || []).length} spawn_failed=${bundle.spawnFailed || 0} solver_blocked=${bundle.solverBlocked || 0}`,
      "", `PER-ITEM STATUS TABLE (mark# | id | asset | category | tier | status):`, ...rows.slice(0, 400),
      "", "PALETTE (asset_id | name | category | dims) — the ONLY legal vocabulary for adds/swaps:", ...palette.slice(0, 200),
      bundle.ledger && bundle.ledger.length ? "\nOP LEDGER (earlier tiers): " + JSON.stringify(bundle.ledger.slice(-20)) : "",
      "", "Return ONLY the JSON {assessment, ops, no_change}.",
    ].filter(Boolean).join("\n");

    // No --output-schema: the typed-ops union is awkward for strict structured output; we prompt for
    // JSON and parse it from the agent message (extractJSON). Read-only sandbox = contamination guardrail.
    const args = ["exec", "--json", "--skip-git-repo-check", "-s", "read-only",
      "-m", codexModel(o.model), "-c", `model_reasoning_effort=${o.effort || process.env.IR_REFLECT_EFFORT || "high"}`,
      "-C", os.tmpdir()];
    for (const img of images) args.push("-i", img);
    args.push("-");
    const env = { ...process.env, NO_COLOR: "1" };
    Object.keys(env).forEach(k => { if (k.startsWith("CLAUDE")) delete env[k]; });
    const timeoutMs = Number(o.timeoutMs || process.env.IR_REFLECT_TIMEOUT_MS || 180000);
    const p = spawn(process.env.CODEX_BIN || "codex", args, { stdio: ["pipe", "pipe", "pipe"], cwd: os.tmpdir(), env });
    const timer = setTimeout(() => { try { p.kill("SIGTERM"); } catch (_e) {} resolve({ assessment: "", ops: [], no_change: true, error: "timeout" }); }, timeoutMs);
    let so = "";
    try { p.stdin.write(prompt); p.stdin.end(); } catch (_e) {}
    p.stdout.on("data", d => { so += d.toString(); });
    p.on("error", (e) => { clearTimeout(timer); resolve({ assessment: "", ops: [], no_change: true, error: (e && e.message) || "spawn_error" }); });
    p.on("close", () => {
      clearTimeout(timer);
      let r = null;
      try { r = extractJSON(parseCodexJsonl(so).last_agent_text || ""); } catch (_e) {}
      try { require("./telemetry").record({ component: "reflect", model: codexModel(o.model), reasoning: o.effort || "high" }); } catch (_e) {}
      if (!r || typeof r !== "object") return resolve({ assessment: "", ops: [], no_change: true, raw: r });
      resolve({ assessment: String(r.assessment || ""), ops: Array.isArray(r.ops) ? r.ops : [], no_change: !!r.no_change, raw: r });
    });
  });
}

// ── Apply typed ops to the persistent IR graph ────────────────────────────────
// Mutates `graph` in place and returns the deltas the orchestrator enacts:
//   deleteIds  — actors to delete NOW (remove / move / swap re-spawn),
//   reassignIds— moved/swapped ids to schedule into the NEXT tier (respawn re-solved),
//   addedIds   — new object ids to schedule into the next tier,
//   opLog      — [{op, id, outcome:'applied'}] appended for the ledger.
// One mechanism, no special cases: placed-item move/swap = delete now + reassign to k+1.
function applyOps(ops, graph, curTier) {
  const g = graph;
  g.objects = Array.isArray(g.objects) ? g.objects : [];
  g.patterns = Array.isArray(g.patterns) ? g.patterns : [];
  const objById = new Map(g.objects.map(o => [String(o.id), o]));
  const patByPrefix = new Map(g.patterns.map(p => [String(p.id_prefix || p.id), p]));
  const deleteIds = new Set(), reassignIds = new Set(), addedIds = new Set();
  const opLog = [];
  let addN = 0;
  const uniqId = (base) => { let id = base; while (objById.has(id) || addedIds.has(id)) id = base + "_r" + (++addN); return id; };
  for (const op of (ops || [])) {
    try {
      if (op.op === "add_object") {
        const o = Object.assign({}, op.object);
        o.id = uniqId(String(o.id || o.asset_id || "add") + "_t" + curTier);
        g.objects.push(o); objById.set(o.id, o); addedIds.add(o.id);
        opLog.push({ op: "add_object", id: o.id, outcome: "applied" });
      } else if (op.op === "add_pattern") {
        const p = Object.assign({}, op.pattern);
        p.id_prefix = uniqId(String(p.id_prefix || p.asset_id || "pat") + "_t" + curTier);
        g.patterns.push(p); addedIds.add(p.id_prefix);
        opLog.push({ op: "add_pattern", id: p.id_prefix, outcome: "applied" });
      } else if (op.op === "remove") {
        const id = String(op.id);
        g.objects = g.objects.filter(o => String(o.id) !== id);
        g.patterns = g.patterns.filter(p => String(p.id_prefix || p.id) !== id);
        deleteIds.add(id);
        opLog.push({ op: "remove", id, outcome: "applied" });
      } else if (op.op === "move") {
        const o = objById.get(String(op.id));
        if (o && op.to && op.to.relative_to) { o.relative_to = String(op.to.relative_to); if (op.to.offset) o.offset = op.to.offset; deleteIds.add(String(op.id)); reassignIds.add(String(op.id)); opLog.push({ op: "move", id: String(op.id), outcome: "applied" }); }
        else opLog.push({ op: "move", id: String(op.id), outcome: "skipped_no_target" });
      } else if (op.op === "swap_asset") {
        const o = objById.get(String(op.id));
        if (o) { o.asset_id = String(op.new_asset_id); deleteIds.add(String(op.id)); reassignIds.add(String(op.id)); opLog.push({ op: "swap_asset", id: String(op.id), outcome: "applied" }); }
        else opLog.push({ op: "swap_asset", id: String(op.id), outcome: "skipped_missing" });
      } else if (op.op === "adjust_pattern") {
        const p = patByPrefix.get(String(op.id_prefix));
        if (p) { if (op.count != null) p.count = Math.max(1, Math.floor(op.count)); if (op.spacing != null) p.spacing = Number(op.spacing); opLog.push({ op: "adjust_pattern", id: String(op.id_prefix), outcome: "applied" }); }
        else opLog.push({ op: "adjust_pattern", id: String(op.id_prefix), outcome: "skipped_missing" });
      } else if (op.op === "rotate") {
        const o = objById.get(String(op.id));
        if (o) {
          const cur = Number(o.yaw_deg || 0);
          o.yaw_deg = op.yaw_deg != null ? Number(op.yaw_deg) : cur + Number(op.delta_yaw_deg || 0);
          deleteIds.add(String(op.id)); reassignIds.add(String(op.id));   // respawn with new yaw
          opLog.push({ op: "rotate", id: String(op.id), outcome: "applied" });
        } else opLog.push({ op: "rotate", id: String(op.id), outcome: "skipped_missing" });
      }
    } catch (e) { opLog.push({ op: op.op, id: op.id || op.id_prefix, outcome: "error:" + (e && e.message) }); }
  }
  return { deleteIds, reassignIds, addedIds, opLog };
}

module.exports = { normalizeOps, updateFreeze, statusTable, reflect, applyOps, REFLECT_SYSTEM };

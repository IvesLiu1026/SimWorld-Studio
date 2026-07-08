"use strict";
// ── Tier derivation (staged builder, Phase 2) ─────────────────────────────────
// Pure function: assign each solved `placed[]` entry a build tier from the IR graph — no LLM, no IO.
//   tier 0  ground (spawned with/after the carpet)
//   tier 1  anchors / structures / focal hubs / boundary lines  (the backbone, spawned first)
//   tier 2  secondary objects
//   tier 3  small clutter
// Tiers are evaluated per GROUP (a pattern moves as a unit — its members share one tier), matching
// PlanIT "most-constrained-first". Deterministic; zero prompt changes (locked decision D2).
//
// Rules (first match wins, per group):
//   1. isGround                                                        -> tier 0
//   2. hub score >= 2 (# of graph entries whose relative_to := this id) -> tier 1  (small-but-focal)
//   3. category in STRUCTURAL_CATS, OR group radius >= IR_TIER_BIG_M,
//      OR a barriers_and_fencing group whose extent >= 8 m (boundary)  -> tier 1
//   4. member radius < IR_CLUTTER_RADIUS_M AND hub score 0             -> tier 3
//   5. else                                                            -> tier 2
// Constraint pass: a child (via relative_to chain) is never in an EARLIER tier than its parent.

const CLUTTER_R_M = Math.max(0, Number(process.env.IR_CLUTTER_RADIUS_M || 0.8));

function _num(v, d) { const n = Number(v); return Number.isFinite(n) ? n : d; }
function _structuralCats() {
  const raw = process.env.IR_TIER_STRUCTURAL_CATS || "buildings,building_pieces,walls";
  return new Set(raw.split(",").map(s => s.trim().toLowerCase()).filter(Boolean));
}

// Build { hub: refId->count, refOf: id->relative_to } from the graph (objects + patterns).
// Pattern members (id_prefix_N) inherit their pattern's relative_to via the id_prefix key.
function _graphRefs(graph) {
  const g = graph || {};
  const hub = new Map();
  const refOf = new Map();
  const bump = (ref) => { if (ref) hub.set(ref, (hub.get(ref) || 0) + 1); };
  for (const o of (Array.isArray(g.objects) ? g.objects : [])) {
    if (!o || !o.id) continue;
    const ref = o.relative_to || o.relativeTo || "origin";
    refOf.set(String(o.id), String(ref)); bump(String(ref));
  }
  for (const p of (Array.isArray(g.patterns) ? g.patterns : [])) {
    if (!p) continue;
    const key = String(p.id_prefix || p.id || "");
    const ref = p.relative_to || p.relativeTo || "origin";
    if (key) refOf.set(key, String(ref));
    bump(String(ref));
  }
  return { hub, refOf };
}

// assignTiers(graph, placed, opts?) -> the SAME placed array with each entry given `.tier` (0..K).
function assignTiers(graph, placed, opts) {
  const o = opts || {};
  const K = Math.max(1, Math.floor(_num(o.tiers != null ? o.tiers : process.env.IR_TIERS, 3)));
  const BIG_M = Math.max(0, _num(o.bigM != null ? o.bigM : process.env.IR_TIER_BIG_M, 3.0));
  const BOUNDARY_M = Math.max(0, _num(process.env.IR_TIER_BOUNDARY_M, 8.0));
  const STRUCT = _structuralCats();
  const list = Array.isArray(placed) ? placed : [];
  if (!list.length) return list;

  const { hub, refOf } = _graphRefs(graph);

  // Partition into groups: pattern members share `group`; a standalone object is its own singleton.
  const groups = new Map();               // key -> { key, members:[], isGround, cat, radius, ... }
  const idToGroupKey = new Map();
  for (const p of list) {
    const key = p.group ? ("g:" + p.group) : ("o:" + p.id);
    if (!groups.has(key)) groups.set(key, { key, members: [], groupField: p.group || null });
    groups.get(key).members.push(p);
    idToGroupKey.set(String(p.id), key);
  }

  // Per-group aggregates + hub score.
  for (const G of groups.values()) {
    const ms = G.members;
    G.isGround = ms.some(m => m.isGround);
    G.cat = String((ms[0] && ms[0].category) || "").toLowerCase();
    G.radius = ms.reduce((r, m) => Math.max(r, _num(m.radius, 0)), 0);
    // hub score: max over member ids (things referencing a member) and, for patterns, the id_prefix.
    let hs = 0;
    for (const m of ms) hs = Math.max(hs, hub.get(String(m.id)) || 0);
    if (G.groupField) hs = Math.max(hs, hub.get(String(G.groupField)) || 0);
    G.hub = hs;
    // group extent (m): bbox diagonal of member positions — boundary lines are long.
    let minx = Infinity, maxx = -Infinity, miny = Infinity, maxy = -Infinity;
    for (const m of ms) { const x = _num(m.x_m, 0), y = _num(m.y_m, 0); if (x < minx) minx = x; if (x > maxx) maxx = x; if (y < miny) miny = y; if (y > maxy) maxy = y; }
    G.extent = ms.length > 1 ? Math.hypot(maxx - minx, maxy - miny) : 0;
    // parent ref: pattern uses id_prefix->ref; standalone uses id->ref.
    G.parentRef = G.groupField ? (refOf.get(String(G.groupField)) || "origin") : (refOf.get(String(ms[0].id)) || "origin");
  }

  // Rule-based tier per group (first match wins).
  const tierOf = (G) => {
    if (G.isGround) return 0;
    if (G.hub >= 2) return 1;
    const isBarrier = /barrier|fenc/.test(G.cat);
    if (STRUCT.has(G.cat) || G.radius >= BIG_M || (isBarrier && G.extent >= BOUNDARY_M)) return 1;
    if (G.radius < CLUTTER_R_M && G.hub === 0) return 3;
    return 2;
  };
  for (const G of groups.values()) G.tier = Math.min(K, tierOf(G));

  // Constraint pass: a child is never in an earlier tier than its parent (bump children later).
  // Iterate to a fixed point over the group DAG (relative_to chains are shallow).
  for (let pass = 0; pass < groups.size + 2; pass++) {
    let changed = false;
    for (const G of groups.values()) {
      if (G.isGround) continue;
      const pk = idToGroupKey.get(String(G.parentRef));   // undefined if parent is an anchor (origin, …)
      if (pk && groups.has(pk)) {
        const P = groups.get(pk);
        if (P.tier > G.tier) { G.tier = Math.min(K, P.tier); changed = true; }
      }
    }
    if (!changed) break;
  }

  // Write tier onto every placed entry.
  for (const G of groups.values()) for (const m of G.members) m.tier = G.tier;
  return list;
}

// Small summary for logging / persistence: count per tier.
function tierHistogram(placed) {
  const h = {};
  for (const p of (placed || [])) { const t = p.tier == null ? "?" : p.tier; h[t] = (h[t] || 0) + 1; }
  return h;
}

module.exports = { assignTiers, tierHistogram };

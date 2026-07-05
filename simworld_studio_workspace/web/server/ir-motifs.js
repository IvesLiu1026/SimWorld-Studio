"use strict";
// ── Motif / assembly layer (Phase C) ──────────────────────────────────────────
// Flat per-object planning maxes out at "populated": dense but monotone (empty stall skeletons, no
// relational micro-structure). Motifs are canonical multi-object arrangements ("market stall + goods
// in front + barrels at side + lantern") the PLANNER places as single units; this module expands each
// instance into concrete palette-bound objects in the motif's local frame (rotated to its facing) and
// marks them as one rigid group so the gentle solver moves them together. Unbindable slots degrade
// gracefully (skip slot; skip whole motif only if a core slot is unbound).
const fs = require("fs");
const path = require("path");

let _lib = null;
function library() {
  if (!_lib) _lib = JSON.parse(fs.readFileSync(path.join(__dirname, "motifs.json"), "utf8")).motifs;
  return _lib;
}

const DEG = Math.PI / 180;
function _num(v, d) { const n = Number(v); return isFinite(n) ? n : d; }
function _text(a) { return `${a.id || ""} ${a.name || ""} ${(a.tags || []).join(" ")} ${a.subcategory || ""}`.toLowerCase(); }
function _maxDim(a) { const f = a.footprint || a.dims || {}; return Math.max(_num(f.width, 0), _num(f.depth, 0)); }

// Rank palette assets for a slot: category gate, keyword boost, size sanity. Returns sorted candidates.
function _bind(slot, assets) {
  const cats = slot.cats || [], kw = slot.kw || [], avoid = slot.avoid || [];
  const maxm = _num(slot.maxm, 8);
  const out = [];
  for (const a of assets) {
    if (cats.length && !cats.includes(a.category)) continue;
    const dim = _maxDim(a);
    if (!dim || dim > maxm) continue;
    const t = _text(a);
    if (avoid.some(k => t.includes(k))) continue;
    const hits = kw.filter(k => t.includes(k)).length;
    if (kw.length && !hits) continue;
    out.push({ a, score: hits * 10 - Math.abs(dim - Math.min(maxm, 3)) * 0.1 });
  }
  out.sort((x, y) => y.score - x.score);
  return out.map(o => o.a);
}

// The planner-facing library listing (only motifs whose CORE slot can bind against this palette).
function motifPromptBlock(assets) {
  const lib = library();
  const lines = [];
  for (const [name, def] of Object.entries(lib)) {
    const core = (def.slots || []).find(s => s.core);
    if (core && !_bind(core, assets).length) continue;   // don't advertise unbindable motifs
    lines.push(`- ${name}: ${def.desc}`);
  }
  if (!lines.length) return "";
  return [
    "MOTIF LIBRARY (multi-object assemblies you can place as SINGLE units — the expander binds real",
    "palette assets into each and arranges them canonically in the motif's local frame):",
    ...lines,
    "",
  ].join("\n");
}

// Expand graph.motifs -> concrete objects appended to graph.objects. Returns {graph, report}.
function expandMotifs(graph, assets, opts) {
  const o = opts || {};
  const log = o.log || (() => {});
  const lib = library();
  const g = graph || {};
  const instances = Array.isArray(g.motifs) ? g.motifs : [];
  const report = { instances: instances.length, expanded: 0, members: 0, skippedInstances: [], skippedSlots: [] };
  if (!instances.length) return { graph: g, report };

  const anchors = g.anchors || {};
  const objects = (g.objects || []).slice();
  const usedIds = new Set(objects.map(x => x.id));
  const MAX_INST = Number(process.env.IR_MOTIF_MAX_INSTANCES || 28);
  const rot = (dx, dy, th) => ({ dx: dx * Math.cos(th) - dy * Math.sin(th), dy: dx * Math.sin(th) + dy * Math.cos(th) });

  let count = 0;
  for (const inst of instances) {
    if (++count > MAX_INST) { report.skippedInstances.push(`${inst.motif}(cap)`); continue; }
    const def = lib[inst.motif];
    if (!def) { report.skippedInstances.push(`${inst.motif}(unknown)`); continue; }

    // facing rotation: explicit rotation_deg, else derivable when both `at` and `facing` are anchors
    let th = null;
    if (inst.rotation_deg != null) th = _num(inst.rotation_deg, 0) * DEG;
    else if (inst.facing && anchors[inst.at] && anchors[inst.facing]) {
      const A = anchors[inst.at], B = anchors[inst.facing];
      th = Math.atan2(_num(B[1], 0) - _num(A[1], 0), _num(B[0], 0) - _num(A[0], 0));
    }
    if (th == null) th = 0;

    // instance-level offset (added to every member)
    const io = inst.offset || {};
    let ox, oy;
    if (io.dx != null || io.dy != null) { ox = _num(io.dx, 0); oy = _num(io.dy, 0); }
    else { const d = _num(io.distance, 0), an = _num(io.angle, 0) * DEG; ox = d * Math.cos(an); oy = d * Math.sin(an); }

    // bind the core slot first — skip the instance if it can't bind
    const coreSlot = (def.slots || []).find(s => s.core);
    if (coreSlot && !_bind(coreSlot, assets).length) { report.skippedInstances.push(`${inst.motif}(core:${coreSlot.role})`); continue; }

    const gid = "motif_" + (inst.id_prefix || inst.motif + "_" + count);
    let emitted = 0;
    for (const slot of (def.slots || [])) {
      const cands = _bind(slot, assets);
      if (!cands.length) { report.skippedSlots.push(`${gid}:${slot.role}`); continue; }
      (slot.place || []).forEach((pl, i) => {
        const asset = cands[slot.distinct ? i % cands.length : 0];
        const r = rot(_num(pl[0], 0), _num(pl[1], 0), th);
        let id = `${gid}_${slot.role}_${i}`;
        while (usedIds.has(id)) id += "x";
        usedIds.add(id);
        objects.push({
          id, asset_id: asset.id,
          relative_to: String(inst.at || "origin"),
          offset: { dx: ox + r.dx, dy: oy + r.dy },
          rotation_deg: Math.round((_num(pl[2], 0) + th / DEG) * 10) / 10,
          _group: gid, _gkind: "motif",
        });
        emitted++;
      });
    }
    if (emitted) { report.expanded++; report.members += emitted; }
  }

  const out = Object.assign({}, g, { objects, motifs_expanded: instances, motifs: [] });
  log(`ir motifs: ${report.expanded}/${report.instances} instances -> ${report.members} members` +
    (report.skippedInstances.length ? ` | skipped inst: ${report.skippedInstances.join(",")}` : "") +
    (report.skippedSlots.length ? ` | skipped slots: ${report.skippedSlots.length}` : ""));
  return { graph: out, report };
}

module.exports = { library, motifPromptBlock, expandMotifs, _bind };

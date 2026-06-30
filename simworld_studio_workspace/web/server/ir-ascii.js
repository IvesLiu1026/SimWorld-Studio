"use strict";
// ── IR ASCII renderer ────────────────────────────────────────────────────────
// Projects SOLVED objects (from ir-solver) to a labeled top-down grid + legend —
// a VIEW for the LLM's spatial gestalt, NOT a storage format. Pure + deterministic.
// Deliberately drops Z and fine rotation; the JSON graph remains the source of truth.
//
// Orientation: North up. +X = East (→), +Y = North (↑). Each cell ≈ a square patch of
// ground; the title states the metres-per-cell scale. Works on any placed-object list
// (so the same renderer can later draw the live scene for planned-vs-actual diffs).

// Category → symbol preferences (first match wins); unmatched categories draw from a pool.
const PREF = [
  [/tree|veget|plant|foliage|bush|shrub|hedge/i, "T"],
  [/fountain/i, "F"],
  [/build|house|tower|skyscraper|hut|cabin|barn/i, "H"],
  [/bench|seat|chair|stool|sofa|couch/i, "b"],
  [/lamp|light|lantern|torch|streetlight/i, "i"],
  [/vehic|\bcar\b|truck|scooter|cart|bike|bus|van|boat/i, "V"],
  [/person|agent|ped|character|human|npc|crowd/i, "p"],
  [/stall|market|vendor|kiosk|booth|shop/i, "S"],
  [/wall|fence|barrier|gate|railing|hedge/i, "#"],
  [/water|pond|pool|river|lake|sea/i, "~"],
];
const POOL = "ABCDEGJKLMNOQRUWXYZ123456789".split("");

function _symbolFor(category, name, assigned) {
  // Match on the clean DB CATEGORY, not the free-text name (e.g. "Street Lamp" contains
  // the substring "tree" → would wrongly map lighting to the tree symbol).
  const cat = String(category || "").trim();
  const key = cat || String(name || "?");
  if (assigned.has(key)) return assigned.get(key);
  const probe = cat || String(name || "");
  let sym = null;
  for (const [re, s] of PREF) { if (re.test(probe)) { sym = s; break; } }
  if (!sym || [...assigned.values()].includes(sym)) {
    // take a fresh pool char not yet used (so distinct categories stay distinguishable)
    const used = new Set(assigned.values());
    sym = POOL.find(c => !used.has(c)) || "*";
  }
  assigned.set(key, sym);
  return sym;
}

function renderAscii(placed, opts) {
  const o = opts || {};
  const items = (Array.isArray(placed) ? placed : []).filter(Boolean);
  const ground = items.filter(p => p.isGround);
  const objs = items.filter(p => !p.isGround);

  // Auto-fit extent (metres) to the non-ground objects, with a sensible floor and ±130 cap.
  let maxAbs = 0;
  for (const p of objs) maxAbs = Math.max(maxAbs, Math.abs(p.x_m || 0), Math.abs(p.y_m || 0));
  let extent = Math.ceil((maxAbs + 4) / 5) * 5;            // round up to 5 m, +4 m margin
  extent = Math.max(_num(o.minExtentM, 25), Math.min(_num(o.maxExtentM, 130), extent || 25));

  const cols = Math.max(11, _num(o.cols, 49) | 1);          // odd → origin sits on a column
  const rows = Math.max(7, _num(o.rows, 25) | 1);           // odd → origin sits on a row
  const mPerColCell = (2 * extent) / cols;
  const mPerRowCell = (2 * extent) / rows;

  const grid = Array.from({ length: rows }, () => new Array(cols).fill(" "));
  const assigned = new Map();
  const legend = new Map();   // symbol -> { categories:Set, count, samples:Set }
  let offscreen = 0;

  const colOf = x => Math.round((x + extent) / (2 * extent) * (cols - 1));
  const rowOf = y => Math.round((extent - y) / (2 * extent) * (rows - 1));

  for (const p of objs) {
    const c = colOf(p.x_m || 0), r = rowOf(p.y_m || 0);
    if (c < 0 || c >= cols || r < 0 || r >= rows) { offscreen++; continue; }
    const sym = _symbolFor(p.category, p.name, assigned);
    // first-wins per cell, but never overwrite with the same symbol
    if (grid[r][c] === " ") grid[r][c] = sym;
    const e = legend.get(sym) || { categories: new Set(), count: 0, samples: new Set() };
    e.categories.add(p.category || "?"); e.count++;
    if (e.samples.size < 3) e.samples.add(p.name || p.id);
    legend.set(sym, e);
  }

  // mark origin with a '+' if that cell is empty (visual anchor)
  const oc = colOf(0), or = rowOf(0);
  if (or >= 0 && or < rows && oc >= 0 && oc < cols && grid[or][oc] === " ") grid[or][oc] = "+";

  // ── assemble ──
  const lines = [];
  const title = (o.title || (o.sceneName ? `SCENE PLAN — ${o.sceneName}` : "SCENE PLAN (top-down)"));
  lines.push(title);
  lines.push(`extent ±${extent} m  ·  ~${(mPerColCell).toFixed(1)}×${(mPerRowCell).toFixed(1)} m / cell  ·  N ↑  +X→E  +Y→N  ·  origin '+'`);
  lines.push("+" + "-".repeat(cols) + "+   N↑");
  for (let r = 0; r < rows; r++) {
    lines.push("|" + grid[r].join("") + "|" + (r === Math.floor(rows / 2) ? "  ←W · E→" : ""));
  }
  lines.push("+" + "-".repeat(cols) + "+   S↓");

  // legend
  lines.push("");
  lines.push("LEGEND:");
  const entries = [...legend.entries()].sort((a, b) => b[1].count - a[1].count);
  for (const [sym, e] of entries) {
    const cats = [...e.categories].slice(0, 3).join("/");
    const samp = [...e.samples].slice(0, 2).join(", ");
    lines.push(`  ${sym}  ${cats} ×${e.count}${samp ? "  (e.g. " + samp + ")" : ""}`);
  }
  if (ground.length) {
    const gnames = [...new Set(ground.map(p => p.name))].slice(0, 3).join(", ");
    lines.push(`  .  ground/floor (tiled, not shown) ×${ground.length}  (${gnames})`);
  }
  if (offscreen) lines.push(`  (!) ${offscreen} object(s) beyond ±${extent} m not shown`);

  return lines.join("\n");
}

function _num(v, d) { const n = Number(v); return Number.isFinite(n) ? n : d; }

module.exports = { renderAscii };

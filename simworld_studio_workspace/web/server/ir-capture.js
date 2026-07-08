"use strict";
// ── Per-tier capture + annotation (staged builder, Phase 3) ───────────────────
// Between tiers, capture two FIXED cameras framed from the SOLVED PLAN extent (stable across tiers so
// before/after is comparable) and annotate them (marks / ghost footprints / grid / north / banner) for
// the reflect agent. Views: `top` (near-plan, pitch -82) + `aerial` (elevated 3/4, pitch -30 — well-lit
// and shows facades/depth; the plan's eye-level street view is dark + compresses to the horizon).
// Framing is from the plan (not current actors) so the same world→image projection holds every tier.
const fs = require("fs");
const path = require("path");
const { spawnSync } = require("child_process");

const ANNOTATE = path.resolve(__dirname, "../../../tools/annotate_screenshot.py");
const FOV_H = Number(process.env.IR_CAPTURE_FOV_H || 90);

// Frame from plan positions (percentile, like the harness). Returns { cx, cy, extent } in cm.
function frameFromPlan(placed) {
  const objs = (placed || []).filter(p => p && !p.isGround && Array.isArray(p.location));
  if (!objs.length) return { cx: 0, cy: 0, extent: 3000 };
  const xs = objs.map(p => p.location[0]).sort((a, b) => a - b);
  const ys = objs.map(p => p.location[1]).sort((a, b) => a - b);
  const pct = (v, q) => v[Math.min(v.length - 1, Math.max(0, Math.floor(q * (v.length - 1))))];
  const cx = (pct(xs, 0.05) + pct(xs, 0.95)) / 2, cy = (pct(ys, 0.05) + pct(ys, 0.95)) / 2;
  const extent = Math.max((pct(xs, 0.95) - pct(xs, 0.05)) / 2, (pct(ys, 0.95) - pct(ys, 0.05)) / 2, 2000);
  return { cx, cy, extent };
}

// The per-tier camera poses from a framing. Default: top (near-overhead) + aerial (3/4). With
// IR_REFLECT_EYE_VIEWS on (Option B), also add elevated EYE-LEVEL views from opposite edges looking
// across the scene — these expose interpenetration / facing / scale / dead-space that top-down hides,
// which is the visual gap the reflect agent was blind to. Captures are mid-build (pre-atmosphere) so
// they're well-lit, not the dark street view the final comparison-views suffer from.
function tierCameras(framing, eyeOn) {
  const { cx, cy, extent } = framing;
  const eye = (eyeOn != null) ? !!eyeOn : /^(1|true|yes|on)$/i.test(String(process.env.IR_REFLECT_EYE_VIEWS || ""));
  const mk = (name, pitchDeg, bearingDeg, kind) => {
    const b = bearingDeg * Math.PI / 180;
    let horiz, cam_z;
    if (kind === "overhead") { cam_z = Math.max(extent * 1.55, 2500); horiz = Math.max(extent * 0.28, 800); }
    else if (kind === "eye") { horiz = Math.max(1.0 * extent, 2200); cam_z = Math.min(Math.max(extent * 0.07, 300), 550); }
    else { horiz = Math.max(1.15 * extent, 2600); cam_z = Math.max(horiz * Math.tan(Math.abs(pitchDeg) * Math.PI / 180), 0.5 * extent, 900); }
    const cam_x = cx + horiz * Math.cos(b), cam_y = cy + horiz * Math.sin(b);
    const yaw = Math.atan2(cy - cam_y, cx - cam_x) * 180 / Math.PI;
    return { name, location: [cam_x, cam_y, cam_z], rotation: [0, pitchDeg, yaw], fov_h_deg: FOV_H, width: 1280, height: 720 };
  };
  const cams = [mk("top", -82, 45, "overhead"), mk("aerial", -30, 135, "aerial")];
  if (eye) cams.push(mk("eye_e", -9, 15, "eye"), mk("eye_w", -9, 195, "eye"));
  return cams;
}

// A1 (Option A): path-based EYE cameras placed on FREE (walkable) ground next to object clusters, each
// looking at a LOCAL cluster — so every view is a readable slice (not the whole scene). Deterministic from
// footprints. Returns [{name, location, rotation, ..., target, lookRadiusCm}] or null if too sparse (caller
// falls back to the edge eye cams).
function pathCameras(placed, opts) {
  const o = opts || {};
  const nEye = Math.max(1, o.nEye || Number(process.env.IR_REFLECT_N_EYE || 4));
  const objs = (placed || []).filter(p => p && !p.isGround && Array.isArray(p.location));
  if (objs.length < 10) return null;
  const CM = 100, cell = 250;               // 2.5 m cells
  const xs = objs.map(p => p.location[0]), ys = objs.map(p => p.location[1]);
  const minx = Math.min(...xs) - 500, miny = Math.min(...ys) - 500;
  const maxx = Math.max(...xs) + 500, maxy = Math.max(...ys) + 500;
  const nx = Math.max(1, Math.ceil((maxx - minx) / cell)), ny = Math.max(1, Math.ceil((maxy - miny) / cell));
  const ci = x => Math.min(nx - 1, Math.max(0, Math.floor((x - minx) / cell)));
  const cj = y => Math.min(ny - 1, Math.max(0, Math.floor((y - miny) / cell)));
  const occ = Array.from({ length: nx }, () => new Int8Array(ny));
  for (const p of objs) {                    // stamp footprint occupancy
    const w = ((p.measured && p.measured.w_m) || (p.footprint && p.footprint.width) || 1) * CM;
    const d = ((p.measured && p.measured.d_m) || (p.footprint && p.footprint.depth) || 1) * CM;
    const i0 = ci(p.location[0]), j0 = cj(p.location[1]);
    const ri = Math.ceil(w / 2 / cell), rj = Math.ceil(d / 2 / cell);
    for (let i = Math.max(0, i0 - ri); i <= Math.min(nx - 1, i0 + ri); i++)
      for (let j = Math.max(0, j0 - rj); j <= Math.min(ny - 1, j0 + rj); j++) occ[i][j] = 1;
  }
  const cand = [];                           // FREE cells within 2 cells of an object = on a path, near stuff
  for (let i = 0; i < nx; i++) for (let j = 0; j < ny; j++) {
    if (occ[i][j]) continue;
    let near = false;
    for (let a = -2; a <= 2 && !near; a++) for (let b = -2; b <= 2; b++) {
      const ii = i + a, jj = j + b;
      if (ii >= 0 && ii < nx && jj >= 0 && jj < ny && occ[ii][jj]) { near = true; }
    }
    if (near) cand.push([minx + (i + 0.5) * cell, miny + (j + 0.5) * cell]);
  }
  if (cand.length < 2) return null;
  const picks = [cand[0]];                   // greedy farthest-point sampling for coverage
  while (picks.length < Math.min(nEye, cand.length)) {
    let best = null, bd = -1;
    for (const c of cand) {
      let dmin = Infinity; for (const p of picks) { const dd = Math.hypot(c[0] - p[0], c[1] - p[1]); if (dd < dmin) dmin = dd; }
      if (dmin > bd) { bd = dmin; best = c; }
    }
    picks.push(best);
  }
  const LOOK = Number(process.env.IR_REFLECT_LOOK_M || 30) * CM;
  const cams = [];
  picks.forEach((c, idx) => {
    const near = objs.filter(p => Math.hypot(p.location[0] - c[0], p.location[1] - c[1]) < LOOK);
    if (near.length < 2) return;
    const tx = near.reduce((s, p) => s + p.location[0], 0) / near.length;
    const ty = near.reduce((s, p) => s + p.location[1], 0) / near.length;
    const yaw = Math.atan2(ty - c[1], tx - c[0]) * 180 / Math.PI;
    cams.push({ name: `eye_${idx + 1}`, location: [c[0], c[1], 350], rotation: [0, -8, yaw],
      fov_h_deg: FOV_H, width: 1280, height: 720, target: [tx, ty], lookRadiusCm: LOOK });
  });
  return cams.length ? cams : null;
}

// Build the annotate items[] for a set of placed entries. `markOf` maps id→mark number (stable per
// scene). `scheduledTiers` (Set) marks pending tiers as ghost footprints. If `nearTo`/`radiusCm` given
// (A2), keep only items within that radius of the camera target → readable LOCAL marks per view.
function buildItems(placed, markOf, curTier, nearTo, radiusCm) {
  const items = [];
  for (const p of (placed || [])) {
    if (p.isGround) continue;
    if (nearTo && radiusCm && Math.hypot(p.location[0] - nearTo[0], p.location[1] - nearTo[1]) > radiusCm) continue;
    const scheduled = p.tier != null && curTier != null && p.tier > curTier;
    const status = scheduled ? "scheduled" : (p.status || "ok");
    const fw = (p.measured && p.measured.w_m) || (p.footprint && p.footprint.width) || 1;
    const fd = (p.measured && p.measured.d_m) || (p.footprint && p.footprint.depth) || 1;
    items.push({ mark: markOf.get(String(p.id)) || 0, id: p.id, asset: p.name, tier: p.tier, status, x_cm: p.location[0], y_cm: p.location[1], w_m: fw, d_m: fd, scheduled });
  }
  return items;
}

// Capture + annotate BOTH views for tier `curTier`. Returns [{ view, raw, annotated }].
// deps: ueCommand(type,params,timeoutMs), log. Writes into `outDir`.
async function captureTier(opts) {
  const o = opts || {};
  const ueCommand = o.ueCommand;
  const log = o.log || (() => {});
  const outDir = o.outDir;
  fs.mkdirSync(outDir, { recursive: true });
  const framing = o.framing || frameFromPlan(o.placed);
  let cams;
  if (o.rig) {
    const rig = pathCameras(o.placed, { nEye: o.nEye });
    cams = rig ? tierCameras(framing, false).concat(rig) : tierCameras(framing, true); // fallback: edge eye cams
  } else cams = tierCameras(framing, o.eyeOn);
  const allItems = buildItems(o.placed, o.markOf, o.curTier);
  const out = [];
  for (const cam of cams) {
    // Per-camera marks: LOCAL slice for path cams (readable), the full set for global (top/aerial) cams.
    const items = cam.target ? buildItems(o.placed, o.markOf, o.curTier, cam.target, cam.lookRadiusCm) : allItems;
    const shot = `stg_${process.pid}_${cam.name}_${o.curTier}.png`;
    let raw = null;
    try {
      const sr = await ueCommand("take_screenshot", { filename: shot, width: cam.width, height: cam.height, camera_location: cam.location, camera_rotation: cam.rotation }, 60000);
      const src = sr && sr.filepath && path.isAbsolute(sr.filepath) ? sr.filepath : null;
      if (src) {
        for (let i = 0; i < 40 && !fs.existsSync(src); i++) await new Promise(r => setTimeout(r, 100));
        if (fs.existsSync(src)) { raw = path.join(outDir, `raw_${o.curTier}_${cam.name}.png`); await new Promise(r => setTimeout(r, 400)); fs.copyFileSync(src, raw); }
      }
    } catch (e) { log(`capture ${cam.name} t${o.curTier} failed: ${e && e.message}`); }
    if (!raw) { out.push({ view: cam.name, raw: null, annotated: null }); continue; }
    const cfg = { camera: cam, tier: o.curTier, tiers: o.tiers, scene: o.scene, items };
    const cfgPath = path.join(outDir, `cfg_${o.curTier}_${cam.name}.json`);
    fs.writeFileSync(cfgPath, JSON.stringify(cfg));
    const ann = path.join(outDir, `annotated_${o.curTier}_${cam.name}.png`);
    const r = spawnSync("python3", [ANNOTATE, cfgPath, raw, ann], { encoding: "utf8", timeout: 60000 });
    const okAnn = fs.existsSync(ann);
    if (!okAnn) log(`annotate ${cam.name} t${o.curTier} failed: ${(r.stderr || "").slice(0, 160)}`);
    out.push({ view: cam.name, raw, annotated: okAnn ? ann : null });
  }
  log(`capture tier ${o.curTier}: ${out.filter(v => v.annotated).length}/${cams.length} views annotated`);
  return out;
}

module.exports = { frameFromPlan, tierCameras, pathCameras, buildItems, captureTier };

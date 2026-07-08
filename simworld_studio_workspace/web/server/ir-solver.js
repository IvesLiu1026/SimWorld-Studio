"use strict";
// ── IR solver ────────────────────────────────────────────────────────────────
// Lowers a RELATIONAL layout graph (anchors + objects + patterns + constraints)
// into concrete UE-centimeter world transforms, with a PRE-SPAWN non-overlap pass.
// Pure + deterministic: no LLM, no IO, no Math.random, no Date.
//
// Two solver modes (opts.mode | env IR_SOLVER):
//   "legacy"    — original particle relaxation (every object pushed independently).
//                 Resolves overlap but SCATTERS the planner's lines/grids into a blob.
//   "structure" — structure-preserving: each line/grid/ring pattern is kept rigid
//                 (normalized internally so its own members don't overlap, then the
//                 WHOLE group is translated to separate from other groups). Only loose
//                 standalone clutter gets free 2-D jitter. Structural yaws snap to a grid.
//
// Coordinate conventions:
//   - Graph authored in METRES; output transforms in CENTIMETRES (UE: 1m=100u).
//   - angle / along_angle: degrees, 0° = +X, 90° = +Y, CCW positive (standard math).
//   - Positions are RELATIVE to a reference (anchor or another object); solved outward.
//   - on_ground assumes base_center pivot → object base sits at z=0.
//
// Returns { placed: [...], report: {...} }.

const GROUND_HALF_M = Number(process.env.IR_GROUND_HALF_M || 130); // matches mcp-server GROUND_HALF=13000cm
const M2CM = 100;
const DEG = Math.PI / 180;
const DEFAULT_ANCHOR = "origin";
const CLUTTER_R = Math.max(0, Number(process.env.IR_CLUTTER_RADIUS_M || 0.8));

function _num(v, d) { const n = Number(v); return Number.isFinite(n) ? n : d; }
function _round(v) { return Math.round(v * 100) / 100; }

// Resolve the collision geometry + metadata for an asset id from the retrieval index.
function _assetGeom(assetIndex, id) {
  let a = null;
  if (assetIndex) {
    if (typeof assetIndex.get === "function") a = assetIndex.get(id);
    else a = assetIndex[id];
  }
  const fp = a && a.footprint ? a.footprint : (a && a.dims ? { width: a.dims.width, depth: a.dims.depth } : null);
  const w = fp ? _num(fp.width, 1) : 1;
  const d = fp ? _num(fp.depth, 1) : 1;
  let r = (a && typeof a.boundingRadius === "number" && a.boundingRadius > 0)
    ? a.boundingRadius
    : Math.sqrt((w / 2) * (w / 2) + (d / 2) * (d / 2));
  if (!(r > 0)) r = 0.5;
  return {
    found: !!a,
    footprint: { width: w, depth: d },
    radius: r,
    height: a && a.dims ? _num(a.dims.height, 0) : 0,
    category: (a && a.category) || "",
    name: (a && a.name) || id,
    path: (a && a.path) || "",
    spawnTool: (a && a.spawnTool) || "",
  };
}

// Ground/road/floor assets are meant to tile and overlap — exclude them from the
// non-overlap pass and from collision radius accounting.
function _isGround(geom) { return /ground|road|floor|pavement|paving|surface|grass|terrain/i.test(geom.category || ""); }

// Organic assets (trees/plants/rocks) look unnatural in perfect rings/grids — they get deterministic
// jitter on emit so they read as scattered. Built structures (walls/buildings/fences) stay rigid.
function _isOrganic(cat) { return /veget|foliage|\btree|plant|bush|shrub|hedge|flower|\brock|nature_terrain/i.test(cat || ""); }
// Deterministic [0,1) hash (FNV-1a) so jitter is reproducible — no Math.random (would break resume).
function _hashUnit(id, salt) {
  let h = 2166136261 >>> 0; const s = String(id) + ":" + String(salt);
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619) >>> 0; }
  return (h >>> 0) / 4294967296;
}

// Frozen-obstacle map for the staged builder's per-tier re-solve (opts.frozen): id -> {x, y, r} (metres)
// from the MEASURED footprints of already-spawned actors. Radius = footprint half-diagonal.
function _frozenMap(frozen) {
  const m = new Map();
  const list = Array.isArray(frozen) ? frozen : (frozen && typeof frozen.values === "function" ? [...frozen.values()] : []);
  for (const f of list) {
    if (!f || f.id == null) continue;
    const w = _num(f.w_m, 1), d = _num(f.d_m, 1);
    let r = Math.sqrt((w / 2) * (w / 2) + (d / 2) * (d / 2));
    if (!(r > 0)) r = 0.5;
    m.set(String(f.id), { x: _num(f.x_m, 0), y: _num(f.y_m, 0), r });
  }
  return m;
}

// Expand a pattern (line | ring | grid) into concrete object entries that each carry a
// {dx,dy} offset relative to the pattern's reference, so they flow through normal resolution.
// Members are tagged (_group/_gkind/…) so the structure-preserving solver can keep them rigid.
function _expandPattern(p) {
  const out = [];
  const prefix = String(p.id_prefix || p.id || "obj");
  const assetId = p.asset_id || p.asset || "";
  const relTo = p.relative_to || p.relativeTo || DEFAULT_ANCHOR;
  const facing = p.facing || null;
  const scale = _num(p.scale, 1);
  const kind = String(p.kind || "line").toLowerCase();

  // shared perpendicular/base offset applied to every instance (e.g. trees onto the sidewalk)
  const off = p.offset || {};
  let baseX = 0, baseY = 0;
  if (off.dx != null || off.dy != null) { baseX = _num(off.dx, 0); baseY = _num(off.dy, 0); }
  else if (off.distance != null || off.angle != null) {
    const dist = _num(off.distance, 0), ang = _num(off.angle, 0) * DEG;
    baseX = dist * Math.cos(ang); baseY = dist * Math.sin(ang);
  }

  if (kind === "ring") {
    const count = Math.max(1, Math.floor(_num(p.count, 1)));
    const radius = _num(p.radius, 1);
    const start = _num(p.start_angle, 0);
    for (let i = 0; i < count; i++) {
      const th = (start + 360 * i / count) * DEG;
      out.push({ id: `${prefix}_${i + 1}`, asset_id: assetId, relative_to: relTo, facing, scale,
        _group: prefix, _gkind: "ring", _ringI: i, _ringCount: count, _ringStart: start, _ringRadius: radius,
        offset: { dx: baseX + radius * Math.cos(th), dy: baseY + radius * Math.sin(th) } });
    }
  } else if (kind === "grid") {
    const rows = Math.max(1, Math.floor(_num(p.rows, 1)));
    const cols = Math.max(1, Math.floor(_num(p.cols, 1)));
    const spacing = _num(p.spacing, 5);
    let k = 0;
    for (let r = 0; r < rows; r++) for (let c = 0; c < cols; c++) {
      out.push({ id: `${prefix}_${++k}`, asset_id: assetId, relative_to: relTo, facing, scale,
        _group: prefix, _gkind: "grid", _gr: r, _gc: c, _grows: rows, _gcols: cols, _gspacing: spacing,
        offset: { dx: baseX + (c - (cols - 1) / 2) * spacing, dy: baseY + (r - (rows - 1) / 2) * spacing } });
    }
  } else { // line (default): instances strung along `along_angle`, centered, plus the base offset
    const count = Math.max(1, Math.floor(_num(p.count, 1)));
    const spacing = _num(p.spacing, 5);
    const alongDeg = _num(p.along_angle, 0);
    const ang = alongDeg * DEG;
    const ux = Math.cos(ang), uy = Math.sin(ang);
    for (let i = 0; i < count; i++) {
      const t = (i - (count - 1) / 2) * spacing;
      out.push({ id: `${prefix}_${i + 1}`, asset_id: assetId, relative_to: relTo, facing, scale,
        _group: prefix, _gkind: "line", _lineI: i, _lineCount: count, _axisDeg: alongDeg,
        offset: { dx: baseX + ux * t, dy: baseY + uy * t } });
    }
  }
  return out;
}

// Phases 1–3 (shared): anchors → objects(+patterns) → resolved positions (metres).
function _layout(graph, assetIndex, opts) {
  const o = opts || {};
  const g = graph || {};
  const halfM = _num(o.groundHalfM, GROUND_HALF_M);
  const report = { objects: 0, fromPatterns: 0, missingAssets: [], unresolvedRefs: [], overlapsResolved: 0, overlapsRemaining: 0, clamped: 0 };

  const positions = new Map();
  const anchors = g.anchors || {};
  for (const name of Object.keys(anchors)) {
    const a = anchors[name] || [];
    positions.set(name, { x: _num(a[0], 0), y: _num(a[1], 0), z: _num(a[2], 0) });
  }
  if (!positions.has(DEFAULT_ANCHOR)) positions.set(DEFAULT_ANCHOR, { x: 0, y: 0, z: 0 });

  const objects = [];
  for (const obj of (Array.isArray(g.objects) ? g.objects : [])) {
    if (obj && obj.id) objects.push(obj);
  }
  for (const p of (Array.isArray(g.patterns) ? g.patterns : [])) {
    const exp = _expandPattern(p);
    report.fromPatterns += exp.length;
    for (const e of exp) objects.push(e);
  }
  report.objects = objects.length;

  const byId = new Map(objects.map(ob => [ob.id, ob]));
  const placedMeta = new Map();
  let pending = objects.slice();
  let guard = pending.length + 5;
  const computePos = (obj) => {
    const refName = obj.relative_to || obj.relativeTo || DEFAULT_ANCHOR;
    const ref = positions.get(refName) || positions.get(DEFAULT_ANCHOR);
    const off = obj.offset || {};
    let dx, dy;
    if (off.dx != null || off.dy != null) { dx = _num(off.dx, 0); dy = _num(off.dy, 0); }
    else { const dist = _num(off.distance, 0), ang = _num(off.angle, 0) * DEG; dx = dist * Math.cos(ang); dy = dist * Math.sin(ang); }
    return { x: ref.x + dx, y: ref.y + dy, z: 0 };
  };
  const mkMeta = (obj, pos) => {
    const geom = _assetGeom(assetIndex, obj.asset_id || obj.asset);
    if (!geom.found) report.missingAssets.push(obj.asset_id || obj.asset || obj.id);
    return { obj, geom, x: pos.x, y: pos.y, z: pos.z,
      _group: obj._group || null, _gkind: obj._gkind || null,
      _axisDeg: obj._axisDeg, _lineI: obj._lineI, _lineCount: obj._lineCount,
      _gr: obj._gr, _gc: obj._gc, _grows: obj._grows, _gcols: obj._gcols, _gspacing: obj._gspacing,
      _ringI: obj._ringI, _ringCount: obj._ringCount, _ringStart: obj._ringStart, _ringRadius: obj._ringRadius };
  };
  while (pending.length && guard-- > 0) {
    const next = [];
    for (const obj of pending) {
      const refName = obj.relative_to || obj.relativeTo || DEFAULT_ANCHOR;
      const refReady = positions.has(refName) || refName === DEFAULT_ANCHOR;
      if (!refReady && byId.has(refName) && !positions.has(refName)) { next.push(obj); continue; }
      const pos = computePos(obj);
      positions.set(obj.id, pos);
      placedMeta.set(obj.id, mkMeta(obj, pos));
    }
    if (next.length === pending.length) {
      for (const obj of next) {
        report.unresolvedRefs.push(obj.id);
        const pos = computePos(obj);
        positions.set(obj.id, pos);
        placedMeta.set(obj.id, mkMeta(obj, pos));
      }
      pending = [];
      break;
    }
    pending = next;
  }

  const placedList = objects.map(ob => placedMeta.get(ob.id)).filter(Boolean);
  const constraints = (Array.isArray(g.constraints) ? g.constraints : []).map(c => String(c).toLowerCase());
  return { placedList, positions, report, constraints, halfM };
}

function _clamp(placedList, halfM, report) {
  for (const p of placedList) {
    const mx = halfM - p.geom.radius;
    let cx = p.x, cy = p.y;
    if (cx > mx) cx = mx; else if (cx < -mx) cx = -mx;
    if (cy > mx) cy = mx; else if (cy < -mx) cy = -mx;
    if (cx !== p.x || cy !== p.y) report.clamped++;
    p.x = cx; p.y = cy;
  }
}

// Phase 6 (shared): facing/rotation → cm transforms. yawSnapDeg>0 snaps STRUCTURAL
// objects (non-ground, and either in a pattern or radius≥clutter) to that grid.
function _emit(placedList, positions, emitOpts) {
  const eo = emitOpts || {};
  const yawSnap = _num(eo.yawSnapDeg, 0);
  const JIT = _num(eo.organicJitterM, 0);   // organic position jitter radius (m); 0 = off
  const snapStructural = (p) => yawSnap > 0 && !_isGround(p.geom) && (!!p._group || p.geom.radius >= CLUTTER_R);
  return placedList.map(p => {
    const obj = p.obj;
    let px = p.x, py = p.y;
    // Organic objects (trees/plants/rocks): scatter off the perfect ring/grid + random spin so nature
    // looks natural, not gridded. Deterministic per object id. Built structures are untouched.
    const organic = JIT > 0 && !_isGround(p.geom) && _isOrganic(p.geom.category);
    if (organic) { px += (_hashUnit(obj.id, "jx") * 2 - 1) * JIT; py += (_hashUnit(obj.id, "jy") * 2 - 1) * JIT; }
    let yaw = _num(obj.rotation_deg, _num(obj.yaw, 0));
    const faceName = obj.facing || obj.face;
    if (faceName) {
      const tp = positions.get(faceName);
      if (tp) yaw = Math.atan2(tp.y - py, tp.x - px) / DEG;
    }
    if (organic) yaw = _hashUnit(obj.id, "jr") * 360;          // natural random spin
    else if (snapStructural(p)) yaw = Math.round(yaw / yawSnap) * yawSnap;
    return {
      id: obj.id,
      asset_id: obj.asset_id || obj.asset || "",
      name: p.geom.name,
      category: p.geom.category,
      path: p.geom.path,
      spawnTool: p.geom.spawnTool,
      assetFound: p.geom.found,
      footprint: p.geom.footprint,
      radius: _round(p.geom.radius),
      isGround: _isGround(p.geom),
      x_m: _round(px), y_m: _round(py),
      location: [Math.round(px * M2CM), Math.round(py * M2CM), Math.round((p.z || 0) * M2CM)],
      yaw_deg: _round(((yaw % 360) + 360) % 360 > 180 ? (((yaw % 360) + 360) % 360) - 360 : ((yaw % 360) + 360) % 360),
      scale: _num(obj.scale, 1),
      group: p._group || null,
    };
  });
}

// ── LEGACY solver (unchanged behaviour: particle relaxation) ──────────────────
function solveLegacy(graph, assetIndex, opts) {
  const o = opts || {};
  const { placedList, positions, report, constraints, halfM } = _layout(graph, assetIndex, o);
  report.solver = "legacy";

  const wantNoOverlap = o.noOverlap !== false && !constraints.includes("allow_overlap");
  if (wantNoOverlap) {
    const movable = placedList.filter(p => !_isGround(p.geom));
    const PAD = 0.05;
    const ITERS = Math.max(1, Number(process.env.IR_OVERLAP_ITERS || 400));
    const big = (p) => p.geom.radius >= CLUTTER_R;
    for (let it = 0; it < ITERS; it++) {
      let moved = 0;
      for (let i = 0; i < movable.length; i++) {
        for (let j = i + 1; j < movable.length; j++) {
          const A = movable[i], B = movable[j];
          if (!big(A) && !big(B)) continue;
          let dxv = B.x - A.x, dyv = B.y - A.y;
          let dist = Math.sqrt(dxv * dxv + dyv * dyv);
          const minD = A.geom.radius + B.geom.radius + PAD;
          if (dist >= minD) continue;
          if (dist < 1e-6) { dxv = (i % 2 === 0 ? 1 : -1); dyv = (j % 2 === 0 ? 1 : -1); dist = Math.SQRT2; }
          const pen = minD - dist, nx = dxv / dist, ny = dyv / dist;
          const sum = A.geom.radius + B.geom.radius || 1, wA = B.geom.radius / sum, wB = A.geom.radius / sum;
          A.x -= nx * pen * wA; A.y -= ny * pen * wA;
          B.x += nx * pen * wB; B.y += ny * pen * wB;
          moved++;
        }
      }
      report.overlapsResolved += moved;
      if (!moved) break;
    }
    let remain = 0;
    for (let i = 0; i < movable.length; i++) for (let j = i + 1; j < movable.length; j++) {
      const A = movable[i], B = movable[j];
      if (!big(A) && !big(B)) continue;
      if (Math.hypot(B.x - A.x, B.y - A.y) < A.geom.radius + B.geom.radius) remain++;
    }
    report.overlapsRemaining = remain;
  }

  _clamp(placedList, halfM, report);
  return { placed: _emit(placedList, positions, {}), report };
}

// ── STRUCTURE-PRESERVING solver ──────────────────────────────────────────────
// 1) Normalize each pattern internally so its own members never overlap, keeping its
//    SHAPE (line stays a line, grid a lattice, ring a circle).
// 2) Separate whole groups rigidly (a group = one pattern, or one standalone object).
// 3) Free-jitter only loose standalone clutter against the structure.
// 4) Snap structural yaws to a grid (default 90°).
function solveStructured(graph, assetIndex, opts) {
  const o = opts || {};
  const { placedList, positions, report, constraints, halfM } = _layout(graph, assetIndex, o);
  report.solver = "structure";

  const wantNoOverlap = o.noOverlap !== false && !constraints.includes("allow_overlap");
  const PAD = Math.max(0, _num(o.structPadM, Number(process.env.IR_STRUCT_PAD_M || 0.3)));
  const movable = placedList.filter(p => !_isGround(p.geom));

  // Staged builder: opts.frozen = MEASURED footprints of already-spawned actors. Pin them immovable so
  // pending groups/clutter de-overlap AGAINST them (they never move). No-op when absent → unchanged.
  const FROZEN = (o.frozen && (Array.isArray(o.frozen) ? o.frozen.length : (o.frozen.size || 0))) ? _frozenMap(o.frozen) : null;
  if (FROZEN) {
    for (const p of placedList) {
      const f = FROZEN.get(String(p.obj.id));
      if (f) { p.x = f.x; p.y = f.y; p.geom = Object.assign({}, p.geom, { radius: f.r }); p._frozen = true; }
    }
  }
  const isFrozen = (p) => !!(p && p._frozen);

  // Partition: pattern members → rigid groups; standalone big objects → singleton groups;
  // standalone small props → free clutter.
  const isClutter = (p) => !p._group && p.geom.radius < CLUTTER_R;
  const groups = new Map();
  const clutter = [];
  for (const p of movable) {
    if (isClutter(p)) { clutter.push(p); continue; }
    const gid = p._group || ("__obj_" + p.obj.id);
    if (!groups.has(gid)) groups.set(gid, { id: gid, members: [], mass: 0, kind: p._gkind || "object" });
    const G = groups.get(gid); G.members.push(p); G.mass += p.geom.radius * p.geom.radius;
  }
  const groupList = [...groups.values()];
  const centroid = (G) => { let sx = 0, sy = 0; for (const m of G.members) { sx += m.x; sy += m.y; } return { x: sx / G.members.length, y: sy / G.members.length }; };
  const maxDiam = (G) => { let r = 0; for (const m of G.members) if (m.geom.radius > r) r = m.geom.radius; return 2 * r; };
  // Frozen groups (staged): all-members-pinned → obstacles that never move. All false when no frozen.
  for (const G of groupList) G.frozen = G.members.length > 0 && G.members.every(isFrozen);

  if (wantNoOverlap) {
    // 1) Intra-group normalize — re-derive member coords on the pattern shape with spacing
    //    wide enough that members don't overlap, centred on the group's current centroid.
    for (const G of groupList) {
      if (G.members.length < 2) continue;
      const c = centroid(G);
      const need = maxDiam(G) + PAD;
      if (G.kind === "line") {
        const aDeg = _num(G.members[0]._axisDeg, 0), a = aDeg * DEG;
        const ux = Math.cos(a), uy = Math.sin(a);
        const sp = Math.max(need, _num(G.members[0]._gspacing, need));
        const ms = G.members.slice().sort((p, q) => p._lineI - q._lineI);
        const n = ms.length;
        ms.forEach((m, i) => { const t = (i - (n - 1) / 2) * sp; m.x = c.x + ux * t; m.y = c.y + uy * t; });
      } else if (G.kind === "grid") {
        const rows = _num(G.members[0]._grows, 1), cols = _num(G.members[0]._gcols, 1);
        const sp = Math.max(need, _num(G.members[0]._gspacing, need));
        for (const m of G.members) { m.x = c.x + (_num(m._gc, 0) - (cols - 1) / 2) * sp; m.y = c.y + (_num(m._gr, 0) - (rows - 1) / 2) * sp; }
      } else if (G.kind === "ring") {
        const n = _num(G.members[0]._ringCount, G.members.length);
        const start = _num(G.members[0]._ringStart, 0);
        const minR = (n * need) / (2 * Math.PI);
        const rad = Math.max(minR, _num(G.members[0]._ringRadius, minR));
        for (const m of G.members) { const th = (start + 360 * _num(m._ringI, 0) / n) * DEG; m.x = c.x + rad * Math.cos(th); m.y = c.y + rad * Math.sin(th); }
      }
    }

    // 2) Inter-group separation with ANCHOR-PULL — de-overlap groups, then tug each one back
    //    toward its PLANNED position so the scene stays COMPACT (no spreading into the void).
    //    IR_ANCHOR_PULL (0..0.9): fraction pulled home each sweep; higher = denser/tighter.
    for (const G of groupList) G.home = centroid(G);
    const ITERS = Math.max(1, _num(o.groupIters, Number(process.env.IR_GROUP_ITERS || 250)));
    const PULL = Math.max(0, Math.min(0.9, _num(o.anchorPull, Number(process.env.IR_ANCHOR_PULL || 0.25))));
    const DAMP = Math.max(0.05, Math.min(1, _num(o.pushDamp, Number(process.env.IR_PUSH_DAMP || 1))));
    const TGT = Math.max(0, _num(o.targetExtentM, Number(process.env.IR_TARGET_EXTENT_M || 0))); // 0=off; else floor ±TGT m
    // Size-aware: grow the clamp box to hold the scene's total object footprint area, so dense /
    // big-structure scenes (harbor) aren't crushed into overlap while small scenes (market) stay tight.
    let TEFF = TGT;
    if (TGT > 0) {
      const PACK = Math.max(0, _num(o.targetPack, Number(process.env.IR_TARGET_PACK || 0))); // 0=fixed TGT; else area-scaled
      if (PACK > 0) {
        let area = 0;
        for (const p of movable) area += Math.PI * p.geom.radius * p.geom.radius;
        TEFF = Math.max(TGT, PACK * Math.sqrt(area));
      }
    }
    for (let it = 0; it < ITERS; it++) {
      let moved = 0;
      for (const G of groupList) G.over = false;
      for (let i = 0; i < groupList.length; i++) {
        for (let j = i + 1; j < groupList.length; j++) {
          const G = groupList[i], H = groupList[j];
          if (G.frozen && H.frozen) continue;
          let pen = 0;
          for (const a of G.members) for (const b of H.members) {
            const d = Math.hypot(b.x - a.x, b.y - a.y);
            const p = a.geom.radius + b.geom.radius + PAD - d;
            if (p > pen) pen = p;
          }
          if (pen <= 0) continue;
          const cG = centroid(G), cH = centroid(H);
          let nx = cH.x - cG.x, ny = cH.y - cG.y, nl = Math.hypot(nx, ny);
          if (nl < 1e-6) { nx = (i % 2 ? 1 : -1); ny = (j % 2 ? 1 : -1); nl = Math.hypot(nx, ny); }
          nx /= nl; ny /= nl;
          if (G.frozen) { for (const m of H.members) { m.x += nx * pen * DAMP; m.y += ny * pen * DAMP; } H.over = true; }        // G fixed → H clears fully
          else if (H.frozen) { for (const m of G.members) { m.x -= nx * pen * DAMP; m.y -= ny * pen * DAMP; } G.over = true; }   // H fixed → G clears fully
          else {
            const ws = G.mass + H.mass || 1, wG = H.mass / ws, wH = G.mass / ws;
            for (const m of G.members) { m.x -= nx * pen * wG * DAMP; m.y -= ny * pen * wG * DAMP; }
            for (const m of H.members) { m.x += nx * pen * wH * DAMP; m.y += ny * pen * wH * DAMP; }
            G.over = true; H.over = true;
          }
          moved++;
        }
      }
      report.overlapsResolved += moved;
      // ANCHOR-PULL: compact ONLY groups that are already clear this sweep — tug them toward their
      // planned home. Overlapping groups get separation only, so de-overlap always wins; clear ones
      // nestle home until they just touch a neighbour → compact AND collision-free, not fanned out.
      if (PULL > 0) {
        for (const G of groupList) {
          if (G.over || G.frozen) continue;
          const c = centroid(G), dx = (G.home.x - c.x) * PULL, dy = (G.home.y - c.y) * PULL;
          if (dx || dy) for (const m of G.members) { m.x += dx; m.y += dy; }
        }
      }
      // TARGET-EXTENT clamp (rigid): keep each whole group inside ±TGT m so the scene can't fan out
      // past the ground/build area. The group translates as one unit, so lines/grids keep their shape.
      if (TGT > 0) {
        for (const G of groupList) {
          if (G.frozen) continue;
          const c = centroid(G);
          let hx = 0, hy = 0;
          for (const m of G.members) { const ax = Math.abs(m.x - c.x), ay = Math.abs(m.y - c.y); if (ax > hx) hx = ax; if (ay > hy) hy = ay; }
          const lx = Math.max(0, TEFF - hx), ly = Math.max(0, TEFF - hy);
          let dx = 0, dy = 0;
          if (c.x > lx) dx = lx - c.x; else if (c.x < -lx) dx = -lx - c.x;
          if (c.y > ly) dy = ly - c.y; else if (c.y < -ly) dy = -ly - c.y;
          if (dx || dy) for (const m of G.members) { m.x += dx; m.y += dy; }
        }
      }
      if (!moved && PULL === 0) break;
    }

    // 3) Clutter pass — loose props move (fully) out of structural members + each other.
    const structMembers = movable.filter(p => !isClutter(p));
    const CITERS = Math.max(1, Number(process.env.IR_CLUTTER_ITERS || 80));
    const bigClut = (p) => p.geom.radius >= CLUTTER_R;
    for (let it = 0; it < CITERS; it++) {
      let moved = 0;
      for (const c of clutter) {
        const cFrozen = isFrozen(c);
        for (const s of structMembers) {
          const d = Math.hypot(s.x - c.x, s.y - c.y), minD = c.geom.radius + s.geom.radius + PAD;
          if (d >= minD) continue;
          if (cFrozen) continue;
          let nx = c.x - s.x, ny = c.y - s.y, nl = Math.hypot(nx, ny);
          if (nl < 1e-6) { nx = 1; ny = 0; nl = 1; }
          nx /= nl; ny /= nl; const pen = minD - d;
          c.x += nx * pen; c.y += ny * pen; moved++;
        }
        for (const c2 of clutter) {
          if (c2 === c || (!bigClut(c) && !bigClut(c2))) continue;
          const d = Math.hypot(c2.x - c.x, c2.y - c.y), minD = c.geom.radius + c2.geom.radius + PAD;
          if (d >= minD || d < 1e-6) continue;
          const nx = (c.x - c2.x) / d, ny = (c.y - c2.y) / d, pen = (minD - d) / 2;
          if (!cFrozen) { c.x += nx * pen; c.y += ny * pen; }
          if (!isFrozen(c2)) { c2.x -= nx * pen; c2.y -= ny * pen; }
          moved++;
        }
      }
      if (!moved) break;
    }

    // Escalation (staged): pending groups still penetrating a frozen obstacle after full iterations.
    if (FROZEN) {
      const blockPad = Math.max(0, Number(process.env.IR_SOLVER_BLOCKED_PAD_M || 0.1));
      const blocked = [];
      for (const G of groupList) {
        if (G.frozen) continue;
        let pen = 0;
        for (const m of G.members) { if (isFrozen(m)) continue; for (const f of FROZEN.values()) { const p = m.geom.radius + f.r - Math.hypot(m.x - f.x, m.y - f.y); if (p > pen) pen = p; } }
        if (pen > blockPad) { const g0 = G.members[0]; blocked.push({ id: (g0 && (g0._group || g0.obj.id)) || null, penetration: _round(pen) }); }
      }
      report.solverBlocked = blocked;
    }

    // Residual STRUCTURAL overlaps (for the repair decision): pairs among non-clutter objects.
    const sm = structMembers;
    let remain = 0; const pairs = [];
    for (let i = 0; i < sm.length; i++) for (let j = i + 1; j < sm.length; j++) {
      if (Math.hypot(sm[j].x - sm[i].x, sm[j].y - sm[i].y) < sm[i].geom.radius + sm[j].geom.radius) {
        remain++;
        if (pairs.length < 40) pairs.push([sm[i]._group || sm[i].obj.id, sm[j]._group || sm[j].obj.id]);
      }
    }
    report.overlapsRemaining = remain;
    report.residualPairs = pairs;
  }

  _clamp(placedList, halfM, report);
  const yawSnapDeg = _num(o.yawSnapDeg, Number(process.env.IR_YAW_SNAP_DEG || 90));
  const organicJitterM = _num(o.organicJitterM, Number(process.env.IR_ORGANIC_JITTER_M || 1.5));
  return { placed: _emit(placedList, positions, { yawSnapDeg, organicJitterM }), report };
}

// ── GENTLE solver ─────────────────────────────────────────────────────────────
// Intent-preserving: trust the planner's coordinates and fix only genuine LOCAL
// overlaps. Structural objects move as RIGID groups, capped near their planned spot
// (IR_GENTLE_GROUP_BUDGET_M, default 3 m); only loose clutter floats freely
// (IR_GENTLE_CLUTTER_BUDGET_M, default 12 m). Residual overlaps within the cap are
// ACCEPTED — the builder/physics settle minor contacts. Unlike solveStructured, there is
// NO global group-separation / anchor-pull / target clamp, so the planner's composition
// (perimeter at the edges, focal object centered, zones in place) is preserved.
function solveGentle(graph, assetIndex, opts) {
  const o = opts || {};
  const { placedList, positions, report, constraints, halfM } = _layout(graph, assetIndex, o);
  report.solver = "gentle";

  // Staged builder (Phase 2): opts.frozen = MEASURED footprints of already-spawned actors. Pin those
  // placed entries to their measured position + radius and mark them immovable — pending objects
  // de-overlap AGAINST them but they never move. When absent, every guard below is a no-op → the
  // baseline gentle solve is byte-for-byte unchanged.
  const FROZEN = (o.frozen && (Array.isArray(o.frozen) ? o.frozen.length : (o.frozen.size || 0))) ? _frozenMap(o.frozen) : null;
  if (FROZEN) {
    for (const p of placedList) {
      const f = FROZEN.get(String(p.obj.id));
      if (f) { p.x = f.x; p.y = f.y; p.geom = Object.assign({}, p.geom, { radius: f.r }); p._frozen = true; }
    }
  }
  const isFrozen = (p) => !!(p && p._frozen);

  const wantNoOverlap = o.noOverlap !== false && !constraints.includes("allow_overlap");
  if (wantNoOverlap) {
    const PAD = Math.max(0, Number(process.env.IR_STRUCT_PAD_M || 0.3));
    const GROUP_BUDGET = Math.max(0, _num(o.groupBudgetM, Number(process.env.IR_GENTLE_GROUP_BUDGET_M || 3)));
    const CLUT_BUDGET = Math.max(0, _num(o.clutterBudgetM, Number(process.env.IR_GENTLE_CLUTTER_BUDGET_M || 12)));
    const ITERS = Math.max(1, Number(process.env.IR_GENTLE_ITERS || 120));
    const movable = placedList.filter(p => !_isGround(p.geom));
    const isClutter = (p) => !p._group && p.geom.radius < CLUTTER_R;
    const clutter = movable.filter(isClutter);
    const structural = movable.filter(p => !isClutter(p));

    // structural groups: pattern members share _group; standalone bigs are singletons
    const gmap = new Map();
    for (const p of structural) { const g = p._group || ("__" + p.obj.id); if (!gmap.has(g)) gmap.set(g, []); gmap.get(g).push(p); }
    const groups = [...gmap.values()];
    const cent = (m) => { let x = 0, y = 0; for (const p of m) { x += p.x; y += p.y; } return { x: x / m.length, y: y / m.length }; };
    const ghome = groups.map(cent);
    const chome = new Map(clutter.map(p => [p.obj.id, { x: p.x, y: p.y }]));
    // A group is frozen iff all its members are pinned (homogeneous per tier). Frozen groups/clutter
    // exert push but never move; when no frozen obstacles exist these are all false → unchanged.
    const gfrozen = groups.map(g => g.length > 0 && g.every(isFrozen));

    for (let it = 0; it < ITERS; it++) {
      let moved = 0;
      // rigid group vs group: small damped nudges (no big shoves). Against a FROZEN group, only the
      // pending group moves — by the full penetration — since the obstacle won't yield.
      for (let i = 0; i < groups.length; i++) for (let j = i + 1; j < groups.length; j++) {
        if (gfrozen[i] && gfrozen[j]) continue;
        const A = groups[i], B = groups[j]; let pen = 0;
        for (const a of A) for (const b of B) { const p = a.geom.radius + b.geom.radius + PAD - Math.hypot(b.x - a.x, b.y - a.y); if (p > pen) pen = p; }
        if (pen <= 0) continue;
        const cA = cent(A), cB = cent(B); let nx = cB.x - cA.x, ny = cB.y - cA.y, nl = Math.hypot(nx, ny);
        if (nl < 1e-6) { nx = (i % 2 ? 1 : -1); ny = (j % 2 ? 1 : -1); nl = Math.hypot(nx, ny); }
        nx /= nl; ny /= nl;
        if (gfrozen[i]) { for (const m of B) { m.x += nx * pen; m.y += ny * pen; } }        // A fixed → B clears fully
        else if (gfrozen[j]) { for (const m of A) { m.x -= nx * pen; m.y -= ny * pen; } }    // B fixed → A clears fully
        else { const step = Math.min(pen, 1.0) * 0.25; for (const m of A) { m.x -= nx * step; m.y -= ny * step; } for (const m of B) { m.x += nx * step; m.y += ny * step; } }
        moved++;
      }
      // HARD displacement cap: pull each (non-frozen) group back to within GROUP_BUDGET of its home.
      groups.forEach((m, k) => { if (gfrozen[k]) return; const c = cent(m), dx = c.x - ghome[k].x, dy = c.y - ghome[k].y, dist = Math.hypot(dx, dy); if (dist > GROUP_BUDGET) { const b = (dist - GROUP_BUDGET) / dist; for (const p of m) { p.x -= dx * b; p.y -= dy * b; } } });
      // clutter: float fully out of structure + each other, capped to CLUT_BUDGET. Frozen clutter is an
      // obstacle (never moved); a frozen partner in a pair is not displaced.
      for (const c of clutter) {
        const cFrozen = isFrozen(c);
        for (const s of structural) { const dx = s.x - c.x, dy = s.y - c.y, dd = Math.hypot(dx, dy), minD = c.geom.radius + s.geom.radius + PAD; if (dd >= minD) continue; if (cFrozen) continue; let nx, ny; if (dd < 1e-6) { nx = 1; ny = 0; } else { nx = -dx / dd; ny = -dy / dd; } c.x += nx * (minD - dd); c.y += ny * (minD - dd); moved++; }
        for (const c2 of clutter) { if (c2 === c) continue; const dx = c2.x - c.x, dy = c2.y - c.y, dd = Math.hypot(dx, dy), minD = c.geom.radius + c2.geom.radius + PAD; if (dd >= minD || dd < 1e-6) continue; const nx = dx / dd, ny = dy / dd, pen = (minD - dd) / 2; if (!cFrozen) { c.x -= nx * pen; c.y -= ny * pen; } if (!isFrozen(c2)) { c2.x += nx * pen; c2.y += ny * pen; } moved++; }
        if (!cFrozen) { const h = chome.get(c.obj.id), dx = c.x - h.x, dy = c.y - h.y, dist = Math.hypot(dx, dy); if (dist > CLUT_BUDGET) { const b = (dist - CLUT_BUDGET) / dist; c.x -= dx * b; c.y -= dy * b; } }
      }
      if (!moved) break;
    }

    // Escalation (staged): a pending group that still penetrates a frozen obstacle after full iterations
    // is `solver_blocked` — surfaced so a later reflect round may move/remove it (never auto-moved here).
    if (FROZEN) {
      const blockPad = Math.max(0, Number(process.env.IR_SOLVER_BLOCKED_PAD_M || 0.1));
      const blocked = [];
      for (let k = 0; k < groups.length; k++) {
        if (gfrozen[k]) continue;
        let pen = 0;
        for (const m of groups[k]) { if (isFrozen(m)) continue; for (const f of FROZEN.values()) { const p = m.geom.radius + f.r - Math.hypot(m.x - f.x, m.y - f.y); if (p > pen) pen = p; } }
        if (pen > blockPad) { const g0 = groups[k][0]; blocked.push({ id: (g0 && (g0._group || g0.obj.id)) || null, penetration: _round(pen) }); }
      }
      report.solverBlocked = blocked;
    }

    // residual structural overlaps (informational only — gentle accepts minor contacts)
    let remain = 0;
    for (let i = 0; i < structural.length; i++) for (let j = i + 1; j < structural.length; j++) if (Math.hypot(structural[j].x - structural[i].x, structural[j].y - structural[i].y) < structural[i].geom.radius + structural[j].geom.radius) remain++;
    report.overlapsRemaining = remain;
  }

  _clamp(placedList, halfM, report);
  const yawSnapDeg = _num(o.yawSnapDeg, Number(process.env.IR_YAW_SNAP_DEG || 90));
  const organicJitterM = _num(o.organicJitterM, Number(process.env.IR_ORGANIC_JITTER_M || 1.5));
  return { placed: _emit(placedList, positions, { yawSnapDeg, organicJitterM }), report };
}

function solve(graph, assetIndex, opts) {
  const o = opts || {};
  const mode = String(o.mode || process.env.IR_SOLVER || "legacy").toLowerCase();
  if (mode === "gentle") return solveGentle(graph, assetIndex, o);
  if (mode === "structure" || mode === "structured" || mode === "structure_preserving") return solveStructured(graph, assetIndex, o);
  return solveLegacy(graph, assetIndex, o);
}

module.exports = { solve, solveLegacy, solveStructured, solveGentle, _assetGeom, _expandPattern, GROUND_HALF_M };

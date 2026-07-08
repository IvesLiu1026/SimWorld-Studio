"use strict";
// ── Measurement pass (staged builder, Phase 2) ────────────────────────────────
// One python job over all labeled actors: get_actor_bounds → per-label REAL footprint (BP pivots,
// overhangs, scale all included), plus the same deterministic collision/floating/OOB checks the eval
// harness uses (computeSceneMetrics), so the staged loop measures reality instead of trusting the
// solver's DB-estimated boxes. The measured footprints feed the frozen re-solve (ir-solver opts.frozen)
// and the reflect status table. Keyed by actor label = plan id (the join key set by ir-exec).
//
// I/O-only: takes ueExec(script,timeoutMs) + ueLogs(result) injected by the orchestrator.

const MARK = "IR_MEASURE_OK";

// Where the measure python writes its JSON payload. Node reads it directly (UE editor + node share the
// local filesystem). Per-actor data for a dense scene is 100s of KB — printing it as ONE log line
// overflows the editor-log capture (and desyncs later reads), so we go through a file + a tiny marker.
function _defaultOutPath() {
  const port = process.env.UNREAL_PORT || "ue";
  return `/tmp/simworld_ir_measure_${port}.json`;
}

// Build the measure python. Mirrors run_asset_retrieval_ab_eval.js:computeSceneMetrics (INFRA filter,
// flat/big classification, AABB collision with TOUCH slack, support-surface floating, OOB) but emits
// per-actor measured bounds + collision partners keyed by label — written to `outPath`.
function _measureScript(groundHalfCm, floatThresholdCm, outPath) {
  const GH = Number(groundHalfCm || 13000), FT = Number(floatThresholdCm || 12);
  const OUT = JSON.stringify(String(outPath || _defaultOutPath()));
  return [
    "import unreal, json",
    `GH = ${GH}; FT = ${FT}; TOUCH = 5.0`,
    "acts = unreal.get_editor_subsystem(unreal.EditorActorSubsystem).get_all_level_actors()",
    "INFRA = ('floor','sky','light','atmo','fog','post','sphere','world','brush','default','player','gamemode','nav','levelbounds','landscape','volume','note','camera','directional','exponential','exprunner','arena_env','ground_plane')",
    "INFRA_CLS = ('directionallight','skylight','skyatmosphere','exponentialheightfog','worldsettings','playerstart','cameraactor','reflectioncapture','brush','navmeshboundingvolume','postprocessvolume')",
    "def infra(a):",
    "    if a.get_class().get_name().lower() in INFRA_CLS: return True",
    "    try: lbl=a.get_actor_label().lower()",
    "    except: lbl=a.get_name().lower()",
    "    return lbl.startswith(INFRA)",
    "B=[]",
    "for a in acts:",
    "    if infra(a): continue",
    "    try: o,e=a.get_actor_bounds(False)",
    "    except: continue",
    "    if e.x<1 and e.y<1 and e.z<1: continue",
    "    try: lbl=a.get_actor_label()",
    "    except: lbl=a.get_name()",
    "    loc=a.get_actor_location()",
    "    B.append({'n':lbl,'ox':o.x,'oy':o.y,'oz':o.z,'ex':max(e.x,1.0),'ey':max(e.y,1.0),'ez':max(e.z,1.0),'lx':loc.x,'ly':loc.y,'bot':o.z-e.z,'top':o.z+e.z,'cls':a.get_class().get_name()})",
    "n=len(B)",
    "def flat(b): return b['ez']<20.0",
    "CLUTTER_R=80.0",
    "def big(b): return (b['ex']*b['ex']+b['ey']*b['ey'])**0.5 >= CLUTTER_R",
    "coll=set(); pairs=0; scoll=set(); spairs=0; cw={}",
    "for i in range(n):",
    "    a=B[i]",
    "    for j in range(i+1,n):",
    "        b=B[j]",
    "        if flat(a) or flat(b): continue",
    "        ox=min(a['ox']+a['ex'],b['ox']+b['ex'])-max(a['ox']-a['ex'],b['ox']-b['ex'])",
    "        if ox<=TOUCH: continue",
    "        oy=min(a['oy']+a['ey'],b['oy']+b['ey'])-max(a['oy']-a['ey'],b['oy']-b['ey'])",
    "        if oy<=TOUCH: continue",
    "        oz=min(a['top'],b['top'])-max(a['bot'],b['bot'])",
    "        if oz<=TOUCH: continue",
    "        pairs+=1; coll.add(i); coll.add(j)",
    "        if big(a) or big(b):",
    "            spairs+=1; scoll.add(i); scoll.add(j)",
    "            cw.setdefault(a['n'],[]).append(b['n']); cw.setdefault(b['n'],[]).append(a['n'])",
    "fl=[]",
    "for a in B:",
    "    if a['bot']<=FT: continue",
    "    sup=False",
    "    for b in B:",
    "        if b is a: continue",
    "        if b['top']<a['bot']-1 and abs(b['ox']-a['lx'])<(a['ex']*0.5+b['ex']) and abs(b['oy']-a['ly'])<(a['ey']*0.5+b['ey']):",
    "            if a['bot']-b['top']<=FT: sup=True; break",
    "    if not sup: fl.append(a['n'])",
    "oob=[]",
    "for a in B:",
    "    if abs(a['lx'])>GH or abs(a['ly'])>GH: oob.append(a['n'])",
    "actors=[{'label':b['n'],'x_m':round(b['ox']/100.0,3),'y_m':round(b['oy']/100.0,3),'z_min':round(b['bot']/100.0,3),'w_m':round(2*b['ex']/100.0,3),'d_m':round(2*b['ey']/100.0,3),'h_m':round(2*b['ez']/100.0,3),'cls':b['cls'],'flat':flat(b),'big':big(b)} for b in B]",
    "rep={'checked':n,'collision_pairs':pairs,'structural_collision_pairs':spairs,'structural_collision_actors':sorted(set(B[i]['n'] for i in scoll)),'collision_actors':len(coll),'floating':fl,'out_of_bounds':oob,'colliding_with':cw}",
    "_OUT=" + OUT,
    "try:",
    "    with open(_OUT,'w') as _f: _f.write(json.dumps({'actors':actors,'report':rep}))",
    "    print('" + MARK + " n='+str(n)+' path='+_OUT)",
    "except Exception as _e:",
    "    print('IR_MEASURE_ERR '+str(_e))",
  ].join("\n");
}

// Run the measurement pass. Returns { ok, actors:[...], report:{...}, byLabel: Map }.
async function measureScene(opts) {
  const o = opts || {};
  const fs = require("fs");
  const log = o.log || (() => {});
  const ueExec = o.ueExec, ueLogs = o.ueLogs;
  const timeoutMs = Math.max(30000, Number(o.timeoutMs || 120000));
  const outPath = o.outPath || _defaultOutPath();
  // Delete any stale payload FIRST, so a python failure can't leave us reading a previous run's data.
  try { fs.unlinkSync(outPath); } catch (_e) {}
  const script = _measureScript(o.groundHalfCm, o.floatThresholdCm, outPath);
  const r = await ueExec(script, timeoutMs);
  // Read the FRESH payload file (no dependence on log capture — the desync source). When measuring
  // right after a spawn burst the bridge can resolve ueExec a beat before the editor flushes the file
  // to disk, so poll briefly for it to appear/parse instead of failing the whole re-solve.
  const readTry = () => { try { return JSON.parse(fs.readFileSync(outPath, "utf8")); } catch (_e) { return null; } };
  let parsed = readTry();
  for (let i = 0; i < 24 && !parsed; i++) { await new Promise(res => setTimeout(res, 250)); parsed = readTry(); }
  if (!parsed) {
    const logs = (typeof ueLogs === "function") ? (ueLogs(r) || "") : "";
    const ei = logs.indexOf("IR_MEASURE_ERR");
    return { ok: false, error: ei >= 0 ? logs.slice(ei).split(/\r?\n/)[0] : "no measure output file", actors: [], report: {}, byLabel: new Map() };
  }
  try {
    const actors = Array.isArray(parsed.actors) ? parsed.actors : [];
    const byLabel = new Map(actors.map(a => [String(a.label), a]));
    const report = parsed.report || {};
    // convenience rates (mirror computeSceneMetrics)
    const n = report.checked || 0;
    report.structural_collision_rate = n ? +(((report.structural_collision_actors || []).length) / n).toFixed(4) : 0;
    report.floating_rate = n ? +(((report.floating || []).length) / n).toFixed(4) : 0;
    report.oob_rate = n ? +(((report.out_of_bounds || []).length) / n).toFixed(4) : 0;
    return { ok: true, actors, report, byLabel };
  } catch (e) {
    return { ok: false, error: "measure parse failed: " + (e && e.message), actors: [], report: {}, byLabel: new Map() };
  }
}

// Join measured footprints onto placed entries by label==id. Mutates placed (adds .measured).
// Returns { matched, missing } counts.
function joinMeasured(placed, byLabel) {
  let matched = 0, missing = 0;
  for (const p of (placed || [])) {
    const a = byLabel.get(String(p.id));
    if (a) {
      p.measured = { x_m: a.x_m, y_m: a.y_m, w_m: a.w_m, d_m: a.d_m, h_m: a.h_m, z_min: a.z_min };
      matched++;
    } else { missing++; }
  }
  return { matched, missing };
}

// Build the frozen-obstacle list the solver consumes, from placed entries that have a `.measured`
// footprint (i.e. already spawned + measured). Only these exert immovable push in the re-solve.
function frozenFromMeasured(placed) {
  const out = [];
  for (const p of (placed || [])) {
    if (p.measured && p.measured.w_m != null) {
      out.push({ id: String(p.id), x_m: p.measured.x_m, y_m: p.measured.y_m, w_m: p.measured.w_m, d_m: p.measured.d_m, yaw: p.yaw_deg });
    }
  }
  return out;
}

// ── Calibration ───────────────────────────────────────────────────────────────
// The DB footprints are estimates (often wildly off for Blueprint actors — a courtyard building's real
// bbox can be 10× its DB dims), and the gentle solver ACCEPTS overlaps, so estimate error → collisions
// the solver never resolves. Calibration measures each UNIQUE asset's REAL footprint ONCE (spawn one
// of each on a scratch grid, measure, delete) so the structure re-solve places the plan with accurate
// bounds. Returns Map(asset_id -> { w_m, d_m, h_m }).
async function calibrateAssets(uniqueAssets, opts) {
  const o = opts || {};
  const log = o.log || (() => {});
  const irExec = require("./ir-exec");
  const list = (uniqueAssets || []).filter(a => a && a.path && a.asset_id);
  if (!list.length) return new Map();
  // Spawn one probe per asset on a wide grid (label "__cal_<i>"). Overlap is irrelevant — get_actor_bounds
  // measures each actor independently — so spacing only needs to keep most probes on the ground plane.
  const SP = 1200; // cm (12 m)
  const perRow = Math.max(1, Math.ceil(Math.sqrt(list.length)));
  const half = (perRow * SP) / 2;
  const entries = list.map((a, i) => ({
    id: "__cal_" + i, path: a.path, spawnTool: a.spawnTool,
    location: [(i % perRow) * SP - half, Math.floor(i / perRow) * SP - half, 0], yaw_deg: 0, scale: 1,
  }));
  const idToAsset = new Map(entries.map((e, i) => [e.id, list[i].asset_id]));
  await irExec.execTier(entries, { ueExec: o.ueExec, ueLogs: o.ueLogs, log, tier: "cal" });
  const meas = await measureScene({ ueExec: o.ueExec, ueLogs: o.ueLogs, log, outPath: o.outPath });
  const footprints = new Map();
  if (meas.ok) {
    for (const e of entries) {
      const m = meas.byLabel.get(e.id);
      if (m && m.w_m > 0) footprints.set(idToAsset.get(e.id), { w_m: m.w_m, d_m: m.d_m, h_m: m.h_m });
    }
  }
  const del = await irExec.deleteByPrefix("__cal_", { ueExec: o.ueExec, ueLogs: o.ueLogs });
  log(`calibrate: measured ${footprints.size}/${list.length} unique asset footprints (probes deleted=${del.deleted}, left=${del.left})`);
  return footprints;
}

// Build a solver assetIndex from a base index but with CALIBRATED (measured) footprints where available.
// Falls back to the base (estimated) footprint for any asset not calibrated.
function calibratedAssetIndex(baseIndex, footprints, placed) {
  const ai = new Map();
  const seen = new Set();
  const add = (id, base) => {
    if (seen.has(id)) return; seen.add(id);
    const fp = footprints.get(id);
    if (fp && fp.w_m > 0) ai.set(id, Object.assign({}, base, { footprint: { width: fp.w_m, depth: fp.d_m }, dims: { width: fp.w_m, depth: fp.d_m, height: fp.h_m }, boundingRadius: undefined }));
    else ai.set(id, base);
  };
  // seed from placed (guarantees every referenced asset is present with at least its estimated geom)
  for (const p of (placed || [])) {
    if (!p || !p.asset_id || seen.has(p.asset_id)) continue;
    const base = (baseIndex && baseIndex.get && baseIndex.get(p.asset_id)) || { id: p.asset_id, footprint: p.footprint, dims: { width: p.footprint.width, depth: p.footprint.depth, height: 0 }, category: p.category, name: p.name, path: p.path, spawnTool: p.spawnTool };
    add(p.asset_id, base);
  }
  return ai;
}

module.exports = { measureScene, joinMeasured, frozenFromMeasured, calibrateAssets, calibratedAssetIndex, _measureScript, MARK };

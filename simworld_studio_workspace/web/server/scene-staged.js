"use strict";
// ── Staged reflective builder — orchestrator ──────────────────────────────────
// A new /api/chat mode (body.stagedBuild / env IR_STAGED) that REPLACES the LLM builder with a
// deterministic, visually-grounded build loop. The LLM never emits coordinates (locked decision D1):
// the solved IR plan is spawned directly, tier by tier, with measurement / annotated-screenshot
// reflection between tiers (later phases).
//
// Phase 1 (this file's initial shape): plan → solve → exec-all (one pass, no tiers) → atmosphere →
// persist → finalize. SSE-streams the same event vocabulary the frontend / harness expect (system,
// retrieval, ir, progress, text, done) and NEVER calls load_level (fatal in the headless editor);
// scene resets go through the existing /api/ab-eval/reset-scene endpoint (owned by the caller/harness).
const fs = require("fs");
const path = require("path");

function _truthy(v) { return /^(1|true|yes|on)$/i.test(String(v == null ? "" : v)); }

// The eval harness appends a BUILD_GUIDE that enumerates EVERY theme ("…cobblestone for medieval;
// sand for a bazaar; grass for a park; snow for winter…"). ir-ground.themeKeys does keyword matching,
// and "snow" is its first theme — so the full message themes as snowy for ANY scene. The LLM builder
// silently overrides that suggestion; the deterministic executor would faithfully lay the wrong carpet.
// Theme off the ACTUAL scene prompt (the text before the appended guide) instead. No-op in the live app
// (no guide appended). Scoped to the carpet's keyword theming; the LLM planner reads the whole message.
function _sceneForTheme(message) {
  const s = String(message || "");
  const cut = s.search(/\n\nBUILD A FULL|\n\nBUILD A ~|\n\nASSET DISCOVERY|\n\nFor this automated/);
  return cut > 0 ? s.slice(0, cut).trim() : s;
}

// Master switch for the staged path: body.stagedBuild / body.irStaged | env IR_STAGED. Default OFF.
function resolveStaged(body) {
  const b = body || {};
  for (const k of ["stagedBuild", "irStaged"]) { const v = b[k]; if (v != null && v !== "") return _truthy(v); }
  return _truthy(process.env.IR_STAGED);
}
// Measured-bounds frozen re-solve between tiers: body.irMeasureResolve | env IR_MEASURE_RESOLVE.
// Default OFF — the staged-exec arm is a single pass (Phase 1); staged-noreflect/reflect enable it.
function resolveMeasureResolve(body) {
  const b = body || {};
  if (b.irMeasureResolve != null && b.irMeasureResolve !== "") return _truthy(b.irMeasureResolve);
  return _truthy(process.env.IR_MEASURE_RESOLVE);
}
// Number of spawn tiers after ground (K): body.irTiers | env IR_TIERS. Default 3.
function resolveTiers(body) {
  const b = body || {};
  const v = (b.irTiers != null && b.irTiers !== "") ? b.irTiers : process.env.IR_TIERS;
  const n = Math.floor(Number(v));
  return Number.isFinite(n) && n >= 1 ? n : 3;
}
// Per-tier annotated capture: body.irCapture | env IR_CAPTURE, OR implied when reflect is on. Default off.
function resolveReflect(body) {
  const b = body || {};
  if (b.irReflect != null && b.irReflect !== "") return _truthy(b.irReflect);
  return _truthy(process.env.IR_REFLECT);
}
function resolveCapture(body) {
  const b = body || {};
  if (b.irCapture != null && b.irCapture !== "") return _truthy(b.irCapture);
  if (_truthy(process.env.IR_CAPTURE)) return true;
  return resolveReflect(body); // reflect needs the captures
}

// Idempotent atmosphere pass — mirrors mcp-server.js:toolSetupEnvironment's core (afternoon light):
// spawn sky/sun/skylight/fog ONLY if the class is missing, so a deterministic scene is lit even
// outside the eval harness. The harness's ensureDaytimeSky still runs after `done` for eval parity.
function _atmosphereScript() {
  return [
    "import unreal",
    "_s = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)",
    "_acts = _s.get_all_level_actors()",
    "def _hasc(names):",
    "    for a in _acts:",
    "        try:",
    "            if a.get_class().get_name() in names: return True",
    "        except Exception: pass",
    "    return False",
    "_created = []",
    "if not _hasc(['SkyAtmosphere']):",
    "    _a = _s.spawn_actor_from_class(unreal.SkyAtmosphere.static_class(), unreal.Vector(0,0,0)); _a.set_actor_label('Arena_Env_Atmosphere'); _created.append('atmosphere')",
    "if not _hasc(['DirectionalLight']):",
    "    _sun = _s.spawn_actor_from_class(unreal.DirectionalLight.static_class(), unreal.Vector(0,0,500)); _sun.set_actor_label('Arena_Env_Sun')",
    "    _sun.set_actor_rotation(unreal.Rotator(pitch=-45.0, yaw=30.0, roll=0.0), False)",
    "    _c = _sun.get_component_by_class(unreal.DirectionalLightComponent)",
    "    if _c: _c.set_intensity(10.0); _c.set_atmosphere_sun_light(True)",
    "    _created.append('sun')",
    "if not _hasc(['SkyLight']):",
    "    _sky = _s.spawn_actor_from_class(unreal.SkyLight.static_class(), unreal.Vector(0,0,500)); _sky.set_actor_label('Arena_Env_SkyLight')",
    "    _sc = _sky.get_component_by_class(unreal.SkyLightComponent)",
    "    if _sc: _sc.set_editor_property('intensity', 3.0)",
    "    _created.append('skylight')",
    "if not _hasc(['ExponentialHeightFog','AtmosphericFog']):",
    "    _fog = _s.spawn_actor_from_class(unreal.ExponentialHeightFog.static_class(), unreal.Vector(0,0,0)); _fog.set_actor_label('Arena_Env_Fog')",
    "    _fc = _fog.get_component_by_class(unreal.ExponentialHeightFogComponent)",
    "    if _fc: _fc.set_editor_property('fog_density', 0.002); _fc.set_editor_property('fog_max_opacity', 0.6)",
    "    _created.append('fog')",
    "for _cmd in ['r.ViewDistanceScale 100','r.StaticMeshLODDistanceScale 0.01','r.ForceLOD 0','foliage.LODDistanceScale 100']:",
    "    unreal.SystemLibrary.execute_console_command(None, _cmd)",
    "print('SETUP_ENV created=' + ','.join(_created))",
  ].join("\n");
}

// Express handler: /api/chat when the staged path is selected. `deps` (injected by index.js) provides:
//   ueExecScript(script, timeoutMs) · ueLogs(result) · ctxManager · studioSession · logToFile · MOCK_MODE
async function handleStagedBuild(req, res, deps) {
  const body = req.body || {};
  const message = body.message;
  if (!message) { res.status(400).json({ error: "message required" }); return; }

  res.setHeader("Content-Type", "text/event-stream");
  res.setHeader("Cache-Control", "no-cache");
  res.setHeader("Connection", "keep-alive");
  res.setHeader("X-Accel-Buffering", "no");
  res.flushHeaders();
  const emit = (name, data) => { if (!res.writableEnded) res.write(`event: ${name}\ndata: ${JSON.stringify(data || {})}\n\n`); };
  const ping = setInterval(() => { if (!res.writableEnded) res.write(`: ping\n\n`); }, 5000);
  const log = (x) => { try { deps.logToFile && deps.logToFile("staged", x); } catch (_e) {} };

  const ueExec = deps.ueExecScript, ueLogs = deps.ueLogs;
  const tStart = Date.now();
  const stages = {};
  emit("system", { sessionId: deps.studioSession, mcpServers: [{ name: "simworld", status: "connected" }], staged: true });

  try {
    const sceneIR = require("./scene-ir");
    const irExec = require("./ir-exec");
    const model = require("./model-config").resolveModel(body);

    // ── 1. Plan + solve (retrieval → IR planner → gentle solver → plan-critic), structured ──
    emit("retrieval", { phase: "start" });
    emit("ir", { phase: "start" });
    const _tp = Date.now();
    const plan = await sceneIR.planAndSolve(message, {
      model, provider: "codex", runner: "codex",
      irSolver: body.irSolver, irRepair: body.irRepair, irCot: body.irCot,
      irRichAssets: body.irRichAssets, irPlanCritic: body.irPlanCritic,
      log,
    });
    stages.plan_ms = Date.now() - _tp;
    emit("retrieval", { phase: "done" });

    if (!plan || !plan.placed || !plan.placed.length) {
      log("staged: planner produced no placements — aborting staged build");
      emit("ir", { phase: "error", message: "no plan/placements" });
      emit("text", { delta: "\n\nStaged build failed: planner produced no placements.\n" });
      emit("done", { sessionId: deps.studioSession, isError: true, staged: true, latestScreenshot: null });
      clearInterval(ping); res.end(); return;
    }
    const nObj = (plan.graph.objects || []).length, nPat = (plan.graph.patterns || []).length;
    emit("ir", { phase: "done", placed: plan.placed.length, objects: nObj, patterns: nPat });
    log(`staged: plan solved — ${plan.placed.length} placements (${nObj} obj + ${nPat} patterns), solver=${plan.solveReport && plan.solveReport.solver}`);

    const irTiers = require("./ir-tiers");
    const irMeasure = require("./ir-measure");
    const { solve } = require("./ir-solver");
    const K = resolveTiers(body);
    const measureResolve = resolveMeasureResolve(body);

    // Structure-solver tuning for the measured-bounds re-solve (run3 clamp/pack; opts, not global env).
    // DENSITY↔COLLISION defaults (Phase 6 same-plan sweep, experiments/phase6_sweep). The winning point:
    // enough room (extent 65) + mild size-awareness (pack 0.3) + strong anchor-pull (0.5) + MANY de-overlap
    // iterations (1200) — objects separate PROPERLY instead of packing with residual overlap, so collisions
    // drop BELOW baseline (same plan: desert 0.29→0.20, park 0.27→0.17) while staying DENSE (spread ~65m,
    // not the old sparse 169m). Precedence: per-request body → env → default; the body override also lets
    // one server session solve the SAME cached plan under different configs (clean solver A/B). Shift to a
    // higher extent / fewer iters for a sparser sim-valid point, or lower extent for denser+more overlap.
    const _sc = (b, e, d) => { const v = body[b] != null ? body[b] : process.env[e]; const n = Number(v); return Number.isFinite(n) ? n : d; };
    const STRUCT_CFG = {
      mode: "structure",
      targetExtentM: _sc("irStagedExtentM", "IR_STAGED_TARGET_EXTENT_M", 65),
      targetPack: _sc("irStagedPack", "IR_STAGED_TARGET_PACK", 0.3),
      groupIters: _sc("irStagedIters", "IR_STAGED_GROUP_ITERS", 1200),
      anchorPull: _sc("irStagedPull", "IR_STAGED_ANCHOR_PULL", 0.5),
      organicJitterM: _sc("irStagedJitterM", "IR_STAGED_JITTER_M", 2.0),
    };

    // ── 2. MEASURED-BOUNDS STAGING (the collision win) — calibrate each unique asset's REAL footprint,
    //    then re-solve the plan with the STRUCTURE solver (aggressive de-overlap) using those measured
    //    bounds. The gentle solver accepts overlaps, so estimate error → collisions it never resolves;
    //    structure+measured drops struct_rate ~3× (validated 0.32→0.11). staged-exec skips this. ──
    let calAI = null;
    if (measureResolve) {
      emit("progress", { stage: "calibrate", phase: "start" });
      const _tc = Date.now();
      const seenA = new Set(); const uniq = [];
      for (const p of plan.placed) { if (!p.isGround && p.asset_id && !seenA.has(p.asset_id)) { seenA.add(p.asset_id); uniq.push({ asset_id: p.asset_id, path: p.path, spawnTool: p.spawnTool }); } }
      const footprints = await irMeasure.calibrateAssets(uniq, { ueExec, ueLogs, log });
      stages.calibrate_ms = Date.now() - _tc;
      emit("progress", { stage: "calibrate", phase: "done", measured: footprints.size, unique: uniq.length });
      if (footprints.size) {
        calAI = irMeasure.calibratedAssetIndex(plan.assetIndex, footprints, plan.placed);
        const corrected = solve(plan.graph, calAI, STRUCT_CFG);
        const cById = new Map(corrected.placed.map(p => [String(p.id), p]));
        for (const p of plan.placed) { const cp = cById.get(String(p.id)); if (cp) { p.location = cp.location; p.x_m = cp.x_m; p.y_m = cp.y_m; p.yaw_deg = cp.yaw_deg; } }
        log(`staged: calibrated structure re-solve applied (${footprints.size}/${uniq.length} measured, solverBlocked=${(corrected.report.solverBlocked || []).length})`);
      } else { log("staged: calibration measured 0 footprints — falling back to the gentle plan"); }
    }

    // ── 3. Tier derivation (§3.1), on the (possibly corrected) positions. ──
    irTiers.assignTiers(plan.graph, plan.placed, { tiers: K });
    const tierHist = irTiers.tierHistogram(plan.placed);
    log(`staged: tiers assigned (K=${K}) — ${JSON.stringify(tierHist)}`);
    emit("progress", { stage: "tiers", phase: "done", histogram: tierHist });

    const objectEntries = plan.placed.filter(p => !p.isGround);
    // Fit the ground to the actual (re-solved) object cluster. The tuned structure solver clusters objects
    // tighter than the planner's fixed ground-fill grid assumed, so a fixed-size carpet AND that grid would
    // overhang the objects as an empty offset square. Size both to the 92nd-pct object extent (outlier-robust).
    const _cheb = objectEntries.map(p => Math.max(Math.abs(p.x_m || 0), Math.abs(p.y_m || 0))).sort((a, b) => a - b);
    const groundHalfM = Math.max(20, Math.ceil((_cheb.length ? _cheb[Math.floor(0.92 * (_cheb.length - 1))] : 40) + 8));
    // Clip the plan's ground-FILL grid (a high-count repeated tile asset) to that extent; keep singular
    // ground ACCENTS (roads, manholes, junctions) wherever the planner put them.
    let groundEntries = plan.placed.filter(p => p.isGround);
    {
      const cnt = new Map(); for (const p of groundEntries) cnt.set(p.asset_id, (cnt.get(p.asset_id) || 0) + 1);
      const before = groundEntries.length;
      groundEntries = groundEntries.filter(p => (cnt.get(p.asset_id) || 0) < 25
        || (Math.abs(p.x_m || 0) <= groundHalfM && Math.abs(p.y_m || 0) <= groundHalfM));
      if (groundEntries.length !== before) log(`staged: clipped ground-fill ${before}->${groundEntries.length} tiles to +/-${groundHalfM}m object extent`);
    }
    let placedById = new Map(plan.placed.map(p => [String(p.id), p]));

    // Per-tier capture setup (Phase 3): stable mark numbers per scene, fixed framing from the plan.
    const capture = resolveCapture(body);
    const irCapture = capture ? require("./ir-capture") : null;
    const markOf = new Map(); if (capture) { let n = 0; for (const p of objectEntries) markOf.set(String(p.id), ++n); }
    const framing = capture ? irCapture.frameFromPlan(plan.placed) : null;
    const captureDir = capture ? path.join(sceneIR.irArtifactBase(message) + "__captures") : null;
    const tierCaptures = [];

    // Reflect setup (Phase 4): one VLM round per tier edits the persistent IR via typed ops, gated.
    const reflectOn = resolveReflect(body);
    const irReflect = reflectOn ? require("./ir-reflect") : null;
    // Option B: add elevated eye-level views to the reflect capture (body → env). Per-request so eye-on vs
    // eye-off can be A/B'd on the SAME cached plan.
    const eyeViews = /^(1|true|yes|on)$/i.test(String(body.irReflectEyeViews != null ? body.irReflectEyeViews : (process.env.IR_REFLECT_EYE_VIEWS || "")));
    // Option A: path-based multi-camera rig (readable LOCAL eye views on walkable ground + top/aerial).
    const multiview = /^(1|true|yes|on)$/i.test(String(body.irReflectMultiview != null ? body.irReflectMultiview : (process.env.IR_REFLECT_MULTIVIEW || "")));
    const ledger = [];              // applied placed-op history for the anti-oscillation freeze + prompt
    const frozenAttrs = new Set();  // "id|attr" frozen after an inverse/repeat edit
    const opLog = [];               // full op audit (all tiers)
    const reflectRounds = [];       // per-tier reflect I/O for artifacts
    const paletteList = (plan.assets || []).map(a => ({ id: a.id, name: a.name, category: a.category, dims: a.dims ? `${a.dims.width}x${a.dims.depth}x${a.dims.height}` : "" }));
    const paletteIds = new Set((plan.assets || []).map(a => String(a.id)));
    const REFLECT_PLACED_BUDGET = Math.max(0, Number(process.env.IR_REFLECT_PLACED_BUDGET || 3));

    // ── 3. Tier 0 — deterministic FLAT-PLANE ground carpet (themed material). Flat planes are
    //    metric-exempt (unlike thick mesh tiles) and robust; the plan's own ground_and_road accents
    //    (spawned below at tier 0) sit on top. ──
    let carpet = null;
    if (sceneIR.resolveGroundPass(body)) {
      try {
        const irGround = require("./ir-ground");
        const cs = irGround.groundCarpetPlaneScript(_sceneForTheme(message), Number(process.env.IR_GROUND_CARPET_HALF_M) || groundHalfM);
        emit("progress", { stage: "ground", phase: "start" });
        const _tg = Date.now();
        const logs = await irExec.execRawScript(cs.script, { ueExec, ueLogs, timeoutMs: 180000 });
        const m = logs.match(/\[GROUND\]\s+carpet planes spawned:\s*(\d+)/);
        carpet = { material: cs.material, planeM: cs.planeM, planes: m ? Number(m[1]) : null };
        stages.ground_ms = Date.now() - _tg;
        log(`staged tier0 ground carpet: ${carpet.planes} planes @ ${cs.planeM}m, material=${cs.material}`);
        emit("progress", { stage: "ground", phase: "done", planes: carpet.planes });
      } catch (e) { log("staged ground-carpet failed (non-fatal): " + (e && e.message)); }
    }

    // ── 4. Spawn. staged-noreflect/reflect: tier-by-tier with measure + frozen re-solve between tiers.
    //    staged-exec: single pass (Phase 1 behaviour). ──
    emit("progress", { stage: "spawn", phase: "start", total: plan.placed.length });
    const _ts = Date.now();
    const execOpts = { ueExec, ueLogs, log };
    const spawnFailures = [];
    const tierReports = [];
    let spawnedCount = 0, failedCount = 0;
    const acct = (r) => { spawnedCount += r.counts.spawned; failedCount += r.counts.failed; for (const f of r.failed) { spawnFailures.push(f); const p = placedById.get(String(f.id)); if (p) p.status = "spawn_failed"; } };

    // Tier 0: ground accents spawn with the carpet.
    acct(await irExec.execTier(groundEntries, Object.assign({ tier: 0 }, execOpts)));

    if (measureResolve) {
      for (let k = 1; k <= K; k++) {
        // filter plan.placed directly — reflect may have added/reassigned objects into later tiers.
        const tierEntries = plan.placed.filter(p => !p.isGround && p.tier === k);
        if (!tierEntries.length) { log(`staged tier ${k}: (empty)`); continue; }
        emit("progress", { stage: "tier", tier: k, phase: "spawn", count: tierEntries.length });
        acct(await irExec.execTier(tierEntries, Object.assign({ tier: k }, execOpts)));

        // MEASURE everything spawned so far → real footprints joined onto placed by label.
        const meas = await irMeasure.measureScene({ ueExec, ueLogs, log });
        if (meas.ok) {
          const joined = irMeasure.joinMeasured(plan.placed, meas.byLabel);
          tierReports.push({ tier: k, checked: meas.report.checked, struct_rate: meas.report.structural_collision_rate, floating: (meas.report.floating || []).length, oob: (meas.report.out_of_bounds || []).length, joined: joined.matched });
          log(`staged tier ${k} measure: checked=${meas.report.checked} struct_rate=${meas.report.structural_collision_rate} floating=${(meas.report.floating || []).length} joined=${joined.matched}/${joined.matched + joined.missing}`);
          emit("progress", { stage: "tier", tier: k, phase: "measure", struct_rate: meas.report.structural_collision_rate, floating: (meas.report.floating || []).length });
        } else { log(`staged tier ${k} measure FAILED (non-fatal): ${meas.error}`); }

        // CAPTURE + ANNOTATE this tier (top + aerial), for the reflect agent / evolution-strip artifacts.
        if (capture && deps.ueCommand) {
          try {
            const _tcap = Date.now();
            const shots = await irCapture.captureTier({ ueCommand: deps.ueCommand, log, outDir: captureDir, placed: plan.placed, framing, markOf, curTier: k, tiers: K, eyeOn: eyeViews, rig: multiview, scene: message.split("\n")[0].slice(0, 70) });
            tierCaptures.push({ tier: k, views: shots.map(v => ({ view: v.view, annotated: v.annotated, raw: v.raw })) });
            stages.capture_ms = (stages.capture_ms || 0) + (Date.now() - _tcap);
            emit("progress", { stage: "tier", tier: k, phase: "capture", views: shots.filter(v => v.annotated).length });
          } catch (e) { log(`staged tier ${k} capture failed (non-fatal): ${e && e.message}`); }
        }

        // REFLECT (one VLM round): typed ops on the persistent IR, validated + gated. Only when there's
        // a next tier to fold edits into (the last tier has nowhere to respawn moved/added objects).
        let deltas = null, graphSnapshot = null, ckptManifest = null, preOp = null;
        if (reflectOn && irReflect && meas.ok && k < K) {
          try {
            const capViews = (tierCaptures.find(t => t.tier === k) || { views: [] }).views.filter(v => v.annotated);
            const imgs = capViews.map(v => v.annotated);
            log(`staged tier ${k} reflect INPUT: ${imgs.length} images [${capViews.map(v => v.view).join(",")}] eyeViews=${eyeViews}`);
            const st = irReflect.statusTable(plan.placed, markOf, k, meas.report);
            emit("progress", { stage: "tier", tier: k, phase: "reflect_start" });
            const _tr = Date.now();
            const rf = await irReflect.reflect({
              scene: message.split("\n")[0], brief: plan.brief, tier: k, tiers: K, images: imgs, imageViews: capViews.map(v => v.view),
              measure: meas.report, statusTable: st, palette: paletteList, ledger,
              spawnFailed: plan.placed.filter(p => p.status === "spawn_failed").length,
              solverBlocked: plan.placed.filter(p => p.status === "solver_blocked").length,
            }, { model, timeoutMs: Number(process.env.IR_REFLECT_TIMEOUT_MS || 180000) });
            stages.reflect_ms = (stages.reflect_ms || 0) + (Date.now() - _tr);
            const markNumbers = new Set([...markOf.values()]);
            const placedIds = new Set(plan.placed.filter(p => !p.isGround && p.tier <= k).map(p => String(p.id)));
            const { valid, dropped } = irReflect.normalizeOps(rf.ops, { placedIds, markNumbers, palette: paletteIds, frozenAttrs, placedBudget: REFLECT_PLACED_BUDGET, totalCap: 12 });
            log(`staged tier ${k} reflect: "${(rf.assessment || "").slice(0, 90)}" ops=${(rf.ops || []).length} valid=${valid.length} dropped=${dropped.length} no_change=${rf.no_change}`);
            const PLACED_OPS = new Set(["remove", "move", "swap_asset", "adjust_pattern"]);
            if (valid.length) {
              const hasPlacedOp = valid.some(op => PLACED_OPS.has(op.op));
              if (hasPlacedOp && deps.ckptCaptureManifest) { ckptManifest = await deps.ckptCaptureManifest(); preOp = { struct: (meas.report.structural_collision_actors || []).length, float: (meas.report.floating || []).length }; }
              graphSnapshot = JSON.parse(JSON.stringify(plan.graph));
              deltas = irReflect.applyOps(valid, plan.graph, k);
              if (deltas.deleteIds.size) await irExec.deleteByLabels([...deltas.deleteIds], { ueExec, ueLogs });
              irReflect.updateFreeze(ledger, valid.filter(op => PLACED_OPS.has(op.op)), frozenAttrs);
              opLog.push({ tier: k, applied: deltas.opLog, dropped });
            }
            reflectRounds.push({ tier: k, assessment: rf.assessment, no_change: rf.no_change, ops_raw: rf.ops, valid_count: valid.length, dropped, images: imgs });
            try { fs.mkdirSync(captureDir, { recursive: true }); fs.writeFileSync(path.join(captureDir, `reflect_${k}.json`), JSON.stringify({ tier: k, bundle: { statusTable: st, measure: meas.report }, model_output: rf, valid, dropped, applied: deltas ? deltas.opLog : [] }, null, 2)); } catch (_e) {}
            emit("progress", { stage: "tier", tier: k, phase: "reflect_done", valid: valid.length, dropped: dropped.length });
          } catch (e) { log(`staged tier ${k} reflect failed (non-fatal): ${e && e.message}`); }
        }

        // RE-SOLVE + REBUILD pending: re-derive from the (possibly reflect-mutated) graph with the
        // structure solver + calibrated bounds; spawned actors FROZEN at their measured footprint.
        if (k < K && meas.ok) {
          try {
            const frozenList = plan.placed.filter(p => !p.isGround && p.tier <= k && p.measured && !(deltas && deltas.deleteIds.has(String(p.id))));
            const frozen = irMeasure.frozenFromMeasured(frozenList);
            const re = solve(plan.graph, calAI || plan.assetIndex, Object.assign({}, calAI ? STRUCT_CFG : { mode: plan.mode || "gentle" }, { frozen }));
            const frozenIds = new Set(frozenList.map(p => String(p.id)));
            const oldById = new Map(plan.placed.map(p => [String(p.id), p]));
            const tierMap = new Map(plan.placed.filter(p => !p.isGround).map(p => [String(p.id), p.tier]));
            if (deltas) { for (const id of deltas.reassignIds) tierMap.set(String(id), k + 1); for (const id of deltas.addedIds) tierMap.set(String(id), k + 1); }
            const kept = plan.placed.filter(p => p.isGround);
            for (const rp of re.placed) {
              if (rp.isGround) continue;
              const id = String(rp.id);
              if (frozenIds.has(id)) { kept.push(oldById.get(id)); continue; } // spawned + frozen: keep measured entry
              const old = oldById.get(id);
              rp.tier = tierMap.has(id) ? tierMap.get(id) : (old ? old.tier : k + 1);
              if (old && old.status && old.tier <= k) rp.status = old.status;
              kept.push(rp);
            }
            plan.placed = kept;
            placedById = new Map(plan.placed.map(p => [String(p.id), p]));
            const blocked = (re.report.solverBlocked || []);
            for (const b of blocked) { const p = placedById.get(String(b.id)); if (p && p.status !== "spawn_failed") p.status = "solver_blocked"; }
            log(`staged tier ${k} re-solve: ${re.placed.length} placed, frozen=${frozen.length}, solver_blocked=${blocked.length}${deltas ? `, reflect(+${deltas.addedIds.size} add, ${deltas.deleteIds.size} del)` : ""}`);
            emit("progress", { stage: "tier", tier: k, phase: "resolve", frozen: frozen.length, blocked: blocked.length });

            // GATE: revert reflect placed-ops if the placed set's collisions/floating regressed.
            if (deltas && ckptManifest && preOp && deps.ckptRestoreManifest) {
              const rem = await irMeasure.measureScene({ ueExec, ueLogs });
              if (rem.ok) {
                const post = { struct: (rem.report.structural_collision_actors || []).length, float: (rem.report.floating || []).length };
                const forceRevert = /^(1|true|yes|on)$/i.test(String(process.env.IR_REFLECT_FORCE_REVERT || "")); // debug: exercise the revert path
                if (forceRevert || post.struct > preOp.struct || post.float > preOp.float) {
                  await deps.ckptRestoreManifest(ckptManifest);
                  plan.graph = graphSnapshot;
                  log(`staged tier ${k} reflect GATE: regression (struct ${preOp.struct}->${post.struct}, float ${preOp.float}->${post.float}) → reverted`);
                  if (opLog.length) opLog[opLog.length - 1].reverted = true;
                }
              }
            }
          } catch (e) { log(`staged tier ${k} re-solve failed (non-fatal): ${e && e.message}`); }
        }
      }
    } else {
      // staged-exec: single pass over all non-ground objects.
      acct(await irExec.execTier(objectEntries, Object.assign({ tier: 1 }, execOpts)));
    }
    stages.spawn_ms = Date.now() - _ts;
    emit("progress", { stage: "spawn", phase: "done", spawned: spawnedCount, failed: failedCount });
    log(`staged spawn: ${spawnedCount}/${plan.placed.length} spawned, ${failedCount} spawn_failed`);

    // ── 4. Atmosphere (idempotent; harness ensureDaytimeSky still runs after for eval parity) ──
    try {
      emit("progress", { stage: "atmosphere", phase: "start" });
      const _ta = Date.now();
      const alogs = await irExec.execRawScript(_atmosphereScript(), { ueExec, ueLogs, timeoutMs: 60000 });
      stages.atmosphere_ms = Date.now() - _ta;
      const am = alogs.match(/SETUP_ENV\s+(.*)$/m);
      log("staged atmosphere: " + (am ? am[1] : "(applied)"));
      emit("progress", { stage: "atmosphere", phase: "done" });
    } catch (e) { log("staged atmosphere failed (non-fatal): " + (e && e.message)); }

    // ── 5. Persist ir_scene.json (+ascii) where the harness copies it into the cell dir ──
    stages.total_ms = Date.now() - tStart;   // computed before persist so it lands in the saved telemetry
    try {
      const base = sceneIR.irArtifactBase(message);
      fs.mkdirSync(path.dirname(base), { recursive: true });
      const irScene = {
        scene: message,
        graph: plan.graph,
        placed: plan.placed,
        solveReport: plan.solveReport,
        planReport: plan.planReport,
        staged: {
          mode: plan.mode,
          tiers: K,
          tier_histogram: tierHist,
          measure_resolve: measureResolve,
          calibrated: !!calAI,
          struct_cfg: measureResolve ? STRUCT_CFG : null,
          capture: capture,
          capture_dir: captureDir,
          tier_captures: tierCaptures,
          reflect: reflectOn,
          reflect_rounds: reflectRounds.map(r => ({ tier: r.tier, assessment: r.assessment, no_change: r.no_change, ops_proposed: (r.ops_raw || []).length, ops_applied: r.valid_count, ops_dropped: (r.dropped || []).length })),
          op_log: opLog,
          tier_reports: tierReports,
          ground_carpet: carpet,
          spawn: { attempted: plan.placed.length, spawned: spawnedCount, failed: failedCount, failures: spawnFailures.slice(0, 80) },
          stages_ms: stages,
        },
      };
      fs.writeFileSync(base + ".json", JSON.stringify(irScene, null, 2));
      if (plan.ascii) fs.writeFileSync(base + ".ascii.txt", plan.ascii);
      log("staged: persisted ir_scene.json -> " + base + ".json");
    } catch (e) { log("staged persist failed (non-fatal): " + (e && e.message)); }

    try { if (deps.ctxManager && deps.studioSession) deps.ctxManager.setEnvironmentReady(deps.studioSession); } catch (_e) {}

    emit("text", { delta: `\nStaged build complete: ${spawnedCount} actors spawned (${failedCount} failed) from ${plan.placed.length} planned placements in ${(stages.total_ms / 1000).toFixed(1)}s.\n` });
    emit("done", { sessionId: deps.studioSession, isError: false, staged: true, spawned: spawnedCount, failed: failedCount, planned: plan.placed.length, latestScreenshot: null });
    clearInterval(ping); res.end();
  } catch (e) {
    log("staged FAILED: " + ((e && e.stack) || (e && e.message) || e));
    try {
      emit("text", { delta: "\n\nStaged build error: " + ((e && e.message) || e) + "\n" });
      emit("done", { sessionId: deps.studioSession, isError: true, staged: true, latestScreenshot: null });
    } catch (_e) {}
    clearInterval(ping); try { res.end(); } catch (_e) {}
  }
}

module.exports = { handleStagedBuild, resolveStaged };

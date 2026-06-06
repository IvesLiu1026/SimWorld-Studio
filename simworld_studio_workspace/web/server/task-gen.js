"use strict";
/**
 * task-gen.js — Task-Generation stage backend (navmesh build + episode sampling).
 *
 * Builds a navmesh over the live UE scene and samples navigation episodes (start + goal on
 * the walkable surface, plus the ground-truth geodesic path) directly in the running editor
 * via `execute_python_script`. No external Python process / file round-trip — the sampler
 * runs inside UE through the same TCP path the rest of the server uses, so it works on
 * whatever scene is currently loaded (a just-built scene, not only static maps).
 *
 * Recipe (proven on dev): ensure a NavMeshBoundsVolume covers the scene → RebuildNavigation
 * → project a point with a large vertical extent to find the walkable surface height (the
 * floor is not at z=0) → get_random_reachable_point_in_radius for start/goal →
 * find_path_to_location_synchronously for the gt path + geodesic length. Episodes are
 * filtered by [minPathCm, maxPathCm] so degenerate/too-long pairs are dropped.
 *
 * Schema matches datasets/diverse50/*.jsonl + nav_task/ so generated sets are training-ready.
 */

// Common Python prelude shared by both scripts. `emit` writes the result to a server-chosen
// file we read back from disk (server + UE share the host) — far more robust than captured
// stdout, which UE truncates at ~2000 chars/line and may not capture right after a boot.
function _prelude(outPath) {
  return `
import unreal, math, json
OUT_PATH = ${JSON.stringify(outPath)}
def emit(out):
    with open(OUT_PATH, "w") as f: json.dump(out, f)
def jv(v): return [round(v.x,1), round(v.y,1), round(v.z,1)]
ues=unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem); world=ues.get_editor_world()
eas=unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
nav=unreal.NavigationSystemV1
try: map_name=unreal.SystemLibrary.get_object_name(world)
except Exception: map_name=""
`;
}

// Phase 1: ensure a NavMeshBoundsVolume covers the scene, then kick off the navmesh build.
// The build then proceeds over subsequent engine ticks — which only happen BETWEEN our calls,
// so the caller polls the sampler (phase 2) with delays rather than sampling inline here.
function buildSetupScript(outPath) {
  return (_prelude(outPath) + `
if world is None:
    emit({"ok":False,"error":"no editor world loaded"})
else:
    have_vol=False
    for a in eas.get_all_level_actors():
        if isinstance(a, unreal.NavMeshBoundsVolume): have_vol=True; break
    spawned_vol=False
    if not have_vol:
        vol=eas.spawn_actor_from_class(unreal.NavMeshBoundsVolume, unreal.Vector(0,0,300), unreal.Rotator(0,0,0))
        vol.set_actor_label("Arena_Nav_Bounds")
        vol.set_actor_scale3d(unreal.Vector(120,120,30))
        spawned_vol=True
    unreal.SystemLibrary.execute_console_command(world, "RebuildNavigation")
    emit({"ok":True,"spawned_vol":spawned_vol,"map_name":map_name})
`).trim();
}

// Phase 2: project to find the walkable surface; if the navmesh isn't ready yet, report
// ready=False (the caller waits and retries). Once ready, sample episodes across the WHOLE
// navmesh: random XY over the navmesh bounds (× area_frac, centred) → project each to the
// navmesh → keep only navigable hits (so obstacle footprints are skipped automatically) →
// validate the pair is actually connected (path end reaches the goal) → length filter.
// ObjectNav: goal is the navmesh point nearest a real scene object (its view point).
function buildSampleScript(params, outPath) {
  const P = {
    n_episodes:  Math.max(1, Math.min(500, params.episodes | 0 || 12)),
    min_path_cm: Number(params.minPathCm) > 0 ? Number(params.minPathCm) : 500.0,
    max_path_cm: Number(params.maxPathCm) > 0 ? Number(params.maxPathCm) : 12000.0,
    task_type:   params.taskType === "objectnav" ? "objectnav" : "pointnav",
    area_frac:   Number(params.areaFrac) > 0 ? Math.min(1, Number(params.areaFrac)) : 0.9,
    seed:        Number.isFinite(params.seed) ? (params.seed | 0) : 0,
  };
  return (_prelude(outPath) + `
import random
P = ${JSON.stringify(P)}
random.seed(P["seed"])
INFRA=("SW_","Sky","Light","Atmospheric","Directional","Sun","Arena_Nav","Nav","PlayerStart","Brush","Floor","Ground","Plane")
def heading(pts):
    if len(pts)<2: return 0.0
    return round(math.degrees(math.atan2(pts[1][1]-pts[0][1], pts[1][0]-pts[0][0])),1)
if world is None:
    emit({"ok":True,"ready":False,"map_name":map_name})
else:
    vols=[a for a in eas.get_all_level_actors() if isinstance(a, unreal.NavMeshBoundsVolume)]
    if not vols:
        emit({"ok":True,"ready":False,"map_name":map_name})
    else:
        minx=miny=1e18; maxx=maxy=-1e18
        for v in vols:
            o,ext=v.get_actor_bounds(False)
            minx=min(minx,o.x-ext.x); maxx=max(maxx,o.x+ext.x)
            miny=min(miny,o.y-ext.y); maxy=max(maxy,o.y+ext.y)
        cx=(minx+maxx)/2.0; cy=(miny+maxy)/2.0
        hx=(maxx-minx)/2.0*P["area_frac"]; hy=(maxy-miny)/2.0*P["area_frac"]
        big=unreal.Vector(max(hx,hy)+3000.0, max(hx,hy)+3000.0, 9000.0)
        probe=nav.project_point_to_navigation(world, unreal.Vector(cx,cy,0.0), None, None, big)
        if not isinstance(probe, unreal.Vector):
            unreal.SystemLibrary.execute_console_command(world, "RebuildNavigation")
            emit({"ok":True,"ready":False,"map_name":map_name})
        else:
            floor_z=probe.z
            pe=unreal.Vector(1500.0,1500.0,6000.0)
            def proj(x,y):
                r=nav.project_point_to_navigation(world, unreal.Vector(x,y,floor_z), None, None, pe)
                return r if isinstance(r, unreal.Vector) else None
            def rand_pt():
                for _ in range(60):
                    p=proj(cx+random.uniform(-hx,hx), cy+random.uniform(-hy,hy))
                    if p is not None: return p
                return None
            def path_between(a,b):
                path=nav.find_path_to_location_synchronously(world, a, b)
                if not path: return None
                pp=path.get_editor_property("path_points")
                if not pp or len(pp)<2: return None
                if math.sqrt((pp[-1].x-b.x)**2+(pp[-1].y-b.y)**2) > 150.0: return None  # partial/unreachable
                L=sum(math.sqrt((pp[j+1].x-pp[j].x)**2+(pp[j+1].y-pp[j].y)**2+(pp[j+1].z-pp[j].z)**2) for j in range(len(pp)-1))
                return [jv(p) for p in pp], L
            do_obj=(P["task_type"]=="objectnav")
            targets=[]
            if do_obj:
                for a in eas.get_all_level_actors():
                    if not isinstance(a, unreal.StaticMeshActor): continue
                    lbl=a.get_actor_label()
                    if any(lbl.startswith(p) for p in INFRA): continue
                    loc=a.get_actor_location()
                    if abs(loc.x-cx) > (maxx-minx)/2.0 or abs(loc.y-cy) > (maxy-miny)/2.0: continue
                    # semantic category: prefer the static-mesh asset name, else actor class
                    cls=a.get_class().get_name(); cat=cls
                    try:
                        smc=a.get_component_by_class(unreal.StaticMeshComponent)
                        sm=smc.get_editor_property("static_mesh") if smc else None
                        if sm: cat=sm.get_name()
                    except Exception: pass
                    targets.append((lbl, cls, cat, loc))
            abort = do_obj and not targets
            eps=[]; tries=0; maxtries=P["n_episodes"]*120
            while (not abort) and len(eps) < P["n_episodes"] and tries < maxtries:
                tries+=1
                a=rand_pt()
                if a is None: continue
                if do_obj:
                    lbl,cls,cat,oloc=random.choice(targets)
                    b=proj(oloc.x, oloc.y)            # nearest navmesh point to the object = view point
                    if b is None: continue
                else:
                    b=rand_pt()
                    if b is None: continue
                if math.sqrt((a.x-b.x)**2+(a.y-b.y)**2) < P["min_path_cm"]*0.5: continue
                res=path_between(a,b)
                if not res: continue
                pts,L=res
                if L < P["min_path_cm"] or L > P["max_path_cm"]: continue
                eu=math.sqrt((a.x-b.x)**2+(a.y-b.y)**2+(a.z-b.z)**2)
                ep={
                    "episode_id":"ep_%04d"%len(eps),
                    "task_type":P["task_type"],
                    "map":map_name,
                    "start_position":jv(a),
                    "goal_position":jv(b),
                    "start_heading_deg":heading(pts),
                    "gt_path":pts,
                    "geodesic_distance_cm":round(L,1),
                    "euclidean_distance_cm":round(eu,1),
                    "tortuosity":round(L/eu,3) if eu>1 else 1.0,
                }
                if do_obj:
                    ep["object_category"]=cat
                    ep["object_goal"]={"object_id":lbl,"object_type":cls,"object_category":cat,"position":jv(oloc),"view_point":jv(b)}
                eps.append(ep)
            out={"ok":True,"ready":True,"map_name":map_name,"floor_z":round(floor_z,1),"seed":P["seed"],
                 "area":[round(minx,1),round(miny,1),round(maxx,1),round(maxy,1)],
                 "n_requested":P["n_episodes"],"n_generated":len(eps),"tries":tries,"episodes":eps}
            if abort: out["warning"]="no target objects found in scene for ObjectNav"
            emit(out)
`).trim();
}

const _delay = (ms) => new Promise(r => setTimeout(r, ms));

// Orchestrate: setup (spawn volume + rebuild) → poll the sampler until the navmesh has
// finished building over ticks, then return the sampled episodes. Returns {ok,...}.
async function generateEpisodes(ueExecScript, _ueLogs, params) {
  const fs = require("fs");
  const os = require("os");
  const path = require("path");

  // Run a script that writes its JSON result to a fresh temp file; read + delete it.
  async function runUE(scriptFor, timeoutMs) {
    const fpath = path.join(os.tmpdir(), `sw_taskset_${Date.now()}_${Math.random().toString(36).slice(2, 8)}.json`);
    try { if (fs.existsSync(fpath)) fs.unlinkSync(fpath); } catch (_e) {}
    const r = await ueExecScript(scriptFor(fpath), timeoutMs);
    if (!r) return { ok: false, error: "UE did not respond (is the editor connected?)" };
    if (!fs.existsSync(fpath)) {
      const err = (r.result && (r.result.error || r.result.message)) || r.error || "";
      return { ok: false, error: "no output from UE" + (err ? (": " + err) : ""), raw: _ueLogs(r).slice(-400) };
    }
    try { const d = JSON.parse(fs.readFileSync(fpath, "utf-8")); try { fs.unlinkSync(fpath); } catch (_e) {} return d; }
    catch (e) { return { ok: false, error: "failed to read UE output: " + e.message }; }
  }

  // Resolve a seed once (so generation is reproducible + recorded); default time-derived.
  const seed = Number.isInteger(params.seed) ? params.seed : (Date.now() % 1000000007);
  const sampleParams = Object.assign({}, params, { seed });

  // Phase 1 — setup + trigger build.
  const setup = await runUE(buildSetupScript, 60000);
  if (!setup.ok) return setup;

  // Phase 2 — poll the sampler. Each call returns fast; the delay between calls is what lets
  // UE tick and finish the navmesh. ~30 polls × ~1.5s ≈ 45s budget for the build to complete.
  const MAX_POLLS = 30;
  for (let i = 0; i < MAX_POLLS; i++) {
    await _delay(i === 0 ? 1000 : 1500);
    const res = await runUE((fp) => buildSampleScript(sampleParams, fp), 180000);
    if (!res.ok) return res;                 // genuine script/UE error
    if (res.ready) { res.spawned_vol = setup.spawned_vol; res.seed = seed; return res; }   // navmesh built → episodes inside
  }
  return { ok: false, error: "navmesh did not finish building in time — the scene may lack a walkable floor with collision, or the area is too large", map_name: setup.map_name };
}

// Convert our compact stored episode → the canonical NavigationEpisode shape that
// nav_task/episode.py `from_dict` (and therefore `gym_env.batch_runner --episodes-file`)
// expects: {x,y,node_type} positions, reference_path.waypoints, seed, world, success_criteria,
// evaluation_metrics. The compact shape drives the UI; this is what makes EXPORT trainable.
function toCanonicalEpisode(ep, idx, record, bounds) {
  const NT = "navmesh";
  const pos = (a) => ({ x: a[0], y: a[1], node_type: NT });
  const geo = ep.geodesic_distance_cm;
  const p = record.params || {};
  const succ = Number(p.successRadiusM) > 0 ? Number(p.successRadiusM) * 100 : 100.0;
  const maxSteps = Number(p.maxSteps) > 0 ? Number(p.maxSteps) : 5000;
  const out = {
    schema_version: "1.0.0",
    tool: "simworld_studio_task_gen",
    generated_at: record.createdAt || new Date().toISOString(),
    seed: idx,
    episode_id: ep.episode_id,
    task_type: ep.task_type || record.taskType || "pointnav",
    world: { map_file: record.mapName || ep.map || "navmesh", coordinate_unit: "cm", bounds },
    start_position: pos(ep.start_position),
    goal_position: pos(ep.goal_position),
    reference_path: { waypoints: (ep.gt_path || []).map(pos), shortest_path_length_cm: geo },
    success_criteria: { success_distance_cm: succ, max_steps: maxSteps, max_episode_time_s: 300.0 },
    evaluation_metrics: { type: "Anderson2018", SR: { success_distance_cm: succ }, SPL: { shortest_path_length_cm: geo } },
  };
  if (Number.isFinite(ep.start_heading_deg)) out.start_heading_deg = ep.start_heading_deg;
  if (ep.difficulty) { out.difficulty = ep.difficulty; out.difficulty_score = ep.difficulty_score; }
  if (ep.object_goal) {
    const og = ep.object_goal;
    out.object_category = ep.object_category || og.object_category;
    out.object_goal = {
      object_id: og.object_id, object_type: og.object_type, object_category: og.object_category,
      position: { x: og.position[0], y: og.position[1], node_type: "object" },
      view_points: og.view_point ? [{ position: { x: og.view_point[0], y: og.view_point[1], node_type: "navmesh" } }] : [],
    };
  }
  return out;
}

// Build the training-ready export for a task set: a single JSON document in the "split file"
// shape `{episodes:[...]}` that `gym_env.batch_runner._load_episodes_file` consumes directly
// (it json.loads the whole file, not line-by-line). Episodes are the canonical schema.
function canonicalExport(record) {
  const eps = record.episodes || [];
  // world bounds from the episode extents (fallback to defaults in WorldConfig)
  const xs = [], ys = [];
  eps.forEach(e => { [e.start_position, e.goal_position, ...(e.gt_path || [])].forEach(pp => { if (pp) { xs.push(pp[0]); ys.push(pp[1]); } }); });
  const bounds = xs.length
    ? { x_min: Math.min(...xs), x_max: Math.max(...xs), y_min: Math.min(...ys), y_max: Math.max(...ys) }
    : { x_min: -9500, x_max: 9500, y_min: -9500, y_max: 9500 };
  return JSON.stringify({
    schema_version: "1.0.0",
    tool: "simworld_studio_task_gen",
    task_type: record.taskType,
    map: record.mapName,
    count: eps.length,
    episodes: eps.map((e, i) => toCanonicalEpisode(e, i, record, bounds)),
  });
}

// Register the Task-Generation REST surface on `app`.
function registerTaskRoutes(app, { ueExecScript, _ueLogs, taskSetManager }) {
  // POST /api/tasks/generate — build navmesh in the live scene + sample episodes, persist.
  app.post("/api/tasks/generate", async (req, res) => {
    const b = req.body || {};
    try {
      const result = await generateEpisodes(ueExecScript, _ueLogs, b);
      if (!result.ok) return res.status(502).json({ error: result.error || "generation failed", raw: result.raw });
      if (!result.episodes || result.episodes.length === 0) {
        const why = result.warning || "no episodes met the path-length filter — widen min/max or add reachable area";
        return res.status(422).json({ error: why, debug: result });
      }
      const record = await taskSetManager.save({
        name:     b.name || null,
        taskType: b.taskType || "pointnav",
        mapName:  result.map_name || b.mapName || null,
        sceneId:  b.sceneId || null,
        ownerId:  b.ownerId || null,
        params:   { episodes: b.episodes, minPathCm: b.minPathCm, maxPathCm: b.maxPathCm,
                    areaFrac: Number(b.areaFrac) > 0 ? Math.min(1, Number(b.areaFrac)) : 0.9,
                    seed: result.seed, successRadiusM: b.successRadiusM, maxSteps: b.maxSteps,
                    floorZ: result.floor_z, area: result.area },
        episodes: result.episodes,
      });
      res.status(201).json({ taskSet: record, generated: result.n_generated, requested: result.n_requested, tries: result.tries, seed: result.seed });
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });

  // GET /api/tasksets — list all generated task sets (metadata only).
  app.get("/api/tasksets", (req, res) => { res.json({ taskSets: taskSetManager.list() }); });

  // GET /api/tasksets/:id — one task set with its episodes (for the inspector).
  app.get("/api/tasksets/:id", (req, res) => {
    const rec = taskSetManager.load(req.params.id);
    if (!rec) return res.status(404).json({ error: "task set not found" });
    res.json(rec);
  });

  // GET /api/tasksets/:id/download — training-ready export: a single JSON doc in the canonical
  // NavigationEpisode schema, loadable via `python -m gym_env.batch_runner --episodes-file <file>`.
  // Pass ?raw=1 to instead download the compact stored JSONL used by the UI.
  app.get("/api/tasksets/:id/download", (req, res) => {
    if (req.query.raw) {
      const file = taskSetManager.episodesPath(req.params.id);
      if (!file) return res.status(404).json({ error: "task set not found" });
      return res.download(file, req.params.id + ".jsonl");
    }
    const rec = taskSetManager.load(req.params.id);
    if (!rec) return res.status(404).json({ error: "task set not found" });
    res.setHeader("Content-Type", "application/json");
    res.setHeader("Content-Disposition", `attachment; filename="${req.params.id}.json"`);
    res.send(canonicalExport(rec));
  });

  // DELETE /api/tasksets/:id
  app.delete("/api/tasksets/:id", (req, res) => {
    const r = taskSetManager.delete(req.params.id, req.query.ownerId || null);
    if (r === "forbidden") return res.status(403).json({ error: "Forbidden" });
    if (r === null) return res.status(404).json({ error: "task set not found" });
    res.json({ ok: true });
  });
}

module.exports = { buildSetupScript, buildSampleScript, generateEpisodes, toCanonicalEpisode, canonicalExport, registerTaskRoutes };

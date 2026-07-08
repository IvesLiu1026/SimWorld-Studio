"use strict";
// ── Deterministic executor (staged builder, Phase 1) ──────────────────────────
// Spawns the solved IR plan's `placed[]` entries DIRECTLY via server-side UE python — no builder LLM
// re-types coordinates (locked decision D1). Mirrors ir-ground._carpetScript: one python job per batch
// (<=120 spawns), each spawn in its own try/except, actor label := plan id (the join key used later by
// measurement, annotation marks, checkpoint manifests, and reflect ops), and a single JSON result
// marker line the caller parses. Failures are retried once, then reported as spawn_failed.
//
// The executor is I/O-only: it takes a `ueExec(script, timeoutMs)` function (index.js:ueExecScript)
// and a `ueLogs(result)` extractor (index.js:_ueLogs) injected by the orchestrator, so this module has
// no socket/UE coupling of its own and stays trivially testable.

const MARK = "SIMWORLD_STUDIO_JSON";

// Normalize a placed entry into the compact row the python loop consumes.
function _row(e) {
  const loc = Array.isArray(e.location) ? e.location : [0, 0, 0];
  let yaw = Number(e.yaw_deg || 0);
  if (!isFinite(yaw)) yaw = 0;
  const sc = (typeof e.scale === "number" && isFinite(e.scale) && e.scale > 0) ? e.scale : 1;
  const path = String(e.path || "");
  // Blueprint iff the retrieval tool says so or the path already carries the _C generated-class suffix.
  const isBP = /blueprint/i.test(String(e.spawnTool || "")) || /_C$/.test(path);
  return {
    id: String(e.id != null ? e.id : ""),
    path, bp: isBP,
    x: Number(loc[0] || 0), y: Number(loc[1] || 0), z: Number(loc[2] || 0),
    yaw, sc,
  };
}

// Build the python for one batch. Entries embedded as JSON (same technique as ckptRestoreManifest),
// so the whole batch is one execute_python_script job.
function buildBatchScript(entries) {
  const rows = entries.map(_row);
  const embedded = JSON.stringify(JSON.stringify(rows));
  return [
    "import unreal, json",
    "_eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)",
    "_mesh_cache = {}",
    "_cls_cache = {}",
    "def _load_mesh(p):",
    "    if p in _mesh_cache: return _mesh_cache[p]",
    "    o = None",
    "    try: o = unreal.EditorAssetLibrary.load_asset(p)",
    "    except Exception: o = None",
    "    _mesh_cache[p] = o; return o",
    "def _load_bp_class(p):",
    "    if p in _cls_cache: return _cls_cache[p]",
    "    cls = None",
    "    try: cls = unreal.EditorAssetLibrary.load_blueprint_class(p)",
    "    except Exception: cls = None",
    "    if cls is None:",
    "        cp = p if p.endswith('_C') else (p + '_C')",
    "        try: cls = unreal.load_object(None, cp)",
    "        except Exception: cls = None",
    "    _cls_cache[p] = cls; return cls",
    "_ITEMS = json.loads(" + embedded + ")",
    "_spawned = []; _failed = []",
    "for it in _ITEMS:",
    "    try:",
    "        loc = unreal.Vector(float(it['x']), float(it['y']), float(it['z']))",
    "        rot = unreal.Rotator(pitch=0.0, yaw=float(it['yaw']), roll=0.0)",
    "        a = None",
    "        if it['bp']:",
    "            cls = _load_bp_class(it['path'])",
    "            if cls is not None: a = _eas.spawn_actor_from_class(cls, loc, rot)",
    "        else:",
    "            m = _load_mesh(it['path'])",
    "            if m is not None: a = _eas.spawn_actor_from_object(m, loc, rot)",
    "        if a is None:",
    "            _failed.append({'id': it['id'], 'err': 'load_or_spawn_null'}); continue",
    "        try: a.set_actor_label(it['id'])",
    "        except Exception: pass",
    "        s = it.get('sc', 1.0)",
    "        if s and abs(float(s) - 1.0) > 1e-6:",
    "            try: a.set_actor_scale3d(unreal.Vector(float(s), float(s), float(s)))",
    "            except Exception: pass",
    "        _spawned.append(it['id'])",
    "    except Exception as _e:",
    "        _failed.append({'id': it.get('id', '?'), 'err': str(_e)[:140]})",
    "print('" + MARK + "=' + json.dumps({'spawned': _spawned, 'failed': _failed}))",
  ].join("\n");
}

// Run one batch and parse its result marker. Returns { spawned:[ids], failed:[{id,err}] }.
// A missing marker (timeout / editor crash) fails the WHOLE batch (all ids) so retry can re-attempt.
async function _runBatch(entries, ueExec, ueLogs, timeoutMs, log) {
  if (!entries.length) return { spawned: [], failed: [] };
  const script = buildBatchScript(entries);
  const r = await ueExec(script, timeoutMs);
  const logs = (typeof ueLogs === "function") ? (ueLogs(r) || "") : "";
  const m = logs.match(new RegExp(MARK + "=(.*)$", "m"));
  if (!m) {
    log(`ir-exec: no result marker for a ${entries.length}-entry batch (timeout/crash?)`);
    return { spawned: [], failed: entries.map(e => ({ id: String(e.id != null ? e.id : "?"), err: "no_marker" })) };
  }
  try {
    const j = JSON.parse(m[1]);
    return { spawned: Array.isArray(j.spawned) ? j.spawned : [], failed: Array.isArray(j.failed) ? j.failed : [] };
  } catch (e) {
    log("ir-exec: result marker parse failed: " + (e && e.message));
    return { spawned: [], failed: entries.map(e2 => ({ id: String(e2.id != null ? e2.id : "?"), err: "marker_parse" })) };
  }
}

// Spawn a flat list of entries in <=batchSize batches. Returns { spawned:Set<id>, failed:Map<id,err> }.
async function execEntries(entries, opts) {
  const o = opts || {};
  const ueExec = o.ueExec, ueLogs = o.ueLogs;
  const log = o.log || (() => {});
  const BATCH = Math.max(1, Number(o.batchSize || process.env.IR_EXEC_BATCH || 120));
  const timeoutMs = Math.max(30000, Number(o.timeoutMs || process.env.IR_EXEC_TIMEOUT_MS || 180000));
  const spawned = new Set();
  const failed = new Map();
  const nBatches = Math.ceil(entries.length / BATCH);
  for (let i = 0, b = 0; i < entries.length; i += BATCH, b++) {
    const batch = entries.slice(i, i + BATCH);
    const res = await _runBatch(batch, ueExec, ueLogs, timeoutMs, log);
    for (const id of res.spawned) { spawned.add(String(id)); failed.delete(String(id)); }
    for (const f of res.failed) { const id = String(f.id); if (!spawned.has(id)) failed.set(id, f.err || "unknown"); }
    log(`ir-exec batch ${b + 1}/${nBatches}: +${res.spawned.length} spawned, ${res.failed.length} failed (running: ${spawned.size} ok / ${failed.size} failed)`);
  }
  return { spawned, failed };
}

// Execute one tier: spawn all entries, then retry the failures ONCE (re-batched). Returns a report.
//   { tier, attempted, spawned:[ids], failed:[{id,err}], counts:{attempted,spawned,failed} }
async function execTier(entries, opts) {
  const o = opts || {};
  const log = o.log || (() => {});
  const tier = o.tier != null ? o.tier : null;
  const list = Array.isArray(entries) ? entries.filter(e => e && e.path) : [];
  if (!list.length) return { tier, attempted: 0, spawned: [], failed: [], counts: { attempted: 0, spawned: 0, failed: 0 } };
  const byId = new Map(list.map(e => [String(e.id), e]));

  const pass1 = await execEntries(list, o);
  let spawned = pass1.spawned;
  let failed = pass1.failed;

  // Retry-once: re-attempt only the failed entries (they may have hit a transient asset-load race).
  if (failed.size) {
    const retry = [...failed.keys()].map(id => byId.get(id)).filter(Boolean);
    log(`ir-exec tier ${tier}: retry-once ${retry.length} failed entr${retry.length === 1 ? "y" : "ies"}`);
    const pass2 = await execEntries(retry, o);
    for (const id of pass2.spawned) { spawned.add(id); failed.delete(id); }
    // keep only still-failing ids with their latest error
    for (const [id, err] of pass2.failed) { if (!spawned.has(id)) failed.set(id, err); }
  }

  const failedArr = [...failed.entries()].map(([id, err]) => ({ id, err }));
  const spawnedArr = [...spawned];
  log(`ir-exec tier ${tier}: DONE ${spawnedArr.length}/${list.length} spawned, ${failedArr.length} spawn_failed`);
  return {
    tier, attempted: list.length, spawned: spawnedArr, failed: failedArr,
    counts: { attempted: list.length, spawned: spawnedArr.length, failed: failedArr.length },
  };
}

// Delete all actors whose label starts with `prefix` (used to clean up calibration probes). Verifies
// the count is 0 afterwards (retries once) so a stale early-return can't leave probes polluting metrics.
async function deleteByPrefix(prefix, opts) {
  const o = opts || {};
  const ueExec = o.ueExec, ueLogs = o.ueLogs;
  const pfx = String(prefix);
  const script = [
    "import unreal",
    "_eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)",
    `_PFX = ${JSON.stringify(pfx)}`,
    "_n = 0",
    "for a in list(_eas.get_all_level_actors()):",
    "    try:",
    "        if a.get_actor_label().startswith(_PFX): _eas.destroy_actor(a); _n += 1",
    "    except Exception: pass",
    "unreal.SystemLibrary.collect_garbage()",
    "_left = sum(1 for a in _eas.get_all_level_actors() if a.get_actor_label().startswith(_PFX))",
    "print('SIMWORLD_DELETED=' + str(_n) + ' left=' + str(_left))",
  ].join("\n");
  let deleted = 0, left = -1;
  for (let attempt = 0; attempt < 3; attempt++) {
    const r = await ueExec(script, Number(o.timeoutMs || 60000));
    const logs = (typeof ueLogs === "function") ? (ueLogs(r) || "") : "";
    const m = logs.match(/SIMWORLD_DELETED=(\d+)\s+left=(\d+)/);
    if (m) { deleted += Number(m[1]); left = Number(m[2]); if (left === 0) break; }
  }
  return { deleted, left };
}

// Delete actors by exact label (reflect: remove / respawn-after-move-or-swap). Returns count deleted.
async function deleteByLabels(labels, opts) {
  const o = opts || {};
  const list = [...new Set((labels || []).map(String))];
  if (!list.length) return 0;
  const embedded = JSON.stringify(JSON.stringify(list));
  const script = [
    "import unreal, json",
    "_eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)",
    "_L = set(json.loads(" + embedded + "))",
    "_n = 0",
    "for a in list(_eas.get_all_level_actors()):",
    "    try:",
    "        if a.get_actor_label() in _L: _eas.destroy_actor(a); _n += 1",
    "    except Exception: pass",
    "unreal.SystemLibrary.collect_garbage()",
    "print('SIMWORLD_DELETED=' + str(_n))",
  ].join("\n");
  const r = await o.ueExec(script, Number(o.timeoutMs || 60000));
  const logs = (typeof o.ueLogs === "function") ? (o.ueLogs(r) || "") : "";
  const m = logs.match(/SIMWORLD_DELETED=(\d+)/);
  return m ? Number(m[1]) : 0;
}

// Run a raw python script server-side (used for the tier-0 ground carpet). Returns the UE logs string.
async function execRawScript(script, opts) {
  const o = opts || {};
  const ueExec = o.ueExec, ueLogs = o.ueLogs;
  const timeoutMs = Math.max(30000, Number(o.timeoutMs || 180000));
  const r = await ueExec(script, timeoutMs);
  return (typeof ueLogs === "function") ? (ueLogs(r) || "") : "";
}

module.exports = { buildBatchScript, execEntries, execTier, execRawScript, deleteByPrefix, deleteByLabels, MARK };

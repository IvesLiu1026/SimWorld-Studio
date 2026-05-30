"use strict";
/**
 * tasksets.js — artifact store for navigation **task sets** (the Task-Generation stage).
 *
 * A task set is a batch of navigation episodes (PointNav / ObjectNav) sampled from a live
 * scene's navmesh: each episode is a start + goal on the walkable surface plus the
 * ground-truth geodesic path (waypoints) and its length. This is the first pipeline link
 * after Scene generation — the engine (`nav_task/`) and the static datasets
 * (`datasets/diverse50/*.jsonl`) already use this schema; here we persist freshly-generated
 * sets so the UI can list / inspect / export / train on them.
 *
 * Mirrors scenes.js / checkpoints.js: a flat JSON store with an ownerId guard and a
 * per-file write lock. Lineage is recorded via `mapName` (the scene the navmesh was built
 * on) so a later Training run can trace back to its source scene.
 *
 * Layout:  <root>/tasksets/<id>/taskset.json    — metadata + params + summary + lineage
 *          <root>/tasksets/<id>/episodes.jsonl  — one episode per line (training-ready)
 */

const fs   = require("fs");
const path = require("path");

const DEFAULT_DIR = path.resolve(__dirname, "../../tasksets");

const _writeLocks = new Map();
function _withLock(key, fn) {
  const prev = _writeLocks.get(key) || Promise.resolve();
  const next = prev.then(fn).catch(fn);
  _writeLocks.set(key, next.then(() => { if (_writeLocks.get(key) === next) _writeLocks.delete(key); }));
  return next;
}

// Grade episodes by difficulty (geodesic distance × tortuosity — longer + windier = harder),
// then sort easy→hard so the Training stage can run them as a sequential curriculum. Tags each
// episode with difficulty (easy/medium/hard) + difficulty_score + order, and re-ids in order.
function _gradeDifficulty(episodes) {
  if (!episodes.length) return episodes;
  const scored = episodes.map(e => ({ e, s: (e.geodesic_distance_cm || 0) * (e.tortuosity || 1) }));
  scored.sort((a, b) => a.s - b.s);
  const n = scored.length;
  scored.forEach((o, i) => {
    const frac = i / n;
    o.e.difficulty = frac < 1 / 3 ? "easy" : frac < 2 / 3 ? "medium" : "hard";
    o.e.difficulty_score = Math.round(o.s);
    o.e.order = i;
    o.e.episode_id = "ep_" + String(i).padStart(4, "0");
  });
  return scored.map(o => o.e);
}

class TaskSetManager {
  constructor(dir) {
    this.dir = dir || DEFAULT_DIR;
    fs.existsSync(this.dir) || fs.mkdirSync(this.dir, { recursive: true });
  }

  // Persist a generated task set. `episodes` is an array of episode objects.
  save(data) {
    const id  = data.id || `ts_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 6)}`;
    const dir = path.join(this.dir, id);
    fs.existsSync(dir) || fs.mkdirSync(dir, { recursive: true });

    const episodes = _gradeDifficulty(Array.isArray(data.episodes) ? data.episodes : []);
    const dists = episodes.map(e => e.geodesic_distance_cm).filter(n => typeof n === "number");
    const diffCounts = episodes.reduce((a, e) => { a[e.difficulty] = (a[e.difficulty] || 0) + 1; return a; }, {});
    const summary = {
      episodeCount: episodes.length,
      minDistanceCm: dists.length ? Math.round(Math.min(...dists)) : 0,
      maxDistanceCm: dists.length ? Math.round(Math.max(...dists)) : 0,
      avgDistanceCm: dists.length ? Math.round(dists.reduce((a, b) => a + b, 0) / dists.length) : 0,
      difficulty: { easy: diffCounts.easy || 0, medium: diffCounts.medium || 0, hard: diffCounts.hard || 0 },
    };

    const record = {
      id,
      name:        data.name      || `Task Set ${new Date().toISOString().slice(0, 16).replace("T", " ")}`,
      taskType:    data.taskType  || "pointnav",
      mapName:     data.mapName   || null,   // lineage: the scene/map the navmesh was built on
      sceneId:     data.sceneId   || null,
      ownerId:     data.ownerId   || "_anon",
      params:      data.params    || {},
      summary,
      createdAt:   data.createdAt || new Date().toISOString(),
    };

    return _withLock(id, () => {
      fs.writeFileSync(path.join(dir, "taskset.json"), JSON.stringify(record, null, 2));
      const jsonl = episodes.map(e => JSON.stringify(e)).join("\n") + (episodes.length ? "\n" : "");
      fs.writeFileSync(path.join(dir, "episodes.jsonl"), jsonl);
      return record;
    });
  }

  list() {
    if (!fs.existsSync(this.dir)) return [];
    return fs.readdirSync(this.dir)
      .map(id => {
        try { return JSON.parse(fs.readFileSync(path.join(this.dir, id, "taskset.json"), "utf-8")); }
        catch (_e) { return null; }
      })
      .filter(Boolean)
      .sort((a, b) => (b.createdAt || "").localeCompare(a.createdAt || ""));
  }

  load(id) {
    if (!/^[A-Za-z0-9_\-]+$/.test(id || "")) return null;
    const file = path.join(this.dir, id, "taskset.json");
    if (!fs.existsSync(file)) return null;
    try {
      const record = JSON.parse(fs.readFileSync(file, "utf-8"));
      record.episodes = this.episodes(id);
      return record;
    } catch (_e) { return null; }
  }

  // Parse episodes.jsonl back into an array (used by load() and the inspector).
  episodes(id) {
    if (!/^[A-Za-z0-9_\-]+$/.test(id || "")) return [];
    const file = path.join(this.dir, id, "episodes.jsonl");
    if (!fs.existsSync(file)) return [];
    return fs.readFileSync(file, "utf-8").split("\n").filter(Boolean)
      .map(l => { try { return JSON.parse(l); } catch (_e) { return null; } })
      .filter(Boolean);
  }

  episodesPath(id) {
    if (!/^[A-Za-z0-9_\-]+$/.test(id || "")) return null;
    const file = path.join(this.dir, id, "episodes.jsonl");
    return fs.existsSync(file) ? file : null;
  }

  delete(id, ownerId) {
    if (!/^[A-Za-z0-9_\-]+$/.test(id || "")) return null;
    const dir = path.join(this.dir, id);
    if (!fs.existsSync(dir)) return null;
    try {
      const rec = JSON.parse(fs.readFileSync(path.join(dir, "taskset.json"), "utf-8"));
      if (ownerId && rec.ownerId && rec.ownerId !== "_anon" && rec.ownerId !== ownerId) return "forbidden";
    } catch (_e) {}
    fs.rmSync(dir, { recursive: true, force: true });
    return true;
  }
}

module.exports = { TaskSetManager };

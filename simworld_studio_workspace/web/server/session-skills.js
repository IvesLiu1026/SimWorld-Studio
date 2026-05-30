"use strict";
// Session-scoped, incrementally-updated skill library.
// Stores per-session helper "skills" (Python functions + metadata) under tmp/session-skills/<sid>/,
// assembles a deployable UE Python module + signature cards, and supports reuse/update/add across turns.
// Mirrors the lifecycle of CheckpointManager (clearSession / sweepIdle / clearAll).
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "../..");            // arena workspace root
const SKILLS_TMP = path.join(ROOT, "tmp", "session-skills");

// Base helpers prepended to every deployed module so generated skills can rely on them.
const MODULE_HEADER = `# AUTO-GENERATED session skill module — regenerated each turn. Do not hand-edit.
import unreal, math, random

def get_subsystem():
    """Editor actor subsystem (spawn/destroy/query level actors)."""
    return unreal.get_editor_subsystem(unreal.EditorActorSubsystem)

def delete_by_prefix(prefix):
    """Delete every actor whose label starts with prefix; returns count."""
    S = get_subsystem(); n = 0
    for a in list(S.get_all_level_actors()):
        if a.get_actor_label().startswith(prefix):
            S.destroy_actor(a); n += 1
    return n

_ASSET_SIZE_CACHE = {}
def asset_size(path):
    """Measure an asset's real world size (size_x, size_y, size_z) in cm by spawning a temporary
    probe far below the scene, reading its bounds, then deleting it (cached per path).
    ALWAYS use this for placement math — never hardcode an asset's width/length/footprint."""
    if path in _ASSET_SIZE_CACHE:
        return _ASSET_SIZE_CACHE[path]
    S = get_subsystem()
    a = None
    cls = unreal.load_object(None, path if path.endswith('_C') else path + '_C')
    if cls is not None:
        a = S.spawn_actor_from_class(cls, unreal.Vector(0.0, 0.0, -100000.0))
    else:
        m = unreal.load_asset(path)
        if m is not None:
            a = S.spawn_actor_from_object(m, unreal.Vector(0.0, 0.0, -100000.0))
    if a is None:
        return (0.0, 0.0, 0.0)
    _o, e = a.get_actor_bounds(False)
    S.destroy_actor(a)
    size = (e.x * 2.0, e.y * 2.0, e.z * 2.0)
    _ASSET_SIZE_CACHE[path] = size
    return size
`;

class SessionSkillManager {
  constructor() {
    this.lastTouch = new Map();
    try { fs.mkdirSync(SKILLS_TMP, { recursive: true }); } catch (_) {}
  }
  _safe(s) { return String(s || "default").replace(/[^a-zA-Z0-9_-]/g, "_"); }
  _safeName(n) { return String(n || "skill").replace(/[^a-zA-Z0-9_]/g, "_").replace(/^([0-9])/, "_$1"); }
  _dir(sid) { return path.join(SKILLS_TMP, this._safe(sid)); }
  _manifestPath(sid) { return path.join(this._dir(sid), "manifest.json"); }

  _load(sid) {
    try { return JSON.parse(fs.readFileSync(this._manifestPath(sid), "utf-8")); }
    catch (_) { return { skills: {}, lastTurn: 0, deployed: [] }; }
  }
  _save(sid, m) {
    try { fs.mkdirSync(this._dir(sid), { recursive: true }); fs.writeFileSync(this._manifestPath(sid), JSON.stringify(m, null, 2)); }
    catch (_) {}
  }

  /** Metadata (no code) of all current skills — fed to the skill-maker as the existing library. */
  getLibrary(sid) {
    const m = this._load(sid);
    return Object.values(m.skills).map(({ code, ...meta }) => meta);
  }
  hasSkills(sid) { return Object.keys(this._load(sid).skills).length > 0; }
  count(sid) { return Object.keys(this._load(sid).skills).length; }

  /** Merge a maker decision {reuse[], update[], add[]} into the session library. */
  applyUpdate(sid, upd, turn) {
    const m = this._load(sid); upd = upd || {};
    const added = [], updated = [], reused = [];
    const ingest = (s, isUpdate) => {
      if (!s || !s.name) return;
      const nm = this._safeName(s.name);
      const prev = m.skills[nm];
      if (!prev) {
        if (!s.code) return;
        m.skills[nm] = { name: nm, signature: s.signature || (nm + "()"), description: s.description || "", tags: Array.isArray(s.tags) ? s.tags : [], code: s.code, version: 1, createdTurn: turn, updatedTurn: turn };
        added.push(nm); return;
      }
      // existing: revise
      m.skills[nm] = { ...prev,
        signature: s.signature || prev.signature,
        description: s.description || prev.description,
        tags: Array.isArray(s.tags) && s.tags.length ? s.tags : prev.tags,
        code: s.code || prev.code,
        version: (prev.version || 1) + (s.code ? 1 : 0),
        updatedTurn: turn };
      (s.code ? updated : reused).push(nm);
    };
    for (const s of (upd.add || [])) ingest(s, false);
    for (const s of (upd.update || [])) ingest(s, true);
    for (const nm of (upd.reuse || [])) { const k = this._safeName(nm); if (m.skills[k]) reused.push(k); }
    m.lastTurn = turn; this._save(sid, m); this.touch(sid);
    return { added, updated, reused, total: Object.keys(m.skills).length };
  }

  /** Assemble the deployable module SOURCE + signature cards for the current library. */
  buildModule(sid, turn) {
    const m = this._load(sid);
    const skills = Object.values(m.skills);
    const moduleName = `sess_${this._safe(sid).slice(0, 8).replace(/-/g, "_")}_v${turn}_skills`;
    const parts = [MODULE_HEADER, "", `# ${skills.length} session skills (turn ${turn})`, ""];
    for (const s of skills) {
      parts.push(`# --- ${s.name} (v${s.version}): ${(s.description || "").replace(/\n/g, " ")}`);
      parts.push(String(s.code || "").trim());
      parts.push("");
    }
    const source = parts.join("\n");
    const cards = skills.length
      ? skills.map(s => `- ${s.signature}  —  ${(s.description || "").replace(/\n/g, " ")}`).join("\n")
      : "";
    if (!m.deployed) m.deployed = [];
    const prevModules = m.deployed.filter(x => x !== moduleName);
    if (!m.deployed.includes(moduleName)) m.deployed.push(moduleName);
    this._save(sid, m);
    return { moduleName, source, cards, count: skills.length, prevModules };
  }

  deployedModules(sid) { return this._load(sid).deployed || []; }

  /** Remove all session skill state; returns the deployed module names so the caller can delete the UE .py files. */
  clearSession(sid) {
    const mods = this.deployedModules(sid);
    try { fs.rmSync(this._dir(sid), { recursive: true, force: true }); } catch (_) {}
    this.lastTouch.delete(this._safe(sid));
    return mods;
  }
  touch(sid) { this.lastTouch.set(this._safe(sid), Date.now()); }
  sweepIdle(ttlMs) {
    const now = Date.now(); const dead = [];
    for (const [s, t] of this.lastTouch) {
      if (now - t > ttlMs) { try { fs.rmSync(path.join(SKILLS_TMP, s), { recursive: true, force: true }); } catch (_) {} this.lastTouch.delete(s); dead.push(s); }
    }
    return dead;
  }
  clearAll() {
    try { fs.rmSync(SKILLS_TMP, { recursive: true, force: true }); fs.mkdirSync(SKILLS_TMP, { recursive: true }); } catch (_) {}
    this.lastTouch.clear();
  }
}

module.exports = { SessionSkillManager };

"use strict";
/**
 * checkpoints.js — per-session scene checkpoint store with a branch tree.
 *
 * A checkpoint captures the scene at the end of a scene-changing chat turn as a
 * **scene manifest** (the list of spawned actors: class + transform + mesh) plus the
 * chat history up to that turn and metadata (parentId, prompt, turnIndex, thumbnail).
 *
 * Why a manifest and not a .umap snapshot: reloading a duplicated .umap via
 * LevelEditorSubsystem.load_level crashes the headless editor when Pixel Streaming is
 * active (the duplicated UWorld stays resident and trips UE's "old world not cleaned up"
 * fatal). Capturing/replaying actors needs no map load, never interrupts the stream, and
 * has no in-memory world leak — so it's the robust way to revert + branch.
 *
 * The parentId links form the branch tree: restoring a checkpoint and continuing creates
 * a new child, so siblings under one parent are parallel branches (ChatGPT/Claude-style).
 * Everything is keyed by sessionId with an ownerId guard (mirrors scenes.js) so multi-user
 * deployments stay isolated. Checkpoints are pure filesystem state — no UE assets — so
 * cleanup is a simple directory removal.
 *
 * Layout:  <root>/<sessionId>/meta.json            — tree + lightweight records
 *          <root>/<sessionId>/<id>.manifest.json   — captured scene actors (for restore)
 *          <root>/<sessionId>/<id>.chat.json        — chat history at that checkpoint
 *          <root>/<sessionId>/<id>.png              — thumbnail (optional)
 */

const fs   = require("fs");
const path = require("path");

const DEFAULT_DIR = path.resolve(__dirname, "../../checkpoints");

// Per-key async mutex — serializes writes to one session's meta file.
const _writeLocks = new Map();
function _withLock(key, fn) {
  const prev = _writeLocks.get(key) || Promise.resolve();
  const next = prev.then(fn).catch(fn);
  _writeLocks.set(key, next.then(() => { if (_writeLocks.get(key) === next) _writeLocks.delete(key); }));
  return next;
}

// Keep session ids safe as directory names.
function _sanitize(s) {
  return String(s || "_anon").replace(/[^A-Za-z0-9_]/g, "_").slice(0, 64) || "_anon";
}

class CheckpointManager {
  constructor(dir) {
    this.root = dir || DEFAULT_DIR;
    fs.existsSync(this.root) || fs.mkdirSync(this.root, { recursive: true });
  }

  _sessionDir(sessionId) { return path.join(this.root, _sanitize(sessionId)); }

  _loadMeta(sessionId) {
    const file = path.join(this._sessionDir(sessionId), "meta.json");
    if (!fs.existsSync(file)) return { sessionId, ownerId: null, lastActivity: Date.now(), checkpoints: {} };
    try { return JSON.parse(fs.readFileSync(file, "utf-8")); }
    catch { return { sessionId, ownerId: null, lastActivity: Date.now(), checkpoints: {} }; }
  }

  _writeMeta(sessionId, meta) {
    const dir = this._sessionDir(sessionId);
    fs.existsSync(dir) || fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, "meta.json"), JSON.stringify(meta, null, 2));
  }

  /** Record activity for the idle-TTL sweep. */
  touch(sessionId, ownerId) {
    return _withLock(_sanitize(sessionId), () => {
      const meta = this._loadMeta(sessionId);
      meta.lastActivity = Date.now();
      if (ownerId && !meta.ownerId) meta.ownerId = ownerId;
      this._writeMeta(sessionId, meta);
    });
  }

  /**
   * Create a checkpoint from a captured scene manifest.
   * @returns {Promise<object>} lightweight record
   */
  create({ sessionId, ownerId, parentId = null, prompt = "", turnIndex = 0, manifest = [], chatHistory = null, thumbnailSrcPath = null, messageId = null }) {
    const id = `c${turnIndex}_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 6)}`;
    return _withLock(_sanitize(sessionId), () => {
      const dir = this._sessionDir(sessionId);
      fs.existsSync(dir) || fs.mkdirSync(dir, { recursive: true });
      const meta = this._loadMeta(sessionId);
      if (ownerId && !meta.ownerId) meta.ownerId = ownerId;

      fs.writeFileSync(path.join(dir, `${id}.manifest.json`), JSON.stringify(manifest || []));
      if (chatHistory) { try { fs.writeFileSync(path.join(dir, `${id}.chat.json`), JSON.stringify(chatHistory)); } catch {} }
      let thumbnail = null;
      if (thumbnailSrcPath && fs.existsSync(thumbnailSrcPath)) {
        try { fs.copyFileSync(thumbnailSrcPath, path.join(dir, `${id}.png`)); thumbnail = `${id}.png`; } catch {}
      }

      const rec = {
        id,
        parentId:   parentId && meta.checkpoints[parentId] ? parentId : null,
        messageId:  messageId || null,
        prompt:     String(prompt || "").slice(0, 500),
        turnIndex,
        createdAt:  new Date().toISOString(),
        actorCount: Array.isArray(manifest) ? manifest.length : 0,
        thumbnail,
      };
      meta.checkpoints[id] = rec;
      meta.lastActivity = Date.now();
      this._writeMeta(sessionId, meta);
      return rec;
    });
  }

  /** List lightweight records for a session (owner-guarded). */
  list(sessionId, callerOwnerId = null) {
    const meta = this._loadMeta(sessionId);
    if (callerOwnerId && meta.ownerId && meta.ownerId !== callerOwnerId) return [];
    return Object.values(meta.checkpoints)
      .map(r => ({ ...r, thumbnailUrl: r.thumbnail ? `/api/checkpoints/${sessionId}/${r.id}/thumbnail` : null }))
      .sort((a, b) => new Date(a.createdAt) - new Date(b.createdAt));
  }

  /** Full record incl. chatHistory (owner-guarded). */
  get(sessionId, id, callerOwnerId = null) {
    const meta = this._loadMeta(sessionId);
    if (callerOwnerId && meta.ownerId && meta.ownerId !== callerOwnerId) return null;
    const rec = meta.checkpoints[id];
    if (!rec) return null;
    let chatHistory = null;
    const chatFile = path.join(this._sessionDir(sessionId), `${id}.chat.json`);
    if (fs.existsSync(chatFile)) { try { chatHistory = JSON.parse(fs.readFileSync(chatFile, "utf-8")); } catch {} }
    return { ...rec, chatHistory };
  }

  /** Scene manifest for a checkpoint (used to replay actors on restore). */
  getManifest(sessionId, id, callerOwnerId = null) {
    const meta = this._loadMeta(sessionId);
    if (callerOwnerId && meta.ownerId && meta.ownerId !== callerOwnerId) return null;
    if (!meta.checkpoints[id]) return null;
    const f = path.join(this._sessionDir(sessionId), `${id}.manifest.json`);
    if (!fs.existsSync(f)) return [];
    try { return JSON.parse(fs.readFileSync(f, "utf-8")); } catch { return []; }
  }

  getThumbnailPath(sessionId, id) {
    const p = path.join(this._sessionDir(sessionId), `${id}.png`);
    return fs.existsSync(p) ? p : null;
  }

  /** Delete one checkpoint; children re-parent to its parent so the tree stays connected. */
  delete(sessionId, id, callerOwnerId = null) {
    return _withLock(_sanitize(sessionId), () => {
      const meta = this._loadMeta(sessionId);
      if (callerOwnerId && meta.ownerId && meta.ownerId !== callerOwnerId) return "forbidden";
      const rec = meta.checkpoints[id];
      if (!rec) return null;
      for (const c of Object.values(meta.checkpoints)) if (c.parentId === id) c.parentId = rec.parentId;
      delete meta.checkpoints[id];
      this._writeMeta(sessionId, meta);
      const dir = this._sessionDir(sessionId);
      for (const f of [`${id}.manifest.json`, `${id}.chat.json`, `${id}.png`]) {
        try { fs.rmSync(path.join(dir, f), { force: true }); } catch {}
      }
      return true;
    });
  }

  /** Clear an entire session's checkpoints. */
  clearSession(sessionId, callerOwnerId = null) {
    return _withLock(_sanitize(sessionId), () => {
      const meta = this._loadMeta(sessionId);
      if (callerOwnerId && meta.ownerId && meta.ownerId !== callerOwnerId) return "forbidden";
      try { fs.rmSync(this._sessionDir(sessionId), { recursive: true, force: true }); } catch {}
      return true;
    });
  }

  allSessionIds() {
    if (!fs.existsSync(this.root)) return [];
    return fs.readdirSync(this.root).filter(d => {
      try { return fs.statSync(path.join(this.root, d)).isDirectory(); } catch { return false; }
    });
  }

  /** Remove sessions idle longer than ttlMs. Returns count removed. */
  sweepIdle(ttlMs) {
    const now = Date.now();
    let n = 0;
    for (const sid of this.allSessionIds()) {
      const meta = this._loadMeta(sid);
      if (now - (meta.lastActivity || 0) > ttlMs) {
        try { fs.rmSync(this._sessionDir(sid), { recursive: true, force: true }); n++; } catch {}
      }
    }
    return n;
  }

  /** Purge all checkpoint data (startup / server-stop). */
  clearAll() {
    try {
      if (fs.existsSync(this.root)) {
        for (const d of fs.readdirSync(this.root)) fs.rmSync(path.join(this.root, d), { recursive: true, force: true });
      }
    } catch {}
  }
}

module.exports = { CheckpointManager };

"use strict";
// ─────────────────────────────────────────────────────────────────────────────
// DataHub — the single data-serving abstraction for the studio backend.
//
// Why this exists: data used to be served two ways — one hardcoded `_gatherStatus`
// SSE snapshot (6 domains) plus ~65 ad-hoc REST endpoints, each consumed by a
// bespoke frontend fetch. Adding a domain meant editing several places and there
// was no shared contract, no caching, no per-domain change detection.
//
// The hub turns every renderable "data domain" into a registered provider:
//
//   hub.register("scene", { snapshot: () => ctxManager.getState(sid), changeSig });
//   hub.register("sceneSummary", { snapshot: async () => queryUE(), refreshMs: 15000,
//                                  lazy: true, fallback: null });
//
//   - SYNC provider  (no refreshMs): snapshot(ctx) read on every gather() — for cheap
//     in-memory reads. Returns its value directly.
//   - ASYNC provider (refreshMs > 0): snapshot() may be async and is refreshed on its
//     OWN timer into a cache; gather()/SSE read the cached value and NEVER block on it.
//     This is how slow sources (UE round-trips) become cheap to render.
//   - lazy: skip background refresh while no SSE client is connected (no idle UE load).
//
// One SSE push loop fans the full snapshot to all clients with per-domain change
// signatures (precise hash-gating → no wasted pushes). Pull routes are auto-derived:
//   GET /api/data            → full snapshot
//   GET /api/data/:domain    → one domain (instant, served from cache for async ones)
//
// Extending = register one provider. Nothing else changes.
// ─────────────────────────────────────────────────────────────────────────────
const crypto = require("crypto");

class DataHub {
  constructor(opts = {}) {
    this.providers = new Map(); // key -> { snapshot, changeSig, refreshMs, lazy, fallback }
    this.cache = new Map();     // key -> last value (async providers only)
    this.timers = new Map();    // key -> refresh interval handle
    this.clients = new Map();   // sse id -> { res, lastSeen }
    this.seq = 0;
    this.lastSig = "";
    this.pushIntervalMs = opts.pushIntervalMs || 3000;
    this.idleMs = opts.idleMs || 90000;
    this.keepaliveMs = opts.keepaliveMs || 9000;
    this.log = typeof opts.logger === "function" ? opts.logger : () => {};
  }

  /**
   * Register a data domain.
   * @param {string} key
   * @param {object} provider
   *   - snapshot(ctx): value | Promise<value>   (required)
   *   - changeSig(value): string                (optional; default = JSON length)
   *   - refreshMs: number                       (optional; >0 ⇒ async/cached provider)
   *   - lazy: boolean                           (optional; skip refresh when no clients)
   *   - fallback: any                           (optional; value before first refresh)
   */
  register(key, provider) {
    if (!key || !provider || typeof provider.snapshot !== "function") {
      throw new Error(`DataHub.register("${key}"): provider.snapshot() is required`);
    }
    const p = {
      snapshot: provider.snapshot,
      changeSig: typeof provider.changeSig === "function" ? provider.changeSig : null,
      refreshMs: provider.refreshMs > 0 ? provider.refreshMs : 0,
      lazy: !!provider.lazy,
      fallback: provider.fallback,
    };
    this.providers.set(key, p);
    if (p.refreshMs) {
      this.cache.set(key, p.fallback !== undefined ? p.fallback : null);
      this._refreshAsync(key);
      const t = setInterval(() => this._refreshAsync(key), p.refreshMs);
      if (t.unref) t.unref();
      this.timers.set(key, t);
    }
    return this;
  }

  /** Force a slow provider to refresh now (e.g. right after a scene-changing turn). */
  refresh(key) { return this._refreshAsync(key, true); }

  async _refreshAsync(key, force = false) {
    const p = this.providers.get(key);
    if (!p) return;
    if (!force && p.lazy && this.clients.size === 0) return; // background tick + nobody listening → skip the round-trip
    try {
      const v = await p.snapshot();
      if (v !== undefined) this.cache.set(key, v);
    } catch (e) {
      this.log(`[data-hub] "${key}" refresh failed: ${e.message}`);
    }
  }

  _value(key, ctx) {
    const p = this.providers.get(key);
    if (!p) return undefined;
    if (p.refreshMs) return this.cache.has(key) ? this.cache.get(key) : (p.fallback ?? null);
    try { return p.snapshot(ctx); }
    catch (e) { this.log(`[data-hub] "${key}" snapshot error: ${e.message}`); return p.fallback ?? null; }
  }

  /** Flat snapshot across every domain: { [key]: value }. */
  gather(ctx = {}) {
    const out = {};
    for (const key of this.providers.keys()) out[key] = this._value(key, ctx);
    return out;
  }

  /** One domain's current value (cached for async providers). */
  get(key, ctx = {}) { return this._value(key, ctx); }

  has(key) { return this.providers.has(key); }
  domains() { return [...this.providers.keys()]; }
  hasClients() { return this.clients.size > 0; }

  _signature(snapshot) {
    const parts = [];
    for (const [key, p] of this.providers) {
      const v = snapshot[key];
      if (p.changeSig) { try { parts.push(key + "=" + p.changeSig(v)); } catch { parts.push(key + "=?"); } }
      else parts.push(key + "#" + (v == null ? "0" : JSON.stringify(v).length));
    }
    return crypto.createHash("md5").update(parts.join("|")).digest("hex").slice(0, 12);
  }

  /**
   * Mount the streaming endpoint.
   * opts.connectCtx(): ctx for a new client's initial full snapshot.
   * opts.tickCtx():    ctx for each periodic push.
   * opts.afterTick(snapshot): advance shared state (e.g. chat `since`) after a push.
   */
  attachSSE(app, routePath, opts = {}) {
    const connectCtx = typeof opts.connectCtx === "function" ? opts.connectCtx : () => ({});
    const tickCtx = typeof opts.tickCtx === "function" ? opts.tickCtx : () => ({});
    const afterTick = typeof opts.afterTick === "function" ? opts.afterTick : null;

    app.get(routePath, (req, res) => {
      res.setHeader("Content-Type", "text/event-stream");
      res.setHeader("Cache-Control", "no-cache");
      res.setHeader("Connection", "keep-alive");
      res.setHeader("X-Accel-Buffering", "no");
      res.flushHeaders();
      res.write(`data: ${JSON.stringify(this.gather(connectCtx()))}\n\n`);
      const cid = "sse-" + (++this.seq);
      this.clients.set(cid, { res, lastSeen: Date.now() });
      req.on("close", () => this.clients.delete(cid));
    });

    const timer = setInterval(() => {
      if (this.clients.size === 0) return;
      const snapshot = this.gather(tickCtx());
      if (afterTick) { try { afterTick(snapshot); } catch (e) { this.log(`[data-hub] afterTick: ${e.message}`); } }
      const sig = this._signature(snapshot);
      const unchanged = sig === this.lastSig;
      this.lastSig = sig;
      const payload = `data: ${JSON.stringify(snapshot)}\n\n`;
      const now = Date.now();
      for (const [id, c] of this.clients) {
        if (now - c.lastSeen > this.idleMs) { this.clients.delete(id); try { c.res.end(); } catch {} continue; }
        if (unchanged && now - c.lastSeen < this.keepaliveMs) continue; // skip unchanged within keepalive window
        try { c.res.write(payload); c.lastSeen = now; } catch { this.clients.delete(id); }
      }
    }, this.pushIntervalMs);
    if (timer.unref) timer.unref();
    this._pushTimer = timer;
    return this;
  }

  /** Auto-derived pull routes: GET base (full) + GET base/:domain (one). */
  mountPullRoutes(app, base = "/api/data") {
    app.get(base, (req, res) => { res.set("Cache-Control", "no-store"); res.json(this.gather()); });
    app.get(base + "/:domain", (req, res) => {
      const key = req.params.domain;
      if (!this.providers.has(key)) return res.status(404).json({ error: `unknown data domain: ${key}`, domains: this.domains() });
      res.set("Cache-Control", "no-store");
      res.json({ domain: key, value: this.get(key) });
    });
    return this;
  }
}

module.exports = { DataHub };

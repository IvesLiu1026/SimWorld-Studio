// ─────────────────────────────────────────────────────────────────────────────
// dataClient — the single frontend data-access layer, paired with the server
// DataHub. One SSE connection fans out to per-domain stores; components read a
// domain with useDomain("scene") and get loading/staleness for free. Non-stream
// resources go through apiGet/apiPost (TTL cache + in-flight dedup), replacing
// the dozens of ad-hoc fetch() + useEffect + useState blocks scattered today.
//
//   const summary = useDomain("sceneSummary");        // live, pushed from the hub
//   const skills  = await apiGet("/skills", { ttl: 30000 });
//
// Back-compat: PollProvider subscribes via subscribeRaw() to the full snapshot,
// so its existing reducer (stale detection, chat dedup) keeps working unchanged
// while this module owns the one and only EventSource.
// ─────────────────────────────────────────────────────────────────────────────
import React from "react";

const API_BASE = "/api";
const CHATLOG_MAX = 200;

function chatKey(m) { return `${m.from}|${m.timestamp}|${(m.text || "").slice(0, 20)}`; }
function mergeChatLog(prev, incoming) {
  if (!Array.isArray(incoming) || incoming.length === 0) return prev || [];
  const base = Array.isArray(prev) ? prev : [];
  const seen = new Set(base.map(chatKey));
  const fresh = incoming.filter((m) => !seen.has(chatKey(m)));
  return fresh.length ? [...base, ...fresh].slice(-CHATLOG_MAX) : base;
}

class DataClient {
  constructor() {
    this.stores = new Map();      // domain -> { value, listeners:Set<fn> }
    this.rawListeners = new Set(); // fn(snapshot) — full-snapshot subscribers (PollProvider)
    this.lastSnapshot = null;
    this.es = null;
    this.started = false;
    this.pullCache = new Map();    // url -> { value, ts, promise }
  }

  _store(key) {
    let s = this.stores.get(key);
    if (!s) { s = { value: undefined, listeners: new Set() }; this.stores.set(key, s); }
    return s;
  }

  start() {
    if (this.started) return;
    this.started = true;
    const connect = () => {
      const token = (typeof sessionStorage !== "undefined" && sessionStorage.getItem("sw_session_token")) || "";
      const url = token ? `${API_BASE}/events?token=${token}` : `${API_BASE}/events`;
      let es;
      try { es = new EventSource(url); } catch { setTimeout(connect, 3000); return; }
      this.es = es;
      es.onmessage = (evt) => {
        let snap; try { snap = JSON.parse(evt.data); } catch { return; }
        this._ingest(snap);
      };
      es.onerror = () => { try { es.close(); } catch {} this.es = null; setTimeout(connect, 3000); };
    };
    connect();
  }

  _ingest(snap) {
    this.lastSnapshot = snap;
    for (const key of Object.keys(snap)) {
      const s = this._store(key);
      s.value = key === "chatLog" ? mergeChatLog(s.value, snap[key]) : snap[key];
      s.listeners.forEach((l) => { try { l(); } catch {} });
    }
    this.rawListeners.forEach((l) => { try { l(snap); } catch {} });
  }

  /** Subscribe to a single domain (used by useDomain). */
  subscribe(key, cb) {
    const s = this._store(key);
    s.listeners.add(cb);
    this.start();
    return () => s.listeners.delete(cb);
  }

  /** Subscribe to every full snapshot push (used by PollProvider's reducer). */
  subscribeRaw(cb) {
    this.rawListeners.add(cb);
    this.start();
    if (this.lastSnapshot) { try { cb(this.lastSnapshot); } catch {} }
    return () => this.rawListeners.delete(cb);
  }

  get(key) { return this._store(key).value; }

  /** GET a non-stream resource with optional TTL cache + in-flight dedup. */
  async apiGet(path, { ttl = 0 } = {}) {
    const url = path.startsWith("/api") ? path : `${API_BASE}${path}`;
    const now = Date.now();
    const hit = this.pullCache.get(url);
    if (hit) {
      if (hit.promise) return hit.promise;                       // in-flight → share it
      if (ttl > 0 && now - hit.ts < ttl) return hit.value;       // fresh enough
    }
    const promise = fetch(url)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`GET ${url} → ${r.status}`))))
      .then((value) => { this.pullCache.set(url, { value, ts: Date.now() }); return value; })
      .catch((e) => { this.pullCache.delete(url); throw e; });
    this.pullCache.set(url, { value: hit?.value, ts: hit?.ts || 0, promise });
    return promise;
  }

  async apiSend(path, method, body) {
    const url = path.startsWith("/api") ? path : `${API_BASE}${path}`;
    const r = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: body == null ? undefined : JSON.stringify(body) });
    if (!r.ok) throw new Error(`${method} ${url} → ${r.status}`);
    return r.json().catch(() => ({}));
  }
  apiPost(path, body) { return this.apiSend(path, "POST", body); }
  apiPatch(path, body) { return this.apiSend(path, "PATCH", body); }
  apiDelete(path, body) { return this.apiSend(path, "DELETE", body); }

  /** Invalidate a cached pull (e.g. after a mutation). */
  invalidate(path) { const url = path.startsWith("/api") ? path : `${API_BASE}${path}`; this.pullCache.delete(url); }
}

export const dataClient = new DataClient();

/**
 * Read a live data domain pushed by the server DataHub.
 * Returns `fallback` until the first snapshot for that domain arrives.
 */
export function useDomain(key, fallback = undefined) {
  const value = React.useSyncExternalStore(
    React.useCallback((cb) => dataClient.subscribe(key, cb), [key]),
    () => dataClient.get(key),
    () => undefined
  );
  return value === undefined ? fallback : value;
}

export function apiGet(path, opts) { return dataClient.apiGet(path, opts); }
export function apiPost(path, body) { return dataClient.apiPost(path, body); }
export function apiPatch(path, body) { return dataClient.apiPatch(path, body); }
export function apiDelete(path, body) { return dataClient.apiDelete(path, body); }

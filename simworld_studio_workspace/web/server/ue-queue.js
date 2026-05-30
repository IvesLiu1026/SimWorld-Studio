"use strict";
// ─────────────────────────────────────────────────────────────────────────────
// UeQueue — serializes ALL of this process's UE MCP commands onto one channel.
//
// UE exposes a single MCP TCP port and executes Python single-threaded. Opening
// several sockets at once (background refreshes racing the things the user
// triggers) causes connection churn ("client disconnected" thrash) and lets a
// slow background query (sceneSummary ~3-8s) delay an interactive action.
//
// This queue runs at most ONE command at a time, highest-priority first, and
// COALESCES duplicate background refreshes by key (a second request for an
// already-queued key just shares the in-flight promise). Interactive actions
// (reset/save/load) use a higher priority than background domain refreshes, so
// they jump ahead of the queued background work.
//
//   const r = await ueQueue.send({ type: "execute_python_script", params: {script} },
//                                { timeoutMs: 30000, priority: UeQueue.LOW, key: "sceneSummary" });
// ─────────────────────────────────────────────────────────────────────────────
const net = require("net");

class UeQueue {
  constructor({ host, port, logger } = {}) {
    this.host = host || "127.0.0.1";
    this.port = parseInt(port, 10) || 55559;
    this.log = typeof logger === "function" ? logger : () => {};
    this.pending = [];      // [{ priority, seq, key, fn, resolve, reject, promise }]
    this.byKey = new Map(); // key -> pending entry (for coalescing)
    this.running = false;
    this.seq = 0;
    this.inFlight = 0;
  }

  /** Run an arbitrary async fn on the serial channel. */
  run(fn, { priority = UeQueue.NORMAL, key = null } = {}) {
    if (key && this.byKey.has(key)) return this.byKey.get(key).promise; // coalesce
    let resolve, reject;
    const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
    const entry = { priority, seq: ++this.seq, key, fn, resolve, reject, promise };
    this.pending.push(entry);
    if (key) this.byKey.set(key, entry);
    this._drain();
    return promise;
  }

  /** Send one MCP command (JSON line) and resolve with the parsed reply (or null). */
  send(payload, { timeoutMs = 60000, priority = UeQueue.NORMAL, key = null } = {}) {
    return this.run(() => this._roundTrip(payload, timeoutMs), { priority, key });
  }

  _roundTrip(payload, timeoutMs) {
    return new Promise((resolve) => {
      const sock = new net.Socket();
      let done = false;
      const finish = (v) => { if (done) return; done = true; clearTimeout(timer); try { sock.destroy(); } catch {} resolve(v); };
      const timer = setTimeout(() => finish(null), timeoutMs);
      let buf = "";
      sock.connect(this.port, this.host, () => { sock.write(JSON.stringify(payload) + "\n"); });
      sock.on("data", (d) => { buf += d.toString(); try { finish(JSON.parse(buf)); } catch {} });
      sock.on("error", () => finish(null));
      sock.on("close", () => { if (!done) { try { finish(JSON.parse(buf)); } catch { finish(null); } } });
    });
  }

  async _drain() {
    if (this.running) return;
    this.running = true;
    try {
      while (this.pending.length) {
        // Pick highest priority, then FIFO by seq.
        this.pending.sort((a, b) => (b.priority - a.priority) || (a.seq - b.seq));
        const entry = this.pending.shift();
        if (entry.key) this.byKey.delete(entry.key);
        this.inFlight++;
        try { entry.resolve(await entry.fn()); }
        catch (e) { entry.reject(e); }
        finally { this.inFlight--; }
      }
    } finally { this.running = false; }
  }

  stats() { return { queued: this.pending.length, running: this.running, inFlight: this.inFlight }; }
}
UeQueue.LOW = 0;     // background domain refreshes
UeQueue.NORMAL = 5;  // default reads (snapshot after a turn)
UeQueue.HIGH = 10;   // user-initiated, interactive (reset / save-as / load-map)

module.exports = { UeQueue };

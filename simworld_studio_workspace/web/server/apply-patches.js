#!/usr/bin/env node
'use strict';
/**
 * apply-patches.js
 * Re-applies all P0/P1 patches to the minified index.js safely.
 * Run: node apply-patches.js
 * Run again: idempotent (checks for already-applied patches).
 */

const fs   = require('fs');
const path = require('path');
const { execSync } = require('child_process');

const FILE = path.join(__dirname, 'index.js');
// Normalize to LF for reliable string matching (write back at end)
let src = fs.readFileSync(FILE, 'utf-8').replace(/\r\n/g, '\n');

let changed = 0;

function patch(description, oldStr, newStr) {
  if (src.includes(newStr.slice(0, 40))) {
    console.log(`  [SKIP] already applied: ${description}`);
    return;
  }
  if (!src.includes(oldStr)) {
    console.error(`  [FAIL] Pattern not found: ${description}`);
    console.error(`  Pattern (first 80 chars): ${JSON.stringify(oldStr.slice(0, 80))}`);
    process.exit(1);
  }
  src = src.replace(oldStr, newStr);
  changed++;
  console.log(`  [OK]   ${description}`);
}

console.log('Applying patches to index.js...\n');

// ── P0-1: chatProcs Map (replaces global activeChatProc) ─────────────────────
patch(
  'P0-1: chatProcs Map — global let activeChatProc → Map declaration',
  `// Tracks the currently-running scene chat subprocess so /api/chat-stop can kill it.
// Single-tenant: only one scene chat runs at a time per studio session.
let activeChatProc=null;
app.post("/api/chat-stop",(s,e)=>{
  if(activeChatProc&&!activeChatProc.killed){
    try{activeChatProc.kill("SIGTERM");logToFile("claude","Scene chat stopped by user")}catch{}
    activeChatProc=null;
    return e.json({stopped:true});
  }
  e.json({stopped:false});
});`,
  `// P0-1: Per-session subprocess tracking (was single global activeChatProc).
// Key = sessionId. Each user gets their own isolated Claude subprocess.
const _chatProcs = new Map(); // sessionId -> ChildProcess

// Simple async queue: max N concurrent Claude invocations.
const _MAX_CHAT_CONCURRENCY = parseInt(process.env.MAX_CONCURRENT_CHATS || '10', 10);
let _chatRunning = 0;
const _chatWaitQ  = [];
function _enqueueChat(fn) {
  return new Promise((res, rej) => {
    const run = () => {
      _chatRunning++;
      Promise.resolve().then(fn).then(res, rej).finally(() => {
        _chatRunning--;
        if (_chatWaitQ.length) _chatWaitQ.shift()();
      });
    };
    if (_chatRunning < _MAX_CHAT_CONCURRENCY) run();
    else if (_chatWaitQ.length > 200) rej(Object.assign(new Error('Server busy'), { code: 'QUEUE_FULL' }));
    else _chatWaitQ.push(run);
  });
}

app.post("/api/chat-stop",(req,res)=>{
  const sid = req.body && req.body.sessionId || req.query && req.query.sessionId || '_global';
  const proc = _chatProcs.get(sid);
  if(proc && !proc.killed){
    try{ proc.kill('SIGTERM'); logToFile('claude', 'Chat stopped by user session='+sid); }catch{}
    _chatProcs.delete(sid);
    return res.json({stopped:true});
  }
  res.json({stopped:false});
});`
);

// ── P0-2: SSE hub — Map with heartbeat + dead-client eviction ────────────────
patch(
  'P0-2: SSE hub — Set → Map with heartbeat',
  `// SSE endpoint — client opens ONE persistent connection, server pushes every 3s
const _sseClients=new Set();
app.get("/api/events",(req,res)=>{
  res.setHeader("Content-Type","text/event-stream");
  res.setHeader("Cache-Control","no-cache");
  res.setHeader("Connection","keep-alive");
  res.setHeader("X-Accel-Buffering","no");
  res.flushHeaders();
  // Send initial snapshot immediately
  const initial=_gatherStatus(0);
  res.write(\`data: \${JSON.stringify(initial)}\\n\\n\`);
  _sseClients.add(res);
  req.on("close",()=>{_sseClients.delete(res)});
});
// Push status to all SSE clients every 3s
let _sseSince=0;
setInterval(()=>{
  if(_sseClients.size===0)return;
  const snapshot=_gatherStatus(_sseSince);
  if(snapshot.chatLog?.length>0) _sseSince=Math.max(...snapshot.chatLog.map(m=>m.timestamp));
  const payload=\`data: \${JSON.stringify(snapshot)}\\n\\n\`;
  for(const client of _sseClients){
    try{client.write(payload)}catch{_sseClients.delete(client)}
  }
},3000);`,
  `// P0-2: SSE hub — Map<clientId, {res, lastSeen}> with heartbeat eviction.
const _sseClients = new Map();
let _sseSeq = 0;
const _SSE_IDLE_MS = 90000;

app.get("/api/events", (req, res) => {
  res.setHeader("Content-Type",  "text/event-stream");
  res.setHeader("Cache-Control", "no-cache");
  res.setHeader("Connection",    "keep-alive");
  res.setHeader("X-Accel-Buffering", "no");
  res.flushHeaders();
  const cid = 'sse-' + (++_sseSeq);
  _sseClients.set(cid, { res, lastSeen: Date.now() });
  try {
    const init = _gatherStatus(0);
    res.write('data: ' + JSON.stringify(init) + '\\n\\n');
  } catch {}
  req.on("close", () => _sseClients.delete(cid));
});

let _sseSince = 0;
setInterval(() => {
  if (_sseClients.size === 0) return;
  const snapshot = _gatherStatus(_sseSince);
  if (snapshot.chatLog && snapshot.chatLog.length > 0)
    _sseSince = Math.max(...snapshot.chatLog.map(m => m.timestamp));
  const payload = 'data: ' + JSON.stringify(snapshot) + '\\n\\n';
  const now = Date.now();
  for (const [id, client] of _sseClients) {
    if (now - client.lastSeen > _SSE_IDLE_MS) {
      _sseClients.delete(id);
      try { client.res.end(); } catch {}
      continue;
    }
    try { client.res.write(payload); client.lastSeen = now; }
    catch { _sseClients.delete(id); }
  }
  // Keepalive ping
  for (const [, client] of _sseClients) {
    try { client.res.write(': ping\\n\\n'); } catch {}
  }
}, 3000);`
);

// ── P0-3: chatProcs usage in chat endpoint — use n not S ─────────────────────
patch(
  'P0-3: chatProc tracking inside chat endpoint',
  `if(activeChatProc&&!activeChatProc.killed){try{activeChatProc.kill("SIGTERM")}catch{}}activeChatProc=g;`,
  `// P0-3: per-session proc (n = sessionId from req.body, declared above).
const _prevProc = _chatProcs.get(n || '_global');
if (_prevProc && !_prevProc.killed) { try { _prevProc.kill('SIGTERM'); } catch {} }
_chatProcs.set(n || '_global', g);`
);

// Remove stale activeChatProc reference in close handler
patch(
  'P0-3b: remove stale activeChatProc=null in close handler',
  `if(activeChatProc===g)activeChatProc=null;`,
  `// P0-3b: cleanup via _chatProcs.delete() happens in g.on("exit") above.`
);

// Add g.on("exit") cleanup right after the chatProcs.set line
patch(
  'P0-3c: add exit handler to clean chatProcs Map',
  `_chatProcs.set(n || '_global', g);`,
  `_chatProcs.set(n || '_global', g);
g.on('exit', () => _chatProcs.delete(n || '_global'));`
);

// ── P1-4: Arena SSE 5-minute timeout ─────────────────────────────────────────
// Find the exact arena run handler dynamically
const arenaOld = (() => {
  const start = src.indexOf('app.post("/api/arena/run"');
  if (start === -1) return '';
  // The arena handler is always followed by ,app.get("/api/assets")
  const endMarker = '),app.get("/api/assets"';
  const endIdx = src.indexOf(endMarker, start);
  if (endIdx === -1) return '';
  return src.slice(start, endIdx + 1); // +1 to include closing )
})();

if (!arenaOld || arenaOld.includes('P1-4')) {
  console.log('  [SKIP] already applied: P1-4: Arena SSE 5-min timeout');
} else {
  const arenaNew = `app.post("/api/arena/run", async (s, e) => {
  const { prompt: t, skills: n } = s.body;
  if (!t) return e.status(400).json({ error: "prompt required" });
  const o = arenaManager.createBattle(t, n || []);
  e.setHeader("Content-Type", "text/event-stream");
  e.setHeader("Cache-Control", "no-cache");
  e.setHeader("Connection",    "keep-alive");
  e.flushHeaders();
  function i(a, c) { e.writableEnded || e.write('event: ' + a + '\\ndata: ' + JSON.stringify(c) + '\\n\\n'); }
  i("battle_created", { battleId: o.id, prompt: t });
  // P1-4: 5-minute hard timeout
  const aAbort = new AbortController();
  const aTimer = setTimeout(() => { aAbort.abort(); i("error", { message: "Arena timed out (5 min)" }); if (!e.writableEnded) e.end(); }, 5 * 60 * 1000);
  e.on("close", () => { clearTimeout(aTimer); aAbort.abort(); });
  try {
    const a = await agentManager.runBattle(t, n || [], ARENA_SYSTEM_PROMPT, (m, _) => i("progress", { phase: m, ..._ }));
    if (!aAbort.signal.aborted) {
      clearTimeout(aTimer);
      arenaManager.submitSceneForBattle(o.id, "a", a.side_a);
      arenaManager.submitSceneForBattle(o.id, "b", a.side_b);
      i("complete", arenaManager.getBattle(o.id));
    }
  } catch (a) {
    if (!aAbort.signal.aborted) { clearTimeout(aTimer); i("error", { message: a.message }); }
  }
  if (!e.writableEnded) e.end();
})`;
  src = src.replace(arenaOld, arenaNew);
  changed++;
  console.log('  [OK]   P1-4: Arena SSE 5-min timeout');
}


// ── P2-8: Agent busy guard ────────────────────────────────────────────────────
patch(
  'P2-8: Agent concurrent command guard',
  `  const agent=agentCtrl.get(agentName);
  if(!agent){logToFile("agent-chat",\`AGENT NOT FOUND: "\${agentName}"\`);return e.status(404).json({error:\`Agent "\${agentName}" not found in scene\`})}`,
  `  const agent=agentCtrl.get(agentName);
  if(!agent){logToFile("agent-chat",\`AGENT NOT FOUND: "\${agentName}"\`);return e.status(404).json({error:\`Agent "\${agentName}" not found in scene\`})}
  // P2-8: reject concurrent commands
  if(agent.status==="running"){return e.status(409).json({error:\`Agent "\${agentName}" is busy. Wait for current action to finish.\`,code:"AGENT_BUSY"})}`
);

// ── Scene routes fix — await async save/delete ────────────────────────────────
patch(
  'Scenes: await async save()',
  'app.post("/api/scenes",(s,e)=>{const t=sceneManager.save(s.body);e.json(t)})',
  'app.post("/api/scenes",async(s,e)=>{try{const t=await sceneManager.save(s.body);e.status(201).json(t)}catch(err){e.status(500).json({error:err.message})}})'
);

patch(
  'Scenes: await async delete() with ownerId',
  'app.delete("/api/scenes/:id",(s,e)=>{const t=sceneManager.delete(s.params.id);e.json({ok:t})})',
  'app.delete("/api/scenes/:id",async(s,e)=>{const own=s.query.ownerId||null;try{const t=await sceneManager.delete(s.params.id,own);if(t==="forbidden")return e.status(403).json({error:"Forbidden"});e.json({ok:t})}catch(err){e.status(500).json({error:err.message})}})'
);

// ── Session routes — insert BEFORE catch-all 404 handler ─────────────────────
const SESSION_ROUTES = `
// ── Session Management Routes (P0) ───────────────────────────────────────────
let _sessionMgr = null;
try {
  const { sessionManager } = require('./session-manager');
  _sessionMgr = sessionManager;
  _sessionMgr.on('acquired', ({ token, slotId, userId }) => logToFile('session', 'acquired slot=' + slotId + ' user=' + userId));
  _sessionMgr.on('released', ({ token, slotId, reason }) => logToFile('session', 'released slot=' + slotId + ' reason=' + reason));
} catch (e) {
  logToFile('session', 'session-manager unavailable (single-user mode): ' + e.message);
}

app.post('/api/session/acquire', async (req, res) => {
  if (!_sessionMgr) return res.json({ token: '_dev', slotId: 0, totalSlots: 1, freeSlots: 1, sessionTtlMs: 0, dev: true });
  const userId = req.ip || 'anon';
  try {
    const rec = await _sessionMgr.acquire(userId);
    res.json({ token: rec.token, slotId: rec.slotId, uePorts: rec.uePorts, totalSlots: _sessionMgr.totalSlots, freeSlots: _sessionMgr.freeSlots, sessionTtlMs: parseInt(process.env.SESSION_TTL_MS || '1800000', 10) });
  } catch (e) {
    res.status(503).json({ error: e.message, code: e.code || 'UNAVAILABLE', queueLength: _sessionMgr.queueLength });
  }
});

app.post('/api/session/heartbeat', (req, res) => {
  if (!_sessionMgr) return res.json({ ok: true, dev: true });
  const token = (req.headers['x-session-token'] || req.body && req.body.token || '').trim();
  const rec = token ? _sessionMgr.touch(token) : null;
  if (!rec) return res.status(401).json({ error: 'Session expired or invalid' });
  res.json({ ok: true, slotId: rec.slotId, idleMs: Date.now() - rec.lastActivity });
});

app.post('/api/session/release', (req, res) => {
  if (_sessionMgr) {
    const token = (req.headers['x-session-token'] || req.body && req.body.token || '').trim();
    if (token) _sessionMgr.release(token);
  }
  res.json({ ok: true });
});

app.get('/api/session/status', (req, res) => {
  if (!_sessionMgr) return res.json({ mode: 'single-user' });
  res.json({ totalSlots: _sessionMgr.totalSlots, freeSlots: _sessionMgr.freeSlots, activeSessions: _sessionMgr.activeSessions, queueLength: _sessionMgr.queueLength, sessions: _sessionMgr.snapshot() });
});

`;

const CATCH_ALL = `app.all("/api/*",(s,e)=>{e.status(404).json({error:\`Unknown API endpoint: \${s.method} \${s.path}\`})});`;

if (src.includes(SESSION_ROUTES.trim().slice(0, 50))) {
  console.log('  [SKIP] already applied: session routes');
} else if (src.includes(CATCH_ALL)) {
  src = src.replace(CATCH_ALL, SESSION_ROUTES + CATCH_ALL);
  changed++;
  console.log('  [OK]   session routes (inserted before catch-all)');
} else {
  console.error('  [FAIL] catch-all 404 handler not found');
  process.exit(1);
}

// ── Write + verify ────────────────────────────────────────────────────────────
fs.writeFileSync(FILE, src);

try {
  execSync(`node --check "${FILE}"`, { stdio: 'pipe' });
  console.log('\nSyntax check: PASSED');
} catch (e) {
  console.error('\nSyntax check: FAILED');
  console.error(e.stderr.toString());
  process.exit(1);
}

console.log('\nPatches applied:', changed, '| Total file size:', src.length, 'bytes');
if (changed === 0) console.log('(all patches already applied)');

'use strict';
// patch-index.js — Apply all P0/P1 patches to index.js safely.
// Run: node patch-index.js  (idempotent)

const fs   = require('fs');
const path = require('path');
const { execSync } = require('child_process');

const FILE = path.join(__dirname, 'index.js');

function check(label) {
  try { execSync('node --check "' + FILE + '"', { stdio: 'pipe' }); console.log('  ✓', label); return true; }
  catch(e) { console.error('  ✗', label, '—', e.stderr.toString().split('\n')[0]); return false; }
}

function apply(label, oldStr, newStr, src) {
  if (src.includes(newStr.slice(0, 60))) { console.log('  · (skip)', label); return src; }
  if (!src.includes(oldStr)) { console.error('  ✗ NOT FOUND:', label); return src; }
  return src.replace(oldStr, newStr);
}

let src = fs.readFileSync(FILE, 'utf-8').replace(/\r\n/g, '\n');

// ── P0-1: chatProcs Map ───────────────────────────────────────────────────────
src = apply('P0-1 chatProcs Map',
  '// Tracks the currently-running scene chat subprocess so /api/chat-stop can kill it.\n// Single-tenant: only one scene chat runs at a time per studio session.\nlet activeChatProc=null;\napp.post("/api/chat-stop",(s,e)=>{\n  if(activeChatProc&&!activeChatProc.killed){\n    try{activeChatProc.kill("SIGTERM");logToFile("claude","Scene chat stopped by user")}catch{}\n    activeChatProc=null;\n    return e.json({stopped:true});\n  }\n  e.json({stopped:false});\n});',
  '// P0-1: per-session subprocess tracking\nconst _chatProcs=new Map();\napp.post("/api/chat-stop",(req,res)=>{\n  const sid=(req.body&&req.body.sessionId)||(req.query&&req.query.sessionId)||"_global";\n  const p=_chatProcs.get(sid);\n  if(p&&!p.killed){try{p.kill("SIGTERM")}catch{};_chatProcs.delete(sid);return res.json({stopped:true});}\n  res.json({stopped:false});\n});',
  src
);
fs.writeFileSync(FILE, src); check('P0-1');

// ── P0-2a: SSE Set → Map declaration ─────────────────────────────────────────
src = apply('P0-2a SSE Map decl',
  'const _sseClients=new Set();',
  'const _sseClients=new Map();let _sseSeq=0;const _SSE_IDLE_MS=90000;',
  src
);
fs.writeFileSync(FILE, src); check('P0-2a');

// ── P0-2b: SSE client registration (add→set) ─────────────────────────────────
src = apply('P0-2b SSE registration',
  '_sseClients.add(res);\n  req.on("close",()=>{_sseClients.delete(res)});',
  'const cid="sse-"+(++_sseSeq);\n  _sseClients.set(cid,{res,lastSeen:Date.now()});\n  req.on("close",()=>_sseClients.delete(cid));',
  src
);
fs.writeFileSync(FILE, src); check('P0-2b');

// ── P0-2c: SSE push loop ─────────────────────────────────────────────────────
src = apply('P0-2c SSE push loop',
  'for(const client of _sseClients){\n    try{client.write(payload)}catch{_sseClients.delete(client)}\n  }',
  'const now=Date.now();\n  for(const [id,c] of _sseClients){\n    if(now-c.lastSeen>_SSE_IDLE_MS){_sseClients.delete(id);try{c.res.end()}catch{};continue;}\n    try{c.res.write(payload);c.lastSeen=now;}catch{_sseClients.delete(id);}\n  }',
  src
);
fs.writeFileSync(FILE, src); check('P0-2c');

// ── P0-3: chatProc tracking in /api/chat ─────────────────────────────────────
src = apply('P0-3 chatProc in chat',
  'if(activeChatProc&&!activeChatProc.killed){try{activeChatProc.kill("SIGTERM")}catch{}}activeChatProc=g;',
  'const _pp=_chatProcs.get(n||"_global");if(_pp&&!_pp.killed){try{_pp.kill("SIGTERM")}catch{}}\n_chatProcs.set(n||"_global",g);\ng.on("exit",()=>_chatProcs.delete(n||"_global"));',
  src
);
// Remove stale close-handler reference
if (src.includes('if(activeChatProc===g)activeChatProc=null;')) {
  src = src.replace('if(activeChatProc===g)activeChatProc=null;', '/* P0-3: cleaned up via g.on(exit) */');
}
fs.writeFileSync(FILE, src); check('P0-3');

// ── Scenes: await async save/delete ──────────────────────────────────────────
src = apply('Scenes save async',
  'app.post("/api/scenes",(s,e)=>{const t=sceneManager.save(s.body);e.json(t)})',
  'app.post("/api/scenes",async(s,e)=>{try{const t=await sceneManager.save(s.body);e.status(201).json(t)}catch(err){e.status(500).json({error:err.message})}})',
  src
);
src = apply('Scenes delete async',
  'app.delete("/api/scenes/:id",(s,e)=>{const t=sceneManager.delete(s.params.id);e.json({ok:t})})',
  'app.delete("/api/scenes/:id",async(s,e)=>{const own=s.query.ownerId||null;try{const t=await sceneManager.delete(s.params.id,own);if(t==="forbidden")return e.status(403).json({error:"Forbidden"});e.json({ok:t})}catch(err){e.status(500).json({error:err.message})}})',
  src
);
fs.writeFileSync(FILE, src); check('Scenes routes');

// ── Agent busy guard ──────────────────────────────────────────────────────────
const AGENT_OLD = '  const agent=agentCtrl.get(agentName);\n  if(!agent){logToFile("agent-chat",`AGENT NOT FOUND: "${agentName}"`);return e.status(404).json({error:`Agent "${agentName}" not found in scene`})}';
const AGENT_NEW = AGENT_OLD + '\n  if(agent.status==="running"){return e.status(409).json({error:"Agent "+agentName+" is busy.",code:"AGENT_BUSY"})}';
src = apply('Agent busy guard', AGENT_OLD, AGENT_NEW, src);
fs.writeFileSync(FILE, src); check('Agent guard');

// ── Session routes (before catch-all) ────────────────────────────────────────
// Use a string constant to avoid template literal issues
const SESS_ROUTES = [
  '',
  '// ── Session Routes ──────────────────────────────────────────────────────────',
  "let _sessionMgr=null;",
  "try{const{sessionManager}=require('./session-manager');_sessionMgr=sessionManager;}",
  "catch(e){logToFile('session','session-manager not loaded: '+e.message);}",
  '',
  "app.post('/api/session/acquire',async(req,res)=>{",
  "  if(!_sessionMgr)return res.json({token:'_dev',slotId:0,totalSlots:1,freeSlots:1,sessionTtlMs:0,dev:true});",
  "  const userId=req.ip||'anon';",
  "  try{",
  "    const rec=await _sessionMgr.acquire(userId);",
  "    res.json({token:rec.token,slotId:rec.slotId,uePorts:rec.uePorts,totalSlots:_sessionMgr.totalSlots,freeSlots:_sessionMgr.freeSlots,sessionTtlMs:parseInt(process.env.SESSION_TTL_MS||'1800000',10)});",
  "  }catch(e){res.status(503).json({error:e.message,code:e.code||'UNAVAILABLE',queueLength:_sessionMgr.queueLength});}",
  "});",
  '',
  "app.post('/api/session/heartbeat',(req,res)=>{",
  "  if(!_sessionMgr)return res.json({ok:true,dev:true});",
  "  const tok=(req.headers['x-session-token']||req.body&&req.body.token||'').trim();",
  "  const rec=tok?_sessionMgr.touch(tok):null;",
  "  if(!rec)return res.status(401).json({error:'Session expired or invalid'});",
  "  res.json({ok:true,slotId:rec.slotId,idleMs:Date.now()-rec.lastActivity});",
  "});",
  '',
  "app.post('/api/session/release',(req,res)=>{",
  "  if(_sessionMgr){const tok=(req.headers['x-session-token']||req.body&&req.body.token||'').trim();if(tok)_sessionMgr.release(tok);}",
  "  res.json({ok:true});",
  "});",
  '',
  "app.get('/api/session/status',(req,res)=>{",
  "  if(!_sessionMgr)return res.json({mode:'single-user'});",
  "  res.json({totalSlots:_sessionMgr.totalSlots,freeSlots:_sessionMgr.freeSlots,activeSessions:_sessionMgr.activeSessions,queueLength:_sessionMgr.queueLength,sessions:_sessionMgr.snapshot()});",
  "});",
  '',
].join('\n');

// The catch-all contains a template literal — find it precisely
const catchIdx = src.indexOf('app.all("/api/*",(s,e)=>{e.status(404)');
if (catchIdx === -1) {
  console.error('  ✗ catch-all 404 not found');
} else if (src.slice(catchIdx - 50, catchIdx).includes('Session Routes')) {
  console.log('  · (skip) Session routes already inserted');
} else {
  src = src.slice(0, catchIdx) + SESS_ROUTES + src.slice(catchIdx);
  fs.writeFileSync(FILE, src);
  check('Session routes');
}

console.log('\nDone. File size:', src.length, 'bytes');

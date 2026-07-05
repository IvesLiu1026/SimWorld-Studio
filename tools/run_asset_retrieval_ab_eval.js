#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const net = require("net");

const DEFAULT_SERVER = "http://127.0.0.1:3002";
const DEFAULT_OUT_ROOT = "/data/siddhant/ir_ab_eval";
const DEFAULT_MODEL = process.env.AB_EVAL_MODEL || "";

// 10 outdoor, genre-diverse scenes (run5 = original 5 + 5 new).
const PROMPTS = [
  "A foggy medieval market square outside a gothic cathedral with wooden stalls, candles, barrels, carts, and wet cobblestone.",
  "A dense Hong Kong night alley with neon signs, rollup doors, AC units, cables, carts, trash bins, benches, and shopfront clutter.",
  "An industrial harbor loading dock with shipping containers, barrels, cranes, warehouse props, pallets, and warning signs.",
  "An East Asian temple courtyard with stone lanterns, carved statues, shrine props, plants, benches, and ceremonial objects.",
  "A suburban park plaza with benches, trash bins, trees, planters, playground-like props, street lamps, and path clutter.",
  "A winter village street with snow piles, lanterns, bare trees, firewood stacks, market stalls, benches, and fences.",
  "A Middle Eastern bazaar courtyard with awnings, pottery, crates, carts, carpets, lanterns, plants, and market clutter.",
  "A construction site with a tower crane, scaffolding, stacked building materials, portable site cabins, fences, barriers, machinery, and debris.",
  "A roadside gas station and truck stop at dusk with fuel pumps, a convenience store, parked trucks, signage, trash bins, and lot markings.",
];

function stamp() {
  return new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d+Z$/, "Z");
}

function parseArgs(argv) {
  const opts = {
    serverUrl: process.env.SIMWORLD_SERVER_URL || DEFAULT_SERVER,
    outDir: "",
    modes: ["db", "off"],
    loopModes: ["vanilla"],
    irModes: [],
    variants: [],
    skipExisting: false,
    runner: process.env.AB_EVAL_RUNNER || "",
    model: DEFAULT_MODEL,
    timeoutMs: Number(process.env.AB_EVAL_TIMEOUT_MS || 30 * 60 * 1000),
    limit: 1,
    startIndex: 1,
    promptIndex: null,
    prompt: "",
    dryRunOnly: false,
    preview: true,
    requireUe: true,
    resetScene: true,
    visibleEvalNote: true,
    screenshotAngles: 1,
    comparisonViews: Number(process.env.AB_EVAL_COMPARISON_VIEWS || 0),
    ueHost: process.env.UNREAL_HOST || "127.0.0.1",
    uePort: process.env.UNREAL_PORT || "",
    abortOnInfraFailure: !/^(0|false|no|off)$/i.test(String(process.env.AB_EVAL_ABORT_ON_INFRA_FAILURE || "true")),
  };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    const next = () => {
      if (i + 1 >= argv.length) throw new Error(`missing value after ${a}`);
      return argv[++i];
    };
    if (a === "--server-url") opts.serverUrl = next().replace(/\/+$/, "");
    else if (a === "--out-dir") opts.outDir = next();
    else if (a === "--modes") opts.modes = next().split(",").map(s => s.trim()).filter(Boolean);
    else if (a === "--loop-modes") opts.loopModes = next().split(",").map(s => normalizeLoopMode(s)).filter(Boolean);
    else if (a === "--ir-modes") opts.irModes = next().split(",").map(s => s.trim().toLowerCase()).filter(s => s === "on" || s === "off");
    else if (a === "--variants") opts.variants = next().split(",").map(s => s.trim()).filter(Boolean);
    else if (a === "--skip-existing") opts.skipExisting = true;
    else if (a === "--runner") opts.runner = next();
    else if (a === "--model") opts.model = next();
    else if (a === "--timeout-ms") opts.timeoutMs = Number(next());
    else if (a === "--limit") opts.limit = Number(next());
    else if (a === "--start-index") opts.startIndex = Number(next());
    else if (a === "--prompt-index") opts.promptIndex = Number(next());
    else if (a === "--prompt") opts.prompt = next();
    else if (a === "--all-prompts") opts.limit = PROMPTS.length;
    else if (a === "--dry-run-only") opts.dryRunOnly = true;
    else if (a === "--skip-preview") opts.preview = false;
    else if (a === "--allow-no-ue") opts.requireUe = false;
    else if (a === "--no-reset-scene") opts.resetScene = false;
    else if (a === "--no-visible-eval-note") opts.visibleEvalNote = false;
    else if (a === "--screenshot-angles") opts.screenshotAngles = Number(next());
    else if (a === "--comparison-views") opts.comparisonViews = Number(next());
    else if (a === "--ue-host") opts.ueHost = next();
    else if (a === "--ue-port") opts.uePort = next();
    else if (a === "--abort-on-infra-failure") opts.abortOnInfraFailure = true;
    else if (a === "--no-abort-on-infra-failure") opts.abortOnInfraFailure = false;
    else if (a === "--help" || a === "-h") {
      printHelp();
      process.exit(0);
    } else {
      throw new Error(`unknown argument: ${a}`);
    }
  }
  if (!opts.outDir) opts.outDir = path.join(DEFAULT_OUT_ROOT, `ab_eval_${stamp()}`);
  opts.serverUrl = opts.serverUrl.replace(/\/+$/, "");
  return opts;
}

function printHelp() {
  console.log(`Usage:
  node tools/run_asset_retrieval_ab_eval.js [options]

Options:
  --server-url URL       SimWorld server URL, default ${DEFAULT_SERVER}
  --out-dir DIR          Output artifact directory
  --modes db,off         Retrieval modes to compare, default db,off
  --loop-modes LIST      Loop modes to compare: vanilla,visual_loop, default vanilla
  --ir-modes on,off      IR (intermediate-representation plan) modes to compare; holds
                         retrieval mode constant and flips sceneIr. Default: none (off)
  --skip-existing        Resume: skip cells whose actual_summary.json already exists (ok),
                         loading their prior result into the summary. Rebuilds only missing cells.
  --runner RUNNER        Scene runner to pass to /api/chat, e.g. codex
  --model MODEL          Scene-generation model; default server/model config
  --prompt-index N       Use one built-in prompt, 1-based
  --prompt TEXT          Use a custom prompt
  --start-index N        Start built-in prompt range at N, 1-based
  --limit N              Number of built-in prompts, default 1
  --all-prompts          Run all built-in prompts
  --dry-run-only         Only collect retrieval prompt previews
  --skip-preview         Do not run preflight prompt-preview requests
  --allow-no-ue          Do not require /api/health ueConnected=true
  --no-reset-scene       Do not call eval blank-scene reset before each mode
  --no-visible-eval-note Do not append equal-condition visible-lighting eval note
  --screenshot-angles N  Request N final screenshots from different viewpoints, default 1
  --comparison-views N   Capture N standardized post-run views directly from UE, default 0
  --ue-host HOST         UE TCP host for standardized comparison views, default $UNREAL_HOST or 127.0.0.1
  --ue-port PORT         UE TCP port for standardized comparison views, default $UNREAL_PORT
  --no-abort-on-infra-failure
                         Keep going after UE/reset/screenshot infrastructure failures
  --timeout-ms N         Per chat request timeout, default 1800000
`);
}

function normalizeLoopMode(v) {
  const s = String(v || "").trim().toLowerCase();
  if (!s) return "";
  if (["vanilla", "off", "none", "no_loop", "noloop"].includes(s)) return "vanilla";
  if (["visual", "visual_loop", "visualloop", "multi_view"].includes(s)) return "visual_loop";
  if (["text", "text_loop", "loop"].includes(s)) return "text_loop";
  return s;
}

function mkdirp(p) {
  fs.mkdirSync(p, { recursive: true });
}

async function getJson(url, timeoutMs = 5000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const resp = await fetch(url, { signal: ctrl.signal });
    const text = await resp.text();
    try {
      return JSON.parse(text);
    } catch {
      return { parse_error: true, status: resp.status, text };
    }
  } catch (e) {
    return { request_error: e.message };
  } finally {
    clearTimeout(timer);
  }
}

async function postJson(url, body = {}, timeoutMs = 120000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  const started = Date.now();
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: ctrl.signal,
    });
    const text = await resp.text();
    let data = text;
    try {
      data = JSON.parse(text);
    } catch {}
    return { ok: resp.ok, status: resp.status, data, duration_ms: Date.now() - started };
  } catch (e) {
    return { ok: false, error: e.message, duration_ms: Date.now() - started };
  } finally {
    clearTimeout(timer);
  }
}

// Standing rule: every generated scene is persisted as a reloadable .umap
// (/Game/SavedScenes/<name>.umap) via the server's tested save-as endpoint, so a
// scene can be reloaded and hand-corrected later, not just viewed as screenshots.
async function saveSceneUmap(serverUrl, name, runDir) {
  try {
    const r = await postJson(`${serverUrl}/api/scene/save-as`, { name }, 180000);
    if (!(r.ok && r.data && r.data.ok)) return { ok: false, error: (r.data && r.data.error) || r.error || `status ${r.status}` };
    const res = { ok: true, file_hint: r.data.file_hint, asset_path: r.data.asset_path };
    // Copy the saved .umap from UE project content INTO the run dir so the scene travels with
    // the run artifacts (not just left under /Game/SavedScenes in the project).
    try {
      const contentDir = process.env.AB_EVAL_UE_CONTENT_DIR || "/data/siddhant/ue58_smoke_instances/inst_0/Content";
      const src = path.join(contentDir, "SavedScenes", name + ".umap");
      // The .umap lands via UE SavePackage through a symlinked/overlay content store, so the file
      // can lag a beat behind the save-as HTTP response. Poll briefly before giving up.
      let found = false;
      for (let i = 0; i < 40; i++) {
        try { if (fs.existsSync(src) && fs.statSync(src).size > 0) { found = true; break; } } catch (_e) {}
        await new Promise(r => setTimeout(r, 400));
      }
      if (runDir && found) { const dst = path.join(runDir, "scene.umap"); fs.copyFileSync(src, dst); res.copied_to = dst; }
      else if (runDir) res.copy_error = "umap not found on disk after ~16s wait at " + src;
    } catch (e) { res.copy_error = e.message; }
    return res;
  } catch (e) { return { ok: false, error: e.message }; }
}

function parseSseBlock(block) {
  const lines = block.replace(/\r/g, "").split("\n");
  let event = "message";
  const dataLines = [];
  for (const line of lines) {
    if (!line || line.startsWith(":")) continue;
    if (line.startsWith("event:")) event = line.slice("event:".length).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice("data:".length).trimStart());
  }
  if (!dataLines.length && event === "message") return null;
  const dataText = dataLines.join("\n");
  let data = dataText;
  try {
    data = JSON.parse(dataText);
  } catch {}
  return { event, data };
}

async function postSse({ serverUrl, body, outRaw, outEvents, timeoutMs }) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  const raw = fs.createWriteStream(outRaw, { flags: "w" });
  const jsonl = fs.createWriteStream(outEvents, { flags: "w" });
  const events = [];
  const started = Date.now();
  try {
    const resp = await fetch(`${serverUrl}/api/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: ctrl.signal,
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${await resp.text()}`);
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    let doneEvent = null;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        raw.write(block + "\n\n");
        const parsed = parseSseBlock(block);
        if (!parsed) continue;
        const row = { ts: new Date().toISOString(), ...parsed };
        events.push(row);
        jsonl.write(JSON.stringify(row) + "\n");
        if (parsed.event === "done") {
          doneEvent = parsed.data;
          try { await reader.cancel(); } catch {}
          break;
        }
      }
      if (doneEvent) break;
    }
    return { ok: !(doneEvent && doneEvent.isError), done: doneEvent, events, duration_ms: Date.now() - started };
  } catch (e) {
    return { ok: false, error: e.message, events, duration_ms: Date.now() - started };
  } finally {
    clearTimeout(timer);
    raw.end();
    jsonl.end();
  }
}

function isInfraFailureText(value) {
  const text = typeof value === "string" ? value : JSON.stringify(value || "");
  return /ECONNREFUSED|ECONNRESET|ETIMEDOUT|UE connection error|blank-scene reset failed|screenshot file was not created|No screenshot available|server\/UE health|fetch failed|aborted/i.test(text);
}

function comparisonViewsHaveInfraFailure(views) {
  return (views || []).some(v => v && !v.saved && isInfraFailureText(v));
}

function parseJsonMaybe(value) {
  if (!value) return null;
  if (typeof value === "object") return value;
  if (typeof value !== "string") return null;
  try {
    return JSON.parse(value);
  } catch {
    return null;
  }
}

function toolResultPayloads(result) {
  const payloads = [];
  const outer = parseJsonMaybe(result);
  if (outer) payloads.push(outer);
  for (const item of outer && Array.isArray(outer.content) ? outer.content : []) {
    const nested = parseJsonMaybe(item && item.text);
    if (nested) payloads.push(nested);
  }
  return payloads;
}

function isRecoverableScreenshotFailure(ev) {
  if (!ev || ev.event !== "tool_result") return false;
  for (const payload of toolResultPayloads(ev.data && ev.data.result)) {
    if (payload && payload.error === "screenshot file was not created" && payload.tmp_filepath && fs.existsSync(payload.tmp_filepath)) {
      return true;
    }
  }
  return false;
}

function actualHasInfraFailure(actual) {
  if (!actual) return false;
  if (isInfraFailureText(actual.error)) return true;
  if (isInfraFailureText(actual.done)) return true;
  for (const ev of actual.events || []) {
    if ((ev.event === "tool_result" || ev.event === "retrieval" || ev.event === "done") && isInfraFailureText(ev.data)) {
      if (isRecoverableScreenshotFailure(ev)) continue;
      return true;
    }
  }
  return false;
}

function abortBatch({ opts, summary, run, reason, exitCode = 3 }) {
  summary.aborted_at = new Date().toISOString();
  summary.aborted_reason = reason;
  if (run && !(summary.runs || []).includes(run)) summary.runs.push(run);
  writeReport(opts.outDir, summary);
  writeComparisonGrid(opts.outDir, summary);
  fs.writeFileSync(path.join(opts.outDir, "summary.json"), JSON.stringify(summary, null, 2), "utf-8");
  console.error(`Aborting A/B eval: ${reason}`);
  console.error(`Report: ${path.join(opts.outDir, "ab_eval_report.md")}`);
  process.exitCode = exitCode;
}

function extractPromptPreview(events) {
  const ev = events.find(e => e.event === "prompt_preview");
  return ev ? ev.data : null;
}

function extractRetrievalEvents(events) {
  return events.filter(e => e.event === "retrieval").map(e => e.data);
}

function extractSpawnCalls(events) {
  const out = [];
  for (const e of events) {
    if (e.event !== "tool_details") continue;
    const d = e.data || {};
    const name = String(d.displayName || d.name || "");
    if (!["spawn_actor", "spawn_blueprint_actor", "spawn_agent"].includes(name)) continue;
    const input = d.input || {};
    const pathValue = input.blueprint_id || input.blueprint_name || input.static_mesh || input.agent_type || "";
    out.push({
      tool: name,
      actor_name: input.actor_name || input.name || input.agent_name || "",
      path: pathValue,
      location: input.location || null,
      rotation: input.rotation || null,
      scale: input.scale || null,
      input,
    });
  }
  return out;
}

function screenshotRefs(events) {
  const refs = [];
  for (const e of events) {
    if (e.event === "screenshot" && e.data && e.data.filepath) refs.push(e.data.filepath);
    if (e.event === "done" && e.data && e.data.latestScreenshot) refs.push(e.data.latestScreenshot);
    if (e.event === "verifier_result" && e.data && e.data.screenshot) refs.push(e.data.screenshot);
    if (e.event === "multi_shots" && e.data && Array.isArray(e.data.paths)) refs.push(...e.data.paths);
    if (e.event === "tool_result" && e.data && typeof e.data.result === "string") {
      for (const parsed of toolResultPayloads(e.data.result)) {
        if (parsed && parsed.filepath) refs.push(parsed.filepath);
        if (parsed && parsed.tmp_filepath) refs.push(parsed.tmp_filepath);
        if (parsed && parsed.screenshot) refs.push(parsed.screenshot);
      }
    }
  }
  return [...new Set(refs.filter(Boolean))];
}

function localPathFromScreenshotRef(serverUrl, ref) {
  try {
    const url = new URL(ref, serverUrl);
    if (url.pathname === "/api/screenshot/file") {
      const p = url.searchParams.get("path");
      if (p) return p;
    }
  } catch {}
  return ref && ref.startsWith("/") ? ref : "";
}

async function saveScreenshotRefs(serverUrl, refs, outDir) {
  const saved = [];
  const seenSources = new Set();
  mkdirp(outDir);
  let idx = 0;
  for (const ref of refs) {
    const local = localPathFromScreenshotRef(serverUrl, ref);
    const sourceKey = local || ref;
    if (sourceKey && seenSources.has(sourceKey)) continue;
    if (sourceKey) seenSources.add(sourceKey);
    const dst = path.join(outDir, `screenshot_${String(++idx).padStart(2, "0")}.png`);
    if (local && fs.existsSync(local)) {
      fs.copyFileSync(local, dst);
      saved.push({ ref, source: local, saved: dst });
      continue;
    }
    if (local && path.isAbsolute(local)) {
      saved.push({ ref, source: local, error: "local file not found" });
      continue;
    }
    try {
      const resp = await fetch(new URL(ref, serverUrl));
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const arr = new Uint8Array(await resp.arrayBuffer());
      fs.writeFileSync(dst, arr);
      saved.push({ ref, source: "http", saved: dst });
    } catch (e) {
      saved.push({ ref, error: e.message });
    }
  }
  return saved;
}

function ueCommand(opts, type, params, timeoutMs = 60000) {
  const port = Number(opts.uePort || 0);
  if (!port) return Promise.resolve(null);
  return new Promise((resolve) => {
    const sock = new net.Socket();
    const timer = setTimeout(() => { try { sock.destroy(); } catch {} resolve(null); }, timeoutMs);
    let buf = "";
    sock.connect(port, opts.ueHost || "127.0.0.1", () => {
      sock.write(JSON.stringify({ type, params: params || {} }) + "\n");
    });
    sock.on("data", d => {
      buf += d.toString();
      try {
        const parsed = JSON.parse(buf);
        clearTimeout(timer);
        sock.destroy();
        resolve(parsed);
      } catch {}
    });
    sock.on("error", () => { clearTimeout(timer); try { sock.destroy(); } catch {} resolve(null); });
  });
}

async function waitForFileStable(file, timeoutMs = 45000) {
  const until = Date.now() + timeoutMs;
  let last = -1;
  let same = 0;
  while (Date.now() < until) {
    try {
      const st = fs.statSync(file);
      if (st.size > 0) {
        if (st.size === last) same += 1;
        else { last = st.size; same = 0; }
        if (same >= 2) return true;
      }
    } catch {}
    await new Promise(r => setTimeout(r, 500));
  }
  return false;
}

// Varied view set (mirrors /data/siddhant/static_scene_eval): eye-level street + mid 3/4 +
// high aerials + overview + top-down. Distances derive from XY-extent (not building height),
// so tall scenes are not pushed far away. kind drives the height/distance profile.
const COMPARISON_VIEW_CONFIGS = [
  { name: "street_e",  bearing: 0,   pitch: -3,  kind: "street"   }, // eye-level from W edge, look E across
  { name: "street_n",  bearing: 90,  pitch: -3,  kind: "street"   }, // eye-level from S edge, look N across
  { name: "mid_ne",    bearing: 135, pitch: -18, kind: "mid"      }, // lower 3/4 "tourist" altitude
  { name: "mid_sw",    bearing: -45, pitch: -18, kind: "mid"      },
  { name: "aerial_nw", bearing: 225, pitch: -32, kind: "elevated" }, // high 3/4 corner
  { name: "aerial_se", bearing: 45,  pitch: -32, kind: "elevated" },
  { name: "overview",  bearing: 90,  pitch: -55, kind: "elevated" }, // high tilted overview
  { name: "top",       bearing: 45,  pitch: -82, kind: "overhead" }, // near plan view
];

// Sets the editor viewport camera to an orbit position aimed at the scene centroid
// (percentile bounds so a stray actor can't blow up the frame). Screenshot is taken
// separately via the official take_screenshot MCP tool (AutomationLibrary.take_high_res_screenshot
// produces no file in this UE5.8 headless setup).
function comparisonShotScript(view) {
  return `
import unreal, math
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
INFRA = ('Floor','Sky','Light','Atmo','Fog','Post','Sphere','World','Brush','Default','Player','GameMode','Nav','LevelBounds','Landscape','Volume','Note','Camera','Directional','Exponential','ExpRunnerGroundPlane','ExpRunnerSun','ExpRunnerSkyAtmo','Arena_Env')
boxes = []
for a in eas.get_all_level_actors():
    try:
        lbl = a.get_actor_label()
        cn = a.get_class().get_name()
        if lbl.startswith(INFRA) or cn in ('DirectionalLight','SkyLight','SkyAtmosphere','ExponentialHeightFog'): continue
        o, e = a.get_actor_bounds(False)
        boxes.append((o.x, o.y, o.z, e.x, e.y, e.z))
    except Exception:
        pass
def pct(v, q):
    if not v: return 0.0
    s=sorted(v); i=min(len(s)-1, max(0, int(q*(len(s)-1)))); return s[i]
if boxes:
    cxs=[b[0] for b in boxes]; cys=[b[1] for b in boxes]
    lox,hix=pct(cxs,0.05),pct(cxs,0.95); loy,hiy=pct(cys,0.05),pct(cys,0.95)
    # frame from POSITION spread (percentile), NOT per-asset bounds — one bad-bounds/giant asset
    # must not blow up the framing. zt (height for aerial clearance) is capped per-asset.
    zt=0.0
    for (ox,oy,oz,ex,ey,ez) in boxes:
        if ox<lox-1 or ox>hix+1 or oy<loy-1 or oy>hiy+1: continue
        zt=max(zt, min(oz+ez, 8000.0))
    cx=(lox+hix)/2.0; cy=(loy+hiy)/2.0
    extent=max((hix-lox)/2.0, (hiy-loy)/2.0, 2000.0)
else:
    cx=cy=0.0; extent=3000.0; zt=500.0
pitch_deg=${view.pitch}; kind="${view.kind}"
# distance from XY-extent (NOT height) so tall scenes aren't pushed far; height varies by kind
if kind=="overhead":
    cam_z=max(extent*1.55, 2500.0); horiz=max(extent*0.28, 800.0)
elif kind=="street":
    # eye-level from INSIDE the scene looking across, not outside staring at a perimeter building
    horiz=max(0.32*extent, 1500.0); cam_z=190.0
elif kind=="mid":
    horiz=max(1.1*extent, 2400.0); cam_z=max(horiz*math.tan(math.radians(abs(pitch_deg))), max(0.4*zt, 500.0))
else:
    horiz=max(1.6*extent, 3200.0); cam_z=max(horiz*math.tan(math.radians(abs(pitch_deg))), 2.5*zt, 700.0)
b=math.radians(${view.bearing})
cam_x=cx+horiz*math.cos(b); cam_y=cy+horiz*math.sin(b)
yaw=math.degrees(math.atan2(cy-cam_y, cx-cam_x))
subsys = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
subsys.set_level_viewport_camera_info(unreal.Vector(cam_x, cam_y, cam_z), unreal.Rotator(pitch=pitch_deg, yaw=yaw, roll=0.0))
print("AB_CAM_SET ${view.name}")
`;
}

// Post-build correction: disable physics on all spawned actors, zero pitch/roll (keep yaw),
// and re-ground anything sunk/floating. Fixes physics-toppled or stray-tilted props/buildings.
async function straightenScene(opts) {
  if (!opts.uePort) return { error: "no ue port" };
  const script = `
import unreal
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
INFRA = ('Floor','Sky','Light','Atmo','Fog','Post','Sphere','World','Brush','Default','Player','GameMode','Nav','LevelBounds','Landscape','Volume','Note','Camera','Directional','Exponential','ExpRunnerGroundPlane','ExpRunnerSun','ExpRunnerSkyAtmo','Arena_Env')
rotn=0; gnd=0
for a in eas.get_all_level_actors():
    try:
        lbl=a.get_actor_label(); cn=a.get_class().get_name()
        if lbl.startswith(INFRA) or cn in ('DirectionalLight','SkyLight','SkyAtmosphere','ExponentialHeightFog'): continue
        try:
            for comp in a.get_components_by_class(unreal.PrimitiveComponent):
                try: comp.set_simulate_physics(False)
                except Exception: pass
        except Exception: pass
        rot=a.get_actor_rotation()
        if abs(rot.pitch)>0.5 or abs(rot.roll)>0.5:
            a.set_actor_rotation(unreal.Rotator(pitch=0.0, yaw=rot.yaw, roll=0.0), False); rotn+=1
        o,e=a.get_actor_bounds(False); bottom=o.z-e.z
        if bottom < -10 or bottom > 60:
            loc=a.get_actor_location(); loc.z=loc.z-bottom
            a.set_actor_location(loc, False, False); gnd+=1
    except Exception: pass
print("STRAIGHTEN rotated=%d regrounded=%d"%(rotn,gnd))
`;
  try {
    const r = await ueCommand(opts, "execute_python_script", { script }, 120000);
    const logs = (r && r.result && r.result.python_logs) || [];
    return { ok: true, logs };
  } catch (e) {
    return { ok: false, error: e && e.message ? e.message : String(e) };
  }
}

// ── Scene-quality metrics over ALL spawned (non-infra) actors: collision (AABB overlap
//    pairs), floating (no support surface below within footprint), out-of-bounds (beyond
//    ±GROUND_HALF). Works regardless of whether actors were spawned via MCP tools or python
//    (unlike /api/scene-check, which is session-tracked). Measured PRE-straighten so it
//    reflects the RAW build quality. Non-fatal.
async function computeSceneMetrics(opts, groundHalfCm = 13000, floatThresholdCm = 12) {
  if (!opts.uePort) return { ok: false, error: "no ue port" };
  const script = `
import unreal, json
GH = ${groundHalfCm}; FT = ${floatThresholdCm}; TOUCH = 5.0
# Editor-world enumeration: GameplayStatics (game-world registry) is blind to actors
# spawned into the editor via python, so use EditorActorSubsystem (validated).
acts = unreal.get_editor_subsystem(unreal.EditorActorSubsystem).get_all_level_actors()
INFRA = ('floor','sky','light','atmo','fog','post','sphere','world','brush','default','player','gamemode','nav','levelbounds','landscape','volume','note','camera','directional','exponential','exprunner','arena_env','ground_plane')
INFRA_CLS = ('directionallight','skylight','skyatmosphere','exponentialheightfog','worldsettings','playerstart','cameraactor','reflectioncapture','brush','navmeshboundingvolume','postprocessvolume')
def infra(a):
    if a.get_class().get_name().lower() in INFRA_CLS: return True
    try: lbl=a.get_actor_label().lower()
    except: lbl=a.get_name().lower()
    return lbl.startswith(INFRA)
B=[]
for a in acts:
    if infra(a): continue
    try: o,e=a.get_actor_bounds(False)
    except: continue
    if e.x<1 and e.y<1 and e.z<1: continue
    try: lbl=a.get_actor_label()
    except: lbl=a.get_name()
    loc=a.get_actor_location()
    B.append({'n':lbl,'ox':o.x,'oy':o.y,'oz':o.z,'ex':max(e.x,1.0),'ey':max(e.y,1.0),'ez':max(e.z,1.0),'lx':loc.x,'ly':loc.y,'bot':o.z-e.z,'top':o.z+e.z})
n=len(B)
def flat(b): return b['ez']<20.0
# structural vs clutter: matches solver IR_CLUTTER_RADIUS_M (0.8m). small-small overlaps are
# allowed by the solver, so report STRUCTURAL collisions (>=1 big object) as the meaningful metric.
CLUTTER_R=80.0  # cm radius
def big(b): return (b['ex']*b['ex']+b['ey']*b['ey'])**0.5 >= CLUTTER_R
coll=set(); pairs=0; scoll=set(); spairs=0
for i in range(n):
    a=B[i]
    for j in range(i+1,n):
        b=B[j]
        if flat(a) or flat(b): continue
        ox=min(a['ox']+a['ex'],b['ox']+b['ex'])-max(a['ox']-a['ex'],b['ox']-b['ex'])
        if ox<=TOUCH: continue
        oy=min(a['oy']+a['ey'],b['oy']+b['ey'])-max(a['oy']-a['ey'],b['oy']-b['ey'])
        if oy<=TOUCH: continue
        oz=min(a['top'],b['top'])-max(a['bot'],b['bot'])
        if oz<=TOUCH: continue
        pairs+=1; coll.add(i); coll.add(j)
        if big(a) or big(b): spairs+=1; scoll.add(i); scoll.add(j)
fl=0; flers=[]
for a in B:
    if a['bot']<=FT: continue
    sup=False
    for b in B:
        if b is a: continue
        if b['top']<a['bot']-1 and abs(b['ox']-a['lx'])<(a['ex']*0.5+b['ex']) and abs(b['oy']-a['ly'])<(a['ey']*0.5+b['ey']):
            if a['bot']-b['top']<=FT: sup=True; break
    if not sup: fl+=1; flers.append(a['n'])
oob=0; oobs=[]
for a in B:
    if abs(a['lx'])>GH or abs(a['ly'])>GH: oob+=1; oobs.append(a['n'])
print('AB_METRICS '+json.dumps({'checked':n,'collision_actors':len(coll),'collision_pairs':pairs,'structural_collision_pairs':spairs,'structural_collision_actors':len(scoll),'floating':fl,'out_of_bounds':oob,'floaters':flers[:25],'oob':oobs[:25]}))
`;
  try {
    const r = await ueCommand(opts, "execute_python_script", { script }, 120000);
    const logs = (r && r.result && r.result.python_logs) || [];
    // koe's editor returns python output only via the editor log; the vendored bridge scrapes
    // it back as `LogPython: ...` lines — so match the marker ANYWHERE in a line, not at start.
    const joined = Array.isArray(logs) ? logs.join("\n") : String(logs || "");
    const idx = joined.indexOf("AB_METRICS ");
    if (idx < 0) return { ok: false, error: "no metrics output", logs_tail: (Array.isArray(logs) ? logs.slice(-3) : joined.slice(-300)) };
    const m = JSON.parse(joined.slice(idx + "AB_METRICS ".length).split(/\r?\n/)[0]);
    m.ok = true;
    m.collision_rate = m.checked ? +(m.collision_actors / m.checked).toFixed(4) : 0;
    m.structural_collision_rate = m.checked ? +((m.structural_collision_actors || 0) / m.checked).toFixed(4) : 0;
    m.floating_rate = m.checked ? +(m.floating / m.checked).toFixed(4) : 0;
    m.oob_rate = m.checked ? +(m.out_of_bounds / m.checked).toFixed(4) : 0;
    return m;
  } catch (e) { return { ok: false, error: e && e.message ? e.message : String(e) }; }
}

// Ensure a daytime SkyAtmosphere so screenshots/umap show a blue sky (vanilla setup leaves it
// black). Lighting (sun/fog) is left untouched — the sun is just linked to drive the sky.
async function ensureDaytimeSky(opts) {
  if (!opts.uePort) return { ok: false, error: "no ue port" };
  const script = [
    "import unreal",
    "eas=unreal.get_editor_subsystem(unreal.EditorActorSubsystem)",
    "acts=eas.get_all_level_actors()",
    "had=any(a.get_class().get_name()=='SkyAtmosphere' for a in acts)",
    "if not had:",
    "    at=eas.spawn_actor_from_class(unreal.SkyAtmosphere, unreal.Vector(0,0,0), unreal.Rotator(0,0,0))",
    "    if at: at.set_actor_label('Arena_Env_Atmosphere')",
    "for a in acts:",
    "    if a.get_class().get_name()=='DirectionalLight':",
    "        c=a.get_component_by_class(unreal.DirectionalLightComponent)",
    "        if c:",
    "            try: c.set_editor_property('atmosphere_sun_light', True)",
    "            except Exception: pass",
    "        break",
    "print('AB_SKY had='+str(had))",
  ].join("\n");
  try { await ueCommand(opts, "execute_python_script", { script }, 60000); return { ok: true }; }
  catch (e) { return { ok: false, error: e && e.message ? e.message : String(e) }; }
}

// Phase A1: derive lighting/atmosphere from the scene prompt + apply it deterministically in UE
// (post-build, pre-staged-capture). Never fatal — a mood failure leaves the flat-lit scene intact.
async function applyMood(opts, scene) {
  try {
    const { deriveMood, buildMoodScript } = require(path.resolve(__dirname, "..", "simworld_studio_workspace", "web", "server", "scene-mood"));
    const mood = await deriveMood(scene, { model: opts.model, provider: opts.runner || "codex", runner: opts.runner || "codex" });
    await ueCommand(opts, "execute_python_script", { script: buildMoodScript(mood) }, 60000);
    return { ok: true, mood_tag: mood.mood_tag, params: mood };
  } catch (e) { return { ok: false, error: String(e && e.message) }; }
}

async function captureComparisonViews(opts, runDir, count, subdir) {
  const n = Math.max(0, Math.min(COMPARISON_VIEW_CONFIGS.length, Number(count) || 0));
  if (!n) return [];
  if (!opts.uePort) return [{ error: "ue port not configured" }];
  const outDir = path.join(runDir, subdir || "comparison_views");
  mkdirp(outDir);
  // 1) Frame the scene ONCE: centroid + extent from non-infra actor POSITIONS (percentile).
  const boundsScript = [
    "import unreal, json",
    "eas=unreal.get_editor_subsystem(unreal.EditorActorSubsystem)",
    "INFRA=('floor','sky','light','atmo','fog','post','sphere','world','brush','default','player','gamemode','nav','levelbounds','landscape','volume','note','camera','directional','exponential','exprunner','arena_env','ground_plane','_swshotcam')",
    "xs=[];ys=[];zt=0.0",
    "for a in eas.get_all_level_actors():",
    "    try:",
    "        if a.get_actor_label().lower().startswith(INFRA): continue",
    "        o,e=a.get_actor_bounds(False)",
    "        if e.x<1 and e.y<1 and e.z<1: continue",
    "        xs.append(o.x); ys.append(o.y); zt=max(zt, min(o.z+e.z, 8000.0))",
    "    except Exception: pass",
    "def pct(v,q):",
    "    if not v: return 0.0",
    "    s=sorted(v); return s[min(len(s)-1,max(0,int(q*(len(s)-1))))]",
    "if xs:",
    "    lox=pct(xs,0.05);hix=pct(xs,0.95);loy=pct(ys,0.05);hiy=pct(ys,0.95)",
    "    cx=(lox+hix)/2.0;cy=(loy+hiy)/2.0;extent=max((hix-lox)/2.0,(hiy-loy)/2.0,2000.0)",
    "else:",
    "    cx=0.0;cy=0.0;extent=3000.0",
    "print('AB_BOUNDS '+json.dumps({'cx':cx,'cy':cy,'extent':extent,'zt':zt}))",
  ].join("\n");
  let bounds = { cx: 0, cy: 0, extent: 3000, zt: 500 };
  try {
    const br = await ueCommand(opts, "execute_python_script", { script: boundsScript }, 60000);
    const logs = (br && br.result && br.result.python_logs) || [];
    const j = Array.isArray(logs) ? logs.join("\n") : String(logs || "");
    const idx = j.indexOf("AB_BOUNDS ");
    if (idx >= 0) bounds = JSON.parse(j.slice(idx + "AB_BOUNDS ".length).split(/\r?\n/)[0]);
  } catch (_e) {}
  const { cx, cy, extent, zt } = bounds;
  const out = [];
  for (const view of COMPARISON_VIEW_CONFIGS.slice(0, n)) {
    const fp = path.join(outDir, `view_${String(out.length + 1).padStart(2, "0")}_${view.name}.png`);
    try { fs.rmSync(fp, { force: true }); } catch {}
    const started = Date.now();
    // 2) Compute this view's camera pose from the framing (mirrors the old in-UE script).
    const pitch = view.pitch, b = view.bearing * Math.PI / 180;
    let horiz, cam_z;
    if (view.kind === "overhead") { cam_z = Math.max(extent * 1.55, 2500); horiz = Math.max(extent * 0.28, 800); }
    else if (view.kind === "street") { horiz = extent + 1000; cam_z = 220; }
    else if (view.kind === "mid") { horiz = Math.max(1.1 * extent, 2400); cam_z = Math.max(horiz * Math.tan(Math.abs(pitch) * Math.PI / 180), Math.max(0.4 * zt, 500)); }
    else { horiz = Math.max(1.6 * extent, 3200); cam_z = Math.max(horiz * Math.tan(Math.abs(pitch) * Math.PI / 180), 2.5 * zt, 700); }
    const cam_x = cx + horiz * Math.cos(b), cam_y = cy + horiz * Math.sin(b);
    const yaw = Math.atan2(cy - cam_y, cx - cam_x) * 180 / Math.PI;
    let ok = false, real = null, ueStatus = null;
    try {
      // Camera-AWARE capture: take_screenshot spawns a CameraActor at this pose (camera_rotation
      // is [roll, pitch, yaw]). A bare take_screenshot auto-picks ONE pose for every view → that
      // was the identical-views bug; set_level_viewport_camera_info is ignored by this editor.
      const shotName = `abv_${process.pid}_${Date.now()}_${view.name}.png`;
      const sr = await ueCommand(opts, "take_screenshot", { filename: shotName, width: 1280, height: 720, camera_location: [cam_x, cam_y, cam_z], camera_rotation: [0, pitch, yaw] }, 60000);
      ueStatus = sr && sr.status;
      real = sr && sr.filepath && path.isAbsolute(sr.filepath) ? sr.filepath : null;
      if (real) {
        const landed = await waitForFileStable(real, 50000);
        if (landed) { fs.copyFileSync(real, fp); ok = await waitForFileStable(fp, 5000); }
      }
    } catch (e) {
      ueStatus = "error:" + (e && e.message ? e.message : String(e));
    }
    out.push({
      name: view.name,
      saved: ok ? fp : null,
      source: real,
      error: ok ? null : "screenshot not captured",
      duration_ms: Date.now() - started,
      ue_status: ueStatus,
      camera: { location: [Math.round(cam_x), Math.round(cam_y), Math.round(cam_z)], pitch, yaw: Math.round(yaw) },
    });
  }
  return out;
}

function promptSet(opts) {
  if (opts.prompt) return [{ index: 0, text: opts.prompt }];
  if (opts.promptIndex != null) {
    const i = opts.promptIndex - 1;
    if (i < 0 || i >= PROMPTS.length) throw new Error(`prompt-index must be 1..${PROMPTS.length}`);
    return [{ index: opts.promptIndex, text: PROMPTS[i] }];
  }
  const start = Math.max(1, Math.min(PROMPTS.length, Number(opts.startIndex) || 1));
  const count = Math.max(1, Math.min(PROMPTS.length - start + 1, Number(opts.limit) || 1));
  return PROMPTS.slice(start - 1, start - 1 + count).map((text, i) => ({ index: start + i, text }));
}

function evalPromptText(prompt, opts, mode) {
  const OFF_DISCOVERY = (mode === "off") ? "\n\nASSET DISCOVERY (no curated palette is provided for this scene): the UE content library has MANY themed asset packs beyond the basic CityDatabase. DISCOVER and use them — call execute_python_script and run `import unreal; print(chr(10).join(unreal.EditorAssetLibrary.list_assets('/Game', recursive=False)))` to list the top-level packs/folders, then drill into the ones matching THIS scene's setting with `unreal.EditorAssetLibrary.list_assets('/Game/<Pack>', recursive=True, include_folder=False)`. Spawn the genre-appropriate meshes/blueprints you find by their FULL /Game/... path (spawn_actor for static meshes, spawn_blueprint_actor for blueprints). STRONGLY prefer these themed assets over generic CityDatabase BP_Building towers, and do NOT use BasicShapes cubes/planes as stand-ins for real objects." : "";
  // Scene size is parameterized by AB_EVAL_SIZE_M (default 100 for back-compat). Density + spacing scale with area.
  const SIZE = Math.max(60, Number(process.env.AB_EVAL_SIZE_M) || 100);
  const HALF = Math.round(SIZE / 2), UU = HALF * 100;
  const dens = Math.round(Math.pow(SIZE / 100, 2) * 100);          // ~100 @100 m, ~400 @200 m
  const dLo = Math.round(dens * 0.75), dHi = Math.round(dens * 1.6);
  const step = Math.max(8, Math.round(SIZE / 12));                 // ~8 m @100 m, ~17 m @200 m
  const BUILD_GUIDE =
    `\n\nBUILD A FULL ~${SIZE} m × ${SIZE} m SCENE (≈${SIZE * 100}×${SIZE * 100} UE units centered at origin, so X and Y roughly -${UU}..+${UU}) — a substantial, real-world-scale place you could walk around in, NOT a small patch with a few props. FIRST PLAN, then build to the plan — organized with natural real-world variation, not random.` +
    `\nPLAN: decide a focal anchor (main building / fountain / gate) + the main streets/axes/paths + ZONES spread ACROSS the full ${SIZE}×${SIZE} m (buildings around the perimeter and lining the streets, open plaza/paths between them, clustered detail areas). This is a LARGE site — use its full extent, but EVERY region must be FULL: no large empty gaps between zones (fill with paths, secondary clusters, vegetation, dressing).` +
    `\nGROUND FIRST (before any props): carpet the WHOLE ${SIZE}×${SIZE} m with a solid, scene-appropriate ground (asphalt/concrete for city or harbor; cobblestone/dirt for medieval; stone for a temple; sand for a bazaar; grass for a park; snow for winter) so NO bare default grey ground shows anywhere. Lay it by EITHER tiling ground/floor MESHES (grass tiles, walkway/plaza slabs, snowy-road or stone-floor pieces) edge-to-edge across the full ${SIZE} m, OR spawning a grid of flat planes (/Engine/BasicShapes/Plane, ~4–8 m each) and applying a matching ground MATERIAL via StaticMeshComponent.set_material(0, material) so the texture tiles instead of stretching. NEVER use water/ocean as the floor (water only as a separate edge feature). Every object sits ON this ground, inside the ${SIZE} m.` +
    `\nFILL DENSELY ACROSS THE WHOLE AREA: ${SIZE}×${SIZE} m is a big site, so you MUST place MANY assets — aim for ~${dLo}–${dHi}+ — DISTRIBUTED across the full area (something roughly every ${step}–${step + 6} m), NOT clustered in one corner with empty ground around it. LEAD with LARGE / structural assets (whole buildings, houses, sheds, market stalls, walls, big trees, large set-pieces) as the backbone, arranged in rows and clusters along the streets/zones; REUSE each type many times; THEN add mid-size and small props as dressing (small props like bags/bottles/rocks are NEVER the bulk). Layer background structures → midground → foreground so little ground is left empty.` +
    `\nUse execute_python_script to place many instances efficiently (loops/grids spanning the ${SIZE} m). Prefer COMPLETE buildings over modular fragments; realistic scale (~0.8–1.3×, no giant stretching); everything upright (yaw only) and on the ground. The result must read as ONE busy, organized, instantly-recognizable ~${SIZE} m place with believable variation.` +
    `\nSPAWN ROBUSTLY (critical for dense scenes — a whole scene has been lost to this): the UE python job is SERIAL and TIME-LIMITED, so do NOT put hundreds of spawns in ONE execute_python_script call. Split them across SEVERAL calls of at most ~120 spawns each (e.g. ground first, then structures, then dressing), and read the job log after each call before the next. Wrap EACH individual spawn in its own try/except so one failing asset is SKIPPED, never aborting the batch, and print a running spawned-count from each job. After the final batch, call get_actors_in_level; if far fewer actors exist than you intended, spawn the missing ones in another batch. NEVER finish with a near-empty scene (ground only).`;
  prompt = `${prompt}${OFF_DISCOVERY}${BUILD_GUIDE}`;
  const screenshotCount = Math.max(1, Math.floor(Number(opts.screenshotAngles) || 1));
  const screenshotInstruction = screenshotCount > 1
    ? `Build the scene, then call take_screenshot ${screenshotCount} times from clearly different viewpoints: (1) wide establishing/front three-quarter, (2) opposite/rear three-quarter, (3) left or side angle focused on mid-ground structure, and (4) closer detail angle focused on props/clutter. Use descriptive filenames ending in _wide, _reverse, _side, and _detail when possible.`
    : "Build the scene, call take_screenshot once at the end, then finish.";
  return `${prompt}

For this automated A/B evaluation, build the scene from scratch on the blank stage and keep the final screenshot visibly lit/readable. If the requested scene is night-like, preserve the night/neon mood with signs and props, but use enough ambient/daylight or bright dusk lighting that buildings, streets, and clutter are clearly visible in the saved screenshot.

Do not call verify_scene, check_floating, check_collisions, file-read/search tools, or screenshot-inspection tools during this eval. Do not try to inspect screenshots yourself. ${screenshotInstruction}`;
}

function writeTextList(file, values) {
  fs.writeFileSync(file, values.map(v => String(v)).join("\n") + (values.length ? "\n" : ""), "utf-8");
}

function writeReport(outDir, summary) {
  const lines = [];
  lines.push(`# Asset Retrieval A/B Evaluation - ${summary.started_at}`);
  lines.push("");
  lines.push("## Environment");
  lines.push("");
  lines.push(`- Server: \`${summary.server_url}\``);
  lines.push(`- Runner: \`${summary.runner || "(server default)"}\``);
  lines.push(`- Model: \`${summary.model || "(server default)"}\``);
  lines.push(`- Retrieval modes: \`${(summary.modes || []).join(",")}\``);
  lines.push(`- Loop modes: \`${(summary.loop_modes || []).join(",")}\``);
  lines.push(`- IR-plan modes: \`${(summary.ir_modes || []).join(",") || "(off)"}\``);
  lines.push(`- Health: \`${JSON.stringify(summary.health)}\``);
  lines.push(`- UCV status: \`${JSON.stringify(summary.ucv_status)}\``);
  if (summary.blocked) {
    lines.push("");
    lines.push("## Status");
    lines.push("");
    lines.push(`Blocked: ${summary.blocked}`);
  }
  lines.push("");
  lines.push("## Runs");
  lines.push("");
  for (const run of summary.runs || []) {
    lines.push(`### ${run.label}`);
    lines.push("");
    lines.push(`- Prompt: ${run.prompt}`);
    lines.push(`- Mode: \`${run.mode}\``);
    lines.push(`- Loop mode: \`${run.loopMode || "vanilla"}\``);
    lines.push(`- IR-plan: \`${run.irMode || "off"}\``);
    lines.push(`- Preview paths: \`${run.preview_path_count ?? 0}\``);
    if (run.scene_reset) lines.push(`- Blank-scene reset: \`${run.scene_reset.ok && !(run.scene_reset.data && run.scene_reset.data.ok === false)}\``);
    lines.push(`- Actual ok: \`${run.actual ? run.actual.ok : "not_run"}\``);
    lines.push(`- Spawn calls: \`${run.spawn_count ?? 0}\``);
    lines.push(`- Screenshots saved: \`${(run.screenshots || []).filter(s => s.saved).length}\``);
    lines.push(`- Comparison views saved: \`${(run.comparison_views || []).filter(s => s.saved).length}\``);
    lines.push(`- Saved .umap: \`${run.umap && run.umap.ok ? run.umap.file_hint : (run.umap ? "FAILED: " + run.umap.error : "n/a")}\``);
    if (run.actual && run.actual.error) lines.push(`- Error: \`${run.actual.error}\``);
    if (run.actual && run.actual.done) lines.push(`- Done: \`${JSON.stringify(run.actual.done)}\``);
    lines.push(`- Directory: \`${run.dir}\``);
    lines.push("");
  }
  lines.push("## Notes");
  lines.push("");
  lines.push("- `db` and `off` are sent through the same `/api/chat` endpoint; loop mode is varied per run and `dynamicSkills` is disabled.");
  lines.push("- Preview runs use `assetRetrievalDryRun: true`; actual runs do not.");
  lines.push("- Spawn calls are extracted from streamed `tool_details` events for `spawn_actor`, `spawn_blueprint_actor`, and `spawn_agent`.");
  fs.writeFileSync(path.join(outDir, "ab_eval_report.md"), lines.join("\n"), "utf-8");
}

function relPath(fromDir, file) {
  if (!file) return "";
  return path.relative(fromDir, file).split(path.sep).join("/");
}

function imageCells(outDir, run) {
  const preferred = (run.comparison_views || []).filter(v => v && v.saved).map(v => v.saved);
  const fallback = (run.screenshots || []).filter(v => v && v.saved).map(v => v.saved);
  const files = (preferred.length ? preferred : fallback).slice(0, 4);
  const cells = [];
  for (let i = 0; i < 4; i++) {
    const f = files[i];
    cells.push(f ? `![](${relPath(outDir, f)})` : "");
  }
  return cells;
}

function writeComparisonGrid(outDir, summary) {
  const prompts = [];
  const seen = new Set();
  for (const run of summary.runs || []) {
    const key = `${run.prompt_index || 0}::${run.prompt}`;
    if (!seen.has(key)) {
      seen.add(key);
      prompts.push({ index: run.prompt_index || 0, text: run.prompt });
    }
  }
  prompts.sort((a, b) => a.index - b.index);
  const lines = [];
  lines.push(`# Asset Retrieval 4-Way Visual Comparison - ${summary.started_at}`);
  lines.push("");
  lines.push(`Model/runner: \`${summary.model || "(server default)"}\` / \`${summary.runner || "(server default)"}\``);
  lines.push("");
  for (const prompt of prompts) {
    lines.push(`## Scene ${prompt.index || "custom"}`);
    lines.push("");
    lines.push(prompt.text);
    lines.push("");
    lines.push("| Condition | View 1 | View 2 | View 3 | View 4 |");
    lines.push("|---|---|---|---|---|");
    const irArms = (summary.ir_modes && summary.ir_modes.length) ? summary.ir_modes : [null];
    for (const mode of summary.modes || []) {
      for (const loopMode of summary.loop_modes || ["vanilla"]) {
        for (const irMode of irArms) {
          const run = (summary.runs || []).find(r => r.prompt_index === prompt.index && r.mode === mode && (r.loopMode || "vanilla") === loopMode && (r.irMode || null) === (irMode || null));
          const irLabel = irMode ? ` / ir-${irMode}` : "";
          const label = `${mode}${irLabel} / ${loopMode === "vanilla" ? "no visual loop" : loopMode}`;
          const cells = run ? imageCells(outDir, run) : ["", "", "", ""];
          lines.push(`| ${label} | ${cells.join(" | ")} |`);
        }
      }
    }
    lines.push("");
  }
  lines.push("## Notes");
  lines.push("");
  lines.push("- Each row is one condition: retrieval mode crossed with loop mode and IR-plan arm.");
  lines.push("- Images are standardized post-run camera captures when available, so rows are easier to compare.");
  lines.push("- Per-run raw SSE, spawn calls, screenshots, and summaries are stored under each condition directory.");
  fs.writeFileSync(path.join(outDir, "comparison_grid.md"), lines.join("\n"), "utf-8");
}

async function main() {
  const opts = parseArgs(process.argv);
  const launchId = stamp();  // unique per launch so umap package names never collide across runs in a long-lived editor session
  mkdirp(opts.outDir);
  const prompts = promptSet(opts);
  const summary = {
    started_at: new Date().toISOString(),
    server_url: opts.serverUrl,
    runner: opts.runner,
    model: opts.model,
    modes: opts.modes,
    loop_modes: opts.loopModes,
    ir_modes: opts.irModes,
    variants: opts.variants,
    dry_run_only: opts.dryRunOnly,
    reset_scene: opts.resetScene,
    screenshot_angles: opts.screenshotAngles,
    comparison_views: opts.comparisonViews,
    ue_host: opts.ueHost,
    ue_port: opts.uePort,
    health: await getJson(`${opts.serverUrl}/api/health`),
    ucv_status: await getJson(`${opts.serverUrl}/api/internal/ucv/status`),
    runs: [],
  };
  fs.writeFileSync(path.join(opts.outDir, "environment.json"), JSON.stringify(summary, null, 2), "utf-8");

  if (opts.requireUe && !summary.health.ueConnected) {
    summary.blocked = "UE is not connected according to /api/health; actual A/B scene generation was not started.";
    writeReport(opts.outDir, summary);
    fs.writeFileSync(path.join(opts.outDir, "summary.json"), JSON.stringify(summary, null, 2), "utf-8");
    console.error(summary.blocked);
    console.error(`Report: ${path.join(opts.outDir, "ab_eval_report.md")}`);
    process.exitCode = 2;
    return;
  }

  // Run2: IR-ON solver variants (all share the same plan; only the solver/repair differs).
  // Falls back to --ir-modes (or one null cell) when --variants is not given.
  const VARIANT_PRESETS = {
    "vanilla": { sceneIr: false },                                              // no-IR baseline (builder decides layout)
    "solver-baseline": { sceneIr: true, irSolver: "legacy", irRepair: false },
    "solver-structure": { sceneIr: true, irSolver: "structure", irRepair: false },
    "solver-structrepair": { sceneIr: true, irSolver: "structure", irRepair: true },
    "solver-gentle": { sceneIr: true, irSolver: "gentle", irRepair: false },    // intent-preserving solver
    // CoT experiment (ASCII off for both; JSON coords always present):
    "gentle-nocot": { sceneIr: true, irSolver: "gentle", irRepair: false, irCot: false, irAscii: false },
    "gentle-cot":   { sceneIr: true, irSolver: "gentle", irRepair: false, irCot: true,  irAscii: false },
    // Un-blind planner experiment (rich asset context = desc/tags/subcategory fed to the planner):
    "gentle-rich":  { sceneIr: true, irSolver: "gentle", irRepair: false, irCot: false, irAscii: false, irRichAssets: true },
    // Fix B: rich planner + plan-level visual critic (render plan -> VLM critiques layout -> revise -> re-solve):
    "gentle-plancritic": { sceneIr: true, irSolver: "gentle", irRepair: false, irCot: false, irAscii: false, irRichAssets: true, irPlanCritic: true },
    // Fix #1: plan-critic + deterministic themed ground carpet:
    "gentle-pc-ground": { sceneIr: true, irSolver: "gentle", irRepair: false, irCot: false, irAscii: false, irRichAssets: true, irPlanCritic: true, irGroundPass: true },
    // Phase A1: plan-critic + mood/lighting/camera stage (`mood` is harness-only — dual-capture flat+staged):
    "gentle-pc-mood": { sceneIr: true, irSolver: "gentle", irRepair: false, irCot: false, irAscii: false, irRichAssets: true, irPlanCritic: true, mood: true },
    // Phase A/B ENHANCED: plan-critic + ground carpet + mood (facing fix is now default in the solver):
    "gentle-enh": { sceneIr: true, irSolver: "gentle", irRepair: false, irCot: false, irAscii: false, irRichAssets: true, irPlanCritic: true, irGroundPass: true, mood: true },
  };
  const variantList = (opts.variants && opts.variants.length)
    ? opts.variants.map(v => ({ name: v, cfg: VARIANT_PRESETS[v] || { sceneIr: true } }))
    : (opts.irModes.length ? opts.irModes.map(m => ({ name: null, irMode: m })) : [{ name: null, irMode: null }]);

  for (const prompt of prompts) {
    for (const mode of opts.modes) {
      for (const loopMode of opts.loopModes) {
      for (const variant of variantList) {
      const irMode = variant.irMode != null ? variant.irMode : (variant.cfg ? "on" : null);
      const vTag = variant.name ? `_${variant.name}` : (irMode ? `_ir-${irMode}` : "");
      const label = `prompt_${String(prompt.index).padStart(2, "0")}_${mode}${vTag}_${loopMode}`;
      const runDir = path.join(opts.outDir, label);
      mkdirp(runDir);
      // --skip-existing (resume): if this cell already finished, load its result and skip.
      if (opts.skipExisting) {
        try {
          const prev = JSON.parse(fs.readFileSync(path.join(runDir, "actual_summary.json"), "utf-8"));
          if (prev && prev.actual && prev.actual.ok) {
            console.log(`skip-existing: ${label} (already built)`);
            summary.runs.push({ label, prompt_index: prompt.index, prompt: prompt.text, mode, loopMode, irMode, variant: variant.name, dir: runDir, skipped_existing: true, actual: prev.actual, spawn_count: prev.spawn_count, screenshots: prev.screenshots, comparison_views: prev.comparison_views, umap: prev.umap, metrics: prev.metrics });
            fs.writeFileSync(path.join(opts.outDir, "summary.json"), JSON.stringify(summary, null, 2), "utf-8");
            continue;
          }
        } catch (_e) {}
      }
      const run = { label, prompt_index: prompt.index, prompt: prompt.text, mode, loopMode, irMode, variant: variant.name, dir: runDir };
      const requestMessage = evalPromptText(prompt.text, opts, mode);
      const baseBody = {
        message: requestMessage,
        assetRetrievalMode: mode,
        loopMode,
        useLoop: loopMode !== "vanilla",
        dynamicSkills: false,
      };
      if (variant.cfg) {
        baseBody.sceneIr = !!variant.cfg.sceneIr;
        if (variant.cfg.irSolver) baseBody.irSolver = variant.cfg.irSolver;
        baseBody.irRepair = !!variant.cfg.irRepair;
        if (variant.cfg.irCot != null) baseBody.irCot = !!variant.cfg.irCot;
        if (variant.cfg.irAscii != null) baseBody.irAscii = !!variant.cfg.irAscii;
        if (variant.cfg.irRichAssets != null) baseBody.irRichAssets = !!variant.cfg.irRichAssets;
        if (variant.cfg.irPlanCritic != null) baseBody.irPlanCritic = !!variant.cfg.irPlanCritic;
        if (variant.cfg.irGroundPass != null) baseBody.irGroundPass = !!variant.cfg.irGroundPass;
      } else if (irMode) baseBody.sceneIr = (irMode === "on");
      if (opts.runner) baseBody.runner = opts.runner;
      if (opts.model) baseBody.model = opts.model;
      fs.writeFileSync(path.join(runDir, "request.json"), JSON.stringify(baseBody, null, 2), "utf-8");

      if (opts.resetScene && !opts.dryRunOnly) {
        // The previous cell's umap save_map triggers a UE content-validation pass that briefly
        // blocks the editor MCP, so a back-to-back reset can transiently 502 even though the
        // editor recovers (and often even runs the reset). Retry with backoff before treating
        // it as an infra failure that aborts the whole batch.
        let reset;
        for (let attempt = 1; attempt <= 4; attempt++) {
          reset = await postJson(`${opts.serverUrl}/api/ab-eval/reset-scene`, { label, prompt: prompt.text, mode, loopMode }, 180000);
          if (reset.ok && reset.data && reset.data.ok !== false) break;
          console.log(`reset ${label} attempt ${attempt}/4 failed (${reset.error || reset.status || (reset.data && reset.data.ok)}); backing off ${5 * attempt}s…`);
          await new Promise(r => setTimeout(r, 5000 * attempt));
        }
        run.scene_reset = reset;
        fs.writeFileSync(path.join(runDir, "scene_reset.json"), JSON.stringify(reset, null, 2), "utf-8");
        if (!reset.ok || !reset.data || reset.data.ok === false) {
          run.actual = { ok: false, error: `blank-scene reset failed: ${reset.error || reset.status || JSON.stringify(reset.data)}` };
          if (opts.abortOnInfraFailure) {
            abortBatch({ opts, summary, run, reason: `${label}: ${run.actual.error}` });
            return;
          } else {
            summary.runs.push(run);
            fs.writeFileSync(path.join(opts.outDir, "summary.json"), JSON.stringify(summary, null, 2), "utf-8");
            writeReport(opts.outDir, summary);
            writeComparisonGrid(opts.outDir, summary);
            continue;
          }
        }
      }

      if (opts.preview) {
        const preview = await postSse({
          serverUrl: opts.serverUrl,
          body: { ...baseBody, assetRetrievalDryRun: true },
          outRaw: path.join(runDir, "preview_raw.sse"),
          outEvents: path.join(runDir, "preview_events.jsonl"),
          timeoutMs: opts.timeoutMs,
        });
        run.preview = { ok: preview.ok, done: preview.done, error: preview.error, duration_ms: preview.duration_ms };
        const pp = extractPromptPreview(preview.events);
        run.preview_path_count = pp && pp.pathCount || 0;
        run.preview_has_retrieved_palette = pp && pp.hasRetrievedPalette || false;
        run.retrieval_events = extractRetrievalEvents(preview.events);
        const paths = pp && Array.isArray(pp.paths) ? pp.paths : [];
        writeTextList(path.join(runDir, "retrieved_preview_paths.txt"), paths);
        fs.writeFileSync(path.join(runDir, "preview_summary.json"), JSON.stringify({ ...run.preview, prompt_preview: pp, retrieval_events: run.retrieval_events }, null, 2), "utf-8");
      }

      if (!opts.dryRunOnly) {
        run.t_start = Date.now();
        const actual = await postSse({
          serverUrl: opts.serverUrl,
          body: baseBody,
          outRaw: path.join(runDir, "actual_raw.sse"),
          outEvents: path.join(runDir, "actual_events.jsonl"),
          timeoutMs: opts.timeoutMs,
        });
        run.actual = { ok: actual.ok, done: actual.done, error: actual.error, duration_ms: actual.duration_ms };
        const spawns = extractSpawnCalls(actual.events);
        run.spawn_count = spawns.length;
        fs.writeFileSync(path.join(runDir, "spawn_calls.json"), JSON.stringify(spawns, null, 2), "utf-8");
        writeTextList(path.join(runDir, "spawned_paths.txt"), spawns.map(s => s.path).filter(Boolean));
        run.screenshots = await saveScreenshotRefs(opts.serverUrl, screenshotRefs(actual.events), path.join(runDir, "screenshots"));
        run.metrics = await computeSceneMetrics(opts);   // raw build quality (pre-straighten)
        fs.writeFileSync(path.join(runDir, "metrics.json"), JSON.stringify(run.metrics, null, 2), "utf-8");
        run.straighten = await straightenScene(opts);
        run.sky = await ensureDaytimeSky(opts);
        if (variant && variant.cfg && variant.cfg.mood) {
          // Phase A1 dual-capture: flat-lit set first (for layout A/B), then apply mood + staged set.
          await captureComparisonViews(opts, runDir, opts.comparisonViews, "comparison_views_flat");
          run.mood = await applyMood(opts, prompt.text);
          run.comparison_views = await captureComparisonViews(opts, runDir, opts.comparisonViews);
        } else {
          run.comparison_views = await captureComparisonViews(opts, runDir, opts.comparisonViews);
        }
        // Persist the evaluated (post-straighten) scene as a reloadable .umap.
        run.umap = (run.actual && run.actual.ok) ? await saveSceneUmap(opts.serverUrl, label + "__" + launchId, runDir) : { ok: false, error: "build not ok" };
        // Let the editor finish its post-save content-validation pass before the next cell's reset.
        if (run.umap && run.umap.ok) await new Promise(r => setTimeout(r, 4000));
        // Copy server-side per-scene artifacts into the run dir for later analysis:
        //   ir_scene.json + ir_scene.ascii.txt (the IR plan; IR-on cells only) and
        //   retrieved_assets.json (the asset-retrieval palette; both arms).
        try {
          const _slug = String(requestMessage).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40) || "scene";
          const _h = require("crypto").createHash("sha1").update(String(requestMessage)).digest("hex").slice(0, 8);
          const _base = _slug + "-" + _h;
          const _ws = path.resolve(__dirname, "..", "simworld_studio_workspace");
          const _irDir = process.env.IR_OUTPUT_DIR || path.join(_ws, "tmp", "ir");
          const _retDir = process.env.RETRIEVAL_OUTPUT_DIR || path.join(_ws, "tmp", "retrieval");
          // retrieved_assets.json is produced for BOTH arms; the IR plan files only for ir-on
          // (guard so an ir-off cell can't pick up a stale ir_scene.* left from a prior ir-on run).
          const _copyList = [[path.join(_retDir, _base + ".json"), "retrieved_assets.json"]];
          if (irMode === "on") _copyList.push([path.join(_irDir, _base + ".json"), "ir_scene.json"], [path.join(_irDir, _base + ".ascii.txt"), "ir_scene.ascii.txt"]);
          for (const [src, dst] of _copyList) {
            try { if (fs.existsSync(src)) fs.copyFileSync(src, path.join(runDir, dst)); } catch (_e) {}
          }
        } catch (_e) {}
        run.t_end = Date.now();
        fs.writeFileSync(path.join(runDir, "actual_summary.json"), JSON.stringify({
          actual: run.actual,
          spawn_count: run.spawn_count,
          screenshots: run.screenshots,
          comparison_views: run.comparison_views,
          umap: run.umap,
          metrics: run.metrics,
        }, null, 2), "utf-8");
        if (opts.abortOnInfraFailure && (actualHasInfraFailure(actual) || comparisonViewsHaveInfraFailure(run.comparison_views))) {
          abortBatch({ opts, summary, run, reason: `${label}: UE/screenshot infrastructure failure during actual run` });
          return;
        }
      }

      summary.runs.push(run);
      fs.writeFileSync(path.join(opts.outDir, "summary.json"), JSON.stringify(summary, null, 2), "utf-8");
      writeReport(opts.outDir, summary);
      writeComparisonGrid(opts.outDir, summary);
      }
      }
    }
  }

  summary.finished_at = new Date().toISOString();
  writeReport(opts.outDir, summary);
  writeComparisonGrid(opts.outDir, summary);
  fs.writeFileSync(path.join(opts.outDir, "summary.json"), JSON.stringify(summary, null, 2), "utf-8");
  console.log(`Report: ${path.join(opts.outDir, "ab_eval_report.md")}`);
}

main().catch(err => {
  console.error(err.stack || err.message);
  process.exit(1);
});

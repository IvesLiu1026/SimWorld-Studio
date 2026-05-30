"use strict";
// VISUAL scene-critic loop. Differs from scene-loop.js in two ways:
//   1. Per round, take N screenshots from different camera angles (multi-view) instead of one.
//   2. Critic evaluates ALL N images, then the critic's text feedback + the N image paths are
//      threaded to the next round's builder as feedback. Builder uses Read tool to view them.
//
// Why: the text-only loop ("text_loop") gives the builder only the critic's words. The builder has
// no visual context of what it just produced — so it can't see "the trees are clumped together"
// or "the sky is dark" unless the critic explicitly says so. Visual loop closes this gap.
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");
const net = require("net");

const NL = String.fromCharCode(10);
const UNREAL_HOST = process.env.UNREAL_HOST || "127.0.0.1";
const UNREAL_PORT = process.env.UNREAL_PORT || "55561";
const ARENA_ROOT = path.resolve(__dirname, "..", "..");
const VISUAL_DIR = path.join(ARENA_ROOT, "tmp", "visual_loop");
const UE_SHOTDIR = process.env.UE_SHOTDIR || "/data/siddhant/simworld_projects/Saved/Screenshots/LinuxEditor";

const CRITIC_SYSTEM_PROMPT_MULTI = `You are a 3D scene verification expert for SimWorld Studio (Unreal Engine 5).
You will be shown MULTIPLE screenshots of the SAME scene from different camera angles (typically: a 3/4 view from one side, a 3/4 view from the opposite side, and a top-down view). Use the combined views to evaluate the scene more comprehensively than a single screenshot would allow.

Evaluate:
1. Completeness: Are all requested objects present? (Cross-check across views — something hidden in one view may be visible in another.)
2. Placement: Are objects in good positions? Spatial arrangement looks right?
3. Scale: Do objects look appropriately sized relative to each other across all views?
4. Realism: Does the scene match the original request?
5. Issues: Any obvious problems (floating, buried, misaligned, upside-down, dark)?
6. Layout from top-down: Is the spatial arrangement clear and intentional? Are there obvious gaps or clusters?

Format your response as:
- **Status**: PASS / NEEDS_IMPROVEMENT / FAIL
- **Issues**: (bullet list of specific problems, or "None" if PASS — refer to views by their angle e.g. "from the top-down view, the trees are clumped on one side")
- **Suggestions**: (bullet list of specific actionable improvements the builder agent should make next)`;

// ── Low-level UE TCP send (Python eval) ────────────────────────────────────
function ueCommand(type, params, timeoutMs = 30000) {
  return new Promise((resolve) => {
    const sock = new net.Socket();
    const timer = setTimeout(() => { try { sock.destroy(); } catch (_e) {} resolve(null); }, timeoutMs);
    let buf = "";
    sock.connect(parseInt(UNREAL_PORT), UNREAL_HOST, () => {
      sock.write(JSON.stringify({ type, params: params || {} }) + NL);
    });
    sock.on("data", (d) => {
      buf += d.toString();
      try { const r = JSON.parse(buf); clearTimeout(timer); sock.destroy(); resolve(r); } catch (_e) {}
    });
    sock.on("error", () => { clearTimeout(timer); try { sock.destroy(); } catch (_e) {} resolve(null); });
  });
}

// ── Multi-view screenshot ──────────────────────────────────────────────────
// For each view config, position the editor camera, force daytime lighting + ground plane (reuses
// the exp_runner pattern), HighResShot, copy the newest png from UE Saved dir to our visual dir.
//
// Returns array of file paths in the same order as VIEW_CONFIGS.
const VIEW_CONFIGS = [
  { name: "ne_3q", bearing_deg: 135.0, pitch_deg: -22.0, kind: "elevated" }, // standard 3/4 NE
  { name: "sw_3q", bearing_deg: -45.0, pitch_deg: -22.0, kind: "elevated" }, // opposite 3/4 SW
  { name: "top",   bearing_deg: 45.0,  pitch_deg: -75.0, kind: "overhead" }, // near top-down with slight tilt
];

function buildShotScript(viewName, bearing_deg, pitch_deg, kind) {
  // Same lighting/ground-plane prep as exp_runner so visual_loop shots look like the report shots.
  // Then position camera per the view config.
  return `
import unreal, math
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
INFRA = ('Floor','Sky','Light','Atmo','Fog','Post','Sphere','World','Brush','Default','Player','GameMode','Nav','LevelBounds','Landscape','Volume','Note','Camera','Directional','Exponential','ExpRunnerGroundPlane','ExpRunnerSun','ExpRunnerSkyAtmo')
xs, ys = [], []
for a in eas.get_all_level_actors():
    lbl = a.get_actor_label()
    if lbl.startswith(INFRA): continue
    loc = a.get_actor_location()
    xs.append(loc.x); ys.append(loc.y)
if xs:
    cx = sum(xs)/len(xs); cy = sum(ys)/len(ys)
    ex = max(max(xs)-cx, cx-min(xs))
    ey = max(max(ys)-cy, cy-min(ys))
    extent = max(ex, ey, 2000.0)

    # Force daytime (only on first view per round; harmless to repeat)
    sky_killed = False
    for a in list(eas.get_all_level_actors()):
        cn = a.get_class().get_name()
        if 'Sky_Sphere' in cn or cn.startswith('BP_Sky'):
            try: eas.destroy_actor(a); sky_killed = True
            except Exception: pass
    have_dl = False
    for a in eas.get_all_level_actors():
        cn = a.get_class().get_name()
        if cn == 'DirectionalLight':
            have_dl = True
            a.set_actor_rotation(unreal.Rotator(pitch=-45.0, yaw=50.0, roll=0.0), False)
            c = a.get_component_by_class(unreal.DirectionalLightComponent)
            if c:
                try: c.set_intensity(10.0)
                except Exception: pass
        elif cn == 'SkyLight':
            c = a.get_component_by_class(unreal.SkyLightComponent)
            if c:
                try: c.set_intensity(1.0)
                except Exception: pass
                try: c.recapture_sky()
                except Exception: pass
        elif cn == 'ExponentialHeightFog':
            c = a.get_component_by_class(unreal.ExponentialHeightFogComponent)
            if c:
                try: c.set_editor_property('fog_density', 0.0005)
                except Exception: pass
    if not have_dl:
        try:
            dl = eas.spawn_actor_from_class(unreal.DirectionalLight, unreal.Vector(0,0,5000), unreal.Rotator(pitch=-45.0, yaw=50.0, roll=0.0))
            if dl:
                dl.set_actor_label('ExpRunnerSun')
                c = dl.get_component_by_class(unreal.DirectionalLightComponent)
                if c: c.set_intensity(10.0)
        except Exception: pass
    if not any(a.get_class().get_name() == 'SkyAtmosphere' for a in eas.get_all_level_actors()):
        try:
            atmo = eas.spawn_actor_from_class(unreal.SkyAtmosphere, unreal.Vector(0,0,0), unreal.Rotator(0,0,0))
            if atmo: atmo.set_actor_label('ExpRunnerSkyAtmo')
        except Exception: pass

    # Ground plane (spawn or resize)
    have_ground = False
    for a in eas.get_all_level_actors():
        if a.get_actor_label() == 'ExpRunnerGroundPlane':
            have_ground = True
            a.set_actor_location(unreal.Vector(cx, cy, -2.0), False, False)
            s = max(extent/50.0 * 2.5, 60.0)
            a.set_actor_scale3d(unreal.Vector(s, s, 1.0))
            break
    if not have_ground:
        try:
            plane_mesh = unreal.load_asset('/Engine/BasicShapes/Plane.Plane')
            actor = eas.spawn_actor_from_class(unreal.StaticMeshActor, unreal.Vector(cx, cy, -2.0), unreal.Rotator(0,0,0))
            if actor:
                actor.set_actor_label('ExpRunnerGroundPlane')
                comp = actor.get_component_by_class(unreal.StaticMeshComponent)
                if comp and plane_mesh: comp.set_static_mesh(plane_mesh)
                s = max(extent/50.0 * 2.5, 60.0)
                actor.set_actor_scale3d(unreal.Vector(s, s, 1.0))
        except Exception: pass

    # Camera per view config
    pitch_deg = ${pitch_deg}
    bearing_deg = ${bearing_deg}
    kind = "${kind}"

    if kind == "overhead":
        # Near top-down: high altitude, steep downward pitch
        cam_z = max(extent * 1.5, 3000.0)
        horiz = max(extent * 0.5, 1500.0)
    else:
        # Elevated 3/4 view: distance from XY extent, pitch determines height
        horiz = max(2.2 * extent, 4500.0)
        cam_z = max(horiz * math.tan(math.radians(abs(pitch_deg))), 700.0)

    br = math.radians(bearing_deg)
    cam_x = cx + horiz * math.cos(br)
    cam_y = cy + horiz * math.sin(br)
    dx, dy = cx - cam_x, cy - cam_y
    yaw = math.degrees(math.atan2(dy, dx))
    pitch = pitch_deg  # use the configured pitch directly

    subsys = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
    subsys.set_level_viewport_camera_info(unreal.Vector(cam_x, cam_y, cam_z), unreal.Rotator(pitch=pitch, yaw=yaw, roll=0.0))
    print("VLOOP_CAM view=${viewName} cam=(%.0f,%.0f,%.0f) pitch=%.1f yaw=%.1f extent=%.0f"%(cam_x,cam_y,cam_z,pitch,yaw,extent))
else:
    print("VLOOP_NO_ACTORS view=${viewName}")
unreal.SystemLibrary.execute_console_command(None, "HighResShot 1920x1080")
print("shot_requested view=${viewName}")
`;
}

function listLatestPngs(dir, sinceMs) {
  try {
    return fs.readdirSync(dir)
      .filter((f) => f.endsWith(".png"))
      .map((f) => ({ f, p: path.join(dir, f), m: fs.statSync(path.join(dir, f)).mtimeMs }))
      .filter((x) => x.m > sinceMs)
      .sort((a, b) => b.m - a.m);
  } catch { return []; }
}

async function waitForNewScreenshot(sinceMs, timeoutMs = 30000, exclude = new Set()) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const list = listLatestPngs(UE_SHOTDIR, sinceMs).filter((x) => !exclude.has(x.p));
    if (list.length) return list[0].p;
    await new Promise((r) => setTimeout(r, 700));
  }
  return null;
}

async function multiViewScreenshot({ round, destDir }) {
  try { fs.mkdirSync(destDir, { recursive: true }); } catch (_e) {}
  const out = [];
  // Track files already consumed in this round so the second shot doesn't race-grab the first
  // (HighResShot is async — the new file may not be on disk yet when waitForNewScreenshot polls).
  const used = new Set();
  for (let i = 0; i < VIEW_CONFIGS.length; i++) {
    const v = VIEW_CONFIGS[i];
    const before = Date.now() - 500;
    const script = buildShotScript(v.name, v.bearing_deg, v.pitch_deg, v.kind);
    await ueCommand("execute_python_script", { script }, 30000);
    // Small delay so HighResShot has time to flush before we start polling
    await new Promise((r) => setTimeout(r, 800));
    const newest = await waitForNewScreenshot(before, 25000, used);
    if (!newest) continue;
    used.add(newest);
    const dest = path.join(destDir, `round${round}_${v.name}.png`);
    try { fs.copyFileSync(newest, dest); out.push({ name: v.name, path: dest }); } catch (_e) {}
  }
  return out;
}

// ── Multi-image critic ─────────────────────────────────────────────────────
// Variant of scene-critic's runCritic that takes an array of {name, path} screenshots
// instead of a single one. Each image becomes its own content block with a "View N" label.
function parseCriticFeedback(text) {
  const t = String(text || "");
  const sm = t.match(/(?:\*\*Status\*\*|\bStatus\b)\s*:\s*([A-Za-z_]+)/i);
  let status = "NEEDS_IMPROVEMENT";
  if (sm) {
    const s = sm[1].toUpperCase();
    if (s === "PASS" || s === "NEEDS_IMPROVEMENT" || s === "FAIL") status = s;
  }
  const labelRe = /(?:\*\*(Status|Issues|Suggestions)\*\*|\b(Status|Issues|Suggestions)\b)\s*:\s*/gi;
  const positions = []; let m;
  while ((m = labelRe.exec(t)) !== null) {
    positions.push({ label: ((m[1] || m[2]) || "").toLowerCase(), start: m.index, contentStart: m.index + m[0].length });
  }
  const sectionBody = (label) => {
    const i = positions.findIndex((p) => p.label === label.toLowerCase());
    if (i < 0) return "";
    const start = positions[i].contentStart;
    const end = i + 1 < positions.length ? positions[i + 1].start : t.length;
    return t.slice(start, end);
  };
  const toBullets = (s) => (s || "")
    .split(/\n/)
    .map((l) => l.replace(/^[\s\-•*\d.)]+/, "").trim())
    .filter((x) => x && !/^none$/i.test(x) && !/^\(none\)$/i.test(x));
  return { status, issues: toBullets(sectionBody("Issues")), suggestions: toBullets(sectionBody("Suggestions")) };
}

async function getActorsSnapshot() {
  const r = await ueCommand("get_actors_in_level", {}, 15000);
  return r || {};
}

async function visualCritique({ originalPrompt, screenshots, model, timeoutMs = 180000 }) {
  if (!screenshots || !screenshots.length) {
    return { status: "FAIL", issues: ["No screenshots captured"], suggestions: [], raw: "", screenshots: [], actorsCount: 0 };
  }
  const actors = await getActorsSnapshot();

  const userContent = [];
  // Intro text
  userContent.push({
    type: "text",
    text:
      `You will see ${screenshots.length} screenshots of the SAME 3D scene from different camera angles. ` +
      `The views are labeled below each image.\n\n` +
      (originalPrompt ? `Original scene request: "${originalPrompt}"\n\n` : "") +
      `Current actors in the scene:\n${JSON.stringify(actors, null, 2)}\n\n` +
      `Now reviewing the views:`,
  });
  // Each image + a label text block
  for (const s of screenshots) {
    if (!fs.existsSync(s.path)) continue;
    const data = fs.readFileSync(s.path);
    const isJpeg = data[0] === 0xff && data[1] === 0xd8;
    userContent.push({
      type: "image",
      source: { type: "base64", media_type: isJpeg ? "image/jpeg" : "image/png", data: data.toString("base64") },
    });
    userContent.push({ type: "text", text: `(↑ view: ${s.name})` });
  }
  userContent.push({ type: "text", text: `Now produce your verdict in the required Status/Issues/Suggestions format.` });

  const CLAUDE = process.env.CLAUDE_BIN || "claude";
  const args = [
    "--input-format", "stream-json",
    "--output-format", "stream-json",
    "--verbose",
    "--dangerously-skip-permissions",
    "--append-system-prompt", CRITIC_SYSTEM_PROMPT_MULTI,
  ];
  if (model) args.push("--model", model);

  return new Promise((resolve, reject) => {
    const env = { ...process.env };
    Object.keys(env).forEach((k) => { if (k.startsWith("CLAUDE")) delete env[k]; });
    const p = spawn(CLAUDE, args, { stdio: ["pipe", "pipe", "pipe"], cwd: path.resolve(__dirname, ".."), env });
    const timer = setTimeout(() => { try { p.kill("SIGTERM"); } catch (_e) {} reject(new Error("visual critic timed out")); }, timeoutMs);
    p.stdin.write(JSON.stringify({ type: "user", message: { role: "user", content: userContent } }) + NL);
    p.stdin.end();
    let outBuf = "", errBuf = "", feedback = "", isErr = false;
    p.stdout.on("data", (d) => {
      outBuf += d.toString();
      const lines = outBuf.split(NL);
      outBuf = lines.pop() || "";
      for (const line of lines) {
        if (!line.trim()) continue;
        let ev; try { ev = JSON.parse(line); } catch { continue; }
        if (ev.type === "result") {
          if (typeof ev.result === "string" && ev.result) feedback = ev.result;
          if (ev.is_error || ev.subtype === "error_during_turn") isErr = true;
        } else if (ev.type === "assistant") {
          for (const b of (ev.message && ev.message.content) || []) {
            if (b.type === "text" && b.text) feedback += b.text;
          }
        }
      }
    });
    p.stderr.on("data", (d) => { errBuf += d.toString(); });
    p.on("error", (e) => { clearTimeout(timer); reject(e); });
    p.on("close", (code) => {
      clearTimeout(timer);
      if (isErr || code !== 0) return reject(new Error(`visual critic exited ${code}: ${errBuf.slice(0, 200)}`));
      const parsed = parseCriticFeedback(feedback);
      const actorsCount = (actors && actors.result && actors.result.actors && actors.result.actors.length) || 0;
      resolve({ ...parsed, raw: feedback, screenshots, actorsCount });
    });
  });
}

// ── Build the feedback string sent to the next round's builder ─────────────
// Contains critic's text feedback + paths the agent should Read to view current scene.
function formatVisualFeedback({ issues, suggestions, screenshots }) {
  const i = (issues || []).filter(Boolean);
  const s = (suggestions || []).filter(Boolean);
  const parts = ["The critic reviewed the current scene from multiple angles and flagged the following:"];
  parts.push(i.length ? "Issues:\n" + i.map((x) => "- " + x).join("\n") : "Issues: (none specified)");
  if (s.length) parts.push("Suggested fixes:\n" + s.map((x) => "- " + x).join("\n"));
  if (screenshots && screenshots.length) {
    parts.push(
      "VISUAL CONTEXT — to see the current state of the scene yourself before making changes, " +
      "use the Read tool on each of these screenshot files (each shows the same scene from a different angle):\n" +
      screenshots.map((s) => `- ${s.path}  (view: ${s.name})`).join("\n") +
      "\n\nLook at these images first, then refine the existing scene to address the critic's notes. " +
      "Do NOT start from scratch — modify what already exists."
    );
  } else {
    parts.push("Please refine the existing scene to address these. Do NOT start from scratch — modify what already exists.");
  }
  return parts.join("\n\n");
}

// ── Loop orchestrator ──────────────────────────────────────────────────────
async function runVisualSceneLoop({
  prompt,
  intentSummary,
  sessionId,
  maxRounds = 5,
  criticModel,
  criticTimeoutMs = 180000,
  builderRunner,
  emit,
  destDir,
}) {
  if (typeof builderRunner !== "function") throw new Error("builderRunner is required");
  let lastStatus = "NEEDS_IMPROVEMENT";
  let lastIssues = [];
  let lastSuggestions = [];
  let lastScreenshots = [];
  let lastBuilderResult = null;
  let reason = "max_iterations";
  let actualRound = 0;

  for (let round = 1; round <= maxRounds; round++) {
    actualRound = round;
    if (emit) emit("round_start", { round, max: maxRounds, mode: "visual_loop" });

    // ---- Builder turn ----
    const roundInput = {
      prompt,
      intentSummary,
      feedback: round === 1
        ? null
        : formatVisualFeedback({ issues: lastIssues, suggestions: lastSuggestions, screenshots: lastScreenshots }),
      round,
      maxRounds,
    };
    let builderResult;
    try {
      builderResult = await builderRunner(roundInput);
    } catch (e) {
      if (emit) emit("builder_done", { round, isError: true, error: String(e && e.message) });
      reason = "builder_error";
      break;
    }
    lastBuilderResult = builderResult;
    const builderErr = !!(builderResult && builderResult.isError);
    if (emit) emit("builder_done", { round, isError: builderErr });
    if (builderErr) {
      reason = "builder_error";
      break;
    }

    // ---- Multi-view capture ----
    const roundDir = path.join(destDir || VISUAL_DIR, `round${round}`);
    let shots;
    try {
      shots = await multiViewScreenshot({ round, destDir: roundDir });
    } catch (e) {
      shots = [];
    }
    if (emit) emit("multi_shots", { round, count: shots.length, paths: shots.map((s) => s.path) });
    if (!shots.length) {
      if (emit) emit("critic_verdict", { round, status: "FAIL", issues: ["multi-view screenshot failed"], suggestions: [], error: true });
      reason = "critic_error";
      break;
    }

    // ---- Visual critic ----
    let critic;
    try {
      critic = await visualCritique({
        originalPrompt: intentSummary || prompt,
        screenshots: shots,
        model: criticModel,
        timeoutMs: criticTimeoutMs,
      });
    } catch (e) {
      if (emit) emit("critic_verdict", { round, status: "FAIL", issues: ["Critic error: " + String(e && e.message)], suggestions: [], error: true });
      reason = "critic_error";
      break;
    }
    lastStatus = critic.status;
    lastIssues = critic.issues || [];
    lastSuggestions = critic.suggestions || [];
    lastScreenshots = critic.screenshots || shots;
    if (emit) emit("critic_verdict", {
      round,
      status: critic.status,
      issues: lastIssues,
      suggestions: lastSuggestions,
      screenshotUrls: (critic.screenshots || []).map((s) => `/api/screenshot/file?path=${encodeURIComponent(s.path)}`),
      actorsCount: critic.actorsCount || 0,
    });

    if (critic.status === "PASS") { reason = "pass"; break; }
    if (round >= maxRounds) { reason = "max_iterations"; break; }
  }

  const payload = {
    finalStatus: lastStatus,
    rounds: actualRound,
    reason,
    issues: lastIssues,
    suggestions: lastSuggestions,
    latestScreenshots: lastScreenshots,
    builderResult: lastBuilderResult,
    mode: "visual_loop",
  };
  if (emit) emit("loop_done", payload);
  return payload;
}

// ── /api/chat handler for visual_loop mode ────────────────────────────────
async function handleVisualSceneLoop(req, res, deps) {
  const { updateIntentSummary } = require("./intent-summarizer");
  const http = require("http");
  const { message, sessionId, skills, feedback: userFeedback, runner: outerRunner } = req.body || {};
  if (!message) { res.status(400).json({ error: "message required" }); return; }

  res.setHeader("Content-Type", "text/event-stream");
  res.setHeader("Cache-Control", "no-cache");
  res.setHeader("Connection", "keep-alive");
  res.setHeader("X-Accel-Buffering", "no");
  res.flushHeaders();
  const emit = (name, data) => { if (!res.writableEnded) res.write(`event: ${name}\ndata: ${JSON.stringify(data || {})}\n\n`); };
  const ping = setInterval(() => { if (!res.writableEnded) res.write(`: ping\n\n`); }, 5000);

  const STUDIO_SESSION = deps.STUDIO_SESSION;
  const port = parseInt(process.env.PORT || "3004", 10);
  const log = deps.logToFile || (() => {});

  emit("intent_start", {});
  const prior = (deps.intentStore && deps.intentStore.get(STUDIO_SESSION)) || "";
  let intentSummary = prior;
  try {
    intentSummary = await updateIntentSummary({
      priorSummary: prior, newPrompt: message,
      model: process.env.SUMMARIZER_MODEL || "claude-sonnet-4-6",
      timeoutMs: parseInt(process.env.SUMMARIZER_TIMEOUT_MS || "60000", 10),
    });
    if (deps.intentStore) deps.intentStore.set(STUDIO_SESSION, intentSummary);
    emit("intent_updated", { summary: intentSummary });
    log("vloop", "intent summary updated (" + intentSummary.length + " chars)");
  } catch (e) {
    intentSummary = (prior ? prior + "\n\nNEW: " : "") + message;
    log("vloop", "summarizer failed (" + e.message + ") — using fallback intent");
    emit("intent_updated", { summary: intentSummary, fallback: true });
  }

  async function builderRunner({ prompt, intentSummary, feedback, round }) {
    return new Promise((resolve) => {
      const combinedPrompt =
        `USER INTENT (cumulative across all prior prompts in this session):\n${intentSummary}\n\n` +
        `CURRENT TURN INSTRUCTION:\n${prompt}`;
      const combinedFeedback = feedback || userFeedback || undefined;
      const body = JSON.stringify({
        message: combinedPrompt,
        sessionId,
        skills: skills || [],
        feedback: combinedFeedback,
        useLoop: false, // route to the existing single-turn path
        ...(outerRunner ? { runner: outerRunner } : {}),
      });
      const opts = {
        host: "127.0.0.1", port, path: "/api/chat", method: "POST",
        headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body) },
      };
      let buf = "", isError = false, latestScreenshot = null;
      const r = http.request(opts, (inner) => {
        inner.setEncoding("utf8");
        inner.on("data", (chunk) => {
          buf += chunk;
          const parts = buf.split("\n\n");
          buf = parts.pop() || "";
          for (const part of parts) {
            if (!part.trim() || part.startsWith(":")) continue;
            const lines = part.split("\n");
            let name = null, dataStr = "";
            for (const line of lines) {
              if (line.startsWith("event:")) name = line.slice(6).trim();
              else if (line.startsWith("data:")) dataStr += line.slice(5).trim();
            }
            if (!name) continue;
            let data; try { data = JSON.parse(dataStr); } catch { data = dataStr; }
            if (name === "done") { isError = !!(data && data.isError); latestScreenshot = data && data.latestScreenshot; continue; }
            emit(name, data);
          }
        });
        inner.on("end", () => resolve({ isError, latestScreenshot }));
      });
      r.on("error", (e) => resolve({ isError: true, error: e.message }));
      r.write(body); r.end();
    });
  }

  const destDir = path.join(VISUAL_DIR, `${STUDIO_SESSION}_${Date.now()}`);
  const result = await runVisualSceneLoop({
    prompt: message,
    intentSummary,
    sessionId: STUDIO_SESSION,
    maxRounds: parseInt(process.env.SCENE_LOOP_MAX_ROUNDS || "5", 10),
    criticModel: process.env.CRITIC_MODEL || "claude-sonnet-4-6",
    criticTimeoutMs: parseInt(process.env.CRITIC_TIMEOUT_MS || "180000", 10),
    builderRunner,
    emit,
    destDir,
  });

  clearInterval(ping);
  const fatalReasons = new Set(["builder_error", "critic_error"]);
  emit("done", {
    sessionId: STUDIO_SESSION,
    isError: fatalReasons.has(result.reason),
    loop: { reason: result.reason, rounds: result.rounds, finalStatus: result.finalStatus, mode: "visual_loop" },
    latestScreenshot: (result.latestScreenshots && result.latestScreenshots[0])
      ? `/api/screenshot/file?path=${encodeURIComponent(result.latestScreenshots[0].path)}`
      : null,
  });
  res.end();
}

module.exports = {
  runVisualSceneLoop,
  formatVisualFeedback,
  visualCritique,
  multiViewScreenshot,
  handleVisualSceneLoop,
  VIEW_CONFIGS,
  VISUAL_DIR,
};

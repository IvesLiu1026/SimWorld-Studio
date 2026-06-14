"use strict";
/**
 * training.js — Agent Training stage: run the gym_env experiment engine on a generated task
 * set with an LLM controlling a ghost agent in the live UE (PIE), and stream the run to the
 * browser. SAME experiment code (`gym_env.batch_runner`) — we only orchestrate + visualize:
 * per-step camera frame (the exact image the LLM sees), the LLM reasoning + action, and
 * running metrics (success rate, distance travelled, collisions, distance-to-goal).
 *
 * Sequential curriculum: batch_runner runs exactly one wave of --n-tasks episodes, so to run
 * the whole set ONE-BY-ONE in difficulty order (easy→hard) we loop: per episode, write a
 * 1-episode file and spawn batch_runner --n-tasks 1. PIE is left running between episodes
 * (start_pie is a no-op when already active) and only ended when the whole job finishes.
 *
 * Per step batch_runner writes episode.jsonl (action/distance/path_length/success),
 * llm_raw.jsonl (reasoning + tool call) and frames/step_NNNN.png. We TAIL those and push SSE.
 * Collisions aren't logged by the engine, so we derive a "blocked" proxy (a MOVE_FORWARD that
 * produced ~no displacement = bumped into something).
 *
 * Trusted code (our engine) → spawned directly (not bwrap-sandboxed) so it can write runs/ and
 * reach the LLM API + UE.
 */

const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");

const WORKSPACE = process.env.TRAIN_WORKSPACE || path.resolve(__dirname, "../..");
const RUNS_DIR  = path.join(WORKSPACE, "runs");
const PY        = process.env.TRAIN_PYTHON || "python3";
// The launcher exports UCV_PORT (the UnrealCV port it configured for THIS UE instance). On a
// shared box other users' UEs grab ports, so this must match what our UE actually bound —
// prefer the launcher's env over any hardcoded default.
const UCV_PORT  = process.env.UCV_PORT || process.env.UNREALCV_PORT || process.env.TRAIN_UCV_PORT || "9017";
const COLLISION_MIN_MOVE_CM = 10;

// Backend: "spear" routes training to the UE 5.8 / SPEAR runner (gym_env.spear_nav_runner,
// own server+render-client cluster) instead of the legacy 5.3 / UnrealCV batch_runner.
const BACKEND   = process.env.TRAIN_BACKEND || "unrealcv";
const SPEAR_REPO = process.env.SIMWORLD_SPEAR_REPO || "/data/koe/SimWorld_SPEAR_dev";
const SPEAR_PY   = process.env.SIMWORLD_SPEAR_PYTHON || "/data/koe/spear-sim-spear/python";
const SPEAR_EXT  = process.env.SIMWORLD_SPEAR_EXT || "/data/koe/spear-sim-spear/python_ext/python";
const SPEAR_LD_PRELOAD = process.env.SPEAR_LD_PRELOAD || "/usr/lib/x86_64-linux-gnu/libstdc++.so.6";
let _spearPortSeq = 0;

const TRAIN_MODELS = [
  { id: "gpt-4o",      provider: "gpt", label: "GPT-4o (vision)" },
  { id: "gpt-4o-mini", provider: "gpt", label: "GPT-4o mini (vision)" },
];

const jobs = new Map();

function _newId() { return "run_" + Date.now().toString(36) + "_" + Math.random().toString(36).slice(2, 6); }

function _send(job, ev) {
  job.events.push(ev);
  if (job.events.length > 1000) job.events.shift();
  const line = "data: " + JSON.stringify(ev) + "\n\n";
  for (const res of job.clients) { try { res.write(line); } catch (_e) {} }
}

function _readLines(file) {
  try { return fs.readFileSync(file, "utf-8").split("\n").filter(Boolean); }
  catch (_e) { return []; }
}

function _aggSnapshot(job) {
  const a = job.agg;
  return {
    episodesTotal: a.episodesTotal, episodesDone: a.episodesDone,
    successRate: a.episodesDone ? a.success / a.episodesDone : 0,
    successCount: a.success, collisions: a.collisions,
    distanceTraveledM: +(a.distanceTraveledCm / 100).toFixed(1),
  };
}

// Tail the current episode's run dir → emit 'step' for each new episode.jsonl line + an
// 'episode_end' once summary.json appears. `epIndex` is the curriculum position (easy→hard).
function _pollEpisode(job, epName, runDir, epIndex) {
  const epDir = path.join(runDir, epName);
  let st = job.consumed.get(epName);
  if (!st) { st = { epLines: 0, llmByT: {}, llmLines: 0, prevXY: null, ended: false, epIndex, runDir }; job.consumed.set(epName, st); job.epRunDir[epName] = runDir; }

  const llm = _readLines(path.join(epDir, "llm_raw.jsonl"));
  for (let i = st.llmLines; i < llm.length; i++) { try { const d = JSON.parse(llm[i]); st.llmByT[d.t] = d; } catch (_e) {} }
  st.llmLines = llm.length;

  const ep = _readLines(path.join(epDir, "episode.jsonl"));
  for (let i = st.epLines; i < ep.length; i++) {
    let d; try { d = JSON.parse(ep[i]); } catch (_e) { continue; }
    const info = d.info || {};
    if (info.initial) { if (info.agent_xy) st.prevXY = info.agent_xy; continue; }
    const t = d.t, xy = info.agent_xy;
    let collided = false;
    if (st.prevXY && xy && /FORWARD/i.test(info.action_name || "")) {
      if (Math.hypot(xy[0] - st.prevXY[0], xy[1] - st.prevXY[1]) < COLLISION_MIN_MOVE_CM) collided = true;
    }
    if (xy) st.prevXY = xy;
    if (collided) job.agg.collisions++;
    const llmd = st.llmByT[t] || {};
    _send(job, {
      type: "step",
      episode: st.epIndex, episodeId: info.episode_id || epName, step: t,
      action: info.action_name || (d.action && d.action.tool) || "?",
      reasoning: llmd.reasoning || llmd.text || "",
      distanceToGoalCm: info.distance_to_goal_cm, pathLengthCm: info.path_length_cm,
      success: !!info.success, collided,
      frameUrl: `/api/training/${job.id}/frame?ep=${encodeURIComponent(epName)}&step=${t}`,
      tokens: llmd.usage || null, agg: _aggSnapshot(job),
    });
  }
  st.epLines = ep.length;

  if (!st.ended) {
    const sfile = path.join(epDir, "summary.json");
    if (fs.existsSync(sfile)) {
      let s = {}; try { s = JSON.parse(fs.readFileSync(sfile, "utf-8")); } catch (_e) {}
      st.ended = true;
      job.agg.episodesDone++;
      if (s.SR >= 1) job.agg.success++;
      job.agg.distanceTraveledCm += (s.path_length_cm || 0);
      _send(job, {
        type: "episode_end", episode: st.epIndex, episodeId: s.episode_id || epName,
        SR: s.SR, SPL: s.SPL, steps: s.steps, pathLengthCm: s.path_length_cm,
        endedReason: s.ended_reason, agg: _aggSnapshot(job),
      });
    }
  }
}

function _poll(job) {
  const runDir = job.currentRunDir;
  if (!runDir || !fs.existsSync(runDir)) return;
  // spear runner writes one dir per episode named `${jobId}_e${i}`; the legacy
  // batch_runner writes `ep_*` subdirs under one batch dir.
  const prefix = job.spear ? (job.id + "_e") : "ep_";
  let eps;
  try { eps = fs.readdirSync(runDir).filter(n => n.startsWith(prefix) && !n.endsWith(".json")).sort(); } catch (_e) { return; }
  for (const ep of eps) {
    const idx = job.spear ? (parseInt(ep.slice(prefix.length), 10) || 0) : job.epIndex;
    try { _pollEpisode(job, ep, runDir, idx); } catch (_e) {}
  }
}

async function _endPIE(ueExecScript) {
  try {
    await ueExecScript(
      "import unreal\nle=unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)\n" +
      "try:\n if le.is_in_play_in_editor(): le.editor_request_end_play()\nexcept Exception:\n pass", 15000);
  } catch (_e) {}
}

function registerTrainingRoutes(app, { taskSetManager, canonicalExport, ueExecScript, UNREAL_PORT, logToFile }) {
  const log = (m) => { try { logToFile && logToFile("training", m); } catch (_e) {} };

  function _finishAll(job, status) {
    if (["done", "failed", "cancelled"].includes(job.status)) return;
    job.status = status;
    if (job.poller) { clearInterval(job.poller); job.poller = null; }
    _poll(job);
    _endPIE(ueExecScript);
    _send(job, { type: "done", status, agg: _aggSnapshot(job) });
    for (const res of job.clients) { try { res.end(); } catch (_e) {} }
    job.clients.clear();
  }

  // Spawn batch_runner for episode job.epIndex (single-episode file). Sequential curriculum.
  function _runNextEpisode(job, modelDef, maxSteps, memory) {
    if (job.status === "cancelled") return;
    if (job.epIndex >= job.episodes.length) { _finishAll(job, "done"); return; }
    const ep = job.episodes[job.epIndex];
    const epFile = path.join(RUNS_DIR, `${job.id}_e${job.epIndex}.episodes.json`);
    fs.mkdirSync(RUNS_DIR, { recursive: true });
    fs.writeFileSync(epFile, canonicalExport(Object.assign({}, job.rec, { episodes: [ep] })));

    const args = [
      "-m", "gym_env.batch_runner", "--mode", "batch", "--n-tasks", "1",
      "--model", modelDef.provider, "--model-id", modelDef.id,
      "--episodes-file", epFile, "--max-steps", String(maxSteps),
      "--ucv-port", UCV_PORT, "--mcp-port", String(UNREAL_PORT),
      "--save-frames", "--no-wandb", "--memory", memory,
      "--run-name", `${job.id}_e${job.epIndex}`, "--log-level", "INFO",
    ];
    job.currentRunDir = null;
    const child = spawn(PY, args, { cwd: WORKSPACE, env: Object.assign({}, process.env), stdio: ["ignore", "pipe", "pipe"] });
    job.currentChild = child;

    const onData = (buf) => {
      const s = buf.toString();
      job.log = (job.log + s).slice(-20000);
      if (!job.currentRunDir) {
        const m = s.match(/batch output dir:\s*(\S+)/);
        if (m) {
          job.currentRunDir = path.isAbsolute(m[1]) ? m[1] : path.join(WORKSPACE, m[1]);
          if (job.status === "starting") { job.status = "running"; _send(job, { type: "status", status: "running", agg: _aggSnapshot(job) }); }
        }
      }
      if (/UnrealCV not available|Traceback|Error:|raise /i.test(s)) _send(job, { type: "log", line: s.trim().slice(0, 400) });
    };
    child.stdout.on("data", onData);
    child.stderr.on("data", onData);
    child.on("error", (e) => { _send(job, { type: "log", line: "spawn error: " + e.message }); _finishAll(job, "failed"); });
    child.on("close", () => {
      try { fs.unlinkSync(epFile); } catch (_e) {}
      _poll(job);                              // final drain for this episode
      if (job.status === "cancelled") return;
      job.epIndex++;
      _runNextEpisode(job, modelDef, maxSteps, memory);
    });
  }

  // UE 5.8 / SPEAR backend: ONE spawn drives the whole task set in one cluster
  // (server computes physics, render client serves the agent's own camera). The
  // runner writes Studio-format output per episode + prints `batch output dir:`.
  function _runSpearBackend(job, modelDef, maxSteps) {
    if (job.status === "cancelled") return;
    job.spear = true;
    fs.mkdirSync(RUNS_DIR, { recursive: true });
    const epFile = path.join(RUNS_DIR, `${job.id}.episodes.json`);
    fs.writeFileSync(epFile, canonicalExport(job.rec));   // ALL episodes, one file
    const off = (_spearPortSeq++ % 20) * 4;
    const args = [
      "-m", "gym_env.spear_nav_runner",
      "--episodes-file", epFile, "--max-steps", String(maxSteps),
      "--model-id", modelDef.id, "--root", RUNS_DIR, "--run-prefix", job.id,
      "--ue-port", String(7840 + off), "--spear-port", String(30060 + off),
      "--client-spear-port", String(31060 + off), "--beacon-port", String(17980 + off),
    ];
    const env = Object.assign({}, process.env, {
      LD_PRELOAD: SPEAR_LD_PRELOAD,
      PYTHONPATH: [SPEAR_PY, SPEAR_EXT, path.join(SPEAR_REPO, "utils"), WORKSPACE,
                   process.env.PYTHONPATH || ""].filter(Boolean).join(":"),
    });
    job.currentRunDir = RUNS_DIR;
    log(`start ${job.id}: SPEAR backend, ${job.episodes.length} eps, ports ${7840 + off}/${30060 + off}/${31060 + off}`);
    const child = spawn(PY, args, { cwd: WORKSPACE, env, stdio: ["ignore", "pipe", "pipe"] });
    job.currentChild = child;
    const onData = (buf) => {
      const s = buf.toString();
      job.log = (job.log + s).slice(-20000);
      if (job.status === "starting" && /cluster up|=== episode /i.test(s)) {
        job.status = "running";
        _send(job, { type: "status", status: "running", agg: _aggSnapshot(job) });
      }
      if (/Traceback|RuntimeError|Error:|episode .* failed:/i.test(s)) _send(job, { type: "log", line: s.trim().slice(0, 400) });
    };
    child.stdout.on("data", onData);
    child.stderr.on("data", onData);
    child.on("error", (e) => { _send(job, { type: "log", line: "spawn error: " + e.message }); _finishAll(job, "failed"); });
    child.on("close", () => {
      try { fs.unlinkSync(epFile); } catch (_e) {}
      _poll(job);
      _finishAll(job, job.status === "cancelled" ? "cancelled" : "done");
    });
  }

  app.get("/api/training/models", (req, res) => res.json({ models: TRAIN_MODELS }));

  app.get("/api/training/runs", (req, res) => {
    res.json({ runs: [...jobs.values()].map(j => ({
      id: j.id, status: j.status, taskSetId: j.taskSetId, model: j.model,
      startedAt: j.startedAt, agg: _aggSnapshot(j),
    })).sort((a, b) => (b.startedAt || "").localeCompare(a.startedAt || "")) });
  });

  app.post("/api/training/start", async (req, res) => {
    const b = req.body || {};
    const rec = taskSetManager.load(b.taskSetId);
    if (!rec) return res.status(404).json({ error: "task set not found" });
    if (!rec.episodes || !rec.episodes.length) return res.status(422).json({ error: "task set has no episodes" });
    const modelDef = TRAIN_MODELS.find(m => m.id === b.model) || TRAIN_MODELS[0];
    const maxSteps = Math.max(1, Math.min(200, parseInt(b.maxSteps, 10) || 40));
    const memory = ["none", "text", "mem0", "strategy", "hierarchical"].includes(b.memory) ? b.memory : "none";

    const id = _newId();
    const job = {
      id, status: "starting", taskSetId: rec.id, model: modelDef.id, maxSteps, memory,
      rec, episodes: rec.episodes, epIndex: 0, currentChild: null, currentRunDir: null,
      startedAt: new Date().toISOString(),
      clients: new Set(), events: [], consumed: new Map(), epRunDir: {},
      agg: { episodesTotal: rec.episodes.length, episodesDone: 0, success: 0, collisions: 0, distanceTraveledCm: 0 },
      log: "",
    };
    jobs.set(id, job);
    log(`start ${id}: model=${modelDef.id} taskSet=${rec.id} eps=${rec.episodes.length} maxSteps=${maxSteps}`);
    job.poller = setInterval(() => _poll(job), 600);
    if (BACKEND === "spear") _runSpearBackend(job, modelDef, maxSteps);
    else _runNextEpisode(job, modelDef, maxSteps, memory);
    res.status(201).json({ jobId: id, model: modelDef.id, episodes: rec.episodes.length, maxSteps, backend: BACKEND });
  });

  app.get("/api/training/:jobId/stream", (req, res) => {
    const job = jobs.get(req.params.jobId);
    if (!job) return res.status(404).json({ error: "run not found" });
    res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no" });
    res.write(": connected\n\n");
    res.write("data: " + JSON.stringify({ type: "status", status: job.status, agg: _aggSnapshot(job) }) + "\n\n");
    for (const ev of job.events) { try { res.write("data: " + JSON.stringify(ev) + "\n\n"); } catch (_e) {} }
    if (["done", "failed", "cancelled"].includes(job.status)) { res.end(); return; }
    job.clients.add(res);
    req.on("close", () => job.clients.delete(res));
  });

  app.get("/api/training/:jobId/frame", (req, res) => {
    const job = jobs.get(req.params.jobId);
    if (!job) return res.status(404).end();
    const ep = String(req.query.ep || "");
    const step = parseInt(req.query.step, 10);
    // Accept legacy `ep_*` and spear `${jobId}_e*` dir names; no dots/slashes so
    // path traversal is impossible (further guarded by the startsWith check below).
    if (!/^[A-Za-z0-9_\-]+$/.test(ep) || !Number.isInteger(step)) return res.status(400).end();
    const runDir = job.epRunDir[ep] || job.currentRunDir;
    if (!runDir) return res.status(404).end();
    const file = path.join(runDir, ep, "frames", "step_" + String(step).padStart(4, "0") + ".png");
    if (!file.startsWith(runDir) || !fs.existsSync(file)) return res.status(404).end();
    res.setHeader("Cache-Control", "no-store");
    res.sendFile(file);
  });

  app.post("/api/training/:jobId/cancel", async (req, res) => {
    const job = jobs.get(req.params.jobId);
    if (!job) return res.status(404).json({ error: "run not found" });
    job.status = "cancelled";
    if (job.currentChild && !job.currentChild.killed) { try { job.currentChild.kill("SIGTERM"); } catch (_e) {} }
    if (job.poller) { clearInterval(job.poller); job.poller = null; }
    await _endPIE(ueExecScript);
    _send(job, { type: "done", status: "cancelled", agg: _aggSnapshot(job) });
    for (const r of job.clients) { try { r.end(); } catch (_e) {} }
    job.clients.clear();
    res.json({ ok: true });
  });
}

module.exports = { registerTrainingRoutes, TRAIN_MODELS };

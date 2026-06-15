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
const HISTORY_FILE = path.join(RUNS_DIR, "history.jsonl");  // persistent run results for comparison
const PY        = process.env.TRAIN_PYTHON || "python3";
// The launcher exports UCV_PORT (the UnrealCV port it configured for THIS UE instance). On a
// shared box other users' UEs grab ports, so this must match what our UE actually bound —
// prefer the launcher's env over any hardcoded default.
const UCV_PORT  = process.env.UCV_PORT || process.env.UNREALCV_PORT || process.env.TRAIN_UCV_PORT || "9017";
const COLLISION_MIN_MOVE_CM = 10;

// UE 5.8 backend — training drives the BP pedestrian agent with REAL SPEAR: a dedicated
// cluster (server computes physics, render client renders the agent's own SpringArm camera)
// via gym_env.spear_nav_runner. The agent WALKS via MoveForward/Rotate over SPEAR RPC (not a
// teleport), and its camera frames stream to the web UI = "viewport 接入 agent camera".
const SPEAR_REPO = process.env.SIMWORLD_SPEAR_REPO || "/data/koe/SimWorld_SPEAR_dev";
const SPEAR_PY   = process.env.SIMWORLD_SPEAR_PYTHON || "/data/koe/spear-sim-spear/python";
const SPEAR_EXT  = process.env.SIMWORLD_SPEAR_EXT || "/data/koe/spear-sim-spear/python_ext/python";
const SPEAR_LD_PRELOAD = process.env.SPEAR_LD_PRELOAD || "/usr/lib/x86_64-linux-gnu/libstdc++.so.6";
// The runner needs openai/Pillow/numpy + spear — use the simworld venv unless overridden.
const TRAIN_PY = process.env.TRAIN_PYTHON || "/data/koe/.simworld/simworld-venv/bin/python3";
const DATAHUB_FILE = path.join(RUNS_DIR, "datahub.jsonl");  // one shared store, all runs append
// PixelStreaming the render client gives smooth WebRTC video of the agent BUT its render+
// encode load destabilizes the SPEAR control-loop timing — the agent coasts unpredictably
// (per-step 300-600cm vs a 180cm stride) and success rate collapses (~0-50% vs 83% without).
// So it's OFF by default (training/nav stays reliable; the viewport uses the JPEG agent-camera
// feed). Set SPEAR_PIXELSTREAM=1 to opt in for pure watching, accepting degraded navigation.
const PS_ENABLED = ["1", "true", "yes"].includes(String(process.env.SPEAR_PIXELSTREAM || "").toLowerCase());
const PS_STREAMER_PORT = PS_ENABLED ? (parseInt(process.env.CIRRUS_WS_PORT || "0", 10) || null) : null;
let _spearPortSeq = 0;
let _mcpRunningJob = null;   // one cluster at a time on this box (GPU/ports); serialize runs

// Pick the least-utilized GPU with enough free memory for the render client, so on a
// shared box the client isn't GPU-starved (a throttled client can't tick its net driver
// fast enough to finish the join handshake → ConnectionTimeout, cluster boot fails).
function _pickClientGpu() {
  if (process.env.SPEAR_CLIENT_GPU) return parseInt(process.env.SPEAR_CLIENT_GPU, 10);
  try {
    const out = require("child_process").execSync(
      "nvidia-smi --query-gpu=index,memory.free,utilization.gpu --format=csv,noheader,nounits",
      { encoding: "utf-8", timeout: 5000 });
    const gpus = out.trim().split("\n").map(l => {
      const [i, free, util] = l.split(",").map(s => parseInt(s.trim(), 10));
      return { i, free, util };
    }).filter(g => Number.isFinite(g.i) && g.free >= 5000);  // ~5GB headroom for the UE client
    if (!gpus.length) return null;
    gpus.sort((a, b) => a.util - b.util || b.free - a.free);  // least-busy, then most free
    return gpus[0].i;
  } catch (_e) { return null; }
}

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
      reasoning: llmd.reasoning || llmd.text || info.response || "",
      prompt: info.prompt || "",   // LLM input (goal/bearing/distance + injected memory)
      distanceToGoalCm: info.distance_to_goal_cm, pathLengthCm: info.path_length_cm,
      success: !!info.success, collided: collided || !!info.collided,
      agentXy: xy || null, bearingDeg: info.bearing_deg, yawDeg: info.yaw_deg,
      stuck: info.stuck || 0,
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
      // The episode's start/goal (from the task set) let the UI draw the trajectory.
      const recEp = (job.rec && job.rec.episodes && job.rec.episodes[st.epIndex]) || {};
      const _xy = (p) => Array.isArray(p) ? [p[0], p[1]] : null;
      _send(job, {
        type: "episode_end", episode: st.epIndex, episodeId: s.episode_id || epName,
        SR: s.SR, SPL: s.SPL, steps: s.steps, pathLengthCm: s.path_length_cm,
        endedReason: s.ended_reason, lesson: s.lesson || null,
        difficulty: recEp.difficulty || null,
        startXy: _xy(recEp.start_position), goalXy: _xy(recEp.goal_position),
        agg: _aggSnapshot(job),
      });
    }
  }
}

function _poll(job) {
  const runDir = job.currentRunDir;
  if (!runDir || !fs.existsSync(runDir)) return;
  let eps;
  try {
    const all = fs.readdirSync(runDir);
    // SPEAR runner writes one dir per episode named `${job.id}_e<idx>`; the legacy
    // batch_runner used `ep_*`. Match whichever this backend produces.
    eps = job.spear ? all.filter(n => n.startsWith(job.id + "_e")).sort()
                    : all.filter(n => n.startsWith("ep_")).sort();
  } catch (_e) { return; }
  for (const ep of eps) {
    const m = ep.match(/_e(\d+)$/);
    const idx = m ? parseInt(m[1], 10) : job.epIndex;
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
    // Persist the run result so the comparison view survives restarts (in-memory jobs don't).
    try {
      const a = job.agg;
      const rec = {
        jobId: job.id, model: job.model, memory: job.memory || "none", maxSteps: job.maxSteps,
        taskSetId: job.taskSetId, taskSetName: (job.rec && job.rec.name) || job.taskSetId,
        episodes: a.episodesTotal, success: a.success,
        SR: a.episodesDone ? +(a.success / a.episodesDone).toFixed(4) : 0,
        status, startedAt: job.startedAt, finishedAt: new Date().toISOString(),
      };
      fs.appendFileSync(HISTORY_FILE, JSON.stringify(rec) + "\n");
    } catch (_e) {}
    _send(job, { type: "done", status, agg: _aggSnapshot(job) });
    for (const res of job.clients) { try { res.end(); } catch (_e) {} }
    job.clients.clear();
  }

  // Terminate a run's child robustly: SIGTERM then SIGKILL. The runner can block inside a
  // SPEAR C-extension RPC (waiting on a dead cluster) where Python can't service SIGTERM
  // until a long internal timeout; SIGKILL can't be blocked.
  function _killChild(job) {
    const child = job.currentChild;
    if (!child || child.killed) return;
    try { child.kill("SIGTERM"); } catch (_e) {}
    setTimeout(() => {
      try { if (job.currentChild && !job.currentChild.killed) job.currentChild.kill("SIGKILL"); } catch (_e) {}
    }, 6000);
  }

  // Kill the UE CLUSTER processes (server + render client) — NOT just the Python runner. Killing the
  // runner alone leaves the editor children alive; they keep holding GPU/DDC/memory, and the next
  // run's server then fails its bootstrap map load ("Failed to load package /Game/Maps/empty_map")
  // because the box hasn't settled. `marker` is a log-dir path the cluster procs carry in -AbsLog.
  // NEVER touch the bound editor (it carries -ModelContextProtocolPort) or other unix users.
  function _killClusterProcs(marker) {
    try {
      require("child_process").execSync(
        `for p in $(pgrep -u "$(id -u)" -f '${marker}' 2>/dev/null); do ` +
        `a=$(ps -o args= -p "$p" 2>/dev/null); ` +
        `echo "$a" | grep -q ModelContextProtocolPort && continue; ` +
        `echo "$a" | grep -q SimWorldEditor && kill -9 "$p" 2>/dev/null; done; true`,
        { shell: "/bin/bash", stdio: "ignore", timeout: 8000 });
    } catch (_e) {}
  }

  // Before booting a new cluster, make sure NO straggler cluster procs from a prior run (cancelled,
  // failed, or crashed) are still alive in this workspace — else the new boot races them.
  function _ensureCleanBox() { _killClusterProcs(`${RUNS_DIR}/_cluster_run_`); }

  // Delete runs: drop their records from the append-only datahub, then remove their on-disk dirs
  // (episode frames, cluster logs, live jpg, episodes json). Used by Run History delete.
  function _deleteRunsFromDatahub(idSet) {
    try {
      const lines = fs.readFileSync(DATAHUB_FILE, "utf8").split("\n");
      const kept = lines.filter((ln) => {
        if (!ln.trim()) return false;
        try { return !idSet.has(JSON.parse(ln).runId); } catch (_e) { return true; }
      });
      fs.writeFileSync(DATAHUB_FILE + ".tmp", kept.join("\n") + (kept.length ? "\n" : ""));
      fs.renameSync(DATAHUB_FILE + ".tmp", DATAHUB_FILE);
    } catch (_e) {}
  }
  function _deleteRunDirs(runId) {
    try {
      for (const name of fs.readdirSync(RUNS_DIR)) {
        // Match this run's own entries only — the trailing "_" / "." / exact boundary prevents a
        // run id from also matching a longer-named sibling (run_abc vs run_abc2).
        if (name === runId || name.startsWith(runId + "_") || name.startsWith(runId + ".") ||
            name === `_cluster_${runId}`) {
          try { fs.rmSync(path.join(RUNS_DIR, name), { recursive: true, force: true }); } catch (_e) {}
        }
      }
    } catch (_e) {}
  }

  // SPEAR backend: ONE spawn drives the whole task set in one cluster (server computes
  // physics, render client serves the agent's own camera). The runner writes Studio-format
  // output per episode (`${job.id}_e<idx>/episode.jsonl|llm_raw.jsonl|frames|summary.json`)
  // and prints `batch output dir:` so the existing tailer + live charts work unchanged.
  function _runNextEpisode(job, modelDef, maxSteps, memory) {
    if (job.status === "cancelled") return;
    _ensureCleanBox();  // kill straggler cluster procs from any prior run BEFORE booting — the new
                        // server's ~30 s boot then has the box to itself by the time it loads its map
    job.spear = true;   // reuse the one-shot-runner poll path (`${job.id}_e<idx>` dirs)
    _mcpRunningJob = job.id;
    fs.mkdirSync(RUNS_DIR, { recursive: true });
    const epFile = path.join(RUNS_DIR, `${job.id}.episodes.json`);
    fs.writeFileSync(epFile, canonicalExport(job.rec));   // ALL episodes, one editor session
    const memDir = path.join(RUNS_DIR, "memory");
    fs.mkdirSync(memDir, { recursive: true });
    const memFile = path.join(memDir, `${job.taskSetId}.json`);
    const useMemory = memory && memory !== "none";
    // Resolve the task set's map to a UE path (Main → /Game/Main, demo maps → /Game/Maps/<n>).
    const mapName = (job.rec && job.rec.mapName) || "demo_2";
    const mapPath = mapName.includes("/") ? mapName
      : (mapName === "Main" ? "/Game/Main" : `/Game/Maps/${mapName}`);
    const off = (_spearPortSeq++ % 12) * 4;   // stagger ports so back-to-back runs don't clash
    const args = [
      "-m", "gym_env.spear_nav_runner",
      "--episodes-file", epFile, "--max-steps", String(maxSteps),
      "--epochs", String(job.epochs || 1),
      "--model-id", modelDef.id, "--root", RUNS_DIR, "--run-prefix", job.id,
      "--map", `${mapPath}?game=/Script/Engine.GameMode`,
      "--ue-port", String(7810 + off), "--spear-port", String(30040 + off),
      "--client-spear-port", String(31040 + off), "--beacon-port", String(17960 + off),
      "--datahub", DATAHUB_FILE, "--log-dir", path.join(RUNS_DIR, `_cluster_${job.id}`),
      // Stream the agent's camera to the SAME cirrus the bound editor uses (the WebRTC path
      // the user already reaches), under StreamerId "AgentTrain" — the viewport selects it.
      ...(PS_STREAMER_PORT ? ["--pixelstream-ws-port", String(PS_STREAMER_PORT),
                              "--pixelstream-streamer-id", "AgentTrain"] : []),
      ...(useMemory ? ["--memory-file", memFile] : []),
      ...(useMemory && job.mode === "eval" ? ["--memory-readonly"] : []),
    ];
    const clientGpu = _pickClientGpu();
    if (clientGpu != null) args.push("--client-gpu", String(clientGpu));
    const env = Object.assign({}, process.env, {
      LD_PRELOAD: SPEAR_LD_PRELOAD,
      PYTHONPATH: [SPEAR_PY, SPEAR_EXT, path.join(SPEAR_REPO, "utils"), WORKSPACE,
                   process.env.PYTHONPATH || ""].filter(Boolean).join(":"),
    });
    job.currentRunDir = RUNS_DIR;
    log(`start ${job.id}: SPEAR cluster, ${job.episodes.length} eps, ports ${7810 + off}/${30040 + off}, gpu ${clientGpu}, map ${mapPath}`);
    const child = spawn(TRAIN_PY, args, { cwd: WORKSPACE, env, stdio: ["ignore", "pipe", "pipe"] });
    job.currentChild = child;
    const onData = (buf) => {
      const s = buf.toString();
      job.log = (job.log + s).slice(-20000);
      if (job.status === "starting" && /cluster up|=== episode /i.test(s)) {
        job.status = "running";
        _send(job, { type: "status", status: "running", agg: _aggSnapshot(job) });
      }
      if (/Traceback|RuntimeError|Error:|could not connect|episode .* failed:/i.test(s)) _send(job, { type: "log", line: s.trim().slice(0, 400) });
    };
    child.stdout.on("data", onData);
    child.stderr.on("data", onData);
    child.on("error", (e) => { _mcpRunningJob = null; _send(job, { type: "log", line: "spawn error: " + e.message }); _finishAll(job, "failed"); });
    child.on("close", (code) => {
      if (_mcpRunningJob === job.id) _mcpRunningJob = null;
      try { fs.unlinkSync(epFile); } catch (_e) {}
      _poll(job);
      let status = job.status === "cancelled" ? "cancelled" : "done";
      if (status !== "cancelled" && code && job.agg.episodesDone === 0) {
        status = "failed";
        _send(job, { type: "log", line: `runner exited with code ${code} and no episodes completed` });
      }
      _finishAll(job, status);
    });
  }

  app.get("/api/training/models", (req, res) => res.json({ models: TRAIN_MODELS }));

  app.get("/api/training/runs", (req, res) => {
    res.json({ runs: [...jobs.values()].map(j => ({
      id: j.id, status: j.status, taskSetId: j.taskSetId, model: j.model, memory: j.memory || "none",
      startedAt: j.startedAt, agg: _aggSnapshot(j),
    })).sort((a, b) => (b.startedAt || "").localeCompare(a.startedAt || "")) });
  });

  // Shared datahub: every run/episode/step the runner appended to runs/datahub.jsonl,
  // regrouped run -> episode -> step. The frontend reads this directly (decoupled from
  // any one job's SSE) so opening the page always shows the latest/running data.
  // Hierarchy: run -> epochs -> tasks (a "task" is one episode/pointnav; an "epoch" is one pass
  // over all tasks; memory carries across epochs, so SR-per-epoch is the learning curve).
  function _readDatahub(filterRunId) {
    const order = [];
    const runs = new Map();
    const ensureEpoch = (run, e) => {
      if (!run.epochs.has(e)) run.epochs.set(e, { epoch: e, tasks: new Map() });
      return run.epochs.get(e);
    };
    const ensureTask = (run, e, idx) => {
      const ep = ensureEpoch(run, e);
      if (!ep.tasks.has(idx)) ep.tasks.set(idx, { idx, epoch: e, runName: `${run.runId}_ep${e}_e${idx}`, steps: [] });
      return ep.tasks.get(idx);
    };
    for (const ln of _readLines(DATAHUB_FILE)) {
      let r; try { r = JSON.parse(ln); } catch (_e) { continue; }
      if (!r.runId || (filterRunId && r.runId !== filterRunId)) continue;
      if (!runs.has(r.runId)) { runs.set(r.runId, { runId: r.runId, model: r.model, epochs: new Map() }); order.push(r.runId); }
      const run = runs.get(r.runId);
      const e = r.epoch || 0;
      if (r.kind === "run_start") {
        run.model = r.model; run.tasksPerEpoch = r.tasks ?? r.episodes; run.epochsTotal = r.epochs || 1;
        run.maxSteps = r.maxSteps; run.memory = r.memory; run.backend = r.backend || run.backend;
        run.startedAt = r.startedAt || run.startedAt;
      } else if (r.kind === "run_end") {
        run.success = r.success; run.SR = r.SR; run.ended = true;
      } else if (r.kind === "epoch_end") {
        const ep = ensureEpoch(run, e);
        ep.SR = r.SR; ep.success = r.success; ep.tasksTotal = r.tasks; ep.lessons = r.lessons; ep.done = true;
      } else if (r.kind === "episode") {
        const t = ensureTask(run, e, r.episodeIdx);
        Object.assign(t, { episodeId: r.episodeId, SR: r.SR, SPL: r.SPL, endedReason: r.endedReason,
          startXy: r.startXy, goalXy: r.goalXy, lesson: r.lesson, done: true });
      } else if (r.kind === "step") {
        const t = ensureTask(run, e, r.episodeIdx);
        t.episodeId = t.episodeId || r.episodeId; t.goalXy = t.goalXy || r.goalXy;
        t.steps.push({ step: r.step, action: r.action, input: r.input, output: r.output,
          agentXy: r.agentXy, distanceCm: r.distanceCm, bearingDeg: r.bearingDeg, yawDeg: r.yawDeg,
          stuck: r.stuck, frame: r.frame });
      }
    }
    const toArr = (run) => {
      const epochs = [...run.epochs.values()].sort((a, b) => a.epoch - b.epoch).map((ep) => {
        const tasks = [...ep.tasks.values()].sort((a, b) => a.idx - b.idx);
        const done = tasks.filter((t) => t.done);
        const successCount = done.filter((t) => t.SR >= 1).length;
        return { epoch: ep.epoch, tasks, done: ep.done, lessons: ep.lessons,
          tasksTotal: ep.tasksTotal ?? run.tasksPerEpoch ?? tasks.length, tasksDone: done.length, successCount,
          SR: ep.SR != null ? ep.SR : (done.length ? +(successCount / done.length).toFixed(4) : null) };
      });
      // Learning curve = SR per epoch (finished epochs first, plus the in-progress one's running SR).
      const learningCurve = epochs.map((ep) => ({ epoch: ep.epoch, SR: ep.SR, success: ep.successCount,
        tasksDone: ep.tasksDone, tasksTotal: ep.tasksTotal, lessons: ep.lessons, done: ep.done }));
      // Flat task list (chronological) for the live-camera "latest frame".
      const flat = epochs.flatMap((ep) => ep.tasks);
      const doneAll = flat.filter((t) => t.done);
      return { runId: run.runId, model: run.model, memory: run.memory, maxSteps: run.maxSteps, backend: run.backend,
        startedAt: run.startedAt, ended: run.ended,
        epochsTotal: run.epochsTotal || epochs.length || 1, tasksPerEpoch: run.tasksPerEpoch,
        epochs, learningCurve, episodes: flat,
        tasksDone: doneAll.length, successCount: doneAll.filter((t) => t.SR >= 1).length,
        SR: run.SR != null ? run.SR : (doneAll.length ? +(doneAll.filter((t) => t.SR >= 1).length / doneAll.length).toFixed(4) : 0) };
    };
    return order.map(id => toArr(runs.get(id)));
  }

  // Full datahub (all runs) or one run via ?runId=. Frames map to /api/training/:jobId/frame.
  app.get("/api/training/datahub", (req, res) => {
    res.set("Cache-Control", "no-store");
    res.json({ runs: _readDatahub(req.query.runId) });
  });
  // The most-recently-active run (running or finished) — what the page shows on open.
  app.get("/api/training/datahub/latest", (req, res) => {
    res.set("Cache-Control", "no-store");
    const all = _readDatahub();
    const run = all.length ? all[all.length - 1] : null;
    const live = run ? (jobs.get(run.runId) || null) : null;
    res.json({ run, status: live ? live.status : (run && run.ended ? "done" : "unknown") });
  });

  // Persistent run history (for the baseline-vs-trained comparison view). Optional
  // ?taskSetId=... filter. Most recent first.
  app.get("/api/training/history", (req, res) => {
    let recs = [];
    try {
      recs = fs.readFileSync(HISTORY_FILE, "utf-8").split("\n").filter(Boolean)
        .map(l => { try { return JSON.parse(l); } catch (_e) { return null; } }).filter(Boolean);
    } catch (_e) {}
    if (req.query.taskSetId) recs = recs.filter(r => r.taskSetId === req.query.taskSetId);
    recs.sort((a, b) => (b.finishedAt || "").localeCompare(a.finishedAt || ""));
    res.json({ history: recs.slice(0, 50) });
  });

  app.post("/api/training/start", async (req, res) => {
    const b = req.body || {};
    const rec = taskSetManager.load(b.taskSetId);
    if (!rec) return res.status(404).json({ error: "task set not found" });
    if (!rec.episodes || !rec.episodes.length) return res.status(422).json({ error: "task set has no episodes" });
    const modelDef = TRAIN_MODELS.find(m => m.id === b.model) || TRAIN_MODELS[0];
    const maxSteps = Math.max(1, Math.min(200, parseInt(b.maxSteps, 10) || 40));
    const memory = ["none", "text", "mem0", "strategy", "hierarchical"].includes(b.memory) ? b.memory : "none";
    // Optional episode-count cap: run only the first N of the task set's episodes (a quick
    // run / shorter cluster session). Empty/0 = all. Clamp to the task set size.
    const nEps = parseInt(b.maxEpisodes, 10);
    if (Number.isFinite(nEps) && nEps > 0 && nEps < rec.episodes.length) {
      rec.episodes = rec.episodes.slice(0, nEps);
    }
    // Epochs: run the whole task set this many times; memory accumulates across epochs so SR
    // per epoch is the learning curve. Default 1 (a single pass).
    const epochs = Math.max(1, Math.min(20, parseInt(b.epochs, 10) || 1));

    // One bound editor → one training run at a time (a second run would fight over the
    // same agent/camera in the shared editor). Reject fast with the active run's id.
    if (_mcpRunningJob) {
      const active = jobs.get(_mcpRunningJob);
      if (active && ["starting", "running"].includes(active.status)) {
        return res.status(409).json({ error: "a training run is already driving the editor", activeJobId: _mcpRunningJob });
      }
      _mcpRunningJob = null;
    }

    const id = _newId();
    const job = {
      id, status: "starting", taskSetId: rec.id, model: modelDef.id, maxSteps, memory, epochs,
      rec, episodes: rec.episodes, epIndex: 0, currentChild: null, currentRunDir: null,
      startedAt: new Date().toISOString(),
      clients: new Set(), events: [], consumed: new Map(), epRunDir: {},
      agg: { episodesTotal: rec.episodes.length, episodesDone: 0, success: 0, collisions: 0, distanceTraveledCm: 0 },
      log: "",
    };
    jobs.set(id, job);
    log(`start ${id}: model=${modelDef.id} taskSet=${rec.id} eps=${rec.episodes.length} maxSteps=${maxSteps}`);
    job.poller = setInterval(() => _poll(job), 600);
    _runNextEpisode(job, modelDef, maxSteps, memory);
    res.status(201).json({ jobId: id, model: modelDef.id, episodes: rec.episodes.length, maxSteps });
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

  // Real-time agent-camera broadcast: the run's single 'live' JPEG, overwritten by the
  // runner several times per stride. The viewport polls this at ~5 Hz for a live feed.
  app.get("/api/training/:jobId/live", (req, res) => {
    const id = String(req.params.jobId || "");
    if (!/^[A-Za-z0-9_\-]+$/.test(id)) return res.status(400).end();
    const file = path.join(RUNS_DIR, `${id}_live.jpg`);
    if (!file.startsWith(RUNS_DIR) || !fs.existsSync(file)) return res.status(404).end();
    res.setHeader("Cache-Control", "no-store");
    res.sendFile(file);
  });

  app.get("/api/training/:jobId/frame", (req, res) => {
    const ep = String(req.query.ep || "");
    const step = parseInt(req.query.step, 10);
    // Accept legacy `ep_*` and `${jobId}_e*` dir names; no dots/slashes so path traversal
    // is impossible (further guarded by the startsWith check below).
    if (!/^[A-Za-z0-9_\-]+$/.test(ep) || !Number.isInteger(step)) return res.status(400).end();
    // Live job knows the exact run dir; otherwise (datahub/history view after a restart)
    // fall back to the shared RUNS_DIR where every run writes its episode frames.
    const job = jobs.get(req.params.jobId);
    const runDir = (job && (job.epRunDir[ep] || job.currentRunDir)) || RUNS_DIR;
    const file = path.join(runDir, ep, "frames", "step_" + String(step).padStart(4, "0") + ".png");
    if (!file.startsWith(runDir) || !fs.existsSync(file)) return res.status(404).end();
    res.setHeader("Cache-Control", "no-store");
    res.sendFile(file);
  });

  app.post("/api/training/:jobId/cancel", async (req, res) => {
    const job = jobs.get(req.params.jobId);
    if (!job) return res.status(404).json({ error: "run not found" });
    job.status = "cancelled";
    _killChild(job);
    // Full teardown: the runner alone doesn't take the UE cluster down fast enough, so kill its
    // server + render client now and clear the lock. A second sweep after a short settle catches any
    // that were mid-spawn, so a Stop → immediate Start finds a genuinely clean box.
    _killClusterProcs(`${RUNS_DIR}/_cluster_${job.id}`);
    if (_mcpRunningJob === job.id) _mcpRunningJob = null;
    if (job.poller) { clearInterval(job.poller); job.poller = null; }
    await new Promise((r) => setTimeout(r, 1500));
    _killClusterProcs(`${RUNS_DIR}/_cluster_${job.id}`);
    await _endPIE(ueExecScript);
    _send(job, { type: "done", status: "cancelled", agg: _aggSnapshot(job) });
    for (const r of job.clients) { try { r.end(); } catch (_e) {} }
    job.clients.clear();
    res.json({ ok: true });
  });

  // Delete one or more past runs (Run History → delete / batch-delete). Skips a run that is
  // currently live (must be Stopped first). Removes datahub records + on-disk dirs.
  app.post("/api/training/delete", (req, res) => {
    const ids = Array.isArray(req.body && req.body.runIds) ? req.body.runIds : [];
    const valid = ids.filter((id) => typeof id === "string" && /^run_[a-z0-9_]+$/i.test(id));
    const deleted = [], skipped = [];
    const toDelete = new Set();
    for (const id of valid) {
      if (id === _mcpRunningJob) { skipped.push(id); continue; }   // never delete a live run
      if (!toDelete.has(id)) { toDelete.add(id); deleted.push(id); }
    }
    if (toDelete.size) {
      _deleteRunsFromDatahub(toDelete);
      for (const id of toDelete) { _deleteRunDirs(id); jobs.delete(id); }
    }
    res.json({ ok: true, deleted, skipped });
  });
}

module.exports = { registerTrainingRoutes, TRAIN_MODELS };

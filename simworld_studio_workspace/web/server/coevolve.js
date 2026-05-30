"use strict";
// ─────────────────────────────────────────────────────────────────────────────
// coevolve.js — backend for the Co-evolution (adaptive curriculum) mode.
//
// Orchestrates the SAME experiment code the lab runs offline: `python -m co_evolve`
// (co_evolve/loop.py). That loop co-evolves a CODING agent (designs scenes+tasks at
// a teacher-chosen difficulty) against an EMBODIED nav agent (executes episodes in
// UE), recording per-generation metrics (SR, SPL, difficulty, coding reward, …).
//
// We only orchestrate + stream: spawn the process, tail the per-generation JSONL the
// loop writes (generations.jsonl), and expose status. A live run needs:
//   - reachable coding + nav LLM endpoints (CODING_BASE_URL / NAV_BASE_URL), and
//   - a UE instance on the configured UCV/MCP ports.
// Both are configurable per-request or via env.
// ─────────────────────────────────────────────────────────────────────────────
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");

const WORKSPACE = process.env.COEVOLVE_WORKSPACE || process.env.TRAIN_WORKSPACE || path.resolve(__dirname, "../..");
const RUNS_DIR = path.join(WORKSPACE, "runs", "co_evolve");
const PY = process.env.COEVOLVE_PYTHON || process.env.TRAIN_PYTHON || "python3";

function registerCoevolveRoutes(app, { logToFile } = {}) {
  const log = typeof logToFile === "function" ? logToFile : () => {};
  let job = null; // { id, status, startedAt, finishedAt, exitCode, runDir, cfg, proc, logBuf }

  // The runner creates a timestamped subdir (coevolve_YYYYMMDD_HHMMSS) under
  // --output-dir and writes generations.jsonl there — so look one level down too.
  function findGenFile(runDir) {
    try {
      const direct = path.join(runDir, "generations.jsonl");
      if (fs.existsSync(direct)) return direct;
      const subs = fs.readdirSync(runDir, { withFileTypes: true })
        .filter((e) => e.isDirectory())
        .map((e) => path.join(runDir, e.name, "generations.jsonl"))
        .filter((f) => fs.existsSync(f));
      return subs.length ? subs[subs.length - 1] : null;
    } catch { return null; }
  }

  function readGenerations(runDir) {
    const f = findGenFile(runDir);
    if (!f) return [];
    try {
      return fs.readFileSync(f, "utf-8").split("\n").filter(Boolean)
        .map((l) => { try { return JSON.parse(l); } catch { return null; } })
        .filter(Boolean);
    } catch { return []; }
  }

  // Compact a generation record down to what the UI charts/lists need (the raw
  // record carries trajectories/strategies we don't want to push over SSE).
  function compactGen(g) {
    return {
      generation: g.generation,
      sr: g.sr, spl: g.spl, avgSteps: g.avg_steps,
      nEpisodes: g.n_episodes, nSuccess: g.n_success,
      difficulty: g.difficulty_score,
      blockedRatio: g.blocked_ratio,
      codingReward: g.coding_reward,
      sceneId: g.scene_id, taskType: g.task_type,
      minPathCm: g.min_path_cm, maxPathCm: g.max_path_cm,
      teacherTarget: g.teacher_target, teacherBand: g.teacher_band, inBand: g.in_band,
      reasoning: typeof g.task_reasoning === "string" ? g.task_reasoning.slice(0, 160) : "",
    };
  }

  function getStatus() {
    if (!job) return { status: "idle", generations: [], cfg: null };
    const gens = readGenerations(job.runDir).map(compactGen);
    return {
      id: job.id, status: job.status, startedAt: job.startedAt, finishedAt: job.finishedAt,
      exitCode: job.exitCode, runDir: job.runDir, cfg: job.cfg,
      generations: gens,
      lastLog: (job.logBuf || []).slice(-14).join(""),
    };
  }

  app.post("/api/coevolve/start", (req, res) => {
    if (job && job.status === "running") return res.status(409).json({ error: "a co-evolution run is already in progress", id: job.id });
    const b = req.body || {};
    const id = "coev_" + Date.now().toString(36);
    const runDir = path.join(RUNS_DIR, id);
    try { fs.mkdirSync(runDir, { recursive: true }); } catch (e) { return res.status(500).json({ error: "mkdir failed: " + e.message }); }

    const cfg = {
      generations: Math.max(1, Math.min(200, parseInt(b.generations, 10) || 30)),
      episodesPerGen: Math.max(1, Math.min(64, parseInt(b.episodesPerGen, 10) || 8)),
      maxSteps: Math.max(1, Math.min(500, parseInt(b.maxSteps, 10) || 40)),
      teacher: ["alpgmm", "epsilon_greedy", "fixed"].includes(b.teacher) ? b.teacher : "alpgmm",
      noRgb: !!b.noRgb, // text-only nav (no per-step RGB) — far faster on VL models / many ghosts
      waveSize: parseInt(b.waveSize, 10) || undefined,
      // Default to the studio's OWN UE (the env the studio launched with), so
      // co-evolution drives the same Editor the user sees — not a separate one.
      ucvPort: parseInt(b.ucvPort, 10) || parseInt(process.env.UCV_PORT, 10) || parseInt(process.env.COEVOLVE_UCV_PORT, 10) || 9018,
      mcpPort: parseInt(b.mcpPort, 10) || parseInt(process.env.UNREAL_PORT, 10) || parseInt(process.env.COEVOLVE_MCP_PORT, 10) || 55564,
    };
    const args = [
      "-m", "co_evolve", "--mode", "live",
      "--generations", String(cfg.generations),
      "--episodes-per-gen", String(cfg.episodesPerGen),
      "--max-steps", String(cfg.maxSteps),
      "--teacher", cfg.teacher,
      "--ucv-port", String(cfg.ucvPort),
      "--mcp-port", String(cfg.mcpPort),
      "--output-dir", runDir,
    ];
    if (cfg.noRgb) args.push("--no-rgb");
    if (cfg.waveSize) args.push("--wave-size", String(cfg.waveSize));
    // LLM endpoints: per-request overrides, else inherit env (co_evolve/config.py reads these).
    const env = Object.assign({}, process.env);
    if (b.codingModelId) env.CODING_MODEL_ID = String(b.codingModelId);
    if (b.codingBaseUrl) env.CODING_BASE_URL = String(b.codingBaseUrl);
    if (b.codingApiKey) env.CODING_API_KEY = String(b.codingApiKey);
    if (b.navModelId) env.NAV_MODEL_ID = String(b.navModelId);
    if (b.navBaseUrl) env.NAV_BASE_URL = String(b.navBaseUrl);
    if (b.navApiKey) env.NAV_API_KEY = String(b.navApiKey);

    let proc;
    try {
      proc = spawn(PY, args, { cwd: WORKSPACE, env, stdio: ["ignore", "pipe", "pipe"] });
    } catch (e) {
      return res.status(500).json({ error: "spawn failed: " + e.message });
    }
    job = { id, status: "running", startedAt: new Date().toISOString(), finishedAt: null,
      exitCode: null, runDir, cfg, proc, logBuf: [] };
    const onOut = (d) => { job.logBuf.push(d.toString()); if (job.logBuf.length > 400) job.logBuf.splice(0, job.logBuf.length - 400); };
    proc.stdout.on("data", onOut);
    proc.stderr.on("data", (d) => { onOut(d); log("coevolve", d.toString().slice(0, 200)); });
    proc.on("close", (code) => { job.status = code === 0 ? "done" : (job.status === "cancelled" ? "cancelled" : "error"); job.exitCode = code; job.finishedAt = new Date().toISOString(); log("coevolve", `run ${id} exited code=${code}`); });
    proc.on("error", (e) => { job.status = "error"; job.errorMsg = e.message; log("coevolve", `run ${id} error: ${e.message}`); });
    log("coevolve", `started ${id} gens=${cfg.generations} teacher=${cfg.teacher} ucv=${cfg.ucvPort} mcp=${cfg.mcpPort}`);
    res.status(201).json({ id, status: "running", runDir, cfg });
  });

  app.post("/api/coevolve/stop", (req, res) => {
    if (job && job.proc && !job.proc.killed && job.status === "running") {
      job.status = "cancelled";
      try { job.proc.kill("SIGTERM"); } catch {}
    }
    res.json({ ok: true, status: job ? job.status : "idle" });
  });

  app.get("/api/coevolve/status", (req, res) => { res.set("Cache-Control", "no-store"); res.json(getStatus()); });

  return { getStatus };
}

module.exports = { registerCoevolveRoutes };

import { useEffect, useState } from "react";
import { API_BASE } from "../../api/client.js";

// Layered live-training state: a SESSION (the run) holds EPISODES, each episode holds a
// TRAJECTORY of per-step ACTIONS. The browser drills session → episode → trajectory → action.
const initialTrainingState = {
  jobId: null,
  selectedRunId: null, // when set, the monitor shows THIS past run (from Run History) instead of live
  status: "idle",      // idle | starting | running | done | failed | cancelled
  mode: "train",
  model: null,
  taskSetId: null,
  episodesTotal: 0,
  agg: null,
  current: null,       // latest step event (live)
  steps: [],           // flat recent steps (live action log), capped
  episodes: [],        // [{episode, episodeId, done, SR, SPL, steps, endedReason, lesson,
                       //   difficulty, startXy, goalXy, trajectory:[stepEvent...]}]
  epHistory: [],       // [{episode, SR, cumSuccessRate, SPL}] for the success-rate chart
  distSeries: [],      // [{x, dist}] live distance-to-goal (m)
  lastLog: null,
  error: null,
};

const MAX_STEPS = 300;
const MAX_DIST = 600;

function _ensureEpisode(eps, idx, episodeId) {
  let ep = eps.find((e) => e.episode === idx);
  if (!ep) {
    ep = { episode: idx, episodeId: episodeId || `ep_${idx}`, done: false, trajectory: [],
           SR: null, SPL: null, steps: 0, endedReason: null, lesson: null, difficulty: null,
           startXy: null, goalXy: null };
    eps.push(ep);
    eps.sort((a, b) => a.episode - b.episode);
  }
  return ep;
}

export const trainingStore = {
  s: initialTrainingState,
  subs: new Set(),
  _es: null,
  _retries: 0,
  _timer: null,

  set(patch) {
    trainingStore.s = { ...trainingStore.s, ...patch };
    trainingStore.subs.forEach((listener) => listener());
  },

  subscribe(listener) {
    trainingStore.subs.add(listener);
    return () => trainingStore.subs.delete(listener);
  },

  start(jobId, model, episodesTotal, opts = {}) {
    trainingStore.stop();
    trainingStore.set({
      ...initialTrainingState,
      jobId, model, episodesTotal,
      mode: opts.mode || "train",
      taskSetId: opts.taskSetId || null,
      status: "starting",
    });
    trainingStore._retries = 0;
    trainingStore._connect(jobId);
  },

  // Poll the backend for a live run and attach to it automatically — so a run
  // started anywhere (API/curl, another tab, a reload mid-run) streams into this
  // browser without the user having to click Start Training.
  async autoAttach() {
    try {
      const r = await fetch(`${API_BASE}/training/runs`);
      const { runs = [] } = await r.json();
      const live = runs.find((j) => j.status === "running" || j.status === "starting");
      const cur = trainingStore.s;
      // Already tracking this live run, or actively watching one — leave it alone.
      if (live && live.id === cur.jobId) return;
      const watching = cur.jobId && !["done", "failed", "cancelled", "idle"].includes(cur.status);
      if (live && !watching) {
        trainingStore.start(live.id, live.model, (live.agg && live.agg.episodesTotal) || 0, {
          taskSetId: live.taskSetId,
          mode: "train",
        });
      }
    } catch {}
  },

  _connect(jobId) {
    if (trainingStore.s.jobId !== jobId) return;
    const source = new EventSource(`${API_BASE}/training/${jobId}/stream`);
    trainingStore._es = source;
    source.onmessage = (event) => {
      try { trainingStore._ev(JSON.parse(event.data)); } catch {}
    };
    source.onerror = () => {
      try { source.close(); } catch {}
      trainingStore._es = null;
      const live = trainingStore.s.jobId === jobId &&
        !["done", "failed", "cancelled", "idle"].includes(trainingStore.s.status);
      if (live && trainingStore._retries < 5) {
        trainingStore._retries += 1;
        trainingStore._timer = setTimeout(() => trainingStore._connect(jobId),
          Math.min(1000 * 2 ** trainingStore._retries, 15000));
      }
    };
  },

  stop() {
    if (trainingStore._timer) { clearTimeout(trainingStore._timer); trainingStore._timer = null; }
    if (trainingStore._es) { try { trainingStore._es.close(); } catch {} trainingStore._es = null; }
  },

  _ev(event) {
    trainingStore._retries = 0;
    const s = trainingStore.s;
    if (event.type === "status") {
      trainingStore.set({ status: event.status, agg: event.agg || s.agg, error: null });
    } else if (event.type === "step") {
      const episodes = s.episodes.slice();
      const ep = _ensureEpisode(episodes, event.episode, event.episodeId);
      ep.trajectory = ep.trajectory.concat([event]);
      ep.steps = ep.trajectory.length;
      const patch = {
        status: "running",
        current: event,
        agg: event.agg || s.agg,
        steps: s.steps.concat([event]).slice(-MAX_STEPS),
        episodes,
      };
      if (event.distanceToGoalCm != null) {
        patch.distSeries = s.distSeries
          .concat([{ x: s.distSeries.length, dist: +(event.distanceToGoalCm / 100).toFixed(2) }])
          .slice(-MAX_DIST);
      }
      trainingStore.set(patch);
    } else if (event.type === "episode_end") {
      const episodes = s.episodes.slice();
      const ep = _ensureEpisode(episodes, event.episode, event.episodeId);
      Object.assign(ep, {
        done: true,
        SR: typeof event.SR === "number" ? event.SR : (event.SR ? 1 : 0),
        SPL: typeof event.SPL === "number" ? event.SPL : null,
        steps: event.steps != null ? event.steps : ep.steps,
        endedReason: event.endedReason, lesson: event.lesson || null,
        difficulty: event.difficulty || null,
        startXy: event.startXy || ep.startXy, goalXy: event.goalXy || ep.goalXy,
      });
      const agg = event.agg || s.agg;
      trainingStore.set({
        agg, episodes,
        epHistory: s.epHistory.concat([{
          episode: event.episode,
          SR: ep.SR,
          SPL: ep.SPL,
          cumSuccessRate: agg ? agg.successRate : null,
        }]),
      });
    } else if (event.type === "done") {
      trainingStore.set({ status: event.status || "done", agg: event.agg || s.agg });
      trainingStore.stop();
    } else if (event.type === "log") {
      trainingStore.set({ lastLog: event.line });
    }
  },
};

export function useTraining() {
  const [, force] = useState(0);
  useEffect(() => trainingStore.subscribe(() => force((value) => value + 1)), []);
  return trainingStore.s;
}

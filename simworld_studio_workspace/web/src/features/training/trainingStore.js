import { useEffect, useState } from "react";
import { API_BASE } from "../../api/client.js";

const initialTrainingState = {
  jobId: null,
  status: "idle",
  model: null,
  episodesTotal: 0,
  current: null,
  agg: null,
  steps: [],
  lastLog: null,
  // Live chart series.
  epHistory: [],   // one point per finished episode: {episode, SR, SPL, cumSuccessRate}
  distSeries: [],  // distance-to-goal (m) per step: {x, dist}
};

const MAX_DIST_POINTS = 400;

export const trainingStore = {
  s: initialTrainingState,
  subs: new Set(),
  _es: null,

  set(patch) {
    trainingStore.s = { ...trainingStore.s, ...patch };
    trainingStore.subs.forEach((listener) => listener());
  },

  subscribe(listener) {
    trainingStore.subs.add(listener);
    return () => trainingStore.subs.delete(listener);
  },

  start(jobId, model, episodesTotal) {
    trainingStore.stop();
    trainingStore.set({
      jobId,
      model,
      episodesTotal,
      status: "starting",
      current: null,
      agg: null,
      steps: [],
      lastLog: null,
      epHistory: [],
      distSeries: [],
    });
    const source = new EventSource(`${API_BASE}/training/${jobId}/stream`);
    trainingStore._es = source;
    source.onmessage = (event) => {
      try {
        trainingStore._ev(JSON.parse(event.data));
      } catch {}
    };
    source.onerror = () => {};
  },

  stop() {
    if (trainingStore._es) {
      try {
        trainingStore._es.close();
      } catch {}
      trainingStore._es = null;
    }
  },

  _ev(event) {
    if (event.type === "status") {
      trainingStore.set({ status: event.status, agg: event.agg || trainingStore.s.agg });
    } else if (event.type === "step") {
      const patch = {
        status: "running",
        current: event,
        agg: event.agg,
        steps: trainingStore.s.steps.concat([event]).slice(-200),
      };
      if (event.distanceToGoalCm != null) {
        const prev = trainingStore.s.distSeries;
        patch.distSeries = prev
          .concat([{ x: prev.length, dist: +(event.distanceToGoalCm / 100).toFixed(2) }])
          .slice(-MAX_DIST_POINTS);
      }
      trainingStore.set(patch);
    } else if (event.type === "episode_end") {
      const agg = event.agg || trainingStore.s.agg;
      trainingStore.set({
        agg,
        epHistory: trainingStore.s.epHistory.concat([{
          episode: event.episode,
          SR: typeof event.SR === "number" ? event.SR : (event.SR ? 1 : 0),
          SPL: typeof event.SPL === "number" ? event.SPL : null,
          cumSuccessRate: agg ? agg.successRate : null,
        }]),
      });
    } else if (event.type === "done") {
      trainingStore.set({ status: event.status || "done", agg: event.agg || trainingStore.s.agg });
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

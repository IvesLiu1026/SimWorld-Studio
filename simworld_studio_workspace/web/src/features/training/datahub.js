// Shared training-datahub helpers + hooks. The three training surfaces (config panel, monitor,
// viewport) all poll the same datahub endpoints and reuse the same status colors / frame URLs;
// this module is the single source so they stay in sync and don't re-implement the polling.
import { useEffect, useState } from "react";
import { API_BASE } from "../../api/client.js";

// Status colors used across the training UI (success / failure / pending).
export const OK = "#3ddc84";
export const FAIL = "#e0673a";
export const PEND = "var(--ink-3)";

// URL of a captured observation frame for a run/episode/step (the agent's first-person LLM input).
export const frameUrl = (runId, runName, step) =>
  `${API_BASE}/training/${runId}/frame?ep=${encodeURIComponent(runName)}&step=${step}`;

// Generic polling-JSON hook: fetch `url` now and every `pollMs`, returning the latest payload
// (or null before the first response). Safe against unmount.
export function useJson(url, pollMs) {
  const [data, setData] = useState(null);
  useEffect(() => {
    let on = true;
    const tick = async () => {
      try { const r = await fetch(url); const j = await r.json(); if (on) setData(j); } catch (_e) {}
    };
    tick();
    const t = setInterval(tick, pollMs);
    return () => { on = false; clearInterval(t); };
  }, [url, pollMs]);
  return data;
}

// The latest/live run (datahub). Returns the raw { status, run } payload (or null).
export function useLatestRun(pollMs = 1500) {
  return useJson(`${API_BASE}/training/datahub/latest`, pollMs);
}

// All runs (datahub), newest-first for Run History. Returns an array (empty before first load).
export function useAllRuns(pollMs = 2000) {
  const j = useJson(`${API_BASE}/training/datahub`, pollMs);
  return j && j.runs ? j.runs.slice().reverse() : [];
}

// Is this run live (booting or running)?
export const isLiveStatus = (status) => status === "running" || status === "starting";

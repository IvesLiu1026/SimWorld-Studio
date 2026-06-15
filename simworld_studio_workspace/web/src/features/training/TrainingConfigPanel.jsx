import React, { useEffect, useState } from "react";
import { API_BASE } from "../../api/client.js";
import { trainingStore, useTraining } from "./trainingStore.js";
import { useLatestRun, useAllRuns, isLiveStatus } from "./datahub.js";

// Persist the run config across sessions (a multi-user box: reopening the page keeps your last
// model / memory / epochs / tasks / maxSteps / taskset instead of resetting to defaults).
const LS_KEY = "simworld.trainConfig.v1";
const _loadCfg = () => { try { return JSON.parse(localStorage.getItem(LS_KEY) || "{}"); } catch (_e) { return {}; } };

export default function TrainingConfigPanel({ icons, sessionId }) {
  const st = useTraining();
  const [_cfg] = useState(_loadCfg);
  const [taskSets, setTaskSets] = useState([]);
  const [taskSetId, setTaskSetId] = useState(_cfg.taskSetId || "");
  const [models, setModels] = useState([]);
  const [model, setModel] = useState(_cfg.model || "gpt-4o");
  const [maxSteps, setMaxSteps] = useState(_cfg.maxSteps || "40");
  const [maxEpisodes, setMaxEpisodes] = useState(_cfg.maxEpisodes || "");
  const [epochs, setEpochs] = useState(_cfg.epochs || "1");
  const [memory, setMemory] = useState(_cfg.memory || "none");
  // Persist on any change.
  useEffect(() => {
    try { localStorage.setItem(LS_KEY, JSON.stringify({ model, maxSteps, maxEpisodes, epochs, memory, taskSetId })); }
    catch (_e) {}
  }, [model, maxSteps, maxEpisodes, epochs, memory, taskSetId]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  // Datahub-driven live status + run history (multi-user: reflects ANY run, not just ours).
  const latest = useLatestRun(2000);
  const liveRun = latest && isLiveStatus(latest.status) ? { ...latest.run, status: latest.status } : null;
  const allRuns = useAllRuns(2000);
  // Run History delete: a select mode with per-run checkboxes + batch delete.
  const [selMode, setSelMode] = useState(false);
  const [selRuns, setSelRuns] = useState(() => new Set());
  const toggleSel = (id) => setSelRuns((prev) => {
    const next = new Set(prev); next.has(id) ? next.delete(id) : next.add(id); return next;
  });
  async function deleteSelected() {
    const runIds = [...selRuns];
    if (!runIds.length) return;
    if (!window.confirm(`Delete ${runIds.length} run(s)? This removes their data and frames.`)) return;
    try {
      await fetch(`${API_BASE}/training/delete`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ runIds }),
      });
    } catch (_e) {}
    if (selRuns.has(st.selectedRunId)) trainingStore.set({ selectedRunId: null });
    setSelRuns(new Set()); setSelMode(false);
    // The Run History list refreshes from the datahub poll (≤2 s), reflecting the deletion.
  }

  useEffect(() => {
    fetch(`${API_BASE}/tasksets`)
      .then((response) => response.json())
      .then((data) => {
        const list = data.taskSets || [];
        setTaskSets(list);
        // Keep the persisted taskset if it still exists; otherwise fall back to the first.
        setTaskSetId((previous) => (list.some((t) => t.id === previous) ? previous : (list[0]?.id || "")));
      })
      .catch(() => {});

    fetch(`${API_BASE}/training/models`)
      .then((response) => response.json())
      .then((data) => {
        setModels(data.models || []);
        if (data.models?.[0]) setModel((previous) => previous || data.models[0].id);
      })
      .catch(() => {});
  }, []);

  const running = !!liveRun || st.status === "running" || st.status === "starting";
  const agg = st.agg || {};

  async function start() {
    setBusy(true);
    setErr(null);
    try {
      const response = await fetch(`${API_BASE}/training/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ taskSetId, model, maxSteps: parseInt(maxSteps, 10) || 40, memory,
          maxEpisodes: parseInt(maxEpisodes, 10) || 0, epochs: parseInt(epochs, 10) || 1 }),
      });
      const data = await response.json();
      if (!response.ok) {
        setErr(data.error || `HTTP ${response.status}`);
        return;
      }
      trainingStore.start(data.jobId, data.model, data.episodes, { taskSetId, mode: "train" });
    } catch (error) {
      setErr(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function cancel() {
    // Cancel whatever run is live (datahub), not just one we started — multi-user safe.
    const id = (liveRun && liveRun.runId) || st.jobId;
    if (id) {
      await fetch(`${API_BASE}/training/${id}/cancel`, { method: "POST" }).catch(() => {});
    }
    trainingStore.stop();
    trainingStore.set({ status: "cancelled" });
    // liveRun is derived from the datahub poll; it clears on its own once the cancel lands.
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" }}>
      <div className="config-section">
        <div className="config-section-title">Experiment Setup</div>
        <div className="config-row">
          <label>Task set</label>
          <select className="config-select" value={taskSetId} onChange={(event) => setTaskSetId(event.target.value)} disabled={running}>
            {taskSets.length === 0 && <option value="">- generate one first -</option>}
            {taskSets.map((taskSet) => (
              <option key={taskSet.id} value={taskSet.id}>
                {taskSet.name} / {taskSet.summary?.episodeCount || 0} eps
              </option>
            ))}
          </select>
        </div>
        <div className="config-row">
          <label>Agent LLM</label>
          <select className="config-select" value={model} onChange={(event) => setModel(event.target.value)} disabled={running}>
            {models.map((modelDef) => (
              <option key={modelDef.id} value={modelDef.id}>
                {modelDef.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div className="config-section" style={{ flex: 1, overflow: "auto" }}>
        <div className="config-section-title">Run Config</div>
        <div className="config-row">
          <label>Max steps / episode</label>
          <input className="config-input" value={maxSteps} onChange={(event) => setMaxSteps(event.target.value)} disabled={running} />
        </div>
        <div className="config-row">
          <label>Tasks (blank = all)</label>
          <input className="config-input" type="number" min="1" placeholder="all" value={maxEpisodes}
            onChange={(event) => setMaxEpisodes(event.target.value)} disabled={running} />
        </div>
        <div className="config-row">
          <label>Epochs (learning curve)</label>
          <input className="config-input" type="number" min="1" max="20" value={epochs}
            onChange={(event) => setEpochs(event.target.value)} disabled={running} />
        </div>
        <div className="config-row">
          <label>Memory</label>
          <select className="config-select" value={memory} onChange={(event) => setMemory(event.target.value)} disabled={running}>
            {[
              ["none", "none — baseline (no learning)"],
              ["text", "text — memory-trained (writes lessons)"],
              ["hierarchical", "hierarchical"],
            ].map(([value, label]) => (
              <option key={value} value={value}>{label}</option>
            ))}
          </select>
        </div>
        <div style={{ fontSize: 11, color: "var(--ink-3)", padding: "2px 2px", lineHeight: 1.5 }}>
          Episodes run easy→hard in a dedicated UE 5.8 SPEAR cluster, driven live by the selected LLM. With
          memory on, the agent writes a lesson after each collision and applies it next time. Watch it in the Monitor.
        </div>

        <div className="config-section-title" style={{ marginTop: 12 }}>Live Run</div>
        {liveRun ? [
          ["Status", liveRun.status],
          ["Epoch", `${(liveRun.epochs || []).filter((e) => e.done).length} / ${liveRun.epochsTotal || 1}`],
          ["Tasks done", `${liveRun.tasksDone || 0} / ${(liveRun.epochsTotal || 1) * (liveRun.tasksPerEpoch || 0)}`],
          ["Success rate", liveRun.tasksDone ? `${Math.round((liveRun.SR || 0) * 100)}%` : "-"],
        ].map(([label, value]) => (
          <div key={label} className="status-row"><span>{label}</span><span className="status-score">{value}</span></div>
        )) : <div style={{ fontSize: 11, color: "var(--ink-3)", padding: "2px" }}>No run in progress.</div>}

        <div className="config-section-title" style={{ marginTop: 12, display: "flex",
          justifyContent: "space-between", alignItems: "center" }}>
          <span>Run History</span>
          {allRuns.length > 0 && (selMode ? (
            <span style={{ display: "flex", gap: 6 }}>
              <button onClick={deleteSelected} disabled={!selRuns.size}
                style={{ fontSize: 10, padding: "1px 7px", borderRadius: 5, cursor: selRuns.size ? "pointer" : "default",
                  border: "1px solid var(--red)", background: "transparent",
                  color: selRuns.size ? "var(--red)" : "var(--ink-3)", fontWeight: 700 }}>
                Delete ({selRuns.size})
              </button>
              <button onClick={() => { setSelMode(false); setSelRuns(new Set()); }}
                style={{ fontSize: 10, padding: "1px 7px", borderRadius: 5, cursor: "pointer",
                  border: "1px solid var(--line)", background: "transparent", color: "var(--ink-2)" }}>
                Cancel
              </button>
            </span>
          ) : (
            <button onClick={() => setSelMode(true)}
              style={{ fontSize: 10, padding: "1px 7px", borderRadius: 5, cursor: "pointer",
                border: "1px solid var(--line)", background: "transparent", color: "var(--ink-2)" }}>
              Select
            </button>
          ))}
        </div>
        <div style={{ maxHeight: 220, overflow: "auto" }}>
          {allRuns.length === 0 && <div style={{ fontSize: 11, color: "var(--ink-3)" }}>No past runs yet.</div>}
          {allRuns.map((r) => {
            const live = r.runId === (liveRun && liveRun.runId);
            const selected = r.runId === st.selectedRunId;
            const checked = selRuns.has(r.runId);
            const sr = Math.round((r.SR || 0) * 100);
            const onRowClick = () => {
              if (selMode) { if (!live) toggleSel(r.runId); }
              else trainingStore.set({ selectedRunId: live ? null : r.runId });
            };
            return (
              <div key={r.runId} onClick={onRowClick}
                title={selMode ? (live ? "Live run — stop it first to delete" : "Select to delete") : "View this run in the monitor"}
                style={{ padding: "5px 6px", borderBottom: "1px solid var(--line)", fontSize: 11,
                  cursor: selMode && live ? "default" : "pointer",
                  background: checked ? "rgba(255,95,99,0.12)" : selected ? "var(--violet-soft)" : "transparent",
                  borderLeft: checked ? "2px solid var(--red)" : selected ? "2px solid var(--violet)" : "2px solid transparent",
                  opacity: selMode && live ? 0.5 : 1 }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <span style={{ display: "flex", alignItems: "center", gap: 6, minWidth: 0 }}>
                    {selMode && <input type="checkbox" checked={checked} disabled={live} readOnly
                      style={{ pointerEvents: "none", accentColor: "var(--red)" }} />}
                    <span style={{ fontFamily: "monospace", color: live ? "#3ddc84" : selected ? "var(--violet)" : "var(--ink-2)",
                      overflow: "hidden", textOverflow: "ellipsis" }}>
                      {live ? "● " : ""}{r.runId.replace(/^run_/, "")}
                    </span>
                  </span>
                  <span style={{ fontWeight: 700, color: sr >= 50 ? "#3ddc84" : "var(--ink)" }}>{r.tasksDone ? `${sr}%` : "…"}</span>
                </div>
                <div style={{ color: "var(--ink-3)", fontSize: 10, marginTop: 1 }}>
                  {r.model} · mem {r.memory || "none"} · {r.epochsTotal || 1} epoch{(r.epochsTotal || 1) > 1 ? "s" : ""}
                  {" "}· {r.tasksPerEpoch != null ? `${r.tasksPerEpoch} tasks` : `${r.tasksDone || 0} tasks`}
                  {r.maxSteps ? ` · ${r.maxSteps} steps` : ""}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      <div style={{ padding: "10px 14px", borderTop: "1px solid var(--line)", display: "flex", flexDirection: "column", gap: 6 }}>
        {err && <div style={{ fontSize: 11, color: "var(--red)" }}>Error: {err}</div>}
        {!running ? (
          <button className="primary-cta violet" style={{ width: "100%", justifyContent: "center" }} onClick={start} disabled={busy || !taskSetId}>
            {busy ? (
              "Starting..."
            ) : (
              <>
                {icons.activity(13)} Start Training
              </>
            )}
          </button>
        ) : (
          <button className="primary-cta orange" style={{ width: "100%", justifyContent: "center" }} onClick={cancel}>
            {icons.collision(13)} Stop
          </button>
        )}
      </div>
    </div>
  );
}

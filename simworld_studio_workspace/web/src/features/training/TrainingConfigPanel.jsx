import React, { useEffect, useState } from "react";
import { API_BASE } from "../../api/client.js";
import { trainingStore, useTraining } from "./trainingStore.js";

export default function TrainingConfigPanel({ icons, sessionId }) {
  const st = useTraining();
  const [taskSets, setTaskSets] = useState([]);
  const [taskSetId, setTaskSetId] = useState("");
  const [models, setModels] = useState([]);
  const [model, setModel] = useState("gpt-4o");
  const [maxSteps, setMaxSteps] = useState("40");
  const [memory, setMemory] = useState("none");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  useEffect(() => {
    fetch(`${API_BASE}/tasksets`)
      .then((response) => response.json())
      .then((data) => {
        const list = data.taskSets || [];
        setTaskSets(list);
        setTaskSetId((previous) => previous || list[0]?.id || "");
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

  const running = st.status === "running" || st.status === "starting";
  const agg = st.agg || {};

  async function start() {
    setBusy(true);
    setErr(null);
    try {
      const response = await fetch(`${API_BASE}/training/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ taskSetId, model, maxSteps: parseInt(maxSteps, 10) || 40, memory }),
      });
      const data = await response.json();
      if (!response.ok) {
        setErr(data.error || `HTTP ${response.status}`);
        return;
      }
      trainingStore.start(data.jobId, data.model, data.episodes);
    } catch (error) {
      setErr(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function cancel() {
    if (st.jobId) {
      await fetch(`${API_BASE}/training/${st.jobId}/cancel`, { method: "POST" }).catch(() => {});
    }
    trainingStore.stop();
    trainingStore.set({ status: "cancelled" });
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
          <label>Memory</label>
          <select className="config-select" value={memory} onChange={(event) => setMemory(event.target.value)} disabled={running}>
            {["none", "text", "hierarchical"].map((option) => (
              <option key={option}>{option}</option>
            ))}
          </select>
        </div>
        <div style={{ fontSize: 11, color: "var(--ink-3)", padding: "2px 2px", lineHeight: 1.5 }}>
          Episodes run one-by-one, easy to hard. The agent enters PIE and is driven live by the selected LLM; watch
          it in the Agent Monitor.
        </div>

        <div className="config-section-title" style={{ marginTop: 12 }}>
          Live Metrics
        </div>
        {[
          ["Status", st.status],
          ["Episode", `${agg.episodesDone || 0} / ${st.episodesTotal || agg.episodesTotal || 0}`],
          ["Success rate", agg.episodesDone ? `${Math.round((agg.successRate || 0) * 100)}%` : "-"],
          ["Collisions", String(agg.collisions ?? 0)],
          ["Distance travelled", agg.distanceTraveledM != null ? `${agg.distanceTraveledM} m` : "-"],
        ].map(([label, value]) => (
          <div key={label} className="status-row">
            <span>{label}</span>
            <span className="status-score">{value}</span>
          </div>
        ))}
        {st.lastLog && <div className="training-last-log">{st.lastLog}</div>}
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

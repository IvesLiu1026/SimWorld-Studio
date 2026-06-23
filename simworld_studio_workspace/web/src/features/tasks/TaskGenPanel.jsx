import React, { useState } from "react";
import { API_BASE } from "../../api/client.js";

export default function TaskGenPanel({ icons, sessionId }) {
  const [taskType, setTaskType] = useState("PointNav");
  const [episodes, setEpisodes] = useState("100");
  const [minPath, setMinPath] = useState("3");
  const [maxPath, setMaxPath] = useState("30");
  const [successR, setSuccessR] = useState("0.5");
  const [maxSteps, setMaxSteps] = useState("500");
  const [generating, setGenerating] = useState(false);
  const [result, setResult] = useState(null);

  async function generate() {
    setGenerating(true);
    setResult(null);
    try {
      const body = {
        taskType: taskType.toLowerCase(),
        episodes: parseInt(episodes, 10) || 100,
        minPathCm: (parseFloat(minPath) || 0) * 100,
        maxPathCm: (parseFloat(maxPath) || 0) * 100,
        successRadiusM: parseFloat(successR) || 0.5,
        maxSteps: parseInt(maxSteps, 10) || 500,
        sceneId: sessionId || null,
      };
      const response = await fetch(`${API_BASE}/tasks/generate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await response.json();
      if (!response.ok) {
        setResult({ ok: false, msg: data.error || `HTTP ${response.status}` });
        return;
      }
      setResult({
        ok: true,
        msg: `Generated ${data.generated}/${data.requested} episodes on "${data.taskSet.mapName || "current scene"}"`,
      });
      window.dispatchEvent(new CustomEvent("sw-taskset-changed", { detail: { id: data.taskSet.id } }));
    } catch (error) {
      setResult({ ok: false, msg: error.message });
    } finally {
      setGenerating(false);
    }
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" }}>
      <div className="config-section">
        <div className="config-section-title">Task Builder</div>
        <div className="config-row">
          <label>Task type</label>
          <select className="config-select" value={taskType} onChange={(event) => setTaskType(event.target.value)}>
            <option>PointNav</option>
            <option>ObjectNav</option>
          </select>
        </div>
        <div style={{ fontSize: 11, color: "var(--ink-3)", padding: "2px 2px 0", lineHeight: 1.5 }}>
          Builds a navmesh on the <b>currently-loaded scene</b> and samples reachable start-goal episodes with
          ground-truth paths.
        </div>
      </div>

      <div className="config-section" style={{ flex: 1, overflow: "auto" }}>
        <div className="config-section-title">Sampling Parameters</div>
        {[
          ["Episodes", episodes, setEpisodes],
          ["Min path (m)", minPath, setMinPath],
          ["Max path (m)", maxPath, setMaxPath],
          ["Success radius (m)", successR, setSuccessR],
          ["Max episode steps", maxSteps, setMaxSteps],
        ].map(([label, value, setter]) => (
          <div key={label} className="config-row">
            <label>{label}</label>
            <input className="config-input" value={value} onChange={(event) => setter(event.target.value)} />
          </div>
        ))}

        {taskType === "PointNav" && (
          <>
            <div className="config-section-title" style={{ marginTop: 12 }}>
              PointNav Options
            </div>
            {[
              ["Require NavMesh", true],
              ["Filter by path length", true],
              ["Sample reachable pairs", true],
            ].map(([label, value]) => (
              <div key={label} className="config-row">
                <label>{label}</label>
                <span style={{ fontSize: 12, color: value ? "var(--green)" : "var(--ink-3)", fontWeight: 600 }}>
                  {value ? "On" : "Off"}
                </span>
              </div>
            ))}
          </>
        )}

        {taskType === "ObjectNav" && (
          <>
            <div className="config-section-title" style={{ marginTop: 12 }}>
              ObjectNav Options
            </div>
            {[
              ["Target category", "Any"],
              ["Require reachable", "Yes"],
              ["Visible from path", "Yes"],
            ].map(([label, value]) => (
              <div key={label} className="config-row">
                <label>{label}</label>
                <span className="config-val">{value}</span>
              </div>
            ))}
          </>
        )}
      </div>

      <div style={{ padding: "10px 14px", borderTop: "1px solid var(--line)", display: "flex", flexDirection: "column", gap: 6 }}>
        {result && (
          <div
            style={{
              fontSize: 11,
              lineHeight: 1.4,
              padding: "6px 8px",
              borderRadius: 6,
              border: `1px solid ${result.ok ? "var(--green)" : "var(--red)"}`,
              color: result.ok ? "var(--green)" : "var(--red)",
              background: "var(--bg-tertiary)",
            }}
          >
            {result.ok ? "OK: " : "Error: "}
            {result.msg}
          </div>
        )}
        <button className="primary-cta green" style={{ width: "100%", justifyContent: "center" }} onClick={generate} disabled={generating}>
          {generating ? (
            "Building navmesh & sampling..."
          ) : (
            <>
              {icons.target(13)} Generate Tasks
            </>
          )}
        </button>
      </div>
    </div>
  );
}

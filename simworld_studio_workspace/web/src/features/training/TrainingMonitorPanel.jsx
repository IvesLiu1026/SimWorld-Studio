import React, { useEffect, useRef, useMemo } from "react";
import { useTraining } from "./trainingStore.js";
import PlotlyChart from "../../components/charts/PlotlyChart.jsx";

export default function TrainingMonitorPanel() {
  const st = useTraining();
  const cur = st.current;
  const agg = st.agg || {};
  const logRef = useRef(null);

  const srTraces = useMemo(() => {
    const h = st.epHistory;
    if (!h.length) return null;
    const eps = h.map((d) => d.episode);
    return [
      {
        x: eps,
        y: h.map((d) => (d.cumSuccessRate != null ? +(d.cumSuccessRate * 100).toFixed(1) : null)),
        mode: "lines",
        line: { color: "#3ddc84", width: 2, shape: "spline" },
        fill: "tozeroy",
        fillcolor: "rgba(61,220,132,0.10)",
        name: "success %",
        hovertemplate: "ep %{x}: %{y:.0f}%<extra></extra>",
      },
      {
        x: eps,
        y: h.map((d) => d.SR * 100),
        mode: "markers",
        marker: { color: h.map((d) => (d.SR >= 0.5 ? "#3ddc84" : "#e0673a")), size: 6 },
        name: "episode",
        hovertemplate: "ep %{x}: %{customdata}<extra></extra>",
        customdata: h.map((d) => (d.SR >= 0.5 ? "success" : "fail")),
      },
    ];
  }, [st.epHistory]);

  const distTraces = useMemo(() => {
    const d = st.distSeries;
    if (d.length < 2) return null;
    return [
      {
        x: d.map((p) => p.x),
        y: d.map((p) => p.dist),
        mode: "lines",
        line: { color: "#5b9dff", width: 1.6 },
        name: "dist-to-goal (m)",
        hovertemplate: "step %{x}: %{y:.1f} m<extra></extra>",
      },
    ];
  }, [st.distSeries]);

  const splTraces = useMemo(() => {
    const h = st.epHistory.filter((d) => d.SPL != null);
    if (!h.length) return null;
    return [
      {
        x: h.map((d) => d.episode),
        y: h.map((d) => +(d.SPL).toFixed(3)),
        type: "bar",
        marker: { color: "#a78bfa" },
        hovertemplate: "ep %{x}: SPL %{y:.2f}<extra></extra>",
      },
    ];
  }, [st.epHistory]);

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [st.steps.length]);

  if (st.status === "idle" || !st.jobId) {
    return (
      <div style={{ padding: 16, fontSize: 12, color: "var(--ink-3)", lineHeight: 1.6 }}>
        No active run. Pick a task set and LLM on the left, then <b>Start Training</b>. The agent camera,
        reasoning, action, and live metrics appear here every step.
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" }}>
      <div style={{ padding: "8px 10px" }}>
        <div className="training-camera-frame">
          {cur?.frameUrl ? (
            <img src={cur.frameUrl} alt="agent camera" />
          ) : (
            <span style={{ color: "var(--ink-3)", fontSize: 12 }}>waiting for first frame...</span>
          )}
          {cur && (
            <div className="training-camera-chip">
              ep {cur.episode} / step {cur.step}
              {cur.collided ? " / collision" : ""}
            </div>
          )}
        </div>
      </div>

      <div style={{ display: "flex", gap: 6, padding: "0 10px 8px", flexWrap: "wrap" }}>
        {[
          ["SR", agg.episodesDone ? `${Math.round((agg.successRate || 0) * 100)}%` : "-"],
          ["eps", `${agg.episodesDone || 0}/${st.episodesTotal || 0}`],
          ["collisions", agg.collisions ?? 0],
          ["dist", agg.distanceTraveledM != null ? `${agg.distanceTraveledM}m` : "-"],
          ["d-goal", cur?.distanceToGoalCm != null ? `${(cur.distanceToGoalCm / 100).toFixed(1)}m` : "-"],
        ].map(([label, value]) => (
          <div key={label} className="training-metric-tile">
            <div className="training-metric-value">{value}</div>
            <div className="training-metric-label">{label}</div>
          </div>
        ))}
      </div>

      {(srTraces || distTraces || splTraces) && (
        <div style={{ padding: "0 10px 6px", display: "flex", flexDirection: "column", gap: 6 }}>
          {srTraces && (
            <PlotlyChart
              title="Success rate over episodes"
              traces={srTraces}
              height={148}
              layout={{ yaxis: { range: [0, 100], ticksuffix: "%" }, xaxis: { title: "" } }}
            />
          )}
          {distTraces && (
            <PlotlyChart
              title="Distance to goal (m) - live"
              traces={distTraces}
              height={130}
            />
          )}
          {splTraces && (
            <PlotlyChart title="SPL per episode" traces={splTraces} height={120} />
          )}
        </div>
      )}

      <div style={{ padding: "0 10px 6px" }}>
        <div style={{ fontSize: 11, color: "var(--ink-3)", marginBottom: 2 }}>
          LLM - <b style={{ color: "var(--violet)" }}>{cur ? cur.action : "-"}</b>
        </div>
        <div style={{ fontSize: 12, color: "var(--ink-2)", lineHeight: 1.4, maxHeight: 64, overflow: "auto" }}>
          {cur?.reasoning ? cur.reasoning : <span style={{ color: "var(--ink-3)" }}>(model returned no rationale text)</span>}
        </div>
      </div>

      <div className="config-section-title" style={{ padding: "4px 10px" }}>
        Step log
      </div>
      <div ref={logRef} className="training-step-log">
        {st.steps.map((step, index) => (
          <div key={index} className={`training-step-row${step.collided ? " collided" : ""}`}>
            <span className="training-step-time">
              e{step.episode}/t{step.step}
            </span>
            <span className="training-step-action">{step.action}</span>
            <span className="training-step-reasoning">{step.reasoning || ""}</span>
            {step.success && <span className="training-step-success">OK</span>}
          </div>
        ))}
      </div>
    </div>
  );
}

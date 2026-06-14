import React, { useEffect, useRef } from "react";
import Plotly from "plotly.js-dist-min";

/**
 * Thin, interactive Plotly wrapper for live training metrics.
 *
 * Interactive out of the box: hover tooltips, box/scroll zoom, pan, and a
 * double-click to autoscale. Styled dark to match the Studio shell. Updates
 * efficiently in place via Plotly.react when `traces`/`layout` change, so it is
 * safe to re-render every SSE tick.
 */
const BASE_LAYOUT = {
  paper_bgcolor: "rgba(0,0,0,0)",
  plot_bgcolor: "rgba(0,0,0,0)",
  font: { color: "#9aa4b2", size: 10, family: "inherit" },
  margin: { l: 38, r: 10, t: 8, b: 24 },
  showlegend: false,
  hovermode: "x unified",
  xaxis: { gridcolor: "rgba(255,255,255,0.06)", zeroline: false, automargin: true },
  yaxis: { gridcolor: "rgba(255,255,255,0.06)", zeroline: false, automargin: true },
  dragmode: "pan",
};

const CONFIG = {
  responsive: true,
  displayModeBar: "hover",
  displaylogo: false,
  scrollZoom: true,
  modeBarButtonsToRemove: ["select2d", "lasso2d", "autoScale2d", "toggleSpikelines"],
};

export default function PlotlyChart({ traces, layout = {}, height = 150, title }) {
  const ref = useRef(null);

  useEffect(() => {
    if (!ref.current) return;
    const merged = {
      ...BASE_LAYOUT,
      ...layout,
      xaxis: { ...BASE_LAYOUT.xaxis, ...(layout.xaxis || {}) },
      yaxis: { ...BASE_LAYOUT.yaxis, ...(layout.yaxis || {}) },
    };
    Plotly.react(ref.current, traces || [], merged, CONFIG);
  }, [traces, layout]);

  useEffect(() => () => { if (ref.current) Plotly.purge(ref.current); }, []);

  return (
    <div>
      {title && (
        <div style={{ fontSize: 11, color: "var(--ink-3)", fontWeight: 600, margin: "2px 0 1px 2px" }}>
          {title}
        </div>
      )}
      <div ref={ref} style={{ width: "100%", height }} />
    </div>
  );
}

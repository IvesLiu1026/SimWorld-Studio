import React from "react";

export default function LineChart({ color = "var(--blue)", H = 80, label, series, unit = "", W = 440 }) {
  if (!series || series.length < 2) {
    return (
      <div className="line-chart-empty" style={{ width: W, height: H }}>
        {label}: no data yet
      </div>
    );
  }

  const pad = { t: 8, r: 6, b: 22, l: 36 };
  const innerWidth = W - pad.l - pad.r;
  const innerHeight = H - pad.t - pad.b;
  const minValue = Math.min(...series);
  const maxValue = Math.max(...series);
  const valueRange = maxValue - minValue || 1;
  const toX = (index) => pad.l + (index / (series.length - 1)) * innerWidth;
  const toY = (value) => pad.t + innerHeight - ((value - minValue) / valueRange) * innerHeight;

  const pathD = series
    .map((value, index) => `${index === 0 ? "M" : "L"}${toX(index).toFixed(1)},${toY(value).toFixed(1)}`)
    .join(" ");
  const areaD = `${pathD} L${toX(series.length - 1).toFixed(1)},${pad.t + innerHeight} L${pad.l},${pad.t + innerHeight} Z`;
  const ticks = [minValue, (minValue + maxValue) / 2, maxValue].map((value) => Math.round(value * 10) / 10);

  return (
    <svg width={W} height={H} className="line-chart">
      {ticks.map((value, index) => {
        const y = toY(value);
        return (
          <g key={index}>
            <line
              x1={pad.l}
              y1={y}
              x2={pad.l + innerWidth}
              y2={y}
              stroke="var(--line)"
              strokeWidth={0.5}
              strokeDasharray="3,3"
            />
            <text x={pad.l - 4} y={y + 4} textAnchor="end" fontSize={10} fill="var(--ink-3)">
              {value}{unit}
            </text>
          </g>
        );
      })}
      <path d={areaD} fill={color} opacity={0.12} />
      <path d={pathD} fill="none" stroke={color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={toX(series.length - 1)} cy={toY(series[series.length - 1])} r={3} fill={color} />
      <text x={pad.l + innerWidth / 2} y={H - 4} textAnchor="middle" fontSize={10} fill="var(--ink-3)" fontWeight={600}>
        {label}
      </text>
    </svg>
  );
}

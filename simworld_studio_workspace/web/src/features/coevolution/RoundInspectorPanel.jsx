import React from "react";

const ROUND_HISTORY = [
  ["R1", "L0", "82%", "Advance"],
  ["R2", "L1", "76%", "Advance"],
  ["R3", "L2", "58%", "Hold"],
  ["R4", "L2", "71%", "Advance"],
  ["R5", "L3", "49%", "Hold"],
  ["R12", "L4", "64%", "Active"],
];

export default function RoundInspectorPanel({ AggregatePanel, sessionId }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" }}>
      <div className="config-section">
        <div className="config-section-title">Round History</div>
        <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
          {ROUND_HISTORY.map(([round, level, successRate, action]) => (
            <div key={round} className="round-history-row">
              <span className="round-history-muted">{round}</span>
              <span className="round-history-level">{level}</span>
              <span className="round-history-score">{successRate}</span>
              <span className={`round-history-action ${action.toLowerCase()}`}>{action}</span>
            </div>
          ))}
        </div>
      </div>
      <div style={{ flex: 1, overflow: "auto" }}>
        {AggregatePanel ? <AggregatePanel agents={[]} sessionId={sessionId} /> : null}
      </div>
    </div>
  );
}

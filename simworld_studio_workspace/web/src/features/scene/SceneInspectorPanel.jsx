import React from "react";
import { useScene } from "../../state/pollContext.jsx";

export default function SceneInspectorPanel({ latestScreenshot, sessionId, VerifierPanel }) {
  const scene = useScene();
  const actorCount = ((scene.objects || []).length + (scene.agents || []).length) || "-";

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" }}>
      <div className="config-section">
        <div className="config-section-title">Scene Summary</div>
        {[
          ["Actors", actorCount],
          ["Ground size", "200 m"],
          ["Version", "v3"],
        ].map(([label, value]) => (
          <div key={label} className="status-row">
            <span>{label}</span>
            <span className="config-val">{value}</span>
          </div>
        ))}
      </div>

      <div style={{ flex: 1, overflow: "hidden", display: "flex", flexDirection: "column" }}>
        <div style={{ flex: 1, overflow: "hidden" }}>
          {VerifierPanel ? <VerifierPanel sessionId={sessionId} latestScreenshot={latestScreenshot} /> : null}
        </div>
      </div>
    </div>
  );
}

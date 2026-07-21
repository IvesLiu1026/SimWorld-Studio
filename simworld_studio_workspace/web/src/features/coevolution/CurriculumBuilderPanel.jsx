import React, { useState } from "react";
import { Btn } from "../../components/ui/primitives.jsx";

export default function CurriculumBuilderPanel({ icons, sessionId }) {
  const [running, setRunning] = useState(false);

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" }}>
      <div className="config-section">
        <div className="config-section-title">Curriculum Status</div>
        {[
          ["Round", "12 / 25"],
          ["Difficulty", "Level 4"],
          ["Current SR", "64%"],
          ["Next action", "Advance to L5"],
        ].map(([label, value]) => (
          <div key={label} className="status-row">
            <span>{label}</span>
            <span className="config-val">{value}</span>
          </div>
        ))}
      </div>

      <div className="config-section" style={{ flex: 1, overflow: "auto" }}>
        <div className="config-section-title">Difficulty Axes</div>
        {[
          ["Path length", "12-22 m"],
          ["Heading offset", "0-90 deg"],
          ["Obstacle density", "0.20"],
          ["Object clutter", "medium"],
          ["Distractors", "3"],
        ].map(([label, value]) => (
          <div key={label} className="config-row">
            <label>{label}</label>
            <span className="config-val">{value}</span>
          </div>
        ))}

        <div className="config-section-title" style={{ marginTop: 12 }}>
          Curriculum Config
        </div>
        {[
          ["Mastery threshold", "70%"],
          ["Episodes per round", "500"],
          ["Max rounds", "25"],
          ["Advance policy", "Consecutive"],
          ["Controller update", "Online"],
        ].map(([label, value]) => (
          <div key={label} className="config-row">
            <label>{label}</label>
            <span className="config-val">{value}</span>
          </div>
        ))}

        <div className="config-section-title" style={{ marginTop: 12 }}>
          Curriculum Adaptation
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          {[
            "Increase obstacle density",
            "Add longer routes",
            "Preserve successful layouts",
            "Oversample sharp-turn failures",
          ].map((label) => (
            <div key={label} className="curriculum-adaptation-row">
              <span className="curriculum-adaptation-dot" />
              {label}
            </div>
          ))}
        </div>
      </div>

      <div style={{ padding: "10px 14px", borderTop: "1px solid var(--line)", display: "flex", flexDirection: "column", gap: 6 }}>
        <button className="primary-cta orange" style={{ width: "100%", justifyContent: "center" }} onClick={() => setRunning((value) => !value)}>
          {running ? (
            <>
              {icons.collision(13)} Pause
            </>
          ) : (
            <>
              {icons.refresh(13)} Run Iteration
            </>
          )}
        </button>
        <div style={{ display: "flex", gap: 6 }}>
          <Btn variant="ghost" size="sm" style={{ flex: 1, justifyContent: "center" }}>
            Evaluate
          </Btn>
          <Btn variant="ghost" size="sm" style={{ flex: 1, justifyContent: "center" }}>
            Export
          </Btn>
        </div>
      </div>
    </div>
  );
}

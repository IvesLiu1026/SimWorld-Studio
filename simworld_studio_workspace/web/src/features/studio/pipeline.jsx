import React from "react";

export const STUDIO_MODES = [
  {
    id: "scene",
    num: 1,
    label: "Scene",
    sub: "Define and validate an Unreal Engine environment",
    cta: "Build Scene",
    ctaColor: "blue",
    iconKey: "cube",
  },
  {
    id: "task",
    num: 2,
    label: "Tasks",
    sub: "Configure PointNav, ObjectNav, or custom task sets",
    cta: "Build Task Set",
    ctaColor: "green",
    iconKey: "target",
  },
  {
    id: "training",
    num: 3,
    label: "Training",
    sub: "Run navigation experiments and collect trajectories",
    cta: "Start Training",
    ctaColor: "violet",
    iconKey: "activity",
  },
  {
    id: "coevolve",
    num: 4,
    label: "Iteration",
    sub: "Review results and adjust curriculum parameters",
    cta: "Start Iteration",
    ctaColor: "orange",
    iconKey: "refresh",
  },
];

const ARTIFACT_STAGES = [
  { id: "scene", label: "Scene", placeholder: "Scene not saved", iconKey: "cube" },
  { id: "task", label: "Task Set", placeholder: "Tasks not configured", iconKey: "target" },
  { id: "training", label: "Training", placeholder: "No training run", iconKey: "activity" },
  { id: "coevolve", label: "Curriculum", placeholder: "No iteration record", iconKey: "refresh" },
];

function renderIcon(icons, key, size) {
  const Icon = icons?.[key];
  return typeof Icon === "function" ? Icon(size) : null;
}

export function PipelineStepper({ activeMode, onChange, icons }) {
  return (
    <div className="pipeline-stepper">
      {STUDIO_MODES.map((mode, index) => (
        <React.Fragment key={mode.id}>
          <button
            className={`pipeline-tab${activeMode === mode.id ? " active" : ""}`}
            onClick={() => onChange(mode.id)}
            title={mode.sub}
          >
            <span className="pipeline-tab-num">{mode.num}</span>
            {mode.label}
          </button>
          {index < STUDIO_MODES.length - 1 && (
            <svg className="pipeline-arrow" viewBox="0 0 16 16" width="14" height="14" fill="none">
              <polyline
                points="5,3 11,8 5,13"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          )}
        </React.Fragment>
      ))}
    </div>
  );
}

export function ArtifactChain({ artifacts, activeMode, onSelect, icons }) {
  return (
    <div className="artifact-chain">
      <span
        style={{
          fontSize: 10,
          fontWeight: 700,
          letterSpacing: "0.06em",
          textTransform: "uppercase",
          color: "var(--ink-3)",
          flexShrink: 0,
        }}
      >
        Workflow
      </span>
      {ARTIFACT_STAGES.map((stage, index) => {
        const artifact = artifacts[stage.id];
        const isActive = activeMode === stage.id;
        return (
          <React.Fragment key={stage.id}>
            {index > 0 && <span className="artifact-sep">-&gt;</span>}
            <button
              className={`artifact-chip${!artifact ? " empty" : " ready"}${isActive ? " active" : ""}`}
              onClick={() => artifact && onSelect(stage.id)}
              title={artifact ? artifact.name : stage.placeholder}
            >
              <span style={{ display: "inline-flex", alignItems: "center" }}>
                {renderIcon(icons, stage.iconKey, 11)}
              </span>
              {artifact ? artifact.name : stage.placeholder}
            </button>
          </React.Fragment>
        );
      })}
    </div>
  );
}

export function StudioLanding({ activeMode, onSelect, artifacts, icons }) {
  return (
    <div style={{ flex: 1, overflow: "auto", background: "var(--bg)" }}>
      <div style={{ padding: "28px 28px 12px", borderBottom: "1px solid var(--line)" }}>
        <div style={{ fontSize: 16, fontWeight: 700, color: "var(--ink)", marginBottom: 4 }}>
          SimWorld Studio
        </div>
        <div style={{ fontSize: 12, color: "var(--ink-3)" }}>
          Select a workflow stage to begin or continue the project.
        </div>
      </div>
      <div className="mode-landing">
        {STUDIO_MODES.map((mode) => {
          const artifact = artifacts[mode.id];
          return (
            <div
              key={mode.id}
              className={`mode-card${activeMode === mode.id ? " current" : ""}`}
              onClick={() => onSelect(mode.id)}
            >
              <div className="mode-card-num">{mode.num}</div>
              <div>
                <div className="mode-card-title">{mode.label}</div>
                <div className="mode-card-sub">{mode.sub}</div>
              </div>
              <div className={`mode-card-status${artifact ? " has-data" : ""}`}>
                {artifact ? (
                  <>
                    {renderIcon(icons, "check", 11)} {artifact.name}
                  </>
                ) : (
                  "Not configured"
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

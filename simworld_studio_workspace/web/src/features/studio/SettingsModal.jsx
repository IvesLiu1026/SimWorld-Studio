import React from "react";
import CodingAgentSelector from "../agents/CodingAgentSelector.jsx";

const THEMES = [
  { id: "dark", label: "Dark", desc: "Low-glare workstation" },
  { id: "light", label: "Light", desc: "High-contrast workspace" },
];

const LAYOUTS = [
  {
    id: "scene",
    label: "Scene Setup",
    desc: "Specification | Viewport | Validation",
    left: true,
    right: true,
  },
  {
    id: "task",
    label: "Task Design",
    desc: "Parameters | Viewport | Task Set",
    left: true,
    right: true,
  },
  {
    id: "training",
    label: "Training",
    desc: "Run Configuration | Viewport | Run Monitor",
    left: true,
    right: true,
  },
  {
    id: "coevolve",
    label: "Iteration",
    desc: "Curriculum | Viewport | Round Review",
    left: true,
    right: true,
  },
];

function MiniThemePreview({ themeId }) {
  return (
    <div className={`settings-theme-preview ${themeId}`}>
      <div className="settings-theme-nav" />
      <div className="settings-theme-body">
        <div className="settings-theme-side" />
        <div className="settings-theme-center" />
        <div className="settings-theme-side" />
      </div>
    </div>
  );
}

function MiniLayoutPreview({ left, right }) {
  return (
    <div className="settings-layout-preview">
      <div className={`settings-layout-pane ${left ? "on" : "off"}`} />
      <div className="settings-layout-main" />
      <div className={`settings-layout-pane ${right ? "on" : "off"}`} />
    </div>
  );
}

export default function SettingsModal({
  codingAgent,
  codingAgents,
  codingModel,
  icons,
  layoutMode,
  onClose,
  onCodingAgentChange,
  onCodingModelChange,
  onLayoutMode,
  onThemeChange,
  uiTheme,
}) {
  return (
    <div className="settings-modal-overlay" onClick={(event) => event.target === event.currentTarget && onClose()}>
      <div className="settings-modal-card">
        <div className="settings-modal-header">
          <span className="settings-modal-title">Settings</span>
          <button className="settings-close-btn" onClick={onClose} title="Close settings">
            {icons.close(16)}
          </button>
        </div>

        <section className="settings-section">
          <div className="settings-section-label">Appearance</div>
          <div className="settings-theme-grid">
            {THEMES.map((theme) => {
              const active = uiTheme === theme.id;
              return (
                <button
                  key={theme.id}
                  className={`settings-theme-card${active ? " active" : ""}`}
                  onClick={() => onThemeChange(theme.id)}
                >
                  <MiniThemePreview themeId={theme.id} />
                  <div className="settings-card-title">{theme.label}</div>
                  <div className="settings-card-desc">{theme.desc}</div>
                </button>
              );
            })}
          </div>
        </section>

        <section className="settings-section">
          <div className="settings-section-label">Execution</div>
          <div className="settings-execution-row">
            <div className="settings-execution-copy">
              <strong>Scene build runtime</strong>
              <span>Advanced backend configuration for scene operations.</span>
            </div>
            <CodingAgentSelector
              agents={codingAgents}
              agent={codingAgent}
              setAgent={onCodingAgentChange}
              model={codingModel}
              setModel={onCodingModelChange}
              icon={icons.wrench(14)}
            />
          </div>
        </section>

        <section className="settings-section no-margin">
          <div className="settings-section-label">Workspace Mode</div>
          <div className="settings-layout-list">
            {LAYOUTS.map((layout) => {
              const active = layoutMode === layout.id;
              return (
                <button
                  key={layout.id}
                  className={`settings-layout-card${active ? " active" : ""}`}
                  onClick={() => onLayoutMode(layout.id)}
                >
                  <MiniLayoutPreview left={layout.left} right={layout.right} />
                  <div className="settings-layout-copy">
                    <div className="settings-card-title">{layout.label}</div>
                    <div className="settings-card-desc">{layout.desc}</div>
                  </div>
                  {active && <div className="settings-check">{icons.check(16)}</div>}
                </button>
              );
            })}
          </div>
        </section>
      </div>
    </div>
  );
}

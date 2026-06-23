import React from "react";

const THEMES = [
  { id: "dark", label: "Dark", desc: "Dashboard default" },
  { id: "light", label: "Light", desc: "Clean and bright" },
];

const LAYOUTS = [
  {
    id: "scene",
    label: "Scene Generation",
    desc: "Intent+SimCoder | Viewport | Scene Inspector",
    left: true,
    right: true,
  },
  {
    id: "task",
    label: "Task Generation",
    desc: "Task Builder | Viewport | Task Inspector",
    left: true,
    right: true,
  },
  {
    id: "training",
    label: "Agent Training",
    desc: "Training Config | Viewport | Agent Monitor",
    left: true,
    right: true,
  },
  {
    id: "coevolve",
    label: "Co-evolution",
    desc: "Curriculum Builder | Viewport | Round Inspector",
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

export default function SettingsModal({ icons, layoutMode, onClose, onLayoutMode, onThemeChange, uiTheme }) {
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

        <section className="settings-section no-margin">
          <div className="settings-section-label">Layout Mode</div>
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

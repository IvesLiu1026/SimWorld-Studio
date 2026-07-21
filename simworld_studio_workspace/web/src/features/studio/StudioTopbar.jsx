import React, { useState } from "react";
import { PipelineStepper } from "./pipeline.jsx";

function StatusPill({ health }) {
  const [open, setOpen] = useState(false);
  const connecting = !health;
  const ue = !!health?.ueConnected;
  const mcp = !!health?.mcpConnected;
  const state = connecting ? "connecting" : ue && mcp ? "ok" : !ue && !mcp ? "down" : "warn";
  const labels = { ok: "Operational", warn: "Degraded", down: "Offline", connecting: "Starting" };
  const modules = [
    { name: "Unreal Engine", ok: ue },
    { name: "Scene Bridge", ok: mcp },
  ];

  return (
    <div className="sw-connection-status">
      <button
        className={`sw-connection-pill ${state}`}
        onClick={() => setOpen((value) => !value)}
        title="System status"
      >
        <span className="sw-connection-dot" />
        <span>{labels[state]}</span>
        <span className="sw-connection-caret">v</span>
      </button>
      {open && (
        <>
          <div className="sw-popover-scrim" onClick={() => setOpen(false)} />
          <div className="sw-connection-menu">
            {modules.map((module) => (
              <div key={module.name} className="sw-connection-row">
                <span
                  className={`sw-module-dot ${
                    connecting ? "connecting" : module.ok ? "connected" : "disconnected"
                  }`}
                />
                <span className="sw-connection-name">{module.name}</span>
                <span
                  className={`sw-connection-state ${
                    connecting ? "connecting" : module.ok ? "connected" : "disconnected"
                  }`}
                >
                  {connecting ? "..." : module.ok ? "Ready" : "Unavailable"}
                </span>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

export default function StudioTopbar({
  artifactUnread,
  health,
  icons,
  onSettingsOpen,
  onStudioModeChange,
  onTopSectionChange,
  secsLeft,
  session,
  studioMode,
  syncStatus,
  topSection,
  warningSoon,
}) {
  const hasUnreadLibrary = !!(artifactUnread?.skills || artifactUnread?.tools);
  const staleAgents = syncStatus?.staleAgents;
  const staleCount = staleAgents?.size || 0;

  const openStudioMode = (mode) => {
    onStudioModeChange(mode);
    onTopSectionChange("studio");
  };

  return (
    <header className="sw-topbar">
      <div className="sw-brand">
        <div className="sw-brand-mark" aria-hidden="true">
          {icons.cube(15)}
        </div>
        <span className="sw-brand-name">SimWorld Studio</span>
      </div>

      <div className="sw-project-context" title="Current workspace">
        <span>WORKSPACE</span>
        <strong>Scene Development</strong>
      </div>
      <div className="sw-topbar-divider" />

      {topSection === "studio" ? (
        <PipelineStepper activeMode={studioMode} onChange={openStudioMode} icons={icons} />
      ) : (
        <div className="sw-topbar-section-title">{topSection === "library" ? "Asset Catalog" : "Reports"}</div>
      )}

      <div className="sw-secondary-nav">
        <button
          className={`sec-nav-btn${topSection === "studio" ? " active" : ""}`}
          onClick={() => onTopSectionChange("studio")}
        >
          {icons.layout(13)} Workspace
        </button>
        <button
          className={`sec-nav-btn${topSection === "library" ? " active" : ""}`}
          onClick={() => onTopSectionChange("library")}
        >
          {icons.book(13)} Catalog
          {hasUnreadLibrary && <span className="sw-nav-dot" />}
        </button>
        <button
          className={`sec-nav-btn${topSection === "results" ? " active" : ""}`}
          onClick={() => onTopSectionChange("results")}
        >
          {icons.frame(13)} Reports
        </button>
      </div>

      <div className="sw-topbar-divider" />

      <div className="sw-topbar-right">
        {!syncStatus?.sseOk && (
          <div className="sw-topbar-alert error">
            {icons.warning(14)} {syncStatus?.syncError || "SSE disconnected"}
          </div>
        )}
        {staleCount > 0 && (
          <div className="sw-topbar-alert warn" title={`Stale: ${[...staleAgents].join(", ")}`}>
            {icons.warning(14)} {staleCount} stale
          </div>
        )}

        {secsLeft !== null && !session?.dev && (
          <div className={`sw-session-countdown${warningSoon ? " warn" : " ok"}`}>
            {icons.clock(14)}
            {Math.floor(secsLeft / 60)}:{String(secsLeft % 60).padStart(2, "0")}
          </div>
        )}

        <StatusPill health={health} />
        <button className="sw-settings-btn" onClick={onSettingsOpen} title="Settings">
          {icons.gear(22)}
        </button>
      </div>
    </header>
  );
}

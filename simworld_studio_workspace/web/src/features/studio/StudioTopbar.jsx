import React, { useState } from "react";
import CodingAgentSelector from "../agents/CodingAgentSelector.jsx";
import { agentLabel } from "../agents/codingAgents.js";
import { PipelineStepper } from "./pipeline.jsx";

function StatusPill({ health, codingAgent }) {
  const [open, setOpen] = useState(false);
  const connecting = !health;
  const ue = !!health?.ueConnected;
  const mcp = !!health?.mcpConnected;
  const agentOk = true;
  const state = connecting ? "connecting" : ue && mcp ? "ok" : !ue && !mcp ? "down" : "warn";
  const labels = { ok: "Running", warn: "Issues", down: "Offline", connecting: "Connecting..." };
  const modules = [
    { name: "UE Engine", ok: ue },
    { name: "MCP Server", ok: mcp },
    { name: agentLabel(codingAgent), ok: agentOk },
  ];

  return (
    <div className="sw-connection-status">
      <button
        className={`sw-connection-pill ${state}`}
        onClick={() => setOpen((value) => !value)}
        title="Connection status - click for details"
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
                  {connecting ? "..." : module.ok ? "Connected" : "Not connected"}
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
  codingAgent,
  codingAgents,
  codingModel,
  health,
  icons,
  onCodingAgentChange,
  onCodingModelChange,
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
      <div className="sw-brand" style={{ paddingRight: 12 }}>
        <div className="sw-brand-logo">
          <img src="/simworld-studio-logo.png" alt="SimWorld" />
        </div>
        <span className="sw-brand-name" style={{ fontSize: 14 }}>
          SimWorld Studio
        </span>
      </div>

      <CodingAgentSelector
        agents={codingAgents}
        agent={codingAgent}
        setAgent={onCodingAgentChange}
        model={codingModel}
        setModel={onCodingModelChange}
        icon={icons?.robot ? icons.robot(14) : null}
      />
      <div className="sw-topbar-divider" />

      {topSection === "studio" ? (
        <PipelineStepper activeMode={studioMode} onChange={openStudioMode} icons={icons} />
      ) : (
        <div className="sw-topbar-section-title">{topSection === "library" ? "Library" : "Results"}</div>
      )}

      <div className="sw-secondary-nav">
        <button
          className={`sec-nav-btn${topSection === "studio" ? " active" : ""}`}
          onClick={() => onTopSectionChange("studio")}
        >
          {icons.layout(13)} Studio
        </button>
        <button
          className={`sec-nav-btn${topSection === "library" ? " active" : ""}`}
          onClick={() => onTopSectionChange("library")}
        >
          {icons.book(13)} Library
          {hasUnreadLibrary && <span className="sw-nav-dot" />}
        </button>
        <button
          className={`sec-nav-btn${topSection === "results" ? " active" : ""}`}
          onClick={() => onTopSectionChange("results")}
        >
          {icons.frame(13)} Results
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
            {icons.ghost(14)} {staleCount} stale
          </div>
        )}

        {secsLeft !== null && !session?.dev && (
          <div className={`sw-session-countdown${warningSoon ? " warn" : " ok"}`}>
            {icons.clock(14)}
            {Math.floor(secsLeft / 60)}:{String(secsLeft % 60).padStart(2, "0")}
          </div>
        )}

        <StatusPill health={health} codingAgent={codingAgent} />
        <button className="sw-settings-btn" onClick={onSettingsOpen} title="Settings">
          {icons.gear(22)}
        </button>
      </div>
    </header>
  );
}

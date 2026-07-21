import React, { useState } from "react";

export default function SceneAgentHeader({
  icons = {},
  mcpStatus,
  selfEvolutionReady,
  selfEvolutionOn,
  sessionId,
  turnCount,
  latestScreenshot,
  loading,
  onToggleSelfEvolution,
  onAnnotate,
  onSave,
  onShare,
  onStop,
  onReset,
  onOpenSessions,
  onNewConversation,
}) {
  const [detailsOpen, setDetailsOpen] = useState(false);
  const normalizedMcpStatus = String(mcpStatus || "").toLowerCase();
  const mcpKnown = normalizedMcpStatus && normalizedMcpStatus !== "-";
  const mcpHealthy =
    normalizedMcpStatus.startsWith("connected") || normalizedMcpStatus.startsWith("ok");
  const mcpLabel = !mcpKnown ? "Bridge pending" : mcpHealthy ? "Bridge ready" : "Bridge offline";

  return (
    <div className="scene-agent-header">
      <div className="scene-agent-main">
        <button
          className="scene-agent-icon-btn"
          onClick={onOpenSessions}
          title="Open build sessions"
          type="button"
        >
          {icons.chat?.(15)}
        </button>

        <div className="scene-agent-titleblock">
          <div className="scene-agent-title-row">
            <span className="scene-agent-title">Scene Operations</span>
            <span className={`scene-agent-health${!mcpKnown ? " pending" : mcpHealthy ? " ok" : " down"}`}>
              <span className="scene-agent-health-dot" />
              {mcpLabel}
            </span>
          </div>
          <div className="scene-agent-subtitle">Instruction and revision log</div>
        </div>

        <div className="scene-agent-actions">
          {latestScreenshot && !loading && (
            <button className="scene-agent-action subtle" onClick={onAnnotate} title="Mark up the latest capture" type="button">
              {icons.scan?.(13)}
              <span>Markup</span>
            </button>
          )}
          {!loading && sessionId && (
            <button className="scene-agent-action success" onClick={onSave} title="Save current scene" type="button">
              <span>Save</span>
            </button>
          )}
          {!loading && sessionId && latestScreenshot && (
            <button className="scene-agent-action primary" onClick={onShare} title="Publish to the project gallery" type="button">
              <span>Publish</span>
            </button>
          )}
          {loading && (
            <button className="scene-agent-action danger" onClick={onStop} type="button">
              <span>Stop</span>
            </button>
          )}
          <button
            className={`scene-agent-icon-btn${detailsOpen ? " active" : ""}`}
            onClick={() => setDetailsOpen((value) => !value)}
            title="Operation details"
            type="button"
          >
            {icons.gear?.(14)}
          </button>
        </div>
      </div>

      {detailsOpen && (
        <div className="scene-agent-details">
          <div className="scene-agent-detail-grid">
            <div className="scene-agent-detail">
              <span>Bridge</span>
              <strong title={mcpStatus}>{mcpStatus || "-"}</strong>
            </div>
            <div className="scene-agent-detail">
              <span>Run</span>
              <strong>{sessionId ? sessionId.slice(0, 10) : "-"}</strong>
            </div>
            <div className="scene-agent-detail">
              <span>Revisions</span>
              <strong>{turnCount || 0}</strong>
            </div>
            <div className="scene-agent-detail">
              <span>Capture</span>
              <strong>{latestScreenshot ? "Ready" : "-"}</strong>
            </div>
          </div>

          <div className="scene-agent-detail-actions">
            <button
              className={`scene-agent-evolution${selfEvolutionOn ? " on" : ""}`}
              onClick={onToggleSelfEvolution}
              disabled={!selfEvolutionReady}
              title="Enable or disable iterative validation"
              type="button"
            >
              <span className="scene-agent-evolution-dot" />
              <span>Iterative validation</span>
              <strong>{selfEvolutionReady ? (selfEvolutionOn ? "ON" : "OFF") : "..."}</strong>
            </button>
            {onNewConversation && (
              <button className="scene-agent-action subtle" onClick={onNewConversation} disabled={loading} type="button">
                {icons.plus?.(13)}
                <span>New Session</span>
              </button>
            )}
            {!loading && sessionId && (
              <button className="scene-agent-action muted" onClick={onReset} title="Clear operation history" type="button">
                <span>Clear</span>
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

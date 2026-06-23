import React, { useState } from "react";

export default function SceneAgentHeader({
  agentLabelText,
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
  const mcpLabel = !mcpKnown ? "MCP pending" : mcpHealthy ? "MCP ready" : "MCP offline";

  return (
    <div className="scene-agent-header">
      <div className="scene-agent-main">
        <button
          className="scene-agent-icon-btn"
          onClick={onOpenSessions}
          title="Open chat history"
          type="button"
        >
          {icons.chat?.(15)}
        </button>

        <div className="scene-agent-titleblock">
          <div className="scene-agent-title-row">
            <span className="scene-agent-title">Scene Agent</span>
            <span className={`scene-agent-health${!mcpKnown ? " pending" : mcpHealthy ? " ok" : " down"}`}>
              <span className="scene-agent-health-dot" />
              {mcpLabel}
            </span>
          </div>
          <div className="scene-agent-subtitle">{agentLabelText}</div>
        </div>

        <div className="scene-agent-actions">
          {latestScreenshot && !loading && (
            <button className="scene-agent-action subtle" onClick={onAnnotate} title="Annotate screenshot to give feedback" type="button">
              {icons.scan?.(13)}
              <span>Annotate</span>
            </button>
          )}
          {!loading && sessionId && (
            <button className="scene-agent-action success" onClick={onSave} title="Save current scene" type="button">
              <span>Save</span>
            </button>
          )}
          {!loading && sessionId && latestScreenshot && (
            <button className="scene-agent-action primary" onClick={onShare} title="Share to community gallery" type="button">
              <span>Share</span>
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
            title="Agent details"
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
              <span>MCP</span>
              <strong title={mcpStatus}>{mcpStatus || "-"}</strong>
            </div>
            <div className="scene-agent-detail">
              <span>Session</span>
              <strong>{sessionId ? sessionId.slice(0, 10) : "-"}</strong>
            </div>
            <div className="scene-agent-detail">
              <span>Turns</span>
              <strong>{turnCount || 0}</strong>
            </div>
            <div className="scene-agent-detail">
              <span>Screenshot</span>
              <strong>{latestScreenshot ? "Ready" : "-"}</strong>
            </div>
          </div>

          <div className="scene-agent-detail-actions">
            <button
              className={`scene-agent-evolution${selfEvolutionOn ? " on" : ""}`}
              onClick={onToggleSelfEvolution}
              disabled={!selfEvolutionReady}
              title="Enable or disable self-evolution ingestion"
              type="button"
            >
              <span className="scene-agent-evolution-dot" />
              <span>Self-Evolution</span>
              <strong>{selfEvolutionReady ? (selfEvolutionOn ? "ON" : "OFF") : "..."}</strong>
            </button>
            {onNewConversation && (
              <button className="scene-agent-action subtle" onClick={onNewConversation} disabled={loading} type="button">
                {icons.plus?.(13)}
                <span>New Chat</span>
              </button>
            )}
            {!loading && sessionId && (
              <button className="scene-agent-action muted" onClick={onReset} title="Reset conversation" type="button">
                <span>Reset</span>
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

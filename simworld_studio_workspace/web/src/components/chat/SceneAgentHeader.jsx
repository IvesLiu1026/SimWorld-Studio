import React from "react";

const HEADER_BUTTON_TONES = {
  orange: "var(--orange)",
  green: "var(--green)",
  blue: "var(--blue)",
  red: "var(--red)",
  muted: "var(--ink-3)",
};

function headerButtonStyle(tone) {
  const color = HEADER_BUTTON_TONES[tone] || HEADER_BUTTON_TONES.muted;
  return {
    padding: "3px 11px",
    fontSize: 12,
    lineHeight: 1.2,
    background: "var(--panel)",
    border: `1px solid color-mix(in srgb, ${color} 35%, transparent)`,
    borderRadius: 8,
    color,
    cursor: "pointer",
    fontWeight: 600,
    fontFamily: "inherit",
    boxShadow: "0 1px 2px rgba(15,23,42,.04)",
    whiteSpace: "nowrap",
    minWidth: 0,
  };
}

export default function SceneAgentHeader({
  agentLabelText,
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
}) {
  const mcpHealthy = String(mcpStatus || "").startsWith("✓");

  return (
    <div
      style={{
        padding: "10px 14px",
        borderBottom: "1px solid var(--line)",
        background: "var(--panel)",
        display: "flex",
        alignItems: "flex-start",
        gap: 10,
        flexWrap: "wrap",
        flexShrink: 0,
        boxShadow: "0 1px 2px rgba(15,23,42,.04)",
      }}
    >
      <div style={{ flex: "1 1 180px", minWidth: 0 }}>
        <div style={{ fontWeight: 700, fontSize: 13, color: "var(--ink)" }}>Scene Agent</div>
        <div
          style={{
            fontSize: 12,
            color: "var(--ink-3)",
            display: "flex",
            gap: "4px 8px",
            alignItems: "center",
            flexWrap: "wrap",
            minWidth: 0,
            lineHeight: 1.35,
          }}
        >
          <span style={{ whiteSpace: "nowrap" }}>{agentLabelText}</span>
          <span>·</span>
          <span
            style={{
              color: mcpHealthy ? "var(--green)" : "var(--red)",
              minWidth: 0,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
              maxWidth: "100%",
            }}
          >
            MCP: {mcpStatus}
          </span>
          <span>·</span>
          <span style={{ color: selfEvolutionOn ? "var(--orange)" : "var(--ink-3)", whiteSpace: "nowrap" }}>
            Self-evolution: {selfEvolutionReady ? (selfEvolutionOn ? "on" : "off") : "syncing"}
          </span>
          {sessionId && (
            <>
              <span>·</span>
              <span style={{ color: "var(--ink-3)", whiteSpace: "nowrap" }}>session: {sessionId.slice(0, 8)}</span>
              <span>·</span>
              <span style={{ color: "var(--ink-2)", whiteSpace: "nowrap" }}>turn {turnCount}</span>
            </>
          )}
        </div>
      </div>

      <div
        style={{
          display: "flex",
          gap: 4,
          flex: "0 1 auto",
          minWidth: 0,
          maxWidth: "100%",
          flexWrap: "wrap",
          justifyContent: "flex-end",
        }}
      >
        <button
          onClick={onToggleSelfEvolution}
          title="Enable or disable self-evolution ingestion"
          style={{
            height: 24,
            padding: "0 10px",
            borderRadius: 7,
            border: selfEvolutionOn
              ? "1px solid color-mix(in srgb, var(--orange) 40%, transparent)"
              : "1px solid var(--line)",
            background: selfEvolutionOn ? "var(--orange-soft)" : "var(--panel-2)",
            color: selfEvolutionOn ? "var(--orange)" : "var(--ink-3)",
            cursor: selfEvolutionReady ? "pointer" : "not-allowed",
            display: "flex",
            alignItems: "center",
            gap: 7,
            fontSize: 12,
            fontWeight: selfEvolutionOn ? 600 : 500,
            opacity: selfEvolutionReady ? 1 : 0.7,
            boxShadow: selfEvolutionOn ? "0 0 12px rgba(240,136,62,0.35)" : "none",
            transition: "all 0.18s ease",
            minWidth: 0,
            maxWidth: "100%",
            whiteSpace: "nowrap",
            overflow: "hidden",
          }}
        >
          <span
            style={{
              width: 8,
              height: 8,
              borderRadius: "50%",
              background: selfEvolutionOn ? "var(--orange)" : "var(--line)",
              boxShadow: selfEvolutionOn ? "0 0 8px rgba(255,157,66,0.4)" : "none",
              animation: selfEvolutionOn ? "selfEvoPulse 1.3s ease-in-out infinite" : "none",
              flexShrink: 0,
            }}
          />
          <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>Self-Evolution</span>
          <span
            style={{
              color: selfEvolutionOn ? "var(--orange)" : "var(--ink-2)",
              flexShrink: 0,
              letterSpacing: 0.2,
            }}
          >
            {selfEvolutionReady ? (selfEvolutionOn ? "ON" : "OFF") : "..."}
          </span>
        </button>
        <style>
          {"@keyframes selfEvoPulse { 0%,100%{opacity:1} 50%{opacity:0.35} }"}
        </style>
        {latestScreenshot && !loading && (
          <button onClick={onAnnotate} style={headerButtonStyle("orange")} title="Annotate screenshot to give feedback">
            Annotate
          </button>
        )}
        {!loading && sessionId && (
          <button onClick={onSave} style={headerButtonStyle("green")} title="Save current scene">
            Save
          </button>
        )}
        {!loading && sessionId && latestScreenshot && (
          <button onClick={onShare} style={headerButtonStyle("blue")} title="Share to community gallery">
            Share
          </button>
        )}
        {loading && (
          <button onClick={onStop} style={headerButtonStyle("red")}>
            Stop
          </button>
        )}
        {!loading && sessionId && (
          <button onClick={onReset} title="Reset conversation" style={headerButtonStyle("muted")}>
            Reset
          </button>
        )}
      </div>
    </div>
  );
}

import React, { useEffect, useState } from "react";

const selectStyle = {
  fontSize: 12,
  fontWeight: 600,
  height: 28,
  padding: "0 6px",
  background: "var(--panel-2)",
  color: "var(--ink)",
  border: "1px solid var(--line)",
  borderRadius: 6,
  cursor: "pointer",
};

export default function CodingAgentSelector({ agents, agent, setAgent, model, setModel, icon }) {
  const cfg = agents[agent] || {};
  const models = cfg.models || [];
  const derivedCustom = !!model && !models.includes(model);
  const [forceCustom, setForceCustom] = useState(false);
  const showCustom = forceCustom || derivedCustom;

  useEffect(() => {
    if (!derivedCustom) setForceCustom(false);
  }, [derivedCustom, agent]);

  const onModelChange = (value) => {
    if (value === "__custom__") setForceCustom(true);
    else {
      setForceCustom(false);
      setModel(value);
    }
  };

  return (
    <div
      className="coding-agent-selector"
      style={{ display: "flex", alignItems: "center", gap: 6, flexShrink: 0, minWidth: 0 }}
      title="Coding agent backend and model used for scene generation"
    >
      {icon ? <span style={{ display: "inline-flex", color: "var(--ink-3)", flexShrink: 0 }}>{icon}</span> : null}
      <select value={agent} onChange={(event) => setAgent(event.target.value)} style={selectStyle} aria-label="Coding agent">
        {Object.entries(agents).map(([id, agentDef]) => (
          <option key={id} value={id}>
            {agentDef.label || id}
          </option>
        ))}
      </select>
      <select
        value={showCustom ? "__custom__" : model}
        onChange={(event) => onModelChange(event.target.value)}
        style={{ ...selectStyle, maxWidth: 170 }}
        aria-label="Model"
      >
        <option value="">Default</option>
        {models.map((modelId) => (
          <option key={modelId} value={modelId}>
            {modelId}
          </option>
        ))}
        <option value="__custom__">Custom...</option>
      </select>
      {showCustom && (
        <input
          value={model}
          onChange={(event) => setModel(event.target.value)}
          placeholder="model id"
          style={{ ...selectStyle, width: 130, fontWeight: 400, cursor: "text" }}
          aria-label="Custom model id"
        />
      )}
    </div>
  );
}

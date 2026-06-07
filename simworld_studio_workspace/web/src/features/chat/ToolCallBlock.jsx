import React, { memo, useState } from "react";

function summarizeToolInput(tool) {
  try {
    const input = tool.input || (tool.inputBuffer ? JSON.parse(tool.inputBuffer) : null);
    if (!input) return "";
    return Object.keys(input)
      .slice(0, 2)
      .map((key) => {
        const value = input[key];
        const text = Array.isArray(value) ? `[${value.join(",")}]` : String(value);
        return `${key}: ${text.slice(0, 30)}`;
      })
      .join(", ");
  } catch {
    return "";
  }
}

function ToolChevron({ expanded }) {
  return (
    <svg aria-hidden="true" focusable="false" viewBox="0 0 16 16" width="13" height="13">
      <path
        d={expanded ? "M4 10l4-4 4 4" : "M4 6l4 4 4-4"}
        fill="none"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.8"
      />
    </svg>
  );
}

const ToolCallBlock = memo(function ToolCallBlock({ fallbackIcon, tool, toolIcons = {} }) {
  const [expanded, setExpanded] = useState(false);
  if (!tool) return null;

  const iconFn = toolIcons[tool.displayName] || fallbackIcon;
  const displayName = tool.displayName || tool.name?.replace(/^mcp__\w+__/, "") || "tool";
  const paramSummary = summarizeToolInput(tool);
  const status = tool.status || "running";

  return (
    <div className="chat-tool-call">
      <button className="chat-tool-header" onClick={() => setExpanded((prev) => !prev)} type="button">
        <span className={`chat-tool-status ${status}`} />
        <span className="chat-tool-icon">{iconFn ? iconFn(13) : null}</span>
        <span className="chat-tool-name">{displayName}</span>
        {paramSummary && <span className="chat-tool-summary">({paramSummary})</span>}
        {status === "running" && !tool.input && <span className="chat-tool-running">running...</span>}
        <span className="chat-tool-caret">
          <ToolChevron expanded={expanded} />
        </span>
      </button>

      {expanded && (
        <div className="chat-tool-body">
          {(tool.input || tool.inputBuffer) && (
            <div className="chat-tool-section">
              <div className="chat-tool-label">Input</div>
              <pre>{tool.input ? JSON.stringify(tool.input, null, 2) : tool.inputBuffer}</pre>
            </div>
          )}
          {tool.result && (
            <div className="chat-tool-section">
              <div className="chat-tool-label">Result</div>
              <pre className={tool.isError ? "error" : "ok"}>{tool.result}</pre>
            </div>
          )}
          {tool.screenshot && (
            <div className="chat-tool-screenshot">
              <img src={`${tool.screenshot}?t=${Date.now()}`} alt="UE screenshot" />
            </div>
          )}
        </div>
      )}
    </div>
  );
});

export default ToolCallBlock;

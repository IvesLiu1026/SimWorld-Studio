import React from "react";

export default function ArtifactToastStack({ items }) {
  if (!Array.isArray(items) || items.length === 0) return null;

  return (
    <div className="sw-artifact-toast-stack">
      {items.map((item) => {
        const isTool = item.kind === "tool";
        const title = isTool ? "New Tool Learned" : "New Skill Learned";
        return (
          <div key={item.id} className={`sw-artifact-toast ${isTool ? "tool" : "skill"}`}>
            <div className="sw-artifact-toast-body">
              <div className="sw-artifact-toast-title">{title}</div>
              <div className="sw-artifact-toast-name" title={item.name}>
                {item.name}
              </div>
            </div>
            {item.extraCount > 0 && <div className="sw-artifact-toast-count">+{item.extraCount}</div>}
          </div>
        );
      })}
    </div>
  );
}

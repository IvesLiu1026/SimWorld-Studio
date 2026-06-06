import React, { useState } from "react";

export default function LibraryPage({
  ArenaPage,
  icons,
  newlyAddedSkillIds,
  newlyAddedToolIds,
  onMarkSkillSeen,
  onMarkToolSeen,
  SkillsPage,
  ToolsPage,
}) {
  const [tab, setTab] = useState("skills");
  const tabs = [
    ["skills", "Skills", icons.book],
    ["tools", "Tools", icons.wrench],
    ["arena", "Arena", icons.swords],
  ];

  return (
    <div className="studio-page">
      <div className="studio-page-tabs">
        <span className="studio-page-title">Library</span>
        {tabs.map(([id, label, icon]) => (
          <button key={id} className={`sw-tab-btn${tab === id ? " active" : ""}`} onClick={() => setTab(id)}>
            <span className="studio-tab-label">
              {icon(13)} {label}
            </span>
          </button>
        ))}
      </div>
      <div className="studio-page-body">
        {tab === "skills" && (
          <SkillsPage newlyAddedSkillIds={newlyAddedSkillIds} onMarkSkillSeen={onMarkSkillSeen} />
        )}
        {tab === "tools" && <ToolsPage newlyAddedToolIds={newlyAddedToolIds} onMarkToolSeen={onMarkToolSeen} />}
        {tab === "arena" && <ArenaPage />}
      </div>
    </div>
  );
}

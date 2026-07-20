import React, { useState } from "react";
import SkillsPage from "./SkillsPage.jsx";
import ToolsPage from "./ToolsPage.jsx";

export default function LibraryPage({
  ArenaPage,
  icons,
  newlyAddedSkillIds,
  newlyAddedToolIds,
  onMarkSkillSeen,
  onMarkToolSeen,
}) {
  const [tab, setTab] = useState("skills");
  const tabs = [
    ["skills", "Procedures", icons.book],
    ["tools", "Tools", icons.wrench],
    ["arena", "Arena", icons.swords],
  ];

  return (
    <div className="studio-page">
      <div className="studio-page-tabs">
        <span className="studio-page-title">Catalog</span>
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
          <SkillsPage icons={icons} newlyAddedSkillIds={newlyAddedSkillIds} onMarkSkillSeen={onMarkSkillSeen} />
        )}
        {tab === "tools" && <ToolsPage icons={icons} newlyAddedToolIds={newlyAddedToolIds} onMarkToolSeen={onMarkToolSeen} />}
        {tab === "arena" && <ArenaPage icons={icons} />}
      </div>
    </div>
  );
}

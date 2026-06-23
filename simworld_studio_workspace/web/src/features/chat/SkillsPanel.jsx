import React, { useEffect, useState } from "react";
import { deleteSkill, fetchSkillDetails, fetchSkills } from "../../api/appApi.js";
import { Badge, Btn, SourceBadge, TagChip } from "../../components/ui/primitives.jsx";
import { SkillPageCreateModal, SkillPageDetailModal } from "../library/SkillsPage.jsx";

function SkillItem({ active, disabled, onPreview, onToggle, skill }) {
  const toggle = () => {
    if (!disabled) onToggle();
  };

  return (
    <div
      style={{
        padding: "8px 10px",
        borderRadius: 6,
        border: `1px solid ${active ? "var(--blue)" : "var(--line)"}`,
        background: active ? "var(--blue-soft)" : "var(--panel)",
        transition: "all 0.15s",
        opacity: disabled ? 0.75 : 1,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <button
          type="button"
          onClick={toggle}
          style={{
            width: 16,
            height: 16,
            borderRadius: 3,
            border: `2px solid ${active ? "var(--blue)" : "var(--line)"}`,
            background: active ? "var(--blue)" : "transparent",
            color: "var(--accent-ink)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontSize: 11,
            flexShrink: 0,
            cursor: disabled ? "not-allowed" : "pointer",
            padding: 0,
          }}
          aria-pressed={active}
          disabled={disabled}
        >
          {active ? (
            <svg aria-hidden="true" focusable="false" viewBox="0 0 12 12" width="10" height="10">
              <path
                d="M2.2 6.1l2.2 2.2 5-5"
                fill="none"
                stroke="currentColor"
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth="1.8"
              />
            </svg>
          ) : null}
        </button>
        <button
          type="button"
          onClick={toggle}
          disabled={disabled}
          style={{
            flex: 1,
            minWidth: 0,
            border: "none",
            background: "transparent",
            color: "var(--ink)",
            cursor: disabled ? "not-allowed" : "pointer",
            font: "inherit",
            fontSize: 12,
            fontWeight: 500,
            padding: 0,
            textAlign: "left",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {skill.name}
        </button>
        <Btn
          variant="ghost"
          size="xs"
          onClick={(event) => {
            event.stopPropagation();
            onPreview();
          }}
          title="Preview skill details"
        >
          Preview
        </Btn>
        <SourceBadge source={skill.source} />
      </div>

      <div style={{ fontSize: 12, color: "var(--ink-3)", marginTop: 4, marginLeft: 22, lineHeight: 1.4 }}>
        {skill.description}
      </div>

      {skill.tags.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginTop: 5, marginLeft: 22 }}>
          {skill.tags.map((tag) => (
            <TagChip key={tag} tag={tag} />
          ))}
        </div>
      )}

      {skill.dependencies.length > 0 && (
        <div style={{ fontSize: 12, color: "var(--ink-2)", marginTop: 4, marginLeft: 22 }}>
          Depends on: {skill.dependencies.join(", ")}
        </div>
      )}
    </div>
  );
}

export default function SkillsPanel({
  autoEnabled,
  autoSelected,
  icons,
  onAutoEnabledChange,
  onChange,
  selected,
}) {
  const [skills, setSkills] = useState([]);
  const [expanded, setExpanded] = useState(false);
  const [previewSkill, setPreviewSkill] = useState(null);
  const [showCreate, setShowCreate] = useState(false);
  const activeSkills = autoEnabled ? autoSelected : selected;

  const reload = () => {
    fetchSkills()
      .then(setSkills)
      .catch(() => {});
  };

  useEffect(() => {
    const timer = setTimeout(reload, 5000);
    return () => clearTimeout(timer);
  }, []);

  useEffect(() => {
    if (!expanded) return undefined;
    reload();
    const timer = setInterval(reload, 30000);
    return () => clearInterval(timer);
  }, [expanded]);

  const toggleSkill = (id) => {
    if (autoEnabled) return;
    onChange(selected.includes(id) ? selected.filter((skillId) => skillId !== id) : [...selected, id]);
  };

  const handlePreview = async (id) => {
    try {
      const detail = await fetchSkillDetails(id);
      setPreviewSkill(detail);
    } catch {}
  };

  const handleDelete = async (id) => {
    await deleteSkill(id);
    setPreviewSkill(null);
    reload();
  };

  if (skills.length === 0) return null;

  const builtinSkills = skills.filter((skill) => skill.source === "builtin");
  const customSkills = skills.filter((skill) => skill.source === "custom");

  return (
    <div style={{ padding: "6px 12px", borderBottom: "1px solid var(--line)", background: "var(--bg)" }}>
      <div
        style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer", userSelect: "none" }}
        onClick={() => setExpanded(!expanded)}
      >
        <span style={{ fontSize: 12, color: "var(--ink-3)", fontFamily: "monospace" }}>{expanded ? "v" : ">"}</span>
        <span style={{ fontSize: 12, fontWeight: 600, color: "var(--ink)" }}>Skills</span>
        {activeSkills.length > 0 && (
          <Badge variant="blue" style={{ marginLeft: 4 }}>
            {activeSkills.length}
          </Badge>
        )}
        <div onClick={(event) => event.stopPropagation()} style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ fontSize: 12, color: "var(--ink-3)" }}>Auto-select skills</span>
          <button
            type="button"
            onClick={() => onAutoEnabledChange(!autoEnabled)}
            style={{
              width: 34,
              height: 18,
              borderRadius: 999,
              border: "1px solid var(--line)",
              background: autoEnabled ? "var(--blue)" : "var(--line)",
              padding: 1,
              position: "relative",
              cursor: "pointer",
            }}
            title={`Auto-select skills: ${autoEnabled ? "on" : "off"}`}
            aria-label="Toggle auto-select skills"
            aria-pressed={autoEnabled}
          >
            <span
              style={{
                display: "block",
                width: 14,
                height: 14,
                borderRadius: "50%",
                background: "var(--panel)",
                transform: autoEnabled ? "translateX(16px)" : "translateX(0)",
                transition: "transform 0.15s ease",
              }}
            />
          </button>
        </div>
        <span style={{ fontSize: 12, color: "var(--ink-2)", marginLeft: 6 }}>{skills.length} available</span>
      </div>

      {expanded && (
        <div style={{ marginTop: 6, display: "flex", flexDirection: "column", gap: 4, maxHeight: 260, overflowY: "auto", paddingRight: 4 }}>
          <div style={{ fontSize: 12, color: "var(--ink-3)", border: "1px solid var(--line)", background: "var(--bg)", borderRadius: 6, padding: "6px 8px", marginBottom: 2 }}>
            {autoEnabled
              ? "Auto mode: the agent pre-selects relevant skills before each run."
              : "Manual mode: check the exact skills you want active."}
          </div>

          {builtinSkills.length > 0 && (
            <div style={{ fontSize: 12, color: "var(--ink-2)", fontWeight: 600, padding: "4px 0 2px" }}>BUILTIN</div>
          )}
          {builtinSkills.map((skill) => (
            <SkillItem
              key={skill.id}
              skill={skill}
              active={activeSkills.includes(skill.id)}
              onToggle={() => toggleSkill(skill.id)}
              onPreview={() => handlePreview(skill.id)}
              disabled={autoEnabled}
            />
          ))}

          {customSkills.length > 0 && (
            <div style={{ fontSize: 12, color: "var(--ink-2)", fontWeight: 600, padding: "6px 0 2px" }}>CUSTOM</div>
          )}
          {customSkills.map((skill) => (
            <SkillItem
              key={skill.id}
              skill={skill}
              active={activeSkills.includes(skill.id)}
              onToggle={() => toggleSkill(skill.id)}
              onPreview={() => handlePreview(skill.id)}
              disabled={autoEnabled}
            />
          ))}

          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation();
              setShowCreate(true);
            }}
            style={{
              padding: "6px 10px",
              marginTop: 4,
              borderRadius: 6,
              border: "1px dashed var(--line)",
              background: "transparent",
              color: "var(--blue)",
              fontSize: 12,
              cursor: "pointer",
              display: "flex",
              alignItems: "center",
              gap: 6,
              justifyContent: "center",
            }}
          >
            + Add Custom Skill
          </button>
        </div>
      )}

      {previewSkill && (
        <SkillPageDetailModal
          icons={icons}
          skill={previewSkill}
          onClose={() => setPreviewSkill(null)}
          onDelete={previewSkill.source === "custom" ? () => handleDelete(previewSkill.id) : undefined}
        />
      )}
      {showCreate && (
        <SkillPageCreateModal
          icons={icons}
          onClose={() => setShowCreate(false)}
          onCreated={() => {
            setShowCreate(false);
            reload();
          }}
        />
      )}
    </div>
  );
}

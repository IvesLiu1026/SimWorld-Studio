import React, { useEffect, useMemo, useState } from "react";
import { createSkill, deleteSkill, fetchSkillDetails, fetchSkills } from "../../api/appApi.js";
import {
  Badge,
  Btn,
  Eyebrow,
  Field,
  ModalFooter,
  ModalHeader,
  ModalOverlay,
  PageHeader,
  SourceBadge,
  TagChip,
  inputSx,
} from "../../components/ui/primitives.jsx";
import ConfirmDeleteModal from "./ConfirmDeleteModal.jsx";

function SkillPageCard({ isNew = false, onClick, skill }) {
  const desc = skill.description.length > 120 ? `${skill.description.slice(0, 120)}...` : skill.description;
  return (
    <div
      onClick={onClick}
      style={{
        padding: "14px 16px",
        borderRadius: 10,
        border: "1px solid var(--line)",
        background: "var(--panel)",
        cursor: "pointer",
        transition: "border-color 0.15s, box-shadow 0.15s",
        display: "flex",
        flexDirection: "column",
        gap: 8,
      }}
      onMouseEnter={(event) => {
        event.currentTarget.style.borderColor = "var(--blue)";
        event.currentTarget.style.boxShadow = "0 2px 12px color-mix(in srgb, var(--blue) 10%, transparent)";
      }}
      onMouseLeave={(event) => {
        event.currentTarget.style.borderColor = "var(--line)";
        event.currentTarget.style.boxShadow = "none";
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 6, minWidth: 0 }}>
        <span
          style={{
            fontSize: 13,
            fontWeight: 600,
            color: "var(--ink)",
            flex: 1,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {skill.name}
        </span>
        {isNew && (
          <Badge variant="red" dot>
            NEW
          </Badge>
        )}
        <SourceBadge source={skill.source} />
      </div>
      <div style={{ fontSize: 12, color: "var(--ink-3)", lineHeight: 1.5 }}>{desc}</div>
      {skill.tags.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
          {skill.tags.map((tag) => (
            <TagChip key={tag} tag={tag} />
          ))}
        </div>
      )}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          marginTop: "auto",
          paddingTop: 4,
          borderTop: "1px solid var(--line)",
          fontSize: 11,
          color: "var(--ink-3)",
        }}
      >
        <span>v{skill.version}</span>
        <span style={{ marginLeft: "auto" }}>{skill.author}</span>
      </div>
    </div>
  );
}

export function SkillPageDetailModal({ icons, onClose, onDelete, skill }) {
  return (
    <ModalOverlay onClose={onClose}>
      <ModalHeader
        title={skill.name}
        subtitle={
          <>
            v{skill.version} by {skill.author} <SourceBadge source={skill.source} style={{ marginLeft: 6 }} />
          </>
        }
        onClose={onClose}
        closeIcon={icons?.close?.(14)}
      >
        {onDelete && <Btn variant="danger" onClick={onDelete}>Delete</Btn>}
      </ModalHeader>

      <div style={{ padding: "12px 20px", borderBottom: "1px solid var(--line)" }}>
        <div style={{ fontSize: 13, color: "var(--ink)", lineHeight: 1.6 }}>{skill.description}</div>
        {skill.tags.length > 0 && (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginTop: 10 }}>
            {skill.tags.map((tag) => (
              <TagChip key={tag} tag={tag} />
            ))}
          </div>
        )}
        {skill.dependencies.length > 0 && (
          <div style={{ fontSize: 12, color: "var(--ink-3)", marginTop: 10 }}>
            <span style={{ fontWeight: 600 }}>Dependencies: </span>
            {skill.dependencies.map((dep, index) => (
              <span key={dep}>
                <span style={{ color: "var(--blue)" }}>{dep}</span>
                {index < skill.dependencies.length - 1 ? ", " : ""}
              </span>
            ))}
          </div>
        )}
      </div>

      <div style={{ flex: 1, overflow: "auto", padding: "14px 20px" }}>
        <Eyebrow style={{ marginBottom: 8 }}>Skill Content</Eyebrow>
        <pre
          style={{
            fontSize: 12,
            color: "var(--ink-2)",
            lineHeight: 1.6,
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
            fontFamily: "ui-monospace, 'Cascadia Code', Menlo, monospace",
            margin: 0,
            background: "var(--bg-tertiary)",
            border: "1px solid var(--line)",
            borderRadius: 8,
            padding: 16,
          }}
        >
          {skill.content}
        </pre>
      </div>
    </ModalOverlay>
  );
}

function SkillPageCreateModal({ icons, onClose, onCreated }) {
  const [id, setId] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [tags, setTags] = useState("");
  const [content, setContent] = useState(
    "# My Custom Skill\n\n## Overview\nDescribe what this skill does.\n\n## Instructions\nProvide detailed instructions for the AI agent.\n"
  );
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  const handleSave = async () => {
    if (!id.trim() || !name.trim() || !content.trim()) {
      setError("ID, name, and content are required");
      return;
    }
    if (!/^[a-z0-9_]+$/.test(id)) {
      setError("ID must be lowercase letters, numbers, and underscores only");
      return;
    }
    setSaving(true);
    try {
      await createSkill({
        id: id.trim(),
        name: name.trim(),
        description: description.trim(),
        tags: tags.split(",").map((tag) => tag.trim()).filter(Boolean),
        content: content.trim(),
      });
      onCreated();
    } catch {
      setError("Failed to save skill");
    } finally {
      setSaving(false);
    }
  };

  return (
    <ModalOverlay onClose={onClose} maxWidth={650}>
      <ModalHeader title="Create Custom Skill" onClose={onClose} closeIcon={icons?.close?.(14)} />

      <div style={{ flex: 1, overflow: "auto", padding: "16px 20px", display: "flex", flexDirection: "column", gap: 14 }}>
        <Field label="Skill ID (lowercase, no spaces)">
          <input value={id} onChange={(event) => setId(event.target.value)} placeholder="my_custom_skill" style={inputSx} />
        </Field>
        <Field label="Name">
          <input value={name} onChange={(event) => setName(event.target.value)} placeholder="My Custom Skill" style={inputSx} />
        </Field>
        <Field label="Description (short summary)">
          <input
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            placeholder="What this skill teaches the agent to do"
            style={inputSx}
          />
        </Field>
        <Field label="Tags (comma-separated)">
          <input value={tags} onChange={(event) => setTags(event.target.value)} placeholder="buildings, layout, custom" style={inputSx} />
        </Field>
        <Field label="Content (Markdown - instructions for the AI agent)">
          <textarea
            value={content}
            onChange={(event) => setContent(event.target.value)}
            style={{
              ...inputSx,
              height: 200,
              resize: "vertical",
              fontFamily: "ui-monospace, Menlo, monospace",
              lineHeight: 1.5,
            }}
          />
        </Field>
        {error && <div style={{ fontSize: 12, color: "var(--red)" }}>{error}</div>}
      </div>

      <ModalFooter>
        <Btn variant="cancel" onClick={onClose}>Cancel</Btn>
        <Btn variant="success" disabled={saving} onClick={handleSave}>{saving ? "Saving..." : "Create Skill"}</Btn>
      </ModalFooter>
    </ModalOverlay>
  );
}

export default function SkillsPage({ icons, newlyAddedSkillIds = [], onMarkSkillSeen }) {
  const [skills, setSkills] = useState([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("all");
  const [previewSkill, setPreviewSkill] = useState(null);
  const [showCreate, setShowCreate] = useState(false);
  const [deleteId, setDeleteId] = useState(null);

  const reload = () => {
    setLoading(true);
    fetchSkills()
      .then(setSkills)
      .catch(() => {})
      .finally(() => setLoading(false));
  };

  useEffect(reload, []);

  const handlePreview = async (id) => {
    const normalizedId = String(id || "").trim();
    if (normalizedId && typeof onMarkSkillSeen === "function") {
      onMarkSkillSeen(normalizedId);
    }
    try {
      const detail = await fetchSkillDetails(id);
      setPreviewSkill(detail);
    } catch {}
  };

  const handleDelete = async (id) => {
    await deleteSkill(id);
    setPreviewSkill(null);
    setDeleteId(null);
    reload();
  };

  const filtered = skills.filter((skill) => {
    if (filter !== "all" && skill.source !== filter) return false;
    if (!search.trim()) return true;
    const q = search.toLowerCase();
    return (
      skill.name.toLowerCase().includes(q) ||
      skill.description.toLowerCase().includes(q) ||
      skill.tags.some((tag) => tag.toLowerCase().includes(q))
    );
  });

  const newlyAddedSkillIdSet = useMemo(
    () =>
      new Set(
        (Array.isArray(newlyAddedSkillIds) ? newlyAddedSkillIds : [])
          .map((id) => String(id || "").trim())
          .filter(Boolean)
      ),
    [newlyAddedSkillIds]
  );

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column", background: "var(--bg)" }}>
      <PageHeader
        icon={icons?.book?.(22)}
        title="Skills"
        subtitle="Browse, create, and manage skills that teach the AI agent new capabilities"
        action={<Btn variant="success" size="md" onClick={() => setShowCreate(true)}>+ Create Skill</Btn>}
      />

      <div style={{ padding: "10px 24px", borderBottom: "1px solid var(--line)", display: "flex", gap: 10, alignItems: "center" }}>
        <input
          type="text"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          placeholder="Search skills..."
          style={{ ...inputSx, flex: 1 }}
        />
        <div style={{ display: "flex", gap: 4 }}>
          {["all", "builtin", "custom"].map((item) => (
            <button key={item} onClick={() => setFilter(item)} className={`filter-pill${filter === item ? " active" : ""}`}>
              {item}
            </button>
          ))}
        </div>
        <span style={{ fontSize: 12, color: "var(--ink-2)", whiteSpace: "nowrap" }}>
          {filtered.length} skill{filtered.length !== 1 ? "s" : ""}
        </span>
      </div>

      <div style={{ flex: 1, overflow: "auto", padding: 20 }}>
        {loading ? (
          <div style={{ textAlign: "center", padding: 60, color: "var(--ink-3)" }}>Loading...</div>
        ) : filtered.length === 0 ? (
          <div style={{ textAlign: "center", padding: 60 }}>
            <div style={{ fontSize: 40, marginBottom: 12 }}>{icons?.book?.(40)}</div>
            <div style={{ fontSize: 16, color: "var(--ink)", fontWeight: 600, marginBottom: 8 }}>
              No skills found
            </div>
            <div style={{ fontSize: 13, color: "var(--ink-3)" }}>
              {search ? "Try a different search term." : "Create a custom skill to get started."}
            </div>
          </div>
        ) : (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))", gap: 16 }}>
            {filtered.map((skill) => (
              <SkillPageCard
                key={skill.id}
                skill={skill}
                isNew={newlyAddedSkillIdSet.has(String(skill?.id || "").trim())}
                onClick={() => handlePreview(skill.id)}
              />
            ))}
          </div>
        )}
      </div>

      {previewSkill && (
        <SkillPageDetailModal
          icons={icons}
          skill={previewSkill}
          onClose={() => setPreviewSkill(null)}
          onDelete={previewSkill.source === "custom" ? () => setDeleteId(previewSkill.id) : undefined}
        />
      )}
      {deleteId && (
        <ConfirmDeleteModal
          message="Are you sure you want to delete this skill? This cannot be undone."
          onConfirm={() => handleDelete(deleteId)}
          onCancel={() => setDeleteId(null)}
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

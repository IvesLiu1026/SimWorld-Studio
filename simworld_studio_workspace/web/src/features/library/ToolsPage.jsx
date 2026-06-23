import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  deleteToolProcedure,
  fetchSkillDetails,
  fetchSkills,
  fetchTools,
  updateToolProcedure,
} from "../../api/appApi.js";
import {
  Badge,
  Btn,
  ModalFooter,
  ModalHeader,
  ModalOverlay,
  PageHeader,
  StatusBadge,
  ToggleBtn,
  inputSx,
} from "../../components/ui/primitives.jsx";
import ConfirmDeleteModal from "./ConfirmDeleteModal.jsx";
import { SkillPageDetailModal } from "./SkillsPage.jsx";
import { STATIC_MCP_TOOL_DEFS } from "./staticMcpTools.js";

function ToolPageCard({ busy, isNew = false, onClick, onDelete, onToggleEnabled, tool }) {
  const successRate =
    tool.metrics?.usageCount > 0 ? Math.round((tool.metrics.successCount / tool.metrics.usageCount) * 100) : null;

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
          {tool.name || tool.id}
        </span>
        {isNew && (
          <Badge variant="red" dot>
            NEW
          </Badge>
        )}
        <StatusBadge enabled={tool.enabled} />
      </div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "1fr 1fr",
          gap: 3,
          paddingTop: 4,
          borderTop: "1px solid var(--line)",
          fontSize: 11,
          color: "var(--ink-3)",
        }}
      >
        <div>Template: <span style={{ color: "var(--ink)" }}>{tool.template || "-"}</span></div>
        <div>Primitive: <span style={{ color: "var(--ink)" }}>{tool.primitive || "-"}</span></div>
        <div>Usage: <span style={{ color: "var(--ink)" }}>{tool.metrics?.usageCount || 0}</span></div>
        <div>Success: <span style={{ color: "var(--ink)" }}>{successRate == null ? "-" : `${successRate}%`}</span></div>
      </div>

      <div style={{ display: "flex", gap: 6, marginTop: "auto" }}>
        <ToggleBtn enabled={tool.enabled} busy={busy} onClick={(event) => { event.stopPropagation(); onToggleEnabled(); }} />
        <Btn variant="danger" disabled={busy} onClick={(event) => { event.stopPropagation(); onDelete(); }}>Delete</Btn>
      </div>
    </div>
  );
}

function StaticToolRefCard({ tool }) {
  return (
    <div
      style={{
        padding: "10px 12px",
        borderRadius: 8,
        border: "1px solid var(--line)",
        background: "var(--panel)",
        display: "flex",
        alignItems: "center",
        gap: 8,
        cursor: "pointer",
      }}
    >
      <span style={{ color: "var(--ink)", fontSize: 12, fontWeight: 600 }}>{tool.name}</span>
      <span
        style={{
          marginLeft: "auto",
          fontSize: 12,
          color: "var(--ink-3)",
          border: "1px solid var(--line)",
          borderRadius: 10,
          padding: "2px 7px",
        }}
      >
        Reference
      </span>
    </div>
  );
}

function ToolDetailModal({ busy, onClose, onDelete, onOpenSkill, onToggleEnabled, readOnly = false, relatedSkills, tool }) {
  const successRate =
    tool.metrics?.usageCount > 0 ? Math.round((tool.metrics.successCount / tool.metrics.usageCount) * 100) : null;
  const schema = tool.paramsSchema || { type: "object", properties: {} };
  const requiredKeys = Array.isArray(schema.required) ? schema.required : [];

  return (
    <ModalOverlay onClose={onClose}>
      <ModalHeader
        title={tool.name || tool.id}
        subtitle={
          <>
            <span style={{ color: "var(--blue)", fontFamily: "monospace" }}>{tool.mcpName}</span>
            <StatusBadge enabled={tool.enabled} readOnly={readOnly} style={{ marginLeft: 8 }} />
          </>
        }
        onClose={onClose}
      >
        {!readOnly && <Btn variant="danger" disabled={busy} onClick={onDelete}>Delete</Btn>}
      </ModalHeader>

      <div style={{ padding: "12px 20px", borderBottom: "1px solid var(--line)" }}>
        <div style={{ fontSize: 13, color: "var(--ink)", lineHeight: 1.5 }}>{tool.description || "No description"}</div>
        {!readOnly && (
          <div style={{ marginTop: 10, fontSize: 12, color: "var(--ink-3)" }}>
            Related skills: <span style={{ color: "var(--ink)" }}>{relatedSkills?.length || 0}</span>
          </div>
        )}
        {!readOnly && relatedSkills?.length > 0 && (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 8 }}>
            {relatedSkills.map((skill) => (
              <button
                key={skill.id || skill.name}
                onClick={() => onOpenSkill?.(skill.id)}
                style={{
                  fontSize: 12,
                  padding: "2px 7px",
                  borderRadius: 10,
                  background: "var(--blue-soft)",
                  color: "var(--blue)",
                  border: "1px solid color-mix(in srgb, var(--blue) 30%, transparent)",
                  cursor: "pointer",
                }}
              >
                {skill.name}
              </button>
            ))}
          </div>
        )}
      </div>

      <div style={{ flex: 1, overflow: "auto", padding: "14px 20px" }}>
        {!readOnly && (
          <div
            style={{
              marginTop: 2,
              display: "grid",
              gridTemplateColumns: "repeat(2, minmax(140px, 1fr))",
              gap: 8,
              fontSize: 12,
              color: "var(--ink-3)",
            }}
          >
            <div>Template: <span style={{ color: "var(--ink)" }}>{tool.template || "-"}</span></div>
            <div>Primitive: <span style={{ color: "var(--ink)" }}>{tool.primitive || "-"}</span></div>
            <div>Usage: <span style={{ color: "var(--ink)" }}>{tool.metrics?.usageCount || 0}</span></div>
            <div>Success: <span style={{ color: "var(--ink)" }}>{successRate == null ? "-" : `${successRate}%`}</span></div>
          </div>
        )}

        <div style={{ marginTop: 16, fontSize: 12, color: "var(--ink-2)", fontWeight: 600, textTransform: "uppercase", letterSpacing: 0.5 }}>
          How the Agent Calls This Tool
        </div>
        <div style={{ marginTop: 8, fontSize: 12, color: "var(--ink-3)", lineHeight: 1.5 }}>
          Use <span style={{ color: "var(--blue)", fontFamily: "monospace" }}>{tool.mcpName}</span> with an arguments object.
        </div>
        <div style={{ marginTop: 10, fontSize: 12, color: "var(--ink-3)" }}>
          Required arguments: <span style={{ color: "var(--ink)" }}>{requiredKeys.length > 0 ? requiredKeys.join(", ") : "none"}</span>
        </div>
        <div style={{ marginTop: 10, fontSize: 12, color: "var(--ink-3)" }}>Input schema</div>
        <pre
          style={{
            margin: "6px 0 0",
            background: "var(--panel)",
            border: "1px solid var(--line)",
            borderRadius: 8,
            padding: 12,
            fontSize: 12,
            color: "var(--ink)",
            lineHeight: 1.45,
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
            fontFamily: "ui-monospace, 'Cascadia Code', 'Source Code Pro', Menlo, monospace",
          }}
        >
          {JSON.stringify(schema, null, 2)}
        </pre>
      </div>

      {!readOnly && (
        <ModalFooter>
          <ToggleBtn enabled={tool.enabled} busy={busy} onClick={onToggleEnabled} />
        </ModalFooter>
      )}
    </ModalOverlay>
  );
}

export default function ToolsPage({ icons, newlyAddedToolIds = [], onMarkToolSeen }) {
  const [tools, setTools] = useState([]);
  const [skillDetails, setSkillDetails] = useState([]);
  const [loading, setLoading] = useState(true);
  const [busyToolId, setBusyToolId] = useState(null);
  const [deleteToolId, setDeleteToolId] = useState(null);
  const [previewTool, setPreviewTool] = useState(null);
  const [previewStaticTool, setPreviewStaticTool] = useState(null);
  const [previewSkill, setPreviewSkill] = useState(null);
  const [search, setSearch] = useState("");
  const [error, setError] = useState("");

  const reload = useCallback(async () => {
    try {
      setError("");
      const [toolData, skillsMeta] = await Promise.all([fetchTools(), fetchSkills()]);
      const candidateSkills = Array.isArray(skillsMeta)
        ? skillsMeta.filter((skill) => skill && skill.id && (skill.source === "custom" || (skill.tags || []).includes("learned")))
        : [];
      const detailedSkills = await Promise.all(
        candidateSkills.map(async (skill) => {
          try {
            return await fetchSkillDetails(skill.id);
          } catch {
            return skill;
          }
        })
      );
      setTools(Array.isArray(toolData) ? toolData : []);
      setSkillDetails(detailedSkills.filter(Boolean));
    } catch {
      setError("Failed to load learned tools");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    reload();
    const timer = setInterval(reload, 60000);
    return () => clearInterval(timer);
  }, [reload]);

  useEffect(() => {
    if (!previewTool) return;
    const next = tools.find((tool) => tool.id === previewTool.id) || null;
    if (!next) {
      setPreviewTool(null);
      return;
    }
    if (next !== previewTool) setPreviewTool(next);
  }, [tools, previewTool]);

  const filteredTools = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return tools;
    return tools.filter((tool) => {
      const haystack = [tool.id, tool.name, tool.description, tool.mcpName, tool.template, tool.primitive]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      return haystack.includes(q);
    });
  }, [tools, search]);

  const newlyAddedToolIdSet = useMemo(
    () =>
      new Set(
        (Array.isArray(newlyAddedToolIds) ? newlyAddedToolIds : [])
          .map((id) => String(id || "").trim())
          .filter(Boolean)
      ),
    [newlyAddedToolIds]
  );

  const relatedSkillsByToolId = useMemo(() => {
    const map = new Map();
    const add = (toolId, skill) => {
      if (!toolId || !skill || !skill.id || !skill.name) return;
      const key = String(toolId).trim();
      if (!key) return;
      if (!map.has(key)) map.set(key, []);
      const arr = map.get(key);
      if (!arr.some((item) => item.id === skill.id)) arr.push(skill);
    };

    const extractToolIds = (text) => {
      const out = [];
      const re = /learned__([a-z0-9_]+)/gi;
      const src = String(text || "");
      let match;
      while ((match = re.exec(src)) !== null) out.push(match[1]);
      return out;
    };

    for (const skill of skillDetails) {
      const skillId = String(skill?.id || "").trim();
      const skillName = String(skill?.name || skill?.id || "").trim();
      if (!skillId || !skillName) continue;
      const skillRef = { id: skillId, name: skillName };
      const references = new Set([
        ...extractToolIds(skill?.content),
        ...extractToolIds(skill?.description),
        ...((Array.isArray(skill?.dependencies) ? skill.dependencies : [])
          .map((dep) => String(dep || "").trim())
          .filter((dep) => dep.startsWith("learned__"))
          .map((dep) => dep.slice("learned__".length))),
      ]);
      for (const toolId of references) add(toolId, skillRef);
    }
    return map;
  }, [skillDetails]);

  const openRelatedSkill = async (skillId) => {
    if (!skillId) return;
    const existing = skillDetails.find((skill) => skill.id === skillId);
    if (existing?.content) {
      setPreviewSkill(existing);
      return;
    }
    try {
      const detail = await fetchSkillDetails(skillId);
      if (detail) setPreviewSkill(detail);
    } catch {}
  };

  const toggleEnabled = async (tool) => {
    setBusyToolId(tool.id);
    try {
      await updateToolProcedure(tool.id, { enabled: !tool.enabled });
      await reload();
    } catch {
      setError("Failed to update tool");
    } finally {
      setBusyToolId(null);
    }
  };

  const deleteTool = async (toolId) => {
    setBusyToolId(toolId);
    try {
      await deleteToolProcedure(toolId);
      await reload();
      setDeleteToolId(null);
      if (previewTool?.id === toolId) setPreviewTool(null);
    } catch {
      setError("Failed to delete tool");
    } finally {
      setBusyToolId(null);
    }
  };

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column", background: "var(--bg)" }}>
      <PageHeader
        icon={icons?.wrench?.(22)}
        title="Tools"
        subtitle="Static MCP tools for reference + learned tools you can manage"
      />

      {error && (
        <div style={{ padding: "8px 24px", borderBottom: "1px solid var(--line)", color: "var(--red)", fontSize: 12 }}>
          {error}
        </div>
      )}

      <div style={{ flex: 1, overflow: "auto", padding: 20 }}>
        <div style={{ border: "1px solid var(--line)", borderRadius: 10, marginBottom: 16, overflow: "hidden", background: "var(--bg)" }}>
          <div style={{ padding: "10px 12px", borderBottom: "1px solid var(--line)", display: "flex", alignItems: "center", gap: 8, background: "var(--panel)" }}>
            <span style={{ fontSize: 14, display: "inline-flex", alignItems: "center" }}>{icons?.book?.(14)}</span>
            <span style={{ fontSize: 13, fontWeight: 700, color: "var(--ink)" }}>Static MCP Tools (Reference)</span>
            <span style={{ marginLeft: "auto", fontSize: 12, color: "var(--ink-3)" }}>{STATIC_MCP_TOOL_DEFS.length} tools</span>
          </div>
          <div style={{ padding: 12, display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))", gap: 10 }}>
            {STATIC_MCP_TOOL_DEFS.map((tool) => (
              <div key={tool.id} onClick={() => setPreviewStaticTool(tool)}>
                <StaticToolRefCard tool={tool} />
              </div>
            ))}
          </div>
        </div>

        <div style={{ border: "1px solid var(--line)", borderRadius: 10, overflow: "hidden", background: "var(--bg)" }}>
          <div style={{ padding: "10px 12px", borderBottom: "1px solid var(--line)", display: "flex", alignItems: "center", gap: 8, background: "var(--panel)" }}>
            <span style={{ fontSize: 14, display: "inline-flex", alignItems: "center" }}>{icons?.brain?.(14)}</span>
            <span style={{ fontSize: 13, fontWeight: 700, color: "var(--ink)" }}>Dynamic Learned Tools</span>
            <span style={{ marginLeft: "auto", fontSize: 12, color: "var(--ink-3)" }}>
              {filteredTools.length} tool{filteredTools.length !== 1 ? "s" : ""}
            </span>
          </div>
          <div style={{ padding: 12, borderBottom: "1px solid var(--line)", display: "flex", gap: 10, alignItems: "center" }}>
            <input
              type="text"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Search learned tools by name, template, primitive, or description..."
              style={{ ...inputSx, flex: 1, background: "var(--panel)" }}
            />
          </div>
          <div style={{ padding: 12 }}>
            {loading ? (
              <div style={{ color: "var(--ink-3)", padding: 20 }}>Loading...</div>
            ) : filteredTools.length === 0 ? (
              <div style={{ textAlign: "center", padding: 40 }}>
                <div style={{ fontSize: 32, marginBottom: 10 }}>{icons?.wrench?.(32)}</div>
                <div style={{ fontSize: 15, color: "var(--ink)", fontWeight: 600, marginBottom: 6 }}>
                  No learned tools found
                </div>
                <div style={{ fontSize: 12, color: "var(--ink-3)" }}>
                  {search.trim() ? "Try a different search term." : "No learned tools have been promoted yet."}
                </div>
              </div>
            ) : (
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))", gap: 16 }}>
                {filteredTools.map((tool) => (
                  <ToolPageCard
                    key={tool.id}
                    tool={tool}
                    busy={busyToolId === tool.id}
                    isNew={newlyAddedToolIdSet.has(String(tool?.id || "").trim())}
                    onClick={() => {
                      if (typeof onMarkToolSeen === "function") onMarkToolSeen(String(tool?.id || "").trim());
                      setPreviewTool(tool);
                    }}
                    onToggleEnabled={() => toggleEnabled(tool)}
                    onDelete={() => setDeleteToolId(tool.id)}
                  />
                ))}
              </div>
            )}
          </div>
        </div>
      </div>

      {previewTool && (
        <ToolDetailModal
          tool={previewTool}
          relatedSkills={relatedSkillsByToolId.get(previewTool.id) || []}
          busy={busyToolId === previewTool.id}
          onClose={() => setPreviewTool(null)}
          onToggleEnabled={() => toggleEnabled(previewTool)}
          onDelete={() => setDeleteToolId(previewTool.id)}
          onOpenSkill={openRelatedSkill}
        />
      )}
      {previewStaticTool && (
        <ToolDetailModal
          tool={previewStaticTool}
          relatedSkills={[]}
          busy={false}
          onClose={() => setPreviewStaticTool(null)}
          onToggleEnabled={() => {}}
          onDelete={() => {}}
          onOpenSkill={() => {}}
          readOnly
        />
      )}
      {previewSkill && <SkillPageDetailModal icons={icons} skill={previewSkill} onClose={() => setPreviewSkill(null)} />}
      {deleteToolId && (
        <ConfirmDeleteModal
          message={`Delete learned tool "${deleteToolId}"? This cannot be undone.`}
          onConfirm={() => deleteTool(deleteToolId)}
          onCancel={() => setDeleteToolId(null)}
        />
      )}
    </div>
  );
}

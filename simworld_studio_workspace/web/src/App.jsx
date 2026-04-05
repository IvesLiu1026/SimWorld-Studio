import React, { useState, useEffect, useRef, useCallback, useMemo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

// ─── Constants ───────────────────────────────────────────────────────────────

const API_BASE = "/api";
const EVOLUTION_ARTIFACT_POLL_MS = 5000;
const EVOLUTION_TOAST_MAX = 2;
const EVOLUTION_TOAST_TTL_MS = 5200;

const TOOL_ICONS = {
  get_actors_in_level: "📋",
  find_actors_by_name: "🔍",
  spawn_actor: "➕",
  delete_actor: "🗑️",
  set_actor_transform: "↔️",
  get_actor_properties: "📄",
  focus_viewport: "🎥",
  take_screenshot: "📸",
  get_camera_0_view: "📸",
  execute_python_script: "🐍",
  create_blueprint: "🔧",
  compile_blueprint: "⚙️",
  apply_material_to_actor: "🎨",
  initialize: "🔌",
  observe_scene: "👁️",
  get_scene_overview: "🗺️",
};

const TAG_COLORS = {
  city: "#1f6feb",
  buildings: "#da3633",
  props: "#8b949e",
  weather: "#f0883e",
  camera: "#a371f7",
  layout: "#3fb950",
  planning: "#1f6feb",
  spacing: "#d29922",
  trees: "#3fb950",
  vehicles: "#f0883e",
  lighting: "#d29922",
  atmosphere: "#a371f7",
  screenshot: "#a371f7",
  decoration: "#8b949e",
  furniture: "#8b949e",
  architecture: "#da3633",
  environment: "#3fb950",
  viewpoint: "#a371f7",
  placement: "#d29922",
  roads: "#656d76",
  capture: "#a371f7",
};

const CATEGORY_ICONS = {
  buildings: "🏢",
  trees: "🌳",
  vehicles: "🚗",
  street_furniture: "🚰",
  roads: "🛣️",
  static_meshes: "⬜",
};

const CATEGORY_COLORS = {
  buildings: "#1f6feb",
  trees: "#3fb950",
  vehicles: "#f0883e",
  street_furniture: "#8b949e",
  roads: "#656d76",
  static_meshes: "#484f58",
};

const CAMERA_PRESETS = [
  { label: "⬆ Top", title: "Bird's-eye view", args: [0, 0, 5000, -90, 0, 0] },
  { label: "◎ Iso", title: "Isometric overview", args: [3000, -3000, 3000, -35, 45, 0] },
  { label: "▶ Front", title: "Front view (Y-axis)", args: [0, -4000, 1000, 0, 0, 0] },
  { label: "▷ Side", title: "Side view (X-axis)", args: [-4000, 0, 1000, 0, 90, 0] },
];

const QUICK_SUGGESTIONS = [
  "Add more trees",
  "Move buildings further apart",
  "Change to sunset lighting",
  "Take a screenshot from a different angle",
  "Add street furniture",
];

const STATIC_MCP_TOOL_DEFS = [
  {
    id: "spawn_blueprint_actor",
    name: "spawn_blueprint_actor",
    mcpName: "spawn_blueprint_actor",
    enabled: true,
    description:
      "Spawn a SimWorld Blueprint actor (building, tree, vehicle, prop). Use this for all CityDatabase assets. The blueprint_id can be a full path like '/Game/CityDatabase/blueprints/BP_Building_01.BP_Building_01_C', or a shorthand like 'BP_Building_01', 'BP_Tree1', etc. For buildings you can even use just the number like '01' through '06'.",
    paramsSchema: {
      type: "object",
      properties: {
        actor_name: { type: "string", description: "Unique name for this actor (e.g. 'House_01', 'Tree_Left_1')" },
        blueprint_id: {
          type: "string",
          description:
            "Blueprint path or shorthand. Buildings: 'BP_Building_01' to 'BP_Building_06' (ONLY 01-06 available) (or just number). Trees: 'BP_Tree1'-'BP_Tree6'. Vehicles: 'BP_Scooter_01'-'BP_Scooter_04', 'BP_Cart'. Props: 'BP_Hydrant', 'BP_Trash_bin_a', 'BP_Table', etc.",
        },
        location: {
          type: "array",
          items: { type: "number" },
          description:
            "[x, y, z] in UE units (cm). 1m=100 units. Ground is 200m x 200m centered at origin, so keep X and Y between -9500 and 9500. Values outside this range will be clamped to stay on the ground.",
        },
        rotation: { type: "array", items: { type: "number" }, description: "[pitch, yaw, roll] in degrees" },
        scale: { type: "array", items: { type: "number" }, description: "[x, y, z] scale multipliers, default [1,1,1]" },
      },
      required: ["actor_name", "blueprint_id", "location"],
    },
  },
  {
    id: "spawn_actor",
    name: "spawn_actor",
    mcpName: "spawn_actor",
    enabled: true,
    description:
      "Spawn a static mesh actor. Use for basic shapes (/Engine/BasicShapes/Cube, Plane, etc.) or SM_ meshes. For SimWorld buildings/trees/props, prefer spawn_blueprint_actor instead.",
    paramsSchema: {
      type: "object",
      properties: {
        name: { type: "string", description: "Unique actor name" },
        static_mesh: {
          type: "string",
          description: "Full mesh path, e.g. '/Engine/BasicShapes/Cube.Cube' or '/Game/CityDatabase/meshes/SM_Road.SM_Road'",
        },
        location: { type: "array", items: { type: "number" }, description: "[x, y, z]" },
        rotation: { type: "array", items: { type: "number" }, description: "[pitch, yaw, roll]" },
        scale: { type: "array", items: { type: "number" }, description: "[x, y, z]" },
      },
      required: ["name", "static_mesh", "location"],
    },
  },
  { id: "delete_actor", name: "delete_actor", mcpName: "delete_actor", enabled: true, description: "Delete an actor by its name.", paramsSchema: { type: "object", properties: { name: { type: "string", description: "Actor name to delete" } }, required: ["name"] } },
  { id: "delete_all_spawned", name: "delete_all_spawned", mcpName: "delete_all_spawned", enabled: true, description: "Delete ALL actors spawned in this session. Use to clear the scene before rebuilding.", paramsSchema: { type: "object", properties: {} } },
  { id: "get_actors_in_level", name: "get_actors_in_level", mcpName: "get_actors_in_level", enabled: true, description: "List all actors currently in the UE level.", paramsSchema: { type: "object", properties: {} } },
  { id: "find_actors_by_name", name: "find_actors_by_name", mcpName: "find_actors_by_name", enabled: true, description: "Search for actors whose name matches a pattern.", paramsSchema: { type: "object", properties: { pattern: { type: "string", description: "Name pattern to search" } }, required: ["pattern"] } },
  { id: "set_actor_transform", name: "set_actor_transform", mcpName: "set_actor_transform", enabled: true, description: "Move, rotate, or scale an existing actor.", paramsSchema: { type: "object", properties: { name: { type: "string", description: "Actor name" }, location: { type: "array", items: { type: "number" }, description: "[x, y, z]" }, rotation: { type: "array", items: { type: "number" }, description: "[pitch, yaw, roll]" }, scale: { type: "array", items: { type: "number" }, description: "[x, y, z]" } }, required: ["name"] } },
  { id: "take_screenshot", name: "take_screenshot", mcpName: "take_screenshot", enabled: true, description: "Capture a screenshot of the current UE viewport and save it as PNG.", paramsSchema: { type: "object", properties: { filename: { type: "string", description: "Output filename (optional, auto-generated if omitted)" } } } },
  { id: "execute_python_script", name: "execute_python_script", mcpName: "execute_python_script", enabled: true, description: "Execute arbitrary Unreal Engine Python script. Use for advanced operations not covered by other tools.", paramsSchema: { type: "object", properties: { script: { type: "string", description: "Python code to execute in UE" } }, required: ["script"] } },
  { id: "list_assets", name: "list_assets", mcpName: "list_assets", enabled: true, description: "List available SimWorld assets. Returns buildings, trees, vehicles, street furniture, roads, and static meshes with their paths.", paramsSchema: { type: "object", properties: { category: { type: "string", description: "Optional: 'buildings', 'trees', 'vehicles', 'street_furniture', 'roads', 'static_meshes'. Omit for all." } } } },
  { id: "setup_environment", name: "setup_environment", mcpName: "setup_environment", enabled: true, description: "CALL THIS FIRST before spawning any objects! Sets up the scene environment: directional light (sun), sky atmosphere, sky light, fog, ground plane, and increases view distance. Without this, the scene will be black/empty.", paramsSchema: { type: "object", properties: { ground_size: { type: "number", description: "Ground plane scale (default 200 = 20km x 20km). Use 100 for small scenes, 300 for large cities." }, time_of_day: { type: "string", description: "'morning', 'noon', 'afternoon' (default), 'sunset', or 'night'" } } } },
  { id: "verify_scene", name: "verify_scene", mcpName: "verify_scene", enabled: true, description: "Call a verifier AI (Claude) to analyze the current scene. Takes a screenshot, gets all actors, then asks Claude to evaluate if placement is correct and matches the original request. Returns structured feedback with status (PASS/NEEDS_IMPROVEMENT/FAIL), issues found, and actionable suggestions. Use this after placing objects to check quality before finishing.", paramsSchema: { type: "object", properties: { original_request: { type: "string", description: "The original scene generation request to verify against (e.g. 'a suburban street with 3 houses and 2 trees')" }, focus_areas: { type: "string", description: "Optional: specific aspects to focus on (e.g. 'check building spacing', 'verify tree placement')" } }, required: [] } },
].sort((a, b) => a.id.localeCompare(b.id));

function buildWelcomeMessage() {
  return {
    id: generateMessageId(),
    role: "assistant",
    content: `Welcome to **SimWorld Studio**! I'm your scene generation agent.

I can build city scenes in Unreal Engine using SimWorld's assets.

Try:
- *"Build a small residential neighborhood with 6 houses and tree-lined streets"*
- *"Create a busy downtown intersection with tall buildings"*
- *"Place a park with trees and benches, set the weather to sunset"*`,
    timestamp: Date.now(),
  };
}

// ─── API Functions ───────────────────────────────────────────────────────────

async function fetchHealth() {
  return (await fetch(`${API_BASE}/health`)).json();
}

async function fetchSkills() {
  return (await fetch(`${API_BASE}/skills`)).json();
}

async function fetchSkillDetails(id) {
  return (await fetch(`${API_BASE}/skills/${id}`)).json();
}

async function createSkill(skill) {
  return (
    await fetch(`${API_BASE}/skills`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(skill),
    })
  ).json();
}

async function deleteSkill(id) {
  await fetch(`${API_BASE}/skills/${id}`, { method: "DELETE" });
}

async function fetchScenes() {
  return (await fetch(`${API_BASE}/scenes`)).json();
}

async function saveScene(scene) {
  return (
    await fetch(`${API_BASE}/scenes`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(scene),
    })
  ).json();
}

async function deleteScene(id) {
  await fetch(`${API_BASE}/scenes/${id}`, { method: "DELETE" });
}

async function fetchAssets() {
  return (await fetch(`${API_BASE}/assets`)).json();
}

async function sendChat(message, sessionId, onEvent, signal, options) {
  // Timeout for initial connection — if the server doesn't respond in 30s, fail
  const controller = signal ? undefined : new AbortController();
  const effectiveSignal = signal || controller?.signal;
  const connectTimeout = setTimeout(() => controller?.abort(), 30000);

  let response;
  try {
    response = await fetch(`${API_BASE}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message,
        sessionId,
        skills: options?.skills,
        feedback: options?.feedback,
        skillSelectionMode: options?.skillSelectionMode,
      }),
      signal: effectiveSignal,
    });
  } finally {
    clearTimeout(connectTimeout);
  }

  if (!response.ok || !response.body) {
    throw new Error(`Server error: ${response.status}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let lastDataTime = Date.now();
  const IDLE_TIMEOUT = 330000; // 5.5 minutes without any data = dead connection

  for (;;) {
    // Race between read and idle timeout
    const readPromise = reader.read();
    const timeoutPromise = new Promise((_, reject) => {
      const check = setInterval(() => {
        if (Date.now() - lastDataTime > IDLE_TIMEOUT) {
          clearInterval(check);
          reader.cancel();
          reject(new Error("Connection idle timeout"));
        }
      }, 5000);
      readPromise.then(() => clearInterval(check)).catch(() => clearInterval(check));
    });

    let result;
    try {
      result = await Promise.race([readPromise, timeoutPromise]);
    } catch (err) {
      // Idle timeout — treat as done
      onEvent({ type: "done", data: { sessionId: null, isError: true, latestScreenshot: null } });
      break;
    }

    const { done, value } = result;
    if (done) break;

    lastDataTime = Date.now();
    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() ?? "";

    for (const chunk of chunks) {
      const lines = chunk.split("\n");
      let eventType = "message";
      let data = "";

      for (const line of lines) {
        if (line.startsWith("event: ")) eventType = line.slice(7).trim();
        if (line.startsWith("data: ")) data = line.slice(6);
      }

      if (data) {
        try {
          const parsed = JSON.parse(data);
          onEvent({ type: eventType, data: parsed });
        } catch {}
      }
    }
  }
}

async function voteOnBattle(battleId, winner) {
  return (
    await fetch(`${API_BASE}/arena/battles/${battleId}/vote`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ winner }),
    })
  ).json();
}

async function fetchLeaderboard() {
  return (await fetch(`${API_BASE}/arena/leaderboard`)).json();
}

async function fetchGallery(options) {
  const params = new URLSearchParams();
  params.set("limit", String(options.limit));
  if (options?.offset) params.set("offset", String(options.offset));
  if (options?.sort) params.set("sort", options.sort);

  const query = params.toString() ? `?${params}` : "";
  const result = await (await fetch(`${API_BASE}/arena/gallery${query}`)).json();
  return Array.isArray(result) ? result : result.items || [];
}

async function shareToGallery(item) {
  return (
    await fetch(`${API_BASE}/arena/gallery`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(item),
    })
  ).json();
}

async function fetchAgents() {
  return (await fetch(`${API_BASE}/agents`)).json();
}

async function updateAgent(id, settings) {
  return (
    await fetch(`${API_BASE}/agents/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(settings),
    })
  ).json();
}

async function runArena(prompt, skills, onEvent, signal) {
  const response = await fetch(`${API_BASE}/arena/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt, skills }),
    signal,
  });

  if (!response.ok || !response.body) {
    throw new Error(`Server error: ${response.status}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() ?? "";

    for (const chunk of chunks) {
      const lines = chunk.split("\n");
      let eventType = "message";
      let data = "";

      for (const line of lines) {
        if (line.startsWith("event: ")) eventType = line.slice(7).trim();
        if (line.startsWith("data: ")) data = line.slice(6);
      }

      if (data) {
        try {
          const parsed = JSON.parse(data);
          onEvent(eventType, parsed);
        } catch {}
      }
    }
  }
}

async function fetchTools() {
  return (await fetch(`${API_BASE}/tools`)).json();
}

async function fetchEvolutionConfig() {
  return (await fetch(`${API_BASE}/evolution/config`)).json();
}

async function updateEvolutionConfig(enabled) {
  return (
    await fetch(`${API_BASE}/evolution/config`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: Boolean(enabled), source: "scene_agent_toggle" }),
    })
  ).json();
}

async function updateToolProcedure(id, patch) {
  return (
    await fetch(`${API_BASE}/tools/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    })
  ).json();
}

async function deleteToolProcedure(id) {
  return (
    await fetch(`${API_BASE}/tools/${id}`, {
      method: "DELETE",
    })
  ).json();
}

async function sendCameraCommand(cmd, args = []) {
  await fetch("/api/camera", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cmd, args }),
  });
}

// ─── Utility ─────────────────────────────────────────────────────────────────

let messageCounter = 0;

function generateMessageId() {
  return `msg-${++messageCounter}-${Date.now()}`;
}

function isLearnedSkillMeta(skill) {
  if (!skill || typeof skill !== "object") return false;
  const source = String(skill.source || "").toLowerCase();
  const tags = Array.isArray(skill.tags) ? skill.tags : [];
  return source === "custom" && tags.includes("learned");
}

function headerButtonStyle(color) {
  return {
    padding: "2px 10px",
    fontSize: 11,
    background: "#21262d",
    border: "1px solid #30363d",
    borderRadius: 4,
    color,
    cursor: "pointer",
  };
}

// ─── ToolCallBlock ───────────────────────────────────────────────────────────

function ToolCallBlock({ tool }) {
  const [expanded, setExpanded] = useState(false);

  const icon = TOOL_ICONS[tool.displayName] || "🔨";
  const statusColor = {
    starting: "#8b949e",
    running: "#d29922",
    done: "#3fb950",
    error: "#f85149",
  }[tool.status];
  const displayName = tool.displayName || tool.name.replace(/^mcp__\w+__/, "");

  let paramSummary = "";
  try {
    const input = tool.input || (tool.inputBuffer ? JSON.parse(tool.inputBuffer) : null);
    if (input) {
      paramSummary = Object.keys(input)
        .slice(0, 2)
        .map((key) => {
          const val = input[key];
          const str = Array.isArray(val) ? `[${val.join(",")}]` : String(val);
          return `${key}: ${str.slice(0, 30)}`;
        })
        .join(", ");
    }
  } catch {}

  return (
    <div
      style={{
        margin: "4px 0",
        border: "1px solid #30363d",
        borderRadius: 6,
        overflow: "hidden",
        background: "#0d1117",
      }}
    >
      <button
        onClick={() => setExpanded((prev) => !prev)}
        style={{
          width: "100%",
          display: "flex",
          alignItems: "center",
          gap: 8,
          padding: "5px 10px",
          background: "none",
          border: "none",
          color: "#8b949e",
          cursor: "pointer",
          textAlign: "left",
          fontSize: 12,
        }}
      >
        <span
          style={{
            display: "inline-block",
            width: 7,
            height: 7,
            borderRadius: "50%",
            background: statusColor,
            flexShrink: 0,
            ...(tool.status === "running"
              ? { animation: "pulse 1s ease-in-out infinite" }
              : {}),
          }}
        />
        <span style={{ fontSize: 13 }}>{icon}</span>
        <span style={{ fontFamily: "monospace", color: "#79c0ff", fontWeight: 500 }}>
          {displayName}
        </span>
        {paramSummary && (
          <span
            style={{
              color: "#656d76",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
              flex: 1,
              fontSize: 11,
            }}
          >
            ({paramSummary})
          </span>
        )}
        {tool.status === "running" && !tool.input && (
          <span style={{ color: "#d29922", fontSize: 10, marginLeft: "auto" }}>
            running…
          </span>
        )}
        <span style={{ marginLeft: "auto", fontSize: 10, flexShrink: 0 }}>
          {expanded ? "▲" : "▼"}
        </span>
      </button>

      {expanded && (
        <div style={{ padding: "8px 10px", borderTop: "1px solid #21262d" }}>
          {(tool.input || tool.inputBuffer) && (
            <div style={{ marginBottom: 6 }}>
              <div
                style={{
                  color: "#656d76",
                  fontSize: 10,
                  marginBottom: 3,
                  textTransform: "uppercase",
                  letterSpacing: 1,
                }}
              >
                Input
              </div>
              <pre
                style={{
                  margin: 0,
                  fontSize: 11,
                  color: "#e6edf3",
                  whiteSpace: "pre-wrap",
                  wordBreak: "break-word",
                }}
              >
                {tool.input ? JSON.stringify(tool.input, null, 2) : tool.inputBuffer}
              </pre>
            </div>
          )}
          {tool.result && (
            <div>
              <div
                style={{
                  color: "#656d76",
                  fontSize: 10,
                  marginBottom: 3,
                  textTransform: "uppercase",
                  letterSpacing: 1,
                }}
              >
                Result
              </div>
              <pre
                style={{
                  margin: 0,
                  fontSize: 11,
                  color: tool.isError ? "#f85149" : "#3fb950",
                  whiteSpace: "pre-wrap",
                  wordBreak: "break-word",
                  maxHeight: 200,
                  overflow: "auto",
                }}
              >
                {tool.result}
              </pre>
            </div>
          )}
          {tool.screenshot && (
            <div style={{ marginTop: 8 }}>
              <img
                src={tool.screenshot + `?t=${Date.now()}`}
                alt="UE screenshot"
                style={{
                  maxWidth: "100%",
                  borderRadius: 4,
                  border: "1px solid #30363d",
                }}
              />
            </div>
          )}
        </div>
      )}

      <style>{`@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} } @keyframes spin { to{transform:rotate(360deg)} }`}</style>
    </div>
  );
}

// ─── SkillItem ───────────────────────────────────────────────────────────────

function SkillItem({ skill, active, onToggle, onPreview, disabled }) {
  const toggle = () => {
    if (disabled) return;
    onToggle();
  };

  return (
    <div
      style={{
        padding: "8px 10px",
        borderRadius: 6,
        border: `1px solid ${active ? "#1f6feb" : "#21262d"}`,
        background: active ? "#1f3a5f22" : "#161b22",
        transition: "all 0.15s",
        opacity: disabled ? 0.75 : 1,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <span
          onClick={toggle}
          style={{
            width: 14,
            height: 14,
            borderRadius: 3,
            border: `2px solid ${active ? "#1f6feb" : "#30363d"}`,
            background: active ? "#1f6feb" : "transparent",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontSize: 10,
            color: "#fff",
            flexShrink: 0,
            cursor: disabled ? "not-allowed" : "pointer",
          }}
        >
          {active && "✓"}
        </span>
        <span
          onClick={toggle}
          style={{
            fontSize: 12,
            fontWeight: 500,
            color: "#e6edf3",
            cursor: disabled ? "not-allowed" : "pointer",
            flex: 1,
          }}
        >
          {skill.name}
        </span>
        <button
          onClick={(e) => {
            e.stopPropagation();
            onPreview();
          }}
          style={{
            padding: "2px 6px",
            fontSize: 10,
            background: "#21262d",
            border: "1px solid #30363d",
            borderRadius: 4,
            color: "#8b949e",
            cursor: "pointer",
          }}
          title="Preview skill details"
        >
          Preview
        </button>
        <span
          style={{
            fontSize: 9,
            padding: "1px 5px",
            borderRadius: 4,
            background: skill.source === "custom" ? "#1f6feb22" : "#21262d",
            color: skill.source === "custom" ? "#58a6ff" : "#484f58",
          }}
        >
          {skill.source}
        </span>
      </div>

      <div
        style={{
          fontSize: 11,
          color: "#8b949e",
          marginTop: 4,
          marginLeft: 20,
          lineHeight: 1.4,
        }}
      >
        {skill.description}
      </div>

      {skill.tags.length > 0 && (
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            gap: 4,
            marginTop: 5,
            marginLeft: 20,
          }}
        >
          {skill.tags.map((tag) => (
            <span
              key={tag}
              style={{
                fontSize: 9,
                padding: "1px 5px",
                borderRadius: 4,
                background: (TAG_COLORS[tag] || "#30363d") + "33",
                color: TAG_COLORS[tag] || "#8b949e",
                border: `1px solid ${TAG_COLORS[tag] || "#30363d"}44`,
              }}
            >
              {tag}
            </span>
          ))}
        </div>
      )}

      {skill.dependencies.length > 0 && (
        <div
          style={{
            fontSize: 10,
            color: "#484f58",
            marginTop: 4,
            marginLeft: 20,
          }}
        >
          Depends on: {skill.dependencies.join(", ")}
        </div>
      )}
    </div>
  );
}

// ─── SkillPreviewModal ───────────────────────────────────────────────────────

function SkillPreviewModal({ skill, onClose, onDelete }) {
  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.7)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 9999,
      }}
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: "90%",
          maxWidth: 700,
          maxHeight: "80vh",
          background: "#0d1117",
          border: "1px solid #30363d",
          borderRadius: 12,
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
        }}
      >
        {/* Header */}
        <div
          style={{
            padding: "14px 18px",
            borderBottom: "1px solid #21262d",
            display: "flex",
            alignItems: "center",
            gap: 10,
          }}
        >
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 16, fontWeight: 600, color: "#e6edf3" }}>
              {skill.name}
            </div>
            <div style={{ fontSize: 11, color: "#8b949e", marginTop: 3 }}>
              v{skill.version} by {skill.author}
              <span
                style={{
                  marginLeft: 8,
                  padding: "1px 5px",
                  borderRadius: 4,
                  background: skill.source === "custom" ? "#1f6feb22" : "#21262d",
                  color: skill.source === "custom" ? "#58a6ff" : "#484f58",
                  fontSize: 9,
                }}
              >
                {skill.source}
              </span>
            </div>
          </div>
          {onDelete && (
            <button
              onClick={onDelete}
              style={{
                padding: "4px 10px",
                fontSize: 11,
                background: "#da363322",
                border: "1px solid #da363366",
                borderRadius: 6,
                color: "#f85149",
                cursor: "pointer",
              }}
            >
              Delete
            </button>
          )}
          <button
            onClick={onClose}
            style={{
              padding: "4px 10px",
              fontSize: 14,
              background: "transparent",
              border: "none",
              color: "#8b949e",
              cursor: "pointer",
            }}
          >
            ✕
          </button>
        </div>

        {/* Description + Tags */}
        <div style={{ padding: "10px 18px", borderBottom: "1px solid #21262d" }}>
          <div style={{ fontSize: 12, color: "#8b949e", lineHeight: 1.5 }}>
            {skill.description}
          </div>
          {skill.tags.length > 0 && (
            <div
              style={{
                display: "flex",
                flexWrap: "wrap",
                gap: 4,
                marginTop: 8,
              }}
            >
              {skill.tags.map((tag) => (
                <span
                  key={tag}
                  style={{
                    fontSize: 10,
                    padding: "2px 7px",
                    borderRadius: 4,
                    background: (TAG_COLORS[tag] || "#30363d") + "33",
                    color: TAG_COLORS[tag] || "#8b949e",
                    border: `1px solid ${TAG_COLORS[tag] || "#30363d"}44`,
                  }}
                >
                  {tag}
                </span>
              ))}
            </div>
          )}
        </div>

        {/* Content */}
        <div style={{ flex: 1, overflow: "auto", padding: "14px 18px" }}>
          <pre
            style={{
              fontSize: 12,
              color: "#c9d1d9",
              lineHeight: 1.6,
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
              fontFamily:
                "ui-monospace, 'Cascadia Code', 'Source Code Pro', Menlo, monospace",
              margin: 0,
            }}
          >
            {skill.content}
          </pre>
        </div>
      </div>
    </div>
  );
}

// ─── CreateSkillModal ────────────────────────────────────────────────────────

function CreateSkillModal({ onClose, onCreated }) {
  const [id, setId] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [tags, setTags] = useState("");
  const [content, setContent] = useState(`# My Custom Skill

## Overview
Describe what this skill does.

## Instructions
Provide detailed instructions for the AI agent.
`);
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
        tags: tags
          .split(",")
          .map((t) => t.trim())
          .filter(Boolean),
        content: content.trim(),
      });
      onCreated();
    } catch {
      setError("Failed to save skill");
    } finally {
      setSaving(false);
    }
  };

  const inputStyle = {
    width: "100%",
    padding: "6px 10px",
    fontSize: 12,
    background: "#161b22",
    border: "1px solid #30363d",
    borderRadius: 6,
    color: "#e6edf3",
    outline: "none",
    boxSizing: "border-box",
  };

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.7)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 9999,
      }}
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: "90%",
          maxWidth: 650,
          maxHeight: "85vh",
          background: "#0d1117",
          border: "1px solid #30363d",
          borderRadius: 12,
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
        }}
      >
        {/* Header */}
        <div
          style={{
            padding: "14px 18px",
            borderBottom: "1px solid #21262d",
            display: "flex",
            alignItems: "center",
          }}
        >
          <span style={{ fontSize: 16, fontWeight: 600, color: "#e6edf3" }}>
            Create Custom Skill
          </span>
          <button
            onClick={onClose}
            style={{
              marginLeft: "auto",
              padding: "4px 10px",
              fontSize: 14,
              background: "transparent",
              border: "none",
              color: "#8b949e",
              cursor: "pointer",
            }}
          >
            ✕
          </button>
        </div>

        {/* Form */}
        <div
          style={{
            flex: 1,
            overflow: "auto",
            padding: "14px 18px",
            display: "flex",
            flexDirection: "column",
            gap: 12,
          }}
        >
          <div>
            <label style={{ fontSize: 11, color: "#8b949e", display: "block", marginBottom: 4 }}>
              Skill ID (lowercase, no spaces)
            </label>
            <input
              value={id}
              onChange={(e) => setId(e.target.value)}
              placeholder="my_custom_skill"
              style={inputStyle}
            />
          </div>
          <div>
            <label style={{ fontSize: 11, color: "#8b949e", display: "block", marginBottom: 4 }}>
              Name
            </label>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="My Custom Skill"
              style={inputStyle}
            />
          </div>
          <div>
            <label style={{ fontSize: 11, color: "#8b949e", display: "block", marginBottom: 4 }}>
              Description (short summary)
            </label>
            <input
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="What this skill teaches the agent to do"
              style={inputStyle}
            />
          </div>
          <div>
            <label style={{ fontSize: 11, color: "#8b949e", display: "block", marginBottom: 4 }}>
              Tags (comma-separated)
            </label>
            <input
              value={tags}
              onChange={(e) => setTags(e.target.value)}
              placeholder="buildings, layout, custom"
              style={inputStyle}
            />
          </div>
          <div style={{ flex: 1 }}>
            <label style={{ fontSize: 11, color: "#8b949e", display: "block", marginBottom: 4 }}>
              Content (Markdown — instructions for the AI agent)
            </label>
            <textarea
              value={content}
              onChange={(e) => setContent(e.target.value)}
              style={{
                ...inputStyle,
                height: 250,
                resize: "vertical",
                fontFamily:
                  "ui-monospace, 'Cascadia Code', 'Source Code Pro', Menlo, monospace",
                lineHeight: 1.5,
              }}
            />
          </div>
          {error && (
            <div style={{ fontSize: 11, color: "#f85149", padding: "4px 0" }}>{error}</div>
          )}
        </div>

        {/* Footer */}
        <div
          style={{
            padding: "12px 18px",
            borderTop: "1px solid #21262d",
            display: "flex",
            gap: 8,
            justifyContent: "flex-end",
          }}
        >
          <button
            onClick={onClose}
            style={{
              padding: "6px 14px",
              fontSize: 12,
              background: "#21262d",
              border: "1px solid #30363d",
              borderRadius: 6,
              color: "#c9d1d9",
              cursor: "pointer",
            }}
          >
            Cancel
          </button>
          <button
            onClick={handleSave}
            disabled={saving}
            style={{
              padding: "6px 14px",
              fontSize: 12,
              background: "#238636",
              border: "1px solid #2ea043",
              borderRadius: 6,
              color: "#fff",
              cursor: saving ? "wait" : "pointer",
              opacity: saving ? 0.7 : 1,
            }}
          >
            {saving ? "Saving..." : "Create Skill"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ─── SkillsPanel (sidebar in chat) ───────────────────────────────────────────

function SkillsPanel({
  selected,
  onChange,
  autoEnabled,
  onAutoEnabledChange,
  autoSelected,
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

  useEffect(reload, []);

  useEffect(() => {
    if (!expanded) return;

    // Keep learned skills in sync while users are actively viewing this panel.
    reload();
    const timer = setInterval(reload, 8000);
    return () => clearInterval(timer);
  }, [expanded]);

  const toggleSkill = (id) => {
    if (autoEnabled) return;
    onChange(selected.includes(id) ? selected.filter((s) => s !== id) : [...selected, id]);
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

  const builtinSkills = skills.filter((s) => s.source === "builtin");
  const customSkills = skills.filter((s) => s.source === "custom");

  return (
    <div
      style={{
        padding: "6px 12px",
        borderBottom: "1px solid #21262d",
        background: "#0d1117",
      }}
    >
      {/* Toggle header */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          cursor: "pointer",
          userSelect: "none",
        }}
        onClick={() => setExpanded(!expanded)}
      >
        <span style={{ fontSize: 11, color: "#8b949e", fontFamily: "monospace" }}>
          {expanded ? "▼" : "▶"}
        </span>
        <span style={{ fontSize: 12, fontWeight: 600, color: "#e6edf3" }}>Skills</span>
        {activeSkills.length > 0 && (
          <span
            style={{
              fontSize: 10,
              background: "#1f6feb",
              color: "#fff",
              borderRadius: 8,
              padding: "1px 6px",
              marginLeft: 4,
            }}
          >
            {activeSkills.length}
          </span>
        )}
        <div
          onClick={(e) => e.stopPropagation()}
          style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 6 }}
        >
          <span style={{ fontSize: 10, color: "#8b949e" }}>Auto-select skills</span>
          <button
            onClick={() => onAutoEnabledChange(!autoEnabled)}
            style={{
              width: 34,
              height: 18,
              borderRadius: 999,
              border: "1px solid #30363d",
              background: autoEnabled ? "#1f6feb" : "#21262d",
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
                background: "#fff",
                transform: autoEnabled ? "translateX(16px)" : "translateX(0)",
                transition: "transform 0.15s ease",
              }}
            />
          </button>
        </div>
        <span style={{ fontSize: 10, color: "#484f58", marginLeft: 6 }}>
          {skills.length} available
        </span>
      </div>

      {/* Expanded skill list */}
      {expanded && (
        <div
          style={{
            marginTop: 6,
            display: "flex",
            flexDirection: "column",
            gap: 4,
            maxHeight: 260,
            overflowY: "auto",
            paddingRight: 4,
          }}
        >
          <div
            style={{
              fontSize: 10,
              color: "#656d76",
              border: "1px solid #21262d",
              background: "#0d1117",
              borderRadius: 6,
              padding: "6px 8px",
              marginBottom: 2,
            }}
          >
            {autoEnabled
              ? "Auto mode: Claude pre-selects relevant skills before each run."
              : "Manual mode: check the exact skills you want active."}
          </div>
          {builtinSkills.length > 0 && (
            <div
              style={{
                fontSize: 10,
                color: "#484f58",
                fontWeight: 600,
                padding: "4px 0 2px",
              }}
            >
              BUILTIN
            </div>
          )}
          {builtinSkills.map((s) => (
            <SkillItem
              key={s.id}
              skill={s}
              active={activeSkills.includes(s.id)}
              onToggle={() => toggleSkill(s.id)}
              onPreview={() => handlePreview(s.id)}
              disabled={autoEnabled}
            />
          ))}

          {customSkills.length > 0 && (
            <div
              style={{
                fontSize: 10,
                color: "#484f58",
                fontWeight: 600,
                padding: "6px 0 2px",
              }}
            >
              CUSTOM
            </div>
          )}
          {customSkills.map((s) => (
            <SkillItem
              key={s.id}
              skill={s}
              active={activeSkills.includes(s.id)}
              onToggle={() => toggleSkill(s.id)}
              onPreview={() => handlePreview(s.id)}
              disabled={autoEnabled}
            />
          ))}

          <button
            onClick={(e) => {
              e.stopPropagation();
              setShowCreate(true);
            }}
            style={{
              padding: "6px 10px",
              marginTop: 4,
              borderRadius: 6,
              border: "1px dashed #30363d",
              background: "transparent",
              color: "#58a6ff",
              fontSize: 11,
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

      {/* Modals */}
      {previewSkill && (
        <SkillPreviewModal
          skill={previewSkill}
          onClose={() => setPreviewSkill(null)}
          onDelete={previewSkill.source === "custom" ? () => handleDelete(previewSkill.id) : undefined}
        />
      )}
      {showCreate && (
        <CreateSkillModal
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

// ─── AnnotateOverlay ─────────────────────────────────────────────────────────

function AnnotateOverlay({ src, onSubmitFeedback, onCancel }) {
  const [points, setPoints] = useState([]);
  const [feedbackText, setFeedbackText] = useState("");
  const imgRef = useRef(null);

  const handleImageClick = useCallback((e) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const x = (e.clientX - rect.left) / rect.width;
    const y = (e.clientY - rect.top) / rect.height;
    const text = prompt("Describe what to change at this point:");
    if (text) setPoints((prev) => [...prev, { x, y, text }]);
  }, []);

  const removePoint = (index) => {
    setPoints((prev) => prev.filter((_, i) => i !== index));
  };

  const handleSubmit = () => {
    let result = feedbackText;
    if (points.length > 0) {
      result += "\n\nAnnotated points on the screenshot:";
      for (const pt of points) {
        const pctX = Math.round(pt.x * 100);
        const pctY = Math.round(pt.y * 100);
        result += `\n- At position (${pctX}% from left, ${pctY}% from top): "${pt.text}"`;
      }
    }
    onSubmitFeedback(result.trim(), points);
  };

  return (
    <div
      style={{
        position: "absolute",
        inset: 0,
        background: "rgba(0,0,0,0.85)",
        display: "flex",
        flexDirection: "column",
        zIndex: 100,
      }}
    >
      {/* Toolbar */}
      <div
        style={{
          padding: "8px 14px",
          borderBottom: "1px solid #21262d",
          display: "flex",
          alignItems: "center",
          gap: 8,
          background: "#161b22",
        }}
      >
        <span style={{ fontSize: 13, fontWeight: 600, color: "#f0883e" }}>
          Annotate Screenshot
        </span>
        <span style={{ fontSize: 11, color: "#8b949e" }}>
          Click on the image to add feedback points
        </span>
        <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          <button
            onClick={onCancel}
            style={{
              padding: "4px 12px",
              fontSize: 11,
              borderRadius: 4,
              border: "1px solid #30363d",
              background: "#21262d",
              color: "#8b949e",
              cursor: "pointer",
            }}
          >
            Cancel
          </button>
          <button
            onClick={handleSubmit}
            disabled={!feedbackText && points.length === 0}
            style={{
              padding: "4px 12px",
              fontSize: 11,
              borderRadius: 4,
              border: "1px solid #1f6feb",
              background: "#1f6feb",
              color: "#fff",
              cursor: "pointer",
              opacity: !feedbackText && points.length === 0 ? 0.5 : 1,
            }}
          >
            Send Feedback
          </button>
        </div>
      </div>

      {/* Body */}
      <div style={{ flex: 1, display: "flex", gap: 0, overflow: "hidden" }}>
        {/* Image area */}
        <div style={{ flex: 1, position: "relative", overflow: "hidden" }}>
          <div
            onClick={handleImageClick}
            style={{
              width: "100%",
              height: "100%",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              cursor: "crosshair",
              position: "relative",
            }}
          >
            <img
              ref={imgRef}
              src={src}
              alt="Annotate"
              style={{ maxWidth: "100%", maxHeight: "100%", objectFit: "contain" }}
            />
            {points.map((pt, i) => (
              <div
                key={i}
                style={{
                  position: "absolute",
                  left: `${pt.x * 100}%`,
                  top: `${pt.y * 100}%`,
                  transform: "translate(-50%, -50%)",
                  pointerEvents: "auto",
                }}
              >
                <div
                  style={{
                    width: 24,
                    height: 24,
                    borderRadius: "50%",
                    background: "#f0883e",
                    border: "2px solid #fff",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    fontSize: 11,
                    fontWeight: 700,
                    color: "#fff",
                    cursor: "pointer",
                  }}
                  onClick={(e) => {
                    e.stopPropagation();
                    removePoint(i);
                  }}
                >
                  {i + 1}
                </div>
                <div
                  style={{
                    position: "absolute",
                    left: 16,
                    top: -4,
                    background: "#21262d",
                    border: "1px solid #30363d",
                    borderRadius: 4,
                    padding: "2px 6px",
                    fontSize: 10,
                    color: "#e6edf3",
                    whiteSpace: "nowrap",
                    maxWidth: 200,
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                  }}
                >
                  {pt.text}
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Sidebar */}
        <div
          style={{
            width: 260,
            borderLeft: "1px solid #21262d",
            background: "#0d1117",
            display: "flex",
            flexDirection: "column",
            padding: 12,
            gap: 8,
          }}
        >
          <span style={{ fontSize: 12, fontWeight: 600, color: "#e6edf3" }}>Feedback</span>
          <textarea
            value={feedbackText}
            onChange={(e) => setFeedbackText(e.target.value)}
            placeholder="Describe what to change overall..."
            style={{
              flex: 1,
              resize: "none",
              background: "#161b22",
              border: "1px solid #30363d",
              borderRadius: 6,
              color: "#e6edf3",
              padding: 8,
              fontSize: 12,
              fontFamily: "inherit",
            }}
          />
          {points.length > 0 && (
            <div style={{ fontSize: 11, color: "#8b949e" }}>
              <div style={{ fontWeight: 600, marginBottom: 4 }}>Annotations:</div>
              {points.map((pt, i) => (
                <div
                  key={i}
                  style={{
                    display: "flex",
                    gap: 4,
                    alignItems: "flex-start",
                    marginBottom: 4,
                  }}
                >
                  <span
                    style={{
                      width: 16,
                      height: 16,
                      borderRadius: "50%",
                      background: "#f0883e",
                      fontSize: 9,
                      color: "#fff",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      flexShrink: 0,
                    }}
                  >
                    {i + 1}
                  </span>
                  <span style={{ fontSize: 10, color: "#c9d1d9" }}>{pt.text}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ─── ChatMessage ─────────────────────────────────────────────────────────────

function ChatMessage({ message }) {
  const isUser = message.role === "user";

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: isUser ? "flex-end" : "flex-start",
      }}
    >
      <div
        style={{
          maxWidth: "92%",
          padding: "9px 13px",
          borderRadius: isUser ? "12px 12px 4px 12px" : "12px 12px 12px 4px",
          background: isUser ? "#1f3a5f" : "#161b22",
          border: `1px solid ${isUser ? "#1f6feb" : "#21262d"}`,
        }}
      >
        {isUser ? (
          <div style={{ color: "#e6edf3", fontSize: 14, whiteSpace: "pre-wrap" }}>
            {message.content}
          </div>
        ) : (
          <>
            {message.waiting && (
              <div style={{ color: "#8b949e", fontSize: 13, display: "flex", alignItems: "center", gap: 8, padding: "4px 0" }}>
                <span style={{ display: "inline-block", width: 8, height: 8, borderRadius: "50%", border: "2px solid #58a6ff", borderTopColor: "transparent", animation: "spin 1s linear infinite" }} />
                Waiting for Claude...
              </div>
            )}
            {message.blocks
              ? message.blocks.map((block, idx) =>
                  block.type === "text" ? (
                    block.content ? (
                      <div
                        className="markdown"
                        key={"t" + idx}
                        style={{ color: "#e6edf3", fontSize: 14 }}
                      >
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>
                          {block.content}
                        </ReactMarkdown>
                      </div>
                    ) : null
                  ) : (
                    <ToolCallBlock
                      key={block.toolId}
                      tool={(message.toolCalls || []).find((tc) => tc.id === block.toolId)}
                    />
                  )
                )
              : [
                  message.content && (
                    <div
                      className="markdown"
                      key="content"
                      style={{ color: "#e6edf3", fontSize: 14 }}
                    >
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>
                        {message.content}
                      </ReactMarkdown>
                    </div>
                  ),
                  (message.toolCalls || []).map((tc) => (
                    <ToolCallBlock key={tc.id} tool={tc} />
                  )),
                ]}
          </>
        )}
      </div>
      <div style={{ marginTop: 3, fontSize: 10, color: "#656d76", padding: "0 4px" }}>
        {isUser ? "You" : "Agent"} · {new Date(message.timestamp).toLocaleTimeString()}
      </div>
    </div>
  );
}

// ─── TypingIndicator ─────────────────────────────────────────────────────────

function TypingIndicator() {
  return (
    <div style={{ display: "flex", gap: 4, alignItems: "center", padding: "4px 0" }}>
      {[0, 1, 2].map((i) => (
        <div
          key={i}
          style={{
            width: 6,
            height: 6,
            borderRadius: "50%",
            background: "#656d76",
            animation: `bounce 1.2s ease-in-out ${i * 0.2}s infinite`,
          }}
        />
      ))}
      <style>
        {
          "@keyframes bounce { 0%,60%,100%{transform:translateY(0)} 30%{transform:translateY(-6px)} }"
        }
      </style>
    </div>
  );
}

// ─── ChatPanel ───────────────────────────────────────────────────────────────

function ChatPanel({ onScreenshotUpdate, onRef, onSessionChange, onChatDone }) {
  const [messages, setMessages] = useState(() => [buildWelcomeMessage()]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [sessionId, setSessionId] = useState(null);
  const [mcpStatus, setMcpStatus] = useState("–");
  const [selectedSkills, setSelectedSkills] = useState([]);
  const [autoSkillSelectionEnabled, setAutoSkillSelectionEnabled] = useState(true);
  const [autoSelectedSkills, setAutoSelectedSkills] = useState([]);
  const [autoSelectingSkills, setAutoSelectingSkills] = useState(false);
  const [autoSelectionError, setAutoSelectionError] = useState("");
  const [selfEvolutionEnabled, setSelfEvolutionEnabled] = useState(null);
  const [latestScreenshot, setLatestScreenshot] = useState(null);
  const [annotating, setAnnotating] = useState(false);
  const [turnCount, setTurnCount] = useState(0);
  const scrollRef = useRef(null);
  const abortRef = useRef(null);
  const textareaRef = useRef(null);
  const demoReplayQueueRef = useRef(Promise.resolve());
  const demoAssistantIdsRef = useRef(new Map());
  const selfEvolutionReqSeqRef = useRef(0);
  const activeSkills = autoSkillSelectionEnabled ? autoSelectedSkills : selectedSkills;
  const selfEvolutionReady = typeof selfEvolutionEnabled === "boolean";
  const selfEvolutionOn = selfEvolutionEnabled === true;

  const sleep = useCallback((ms) => new Promise((resolve) => setTimeout(resolve, ms)), []);

  const enqueueDemoReplay = useCallback((task) => {
    demoReplayQueueRef.current = demoReplayQueueRef.current.then(task).catch(() => {});
    return demoReplayQueueRef.current;
  }, []);

  const beginDemoPromptReplay = useCallback(
    (text, key) =>
      enqueueDemoReplay(async () => {
        const src = String(text || "");
        setInput("");
        let cur = "";
        for (const ch of src) {
          cur += ch;
          setInput(cur);
          await sleep(16);
        }
        await sleep(120);

        const userMsg = {
          id: generateMessageId(),
          role: "user",
          content: src,
          timestamp: Date.now(),
        };
        const assistantId = generateMessageId();
        const assistantMsg = {
          id: assistantId,
          role: "assistant",
          content: "",
          toolCalls: [],
          timestamp: Date.now(),
        };

        if (key) demoAssistantIdsRef.current.set(String(key), assistantId);
        setMessages((prev) => [...prev, userMsg, assistantMsg]);
        setInput("");
      }),
    [enqueueDemoReplay, sleep]
  );

  const completeDemoAssistantReplay = useCallback(
    (text, key) =>
      enqueueDemoReplay(async () => {
        const content = String(text || "").trim();
        if (!content) return;
        const k = String(key || "");
        const assistantId = k ? demoAssistantIdsRef.current.get(k) : null;
        if (!assistantId) {
          setMessages((prev) => [
            ...prev,
            {
              id: generateMessageId(),
              role: "assistant",
              content,
              toolCalls: [],
              timestamp: Date.now(),
            },
          ]);
          return;
        }
        setMessages((prev) =>
          prev.map((m) => (m.id === assistantId ? { ...m, content, timestamp: Date.now() } : m))
        );
      }),
    [enqueueDemoReplay]
  );

  const resetDemoReplayState = useCallback(() => {
    demoAssistantIdsRef.current.clear();
    demoReplayQueueRef.current = Promise.resolve();
    setInput("");
  }, []);

  // Expose methods to parent
  useEffect(() => {
    onRef?.({
      insertText: (text) =>
        setInput((prev) => (prev ? prev + "\n" + text : text)),
      beginDemoPromptReplay: (text, key) => beginDemoPromptReplay(text, key),
      completeDemoAssistantReplay: (text, key) => completeDemoAssistantReplay(text, key),
      resetDemoReplayState: () => resetDemoReplayState(),
      loadScene: (scene) => {
        if (scene.sessionId) {
          setSessionId(scene.sessionId);
          setMessages(
            scene.chatHistory || [
              {
                id: generateMessageId(),
                role: "assistant",
                content: `Loaded scene: **${scene.name}**\nContinuing from previous session.`,
                timestamp: Date.now(),
              },
            ]
          );
        }
      },
    });
  }, [onRef, beginDemoPromptReplay, completeDemoAssistantReplay, resetDemoReplayState]);

  useEffect(() => {
    function onRunStarted() {
      resetDemoReplayState();
    }

    function onRoundStarted(e) {
      const d = (e && e.detail) || {};
      beginDemoPromptReplay(d.prompt || "", d.key || "");
    }

    function onRoundCompleted(e) {
      const d = (e && e.detail) || {};
      completeDemoAssistantReplay(d.assistantText || "", d.key || "");
    }

    window.addEventListener("simworld_demo_run_started", onRunStarted);
    window.addEventListener("simworld_demo_round_started", onRoundStarted);
    window.addEventListener("simworld_demo_round_completed", onRoundCompleted);
    return () => {
      window.removeEventListener("simworld_demo_run_started", onRunStarted);
      window.removeEventListener("simworld_demo_round_started", onRoundStarted);
      window.removeEventListener("simworld_demo_round_completed", onRoundCompleted);
    };
  }, [beginDemoPromptReplay, completeDemoAssistantReplay, resetDemoReplayState]);

  useEffect(() => {
    let mounted = true;
    fetchEvolutionConfig()
      .then((cfg) => {
        if (!mounted) return;
        if (cfg && typeof cfg.enabled === "boolean") {
          setSelfEvolutionEnabled(cfg.enabled);
          return;
        }
        setSelfEvolutionEnabled(true);
      })
      .catch(() => {
        if (!mounted) return;
        setSelfEvolutionEnabled(true);
      });
    return () => {
      mounted = false;
    };
  }, []);

  // Auto-scroll
  useEffect(() => {
    scrollRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // Auto-resize textarea
  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
      textareaRef.current.style.height =
        Math.min(textareaRef.current.scrollHeight, 160) + "px";
    }
  }, [input]);

  const handleSend = useCallback(
    async (overrideMessage, feedbackText) => {
      const text = (overrideMessage || input).trim();
      if (!text || loading) return;
      if (!overrideMessage) setInput("");
      if (autoSkillSelectionEnabled) {
        setAutoSelectionError("");
      }

      const userMsg = {
        id: generateMessageId(),
        role: "user",
        content: text,
        timestamp: Date.now(),
      };
      const assistantId = generateMessageId();
      const assistantMsg = {
        id: assistantId,
        role: "assistant",
        content: "",
        waiting: true,  // Show "Waiting for Claude..." until first event
        toolCalls: [],
        timestamp: Date.now(),
      };

      setMessages((prev) => [...prev, userMsg, assistantMsg]);
      setLoading(true);
      setTurnCount((c) => c + 1);

      const controller = new AbortController();
      abortRef.current = controller;
      const inputBuffers = new Map();

      // Safety: force-reset loading after 6 minutes no matter what
      const safetyTimer = setTimeout(() => {
        if (abortRef.current === controller) {
          controller.abort();
          setLoading(false);
          setMessages((prev) => {
            const updated = [...prev];
            const idx = updated.findIndex((m) => m.id === assistantId);
            if (idx !== -1 && !updated[idx].content) {
              updated[idx] = { ...updated[idx], content: "Request timed out after 6 minutes. Please try again." };
            }
            return updated;
          });
        }
      }, 360000);

      try {
        await sendChat(
          text,
          sessionId,
          (event) => {
            setMessages((prev) => {
              const updated = [...prev];
              const idx = updated.findIndex((m) => m.id === assistantId);
              if (idx === -1) return prev;
              const msg = { ...updated[idx] };
              // Clear waiting flag on first real event
              if (msg.waiting && (event.type === "text" || event.type === "tool_start" || event.type === "system")) {
                msg.waiting = false;
              }

              switch (event.type) {
                case "skill_selection_start": {
                  setAutoSelectionError("");
                  setAutoSelectingSkills(true);
                  break;
                }
                case "skill_selection_error": {
                  setAutoSelectingSkills(false);
                  setAutoSelectionError(event.data?.message || "Auto skill selection failed");
                  break;
                }
                case "skill_selection_done": {
                  setAutoSelectingSkills(false);
                  const mode = event.data?.mode;
                  const selected = Array.isArray(event.data?.selectedSkills)
                    ? event.data.selectedSkills
                    : [];
                  if (mode === "auto") {
                    setAutoSelectionError("");
                    setAutoSelectedSkills(selected);
                    setSelectedSkills(selected);
                  } else if (mode === "manual") {
                    setAutoSelectedSkills([]);
                  }
                  break;
                }
                case "system": {
                  const connected = event.data.mcpServers
                    .filter((s) => s.status === "connected")
                    .map((s) => s.name);
                  setMcpStatus(connected.length ? `✓ ${connected.join(", ")}` : "✗ none");
                  console.log("[CTX-DEBUG] system event, sessionId:", event.data.sessionId);
                  if (event.data.sessionId) {
                    setSessionId(event.data.sessionId);
                    onSessionChange?.(event.data.sessionId);
                  }
                  break;
                }
                case "text": {
                  const blocks = msg.blocks || [];
                  const lastBlock = blocks[blocks.length - 1];
                  if (lastBlock && lastBlock.type === "text") {
                    msg.blocks = [
                      ...blocks.slice(0, -1),
                      { ...lastBlock, content: lastBlock.content + event.data.delta },
                    ];
                  } else {
                    msg.blocks = [
                      ...blocks,
                      { type: "text", content: event.data.delta },
                    ];
                  }
                  msg.content = (msg.content || "") + event.data.delta;
                  break;
                }
                case "tool_start": {
                  const toolCall = {
                    id: event.data.id,
                    name: event.data.name,
                    displayName: event.data.displayName,
                    status: "running",
                    inputBuffer: "",
                  };
                  inputBuffers.set(toolCall.id, "");
                  msg.toolCalls = [...(msg.toolCalls || []), toolCall];
                  msg.blocks = [
                    ...(msg.blocks || []),
                    { type: "tool", toolId: toolCall.id },
                  ];
                  break;
                }
                case "tool_input": {
                  const calls = msg.toolCalls || [];
                  const lastCall = calls[calls.length - 1];
                  if (lastCall) {
                    const buf = (inputBuffers.get(lastCall.id) || "") + event.data.delta;
                    inputBuffers.set(lastCall.id, buf);
                    msg.toolCalls = (msg.toolCalls || []).map((tc) =>
                      tc.id === lastCall.id ? { ...tc, inputBuffer: buf } : tc
                    );
                  }
                  break;
                }
                case "tool_details": {
                  const toolId = event.data.id;
                  msg.toolCalls = (msg.toolCalls || []).map((tc) =>
                    tc.id === toolId
                      ? { ...tc, input: event.data.input, displayName: event.data.displayName }
                      : tc
                  );
                  break;
                }
                case "tool_result": {
                  const toolUseId = event.data.toolUseId;
                  const isError = event.data.isError;
                  msg.toolCalls = (msg.toolCalls || []).map((tc) =>
                    tc.id === toolUseId
                      ? {
                          ...tc,
                          result: event.data.result,
                          isError,
                          status: isError ? "error" : "done",
                        }
                      : tc
                  );
                  break;
                }
                case "screenshot": {
                  const filepath = event.data.filepath;
                  onScreenshotUpdate(filepath);
                  setLatestScreenshot(filepath);
                  const toolUseId = event.data.toolUseId;
                  if (toolUseId) {
                    msg.toolCalls = (msg.toolCalls || []).map((tc) =>
                      tc.id === toolUseId ? { ...tc, screenshot: filepath } : tc
                    );
                  }
                  break;
                }
                case "done": {
                  const sid = event.data.sessionId;
                  const isErr = event.data.isError;
                  // Always keep sessionId — it's the stable studio session, not Claude's transient one
                  if (sid) {
                    setSessionId(sid);
                    onSessionChange?.(sid);
                  }
                  const screenshot = event.data.latestScreenshot;
                  if (screenshot) {
                    onScreenshotUpdate(screenshot);
                    setLatestScreenshot(screenshot);
                  }
                  // If no content was streamed at all, show fallback but keep session
                  if (!msg.content && (!msg.toolCalls || msg.toolCalls.length === 0)) {
                    msg.content = isErr
                      ? "⚠️ Agent exited unexpectedly. Try again — each message starts a fresh process."
                      : "⚠️ No response received. Try sending your message again.";
                  }
                  onChatDone?.();
                  break;
                }
              }

              updated[idx] = msg;
              return updated;
            });
          },
          controller.signal,
          {
            skills: selectedSkills.length > 0 ? selectedSkills : undefined,
            feedback: feedbackText,
            skillSelectionMode: autoSkillSelectionEnabled ? "auto" : "manual",
          }
        );
      } catch (err) {
        if (err instanceof Error && err.name !== "AbortError") {
          setMessages((prev) => {
            const updated = [...prev];
            const idx = updated.findIndex((m) => m.id === assistantId);
            if (idx !== -1) {
              updated[idx] = {
                ...updated[idx],
                content: updated[idx].content || `Error: ${err.message}`,
              };
            }
            return updated;
          });
        }
      } finally {
        clearTimeout(safetyTimer);
        setLoading(false);
        setAutoSelectingSkills(false);
        abortRef.current = null;
      }
    },
    [input, loading, sessionId, selectedSkills, autoSkillSelectionEnabled, onScreenshotUpdate]
  );

  const handleStop = () => {
    abortRef.current?.abort();
    setLoading(false);
  };

  const handleKeyDown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleReset = () => {
    abortRef.current?.abort();
    setInput("");
    setLoading(false);
    setSessionId(null);
    setMcpStatus("–");
    setSelectedSkills([]);
    setAutoSelectedSkills([]);
    setAutoSelectingSkills(false);
    setAutoSelectionError("");
    setTurnCount(0);
    setLatestScreenshot(null);
    abortRef.current = null;
    setMessages([
      {
        id: generateMessageId(),
        role: "assistant",
        content: "Session reset. Starting a fresh conversation.",
        timestamp: Date.now(),
      },
    ]);
  };

  const handleSelfEvolutionToggle = async () => {
    if (!selfEvolutionReady) return;
    const prevEnabled = selfEvolutionEnabled;
    const nextEnabled = !prevEnabled;
    const reqSeq = ++selfEvolutionReqSeqRef.current;
    setSelfEvolutionEnabled(nextEnabled);
    try {
      const out = await updateEvolutionConfig(nextEnabled);
      if (reqSeq !== selfEvolutionReqSeqRef.current) return;
      if (out && typeof out.enabled === "boolean" && out.enabled !== nextEnabled) {
        setSelfEvolutionEnabled(out.enabled);
      }
    } catch {
      if (reqSeq !== selfEvolutionReqSeqRef.current) return;
      setSelfEvolutionEnabled(prevEnabled);
    }
  };

  const handleSave = async () => {
    const firstUser = messages.find((m) => m.role === "user");
    const prompt = firstUser?.content || "";
    const name = window.prompt("Scene name:", prompt.slice(0, 50) || "My Scene");
    if (name) {
      await saveScene({
        name,
        prompt,
        description: `${turnCount} turns, ${messages.length} messages`,
        sessionId: sessionId || undefined,
        skills: activeSkills,
        chatHistory: messages,
      });
      alert("Scene saved!");
    }
  };

  const handleShare = async () => {
    const firstUser = messages.find((m) => m.role === "user");
    const prompt = firstUser?.content || "Untitled scene";
    try {
      await shareToGallery({
        prompt,
        agentName: "claude-code",
        screenshots: latestScreenshot ? [latestScreenshot] : [],
        tags: activeSkills.length > 0 ? activeSkills : ["user-generated"],
        skills: activeSkills,
      });
      alert("Shared to Gallery!");
    } catch {
      alert("Failed to share to gallery");
    }
  };

  const handleAnnotationSubmit = (feedbackMsg, _points) => {
    setAnnotating(false);
    if (feedbackMsg) handleSend(feedbackMsg, feedbackMsg);
  };

  const lastMessage = messages[messages.length - 1];

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        height: "100%",
        background: "#0d1117",
        position: "relative",
      }}
    >
      {/* Annotation overlay */}
      {annotating && latestScreenshot && (
        <AnnotateOverlay
          src={latestScreenshot}
          onSubmitFeedback={handleAnnotationSubmit}
          onCancel={() => setAnnotating(false)}
        />
      )}

      {/* Header */}
      <div
        style={{
          padding: "10px 14px",
          borderBottom: "1px solid #21262d",
          display: "flex",
          alignItems: "center",
          gap: 10,
          flexShrink: 0,
        }}
      >
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontWeight: 600, fontSize: 13, color: "#e6edf3" }}>Scene Agent</div>
          <div
            style={{
              fontSize: 10,
              color: "#8b949e",
              display: "flex",
              gap: 8,
              alignItems: "center",
            }}
          >
            <span>Claude Code CLI</span>
            <span>·</span>
            <span style={{ color: mcpStatus.startsWith("✓") ? "#3fb950" : "#f85149" }}>
              MCP: {mcpStatus}
            </span>
            <span>·</span>
            <span style={{ color: selfEvolutionOn ? "#f0883e" : "#8b949e" }}>
              Self-evolution: {selfEvolutionReady ? (selfEvolutionOn ? "on" : "off") : "syncing"}
            </span>
            {sessionId && (
              <>
                <span>·</span>
                <span style={{ color: "#8b949e" }}>session: {sessionId.slice(0, 8)}</span>
                <span>·</span>
                <span style={{ color: "#656d76" }}>turn {turnCount}</span>
              </>
            )}
          </div>
        </div>
        <div style={{ display: "flex", gap: 4 }}>
          <button
            onClick={handleSelfEvolutionToggle}
            title="Enable or disable self-evolution ingestion"
            style={{
              height: 24,
              padding: "0 10px",
              borderRadius: 7,
              border: selfEvolutionOn ? "1px solid #f0883e88" : "1px solid #30363d",
              background: selfEvolutionOn
                ? "linear-gradient(135deg, #2a1b0f 0%, #3b2414 100%)"
                : "#161b22",
              color: selfEvolutionOn ? "#ffd7a1" : "#8b949e",
              cursor: selfEvolutionReady ? "pointer" : "not-allowed",
              display: "flex",
              alignItems: "center",
              gap: 7,
              fontSize: 10,
              fontWeight: selfEvolutionOn ? 600 : 500,
              opacity: selfEvolutionReady ? 1 : 0.7,
              boxShadow: selfEvolutionOn ? "0 0 12px rgba(240,136,62,0.35)" : "none",
              transition: "all 0.18s ease",
            }}
          >
            <span
              style={{
                width: 8,
                height: 8,
                borderRadius: "50%",
                background: selfEvolutionOn ? "#f0883e" : "#656d76",
                boxShadow: selfEvolutionOn ? "0 0 8px #f0883e" : "none",
                animation: selfEvolutionOn ? "selfEvoPulse 1.3s ease-in-out infinite" : "none",
                flexShrink: 0,
              }}
            />
            <span>Self-Evolution</span>
            <span
              style={{
                color: selfEvolutionOn ? "#f0c674" : "#656d76",
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
            <button
              onClick={() => setAnnotating(true)}
              style={headerButtonStyle("#f0883e")}
              title="Annotate screenshot to give feedback"
            >
              Annotate
            </button>
          )}
          {!loading && sessionId && (
            <button
              onClick={handleSave}
              style={headerButtonStyle("#3fb950")}
              title="Save current scene"
            >
              Save
            </button>
          )}
          {!loading && sessionId && latestScreenshot && (
            <button
              onClick={handleShare}
              style={headerButtonStyle("#1f6feb")}
              title="Share to community gallery"
            >
              Share
            </button>
          )}
          {loading && (
            <button onClick={handleStop} style={headerButtonStyle("#f85149")}>
              Stop
            </button>
          )}
          {!loading && sessionId && (
            <button
              onClick={handleReset}
              title="Reset conversation"
              style={headerButtonStyle("#8b949e")}
            >
              Reset
            </button>
          )}
        </div>
      </div>

      {/* Skills panel */}
      <SkillsPanel
        selected={selectedSkills}
        onChange={setSelectedSkills}
        autoEnabled={autoSkillSelectionEnabled}
        onAutoEnabledChange={(enabled) => {
          setAutoSkillSelectionEnabled(Boolean(enabled));
          setAutoSelectionError("");
          if (!enabled) setAutoSelectingSkills(false);
        }}
        autoSelected={autoSelectedSkills}
      />

      {/* Messages */}
      <div
        style={{
          flex: 1,
          overflowY: "auto",
          padding: "14px",
          display: "flex",
          flexDirection: "column",
          gap: 14,
        }}
      >
        {messages.map((msg) => (
          <ChatMessage key={msg.id} message={msg} />
        ))}
        {loading && lastMessage?.role === "user" && <TypingIndicator />}
        <div ref={scrollRef} />
      </div>

      {/* Quick suggestions */}
      {!loading && sessionId && turnCount > 0 && (
        <div
          style={{
            padding: "6px 14px",
            borderTop: "1px solid #21262d",
            background: "#0d1117",
            display: "flex",
            gap: 6,
            flexWrap: "wrap",
          }}
        >
          {QUICK_SUGGESTIONS.map((suggestion) => (
            <button
              key={suggestion}
              onClick={() => handleSend(suggestion)}
              style={{
                padding: "3px 10px",
                fontSize: 10,
                borderRadius: 12,
                border: "1px solid #21262d",
                background: "#161b22",
                color: "#8b949e",
                cursor: "pointer",
              }}
            >
              {suggestion}
            </button>
          ))}
        </div>
      )}

      {/* Input area */}
      <div
        style={{
          padding: "10px 14px",
          borderTop: "1px solid #21262d",
          flexShrink: 0,
          background: "#161b22",
        }}
      >
        <div
          style={{
            display: "flex",
            gap: 8,
            alignItems: "flex-end",
            background: "#0d1117",
            border: "1px solid #30363d",
            borderRadius: 8,
            padding: "8px 12px",
          }}
        >
          <textarea
            ref={textareaRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={
              sessionId
                ? "Refine the scene or describe changes..."
                : "Describe the city scene you want to generate…"
            }
            disabled={loading}
            rows={1}
            style={{
              flex: 1,
              background: "none",
              border: "none",
              outline: "none",
              color: "#e6edf3",
              fontSize: 14,
              resize: "none",
              lineHeight: 1.5,
              maxHeight: 160,
              overflow: "auto",
              fontFamily: "inherit",
            }}
          />
          <button
            onClick={() => (loading ? handleStop() : handleSend())}
            disabled={!loading && !input.trim()}
            style={{
              flexShrink: 0,
              width: 32,
              height: 32,
              borderRadius: 6,
              border: "none",
              background: loading ? "#21262d" : input.trim() ? "#1f6feb" : "#21262d",
              color: loading ? "#f85149" : "#e6edf3",
              cursor: loading || input.trim() ? "pointer" : "default",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontSize: 16,
            }}
          >
            {loading ? "⏹" : "▶"}
          </button>
        </div>
        <div style={{ marginTop: 5, fontSize: 11, color: "#656d76" }}>
          Enter to send · Shift+Enter for new line
          <span style={{ marginLeft: 8, color: autoSkillSelectionEnabled ? "#58a6ff" : "#8b949e" }}>
            {autoSkillSelectionEnabled ? "Auto-select skills: on" : "Auto-select skills: off"}
            {autoSelectingSkills ? " (selecting...)" : ""}
          </span>          {activeSkills.length > 0 && (
            <span style={{ color: "#58a6ff", marginLeft: 8 }}>
              {activeSkills.length} skill{activeSkills.length > 1 ? "s" : ""} active
            </span>
          )}
          {autoSelectionError && (
            <span style={{ color: "#f85149", marginLeft: 8 }}>{autoSelectionError}</span>
          )}
        </div>
      </div>
    </div>
  );
}

// ─── KeyHint ─────────────────────────────────────────────────────────────────

function KeyHint({ icon, label }) {
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        gap: 4,
      }}
    >
      <div
        style={{
          background: "#21262d",
          border: "1px solid #30363d",
          borderRadius: 4,
          padding: "2px 8px",
          fontSize: 12,
          color: "#e6edf3",
          fontFamily: "monospace",
          minWidth: 32,
          textAlign: "center",
        }}
      >
        {icon}
      </div>
      <div style={{ color: "#656d76", fontSize: 10 }}>{label}</div>
    </div>
  );
}

// ─── PixelStreamView ─────────────────────────────────────────────────────────

function PixelStreamView({ playerUrl }) {
  const iframeRef = useRef(null);
  const [active, setActive] = useState(false);
  const [showHints, setShowHints] = useState(false);

  const activate = useCallback(() => {
    setActive(true);
    setTimeout(() => iframeRef.current?.focus(), 100);
  }, []);

  useEffect(() => {
    if (!active) return;
    setShowHints(true);
    const timer = setTimeout(() => setShowHints(false), 4000);
    return () => clearTimeout(timer);
  }, [active]);

  return (
    <div style={{ width: "100%", height: "100%", position: "relative", background: "#000" }}>
      <iframe
        ref={iframeRef}
        src={playerUrl}
        style={{
          width: "100%",
          height: "100%",
          border: "none",
          display: "block",
          pointerEvents: active ? "auto" : "none",
        }}
        allow="pointer-lock *; fullscreen *; autoplay *; clipboard-read *; clipboard-write *"
        allowFullScreen
        tabIndex={0}
        title="UE Pixel Streaming"
      />

      {/* Activation overlay */}
      {!active && (
        <div
          onClick={activate}
          style={{
            position: "absolute",
            inset: 0,
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            background: "rgba(0,0,0,0.72)",
            cursor: "pointer",
            gap: 16,
          }}
        >
          <div style={{ fontSize: 48 }}>🎮</div>
          <div style={{ color: "#e6edf3", fontSize: 18, fontWeight: 600 }}>
            Click to Activate Stream
          </div>
          <div
            style={{
              color: "#8b949e",
              fontSize: 13,
              textAlign: "center",
              maxWidth: 320,
              lineHeight: 1.6,
            }}
          >
            Enables keyboard & mouse control.
            <br />
            The stream must load before interaction works.
          </div>
          <div
            style={{
              display: "flex",
              gap: 24,
              marginTop: 8,
              color: "#656d76",
              fontSize: 12,
            }}
          >
            <KeyHint icon="🖱️" label="Click & drag to look" />
            <KeyHint icon="⌨️" label="WASD to move" />
            <KeyHint icon="Esc" label="Release mouse" />
          </div>
        </div>
      )}

      {/* Control hints (fade after activation) */}
      {active && showHints && (
        <div
          style={{
            position: "absolute",
            bottom: 12,
            left: "50%",
            transform: "translateX(-50%)",
            background: "rgba(0,0,0,0.75)",
            borderRadius: 8,
            padding: "10px 20px",
            display: "flex",
            gap: 20,
            pointerEvents: "none",
            border: "1px solid #30363d",
          }}
        >
          <KeyHint icon="🖱️" label="Click & drag to look" />
          <KeyHint icon="WASD" label="Move" />
          <KeyHint icon="Esc" label="Release mouse" />
          <KeyHint icon="F" label="Fullscreen" />
        </div>
      )}

      {/* Live badge */}
      {active && (
        <div
          style={{
            position: "absolute",
            top: 8,
            left: 8,
            background: "rgba(0,0,0,0.6)",
            color: "#3fb950",
            fontSize: 11,
            padding: "3px 8px",
            borderRadius: 4,
            pointerEvents: "none",
            display: "flex",
            alignItems: "center",
            gap: 5,
          }}
        >
          <span
            style={{
              display: "inline-block",
              width: 6,
              height: 6,
              borderRadius: "50%",
              background: "#3fb950",
              animation: "livebeat 2s ease-in-out infinite",
            }}
          />
          LIVE · SimWorld
          <style>
            {"@keyframes livebeat { 0%,100%{opacity:1} 50%{opacity:0.4} }"}
          </style>
        </div>
      )}

      {/* Deactivate button */}
      {active && (
        <button
          onClick={() => {
            setActive(false);
            setShowHints(false);
          }}
          style={{
            position: "absolute",
            top: 8,
            right: 8,
            padding: "3px 10px",
            fontSize: 11,
            background: "rgba(0,0,0,0.6)",
            border: "1px solid #30363d",
            borderRadius: 4,
            color: "#8b949e",
            cursor: "pointer",
          }}
        >
          ✕ Deactivate
        </button>
      )}
    </div>
  );
}

// ─── ScreenshotView ──────────────────────────────────────────────────────────

function ScreenshotView({ src, imgKey, onRefresh }) {
  const [loaded, setLoaded] = useState(false);
  const [errored, setErrored] = useState(false);

  // Reset load state when src or key changes
  useEffect(() => { setLoaded(false); setErrored(false); }, [src, imgKey]);

  if (src) {
    return (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          background: "#000",
          overflow: "hidden",
        }}
      >
        <img
          key={imgKey}
          src={src}
          alt="UE viewport"
          style={{ maxWidth: "100%", maxHeight: "100%", objectFit: "contain", display: loaded ? "block" : "none" }}
          onLoad={() => setLoaded(true)}
          onError={() => { setErrored(true); setTimeout(() => onRefresh?.(), 1000); }}
        />
        {!loaded && !errored && (
          <span style={{ color: "#656d76", fontSize: 13 }}>Loading screenshot...</span>
        )}
        {errored && (
          <span style={{ color: "#656d76", fontSize: 13 }}>Retrying screenshot...</span>
        )}
      </div>
    );
  }

  return (
    <div
      style={{
        width: "100%",
        height: "100%",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: 14,
        color: "#656d76",
      }}
    >
      <div style={{ fontSize: 44 }}>📸</div>
      <div style={{ fontSize: 13 }}>No screenshot yet</div>
      <div
        style={{
          fontSize: 11,
          color: "#30363d",
          textAlign: "center",
          maxWidth: 280,
        }}
      >
        Ask the agent to take a screenshot or make changes to the scene.
        <br />
        Screenshots auto-appear after agent actions.
      </div>
      <button
        onClick={onRefresh}
        style={{
          padding: "6px 18px",
          fontSize: 13,
          background: "#21262d",
          border: "1px solid #30363d",
          borderRadius: 6,
          color: "#e6edf3",
          cursor: "pointer",
        }}
      >
        ↻ Fetch Latest
      </button>
    </div>
  );
}

// ─── ContextPanel ────────────────────────────────────────────────────────────

function EntityRow({ entity }) {
  const icon = CATEGORY_ICONS[entity.category] || "📦";
  const loc = Array.isArray(entity.location) && entity.location.length >= 3
    ? entity.location.map((v) => Math.round(v)).join(", ")
    : null;
  return (
    <div style={{ display: "flex", alignItems: "flex-start", gap: 8, padding: "5px 0", borderBottom: "1px solid #21262d" }}>
      <span style={{ fontSize: 14, flexShrink: 0, marginTop: 1 }}>{icon}</span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ color: "#e6edf3", fontSize: 12, fontWeight: 500 }}>{entity.name}</span>
          {entity.cls && (
            <span style={{ fontSize: 10, color: "#58a6ff", background: "#1f3a5f", borderRadius: 3, padding: "1px 5px" }}>
              {entity.cls}
            </span>
          )}
        </div>
        {loc && <div style={{ fontSize: 10, color: "#656d76", marginTop: 2 }}>@ ({loc})</div>}
      </div>
    </div>
  );
}

function ContextPanel({ sessionId, refreshKey }) {
  const [state, setState] = useState(null);
  const [lastUpdated, setLastUpdated] = useState(null);
  const sessionRef = useRef(sessionId);
  sessionRef.current = sessionId;

  // Poll context — works with or without sessionId (backend falls back to latest)
  useEffect(() => {
    let stopped = false;
    const poll = async () => {
      const sid = sessionRef.current;
      try {
        const url = sid
          ? `${API_BASE}/context?sessionId=${encodeURIComponent(sid)}`
          : `${API_BASE}/context`;
        const res = await fetch(url);
        if (!res.ok || stopped) return;
        const data = await res.json();
        if (!stopped && data.updatedAt) { setState(data); setLastUpdated(new Date()); }
      } catch (err) { console.error("[CTX-DEBUG] poll error:", err); }
    };
    poll();
    const id = setInterval(poll, 3000);
    return () => { stopped = true; clearInterval(id); };
  }, [sessionId]);

  // When refreshKey bumps (chat turn finished), do one immediate fetch
  useEffect(() => {
    if (!sessionId || refreshKey === 0) return;
    const fetchOnce = async () => {
      try {
        const res = await fetch(`${API_BASE}/context?sessionId=${encodeURIComponent(sessionId)}`);
        if (!res.ok) return;
        const data = await res.json();
        setState(data);
        setLastUpdated(new Date());
      } catch {}
    };
    fetchOnce();
  }, [refreshKey, sessionId]);

  const containerStyle = {
    height: "100%", display: "flex", flexDirection: "column",
    background: "#0d1117", color: "#e6edf3", overflow: "hidden",
  };
  const headerStyle = {
    padding: "10px 14px", borderBottom: "1px solid #21262d",
    display: "flex", alignItems: "center", justifyContent: "space-between", flexShrink: 0,
  };
  const sectionStyle = { padding: "10px 14px 0" };
  const sectionTitleStyle = {
    fontSize: 11, fontWeight: 600, color: "#8b949e",
    textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 6,
  };

  if (!state || !state.updatedAt) {
    return (
      <div style={containerStyle}>
        <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center" }}>
          <span style={{ color: "#656d76", fontSize: 13 }}>
            {sessionId ? "No scene data yet — complete a round to populate." : "Start a chat session to see scene context."}
          </span>
        </div>
      </div>
    );
  }

  const byCategory = {};
  for (const o of state.objects || []) {
    (byCategory[o.category] = byCategory[o.category] || []).push(o);
  }

  return (
    <div style={containerStyle}>
      {/* Header */}
      <div style={headerStyle}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ fontSize: 13, fontWeight: 600 }}>Scene Context</span>
          <span style={{ fontSize: 10, background: state.environment?.ready ? "#1a3a2a" : "#3a2a1a",
            color: state.environment?.ready ? "#3fb950" : "#d29922",
            borderRadius: 3, padding: "1px 6px" }}>
            {state.environment?.ready ? "env ready" : "env not initialized"}
          </span>
          <span style={{ fontSize: 10, color: "#8b949e" }}>round {state.round ?? 0}</span>
        </div>
        {lastUpdated && (
          <span style={{ fontSize: 10, color: "#656d76" }}>
            updated {lastUpdated.toLocaleTimeString()}
          </span>
        )}
      </div>

      <div style={{ flex: 1, overflowY: "auto", padding: "0 0 16px" }}>
        {/* Agents section */}
        <div style={sectionStyle}>
          <div style={sectionTitleStyle}>
            🤖 Agents &nbsp;<span style={{ color: "#58a6ff" }}>{(state.agents || []).length}</span>
          </div>
          {(state.agents || []).length === 0
            ? <div style={{ fontSize: 12, color: "#656d76", paddingBottom: 8 }}>No agents in scene</div>
            : (state.agents || []).map((a) => <EntityRow key={a.name} entity={a} />)
          }
        </div>

        {/* Objects section, grouped by category */}
        <div style={{ ...sectionStyle, marginTop: 12 }}>
          <div style={sectionTitleStyle}>
            📦 Objects &nbsp;<span style={{ color: "#58a6ff" }}>{(state.objects || []).length}</span>
          </div>
          {(state.objects || []).length === 0
            ? <div style={{ fontSize: 12, color: "#656d76" }}>No objects in scene</div>
            : Object.entries(byCategory).map(([cat, items]) => (
                <div key={cat} style={{ marginBottom: 10 }}>
                  <div style={{ fontSize: 11, color: "#8b949e", marginBottom: 4 }}>
                    {CATEGORY_ICONS[cat] || "📦"} {cat}s ({items.length})
                  </div>
                  {items.map((o) => <EntityRow key={o.name} entity={o} />)}
                </div>
              ))
          }
        </div>
      </div>
    </div>
  );
}

// ─── AgentPanel ──────────────────────────────────────────────────────────────

const AGENT_COLORS = ["#58a6ff", "#3fb950", "#d29922", "#f778ba", "#bc8cff", "#f0883e", "#79c0ff", "#56d364"];

async function sendAgentChat(agentName, message, sessionId, onEvent, signal) {
  const response = await fetch(`${API_BASE}/agent-chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ agentName, message, sessionId }),
    signal,
  });
  if (!response.ok || !response.body) {
    const err = await response.json().catch(() => ({ error: response.statusText }));
    throw new Error(err.error || `Server error: ${response.status}`);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() ?? "";
    for (const chunk of chunks) {
      const lines = chunk.split("\n");
      let eventType = "message";
      let data = "";
      for (const line of lines) {
        if (line.startsWith("event: ")) eventType = line.slice(7).trim();
        if (line.startsWith("data: ")) data = line.slice(6);
      }
      if (data) {
        try { onEvent({ type: eventType, data: JSON.parse(data) }); } catch {}
      }
    }
  }
}

function AgentCard({ agent, sessionId, pieActive, colorIdx }) {
  const [status, setStatus] = useState("idle");
  const [thought, setThought] = useState(""); // Current reasoning text
  const [actions, setActions] = useState([]); // [{tool, ok}]
  const [input, setInput] = useState("");
  const [pastActivities, setPastActivities] = useState([]); // Previous turns
  const abortRef = useRef(null);
  const activityRef = useRef(null);
  const color = AGENT_COLORS[colorIdx % AGENT_COLORS.length];

  // Poll last activity from server (catches completed turns)
  useEffect(() => {
    const poll = async () => {
      try {
        const r = await fetch(`${API_BASE}/agent-activity/${agent.name}`);
        if (r.ok) setPastActivities(await r.json());
      } catch {}
    };
    poll();
    const id = setInterval(poll, 5000);
    return () => clearInterval(id);
  }, [agent.name]);

  const handleSend = useCallback(async (text) => {
    if (!text.trim() || status === "running" || !pieActive) return;
    setInput("");
    setStatus("running");
    setThought("");
    setActions([]);

    const controller = new AbortController();
    abortRef.current = controller;
    try {
      await sendAgentChat(agent.name, text, sessionId, (event) => {
        switch (event.type) {
          case "text":
            setThought(prev => prev + event.data.delta);
            break;
          case "thinking":
            setThought(prev => prev + event.data.delta);
            break;
          case "tool_start":
            setActions(prev => [...prev, { tool: event.data.displayName, ok: null }]);
            break;
          case "tool_result":
            setActions(prev => prev.map((a, i) => i === prev.length - 1 ? { ...a, ok: !event.data.isError } : a));
            break;
          case "done":
            break;
        }
      }, controller.signal);
      setStatus("done");
    } catch (err) {
      if (err.name !== "AbortError") {
        setThought(prev => prev || `Error: ${err.message}`);
        setStatus("error");
      } else {
        setStatus("idle");
      }
    } finally { abortRef.current = null; }
  }, [agent.name, sessionId, status, pieActive]);

  const handleStop = () => {
    abortRef.current?.abort();
    fetch(`${API_BASE}/agent-stop`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ agentName: agent.name }) }).catch(() => {});
    setStatus("idle");
  };

  useEffect(() => { if (activityRef.current) activityRef.current.scrollTop = activityRef.current.scrollHeight; }, [thought, actions]);

  const loc = Array.isArray(agent.location) && agent.location.length >= 3 ? agent.location.map(v => Math.round(v)).join(", ") : null;
  const statusColors = { idle: "#8b949e", running: "#d29922", done: "#3fb950", error: "#f85149" };

  // Render a single activity (ReAct format)
  const renderActivity = (act, isLive) => {
    const t = act.thought || act.response || "";
    const acts = isLive ? actions : (act.actions || []);
    return (
      <div style={{ fontSize: 11, lineHeight: "1.5" }}>
        {/* Thought */}
        {t && (
          <div style={{ color: "#c9d1d9", whiteSpace: "pre-wrap", marginBottom: 4 }}>
            <span style={{ color: "#8b949e", fontWeight: 600 }}>Thought: </span>{t.slice(0, 500)}
          </div>
        )}
        {/* Actions */}
        {acts.length > 0 && acts.map((a, i) => (
          <div key={i} style={{ display: "flex", alignItems: "center", gap: 4, marginBottom: 2, paddingLeft: 8 }}>
            <span style={{ color: a.ok === null ? "#d29922" : a.ok ? "#3fb950" : "#f85149", fontWeight: 600 }}>
              {a.ok === null ? "..." : a.ok ? "ok" : "err"}
            </span>
            <span style={{ color: "#79c0ff" }}>{a.tool || a.name}</span>
          </div>
        ))}
      </div>
    );
  };

  return (
    <div style={{ border: `1px solid ${color}33`, borderRadius: 8, background: "#161b22", minWidth: 240, flex: "1 1 280px", display: "flex", flexDirection: "column", overflow: "hidden" }}>
      {/* Header */}
      <div style={{ padding: "8px 10px", borderBottom: "1px solid #21262d", display: "flex", alignItems: "center", gap: 6, background: `${color}0a` }}>
        <span style={{ width: 8, height: 8, borderRadius: "50%", background: statusColors[status], flexShrink: 0 }} />
        <span style={{ fontSize: 13, fontWeight: 700, color }}>{agent.name}</span>
        <span style={{ fontSize: 9, color: "#8b949e", background: "#0d1117", borderRadius: 3, padding: "1px 5px" }}>{agent.cls}</span>
        {loc && <span style={{ fontSize: 9, color: "#484f58" }}>({loc})</span>}
        <div style={{ flex: 1 }} />
        {status === "running" && <button onClick={handleStop} style={{ background: "none", border: "1px solid #da3633", borderRadius: 3, padding: "1px 6px", color: "#f85149", fontSize: 10, cursor: "pointer" }}>stop</button>}
      </div>

      {/* Activity log (ReAct) */}
      <div ref={activityRef} style={{ flex: 1, overflowY: "auto", padding: "6px 10px", minHeight: 80, maxHeight: 200 }}>
        {/* Past activities */}
        {pastActivities.slice(-3).map((act, i) => (
          <div key={i} style={{ marginBottom: 8, paddingBottom: 6, borderBottom: "1px solid #21262d" }}>
            {renderActivity(act, false)}
          </div>
        ))}
        {/* Live activity */}
        {(thought || actions.length > 0) ? renderActivity({ thought, response: thought }, true) : (
          pastActivities.length === 0 && <span style={{ color: "#484f58", fontSize: 11, fontStyle: "italic" }}>No activity yet</span>
        )}
        {status === "running" && <span style={{ color: "#d29922", fontSize: 10 }}> thinking...</span>}
      </div>

      {/* Input */}
      <div style={{ display: "flex", gap: 4, padding: "6px 8px", borderTop: "1px solid #21262d" }}>
        <input value={input} onChange={e => setInput(e.target.value)}
          onKeyDown={e => { if (e.key === "Enter") { e.preventDefault(); handleSend(input); } }}
          placeholder={pieActive ? `Command ${agent.name}...` : "PIE required"}
          disabled={status === "running" || !pieActive}
          style={{ flex: 1, background: "#0d1117", border: "1px solid #30363d", borderRadius: 4, padding: "5px 8px", color: "#e6edf3", fontSize: 11, outline: "none" }}
        />
        <button onClick={() => handleSend(input)}
          disabled={!input.trim() || status === "running" || !pieActive}
          style={{ background: input.trim() && status !== "running" && pieActive ? "#238636" : "#21262d", border: "none", borderRadius: 4, padding: "5px 10px", color: "#fff", fontSize: 10, cursor: input.trim() && status !== "running" && pieActive ? "pointer" : "default", opacity: input.trim() && status !== "running" && pieActive ? 1 : 0.5 }}
        >Go</button>
      </div>
    </div>
  );
}

// ─── Communication History (group chat sidebar) ─────────────────────────────

function CommHistory({ agents }) {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [target, setTarget] = useState("all");
  const lastTsRef = useRef(0);
  const scrollRef = useRef(null);

  useEffect(() => {
    const poll = async () => {
      try {
        const res = await fetch(`${API_BASE}/agent-chat-log?since=${lastTsRef.current}`);
        if (!res.ok) return;
        const data = await res.json();
        if (data.length > 0) {
          setMessages(prev => {
            const existing = new Set(prev.map(m => `${m.from}-${m.timestamp}`));
            const newMsgs = data.filter(m => !existing.has(`${m.from}-${m.timestamp}`));
            if (!newMsgs.length) return prev;
            const merged = [...prev, ...newMsgs].slice(-100);
            lastTsRef.current = Math.max(...merged.map(m => m.timestamp));
            return merged;
          });
        }
      } catch {}
    };
    poll();
    const id = setInterval(poll, 2000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => { if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight; }, [messages]);

  const handleSend = () => {
    const text = input.trim();
    if (!text) return;
    fetch(`${API_BASE}/agent-message`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ from: "user", to: target === "all" ? null : target, text }),
    }).catch(() => {});
    setMessages(prev => [...prev, { from: "user", to: target, text, timestamp: Date.now() }]);
    setInput("");
  };

  const colors = { user: "#e6edf3" };
  (agents || []).forEach((a, i) => { colors[a.name] = AGENT_COLORS[i % AGENT_COLORS.length]; });

  // Render @mentions in text with color
  const renderText = (text) => {
    const parts = text.split(/(@\w+)/g);
    return parts.map((part, i) => {
      if (part.startsWith("@")) {
        const name = part.slice(1);
        return <span key={i} style={{ color: colors[name] || "#58a6ff", fontWeight: 600 }}>{part}</span>;
      }
      return part;
    });
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", background: "#0d1117" }}>
      <div style={{ padding: "10px 12px", borderBottom: "1px solid #21262d", fontSize: 12, fontWeight: 600, color: "#e6edf3" }}>
        Communication
      </div>
      <div ref={scrollRef} style={{ flex: 1, overflowY: "auto", padding: "8px 12px" }}>
        {messages.length === 0 ? (
          <div style={{ color: "#484f58", fontSize: 11, textAlign: "center", marginTop: 40 }}>
            Messages between you and agents will appear here.
          </div>
        ) : messages.map((m, i) => (
          <div key={i} style={{ marginBottom: 8, fontSize: 12, lineHeight: "1.5" }}>
            <div style={{ display: "flex", alignItems: "baseline", gap: 6 }}>
              <span style={{ fontWeight: 700, color: colors[m.from] || "#8b949e" }}>{m.from === "user" ? "You" : m.from}</span>
              {m.to && m.to !== "all" && <span style={{ fontSize: 10, color: "#656d76" }}>to <span style={{ color: colors[m.to] || "#58a6ff" }}>@{m.to}</span></span>}
              <span style={{ fontSize: 9, color: "#484f58", marginLeft: "auto" }}>{new Date(m.timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>
            </div>
            <div style={{ color: "#c9d1d9", marginTop: 2 }}>{renderText(m.text)}</div>
          </div>
        ))}
      </div>
      <div style={{ padding: "8px 12px", borderTop: "1px solid #21262d", display: "flex", gap: 6 }}>
        <select value={target} onChange={e => setTarget(e.target.value)}
          style={{ background: "#161b22", border: "1px solid #30363d", borderRadius: 4, color: "#8b949e", fontSize: 11, padding: "4px 6px" }}>
          <option value="all">@all</option>
          {(agents || []).map(a => <option key={a.name} value={a.name}>@{a.name}</option>)}
        </select>
        <input value={input} onChange={e => setInput(e.target.value)}
          onKeyDown={e => { if (e.key === "Enter") { e.preventDefault(); handleSend(); } }}
          placeholder="Message..."
          style={{ flex: 1, background: "#161b22", border: "1px solid #30363d", borderRadius: 4, padding: "5px 8px", color: "#e6edf3", fontSize: 11, outline: "none" }}
        />
        <button onClick={handleSend} disabled={!input.trim()} style={{
          background: input.trim() ? "#238636" : "#21262d", border: "none", borderRadius: 4,
          padding: "5px 10px", color: "#fff", fontSize: 11, cursor: input.trim() ? "pointer" : "default", opacity: input.trim() ? 1 : 0.5,
        }}>Send</button>
      </div>
    </div>
  );
}

function AgentPanel({ sessionId }) {
  const [contextAgents, setContextAgents] = useState([]);
  const [pieActive, setPieActive] = useState(false);
  const sessionRef = useRef(sessionId);
  sessionRef.current = sessionId;

  useEffect(() => {
    const poll = async () => {
      try { const r = await fetch(`${API_BASE}/pie-status`); if (r.ok) setPieActive((await r.json()).active); } catch {}
    };
    poll();
    const id = setInterval(poll, 5000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    let stopped = false;
    const poll = async () => {
      try {
        const r = await fetch(`${API_BASE}/agent-sessions`);
        if (r.ok && !stopped) {
          const sessions = await r.json();
          if (sessions.length > 0) {
            setContextAgents(sessions.map(s => ({ name: s.agentName, cls: s.agentClass, location: s.location, status: s.status })));
            return;
          }
        }
        const sid = sessionRef.current;
        const url = sid ? `${API_BASE}/context?sessionId=${encodeURIComponent(sid)}` : `${API_BASE}/context`;
        const cr = await fetch(url);
        if (cr.ok && !stopped) setContextAgents((await cr.json()).agents || []);
      } catch {}
    };
    poll();
    const id = setInterval(poll, 3000);
    return () => { stopped = true; clearInterval(id); };
  }, [sessionId]);

  if (contextAgents.length === 0) {
    return (
      <div style={{ height: "100%", display: "flex", flexDirection: "column", background: "#0d1117", color: "#e6edf3" }}>
        <div style={{ padding: "10px 14px", borderBottom: "1px solid #21262d" }}>
          <span style={{ fontSize: 13, fontWeight: 600 }}>Agents</span>
        </div>
        <div style={{ padding: "6px 14px", borderBottom: "1px solid #21262d", fontSize: 11, color: "#656d76" }}>
          {pieActive ? "PIE active. Spawn agents to control them." : "Start PIE in Unreal Engine to enable agent control."}
        </div>
        <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center" }}>
          <span style={{ color: "#484f58", fontSize: 12 }}>No agents in scene</span>
        </div>
      </div>
    );
  }

  // Split layout: left = comm history, right = agent cards
  return (
    <div style={{ height: "100%", display: "flex", background: "#0d1117", color: "#e6edf3", overflow: "hidden" }}>
      {/* Left: Communication History */}
      <div style={{ width: 300, minWidth: 240, borderRight: "1px solid #21262d", display: "flex", flexDirection: "column" }}>
        <CommHistory agents={contextAgents} />
      </div>

      {/* Right: Agent Cards */}
      <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>
        <div style={{ padding: "8px 14px", borderBottom: "1px solid #21262d", display: "flex", alignItems: "center", gap: 8, flexShrink: 0 }}>
          <span style={{ fontSize: 13, fontWeight: 600 }}>Agents</span>
          <span style={{ fontSize: 10, color: "#3fb950", background: "#1a3a2a", borderRadius: 3, padding: "1px 6px" }}>{contextAgents.length}</span>
          <div style={{ flex: 1 }} />
          <span style={{ width: 6, height: 6, borderRadius: "50%", background: pieActive ? "#3fb950" : "#f85149" }} />
          <span style={{ fontSize: 10, color: pieActive ? "#3fb950" : "#f85149" }}>PIE</span>
        </div>
        <div style={{ flex: 1, overflowY: "auto", padding: 10, display: "flex", flexWrap: "wrap", gap: 10, alignContent: "flex-start" }}>
          {contextAgents.map((a, i) => (
            <AgentCard key={a.name} agent={a} sessionId={sessionId} pieActive={pieActive} colorIdx={i} />
          ))}
        </div>
      </div>
    </div>
  );
}

// ─── ViewportPanel ───────────────────────────────────────────────────────────

function ViewportPanel({ latestScreenshot }) {
  const [mode, setMode] = useState("screenshot");
  const [imgKey, setImgKey] = useState(0);
  const [screenshotUrl, setScreenshotUrl] = useState(null);
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [refreshInterval, setRefreshInterval] = useState(3);
  const [cameraMoving, setCameraMoving] = useState(false);
  const intervalRef = useRef(null);

  const handleCameraPreset = useCallback(async (args) => {
    setCameraMoving(true);
    try {
      await sendCameraCommand("set_camera", args);
    } finally {
      setCameraMoving(false);
    }
  }, []);

  const [playerUrl, setPlayerUrl] = useState(null);
  useEffect(() => {
    fetch("/api/pixel-streaming-url")
      .then((r) => r.json())
      .then((d) => {
        if (d.url) {
          // Use our custom player page, passing cirrus port as param
          try {
            const cirrusPort = new URL(d.url).port;
            setPlayerUrl(`/ue-player.html?cirrus=${cirrusPort}`);
          } catch { setPlayerUrl(d.url); }
        }
      })
      .catch(() => {});
  }, []);

  // Update from prop — fetch as blob for reliable display
  useEffect(() => {
    if (!latestScreenshot) return;
    setMode("screenshot");
    let cancelled = false;
    const sep = latestScreenshot.includes("?") ? "&" : "?";
    const url = latestScreenshot + `${sep}t=${Date.now()}`;
    fetch(url)
      .then((r) => (r.ok ? r.blob() : Promise.reject()))
      .then((blob) => {
        if (!cancelled) {
          setScreenshotUrl(URL.createObjectURL(blob));
          setImgKey((k) => k + 1);
        }
      })
      .catch(() => {
        // Fallback to direct URL if blob fetch fails
        if (!cancelled) {
          setScreenshotUrl(url);
          setImgKey((k) => k + 1);
        }
      });
    return () => { cancelled = true; };
  }, [latestScreenshot]);

  // Initial fetch
  useEffect(() => {
    fetchLatestScreenshot();
  }, []);

  // Auto-refresh interval
  useEffect(() => {
    if (intervalRef.current) clearInterval(intervalRef.current);
    if (autoRefresh && mode === "screenshot") {
      intervalRef.current = setInterval(fetchLatestScreenshot, refreshInterval * 1000);
    }
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, [autoRefresh, mode, refreshInterval]);

  const fetchLatestScreenshot = async () => {
    try {
      const resp = await fetch(`/api/screenshot/latest?t=${Date.now()}`);
      if (resp.ok) {
        const blob = await resp.blob();
        setScreenshotUrl(URL.createObjectURL(blob));
        setImgKey((k) => k + 1);
      }
    } catch {}
  };

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        height: "100%",
        background: "#0a0d12",
      }}
    >
      {/* Toolbar */}
      <div
        style={{
          padding: "8px 14px",
          borderBottom: "1px solid #21262d",
          display: "flex",
          alignItems: "center",
          gap: 8,
          flexShrink: 0,
          background: "#161b22",
          userSelect: "none",
        }}
      >
        <span style={{ fontSize: 15 }}>🎮</span>
        <span style={{ fontWeight: 600, fontSize: 13, color: "#e6edf3" }}>UE Viewport</span>

        {/* Mode toggle */}
        <div style={{ display: "flex", gap: 4, marginLeft: 8 }}>
          {["screenshot", "pixelstream"].map((m) => (
            <button
              key={m}
              onClick={() => setMode(m)}
              style={{
                padding: "3px 10px",
                fontSize: 11,
                borderRadius: 4,
                border: "1px solid",
                borderColor: mode === m ? "#1f6feb" : "#30363d",
                background: mode === m ? "#1f3a5f" : "transparent",
                color: mode === m ? "#58a6ff" : "#8b949e",
                cursor: "pointer",
              }}
            >
              {m === "pixelstream" ? "🔴 Live Stream" : "📸 Screenshot"}
            </button>
          ))}
        </div>

        {/* Screenshot controls */}
        {mode === "screenshot" && (
          <>
            <button
              onClick={fetchLatestScreenshot}
              style={{
                marginLeft: "auto",
                padding: "3px 10px",
                fontSize: 12,
                borderRadius: 4,
                border: "1px solid #30363d",
                background: "#21262d",
                color: "#e6edf3",
                cursor: "pointer",
              }}
            >
              ↻ Refresh
            </button>
            <label
              style={{
                display: "flex",
                alignItems: "center",
                gap: 4,
                fontSize: 12,
                color: "#8b949e",
                cursor: "pointer",
              }}
            >
              <input
                type="checkbox"
                checked={autoRefresh}
                onChange={(e) => setAutoRefresh(e.target.checked)}
              />
              Auto
            </label>
            <select
              value={refreshInterval}
              onChange={(e) => setRefreshInterval(Number(e.target.value))}
              style={{
                padding: "2px 6px",
                fontSize: 11,
                background: "#21262d",
                border: "1px solid #30363d",
                borderRadius: 4,
                color: "#8b949e",
              }}
            >
              {[2, 3, 5, 10].map((s) => (
                <option key={s} value={s}>
                  {s}s
                </option>
              ))}
            </select>
          </>
        )}

        {/* Camera presets */}
        <div style={{ display: "flex", gap: 3, marginLeft: 8, alignItems: "center" }}>
          <span style={{ fontSize: 10, color: "#656d76", marginRight: 2 }}>CAM</span>
          {CAMERA_PRESETS.map((preset) => (
            <button
              key={preset.label}
              title={preset.title}
              disabled={cameraMoving}
              onClick={() => handleCameraPreset(preset.args)}
              style={{
                padding: "3px 8px",
                fontSize: 11,
                borderRadius: 4,
                border: "1px solid #30363d",
                background: "#21262d",
                color: cameraMoving ? "#484f58" : "#8b949e",
                cursor: cameraMoving ? "wait" : "pointer",
              }}
            >
              {preset.label}
            </button>
          ))}
          <button
            title="Eject agent pilot lock — restores your mouse control"
            disabled={cameraMoving}
            onClick={() => {
              setCameraMoving(true);
              sendCameraCommand("unpilot_camera").finally(() => setCameraMoving(false));
            }}
            style={{
              padding: "3px 8px",
              fontSize: 11,
              borderRadius: 4,
              border: "1px solid #30363d",
              background: "#21262d",
              color: cameraMoving ? "#484f58" : "#f85149",
              cursor: cameraMoving ? "wait" : "pointer",
            }}
          >
            ✕ Unlock
          </button>
        </div>

        {/* Open UE tab */}
        <div style={{ marginLeft: "auto" }}>
          <a
            href={playerUrl}
            target="_blank"
            rel="noreferrer"
            title="Open UE pixel stream in a separate tab"
            style={{
              padding: "3px 10px",
              fontSize: 11,
              borderRadius: 4,
              border: "1px solid #30363d",
              background: "#21262d",
              color: "#58a6ff",
              textDecoration: "none",
              cursor: "pointer",
              display: "inline-block",
            }}
          >
            ⧉ Open UE Tab
          </a>
        </div>
      </div>

      {/* Viewport content */}
      <div style={{ flex: 1, position: "relative", overflow: "hidden" }}>
        {/* Keep PixelStreamView always mounted to preserve WebRTC connection */}
        <div style={{ width: "100%", height: "100%", display: mode === "pixelstream" ? "block" : "none" }}>
          {playerUrl
            ? <PixelStreamView playerUrl={playerUrl} />
            : <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%", color: "#8b949e", fontSize: 14 }}>Connecting to pixel stream...</div>
          }
        </div>
        {mode === "screenshot" && (
          <ScreenshotView src={screenshotUrl} imgKey={imgKey} onRefresh={fetchLatestScreenshot} />
        )}
      </div>

      {/* Status bar */}
      <div
        style={{
          padding: "3px 14px",
          background: "#161b22",
          borderTop: "1px solid #21262d",
          display: "flex",
          alignItems: "center",
          gap: 16,
          fontSize: 10,
          color: "#656d76",
          flexShrink: 0,
        }}
      >
        <span>UE 5.3.2 · SimWorld</span>
        <span>PS player: {playerUrl ?? "loading..."}</span>
        {mode === "screenshot" && screenshotUrl && (
          <span style={{ color: "#3fb950", marginLeft: "auto" }}>● Screenshot ready</span>
        )}
      </div>
    </div>
  );
}

// ─── AssetPlaceholder ────────────────────────────────────────────────────────

function AssetPlaceholder({ id, category }) {
  const color = CATEGORY_COLORS[category] || "#30363d";
  const numMatch = id.match(/(\d+)/);
  if (numMatch) parseInt(numMatch[1]);

  return (
    <div
      style={{
        width: "100%",
        height: "100%",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        background: `linear-gradient(135deg, ${color}22 0%, ${color}11 100%)`,
        gap: 4,
      }}
    >
      <span style={{ fontSize: 28, opacity: 0.6 }}>{CATEGORY_ICONS[category] || "⬜"}</span>
      <span
        style={{
          fontSize: 9,
          color,
          opacity: 0.8,
          fontWeight: 600,
          maxWidth: "90%",
          textAlign: "center",
          wordBreak: "break-all",
        }}
      >
        {id.replace("BP_", "").replace(/_/g, " ")}
      </span>
    </div>
  );
}

// ─── AssetCard (grid view) ───────────────────────────────────────────────────

function AssetCard({ item, category, onInsert }) {
  const [imgError, setImgError] = useState(false);
  const thumbnailUrl = `/thumbnails/${item.id}.png`;

  return (
    <div
      onClick={() => onInsert(item.id)}
      title={item.path || item.id}
      style={{
        borderRadius: 6,
        overflow: "hidden",
        border: "1px solid #21262d",
        background: "#161b22",
        cursor: "pointer",
        transition: "border-color 0.15s",
      }}
      onMouseEnter={(e) => (e.currentTarget.style.borderColor = "#58a6ff")}
      onMouseLeave={(e) => (e.currentTarget.style.borderColor = "#21262d")}
    >
      <div
        style={{
          height: 90,
          background: "#0d1117",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          overflow: "hidden",
        }}
      >
        {imgError ? (
          <AssetPlaceholder id={item.id} category={category} />
        ) : (
          <img
            src={thumbnailUrl}
            alt={item.id}
            onError={() => setImgError(true)}
            style={{ width: "100%", height: "100%", objectFit: "cover" }}
          />
        )}
      </div>
      <div style={{ padding: "5px 8px" }}>
        <div
          style={{
            fontSize: 10,
            fontWeight: 500,
            color: "#e6edf3",
            wordBreak: "break-all",
            lineHeight: 1.3,
          }}
        >
          {item.id}
        </div>
      </div>
    </div>
  );
}

// ─── AssetListItem ───────────────────────────────────────────────────────────

function AssetListItem({ item, category, onInsert }) {
  const [imgError, setImgError] = useState(false);
  const thumbnailUrl = `/thumbnails/${item.id}.png`;

  return (
    <div
      onClick={() => onInsert(item.id)}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        padding: "4px 8px",
        borderRadius: 4,
        cursor: "pointer",
      }}
      onMouseEnter={(e) => (e.currentTarget.style.background = "#161b22")}
      onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
    >
      <div
        style={{
          width: 36,
          height: 36,
          borderRadius: 4,
          overflow: "hidden",
          background: "#0d1117",
          flexShrink: 0,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          border: "1px solid #21262d",
        }}
      >
        {imgError ? (
          <span style={{ fontSize: 16 }}>{CATEGORY_ICONS[category] || "⬜"}</span>
        ) : (
          <img
            src={thumbnailUrl}
            alt={item.id}
            onError={() => setImgError(true)}
            style={{ width: "100%", height: "100%", objectFit: "cover" }}
          />
        )}
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 11, color: "#e6edf3", fontWeight: 500 }}>{item.id}</div>
        {item.path && (
          <div
            style={{
              fontSize: 9,
              color: "#484f58",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {item.path.split("/").pop()?.replace("_C", "")}
          </div>
        )}
      </div>
    </div>
  );
}

// ─── AssetBrowser ────────────────────────────────────────────────────────────

function AssetBrowser({ onInsert }) {
  const [assets, setAssets] = useState(null);
  const [activeCategory, setActiveCategory] = useState(null);
  const [search, setSearch] = useState("");
  const [viewMode, setViewMode] = useState("grid");

  useEffect(() => {
    fetchAssets()
      .then(setAssets)
      .catch(() => {});
  }, []);

  if (!assets) {
    return (
      <div style={{ padding: 12, color: "#8b949e", fontSize: 12 }}>Loading assets...</div>
    );
  }

  const categories = Object.keys(assets);
  const currentCategory = activeCategory || categories[0];
  const categoryData = assets[currentCategory];
  let items = categoryData?.items || [];

  if (search) {
    const q = search.toLowerCase();
    items = items.filter((item) => item.id.toLowerCase().includes(q));
  }

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        height: "100%",
        background: "#0d1117",
      }}
    >
      {/* Search bar */}
      <div
        style={{
          padding: "8px 10px",
          borderBottom: "1px solid #21262d",
          display: "flex",
          gap: 6,
          alignItems: "center",
        }}
      >
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search assets..."
          style={{
            flex: 1,
            padding: "5px 8px",
            fontSize: 12,
            background: "#161b22",
            border: "1px solid #30363d",
            borderRadius: 4,
            color: "#e6edf3",
            outline: "none",
            boxSizing: "border-box",
          }}
        />
        <button
          onClick={() => setViewMode(viewMode === "grid" ? "list" : "grid")}
          style={{
            padding: "4px 8px",
            fontSize: 11,
            background: "#21262d",
            border: "1px solid #30363d",
            borderRadius: 4,
            color: "#8b949e",
            cursor: "pointer",
          }}
          title={viewMode === "grid" ? "Switch to list view" : "Switch to grid view"}
        >
          {viewMode === "grid" ? "☰" : "▦"}
        </button>
      </div>

      {/* Category tabs */}
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: 2,
          padding: "6px 10px",
          borderBottom: "1px solid #21262d",
        }}
      >
        {categories.map((cat) => (
          <button
            key={cat}
            onClick={() => {
              setActiveCategory(cat);
              setSearch("");
            }}
            style={{
              padding: "3px 8px",
              fontSize: 10,
              borderRadius: 4,
              border: `1px solid ${cat === currentCategory ? "#1f6feb" : "#21262d"}`,
              background: cat === currentCategory ? "#1f3a5f" : "transparent",
              color: cat === currentCategory ? "#58a6ff" : "#8b949e",
              cursor: "pointer",
            }}
          >
            {CATEGORY_ICONS[cat] || ""} {cat.replace(/_/g, " ")}
          </button>
        ))}
      </div>

      {/* Category description */}
      {categoryData?.description && (
        <div
          style={{
            padding: "6px 10px",
            fontSize: 10,
            color: "#656d76",
            borderBottom: "1px solid #21262d",
          }}
        >
          {categoryData.description}
        </div>
      )}

      {/* Asset grid/list */}
      <div style={{ flex: 1, overflow: "auto", padding: 8 }}>
        {viewMode === "grid" ? (
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(130px, 1fr))",
              gap: 6,
            }}
          >
            {items.map((item) => (
              <AssetCard
                key={item.id}
                item={item}
                category={currentCategory}
                onInsert={onInsert}
              />
            ))}
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
            {items.map((item) => (
              <AssetListItem
                key={item.id}
                item={item}
                category={currentCategory}
                onInsert={onInsert}
              />
            ))}
          </div>
        )}
        {items.length === 0 && (
          <div
            style={{
              textAlign: "center",
              padding: 20,
              color: "#484f58",
              fontSize: 12,
            }}
          >
            {search ? "No matching assets" : "No items in this category"}
          </div>
        )}
      </div>

      {/* Footer */}
      <div
        style={{
          padding: "4px 10px",
          borderTop: "1px solid #21262d",
          fontSize: 10,
          color: "#484f58",
          display: "flex",
          alignItems: "center",
          gap: 8,
        }}
      >
        <span>
          {items.length} assets {search && `matching "${search}"`}
        </span>
        <span style={{ marginLeft: "auto", color: "#656d76" }}>Click to insert into chat</span>
      </div>
    </div>
  );
}

// ─── SceneManager ────────────────────────────────────────────────────────────

function SceneManager({ onLoadScene, currentSessionId }) {
  const [scenes, setScenes] = useState([]);
  const [loading, setLoading] = useState(true);

  const reload = () => {
    setLoading(true);
    fetchScenes()
      .then(setScenes)
      .catch(() => {})
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    reload();
  }, []);

  const handleDelete = async (id) => {
    if (confirm("Delete this scene?")) {
      await deleteScene(id);
      reload();
    }
  };

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        height: "100%",
        background: "#0d1117",
      }}
    >
      <div
        style={{
          padding: "8px 12px",
          borderBottom: "1px solid #21262d",
          display: "flex",
          alignItems: "center",
          gap: 8,
        }}
      >
        <span style={{ fontSize: 12, fontWeight: 600, color: "#e6edf3" }}>Saved Scenes</span>
        <button
          onClick={reload}
          style={{
            marginLeft: "auto",
            padding: "3px 8px",
            fontSize: 10,
            background: "#21262d",
            border: "1px solid #30363d",
            borderRadius: 4,
            color: "#8b949e",
            cursor: "pointer",
          }}
        >
          Refresh
        </button>
      </div>

      <div style={{ flex: 1, overflow: "auto", padding: 8 }}>
        {loading && (
          <div style={{ padding: 12, color: "#8b949e", fontSize: 12 }}>Loading...</div>
        )}
        {!loading && scenes.length === 0 && (
          <div
            style={{
              padding: 20,
              textAlign: "center",
              color: "#484f58",
              fontSize: 12,
            }}
          >
            No saved scenes yet. Use the save button after generating a scene.
          </div>
        )}
        {scenes.map((scene) => (
          <div
            key={scene.id}
            style={{
              marginBottom: 8,
              borderRadius: 6,
              overflow: "hidden",
              border: "1px solid #21262d",
              background: "#161b22",
            }}
          >
            {scene.thumbnail && (
              <div
                style={{
                  height: 100,
                  overflow: "hidden",
                  borderBottom: "1px solid #21262d",
                }}
              >
                <img
                  src={scene.thumbnail}
                  alt={scene.name}
                  style={{ width: "100%", height: "100%", objectFit: "cover" }}
                />
              </div>
            )}
            <div style={{ padding: "8px 10px" }}>
              <div style={{ fontSize: 12, fontWeight: 500, color: "#e6edf3" }}>{scene.name}</div>
              {scene.prompt && (
                <div
                  style={{
                    fontSize: 10,
                    color: "#8b949e",
                    marginTop: 3,
                    lineHeight: 1.4,
                  }}
                >
                  {scene.prompt.length > 80 ? scene.prompt.slice(0, 80) + "..." : scene.prompt}
                </div>
              )}
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 6,
                  marginTop: 6,
                }}
              >
                <span style={{ fontSize: 9, color: "#484f58" }}>
                  {new Date(scene.updatedAt).toLocaleDateString()}
                </span>
                <div style={{ marginLeft: "auto", display: "flex", gap: 4 }}>
                  <button
                    onClick={() => onLoadScene(scene)}
                    style={{
                      padding: "2px 8px",
                      fontSize: 10,
                      borderRadius: 3,
                      border: "1px solid #1f6feb",
                      background: "transparent",
                      color: "#58a6ff",
                      cursor: "pointer",
                    }}
                  >
                    Load
                  </button>
                  <button
                    onClick={() => handleDelete(scene.id)}
                    style={{
                      padding: "2px 8px",
                      fontSize: 10,
                      borderRadius: 3,
                      border: "1px solid #30363d",
                      background: "transparent",
                      color: "#f85149",
                      cursor: "pointer",
                    }}
                  >
                    Del
                  </button>
                </div>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ─── BattleSide ──────────────────────────────────────────────────────────────

function BattleSide({ label, side, isWinner, isLoser, revealed, onVote }) {
  const borderColor = isWinner ? "#3fb950" : isLoser ? "#f8514933" : "#30363d";

  return (
    <div
      style={{
        border: `2px solid ${borderColor}`,
        borderRadius: 12,
        overflow: "hidden",
        background: "#161b22",
        transition: "border-color 0.2s",
        opacity: isLoser ? 0.6 : 1,
      }}
    >
      <div
        style={{
          padding: "10px 16px",
          borderBottom: "1px solid #21262d",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
        }}
      >
        <span style={{ fontSize: 14, fontWeight: 600, color: "#e6edf3" }}>
          {label}
          {isWinner && " ✅"}
        </span>
        {revealed && side && (
          <span
            style={{
              fontSize: 11,
              color: "#8b949e",
              background: "#21262d",
              padding: "2px 8px",
              borderRadius: 4,
            }}
          >
            {side.agentName}
          </span>
        )}
        {!revealed && (
          <span style={{ fontSize: 11, color: "#484f58" }}>Identity hidden</span>
        )}
      </div>

      <div
        style={{
          height: 250,
          background: "#0d1117",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        {side && side.screenshots.length > 0 ? (
          <img
            src={side.screenshots[0]}
            alt={label}
            style={{ width: "100%", height: "100%", objectFit: "cover" }}
          />
        ) : (
          <div style={{ color: "#484f58", fontSize: 13 }}>
            {side ? "No screenshot available" : "Waiting for generation..."}
          </div>
        )}
      </div>

      {onVote && (
        <div style={{ padding: 12, textAlign: "center" }}>
          <button
            onClick={onVote}
            style={{
              padding: "8px 24px",
              fontSize: 13,
              fontWeight: 600,
              background: "#238636",
              border: "1px solid #2ea043",
              borderRadius: 6,
              color: "#fff",
              cursor: "pointer",
              width: "100%",
            }}
          >
            Vote for {label}
          </button>
        </div>
      )}
    </div>
  );
}

// ─── ArenaPage ───────────────────────────────────────────────────────────────

const tieButtonStyle = {
  padding: "8px 20px",
  fontSize: 12,
  background: "#21262d",
  border: "1px solid #30363d",
  borderRadius: 6,
  color: "#8b949e",
  cursor: "pointer",
};

function ArenaPage() {
  const [prompt, setPrompt] = useState("");
  const [battle, setBattle] = useState(null);
  const [phase, setPhase] = useState("prompt");
  const [voted, setVoted] = useState(null);
  const [progress, setProgress] = useState(null);
  const [agents, setAgents] = useState([]);
  const [showAgents, setShowAgents] = useState(false);
  const [shared, setShared] = useState(false);
  const abortRef = useRef(null);

  useEffect(() => {
    fetchAgents()
      .then(setAgents)
      .catch(() => {});
  }, []);

  const startBattle = async () => {
    if (!prompt.trim()) return;
    setPhase("generating");
    setProgress(null);
    setShared(false);

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      await runArena(
        prompt,
        [],
        (eventType, data) => {
          if (eventType === "battle_created") return;
          if (eventType === "progress") {
            setProgress(data);
          } else if (eventType === "complete") {
            setBattle(data);
            setPhase("voting");
          } else if (eventType === "error") {
            console.error("Battle error:", data);
            setPhase("prompt");
          }
        },
        controller.signal
      );
    } catch (err) {
      if (err.name !== "AbortError") console.error("Battle failed:", err);
      if (phase === "generating") setPhase("prompt");
    }
  };

  const handleVote = async (winner) => {
    if (!battle) return;
    setVoted(winner);
    try {
      const result = await voteOnBattle(battle.id, winner);
      setBattle(result);
      setPhase("result");
    } catch {}
  };

  const handleShareWinner = async () => {
    if (!battle) return;
    const winnerSide =
      voted === "a" ? battle.side_a : voted === "b" ? battle.side_b : battle.side_a;
    if (winnerSide) {
      try {
        await shareToGallery({
          prompt: battle.prompt,
          agentName: winnerSide.agentName,
          screenshots: winnerSide.screenshots,
          tags: ["arena", "battle"],
          skills: battle.skills,
        });
        setShared(true);
      } catch {}
    }
  };

  const resetBattle = () => {
    if (abortRef.current) abortRef.current.abort();
    setPrompt("");
    setBattle(null);
    setPhase("prompt");
    setVoted(null);
    setProgress(null);
    setShared(false);
  };

  const toggleAgent = async (agentId, enabled) => {
    try {
      const result = await updateAgent(agentId, { enabled });
      setAgents((prev) => prev.map((a) => (a.id === agentId ? { ...a, ...result } : a)));
    } catch {}
  };

  return (
    <div
      style={{
        height: "100%",
        display: "flex",
        flexDirection: "column",
        background: "#0d1117",
      }}
    >
      {/* Header */}
      <div
        style={{
          padding: "16px 24px",
          borderBottom: "1px solid #21262d",
          display: "flex",
          alignItems: "center",
          gap: 12,
        }}
      >
        <span style={{ fontSize: 24 }}>⚔️</span>
        <div>
          <div style={{ fontSize: 18, fontWeight: 700, color: "#e6edf3" }}>Arena Battle</div>
          <div style={{ fontSize: 12, color: "#8b949e" }}>
            Two agents generate scenes from the same prompt. You decide which is better.
          </div>
        </div>
        <div style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
          <button
            onClick={() => setShowAgents(!showAgents)}
            style={{
              padding: "4px 12px",
              fontSize: 11,
              borderRadius: 4,
              border: "1px solid #30363d",
              background: showAgents ? "#1f3a5f" : "#21262d",
              color: showAgents ? "#58a6ff" : "#8b949e",
              cursor: "pointer",
            }}
          >
            Agents ({agents.filter((a) => a.enabled).length}/{agents.length})
          </button>
        </div>
      </div>

      {/* Agent list */}
      {showAgents && (
        <div
          style={{
            padding: "12px 24px",
            borderBottom: "1px solid #21262d",
            background: "#161b22",
          }}
        >
          <div
            style={{
              fontSize: 12,
              color: "#8b949e",
              marginBottom: 8,
              fontWeight: 600,
            }}
          >
            Available Agents
          </div>
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
            {agents.map((agent) => (
              <div
                key={agent.id}
                style={{
                  padding: "8px 14px",
                  borderRadius: 6,
                  border: `1px solid ${agent.enabled ? "#238636" : "#30363d"}`,
                  background: agent.enabled ? "#23863611" : "transparent",
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  minWidth: 200,
                }}
              >
                <input
                  type="checkbox"
                  checked={agent.enabled}
                  onChange={(e) => toggleAgent(agent.id, e.target.checked)}
                  style={{ accentColor: "#238636" }}
                />
                <div>
                  <div style={{ fontSize: 12, fontWeight: 600, color: "#e6edf3" }}>
                    {agent.name}
                  </div>
                  <div style={{ fontSize: 10, color: "#8b949e" }}>
                    {agent.type}
                    {agent.model ? ` (${agent.model})` : ""}
                  </div>
                  {agent.description && (
                    <div style={{ fontSize: 9, color: "#484f58", marginTop: 2 }}>
                      {agent.description}
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Content area */}
      <div style={{ flex: 1, overflow: "auto", padding: 24 }}>
        {/* Prompt phase */}
        {phase === "prompt" && (
          <div style={{ maxWidth: 600, margin: "60px auto", textAlign: "center" }}>
            <div
              style={{
                fontSize: 28,
                fontWeight: 700,
                color: "#e6edf3",
                marginBottom: 8,
              }}
            >
              Enter a Scene Prompt
            </div>
            <div style={{ fontSize: 14, color: "#8b949e", marginBottom: 24 }}>
              Both agents will try to build this scene. Vote for the better result.
            </div>
            <textarea
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder="A quiet residential neighborhood with tree-lined streets and a small park..."
              style={{
                width: "100%",
                height: 100,
                padding: 14,
                fontSize: 14,
                background: "#161b22",
                border: "1px solid #30363d",
                borderRadius: 8,
                color: "#e6edf3",
                resize: "vertical",
                outline: "none",
                boxSizing: "border-box",
                lineHeight: 1.5,
              }}
            />
            <div
              style={{
                display: "flex",
                gap: 12,
                justifyContent: "center",
                marginTop: 16,
              }}
            >
              <button
                onClick={startBattle}
                disabled={!prompt.trim()}
                style={{
                  padding: "10px 28px",
                  fontSize: 14,
                  background: prompt.trim() ? "#238636" : "#21262d",
                  border: `1px solid ${prompt.trim() ? "#2ea043" : "#30363d"}`,
                  borderRadius: 8,
                  color: prompt.trim() ? "#fff" : "#484f58",
                  cursor: prompt.trim() ? "pointer" : "default",
                  fontWeight: 600,
                }}
              >
                Start Battle
              </button>
            </div>
            <div style={{ fontSize: 11, color: "#484f58", marginTop: 12 }}>
              {agents.filter((a) => a.enabled).length} agent
              {agents.filter((a) => a.enabled).length !== 1 ? "s" : ""} enabled
            </div>
          </div>
        )}

        {/* Generating phase */}
        {phase === "generating" && (
          <div style={{ textAlign: "center", padding: 80 }}>
            <div style={{ fontSize: 40, marginBottom: 16 }}>⚔️</div>
            <div style={{ fontSize: 16, color: "#e6edf3", fontWeight: 600 }}>
              Generating scenes...
            </div>
            <div style={{ fontSize: 13, color: "#8b949e", marginTop: 8 }}>
              "{prompt}"
            </div>
            {progress && (
              <div style={{ marginTop: 20 }}>
                {progress.phase === "starting" && (
                  <div style={{ fontSize: 12, color: "#58a6ff" }}>
                    Matched: {progress.agentA} vs {progress.agentB}
                  </div>
                )}
                {progress.phase === "generating_a" && (
                  <div style={{ fontSize: 12, color: "#3fb950" }}>
                    Agent A ({progress.agent}) is generating...
                  </div>
                )}
                {progress.phase === "generating_b" && (
                  <div style={{ fontSize: 12, color: "#3fb950" }}>
                    Agent A done. Agent B ({progress.agent}) is generating...
                  </div>
                )}
              </div>
            )}
            <div
              style={{
                marginTop: 24,
                width: 200,
                height: 4,
                background: "#21262d",
                borderRadius: 2,
                overflow: "hidden",
                margin: "24px auto 0",
              }}
            >
              <div
                style={{
                  width: progress?.phase === "generating_b" ? "80%" : "40%",
                  height: "100%",
                  background: "#1f6feb",
                  borderRadius: 2,
                  transition: "width 0.5s",
                }}
              />
            </div>
            <button
              onClick={resetBattle}
              style={{
                marginTop: 24,
                padding: "6px 16px",
                fontSize: 12,
                background: "transparent",
                border: "1px solid #30363d",
                borderRadius: 6,
                color: "#8b949e",
                cursor: "pointer",
              }}
            >
              Cancel
            </button>
          </div>
        )}

        {/* Voting / Result phase */}
        {(phase === "voting" || phase === "result") && battle && (
          <div>
            <div style={{ textAlign: "center", marginBottom: 20 }}>
              <div style={{ fontSize: 13, color: "#8b949e" }}>Prompt</div>
              <div style={{ fontSize: 16, color: "#e6edf3", fontWeight: 500 }}>
                "{battle.prompt}"
              </div>
            </div>

            <div
              style={{
                display: "grid",
                gridTemplateColumns: "1fr 1fr",
                gap: 16,
                maxWidth: 1000,
                margin: "0 auto",
              }}
            >
              <BattleSide
                label="Agent A"
                side={battle.side_a}
                isWinner={voted === "a" || battle.winner === "a"}
                isLoser={voted !== null && voted !== "a" && voted !== "tie" && voted !== "both_bad"}
                revealed={phase === "result"}
                onVote={phase === "voting" ? () => handleVote("a") : undefined}
              />
              <BattleSide
                label="Agent B"
                side={battle.side_b}
                isWinner={voted === "b" || battle.winner === "b"}
                isLoser={voted !== null && voted !== "b" && voted !== "tie" && voted !== "both_bad"}
                revealed={phase === "result"}
                onVote={phase === "voting" ? () => handleVote("b") : undefined}
              />
            </div>

            {phase === "voting" && (
              <div
                style={{
                  display: "flex",
                  gap: 12,
                  justifyContent: "center",
                  marginTop: 20,
                }}
              >
                <button onClick={() => handleVote("tie")} style={tieButtonStyle}>
                  Tie - Both Good
                </button>
                <button onClick={() => handleVote("both_bad")} style={tieButtonStyle}>
                  Both Bad
                </button>
              </div>
            )}

            {phase === "result" && (
              <div style={{ textAlign: "center", marginTop: 24 }}>
                <div
                  style={{
                    fontSize: 14,
                    color: "#3fb950",
                    fontWeight: 600,
                    marginBottom: 12,
                  }}
                >
                  {voted === "tie"
                    ? "You voted: Tie"
                    : voted === "both_bad"
                      ? "You voted: Both Bad"
                      : `You voted: Agent ${voted?.toUpperCase()} wins!`}
                </div>
                {battle.side_a && battle.side_b && (
                  <div style={{ fontSize: 12, color: "#8b949e" }}>
                    Agent A: {battle.side_a.agentName} | Agent B: {battle.side_b.agentName}
                  </div>
                )}
                <div
                  style={{
                    display: "flex",
                    gap: 12,
                    justifyContent: "center",
                    marginTop: 16,
                  }}
                >
                  <button
                    onClick={resetBattle}
                    style={{
                      padding: "8px 20px",
                      fontSize: 13,
                      background: "#1f6feb",
                      border: "1px solid #388bfd",
                      borderRadius: 6,
                      color: "#fff",
                      cursor: "pointer",
                    }}
                  >
                    New Battle
                  </button>
                  <button
                    onClick={handleShareWinner}
                    disabled={shared}
                    style={{
                      padding: "8px 20px",
                      fontSize: 13,
                      background: shared ? "#21262d" : "#238636",
                      border: `1px solid ${shared ? "#30363d" : "#2ea043"}`,
                      borderRadius: 6,
                      color: shared ? "#8b949e" : "#fff",
                      cursor: shared ? "default" : "pointer",
                    }}
                  >
                    {shared ? "Shared!" : "Share to Gallery"}
                  </button>
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

// ─── LeaderboardPage ─────────────────────────────────────────────────────────

function LeaderboardPage() {
  const [entries, setEntries] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchLeaderboard()
      .then(setEntries)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  return (
    <div
      style={{
        height: "100%",
        display: "flex",
        flexDirection: "column",
        background: "#0d1117",
      }}
    >
      <div
        style={{
          padding: "16px 24px",
          borderBottom: "1px solid #21262d",
          display: "flex",
          alignItems: "center",
          gap: 12,
        }}
      >
        <span style={{ fontSize: 24 }}>🏆</span>
        <div>
          <div style={{ fontSize: 18, fontWeight: 700, color: "#e6edf3" }}>Leaderboard</div>
          <div style={{ fontSize: 12, color: "#8b949e" }}>
            Agent rankings based on Elo ratings from arena battles
          </div>
        </div>
        {entries.length > 0 && (
          <div style={{ marginLeft: "auto", fontSize: 12, color: "#484f58" }}>
            {entries.reduce((sum, e) => sum + e.numBattles, 0)} total battles
          </div>
        )}
      </div>

      <div style={{ flex: 1, overflow: "auto", padding: 24 }}>
        {loading ? (
          <div style={{ textAlign: "center", padding: 60, color: "#8b949e" }}>Loading...</div>
        ) : entries.length === 0 ? (
          <div style={{ textAlign: "center", padding: 60 }}>
            <div style={{ fontSize: 40, marginBottom: 12 }}>🏆</div>
            <div
              style={{
                fontSize: 16,
                color: "#e6edf3",
                fontWeight: 600,
                marginBottom: 8,
              }}
            >
              No battles yet
            </div>
            <div style={{ fontSize: 13, color: "#8b949e" }}>
              Run arena battles to see agents compete and build the leaderboard.
            </div>
          </div>
        ) : (
          <div style={{ maxWidth: 800, margin: "0 auto" }}>
            {/* Table header */}
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "40px 1fr 100px 80px 80px 80px 80px",
                padding: "8px 16px",
                fontSize: 11,
                color: "#484f58",
                fontWeight: 600,
                borderBottom: "1px solid #21262d",
                textTransform: "uppercase",
              }}
            >
              <span>#</span>
              <span>Agent</span>
              <span style={{ textAlign: "right" }}>Rating</span>
              <span style={{ textAlign: "right" }}>Battles</span>
              <span style={{ textAlign: "right" }}>Wins</span>
              <span style={{ textAlign: "right" }}>Losses</span>
              <span style={{ textAlign: "right" }}>Win Rate</span>
            </div>

            {/* Rows */}
            {entries.map((entry, i) => {
              const delta = entry.rating - 1200;
              return (
                <div
                  key={entry.agentName}
                  style={{
                    display: "grid",
                    gridTemplateColumns: "40px 1fr 100px 80px 80px 80px 80px",
                    padding: "12px 16px",
                    alignItems: "center",
                    borderBottom: "1px solid #21262d",
                    background: i === 0 ? "#1f3a5f11" : "transparent",
                  }}
                >
                  <span
                    style={{
                      fontSize: 14,
                      fontWeight: 700,
                      color:
                        i === 0
                          ? "#f0c000"
                          : i === 1
                            ? "#c0c0c0"
                            : i === 2
                              ? "#cd7f32"
                              : "#484f58",
                    }}
                  >
                    {i === 0 ? "🥇" : i === 1 ? "🥈" : i === 2 ? "🥉" : i + 1}
                  </span>
                  <div>
                    <span style={{ fontSize: 14, fontWeight: 600, color: "#e6edf3" }}>
                      {entry.agentName}
                    </span>
                    {i === 0 && (
                      <span
                        style={{
                          marginLeft: 8,
                          fontSize: 9,
                          padding: "1px 6px",
                          background: "#f0c00022",
                          color: "#f0c000",
                          borderRadius: 4,
                          border: "1px solid #f0c00044",
                        }}
                      >
                        Champion
                      </span>
                    )}
                  </div>
                  <div style={{ textAlign: "right" }}>
                    <span
                      style={{
                        fontSize: 16,
                        fontWeight: 700,
                        color:
                          entry.rating >= 1200
                            ? "#3fb950"
                            : entry.rating >= 1000
                              ? "#e6edf3"
                              : "#f85149",
                      }}
                    >
                      {Math.round(entry.rating)}
                    </span>
                    <span
                      style={{
                        fontSize: 10,
                        marginLeft: 4,
                        color: delta >= 0 ? "#3fb950" : "#f85149",
                      }}
                    >
                      {delta >= 0 ? "+" : ""}
                      {delta}
                    </span>
                  </div>
                  <span style={{ textAlign: "right", fontSize: 13, color: "#8b949e" }}>
                    {entry.numBattles}
                  </span>
                  <span style={{ textAlign: "right", fontSize: 13, color: "#3fb950" }}>
                    {entry.wins}
                  </span>
                  <span style={{ textAlign: "right", fontSize: 13, color: "#f85149" }}>
                    {entry.losses}
                  </span>
                  <span
                    style={{
                      textAlign: "right",
                      fontSize: 13,
                      fontWeight: 600,
                      color:
                        entry.winRate >= 0.6
                          ? "#3fb950"
                          : entry.winRate >= 0.4
                            ? "#e6edf3"
                            : "#f85149",
                    }}
                  >
                    {(entry.winRate * 100).toFixed(1)}%
                  </span>
                </div>
              );
            })}

            {/* Info box */}
            <div
              style={{
                marginTop: 24,
                padding: 16,
                borderRadius: 8,
                background: "#161b22",
                border: "1px solid #21262d",
                fontSize: 11,
                color: "#8b949e",
                lineHeight: 1.6,
              }}
            >
              <div style={{ fontWeight: 600, marginBottom: 4, color: "#e6edf3" }}>
                How ratings work
              </div>
              Agents start at 1200 Elo. Each battle updates ratings using the Bradley-Terry model
              (K=32). Winning against a higher-rated agent gives more points. Ties give half credit.
              Run more battles for more accurate rankings.
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ─── GalleryCard ─────────────────────────────────────────────────────────────

function GalleryCard({ scene, onClick }) {
  return (
    <div
      onClick={onClick}
      style={{
        borderRadius: 10,
        overflow: "hidden",
        border: "1px solid #21262d",
        background: "#161b22",
        cursor: "pointer",
        transition: "border-color 0.15s, transform 0.15s",
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.borderColor = "#58a6ff";
        e.currentTarget.style.transform = "translateY(-2px)";
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.borderColor = "#21262d";
        e.currentTarget.style.transform = "translateY(0)";
      }}
    >
      <div style={{ height: 180, background: "#0d1117", overflow: "hidden" }}>
        {scene.screenshots.length > 0 ? (
          <img
            src={scene.screenshots[0]}
            alt={scene.prompt}
            style={{ width: "100%", height: "100%", objectFit: "cover" }}
          />
        ) : (
          <div
            style={{
              height: "100%",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              color: "#484f58",
              fontSize: 13,
            }}
          >
            No preview
          </div>
        )}
      </div>
      <div style={{ padding: "10px 14px" }}>
        <div
          style={{
            fontSize: 13,
            fontWeight: 500,
            color: "#e6edf3",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
            lineHeight: 1.4,
          }}
        >
          {scene.prompt}
        </div>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            marginTop: 8,
          }}
        >
          <span
            style={{
              fontSize: 10,
              padding: "2px 6px",
              borderRadius: 4,
              background: "#1f6feb22",
              color: "#58a6ff",
              border: "1px solid #1f6feb44",
            }}
          >
            {scene.agentName}
          </span>
          {scene.tags.slice(0, 2).map((tag) => (
            <span
              key={tag}
              style={{
                fontSize: 9,
                padding: "1px 5px",
                borderRadius: 4,
                background: "#21262d",
                color: "#8b949e",
              }}
            >
              {tag}
            </span>
          ))}
          <span style={{ marginLeft: "auto", fontSize: 10, color: "#484f58" }}>
            {new Date(scene.created_at).toLocaleDateString()}
          </span>
        </div>
      </div>
    </div>
  );
}

// ─── GalleryDetailModal ──────────────────────────────────────────────────────

function GalleryDetailModal({ scene, onClose }) {
  const [currentImg, setCurrentImg] = useState(0);

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.8)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 9999,
      }}
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: "90%",
          maxWidth: 900,
          maxHeight: "85vh",
          background: "#0d1117",
          border: "1px solid #30363d",
          borderRadius: 12,
          overflow: "hidden",
          display: "flex",
          flexDirection: "column",
        }}
      >
        {/* Header */}
        <div
          style={{
            padding: "14px 20px",
            borderBottom: "1px solid #21262d",
            display: "flex",
            alignItems: "center",
          }}
        >
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 16, fontWeight: 600, color: "#e6edf3" }}>{scene.prompt}</div>
            <div style={{ fontSize: 11, color: "#8b949e", marginTop: 4 }}>
              Generated by {scene.agentName} on {new Date(scene.created_at).toLocaleString()}
            </div>
          </div>
          {onBack ? (
            <button
              onClick={onBack}
              style={{
                padding: "4px 10px",
                fontSize: 12,
                background: "#21262d",
                border: "1px solid #30363d",
                borderRadius: 6,
                color: "#c9d1d9",
                cursor: "pointer",
              }}
            >
              ← Back
            </button>
          ) : (
            <button
              onClick={onClose}
              style={{
                padding: "4px 10px",
                fontSize: 16,
                background: "transparent",
                border: "none",
                color: "#8b949e",
                cursor: "pointer",
              }}
            >
              ✕
            </button>
          )}
        </div>

        {/* Image */}
        <div style={{ position: "relative", background: "#000" }}>
          {scene.screenshots.length > 0 ? (
            <img
              src={scene.screenshots[currentImg]}
              alt={`Screenshot ${currentImg + 1}`}
              style={{ width: "100%", maxHeight: 400, objectFit: "contain" }}
            />
          ) : (
            <div
              style={{
                height: 200,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                color: "#484f58",
              }}
            >
              No screenshots
            </div>
          )}
          {scene.screenshots.length > 1 && (
            <div
              style={{
                position: "absolute",
                bottom: 8,
                left: "50%",
                transform: "translateX(-50%)",
                display: "flex",
                gap: 6,
              }}
            >
              {scene.screenshots.map((_, i) => (
                <button
                  key={i}
                  onClick={() => setCurrentImg(i)}
                  style={{
                    width: 8,
                    height: 8,
                    borderRadius: "50%",
                    border: "none",
                    background: i === currentImg ? "#58a6ff" : "#484f58",
                    cursor: "pointer",
                  }}
                />
              ))}
            </div>
          )}
        </div>

        {/* Details */}
        <div style={{ flex: 1, overflow: "auto", padding: 20 }}>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 12 }}>
            {scene.skills.map((s) => (
              <span
                key={s}
                style={{
                  fontSize: 10,
                  padding: "2px 7px",
                  borderRadius: 4,
                  background: "#1f6feb22",
                  color: "#58a6ff",
                  border: "1px solid #1f6feb44",
                }}
              >
                {s}
              </span>
            ))}
            {scene.tags.map((tag) => (
              <span
                key={tag}
                style={{
                  fontSize: 10,
                  padding: "2px 7px",
                  borderRadius: 4,
                  background: "#21262d",
                  color: "#8b949e",
                }}
              >
                {tag}
              </span>
            ))}
          </div>
          {scene.codePreview && (
            <div>
              <div
                style={{
                  fontSize: 12,
                  color: "#8b949e",
                  fontWeight: 600,
                  marginBottom: 6,
                }}
              >
                Generated Code Preview
              </div>
              <pre
                style={{
                  fontSize: 11,
                  color: "#c9d1d9",
                  background: "#161b22",
                  border: "1px solid #21262d",
                  borderRadius: 6,
                  padding: 12,
                  overflow: "auto",
                  maxHeight: 200,
                  fontFamily:
                    "ui-monospace, 'Cascadia Code', 'Source Code Pro', Menlo, monospace",
                  lineHeight: 1.5,
                  whiteSpace: "pre-wrap",
                }}
              >
                {scene.codePreview}
              </pre>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ─── GalleryPage ─────────────────────────────────────────────────────────────

function GalleryPage() {
  const [scenes, setScenes] = useState([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState(null);
  const [sortOrder, setSortOrder] = useState("newest");
  const [activeTag, setActiveTag] = useState(null);

  useEffect(() => {
    setLoading(true);
    fetchGallery({ limit: 100, sort: sortOrder })
      .then(setScenes)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [sortOrder]);

  const allTags = Array.from(new Set(scenes.flatMap((s) => s.tags || [])));
  const filtered = activeTag
    ? scenes.filter((s) => s.tags?.includes(activeTag))
    : scenes;

  return (
    <div
      style={{
        height: "100%",
        display: "flex",
        flexDirection: "column",
        background: "#0d1117",
      }}
    >
      {/* Header */}
      <div
        style={{
          padding: "16px 24px",
          borderBottom: "1px solid #21262d",
          display: "flex",
          alignItems: "center",
          gap: 12,
        }}
      >
        <span style={{ fontSize: 24 }}>🖼️</span>
        <div>
          <div style={{ fontSize: 18, fontWeight: 700, color: "#e6edf3" }}>Gallery</div>
          <div style={{ fontSize: 12, color: "#8b949e" }}>
            Browse AI-generated scenes shared by the community
          </div>
        </div>
        <div
          style={{
            marginLeft: "auto",
            display: "flex",
            gap: 8,
            alignItems: "center",
          }}
        >
          <select
            value={sortOrder}
            onChange={(e) => setSortOrder(e.target.value)}
            style={{
              padding: "3px 8px",
              fontSize: 11,
              borderRadius: 4,
              border: "1px solid #30363d",
              background: "#161b22",
              color: "#8b949e",
              cursor: "pointer",
            }}
          >
            <option value="newest">Newest first</option>
            <option value="oldest">Oldest first</option>
          </select>
          <span style={{ fontSize: 12, color: "#484f58" }}>
            {filtered.length} scene{filtered.length !== 1 ? "s" : ""}
          </span>
        </div>
      </div>

      {/* Tag filter */}
      {allTags.length > 0 && (
        <div
          style={{
            padding: "8px 24px",
            borderBottom: "1px solid #21262d",
            display: "flex",
            gap: 6,
            flexWrap: "wrap",
            alignItems: "center",
          }}
        >
          <span style={{ fontSize: 10, color: "#484f58", marginRight: 4 }}>Tags:</span>
          <button
            onClick={() => setActiveTag(null)}
            style={{
              fontSize: 10,
              padding: "2px 8px",
              borderRadius: 10,
              border: `1px solid ${activeTag ? "#30363d" : "#58a6ff"}`,
              background: activeTag ? "transparent" : "#1f6feb22",
              color: activeTag ? "#8b949e" : "#58a6ff",
              cursor: "pointer",
            }}
          >
            All
          </button>
          {allTags.slice(0, 15).map((tag) => (
            <button
              key={tag}
              onClick={() => setActiveTag(activeTag === tag ? null : tag)}
              style={{
                fontSize: 10,
                padding: "2px 8px",
                borderRadius: 10,
                border: `1px solid ${activeTag === tag ? "#58a6ff" : "#30363d"}`,
                background: activeTag === tag ? "#1f6feb22" : "transparent",
                color: activeTag === tag ? "#58a6ff" : "#8b949e",
                cursor: "pointer",
              }}
            >
              {tag}
            </button>
          ))}
        </div>
      )}

      {/* Content */}
      <div style={{ flex: 1, overflow: "auto", padding: 20 }}>
        {loading ? (
          <div style={{ textAlign: "center", padding: 60, color: "#8b949e" }}>Loading...</div>
        ) : filtered.length === 0 ? (
          <div style={{ textAlign: "center", padding: 60 }}>
            <div style={{ fontSize: 40, marginBottom: 12 }}>🖼️</div>
            <div
              style={{
                fontSize: 16,
                color: "#e6edf3",
                fontWeight: 600,
                marginBottom: 8,
              }}
            >
              No scenes yet
            </div>
            <div style={{ fontSize: 13, color: "#8b949e" }}>
              Generate scenes in the chat and share them to the gallery, or run arena battles.
            </div>
          </div>
        ) : (
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))",
              gap: 16,
            }}
          >
            {filtered.map((s) => (
              <GalleryCard key={s.id} scene={s} onClick={() => setSelected(s)} />
            ))}
          </div>
        )}
      </div>

      {selected && <GalleryDetailModal scene={selected} onClose={() => setSelected(null)} />}
    </div>
  );
}

// ─── SkillsPage (full page) ──────────────────────────────────────────────────

function SkillPageCard({ skill, onClick, isNew = false }) {
  const desc =
    skill.description.length > 120
      ? skill.description.slice(0, 120) + "..."
      : skill.description;

  return (
    <div
      onClick={onClick}
      style={{
        padding: "14px 16px",
        borderRadius: 10,
        border: "1px solid #21262d",
        background: "#161b22",
        cursor: "pointer",
        transition: "border-color 0.15s, transform 0.15s",
        display: "flex",
        flexDirection: "column",
        gap: 8,
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.borderColor = "#58a6ff";
        e.currentTarget.style.transform = "translateY(-2px)";
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.borderColor = "#21262d";
        e.currentTarget.style.transform = "translateY(0)";
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <div style={{ fontSize: 14, fontWeight: 600, color: "#e6edf3", flex: 1 }}>
          {skill.name}
        </div>
        {isNew && (
          <span
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 5,
              fontSize: 9,
              padding: "2px 7px",
              borderRadius: 10,
              background: "#f8514922",
              color: "#f85149",
              border: "1px solid #f8514944",
              flexShrink: 0,
              fontWeight: 700,
              letterSpacing: 0.2,
            }}
          >
            <span
              style={{
                width: 6,
                height: 6,
                borderRadius: "50%",
                background: "#f85149",
                boxShadow: "0 0 6px #f85149",
              }}
            />
            NEW
          </span>
        )}
        <span
          style={{
            fontSize: 9,
            padding: "2px 7px",
            borderRadius: 10,
            background: skill.source === "custom" ? "#1f6feb22" : "#21262d",
            color: skill.source === "custom" ? "#58a6ff" : "#484f58",
            border: `1px solid ${skill.source === "custom" ? "#1f6feb44" : "#30363d"}`,
            flexShrink: 0,
          }}
        >
          {skill.source}
        </span>
      </div>
      <div style={{ fontSize: 12, color: "#8b949e", lineHeight: 1.5 }}>{desc}</div>
      {skill.tags.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
          {skill.tags.map((tag) => (
            <span
              key={tag}
              style={{
                fontSize: 9,
                padding: "2px 6px",
                borderRadius: 4,
                background: (TAG_COLORS[tag] || "#30363d") + "33",
                color: TAG_COLORS[tag] || "#8b949e",
                border: `1px solid ${TAG_COLORS[tag] || "#30363d"}44`,
              }}
            >
              {tag}
            </span>
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
          borderTop: "1px solid #21262d",
          fontSize: 10,
          color: "#484f58",
        }}
      >
        <span>v{skill.version}</span>
        <span style={{ marginLeft: "auto" }}>{skill.author}</span>
      </div>
    </div>
  );
}

function SkillPageDetailModal({ skill, onClose, onDelete }) {
  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.8)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 9999,
      }}
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: "90%",
          maxWidth: 750,
          maxHeight: "85vh",
          background: "#0d1117",
          border: "1px solid #30363d",
          borderRadius: 12,
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
        }}
      >
        <div
          style={{
            padding: "14px 20px",
            borderBottom: "1px solid #21262d",
            display: "flex",
            alignItems: "center",
            gap: 10,
          }}
        >
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 18, fontWeight: 600, color: "#e6edf3" }}>{skill.name}</div>
            <div style={{ fontSize: 11, color: "#8b949e", marginTop: 4 }}>
              v{skill.version} by {skill.author}
              <span
                style={{
                  marginLeft: 8,
                  padding: "2px 7px",
                  borderRadius: 10,
                  background: skill.source === "custom" ? "#1f6feb22" : "#21262d",
                  color: skill.source === "custom" ? "#58a6ff" : "#484f58",
                  fontSize: 9,
                }}
              >
                {skill.source}
              </span>
            </div>
          </div>
          {onDelete && (
            <button
              onClick={onDelete}
              style={{
                padding: "5px 12px",
                fontSize: 11,
                background: "#da363322",
                border: "1px solid #da363366",
                borderRadius: 6,
                color: "#f85149",
                cursor: "pointer",
              }}
            >
              Delete
            </button>
          )}
          <button
            onClick={onClose}
            style={{
              padding: "4px 10px",
              fontSize: 16,
              background: "transparent",
              border: "none",
              color: "#8b949e",
              cursor: "pointer",
            }}
          >
            ✕
          </button>
        </div>

        <div style={{ padding: "12px 20px", borderBottom: "1px solid #21262d" }}>
          <div style={{ fontSize: 13, color: "#c9d1d9", lineHeight: 1.5 }}>
            {skill.description}
          </div>
          {skill.tags.length > 0 && (
            <div style={{ display: "flex", flexWrap: "wrap", gap: 5, marginTop: 10 }}>
              {skill.tags.map((tag) => (
                <span
                  key={tag}
                  style={{
                    fontSize: 10,
                    padding: "2px 7px",
                    borderRadius: 4,
                    background: (TAG_COLORS[tag] || "#30363d") + "33",
                    color: TAG_COLORS[tag] || "#8b949e",
                    border: `1px solid ${TAG_COLORS[tag] || "#30363d"}44`,
                  }}
                >
                  {tag}
                </span>
              ))}
            </div>
          )}
          {skill.dependencies.length > 0 && (
            <div style={{ fontSize: 11, color: "#8b949e", marginTop: 10 }}>
              <span style={{ fontWeight: 600 }}>Dependencies:</span>{" "}
              {skill.dependencies.map((dep, i) => (
                <span key={dep}>
                  <span style={{ color: "#58a6ff" }}>{dep}</span>
                  {i < skill.dependencies.length - 1 ? ", " : ""}
                </span>
              ))}
            </div>
          )}
        </div>

        <div style={{ flex: 1, overflow: "auto", padding: "14px 20px" }}>
          <div
            style={{
              fontSize: 10,
              color: "#484f58",
              fontWeight: 600,
              marginBottom: 8,
              textTransform: "uppercase",
              letterSpacing: 0.5,
            }}
          >
            Skill Content
          </div>
          <pre
            style={{
              fontSize: 12,
              color: "#c9d1d9",
              lineHeight: 1.6,
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
              fontFamily:
                "ui-monospace, 'Cascadia Code', 'Source Code Pro', Menlo, monospace",
              margin: 0,
              background: "#161b22",
              border: "1px solid #21262d",
              borderRadius: 8,
              padding: 16,
            }}
          >
            {skill.content}
          </pre>
        </div>
      </div>
    </div>
  );
}

function SkillPageCreateModal({ onClose, onCreated }) {
  const [id, setId] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [tags, setTags] = useState("");
  const [content, setContent] = useState(`# My Custom Skill

## Overview
Describe what this skill does.

## Instructions
Provide detailed instructions for the AI agent.
`);
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
        tags: tags
          .split(",")
          .map((t) => t.trim())
          .filter(Boolean),
        content: content.trim(),
      });
      onCreated();
    } catch {
      setError("Failed to save skill");
    } finally {
      setSaving(false);
    }
  };

  const inputStyle = {
    width: "100%",
    padding: "7px 12px",
    fontSize: 13,
    background: "#161b22",
    border: "1px solid #30363d",
    borderRadius: 6,
    color: "#e6edf3",
    outline: "none",
    boxSizing: "border-box",
  };

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.8)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 9999,
      }}
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: "90%",
          maxWidth: 650,
          maxHeight: "85vh",
          background: "#0d1117",
          border: "1px solid #30363d",
          borderRadius: 12,
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
        }}
      >
        <div
          style={{
            padding: "14px 20px",
            borderBottom: "1px solid #21262d",
            display: "flex",
            alignItems: "center",
          }}
        >
          <span style={{ fontSize: 16, fontWeight: 600, color: "#e6edf3" }}>
            Create Custom Skill
          </span>
          <button
            onClick={onClose}
            style={{
              marginLeft: "auto",
              padding: "4px 10px",
              fontSize: 16,
              background: "transparent",
              border: "none",
              color: "#8b949e",
              cursor: "pointer",
            }}
          >
            ✕
          </button>
        </div>

        <div
          style={{
            flex: 1,
            overflow: "auto",
            padding: "14px 20px",
            display: "flex",
            flexDirection: "column",
            gap: 14,
          }}
        >
          <div>
            <label style={{ fontSize: 11, color: "#8b949e", display: "block", marginBottom: 4 }}>
              Skill ID (lowercase, no spaces)
            </label>
            <input value={id} onChange={(e) => setId(e.target.value)} placeholder="my_custom_skill" style={inputStyle} />
          </div>
          <div>
            <label style={{ fontSize: 11, color: "#8b949e", display: "block", marginBottom: 4 }}>Name</label>
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="My Custom Skill" style={inputStyle} />
          </div>
          <div>
            <label style={{ fontSize: 11, color: "#8b949e", display: "block", marginBottom: 4 }}>Description (short summary)</label>
            <input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="What this skill teaches the agent to do" style={inputStyle} />
          </div>
          <div>
            <label style={{ fontSize: 11, color: "#8b949e", display: "block", marginBottom: 4 }}>Tags (comma-separated)</label>
            <input value={tags} onChange={(e) => setTags(e.target.value)} placeholder="buildings, layout, custom" style={inputStyle} />
          </div>
          <div style={{ flex: 1 }}>
            <label style={{ fontSize: 11, color: "#8b949e", display: "block", marginBottom: 4 }}>
              Content (Markdown -- instructions for the AI agent)
            </label>
            <textarea
              value={content}
              onChange={(e) => setContent(e.target.value)}
              style={{
                ...inputStyle,
                height: 220,
                resize: "vertical",
                fontFamily: "ui-monospace, 'Cascadia Code', 'Source Code Pro', Menlo, monospace",
                lineHeight: 1.5,
              }}
            />
          </div>
          {error && <div style={{ fontSize: 11, color: "#f85149", padding: "2px 0" }}>{error}</div>}
        </div>

        <div
          style={{
            padding: "12px 20px",
            borderTop: "1px solid #21262d",
            display: "flex",
            gap: 8,
            justifyContent: "flex-end",
          }}
        >
          <button
            onClick={onClose}
            style={{
              padding: "6px 14px",
              fontSize: 12,
              background: "#21262d",
              border: "1px solid #30363d",
              borderRadius: 6,
              color: "#c9d1d9",
              cursor: "pointer",
            }}
          >
            Cancel
          </button>
          <button
            onClick={handleSave}
            disabled={saving}
            style={{
              padding: "6px 14px",
              fontSize: 12,
              background: "#238636",
              border: "1px solid #2ea043",
              borderRadius: 6,
              color: "#fff",
              cursor: saving ? "wait" : "pointer",
              opacity: saving ? 0.7 : 1,
              fontWeight: 600,
            }}
          >
            {saving ? "Saving..." : "Create Skill"}
          </button>
        </div>
      </div>
    </div>
  );
}

function ConfirmDeleteModal({ message, onConfirm, onCancel }) {
  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.8)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 10000,
      }}
      onClick={onCancel}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: 400,
          background: "#0d1117",
          border: "1px solid #30363d",
          borderRadius: 12,
          padding: 24,
        }}
      >
        <div style={{ fontSize: 14, color: "#e6edf3", fontWeight: 600, marginBottom: 8 }}>
          Confirm Delete
        </div>
        <div style={{ fontSize: 13, color: "#8b949e", lineHeight: 1.5, marginBottom: 20 }}>
          {message}
        </div>
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <button
            onClick={onCancel}
            style={{
              padding: "6px 14px",
              fontSize: 12,
              background: "#21262d",
              border: "1px solid #30363d",
              borderRadius: 6,
              color: "#c9d1d9",
              cursor: "pointer",
            }}
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            style={{
              padding: "6px 14px",
              fontSize: 12,
              background: "#da3633",
              border: "1px solid #da363366",
              borderRadius: 6,
              color: "#fff",
              cursor: "pointer",
              fontWeight: 600,
            }}
          >
            Delete
          </button>
        </div>
      </div>
    </div>
  );
}

function SkillsPage({ newlyAddedSkillIds = [], onMarkSkillSeen }) {
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

  const filtered = skills.filter((s) => {
    if (filter !== "all" && s.source !== filter) return false;
    if (!search.trim()) return true;
    const q = search.toLowerCase();
    return (
      s.name.toLowerCase().includes(q) ||
      s.description.toLowerCase().includes(q) ||
      s.tags.some((t) => t.toLowerCase().includes(q))
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
    <div
      style={{
        height: "100%",
        display: "flex",
        flexDirection: "column",
        background: "#0d1117",
      }}
    >
      <div
        style={{
          padding: "16px 24px",
          borderBottom: "1px solid #21262d",
          display: "flex",
          alignItems: "center",
          gap: 12,
        }}
      >
        <span style={{ fontSize: 24 }}>🛠️</span>
        <div>
          <div style={{ fontSize: 18, fontWeight: 700, color: "#e6edf3" }}>Skills</div>
          <div style={{ fontSize: 12, color: "#8b949e" }}>
            Browse, create, and manage skills that teach the AI agent new capabilities
          </div>
        </div>
        <button
          onClick={() => setShowCreate(true)}
          style={{
            marginLeft: "auto",
            padding: "6px 14px",
            fontSize: 12,
            background: "#238636",
            border: "1px solid #2ea043",
            borderRadius: 6,
            color: "#fff",
            cursor: "pointer",
            fontWeight: 600,
            display: "flex",
            alignItems: "center",
            gap: 6,
          }}
        >
          + Create Skill
        </button>
      </div>

      <div
        style={{
          padding: "10px 24px",
          borderBottom: "1px solid #21262d",
          display: "flex",
          gap: 10,
          alignItems: "center",
        }}
      >
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search skills by name, description, or tags..."
          style={{
            flex: 1,
            padding: "7px 12px",
            fontSize: 13,
            background: "#161b22",
            border: "1px solid #30363d",
            borderRadius: 6,
            color: "#e6edf3",
            outline: "none",
          }}
        />
        <div style={{ display: "flex", gap: 4 }}>
          {["all", "builtin", "custom"].map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              style={{
                padding: "5px 12px",
                fontSize: 11,
                borderRadius: 6,
                border: `1px solid ${filter === f ? "#58a6ff" : "#30363d"}`,
                background: filter === f ? "#1f6feb22" : "transparent",
                color: filter === f ? "#58a6ff" : "#8b949e",
                cursor: "pointer",
                textTransform: "capitalize",
              }}
            >
              {f}
            </button>
          ))}
        </div>
        <span style={{ fontSize: 12, color: "#484f58", whiteSpace: "nowrap" }}>
          {filtered.length} skill{filtered.length !== 1 ? "s" : ""}
        </span>
      </div>

      <div style={{ flex: 1, overflow: "auto", padding: 20 }}>
        {loading ? (
          <div style={{ textAlign: "center", padding: 60, color: "#8b949e" }}>Loading...</div>
        ) : filtered.length === 0 ? (
          <div style={{ textAlign: "center", padding: 60 }}>
            <div style={{ fontSize: 40, marginBottom: 12 }}>🛠️</div>
            <div style={{ fontSize: 16, color: "#e6edf3", fontWeight: 600, marginBottom: 8 }}>
              No skills found
            </div>
            <div style={{ fontSize: 13, color: "#8b949e" }}>
              {search ? "Try a different search term." : "Create a custom skill to get started."}
            </div>
          </div>
        ) : (
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))",
              gap: 16,
            }}
          >
            {filtered.map((s) => (
              <SkillPageCard
                key={s.id}
                skill={s}
                isNew={newlyAddedSkillIdSet.has(String(s?.id || "").trim())}
                onClick={() => handlePreview(s.id)}
              />
            ))}
          </div>
        )}
      </div>

      {previewSkill && (
        <SkillPageDetailModal
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


function ToolPageCard({ tool, onClick, busy, onToggleEnabled, onDelete, isNew = false }) {
  const successRate =
    tool.metrics?.usageCount > 0
      ? Math.round((tool.metrics.successCount / tool.metrics.usageCount) * 100)
      : null;

  return (
    <div
      onClick={onClick}
      style={{
        padding: "14px 16px",
        borderRadius: 10,
        border: "1px solid #21262d",
        background: "#161b22",
        cursor: "pointer",
        transition: "border-color 0.15s, transform 0.15s",
        display: "flex",
        flexDirection: "column",
        gap: 8,
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.borderColor = "#58a6ff";
        e.currentTarget.style.transform = "translateY(-2px)";
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.borderColor = "#21262d";
        e.currentTarget.style.transform = "translateY(0)";
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <div style={{ fontSize: 14, fontWeight: 600, color: "#e6edf3", flex: 1 }}>
          {tool.name || tool.id}
        </div>
        {isNew && (
          <span
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 5,
              fontSize: 9,
              padding: "2px 7px",
              borderRadius: 10,
              background: "#f8514922",
              color: "#f85149",
              border: "1px solid #f8514944",
              flexShrink: 0,
              fontWeight: 700,
              letterSpacing: 0.2,
            }}
          >
            <span
              style={{
                width: 6,
                height: 6,
                borderRadius: "50%",
                background: "#f85149",
                boxShadow: "0 0 6px #f85149",
              }}
            />
            NEW
          </span>
        )}
        <span
          style={{
            fontSize: 9,
            padding: "2px 7px",
            borderRadius: 10,
            background: tool.enabled ? "#23863622" : "transparent",
            color: tool.enabled ? "#3fb950" : "#8b949e",
            border: "1px solid #30363d",
            flexShrink: 0,
          }}
        >
          {tool.enabled ? "Enabled" : "Disabled"}
        </span>
      </div>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "1fr 1fr",
          gap: 4,
          marginTop: 4,
          paddingTop: 4,
          borderTop: "1px solid #21262d",
          fontSize: 10,
          color: "#8b949e",
        }}
      >
        <div>
          Template: <span style={{ color: "#c9d1d9" }}>{tool.template || "–"}</span>
        </div>
        <div>
          Primitive: <span style={{ color: "#c9d1d9" }}>{tool.primitive || "–"}</span>
        </div>
        <div>
          Usage: <span style={{ color: "#c9d1d9" }}>{tool.metrics?.usageCount || 0}</span>
        </div>
        <div>
          Success: <span style={{ color: "#c9d1d9" }}>{successRate == null ? "–" : `${successRate}%`}</span>
        </div>
      </div>

      <div style={{ display: "flex", gap: 8, marginTop: "auto", paddingTop: 6 }}>
        <button
          onClick={(e) => {
            e.stopPropagation();
            onToggleEnabled();
          }}
          disabled={busy}
          style={{
            padding: "5px 10px",
            fontSize: 11,
            borderRadius: 6,
            border: "1px solid #30363d",
            background: tool.enabled ? "#21262d" : "#1f6feb22",
            color: tool.enabled ? "#c9d1d9" : "#58a6ff",
            cursor: busy ? "wait" : "pointer",
          }}
        >
          {busy ? "Working…" : tool.enabled ? "Disable" : "Enable"}
        </button>
        <button
          onClick={(e) => {
            e.stopPropagation();
            onDelete();
          }}
          disabled={busy}
          style={{
            padding: "5px 10px",
            fontSize: 11,
            borderRadius: 6,
            border: "1px solid #da363366",
            background: "#da363322",
            color: "#f85149",
            cursor: busy ? "wait" : "pointer",
          }}
        >
          Delete
        </button>
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
        border: "1px solid #21262d",
        background: "#161b22",
        display: "flex",
        alignItems: "center",
        gap: 8,
        cursor: "pointer",
      }}
    >
      <span style={{ color: "#e6edf3", fontSize: 12, fontWeight: 600 }}>{tool.name}</span>
      <span
        style={{
          marginLeft: "auto",
          fontSize: 10,
          color: "#8b949e",
          border: "1px solid #30363d",
          borderRadius: 10,
          padding: "2px 7px",
        }}
      >
        Reference
      </span>
    </div>
  );
}

function ToolDetailModal({ tool, relatedSkills, busy, onClose, onToggleEnabled, onDelete, onOpenSkill, readOnly = false }) {
  const successRate =
    tool.metrics?.usageCount > 0
      ? Math.round((tool.metrics.successCount / tool.metrics.usageCount) * 100)
      : null;
  const schema = tool.paramsSchema || { type: "object", properties: {} };
  const schemaProps = schema.properties || {};
  const requiredKeys = Array.isArray(schema.required) ? schema.required : [];

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.8)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 9999,
      }}
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: "90%",
          maxWidth: 750,
          maxHeight: "85vh",
          background: "#0d1117",
          border: "1px solid #30363d",
          borderRadius: 12,
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
        }}
      >
        <div
          style={{
            padding: "14px 20px",
            borderBottom: "1px solid #21262d",
            display: "flex",
            alignItems: "center",
            gap: 10,
          }}
        >
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 18, fontWeight: 600, color: "#e6edf3" }}>{tool.name || tool.id}</div>
            <div style={{ fontSize: 11, color: "#8b949e", marginTop: 4 }}>
              <span style={{ color: "#79c0ff", fontFamily: "monospace" }}>{tool.mcpName}</span>
              <span
                style={{
                  marginLeft: 8,
                  padding: "2px 7px",
                  borderRadius: 10,
                  border: "1px solid #30363d",
                  background: readOnly ? "#1f6feb22" : tool.enabled ? "#23863622" : "transparent",
                  color: readOnly ? "#58a6ff" : tool.enabled ? "#3fb950" : "#8b949e",
                  fontSize: 9,
                }}
              >
                {readOnly ? "Static MCP" : tool.enabled ? "Enabled" : "Disabled"}
              </span>
            </div>
          </div>
          {!readOnly && (
            <button
              onClick={onDelete}
              disabled={busy}
              style={{
                padding: "5px 12px",
                fontSize: 11,
                background: "#da363322",
                border: "1px solid #da363366",
                borderRadius: 6,
                color: "#f85149",
                cursor: busy ? "wait" : "pointer",
              }}
            >
              Delete
            </button>
          )}
          <button
            onClick={onClose}
            style={{
              padding: "4px 10px",
              fontSize: 16,
              background: "transparent",
              border: "none",
              color: "#8b949e",
              cursor: "pointer",
            }}
          >
            ✕
          </button>
        </div>

        <div style={{ padding: "12px 20px", borderBottom: "1px solid #21262d" }}>
          <div style={{ fontSize: 13, color: "#c9d1d9", lineHeight: 1.5 }}>
            {tool.description || "No description"}
          </div>
          {!readOnly && (
            <div style={{ marginTop: 10, fontSize: 12, color: "#8b949e" }}>
              Related skills:{" "}
              <span style={{ color: "#c9d1d9" }}>{relatedSkills && relatedSkills.length > 0 ? relatedSkills.length : 0}</span>
            </div>
          )}
          {!readOnly && relatedSkills && relatedSkills.length > 0 && (
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 8 }}>
              {relatedSkills.map((skill) => (
                <button
                  key={skill.id || skill.name}
                  onClick={() => onOpenSkill && onOpenSkill(skill.id)}
                  style={{
                    fontSize: 10,
                    padding: "2px 7px",
                    borderRadius: 10,
                    background: "#1f6feb22",
                    color: "#58a6ff",
                    border: "1px solid #1f6feb44",
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
                color: "#8b949e",
              }}
            >
              <div>Template: <span style={{ color: "#c9d1d9" }}>{tool.template || "–"}</span></div>
              <div>Primitive: <span style={{ color: "#c9d1d9" }}>{tool.primitive || "–"}</span></div>
              <div>Usage: <span style={{ color: "#c9d1d9" }}>{tool.metrics?.usageCount || 0}</span></div>
              <div>Success: <span style={{ color: "#c9d1d9" }}>{successRate == null ? "–" : `${successRate}%`}</span></div>
            </div>
          )}

          <div
            style={{
              marginTop: 16,
              fontSize: 10,
              color: "#484f58",
              fontWeight: 600,
              textTransform: "uppercase",
              letterSpacing: 0.5,
            }}
          >
            How Claude Calls This Tool
          </div>
          <div style={{ marginTop: 8, fontSize: 12, color: "#8b949e", lineHeight: 1.5 }}>
            Use <span style={{ color: "#79c0ff", fontFamily: "monospace" }}>{tool.mcpName}</span> with an arguments object.
          </div>
          <div style={{ marginTop: 10, fontSize: 12, color: "#8b949e" }}>
            Required arguments:{" "}
            <span style={{ color: "#c9d1d9" }}>
              {requiredKeys.length > 0 ? requiredKeys.join(", ") : "none"}
            </span>
          </div>
          <div style={{ marginTop: 10, fontSize: 11, color: "#8b949e" }}>Input schema</div>
          <pre
            style={{
              margin: "6px 0 0",
              background: "#161b22",
              border: "1px solid #21262d",
              borderRadius: 8,
              padding: 12,
              fontSize: 11,
              color: "#c9d1d9",
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
          <div
            style={{
              padding: "12px 20px",
              borderTop: "1px solid #21262d",
              display: "flex",
              gap: 8,
              justifyContent: "flex-end",
            }}
          >
            <button
              onClick={onToggleEnabled}
              disabled={busy}
              style={{
                padding: "6px 14px",
                fontSize: 12,
                borderRadius: 6,
                border: "1px solid #30363d",
                background: tool.enabled ? "#21262d" : "#1f6feb22",
                color: tool.enabled ? "#c9d1d9" : "#58a6ff",
                cursor: busy ? "wait" : "pointer",
              }}
            >
              {busy ? "Working…" : tool.enabled ? "Disable" : "Enable"}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

function ToolsPage({ newlyAddedToolIds = [], onMarkToolSeen }) {
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
        ? skillsMeta.filter((s) => s && s.id && (s.source === "custom" || (s.tags || []).includes("learned")))
        : [];
      const detailedSkills = await Promise.all(
        candidateSkills.map(async (s) => {
          try {
            return await fetchSkillDetails(s.id);
          } catch {
            return s;
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
    const timer = setInterval(reload, 15000);
    return () => clearInterval(timer);
  }, [reload]);

  useEffect(() => {
    if (!previewTool) return;
    const next = tools.find((t) => t.id === previewTool.id) || null;
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
      const haystack = [
        tool.id,
        tool.name,
        tool.description,
        tool.mcpName,
        tool.template,
        tool.primitive,
      ]
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
      if (!arr.some((s) => s.id === skill.id)) arr.push(skill);
    };

    const extractToolIds = (text) => {
      const out = [];
      const re = /learned__([a-z0-9_]+)/gi;
      const src = String(text || "");
      let m;
      while ((m = re.exec(src)) !== null) out.push(m[1]);
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
          .map((d) => String(d || "").trim())
          .filter((d) => d.startsWith("learned__"))
          .map((d) => d.slice("learned__".length))),
      ]);
      for (const toolId of references) add(toolId, skillRef);
    }
    return map;
  }, [skillDetails]);

  const openRelatedSkill = async (skillId) => {
    if (!skillId) return;
    const existing = skillDetails.find((s) => s.id === skillId);
    if (existing && existing.content) {
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
    <div
      style={{
        height: "100%",
        display: "flex",
        flexDirection: "column",
        background: "#0d1117",
      }}
    >
      <div
        style={{
          padding: "16px 24px",
          borderBottom: "1px solid #21262d",
          display: "flex",
          alignItems: "center",
          gap: 12,
        }}
      >
        <span style={{ fontSize: 24 }}>🔧</span>
        <div>
          <div style={{ fontSize: 18, fontWeight: 700, color: "#e6edf3" }}>Tools</div>
          <div style={{ fontSize: 12, color: "#8b949e" }}>
            Static MCP tools for reference + learned tools you can manage
          </div>
        </div>
      </div>

      {error && (
        <div
          style={{
            padding: "8px 24px",
            borderBottom: "1px solid #21262d",
            color: "#f85149",
            fontSize: 12,
          }}
        >
          {error}
        </div>
      )}

      <div style={{ flex: 1, overflow: "auto", padding: 20 }}>
        <div
          style={{
            border: "1px solid #21262d",
            borderRadius: 10,
            marginBottom: 16,
            overflow: "hidden",
            background: "#0d1117",
          }}
        >
          <div
            style={{
              padding: "10px 12px",
              borderBottom: "1px solid #21262d",
              display: "flex",
              alignItems: "center",
              gap: 8,
              background: "#161b22",
            }}
          >
            <span style={{ fontSize: 14 }}>📚</span>
            <span style={{ fontSize: 13, fontWeight: 700, color: "#e6edf3" }}>
              Static MCP Tools (Reference)
            </span>
            <span style={{ marginLeft: "auto", fontSize: 11, color: "#8b949e" }}>
              {STATIC_MCP_TOOL_DEFS.length} tools
            </span>
          </div>
          <div
            style={{
              padding: 12,
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))",
              gap: 10,
            }}
          >
            {STATIC_MCP_TOOL_DEFS.map((tool) => (
              <div key={tool.id} onClick={() => setPreviewStaticTool(tool)}>
                <StaticToolRefCard tool={tool} />
              </div>
            ))}
          </div>
        </div>

        <div
          style={{
            border: "1px solid #21262d",
            borderRadius: 10,
            overflow: "hidden",
            background: "#0d1117",
          }}
        >
          <div
            style={{
              padding: "10px 12px",
              borderBottom: "1px solid #21262d",
              display: "flex",
              alignItems: "center",
              gap: 8,
              background: "#161b22",
            }}
          >
            <span style={{ fontSize: 14 }}>🧠</span>
            <span style={{ fontSize: 13, fontWeight: 700, color: "#e6edf3" }}>
              Dynamic Learned Tools
            </span>
            <span style={{ marginLeft: "auto", fontSize: 11, color: "#8b949e" }}>
              {filteredTools.length} tool{filteredTools.length !== 1 ? "s" : ""}
            </span>
          </div>
          <div
            style={{
              padding: 12,
              borderBottom: "1px solid #21262d",
              display: "flex",
              gap: 10,
              alignItems: "center",
            }}
          >
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search learned tools by name, template, primitive, or description..."
              style={{
                flex: 1,
                padding: "7px 12px",
                fontSize: 13,
                background: "#161b22",
                border: "1px solid #30363d",
                borderRadius: 6,
                color: "#e6edf3",
                outline: "none",
              }}
            />
          </div>
          <div style={{ padding: 12 }}>
            {loading ? (
              <div style={{ color: "#8b949e", padding: 20 }}>Loading…</div>
            ) : filteredTools.length === 0 ? (
              <div style={{ textAlign: "center", padding: 40 }}>
                <div style={{ fontSize: 32, marginBottom: 10 }}>🔧</div>
                <div style={{ fontSize: 15, color: "#e6edf3", fontWeight: 600, marginBottom: 6 }}>
                  No learned tools found
                </div>
                <div style={{ fontSize: 12, color: "#8b949e" }}>
                  {search.trim()
                    ? "Try a different search term."
                    : "No learned tools have been promoted yet."}
                </div>
              </div>
            ) : (
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))",
                  gap: 16,
                }}
              >
                {filteredTools.map((tool) => (
                  <ToolPageCard
                    key={tool.id}
                    tool={tool}
                    busy={busyToolId === tool.id}
                    isNew={newlyAddedToolIdSet.has(String(tool?.id || "").trim())}
                    onClick={() => {
                      if (typeof onMarkToolSeen === "function") {
                        onMarkToolSeen(String(tool?.id || "").trim());
                      }
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
          readOnly={true}
        />
      )}
      {previewSkill && (
        <SkillPageDetailModal
          skill={previewSkill}
          onClose={() => setPreviewSkill(null)}
        />
      )}
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

// ─── StatusDot ───────────────────────────────────────────────────────────────

function StatusDot({ label, active, activeColor, inactiveColor }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 11, color: "#8b949e" }}>
      <div
        style={{
          width: 6,
          height: 6,
          borderRadius: "50%",
          background: active ? activeColor : inactiveColor,
          boxShadow: active ? `0 0 4px ${activeColor}` : "none",
        }}
      />
      {label}
    </div>
  );
}

function ArtifactToastStack({ items }) {
  if (!Array.isArray(items) || items.length === 0) return null;

  return (
    <div
      style={{
        position: "absolute",
        top: 52,
        right: 16,
        zIndex: 1200,
        width: 320,
        display: "flex",
        flexDirection: "column",
        gap: 8,
        pointerEvents: "none",
      }}
    >
      {items.map((item) => {
        const isTool = item.kind === "tool";
        const accent = isTool ? "#f85149" : "#3fb950";
        const title = isTool ? "New Tool Learned" : "New Skill Learned";
        return (
          <div
            key={item.id}
            style={{
              minHeight: 40,
              borderRadius: 6,
              border: `1px solid ${accent}44`,
              borderLeft: `4px solid ${accent}`,
              background: "linear-gradient(90deg, #161b22 0%, #11161d 100%)",
              padding: "6px 10px",
              display: "flex",
              alignItems: "center",
              gap: 8,
              boxShadow: "0 8px 24px rgba(0,0,0,0.35)",
            }}
          >
            <div style={{ flex: 1, minWidth: 0 }}>
              <div
                style={{
                  fontSize: 10,
                  letterSpacing: 0.2,
                  textTransform: "uppercase",
                  color: accent,
                  lineHeight: 1.2,
                  fontWeight: 700,
                }}
              >
                {title}
              </div>
              <div
                style={{
                  marginTop: 2,
                  fontSize: 12,
                  color: "#e6edf3",
                  whiteSpace: "nowrap",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  lineHeight: 1.2,
                }}
                title={item.name}
              >
                {item.name}
              </div>
            </div>
            {item.extraCount > 0 && (
              <div
                style={{
                  flexShrink: 0,
                  fontSize: 11,
                  color: "#8b949e",
                  background: "#21262d",
                  border: "1px solid #30363d",
                  borderRadius: 999,
                  padding: "2px 8px",
                }}
              >
                +{item.extraCount}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ─── App ─────────────────────────────────────────────────────────────────────

function App() {
  const [health, setHealth] = useState(null);
  const [healthError, setHealthError] = useState(false);
  const [latestScreenshot, setLatestScreenshot] = useState(null);
  const [splitPct, setSplitPct] = useState(38);
  const [currentSessionId, setCurrentSessionId] = useState(null);
  const [contextRefreshKey, setContextRefreshKey] = useState(0);
  const [dragging, setDragging] = useState(false);
  const [rightPanel, setRightPanel] = useState("viewport");
  const [activePage, setActivePage] = useState("generate");
  const [chatRef, setChatRef] = useState(null);
  const [artifactUnread, setArtifactUnread] = useState({ skills: false, tools: false });
  const [artifactNewIds, setArtifactNewIds] = useState({ skills: [], tools: [] });
  const [artifactToasts, setArtifactToasts] = useState([]);
  const containerRef = React.useRef(null);
  const knownLearnedSkillIdsRef = useRef(new Set());
  const knownLearnedToolIdsRef = useRef(new Set());
  const artifactBootstrapRef = useRef(false);
  const artifactToastSeqRef = useRef(0);
  const artifactToastTimersRef = useRef(new Map());

  // Health check + fetch stable session
  useEffect(() => {
    fetchHealth()
      .then(setHealth)
      .catch(() => setHealthError(true));
    fetch(`${API_BASE}/session`).then(r => r.json()).then(d => {
      if (d.sessionId) setCurrentSessionId(d.sessionId);
    }).catch(() => {});
  }, []);

  const dismissArtifactToast = useCallback((id) => {
    if (!id) return;
    setArtifactToasts((prev) => prev.filter((item) => item.id !== id));
    const timerId = artifactToastTimersRef.current.get(id);
    if (timerId) {
      clearTimeout(timerId);
      artifactToastTimersRef.current.delete(id);
    }
  }, []);

  const pushArtifactToast = useCallback(
    (kind, name, extraCount = 0) => {
      const toastId = `artifact-toast-${++artifactToastSeqRef.current}-${Date.now()}`;
      const toast = {
        id: toastId,
        kind: kind === "tool" ? "tool" : "skill",
        name: String(name || "Unnamed"),
        extraCount: Math.max(0, Number(extraCount || 0)),
      };

      setArtifactToasts((prev) => {
        const next = [...prev, toast];
        const overflow = Math.max(0, next.length - EVOLUTION_TOAST_MAX);
        if (overflow > 0) {
          for (const dropped of next.slice(0, overflow)) {
            const tid = artifactToastTimersRef.current.get(dropped.id);
            if (tid) {
              clearTimeout(tid);
              artifactToastTimersRef.current.delete(dropped.id);
            }
          }
        }
        return next.slice(-EVOLUTION_TOAST_MAX);
      });

      const timerId = window.setTimeout(() => {
        dismissArtifactToast(toastId);
      }, EVOLUTION_TOAST_TTL_MS);
      artifactToastTimersRef.current.set(toastId, timerId);
    },
    [dismissArtifactToast]
  );

  useEffect(() => {
    return () => {
      for (const timerId of artifactToastTimersRef.current.values()) {
        clearTimeout(timerId);
      }
      artifactToastTimersRef.current.clear();
    };
  }, []);

  useEffect(() => {
    let cancelled = false;

    const syncLearnedArtifacts = async () => {
      try {
        const [skillsRaw, toolsRaw] = await Promise.all([fetchSkills(), fetchTools()]);
        if (cancelled) return;

        const learnedSkills = (Array.isArray(skillsRaw) ? skillsRaw : []).filter(isLearnedSkillMeta);
        const learnedTools = Array.isArray(toolsRaw) ? toolsRaw : [];

        const skillsById = new Map();
        for (const skill of learnedSkills) {
          const id = String(skill?.id || "").trim();
          if (!id) continue;
          skillsById.set(id, skill);
        }

        const toolsById = new Map();
        for (const tool of learnedTools) {
          const id = String(tool?.id || "").trim();
          if (!id) continue;
          toolsById.set(id, tool);
        }

        const newlyAddedSkills = [];
        const newlyAddedTools = [];

        for (const [id, skill] of skillsById.entries()) {
          if (!knownLearnedSkillIdsRef.current.has(id)) newlyAddedSkills.push(skill);
        }
        for (const [id, tool] of toolsById.entries()) {
          if (!knownLearnedToolIdsRef.current.has(id)) newlyAddedTools.push(tool);
        }

        knownLearnedSkillIdsRef.current = new Set(skillsById.keys());
        knownLearnedToolIdsRef.current = new Set(toolsById.keys());

        if (!artifactBootstrapRef.current) {
          artifactBootstrapRef.current = true;
          return;
        }

        const newlyAddedSkillIds = newlyAddedSkills
          .map((skill) => String(skill?.id || "").trim())
          .filter(Boolean);
        const newlyAddedToolIds = newlyAddedTools
          .map((tool) => String(tool?.id || "").trim())
          .filter(Boolean);

        setArtifactNewIds((prev) => {
          const nextSkills = (Array.isArray(prev?.skills) ? prev.skills : []).filter((id) =>
            skillsById.has(String(id || "").trim())
          );
          const nextTools = (Array.isArray(prev?.tools) ? prev.tools : []).filter((id) =>
            toolsById.has(String(id || "").trim())
          );

          for (const id of newlyAddedSkillIds) {
            if (!nextSkills.includes(id)) nextSkills.push(id);
          }
          for (const id of newlyAddedToolIds) {
            if (!nextTools.includes(id)) nextTools.push(id);
          }

          const unchanged =
            nextSkills.length === (prev?.skills || []).length &&
            nextTools.length === (prev?.tools || []).length &&
            nextSkills.every((id, idx) => id === (prev?.skills || [])[idx]) &&
            nextTools.every((id, idx) => id === (prev?.tools || [])[idx]);

          return unchanged ? prev : { skills: nextSkills, tools: nextTools };
        });

        if (newlyAddedSkills.length > 0) {
          if (activePage !== "skills") {
            setArtifactUnread((prev) => ({ ...prev, skills: true }));
          }
          const latest = newlyAddedSkills[newlyAddedSkills.length - 1];
          const skillName = String(latest?.name || latest?.id || "Unnamed skill");
          pushArtifactToast("skill", skillName, newlyAddedSkills.length - 1);
        }

        if (newlyAddedTools.length > 0) {
          if (activePage !== "tools") {
            setArtifactUnread((prev) => ({ ...prev, tools: true }));
          }
          const latest = newlyAddedTools[newlyAddedTools.length - 1];
          const toolName = String(latest?.name || latest?.id || "Unnamed tool");
          pushArtifactToast("tool", toolName, newlyAddedTools.length - 1);
        }
      } catch {
        // Ignore transient polling errors.
      }
    };

    syncLearnedArtifacts();
    const intervalId = window.setInterval(syncLearnedArtifacts, EVOLUTION_ARTIFACT_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
  }, [activePage, pushArtifactToast]);

  // Drag handler for split pane
  const handleMouseDown = (e) => {
    e.preventDefault();
    setDragging(true);
  };

  useEffect(() => {
    if (!dragging) return;
    const onMove = (e) => {
      if (!containerRef.current) return;
      const rect = containerRef.current.getBoundingClientRect();
      const pct = ((e.clientX - rect.left) / rect.width) * 100;
      setSplitPct(Math.max(25, Math.min(75, pct)));
    };
    const onUp = () => setDragging(false);
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [dragging]);

  const handleNavClick = useCallback((id) => {
    setActivePage(id);
    if (id === "skills") {
      setArtifactUnread((prev) => (prev.skills ? { ...prev, skills: false } : prev));
    } else if (id === "tools") {
      setArtifactUnread((prev) => (prev.tools ? { ...prev, tools: false } : prev));
    }
  }, []);

  const markSkillArtifactSeen = useCallback((skillId) => {
    const normalizedId = String(skillId || "").trim();
    if (!normalizedId) return;
    setArtifactNewIds((prev) => ({
      ...prev,
      skills: (Array.isArray(prev?.skills) ? prev.skills : []).filter((id) => id !== normalizedId),
    }));
  }, []);

  const markToolArtifactSeen = useCallback((toolId) => {
    const normalizedId = String(toolId || "").trim();
    if (!normalizedId) return;
    setArtifactNewIds((prev) => ({
      ...prev,
      tools: (Array.isArray(prev?.tools) ? prev.tools : []).filter((id) => id !== normalizedId),
    }));
  }, []);

  const NAV_ITEMS = [
    { id: "generate", label: "Generate", icon: "💬" },
    { id: "context", label: "Context", icon: "🗺️" },
    { id: "agent", label: "Agent", icon: "🤖" },
    { id: "arena", label: "Arena", icon: "⚔️" },
    { id: "gallery", label: "Gallery", icon: "🖼️" },
    { id: "skills", label: "Skills", icon: "🛠️" },
    { id: "tools", label: "Tools", icon: "🔧" },
    { id: "leaderboard", label: "Leaderboard", icon: "🏆" },
  ];
  const SPLIT_PAGES = ["generate", "context", "agent"];

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        height: "100vh",
        background: "#0d1117",
        overflow: "hidden",
        position: "relative",
      }}
    >
      {/* Top nav bar */}
      <div
        style={{
          height: 46,
          background: "linear-gradient(135deg, #161b22 0%, #1c2333 100%)",
          borderBottom: "1px solid #21262d",
          display: "flex",
          alignItems: "center",
          padding: "0 20px",
          gap: 14,
          flexShrink: 0,
          userSelect: "none",
        }}
      >
        {/* Logo */}
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <img src="/logo.png" style={{ width: 24, height: 24 }} />
          <span
            style={{
              fontWeight: 700,
              fontSize: 16,
              color: "#e6edf3",
              letterSpacing: -0.3,
            }}
          >
            SimWorld Studio
          </span>
        </div>

        {/* Main nav */}
        <div style={{ display: "flex", gap: 2, marginLeft: 16 }}>
          {NAV_ITEMS.map(({ id, label, icon }) => (
            <button
              key={id}
              onClick={() => handleNavClick(id)}
              style={{
                padding: "4px 12px",
                fontSize: 12,
                borderRadius: 4,
                border: `1px solid ${activePage === id ? "#1f6feb" : "transparent"}`,
                background: activePage === id ? "#1f3a5f" : "transparent",
                color: activePage === id ? "#58a6ff" : "#8b949e",
                cursor: "pointer",
                display: "flex",
                alignItems: "center",
                gap: 5,
                fontWeight: activePage === id ? 600 : 400,
              }}
            >
              <span style={{ fontSize: 13 }}>{icon}</span>
              <span>{label}</span>
              {((id === "skills" && artifactUnread.skills) || (id === "tools" && artifactUnread.tools)) && (
                <span
                  style={{
                    width: 7,
                    height: 7,
                    borderRadius: "50%",
                    background: "#f85149",
                    boxShadow: "0 0 6px #f85149",
                    marginLeft: 3,
                    flexShrink: 0,
                  }}
                />
              )}
            </button>
          ))}
        </div>

        {/* Right panel tabs (generate mode only) */}
        {SPLIT_PAGES.includes(activePage) && activePage === "generate" && (
          <div
            style={{
              display: "flex",
              gap: 2,
              marginLeft: 16,
              paddingLeft: 16,
              borderLeft: "1px solid #21262d",
            }}
          >
            {[
              { id: "viewport", label: "Viewport" },
              { id: "assets", label: "Assets" },
              { id: "scenes", label: "Scenes" },
            ].map(({ id, label }) => (
              <button
                key={id}
                onClick={() => setRightPanel(id)}
                style={{
                  padding: "3px 10px",
                  fontSize: 11,
                  borderRadius: 4,
                  border: `1px solid ${rightPanel === id ? "#1f6feb" : "#30363d"}`,
                  background: rightPanel === id ? "#1f3a5f" : "transparent",
                  color: rightPanel === id ? "#58a6ff" : "#8b949e",
                  cursor: "pointer",
                }}
              >
                {label}
              </button>
            ))}
          </div>
        )}

        {/* Status indicators */}
        <div
          style={{
            marginLeft: "auto",
            display: "flex",
            alignItems: "center",
            gap: 14,
          }}
        >
          {health && (
            <>
              <StatusDot
                label="UE Engine"
                active={health.ueConnected}
                activeColor="#3fb950"
                inactiveColor="#f85149"
              />
              <StatusDot
                label="MCP Server"
                active={health.mcpConnected}
                activeColor="#3fb950"
                inactiveColor="#f85149"
              />
              <StatusDot
                label="Claude Code"
                active={true}
                activeColor="#3fb950"
                inactiveColor="#8b949e"
              />
            </>
          )}
          {healthError && (
            <span style={{ fontSize: 11, color: "#f85149" }}>Backend unreachable</span>
          )}
          {!health && !healthError && (
            <span style={{ fontSize: 11, color: "#656d76" }}>Connecting...</span>
          )}
        </div>
      </div>

      <ArtifactToastStack items={artifactToasts} />

      {/* Content */}
      <div
        ref={containerRef}
        style={{
          flex: 1,
          display: SPLIT_PAGES.includes(activePage) ? "flex" : "none",
          overflow: "hidden",
          cursor: dragging ? "col-resize" : "default",
        }}
      >
        {/* Left panel — content depends on active page */}
        <div
          style={{
            width: `${splitPct}%`,
            minWidth: 0,
            overflow: "hidden",
            display: "flex",
            flexDirection: "column",
          }}
        >
          <div style={{ display: activePage === "generate" ? "flex" : "none", flexDirection: "column", flex: 1, overflow: "hidden" }}>
            <ChatPanel
              onScreenshotUpdate={(url) => setLatestScreenshot(url)}
              onRef={setChatRef}
              onSessionChange={setCurrentSessionId}
              onChatDone={() => setContextRefreshKey((k) => k + 1)}
            />
          </div>
          <div style={{ display: activePage === "context" ? "flex" : "none", flexDirection: "column", flex: 1, overflow: "hidden" }}>
            <ContextPanel sessionId={currentSessionId} refreshKey={contextRefreshKey} />
          </div>
          <div style={{ display: activePage === "agent" ? "flex" : "none", flexDirection: "column", flex: 1, overflow: "hidden" }}>
            <AgentPanel sessionId={currentSessionId} />
          </div>
        </div>

        {/* Divider */}
        <div
          onMouseDown={handleMouseDown}
          style={{
            width: 4,
            background: dragging ? "#1f6feb" : "#21262d",
            cursor: "col-resize",
            flexShrink: 0,
            transition: "background 0.15s",
            position: "relative",
            zIndex: 10,
          }}
          title="Drag to resize"
        >
          <div
            style={{
              position: "absolute",
              top: "50%",
              left: "50%",
              transform: "translate(-50%, -50%)",
              display: "flex",
              flexDirection: "column",
              gap: 3,
            }}
          >
            {[0, 1, 2].map((i) => (
              <div
                key={i}
                style={{
                  width: 3,
                  height: 3,
                  borderRadius: "50%",
                  background: dragging ? "#58a6ff" : "#30363d",
                }}
              />
            ))}
          </div>
        </div>

        {/* Right panel */}
        <div style={{ flex: 1, minWidth: 0, overflow: "hidden" }}>
          {rightPanel === "viewport" && (
            <ViewportPanel latestScreenshot={latestScreenshot} />
          )}
          {rightPanel === "assets" && (
            <AssetBrowser
              onInsert={(id) => chatRef?.insertText(`Use asset: ${id}`)}
            />
          )}
          {rightPanel === "scenes" && (
            <SceneManager
              onLoadScene={(scene) => chatRef?.loadScene(scene)}
              currentSessionId={currentSessionId}
            />
          )}
        </div>
      </div>

      <div
        style={{
          flex: SPLIT_PAGES.includes(activePage) ? 0 : 1,
          overflow: "hidden",
          display: SPLIT_PAGES.includes(activePage) ? "none" : "block",
        }}
      >
        {activePage === "arena" && <ArenaPage />}
        {activePage === "skills" && (
          <SkillsPage
            newlyAddedSkillIds={artifactNewIds.skills}
            onMarkSkillSeen={markSkillArtifactSeen}
          />
        )}
        {activePage === "tools" && (
          <ToolsPage
            newlyAddedToolIds={artifactNewIds.tools}
            onMarkToolSeen={markToolArtifactSeen}
          />
        )}
        {activePage === "leaderboard" && <LeaderboardPage />}
        {activePage === "gallery" && <GalleryPage />}
      </div>
    </div>
  );
}

export default App;

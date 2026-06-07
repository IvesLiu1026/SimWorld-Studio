import React, { useState, useEffect, useRef, useCallback, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { API_BASE } from "./api/client.js";
import { fetchSkills, fetchTools } from "./api/appApi.js";
import { fetchCodingAgents, fetchHealth, fetchSession, studioQueryKeys } from "./api/studioApi.js";
import AgentAggregatePanelTabs from "./features/agents/AgentAggregatePanelTabs.jsx";
import AgentPanel from "./features/agents/AgentPanel.jsx";
import { DEFAULT_CODING_AGENTS } from "./features/agents/codingAgents.js";
import ArenaPage from "./features/arena/ArenaPage.jsx";
import AssetBrowser from "./features/assets/AssetBrowser.jsx";
import ChatPanel from "./features/chat/ChatPanel.jsx";
import CurriculumBuilderPanel from "./features/coevolution/CurriculumBuilderPanel.jsx";
import ContextPanel from "./features/context/ContextPanel.jsx";
import RoundInspectorPanel from "./features/coevolution/RoundInspectorPanel.jsx";
import LibraryPage from "./features/library/LibraryPage.jsx";
import ResultsPage from "./features/results/ResultsPage.jsx";
import CodingVerifierPanel from "./features/scene/CodingVerifierPanel.jsx";
import SceneInspectorPanel from "./features/scene/SceneInspectorPanel.jsx";
import SceneManager from "./features/scenes/SceneManager.jsx";
import ArtifactToastStack from "./features/studio/ArtifactToastStack.jsx";
import SessionGateModals from "./features/studio/SessionGateModals.jsx";
import SettingsModal from "./features/studio/SettingsModal.jsx";
import StudioTopbar from "./features/studio/StudioTopbar.jsx";
import { ArtifactChain } from "./features/studio/pipeline.jsx";
import TaskGenPanel from "./features/tasks/TaskGenPanel.jsx";
import TaskInspectorPanel from "./features/tasks/TaskInspectorPanel.jsx";
import TrainingConfigPanel from "./features/training/TrainingConfigPanel.jsx";
import TrainingMonitorPanel from "./features/training/TrainingMonitorPanel.jsx";
import ViewportPanel from "./features/viewport/ViewportPanel.jsx";
import { useStudioStore } from "./state/studioStore.js";
import {
  PollProvider,
  useSync,
} from "./state/pollContext.jsx";
import { clearSessionToken, useSession } from "./state/useSession.js";
import { CATEGORY_ICONS, ICONS } from "./components/ui/icons.jsx";

// ─── Constants ───────────────────────────────────────────────────────────────

const EVOLUTION_ARTIFACT_POLL_MS = 5000;
const EVOLUTION_TOAST_MAX = 2;
const EVOLUTION_TOAST_TTL_MS = 5200;

// ─── Utility ─────────────────────────────────────────────────────────────────

function isLearnedSkillMeta(skill) {
  if (!skill || typeof skill !== "object") return false;
  const source = String(skill.source || "").toLowerCase();
  const tags = Array.isArray(skill.tags) ? skill.tags : [];
  return source === "custom" && tags.includes("learned");
}

// ─── App ─────────────────────────────────────────────────────────────────────

function App() {
  const uiTheme = useStudioStore((state) => state.uiTheme);
  const setUiTheme = useStudioStore((state) => state.setUiTheme);
  const studioMode = useStudioStore((state) => state.studioMode);
  const setStudioMode = useStudioStore((state) => state.setStudioMode);
  const topSection = useStudioStore((state) => state.topSection);
  const setTopSection = useStudioStore((state) => state.setTopSection);
  const showSettings = useStudioStore((state) => state.showSettings);
  const setShowSettings = useStudioStore((state) => state.setShowSettings);
  const codingAgent = useStudioStore((state) => state.codingAgent);
  const setCodingAgent = useStudioStore((state) => state.setCodingAgent);
  const codingModel = useStudioStore((state) => state.codingModel);
  const setCodingModel = useStudioStore((state) => state.setCodingModel);

  const healthQuery = useQuery({
    queryKey: studioQueryKeys.health,
    queryFn: fetchHealth,
    refetchInterval: 5000,
  });
  const health = healthQuery.data || null;

  const codingAgentsQuery = useQuery({
    queryKey: studioQueryKeys.codingAgents,
    queryFn: fetchCodingAgents,
    staleTime: 60_000,
  });
  const codingAgents =
    codingAgentsQuery.data?.agents && Object.keys(codingAgentsQuery.data.agents).length
      ? codingAgentsQuery.data.agents
      : DEFAULT_CODING_AGENTS;
  const handleCodingAgentChange = useCallback(
    (agentId) => setCodingAgent(agentId, codingAgents),
    [codingAgents, setCodingAgent]
  );

  // Artifact chain — tracks what's been produced
  const [artifacts, setArtifacts] = useState({
    scene: null, task: null, training: null, coevolve: null,
  });

  useEffect(() => {
    const themeMap = { dark: "", light: "light" };
    document.documentElement.setAttribute("data-theme", themeMap[uiTheme] ?? "");
  }, [uiTheme]);

  // Column visibility
  const showLeft  = topSection === "studio";
  const showRight = topSection === "studio";

  // One panel per side per mode (clean 1:1 mapping from the plan)
  // Scene     → L: Intent+SimCoder (ChatPanel)     R: Scene Inspector (SceneInspectorPanel)
  // Task      → L: Task Builder (TaskGenPanel)      R: Task Inspector  (TaskInspectorPanel)
  // Training  → L: Training Config (TrainingConfig) R: Agent Monitor   (AgentPanel)
  // Co-evolve → L: Curriculum Builder              R: Round Inspector (RoundInspector)
  const LEFT_PANEL  = { scene:"chat",     task:"taskgen",   training:"trainconfig", coevolve:"curriculum" };
  const RIGHT_PANEL = { scene:"sceneinsp",task:"taskinsp",  training:"agentmonitor",coevolve:"roundinsp"  };
  const leftPanel  = LEFT_PANEL[studioMode]  || "chat";
  const rightPanel2= RIGHT_PANEL[studioMode] || "sceneinsp";

  const [latestScreenshot, setLatestScreenshot] = useState(null);
  const [splitPct, setSplitPct] = useState(38);
  const [currentSessionId, setCurrentSessionId] = useState(null);
  const [contextRefreshKey, setContextRefreshKey] = useState(0);
  const [dragging, setDragging] = useState(false);
  const [rightPanel, setRightPanel] = useState("viewport");
  // activePage kept for compatibility with drawer/panel refs; main nav uses topSection+studioMode
  const activePage = topSection === "studio" ? "generate" : topSection;
  const [chatRef, setChatRef] = useState(null);
  const [leftTab,    setLeftTab]    = useState("chat");
  const [rightTab,   setRightTab]   = useState("agent");
  const [colLeft,    setColLeft]    = useState(Math.round(window.innerWidth * 0.28));  // ~3/10
  const [colRight,   setColRight]   = useState(Math.round(window.innerWidth * 0.28)); // ~3/10
  const [commHeight,      setCommHeight]      = useState(200); // left bottom (Verifier)
  const [rightBottomH,   setRightBottomH]    = useState(200); // right bottom (Statistics)

  const leftColRef  = useRef(null);
  const rightColRef = useRef(null);

  // Row-resize: snapshot current panel height at mousedown, compute absolute on move
  // Pattern: startPanelH + (startY - currentY) tracks the mouse 1:1
  const makeRowResize = useCallback((currentH, setH) => (e) => {
    e.preventDefault();
    const startY = e.clientY;
    const startPanelH = currentH; // snapshot — does NOT change during drag
    const onMove = (ev) => {
      const newH = startPanelH + (startY - ev.clientY); // up = bigger bottom
      setH(Math.max(80, Math.min(600, newH)));
    };
    const onUp = () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  }, []);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerTab,  setDrawerTab]  = useState("assets");
  const [drawerH,    setDrawerH]    = useState(200);   // px when open
  const colResizingLeft  = useRef(false);
  const colResizingRight = useRef(false);
  const colResizeStart   = useRef({ x:0, colLeft:390, colRight:360 });
  const drawerResizing   = useRef(false);
  const drawerResizeStart = useRef({ y:0, h:200 });
  const layoutRef        = useRef(null);
  const { session, poolFull, secsLeft, expired, warningSoon } = useSession();
  const syncStatus = useSync();
  const [artifactUnread, setArtifactUnread] = useState({ skills: false, tools: false });
  const [artifactNewIds, setArtifactNewIds] = useState({ skills: [], tools: [] });
  const [artifactToasts, setArtifactToasts] = useState([]);
  const containerRef = React.useRef(null);
  const knownLearnedSkillIdsRef = useRef(new Set());
  const knownLearnedToolIdsRef = useRef(new Set());
  const artifactBootstrapRef = useRef(false);
  const artifactToastSeqRef = useRef(0);
  const artifactToastTimersRef = useRef(new Map());
  const sessionQuery = useQuery({
    queryKey: studioQueryKeys.session,
    queryFn: fetchSession,
    staleTime: 60_000,
  });

  useEffect(() => {
    if (sessionQuery.data?.sessionId) setCurrentSessionId(sessionQuery.data.sessionId);
  }, [sessionQuery.data?.sessionId]);

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

    // Delay initial sync to avoid connection saturation at startup
    const startTimer = setTimeout(syncLearnedArtifacts, 10000);
    const intervalId = window.setInterval(syncLearnedArtifacts, 60000);
    return () => {
      cancelled = true;
      clearTimeout(startTimer);
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

  // ── Column resize drag handlers ────────────────────────────────────────────
  const startColResize = useCallback((side) => (e) => {
    e.preventDefault();
    if (side === "left") {
      colResizingLeft.current = true;
      colResizeStart.current = { x: e.clientX, colLeft, colRight };
    } else {
      colResizingRight.current = true;
      colResizeStart.current = { x: e.clientX, colLeft, colRight };
    }
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";

    const onMove = (ev) => {
      const dx = ev.clientX - colResizeStart.current.x;
      if (colResizingLeft.current) {
        setColLeft(Math.max(280, Math.min(600, colResizeStart.current.colLeft + dx)));
      } else if (colResizingRight.current) {
        setColRight(Math.max(260, Math.min(560, colResizeStart.current.colRight - dx)));
      }
    };
    const onUp = () => {
      colResizingLeft.current  = false;
      colResizingRight.current = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup",   onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup",   onUp);
  }, [colLeft, colRight]);

  // ── Drawer resize (drag top edge up/down) ──────────────────────────────────
  const startDrawerResize = useCallback((e) => {
    e.preventDefault();
    drawerResizing.current = true;
    drawerResizeStart.current = { y: e.clientY, h: drawerH };
    document.body.style.cursor = "row-resize";
    document.body.style.userSelect = "none";
    // Track pending height in a ref — only commit to state on mouseup
    // This prevents iframe resize (and UE data-channel "cannot send yet" logs) on every mousemove
    const pendingH = { value: drawerH };
    const onMove = (ev) => {
      if (!drawerResizing.current) return;
      const delta = drawerResizeStart.current.y - ev.clientY;
      pendingH.value = Math.max(80, Math.min(600, drawerResizeStart.current.h + delta));
      // Update only the drag-handle visual, not the full React state
      const handle = document.querySelector('.sw-drawer .sw-drawer-handle-preview');
      if (handle) handle.style.transform = `translateY(${-delta}px)`;
    };
    const onUp = () => {
      drawerResizing.current = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      // Commit size only on mouseup — avoids continuous React re-renders + iframe resize
      setDrawerH(pendingH.value);
      if (pendingH.value > 50) setDrawerOpen(true);
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  }, [drawerH, drawerOpen]);

  const handleNavClick = useCallback((id) => {
    // kept for compatibility with any remaining callers
    if (id === "generate") setTopSection("studio");
    else if (id === "skills" || id === "tools" || id === "arena") { setTopSection("library"); }
    else if (id === "gallery" || id === "leaderboard") { setTopSection("results"); }
    if (id === "skills") setArtifactUnread(p => p.skills ? { ...p, skills: false } : p);
    if (id === "tools")  setArtifactUnread(p => p.tools  ? { ...p, tools:  false } : p);
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

  // Open a saved scene (.umap) from the Results gallery → switch to Scene Generation and
  // load it into the live viewport (reuses /api/load-map + the viewport auto-reconnect).
  const openSavedScene = React.useCallback(async (path) => {
    setTopSection("studio");
    setStudioMode("scene");
    try {
      await fetch(`${API_BASE}/load-map`, { method:"POST", headers:{ "Content-Type":"application/json" }, body: JSON.stringify({ path }) });
      setTimeout(() => window.dispatchEvent(new Event("sw-reconnect-stream")), 1500);
    } catch {}
  }, []);

  return (
    <PollProvider>
    <div style={{ display:"flex", flexDirection:"column", height:"100vh", background:"var(--bg)", overflow:"hidden", padding:"8px", gap:8, position:"relative" }}>

      <SessionGateModals
        expired={expired}
        icons={ICONS}
        onRetry={() => window.location.reload()}
        onStartNewSession={() => {
          clearSessionToken();
          window.location.reload();
        }}
        poolFull={poolFull}
      />

      <StudioTopbar
        artifactUnread={artifactUnread}
        codingAgent={codingAgent}
        codingAgents={codingAgents}
        codingModel={codingModel}
        health={health}
        icons={ICONS}
        onCodingAgentChange={handleCodingAgentChange}
        onCodingModelChange={setCodingModel}
        onSettingsOpen={() => setShowSettings(true)}
        onStudioModeChange={setStudioMode}
        onTopSectionChange={setTopSection}
        secsLeft={secsLeft}
        session={session}
        studioMode={studioMode}
        syncStatus={syncStatus}
        topSection={topSection}
        warningSoon={warningSoon}
      />

      {/* Settings modal */}
      {showSettings && (
        <SettingsModal
          icons={ICONS}
          uiTheme={uiTheme}
          onThemeChange={setUiTheme}
          layoutMode={studioMode}
          onLayoutMode={setStudioMode}
          onClose={() => setShowSettings(false)}
        />
      )}

      <ArtifactToastStack items={artifactToasts} />

      {/* ══ ARTIFACT CHAIN ══ */}
      {topSection === "studio" && (
        <ArtifactChain
          artifacts={artifacts}
          activeMode={studioMode}
          onSelect={m => setStudioMode(m)}
          icons={ICONS}
        />
      )}

      {/* ══ 3-COLUMN RESIZABLE STUDIO LAYOUT ══ */}
      {topSection === "studio" && (
      <div ref={layoutRef} style={{
        flex: 1, display:"flex", overflow:"hidden", minHeight:0, gap:5, padding:"4px 0",
      }}>
        {/* ── LEFT PANEL ── */}
        {showLeft && <div ref={leftColRef} style={{
          width: colLeft, minWidth:260, maxWidth:640, flexShrink:0,
          display:"flex", flexDirection:"column", gap:0, overflow:"visible", padding:"0 4px", margin:"0 -4px",
        }}>
          {/* ── Left panel content — driven by studioMode ── */}
          <div className="sw-panel-card" style={{
            flex:1, minHeight:0, borderRadius:12, border:"1px solid var(--line)",
            display:"flex", flexDirection:"column", overflow:"hidden", background:"var(--panel)",
          }}>
            <div className="sw-panel-header">
              <span className="sw-section-title">
                <span className="sw-num-chip" style={{ background:"var(--ink-3)" }}>
                  {leftPanel === "chat"       ? ICONS.chat(11)
                  : leftPanel === "taskgen"   ? ICONS.target(11)
                  : leftPanel === "trainconfig"? ICONS.activity(11)
                  :                             ICONS.refresh(11)}
                </span>
                {leftPanel === "chat"        ? "Intent + SimCoder"
                : leftPanel === "taskgen"    ? "Task Builder"
                : leftPanel === "trainconfig"? "Training Config"
                :                              "Curriculum Builder"}
              </span>
            </div>
            <div style={{ flex:1, overflow:"hidden", display:"flex", flexDirection:"column", minHeight:0 }}>
              {leftPanel === "chat" && (
                <ChatPanel
                  onScreenshotUpdate={url => setLatestScreenshot(url)}
                  onRef={setChatRef}
                  onSessionChange={setCurrentSessionId}
                  onChatDone={() => setContextRefreshKey(k => k + 1)}
                  codingAgent={codingAgent}
                  codingModel={codingModel}
                  icons={ICONS}
                />
              )}
              {leftPanel === "taskgen"    && <TaskGenPanel icons={ICONS} sessionId={currentSessionId} />}
              {leftPanel === "trainconfig"&& <TrainingConfigPanel icons={ICONS} sessionId={currentSessionId} />}
              {leftPanel === "curriculum" && <CurriculumBuilderPanel icons={ICONS} sessionId={currentSessionId} />}
            </div>
          </div>
        </div>}

        {/* ── Resize handle left ── */}
        {showLeft && <div className="sw-resize-col" onMouseDown={startColResize("left")} />}

        {/* ── CENTER: Viewport + drawer ── */}
        <div style={{ flex:1, minWidth:320, display:"flex", flexDirection:"column", gap:8, overflow:"visible", padding:"0 3px", margin:"0 -3px" }}>

          {/* UE Viewport card */}
          <div className="sw-panel-card viewport-card-shell">
            <ViewportPanel icons={ICONS} latestScreenshot={latestScreenshot} />
          </div>

          {/* Drawer: Assets / Scenes / Context / Tools */}
          <div className={`sw-drawer${drawerOpen ? "" : " collapsed"}`}
            style={{ height: drawerOpen ? drawerH : 36 }}>
            {/* Drawer drag handle — drag up/down to resize */}
            <div
              onMouseDown={startDrawerResize}
              style={{
                height:6, background:"transparent", cursor:"row-resize", flexShrink:0,
                display:"flex", alignItems:"center", justifyContent:"center",
              }}
              title="Drag to resize drawer"
            >
              <div style={{ width:32, height:2, borderRadius:2, background:"var(--line)", transition:"background .15s" }}
                onMouseEnter={e => e.currentTarget.style.background = "var(--blue)"}
                onMouseLeave={e => e.currentTarget.style.background = "var(--line)"}
              />
            </div>
            {/* Drawer header — always visible */}
            <div className="sw-drawer-header" onClick={() => setDrawerOpen(o => !o)}>
              <span style={{ fontSize:11, color:"var(--ink-3)", marginRight:4 }}>
                {drawerOpen ? ICONS.chevronDown?.(10) : ICONS.folder(10)}
              </span>
              <span style={{ fontSize:12, fontWeight:700, color:"var(--ink-2)" }}>
                {drawerOpen ? drawerTab.charAt(0).toUpperCase()+drawerTab.slice(1) : "Build Timeline — Assets / Scenes / Context"}
              </span>
              <div style={{ flex:1 }} />
              {drawerOpen && (
                <div style={{ display:"flex", gap:2 }}>
                  {(studioMode === "scene"
                    ? [{ id:"assets",  label:"Assets" },{ id:"scenes",  label:"Scene Versions" },{ id:"context", label:"Tool Calls" }]
                    : studioMode === "task"
                    ? [{ id:"assets",  label:"Task Sets" },{ id:"scenes",  label:"Episodes" },{ id:"context", label:"Validation" }]
                    : studioMode === "training"
                    ? [{ id:"assets",  label:"Episodes" },{ id:"scenes",  label:"Trajectories" },{ id:"context", label:"Metrics" }]
                    : [{ id:"assets",  label:"Rounds" },{ id:"scenes",  label:"Difficulty" },{ id:"context", label:"Rules" }]
                  ).map(t => (
                    <button key={t.id}
                      className={`sw-tab-btn${drawerTab===t.id?" active":""}`}
                      onClick={e => { e.stopPropagation(); setDrawerTab(t.id); }}
                      style={{ fontSize:11, padding:"2px 8px" }}
                    >{t.label}</button>
                  ))}
                  {/* Height adjusters */}
                  {[150,220,320].map(h => (
                    <button key={h} onClick={e=>{ e.stopPropagation(); setDrawerH(h); }}
                      style={{ fontSize:12, padding:"2px 6px", borderRadius:5, border:"1px solid var(--line)",
                        background: drawerH===h?"var(--blue-soft)":"transparent",
                        color: drawerH===h?"var(--blue)":"var(--ink-3)", cursor:"pointer" }}>
                      {h === 150 ? "S" : h === 220 ? "M" : "L"}
                    </button>
                  ))}
                </div>
              )}
            </div>
            {/* Drawer body */}
            {drawerOpen && (
              <div className="sw-drawer-body">
                {drawerTab === "assets" && (
                  <AssetBrowser icons={ICONS} onInsert={path => chatRef?.insertText(`Use Asset: ${path}`)} />
                )}
                {drawerTab === "scenes" && (
                  <SceneManager currentSessionId={currentSessionId} />
                )}
                {drawerTab === "context" && (
                  <ContextPanel
                    categoryIcons={CATEGORY_ICONS}
                    icons={ICONS}
                    sessionId={currentSessionId}
                    refreshKey={contextRefreshKey}
                  />
                )}
              </div>
            )}
          </div>
        </div>

        {/* ── Resize handle right ── */}
        {showRight && <div className="sw-resize-col" onMouseDown={startColResize("right")} />}

        {/* ── RIGHT PANEL — driven by studioMode ── */}
        {showRight && <div ref={rightColRef} style={{
          width: colRight, minWidth:240, maxWidth:560, flexShrink:0,
          display:"flex", flexDirection:"column", gap:0, overflow:"visible", padding:"0 4px", margin:"0 -4px",
        }}>
          <div className="sw-panel-card" style={{
            flex:1, minHeight:0, borderRadius:12, border:"1px solid var(--line)",
            display:"flex", flexDirection:"column", overflow:"hidden", background:"var(--panel)",
          }}>
            <div className="sw-panel-header">
              <span className="sw-section-title">
                <span className="sw-num-chip" style={{ background:"var(--ink-3)" }}>
                  {rightPanel2 === "sceneinsp"    ? ICONS.scan(11)
                  : rightPanel2 === "taskinsp"    ? ICONS.check(11)
                  : rightPanel2 === "agentmonitor"? ICONS.robot(11)
                  :                                 ICONS.chartBar(11)}
                </span>
                {rightPanel2 === "sceneinsp"    ? "Scene Inspector"
                : rightPanel2 === "taskinsp"    ? "Task Inspector"
                : rightPanel2 === "agentmonitor"? "Agent Monitor"
                :                                 "Round Inspector"}
              </span>
            </div>
            <div style={{ flex:1, overflow:"hidden", display:"flex", flexDirection:"column", minHeight:0 }}>
              {rightPanel2 === "sceneinsp"    && (
                <SceneInspectorPanel
                  VerifierPanel={CodingVerifierPanel}
                  sessionId={currentSessionId}
                  latestScreenshot={latestScreenshot}
                />
              )}
              {rightPanel2 === "taskinsp"     && <TaskInspectorPanel />}
              {rightPanel2 === "agentmonitor" && (studioMode === "training"
                ? <TrainingMonitorPanel />
                : <AgentPanel sessionId={currentSessionId} icons={ICONS} commHeight={0} onCommHeightChange={() => {}} hideComm />)}
              {rightPanel2 === "roundinsp"    && (
                <RoundInspectorPanel AggregatePanel={AgentAggregatePanelTabs} sessionId={currentSessionId} />
              )}
            </div>
          </div>
        </div>}

      </div>
      )}

      {/* ══ LIBRARY / RESULTS pages ══ */}
      {(topSection === "library" || topSection === "results") && (
        <div style={{
          flex: 1, overflow: "hidden",
          borderRadius: 12, border: "1px solid var(--line)",
          boxShadow: "var(--shadow-card)", background: "var(--panel)", minHeight: 0,
        }}>
          {topSection === "library" && (
            <LibraryPage
              ArenaPage={ArenaPage}
              icons={ICONS}
              newlyAddedSkillIds={artifactNewIds.skills}
              onMarkSkillSeen={markSkillArtifactSeen}
              newlyAddedToolIds={artifactNewIds.tools}
              onMarkToolSeen={markToolArtifactSeen}
            />
          )}
          {topSection === "results" && (
            <ResultsPage icons={ICONS} onOpenScene={openSavedScene} />
          )}
        </div>
      )}

    </div>
    </PollProvider>
  );
}

export default App;

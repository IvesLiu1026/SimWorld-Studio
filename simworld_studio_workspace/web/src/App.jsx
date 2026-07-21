import { useState, useEffect, useCallback } from "react";
import { useQuery } from "@tanstack/react-query";
import { API_BASE } from "./api/client.js";
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
import {
  getDrawerTabs,
  getLeftPanelMeta,
  getRightPanelMeta,
  getStudioPanels,
} from "./features/studio/studioModeConfig.js";
import { useArtifactNotifications } from "./features/studio/useArtifactNotifications.js";
import { useResizableStudioLayout } from "./features/studio/useResizableStudioLayout.js";
import TaskGenPanel from "./features/tasks/TaskGenPanel.jsx";
import TaskInspectorPanel from "./features/tasks/TaskInspectorPanel.jsx";
import TrainingConfigPanel from "./features/training/TrainingConfigPanel.jsx";
import TrainingMonitorPanel from "./features/training/TrainingMonitorPanel.jsx";
import VistaImportPanel from "./features/vista/VistaImportPanel.jsx";
import ViewportPanel from "./features/viewport/ViewportPanel.jsx";
import { useStudioStore } from "./state/studioStore.js";
import {
  PollProvider,
  useSync,
} from "./state/pollContext.jsx";
import { clearSessionToken, useSession } from "./state/useSession.js";
import { CATEGORY_ICONS, ICONS } from "./components/ui/icons.jsx";

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
  const [leftPanelCollapsed, setLeftPanelCollapsed] = useState(false);
  const [rightPanelCollapsed, setRightPanelCollapsed] = useState(false);
  const [pipelineVisible, setPipelineVisible] = useState(true);

  const { leftPanel, rightPanel: rightPanel2 } = getStudioPanels(studioMode);
  const leftPanelMeta = getLeftPanelMeta(leftPanel);
  const rightPanelMeta = getRightPanelMeta(rightPanel2);
  const drawerTabs = getDrawerTabs(studioMode);

  const [latestScreenshot, setLatestScreenshot] = useState(null);
  const [currentSessionId, setCurrentSessionId] = useState(null);
  const [contextRefreshKey, setContextRefreshKey] = useState(0);
  // activePage kept for compatibility with drawer/panel refs; main nav uses topSection+studioMode
  const activePage = topSection === "studio" ? "generate" : topSection;
  const [chatRef, setChatRef] = useState(null);
  const {
    colLeft,
    colRight,
    drawerH,
    drawerOpen,
    drawerTab,
    layoutRef,
    leftColRef,
    rightColRef,
    setDrawerH,
    setDrawerOpen,
    setDrawerTab,
    startColResize,
    startDrawerResize,
  } = useResizableStudioLayout();
  const { session, poolFull, secsLeft, expired, warningSoon } = useSession();
  const syncStatus = useSync();
  const {
    artifactNewIds,
    artifactToasts,
    artifactUnread,
    markSkillArtifactSeen,
    markToolArtifactSeen,
  } = useArtifactNotifications({ activePage });

  const sessionQuery = useQuery({
    queryKey: studioQueryKeys.session,
    queryFn: fetchSession,
    staleTime: 60_000,
  });

  useEffect(() => {
    if (sessionQuery.data?.sessionId) setCurrentSessionId(sessionQuery.data.sessionId);
  }, [sessionQuery.data?.sessionId]);

  useEffect(() => {
    if (!drawerTabs.some((tab) => tab.id === drawerTab)) {
      setDrawerTab(drawerTabs[0].id);
    }
  }, [drawerTab, drawerTabs, setDrawerTab]);

  // Open a saved scene (.umap) from the Results gallery → switch to Scene Generation and
  // load it into the live viewport (reuses /api/load-map + the viewport auto-reconnect).
  const openSavedScene = useCallback(async (path) => {
    setTopSection("studio");
    setStudioMode("scene");
    try {
      await fetch(`${API_BASE}/load-map`, { method:"POST", headers:{ "Content-Type":"application/json" }, body: JSON.stringify({ path }) });
      setTimeout(() => window.dispatchEvent(new Event("sw-reconnect-stream")), 1500);
    } catch {}
  }, []);

  return (
    <PollProvider>
    <div className="simworld-app-shell">

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
        health={health}
        icons={ICONS}
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
          codingAgent={codingAgent}
          codingAgents={codingAgents}
          codingModel={codingModel}
          icons={ICONS}
          uiTheme={uiTheme}
          onCodingAgentChange={handleCodingAgentChange}
          onCodingModelChange={setCodingModel}
          onThemeChange={setUiTheme}
          layoutMode={studioMode}
          onLayoutMode={setStudioMode}
          onClose={() => setShowSettings(false)}
        />
      )}

      <ArtifactToastStack items={artifactToasts} />

      {/* ══ ARTIFACT CHAIN ══ */}
      {topSection === "studio" && (
        <div className={`artifact-chain-shell${pipelineVisible ? "" : " collapsed"}`}>
          {pipelineVisible && (
            <ArtifactChain
              artifacts={artifacts}
              activeMode={studioMode}
              onSelect={m => setStudioMode(m)}
              icons={ICONS}
            />
          )}
          <button
            className="artifact-chain-toggle"
            onClick={() => setPipelineVisible((value) => !value)}
            title={pipelineVisible ? "Hide workflow status" : "Show workflow status"}
            type="button"
          >
            {pipelineVisible ? ICONS.close(11) : ICONS.folder(12)}
            <span>{pipelineVisible ? "Hide" : "Workflow"}</span>
          </button>
        </div>
      )}

      {/* ══ 3-COLUMN RESIZABLE STUDIO LAYOUT ══ */}
      {topSection === "studio" && (
      <div ref={layoutRef} className="studio-workspace">
        {/* ── LEFT PANEL ── */}
        {showLeft && leftPanelCollapsed && (
          <button
            className="sw-panel-rail left"
            onClick={() => setLeftPanelCollapsed(false)}
            title={`Open ${leftPanelMeta.title}`}
            type="button"
          >
            <span className="sw-panel-rail-icon">{ICONS[leftPanelMeta.icon]?.(14)}</span>
            <span>{leftPanelMeta.title}</span>
          </button>
        )}
        {showLeft && !leftPanelCollapsed && <div ref={leftColRef} style={{
          width: colLeft, minWidth:260, maxWidth:640, flexShrink:0,
          display:"flex", flexDirection:"column", gap:0, overflow:"visible", padding:0, margin:0,
        }}>
          {/* ── Left panel content — driven by studioMode ── */}
          <div className="sw-panel-card" style={{
            flex:1, minHeight:0, borderRadius:"var(--radius)", border:"1px solid var(--line)",
            display:"flex", flexDirection:"column", overflow:"hidden", background:"var(--panel)",
          }}>
            <div className="sw-panel-header">
              <span className="sw-section-title">
                <span className="sw-num-chip">
                  {ICONS[leftPanelMeta.icon]?.(11)}
                </span>
                {leftPanelMeta.title}
              </span>
              <button
                className="sw-panel-collapse-btn"
                onClick={() => setLeftPanelCollapsed(true)}
                title="Collapse left panel"
                type="button"
              >
                {ICONS.panelLeft(13)}
              </button>
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
        {showLeft && !leftPanelCollapsed && <div className="sw-resize-col" onMouseDown={startColResize("left")} />}

        {/* ── CENTER: Viewport + drawer ── */}
        <div className="studio-center-column">

          {/* UE Viewport card */}
          <div className="sw-panel-card viewport-card-shell">
            <ViewportPanel icons={ICONS} latestScreenshot={latestScreenshot} health={health} />
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
                {drawerOpen ? drawerTab.charAt(0).toUpperCase()+drawerTab.slice(1) : "Workspace"}
              </span>
              <div style={{ flex:1 }} />
              {drawerOpen && (
                <div style={{ display:"flex", gap:2 }}>
                  {drawerTabs.map(t => (
                    <button key={t.id}
                      className={`sw-tab-btn${drawerTab===t.id?" active":""}`}
                      onClick={e => {
                        e.stopPropagation();
                        setDrawerTab(t.id);
                        if (t.id === "vista_import" && drawerH < 360) setDrawerH(360);
                      }}
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
                {drawerTab === "vista_import" && studioMode === "scene" && (
                  <VistaImportPanel icons={ICONS} />
                )}
              </div>
            )}
          </div>
        </div>

        {/* ── Resize handle right ── */}
        {showRight && !rightPanelCollapsed && <div className="sw-resize-col" onMouseDown={startColResize("right")} />}

        {/* ── RIGHT PANEL — driven by studioMode ── */}
        {showRight && !rightPanelCollapsed && <div ref={rightColRef} style={{
          width: colRight, minWidth:240, maxWidth:560, flexShrink:0,
          display:"flex", flexDirection:"column", gap:0, overflow:"visible", padding:0, margin:0,
        }}>
          <div className="sw-panel-card" style={{
            flex:1, minHeight:0, borderRadius:"var(--radius)", border:"1px solid var(--line)",
            display:"flex", flexDirection:"column", overflow:"hidden", background:"var(--panel)",
          }}>
            <div className="sw-panel-header">
              <span className="sw-section-title">
                <span className="sw-num-chip">
                  {ICONS[rightPanelMeta.icon]?.(11)}
                </span>
                {rightPanelMeta.title}
              </span>
              <button
                className="sw-panel-collapse-btn"
                onClick={() => setRightPanelCollapsed(true)}
                title="Collapse right panel"
                type="button"
              >
                {ICONS.panelRight(13)}
              </button>
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
        {showRight && rightPanelCollapsed && (
          <button
            className="sw-panel-rail right"
            onClick={() => setRightPanelCollapsed(false)}
            title={`Open ${rightPanelMeta.title}`}
            type="button"
          >
            <span className="sw-panel-rail-icon">{ICONS[rightPanelMeta.icon]?.(14)}</span>
            <span>{rightPanelMeta.title}</span>
          </button>
        )}

      </div>
      )}

      {/* ══ LIBRARY / RESULTS pages ══ */}
      {(topSection === "library" || topSection === "results") && (
        <div style={{
          flex: 1, overflow: "hidden",
          borderRadius: "var(--radius)", border: "1px solid var(--line)",
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

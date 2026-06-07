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

// ─── Inline SVG Icons (flat colorful cartoon style) ─────────────────────────

function SvgIcon({ children, size = "1em", style, ...props }) {
  return (
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width={size} height={size} fill="none" style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }} {...props}>
      {children}
    </svg>
  );
}

const ICONS = {
  // ── feather/outline monochrome icons — all use currentColor ──
  clipboard: (s) => <SvgIcon size={s}><path d="M16 4h2a2 2 0 012 2v14a2 2 0 01-2 2H6a2 2 0 01-2-2V6a2 2 0 012-2h2" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/><rect x="8" y="2" width="8" height="4" rx="1" stroke="currentColor" strokeWidth="2" fill="none"/><line x1="9" y1="12" x2="15" y2="12" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/><line x1="9" y1="16" x2="13" y2="16" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/></SvgIcon>,
  search: (s) => <SvgIcon size={s}><circle cx="11" cy="11" r="8" stroke="currentColor" strokeWidth="2" fill="none"/><line x1="21" y1="21" x2="16.65" y2="16.65" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"/></SvgIcon>,
  plus: (s) => <SvgIcon size={s}><line x1="12" y1="5" x2="12" y2="19" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"/><line x1="5" y1="12" x2="19" y2="12" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"/></SvgIcon>,
  trash: (s) => <SvgIcon size={s}><polyline points="3 6 5 6 21 6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/><path d="M19 6l-1 14H6L5 6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/><path d="M10 11v6M14 11v6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/><path d="M9 6V4h6v2" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/></SvgIcon>,
  transform: (s) => <SvgIcon size={s}><polyline points="5 9 2 12 5 15" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/><polyline points="19 9 22 12 19 15" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/><line x1="2" y1="12" x2="22" y2="12" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/></SvgIcon>,
  document: (s) => <SvgIcon size={s}><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/><polyline points="14 2 14 8 20 8" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/><line x1="8" y1="13" x2="16" y2="13" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/><line x1="8" y1="17" x2="13" y2="17" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/></SvgIcon>,
  video: (s) => <SvgIcon size={s}><polygon points="23 7 16 12 23 17 23 7" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/><rect x="1" y="5" width="15" height="14" rx="2" stroke="currentColor" strokeWidth="2" fill="none"/></SvgIcon>,
  camera: (s) => <SvgIcon size={s}><path d="M23 19a2 2 0 01-2 2H3a2 2 0 01-2-2V8a2 2 0 012-2h4l2-3h6l2 3h4a2 2 0 012 2z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/><circle cx="12" cy="13" r="4" stroke="currentColor" strokeWidth="2" fill="none"/></SvgIcon>,
  python: (s) => <SvgIcon size={s}><path d="M12 2c-3.5 0-6 1.5-6 4v2h6M6 8H4C2 8 2 10 2 12s0 4 2 4h2v-2c0-2.5 2-4 4-4h4c2 0 4-1.5 4-4V6c0-2.5-2.5-4-6-4z" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" fill="none"/><path d="M12 22c3.5 0 6-1.5 6-4v-2h-6M18 16h2c2 0 2-2 2-4s0-4-2-4h-2v2c0 2.5-2 4-4 4H10c-2 0-4 1.5-4 4v2c0 2.5 2.5 4 6 4z" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" fill="none"/><circle cx="9" cy="5.5" r="1" fill="currentColor"/><circle cx="15" cy="18.5" r="1" fill="currentColor"/></SvgIcon>,
  wrench: (s) => <SvgIcon size={s}><path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/></SvgIcon>,
  gear: (s) => <SvgIcon size={s}><circle cx="12" cy="12" r="3" stroke="currentColor" strokeWidth="2" fill="none"/><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 01-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09a1.65 1.65 0 00-1-1.51 1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83-2.83l.06-.06a1.65 1.65 0 00.33-1.82 1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09a1.65 1.65 0 001.51-1 1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 012.83-2.83l.06.06a1.65 1.65 0 001.82.33h0a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 2.83l-.06.06a1.65 1.65 0 00-.33 1.82v0a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z" stroke="currentColor" strokeWidth="2" fill="none"/></SvgIcon>,
  palette: (s) => <SvgIcon size={s}><path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.83 0 1.5-.67 1.5-1.5 0-.39-.15-.74-.39-1.01A1.5 1.5 0 0114 18h1.5c3.04 0 5.5-2.46 5.5-5.5C21 6.81 17.05 2 12 2z" stroke="currentColor" strokeWidth="2" fill="none"/><circle cx="8.5" cy="10.5" r="1.5" fill="currentColor"/><circle cx="12.5" cy="7.5" r="1.5" fill="currentColor"/><circle cx="16.5" cy="10.5" r="1.5" fill="currentColor"/><circle cx="15.5" cy="14.5" r="1.5" fill="currentColor"/></SvgIcon>,
  plug: (s) => <SvgIcon size={s}><path d="M7 2v8M17 2v8M6 10h12v2a6 6 0 01-6 6v0a6 6 0 01-6-6v-2z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/><line x1="12" y1="18" x2="12" y2="22" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/></SvgIcon>,
  eye: (s) => <SvgIcon size={s}><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/><circle cx="12" cy="12" r="3" stroke="currentColor" strokeWidth="2" fill="none"/></SvgIcon>,
  map: (s) => <SvgIcon size={s}><polygon points="1 6 1 22 8 18 16 22 23 18 23 2 16 6 8 2 1 6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/><line x1="8" y1="2" x2="8" y2="18" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/><line x1="16" y1="6" x2="16" y2="22" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/></SvgIcon>,
  building: (s) => <SvgIcon size={s}><path d="M3 2h18v20H3zM9 2v20M15 2v20M3 8h18M3 14h18" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/></SvgIcon>,
  tree: (s) => <SvgIcon size={s}><path d="M12 2l7 12H5L12 2z" stroke="currentColor" strokeWidth="2" strokeLinejoin="round" fill="none"/><path d="M12 8l5 9H7l5-9z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" fill="none"/><line x1="12" y1="18" x2="12" y2="22" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"/></SvgIcon>,
  car: (s) => <SvgIcon size={s}><path d="M5 12l2-6h10l2 6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/><rect x="1" y="12" width="22" height="7" rx="2" stroke="currentColor" strokeWidth="2" fill="none"/><circle cx="6" cy="19" r="2" stroke="currentColor" strokeWidth="2" fill="none"/><circle cx="18" cy="19" r="2" stroke="currentColor" strokeWidth="2" fill="none"/><line x1="1" y1="15" x2="23" y2="15" stroke="currentColor" strokeWidth="1.5"/></SvgIcon>,
  hydrant: (s) => <SvgIcon size={s}><rect x="7" y="11" width="10" height="10" rx="1" stroke="currentColor" strokeWidth="2" fill="none"/><rect x="9" y="5" width="6" height="6" rx="2" stroke="currentColor" strokeWidth="2" fill="none"/><line x1="5" y1="14" x2="7" y2="14" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><line x1="17" y1="14" x2="19" y2="14" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><circle cx="12" cy="7.5" r="1" fill="currentColor"/></SvgIcon>,
  road: (s) => <SvgIcon size={s}><path d="M5 22L9 2h6l4 20H5z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/><line x1="12" y1="4" x2="12" y2="7" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><line x1="12" y1="10" x2="12" y2="14" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><line x1="12" y1="17" x2="12" y2="20" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/></SvgIcon>,
  cube: (s) => <SvgIcon size={s}><path d="M21 16V8a2 2 0 00-1-1.73l-7-4a2 2 0 00-2 0l-7 4A2 2 0 003 8v8a2 2 0 001 1.73l7 4a2 2 0 002 0l7-4A2 2 0 0021 16z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/><polyline points="3.27 6.96 12 12.01 20.73 6.96" stroke="currentColor" strokeWidth="2" fill="none"/><line x1="12" y1="22.08" x2="12" y2="12" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/></SvgIcon>,
  hammer: (s) => <SvgIcon size={s}><path d="M15 12l-8.5 8.5c-.83.83-2.17.83-3 0s-.83-2.17 0-3L12 9" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/><path d="M17.64 15L22 10.64M20.91 11.7l-1.25-1.25c-.6-.6-.93-1.4-.93-2.25v-.86L16.01 4.6a5 5 0 00-3-2.57L9 6.86a5 5 0 002.57 3L13 11" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/></SvgIcon>,
  box: (s) => <SvgIcon size={s}><path d="M21 16V8a2 2 0 00-1-1.73l-7-4a2 2 0 00-2 0l-7 4A2 2 0 003 8v8a2 2 0 001 1.73l7 4a2 2 0 002 0l7-4A2 2 0 0021 16z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/><polyline points="3.27 6.96 12 12.01 20.73 6.96" stroke="currentColor" strokeWidth="2" fill="none"/><line x1="12" y1="22.08" x2="12" y2="12" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><polyline points="7.5 4.21 12 6.81 16.5 4.21" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" fill="none"/></SvgIcon>,
  chat: (s) => <SvgIcon size={s}><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/></SvgIcon>,
  robot: (s) => <SvgIcon size={s}><rect x="4" y="7" width="16" height="13" rx="2" stroke="currentColor" strokeWidth="2" fill="none"/><rect x="7" y="11" width="3" height="3" rx=".5" stroke="currentColor" strokeWidth="1.5" fill="none"/><rect x="14" y="11" width="3" height="3" rx=".5" stroke="currentColor" strokeWidth="1.5" fill="none"/><line x1="12" y1="4" x2="12" y2="7" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><circle cx="12" cy="3" r="1.5" stroke="currentColor" strokeWidth="1.5" fill="none"/><line x1="1" y1="14" x2="4" y2="14" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><line x1="20" y1="14" x2="23" y2="14" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><line x1="9" y1="17" x2="15" y2="17" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/></SvgIcon>,
  swords: (s) => <SvgIcon size={s}><polyline points="14.5 17.5 3 6 3 3 6 3 17.5 14.5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/><line x1="13" y1="19" x2="19" y2="13" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><polyline points="20 16 20 20 16 20" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/><line x1="15" y1="4" x2="4" y2="15" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><polyline points="8 4 4 4 4 8" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/></SvgIcon>,
  frame: (s) => <SvgIcon size={s}><rect x="3" y="3" width="18" height="18" rx="2" stroke="currentColor" strokeWidth="2" fill="none"/><circle cx="8.5" cy="8.5" r="1.5" fill="currentColor"/><polyline points="21 15 16 10 5 21" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/></SvgIcon>,
  tools: (s) => <SvgIcon size={s}><path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/></SvgIcon>,
  trophy: (s) => <SvgIcon size={s}><path d="M6 9H4.5a2.5 2.5 0 010-5H6M18 9h1.5a2.5 2.5 0 000-5H18" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/><path d="M7 4h10v7a5 5 0 01-10 0V4z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/><line x1="12" y1="16" x2="12" y2="20" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><rect x="7" y="19" width="10" height="2.5" rx="1" stroke="currentColor" strokeWidth="1.5" fill="none"/></SvgIcon>,
  gamepad: (s) => <SvgIcon size={s}><rect x="2" y="6" width="20" height="12" rx="5" stroke="currentColor" strokeWidth="2" fill="none"/><line x1="6" y1="12" x2="10" y2="12" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><line x1="8" y1="10" x2="8" y2="14" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><circle cx="16" cy="10" r="1" fill="currentColor"/><circle cx="18" cy="12" r="1" fill="currentColor"/><circle cx="16" cy="14" r="1" fill="currentColor"/><circle cx="14" cy="12" r="1" fill="currentColor"/></SvgIcon>,
  mouse: (s) => <SvgIcon size={s}><rect x="6" y="3" width="12" height="19" rx="6" stroke="currentColor" strokeWidth="2" fill="none"/><line x1="12" y1="8" x2="12" y2="11" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/></SvgIcon>,
  keyboard: (s) => <SvgIcon size={s}><rect x="2" y="6" width="20" height="13" rx="2" stroke="currentColor" strokeWidth="2" fill="none"/><line x1="6" y1="10" x2="6.01" y2="10" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><line x1="10" y1="10" x2="10.01" y2="10" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><line x1="14" y1="10" x2="14.01" y2="10" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><line x1="18" y1="10" x2="18.01" y2="10" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><line x1="8" y1="14" x2="16" y2="14" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><line x1="6" y1="14" x2="6.01" y2="14" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><line x1="18" y1="14" x2="18.01" y2="14" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/></SvgIcon>,
  liveCircle: (s) => <SvgIcon size={s}><circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="2" fill="none"/><circle cx="12" cy="12" r="4" fill="currentColor"/></SvgIcon>,
  warning: (s) => <SvgIcon size={s}><path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/><line x1="12" y1="9" x2="12" y2="13" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><line x1="12" y1="17" x2="12.01" y2="17" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"/></SvgIcon>,
  gold: (s) => <SvgIcon size={s}><circle cx="12" cy="9" r="7" stroke="currentColor" strokeWidth="2" fill="none"/><text x="12" y="13" textAnchor="middle" fontSize="9" fontWeight="800" fill="currentColor">1</text><path d="M6 17h12l-1 4H7l-1-4z" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" fill="none"/></SvgIcon>,
  silver: (s) => <SvgIcon size={s}><circle cx="12" cy="9" r="7" stroke="currentColor" strokeWidth="2" fill="none"/><text x="12" y="13" textAnchor="middle" fontSize="9" fontWeight="800" fill="currentColor">2</text><path d="M6 17h12l-1 4H7l-1-4z" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" fill="none"/></SvgIcon>,
  bronze: (s) => <SvgIcon size={s}><circle cx="12" cy="9" r="7" stroke="currentColor" strokeWidth="2" fill="none"/><text x="12" y="13" textAnchor="middle" fontSize="9" fontWeight="800" fill="currentColor">3</text><path d="M6 17h12l-1 4H7l-1-4z" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" fill="none"/></SvgIcon>,
  book: (s) => <SvgIcon size={s}><path d="M4 19.5A2.5 2.5 0 016.5 17H20" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 014 19.5v-15A2.5 2.5 0 016.5 2z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/><line x1="9" y1="8" x2="15" y2="8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/><line x1="9" y1="12" x2="13" y2="12" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/></SvgIcon>,
  brain: (s) => <SvgIcon size={s}><path d="M9.5 2A2.5 2.5 0 007 4.5 2.5 2.5 0 004.5 7 2.5 2.5 0 002 9.5 2.5 2.5 0 004.5 12 2.5 2.5 0 007 14.5 2.5 2.5 0 009.5 17" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/><path d="M14.5 2A2.5 2.5 0 0117 4.5 2.5 2.5 0 0119.5 7 2.5 2.5 0 0122 9.5 2.5 2.5 0 0119.5 12 2.5 2.5 0 0117 14.5 2.5 2.5 0 0114.5 17" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/><path d="M9 17v3a2 2 0 002 2h2a2 2 0 002-2v-3M9.5 2h5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/></SvgIcon>,
  close: (s) => <SvgIcon size={s}><line x1="18" y1="6" x2="6" y2="18" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"/><line x1="6" y1="6" x2="18" y2="18" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"/></SvgIcon>,
  check: (s) => <SvgIcon size={s}><polyline points="20 6 9 17 4 12" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" fill="none"/></SvgIcon>,
  chartBar: (s) => <SvgIcon size={s}><line x1="18" y1="20" x2="18" y2="10" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"/><line x1="12" y1="20" x2="12" y2="4" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"/><line x1="6" y1="20" x2="6" y2="14" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"/><line x1="3" y1="20" x2="21" y2="20" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/></SvgIcon>,
  layout: (s) => <SvgIcon size={s}><rect x="3" y="3" width="18" height="18" rx="2" stroke="currentColor" strokeWidth="2" fill="none"/><line x1="3" y1="9" x2="21" y2="9" stroke="currentColor" strokeWidth="2"/><line x1="9" y1="21" x2="9" y2="9" stroke="currentColor" strokeWidth="2"/></SvgIcon>,
  panelLeft: (s) => <SvgIcon size={s}><rect x="3" y="3" width="18" height="18" rx="2" stroke="currentColor" strokeWidth="2" fill="none"/><line x1="9" y1="3" x2="9" y2="21" stroke="currentColor" strokeWidth="2"/></SvgIcon>,
  panelRight: (s) => <SvgIcon size={s}><rect x="3" y="3" width="18" height="18" rx="2" stroke="currentColor" strokeWidth="2" fill="none"/><line x1="15" y1="3" x2="15" y2="21" stroke="currentColor" strokeWidth="2"/></SvgIcon>,
  sun: (s) => <SvgIcon size={s}><circle cx="12" cy="12" r="4" stroke="currentColor" strokeWidth="2" fill="none"/><line x1="12" y1="2" x2="12" y2="4" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><line x1="12" y1="20" x2="12" y2="22" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><line x1="2" y1="12" x2="4" y2="12" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><line x1="20" y1="12" x2="22" y2="12" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/></SvgIcon>,
  moon: (s) => <SvgIcon size={s}><path d="M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/></SvgIcon>,
  lock: (s) => <SvgIcon size={s}><rect x="3" y="11" width="18" height="11" rx="2" stroke="currentColor" strokeWidth="2" fill="none"/><path d="M7 11V7a5 5 0 0110 0v4" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/></SvgIcon>,
  clock: (s) => <SvgIcon size={s}><circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="2" fill="none"/><polyline points="12 6 12 12 16 14" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/></SvgIcon>,
  ghost: (s) => <SvgIcon size={s}><path d="M9 10h.01M15 10h.01" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><path d="M12 2a7 7 0 017 7v6l-2-1-2 1-2-1-2 1-2-1-2 1V9a7 7 0 017-7z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/></SvgIcon>,
  maximize: (s) => <SvgIcon size={s}><path d="M8 3H5a2 2 0 00-2 2v3M21 8V5a2 2 0 00-2-2h-3M3 16v3a2 2 0 002 2h3M16 21h3a2 2 0 002-2v-3" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/></SvgIcon>,
  zap:      (s) => <SvgIcon size={s}><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/></SvgIcon>,
  refresh:  (s) => <SvgIcon size={s}><polyline points="23 4 23 10 17 10" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/><polyline points="1 20 1 14 7 14" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/><path d="M3.51 9a9 9 0 0114.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0020.49 15" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/></SvgIcon>,
  collision:(s) => <SvgIcon size={s}><circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="2" fill="none"/><line x1="12" y1="8" x2="12" y2="12" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"/><line x1="12" y1="16" x2="12.01" y2="16" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"/></SvgIcon>,
  target:   (s) => <SvgIcon size={s}><circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="2" fill="none"/><circle cx="12" cy="12" r="6" stroke="currentColor" strokeWidth="2" fill="none"/><circle cx="12" cy="12" r="2" fill="currentColor"/></SvgIcon>,
  activity: (s) => <SvgIcon size={s}><polyline points="22 12 18 12 15 21 9 3 6 12 2 12" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/></SvgIcon>,
  folder:   (s) => <SvgIcon size={s}><path d="M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/></SvgIcon>,
  scan:     (s) => <SvgIcon size={s}><path d="M3 7V5a2 2 0 012-2h2M17 3h2a2 2 0 012 2v2M21 17v2a2 2 0 01-2 2h-2M7 21H5a2 2 0 01-2-2v-2" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/><line x1="7" y1="12" x2="17" y2="12" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/></SvgIcon>,
  users:    (s) => <SvgIcon size={s}><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/><circle cx="9" cy="7" r="4" stroke="currentColor" strokeWidth="2" fill="none"/><path d="M23 21v-2a4 4 0 00-3-3.87M16 3.13a4 4 0 010 7.75" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/></SvgIcon>,
};

// ─── Constants ───────────────────────────────────────────────────────────────

const EVOLUTION_ARTIFACT_POLL_MS = 5000;
const EVOLUTION_TOAST_MAX = 2;
const EVOLUTION_TOAST_TTL_MS = 5200;

const CATEGORY_ICONS = {
  buildings: ICONS.building,
  trees: ICONS.tree,
  vehicles: ICONS.car,
  street_furniture: ICONS.hydrant,
  roads: ICONS.road,
  static_meshes: ICONS.cube,
};


// ─── Utility ─────────────────────────────────────────────────────────────────

function isLearnedSkillMeta(skill) {
  if (!skill || typeof skill !== "object") return false;
  const source = String(skill.source || "").toLowerCase();
  const tags = Array.isArray(skill.tags) ? skill.tags : [];
  return source === "custom" && tags.includes("learned");
}

// ─── ChatPanel ───────────────────────────────────────────────────────────────


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

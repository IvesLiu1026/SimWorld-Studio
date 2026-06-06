import React, { useState, useEffect, useRef, useCallback, useMemo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useQuery } from "@tanstack/react-query";
import { API_BASE } from "./api/client.js";
import {
  clearCheckpoints,
  createCheckpoint,
  createSkill,
  deleteSkill,
  deleteToolProcedure,
  fetchAgents,
  fetchEvolutionConfig,
  fetchGallery,
  fetchLeaderboard,
  fetchSkillDetails,
  fetchSkills,
  fetchTools,
  getCheckpoint,
  listCheckpoints,
  resetScene,
  restoreCheckpoint,
  runArena,
  saveScene,
  sendChat,
  shareToGallery,
  updateAgent,
  updateEvolutionConfig,
  updateToolProcedure,
  voteOnBattle,
} from "./api/appApi.js";
import { fetchCodingAgents, fetchHealth, fetchSession, studioQueryKeys } from "./api/studioApi.js";
import AgentAggregatePanelTabs from "./features/agents/AgentAggregatePanelTabs.jsx";
import AgentPanel from "./features/agents/AgentPanel.jsx";
import SceneAgentHeader from "./components/chat/SceneAgentHeader.jsx";
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
  StatusBadge,
  ToggleBtn,
  inputSx,
} from "./components/ui/primitives.jsx";
import { DEFAULT_CODING_AGENTS, agentLabel } from "./features/agents/codingAgents.js";
import AssetBrowser from "./features/assets/AssetBrowser.jsx";
import CurriculumBuilderPanel from "./features/coevolution/CurriculumBuilderPanel.jsx";
import ContextPanel from "./features/context/ContextPanel.jsx";
import RoundInspectorPanel from "./features/coevolution/RoundInspectorPanel.jsx";
import LibraryPage from "./features/library/LibraryPage.jsx";
import ResultsPage from "./features/results/ResultsPage.jsx";
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
  useMetrics,
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

function tagChipSx(tag, overrides = {}) {
  const color = TAG_COLORS[tag];
  return {
    fontSize: 11,
    padding: "2px 6px",
    borderRadius: 4,
    background: color ? `rgb(${color} / 0.18)` : "var(--panel-2)",
    color: color ? `rgb(${color})` : "var(--ink-3)",
    border: `1px solid ${color ? `rgb(${color} / 0.28)` : "var(--line)"}`,
    ...overrides,
  };
}

// Colored tag chip based on TAG_COLORS
function TagChip({ tag }) {
  return (
    <span style={tagChipSx(tag)}>
      {tag}
    </span>
  );
}

// ─── Constants ───────────────────────────────────────────────────────────────

const EVOLUTION_ARTIFACT_POLL_MS = 5000;
const EVOLUTION_TOAST_MAX = 2;
const EVOLUTION_TOAST_TTL_MS = 5200;

const TOOL_ICONS = {
  get_actors_in_level: ICONS.clipboard,
  find_actors_by_name: ICONS.search,
  spawn_actor: ICONS.plus,
  delete_actor: ICONS.trash,
  set_actor_transform: ICONS.transform,
  get_actor_properties: ICONS.document,
  focus_viewport: ICONS.video,
  take_screenshot: ICONS.camera,
  get_camera_0_view: ICONS.camera,
  execute_python_script: ICONS.python,
  create_blueprint: ICONS.wrench,
  compile_blueprint: ICONS.gear,
  apply_material_to_actor: ICONS.palette,
  initialize: ICONS.plug,
  observe_scene: ICONS.eye,
  get_scene_overview: ICONS.map,
};

const TAG_COLORS = {
  city: "59 130 246",
  buildings: "185 28 28",
  props: "100 116 139",
  weather: "234 88 12",
  camera: "124 58 237",
  layout: "22 163 74",
  planning: "59 130 246",
  spacing: "245 158 11",
  trees: "22 163 74",
  vehicles: "234 88 12",
  lighting: "245 158 11",
  atmosphere: "124 58 237",
  screenshot: "124 58 237",
  decoration: "100 116 139",
  furniture: "100 116 139",
  architecture: "185 28 28",
  environment: "22 163 74",
  viewpoint: "124 58 237",
  placement: "245 158 11",
  roads: "100 116 139",
  capture: "124 58 237",
};

const CATEGORY_ICONS = {
  buildings: ICONS.building,
  trees: ICONS.tree,
  vehicles: ICONS.car,
  street_furniture: ICONS.hydrant,
  roads: ICONS.road,
  static_meshes: ICONS.cube,
};

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

// Tools whose successful use changes the scene → worth a checkpoint.
const SCENE_TOOLS = new Set([
  "spawn_blueprint_actor", "spawn_actor", "spawn_agent",
  "delete_actor", "delete_all_spawned", "setup_environment", "set_actor_transform",
  "execute_python_script",
]);
function turnChangedScene(msg) {
  return (msg?.toolCalls || []).some((tc) => {
    const name = tc.displayName || (tc.name || "").replace(/^mcp__\w+__/, "");
    return SCENE_TOOLS.has(name) && tc.status !== "error";
  });
}
// latestScreenshot is "/api/screenshot/file?path=<abs>" → pull the absolute path for the thumbnail.
function screenshotPathFromUrl(url) {
  if (!url || typeof url !== "string") return null;
  try { return new URLSearchParams(url.split("?")[1] || "").get("path"); } catch { return null; }
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

// ─── ToolCallBlock ───────────────────────────────────────────────────────────

const ToolCallBlock = React.memo(function ToolCallBlock({ tool }) {
  const [expanded, setExpanded] = useState(false);

  const iconFn = TOOL_ICONS[tool.displayName] || ICONS.hammer;
  const statusColor = {
    starting: "#64748b",
    running: "#f59e0b",
    done: "#16a34a",
    error: "#dc2626",
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
        border: "1px solid var(--line)",
        borderRadius: 8,
        overflow: "hidden",
        background: "var(--panel-2)",
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
          color: "var(--ink-3)",
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
        <span style={{ fontSize: 13, display: "inline-flex", alignItems: "center" }}>{iconFn(13)}</span>
        <span style={{ fontFamily: "monospace", color: "var(--blue)", fontWeight: 600 }}>
          {displayName}
        </span>
        {paramSummary && (
          <span
            style={{
              color: "var(--ink-2)",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
              flex: 1,
              fontSize: 12,
            }}
          >
            ({paramSummary})
          </span>
        )}
        {tool.status === "running" && !tool.input && (
          <span style={{ color: "#f59e0b", fontSize: 12, marginLeft: "auto" }}>
            running…
          </span>
        )}
        <span style={{ marginLeft: "auto", fontSize: 12, flexShrink: 0 }}>
          {expanded ? "▲" : "▼"}
        </span>
      </button>

      {expanded && (
        <div style={{ padding: "8px 10px", borderTop: "1px solid var(--line)" }}>
          {(tool.input || tool.inputBuffer) && (
            <div style={{ marginBottom: 6 }}>
              <div
                style={{
                  color: "var(--ink-2)",
                  fontSize: 12,
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
                  fontSize: 12,
                  color: "var(--ink-2)",
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
                  color: "var(--ink-3)",
                  fontSize: 12,
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
                  fontSize: 12,
                  color: tool.isError ? "#dc2626" : "#16a34a",
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
                  border: "1px solid var(--line)",
                }}
              />
            </div>
          )}
        </div>
      )}

      <style>{`@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} } @keyframes spin { to{transform:rotate(360deg)} }`}</style>
    </div>
  );
}); // end React.memo(ToolCallBlock)

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
        border: `1px solid ${active ? "var(--blue)" : "var(--line)"}`,
        background: active ? "var(--blue-soft)" : "#ffffff",
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
            border: `2px solid ${active ? "var(--blue)" : "var(--line)"}`,
            background: active ? "var(--blue)" : "transparent",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontSize: 12,
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
            color: "var(--ink)",
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
            fontSize: 12,
            background: "var(--panel-2)",
            border: "1px solid var(--line)",
            borderRadius: 4,
            color: "var(--ink-3)",
            cursor: "pointer",
          }}
          title="Preview skill details"
        >
          Preview
        </button>
        <span
          style={{
            fontSize: 12,
            padding: "1px 5px",
            borderRadius: 4,
            background: skill.source === "custom" ? "var(--blue-soft)" : "var(--panel-2)",
            color: skill.source === "custom" ? "var(--blue)" : "var(--ink-3)",
          }}
        >
          {skill.source}
        </span>
      </div>

      <div
        style={{
          fontSize: 12,
          color: "var(--ink-3)",
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
              style={tagChipSx(tag, { fontSize: 12, padding: "1px 5px" })}
            >
              {tag}
            </span>
          ))}
        </div>
      )}

      {skill.dependencies.length > 0 && (
        <div
          style={{
            fontSize: 12,
            color: "var(--ink-2)",
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
          background: "var(--bg)",
          border: "1px solid var(--line)",
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
            borderBottom: "1px solid var(--line)",
            display: "flex",
            alignItems: "center",
            gap: 10,
          }}
        >
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 16, fontWeight: 600, color: "var(--ink)" }}>
              {skill.name}
            </div>
            <div style={{ fontSize: 12, color: "var(--ink-3)", marginTop: 3 }}>
              v{skill.version} by {skill.author}
              <span
                style={{
                  marginLeft: 8,
                  padding: "1px 5px",
                  borderRadius: 4,
                  background: skill.source === "custom" ? "var(--blue-soft)" : "#e6e9ef",
                  color: skill.source === "custom" ? "var(--blue)" : "var(--ink-3)",
                  fontSize: 12,
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
                fontSize: 12,
                background: "rgba(255,95,99,0.1)",
                border: "1px solid rgba(255,95,99,0.3)",
                borderRadius: 6,
                color: "var(--red)",
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
              color: "var(--ink-3)",
              cursor: "pointer",
            }}
          >
            ✕
          </button>
        </div>

        {/* Description + Tags */}
        <div style={{ padding: "10px 18px", borderBottom: "1px solid var(--line)" }}>
          <div style={{ fontSize: 12, color: "var(--ink-3)", lineHeight: 1.5 }}>
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
                  style={tagChipSx(tag, { fontSize: 12, padding: "2px 7px" })}
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
              color: "var(--ink)",
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
    background: "var(--panel)",
    border: "1px solid var(--line)",
    borderRadius: 6,
    color: "var(--ink)",
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
          background: "var(--bg)",
          border: "1px solid var(--line)",
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
            borderBottom: "1px solid var(--line)",
            display: "flex",
            alignItems: "center",
          }}
        >
          <span style={{ fontSize: 16, fontWeight: 600, color: "var(--ink)" }}>
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
              color: "var(--ink-3)",
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
            <label style={{ fontSize: 12, color: "var(--ink-3)", display: "block", marginBottom: 4 }}>
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
            <label style={{ fontSize: 12, color: "var(--ink-3)", display: "block", marginBottom: 4 }}>
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
            <label style={{ fontSize: 12, color: "var(--ink-3)", display: "block", marginBottom: 4 }}>
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
            <label style={{ fontSize: 12, color: "var(--ink-3)", display: "block", marginBottom: 4 }}>
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
            <label style={{ fontSize: 12, color: "var(--ink-3)", display: "block", marginBottom: 4 }}>
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
            <div style={{ fontSize: 12, color: "var(--red)", padding: "4px 0" }}>{error}</div>
          )}
        </div>

        {/* Footer */}
        <div
          style={{
            padding: "12px 18px",
            borderTop: "1px solid var(--line)",
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
              background: "var(--panel-2)",
              border: "1px solid var(--line)",
              borderRadius: 6,
              color: "var(--ink)",
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
              background: "var(--green)",
              border: "1px solid var(--green)",
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

  // Delay initial load so it doesn't compete with critical requests at startup
  useEffect(() => { const t = setTimeout(reload, 5000); return () => clearTimeout(t); }, []);

  useEffect(() => {
    if (!expanded) return;
    reload();
    const timer = setInterval(reload, 30000);
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
        borderBottom: "1px solid var(--line)",
        background: "var(--bg)",
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
        <span style={{ fontSize: 12, color: "var(--ink-3)", fontFamily: "monospace" }}>
          {expanded ? "▼" : "▶"}
        </span>
        <span style={{ fontSize: 12, fontWeight: 600, color: "var(--ink)" }}>Skills</span>
        {activeSkills.length > 0 && (
          <span
            style={{
              fontSize: 12,
              background: "var(--blue)",
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
          <span style={{ fontSize: 12, color: "var(--ink-3)" }}>Auto-select skills</span>
          <button
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
                background: "#fff",
                transform: autoEnabled ? "translateX(16px)" : "translateX(0)",
                transition: "transform 0.15s ease",
              }}
            />
          </button>
        </div>
        <span style={{ fontSize: 12, color: "var(--ink-2)", marginLeft: 6 }}>
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
              fontSize: 12,
              color: "var(--ink-3)",
              border: "1px solid var(--line)",
              background: "var(--bg)",
              borderRadius: 6,
              padding: "6px 8px",
              marginBottom: 2,
            }}
          >
            {autoEnabled
              ? "Auto mode: the agent pre-selects relevant skills before each run."
              : "Manual mode: check the exact skills you want active."}
          </div>
          {builtinSkills.length > 0 && (
            <div
              style={{
                fontSize: 12,
                color: "var(--ink-2)",
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
                fontSize: 12,
                color: "var(--ink-2)",
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
          borderBottom: "1px solid var(--line)",
          display: "flex",
          alignItems: "center",
          gap: 8,
          background: "var(--panel)",
        }}
      >
        <span style={{ fontSize: 13, fontWeight: 600, color: "var(--orange)" }}>
          Annotate Screenshot
        </span>
        <span style={{ fontSize: 12, color: "var(--ink-3)" }}>
          Click on the image to add feedback points
        </span>
        <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          <button
            onClick={onCancel}
            style={{
              padding: "4px 12px",
              fontSize: 12,
              borderRadius: 4,
              border: "1px solid var(--line)",
              background: "var(--panel-2)",
              color: "var(--ink-3)",
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
              fontSize: 12,
              borderRadius: 4,
              border: "1px solid var(--blue)",
              background: "var(--blue)",
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
                    background: "var(--orange)",
                    border: "2px solid #fff",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    fontSize: 12,
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
                    background: "var(--panel-2)",
                    border: "1px solid var(--line)",
                    borderRadius: 4,
                    padding: "2px 6px",
                    fontSize: 12,
                    color: "var(--ink)",
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
            borderLeft: "1px solid #e6e9ef",
            background: "var(--bg)",
            display: "flex",
            flexDirection: "column",
            padding: 12,
            gap: 8,
          }}
        >
          <span style={{ fontSize: 12, fontWeight: 600, color: "var(--ink)" }}>Feedback</span>
          <textarea
            value={feedbackText}
            onChange={(e) => setFeedbackText(e.target.value)}
            placeholder="Describe what to change overall..."
            style={{
              flex: 1,
              resize: "none",
              background: "var(--panel)",
              border: "1px solid var(--line)",
              borderRadius: 6,
              color: "var(--ink)",
              padding: 8,
              fontSize: 12,
              fontFamily: "inherit",
            }}
          />
          {points.length > 0 && (
            <div style={{ fontSize: 12, color: "var(--ink-3)" }}>
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
                      background: "var(--orange)",
                      fontSize: 12,
                      color: "#fff",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      flexShrink: 0,
                    }}
                  >
                    {i + 1}
                  </span>
                  <span style={{ fontSize: 12, color: "var(--ink)" }}>{pt.text}</span>
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

const ChatMessage = React.memo(function ChatMessage({ message, agentLabel: agentLabelText }) {
  const isUser = message.role === "user";

  const bubbleContent = isUser ? (
    <div style={{ color:"var(--ink)", fontSize:13, whiteSpace:"pre-wrap" }}>{message.content}</div>
  ) : (
    <>
      {message.waiting && (
        <div style={{ color:"#64748b", fontSize:12, display:"flex", alignItems:"center", gap:8, padding:"2px 0" }}>
          <span style={{ display:"inline-block", width:8, height:8, borderRadius:"50%", border:"2px solid var(--blue)", borderTopColor:"transparent", animation:"spin 1s linear infinite" }} />
          Waiting for {agentLabelText || "agent"}...
        </div>
      )}
      {message.blocks
        ? message.blocks.map((block, idx) =>
            block.type === "text" ? (
              block.content ? (
                <div className="markdown" key={"t"+idx} style={{ color:"#0f172a", fontSize:13 }}>
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{block.content}</ReactMarkdown>
                </div>
              ) : null
            ) : (
              <ToolCallBlock key={block.toolId} tool={(message.toolCalls||[]).find(tc=>tc.id===block.toolId)} />
            )
          )
        : [
            message.content && (
              <div className="markdown" key="content" style={{ color:"var(--ink,#0f172a)", fontSize:13 }}>
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
              </div>
            ),
            (message.toolCalls||[]).map(tc=><ToolCallBlock key={tc.id} tool={tc}/>),
          ]}
    </>
  );

  const bubble = (
    <div className={isUser ? "sw-bubble-user" : "sw-bubble-assistant"} style={{
      padding:"9px 12px",
      borderRadius: isUser ? "12px 12px 4px 12px" : "12px 12px 12px 4px",
      background: isUser ? "var(--user-bubble,#eff4ff)" : "var(--assistant-bubble,#ffffff)",
      border:`1px solid ${isUser?"var(--blue-soft,#dbe6ff)":"var(--line,#e6e9ef)"}`,
      boxShadow:"0 1px 2px rgba(15,23,42,.04)",
    }}>
      {bubbleContent}
    </div>
  );

  if (isUser) {
    return (
      <div style={{ display:"flex", flexDirection:"column", alignItems:"flex-end", gap:3, marginBottom:2 }}>
        <div style={{ display:"flex", alignItems:"flex-end", gap:7, maxWidth:"86%" }}>
          <div style={{ flex:1, minWidth:0 }}>{bubble}</div>
          <div style={{
            width:26, height:26, borderRadius:"50%", flexShrink:0,
            background:"linear-gradient(135deg,#e0e7ff,#c7d2fe)",
            display:"flex", alignItems:"center", justifyContent:"center",
          }}>
            <svg viewBox="0 0 24 24" fill="none" width="14" height="14">
              <circle cx="12" cy="8" r="4" fill="#6366f1"/>
              <path d="M4 20c0-4 3.6-7 8-7s8 3 8 7" fill="#6366f1"/>
            </svg>
          </div>
        </div>
        <div style={{ fontSize:12, color:"#64748b", paddingRight:33 }}>
          You · {new Date(message.timestamp).toLocaleTimeString()}
        </div>
      </div>
    );
  }

  return (
    <div style={{ display:"flex", flexDirection:"column", alignItems:"flex-start", gap:3, marginBottom:2 }}>
      <div style={{ display:"flex", alignItems:"flex-end", gap:7, maxWidth:"86%" }}>
        <div style={{
          width:26, height:26, borderRadius:"50%", flexShrink:0, overflow:"hidden",
          background:"linear-gradient(140deg,#fef3e7,#f5e3cf)",
          border:"1px solid #f3dfc4",
          boxShadow:"0 1px 4px rgba(234,88,12,.15)",
        }}>
          <img src="/SimCoder.png" alt="SimCoder" style={{ width:"100%", height:"100%", objectFit:"contain", padding:2, display:"block" }}/>
        </div>
        <div style={{ flex:1, minWidth:0 }}>{bubble}</div>
      </div>
      <div style={{ fontSize:12, color:"#64748b", paddingLeft:33 }}>
        SimCoder · {new Date(message.timestamp).toLocaleTimeString()}
      </div>
    </div>
  );
}); // end React.memo(ChatMessage)

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
            background: "#64748b",
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

function CheckpointBar({ checkpoint, checkpoints, activeLeafId, restoring, onRestore }) {
  if (!checkpoint) return null;
  const parent = checkpoint.parentId || null;
  const siblings = checkpoints.filter((c) => (c.parentId || null) === parent);
  const idx = siblings.findIndex((c) => c.id === checkpoint.id);
  const hasBranches = siblings.length > 1;
  const isActive = activeLeafId === checkpoint.id;
  // Switching a branch lands on the END of that branch — its deepest, most-recently-built
  // leaf — so the full depth of each branch is preserved (not just the branch-point node).
  // The per-message Restore below still reverts to this exact checkpoint.
  const leafOf = (cid) => {
    let cur = cid;
    for (let guard = 0; guard < 1000; guard++) {
      const kids = checkpoints.filter((c) => c.parentId === cur);
      if (!kids.length) return cur;
      kids.sort((a, b) => new Date(b.createdAt) - new Date(a.createdAt));
      cur = kids[0].id;
    }
    return cur;
  };
  const pill = {
    display: "inline-flex", alignItems: "center", gap: 4, padding: "1px 8px",
    borderRadius: 999, border: "1px solid var(--line)", background: "var(--panel-2)",
    fontSize: 11, fontWeight: 600, color: "var(--ink-3)", cursor: "pointer", fontFamily: "inherit",
  };
  const pager = { ...pill, padding: "0 6px", minWidth: 18, justifyContent: "center" };
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6, margin: "-4px 0 2px 38px", flexWrap: "wrap" }}>
      <span title={`Scene checkpoint · turn ${checkpoint.turnIndex} · ${checkpoint.actorCount} object(s)`}
        style={{ ...pill, cursor: "default", color: isActive ? "var(--blue)" : "var(--ink-3)", borderColor: isActive ? "var(--blue)" : "var(--line)" }}>
        {ICONS.map(11)} Checkpoint{isActive ? " · current" : ""}
      </span>
      {parent && (
        <button style={pill} disabled={restoring} title="Revert the live scene to how it was before this message"
          onClick={() => onRestore(parent)}>
          {restoring ? "Reverting…" : "↩ Undo this change"}
        </button>
      )}
      {hasBranches && (
        <span style={{ display: "inline-flex", alignItems: "center", gap: 3, color: "var(--ink-3)", fontSize: 11 }}
          title="Parallel branches that diverge from this point">
          <button style={pager} disabled={restoring}
            onClick={() => onRestore(leafOf(siblings[(idx - 1 + siblings.length) % siblings.length].id))}>‹</button>
          <span>branch {idx + 1}/{siblings.length}</span>
          <button style={pager} disabled={restoring}
            onClick={() => onRestore(leafOf(siblings[(idx + 1) % siblings.length].id))}>›</button>
        </span>
      )}
    </div>
  );
}

function ChatPanel({ onScreenshotUpdate, onRef, onSessionChange, onChatDone, codingAgent, setCodingAgent, codingModel }) {
  const [messages, setMessages] = useState(() => [buildWelcomeMessage()]);
  const [checkpoints, setCheckpoints] = useState([]);
  const [activeLeafId, setActiveLeafId] = useState(null);
  const [restoringId, setRestoringId] = useState(null);
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
  const selfEvolutionReqSeqRef = useRef(0);
  const activeSkills = autoSkillSelectionEnabled ? autoSelectedSkills : selectedSkills;
  const selfEvolutionReady = typeof selfEvolutionEnabled === "boolean";
  const selfEvolutionOn = selfEvolutionEnabled === true;

  // Expose methods to parent
  useEffect(() => {
    onRef?.({
      insertText: (text) =>
        setInput((prev) => (prev ? prev + "\n" + text : text)),
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
  }, [onRef]);

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

  // ── Checkpoint state mirrors (so async handlers read the latest values) ──
  const messagesRef = useRef(messages);
  const sessionIdRef = useRef(sessionId);
  const activeLeafRef = useRef(activeLeafId);
  const latestScreenshotRef = useRef(latestScreenshot);
  const turnCountRef = useRef(turnCount);
  // Checkpoints key off the STABLE studio session (STUDIO_SESSION from /api/session),
  // NOT the chat's `sessionId` — that changes every turn (each turn is a fresh `claude -p`),
  // which would scatter checkpoints across per-turn ids so only the latest turn's bar shows.
  const [studioSession, setStudioSession] = useState(null);
  const studioSessionRef = useRef(null);
  useEffect(() => { messagesRef.current = messages; }, [messages]);
  useEffect(() => { sessionIdRef.current = sessionId; }, [sessionId]);
  useEffect(() => { activeLeafRef.current = activeLeafId; }, [activeLeafId]);
  useEffect(() => { latestScreenshotRef.current = latestScreenshot; }, [latestScreenshot]);
  useEffect(() => { turnCountRef.current = turnCount; }, [turnCount]);

  // Fetch the stable studio session id once, then load its checkpoint tree.
  useEffect(() => {
    fetch(`${API_BASE}/session`)
      .then((r) => r.json())
      .then((d) => { if (d?.sessionId) { studioSessionRef.current = d.sessionId; setStudioSession(d.sessionId); } })
      .catch(() => {});
  }, []);
  useEffect(() => {
    if (!studioSession) return;
    listCheckpoints(studioSession)
      .then(async (cps) => {
        if (cps.length === 0) {
          // Auto-snapshot the initial (empty) world so the user can always revert to the
          // clean starting map. The base .umap is never modified — new sessions reload it.
          try {
            const sid = studioSession;
            const welcomeId = messagesRef.current?.[0]?.id || null;
            const rec = await createCheckpoint({
              sessionId: sid, ownerId: sid, parentCheckpointId: null,
              messageId: welcomeId, prompt: "Initial scene (empty world)", turnIndex: 0,
              chatHistory: messagesRef.current,
              thumbnailPath: screenshotPathFromUrl(latestScreenshotRef.current),
            });
            const withUrl = { ...rec, thumbnailUrl: rec.thumbnail ? `${API_BASE}/checkpoints/${sid}/${rec.id}/thumbnail` : null };
            setCheckpoints([withUrl]);
            setActiveLeafId(rec.id);
          } catch { setCheckpoints([]); }
        } else {
          setCheckpoints(cps);
          if (!activeLeafRef.current) setActiveLeafId(cps[cps.length - 1].id);
        }
      })
      .catch(() => {});
  }, [studioSession]);

  // Restore (or switch branch to) a checkpoint: revert the live scene + load that chat path.
  const handleRestoreCheckpoint = useCallback(async (id) => {
    const sid = studioSessionRef.current;
    if (!sid || restoringId) return;
    setRestoringId(id);
    try {
      await restoreCheckpoint(sid, id);
      const ck = await getCheckpoint(sid, id);
      if (ck && Array.isArray(ck.chatHistory)) setMessages(ck.chatHistory);
      setActiveLeafId(id);
      onChatDone?.();  // nudge the viewport/screenshot to refresh — the scene changed
    } catch (_e) {
      setMessages((prev) => [...prev, { id: generateMessageId(), role: "assistant", content: "⚠️ Could not restore that checkpoint.", timestamp: Date.now() }]);
    } finally {
      setRestoringId(null);
    }
  }, [restoringId, onChatDone]);

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
        waiting: true,  // Show "Waiting for {agent}..." until first event
        toolCalls: [],
        timestamp: Date.now(),
      };

      setMessages((prev) => [...prev, userMsg, assistantMsg]);
      setLoading(true);
      setTurnCount((c) => c + 1);

      const controller = new AbortController();
      abortRef.current = controller;
      const inputBuffers = new Map();

      // P2-2 SSE text batching: buffer rapid text deltas, flush via rAF to avoid
      // a React setState per character during fast streaming.
      let _textBuf = "";
      let _rafPending = false;
      const _flushTextBuf = (msgId) => {
        if (!_textBuf) return;
        let delta = _textBuf;
        _textBuf = "";
        _rafPending = false;
        setMessages((prev) => {
          const updated = [...prev];
          const idx = updated.findIndex((m) => m.id === msgId);
          if (idx === -1) return prev;
          const msg = { ...updated[idx] };
          const existingContent = msg.content || "";
          if (existingContent && delta.startsWith(existingContent)) {
            delta = delta.slice(existingContent.length);
          } else if (existingContent && existingContent.endsWith(delta)) {
            return prev;
          }
          if (!delta) return prev;
          const blocks = msg.blocks || [];
          const last = blocks[blocks.length - 1];
          if (last?.type === "text") {
            msg.blocks = [...blocks.slice(0, -1), { ...last, content: last.content + delta }];
          } else {
            msg.blocks = [...blocks, { type: "text", content: delta }];
          }
          msg.content = (msg.content || "") + delta;
          updated[idx] = msg;
          return updated;
        });
      };

      // NOTE: no absolute time limit on agent runs. Long scenes can legitimately
      // take 10+ minutes. Dead-connection detection lives inside sendChat's
      // reader-level idle timer (refreshed by server `: ping` heartbeats).
      // To stop a runaway agent, use the Stop button (handleStop → controller.abort).

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
                  if (event.data.sessionId) {
                    setSessionId(event.data.sessionId);
                    onSessionChange?.(event.data.sessionId);
                  }
                  break;
                }
                case "text": {
                  // P2-2: buffer into _textBuf, flush via rAF — avoids setState per character
                  _textBuf += event.data.delta || "";
                  if (!_rafPending) {
                    _rafPending = true;
                    requestAnimationFrame(() => _flushTextBuf(assistantId));
                  }
                  // Don't update msg here — the rAF flush handles it separately
                  return prev; // bail out of setMessages for text events
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
                      ? "**Warning:** Agent exited unexpectedly. Try again — each message starts a fresh process."
                      : "**Warning:** No response received. Try sending your message again.";
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
            agent: codingAgent,
            model: codingModel,
          }
        );
        // After a scene-changing turn, snapshot a checkpoint (branches from the active leaf).
        try {
          const finalMsgs = messagesRef.current;
          const aMsg = finalMsgs.find((m) => m.id === assistantId);
          const sid = studioSessionRef.current;
          if (sid && aMsg && turnChangedScene(aMsg)) {
            const rec = await createCheckpoint({
              sessionId: sid, ownerId: sid,
              parentCheckpointId: activeLeafRef.current,
              messageId: assistantId,
              prompt: text, turnIndex: turnCountRef.current,
              chatHistory: finalMsgs,
              thumbnailPath: screenshotPathFromUrl(latestScreenshotRef.current),
            });
            const withUrl = { ...rec, thumbnailUrl: rec.thumbnail ? `${API_BASE}/checkpoints/${sid}/${rec.id}/thumbnail` : null };
            setCheckpoints((prev) => [...prev, withUrl]);
            setActiveLeafId(rec.id);
          }
        } catch (_e) { /* checkpoint is best-effort — never block the chat */ }
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
        setLoading(false);
        setAutoSelectingSkills(false);
        abortRef.current = null;
      }
    },
    [input, loading, sessionId, selectedSkills, autoSkillSelectionEnabled, codingAgent, codingModel, onScreenshotUpdate]
  );

  const handleStop = () => {
    // Tell the server to actually kill the Claude subprocess. Without this,
    // aborting the SSE alone just leaves the agent running in background
    // (server-side e.on("close") no longer kills on disconnect).
    fetch(`${API_BASE}/chat-stop`, { method: "POST" }).catch(() => {});
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
    fetch(`${API_BASE}/chat-stop`, { method: "POST" }).catch(() => {});
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
    resetScene();  // wipe the UE scene so we start from scratch
    if (studioSessionRef.current) clearCheckpoints(studioSessionRef.current);
    setCheckpoints([]);
    setActiveLeafId(null);
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
        background: "var(--bg)",
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

      <SceneAgentHeader
        agentLabelText={agentLabel(codingAgent)}
        mcpStatus={mcpStatus}
        selfEvolutionReady={selfEvolutionReady}
        selfEvolutionOn={selfEvolutionOn}
        sessionId={sessionId}
        turnCount={turnCount}
        latestScreenshot={latestScreenshot}
        loading={loading}
        onToggleSelfEvolution={handleSelfEvolutionToggle}
        onAnnotate={() => setAnnotating(true)}
        onSave={handleSave}
        onShare={handleShare}
        onStop={handleStop}
        onReset={handleReset}
      />

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
        {messages.map((msg) => {
          const ckpt = checkpoints.find((c) => c.messageId === msg.id);
          return (
            <React.Fragment key={msg.id}>
              <ChatMessage message={msg} agentLabel={agentLabel(codingAgent)} />
              {ckpt && (
                <CheckpointBar
                  checkpoint={ckpt}
                  checkpoints={checkpoints}
                  activeLeafId={activeLeafId}
                  restoring={restoringId === ckpt.id}
                  onRestore={handleRestoreCheckpoint}
                />
              )}
            </React.Fragment>
          );
        })}
        {loading && lastMessage?.role === "user" && <TypingIndicator />}
        <div ref={scrollRef} />
      </div>

      {/* Quick suggestions */}
      {!loading && sessionId && turnCount > 0 && (
        <div
          style={{
            padding: "6px 14px",
            borderTop: "1px solid var(--line)",
            background: "var(--panel)",
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
                padding: "4px 11px",
                fontSize: 12,
                borderRadius: 999,
                border: "1px solid var(--line)",
                background: "var(--panel-2)",
                color: "var(--ink-2)",
                cursor: "pointer",
                fontWeight: 600,
                fontFamily: "inherit",
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
          borderTop: "1px solid var(--line)",
          flexShrink: 0,
          background: "var(--panel)",
        }}
      >
        <div
          style={{
            display: "flex",
            gap: 8,
            alignItems: "flex-end",
            background: "var(--bg-tertiary)",
            border: "1px solid var(--line)",
            borderRadius: 10,
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
              color: "var(--ink)",
              fontSize: 13,
              resize: "none",
              lineHeight: 1.5,
              maxHeight: 160,
              overflow: "auto",
              fontFamily: "inherit",
              cursor: "text",
            }}
          />
          <button
            onClick={() => (loading ? handleStop() : handleSend())}
            disabled={!loading && !input.trim()}
            style={{
              flexShrink: 0,
              width: 34,
              height: 34,
              borderRadius: 10,
              border: "none",
              background: loading ? "rgba(255,95,99,0.15)" : input.trim() ? "var(--blue)" : "var(--panel-2)",
              color: loading ? "var(--red)" : input.trim() ? "#ffffff" : "var(--ink-3)",
              cursor: loading || input.trim() ? "pointer" : "default",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontSize: 15,
              boxShadow: input.trim() && !loading ? "0 4px 10px rgba(37,99,235,.25)" : "none",
            }}
          >
            {loading ? "⏹" : "▶"}
          </button>
        </div>
        <div style={{ marginTop: 5, fontSize: 12, color: "var(--ink-3)" }}>
          Enter to send · Shift+Enter for new line
          {/* Agent/model picker moved to the top-left nav bar (see CodingAgentSelector). */}
          <span style={{ marginLeft: 8, color: autoSkillSelectionEnabled ? "var(--blue)" : "var(--ink-3)" }}>
            {autoSkillSelectionEnabled ? "Auto-select skills: on" : "Auto-select skills: off"}
            {autoSelectingSkills ? " (selecting...)" : ""}
          </span>          {activeSkills.length > 0 && (
            <span style={{ color: "var(--blue)", marginLeft: 8 }}>
              {activeSkills.length} skill{activeSkills.length > 1 ? "s" : ""} active
            </span>
          )}
          {autoSelectionError && (
            <span style={{ color: "var(--red)", marginLeft: 8 }}>{autoSelectionError}</span>
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
          background: "rgba(255,255,255,0.15)",
          border: "1px solid rgba(255,255,255,0.25)",
          borderRadius: 4,
          padding: "2px 8px",
          fontSize: 12,
          color: "#ffffff",
          fontFamily: "monospace",
          minWidth: 32,
          textAlign: "center",
        }}
      >
        {icon}
      </div>
      <div style={{ color: "var(--ink-3)", fontSize: 12 }}>{label}</div>
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
          <div style={{ fontSize: 48 }}>{ICONS.gamepad(48)}</div>
          <div style={{ color: "#ffffff", fontSize: 18, fontWeight: 600 }}>
            Click to Activate Stream
          </div>
          <div
            style={{
              color: "#cbd5e1",
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
              color: "var(--ink-3)",
              fontSize: 12,
            }}
          >
            <KeyHint icon={ICONS.mouse(14)} label="Click & drag to look" />
            <KeyHint icon={ICONS.keyboard(14)} label="WASD to move" />
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
            border: "1px solid var(--line)",
          }}
        >
          <KeyHint icon={ICONS.mouse(14)} label="Click & drag to look" />
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
            color: "var(--green)",
            fontSize: 12,
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
              background: "var(--green)",
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
            fontSize: 12,
            background: "rgba(0,0,0,0.6)",
            border: "1px solid var(--line)",
            borderRadius: 4,
            color: "var(--ink-3)",
            cursor: "pointer",
          }}
        >
          Deactivate
        </button>
      )}
    </div>
  );
}

// ── Reusable SVG Line Chart ───────────────────────────────────────────────────
function LineChart({ series, label, unit = "", color = "var(--blue)", W = 440, H = 80 }) {
  if (!series || series.length < 2) {
    return (
      <div style={{ width:W, height:H, display:"flex", alignItems:"center", justifyContent:"center",
        background:"var(--bg)", borderRadius:6, border:"1px solid var(--line)",
        color:"var(--ink-3)", fontSize:12 }}>
        {label}: no data yet
      </div>
    );
  }
  const pad = { t:8, r:6, b:22, l:36 };
  const iW = W - pad.l - pad.r;
  const iH = H - pad.t - pad.b;
  const minV = Math.min(...series), maxV = Math.max(...series);
  const rangeV = maxV - minV || 1;
  const toX = (i) => pad.l + (i / (series.length - 1)) * iW;
  const toY = (v) => pad.t + iH - ((v - minV) / rangeV) * iH;

  const pathD = series.map((v,i) => `${i===0?'M':'L'}${toX(i).toFixed(1)},${toY(v).toFixed(1)}`).join(' ');
  const areaD = `${pathD} L${toX(series.length-1).toFixed(1)},${pad.t+iH} L${pad.l},${pad.t+iH} Z`;

  // Tick values
  const ticks = [minV, (minV+maxV)/2, maxV].map(v => Math.round(v * 10) / 10);

  return (
    <svg width={W} height={H} style={{ display:"block", background:"var(--bg)", borderRadius:6, border:"1px solid var(--line)" }}>
      {/* Y grid + labels */}
      {ticks.map((v,i) => {
        const y = toY(v);
        return (
          <g key={i}>
            <line x1={pad.l} y1={y} x2={pad.l+iW} y2={y} stroke="var(--line)" strokeWidth={0.5} strokeDasharray="3,3" />
            <text x={pad.l-4} y={y+4} textAnchor="end" fontSize={10} fill="var(--ink-3)">{v}{unit}</text>
          </g>
        );
      })}
      {/* Area fill */}
      <path d={areaD} fill={color} opacity={0.12} />
      {/* Line */}
      <path d={pathD} fill="none" stroke={color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
      {/* Last value dot */}
      <circle cx={toX(series.length-1)} cy={toY(series[series.length-1])} r={3} fill={color} />
      {/* X label */}
      <text x={pad.l + iW/2} y={H-4} textAnchor="middle" fontSize={10} fill="var(--ink-3)" fontWeight={600}>{label}</text>
    </svg>
  );
}

// ─── Coding Agent Verifier Panel ─────────────────────────────────────────────
// Rule-based feedback (scene collisions) + LLM-based (VLM score) per coding turn

function CodingVerifierPanel({ sessionId, latestScreenshot }) {
  const [tab, setTab]         = useState("collisions");
  const [collData, setCollData] = useState(null);
  const [scores, setScores]   = useState([]);
  const [checking, setChecking] = useState(false);
  const [vlmRunning, setVlmRunning] = useState(false);
  const [vlmError, setVlmError] = useState(null);
  const metrics   = useMetrics();
  const sceneCollHistory = (metrics.sceneCollisions || []).map(c => c.count);

  const runCollisionCheck = useCallback(async () => {
    setChecking(true);
    try {
      // /api/scene-check runs UE Python AABB overlap + floating detection
      // Works in editor mode (no PIE needed), no UnrealCV vget required
      const data = await fetch(`${API_BASE}/scene-check`, {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({}),
      }).then(r => r.json());
      setCollData(data);
    } catch { setCollData(null); }
    finally { setChecking(false); }
  }, []);

  const runVlmScore = useCallback(async () => {
    setVlmRunning(true); setVlmError(null);
    try {
      const resp = await fetch(`${API_BASE}/screenshot/latest?t=${Date.now()}`);
      if (!resp.ok) throw new Error("No screenshot");
      const blob = await resp.blob();
      const base64 = await new Promise(resolve => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.readAsDataURL(blob);
      });
      // Send to VLM scoring endpoint
      const result = await fetch(`${API_BASE}/vlm-score`, {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({ imageDataUrl: base64, sessionId }),
      }).then(r => r.json());
      setScores(prev => [...prev.slice(-9), {
        ts: Date.now(), score: result.score, feedback: result.feedback,
        screenshot: base64, label: result.label,
      }]);
    } catch(e) { setVlmError(e.message); }
    finally { setVlmRunning(false); }
  }, [sessionId]);

  // Auto-trigger both checkers when scene screenshot changes
  // Defined AFTER runVlmScore to avoid TDZ in the dependency array
  const prevScreenRef = useRef(null);
  useEffect(() => {
    if (!latestScreenshot || latestScreenshot === prevScreenRef.current) return;
    prevScreenRef.current = latestScreenshot;
    runCollisionCheck();
    runVlmScore();
  }, [latestScreenshot, runCollisionCheck, runVlmScore]);

  const tabs = [
    { id:"collisions", label:"Rule-based Checker" },
    { id:"vlm",        label:"VLM Score" },
  ];

  const collCount  = collData?.collision_count ?? "—";
  const floatCount = collData?.floating_count  ?? "—";
  const collColor  = typeof collCount  === "number" ? (collCount  === 0 ? "#16a34a" : "#dc2626") : "var(--ink-3)";
  const floatColor = typeof floatCount === "number" ? (floatCount === 0 ? "#16a34a" : "#f59e0b") : "var(--ink-3)";

  const FS = "var(--fs-body)"; // 13px minimum throughout

  return (
    <div style={{ display:"flex", flexDirection:"column", height:"100%", background:"var(--bg)" }}>
      {/* Tab bar + action button */}
      <div style={{ display:"flex", alignItems:"center", padding:"6px 10px",
        borderBottom:"1px solid var(--line)", flexShrink:0, gap:6 }}>
        <div style={{ display:"flex", gap:4, flex:1 }}>
          {tabs.map(t => (
            <button key={t.id} onClick={() => setTab(t.id)} style={{
              padding:"5px 12px", fontSize:FS, borderRadius:6, border:"none", cursor:"pointer",
              background: tab===t.id?"var(--orange)":"var(--bg)",
              color: tab===t.id?"#fff":"var(--ink-2)",
              fontWeight: tab===t.id?700:500, fontFamily:"inherit",
            }}>{t.label}</button>
          ))}
        </div>
        {tab === "collisions" && (
          <button onClick={runCollisionCheck} disabled={checking}
            style={{ fontSize:FS, padding:"5px 12px", borderRadius:6,
              border:"1px solid var(--line)", background:"var(--panel)",
              cursor:"pointer", color:"var(--ink-2)", fontFamily:"inherit" }}>
            {checking ? "Checking…" : "↻ Check"}
          </button>
        )}
        {tab === "vlm" && (
          <button onClick={runVlmScore} disabled={vlmRunning}
            style={{ fontSize:FS, padding:"5px 12px", borderRadius:6,
              border:"1px solid var(--line)", background:"var(--panel)",
              cursor:"pointer", color:"var(--ink-2)", fontFamily:"inherit" }}>
            {vlmRunning ? "Scoring…" : "↻ Score"}
          </button>
        )}
      </div>

      {/* Body */}
      <div style={{ flex:1, overflow:"auto", padding:8 }}>
        {tab === "collisions" && (
          collData ? (
            <div>
              {/* Stat chips */}
              <div style={{ display:"flex", gap:5, marginBottom:10 }}>
                {[
                  { val:collData.collision_count ?? 0,      label:"Collisions", color:collColor },
                  { val:collData.floating_count  ?? 0,      label:"Floating",   color:floatColor },
                  { val:collData.checked_actors_count ?? 0, label:"Actors",     color:"var(--ink-2)" },
                ].map(({val,label,color}) => (
                  <div key={label} style={{ flex:1, textAlign:"center", padding:"8px 4px",
                    background:"var(--panel)", borderRadius:8, border:"1px solid var(--line)" }}>
                    <div style={{ fontSize:24, fontWeight:900, color, lineHeight:1 }}>{val}</div>
                    <div style={{ fontSize:11, color:"var(--ink-3)", marginTop:3 }}>{label}</div>
                  </div>
                ))}
              </div>
              {/* Collision history chart */}
              {sceneCollHistory.length > 1 && (
                <div style={{ marginBottom:10 }}>
                  <LineChart series={sceneCollHistory} label="Collision history" color="#dc2626" W={380} H={72} />
                </div>
              )}
              {/* Collision pairs */}
              {(collData.collision_pairs||[]).slice(0,5).map((p,i) => (
                <div key={i} style={{ fontSize:FS, padding:"6px 8px",
                  background:"rgba(220,38,38,.06)", borderRadius:6, marginBottom:4,
                  borderLeft:"3px solid #dc2626", color:"var(--ink-2)", lineHeight:1.4 }}>
                  <strong>{p.actor1}</strong> ↔ <strong>{p.actor2}</strong>
                  <span style={{ color:"var(--ink-3)", marginLeft:6, fontSize:12 }}>
                    {p.collision_type} · {Math.round(p.penetration_depth)}cm
                  </span>
                </div>
              ))}
              {collData.collision_count === 0 && (
                <div style={{ fontSize:FS, color:"#16a34a", textAlign:"center", padding:"8px 0 4px", fontWeight:600 }}>
                  No collisions detected
                </div>
              )}
              {/* Floating actors */}
              {(collData.floating_actors||[]).length > 0 && (
                <div style={{ marginTop:8 }}>
                  <div style={{ fontSize:11, fontWeight:600, color:"var(--ink-3)", marginBottom:4 }}>
                    FLOATING ACTORS
                  </div>
                  {(collData.floating_actors||[]).slice(0,5).map((f,i) => (
                    <div key={i} style={{ fontSize:FS, padding:"5px 8px",
                      background: f.no_surface ? "rgba(239,68,68,.08)" : "rgba(245,158,11,.08)",
                      borderRadius:6, marginBottom:3,
                      borderLeft: `3px solid ${f.no_surface ? "#ef4444" : "#f59e0b"}`,
                      color:"var(--ink-2)" }}>
                      <strong>{f.name}</strong>
                      <span style={{ color:"var(--ink-3)", marginLeft:6, fontSize:12 }}>
                        {f.no_surface
                          ? "no surface below"
                          : `+${Math.round(f.gap_cm)}cm above surface (Z=${f.surface_z})`}
                      </span>
                    </div>
                  ))}
                </div>
              )}
              {collData.floating_count === 0 && collData.collision_count === 0 && (
                <div style={{ fontSize:FS, color:"var(--ink-3)", textAlign:"center", paddingBottom:8 }}>
                  All actors grounded · no overlaps
                </div>
              )}
            </div>
          ) : (
            <div style={{ fontSize:FS, color:"var(--ink-3)", textAlign:"center", padding:16 }}>
              {checking ? "Checking…" : "Click ↻ Check to run collision detection"}
            </div>
          )
        )}
        {tab === "vlm" && (
          <div>
            {vlmError && <div style={{ fontSize:FS, color:"#dc2626", marginBottom:6 }}>{vlmError}</div>}
            {scores.length === 0 && !vlmRunning && (
              <div style={{ fontSize:FS, color:"var(--ink-3)", textAlign:"center", padding:16 }}>
                Click ↻ Score to evaluate the current scene with VLM
              </div>
            )}
            {scores.slice().reverse().map((s,i) => (
              <div key={i} style={{ marginBottom:8, padding:8, background:"var(--panel)",
                borderRadius:8, border:"1px solid var(--line)" }}>
                <div style={{ display:"flex", alignItems:"center", gap:8, marginBottom:4 }}>
                  <span style={{ fontSize:20, fontWeight:800,
                    color: s.score >= 7?"#16a34a":s.score >= 4?"#f59e0b":"#dc2626" }}>
                    {s.score}<span style={{ fontSize:13, fontWeight:500, color:"var(--ink-3)" }}>/10</span>
                  </span>
                  {s.label && <span style={{ fontSize:12, background:"var(--bg)", borderRadius:4,
                    padding:"2px 7px", color:"var(--ink-2)", fontWeight:600 }}>{s.label}</span>}
                  <span style={{ fontSize:12, color:"var(--ink-3)", marginLeft:"auto" }}>
                    {new Date(s.ts).toLocaleTimeString()}
                  </span>
                </div>
                {s.feedback && <div style={{ fontSize:FS, color:"var(--ink-2)", lineHeight:1.5 }}>{s.feedback}</div>}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// ─── BattleSide ──────────────────────────────────────────────────────────────

function BattleSide({ label, side, isWinner, isLoser, revealed, onVote }) {
  const borderColor = isWinner ? "#16a34a" : isLoser ? "#dc262633" : "var(--line)";

  return (
    <div
      style={{
        border: `2px solid ${borderColor}`,
        borderRadius: 12,
        overflow: "hidden",
        background: "var(--panel)",
        transition: "border-color 0.2s",
        opacity: isLoser ? 0.6 : 1,
      }}
    >
      <div
        style={{
          padding: "10px 16px",
          borderBottom: "1px solid var(--line)",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
        }}
      >
        <span style={{ fontSize: 14, fontWeight: 600, color: "var(--ink)" }}>
          {label}
          {isWinner && <span style={{ display:"inline-flex", alignItems:"center", marginLeft:4, color:"var(--green)" }}>{ICONS.check(12)}</span>}
        </span>
        {revealed && side && (
          <span
            style={{
              fontSize: 12,
              color: "var(--ink-3)",
              background: "var(--panel-2)",
              padding: "2px 8px",
              borderRadius: 4,
            }}
          >
            {side.agentName}
          </span>
        )}
        {!revealed && (
          <span style={{ fontSize: 12, color: "var(--ink-2)" }}>Identity hidden</span>
        )}
      </div>

      <div
        style={{
          height: 250,
          background: "var(--bg)",
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
          <div style={{ color: "var(--ink-2)", fontSize: 13 }}>
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
              background: "var(--green)",
              border: "1px solid var(--green)",
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
  background: "var(--panel-2)",
  border: "1px solid var(--line)",
  borderRadius: 6,
  color: "var(--ink-3)",
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
        background: "var(--bg)",
      }}
    >
      {/* Header */}
      <div
        style={{
          padding: "16px 24px",
          borderBottom: "1px solid var(--line)",
          display: "flex",
          alignItems: "center",
          gap: 12,
        }}
      >
        <span style={{ fontSize: 24, display: "inline-flex", alignItems: "center" }}>{ICONS.swords(24)}</span>
        <div>
          <div style={{ fontSize: 18, fontWeight: 700, color: "var(--ink)" }}>Arena Battle</div>
          <div style={{ fontSize: 12, color: "var(--ink-3)" }}>
            Two agents generate scenes from the same prompt. You decide which is better.
          </div>
        </div>
        <div style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
          <button
            onClick={() => setShowAgents(!showAgents)}
            style={{
              padding: "4px 12px",
              fontSize: 12,
              borderRadius: 4,
              border: "1px solid var(--line)",
              background: showAgents ? "#eff4ff" : "#e6e9ef",
              color: showAgents ? "var(--blue)" : "var(--ink-3)",
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
            borderBottom: "1px solid var(--line)",
            background: "var(--panel)",
          }}
        >
          <div
            style={{
              fontSize: 12,
              color: "var(--ink-3)",
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
                  border: `1px solid ${agent.enabled ? "var(--green)" : "var(--line)"}`,
                  background: agent.enabled ? "var(--green-soft)" : "transparent",
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
                  style={{ accentColor: "#15803d" }}
                />
                <div>
                  <div style={{ fontSize: 12, fontWeight: 600, color: "var(--ink)" }}>
                    {agent.name}
                  </div>
                  <div style={{ fontSize: 12, color: "var(--ink-3)" }}>
                    {agent.type}
                    {agent.model ? ` (${agent.model})` : ""}
                  </div>
                  {agent.description && (
                    <div style={{ fontSize: 12, color: "var(--ink-2)", marginTop: 2 }}>
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
                color: "var(--ink)",
                marginBottom: 8,
              }}
            >
              Enter a Scene Prompt
            </div>
            <div style={{ fontSize: 14, color: "var(--ink-3)", marginBottom: 24 }}>
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
                background: "var(--panel)",
                border: "1px solid var(--line)",
                borderRadius: 8,
                color: "var(--ink)",
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
                  background: prompt.trim() ? "#15803d" : "#e6e9ef",
                  border: `1px solid ${prompt.trim() ? "var(--green)" : "var(--line)"}`,
                  borderRadius: 8,
                  color: prompt.trim() ? "#fff" : "#94a3b8",
                  cursor: prompt.trim() ? "pointer" : "default",
                  fontWeight: 600,
                }}
              >
                Start Battle
              </button>
            </div>
            <div style={{ fontSize: 12, color: "var(--ink-2)", marginTop: 12 }}>
              {agents.filter((a) => a.enabled).length} agent
              {agents.filter((a) => a.enabled).length !== 1 ? "s" : ""} enabled
            </div>
          </div>
        )}

        {/* Generating phase */}
        {phase === "generating" && (
          <div style={{ textAlign: "center", padding: 80 }}>
            <div style={{ fontSize: 40, marginBottom: 16 }}>{ICONS.swords(40)}</div>
            <div style={{ fontSize: 16, color: "var(--ink)", fontWeight: 600 }}>
              Generating scenes...
            </div>
            <div style={{ fontSize: 13, color: "var(--ink-3)", marginTop: 8 }}>
              "{prompt}"
            </div>
            {progress && (
              <div style={{ marginTop: 20 }}>
                {progress.phase === "starting" && (
                  <div style={{ fontSize: 12, color: "var(--blue)" }}>
                    Matched: {progress.agentA} vs {progress.agentB}
                  </div>
                )}
                {progress.phase === "generating_a" && (
                  <div style={{ fontSize: 12, color: "var(--green)" }}>
                    Agent A ({progress.agent}) is generating...
                  </div>
                )}
                {progress.phase === "generating_b" && (
                  <div style={{ fontSize: 12, color: "var(--green)" }}>
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
                background: "var(--panel-2)",
                borderRadius: 2,
                overflow: "hidden",
                margin: "24px auto 0",
              }}
            >
              <div
                style={{
                  width: progress?.phase === "generating_b" ? "80%" : "40%",
                  height: "100%",
                  background: "var(--blue)",
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
                border: "1px solid var(--line)",
                borderRadius: 6,
                color: "var(--ink-3)",
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
              <div style={{ fontSize: 13, color: "var(--ink-3)" }}>Prompt</div>
              <div style={{ fontSize: 16, color: "var(--ink)", fontWeight: 500 }}>
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
                    color: "var(--green)",
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
                  <div style={{ fontSize: 12, color: "var(--ink-3)" }}>
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
                      background: "var(--blue)",
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
                      background: shared ? "var(--panel-2)" : "var(--green)",
                      border: `1px solid ${shared ? "var(--line)" : "var(--green)"}`,
                      borderRadius: 6,
                      color: shared ? "var(--ink-3)" : "#fff",
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
        background: "var(--bg)",
      }}
    >
      <div
        style={{
          padding: "16px 24px",
          borderBottom: "1px solid var(--line)",
          display: "flex",
          alignItems: "center",
          gap: 12,
        }}
      >
        <span style={{ fontSize: 24, display: "inline-flex", alignItems: "center" }}>{ICONS.trophy(24)}</span>
        <div>
          <div style={{ fontSize: 18, fontWeight: 700, color: "var(--ink)" }}>Leaderboard</div>
          <div style={{ fontSize: 12, color: "var(--ink-3)" }}>
            Agent rankings based on Elo ratings from arena battles
          </div>
        </div>
        {entries.length > 0 && (
          <div style={{ marginLeft: "auto", fontSize: 12, color: "var(--ink-2)" }}>
            {entries.reduce((sum, e) => sum + e.numBattles, 0)} total battles
          </div>
        )}
      </div>

      <div style={{ flex: 1, overflow: "auto", padding: 24 }}>
        {loading ? (
          <div style={{ textAlign: "center", padding: 60, color: "var(--ink-3)" }}>Loading...</div>
        ) : entries.length === 0 ? (
          <div style={{ textAlign: "center", padding: 60 }}>
            <div style={{ fontSize: 40, marginBottom: 12 }}>{ICONS.trophy(40)}</div>
            <div
              style={{
                fontSize: 16,
                color: "var(--ink)",
                fontWeight: 600,
                marginBottom: 8,
              }}
            >
              No battles yet
            </div>
            <div style={{ fontSize: 13, color: "var(--ink-3)" }}>
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
                fontSize: 12,
                color: "var(--ink-2)",
                fontWeight: 600,
                borderBottom: "1px solid var(--line)",
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
                    borderBottom: "1px solid var(--line)",
                    background: i === 0 ? "var(--blue-soft)" : "transparent",
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
                              : "#94a3b8",
                    }}
                  >
                    {i === 0 ? ICONS.gold(18) : i === 1 ? ICONS.silver(18) : i === 2 ? ICONS.bronze(18) : i + 1}
                  </span>
                  <div>
                    <span style={{ fontSize: 14, fontWeight: 600, color: "var(--ink)" }}>
                      {entry.agentName}
                    </span>
                    {i === 0 && (
                      <span
                        style={{
                          marginLeft: 8,
                          fontSize: 12,
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
                            ? "#16a34a"
                            : entry.rating >= 1000
                              ? "#0f172a"
                              : "#dc2626",
                      }}
                    >
                      {Math.round(entry.rating)}
                    </span>
                    <span
                      style={{
                        fontSize: 12,
                        marginLeft: 4,
                        color: delta >= 0 ? "#16a34a" : "#dc2626",
                      }}
                    >
                      {delta >= 0 ? "+" : ""}
                      {delta}
                    </span>
                  </div>
                  <span style={{ textAlign: "right", fontSize: 13, color: "var(--ink-3)" }}>
                    {entry.numBattles}
                  </span>
                  <span style={{ textAlign: "right", fontSize: 13, color: "var(--green)" }}>
                    {entry.wins}
                  </span>
                  <span style={{ textAlign: "right", fontSize: 13, color: "var(--red)" }}>
                    {entry.losses}
                  </span>
                  <span
                    style={{
                      textAlign: "right",
                      fontSize: 13,
                      fontWeight: 600,
                      color:
                        entry.winRate >= 0.6
                          ? "#16a34a"
                          : entry.winRate >= 0.4
                            ? "#0f172a"
                            : "#dc2626",
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
                background: "var(--panel)",
                border: "1px solid var(--line)",
                fontSize: 12,
                color: "var(--ink-3)",
                lineHeight: 1.6,
              }}
            >
              <div style={{ fontWeight: 600, marginBottom: 4, color: "var(--ink)" }}>
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
        border: "1px solid var(--line)",
        background: "var(--panel)",
        cursor: "pointer",
        transition: "border-color 0.15s, transform 0.15s",
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.borderColor = "var(--blue)";
        e.currentTarget.style.transform = "translateY(-2px)";
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.borderColor = "var(--line)";
        e.currentTarget.style.transform = "translateY(0)";
      }}
    >
      <div style={{ height: 180, background: "var(--bg)", overflow: "hidden" }}>
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
              color: "var(--ink-2)",
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
            color: "var(--ink)",
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
              fontSize: 12,
              padding: "2px 6px",
              borderRadius: 4,
              background: "var(--blue-soft)",
              color: "var(--blue)",
              border: "1px solid rgba(76,141,255,0.26)",
            }}
          >
            {scene.agentName}
          </span>
          {scene.tags.slice(0, 2).map((tag) => (
            <span
              key={tag}
              style={{
                fontSize: 12,
                padding: "1px 5px",
                borderRadius: 4,
                background: "var(--panel-2)",
                color: "var(--ink-3)",
              }}
            >
              {tag}
            </span>
          ))}
          <span style={{ marginLeft: "auto", fontSize: 12, color: "var(--ink-2)" }}>
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
          background: "var(--bg)",
          border: "1px solid var(--line)",
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
            borderBottom: "1px solid var(--line)",
            display: "flex",
            alignItems: "center",
          }}
        >
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 16, fontWeight: 600, color: "var(--ink)" }}>{scene.prompt}</div>
            <div style={{ fontSize: 12, color: "var(--ink-3)", marginTop: 4 }}>
              Generated by {scene.agentName} on {new Date(scene.created_at).toLocaleString()}
            </div>
          </div>
          {onBack ? (
            <button
              onClick={onBack}
              style={{
                padding: "4px 10px",
                fontSize: 12,
                background: "var(--panel-2)",
                border: "1px solid var(--line)",
                borderRadius: 6,
                color: "var(--ink)",
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
                color: "var(--ink-3)",
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
                color: "var(--ink-2)",
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
                    background: i === currentImg ? "var(--blue)" : "var(--ink-3)",
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
                  fontSize: 12,
                  padding: "2px 7px",
                  borderRadius: 4,
                  background: "var(--blue-soft)",
                  color: "var(--blue)",
                  border: "1px solid rgba(76,141,255,0.26)",
                }}
              >
                {s}
              </span>
            ))}
            {scene.tags.map((tag) => (
              <span
                key={tag}
                style={{
                  fontSize: 12,
                  padding: "2px 7px",
                  borderRadius: 4,
                  background: "var(--panel-2)",
                  color: "var(--ink-3)",
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
                  color: "var(--ink-3)",
                  fontWeight: 600,
                  marginBottom: 6,
                }}
              >
                Generated Code Preview
              </div>
              <pre
                style={{
                  fontSize: 12,
                  color: "var(--ink)",
                  background: "var(--panel)",
                  border: "1px solid var(--line)",
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
        background: "var(--bg)",
      }}
    >
      {/* Header */}
      <div
        style={{
          padding: "16px 24px",
          borderBottom: "1px solid var(--line)",
          display: "flex",
          alignItems: "center",
          gap: 12,
        }}
      >
        <span style={{ fontSize: 24, display: "inline-flex", alignItems: "center" }}>{ICONS.frame(24)}</span>
        <div>
          <div style={{ fontSize: 18, fontWeight: 700, color: "var(--ink)" }}>Gallery</div>
          <div style={{ fontSize: 12, color: "var(--ink-3)" }}>
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
              fontSize: 12,
              borderRadius: 4,
              border: "1px solid var(--line)",
              background: "var(--panel)",
              color: "var(--ink-3)",
              cursor: "pointer",
            }}
          >
            <option value="newest">Newest first</option>
            <option value="oldest">Oldest first</option>
          </select>
          <span style={{ fontSize: 12, color: "var(--ink-2)" }}>
            {filtered.length} scene{filtered.length !== 1 ? "s" : ""}
          </span>
        </div>
      </div>

      {/* Tag filter */}
      {allTags.length > 0 && (
        <div
          style={{
            padding: "8px 24px",
            borderBottom: "1px solid var(--line)",
            display: "flex",
            gap: 6,
            flexWrap: "wrap",
            alignItems: "center",
          }}
        >
          <span style={{ fontSize: 12, color: "var(--ink-2)", marginRight: 4 }}>Tags:</span>
          <button
            onClick={() => setActiveTag(null)}
            style={{
              fontSize: 12,
              padding: "2px 8px",
              borderRadius: 10,
              border: `1px solid ${activeTag ? "var(--line)" : "var(--blue)"}`,
              background: activeTag ? "transparent" : "var(--blue-soft)",
              color: activeTag ? "var(--ink-3)" : "var(--blue)",
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
                fontSize: 12,
                padding: "2px 8px",
                borderRadius: 10,
                border: `1px solid ${activeTag === tag ? "var(--blue)" : "var(--line)"}`,
                background: activeTag === tag ? "var(--blue-soft)" : "transparent",
                color: activeTag === tag ? "var(--blue)" : "var(--ink-3)",
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
          <div style={{ textAlign: "center", padding: 60, color: "var(--ink-3)" }}>Loading...</div>
        ) : filtered.length === 0 ? (
          <div style={{ textAlign: "center", padding: 60 }}>
            <div style={{ fontSize: 40, marginBottom: 12 }}>{ICONS.frame(40)}</div>
            <div
              style={{
                fontSize: 16,
                color: "var(--ink)",
                fontWeight: 600,
                marginBottom: 8,
              }}
            >
              No scenes yet
            </div>
            <div style={{ fontSize: 13, color: "var(--ink-3)" }}>
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
  const desc = skill.description.length > 120
    ? skill.description.slice(0, 120) + "…" : skill.description;
  return (
    <div onClick={onClick} style={{
        padding: "14px 16px", borderRadius: 10, border: "1px solid var(--line)",
        background: "var(--panel)", cursor: "pointer",
        transition: "border-color 0.15s, box-shadow 0.15s",
        display: "flex", flexDirection: "column", gap: 8,
      }}
      onMouseEnter={e => { e.currentTarget.style.borderColor = "var(--blue)"; e.currentTarget.style.boxShadow = "0 2px 12px rgba(76,141,255,0.1)"; }}
      onMouseLeave={e => { e.currentTarget.style.borderColor = "var(--line)";  e.currentTarget.style.boxShadow = "none"; }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 6, minWidth: 0 }}>
        <span style={{ fontSize: 13, fontWeight: 600, color: "var(--ink)", flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {skill.name}
        </span>
        {isNew && <Badge variant="red" dot>NEW</Badge>}
        <SourceBadge source={skill.source} />
      </div>
      <div style={{ fontSize: 12, color: "var(--ink-3)", lineHeight: 1.5 }}>{desc}</div>
      {skill.tags.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
          {skill.tags.map(tag => <TagChip key={tag} tag={tag} />)}
        </div>
      )}
      <div style={{
        display: "flex", alignItems: "center", gap: 8, marginTop: "auto",
        paddingTop: 4, borderTop: "1px solid var(--line)", fontSize: 11, color: "var(--ink-3)",
      }}>
        <span>v{skill.version}</span>
        <span style={{ marginLeft: "auto" }}>{skill.author}</span>
      </div>
    </div>
  );
}

function SkillPageDetailModal({ skill, onClose, onDelete }) {
  return (
    <ModalOverlay onClose={onClose}>
      <ModalHeader
        title={skill.name}
        subtitle={<>v{skill.version} by {skill.author} <SourceBadge source={skill.source} style={{ marginLeft: 6 }} /></>}
        onClose={onClose}
        closeIcon={ICONS.close(14)}
      >
        {onDelete && <Btn variant="danger" onClick={onDelete}>Delete</Btn>}
      </ModalHeader>

      <div style={{ padding: "12px 20px", borderBottom: "1px solid var(--line)" }}>
        <div style={{ fontSize: 13, color: "var(--ink)", lineHeight: 1.6 }}>{skill.description}</div>
        {skill.tags.length > 0 && (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginTop: 10 }}>
            {skill.tags.map(tag => <TagChip key={tag} tag={tag} />)}
          </div>
        )}
        {skill.dependencies.length > 0 && (
          <div style={{ fontSize: 12, color: "var(--ink-3)", marginTop: 10 }}>
            <span style={{ fontWeight: 600 }}>Dependencies: </span>
            {skill.dependencies.map((dep, i) => (
              <span key={dep}>
                <span style={{ color: "var(--blue)" }}>{dep}</span>
                {i < skill.dependencies.length - 1 ? ", " : ""}
              </span>
            ))}
          </div>
        )}
      </div>

      <div style={{ flex: 1, overflow: "auto", padding: "14px 20px" }}>
        <Eyebrow style={{ marginBottom: 8 }}>Skill Content</Eyebrow>
        <pre style={{
          fontSize: 12, color: "var(--ink-2)", lineHeight: 1.6,
          whiteSpace: "pre-wrap", wordBreak: "break-word",
          fontFamily: "ui-monospace, 'Cascadia Code', Menlo, monospace",
          margin: 0, background: "var(--bg-tertiary)",
          border: "1px solid var(--line)", borderRadius: 8, padding: 16,
        }}>
          {skill.content}
        </pre>
      </div>
    </ModalOverlay>
  );
}

function SkillPageCreateModal({ onClose, onCreated }) {
  const [id, setId] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [tags, setTags] = useState("");
  const [content, setContent] = useState("# My Custom Skill\n\n## Overview\nDescribe what this skill does.\n\n## Instructions\nProvide detailed instructions for the AI agent.\n");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  const handleSave = async () => {
    if (!id.trim() || !name.trim() || !content.trim()) { setError("ID, name, and content are required"); return; }
    if (!/^[a-z0-9_]+$/.test(id)) { setError("ID must be lowercase letters, numbers, and underscores only"); return; }
    setSaving(true);
    try {
      await createSkill({ id: id.trim(), name: name.trim(), description: description.trim(),
        tags: tags.split(",").map(t => t.trim()).filter(Boolean), content: content.trim() });
      onCreated();
    } catch { setError("Failed to save skill"); } finally { setSaving(false); }
  };

  return (
    <ModalOverlay onClose={onClose} maxWidth={650}>
      <ModalHeader title="Create Custom Skill" onClose={onClose} closeIcon={ICONS.close(14)} />

      <div style={{ flex: 1, overflow: "auto", padding: "16px 20px", display: "flex", flexDirection: "column", gap: 14 }}>
        <Field label="Skill ID (lowercase, no spaces)">
          <input value={id} onChange={e => setId(e.target.value)} placeholder="my_custom_skill" style={inputSx} />
        </Field>
        <Field label="Name">
          <input value={name} onChange={e => setName(e.target.value)} placeholder="My Custom Skill" style={inputSx} />
        </Field>
        <Field label="Description (short summary)">
          <input value={description} onChange={e => setDescription(e.target.value)} placeholder="What this skill teaches the agent to do" style={inputSx} />
        </Field>
        <Field label="Tags (comma-separated)">
          <input value={tags} onChange={e => setTags(e.target.value)} placeholder="buildings, layout, custom" style={inputSx} />
        </Field>
        <Field label="Content (Markdown — instructions for the AI agent)" style={{ flex: 1 }}>
          <textarea value={content} onChange={e => setContent(e.target.value)}
            style={{ ...inputSx, height: 200, resize: "vertical", fontFamily: "ui-monospace, Menlo, monospace", lineHeight: 1.5 }} />
        </Field>
        {error && <div style={{ fontSize: 12, color: "var(--red)" }}>{error}</div>}
      </div>

      <ModalFooter>
        <Btn variant="cancel" onClick={onClose}>Cancel</Btn>
        <Btn variant="success" disabled={saving} onClick={handleSave}>{saving ? "Saving…" : "Create Skill"}</Btn>
      </ModalFooter>
    </ModalOverlay>
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
          background: "var(--bg)",
          border: "1px solid var(--line)",
          borderRadius: 12,
          padding: 24,
        }}
      >
        <div style={{ fontSize: 14, color: "var(--ink)", fontWeight: 600, marginBottom: 8 }}>
          Confirm Delete
        </div>
        <div style={{ fontSize: 13, color: "var(--ink-3)", lineHeight: 1.5, marginBottom: 20 }}>
          {message}
        </div>
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <button
            onClick={onCancel}
            style={{
              padding: "6px 14px",
              fontSize: 12,
              background: "var(--panel-2)",
              border: "1px solid var(--line)",
              borderRadius: 6,
              color: "var(--ink)",
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
              background: "var(--red)",
              border: "1px solid rgba(255,95,99,0.3)",
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
        background: "var(--bg)",
      }}
    >
      <PageHeader
        icon={ICONS.book(22)}
        title="Skills"
        subtitle="Browse, create, and manage skills that teach the AI agent new capabilities"
        action={<Btn variant="success" size="md" onClick={() => setShowCreate(true)}>+ Create Skill</Btn>}
      />

      <div
        style={{
          padding: "10px 24px",
          borderBottom: "1px solid var(--line)",
          display: "flex",
          gap: 10,
          alignItems: "center",
        }}
      >
        <input type="text" value={search} onChange={e => setSearch(e.target.value)}
          placeholder="Search skills…" style={{ ...inputSx, flex: 1 }} />
        <div style={{ display: "flex", gap: 4 }}>
          {["all", "builtin", "custom"].map(f => (
            <button key={f} onClick={() => setFilter(f)}
              className={`filter-pill${filter === f ? " active" : ""}`}>
              {f}
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
            <div style={{ fontSize: 40, marginBottom: 12 }}>{ICONS.book(40)}</div>
            <div style={{ fontSize: 16, color: "var(--ink)", fontWeight: 600, marginBottom: 8 }}>
              No skills found
            </div>
            <div style={{ fontSize: 13, color: "var(--ink-3)" }}>
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
  const successRate = tool.metrics?.usageCount > 0
    ? Math.round((tool.metrics.successCount / tool.metrics.usageCount) * 100) : null;

  return (
    <div onClick={onClick} style={{
        padding: "14px 16px", borderRadius: 10, border: "1px solid var(--line)",
        background: "var(--panel)", cursor: "pointer",
        transition: "border-color 0.15s, box-shadow 0.15s",
        display: "flex", flexDirection: "column", gap: 8,
      }}
      onMouseEnter={e => { e.currentTarget.style.borderColor = "var(--blue)"; e.currentTarget.style.boxShadow = "0 2px 12px rgba(76,141,255,0.1)"; }}
      onMouseLeave={e => { e.currentTarget.style.borderColor = "var(--line)";  e.currentTarget.style.boxShadow = "none"; }}
    >
      {/* Header row */}
      <div style={{ display: "flex", alignItems: "center", gap: 6, minWidth: 0 }}>
        <span style={{ fontSize: 13, fontWeight: 600, color: "var(--ink)", flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {tool.name || tool.id}
        </span>
        {isNew && <Badge variant="red" dot>NEW</Badge>}
        <StatusBadge enabled={tool.enabled} />
      </div>

      {/* Metrics grid */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 3, paddingTop: 4, borderTop: "1px solid var(--line)", fontSize: 11, color: "var(--ink-3)" }}>
        <div>Template: <span style={{ color: "var(--ink)" }}>{tool.template || "–"}</span></div>
        <div>Primitive: <span style={{ color: "var(--ink)" }}>{tool.primitive || "–"}</span></div>
        <div>Usage: <span style={{ color: "var(--ink)" }}>{tool.metrics?.usageCount || 0}</span></div>
        <div>Success: <span style={{ color: "var(--ink)" }}>{successRate == null ? "–" : `${successRate}%`}</span></div>
      </div>

      {/* Actions */}
      <div style={{ display: "flex", gap: 6, marginTop: "auto" }}>
        <ToggleBtn enabled={tool.enabled} busy={busy} onClick={e => { e.stopPropagation(); onToggleEnabled(); }} />
        <Btn variant="danger" disabled={busy} onClick={e => { e.stopPropagation(); onDelete(); }}>Delete</Btn>
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
          background: "var(--bg)",
          border: "1px solid var(--line)",
          borderRadius: 12,
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
        }}
      >
        <div
          style={{
            padding: "14px 20px",
            borderBottom: "1px solid var(--line)",
            display: "flex",
            alignItems: "center",
            gap: 10,
          }}
        >
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 18, fontWeight: 600, color: "var(--ink)" }}>{tool.name || tool.id}</div>
            <div style={{ fontSize: 12, color: "var(--ink-3)", marginTop: 4 }}>
              <span style={{ color: "var(--blue)", fontFamily: "monospace" }}>{tool.mcpName}</span>
              <span
                style={{
                  marginLeft: 8,
                  padding: "2px 7px",
                  borderRadius: 10,
                  border: "1px solid var(--line)",
                  background: readOnly ? "var(--blue-soft)" : tool.enabled ? "var(--green-soft)" : "transparent",
                  color: readOnly ? "var(--blue)" : tool.enabled ? "var(--green)" : "var(--ink-3)",
                  fontSize: 12,
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
                fontSize: 12,
                background: "rgba(255,95,99,0.1)",
                border: "1px solid rgba(255,95,99,0.3)",
                borderRadius: 6,
                color: "var(--red)",
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
              color: "var(--ink-3)",
              cursor: "pointer",
            }}
          >
            ✕
          </button>
        </div>

        <div style={{ padding: "12px 20px", borderBottom: "1px solid var(--line)" }}>
          <div style={{ fontSize: 13, color: "var(--ink)", lineHeight: 1.5 }}>
            {tool.description || "No description"}
          </div>
          {!readOnly && (
            <div style={{ marginTop: 10, fontSize: 12, color: "var(--ink-3)" }}>
              Related skills:{" "}
              <span style={{ color: "var(--ink)" }}>{relatedSkills && relatedSkills.length > 0 ? relatedSkills.length : 0}</span>
            </div>
          )}
          {!readOnly && relatedSkills && relatedSkills.length > 0 && (
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 8 }}>
              {relatedSkills.map((skill) => (
                <button
                  key={skill.id || skill.name}
                  onClick={() => onOpenSkill && onOpenSkill(skill.id)}
                  style={{
                    fontSize: 12,
                    padding: "2px 7px",
                    borderRadius: 10,
                    background: "var(--blue-soft)",
                    color: "var(--blue)",
                    border: "1px solid rgba(76,141,255,0.26)",
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
              <div>Template: <span style={{ color: "var(--ink)" }}>{tool.template || "–"}</span></div>
              <div>Primitive: <span style={{ color: "var(--ink)" }}>{tool.primitive || "–"}</span></div>
              <div>Usage: <span style={{ color: "var(--ink)" }}>{tool.metrics?.usageCount || 0}</span></div>
              <div>Success: <span style={{ color: "var(--ink)" }}>{successRate == null ? "–" : `${successRate}%`}</span></div>
            </div>
          )}

          <div
            style={{
              marginTop: 16,
              fontSize: 12,
              color: "var(--ink-2)",
              fontWeight: 600,
              textTransform: "uppercase",
              letterSpacing: 0.5,
            }}
          >
            How the Agent Calls This Tool
          </div>
          <div style={{ marginTop: 8, fontSize: 12, color: "var(--ink-3)", lineHeight: 1.5 }}>
            Use <span style={{ color: "var(--blue)", fontFamily: "monospace" }}>{tool.mcpName}</span> with an arguments object.
          </div>
          <div style={{ marginTop: 10, fontSize: 12, color: "var(--ink-3)" }}>
            Required arguments:{" "}
            <span style={{ color: "var(--ink)" }}>
              {requiredKeys.length > 0 ? requiredKeys.join(", ") : "none"}
            </span>
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
          <div
            style={{
              padding: "12px 20px",
              borderTop: "1px solid var(--line)",
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
                border: "1px solid var(--line)",
                background: tool.enabled ? "#e6e9ef" : "var(--blue-soft)",
                color: tool.enabled ? "var(--ink-2)" : "var(--blue)",
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
    const timer = setInterval(reload, 60000);
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
        background: "var(--bg)",
      }}
    >
      <div
        style={{
          padding: "16px 24px",
          borderBottom: "1px solid var(--line)",
          display: "flex",
          alignItems: "center",
          gap: 12,
        }}
      >
        <span style={{ fontSize: 24, display: "inline-flex", alignItems: "center" }}>{ICONS.wrench(24)}</span>
        <div>
          <div style={{ fontSize: 18, fontWeight: 700, color: "var(--ink)" }}>Tools</div>
          <div style={{ fontSize: 12, color: "var(--ink-3)" }}>
            Static MCP tools for reference + learned tools you can manage
          </div>
        </div>
      </div>

      {error && (
        <div
          style={{
            padding: "8px 24px",
            borderBottom: "1px solid var(--line)",
            color: "var(--red)",
            fontSize: 12,
          }}
        >
          {error}
        </div>
      )}

      <div style={{ flex: 1, overflow: "auto", padding: 20 }}>
        <div
          style={{
            border: "1px solid var(--line)",
            borderRadius: 10,
            marginBottom: 16,
            overflow: "hidden",
            background: "var(--bg)",
          }}
        >
          <div
            style={{
              padding: "10px 12px",
              borderBottom: "1px solid var(--line)",
              display: "flex",
              alignItems: "center",
              gap: 8,
              background: "var(--panel)",
            }}
          >
            <span style={{ fontSize: 14, display: "inline-flex", alignItems: "center" }}>{ICONS.book(14)}</span>
            <span style={{ fontSize: 13, fontWeight: 700, color: "var(--ink)" }}>
              Static MCP Tools (Reference)
            </span>
            <span style={{ marginLeft: "auto", fontSize: 12, color: "var(--ink-3)" }}>
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
            border: "1px solid var(--line)",
            borderRadius: 10,
            overflow: "hidden",
            background: "var(--bg)",
          }}
        >
          <div
            style={{
              padding: "10px 12px",
              borderBottom: "1px solid var(--line)",
              display: "flex",
              alignItems: "center",
              gap: 8,
              background: "var(--panel)",
            }}
          >
            <span style={{ fontSize: 14, display: "inline-flex", alignItems: "center" }}>{ICONS.brain(14)}</span>
            <span style={{ fontSize: 13, fontWeight: 700, color: "var(--ink)" }}>
              Dynamic Learned Tools
            </span>
            <span style={{ marginLeft: "auto", fontSize: 12, color: "var(--ink-3)" }}>
              {filteredTools.length} tool{filteredTools.length !== 1 ? "s" : ""}
            </span>
          </div>
          <div
            style={{
              padding: 12,
              borderBottom: "1px solid var(--line)",
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
                background: "var(--panel)",
                border: "1px solid var(--line)",
                borderRadius: 6,
                color: "var(--ink)",
                outline: "none",
              }}
            />
          </div>
          <div style={{ padding: 12 }}>
            {loading ? (
              <div style={{ color: "var(--ink-3)", padding: 20 }}>Loading…</div>
            ) : filteredTools.length === 0 ? (
              <div style={{ textAlign: "center", padding: 40 }}>
                <div style={{ fontSize: 32, marginBottom: 10 }}>{ICONS.wrench(32)}</div>
                <div style={{ fontSize: 15, color: "var(--ink)", fontWeight: 600, marginBottom: 6 }}>
                  No learned tools found
                </div>
                <div style={{ fontSize: 12, color: "var(--ink-3)" }}>
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
    <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13, fontWeight: 500, color: "var(--ink-3)" }}>
      <div
        style={{
          width: 8,
          height: 8,
          borderRadius: "50%",
          flexShrink: 0,
          background: active ? activeColor : inactiveColor,
          boxShadow: active ? `0 0 5px ${activeColor}` : "none",
        }}
      />
      {label}
    </div>
  );
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
                  setCodingAgent={handleCodingAgentChange}
                  codingModel={codingModel}
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
              SkillsPage={SkillsPage}
              ToolsPage={ToolsPage}
            />
          )}
          {topSection === "results" && (
            <ResultsPage icons={ICONS} LeaderboardPage={LeaderboardPage} onOpenScene={openSavedScene} />
          )}
        </div>
      )}

    </div>
    </PollProvider>
  );
}

export default App;

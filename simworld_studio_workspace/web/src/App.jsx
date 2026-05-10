import React, { useState, useEffect, useRef, useCallback, useMemo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import PixelStreamPlayer from "./PixelStreamPlayer.jsx";

// ─── SSE Status Stream — Split Contexts (P2-1 fix) ─────────────────────────
// Four fine-grained contexts so consumers only re-render when their slice changes.
// Old single PollContext caused ALL consumers to re-render on every 3s SSE push.

const AgentsContext  = React.createContext({ agents: [], sessions: [], activities: {} });
const SceneContext   = React.createContext({ objects: [], environment: { ready: false }, round: 0 });
const ChatLogContext = React.createContext([]);
const StatusContext  = React.createContext({ pieActive: false, health: null });

// Stale agent context — agents last seen + any sync errors
const SyncContext = React.createContext({ staleAgents: new Set(), syncError: null, sseOk: true });

// Legacy combined context kept for usePoll() callers that haven't migrated yet
const PollContext = React.createContext({
  context: { agents: [], objects: [], environment: { ready: false }, round: 0 },
  sessions: [], activities: {}, chatLog: [], pieActive: false, health: null,
});

// Threshold: agent not seen in UE for > 20s is "stale"
const STALE_AGENT_MS = 20_000;

function PollProvider({ children }) {
  const [agents,   setAgents]   = useState({ agents: [], sessions: [], activities: {} });
  const [scene,    setScene]    = useState({ objects: [], environment: { ready: false }, round: 0 });
  const [chatLog,  setChatLog]  = useState([]);
  const [status,   setStatus]   = useState({ pieActive: false, health: null });
  const [sync,     setSync]     = useState({ staleAgents: new Set(), syncError: null, sseOk: true });

  const agentLastSeen = useRef(new Map()); // agentName → timestamp
  const legacyRef = useRef({ context: { agents:[], objects:[], environment:{ready:false}, round:0 }, sessions:[], activities:{}, chatLog:[], pieActive:false, health:null });

  useEffect(() => {
    const token = sessionStorage.getItem("sw_session_token") || "";
    const url   = token ? `${API_BASE}/events?token=${token}` : `${API_BASE}/events`;
    let es = new EventSource(url);
    let reconnectTimer = null;

    const reconnect = () => {
      es.close();
      reconnectTimer = setTimeout(() => {
        es = new EventSource(url);
        es.onmessage = onMessage;
        es.onerror   = onError;
      }, 3000);
    };

    function onMessage(evt) {
      try {
        const d = JSON.parse(evt.data);
        setSync(prev => prev.sseOk ? prev : { ...prev, sseOk: true, syncError: null });

        // Track agent last-seen timestamps
        const now = Date.now();
        const liveNames = new Set();
        (d.sessions || d.context?.agents || []).forEach(a => {
          const name = a.agentName || a.name;
          if (name) { agentLastSeen.current.set(name, now); liveNames.add(name); }
        });

        // Detect stale agents (were seen before but not in current push)
        const stale = new Set();
        for (const [name, ts] of agentLastSeen.current) {
          if (!liveNames.has(name) && now - ts > STALE_AGENT_MS) stale.add(name);
          if (liveNames.has(name)) agentLastSeen.current.set(name, now); // refresh
        }
        setSync(prev => {
          const same = prev.staleAgents.size === stale.size && [...stale].every(n => prev.staleAgents.has(n));
          return same ? prev : { ...prev, staleAgents: stale };
        });

        // Agents slice — lightweight comparison (names + count, avoid full stringify)
        const nextSessions = d.sessions || [];
        const nextAgentList = d.context?.agents || [];
        const nextActivities = d.activities || {};
        setAgents(prev => {
          const prevSess = prev.sessions || [];
          // Fast check: count + first/last name
          const sameCount = prevSess.length === nextSessions.length && (prev.agents||[]).length === nextAgentList.length;
          const sameName  = sameCount && (nextSessions[0]?.agentName === prevSess[0]?.agentName);
          const sameActs  = Object.keys(nextActivities).join(',') === Object.keys(prev.activities || {}).join(',');
          if (sameCount && sameName && sameActs) return prev;
          return { agents: nextAgentList, sessions: nextSessions, activities: nextActivities };
        });

        // Scene slice — only track count + env.ready (objects list can be 30k items)
        const nextEnv   = d.context?.environment || { ready: false };
        const nextObjs  = d.context?.objects     || [];
        const nextRound = d.context?.round       || 0;
        setScene(prev => {
          if (prev.objects.length === nextObjs.length &&
              prev.environment?.ready === nextEnv.ready &&
              prev.round === nextRound) return prev;
          return { objects: nextObjs, environment: nextEnv, round: nextRound };
        });

        // ChatLog — append new only
        if (Array.isArray(d.chatLog) && d.chatLog.length > 0) {
          setChatLog(prev => {
            const existing = new Set(prev.map(m => `${m.from}-${m.timestamp}`));
            const news = d.chatLog.filter(m => !existing.has(`${m.from}-${m.timestamp}`));
            return news.length > 0 ? [...prev, ...news].slice(-200) : prev;
          });
        }

        // Status — only compare the fields we care about
        const nextHealth = d.health || null;
        const nextPie    = !!d.pieActive;
        setStatus(prev => {
          if (prev.pieActive === nextPie &&
              prev.health?.ueConnected  === nextHealth?.ueConnected &&
              prev.health?.mcpConnected === nextHealth?.mcpConnected) return prev;
          return { pieActive: nextPie, health: nextHealth };
        });

        legacyRef.current = d;
      } catch (e) {
        setSync(prev => ({ ...prev, syncError: "SSE parse error: " + e.message }));
      }
    }

    function onError() {
      setSync(prev => ({ ...prev, sseOk: false, syncError: "SSE connection lost — reconnecting…" }));
      reconnect();
    }

    es.onmessage = onMessage;
    es.onerror   = onError;
    return () => { es.close(); if (reconnectTimer) clearTimeout(reconnectTimer); };
  }, []);

  // Legacy combined context value — stable object so usePoll() consumers
  // still work but don't get extra re-renders from the ref itself
  const legacyValue = useMemo(() => ({
    get context()    { return legacyRef.current.context    || {}; },
    get sessions()   { return legacyRef.current.sessions   || []; },
    get activities() { return legacyRef.current.activities || {}; },
    get chatLog()    { return legacyRef.current.chatLog    || []; },
    get pieActive()  { return legacyRef.current.pieActive  || false; },
    get health()     { return legacyRef.current.health     || null; },
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }), []); // intentionally stable — consumers that need reactivity should use fine-grained contexts

  return (
    <AgentsContext.Provider  value={agents}>
    <SceneContext.Provider   value={scene}>
    <ChatLogContext.Provider value={chatLog}>
    <StatusContext.Provider  value={status}>
    <SyncContext.Provider    value={sync}>
    <PollContext.Provider    value={legacyValue}>
      {children}
    </PollContext.Provider>
    </SyncContext.Provider>
    </StatusContext.Provider>
    </ChatLogContext.Provider>
    </SceneContext.Provider>
    </AgentsContext.Provider>
  );
}

// Fine-grained hooks — prefer these over usePoll() for new code
function useAgents()  { return React.useContext(AgentsContext); }
function useScene()   { return React.useContext(SceneContext); }
function useChatLog() { return React.useContext(ChatLogContext); }
function useStatus()  { return React.useContext(StatusContext); }
function useSync()    { return React.useContext(SyncContext); }

// Legacy hook — works but causes full re-render on every SSE push
function usePoll() { return React.useContext(PollContext); }

// ─── Inline SVG Icons (flat colorful cartoon style) ─────────────────────────

function SvgIcon({ children, size = "1em", style, ...props }) {
  return (
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width={size} height={size} fill="none" style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }} {...props}>
      {children}
    </svg>
  );
}

const ICONS = {
  clipboard: (s) => <SvgIcon size={s}><rect x="5" y="2" width="14" height="20" rx="2" fill="#16a34a" /><rect x="8" y="1" width="8" height="3" rx="1" fill="#15803d" /><rect x="8" y="8" width="8" height="1.5" rx=".75" fill="#fff" /><rect x="8" y="11.5" width="6" height="1.5" rx=".75" fill="#fff" opacity=".7" /><rect x="8" y="15" width="7" height="1.5" rx=".75" fill="#fff" opacity=".5" /></SvgIcon>,
  search: (s) => <SvgIcon size={s}><circle cx="10.5" cy="10.5" r="6" stroke="#2563eb" strokeWidth="2.2" /><line x1="15" y1="15" x2="20" y2="20" stroke="#2563eb" strokeWidth="2.5" strokeLinecap="round" /></SvgIcon>,
  plus: (s) => <SvgIcon size={s}><circle cx="12" cy="12" r="9" fill="#16a34a" /><line x1="12" y1="7.5" x2="12" y2="16.5" stroke="#fff" strokeWidth="2.2" strokeLinecap="round" /><line x1="7.5" y1="12" x2="16.5" y2="12" stroke="#fff" strokeWidth="2.2" strokeLinecap="round" /></SvgIcon>,
  trash: (s) => <SvgIcon size={s}><path d="M6 7h12l-1 13H7L6 7z" fill="#dc2626" /><rect x="4" y="4.5" width="16" height="2.5" rx="1" fill="#b91c1c" /><rect x="9" y="2" width="6" height="3" rx="1" fill="#dc2626" /></SvgIcon>,
  transform: (s) => <SvgIcon size={s}><line x1="4" y1="12" x2="20" y2="12" stroke="#f59e0b" strokeWidth="2" strokeLinecap="round" /><polyline points="7,8.5 4,12 7,15.5" fill="none" stroke="#f59e0b" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" /><polyline points="17,8.5 20,12 17,15.5" fill="none" stroke="#f59e0b" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" /></SvgIcon>,
  document: (s) => <SvgIcon size={s}><rect x="5" y="2" width="14" height="20" rx="2" fill="#64748b" /><path d="M5 2h9l5 5v15a2 2 0 01-2 2H7a2 2 0 01-2-2V2z" fill="#64748b" /><path d="M14 2v5h5" fill="#64748b" /><rect x="8" y="10" width="8" height="1.5" rx=".75" fill="#fff" opacity=".7" /><rect x="8" y="13" width="5" height="1.5" rx=".75" fill="#fff" opacity=".5" /></SvgIcon>,
  video: (s) => <SvgIcon size={s}><rect x="2" y="5" width="14" height="14" rx="2" fill="#7c3aed" /><polygon points="18,7 22,5 22,19 18,17" fill="#6d28d9" /><circle cx="9" cy="12" r="2.5" fill="#fff" opacity=".5" /></SvgIcon>,
  camera: (s) => <SvgIcon size={s}><rect x="2" y="6" width="20" height="14" rx="3" fill="#2563eb" /><circle cx="12" cy="13" r="4" fill="#3b82f6" /><circle cx="12" cy="13" r="2" fill="#93c5fd" /><rect x="8" y="3.5" width="8" height="3" rx="1" fill="#3b82f6" /></SvgIcon>,
  python: (s) => <SvgIcon size={s}><path d="M12 2c-3 0-5 1.5-5 4v2h5v1H6c-2 0-4 1.5-4 4.5S4 18 6 18h2v-3c0-2 1.5-3.5 3.5-3.5h5c1.5 0 3-1.2 3-3V6c0-2.2-2-4-5-4z" fill="#16a34a" /><path d="M12 22c3 0 5-1.5 5-4v-2h-5v-1h6c2 0 4-1.5 4-4.5S18 6 16 6h-2v3c0 2-1.5 3.5-3.5 3.5h-5c-1.5 0-3 1.2-3 3v3.5c0 2.2 2 4 5 4z" fill="#3b82f6" /><circle cx="8.5" cy="5" r="1" fill="#fff" /><circle cx="15.5" cy="19" r="1" fill="#fff" /></SvgIcon>,
  wrench: (s) => <SvgIcon size={s}><path d="M14.5 3a6 6 0 00-5.7 7.9L3.3 16.4a2.2 2.2 0 003.1 3.1l5.5-5.5A6 6 0 1014.5 3z" fill="#f59e0b" /><circle cx="14.5" cy="9" r="2.5" fill="#ea580c" /></SvgIcon>,
  gear: (s) => <SvgIcon size={s}><circle cx="12" cy="12" r="4" fill="#64748b" /><path d="M12 1l1.5 3.2a7.5 7.5 0 012.3 1.3L19 4.5l1 2.5-2.8 1.5a7.5 7.5 0 01.3 2.5H21v2.5h-3.5a7.5 7.5 0 01-.3 2.5l2.8 1.5-1 2.5-3.2-1a7.5 7.5 0 01-2.3 1.3L12 23l-1.5-3.2a7.5 7.5 0 01-2.3-1.3L5 19.5l-1-2.5 2.8-1.5a7.5 7.5 0 01-.3-2.5H3v-2.5h3.5a7.5 7.5 0 01.3-2.5L4 6.5l1-2.5 3.2 1a7.5 7.5 0 012.3-1.3L12 1z" fill="#64748b" /><circle cx="12" cy="12" r="3" fill="#0f172a" /></SvgIcon>,
  palette: (s) => <SvgIcon size={s}><path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c1 0 1.5-.7 1.5-1.5 0-.4-.1-.7-.3-1-.2-.3-.3-.6-.3-1 0-1 .7-1.5 1.5-1.5H16c3.3 0 6-2.7 6-6 0-5-4.5-9-10-9z" fill="#7c3aed" /><circle cx="7" cy="12" r="1.8" fill="#dc2626" /><circle cx="9" cy="8" r="1.8" fill="#f59e0b" /><circle cx="14" cy="7" r="1.8" fill="#16a34a" /><circle cx="17.5" cy="10" r="1.8" fill="#2563eb" /></SvgIcon>,
  plug: (s) => <SvgIcon size={s}><rect x="7" y="2" width="3" height="8" rx="1" fill="#2563eb" /><rect x="14" y="2" width="3" height="8" rx="1" fill="#2563eb" /><path d="M6 10h12v3a6 6 0 01-5 5.9V22h-2v-3.1A6 6 0 016 13V10z" fill="#3b82f6" /></SvgIcon>,
  eye: (s) => <SvgIcon size={s}><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z" fill="#7c3aed" /><circle cx="12" cy="12" r="4" fill="#fff" /><circle cx="12" cy="12" r="2" fill="#6d28d9" /></SvgIcon>,
  map: (s) => <SvgIcon size={s}><path d="M3 6l6-3 6 3 6-3v15l-6 3-6-3-6 3V6z" fill="#3b82f6" /><path d="M9 3v15" stroke="#2563eb" strokeWidth="1.5" /><path d="M15 6v15" stroke="#2563eb" strokeWidth="1.5" /><circle cx="8" cy="10" r="1.2" fill="#dc2626" /><circle cx="16" cy="12" r="1.2" fill="#16a34a" /></SvgIcon>,
  building: (s) => <SvgIcon size={s}><rect x="4" y="4" width="16" height="18" rx="1" fill="#3b82f6" /><rect x="7" y="7" width="3" height="3" rx=".5" fill="#93c5fd" /><rect x="14" y="7" width="3" height="3" rx=".5" fill="#93c5fd" /><rect x="7" y="13" width="3" height="3" rx=".5" fill="#93c5fd" /><rect x="14" y="13" width="3" height="3" rx=".5" fill="#93c5fd" /><rect x="10" y="18" width="4" height="4" fill="#2563eb" /></SvgIcon>,
  tree: (s) => <SvgIcon size={s}><rect x="10.5" y="15" width="3" height="7" fill="#92400e" /><polygon points="12,2 4,15 20,15" fill="#16a34a" /><polygon points="12,6 6,15 18,15" fill="#15803d" /></SvgIcon>,
  car: (s) => <SvgIcon size={s}><rect x="2" y="10" width="20" height="7" rx="2" fill="#ea580c" /><path d="M5 10l2-5h10l2 5" fill="#f59e0b" /><circle cx="7" cy="17" r="2.2" fill="#94a3b8" /><circle cx="7" cy="17" r="1" fill="#64748b" /><circle cx="17" cy="17" r="2.2" fill="#94a3b8" /><circle cx="17" cy="17" r="1" fill="#64748b" /><rect x="7" y="7" width="4" height="3" rx=".5" fill="#93c5fd" opacity=".7" /><rect x="13" y="7" width="4" height="3" rx=".5" fill="#93c5fd" opacity=".7" /></SvgIcon>,
  hydrant: (s) => <SvgIcon size={s}><rect x="4" y="12" width="16" height="10" rx="1" fill="#64748b" /><rect x="6" y="8" width="12" height="4" rx="1" fill="#64748b" /><rect x="2" y="14" width="4" height="3" rx="1" fill="#64748b" /><rect x="18" y="14" width="4" height="3" rx="1" fill="#64748b" /><rect x="8" y="4" width="8" height="5" rx="2" fill="#64748b" /><circle cx="12" cy="6" r="1.5" fill="#94a3b8" /></SvgIcon>,
  road: (s) => <SvgIcon size={s}><polygon points="1,22 8,2 16,2 23,22" fill="#64748b" /><rect x="11" y="4" width="2" height="3.5" rx=".5" fill="#f59e0b" /><rect x="11" y="10" width="2" height="3.5" rx=".5" fill="#f59e0b" /><rect x="11" y="16" width="2" height="3.5" rx=".5" fill="#f59e0b" /></SvgIcon>,
  cube: (s) => <SvgIcon size={s}><rect x="4" y="4" width="16" height="16" rx="2" fill="#94a3b8" /><rect x="6" y="6" width="12" height="12" rx="1" fill="#64748b" /></SvgIcon>,
  hammer: (s) => <SvgIcon size={s}><rect x="10" y="10" width="3" height="12" rx="1" fill="#92400e" transform="rotate(-45 12 16)" /><rect x="5" y="2" width="12" height="7" rx="2" fill="#64748b" transform="rotate(-45 11 5.5)" /></SvgIcon>,
  box: (s) => <SvgIcon size={s}><rect x="3" y="8" width="18" height="14" rx="1" fill="#f59e0b" /><path d="M3 8l9-6 9 6" fill="#ea580c" /><line x1="12" y1="2" x2="12" y2="22" stroke="#92400e" strokeWidth="1" opacity=".3" /><rect x="9" y="8" width="6" height="4" rx="1" fill="#92400e" opacity=".4" /></SvgIcon>,
  chat: (s) => <SvgIcon size={s}><rect x="2" y="3" width="20" height="15" rx="3" fill="#2563eb" /><path d="M6 18l-2 4v-4" fill="#2563eb" /><rect x="6" y="8" width="8" height="1.5" rx=".75" fill="#fff" opacity=".7" /><rect x="6" y="11.5" width="5" height="1.5" rx=".75" fill="#fff" opacity=".5" /></SvgIcon>,
  robot: (s) => <SvgIcon size={s}><rect x="4" y="7" width="16" height="13" rx="3" fill="#64748b" /><rect x="7" y="10" width="4" height="3" rx="1" fill="#2563eb" /><rect x="13" y="10" width="4" height="3" rx="1" fill="#2563eb" /><rect x="9" y="16" width="6" height="2" rx="1" fill="#64748b" /><line x1="12" y1="3" x2="12" y2="7" stroke="#64748b" strokeWidth="2" /><circle cx="12" cy="2.5" r="1.5" fill="#2563eb" /><rect x="1" y="12" width="3" height="4" rx="1" fill="#64748b" /><rect x="20" y="12" width="3" height="4" rx="1" fill="#64748b" /></SvgIcon>,
  swords: (s) => <SvgIcon size={s}><line x1="5" y1="19" x2="18" y2="5" stroke="#ea580c" strokeWidth="2.5" strokeLinecap="round" /><polygon points="18,5 21,3 22,6 19,7" fill="#f59e0b" /><line x1="19" y1="19" x2="6" y2="5" stroke="#2563eb" strokeWidth="2.5" strokeLinecap="round" /><polygon points="6,5 3,3 2,6 5,7" fill="#3b82f6" /><circle cx="12" cy="12" r="1.5" fill="#0f172a" /></SvgIcon>,
  frame: (s) => <SvgIcon size={s}><rect x="3" y="3" width="18" height="18" rx="2" fill="#7c3aed" /><rect x="5" y="5" width="14" height="14" rx="1" fill="#f4f6fa" /><circle cx="9" cy="11" r="3" fill="#16a34a" /><polygon points="6,17 11,11 14,14 17,10 19,17" fill="#3b82f6" opacity=".8" /></SvgIcon>,
  tools: (s) => <SvgIcon size={s}><path d="M14.5 3a6 6 0 00-5.7 7.9L3.3 16.4a2.2 2.2 0 003.1 3.1l5.5-5.5A6 6 0 1014.5 3z" fill="#f59e0b" /><path d="M4 4l3 3M2 8l4 1M8 2l1 4" stroke="#64748b" strokeWidth="1.5" strokeLinecap="round" /><rect x="13" y="13" width="3" height="9" rx="1" fill="#64748b" transform="rotate(-45 14.5 17.5)" /><rect x="14" y="11" width="8" height="4" rx="1" fill="#64748b" transform="rotate(-45 18 13)" /></SvgIcon>,
  trophy: (s) => <SvgIcon size={s}><path d="M7 4h10v7a5 5 0 01-10 0V4z" fill="#f59e0b" /><path d="M7 6H4a2 2 0 00-2 2v1a3 3 0 003 3h2" fill="#ea580c" /><path d="M17 6h3a2 2 0 012 2v1a3 3 0 01-3 3h-2" fill="#ea580c" /><rect x="10" y="15" width="4" height="3" fill="#f59e0b" /><rect x="7" y="18" width="10" height="2.5" rx="1" fill="#ea580c" /></SvgIcon>,
  gamepad: (s) => <SvgIcon size={s}><rect x="2" y="7" width="20" height="12" rx="5" fill="#94a3b8" /><circle cx="8" cy="13" r="3.5" fill="#64748b" /><line x1="8" y1="10.5" x2="8" y2="15.5" stroke="#64748b" strokeWidth="1.5" strokeLinecap="round" /><line x1="5.5" y1="13" x2="10.5" y2="13" stroke="#64748b" strokeWidth="1.5" strokeLinecap="round" /><circle cx="15" cy="11" r="1.3" fill="#16a34a" /><circle cx="18" cy="13" r="1.3" fill="#dc2626" /><circle cx="15" cy="15" r="1.3" fill="#2563eb" /><circle cx="18" cy="11" r="1.3" fill="#f59e0b" /></SvgIcon>,
  mouse: (s) => <SvgIcon size={s}><rect x="5" y="2" width="14" height="20" rx="7" fill="#64748b" /><line x1="12" y1="2" x2="12" y2="10" stroke="#64748b" strokeWidth="1.5" /><rect x="10.5" y="5" width="3" height="4" rx="1.5" fill="#0f172a" /></SvgIcon>,
  keyboard: (s) => <SvgIcon size={s}><rect x="1" y="6" width="22" height="13" rx="2" fill="#94a3b8" /><rect x="3" y="8" width="3" height="2.5" rx=".5" fill="#64748b" /><rect x="7.5" y="8" width="3" height="2.5" rx=".5" fill="#64748b" /><rect x="12" y="8" width="3" height="2.5" rx=".5" fill="#64748b" /><rect x="16.5" y="8" width="4.5" height="2.5" rx=".5" fill="#64748b" /><rect x="3" y="12" width="4" height="2.5" rx=".5" fill="#64748b" /><rect x="8.5" y="12" width="7" height="2.5" rx=".5" fill="#64748b" /><rect x="17" y="12" width="4" height="2.5" rx=".5" fill="#64748b" /><rect x="6" y="16" width="12" height="2" rx=".5" fill="#64748b" /></SvgIcon>,
  liveCircle: (s) => <SvgIcon size={s}><circle cx="12" cy="12" r="6" fill="#dc2626" /><circle cx="12" cy="12" r="3" fill="#ff7b72" /></SvgIcon>,
  warning: (s) => <SvgIcon size={s}><path d="M12 2L1 21h22L12 2z" fill="#f59e0b" /><rect x="11" y="9" width="2" height="6" rx="1" fill="#fff" /><circle cx="12" cy="17.5" r="1.2" fill="#fff" /></SvgIcon>,
  gold: (s) => <SvgIcon size={s}><circle cx="12" cy="10" r="8" fill="#f59e0b" /><circle cx="12" cy="10" r="6" fill="#ea580c" /><text x="12" y="14" textAnchor="middle" fontSize="10" fontWeight="700" fill="#fff">1</text><path d="M6 18h12l-1 3H7l-1-3z" fill="#f59e0b" /></SvgIcon>,
  silver: (s) => <SvgIcon size={s}><circle cx="12" cy="10" r="8" fill="#64748b" /><circle cx="12" cy="10" r="6" fill="#c0c0c0" /><text x="12" y="14" textAnchor="middle" fontSize="10" fontWeight="700" fill="#fff">2</text><path d="M6 18h12l-1 3H7l-1-3z" fill="#64748b" /></SvgIcon>,
  bronze: (s) => <SvgIcon size={s}><circle cx="12" cy="10" r="8" fill="#92400e" /><circle cx="12" cy="10" r="6" fill="#cd7f32" /><text x="12" y="14" textAnchor="middle" fontSize="10" fontWeight="700" fill="#fff">3</text><path d="M6 18h12l-1 3H7l-1-3z" fill="#92400e" /></SvgIcon>,
  book: (s) => <SvgIcon size={s}><rect x="4" y="3" width="16" height="18" rx="2" fill="#3b82f6" /><rect x="4" y="3" width="4" height="18" fill="#dbeafe" /><rect x="10" y="7" width="8" height="1.5" rx=".75" fill="#fff" opacity=".7" /><rect x="10" y="10.5" width="6" height="1.5" rx=".75" fill="#fff" opacity=".5" /><rect x="10" y="14" width="7" height="1.5" rx=".75" fill="#fff" opacity=".4" /></SvgIcon>,
  brain: (s) => <SvgIcon size={s}><path d="M12 4c-2 0-3.5.8-4.2 2-.8-.3-1.8-.2-2.5.4-1 .8-1.3 2.2-.8 3.3-.8.8-1.2 2-1 3.2.3 1.5 1.3 2.5 2.5 2.8.2 1.5 1.2 2.8 2.8 3.3.8.2 1.6.3 2.2.1V4z" fill="#7c3aed" /><path d="M12 4c2 0 3.5.8 4.2 2 .8-.3 1.8-.2 2.5.4 1 .8 1.3 2.2.8 3.3.8.8 1.2 2 1 3.2-.3 1.5-1.3 2.5-2.5 2.8-.2 1.5-1.2 2.8-2.8 3.3-.8.2-1.6.3-2.2.1V4z" fill="#6d28d9" /><line x1="12" y1="4" x2="12" y2="20" stroke="#c9a0ff" strokeWidth="1" /></SvgIcon>,
};

// ─── Constants ───────────────────────────────────────────────────────────────

const API_BASE = "/api";
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
  city: "#3b82f6",
  buildings: "#b91c1c",
  props: "#64748b",
  weather: "#ea580c",
  camera: "#7c3aed",
  layout: "#16a34a",
  planning: "#3b82f6",
  spacing: "#f59e0b",
  trees: "#16a34a",
  vehicles: "#ea580c",
  lighting: "#f59e0b",
  atmosphere: "#7c3aed",
  screenshot: "#7c3aed",
  decoration: "#64748b",
  furniture: "#64748b",
  architecture: "#b91c1c",
  environment: "#16a34a",
  viewpoint: "#7c3aed",
  placement: "#f59e0b",
  roads: "#64748b",
  capture: "#7c3aed",
};

const CATEGORY_ICONS = {
  buildings: ICONS.building,
  trees: ICONS.tree,
  vehicles: ICONS.car,
  street_furniture: ICONS.hydrant,
  roads: ICONS.road,
  static_meshes: ICONS.cube,
};

const CATEGORY_COLORS = {
  buildings: "#3b82f6",
  trees: "#16a34a",
  vehicles: "#ea580c",
  street_furniture: "#64748b",
  roads: "#64748b",
  static_meshes: "#94a3b8",
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
  // Server sends a `: ping` heartbeat every 15s, so 2 min of total silence
  // (heartbeats AND real events both gone) means the connection is genuinely dead.
  // This must NOT be used to bound how long agent work can take — only to detect
  // a silently-dropped SSE stream (tab throttled, network drop, proxy idle-cut).
  const IDLE_TIMEOUT = 120000;

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
    padding: "3px 11px",
    fontSize: 11,
    background: "#ffffff",
    border: `1px solid ${color}55`,
    borderRadius: 8,
    color,
    cursor: "pointer",
    fontWeight: 600,
    fontFamily: "inherit",
    boxShadow: "0 1px 2px rgba(15,23,42,.04)",
  };
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
        border: "1px solid #e6e9ef",
        borderRadius: 8,
        overflow: "hidden",
        background: "#f8fafc",
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
          color: "#64748b",
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
        <span style={{ fontFamily: "monospace", color: "#2563eb", fontWeight: 600 }}>
          {displayName}
        </span>
        {paramSummary && (
          <span
            style={{
              color: "#475569",
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
          <span style={{ color: "#f59e0b", fontSize: 10, marginLeft: "auto" }}>
            running…
          </span>
        )}
        <span style={{ marginLeft: "auto", fontSize: 10, flexShrink: 0 }}>
          {expanded ? "▲" : "▼"}
        </span>
      </button>

      {expanded && (
        <div style={{ padding: "8px 10px", borderTop: "1px solid #e6e9ef" }}>
          {(tool.input || tool.inputBuffer) && (
            <div style={{ marginBottom: 6 }}>
              <div
                style={{
                  color: "#475569",
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
                  color: "#334155",
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
                  color: "#64748b",
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
                  border: "1px solid #e2e8f0",
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
        border: `1px solid ${active ? "#3b82f6" : "#e6e9ef"}`,
        background: active ? "#2563eb11" : "#ffffff",
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
            border: `2px solid ${active ? "#3b82f6" : "#e2e8f0"}`,
            background: active ? "#3b82f6" : "transparent",
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
            color: "#0f172a",
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
            background: "#e6e9ef",
            border: "1px solid #e2e8f0",
            borderRadius: 4,
            color: "#64748b",
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
            background: skill.source === "custom" ? "#3b82f622" : "#e6e9ef",
            color: skill.source === "custom" ? "#2563eb" : "#94a3b8",
          }}
        >
          {skill.source}
        </span>
      </div>

      <div
        style={{
          fontSize: 11,
          color: "#64748b",
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
                background: (TAG_COLORS[tag] || "#e2e8f0") + "33",
                color: TAG_COLORS[tag] || "#64748b",
                border: `1px solid ${TAG_COLORS[tag] || "#e2e8f0"}44`,
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
            color: "#475569",
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
          background: "#f4f6fa",
          border: "1px solid #e2e8f0",
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
            borderBottom: "1px solid #e6e9ef",
            display: "flex",
            alignItems: "center",
            gap: 10,
          }}
        >
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 16, fontWeight: 600, color: "#0f172a" }}>
              {skill.name}
            </div>
            <div style={{ fontSize: 11, color: "#64748b", marginTop: 3 }}>
              v{skill.version} by {skill.author}
              <span
                style={{
                  marginLeft: 8,
                  padding: "1px 5px",
                  borderRadius: 4,
                  background: skill.source === "custom" ? "#3b82f622" : "#e6e9ef",
                  color: skill.source === "custom" ? "#2563eb" : "#94a3b8",
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
                background: "#b91c1c22",
                border: "1px solid #b91c1c66",
                borderRadius: 6,
                color: "#dc2626",
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
              color: "#64748b",
              cursor: "pointer",
            }}
          >
            ✕
          </button>
        </div>

        {/* Description + Tags */}
        <div style={{ padding: "10px 18px", borderBottom: "1px solid #e6e9ef" }}>
          <div style={{ fontSize: 12, color: "#64748b", lineHeight: 1.5 }}>
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
                    background: (TAG_COLORS[tag] || "#e2e8f0") + "33",
                    color: TAG_COLORS[tag] || "#64748b",
                    border: `1px solid ${TAG_COLORS[tag] || "#e2e8f0"}44`,
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
              color: "#1e293b",
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
    background: "#ffffff",
    border: "1px solid #e2e8f0",
    borderRadius: 6,
    color: "#0f172a",
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
          background: "#f4f6fa",
          border: "1px solid #e2e8f0",
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
            borderBottom: "1px solid #e6e9ef",
            display: "flex",
            alignItems: "center",
          }}
        >
          <span style={{ fontSize: 16, fontWeight: 600, color: "#0f172a" }}>
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
              color: "#64748b",
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
            <label style={{ fontSize: 11, color: "#64748b", display: "block", marginBottom: 4 }}>
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
            <label style={{ fontSize: 11, color: "#64748b", display: "block", marginBottom: 4 }}>
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
            <label style={{ fontSize: 11, color: "#64748b", display: "block", marginBottom: 4 }}>
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
            <label style={{ fontSize: 11, color: "#64748b", display: "block", marginBottom: 4 }}>
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
            <label style={{ fontSize: 11, color: "#64748b", display: "block", marginBottom: 4 }}>
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
            <div style={{ fontSize: 11, color: "#dc2626", padding: "4px 0" }}>{error}</div>
          )}
        </div>

        {/* Footer */}
        <div
          style={{
            padding: "12px 18px",
            borderTop: "1px solid #e6e9ef",
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
              background: "#e6e9ef",
              border: "1px solid #e2e8f0",
              borderRadius: 6,
              color: "#1e293b",
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
              background: "#15803d",
              border: "1px solid #15803d",
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
        borderBottom: "1px solid #e6e9ef",
        background: "#f4f6fa",
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
        <span style={{ fontSize: 11, color: "#64748b", fontFamily: "monospace" }}>
          {expanded ? "▼" : "▶"}
        </span>
        <span style={{ fontSize: 12, fontWeight: 600, color: "#0f172a" }}>Skills</span>
        {activeSkills.length > 0 && (
          <span
            style={{
              fontSize: 10,
              background: "#3b82f6",
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
          <span style={{ fontSize: 10, color: "#64748b" }}>Auto-select skills</span>
          <button
            onClick={() => onAutoEnabledChange(!autoEnabled)}
            style={{
              width: 34,
              height: 18,
              borderRadius: 999,
              border: "1px solid #e2e8f0",
              background: autoEnabled ? "#3b82f6" : "#e6e9ef",
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
        <span style={{ fontSize: 10, color: "#475569", marginLeft: 6 }}>
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
              color: "#64748b",
              border: "1px solid #e6e9ef",
              background: "#f4f6fa",
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
                color: "#475569",
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
                color: "#475569",
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
              border: "1px dashed #e2e8f0",
              background: "transparent",
              color: "#2563eb",
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
          borderBottom: "1px solid #e6e9ef",
          display: "flex",
          alignItems: "center",
          gap: 8,
          background: "#ffffff",
        }}
      >
        <span style={{ fontSize: 13, fontWeight: 600, color: "#ea580c" }}>
          Annotate Screenshot
        </span>
        <span style={{ fontSize: 11, color: "#64748b" }}>
          Click on the image to add feedback points
        </span>
        <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          <button
            onClick={onCancel}
            style={{
              padding: "4px 12px",
              fontSize: 11,
              borderRadius: 4,
              border: "1px solid #e2e8f0",
              background: "#e6e9ef",
              color: "#64748b",
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
              border: "1px solid #3b82f6",
              background: "#3b82f6",
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
                    background: "#ea580c",
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
                    background: "#e6e9ef",
                    border: "1px solid #e2e8f0",
                    borderRadius: 4,
                    padding: "2px 6px",
                    fontSize: 10,
                    color: "#0f172a",
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
            background: "#f4f6fa",
            display: "flex",
            flexDirection: "column",
            padding: 12,
            gap: 8,
          }}
        >
          <span style={{ fontSize: 12, fontWeight: 600, color: "#0f172a" }}>Feedback</span>
          <textarea
            value={feedbackText}
            onChange={(e) => setFeedbackText(e.target.value)}
            placeholder="Describe what to change overall..."
            style={{
              flex: 1,
              resize: "none",
              background: "#ffffff",
              border: "1px solid #e2e8f0",
              borderRadius: 6,
              color: "#0f172a",
              padding: 8,
              fontSize: 12,
              fontFamily: "inherit",
            }}
          />
          {points.length > 0 && (
            <div style={{ fontSize: 11, color: "#64748b" }}>
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
                      background: "#ea580c",
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
                  <span style={{ fontSize: 10, color: "#1e293b" }}>{pt.text}</span>
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

const ChatMessage = React.memo(function ChatMessage({ message }) {
  const isUser = message.role === "user";

  const bubbleContent = isUser ? (
    <div style={{ color:"#0f172a", fontSize:13, whiteSpace:"pre-wrap" }}>{message.content}</div>
  ) : (
    <>
      {message.waiting && (
        <div style={{ color:"#64748b", fontSize:12, display:"flex", alignItems:"center", gap:8, padding:"2px 0" }}>
          <span style={{ display:"inline-block", width:8, height:8, borderRadius:"50%", border:"2px solid #2563eb", borderTopColor:"transparent", animation:"spin 1s linear infinite" }} />
          Waiting for Claude...
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
              <div className="markdown" key="content" style={{ color:"#0f172a", fontSize:13 }}>
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
              </div>
            ),
            (message.toolCalls||[]).map(tc=><ToolCallBlock key={tc.id} tool={tc}/>),
          ]}
    </>
  );

  const bubble = (
    <div style={{
      padding:"9px 12px",
      borderRadius: isUser ? "12px 12px 4px 12px" : "12px 12px 12px 4px",
      background: isUser ? "#eff4ff" : "#ffffff",
      border:`1px solid ${isUser?"#dbe6ff":"#e6e9ef"}`,
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
        <div style={{ fontSize:10, color:"#64748b", paddingRight:33 }}>
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
      <div style={{ fontSize:10, color:"#64748b", paddingLeft:33 }}>
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

      // P2-2 SSE text batching: buffer rapid text deltas, flush via rAF to avoid
      // a React setState per character during fast streaming.
      let _textBuf = "";
      let _rafPending = false;
      const _flushTextBuf = (msgId) => {
        if (!_textBuf) return;
        const delta = _textBuf;
        _textBuf = "";
        _rafPending = false;
        setMessages((prev) => {
          const updated = [...prev];
          const idx = updated.findIndex((m) => m.id === msgId);
          if (idx === -1) return prev;
          const msg = { ...updated[idx] };
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
                  console.log("[CTX-DEBUG] system event, sessionId:", event.data.sessionId);
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
        setLoading(false);
        setAutoSelectingSkills(false);
        abortRef.current = null;
      }
    },
    [input, loading, sessionId, selectedSkills, autoSkillSelectionEnabled, onScreenshotUpdate]
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
        background: "#f4f6fa",
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
          borderBottom: "1px solid #e6e9ef",
          background: "#ffffff",
          display: "flex",
          alignItems: "center",
          gap: 10,
          flexShrink: 0,
          boxShadow: "0 1px 2px rgba(15,23,42,.04)",
        }}
      >
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontWeight: 700, fontSize: 13, color: "#0f172a" }}>Scene Agent</div>
          <div
            style={{
              fontSize: 10,
              color: "#64748b",
              display: "flex",
              gap: 8,
              alignItems: "center",
            }}
          >
            <span>Claude Code CLI</span>
            <span>·</span>
            <span style={{ color: mcpStatus.startsWith("✓") ? "#16a34a" : "#dc2626" }}>
              MCP: {mcpStatus}
            </span>
            <span>·</span>
            <span style={{ color: selfEvolutionOn ? "#ea580c" : "#64748b" }}>
              Self-evolution: {selfEvolutionReady ? (selfEvolutionOn ? "on" : "off") : "syncing"}
            </span>
            {sessionId && (
              <>
                <span>·</span>
                <span style={{ color: "#64748b" }}>session: {sessionId.slice(0, 8)}</span>
                <span>·</span>
                <span style={{ color: "#475569" }}>turn {turnCount}</span>
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
              border: selfEvolutionOn ? "1px solid #fed7aa" : "1px solid #e6e9ef",
              background: selfEvolutionOn ? "#fff7ed" : "#f8fafc",
              color: selfEvolutionOn ? "#ea580c" : "#64748b",
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
                background: selfEvolutionOn ? "#ea580c" : "#cbd5e1",
                boxShadow: selfEvolutionOn ? "0 0 8px #ea580c66" : "none",
                animation: selfEvolutionOn ? "selfEvoPulse 1.3s ease-in-out infinite" : "none",
                flexShrink: 0,
              }}
            />
            <span>Self-Evolution</span>
            <span
              style={{
                color: selfEvolutionOn ? "#ea580c" : "#94a3b8",
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
              style={headerButtonStyle("#ea580c")}
              title="Annotate screenshot to give feedback"
            >
              Annotate
            </button>
          )}
          {!loading && sessionId && (
            <button
              onClick={handleSave}
              style={headerButtonStyle("#16a34a")}
              title="Save current scene"
            >
              Save
            </button>
          )}
          {!loading && sessionId && latestScreenshot && (
            <button
              onClick={handleShare}
              style={headerButtonStyle("#3b82f6")}
              title="Share to community gallery"
            >
              Share
            </button>
          )}
          {loading && (
            <button onClick={handleStop} style={headerButtonStyle("#dc2626")}>
              Stop
            </button>
          )}
          {!loading && sessionId && (
            <button
              onClick={handleReset}
              title="Reset conversation"
              style={headerButtonStyle("#64748b")}
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
            borderTop: "1px solid #e6e9ef",
            background: "#ffffff",
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
                fontSize: 11,
                borderRadius: 999,
                border: "1px solid #e6e9ef",
                background: "#f8fafc",
                color: "#334155",
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
          borderTop: "1px solid #e6e9ef",
          flexShrink: 0,
          background: "#ffffff",
        }}
      >
        <div
          style={{
            display: "flex",
            gap: 8,
            alignItems: "flex-end",
            background: "#fafbfd",
            border: "1px solid #e6e9ef",
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
              color: "#0f172a",
              fontSize: 13,
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
              width: 34,
              height: 34,
              borderRadius: 10,
              border: "none",
              background: loading ? "#fee2e2" : input.trim() ? "#2563eb" : "#e2e8f0",
              color: loading ? "#dc2626" : input.trim() ? "#ffffff" : "#94a3b8",
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
        <div style={{ marginTop: 5, fontSize: 11, color: "#64748b" }}>
          Enter to send · Shift+Enter for new line
          <span style={{ marginLeft: 8, color: autoSkillSelectionEnabled ? "#2563eb" : "#94a3b8" }}>
            {autoSkillSelectionEnabled ? "Auto-select skills: on" : "Auto-select skills: off"}
            {autoSelectingSkills ? " (selecting...)" : ""}
          </span>          {activeSkills.length > 0 && (
            <span style={{ color: "#2563eb", marginLeft: 8 }}>
              {activeSkills.length} skill{activeSkills.length > 1 ? "s" : ""} active
            </span>
          )}
          {autoSelectionError && (
            <span style={{ color: "#dc2626", marginLeft: 8 }}>{autoSelectionError}</span>
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
      <div style={{ color: "#94a3b8", fontSize: 10 }}>{label}</div>
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
              color: "#94a3b8",
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
            border: "1px solid #e2e8f0",
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
            color: "#16a34a",
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
              background: "#16a34a",
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
            border: "1px solid #e2e8f0",
            borderRadius: 4,
            color: "#64748b",
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
          <span style={{ color: "#64748b", fontSize: 13 }}>Loading screenshot...</span>
        )}
        {errored && (
          <span style={{ color: "#64748b", fontSize: 13 }}>Retrying screenshot...</span>
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
        color: "#64748b",
      }}
    >
      <div style={{ fontSize: 44 }}>{ICONS.camera(44)}</div>
      <div style={{ fontSize: 13 }}>No screenshot yet</div>
      <div
        style={{
          fontSize: 11,
          color: "#e2e8f0",
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
          background: "#e6e9ef",
          border: "1px solid #e2e8f0",
          borderRadius: 6,
          color: "#0f172a",
          cursor: "pointer",
        }}
      >
        ↻ Fetch Latest
      </button>
    </div>
  );
}

// ─── ContextPanel ────────────────────────────────────────────────────────────

const EntityRow = React.memo(function EntityRow({ entity }) {
  const iconFn = CATEGORY_ICONS[entity.category] || ICONS.box;
  const loc = Array.isArray(entity.location) && entity.location.length >= 3
    ? entity.location.map((v) => Math.round(v)).join(", ")
    : null;
  return (
    <div style={{ display: "flex", alignItems: "flex-start", gap: 8, padding: "5px 0", borderBottom: "1px solid #e6e9ef" }}>
      <span style={{ fontSize: 14, flexShrink: 0, marginTop: 1, display: "inline-flex", alignItems: "center" }}>{iconFn(14)}</span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ color: "#0f172a", fontSize: 12, fontWeight: 500 }}>{entity.name}</span>
          {entity.cls && (
            <span style={{ fontSize: 10, color: "#2563eb", background: "#eff4ff", borderRadius: 3, padding: "1px 5px" }}>
              {entity.cls}
            </span>
          )}
        </div>
        {loc && <div style={{ fontSize: 10, color: "#64748b", marginTop: 2 }}>@ ({loc})</div>}
      </div>
    </div>
  );
}); // React.memo(EntityRow)

function ContextPanel({ sessionId, refreshKey }) {
  // Use fine-grained scene context to avoid re-render on every SSE agent/chatlog push
  const scene = useScene();
  const state  = scene.objects?.length > 0 || scene.environment?.ready ? scene : null;
  const [lastUpdated, setLastUpdated] = useState(null);
  const MAX_DISPLAY = 150; // cap to avoid long lists causing layout thrash

  useEffect(() => { setLastUpdated(new Date()); }, [scene.round]);

  const containerStyle = {
    height: "100%", display: "flex", flexDirection: "column",
    background: "#f4f6fa", color: "#0f172a", overflow: "hidden",
  };
  const headerStyle = {
    padding: "10px 14px", borderBottom: "1px solid #e6e9ef",
    display: "flex", alignItems: "center", justifyContent: "space-between", flexShrink: 0,
  };
  const sectionStyle = { padding: "10px 14px 0" };
  const sectionTitleStyle = {
    fontSize: 11, fontWeight: 600, color: "#64748b",
    textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 6,
  };

  if (!state) {
    return (
      <div style={containerStyle}>
        <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center" }}>
          <span style={{ color: "#64748b", fontSize: 13 }}>
            {sessionId ? "No scene data yet — complete a round to populate." : "Start a chat session to see scene context."}
          </span>
        </div>
      </div>
    );
  }

  // Group objects by category, but cap per-category to avoid rendering 30k items
  const byCategory = {};
  let shown = 0;
  for (const o of scene.objects || []) {
    if (shown >= MAX_DISPLAY) break;
    (byCategory[o.category] = byCategory[o.category] || []).push(o);
    shown++;
  }
  const totalObjects = (scene.objects || []).length;
  const truncated = totalObjects > MAX_DISPLAY;

  return (
    <div style={containerStyle}>
      {/* Header */}
      <div style={headerStyle}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ fontSize: 13, fontWeight: 600 }}>Scene Context</span>
          <span style={{ fontSize: 10, background: state.environment?.ready ? "#1a3a2a" : "#fff7ed",
            color: state.environment?.ready ? "#16a34a" : "#f59e0b",
            borderRadius: 3, padding: "1px 6px" }}>
            {state.environment?.ready ? "env ready" : "env not initialized"}
          </span>
          <span style={{ fontSize: 10, color: "#64748b" }}>round {state.round ?? 0}</span>
        </div>
        {lastUpdated && (
          <span style={{ fontSize: 10, color: "#64748b" }}>
            updated {lastUpdated.toLocaleTimeString()}
          </span>
        )}
      </div>

      <div style={{ flex: 1, overflowY: "auto", padding: "0 0 16px" }}>
        {/* Agents section */}
        <div style={sectionStyle}>
          <div style={sectionTitleStyle}>
            {ICONS.robot(13)} Agents &nbsp;<span style={{ color: "#2563eb" }}>{(scene.agents || []).length}</span>
          </div>
          {(scene.agents || []).length === 0
            ? <div style={{ fontSize: 12, color: "#64748b", paddingBottom: 8 }}>No agents in scene</div>
            : (scene.agents || []).map((a) => <EntityRow key={a.name} entity={a} />)
          }
        </div>

        {/* Objects section, grouped by category (capped at MAX_DISPLAY for performance) */}
        <div style={{ ...sectionStyle, marginTop: 12 }}>
          <div style={sectionTitleStyle}>
            {ICONS.box(13)} Objects &nbsp;
            <span style={{ color: "#2563eb" }}>{totalObjects}</span>
            {truncated && <span style={{ color: "#f59e0b", fontSize: 9, marginLeft: 4 }}>(showing {MAX_DISPLAY})</span>}
          </div>
          {totalObjects === 0
            ? <div style={{ fontSize: 12, color: "#64748b" }}>No objects in scene</div>
            : Object.entries(byCategory).map(([cat, items]) => (
                <div key={cat} style={{ marginBottom: 10 }}>
                  <div style={{ fontSize: 11, color: "#64748b", marginBottom: 4 }}>
                    {(CATEGORY_ICONS[cat] || ICONS.box)(11)} {cat}s ({items.length})
                  </div>
                  {items.map((o) => <EntityRow key={o.name} entity={o} />)}
                </div>
              ))
          }
          {truncated && (
            <div style={{ fontSize: 10, color: "#f59e0b", padding: "6px 0", borderTop: "1px solid var(--line)", marginTop: 4 }}>
              ⚠ {totalObjects - MAX_DISPLAY} more objects not shown — use the coding agent to query specific actors.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ─── AgentPanel ──────────────────────────────────────────────────────────────

const AGENT_COLORS = ["#2563eb", "#16a34a", "#f59e0b", "#f778ba", "#bc8cff", "#ea580c", "#2563eb", "#56d364"];

async function sendAgentChat(agentName, message, sessionId, onEvent, signal) {
  console.log("[sendAgentChat] START", { agentName, message, sessionId });
  const response = await fetch(`${API_BASE}/agent-chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ agentName, message, sessionId }),
    signal,
  });
  console.log("[sendAgentChat] fetch response", { status: response.status, ok: response.ok, hasBody: !!response.body });
  if (!response.ok || !response.body) {
    const err = await response.json().catch(() => ({ error: response.statusText }));
    console.error("[sendAgentChat] ERROR response", err);
    throw new Error(err.error || `Server error: ${response.status}`);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let lastDataTime = Date.now();
  const IDLE_TIMEOUT = 210000; // 3.5 min — allow time for long MCP tool calls

  for (;;) {
    const readPromise = reader.read();
    const timeoutPromise = new Promise((_, reject) => {
      const check = setInterval(() => {
        if (Date.now() - lastDataTime > IDLE_TIMEOUT) {
          clearInterval(check);
          reader.cancel();
          reject(new Error("Agent response timeout"));
        }
      }, 5000);
      readPromise.then(() => clearInterval(check)).catch(() => clearInterval(check));
    });

    let result;
    try {
      result = await Promise.race([readPromise, timeoutPromise]);
    } catch {
      onEvent({ type: "done", data: { isError: true, text: "Agent timed out." } });
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
        try { onEvent({ type: eventType, data: JSON.parse(data) }); } catch {}
      }
    }
  }
}

function AgentCard({ agent, sessionId, pieActive, colorIdx, onExpand }) {
  const [status, setStatus] = useState("idle");
  const [thought, setThought] = useState(""); // Current reasoning text
  const [actions, setActions] = useState([]); // [{tool, ok}]
  const [input, setInput] = useState("");
  const abortRef = useRef(null);
  const activityRef = useRef(null);
  const color = AGENT_COLORS[colorIdx % AGENT_COLORS.length];

  // Get activities + messages from unified poll
  const pollData = usePoll();
  const pastActivities = pollData.activities?.[agent.name] || [];
  // Messages where this agent was mentioned or targeted
  const agentMessages = useMemo(() => {
    return (pollData.chatLog || []).filter(m =>
      m.from !== agent.name && (m.to === agent.name || m.to === "all" || !m.to)
    ).slice(-5);
  }, [pollData.chatLog, agent.name]);

  const handleSend = useCallback(async (text) => {
    console.log("[AgentCard] handleSend called", { text, status, agentName: agent.name, sessionId, pieActive });
    if (!text.trim() || status === "running") {
      console.log("[AgentCard] handleSend BLOCKED", { empty: !text.trim(), running: status === "running" });
      return;
    }
    setInput("");
    setStatus("running");
    setThought("");
    setActions([]);

    const controller = new AbortController();
    abortRef.current = controller;
    try {
      console.log("[AgentCard] calling sendAgentChat...", { agentName: agent.name, sessionId });
      await sendAgentChat(agent.name, text, sessionId, (event) => {
        console.log("[AgentCard] event received", event.type, event.data);
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
      console.log("[AgentCard] sendAgentChat resolved OK");
      setStatus("done");
      // Clear live thought so it doesn't duplicate pastActivities from SSE
      setThought("");
      setActions([]);
    } catch (err) {
      console.error("[AgentCard] sendAgentChat ERROR", err.name, err.message);
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
  const statusColors = { idle: "#64748b", running: "#f59e0b", done: "#16a34a", error: "#dc2626" };

  // Render a single activity (ReAct format)
  const renderActivity = (act, isLive) => {
    const t = act.thought || act.response || "";
    const acts = isLive ? actions : (act.actions || []);
    return (
      <div style={{ fontSize: 11, lineHeight: "1.5" }}>
        {/* Thought */}
        {t && (
          <div style={{ color: "#1e293b", whiteSpace: "pre-wrap", marginBottom: 4 }}>
            <span style={{ color: "#64748b", fontWeight: 600 }}>Thought: </span>{t.slice(0, 500)}
          </div>
        )}
        {/* Actions */}
        {acts.length > 0 && acts.map((a, i) => (
          <div key={i} style={{ display: "flex", alignItems: "center", gap: 4, marginBottom: 2, paddingLeft: 8 }}>
            <span style={{ color: a.ok === null ? "#f59e0b" : a.ok ? "#16a34a" : "#dc2626", fontWeight: 600 }}>
              {a.ok === null ? "..." : a.ok ? "ok" : "err"}
            </span>
            <span style={{ color: "#2563eb" }}>{a.tool || a.name}</span>
          </div>
        ))}
      </div>
    );
  };

  // ── Collapsed card ──────────────────────────────────────────────────────────
  return (
    <div
      onClick={() => onExpand?.(agent)}
      style={{
        border: `1px solid ${color}33`, borderRadius: 8, background: "#ffffff",
        display: "flex", flexDirection: "column", overflow: "hidden", minWidth: 0,
        cursor: "pointer", transition: "box-shadow 0.15s, border-color 0.15s",
      }}
      onMouseEnter={e => { e.currentTarget.style.borderColor = color + "88"; e.currentTarget.style.boxShadow = `0 2px 10px ${color}18`; }}
      onMouseLeave={e => { e.currentTarget.style.borderColor = color + "33"; e.currentTarget.style.boxShadow = "none"; }}
    >
      {/* Header row */}
      <div style={{ padding: "8px 10px", display: "flex", alignItems: "center", gap: 6, background: `${color}0a` }}>
        <span style={{ width: 8, height: 8, borderRadius: "50%", background: statusColors[status], flexShrink: 0,
          boxShadow: status === "running" ? `0 0 0 2px ${statusColors[status]}44` : "none" }} />
        <span style={{ fontSize: 12, fontWeight: 700, color, flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{agent.name}</span>
        <span style={{ fontSize: 9, color: "#94a3b8", background: "#f1f5f9", borderRadius: 3, padding: "1px 5px", flexShrink: 0 }}>{agent.cls || "?"}</span>
        <span style={{ fontSize: 10, color: "#94a3b8" }}>›</span>
      </div>

      {/* Quick info */}
      <div style={{ padding: "5px 10px 7px", fontSize: 10 }}>
        {loc ? (
          <div style={{ color: "#64748b", fontFamily: "monospace" }}>{loc}</div>
        ) : (
          <div style={{ color: "#94a3b8", fontStyle: "italic" }}>location unknown</div>
        )}
        {(thought || actions.length > 0) && (
          <div style={{ color: "#64748b", marginTop: 3, display: "flex", alignItems: "center", gap: 4 }}>
            {status === "running" && <span style={{ width: 6, height: 6, borderRadius: "50%", background: "#f59e0b", animation: "pulse 1s ease-in-out infinite" }}/>}
            <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {thought ? thought.slice(0, 50) + (thought.length > 50 ? "…" : "") : actions[actions.length-1]?.tool}
            </span>
          </div>
        )}
        {pastActivities.length > 0 && !thought && (
          <div style={{ color: "#94a3b8", marginTop: 2, fontSize: 9 }}>
            {pastActivities.length} past action{pastActivities.length > 1 ? "s" : ""}
          </div>
        )}
      </div>
    </div>
  );
}

// ─── AgentDetailPanel — expanded view with camera ────────────────────────────

function AgentDetailPanel({ agent, sessionId, pieActive, colorIdx, onClose }) {
  const color = AGENT_COLORS[colorIdx % AGENT_COLORS.length];
  const [activeTab, setActiveTab]   = useState("camera"); // camera | activity | chat
  const [camImg, setCamImg]         = useState(null);
  const [camLoading, setCamLoading] = useState(false);
  const [camError, setCamError]     = useState(null);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [input, setInput]           = useState("");
  const [sending, setSending]       = useState(false);
  const camIntervalRef = useRef(null);
  const activityRef    = useRef(null);

  const pollData = usePoll();
  const pastActivities = pollData.activities?.[agent.name] || [];
  const agentMessages  = useMemo(() =>
    (pollData.chatLog || []).filter(m =>
      m.from !== agent.name && (m.to === agent.name || m.to === "all" || !m.to)
    ).slice(-20),
  [pollData.chatLog, agent.name]);

  const loc = Array.isArray(agent.location) && agent.location.length >= 3
    ? agent.location : null;

  // Focus UE viewport camera on this agent and take screenshot
  const focusAndShoot = useCallback(async () => {
    if (!loc) { setCamError("No location data"); return; }
    setCamLoading(true);
    setCamError(null);
    try {
      const [x, y, z] = loc;
      // Position camera 400 units behind-left and 350 above agent
      const camX = x - 400, camY = y - 400, camZ = z + 350;
      const dx = x - camX, dy = y - camY, dz = z - camZ;
      const horiz = Math.sqrt(dx*dx + dy*dy);
      const pitch = horiz > 1 ? Math.atan2(dz, horiz) * (180 / Math.PI) : -45;
      const yaw   = Math.atan2(dy, dx) * (180 / Math.PI);

      // 1) Move viewport camera
      await fetch(`${API_BASE}/camera`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cmd: "set_camera", args: [camX, camY, camZ, pitch, yaw, 0] }),
      });

      // 2) Wait a tick for UE to update, then grab screenshot
      await new Promise(r => setTimeout(r, 600));
      const resp = await fetch(`${API_BASE}/screenshot/latest?t=${Date.now()}`);
      if (!resp.ok) throw new Error("No screenshot available");
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      setCamImg(prev => { if (prev) URL.revokeObjectURL(prev); return url; });
    } catch (e) {
      setCamError(e.message);
    } finally {
      setCamLoading(false);
    }
  }, [loc]);

  // Auto-refresh camera when tab is active
  useEffect(() => {
    if (activeTab !== "camera" || !autoRefresh) {
      if (camIntervalRef.current) clearInterval(camIntervalRef.current);
      return;
    }
    focusAndShoot();
    camIntervalRef.current = setInterval(focusAndShoot, 4000);
    return () => clearInterval(camIntervalRef.current);
  }, [activeTab, autoRefresh, focusAndShoot]);

  // Scroll activity log
  useEffect(() => {
    if (activityRef.current) activityRef.current.scrollTop = activityRef.current.scrollHeight;
  }, [pastActivities, agentMessages]);

  const handleSend = async () => {
    const text = input.trim();
    if (!text || sending) return;
    setInput("");
    setSending(true);
    try {
      await fetch(`${API_BASE}/agent-broadcast`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, target: agent.name }),
      });
    } catch {}
    setSending(false);
  };

  const statusColors = { idle:"#64748b", running:"#f59e0b", done:"#16a34a", error:"#dc2626" };
  const status = agent.status || "idle";

  return (
    <div style={{
      position: "absolute", inset: 0, zIndex: 50,
      background: "var(--panel)",
      display: "flex", flexDirection: "column",
      borderRadius: 0,  // inside a panel — no extra radius
    }}>
      {/* ── Panel header ── */}
      <div style={{
        padding: "10px 12px", borderBottom: "1px solid var(--line)",
        display: "flex", alignItems: "center", gap: 8, flexShrink: 0,
        background: `${color}0a`,
      }}>
        <button onClick={onClose} style={{
          background: "none", border: "1px solid var(--line)", borderRadius: 6,
          padding: "3px 8px", cursor: "pointer", fontSize: 11, color: "var(--ink-3)",
          display: "flex", alignItems: "center", gap: 3,
        }}>← Back</button>
        <span style={{ width: 8, height: 8, borderRadius: "50%", background: statusColors[status],
          boxShadow: status === "running" ? `0 0 0 2px ${statusColors[status]}44` : "none" }}/>
        <span style={{ fontSize: 14, fontWeight: 700, color }}>{agent.name}</span>
        <span style={{ fontSize: 10, color: "var(--ink-3)", background: "var(--bg)", borderRadius: 4, padding: "1px 6px" }}>{agent.cls}</span>
        <div style={{ flex: 1 }} />
        {loc && (
          <span style={{ fontSize: 9, color: "var(--ink-3)", fontFamily: "monospace" }}>
            {loc.map(v => Math.round(v)).join(", ")}
          </span>
        )}
      </div>

      {/* ── Tab bar ── */}
      <div style={{ display: "flex", gap: 0, borderBottom: "1px solid var(--line)", flexShrink: 0, background: "var(--panel)" }}>
        {[
          { id: "camera",   label: "📷 Camera"   },
          { id: "activity", label: "📋 Activity" },
          { id: "chat",     label: "💬 Chat"     },
        ].map(t => (
          <button key={t.id} onClick={() => setActiveTab(t.id)} style={{
            flex: 1, padding: "8px 4px", border: "none",
            borderBottom: `2px solid ${activeTab === t.id ? color : "transparent"}`,
            background: "none", cursor: "pointer",
            fontSize: 11, fontWeight: activeTab === t.id ? 700 : 400,
            color: activeTab === t.id ? color : "var(--ink-3)",
            transition: "all 0.12s",
          }}>{t.label}</button>
        ))}
      </div>

      {/* ── Camera tab ── */}
      {activeTab === "camera" && (
        <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>
          {/* Camera controls */}
          <div style={{ padding: "6px 10px", display: "flex", alignItems: "center", gap: 6, borderBottom: "1px solid var(--line)", flexShrink: 0 }}>
            <label style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 11, cursor: "pointer", color: "var(--ink-3)" }}>
              <input type="checkbox" checked={autoRefresh} onChange={e => setAutoRefresh(e.target.checked)} style={{ accentColor: color }}/>
              Auto (4s)
            </label>
            <button onClick={focusAndShoot} disabled={camLoading} style={{
              padding: "3px 10px", borderRadius: 6, border: `1px solid ${color}44`,
              background: `${color}11`, color, cursor: camLoading ? "wait" : "pointer",
              fontSize: 11, fontWeight: 600, opacity: camLoading ? 0.6 : 1,
            }}>
              {camLoading ? "Capturing…" : "↻ Capture"}
            </button>
            {!loc && <span style={{ fontSize: 10, color: "var(--red)" }}>No location — no camera</span>}
          </div>

          {/* Camera image */}
          <div style={{ flex: 1, background: "#0b1220", position: "relative", overflow: "hidden" }}>
            {camImg ? (
              <img src={camImg} alt={`${agent.name} view`} style={{
                width: "100%", height: "100%", objectFit: "contain", display: "block",
              }}/>
            ) : camError ? (
              <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", height: "100%", gap: 8 }}>
                <div style={{ fontSize: 28, opacity: 0.4 }}>📷</div>
                <div style={{ color: "#dc2626", fontSize: 12 }}>{camError}</div>
                <button onClick={focusAndShoot} style={{ padding: "5px 14px", borderRadius: 6, border: "1px solid #2563eb", background: "rgba(37,99,235,.15)", color: "#93c5fd", cursor: "pointer", fontSize: 11 }}>Retry</button>
              </div>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", height: "100%", gap: 10 }}>
                {camLoading ? (
                  <>
                    <div style={{ width: 32, height: 32, borderRadius: "50%", border: "3px solid rgba(37,99,235,.2)", borderTopColor: "#2563eb", animation: "ps-spin 0.9s linear infinite" }}/>
                    <div style={{ color: "#64748b", fontSize: 12 }}>Focusing camera…</div>
                  </>
                ) : (
                  <>
                    <div style={{ fontSize: 28, opacity: 0.3 }}>📷</div>
                    <div style={{ color: "#475569", fontSize: 12 }}>Click "Capture" to take a screenshot</div>
                  </>
                )}
              </div>
            )}
            {/* Loading overlay */}
            {camLoading && camImg && (
              <div style={{ position: "absolute", inset: 0, background: "rgba(11,18,32,.5)", display: "flex", alignItems: "center", justifyContent: "center" }}>
                <div style={{ width: 24, height: 24, borderRadius: "50%", border: "2px solid rgba(255,255,255,.2)", borderTopColor: "#fff", animation: "ps-spin 0.9s linear infinite" }}/>
              </div>
            )}
          </div>
        </div>
      )}

      {/* ── Activity tab ── */}
      {activeTab === "activity" && (
        <div ref={activityRef} style={{ flex: 1, overflowY: "auto", padding: 10 }}>
          {agentMessages.length > 0 && (
            <div style={{ marginBottom: 10 }}>
              <div style={{ fontSize: 9, fontWeight: 700, color: "var(--ink-3)", letterSpacing: ".08em", marginBottom: 4 }}>INCOMING MESSAGES</div>
              {agentMessages.map((m, i) => (
                <div key={i} style={{ marginBottom: 5, fontSize: 11, borderLeft: `2px solid ${color}`, paddingLeft: 7, color: "var(--ink-2)" }}>
                  <span style={{ fontWeight: 600, color }}>{m.from}</span>: {m.text}
                </div>
              ))}
            </div>
          )}
          {pastActivities.length === 0 && agentMessages.length === 0 ? (
            <div style={{ color: "var(--ink-3)", fontSize: 12, fontStyle: "italic", textAlign: "center", paddingTop: 20 }}>No activity yet</div>
          ) : (
            pastActivities.map((act, i) => {
              const t = act.thought || act.response || "";
              const acts = act.actions || [];
              return (
                <div key={i} style={{ marginBottom: 10, padding: "8px 10px", borderRadius: 7, background: "var(--bg)", border: "1px solid var(--line)" }}>
                  <div style={{ fontSize: 9, color: "var(--ink-3)", marginBottom: 4 }}>Turn {i + 1}</div>
                  {t && <div style={{ fontSize: 11, color: "var(--ink-2)", marginBottom: 4, lineHeight: 1.5 }}>{t.slice(0, 200)}{t.length > 200 ? "…" : ""}</div>}
                  {acts.map((a, j) => (
                    <div key={j} style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 10 }}>
                      <span style={{ color: a.ok ? "#16a34a" : "#dc2626" }}>{a.ok ? "✓" : "✗"}</span>
                      <span style={{ color: "var(--blue)", fontFamily: "monospace" }}>{a.tool || a.name}</span>
                    </div>
                  ))}
                </div>
              );
            })
          )}
        </div>
      )}

      {/* ── Chat tab ── */}
      {activeTab === "chat" && (
        <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>
          <div style={{ padding: "8px 10px", fontSize: 11, color: "var(--ink-3)", borderBottom: "1px solid var(--line)", flexShrink: 0 }}>
            Send a direct command to <strong style={{ color }}>{agent.name}</strong>
          </div>
          <div ref={activityRef} style={{ flex: 1, overflowY: "auto", padding: 10 }}>
            {agentMessages.map((m, i) => (
              <div key={i} style={{ marginBottom: 8, fontSize: 11, lineHeight: 1.5 }}>
                <div style={{ fontWeight: 600, color: "var(--ink-3)", marginBottom: 2 }}>{m.from}</div>
                <div style={{ color: "var(--ink-2)", background: "var(--bg)", borderRadius: 6, padding: "5px 8px" }}>{m.text}</div>
              </div>
            ))}
          </div>
          <div style={{ padding: "8px 10px", borderTop: "1px solid var(--line)", display: "flex", gap: 6, flexShrink: 0 }}>
            <textarea
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSend(); } }}
              placeholder={`Command ${agent.name}… (Enter to send)`}
              rows={2}
              style={{
                flex: 1, resize: "none", borderRadius: 8, border: "1px solid var(--line)",
                background: "var(--bg)", padding: "6px 10px", fontSize: 12, color: "var(--ink-2)",
                outline: "none", fontFamily: "inherit",
              }}
            />
            <button onClick={handleSend} disabled={!input.trim() || sending} style={{
              padding: "6px 14px", borderRadius: 8, border: "none",
              background: input.trim() && !sending ? color : "var(--line)",
              color: "#fff", cursor: input.trim() && !sending ? "pointer" : "default",
              fontSize: 12, fontWeight: 600, alignSelf: "flex-end",
            }}>Send</button>
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Communication History (group chat sidebar) ─────────────────────────────

function CommHistory({ agents }) {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [target, setTarget] = useState("all");
  const scrollRef = useRef(null);

  // Get chat log from unified poll
  const pollData = usePoll();
  useEffect(() => {
    const data = pollData.chatLog || [];
    if (data.length > 0) {
      setMessages(prev => {
        const existing = new Set(prev.map(m => `${m.from}-${m.timestamp}`));
        const newMsgs = data.filter(m => !existing.has(`${m.from}-${m.timestamp}`));
        if (!newMsgs.length) return prev;
        return [...prev, ...newMsgs].slice(-100);
      });
    }
  }, [pollData.chatLog]);

  useEffect(() => { if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight; }, [messages]);

  const handleSend = () => {
    const text = input.trim();
    if (!text) return;
    // Use broadcast endpoint — triggers agent turns automatically
    fetch(`${API_BASE}/agent-broadcast`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, target: target === "all" ? "all" : target }),
    }).catch(() => {});
    setMessages(prev => [...prev, { from: "user", to: target, text, timestamp: Date.now() }]);
    setInput("");
  };

  const colors = { user: "#0f172a" };
  (agents || []).forEach((a, i) => { colors[a.name] = AGENT_COLORS[i % AGENT_COLORS.length]; });

  // Render @mentions in text with color
  const renderText = (text) => {
    const parts = text.split(/(@\w+)/g);
    return parts.map((part, i) => {
      if (part.startsWith("@")) {
        const name = part.slice(1);
        return <span key={i} style={{ color: colors[name] || "#2563eb", fontWeight: 600 }}>{part}</span>;
      }
      return part;
    });
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", background: "#f4f6fa" }}>
      <div style={{ padding: "10px 12px", borderBottom: "1px solid #e6e9ef", fontSize: 12, fontWeight: 600, color: "#0f172a" }}>
        Communication
      </div>
      <div ref={scrollRef} style={{ flex: 1, overflowY: "auto", padding: "8px 12px" }}>
        {messages.length === 0 ? (
          <div style={{ color: "#475569", fontSize: 11, textAlign: "center", marginTop: 40 }}>
            Messages between you and agents will appear here.
          </div>
        ) : messages.map((m, i) => (
          <div key={i} style={{ marginBottom: 8, fontSize: 12, lineHeight: "1.5" }}>
            <div style={{ display: "flex", alignItems: "baseline", gap: 6 }}>
              <span style={{ fontWeight: 700, color: colors[m.from] || "#64748b" }}>{m.from === "user" ? "You" : m.from}</span>
              {m.to && m.to !== "all" && <span style={{ fontSize: 10, color: "#64748b" }}>to <span style={{ color: colors[m.to] || "#2563eb" }}>@{m.to}</span></span>}
              <span style={{ fontSize: 9, color: "#475569", marginLeft: "auto" }}>{new Date(m.timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>
            </div>
            <div style={{ color: "#1e293b", marginTop: 2 }}>{renderText(m.text)}</div>
          </div>
        ))}
      </div>
      <div style={{ padding: "8px 12px", borderTop: "1px solid #e6e9ef", display: "flex", gap: 6 }}>
        <select value={target} onChange={e => setTarget(e.target.value)}
          style={{ background: "#ffffff", border: "1px solid #e2e8f0", borderRadius: 4, color: "#64748b", fontSize: 11, padding: "4px 6px" }}>
          <option value="all">@all</option>
          {(agents || []).map(a => <option key={a.name} value={a.name}>@{a.name}</option>)}
        </select>
        <input value={input} onChange={e => setInput(e.target.value)}
          onKeyDown={e => { if (e.key === "Enter") { e.preventDefault(); handleSend(); } }}
          placeholder="Message..."
          style={{ flex: 1, background: "#ffffff", border: "1px solid #e2e8f0", borderRadius: 4, padding: "5px 8px", color: "#0f172a", fontSize: 11, outline: "none" }}
        />
        <button onClick={handleSend} disabled={!input.trim()} style={{
          background: input.trim() ? "#15803d" : "#e6e9ef", border: "none", borderRadius: 4,
          padding: "5px 10px", color: "#fff", fontSize: 11, cursor: input.trim() ? "pointer" : "default", opacity: input.trim() ? 1 : 0.5,
        }}>Send</button>
      </div>
    </div>
  );
}

// ─── AgentPanel — vertical split: agent cards top, comm bottom ───────────────
function AgentPanel({ sessionId, commHeight = 200, onCommHeightChange }) {
  const agentsCtx = useAgents();
  const statusCtx = useStatus();
  const pieActive = statusCtx.pieActive;

  // Use fine-grained contexts for agents
  const contextAgents = useMemo(() => {
    const sessions = agentsCtx.sessions || [];
    if (sessions.length > 0) {
      return sessions.map(s => ({ name: s.agentName, cls: s.agentClass, location: s.location, status: s.status }));
    }
    return (agentsCtx.agents || []);
  }, [agentsCtx.sessions, agentsCtx.agents]);

  // Which agent is expanded to detail view
  const [expandedAgent, setExpandedAgent] = useState(null);

  // Close detail panel if the agent disappears from UE
  useEffect(() => {
    if (expandedAgent && !contextAgents.find(a => a.name === expandedAgent.name)) {
      setExpandedAgent(null);
    }
    // Also update expanded agent data if it changes (location, status)
    if (expandedAgent) {
      const updated = contextAgents.find(a => a.name === expandedAgent.name);
      if (updated && (updated.location !== expandedAgent.location || updated.status !== expandedAgent.status)) {
        setExpandedAgent(updated);
      }
    }
  }, [contextAgents, expandedAgent]);

  // Vertical resize handle for comm panel
  const resizeRef = useRef(null);
  const dragging   = useRef(false);
  const startY     = useRef(0);
  const startH     = useRef(commHeight);

  const onMouseDown = useCallback((e) => {
    e.preventDefault();
    dragging.current = true;
    startY.current   = e.clientY;
    startH.current   = commHeight;
    document.body.style.cursor = "row-resize";
    document.body.style.userSelect = "none";
    if (resizeRef.current) resizeRef.current.classList.add("dragging");
    const onMove = (ev) => {
      if (!dragging.current) return;
      const delta = startY.current - ev.clientY; // drag up = taller comm
      const next  = Math.max(60, Math.min(400, startH.current + delta));
      onCommHeightChange?.(next);
    };
    const onUp = () => {
      dragging.current = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      if (resizeRef.current) resizeRef.current.classList.remove("dragging");
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  }, [commHeight, onCommHeightChange]);

  const empty = contextAgents.length === 0;

  return (
    <div className="sw-right-inner">
      {/* ── Agents pane (fills remaining height) ── */}
      <div className="sw-agents-pane" style={{ flex: 1 }}>
        {/* Sub-header */}
        <div style={{ padding:"8px 12px", borderBottom:"1px solid var(--line)", display:"flex", alignItems:"center", gap:8, flexShrink:0, background:"var(--panel)" }}>
          <span style={{ fontSize:12, fontWeight:700, color:"var(--ink)" }}>
            Agents {contextAgents.length > 0 && <span style={{ color:"var(--ink-3)", fontWeight:400 }}>({contextAgents.length})</span>}
          </span>
          <div style={{ flex:1 }} />
          <span style={{ width:6, height:6, borderRadius:"50%", background:pieActive?"#22c55e":"#94a3b8", boxShadow: pieActive?"0 0 0 2px rgba(34,197,94,.2)":"none" }}/>
          <span style={{ fontSize:10, color:pieActive?"#16a34a":"var(--ink-3)" }}>
            {pieActive ? "PIE Active" : "No PIE"}
          </span>
        </div>

        {/* Detail panel overlay — covers agents pane when expanded */}
        {expandedAgent && (
          <div style={{ position:"absolute", inset:0, zIndex:40 }}>
            <AgentDetailPanel
              agent={expandedAgent}
              sessionId={sessionId}
              pieActive={pieActive}
              colorIdx={contextAgents.findIndex(a => a.name === expandedAgent.name)}
              onClose={() => setExpandedAgent(null)}
            />
          </div>
        )}

        {empty ? (
          <div style={{ flex:1, display:"flex", flexDirection:"column", alignItems:"center", justifyContent:"center", gap:8, padding:20 }}>
            <div style={{ fontSize:28, opacity:0.3 }}>🤖</div>
            <div style={{ fontSize:12, color:"var(--ink-3)", textAlign:"center", lineHeight:1.5 }}>
              {pieActive ? "Spawn agents to see them here." : "Start PIE in Unreal Engine\nto enable agent control."}
            </div>
          </div>
        ) : (
          <div style={{ flex:1, overflowY:"auto", padding:8, display:"grid", gridTemplateColumns:"repeat(auto-fill, minmax(200px, 1fr))", gap:8, alignContent:"flex-start" }}>
            {contextAgents.map((a, i) => (
              <AgentCard key={a.name} agent={a} sessionId={sessionId} pieActive={pieActive} colorIdx={i}
                onExpand={setExpandedAgent}
              />
            ))}
          </div>
        )}
      </div>

      {/* ── Vertical resize handle ── */}
      <div className="sw-resize-row" ref={resizeRef} onMouseDown={onMouseDown} title="Drag to resize" />

      {/* ── Communication pane (fixed height, resizable) ── */}
      <div className="sw-comm-pane" style={{ height: commHeight }}>
        <CommHistory agents={contextAgents} />
      </div>
    </div>
  );
}

// ─── ViewportPanel ───────────────────────────────────────────────────────────

function ViewportPanel({ latestScreenshot }) {
  const [mode, setMode]           = useState("pixelstream"); // default to live stream
  const [imgKey, setImgKey]       = useState(0);
  const [screenshotUrl, setScreenshotUrl] = useState(null);
  const [autoRefresh, setAutoRefresh]     = useState(false);
  const [refreshInterval, setRefreshInterval] = useState(5);
  const [cameraMoving, setCameraMoving]   = useState(false);
  const intervalRef = useRef(null);

  const handleCameraPreset = useCallback(async (args) => {
    setCameraMoving(true);
    try { await sendCameraCommand("set_camera", args); }
    finally { setCameraMoving(false); }
  }, []);

  const [playerUrl, setPlayerUrl] = useState(null);
  useEffect(() => {
    fetch("/api/pixel-streaming-url")
      .then(r => r.json())
      .then(d => {
        if (d.url) {
          try {
            setPlayerUrl(`${d.url}?MatchViewportRes=true&HoveringMouse=true`);
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

  // Auto-refresh — only run when tab is visible AND mode=screenshot
  // Uses visibilitychange to pause when user switches away (reduces background load)
  useEffect(() => {
    if (intervalRef.current) clearInterval(intervalRef.current);
    if (!autoRefresh || mode !== "screenshot") return;

    const tick = () => {
      if (document.visibilityState === "visible") fetchLatestScreenshot();
    };
    intervalRef.current = setInterval(tick, refreshInterval * 1000);
    document.addEventListener("visibilitychange", tick);
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
      document.removeEventListener("visibilitychange", tick);
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
    <div style={{ display:"flex", flexDirection:"column", height:"100%", background:"#0b1220" }}>

      {/* Dark toolbar */}
      <div style={{
        padding:"7px 12px",
        borderBottom:"1px solid rgba(255,255,255,.07)",
        display:"flex", alignItems:"center", gap:6,
        flexShrink:0, background:"#0d1526", userSelect:"none",
      }}>
        {/* Mode toggle */}
        <div style={{ display:"flex", gap:3 }}>
          {[
            { id:"pixelstream", label:"🔴 Live" },
            { id:"screenshot",  label:"📷 Shot" },
          ].map(m => (
            <button key={m.id} onClick={() => setMode(m.id)} style={{
              padding:"3px 10px", fontSize:11, borderRadius:6, cursor:"pointer",
              border: `1px solid ${mode===m.id?"#2563eb":"rgba(255,255,255,.1)"}`,
              background: mode===m.id?"rgba(37,99,235,.25)":"transparent",
              color: mode===m.id?"#93c5fd":"#94a3b8",
              fontWeight: mode===m.id?700:400,
            }}>{m.label}</button>
          ))}
        </div>

        {/* Screenshot controls */}
        {mode === "screenshot" && (
          <>
            <button onClick={fetchLatestScreenshot} style={{
              padding:"3px 8px", fontSize:11, borderRadius:5, cursor:"pointer",
              border:"1px solid rgba(255,255,255,.15)", background:"rgba(255,255,255,.08)",
              color:"#e2e8f0",
            }}>↻ Refresh</button>
            <label style={{ display:"flex", alignItems:"center", gap:3, fontSize:11, color:"#64748b", cursor:"pointer" }}>
              <input type="checkbox" checked={autoRefresh} onChange={e => setAutoRefresh(e.target.checked)} style={{ accentColor:"#2563eb" }}/>
              Auto
            </label>
            <select value={refreshInterval} onChange={e => setRefreshInterval(Number(e.target.value))} style={{
              padding:"2px 5px", fontSize:10, background:"rgba(255,255,255,.08)",
              border:"1px solid rgba(255,255,255,.1)", borderRadius:4, color:"#94a3b8",
            }}>
              {[2,3,5,10].map(s => <option key={s} value={s}>{s}s</option>)}
            </select>
          </>
        )}

        {/* Camera presets */}
        <div style={{ display:"flex", gap:2, alignItems:"center", marginLeft:4 }}>
          <span style={{ fontSize:9, color:"#475569", letterSpacing:".06em" }}>CAM</span>
          {CAMERA_PRESETS.map(p => (
            <button key={p.label} title={p.title} disabled={cameraMoving}
              onClick={() => handleCameraPreset(p.args)} style={{
                padding:"2px 7px", fontSize:10, borderRadius:4, cursor:cameraMoving?"wait":"pointer",
                border:"1px solid rgba(255,255,255,.1)", background:"rgba(255,255,255,.06)",
                color: cameraMoving?"#334155":"#94a3b8",
              }}>{p.label}</button>
          ))}
          <button title="Unlock camera from agent" disabled={cameraMoving}
            onClick={() => { setCameraMoving(true); sendCameraCommand("unpilot_camera").finally(()=>setCameraMoving(false)); }}
            style={{
              padding:"2px 7px", fontSize:10, borderRadius:4, cursor:cameraMoving?"wait":"pointer",
              border:"1px solid rgba(220,38,38,.3)", background:"rgba(220,38,38,.1)",
              color: cameraMoving?"#334155":"#fca5a5",
            }}>✕ Unlock</button>
        </div>

        <div style={{ flex:1 }}/>

        {/* Open in tab */}
        {playerUrl && (
          <a href={playerUrl} target="_blank" rel="noreferrer" style={{
            padding:"3px 8px", fontSize:10, borderRadius:5,
            border:"1px solid rgba(255,255,255,.1)", background:"rgba(255,255,255,.06)",
            color:"#64748b", textDecoration:"none", display:"inline-block",
          }}>⧉ Pop out</a>
        )}
      </div>

      {/* Viewport — live stream always mounted, screenshot overlays */}
      <div style={{ flex:1, position:"relative", overflow:"hidden" }}>
        {/* PixelStreamPlayer — always mounted (preserves WebRTC) */}
        <div style={{ width:"100%", height:"100%", display: mode==="pixelstream"?"block":"none" }}>
          <PixelStreamPlayer playerUrl={playerUrl} />
        </div>
        {mode === "screenshot" && (
          <ScreenshotView src={screenshotUrl} imgKey={imgKey} onRefresh={fetchLatestScreenshot} />
        )}
      </div>

      {/* Micro status bar */}
      <div style={{
        padding:"2px 12px", background:"#0d1526",
        borderTop:"1px solid rgba(255,255,255,.05)",
        display:"flex", alignItems:"center", gap:14,
        fontSize:9, color:"#334155", flexShrink:0,
      }}>
        <span>UE 5.3.2 · SimWorld Studio</span>
        {playerUrl && <span>{playerUrl.match(/cirrus=(\d+)/)?.[1] ? `Cirrus :${playerUrl.match(/cirrus=(\d+)/)[1]}` : playerUrl}</span>}
        {mode==="screenshot" && screenshotUrl && <span style={{ color:"#16a34a", marginLeft:"auto" }}>● Screenshot ready</span>}
      </div>
    </div>
  );
}

// ─── AssetPlaceholder ────────────────────────────────────────────────────────

function AssetPlaceholder({ id, category }) {
  const color = CATEGORY_COLORS[category] || "#e2e8f0";
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
      <span style={{ fontSize: 28, opacity: 0.6, display: "inline-flex" }}>{(CATEGORY_ICONS[category] || ICONS.cube)(28)}</span>
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
        border: "1px solid #e6e9ef",
        background: "#ffffff",
        cursor: "pointer",
        transition: "border-color 0.15s",
      }}
      onMouseEnter={(e) => (e.currentTarget.style.borderColor = "#2563eb")}
      onMouseLeave={(e) => (e.currentTarget.style.borderColor = "#e6e9ef")}
    >
      <div
        style={{
          height: 90,
          background: "#f4f6fa",
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
            color: "#0f172a",
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
      onMouseEnter={(e) => (e.currentTarget.style.background = "#ffffff")}
      onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
    >
      <div
        style={{
          width: 36,
          height: 36,
          borderRadius: 4,
          overflow: "hidden",
          background: "#f4f6fa",
          flexShrink: 0,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          border: "1px solid #e6e9ef",
        }}
      >
        {imgError ? (
          <span style={{ fontSize: 16, display: "inline-flex" }}>{(CATEGORY_ICONS[category] || ICONS.cube)(16)}</span>
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
        <div style={{ fontSize: 11, color: "#0f172a", fontWeight: 500 }}>{item.id}</div>
        {item.path && (
          <div
            style={{
              fontSize: 9,
              color: "#475569",
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
  const containerRef = useRef(null);
  const loadedRef = useRef(false);

  // Lazy load: fetch assets only when this component becomes visible
  // Uses IntersectionObserver so assets are not fetched until drawer is opened
  useEffect(() => {
    if (loadedRef.current) return;
    const el = containerRef.current;
    if (!el) return;
    const observer = new IntersectionObserver(([entry]) => {
      if (entry.isIntersecting && !loadedRef.current) {
        loadedRef.current = true;
        fetchAssets().then(setAssets).catch(() => {});
        observer.disconnect();
      }
    }, { threshold: 0.1 });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  if (!assets) {
    return (
      <div ref={containerRef} style={{ padding: 12, color: "#64748b", fontSize: 12, height:"100%" }}>
        Loading assets…
      </div>
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
        background: "#f4f6fa",
      }}
    >
      {/* Search bar */}
      <div
        style={{
          padding: "8px 10px",
          borderBottom: "1px solid #e6e9ef",
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
            background: "#ffffff",
            border: "1px solid #e2e8f0",
            borderRadius: 4,
            color: "#0f172a",
            outline: "none",
            boxSizing: "border-box",
          }}
        />
        <button
          onClick={() => setViewMode(viewMode === "grid" ? "list" : "grid")}
          style={{
            padding: "4px 8px",
            fontSize: 11,
            background: "#e6e9ef",
            border: "1px solid #e2e8f0",
            borderRadius: 4,
            color: "#64748b",
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
          borderBottom: "1px solid #e6e9ef",
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
              border: `1px solid ${cat === currentCategory ? "#3b82f6" : "#e6e9ef"}`,
              background: cat === currentCategory ? "#eff4ff" : "transparent",
              color: cat === currentCategory ? "#2563eb" : "#64748b",
              cursor: "pointer",
            }}
          >
            {(CATEGORY_ICONS[cat] || ICONS.cube)(12)} {cat.replace(/_/g, " ")}
          </button>
        ))}
      </div>

      {/* Category description */}
      {categoryData?.description && (
        <div
          style={{
            padding: "6px 10px",
            fontSize: 10,
            color: "#64748b",
            borderBottom: "1px solid #e6e9ef",
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
              color: "#475569",
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
          borderTop: "1px solid #e6e9ef",
          fontSize: 10,
          color: "#475569",
          display: "flex",
          alignItems: "center",
          gap: 8,
        }}
      >
        <span>
          {items.length} assets {search && `matching "${search}"`}
        </span>
        <span style={{ marginLeft: "auto", color: "#64748b" }}>Click to insert into chat</span>
      </div>
    </div>
  );
}

// ─── SceneManager ────────────────────────────────────────────────────────────

function SceneManager({ onLoadScene, currentSessionId }) {
  const [scenes, setScenes] = useState([]);
  const [loading, setLoading] = useState(false); // start false — lazy load
  const containerRef = useRef(null);
  const loadedRef    = useRef(false);

  const reload = useCallback(() => {
    setLoading(true);
    fetchScenes()
      .then(setScenes)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  // Lazy: only fetch when component scrolls into view (drawer opened)
  useEffect(() => {
    if (loadedRef.current) return;
    const el = containerRef.current;
    if (!el) return;
    const observer = new IntersectionObserver(([entry]) => {
      if (entry.isIntersecting && !loadedRef.current) {
        loadedRef.current = true;
        reload();
        observer.disconnect();
      }
    }, { threshold: 0.1 });
    observer.observe(el);
    return () => observer.disconnect();
  }, [reload]);

  const handleDelete = async (id) => {
    if (confirm("Delete this scene?")) {
      await deleteScene(id);
      reload();
    }
  };

  return (
    <div
      ref={containerRef}
      style={{
        display: "flex",
        flexDirection: "column",
        height: "100%",
        background: "#f4f6fa",
      }}
    >
      <div
        style={{
          padding: "8px 12px",
          borderBottom: "1px solid #e6e9ef",
          display: "flex",
          alignItems: "center",
          gap: 8,
        }}
      >
        <span style={{ fontSize: 12, fontWeight: 600, color: "#0f172a" }}>Saved Scenes</span>
        <button
          onClick={reload}
          style={{
            marginLeft: "auto",
            padding: "3px 8px",
            fontSize: 10,
            background: "#e6e9ef",
            border: "1px solid #e2e8f0",
            borderRadius: 4,
            color: "#64748b",
            cursor: "pointer",
          }}
        >
          Refresh
        </button>
      </div>

      <div style={{ flex: 1, overflow: "auto", padding: 8 }}>
        {loading && (
          <div style={{ padding: 12, color: "#64748b", fontSize: 12 }}>Loading...</div>
        )}
        {!loading && scenes.length === 0 && (
          <div
            style={{
              padding: 20,
              textAlign: "center",
              color: "#475569",
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
              border: "1px solid #e6e9ef",
              background: "#ffffff",
            }}
          >
            {scene.thumbnail && (
              <div
                style={{
                  height: 100,
                  overflow: "hidden",
                  borderBottom: "1px solid #e6e9ef",
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
              <div style={{ fontSize: 12, fontWeight: 500, color: "#0f172a" }}>{scene.name}</div>
              {scene.prompt && (
                <div
                  style={{
                    fontSize: 10,
                    color: "#64748b",
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
                <span style={{ fontSize: 9, color: "#475569" }}>
                  {new Date(scene.updatedAt).toLocaleDateString()}
                </span>
                <div style={{ marginLeft: "auto", display: "flex", gap: 4 }}>
                  <button
                    onClick={() => onLoadScene(scene)}
                    style={{
                      padding: "2px 8px",
                      fontSize: 10,
                      borderRadius: 3,
                      border: "1px solid #3b82f6",
                      background: "transparent",
                      color: "#2563eb",
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
                      border: "1px solid #e2e8f0",
                      background: "transparent",
                      color: "#dc2626",
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
  const borderColor = isWinner ? "#16a34a" : isLoser ? "#dc262633" : "#e2e8f0";

  return (
    <div
      style={{
        border: `2px solid ${borderColor}`,
        borderRadius: 12,
        overflow: "hidden",
        background: "#ffffff",
        transition: "border-color 0.2s",
        opacity: isLoser ? 0.6 : 1,
      }}
    >
      <div
        style={{
          padding: "10px 16px",
          borderBottom: "1px solid #e6e9ef",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
        }}
      >
        <span style={{ fontSize: 14, fontWeight: 600, color: "#0f172a" }}>
          {label}
          {isWinner && " ✅"}
        </span>
        {revealed && side && (
          <span
            style={{
              fontSize: 11,
              color: "#64748b",
              background: "#e6e9ef",
              padding: "2px 8px",
              borderRadius: 4,
            }}
          >
            {side.agentName}
          </span>
        )}
        {!revealed && (
          <span style={{ fontSize: 11, color: "#475569" }}>Identity hidden</span>
        )}
      </div>

      <div
        style={{
          height: 250,
          background: "#f4f6fa",
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
          <div style={{ color: "#475569", fontSize: 13 }}>
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
              background: "#15803d",
              border: "1px solid #15803d",
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
  background: "#e6e9ef",
  border: "1px solid #e2e8f0",
  borderRadius: 6,
  color: "#64748b",
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
        background: "#f4f6fa",
      }}
    >
      {/* Header */}
      <div
        style={{
          padding: "16px 24px",
          borderBottom: "1px solid #e6e9ef",
          display: "flex",
          alignItems: "center",
          gap: 12,
        }}
      >
        <span style={{ fontSize: 24, display: "inline-flex", alignItems: "center" }}>{ICONS.swords(24)}</span>
        <div>
          <div style={{ fontSize: 18, fontWeight: 700, color: "#0f172a" }}>Arena Battle</div>
          <div style={{ fontSize: 12, color: "#64748b" }}>
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
              border: "1px solid #e2e8f0",
              background: showAgents ? "#eff4ff" : "#e6e9ef",
              color: showAgents ? "#2563eb" : "#64748b",
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
            borderBottom: "1px solid #e6e9ef",
            background: "#ffffff",
          }}
        >
          <div
            style={{
              fontSize: 12,
              color: "#64748b",
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
                  border: `1px solid ${agent.enabled ? "#15803d" : "#e2e8f0"}`,
                  background: agent.enabled ? "#15803d11" : "transparent",
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
                  <div style={{ fontSize: 12, fontWeight: 600, color: "#0f172a" }}>
                    {agent.name}
                  </div>
                  <div style={{ fontSize: 10, color: "#64748b" }}>
                    {agent.type}
                    {agent.model ? ` (${agent.model})` : ""}
                  </div>
                  {agent.description && (
                    <div style={{ fontSize: 9, color: "#475569", marginTop: 2 }}>
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
                color: "#0f172a",
                marginBottom: 8,
              }}
            >
              Enter a Scene Prompt
            </div>
            <div style={{ fontSize: 14, color: "#64748b", marginBottom: 24 }}>
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
                background: "#ffffff",
                border: "1px solid #e2e8f0",
                borderRadius: 8,
                color: "#0f172a",
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
                  border: `1px solid ${prompt.trim() ? "#15803d" : "#e2e8f0"}`,
                  borderRadius: 8,
                  color: prompt.trim() ? "#fff" : "#94a3b8",
                  cursor: prompt.trim() ? "pointer" : "default",
                  fontWeight: 600,
                }}
              >
                Start Battle
              </button>
            </div>
            <div style={{ fontSize: 11, color: "#475569", marginTop: 12 }}>
              {agents.filter((a) => a.enabled).length} agent
              {agents.filter((a) => a.enabled).length !== 1 ? "s" : ""} enabled
            </div>
          </div>
        )}

        {/* Generating phase */}
        {phase === "generating" && (
          <div style={{ textAlign: "center", padding: 80 }}>
            <div style={{ fontSize: 40, marginBottom: 16 }}>{ICONS.swords(40)}</div>
            <div style={{ fontSize: 16, color: "#0f172a", fontWeight: 600 }}>
              Generating scenes...
            </div>
            <div style={{ fontSize: 13, color: "#64748b", marginTop: 8 }}>
              "{prompt}"
            </div>
            {progress && (
              <div style={{ marginTop: 20 }}>
                {progress.phase === "starting" && (
                  <div style={{ fontSize: 12, color: "#2563eb" }}>
                    Matched: {progress.agentA} vs {progress.agentB}
                  </div>
                )}
                {progress.phase === "generating_a" && (
                  <div style={{ fontSize: 12, color: "#16a34a" }}>
                    Agent A ({progress.agent}) is generating...
                  </div>
                )}
                {progress.phase === "generating_b" && (
                  <div style={{ fontSize: 12, color: "#16a34a" }}>
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
                background: "#e6e9ef",
                borderRadius: 2,
                overflow: "hidden",
                margin: "24px auto 0",
              }}
            >
              <div
                style={{
                  width: progress?.phase === "generating_b" ? "80%" : "40%",
                  height: "100%",
                  background: "#3b82f6",
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
                border: "1px solid #e2e8f0",
                borderRadius: 6,
                color: "#64748b",
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
              <div style={{ fontSize: 13, color: "#64748b" }}>Prompt</div>
              <div style={{ fontSize: 16, color: "#0f172a", fontWeight: 500 }}>
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
                    color: "#16a34a",
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
                  <div style={{ fontSize: 12, color: "#64748b" }}>
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
                      background: "#3b82f6",
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
                      background: shared ? "#e6e9ef" : "#15803d",
                      border: `1px solid ${shared ? "#e2e8f0" : "#15803d"}`,
                      borderRadius: 6,
                      color: shared ? "#64748b" : "#fff",
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
        background: "#f4f6fa",
      }}
    >
      <div
        style={{
          padding: "16px 24px",
          borderBottom: "1px solid #e6e9ef",
          display: "flex",
          alignItems: "center",
          gap: 12,
        }}
      >
        <span style={{ fontSize: 24, display: "inline-flex", alignItems: "center" }}>{ICONS.trophy(24)}</span>
        <div>
          <div style={{ fontSize: 18, fontWeight: 700, color: "#0f172a" }}>Leaderboard</div>
          <div style={{ fontSize: 12, color: "#64748b" }}>
            Agent rankings based on Elo ratings from arena battles
          </div>
        </div>
        {entries.length > 0 && (
          <div style={{ marginLeft: "auto", fontSize: 12, color: "#475569" }}>
            {entries.reduce((sum, e) => sum + e.numBattles, 0)} total battles
          </div>
        )}
      </div>

      <div style={{ flex: 1, overflow: "auto", padding: 24 }}>
        {loading ? (
          <div style={{ textAlign: "center", padding: 60, color: "#64748b" }}>Loading...</div>
        ) : entries.length === 0 ? (
          <div style={{ textAlign: "center", padding: 60 }}>
            <div style={{ fontSize: 40, marginBottom: 12 }}>{ICONS.trophy(40)}</div>
            <div
              style={{
                fontSize: 16,
                color: "#0f172a",
                fontWeight: 600,
                marginBottom: 8,
              }}
            >
              No battles yet
            </div>
            <div style={{ fontSize: 13, color: "#64748b" }}>
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
                color: "#475569",
                fontWeight: 600,
                borderBottom: "1px solid #e6e9ef",
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
                    borderBottom: "1px solid #e6e9ef",
                    background: i === 0 ? "#2563eb0a" : "transparent",
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
                    <span style={{ fontSize: 14, fontWeight: 600, color: "#0f172a" }}>
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
                        fontSize: 10,
                        marginLeft: 4,
                        color: delta >= 0 ? "#16a34a" : "#dc2626",
                      }}
                    >
                      {delta >= 0 ? "+" : ""}
                      {delta}
                    </span>
                  </div>
                  <span style={{ textAlign: "right", fontSize: 13, color: "#64748b" }}>
                    {entry.numBattles}
                  </span>
                  <span style={{ textAlign: "right", fontSize: 13, color: "#16a34a" }}>
                    {entry.wins}
                  </span>
                  <span style={{ textAlign: "right", fontSize: 13, color: "#dc2626" }}>
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
                background: "#ffffff",
                border: "1px solid #e6e9ef",
                fontSize: 11,
                color: "#64748b",
                lineHeight: 1.6,
              }}
            >
              <div style={{ fontWeight: 600, marginBottom: 4, color: "#0f172a" }}>
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
        border: "1px solid #e6e9ef",
        background: "#ffffff",
        cursor: "pointer",
        transition: "border-color 0.15s, transform 0.15s",
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.borderColor = "#2563eb";
        e.currentTarget.style.transform = "translateY(-2px)";
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.borderColor = "#e6e9ef";
        e.currentTarget.style.transform = "translateY(0)";
      }}
    >
      <div style={{ height: 180, background: "#f4f6fa", overflow: "hidden" }}>
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
              color: "#475569",
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
            color: "#0f172a",
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
              background: "#3b82f622",
              color: "#2563eb",
              border: "1px solid #3b82f644",
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
                background: "#e6e9ef",
                color: "#64748b",
              }}
            >
              {tag}
            </span>
          ))}
          <span style={{ marginLeft: "auto", fontSize: 10, color: "#475569" }}>
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
          background: "#f4f6fa",
          border: "1px solid #e2e8f0",
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
            borderBottom: "1px solid #e6e9ef",
            display: "flex",
            alignItems: "center",
          }}
        >
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 16, fontWeight: 600, color: "#0f172a" }}>{scene.prompt}</div>
            <div style={{ fontSize: 11, color: "#64748b", marginTop: 4 }}>
              Generated by {scene.agentName} on {new Date(scene.created_at).toLocaleString()}
            </div>
          </div>
          {onBack ? (
            <button
              onClick={onBack}
              style={{
                padding: "4px 10px",
                fontSize: 12,
                background: "#e6e9ef",
                border: "1px solid #e2e8f0",
                borderRadius: 6,
                color: "#1e293b",
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
                color: "#64748b",
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
                color: "#475569",
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
                    background: i === currentImg ? "#2563eb" : "#94a3b8",
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
                  background: "#3b82f622",
                  color: "#2563eb",
                  border: "1px solid #3b82f644",
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
                  background: "#e6e9ef",
                  color: "#64748b",
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
                  color: "#64748b",
                  fontWeight: 600,
                  marginBottom: 6,
                }}
              >
                Generated Code Preview
              </div>
              <pre
                style={{
                  fontSize: 11,
                  color: "#1e293b",
                  background: "#ffffff",
                  border: "1px solid #e6e9ef",
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
        background: "#f4f6fa",
      }}
    >
      {/* Header */}
      <div
        style={{
          padding: "16px 24px",
          borderBottom: "1px solid #e6e9ef",
          display: "flex",
          alignItems: "center",
          gap: 12,
        }}
      >
        <span style={{ fontSize: 24, display: "inline-flex", alignItems: "center" }}>{ICONS.frame(24)}</span>
        <div>
          <div style={{ fontSize: 18, fontWeight: 700, color: "#0f172a" }}>Gallery</div>
          <div style={{ fontSize: 12, color: "#64748b" }}>
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
              border: "1px solid #e2e8f0",
              background: "#ffffff",
              color: "#64748b",
              cursor: "pointer",
            }}
          >
            <option value="newest">Newest first</option>
            <option value="oldest">Oldest first</option>
          </select>
          <span style={{ fontSize: 12, color: "#475569" }}>
            {filtered.length} scene{filtered.length !== 1 ? "s" : ""}
          </span>
        </div>
      </div>

      {/* Tag filter */}
      {allTags.length > 0 && (
        <div
          style={{
            padding: "8px 24px",
            borderBottom: "1px solid #e6e9ef",
            display: "flex",
            gap: 6,
            flexWrap: "wrap",
            alignItems: "center",
          }}
        >
          <span style={{ fontSize: 10, color: "#475569", marginRight: 4 }}>Tags:</span>
          <button
            onClick={() => setActiveTag(null)}
            style={{
              fontSize: 10,
              padding: "2px 8px",
              borderRadius: 10,
              border: `1px solid ${activeTag ? "#e2e8f0" : "#2563eb"}`,
              background: activeTag ? "transparent" : "#3b82f622",
              color: activeTag ? "#64748b" : "#2563eb",
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
                border: `1px solid ${activeTag === tag ? "#2563eb" : "#e2e8f0"}`,
                background: activeTag === tag ? "#3b82f622" : "transparent",
                color: activeTag === tag ? "#2563eb" : "#64748b",
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
          <div style={{ textAlign: "center", padding: 60, color: "#64748b" }}>Loading...</div>
        ) : filtered.length === 0 ? (
          <div style={{ textAlign: "center", padding: 60 }}>
            <div style={{ fontSize: 40, marginBottom: 12 }}>{ICONS.frame(40)}</div>
            <div
              style={{
                fontSize: 16,
                color: "#0f172a",
                fontWeight: 600,
                marginBottom: 8,
              }}
            >
              No scenes yet
            </div>
            <div style={{ fontSize: 13, color: "#64748b" }}>
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
        border: "1px solid #e6e9ef",
        background: "#ffffff",
        cursor: "pointer",
        transition: "border-color 0.15s, transform 0.15s",
        display: "flex",
        flexDirection: "column",
        gap: 8,
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.borderColor = "#2563eb";
        e.currentTarget.style.transform = "translateY(-2px)";
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.borderColor = "#e6e9ef";
        e.currentTarget.style.transform = "translateY(0)";
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <div style={{ fontSize: 14, fontWeight: 600, color: "#0f172a", flex: 1 }}>
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
              background: "#dc262622",
              color: "#dc2626",
              border: "1px solid #dc262644",
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
                background: "#dc2626",
                boxShadow: "0 0 6px #dc2626",
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
            background: skill.source === "custom" ? "#3b82f622" : "#e6e9ef",
            color: skill.source === "custom" ? "#2563eb" : "#94a3b8",
            border: `1px solid ${skill.source === "custom" ? "#3b82f644" : "#e2e8f0"}`,
            flexShrink: 0,
          }}
        >
          {skill.source}
        </span>
      </div>
      <div style={{ fontSize: 12, color: "#64748b", lineHeight: 1.5 }}>{desc}</div>
      {skill.tags.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
          {skill.tags.map((tag) => (
            <span
              key={tag}
              style={{
                fontSize: 9,
                padding: "2px 6px",
                borderRadius: 4,
                background: (TAG_COLORS[tag] || "#e2e8f0") + "33",
                color: TAG_COLORS[tag] || "#64748b",
                border: `1px solid ${TAG_COLORS[tag] || "#e2e8f0"}44`,
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
          borderTop: "1px solid #e6e9ef",
          fontSize: 10,
          color: "#475569",
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
          background: "#f4f6fa",
          border: "1px solid #e2e8f0",
          borderRadius: 12,
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
        }}
      >
        <div
          style={{
            padding: "14px 20px",
            borderBottom: "1px solid #e6e9ef",
            display: "flex",
            alignItems: "center",
            gap: 10,
          }}
        >
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 18, fontWeight: 600, color: "#0f172a" }}>{skill.name}</div>
            <div style={{ fontSize: 11, color: "#64748b", marginTop: 4 }}>
              v{skill.version} by {skill.author}
              <span
                style={{
                  marginLeft: 8,
                  padding: "2px 7px",
                  borderRadius: 10,
                  background: skill.source === "custom" ? "#3b82f622" : "#e6e9ef",
                  color: skill.source === "custom" ? "#2563eb" : "#94a3b8",
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
                background: "#b91c1c22",
                border: "1px solid #b91c1c66",
                borderRadius: 6,
                color: "#dc2626",
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
              color: "#64748b",
              cursor: "pointer",
            }}
          >
            ✕
          </button>
        </div>

        <div style={{ padding: "12px 20px", borderBottom: "1px solid #e6e9ef" }}>
          <div style={{ fontSize: 13, color: "#1e293b", lineHeight: 1.5 }}>
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
                    background: (TAG_COLORS[tag] || "#e2e8f0") + "33",
                    color: TAG_COLORS[tag] || "#64748b",
                    border: `1px solid ${TAG_COLORS[tag] || "#e2e8f0"}44`,
                  }}
                >
                  {tag}
                </span>
              ))}
            </div>
          )}
          {skill.dependencies.length > 0 && (
            <div style={{ fontSize: 11, color: "#64748b", marginTop: 10 }}>
              <span style={{ fontWeight: 600 }}>Dependencies:</span>{" "}
              {skill.dependencies.map((dep, i) => (
                <span key={dep}>
                  <span style={{ color: "#2563eb" }}>{dep}</span>
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
              color: "#475569",
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
              color: "#1e293b",
              lineHeight: 1.6,
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
              fontFamily:
                "ui-monospace, 'Cascadia Code', 'Source Code Pro', Menlo, monospace",
              margin: 0,
              background: "#ffffff",
              border: "1px solid #e6e9ef",
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
    background: "#ffffff",
    border: "1px solid #e2e8f0",
    borderRadius: 6,
    color: "#0f172a",
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
          background: "#f4f6fa",
          border: "1px solid #e2e8f0",
          borderRadius: 12,
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
        }}
      >
        <div
          style={{
            padding: "14px 20px",
            borderBottom: "1px solid #e6e9ef",
            display: "flex",
            alignItems: "center",
          }}
        >
          <span style={{ fontSize: 16, fontWeight: 600, color: "#0f172a" }}>
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
              color: "#64748b",
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
            <label style={{ fontSize: 11, color: "#64748b", display: "block", marginBottom: 4 }}>
              Skill ID (lowercase, no spaces)
            </label>
            <input value={id} onChange={(e) => setId(e.target.value)} placeholder="my_custom_skill" style={inputStyle} />
          </div>
          <div>
            <label style={{ fontSize: 11, color: "#64748b", display: "block", marginBottom: 4 }}>Name</label>
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="My Custom Skill" style={inputStyle} />
          </div>
          <div>
            <label style={{ fontSize: 11, color: "#64748b", display: "block", marginBottom: 4 }}>Description (short summary)</label>
            <input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="What this skill teaches the agent to do" style={inputStyle} />
          </div>
          <div>
            <label style={{ fontSize: 11, color: "#64748b", display: "block", marginBottom: 4 }}>Tags (comma-separated)</label>
            <input value={tags} onChange={(e) => setTags(e.target.value)} placeholder="buildings, layout, custom" style={inputStyle} />
          </div>
          <div style={{ flex: 1 }}>
            <label style={{ fontSize: 11, color: "#64748b", display: "block", marginBottom: 4 }}>
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
          {error && <div style={{ fontSize: 11, color: "#dc2626", padding: "2px 0" }}>{error}</div>}
        </div>

        <div
          style={{
            padding: "12px 20px",
            borderTop: "1px solid #e6e9ef",
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
              background: "#e6e9ef",
              border: "1px solid #e2e8f0",
              borderRadius: 6,
              color: "#1e293b",
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
              background: "#15803d",
              border: "1px solid #15803d",
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
          background: "#f4f6fa",
          border: "1px solid #e2e8f0",
          borderRadius: 12,
          padding: 24,
        }}
      >
        <div style={{ fontSize: 14, color: "#0f172a", fontWeight: 600, marginBottom: 8 }}>
          Confirm Delete
        </div>
        <div style={{ fontSize: 13, color: "#64748b", lineHeight: 1.5, marginBottom: 20 }}>
          {message}
        </div>
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <button
            onClick={onCancel}
            style={{
              padding: "6px 14px",
              fontSize: 12,
              background: "#e6e9ef",
              border: "1px solid #e2e8f0",
              borderRadius: 6,
              color: "#1e293b",
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
              background: "#b91c1c",
              border: "1px solid #b91c1c66",
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
        background: "#f4f6fa",
      }}
    >
      <div
        style={{
          padding: "16px 24px",
          borderBottom: "1px solid #e6e9ef",
          display: "flex",
          alignItems: "center",
          gap: 12,
        }}
      >
        <span style={{ fontSize: 24, display: "inline-flex", alignItems: "center" }}>{ICONS.tools(24)}</span>
        <div>
          <div style={{ fontSize: 18, fontWeight: 700, color: "#0f172a" }}>Skills</div>
          <div style={{ fontSize: 12, color: "#64748b" }}>
            Browse, create, and manage skills that teach the AI agent new capabilities
          </div>
        </div>
        <button
          onClick={() => setShowCreate(true)}
          style={{
            marginLeft: "auto",
            padding: "6px 14px",
            fontSize: 12,
            background: "#15803d",
            border: "1px solid #15803d",
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
          borderBottom: "1px solid #e6e9ef",
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
            background: "#ffffff",
            border: "1px solid #e2e8f0",
            borderRadius: 6,
            color: "#0f172a",
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
                border: `1px solid ${filter === f ? "#2563eb" : "#e2e8f0"}`,
                background: filter === f ? "#3b82f622" : "transparent",
                color: filter === f ? "#2563eb" : "#64748b",
                cursor: "pointer",
                textTransform: "capitalize",
              }}
            >
              {f}
            </button>
          ))}
        </div>
        <span style={{ fontSize: 12, color: "#475569", whiteSpace: "nowrap" }}>
          {filtered.length} skill{filtered.length !== 1 ? "s" : ""}
        </span>
      </div>

      <div style={{ flex: 1, overflow: "auto", padding: 20 }}>
        {loading ? (
          <div style={{ textAlign: "center", padding: 60, color: "#64748b" }}>Loading...</div>
        ) : filtered.length === 0 ? (
          <div style={{ textAlign: "center", padding: 60 }}>
            <div style={{ fontSize: 40, marginBottom: 12 }}>{ICONS.tools(40)}</div>
            <div style={{ fontSize: 16, color: "#0f172a", fontWeight: 600, marginBottom: 8 }}>
              No skills found
            </div>
            <div style={{ fontSize: 13, color: "#64748b" }}>
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
        border: "1px solid #e6e9ef",
        background: "#ffffff",
        cursor: "pointer",
        transition: "border-color 0.15s, transform 0.15s",
        display: "flex",
        flexDirection: "column",
        gap: 8,
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.borderColor = "#2563eb";
        e.currentTarget.style.transform = "translateY(-2px)";
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.borderColor = "#e6e9ef";
        e.currentTarget.style.transform = "translateY(0)";
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <div style={{ fontSize: 14, fontWeight: 600, color: "#0f172a", flex: 1 }}>
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
              background: "#dc262622",
              color: "#dc2626",
              border: "1px solid #dc262644",
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
                background: "#dc2626",
                boxShadow: "0 0 6px #dc2626",
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
            background: tool.enabled ? "#15803d22" : "transparent",
            color: tool.enabled ? "#16a34a" : "#64748b",
            border: "1px solid #e2e8f0",
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
          borderTop: "1px solid #e6e9ef",
          fontSize: 10,
          color: "#64748b",
        }}
      >
        <div>
          Template: <span style={{ color: "#1e293b" }}>{tool.template || "–"}</span>
        </div>
        <div>
          Primitive: <span style={{ color: "#1e293b" }}>{tool.primitive || "–"}</span>
        </div>
        <div>
          Usage: <span style={{ color: "#1e293b" }}>{tool.metrics?.usageCount || 0}</span>
        </div>
        <div>
          Success: <span style={{ color: "#1e293b" }}>{successRate == null ? "–" : `${successRate}%`}</span>
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
            border: "1px solid #e2e8f0",
            background: tool.enabled ? "#e6e9ef" : "#3b82f622",
            color: tool.enabled ? "#1e293b" : "#2563eb",
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
            border: "1px solid #b91c1c66",
            background: "#b91c1c22",
            color: "#dc2626",
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
        border: "1px solid #e6e9ef",
        background: "#ffffff",
        display: "flex",
        alignItems: "center",
        gap: 8,
        cursor: "pointer",
      }}
    >
      <span style={{ color: "#0f172a", fontSize: 12, fontWeight: 600 }}>{tool.name}</span>
      <span
        style={{
          marginLeft: "auto",
          fontSize: 10,
          color: "#64748b",
          border: "1px solid #e2e8f0",
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
          background: "#f4f6fa",
          border: "1px solid #e2e8f0",
          borderRadius: 12,
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
        }}
      >
        <div
          style={{
            padding: "14px 20px",
            borderBottom: "1px solid #e6e9ef",
            display: "flex",
            alignItems: "center",
            gap: 10,
          }}
        >
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 18, fontWeight: 600, color: "#0f172a" }}>{tool.name || tool.id}</div>
            <div style={{ fontSize: 11, color: "#64748b", marginTop: 4 }}>
              <span style={{ color: "#2563eb", fontFamily: "monospace" }}>{tool.mcpName}</span>
              <span
                style={{
                  marginLeft: 8,
                  padding: "2px 7px",
                  borderRadius: 10,
                  border: "1px solid #e2e8f0",
                  background: readOnly ? "#3b82f622" : tool.enabled ? "#15803d22" : "transparent",
                  color: readOnly ? "#2563eb" : tool.enabled ? "#16a34a" : "#64748b",
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
                background: "#b91c1c22",
                border: "1px solid #b91c1c66",
                borderRadius: 6,
                color: "#dc2626",
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
              color: "#64748b",
              cursor: "pointer",
            }}
          >
            ✕
          </button>
        </div>

        <div style={{ padding: "12px 20px", borderBottom: "1px solid #e6e9ef" }}>
          <div style={{ fontSize: 13, color: "#1e293b", lineHeight: 1.5 }}>
            {tool.description || "No description"}
          </div>
          {!readOnly && (
            <div style={{ marginTop: 10, fontSize: 12, color: "#64748b" }}>
              Related skills:{" "}
              <span style={{ color: "#1e293b" }}>{relatedSkills && relatedSkills.length > 0 ? relatedSkills.length : 0}</span>
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
                    background: "#3b82f622",
                    color: "#2563eb",
                    border: "1px solid #3b82f644",
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
                color: "#64748b",
              }}
            >
              <div>Template: <span style={{ color: "#1e293b" }}>{tool.template || "–"}</span></div>
              <div>Primitive: <span style={{ color: "#1e293b" }}>{tool.primitive || "–"}</span></div>
              <div>Usage: <span style={{ color: "#1e293b" }}>{tool.metrics?.usageCount || 0}</span></div>
              <div>Success: <span style={{ color: "#1e293b" }}>{successRate == null ? "–" : `${successRate}%`}</span></div>
            </div>
          )}

          <div
            style={{
              marginTop: 16,
              fontSize: 10,
              color: "#475569",
              fontWeight: 600,
              textTransform: "uppercase",
              letterSpacing: 0.5,
            }}
          >
            How Claude Calls This Tool
          </div>
          <div style={{ marginTop: 8, fontSize: 12, color: "#64748b", lineHeight: 1.5 }}>
            Use <span style={{ color: "#2563eb", fontFamily: "monospace" }}>{tool.mcpName}</span> with an arguments object.
          </div>
          <div style={{ marginTop: 10, fontSize: 12, color: "#64748b" }}>
            Required arguments:{" "}
            <span style={{ color: "#1e293b" }}>
              {requiredKeys.length > 0 ? requiredKeys.join(", ") : "none"}
            </span>
          </div>
          <div style={{ marginTop: 10, fontSize: 11, color: "#64748b" }}>Input schema</div>
          <pre
            style={{
              margin: "6px 0 0",
              background: "#ffffff",
              border: "1px solid #e6e9ef",
              borderRadius: 8,
              padding: 12,
              fontSize: 11,
              color: "#1e293b",
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
              borderTop: "1px solid #e6e9ef",
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
                border: "1px solid #e2e8f0",
                background: tool.enabled ? "#e6e9ef" : "#3b82f622",
                color: tool.enabled ? "#1e293b" : "#2563eb",
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
        background: "#f4f6fa",
      }}
    >
      <div
        style={{
          padding: "16px 24px",
          borderBottom: "1px solid #e6e9ef",
          display: "flex",
          alignItems: "center",
          gap: 12,
        }}
      >
        <span style={{ fontSize: 24, display: "inline-flex", alignItems: "center" }}>{ICONS.wrench(24)}</span>
        <div>
          <div style={{ fontSize: 18, fontWeight: 700, color: "#0f172a" }}>Tools</div>
          <div style={{ fontSize: 12, color: "#64748b" }}>
            Static MCP tools for reference + learned tools you can manage
          </div>
        </div>
      </div>

      {error && (
        <div
          style={{
            padding: "8px 24px",
            borderBottom: "1px solid #e6e9ef",
            color: "#dc2626",
            fontSize: 12,
          }}
        >
          {error}
        </div>
      )}

      <div style={{ flex: 1, overflow: "auto", padding: 20 }}>
        <div
          style={{
            border: "1px solid #e6e9ef",
            borderRadius: 10,
            marginBottom: 16,
            overflow: "hidden",
            background: "#f4f6fa",
          }}
        >
          <div
            style={{
              padding: "10px 12px",
              borderBottom: "1px solid #e6e9ef",
              display: "flex",
              alignItems: "center",
              gap: 8,
              background: "#ffffff",
            }}
          >
            <span style={{ fontSize: 14, display: "inline-flex", alignItems: "center" }}>{ICONS.book(14)}</span>
            <span style={{ fontSize: 13, fontWeight: 700, color: "#0f172a" }}>
              Static MCP Tools (Reference)
            </span>
            <span style={{ marginLeft: "auto", fontSize: 11, color: "#64748b" }}>
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
            border: "1px solid #e6e9ef",
            borderRadius: 10,
            overflow: "hidden",
            background: "#f4f6fa",
          }}
        >
          <div
            style={{
              padding: "10px 12px",
              borderBottom: "1px solid #e6e9ef",
              display: "flex",
              alignItems: "center",
              gap: 8,
              background: "#ffffff",
            }}
          >
            <span style={{ fontSize: 14, display: "inline-flex", alignItems: "center" }}>{ICONS.brain(14)}</span>
            <span style={{ fontSize: 13, fontWeight: 700, color: "#0f172a" }}>
              Dynamic Learned Tools
            </span>
            <span style={{ marginLeft: "auto", fontSize: 11, color: "#64748b" }}>
              {filteredTools.length} tool{filteredTools.length !== 1 ? "s" : ""}
            </span>
          </div>
          <div
            style={{
              padding: 12,
              borderBottom: "1px solid #e6e9ef",
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
                background: "#ffffff",
                border: "1px solid #e2e8f0",
                borderRadius: 6,
                color: "#0f172a",
                outline: "none",
              }}
            />
          </div>
          <div style={{ padding: 12 }}>
            {loading ? (
              <div style={{ color: "#64748b", padding: 20 }}>Loading…</div>
            ) : filteredTools.length === 0 ? (
              <div style={{ textAlign: "center", padding: 40 }}>
                <div style={{ fontSize: 32, marginBottom: 10 }}>{ICONS.wrench(32)}</div>
                <div style={{ fontSize: 15, color: "#0f172a", fontWeight: 600, marginBottom: 6 }}>
                  No learned tools found
                </div>
                <div style={{ fontSize: 12, color: "#64748b" }}>
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
    <div style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 11, color: "#64748b" }}>
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
        const accent = isTool ? "#dc2626" : "#16a34a";
        const title = isTool ? "New Tool Learned" : "New Skill Learned";
        return (
          <div
            key={item.id}
            style={{
              minHeight: 40,
              borderRadius: 6,
              border: `1px solid ${accent}44`,
              borderLeft: `4px solid ${accent}`,
              background: "linear-gradient(90deg, #ffffff 0%, #f1f5f9 100%)",
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
                  color: "#0f172a",
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
                  color: "#64748b",
                  background: "#e6e9ef",
                  border: "1px solid #e2e8f0",
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

// ─── useSession — slot acquire, 60s heartbeat, 30-min countdown ──────────────
// Design rules:
//  - "dev" mode: server returns {dev:true} → no countdown, no modals, full access
//  - "managed" mode: server returns real token+TTL → countdown + expiry modal
//  - Pool full: server returns {error,code:"POOL_FULL"} → waiting room modal
//  - Never store _dev tokens in sessionStorage (they don't survive server restart)

const STORAGE_KEY   = "sw_session_token";
const HEARTBEAT_MS  = 60_000;
const WARN_SECS     = 5 * 60;   // warn when < 5 min left

function useSession() {
  const [session,  setSession]  = useState(null);   // null=loading, {dev,token,...}=ready
  const [poolFull, setPoolFull] = useState(null);   // {message, queueLength} | null
  const [secsLeft, setSecsLeft] = useState(null);
  const [expired,  setExpired]  = useState(false);
  const acquiredAt = useRef(null);
  const ttlMsRef   = useRef(0);

  // ── Acquire on mount ───────────────────────────────────────────────────────
  useEffect(() => {
    let cancelled = false;

    async function init() {
      // Try to reuse a real saved token (never reuse "_dev")
      const saved = sessionStorage.getItem(STORAGE_KEY);
      if (saved && saved !== "_dev") {
        try {
          const r = await fetch(`${API_BASE}/session/heartbeat`, {
            method: "POST",
            headers: { "Content-Type": "application/json", "x-session-token": saved },
            body: "{}",
          });
          const d = await r.json();
          if (d.ok && !cancelled) {
            acquiredAt.current = Date.now() - (d.idleMs || 0);
            setSession({ token: saved, dev: false });
            return; // reuse succeeded
          }
        } catch {}
        // Saved token invalid — clear and acquire fresh
        sessionStorage.removeItem(STORAGE_KEY);
      }

      // Acquire a new slot
      try {
        const r = await fetch(`${API_BASE}/session/acquire`, { method: "POST" });
        const d = await r.json();
        if (cancelled) return;

        if (d.code === "POOL_FULL" || (d.error && !d.token)) {
          setPoolFull({ message: d.error || "Server at capacity", queueLength: d.queueLength });
          return;
        }

        if (d.dev) {
          // Server is in single-user dev mode — no session management
          setSession({ token: "_dev", dev: true });
          return;
        }

        // Real managed session
        sessionStorage.setItem(STORAGE_KEY, d.token);
        acquiredAt.current = Date.now();
        ttlMsRef.current   = d.sessionTtlMs || 30 * 60 * 1000;
        setSession(d);
      } catch {
        // Server not reachable — run in offline/dev mode, no modals
        if (!cancelled) setSession({ token: "_dev", dev: true });
      }
    }

    init();
    return () => { cancelled = true; };
  }, []);

  // ── Heartbeat (managed sessions only) ─────────────────────────────────────
  useEffect(() => {
    if (!session || session.dev) return;
    const iv = setInterval(async () => {
      try {
        const r = await fetch(`${API_BASE}/session/heartbeat`, {
          method: "POST",
          headers: { "Content-Type": "application/json", "x-session-token": session.token },
          body: "{}",
        });
        const d = await r.json();
        if (!d.ok) setExpired(true);
      } catch {}
    }, HEARTBEAT_MS);
    return () => clearInterval(iv);
  }, [session]);

  // ── Countdown (managed sessions only) ─────────────────────────────────────
  useEffect(() => {
    if (!session || session.dev || !acquiredAt.current || !ttlMsRef.current) return;
    const iv = setInterval(() => {
      const left = Math.max(0, ttlMsRef.current - (Date.now() - acquiredAt.current));
      setSecsLeft(Math.floor(left / 1000));
      if (left === 0) { setExpired(true); clearInterval(iv); }
    }, 1000);
    return () => clearInterval(iv);
  }, [session]);

  // ── Release on unload (managed sessions only) ──────────────────────────────
  useEffect(() => {
    if (!session || session.dev) return;
    const handler = () => {
      navigator.sendBeacon?.(`${API_BASE}/session/release`, JSON.stringify({ token: session.token }));
      sessionStorage.removeItem(STORAGE_KEY);
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [session]);

  return {
    session,
    poolFull,
    secsLeft,
    expired,
    isLoading:   session === null && !poolFull,
    warningSoon: secsLeft !== null && secsLeft < WARN_SECS,
  };
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
  const [leftTab,    setLeftTab]    = useState("chat");
  const [rightTab,   setRightTab]   = useState("agent");
  const [colLeft,    setColLeft]    = useState(390);   // px
  const [colRight,   setColRight]   = useState(360);   // px
  const [commHeight, setCommHeight] = useState(200);   // px for comm panel
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerTab,  setDrawerTab]  = useState("assets");
  const [drawerH,    setDrawerH]    = useState(200);   // px when open
  const colResizingLeft  = useRef(false);
  const colResizingRight = useRef(false);
  const colResizeStart   = useRef({ x:0, colLeft:390, colRight:360 });
  const drawerResizing   = useRef(false);
  const drawerResizeStart = useRef({ y:0, h:200 });
  const layoutRef        = useRef(null);
  const { session, poolFull, secsLeft, expired, isLoading, warningSoon } = useSession();
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
    { id: "generate", label: "Studio", icon: ICONS.chat },
    { id: "arena", label: "Arena", icon: ICONS.swords },
    { id: "gallery", label: "Gallery", icon: ICONS.frame },
    { id: "skills", label: "Skills", icon: ICONS.tools },
    { id: "tools", label: "Tools", icon: ICONS.wrench },
    { id: "leaderboard", label: "Leaderboard", icon: ICONS.trophy },
  ];
  const SPLIT_PAGES = ["generate"];

  return (
    <PollProvider>
    <div style={{ display:"flex", flexDirection:"column", height:"100vh", background:"var(--bg)", overflow:"hidden", padding:"10px", gap:0, position:"relative" }}>

      {/* ══ POOL FULL — waiting room (only shows when all UE slots are occupied) ══ */}
      {poolFull && (
        <div style={{ position:"fixed", inset:0, background:"var(--bg)", display:"flex", alignItems:"center", justifyContent:"center", zIndex:9999 }}>
          <div style={{ background:"#fff", border:"1px solid var(--line)", borderRadius:16, padding:"40px 48px", textAlign:"center", maxWidth:420, boxShadow:"var(--shadow-pop)" }}>
            <div style={{ fontSize:40, marginBottom:16 }}>⏳</div>
            <div style={{ fontSize:20, fontWeight:800, color:"var(--ink)", marginBottom:8 }}>Server at capacity</div>
            <div style={{ fontSize:13, color:"var(--ink-3)", lineHeight:1.6, marginBottom:24 }}>
              All simulation slots are currently in use.<br/>
              {poolFull.queueLength > 0 && <>Queue length: <strong style={{color:"var(--blue)"}}>{poolFull.queueLength}</strong><br/></>}
              {poolFull.message}
            </div>
            <button onClick={() => window.location.reload()} className="sw-btn-blue" style={{ width:"100%" }}>
              Try again
            </button>
          </div>
        </div>
      )}

      {/* ══ SESSION EXPIRED modal ══ */}
      {expired && (
        <div style={{ position:"fixed", inset:0, background:"rgba(15,23,42,.6)", display:"flex", alignItems:"center", justifyContent:"center", zIndex:9999, backdropFilter:"blur(4px)" }}>
          <div style={{ background:"#fff", border:"1px solid var(--line)", borderRadius:16, padding:"40px 48px", textAlign:"center", maxWidth:380, boxShadow:"var(--shadow-pop)" }}>
            <div style={{ fontSize:40, marginBottom:16 }}>🔒</div>
            <div style={{ fontSize:20, fontWeight:800, color:"var(--ink)", marginBottom:8 }}>Session ended</div>
            <div style={{ fontSize:13, color:"var(--ink-3)", lineHeight:1.6, marginBottom:24 }}>
              Your 30-minute session has expired.<br/>Refresh to start a new session.
            </div>
            <button onClick={() => { sessionStorage.removeItem("sw_session_token"); window.location.reload(); }} className="sw-btn-blue" style={{ width:"100%" }}>
              Start new session
            </button>
          </div>
        </div>
      )}

      {/* ══ TOP NAV BAR — floating card ══ */}
      <header style={{
        height: 50,
        background: "#fff",
        borderRadius: 12,
        border: "1px solid var(--line)",
        boxShadow: "var(--shadow-card)",
        display: "flex",
        alignItems: "center",
        padding: "0 16px",
        gap: 12,
        flexShrink: 0,
        userSelect: "none",
        zIndex: 20,
        marginBottom: 10,
      }}>
        {/* Brand */}
        <div className="sw-brand">
          <div style={{ width:30, height:30, borderRadius:"50%", overflow:"hidden", flexShrink:0, boxShadow:"0 2px 8px rgba(2,6,23,.2)" }}>
            <img src="/simworld-studio-logo.png" style={{ width:"100%", height:"100%", objectFit:"cover", display:"block" }} alt="SimWorld" />
          </div>
          <span className="sw-brand-name">SimWorld Studio</span>
        </div>

        {/* Main nav */}
        <nav className="sw-nav">
          {NAV_ITEMS.map(({ id, label, icon }) => (
            <button
              key={id}
              onClick={() => handleNavClick(id)}
              className={`sw-nav-item${activePage === id ? " active" : ""}`}
            >
              <span style={{ display:"inline-flex", alignItems:"center" }}>{icon(13)}</span>
              <span>{label}</span>
              {((id === "skills" && artifactUnread.skills) || (id === "tools" && artifactUnread.tools)) && (
                <span className="sw-nav-dot" />
              )}
            </button>
          ))}
        </nav>

        {/* Right side */}
        <div style={{ marginLeft:"auto", display:"flex", alignItems:"center", gap:10 }}>

          {/* Status dots */}
          {health && (
            <div style={{ display:"flex", alignItems:"center", gap:12, paddingRight:10, borderRight:"1px solid var(--line-2)" }}>
              <StatusDot label="UE Engine"   active={health.ueConnected}  activeColor="#16a34a" inactiveColor="#dc2626" />
              <StatusDot label="MCP Server"  active={health.mcpConnected} activeColor="#16a34a" inactiveColor="#dc2626" />
              <StatusDot label="Claude Code" active={true}                activeColor="#16a34a" inactiveColor="#64748b" />
            </div>
          )}
          {healthError && <span style={{ fontSize:11, color:"#dc2626", fontWeight:600 }}>Backend unreachable</span>}
          {!health && !healthError && <span style={{ fontSize:11, color:"#64748b" }}>Connecting…</span>}

          {/* Sync error / stale agent warnings */}
          {!syncStatus.sseOk && (
            <div style={{ display:"inline-flex", alignItems:"center", gap:5, padding:"3px 8px", borderRadius:7, background:"#fef2f2", border:"1px solid #fecaca", fontSize:10, fontWeight:600, color:"#dc2626" }}>
              ⚠ {syncStatus.syncError || "SSE disconnected"}
            </div>
          )}
          {syncStatus.staleAgents?.size > 0 && (
            <div title={`Stale: ${[...syncStatus.staleAgents].join(", ")}`}
              style={{ display:"inline-flex", alignItems:"center", gap:5, padding:"3px 8px", borderRadius:7, background:"#fff7ed", border:"1px solid #fed7aa", fontSize:10, fontWeight:600, color:"#ea580c" }}>
              👻 {syncStatus.staleAgents.size} stale agent{syncStatus.staleAgents.size > 1 ? "s" : ""}
            </div>
          )}

          {/* Session countdown — shown when < 5 min remaining */}
          {secsLeft !== null && !session?.dev && (
            <div style={{
              display:"inline-flex", alignItems:"center", gap:5,
              padding:"4px 10px", borderRadius:8,
              background: warningSoon ? "#fef2f2" : "#f0fdf4",
              border: `1px solid ${warningSoon ? "#fecaca" : "#bbf7d0"}`,
              fontSize:11, fontWeight:700,
              color: warningSoon ? "#dc2626" : "#16a34a",
            }}>
              <svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" strokeWidth="2.5">
                <circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>
              </svg>
              {Math.floor(secsLeft/60)}:{String(secsLeft%60).padStart(2,"0")}
            </div>
          )}

          {/* Running pill */}
          <div style={{
            display:"inline-flex", alignItems:"center", gap:7,
            padding:"5px 8px 5px 11px",
            border:"1px solid var(--line)", borderRadius:999,
            background:"#fff", fontSize:12, fontWeight:600,
          }}>
            <span style={{
              width:8, height:8, borderRadius:"50%", background:"#22c55e",
              boxShadow:"0 0 0 3px rgba(34,197,94,.18)",
              animation: health?.ueConnected ? "sw-glow-pulse 2s ease-in-out infinite" : "none",
            }}/>
            <span style={{ color:"var(--ink-2)" }}>
              {health?.ueConnected ? "Running" : "Standby"}
            </span>
            {/* play/pause controls */}
            {[
              <svg key="play" viewBox="0 0 24 24" width="11" height="11" fill="var(--ink-2)"><polygon points="5,3 19,12 5,21"/></svg>,
              <svg key="pause" viewBox="0 0 24 24" width="11" height="11" fill="var(--ink-2)"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>,
            ].map((icon,i) => (
              <span key={i} style={{
                width:24, height:24, borderRadius:"50%", background:"#f1f5f9",
                display:"flex", alignItems:"center", justifyContent:"center", cursor:"pointer",
              }}>{icon}</span>
            ))}
          </div>

          {/* SimCoder pill */}
          <div className="sw-simcoder-pill">
            <img src="/SimCoder.png" style={{ width:20, height:20, objectFit:"contain", borderRadius:5 }} alt="SimCoder" />
            <span>SimCoder</span>
          </div>

          {/* Avatar */}
          <div style={{
            width:32, height:32, borderRadius:"50%",
            background:"linear-gradient(135deg,#e0e7ff,#c7d2fe)",
            display:"flex", alignItems:"center", justifyContent:"center",
            boxShadow:"0 0 0 2px #fff, 0 0 0 3px var(--line)",
            cursor:"pointer",
          }}>
            <svg viewBox="0 0 24 24" fill="none" width="17" height="17">
              <circle cx="12" cy="8" r="4" fill="#6366f1"/>
              <path d="M4 20c0-4 3.6-7 8-7s8 3 8 7" fill="#6366f1"/>
            </svg>
          </div>

          {/* Settings icon */}
          <button style={{
            width:32, height:32, borderRadius:8, border:"none", background:"transparent",
            display:"flex", alignItems:"center", justifyContent:"center",
            color:"var(--ink-2)", cursor:"pointer",
          }}
            onMouseEnter={e=>e.currentTarget.style.background="#f1f5f9"}
            onMouseLeave={e=>e.currentTarget.style.background="transparent"}
          >
            <svg viewBox="0 0 24 24" fill="none" width="17" height="17" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="12" cy="12" r="3"/>
              <path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 11-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 11-4 0v-.09a1.65 1.65 0 00-1-1.51 1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 11-2.83-2.83l.06-.06a1.65 1.65 0 00.33-1.82 1.65 1.65 0 00-1.51-1H3a2 2 0 110-4h.09a1.65 1.65 0 001.51-1 1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 112.83-2.83l.06.06a1.65 1.65 0 001.82.33h0a1.65 1.65 0 001-1.51V3a2 2 0 114 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 112.83 2.83l-.06.06a1.65 1.65 0 00-.33 1.82v0a1.65 1.65 0 001.51 1H21a2 2 0 110 4h-.09a1.65 1.65 0 00-1.51 1z"/>
            </svg>
          </button>

        </div>
      </header>

      <ArtifactToastStack items={artifactToasts} />

      {/* ══ 3-COLUMN RESIZABLE STUDIO LAYOUT ══ */}
      {activePage === "generate" && (
      <div ref={layoutRef} style={{
        flex: 1, display:"flex", overflow:"hidden", minHeight:0, gap:0,
      }}>
        {/* ── LEFT: Coding Agent ── */}
        <div style={{
          width: colLeft, minWidth:260, maxWidth:640, flexShrink:0,
          borderRadius:12, border:"1px solid var(--line)",
          boxShadow:"var(--shadow-card)", display:"flex",
          flexDirection:"column", overflow:"hidden", background:"var(--panel)",
        }}>
          <div className="sw-panel-header">
            <span className="sw-section-title" style={{ color:"var(--orange)" }}>
              <span className="sw-num-chip" style={{ background:"var(--orange)" }}>1</span>
              Coding Agent
            </span>
            <div style={{ flex:1 }} />
          </div>
          {/* Chat only — assets/scenes moved to drawer */}
          <div style={{ flex:1, overflow:"hidden", display:"flex", flexDirection:"column" }}>
            <ChatPanel
              onScreenshotUpdate={url => setLatestScreenshot(url)}
              onRef={setChatRef}
              onSessionChange={setCurrentSessionId}
              onChatDone={() => setContextRefreshKey(k => k + 1)}
            />
          </div>
        </div>

        {/* ── Resize handle left ── */}
        <div className="sw-resize-col" onMouseDown={startColResize("left")} />

        {/* ── CENTER: Viewport + drawer ── */}
        <div style={{ flex:1, minWidth:320, display:"flex", flexDirection:"column", gap:8, overflow:"hidden" }}>

          {/* UE Viewport card */}
          <div style={{
            flex:1, minHeight:120,
            borderRadius:12, border:"1px solid #0b1220",
            boxShadow:"var(--shadow-pop)", overflow:"hidden", background:"#0b1220",
          }}>
            <ViewportPanel latestScreenshot={latestScreenshot} />
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
              <span style={{ fontSize:11, color:"#94a3b8", marginRight:4 }}>
                {drawerOpen ? "▾" : "▸"}
              </span>
              <span style={{ fontSize:12, fontWeight:700, color:"var(--ink-2)" }}>
                {drawerOpen ? drawerTab.charAt(0).toUpperCase()+drawerTab.slice(1) : "Drawer — Assets / Scenes / Context"}
              </span>
              <div style={{ flex:1 }} />
              {drawerOpen && (
                <div style={{ display:"flex", gap:2 }}>
                  {[
                    { id:"assets",  label:"Assets"  },
                    { id:"scenes",  label:"Scenes"  },
                    { id:"context", label:"Context" },
                    { id:"tools",   label:"Tools"   },
                  ].map(t => (
                    <button key={t.id}
                      className={`sw-tab-btn${drawerTab===t.id?" active":""}`}
                      onClick={e => { e.stopPropagation(); setDrawerTab(t.id); }}
                      style={{ fontSize:10, padding:"2px 8px" }}
                    >{t.label}</button>
                  ))}
                  {/* Height adjusters */}
                  {[150,220,320].map(h => (
                    <button key={h} onClick={e=>{ e.stopPropagation(); setDrawerH(h); }}
                      style={{ fontSize:10, padding:"2px 6px", borderRadius:5, border:"1px solid var(--line)",
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
                  <AssetBrowser onInsert={id => chatRef?.insertText(`Use asset: ${id}`)} />
                )}
                {drawerTab === "scenes" && (
                  <SceneManager onLoadScene={scene => chatRef?.loadScene(scene)} currentSessionId={currentSessionId} />
                )}
                {drawerTab === "context" && (
                  <ContextPanel sessionId={currentSessionId} refreshKey={contextRefreshKey} />
                )}
                {drawerTab === "tools" && (
                  <ToolsPage newlyAddedToolIds={artifactNewIds.tools} onMarkToolSeen={markToolArtifactSeen} />
                )}
              </div>
            )}
          </div>
        </div>

        {/* ── Resize handle right ── */}
        <div className="sw-resize-col" onMouseDown={startColResize("right")} />

        {/* ── RIGHT: Embodied Agent (vertical split) ── */}
        <div style={{
          width: colRight, minWidth:240, maxWidth:560, flexShrink:0,
          borderRadius:12, border:"1px solid var(--line)",
          boxShadow:"var(--shadow-card)", display:"flex",
          flexDirection:"column", overflow:"hidden", background:"var(--panel)",
        }}>
          {/* Panel header */}
          <div className="sw-panel-header" style={{ borderRadius:"12px 12px 0 0" }}>
            <span className="sw-section-title" style={{ color:"var(--blue)" }}>
              <span className="sw-num-chip" style={{ background:"var(--blue)" }}>2</span>
              Embodied Agent
            </span>
            <div style={{ flex:1 }} />
            <button
              className={`sw-tab-btn${rightTab==="agent"?" active":""}`}
              onClick={() => setRightTab("agent")} style={{ fontSize:10, padding:"3px 8px" }}
            >Agents</button>
          </div>

          {/* Full vertical split — agents + resize + comm */}
          <div style={{ flex:1, overflow:"hidden" }}>
            <AgentPanel
              sessionId={currentSessionId}
              commHeight={commHeight}
              onCommHeightChange={setCommHeight}
            />
          </div>
        </div>

      </div>
      )}

      {/* ══ FULL-PAGE CONTENT (non-generate pages) — floating card ══ */}
      <div style={{
        flex: activePage !== "generate" ? 1 : 0,
        overflow: "hidden",
        display: activePage !== "generate" ? "block" : "none",
        borderRadius: 12,
        border: "1px solid var(--line)",
        boxShadow: "var(--shadow-card)",
        background: "var(--panel)",
        minHeight: 0,
      }}>
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
        {activePage === "gallery"     && <GalleryPage />}
      </div>

    </div>
    </PollProvider>
  );
}

export default App;

import React, { useEffect, useMemo, useRef, useState } from "react";
import { API_BASE } from "../api/client.js";

const AgentsContext = React.createContext({ agents: [], sessions: [], activities: {} });
const SceneContext = React.createContext({ objects: [], environment: { ready: false }, round: 0 });
const ChatLogContext = React.createContext([]);
const StatusContext = React.createContext({ pieActive: false, health: null });
const MetricsContext = React.createContext({ series: {}, sceneCollisions: [], sampledAt: 0, intervalMs: 5000 });
const SyncContext = React.createContext({ staleAgents: new Set(), syncError: null, sseOk: true });

const PollContext = React.createContext({
  context: { agents: [], objects: [], environment: { ready: false }, round: 0 },
  sessions: [],
  activities: {},
  chatLog: [],
  pieActive: false,
  health: null,
});

const STALE_AGENT_MS = 20_000;

export function PollProvider({ children }) {
  const [agents, setAgents] = useState({ agents: [], sessions: [], activities: {} });
  const [scene, setScene] = useState({ objects: [], environment: { ready: false }, round: 0 });
  const [chatLog, setChatLog] = useState([]);
  const [status, setStatus] = useState({ pieActive: false, health: null });
  const [sync, setSync] = useState({ staleAgents: new Set(), syncError: null, sseOk: true });
  const [metrics, setMetrics] = useState({ series: {}, sceneCollisions: [], sampledAt: 0, intervalMs: 5000 });

  const agentLastSeen = useRef(new Map());
  const agentMissCnt = useRef(new Map());
  const legacyRef = useRef({
    context: { agents: [], objects: [], environment: { ready: false }, round: 0 },
    sessions: [],
    activities: {},
    chatLog: [],
    pieActive: false,
    health: null,
  });

  useEffect(() => {
    const token = sessionStorage.getItem("sw_session_token") || "";
    const url = token ? `${API_BASE}/events?token=${token}` : `${API_BASE}/events`;
    let es = new EventSource(url);
    let reconnectTimer = null;

    const reconnect = () => {
      es.close();
      reconnectTimer = setTimeout(() => {
        es = new EventSource(url);
        es.onmessage = onMessage;
        es.onerror = onError;
      }, 3000);
    };

    function onMessage(evt) {
      try {
        const d = JSON.parse(evt.data);
        setSync((prev) => (prev.sseOk ? prev : { ...prev, sseOk: true, syncError: null }));

        const now = Date.now();
        const liveNames = new Set();
        (d.sessions || d.context?.agents || []).forEach((a) => {
          const name = a.agentName || a.name;
          if (name) {
            agentLastSeen.current.set(name, now);
            agentMissCnt.current.delete(name);
            liveNames.add(name);
          }
        });

        const stale = new Set();
        for (const [name, ts] of agentLastSeen.current) {
          if (!liveNames.has(name)) {
            const misses = (agentMissCnt.current.get(name) || 0) + 1;
            agentMissCnt.current.set(name, misses);
            if (misses >= 3 && now - ts > STALE_AGENT_MS) stale.add(name);
            if (now - ts > 300_000) {
              agentLastSeen.current.delete(name);
              agentMissCnt.current.delete(name);
            }
          }
        }
        setSync((prev) => {
          const same = prev.staleAgents.size === stale.size && [...stale].every((name) => prev.staleAgents.has(name));
          return same ? prev : { ...prev, staleAgents: stale };
        });

        const nextSessions = d.sessions || [];
        const nextAgentList = d.context?.agents || [];
        const nextActivities = d.activities || {};
        setAgents((prev) => {
          const prevSess = prev.sessions || [];
          const sameCount = prevSess.length === nextSessions.length;
          const sessKey = (s) =>
            `${s.agentName}:${s.status}:${s.collisionCount}:${Math.round((s.location?.[0] || 0) / 10)}:${s.currentAction || ""}`;
          const sameKey = sameCount && nextSessions.every((s, i) => sessKey(s) === sessKey(prevSess[i]));
          const actKey = (acts) =>
            Object.entries(acts || {})
              .map(([k, v]) => `${k}:${(v || []).length}:${(v || [])[v?.length - 1]?.timestamp || 0}`)
              .join("|");
          const sameActs = actKey(nextActivities) === actKey(prev.activities);
          if (sameKey && sameActs && (prev.agents || []).length === nextAgentList.length) return prev;
          return { agents: nextAgentList, sessions: nextSessions, activities: nextActivities };
        });

        const nextEnv = d.context?.environment || { ready: false };
        const nextObjs = d.context?.objects || [];
        const nextRound = d.context?.round || 0;
        setScene((prev) => {
          if (
            prev.objects.length === nextObjs.length &&
            prev.environment?.ready === nextEnv.ready &&
            prev.round === nextRound
          ) {
            return prev;
          }
          return { objects: nextObjs, environment: nextEnv, round: nextRound };
        });

        if (Array.isArray(d.chatLog) && d.chatLog.length > 0) {
          setChatLog((prev) => {
            const msgKey = (m) => `${m.from}|${m.timestamp}|${(m.text || "").slice(0, 20)}`;
            const existing = new Set(prev.map(msgKey));
            const news = d.chatLog.filter((m) => !existing.has(msgKey(m)));
            return news.length > 0 ? [...prev, ...news].slice(-200) : prev;
          });
        }

        const nextHealth = d.health || null;
        const nextPie = !!d.pieActive;
        setStatus((prev) => {
          if (
            prev.pieActive === nextPie &&
            prev.health?.ueConnected === nextHealth?.ueConnected &&
            prev.health?.mcpConnected === nextHealth?.mcpConnected
          ) {
            return prev;
          }
          return { pieActive: nextPie, health: nextHealth };
        });

        if (d.metrics && d.metrics.sampledAt !== undefined) {
          setMetrics((prev) => (prev.sampledAt === d.metrics.sampledAt ? prev : d.metrics));
        }

        legacyRef.current = d;
      } catch (e) {
        setSync((prev) => ({ ...prev, syncError: "SSE parse error: " + e.message }));
      }
    }

    function onError() {
      setSync((prev) => ({ ...prev, sseOk: false, syncError: "SSE connection lost - reconnecting..." }));
      reconnect();
    }

    es.onmessage = onMessage;
    es.onerror = onError;
    return () => {
      es.close();
      if (reconnectTimer) clearTimeout(reconnectTimer);
    };
  }, []);

  const legacyValue = useMemo(
    () => ({
      get context() {
        return legacyRef.current.context || {};
      },
      get sessions() {
        return legacyRef.current.sessions || [];
      },
      get activities() {
        return legacyRef.current.activities || {};
      },
      get chatLog() {
        return legacyRef.current.chatLog || [];
      },
      get pieActive() {
        return legacyRef.current.pieActive || false;
      },
      get health() {
        return legacyRef.current.health || null;
      },
    }),
    []
  );

  return (
    <AgentsContext.Provider value={agents}>
      <SceneContext.Provider value={scene}>
        <ChatLogContext.Provider value={chatLog}>
          <StatusContext.Provider value={status}>
            <MetricsContext.Provider value={metrics}>
              <SyncContext.Provider value={sync}>
                <PollContext.Provider value={legacyValue}>{children}</PollContext.Provider>
              </SyncContext.Provider>
            </MetricsContext.Provider>
          </StatusContext.Provider>
        </ChatLogContext.Provider>
      </SceneContext.Provider>
    </AgentsContext.Provider>
  );
}

export function useAgents() {
  return React.useContext(AgentsContext);
}

export function useScene() {
  return React.useContext(SceneContext);
}

export function useChatLog() {
  return React.useContext(ChatLogContext);
}

export function useStatus() {
  return React.useContext(StatusContext);
}

export function useSync() {
  return React.useContext(SyncContext);
}

export function useMetrics() {
  return React.useContext(MetricsContext);
}

export function usePoll() {
  return React.useContext(PollContext);
}

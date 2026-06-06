import React, { useEffect, useRef, useState } from "react";
import {
  broadcastAgentMessage,
  fetchPieStatus,
  postChatCommand,
  startPie,
  stopAllAgents,
  trackAgent,
} from "../../api/appApi.js";
import { useMetrics, usePoll } from "../../state/pollContext.jsx";

const DEFAULT_AGENT_COLORS = [
  "var(--blue)",
  "var(--green)",
  "var(--orange)",
  "var(--violet)",
  "var(--blue-2)",
  "var(--green-deep)",
];

export default function AgentAggregatePanelTabs({ agentColors = DEFAULT_AGENT_COLORS, agents, icons = {}, sessionId }) {
  const [tab, setTab] = useState("overview");
  const [trackName, setTrackName] = useState("");
  const [trackMsg, setTrackMsg] = useState("");
  const trackTimerRef = useRef(null);

  useEffect(() => () => {
    if (trackTimerRef.current) clearTimeout(trackTimerRef.current);
  }, []);

  const handleTrack = async () => {
    const name = trackName.trim();
    if (!name) return;
    await trackAgent(name);
    setTrackName("");
    setTrackMsg(`Tracking: ${name}`);
    if (trackTimerRef.current) clearTimeout(trackTimerRef.current);
    trackTimerRef.current = setTimeout(() => setTrackMsg(""), 3000);
  };

  const tabs = [
    { id: "overview", label: "Overview" },
    { id: "testbed", label: "Testbed" },
    { id: "chat", label: "Comm" },
  ];

  return (
    <div className="agent-aggregate">
      <div className="agent-trackbar">
        <input
          value={trackName}
          onChange={(event) => setTrackName(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") handleTrack();
          }}
          placeholder="Track agent by name..."
        />
        <button onClick={handleTrack}>Track</button>
        {trackMsg && <span>{trackMsg}</span>}
      </div>

      <div className="agent-aggregate-tabs">
        {tabs.map((item) => (
          <button
            key={item.id}
            className={`agent-aggregate-tab${tab === item.id ? " active" : ""}`}
            onClick={() => setTab(item.id)}
          >
            {item.label}
          </button>
        ))}
      </div>

      <div className="agent-aggregate-body">
        {tab === "overview" && <AgentOverviewPanel agentColors={agentColors} icons={icons} />}
        {tab === "testbed" && <MultiAgentTestbed sessionId={sessionId} />}
        {tab === "chat" && <CommHistory agentColors={agentColors} agents={agents} />}
      </div>
    </div>
  );
}

function MultiLineChart({ agentColors, seriesMap, label, unit = "", W = 440, H = 90 }) {
  const entries = Object.entries(seriesMap).filter(([, series]) => series && series.length >= 2);
  if (entries.length === 0) {
    return (
      <div className="agent-chart-empty" style={{ width: W, height: H }}>
        {label}: no data yet
      </div>
    );
  }

  const pad = { t: 8, r: 6, b: 22, l: 36 };
  const innerWidth = W - pad.l - pad.r;
  const innerHeight = H - pad.t - pad.b;
  const allValues = entries.flatMap(([, series]) => series);
  const minValue = Math.min(...allValues);
  const maxValue = Math.max(...allValues);
  const valueRange = maxValue - minValue || 1;
  const toX = (index, length) => pad.l + (index / (Math.max(length, 2) - 1)) * innerWidth;
  const toY = (value) => pad.t + innerHeight - ((value - minValue) / valueRange) * innerHeight;
  const ticks = [minValue, maxValue].map((value) => Math.round(value * 10) / 10);

  return (
    <svg width={W} height={H} className="agent-line-chart">
      {ticks.map((value, index) => {
        const y = toY(value);
        return (
          <g key={index}>
            <line
              x1={pad.l}
              y1={y}
              x2={pad.l + innerWidth}
              y2={y}
              stroke="var(--line)"
              strokeWidth={0.5}
              strokeDasharray="3,3"
            />
            <text x={pad.l - 4} y={y + 4} textAnchor="end" fontSize={10} fill="var(--ink-3)">
              {value}{unit}
            </text>
          </g>
        );
      })}
      {entries.map(([name, series], index) => {
        const color = agentColors[index % agentColors.length];
        const d = series
          .map((value, pointIndex) => (
            `${pointIndex === 0 ? "M" : "L"}${toX(pointIndex, series.length).toFixed(1)},${toY(value).toFixed(1)}`
          ))
          .join(" ");
        return (
          <g key={name}>
            <path d={d} fill="none" stroke={color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" opacity={0.9} />
            <circle cx={toX(series.length - 1, series.length)} cy={toY(series[series.length - 1])} r={3} fill={color} />
            <text
              x={toX(series.length - 1, series.length) + 5}
              y={toY(series[series.length - 1]) + 4}
              fontSize={9}
              fill={color}
              fontWeight="bold"
            >
              {name}
            </text>
          </g>
        );
      })}
      <text x={pad.l + innerWidth / 2} y={H - 4} textAnchor="middle" fontSize={10} fill="var(--ink-3)" fontWeight={600}>
        {label}
      </text>
    </svg>
  );
}

function AgentOverviewPanel({ agentColors, icons }) {
  const pollData = usePoll();
  const sessions = pollData.sessions || [];
  const metrics = useMetrics();
  const { series } = metrics;

  if (sessions.length === 0) {
    return (
      <div className="agent-overview-empty">
        <div>{icons.robot ? icons.robot(36) : null}</div>
        No agents in scene
      </div>
    );
  }

  const totalCollisions = sessions.reduce((sum, agent) => sum + (agent.collisionCount || 0), 0);
  const totalTurns = sessions.reduce((sum, agent) => sum + (agent.totalTurns || 0), 0);
  const running = sessions.filter((session) => session.status === "running").length;

  const collisionSeries = {};
  const speedSeries = {};
  for (const [name, agentSeries] of Object.entries(series)) {
    if (agentSeries.collision?.length > 1) collisionSeries[name] = agentSeries.collision;
    if (agentSeries.speed?.length > 1) speedSeries[name] = agentSeries.speed;
  }

  return (
    <div className="agent-overview">
      <div className="agent-stat-grid">
        <AgentStat label="Agents" value={sessions.length} tone="blue" />
        <AgentStat label="Running" value={running} tone="orange" />
        <AgentStat label="Collisions" value={totalCollisions} tone={totalCollisions > 0 ? "red" : "green"} />
        <AgentStat label="Total Turns" value={totalTurns} tone="muted" />
      </div>

      <div className="agent-chart-stack">
        <MultiLineChart agentColors={agentColors} seriesMap={collisionSeries} label="Collisions over time" W={440} H={85} />
        <MultiLineChart agentColors={agentColors} seriesMap={speedSeries} label="Speed (m/s) over time" unit="m/s" W={440} H={85} />
      </div>

      {Object.keys(series).length === 0 && (
        <div className="agent-chart-hint">Charts will appear after agents start moving (sampled every 5s)</div>
      )}
    </div>
  );
}

function AgentStat({ label, tone, value }) {
  return (
    <div className="agent-stat-card">
      <div className={`agent-stat-value ${tone}`}>{value}</div>
      <div className="agent-stat-label">{label}</div>
    </div>
  );
}

function MultiAgentTestbed({ sessionId }) {
  const [running, setRunning] = useState(false);
  const [log, setLog] = useState([]);
  const [count, setCount] = useState(3);
  const [goal, setGoal] = useState("Explore the area and report obstacles you encounter");

  const addLog = (message) => setLog((prev) => [...prev.slice(-30), `${new Date().toLocaleTimeString()} ${message}`]);

  const spawnAndRun = async () => {
    setRunning(true);
    setLog([]);
    try {
      addLog("Starting PIE mode...");
      const pieStatus = await fetchPieStatus();
      if (!pieStatus.active) {
        await startPie();
        addLog("PIE start requested - waiting up to 30s...");
        let pieReady = false;
        for (let index = 0; index < 30; index += 1) {
          await new Promise((resolve) => setTimeout(resolve, 1000));
          const check = await fetchPieStatus().catch(() => ({ active: false }));
          if (check.active) {
            pieReady = true;
            break;
          }
        }
        addLog(pieReady ? "PIE active" : "PIE did not start - agents may not work correctly");
      } else {
        addLog("PIE already active");
      }

      addLog(`Spawning ${count} agents...`);
      const positions = Array.from({ length: count }, (_, index) => {
        const angle = (2 * Math.PI * index) / count;
        const radius = 1500;
        return [Math.round(radius * Math.cos(angle)), Math.round(radius * Math.sin(angle)), 110];
      });
      const spawnMessage = positions
        .map((position, index) => (
          `spawn_agent(agent_name="TestAgent_${index + 1}", agent_type="pedestrian", location=[${position.join(",")}])`
        ))
        .join("\n");
      await postChatCommand(spawnMessage, sessionId);
      addLog(`Spawned ${count} agents`);

      for (let index = 1; index <= count; index += 1) {
        addLog(`Sending goal to TestAgent_${index}...`);
        await broadcastAgentMessage(goal, `TestAgent_${index}`);
        await new Promise((resolve) => setTimeout(resolve, 400));
      }
      addLog("All agents received goals. Testbed running.");
    } catch (error) {
      addLog(`Error: ${error.message}`);
    } finally {
      setRunning(false);
    }
  };

  const stopAgents = async () => {
    setRunning(false);
    await stopAllAgents();
    addLog("Stopped all agents");
  };

  return (
    <div className="agent-testbed">
      <div className="agent-testbed-title">Multi-Agent Testbed</div>
      <div className="agent-testbed-row">
        <label>Agents:</label>
        <select value={count} onChange={(event) => setCount(Number(event.target.value))}>
          {[2, 3, 4, 5].map((value) => <option key={value} value={value}>{value}</option>)}
        </select>
      </div>
      <textarea
        value={goal}
        onChange={(event) => setGoal(event.target.value)}
        rows={2}
        placeholder="Navigation goal for all agents..."
      />
      <div className="agent-testbed-actions">
        <button className="agent-testbed-run" onClick={spawnAndRun} disabled={running}>
          {running ? "Running..." : "Spawn & Run"}
        </button>
        <button className="agent-testbed-stop" onClick={stopAgents}>
          Stop
        </button>
      </div>
      <div className="agent-testbed-log">
        {log.map((entry, index) => <div key={`${entry}-${index}`}>{entry}</div>)}
        {log.length === 0 && <div className="agent-testbed-log-empty">Log will appear here...</div>}
      </div>
    </div>
  );
}

function CommHistory({ agentColors, agents }) {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [target, setTarget] = useState("all");
  const scrollRef = useRef(null);
  const pollData = usePoll();

  useEffect(() => {
    const data = pollData.chatLog || [];
    if (data.length <= 0) return;
    setMessages((prev) => {
      const existing = new Set(prev.map((message) => `${message.from}-${message.timestamp}`));
      const newMessages = data.filter((message) => !existing.has(`${message.from}-${message.timestamp}`));
      if (!newMessages.length) return prev;
      return [...prev, ...newMessages].slice(-100);
    });
  }, [pollData.chatLog]);

  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [messages]);

  const handleSend = () => {
    const text = input.trim();
    if (!text) return;
    broadcastAgentMessage(text, target === "all" ? "all" : target).catch(() => {});
    setMessages((prev) => [...prev, { from: "user", to: target, text, timestamp: Date.now() }]);
    setInput("");
  };

  const colors = { user: "var(--ink)" };
  (agents || []).forEach((agent, index) => {
    colors[agent.name] = agentColors[index % agentColors.length];
  });

  const renderText = (text) => {
    const parts = text.split(/(@\w+)/g);
    return parts.map((part, index) => {
      if (!part.startsWith("@")) return part;
      const name = part.slice(1);
      return <span key={`${part}-${index}`} style={{ color: colors[name] || "var(--blue)", fontWeight: 600 }}>{part}</span>;
    });
  };

  return (
    <div className="agent-comm">
      <div className="agent-comm-header">Communication</div>
      <div ref={scrollRef} className="agent-comm-messages">
        {messages.length === 0 ? (
          <div className="agent-comm-empty">Messages between you and agents will appear here.</div>
        ) : messages.map((message, index) => (
          <div key={`${message.timestamp}-${index}`} className="agent-comm-message">
            <div className="agent-comm-meta">
              <span className="agent-comm-from" style={{ color: colors[message.from] || "var(--ink-3)" }}>
                {message.from === "user" ? "You" : message.from}
              </span>
              {message.to && message.to !== "all" && (
                <span className="agent-comm-to">
                  to <span style={{ color: colors[message.to] || "var(--blue)" }}>@{message.to}</span>
                </span>
              )}
              <span className="agent-comm-time">
                {new Date(message.timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
              </span>
            </div>
            <div className="agent-comm-text">{renderText(message.text)}</div>
          </div>
        ))}
      </div>
      <div className="agent-comm-compose">
        <select value={target} onChange={(event) => setTarget(event.target.value)}>
          <option value="all">@all</option>
          {(agents || []).map((agent) => <option key={agent.name} value={agent.name}>@{agent.name}</option>)}
        </select>
        <input
          value={input}
          onChange={(event) => setInput(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              handleSend();
            }
          }}
          placeholder="Message..."
        />
        <button onClick={handleSend} disabled={!input.trim()}>
          Send
        </button>
      </div>
    </div>
  );
}

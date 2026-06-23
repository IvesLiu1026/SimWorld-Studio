import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  broadcastAgentMessage,
  fetchAgentCamera,
  fetchAgentTrajectory,
  refreshAgentContext,
} from "../../api/appApi.js";
import { useAgents, usePoll, useStatus } from "../../state/pollContext.jsx";
import AgentAggregatePanelTabs from "./AgentAggregatePanelTabs.jsx";

const DEFAULT_AGENT_COLORS = [
  "var(--blue)",
  "var(--green)",
  "var(--orange)",
  "var(--violet)",
  "var(--blue-2)",
  "var(--green-deep)",
];

const STATUS_TONE = {
  idle: "idle",
  running: "running",
  done: "done",
  error: "error",
};

function agentColor(agentColors, index) {
  const palette = agentColors?.length ? agentColors : DEFAULT_AGENT_COLORS;
  return palette[Math.max(0, index) % palette.length];
}

function renderIcon(icons, name, size) {
  const icon = icons?.[name];
  return icon ? icon(size) : null;
}

function statusTone(status) {
  return STATUS_TONE[status] || STATUS_TONE.idle;
}

function formatVector(values, separator = ", ") {
  if (!Array.isArray(values) || values.length < 3) return null;
  return values.slice(0, 3).map((value) => Math.round(value)).join(separator);
}

function normalizeYaw(yaw) {
  return ((yaw % 360) + 360) % 360;
}

function yawToCompass(yaw) {
  const dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
  return dirs[Math.round(normalizeYaw(yaw) / 45) % 8];
}

function headingFromRotation(rotation) {
  if (!Array.isArray(rotation) || rotation.length < 3) return null;
  const yaw = normalizeYaw(rotation[1]);
  return `${yawToCompass(yaw)} ${Math.round(yaw)} deg`;
}

function truncate(value, length) {
  if (!value) return "";
  return value.length > length ? `${value.slice(0, length)}...` : value;
}

function AgentCard({ agent, agentColors, colorIdx, onExpand }) {
  const status = agent.status || "idle";
  const color = agentColor(agentColors, colorIdx);
  const pollData = usePoll();
  const pastActivities = pollData.activities?.[agent.name] || [];

  const location = formatVector(agent.location);
  const heading = headingFromRotation(agent.rotation);
  const liveAction = status === "running"
    ? agent.currentAction
    : (agent.lastAction || null);

  return (
    <div
      className="agent-monitor-card"
      onClick={() => onExpand?.(agent)}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onExpand?.(agent);
        }
      }}
      role="button"
      tabIndex={0}
      style={{ "--agent-color": color }}
    >
      <div className="agent-monitor-card-header">
        <span className={`agent-monitor-status ${statusTone(status)}`} />
        <span className="agent-monitor-name">{agent.name}</span>
        <span className="agent-monitor-class">{agent.cls || "?"}</span>
        <span className="agent-monitor-chevron">&gt;</span>
      </div>

      <div className="agent-monitor-card-body">
        <div className="agent-monitor-info-row">
          {location ? (
            <span className="agent-monitor-mono agent-monitor-truncate">{location}</span>
          ) : (
            <span className="agent-monitor-muted agent-monitor-italic">location unknown</span>
          )}
          {heading && <span className="agent-monitor-mono agent-monitor-heading">{heading}</span>}
        </div>

        {liveAction && (
          <div className="agent-monitor-action-row">
            {status === "running" && <span className="agent-monitor-action-pulse" />}
            <span className="agent-monitor-truncate">
              {liveAction}
            </span>
          </div>
        )}

        {pastActivities.length > 0 && (
          <div className="agent-monitor-past">
            {pastActivities.length} past action{pastActivities.length > 1 ? "s" : ""}
          </div>
        )}
      </div>
    </div>
  );
}

function AgentTrajectoryView({ agentName, color, icons, liveState }) {
  const [fullTrajectory, setFullTrajectory] = useState(null);
  const [loadingFull, setLoadingFull] = useState(false);

  const loadFull = useCallback(() => {
    setLoadingFull(true);
    fetchAgentTrajectory(agentName)
      .then(setFullTrajectory)
      .catch(() => {})
      .finally(() => setLoadingFull(false));
  }, [agentName]);

  const trajectory = (fullTrajectory ?? (liveState?.trajectoryPreview || []))
    .filter((point) => Array.isArray(point?.loc) && point.loc.length >= 2);
  const collisions = liveState?.recentCollisions || [];
  const envEvents = liveState?.envFeedback || [];

  if (trajectory.length < 2) {
    return (
      <div className="agent-trajectory-empty">
        <div>{renderIcon(icons, "map", 28)}</div>
        <div>No trajectory data yet - agent needs to move</div>
      </div>
    );
  }

  const xs = trajectory.map((point) => point.loc[0]);
  const ys = trajectory.map((point) => point.loc[1]);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  const width = 420;
  const height = 280;
  const pad = 24;
  const rangeX = maxX - minX || 1;
  const rangeY = maxY - minY || 1;
  const scale = Math.min((width - 2 * pad) / rangeX, (height - 2 * pad) / rangeY);
  const toSvg = (x, y) => [pad + (x - minX) * scale, height - pad - (y - minY) * scale];

  const pathD = trajectory.map((point, index) => {
    const [sx, sy] = toSvg(point.loc[0], point.loc[1]);
    return `${index === 0 ? "M" : "L"} ${sx.toFixed(1)} ${sy.toFixed(1)}`;
  }).join(" ");

  const collisionPoints = [
    ...collisions.map((collision) => (
      Array.isArray(collision.loc) ? toSvg(collision.loc[0], collision.loc[1]) : null
    )),
    ...trajectory.filter((point) => point.hit).map((point) => toSvg(point.loc[0], point.loc[1])),
  ].filter(Boolean);

  const last = trajectory[trajectory.length - 1];
  const [lastX, lastY] = toSvg(last.loc[0], last.loc[1]);
  const yaw = Array.isArray(last.rot) ? normalizeYaw(last.rot[1]) : 0;
  const arrowRad = (yaw * Math.PI) / 180;
  const isPreview = !fullTrajectory;
  const totalPoints = liveState?.trajectoryLength ?? trajectory.length;

  return (
    <div className="agent-trajectory">
      <div className="agent-trajectory-header">
        <span>
          {isPreview ? `Preview (last ${trajectory.length})` : `Full (${trajectory.length} pts)`}
          {" "}of {totalPoints} total
        </span>
        {isPreview && totalPoints > trajectory.length && (
          <button onClick={loadFull} disabled={loadingFull} type="button">
            {loadingFull ? "Loading..." : `Load all ${totalPoints}`}
          </button>
        )}
      </div>

      <svg width={width} height={height} className="agent-trajectory-map">
        {[0.25, 0.5, 0.75].map((fraction) => (
          <line
            key={fraction}
            x1={pad}
            y1={height - pad - fraction * (height - 2 * pad)}
            x2={width - pad}
            y2={height - pad - fraction * (height - 2 * pad)}
            stroke="var(--line)"
            strokeWidth={0.5}
            strokeDasharray="3,3"
          />
        ))}
        <path d={pathD} fill="none" stroke={color} strokeWidth={2} strokeLinejoin="round" opacity={0.85} />
        {(() => {
          const [sx, sy] = toSvg(trajectory[0].loc[0], trajectory[0].loc[1]);
          return <circle cx={sx} cy={sy} r={5} fill="var(--green)" stroke="var(--panel)" strokeWidth={1.5} />;
        })()}
        {collisionPoints.map(([cx, cy], index) => (
          <circle key={index} cx={cx} cy={cy} r={6} fill="var(--red)" opacity={0.72} />
        ))}
        <circle cx={lastX} cy={lastY} r={7} fill={color} stroke="var(--panel)" strokeWidth={2} />
        <line
          x1={lastX}
          y1={lastY}
          x2={lastX + Math.cos(arrowRad - Math.PI / 2) * 14}
          y2={lastY + Math.sin(arrowRad - Math.PI / 2) * 14}
          stroke="var(--panel)"
          strokeWidth={2}
          strokeLinecap="round"
        />
        <circle cx={12} cy={height - 10} r={4} fill="var(--green)" />
        <text x={20} y={height - 7} fontSize={8} fill="var(--ink-3)">Start</text>
        <circle cx={55} cy={height - 10} r={4} fill={color} />
        <text x={63} y={height - 7} fontSize={8} fill="var(--ink-3)">Current</text>
        {collisionPoints.length > 0 && (
          <>
            <circle cx={105} cy={height - 10} r={4} fill="var(--red)" />
            <text x={113} y={height - 7} fontSize={8} fill="var(--ink-3)">
              Collision ({collisionPoints.length})
            </text>
          </>
        )}
      </svg>

      <div className="agent-trajectory-stats">
        <TrajectoryStat label="Track Points" value={trajectory.length} color={color} />
        <TrajectoryStat
          label="Total Collisions"
          value={liveState?.collisionCount || 0}
          color={(liveState?.collisionCount || 0) > 0 ? "var(--red)" : color}
        />
        <TrajectoryStat label="Turns" value={liveState?.totalTurns || 0} color={color} />
        <TrajectoryStat label="m/s" value={Math.round((liveState?.speed || 0) / 100)} color={color} />
      </div>

      {collisions.length > 0 && (
        <div className="agent-trajectory-section">
          <div className="agent-trajectory-section-title red">Recent Collisions</div>
          {collisions.slice(-5).map((collision, index) => (
            <div key={index} className="agent-collision-row">
              {new Date(collision.ts || Date.now()).toLocaleTimeString()} - hit:{" "}
              {collision.overlapping?.join(", ") || "unknown"}
              {collision.impulse > 0 && (
                <span> impulse {Math.round(collision.impulse)} N</span>
              )}
            </div>
          ))}
        </div>
      )}

      {envEvents.length > 0 && (
        <div className="agent-trajectory-section">
          <div className="agent-trajectory-section-title">Nearby Environment</div>
          {envEvents.slice(-3).map((event, index) => (
            <div key={index} className="agent-env-row">
              {new Date(event.ts || Date.now()).toLocaleTimeString()} - {event.nearby?.length || 0}
              {" "}objects within 3m: {event.nearby?.slice(0, 3).map((item) => item.name).join(", ")}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function TrajectoryStat({ color, label, value }) {
  return (
    <div className="agent-trajectory-stat">
      <div style={{ color }}>{value}</div>
      <span>{label}</span>
    </div>
  );
}

function AgentDetailPanel({ agent, agentColors, colorIdx, icons, onClose }) {
  const color = agentColor(agentColors, colorIdx);
  const [activeTab, setActiveTab] = useState("camera");
  const [cameraImage, setCameraImage] = useState(null);
  const [cameraLoading, setCameraLoading] = useState(false);
  const [cameraError, setCameraError] = useState(null);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [windowPosition, setWindowPosition] = useState(() => ({
    x: Math.max(20, window.innerWidth - 540),
    y: 80,
  }));
  const cameraIntervalRef = useRef(null);
  const activityRef = useRef(null);
  const pollData = usePoll();
  const pastActivities = pollData.activities?.[agent.name] || [];

  const liveState = useMemo(() => (
    (pollData.sessions || []).find((session) => session.agentName === agent.name) || null
  ), [pollData.sessions, agent.name]);

  const agentMessages = useMemo(() => (
    (pollData.chatLog || []).filter((message) => (
      message.from !== agent.name && (message.to === agent.name || message.to === "all" || !message.to)
    )).slice(-20)
  ), [pollData.chatLog, agent.name]);

  const startDrag = useCallback((event) => {
    event.preventDefault();
    const startX = event.clientX - windowPosition.x;
    const startY = event.clientY - windowPosition.y;
    const onMove = (moveEvent) => {
      setWindowPosition({ x: moveEvent.clientX - startX, y: moveEvent.clientY - startY });
    };
    const onUp = () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  }, [windowPosition]);

  const isStillSyncing = !liveState;
  const location = liveState?.location ?? (Array.isArray(agent.location) ? agent.location : null);
  const rotation = liveState?.rotation ?? agent.rotation ?? null;
  const liveStatus = liveState?.status ?? agent.status ?? "idle";
  const currentAction = liveState?.currentAction ?? null;
  const lastAction = liveState?.lastAction ?? null;

  const focusAndShoot = useCallback(async () => {
    setCameraLoading(true);
    setCameraError(null);
    try {
      const data = await fetchAgentCamera(agent.name);
      if (data.dataUrl) {
        setCameraImage(data.dataUrl);
      } else {
        throw new Error("No camera data available");
      }
    } catch (error) {
      setCameraError(error.message);
    } finally {
      setCameraLoading(false);
    }
  }, [agent.name]);

  useEffect(() => {
    if (activeTab !== "camera" || !autoRefresh) {
      clearInterval(cameraIntervalRef.current);
      return undefined;
    }
    focusAndShoot();
    cameraIntervalRef.current = setInterval(focusAndShoot, 4000);
    return () => clearInterval(cameraIntervalRef.current);
  }, [activeTab, autoRefresh, focusAndShoot]);

  useEffect(() => {
    if (activityRef.current) {
      activityRef.current.scrollTop = activityRef.current.scrollHeight;
    }
  }, [pastActivities, agentMessages]);

  const handleSend = async () => {
    const text = input.trim();
    if (!text || sending) return;
    setInput("");
    setSending(true);
    try {
      await broadcastAgentMessage(text, agent.name);
    } catch {}
    setSending(false);
  };

  const tabs = [
    { id: "camera", label: "Camera" },
    { id: "trajectory", label: "Trajectory" },
    { id: "activity", label: "Activity" },
    { id: "chat", label: "Chat" },
  ];

  return (
    <div
      className="agent-detail-window"
      style={{
        left: windowPosition.x,
        top: windowPosition.y,
        "--agent-color": color,
      }}
    >
      <div className="agent-detail-header" onMouseDown={startDrag}>
        <span className={`agent-monitor-status ${statusTone(liveStatus)}`} />
        <span className="agent-detail-name">{agent.name}</span>
        <span className="agent-monitor-class">{agent.cls || "?"}</span>
        {isStillSyncing && <span className="agent-detail-sync">syncing...</span>}
        {location && <span className="agent-detail-location">{formatVector(location, ",")}</span>}
        {rotation && <span className="agent-detail-heading">{headingFromRotation(rotation)}</span>}
        {currentAction && (
          <span className="agent-detail-action">
            {renderIcon(icons, "zap", 11)} {currentAction}
          </span>
        )}
        {!currentAction && lastAction && <span className="agent-detail-last">last: {lastAction}</span>}
        <div className="agent-detail-spacer" />
        <button
          className="agent-detail-close"
          onClick={onClose}
          onMouseDown={(event) => event.stopPropagation()}
          type="button"
        >
          {renderIcon(icons, "close", 13) || "x"}
        </button>
      </div>

      <div className="agent-detail-stats">
        {liveState?.speed > 0 && (
          <span>{renderIcon(icons, "zap", 11)} {Math.round((liveState.speed || 0) / 100)} m/s</span>
        )}
        <span className={(liveState?.collisionCount || 0) > 0 ? "danger" : ""}>
          {renderIcon(icons, "collision", 11)} {liveState?.collisionCount || 0} collisions
        </span>
        <span>{renderIcon(icons, "refresh", 11)} {liveState?.totalTurns || 0} turns</span>
        {liveState?.totalCostUsd > 0 && <span>${(liveState.totalCostUsd || 0).toFixed(3)}</span>}
      </div>

      <div className="agent-detail-tabs">
        {tabs.map((tab) => (
          <button
            key={tab.id}
            className={activeTab === tab.id ? "active" : ""}
            onClick={() => setActiveTab(tab.id)}
            type="button"
          >
            {tab.label}
          </button>
        ))}
      </div>

      {activeTab === "camera" && (
        <div className="agent-detail-camera">
          <div className="agent-detail-camera-controls">
            <label>
              <input
                type="checkbox"
                checked={autoRefresh}
                onChange={(event) => setAutoRefresh(event.target.checked)}
              />
              Auto (4s)
            </label>
            <button onClick={focusAndShoot} disabled={cameraLoading} type="button">
              {cameraLoading ? "Capturing..." : "Capture"}
            </button>
            {!location && <span>No location - no camera</span>}
          </div>

          <div className="agent-camera-stage">
            {cameraImage ? (
              <img src={cameraImage} alt={`${agent.name} view`} />
            ) : cameraError ? (
              <div className="agent-camera-placeholder error">
                <div>{renderIcon(icons, "camera", 28)}</div>
                <div>{cameraError}</div>
                <button onClick={focusAndShoot} type="button">Retry</button>
              </div>
            ) : (
              <div className="agent-camera-placeholder">
                {cameraLoading ? (
                  <>
                    <span className="agent-camera-spinner" />
                    <div>Focusing camera...</div>
                  </>
                ) : (
                  <>
                    <div>{renderIcon(icons, "camera", 28)}</div>
                    <div>Click "Capture" to take a screenshot</div>
                  </>
                )}
              </div>
            )}
            {cameraLoading && cameraImage && (
              <div className="agent-camera-loading">
                <span className="agent-camera-spinner small" />
              </div>
            )}
          </div>
        </div>
      )}

      {activeTab === "trajectory" && (
        <AgentTrajectoryView
          agentName={agent.name}
          color={color}
          icons={icons}
          liveState={liveState}
        />
      )}

      {activeTab === "activity" && (
        <div ref={activityRef} className="agent-detail-activity">
          {agentMessages.length > 0 && (
            <div className="agent-detail-message-block">
              <div className="agent-detail-section-title">Incoming Messages</div>
              {agentMessages.map((message, index) => (
                <div key={index} className="agent-detail-incoming">
                  <span>{message.from}</span>: {message.text}
                </div>
              ))}
            </div>
          )}
          {pastActivities.length === 0 && agentMessages.length === 0 ? (
            <div className="agent-detail-empty">No activity yet</div>
          ) : (
            pastActivities.map((activity, index) => {
              const thought = activity.thought || activity.response || "";
              const actions = activity.actions || [];
              return (
                <div key={index} className="agent-detail-turn">
                  <div>Turn {index + 1}</div>
                  {thought && <p>{truncate(thought, 200)}</p>}
                  {actions.map((action, actionIndex) => (
                    <div key={actionIndex} className="agent-detail-tool">
                      <span className={action.ok ? "ok" : "error"}>{action.ok ? "ok" : "err"}</span>
                      <code>{action.tool || action.name}</code>
                    </div>
                  ))}
                </div>
              );
            })
          )}
        </div>
      )}

      {activeTab === "chat" && (
        <div className="agent-detail-chat">
          <div className="agent-detail-chat-header">
            Send a direct command to <strong>{agent.name}</strong>
          </div>
          <div ref={activityRef} className="agent-detail-chat-messages">
            {agentMessages.map((message, index) => (
              <div key={index} className="agent-detail-chat-message">
                <div>{message.from}</div>
                <p>{message.text}</p>
              </div>
            ))}
          </div>
          <div className="agent-detail-chat-compose">
            <textarea
              value={input}
              onChange={(event) => setInput(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  handleSend();
                }
              }}
              placeholder={`Command ${agent.name}... (Enter to send)`}
              rows={2}
            />
            <button onClick={handleSend} disabled={!input.trim() || sending} type="button">
              Send
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

export default function AgentPanel({
  agentColors = DEFAULT_AGENT_COLORS,
  commHeight = 200,
  hideComm = false,
  icons = {},
  onCommHeightChange,
  sessionId,
}) {
  const agentsCtx = useAgents();
  const statusCtx = useStatus();
  const pieActive = statusCtx.pieActive;
  const [expandedAgent, setExpandedAgent] = useState(null);
  const resizeRef = useRef(null);
  const dragging = useRef(false);
  const startY = useRef(0);
  const startHeight = useRef(commHeight);

  const contextAgents = useMemo(() => {
    const sessions = agentsCtx.sessions || [];
    if (sessions.length > 0) {
      return sessions.map((session) => ({
        name: session.agentName,
        cls: session.agentClass,
        location: session.location,
        rotation: session.rotation,
        status: session.status,
        currentAction: session.currentAction,
        lastAction: session.lastAction,
      }));
    }
    return agentsCtx.agents || [];
  }, [agentsCtx.sessions, agentsCtx.agents]);

  useEffect(() => {
    if (!expandedAgent) return;
    const updated = contextAgents.find((agent) => agent.name === expandedAgent.name);
    if (!updated) {
      setExpandedAgent(null);
      return;
    }
    if (
      updated.location !== expandedAgent.location ||
      updated.rotation !== expandedAgent.rotation ||
      updated.status !== expandedAgent.status
    ) {
      setExpandedAgent(updated);
    }
  }, [contextAgents, expandedAgent]);

  const onMouseDown = useCallback((event) => {
    event.preventDefault();
    dragging.current = true;
    startY.current = event.clientY;
    startHeight.current = commHeight;
    document.body.style.cursor = "row-resize";
    document.body.style.userSelect = "none";
    resizeRef.current?.classList.add("dragging");

    const onMove = (moveEvent) => {
      if (!dragging.current) return;
      const delta = startY.current - moveEvent.clientY;
      const next = Math.max(60, Math.min(400, startHeight.current + delta));
      onCommHeightChange?.(next);
    };
    const onUp = () => {
      dragging.current = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      resizeRef.current?.classList.remove("dragging");
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  }, [commHeight, onCommHeightChange]);

  const empty = contextAgents.length === 0;

  return (
    <div className="sw-right-inner">
      <div className="sw-agents-pane">
        <div className="agent-monitor-header">
          <span>
            Agents {contextAgents.length > 0 && <strong>({contextAgents.length})</strong>}
          </span>
          <div />
          <button
            onClick={() => refreshAgentContext()}
            title="Sync context and auto-discover player agents from UE"
            type="button"
          >
            Discover
          </button>
          <span className={`agent-pie-dot ${pieActive ? "active" : ""}`} />
          <span className={pieActive ? "agent-pie-label active" : "agent-pie-label"}>
            {pieActive ? "PIE Active" : "No PIE"}
          </span>
        </div>

        {expandedAgent && createPortal(
          <AgentDetailPanel
            agent={expandedAgent}
            agentColors={agentColors}
            colorIdx={contextAgents.findIndex((agent) => agent.name === expandedAgent.name)}
            icons={icons}
            onClose={() => setExpandedAgent(null)}
          />,
          document.body,
        )}

        {empty ? (
          <div className="agent-monitor-empty">
            <div>{renderIcon(icons, "robot", 28)}</div>
            <p>{pieActive ? "Spawn agents to see them here." : "Start PIE in Unreal Engine to enable agent control."}</p>
          </div>
        ) : (
          <div className="agent-monitor-grid">
            {contextAgents.map((agent, index) => (
              <AgentCard
                key={agent.name}
                agent={agent}
                agentColors={agentColors}
                colorIdx={index}
                onExpand={setExpandedAgent}
              />
            ))}
          </div>
        )}
      </div>

      {!hideComm && (
        <>
          <div className="sw-resize-row" ref={resizeRef} onMouseDown={onMouseDown} title="Drag to resize" />
          <div className="sw-comm-pane" style={{ height: commHeight }}>
            <AgentAggregatePanelTabs
              agentColors={agentColors}
              agents={contextAgents}
              icons={icons}
              sessionId={sessionId}
            />
          </div>
        </>
      )}
    </div>
  );
}

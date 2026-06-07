import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { API_BASE } from "../../api/client.js";
import {
  clearCheckpoints,
  createCheckpoint,
  fetchEvolutionConfig,
  fetchSkills,
  getCheckpoint,
  listCheckpoints,
  resetScene,
  restoreCheckpoint,
  saveScene,
  sendChat,
  shareToGallery,
  updateEvolutionConfig,
} from "../../api/appApi.js";
import SceneAgentHeader from "../../components/chat/SceneAgentHeader.jsx";
import { agentLabel } from "../agents/codingAgents.js";
import AnnotateOverlay from "./AnnotateOverlay.jsx";
import ChatMessage, { TypingIndicator } from "./ChatMessage.jsx";
import CheckpointBar from "./CheckpointBar.jsx";
import SkillsPanel from "./SkillsPanel.jsx";
import {
  appendTextDeltaToMessage,
  buildWelcomeMessage,
  generateMessageId,
  getUniqueTextAppend,
  screenshotPathFromUrl,
  turnChangedScene,
} from "./chatRuntime.js";

const QUICK_SUGGESTIONS = [
  "Add more trees",
  "Move buildings further apart",
  "Change to sunset lighting",
  "Take a screenshot from a different angle",
  "Add street furniture",
];

function buildToolIcons(icons = {}) {
  return {
    get_actors_in_level: icons.clipboard,
    find_actors_by_name: icons.search,
    spawn_actor: icons.plus,
    delete_actor: icons.trash,
    set_actor_transform: icons.transform,
    get_actor_properties: icons.document,
    focus_viewport: icons.video,
    take_screenshot: icons.camera,
    get_camera_0_view: icons.camera,
    execute_python_script: icons.python,
    create_blueprint: icons.wrench,
    compile_blueprint: icons.gear,
    apply_material_to_actor: icons.palette,
    initialize: icons.plug,
    observe_scene: icons.eye,
    get_scene_overview: icons.map,
  };
}

export default function ChatPanel({ onScreenshotUpdate, onRef, onSessionChange, onChatDone, codingAgent, codingModel, icons = {} }) {
  const [messages, setMessages] = useState(() => [buildWelcomeMessage()]);
  const [checkpoints, setCheckpoints] = useState([]);
  const [activeLeafId, setActiveLeafId] = useState(null);
  const [restoringId, setRestoringId] = useState(null);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [sessionId, setSessionId] = useState(null);
  const [mcpStatus, setMcpStatus] = useState("-");
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
  const toolIcons = useMemo(() => buildToolIcons(icons), [icons]);

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

  // Checkpoint state mirrors so async handlers read the latest values.
  const messagesRef = useRef(messages);
  const sessionIdRef = useRef(sessionId);
  const activeLeafRef = useRef(activeLeafId);
  const latestScreenshotRef = useRef(latestScreenshot);
  const turnCountRef = useRef(turnCount);
  // Checkpoints key off the STABLE studio session (STUDIO_SESSION from /api/session),
  // NOT the chat's `sessionId` - that changes every turn (each turn is a fresh `claude -p`),
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
          // clean starting map. The base .umap is never modified - new sessions reload it.
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
      onChatDone?.();  // nudge the viewport/screenshot to refresh - the scene changed
    } catch (_e) {
      setMessages((prev) => [...prev, { id: generateMessageId(), role: "assistant", content: "Warning: Could not restore that checkpoint.", timestamp: Date.now() }]);
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
      const _scheduleTextFlush = (callback) => {
        if (typeof window !== "undefined" && typeof window.requestAnimationFrame === "function") {
          window.requestAnimationFrame(callback);
        } else {
          setTimeout(callback, 0);
        }
      };
      const _flushTextBuf = (msgId) => {
        if (!_textBuf) return;
        const delta = _textBuf;
        _textBuf = "";
        _rafPending = false;
        setMessages((prev) => {
          const updated = [...prev];
          const idx = updated.findIndex((m) => m.id === msgId);
          if (idx === -1) return prev;
          const msg = { ...updated[idx], waiting: false };
          const result = appendTextDeltaToMessage(msg, delta);
          if (!result.changed) {
            if (updated[idx].waiting) {
              updated[idx] = msg;
              return updated;
            }
            return prev;
          }
          updated[idx] = result.message;
          return updated;
        });
      };

      // NOTE: no absolute time limit on agent runs. Long scenes can legitimately
      // take 10+ minutes. Dead-connection detection lives inside sendChat's
      // reader-level idle timer (refreshed by server `: ping` heartbeats).
      // To stop a runaway agent, use the Stop button (handleStop -> controller.abort).

      try {
        await sendChat(
          text,
          sessionId,
          (event) => {
            setMessages((prev) => {
              const updated = [...prev];
              const idx = updated.findIndex((m) => m.id === assistantId);
              if (idx === -1) return prev;
              let msg = { ...updated[idx] };
              if (event.type !== "text" && _textBuf) {
                const result = appendTextDeltaToMessage(msg, _textBuf);
                _textBuf = "";
                _rafPending = false;
                if (result.changed) msg = result.message;
              }
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
                  setMcpStatus(connected.length ? `connected: ${connected.join(", ")}` : "none");
                  if (event.data.sessionId) {
                    setSessionId(event.data.sessionId);
                    onSessionChange?.(event.data.sessionId);
                  }
                  break;
                }
                case "text": {
                  // P2-2: buffer into _textBuf, flush via rAF - avoids setState per character
                  const appendText = getUniqueTextAppend(`${msg.content || ""}${_textBuf}`, event.data.delta || "");
                  if (!appendText) return prev;
                  _textBuf += appendText;
                  if (!_rafPending) {
                    _rafPending = true;
                    _scheduleTextFlush(() => _flushTextBuf(assistantId));
                  }
                  // Don't update msg here - the rAF flush handles it separately
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
                  // Always keep sessionId - it's the stable studio session, not Claude's transient one
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
                      ? "**Warning:** Agent exited unexpectedly. Try again; each message starts a fresh process."
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
        } catch (_e) { /* checkpoint is best-effort; never block the chat */ }
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
    setMcpStatus("-");
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
        icons={icons}
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
              <ChatMessage
                message={msg}
                agentLabel={agentLabel(codingAgent)}
                toolIcons={toolIcons}
                fallbackToolIcon={icons?.hammer}
              />
              {ckpt && (
                <CheckpointBar
                  checkpoint={ckpt}
                  checkpoints={checkpoints}
                  activeLeafId={activeLeafId}
                  icons={icons}
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
                : "Describe the city scene you want to generate..."
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
              background: loading ? "color-mix(in srgb, var(--red) 12%, transparent)" : input.trim() ? "var(--blue)" : "var(--panel-2)",
              color: loading ? "var(--red)" : input.trim() ? "var(--accent-ink)" : "var(--ink-3)",
              cursor: loading || input.trim() ? "pointer" : "default",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontSize: 15,
              boxShadow: input.trim() && !loading ? "var(--shadow-card)" : "none",
            }}
          >
            {loading ? icons?.close?.(15) : icons?.zap?.(15)}
          </button>
        </div>
        <div style={{ marginTop: 5, fontSize: 12, color: "var(--ink-3)" }}>
          Enter to send - Shift+Enter for new line
          {/* Agent/model picker moved to the top-left nav bar (see CodingAgentSelector). */}
          <span style={{ marginLeft: 8, color: autoSkillSelectionEnabled ? "var(--blue)" : "var(--ink-3)" }}>
            {autoSkillSelectionEnabled ? "Auto-select skills: on" : "Auto-select skills: off"}
            {autoSelectingSkills ? " (selecting...)" : ""}
          </span>
          {activeSkills.length > 0 && (
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

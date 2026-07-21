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
  assertReviewRunEvent,
  buildChatStopPayload,
  buildWelcomeMessage,
  generateMessageId,
  getUniqueTextAppend,
  mergeReviewAccounting,
  mergeReviewEvidence,
  normalizeLoopMode,
  claimPendingReviewExclusive,
  clearPreProviderReviewExclusive,
  clearTerminalReviewExclusive,
  createReviewOwnerId,
  markPendingReviewTerminalExclusive,
  pendingReviewRecordExists,
  productionReviewRequiresWebLocks,
  readPendingReview,
  reviewFailureDisposition,
  reviewTerminalDisposition,
  screenshotPathFromUrl,
  turnChangedScene,
} from "./chatRuntime.js";

const QUICK_SUGGESTIONS = [
  "Increase tree coverage",
  "Increase building setbacks",
  "Set late-afternoon lighting",
  "Capture an alternate camera view",
  "Add street furniture",
];

const CHAT_CONVERSATIONS_KEY = "simworld.chat.conversations.v1";
const CHAT_ACTIVE_KEY = "simworld.chat.activeConversation.v1";

function generateReviewRunId() {
  const uuid = typeof globalThis.crypto?.randomUUID === "function"
    ? globalThis.crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 14)}`;
  return `review-client-${uuid}`;
}

function normalizeAssetPolicy(value) {
  return value === "basic_geometry" ? "basic_geometry" : "real_assets";
}

function readJsonStorage(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    return raw ? JSON.parse(raw) : fallback;
  } catch {
    return fallback;
  }
}

function writeJsonStorage(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {}
}

function makeConversation(overrides = {}) {
  const now = Date.now();
  return {
    id: overrides.id || `chat_${now.toString(36)}_${Math.random().toString(36).slice(2, 6)}`,
    title: overrides.title || "New conversation",
    createdAt: overrides.createdAt || now,
    updatedAt: overrides.updatedAt || now,
    state: {
      messages: overrides.state?.messages || [buildWelcomeMessage()],
      input: overrides.state?.input || "",
      sessionId: overrides.state?.sessionId || null,
      mcpStatus: overrides.state?.mcpStatus || "-",
      selectedSkills: overrides.state?.selectedSkills || [],
      autoSkillSelectionEnabled: overrides.state?.autoSkillSelectionEnabled ?? true,
      autoSelectedSkills: overrides.state?.autoSelectedSkills || [],
      autoSelectionError: overrides.state?.autoSelectionError || "",
      loopMode: normalizeLoopMode(overrides.state?.loopMode),
      assetPolicy: normalizeAssetPolicy(overrides.state?.assetPolicy),
      latestScreenshot: overrides.state?.latestScreenshot || null,
      turnCount: overrides.state?.turnCount || 0,
    },
  };
}

function titleFromMessages(messages) {
  const firstUser = messages.find((message) => message.role === "user" && message.content?.trim());
  if (!firstUser) return "New conversation";
  const compact = firstUser.content.replace(/\s+/g, " ").trim();
  return compact.length > 42 ? `${compact.slice(0, 42)}...` : compact;
}

function normalizeConversation(conversation) {
  const normalized = makeConversation(conversation || {});
  return {
    ...normalized,
    title: conversation?.title || titleFromMessages(normalized.state.messages),
  };
}

function loadChatConversations() {
  const saved = readJsonStorage(CHAT_CONVERSATIONS_KEY, []);
  let conversations = Array.isArray(saved) && saved.length > 0
    ? saved.map(normalizeConversation)
    : [makeConversation()];
  const pending = readPendingReview();
  if (pending) {
    let conversation = conversations.find((item) => item.id === pending.conversationId);
    if (!conversation) {
      conversation = makeConversation({ id: pending.conversationId });
      conversations = [conversation, ...conversations];
    }
    conversations = conversations.map((item) => {
      if (item.id !== pending.conversationId) return item;
      const messages = [...(item.state.messages || [])];
      if (!messages.some((message) => message.id === pending.userMessageId)) {
        messages.push({
          id: pending.userMessageId,
          role: "user",
          content: pending.request.prompt,
          timestamp: pending.createdAt || Date.now(),
        });
      }
      const assistantIndex = messages.findIndex((message) => message.id === pending.assistantId);
      const recovered = {
        id: pending.assistantId,
        role: "assistant",
        content: pending.state === "terminal_received"
          ? "A Review terminal was received but browser reconciliation did not finish. Retry the same server-owned run ID to recover it safely."
          : "Review delivery was interrupted. Retry resumes the same server-owned run without starting a second provider attempt.",
        waiting: false,
        toolCalls: [],
        timestamp: pending.createdAt || Date.now(),
        reviewRequest: {
          ...pending.request,
          pendingGeneration: pending.generation,
          pendingOwnerId: pending.ownerId,
          pendingState: pending.state,
          terminalReceipt: pending.terminalReceipt || null,
        },
        reviewRetryAvailable: true,
        reviewStartNewAvailable: false,
        reviewResult: {
          code: "REVIEW_TRANSPORT_AMBIGUOUS",
          retryable: true,
          recovered: false,
        },
      };
      if (assistantIndex >= 0) messages[assistantIndex] = { ...messages[assistantIndex], ...recovered };
      else messages.push(recovered);
      return { ...item, state: { ...item.state, messages } };
    });
  }
  const savedActiveId = (() => {
    try {
      return localStorage.getItem(CHAT_ACTIVE_KEY);
    } catch {
      return null;
    }
  })();
  const activeConversationId = pending
    ? pending.conversationId
    : (conversations.some((conversation) => conversation.id === savedActiveId)
      ? savedActiveId
      : conversations[0].id);
  return { conversations, activeConversationId };
}

function terminalUiPersistenceError(message) {
  const error = new Error(message);
  error.code = "REVIEW_TERMINAL_UI_PERSIST_FAILED";
  error.retryable = true;
  return error;
}

function persistedTerminalMessageMatches({
  storage = globalThis.localStorage,
  conversationId,
  assistantId,
  runId,
  receiptId,
}) {
  try {
    const stored = JSON.parse(storage.getItem(CHAT_CONVERSATIONS_KEY) || "[]");
    const conversation = Array.isArray(stored)
      ? stored.find((item) => item?.id === conversationId)
      : null;
    const message = conversation?.state?.messages?.find((item) => item?.id === assistantId);
    return Boolean(
      message
      && message.reviewRequest?.runId === runId
      && message.reviewTerminalReceiptId === receiptId
      && message.waiting === false,
    );
  } catch {
    return false;
  }
}

function persistTerminalConversation({
  conversations,
  activeConversationId,
  messages,
  assistantId,
  runId,
  receiptId,
  storage = globalThis.localStorage,
}) {
  let stored;
  try {
    const raw = storage.getItem(CHAT_CONVERSATIONS_KEY);
    stored = raw ? JSON.parse(raw) : conversations;
  } catch {
    throw terminalUiPersistenceError("Saved conversations could not be read");
  }
  if (!Array.isArray(stored)) {
    throw terminalUiPersistenceError("Saved conversations are invalid");
  }
  const source = stored;
  let matched = false;
  const next = source.map((conversation) => {
    if (conversation?.id !== activeConversationId) return conversation;
    matched = true;
    return {
      ...conversation,
      title: titleFromMessages(messages),
      updatedAt: Date.now(),
      state: { ...(conversation.state || {}), messages },
    };
  });
  if (!matched) {
    throw terminalUiPersistenceError("The Review conversation no longer exists");
  }
  try {
    storage.setItem(CHAT_CONVERSATIONS_KEY, JSON.stringify(next));
  } catch {
    throw terminalUiPersistenceError("The Review terminal message could not be stored");
  }
  if (!persistedTerminalMessageMatches({
    storage,
    conversationId: activeConversationId,
    assistantId,
    runId,
    receiptId,
  })) {
    throw terminalUiPersistenceError("The Review terminal message could not be verified");
  }
}

function persistActiveConversation(conversations, activeConversationId, state) {
  const updatedAt = Date.now();
  const updated = conversations.map((conversation) =>
    conversation.id === activeConversationId
      ? {
          ...conversation,
          title: titleFromMessages(state.messages),
          updatedAt,
          state,
        }
      : conversation
  );
  writeJsonStorage(CHAT_CONVERSATIONS_KEY, updated);
  try {
    localStorage.setItem(CHAT_ACTIVE_KEY, activeConversationId);
  } catch {}
  return updated;
}

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

function ReviewEvidenceGallery({ evidence }) {
  if (!Array.isArray(evidence) || evidence.length === 0) return null;
  const rounds = [...new Set(evidence.map((item) => item.round).filter(Number.isFinite))];
  const roundLabel = rounds.length === 1 ? ` · round ${rounds[0]}` : "";

  return (
    <section
      aria-label="Visual review evidence"
      style={{
        width: "100%",
        padding: "8px 10px 10px",
        borderBottom: "1px solid var(--line-soft)",
      }}
    >
      <div className="loop-critic-head">Visual evidence{roundLabel} · {evidence.length} view{evidence.length === 1 ? "" : "s"}</div>
      <div
        className="chat-tool-screenshot"
        style={{
          display: "grid",
          gridTemplateColumns: evidence.length === 1 ? "minmax(0, 1fr)" : "repeat(2, minmax(0, 1fr))",
          gap: 6,
        }}
      >
        {evidence.map((item, index) => (
          <a
            href={item.url}
            key={item.url}
            rel="noreferrer"
            target="_blank"
            title={`Open visual review evidence ${index + 1}`}
          >
            <img
              alt={`Visual review evidence ${index + 1}`}
              loading="lazy"
              src={item.url}
            />
          </a>
        ))}
      </div>
    </section>
  );
}

export default function ChatPanel({ onScreenshotUpdate, onRef, onSessionChange, onChatDone, codingAgent, codingModel, icons = {} }) {
  const initialChatStateRef = useRef(null);
  if (!initialChatStateRef.current) initialChatStateRef.current = loadChatConversations();
  const initialConversation = initialChatStateRef.current.conversations.find(
    (conversation) => conversation.id === initialChatStateRef.current.activeConversationId
  ) || initialChatStateRef.current.conversations[0];
  const initialState = initialConversation.state;

  const [conversations, setConversations] = useState(initialChatStateRef.current.conversations);
  const [activeConversationId, setActiveConversationId] = useState(initialChatStateRef.current.activeConversationId);
  const [messages, setMessages] = useState(() => initialState.messages || [buildWelcomeMessage()]);
  const [checkpoints, setCheckpoints] = useState([]);
  const [activeLeafId, setActiveLeafId] = useState(null);
  const [restoringId, setRestoringId] = useState(null);
  const [input, setInput] = useState(initialState.input || "");
  const [loading, setLoading] = useState(false);
  const [sessionId, setSessionId] = useState(initialState.sessionId || null);
  const [mcpStatus, setMcpStatus] = useState(initialState.mcpStatus || "-");
  const [selectedSkills, setSelectedSkills] = useState(initialState.selectedSkills || []);
  const [autoSkillSelectionEnabled, setAutoSkillSelectionEnabled] = useState(initialState.autoSkillSelectionEnabled ?? true);
  const [loopMode, setLoopMode] = useState(normalizeLoopMode(initialState.loopMode));
  const [assetPolicy, setAssetPolicy] = useState(normalizeAssetPolicy(initialState.assetPolicy));
  const [autoSelectedSkills, setAutoSelectedSkills] = useState(initialState.autoSelectedSkills || []);
  const [autoSelectingSkills, setAutoSelectingSkills] = useState(false);
  const [autoSelectionError, setAutoSelectionError] = useState(initialState.autoSelectionError || "");
  const [selfEvolutionEnabled, setSelfEvolutionEnabled] = useState(null);
  const [latestScreenshot, setLatestScreenshot] = useState(initialState.latestScreenshot || null);
  const [annotating, setAnnotating] = useState(false);
  const [turnCount, setTurnCount] = useState(initialState.turnCount || 0);
  const [sessionsOpen, setSessionsOpen] = useState(false);
  const [preparingReview, setPreparingReview] = useState(false);
  const scrollRef = useRef(null);
  const abortRef = useRef(null);
  const activeRunRef = useRef(null);
  const dispatchInFlightRef = useRef(false);
  const preparingReviewRef = useRef(false);
  const reviewOwnerIdRef = useRef(null);
  if (!reviewOwnerIdRef.current) reviewOwnerIdRef.current = createReviewOwnerId();
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
        if (preparingReviewRef.current || dispatchInFlightRef.current) return;
        if (scene.sessionId) {
          setSessionId(scene.sessionId);
          setLoopMode(normalizeLoopMode(scene.loopMode));
          setAssetPolicy(normalizeAssetPolicy(scene.assetPolicy));
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

  useEffect(() => {
    const state = {
      messages,
      input,
      sessionId,
      mcpStatus,
      selectedSkills,
      autoSkillSelectionEnabled,
      autoSelectedSkills,
      autoSelectionError,
      loopMode,
      assetPolicy,
      latestScreenshot,
      turnCount,
    };
    setConversations((prev) => persistActiveConversation(prev, activeConversationId, state));
  }, [
    activeConversationId,
    messages,
    input,
    sessionId,
    mcpStatus,
    selectedSkills,
    autoSkillSelectionEnabled,
    autoSelectedSkills,
    autoSelectionError,
    loopMode,
    assetPolicy,
    latestScreenshot,
    turnCount,
  ]);

  const loadConversationState = useCallback((conversation) => {
    const state = conversation.state || makeConversation().state;
    setMessages(Array.isArray(state.messages) && state.messages.length ? state.messages : [buildWelcomeMessage()]);
    setInput(state.input || "");
    setSessionId(state.sessionId || null);
    setMcpStatus(state.mcpStatus || "-");
    setSelectedSkills(Array.isArray(state.selectedSkills) ? state.selectedSkills : []);
    setAutoSkillSelectionEnabled(state.autoSkillSelectionEnabled ?? true);
    setAutoSelectedSkills(Array.isArray(state.autoSelectedSkills) ? state.autoSelectedSkills : []);
    setAutoSelectingSkills(false);
    setAutoSelectionError(state.autoSelectionError || "");
    setLoopMode(normalizeLoopMode(state.loopMode));
    setAssetPolicy(normalizeAssetPolicy(state.assetPolicy));
    setLatestScreenshot(state.latestScreenshot || null);
    setTurnCount(state.turnCount || 0);
    onSessionChange?.(state.sessionId || null);
  }, [onSessionChange]);

  const handleSelectConversation = useCallback((id) => {
    if (loading || preparingReviewRef.current || dispatchInFlightRef.current
        || id === activeConversationId) return;
    const conversation = conversations.find((item) => item.id === id);
    if (!conversation) return;
    setActiveConversationId(id);
    try {
      localStorage.setItem(CHAT_ACTIVE_KEY, id);
    } catch {}
    loadConversationState(conversation);
    setSessionsOpen(false);
  }, [activeConversationId, conversations, loadConversationState, loading]);

  const handleNewConversation = useCallback(() => {
    if (loading || preparingReviewRef.current || dispatchInFlightRef.current) return;
    const conversation = makeConversation();
    setConversations((prev) => {
      const next = [conversation, ...prev];
      writeJsonStorage(CHAT_CONVERSATIONS_KEY, next);
      return next;
    });
    setActiveConversationId(conversation.id);
    try {
      localStorage.setItem(CHAT_ACTIVE_KEY, conversation.id);
    } catch {}
    setCheckpoints([]);
    setActiveLeafId(null);
    loadConversationState(conversation);
    setSessionsOpen(false);
  }, [loadConversationState, loading]);

  const handleDeleteConversation = useCallback((id) => {
    if (loading || preparingReviewRef.current || dispatchInFlightRef.current) return;
    const remaining = conversations.filter((conversation) => conversation.id !== id);
    const next = remaining.length ? remaining : [makeConversation()];
    writeJsonStorage(CHAT_CONVERSATIONS_KEY, next);
    setConversations(next);
    if (id === activeConversationId) {
      const replacement = next[0];
      setActiveConversationId(replacement.id);
      try {
        localStorage.setItem(CHAT_ACTIVE_KEY, replacement.id);
      } catch {}
      setCheckpoints([]);
      setActiveLeafId(null);
      loadConversationState(replacement);
    }
  }, [activeConversationId, conversations, loadConversationState, loading]);

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
    if (!sid || restoringId || preparingReviewRef.current || dispatchInFlightRef.current) return;
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
    async (overrideMessage, feedbackText, retryRequest = null) => {
      const text = (overrideMessage || input).trim();
      if (!text || loading || dispatchInFlightRef.current) return;
      dispatchInFlightRef.current = true;
      const pendingBeforeDispatch = readPendingReview();
      const pendingRecordExists = pendingReviewRecordExists();
      const isStartNewRequest = retryRequest?.startNew === true;
      const isExactPendingRetry = Boolean(
        pendingBeforeDispatch
        && !isStartNewRequest
        && retryRequest?.runId === pendingBeforeDispatch.request.runId
        && retryRequest?.pendingGeneration === pendingBeforeDispatch.generation
        && activeConversationId === pendingBeforeDispatch.conversationId,
      );
      if ((pendingRecordExists && !isExactPendingRetry)
          || (retryRequest && !isStartNewRequest && !pendingBeforeDispatch)) {
        setMessages((prev) => [...prev, {
          id: generateMessageId(),
          role: "assistant",
          content: pendingBeforeDispatch
            ? "Another Review still requires recovery. Resume that exact request before starting any new scene mutation."
            : "Review recovery state is missing or invalid. No new scene mutation was sent.",
          timestamp: Date.now(),
        }]);
        dispatchInFlightRef.current = false;
        return;
      }
      if (!overrideMessage) setInput("");
      if (autoSkillSelectionEnabled) {
        setAutoSelectionError("");
      }

      const effectiveLoopMode = normalizeLoopMode(retryRequest?.options?.loopMode ?? loopMode);
      const effectiveSessionId = retryRequest?.sessionId ?? sessionId;
      const requestOptions = retryRequest?.options
        ? { ...retryRequest.options }
        : {
            skills: selectedSkills.length > 0 ? selectedSkills : undefined,
            feedback: feedbackText,
            skillSelectionMode: autoSkillSelectionEnabled ? "auto" : "manual",
            agent: codingAgent,
            conversationId: activeConversationId,
            model: codingModel,
            loopMode: effectiveLoopMode,
            requireRealAssets: assetPolicy === "real_assets",
            assetDegradedMode: assetPolicy === "basic_geometry" ? "basic_geometry" : "disabled",
          };
      const reviewRunId = effectiveLoopMode === "vanilla"
        ? null
        : (retryRequest?.runId || generateReviewRunId());
      if (reviewRunId) requestOptions.runId = reviewRunId;
      let reviewRequest = reviewRunId
        ? {
            runId: reviewRunId,
            prompt: text,
            sessionId: effectiveSessionId,
            options: requestOptions,
          }
        : null;

      const candidateCreatedAt = Date.now();
      let userMessageId = generateMessageId();
      let assistantId = generateMessageId();
      let messageCreatedAt = candidateCreatedAt;

      if (reviewRequest) {
        preparingReviewRef.current = true;
        setPreparingReview(true);
        try {
          const persisted = await claimPendingReviewExclusive(
            {
              schema: "simworld-review-pending/v2",
              conversationId: activeConversationId,
              userMessageId,
              assistantId,
              createdAt: candidateCreatedAt,
              request: reviewRequest,
            },
            {
              ownerId: reviewOwnerIdRef.current,
              expectedGeneration: isExactPendingRetry
                ? retryRequest.pendingGeneration
                : null,
            },
            { requireLock: productionReviewRequiresWebLocks() },
          );
          userMessageId = persisted.userMessageId;
          assistantId = persisted.assistantId;
          messageCreatedAt = persisted.createdAt;
          reviewRequest = {
            ...persisted.request,
            pendingGeneration: persisted.generation,
            pendingOwnerId: persisted.ownerId,
            pendingState: persisted.state,
            terminalReceipt: persisted.terminalReceipt || null,
          };
        } catch (error) {
          setMessages((prev) => [...prev, {
            id: userMessageId,
            role: "user",
            content: text,
            timestamp: candidateCreatedAt,
          }, {
            id: assistantId,
            role: "assistant",
            waiting: false,
            content: `Review was not started because its recovery record could not be stored: ${error.message}`,
            toolCalls: [],
            timestamp: candidateCreatedAt,
            reviewRequest: null,
            reviewRetryAvailable: false,
            reviewStartNewAvailable: false,
          }]);
          preparingReviewRef.current = false;
          setPreparingReview(false);
          dispatchInFlightRef.current = false;
          return;
        }
      }

      const userMsg = {
        id: userMessageId,
        role: "user",
        content: text,
        timestamp: messageCreatedAt,
      };
      const assistantMsg = {
        id: assistantId,
        role: "assistant",
        content: "",
        waiting: true,
        toolCalls: [],
        timestamp: messageCreatedAt,
        ...(reviewRequest ? { reviewRequest, reviewRetryAvailable: false } : {}),
      };
      setMessages((prev) => {
        if (!reviewRequest) return [...prev, userMsg, assistantMsg];
        const withoutStaleActions = prev.map((message) => (
          message.reviewRequest?.runId === reviewRequest.runId
            ? { ...message, reviewRetryAvailable: false, reviewStartNewAvailable: false }
            : message
        ));
        const userIndex = withoutStaleActions.findIndex((message) => message.id === userMessageId);
        if (userIndex < 0) withoutStaleActions.push(userMsg);
        const assistantIndex = withoutStaleActions.findIndex((message) => message.id === assistantId);
        if (assistantIndex >= 0) {
          const prior = withoutStaleActions[assistantIndex];
          withoutStaleActions[assistantIndex] = {
            ...prior,
            ...assistantMsg,
            content: prior.content || "",
            blocks: prior.blocks,
            reviewEvidence: prior.reviewEvidence,
            reviewAccounting: prior.reviewAccounting,
            reviewResult: null,
          };
        } else {
          withoutStaleActions.push(assistantMsg);
        }
        return withoutStaleActions;
      });
      setLoading(true);
      preparingReviewRef.current = false;
      setPreparingReview(false);
      setTurnCount((c) => c + 1);

      const controller = new AbortController();
      abortRef.current = controller;
      activeRunRef.current = {
        assistantId,
        conversationId: requestOptions.conversationId || activeConversationId,
        runId: reviewRunId,
        // Review requests carry a client-stable idempotency key. The server
        // must echo it from run_start; retries reuse the stored request below.
        sessionId: effectiveSessionId || "_global",
      };
      const inputBuffers = new Map();
      let authoritativeDone = false;
      const reviewCasIdentity = () => ({
        runId: reviewRequest?.runId,
        conversationId: requestOptions.conversationId || activeConversationId,
        generation: reviewRequest?.pendingGeneration,
        ownerId: reviewRequest?.pendingOwnerId,
      });
      const reviewLockRuntime = { requireLock: productionReviewRequiresWebLocks() };

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
          effectiveSessionId,
          async (event) => {
            const eventRunId = assertReviewRunEvent(
              event,
              reviewRunId,
              requestOptions.conversationId || activeConversationId,
            );
            let terminalDisposition = null;
            let terminalRecord = null;
            if (event.type === "done" && reviewRequest) {
              terminalDisposition = reviewTerminalDisposition({
                isError: event.data.isError,
                cancelled: event.data.cancelled === true,
                recovered: event.data.recovered === true,
                code: event.data.code || event.data.error?.code || null,
                providerAttempted: event.data.providerAttempted
                  ?? event.data.provider_attempted,
                identityBound: true,
              });
              authoritativeDone = true;
              terminalRecord = await markPendingReviewTerminalExclusive({
                ...reviewCasIdentity(),
                terminal: {
                  isError: event.data.isError,
                  cancelled: event.data.cancelled === true,
                  recovered: event.data.recovered === true,
                  code: event.data.code || event.data.error?.code || null,
                  providerAttempted: event.data.providerAttempted
                    ?? event.data.provider_attempted
                    ?? null,
                },
              }, reviewLockRuntime);
              reviewRequest = {
                ...reviewRequest,
                pendingGeneration: terminalRecord.generation,
                pendingOwnerId: terminalRecord.ownerId,
                pendingState: terminalRecord.state,
                terminalReceipt: terminalRecord.terminalReceipt,
              };
            }
            const runSessionId = event.type === "run_start" ? event.data?.sessionId : null;
            if ((eventRunId || runSessionId) && activeRunRef.current?.assistantId === assistantId) {
              activeRunRef.current = {
                ...activeRunRef.current,
                ...(eventRunId ? { runId: eventRunId } : {}),
                // run_start is the server-authoritative process scope for loop requests.
                ...(runSessionId ? { sessionId: runSessionId } : {}),
              };
            }
            let settleTerminalPersistence;
            let failTerminalPersistence;
            let terminalPersistenceSettled = false;
            const terminalPersistence = terminalRecord
              ? new Promise((resolve, reject) => {
                  settleTerminalPersistence = resolve;
                  failTerminalPersistence = reject;
                })
              : null;
            setMessages((prev) => {
              const updated = [...prev];
              const idx = updated.findIndex((m) => m.id === assistantId);
              if (idx === -1) {
                if (failTerminalPersistence && !terminalPersistenceSettled) {
                  terminalPersistenceSettled = true;
                  failTerminalPersistence(terminalUiPersistenceError(
                    "The Review assistant message no longer exists",
                  ));
                }
                return prev;
              }
              let msg = { ...updated[idx] };
              if (event.type !== "text" && _textBuf) {
                const result = appendTextDeltaToMessage(msg, _textBuf);
                _textBuf = "";
                _rafPending = false;
                if (result.changed) msg = result.message;
              }
              // Clear waiting flag on first real event
              if (msg.waiting && (event.type === "text" || event.type === "tool_start" || event.type === "system" || event.type === "retrieval")) {
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
                case "retrieval": {
                  const data = event.data || {};
                  if (data.phase === "start" && !data.status) break;
                  const status = data.status
                    || (data.phase === "error" ? "blocked" : data.phase === "done" ? "ready" : "unknown");
                  const block = {
                    type: "asset_retrieval",
                    status,
                    mode: data.mode,
                    degradedMode: data.degraded_mode,
                    snapshotRevision: data.snapshot_revision,
                    reason: data.reason,
                    code: data.code,
                    message: data.message,
                  };
                  const blocks = [...(msg.blocks || [])];
                  const blockIndex = blocks.findIndex((item) => item.type === "asset_retrieval");
                  if (blockIndex >= 0) blocks[blockIndex] = block;
                  else blocks.push(block);
                  msg.blocks = blocks;
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
                case "intent_start":
                case "intent_updated":
                  break;
                case "round_start": {
                  msg.blocks = [...(msg.blocks || []), { type: "round_header", round: event.data.round, total: event.data.max || event.data.total }];
                  break;
                }
                case "critic_verdict": {
                  msg.blocks = [...(msg.blocks || []), {
                    type: "critic",
                    round: event.data.round,
                    status: event.data.status,
                    issues: event.data.issues,
                    suggestions: event.data.suggestions,
                    provider: event.data.provider,
                    model: event.data.model,
                    error: event.data.error,
                  }];
                  msg.reviewEvidence = mergeReviewEvidence(
                    msg.reviewEvidence,
                    event.data,
                    API_BASE,
                    requestOptions.conversationId || activeConversationId,
                  );
                  msg.reviewAccounting = mergeReviewAccounting(msg.reviewAccounting, event.data, "critic");
                  break;
                }
                case "multi_shots":
                case "review_evidence":
                case "visual_evidence": {
                  msg.reviewEvidence = mergeReviewEvidence(
                    msg.reviewEvidence,
                    event.data,
                    API_BASE,
                    requestOptions.conversationId || activeConversationId,
                  );
                  break;
                }
                case "builder_done": {
                  msg.reviewAccounting = mergeReviewAccounting(msg.reviewAccounting, event.data, "builder");
                  if (event.data && event.data.isError) {
                    msg.blocks = [...(msg.blocks || []), { type: "text", content: `[builder error] ${event.data.error || ""}` }];
                  }
                  break;
                }
                case "loop_done": {
                  msg.reviewAccounting = mergeReviewAccounting(msg.reviewAccounting, event.data, "run");
                  msg.blocks = [...(msg.blocks || []), {
                    type: "loop_done",
                    reason: event.data.reason,
                    rounds: event.data.rounds,
                    finalStatus: event.data.finalStatus,
                    error: event.data.error,
                  }];
                  break;
                }
                case "done": {
                  authoritativeDone = true;
                  msg.reviewAccounting = mergeReviewAccounting(msg.reviewAccounting, event.data, "run");
                  msg.reviewEvidence = mergeReviewEvidence(
                    msg.reviewEvidence,
                    event.data,
                    API_BASE,
                    requestOptions.conversationId || activeConversationId,
                  );
                  const sid = event.data.sessionId;
                  const isErr = event.data.isError;
                  const code = event.data.code || event.data.error?.code || null;
                  const recovered = event.data.recovered === true;
                  const cancelled = event.data.cancelled === true;
                  const disposition = terminalDisposition || reviewTerminalDisposition({
                    isError: isErr,
                    cancelled,
                    recovered,
                    code,
                    providerAttempted: event.data.providerAttempted
                      ?? event.data.provider_attempted,
                    identityBound: Boolean(reviewRequest),
                  });
                  const sameIdRetry = disposition.sameIdRetry;
                  msg.waiting = false;
                  if (msg.reviewRequest) {
                    msg.reviewRequest = reviewRequest;
                    if (terminalRecord) {
                      msg.reviewTerminalReceiptId = terminalRecord.terminalReceipt.receiptId;
                    }
                    msg.reviewRetryAvailable = sameIdRetry;
                    msg.reviewStartNewAvailable = disposition.startNew;
                    msg.reviewResult = {
                      code,
                      retryable: event.data.retryable === true || sameIdRetry,
                      recovered,
                      cancelled,
                    };
                  }
                  if (event.data.loop && !(msg.blocks || []).some((block) => block.type === "loop_done")) {
                    msg.blocks = [...(msg.blocks || []), {
                      type: "loop_done",
                      reason: event.data.loop.reason,
                      rounds: event.data.loop.rounds,
                      finalStatus: event.data.loop.finalStatus,
                      error: event.data.error,
                      recovered,
                    }];
                  }
                  // Always keep sessionId - it's the stable studio session, not Claude's transient one
                  if (sid) {
                    setSessionId(sid);
                    onSessionChange?.(sid);
                  }
                  const screenshot = event.data.latestScreenshot
                    || (Array.isArray(msg.reviewEvidence) ? msg.reviewEvidence.at(-1)?.url : null);
                  if (screenshot) {
                    onScreenshotUpdate(screenshot);
                    setLatestScreenshot(screenshot);
                  }
                  // If no content was streamed at all, show fallback but keep session
                  const hasResultBlock = (msg.blocks || []).some((block) => block.type !== "text" || block.content);
                  if (!msg.content && (!msg.toolCalls || msg.toolCalls.length === 0) && !hasResultBlock) {
                    msg.content = cancelled
                      ? "Review was cancelled. You can start a new Review."
                      : (isErr
                        ? (disposition.sameIdRetry
                          ? "Review finished with an error. Retry the same Review ID to reconcile it safely."
                          : (disposition.startNew
                            ? "Review was blocked before the provider attempt. You can start a new Review."
                            : "Review finished with an error."))
                        : "Review completed.");
                  }
                  break;
                }
              }

              updated[idx] = msg;
              if (terminalRecord && !terminalPersistenceSettled) {
                try {
                  persistTerminalConversation({
                    conversations,
                    activeConversationId: requestOptions.conversationId || activeConversationId,
                    messages: updated,
                    assistantId,
                    runId: reviewRequest.runId,
                    receiptId: terminalRecord.terminalReceipt.receiptId,
                  });
                  terminalPersistenceSettled = true;
                  settleTerminalPersistence();
                } catch (error) {
                  terminalPersistenceSettled = true;
                  failTerminalPersistence(error);
                }
              }
              return updated;
            });
            if (terminalPersistence) {
              await terminalPersistence;
              const terminalIdentity = {
                ...reviewCasIdentity(),
                receiptId: terminalRecord.terminalReceipt.receiptId,
              };
              if (!persistedTerminalMessageMatches({
                conversationId: terminalIdentity.conversationId,
                assistantId,
                runId: terminalIdentity.runId,
                receiptId: terminalIdentity.receiptId,
              })) {
                throw terminalUiPersistenceError(
                  "The persisted Review terminal message changed before recovery could clear",
                );
              }
              if (!terminalDisposition.keepPending) {
                await clearTerminalReviewExclusive(terminalIdentity, reviewLockRuntime);
              }
              onChatDone?.();
            } else if (event.type === "done") {
              onChatDone?.();
            }
          },
          controller.signal,
          requestOptions
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
        if (err instanceof Error) {
          let effectiveError = err;
          const expectedConversationId = requestOptions.conversationId || activeConversationId;
          if (reviewRequest && err.runId && err.runId !== reviewRequest.runId) {
            err.code = "REVIEW_RUN_ID_MISMATCH";
            err.retryable = true;
          }
          if (reviewRequest && err.conversationId
              && err.conversationId !== expectedConversationId) {
            err.code = "REVIEW_RUN_ID_MISMATCH";
            err.retryable = true;
          }
          const identityBound = Boolean(
            reviewRequest
            && err.runId === reviewRequest.runId
            && err.conversationId === expectedConversationId,
          );
          let disposition = reviewFailureDisposition({
            hasReview: Boolean(reviewRequest),
            authoritativeDone,
            code: err.code,
            status: err.status,
            name: err.name,
            providerAttempted: err.providerAttempted,
            identityBound,
          });
          if (reviewRequest && !disposition.keepPending) {
            try {
              await clearPreProviderReviewExclusive({
                ...reviewCasIdentity(),
                providerAttempted: false,
              }, reviewLockRuntime);
            } catch (clearError) {
              effectiveError = clearError instanceof Error
                ? clearError
                : new Error("Review recovery envelope could not be cleared");
              disposition = reviewFailureDisposition({
                hasReview: true,
                authoritativeDone,
                code: effectiveError.code || "REVIEW_PENDING_CLEAR_FAILED",
              });
            }
          }
          setMessages((prev) => {
            const updated = [...prev];
            const idx = updated.findIndex((m) => m.id === assistantId);
            if (idx !== -1) {
              const sameIdRetry = disposition.sameIdRetry;
              const recoveryBlocked = disposition.recoveryBlocked;
              updated[idx] = {
                ...updated[idx],
                content: updated[idx].content || (recoveryBlocked
                  ? "This Review has an unresolved prior provider attempt. Operator recovery is required before another paid run."
                  : (effectiveError.name === "AbortError"
                    ? "Review delivery was interrupted while cancellation was being confirmed."
                    : `Error: ${effectiveError.message}`)),
                waiting: false,
                reviewRetryAvailable: sameIdRetry,
                reviewStartNewAvailable: disposition.startNew,
                reviewResult: reviewRequest ? {
                  code: effectiveError.code || (effectiveError.name === "AbortError" ? "REVIEW_TRANSPORT_AMBIGUOUS" : "CHAT_HTTP_ERROR"),
                  retryable: sameIdRetry || effectiveError.retryable === true,
                  recovered: effectiveError.recovered === true,
                  recoveryBlocked,
                } : null,
              };
            }
            return updated;
          });
        }
      } finally {
        preparingReviewRef.current = false;
        setPreparingReview(false);
        setLoading(false);
        setAutoSelectingSkills(false);
        abortRef.current = null;
        if (activeRunRef.current?.assistantId === assistantId) activeRunRef.current = null;
        dispatchInFlightRef.current = false;
      }
    },
    [
      activeConversationId,
      autoSkillSelectionEnabled,
      assetPolicy,
      codingAgent,
      codingModel,
      conversations,
      input,
      loading,
      loopMode,
      onChatDone,
      onScreenshotUpdate,
      onSessionChange,
      selectedSkills,
      sessionId,
    ]
  );

  const handleRetryReview = useCallback((reviewRequest) => {
    if (loading || preparingReviewRef.current || dispatchInFlightRef.current
        || !reviewRequest?.runId || !reviewRequest?.prompt) return;
    handleSend(reviewRequest.prompt, undefined, reviewRequest);
  }, [handleSend, loading]);

  const handleStartNewReview = useCallback((reviewRequest) => {
    if (loading || preparingReviewRef.current || dispatchInFlightRef.current
        || !reviewRequest?.prompt) return;
    const nextRequest = { ...reviewRequest, runId: generateReviewRunId(), startNew: true };
    handleSend(nextRequest.prompt, undefined, nextRequest);
  }, [handleSend, loading]);

  const requestChatStop = useCallback(() => {
    const payload = buildChatStopPayload(activeRunRef.current, sessionIdRef.current);
    return fetch(`${API_BASE}/chat-stop`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).catch(() => null);
  }, []);

  const handleStop = () => {
    if (preparingReviewRef.current) return;
    // Tell the server to actually kill the Claude subprocess. Without this,
    // aborting the SSE alone just leaves the agent running in background
    // (server-side e.on("close") no longer kills on disconnect).
    requestChatStop();
    abortRef.current?.abort();
    activeRunRef.current = null;
    setLoading(false);
  };

  const handleKeyDown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleReset = () => {
    if (preparingReviewRef.current || dispatchInFlightRef.current) return;
    if (pendingReviewRecordExists()) {
      setMessages((prev) => [...prev, {
        id: generateMessageId(),
        role: "assistant",
        content: "Session reset is blocked while a Review recovery record is unresolved. Resume or reconcile that run first.",
        timestamp: Date.now(),
      }]);
      return;
    }
    requestChatStop();
    abortRef.current?.abort();
    activeRunRef.current = null;
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
    if (!selfEvolutionReady || preparingReviewRef.current || dispatchInFlightRef.current) return;
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
        loopMode,
        assetPolicy,
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
  const sortedConversations = useMemo(
    () => [...conversations].sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0)),
    [conversations]
  );

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

      <div className="chat-panel-layout">
        <div className="chat-main-column">

      <SceneAgentHeader
        agentLabelText={agentLabel(codingAgent)}
        icons={icons}
        mcpStatus={mcpStatus}
        selfEvolutionReady={selfEvolutionReady}
        selfEvolutionOn={selfEvolutionOn}
        sessionId={sessionId}
        turnCount={turnCount}
        latestScreenshot={latestScreenshot}
        loading={loading || preparingReview}
        onToggleSelfEvolution={handleSelfEvolutionToggle}
        onAnnotate={() => setAnnotating(true)}
        onSave={handleSave}
        onShare={handleShare}
        onStop={handleStop}
        onReset={handleReset}
        onOpenSessions={() => setSessionsOpen(true)}
        onNewConversation={handleNewConversation}
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

      {/* Operation history */}
      <div className="chat-record-list">
        {messages.map((msg) => {
          const ckpt = checkpoints.find((c) => c.messageId === msg.id);
          return (
            <React.Fragment key={msg.id}>
              <ChatMessage
                message={msg}
                agentLabel={agentLabel(codingAgent)}
                toolIcons={toolIcons}
                fallbackToolIcon={icons?.hammer}
                onRetry={msg.reviewRetryAvailable && !loading && !preparingReview ? handleRetryReview : null}
                onStartNew={msg.reviewStartNewAvailable && !loading && !preparingReview ? handleStartNewReview : null}
              />
              <ReviewEvidenceGallery evidence={msg.reviewEvidence} />
              {ckpt && (
                <CheckpointBar
                  checkpoint={ckpt}
                  checkpoints={checkpoints}
                  activeLeafId={activeLeafId}
                  icons={icons}
                  restoring={restoringId === ckpt.id || preparingReview}
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
      {!loading && !preparingReview && sessionId && turnCount > 0 && (
        <div className="chat-quick-actions">
          <span className="chat-quick-label">COMMON REVISIONS</span>
          {QUICK_SUGGESTIONS.map((suggestion) => (
            <button
              key={suggestion}
              onClick={() => handleSend(suggestion)}
              className="chat-quick-action"
              type="button"
            >
              {suggestion}
            </button>
          ))}
        </div>
      )}

      {/* Input area */}
      <div className="chat-command-area">
        <div className="chat-command-box">
          <textarea
            ref={textareaRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={
              sessionId
                ? "Enter a scene revision command..."
                : "Enter scene requirements, constraints, and camera needs..."
            }
            disabled={loading || preparingReview}
            rows={1}
            className="chat-command-input"
          />
          <button
            onClick={() => (loading ? handleStop() : preparingReview ? undefined : handleSend())}
            disabled={preparingReview || (!loading && !input.trim())}
            className={`chat-command-submit${loading ? " stop" : input.trim() ? " ready" : ""}`}
            title={loading ? "Stop operation" : preparingReview ? "Preparing Review recovery" : "Run command"}
            type="button"
          >
            {loading ? icons?.close?.(15) : icons?.activity?.(15)}
          </button>
        </div>
        <div className="chat-command-meta">
          Enter to run · Shift+Enter for new line
          {/* Execution backend selection is available in Settings. */}
          <span className={autoSkillSelectionEnabled ? "active" : ""}>
            Procedures: {autoSkillSelectionEnabled ? "Automatic" : "Manual"}
            {autoSelectingSkills ? " (selecting...)" : ""}
          </span>
          <span
            onClick={() => {
              if (!loading && !preparingReviewRef.current && !dispatchInFlightRef.current) {
                setLoopMode((m) => (m === "vanilla" ? "text_loop" : m === "text_loop" ? "visual_loop" : "vanilla"));
              }
            }}
            className={loopMode === "vanilla" ? "" : "active"}
            title="Verification mode — click to cycle: off → text → visual"
          >
            Review: {loopMode === "vanilla" ? "Off" : loopMode === "text_loop" ? "Text" : "Visual"}
          </span>
          <span
            onClick={() => {
              if (!loading && !preparingReviewRef.current && !dispatchInFlightRef.current) {
                setAssetPolicy((policy) => policy === "real_assets" ? "basic_geometry" : "real_assets");
              }
            }}
            className={assetPolicy === "real_assets" ? "active" : ""}
            title={assetPolicy === "real_assets"
              ? "Require retrieved 3D assets; block the build when retrieval is unavailable"
              : "Basic geometry fallback is explicitly allowed for this conversation"}
          >
            Assets: {assetPolicy === "real_assets" ? "Verified only" : "Fallback allowed"}
          </span>
          {activeSkills.length > 0 && (
            <span className="active">
              {activeSkills.length} procedure{activeSkills.length > 1 ? "s" : ""}
            </span>
          )}
          {autoSelectionError && (
            <span style={{ color: "var(--red)", marginLeft: 8 }}>{autoSelectionError}</span>
          )}
        </div>
        </div>
        </div>

        {sessionsOpen && (
          <>
            <button
              className="chat-session-scrim"
              aria-label="Close build sessions"
              onClick={() => setSessionsOpen(false)}
              type="button"
            />
            <aside className="chat-session-drawer" aria-label="Build sessions">
              <div className="chat-session-drawer-head">
                <div>
                  <span>Build Sessions</span>
                  <small>{sortedConversations.length} saved</small>
                </div>
                <button
                  className="chat-session-new"
                  onClick={handleNewConversation}
                  disabled={loading || preparingReview}
                  title="New build session"
                  type="button"
                >
                  {icons.plus?.(13)}
                </button>
                <button
                  className="chat-session-close"
                  onClick={() => setSessionsOpen(false)}
                  title="Close build sessions"
                  type="button"
                >
                  {icons.close?.(12)}
                </button>
              </div>
              <div className="chat-session-list">
                {sortedConversations.map((conversation) => (
                  <div
                    key={conversation.id}
                    className={`chat-session-item${conversation.id === activeConversationId ? " active" : ""}`}
                  >
                    <button
                      className="chat-session-select"
                      onClick={() => handleSelectConversation(conversation.id)}
                      disabled={loading || preparingReview}
                      title={conversation.title}
                      type="button"
                    >
                      <span className="chat-session-title">{conversation.title}</span>
                      <span className="chat-session-meta">
                        {conversation.state?.turnCount || 0} operations
                      </span>
                    </button>
                    <button
                      className="chat-session-delete"
                      onClick={() => handleDeleteConversation(conversation.id)}
                      disabled={loading || preparingReview}
                      title="Delete conversation"
                      type="button"
                    >
                      {icons.trash?.(12)}
                    </button>
                  </div>
                ))}
              </div>
            </aside>
          </>
        )}
      </div>
    </div>
  );
}

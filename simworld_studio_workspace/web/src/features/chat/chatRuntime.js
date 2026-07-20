const SCENE_TOOLS = new Set([
  "spawn_blueprint_actor",
  "spawn_actor",
  "spawn_agent",
  "delete_actor",
  "delete_all_spawned",
  "setup_environment",
  "set_actor_transform",
  "execute_python_script",
]);

const LOOP_MODES = new Set(["vanilla", "text_loop", "visual_loop"]);

let messageCounter = 0;

export function generateMessageId() {
  messageCounter += 1;
  return `msg-${messageCounter}-${Date.now()}`;
}

export function buildWelcomeMessage() {
  return {
    id: generateMessageId(),
    role: "assistant",
    content: `**Scene workspace ready**

Define the environment using layout, asset, scale, lighting, and camera requirements. Operations and validation results will be recorded below.

Example specifications:
- *Residential block with six houses, tree-lined streets, and a 6 m clear route.*
- *Downtown intersection with defined setbacks and pedestrian crossings.*
- *Public park with benches, perimeter trees, and late-afternoon lighting.*`,
    timestamp: Date.now(),
  };
}

export function normalizeLoopMode(value) {
  return LOOP_MODES.has(value) ? value : "vanilla";
}

export function reviewEvidenceUrls(data, apiBase = "/api") {
  const payload = data && typeof data === "object" ? data : {};
  const directUrls = [
    payload.screenshotUrl,
    ...(Array.isArray(payload.screenshotUrls) ? payload.screenshotUrls : []),
  ];
  const screenshotPrefix = `${String(apiBase || "/api").replace(/\/$/, "")}/screenshot/file?`;
  const urls = [];

  for (const value of directUrls) {
    if (typeof value !== "string" || !value.startsWith(screenshotPrefix)) continue;
    urls.push(value);
  }
  for (const filepath of Array.isArray(payload.paths) ? payload.paths : []) {
    if (typeof filepath !== "string" || !filepath.trim()) continue;
    urls.push(`${screenshotPrefix}path=${encodeURIComponent(filepath)}`);
  }

  return [...new Set(urls)];
}

export function mergeReviewEvidence(current, data, apiBase = "/api") {
  const existing = Array.isArray(current) ? current : [];
  const hasRound = data?.round !== null && data?.round !== undefined;
  const round = hasRound && Number.isFinite(Number(data.round)) ? Number(data.round) : null;
  const next = reviewEvidenceUrls(data, apiBase).map((url) => ({ round, url }));
  const seen = new Set(existing.map((item) => item?.url).filter(Boolean));
  return [
    ...existing,
    ...next.filter((item) => {
      if (seen.has(item.url)) return false;
      seen.add(item.url);
      return true;
    }),
  ];
}

function nonNegativeNumber(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

// Cost events are repeated as a run progresses. Treat the server's latest
// aggregate snapshot as authoritative; never add event values in the browser.
export function mergeReviewAccounting(current, data, stage) {
  const payload = data && typeof data === "object" ? data : {};
  const existing = current && typeof current === "object" ? current : {};
  const budget = payload.budget
    || payload.reviewBudget
    || payload.review_budget
    || payload.loop?.budget
    || payload.review?.budget;
  const next = { ...existing };

  if (budget && typeof budget === "object") {
    const values = {
      limitUsd: nonNegativeNumber(budget.limit_usd ?? budget.limitUsd),
      spentUsd: nonNegativeNumber(budget.spent_usd ?? budget.spentUsd),
      remainingUsd: nonNegativeNumber(budget.remaining_usd ?? budget.remainingUsd),
      builderCostUsd: nonNegativeNumber(
        budget.stages?.builder?.cost_usd ?? budget.stages?.builder?.costUsd,
      ),
      criticCostUsd: nonNegativeNumber(
        budget.stages?.critic?.cost_usd ?? budget.stages?.critic?.costUsd,
      ),
    };
    for (const [key, value] of Object.entries(values)) {
      if (value !== null) next[key] = value;
    }
    if (typeof budget.exhausted === "boolean") next.exhausted = budget.exhausted;
  }

  const eventCost = nonNegativeNumber(payload.cost_usd ?? payload.costUsd);
  if (eventCost !== null && stage === "builder") next.builderCostUsd = eventCost;
  if (eventCost !== null && stage === "critic") next.criticCostUsd = eventCost;

  return Object.keys(next).length ? next : null;
}

export function buildChatStopPayload(activeRun, fallbackSessionId) {
  const payload = {
    sessionId: activeRun?.sessionId || fallbackSessionId || "_global",
  };
  if (activeRun?.runId) payload.runId = activeRun.runId;
  if (activeRun?.conversationId) payload.conversationId = activeRun.conversationId;
  return payload;
}

export function turnChangedScene(msg) {
  return (msg?.toolCalls || []).some((toolCall) => {
    const name = toolCall.displayName || (toolCall.name || "").replace(/^mcp__\w+__/, "");
    return SCENE_TOOLS.has(name) && toolCall.status !== "error";
  });
}

export function screenshotPathFromUrl(url) {
  if (!url || typeof url !== "string") return null;
  try {
    return new URLSearchParams(url.split("?")[1] || "").get("path");
  } catch {
    return null;
  }
}

export function getUniqueTextAppend(existingContent, rawDelta) {
  const existing = existingContent || "";
  const incoming = rawDelta || "";
  if (!incoming) return "";

  if (!existing) return incoming;
  if (incoming.startsWith(existing)) return incoming.slice(existing.length);
  if (existing.endsWith(incoming)) return "";

  const trimmedIncoming = incoming.trim();
  if (trimmedIncoming && existing.trimEnd().endsWith(trimmedIncoming)) return "";

  return incoming;
}

export function appendTextDeltaToMessage(message, rawDelta) {
  const appendText = getUniqueTextAppend(message.content, rawDelta);
  if (!appendText) return { changed: false, message };

  const blocks = message.blocks || [];
  const lastBlock = blocks[blocks.length - 1];
  const nextMessage = {
    ...message,
    content: (message.content || "") + appendText,
  };

  if (lastBlock?.type === "text") {
    nextMessage.blocks = [
      ...blocks.slice(0, -1),
      { ...lastBlock, content: lastBlock.content + appendText },
    ];
  } else {
    nextMessage.blocks = [...blocks, { type: "text", content: appendText }];
  }

  return { changed: true, message: nextMessage };
}

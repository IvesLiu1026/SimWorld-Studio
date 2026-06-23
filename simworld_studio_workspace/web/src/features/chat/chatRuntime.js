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

let messageCounter = 0;

export function generateMessageId() {
  messageCounter += 1;
  return `msg-${messageCounter}-${Date.now()}`;
}

export function buildWelcomeMessage() {
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

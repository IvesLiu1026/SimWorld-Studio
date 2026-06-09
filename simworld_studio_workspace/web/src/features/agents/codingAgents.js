export const AGENT_LABELS = {
  claude: "Claude Code",
  codex: "Codex",
  opencode: "OpenCode",
  gemini: "Gemini CLI",
  cursor: "Cursor",
  grok: "Grok Build",
};

export function agentLabel(agentId) {
  return AGENT_LABELS[agentId] || agentId || "Code Agent";
}

export const DEFAULT_CODING_AGENTS = {
  claude: {
    label: "Claude Code",
    defaultModel: "",
    models: ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5-20251001", "claude-fable-5"],
  },
  codex: {
    label: "Codex",
    defaultModel: "",
    models: ["gpt-5-codex", "gpt-5", "o3"],
  },
  opencode: {
    label: "OpenCode",
    defaultModel: "",
    models: ["anthropic/claude-opus-4-8", "openai/gpt-5", "openai/gpt-4o", "google/gemini-2.5-pro"],
  },
  gemini: {
    label: "Gemini CLI",
    defaultModel: "",
    models: ["gemini-2.5-pro", "gemini-2.5-flash"],
  },
  cursor: {
    label: "Cursor",
    defaultModel: "composer-2.5",
    models: ["composer-2.5", "composer-2.5-fast", "claude-opus-4-8-thinking-high", "gpt-5.5-high"],
  },
  grok: {
    label: "Grok Build",
    defaultModel: "grok-composer-2.5-fast",
    models: ["grok-composer-2.5-fast", "grok-build"],
  },
};

export function codingModelStorageKey(agentId) {
  return `simworld.codingModel.${agentId || "claude"}`;
}

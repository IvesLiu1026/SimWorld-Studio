import { create } from "zustand";
import { codingModelStorageKey, DEFAULT_CODING_AGENTS } from "../features/agents/codingAgents.js";

function readStorage(key, fallback) {
  try {
    return localStorage.getItem(key) || fallback;
  } catch {
    return fallback;
  }
}

function writeStorage(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {}
}

function initialCodingAgent() {
  return readStorage("simworld.codingAgent", "claude");
}

const defaultCodingAgent = initialCodingAgent();

export const useStudioStore = create((set, get) => ({
  uiTheme: readStorage("sw_ui_theme", "dark"),
  studioMode: readStorage("sw_studio_mode", "scene"),
  topSection: "studio",
  showSettings: false,
  codingAgent: defaultCodingAgent,
  codingModel: readStorage(codingModelStorageKey(defaultCodingAgent), ""),

  setUiTheme: (uiTheme) => {
    writeStorage("sw_ui_theme", uiTheme);
    set({ uiTheme });
  },

  setStudioMode: (studioMode) => {
    writeStorage("sw_studio_mode", studioMode);
    set({ studioMode });
  },

  setTopSection: (topSection) => set({ topSection }),
  setShowSettings: (showSettings) => set({ showSettings }),

  setCodingModel: (codingModel) => {
    const { codingAgent } = get();
    writeStorage(codingModelStorageKey(codingAgent), codingModel);
    set({ codingModel });
  },

  setCodingAgent: (codingAgent, registry = DEFAULT_CODING_AGENTS) => {
    writeStorage("simworld.codingAgent", codingAgent);
    const savedModel = readStorage(codingModelStorageKey(codingAgent), "");
    const codingModel = savedModel || registry[codingAgent]?.defaultModel || "";
    set({ codingAgent, codingModel });
  },

  openStudioMode: (studioMode) => {
    writeStorage("sw_studio_mode", studioMode);
    set({ studioMode, topSection: "studio" });
  },
}));

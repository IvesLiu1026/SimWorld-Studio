import { apiJson } from "./client.js";

export function fetchHealth() {
  return apiJson("/health").then((health) => ({
    ueConnected: !!health?.ueConnected,
    mcpConnected: !!health?.mcpConnected,
    pixelStreamingUrl: health?.pixelStreamingUrl || null,
    engineVersion: health?.engineVersion || null,
    engineLabel: health?.engineLabel || null,
  }));
}

export function fetchCodingAgents() {
  return apiJson("/coding-agents");
}

export function fetchSession() {
  return apiJson("/session");
}

export const studioQueryKeys = {
  health: ["studio", "health"],
  codingAgents: ["studio", "coding-agents"],
  session: ["studio", "session"],
};

import { API_BASE } from "./client.js";

export async function fetchSkills() {
  return (await fetch(`${API_BASE}/skills`)).json();
}

export async function fetchSkillDetails(id) {
  return (await fetch(`${API_BASE}/skills/${id}`)).json();
}

export async function createSkill(skill) {
  return (
    await fetch(`${API_BASE}/skills`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(skill),
    })
  ).json();
}

export async function deleteSkill(id) {
  await fetch(`${API_BASE}/skills/${id}`, { method: "DELETE" });
}

export async function saveScene(scene) {
  return (
    await fetch(`${API_BASE}/scenes`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(scene),
    })
  ).json();
}

export async function listCheckpoints(sessionId) {
  const response = await fetch(`${API_BASE}/checkpoints?sessionId=${encodeURIComponent(sessionId)}`);
  return (await response.json()).checkpoints || [];
}

export async function createCheckpoint(body) {
  const response = await fetch(`${API_BASE}/checkpoints`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error("checkpoint create failed");
  return response.json();
}

export async function restoreCheckpoint(sessionId, id) {
  const response = await fetch(`${API_BASE}/checkpoints/${encodeURIComponent(sessionId)}/${id}/restore`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
  if (!response.ok) throw new Error("restore failed");
  return response.json();
}

export async function getCheckpoint(sessionId, id) {
  const response = await fetch(`${API_BASE}/checkpoints/${encodeURIComponent(sessionId)}/${id}`);
  return response.ok ? response.json() : null;
}

export async function clearCheckpoints(sessionId) {
  await fetch(`${API_BASE}/checkpoints/${encodeURIComponent(sessionId)}`, { method: "DELETE" }).catch(() => {});
}

export async function resetScene() {
  await fetch(`${API_BASE}/scene/reset`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  }).catch(() => {});
}

export async function sendChat(message, sessionId, onEvent, signal, options) {
  const controller = signal ? undefined : new AbortController();
  const effectiveSignal = signal || controller?.signal;
  const connectTimeout = setTimeout(() => controller?.abort(), 30000);

  let response;
  try {
    response = await fetch(`${API_BASE}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message,
        sessionId,
        skills: options?.skills,
        feedback: options?.feedback,
        skillSelectionMode: options?.skillSelectionMode,
        agent: options?.agent,
        model: options?.model,
      }),
      signal: effectiveSignal,
    });
  } finally {
    clearTimeout(connectTimeout);
  }

  if (!response.ok || !response.body) {
    throw new Error(`Server error: ${response.status}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let lastDataTime = Date.now();
  const idleTimeoutMs = 120000;

  for (;;) {
    const readPromise = reader.read();
    const timeoutPromise = new Promise((_, reject) => {
      const check = setInterval(() => {
        if (Date.now() - lastDataTime > idleTimeoutMs) {
          clearInterval(check);
          reader.cancel();
          reject(new Error("Connection idle timeout"));
        }
      }, 5000);
      readPromise.then(() => clearInterval(check)).catch(() => clearInterval(check));
    });

    let result;
    try {
      result = await Promise.race([readPromise, timeoutPromise]);
    } catch {
      onEvent({ type: "done", data: { sessionId: null, isError: true, latestScreenshot: null } });
      break;
    }

    const { done, value } = result;
    if (done) break;

    lastDataTime = Date.now();
    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() ?? "";

    for (const chunk of chunks) {
      const lines = chunk.split("\n");
      let eventType = "message";
      let data = "";

      for (const line of lines) {
        if (line.startsWith("event: ")) eventType = line.slice(7).trim();
        if (line.startsWith("data: ")) data = line.slice(6);
      }

      if (data) {
        try {
          const parsed = JSON.parse(data);
          onEvent({ type: eventType, data: parsed });
        } catch {}
      }
    }
  }
}

export async function voteOnBattle(battleId, winner) {
  return (
    await fetch(`${API_BASE}/arena/battles/${battleId}/vote`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ winner }),
    })
  ).json();
}

export async function fetchLeaderboard() {
  return (await fetch(`${API_BASE}/arena/leaderboard`)).json();
}

export async function fetchGallery(options) {
  const params = new URLSearchParams();
  params.set("limit", String(options.limit));
  if (options?.offset) params.set("offset", String(options.offset));
  if (options?.sort) params.set("sort", options.sort);

  const query = params.toString() ? `?${params}` : "";
  const result = await (await fetch(`${API_BASE}/arena/gallery${query}`)).json();
  return Array.isArray(result) ? result : result.items || [];
}

export async function shareToGallery(item) {
  return (
    await fetch(`${API_BASE}/arena/gallery`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(item),
    })
  ).json();
}

export async function fetchAgents() {
  return (await fetch(`${API_BASE}/agents`)).json();
}

export async function updateAgent(id, settings) {
  return (
    await fetch(`${API_BASE}/agents/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(settings),
    })
  ).json();
}

export async function trackAgent(name) {
  return fetch(`${API_BASE}/agent-track`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
}

export async function sendAgentChat(agentName, message, sessionId, onEvent, signal) {
  const response = await fetch(`${API_BASE}/agent-chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ agentName, message, sessionId }),
    signal,
  });
  if (!response.ok || !response.body) {
    const error = await response.json().catch(() => ({ error: response.statusText }));
    throw new Error(error.error || `Server error: ${response.status}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let lastDataTime = Date.now();
  const idleTimeoutMs = 210000;

  for (;;) {
    const readPromise = reader.read();
    const timeoutPromise = new Promise((_, reject) => {
      const check = setInterval(() => {
        if (Date.now() - lastDataTime > idleTimeoutMs) {
          clearInterval(check);
          reader.cancel();
          reject(new Error("Agent response timeout"));
        }
      }, 5000);
      readPromise.then(() => clearInterval(check)).catch(() => clearInterval(check));
    });

    let result;
    try {
      result = await Promise.race([readPromise, timeoutPromise]);
    } catch {
      onEvent({ type: "done", data: { isError: true, text: "Agent timed out." } });
      break;
    }

    const { done, value } = result;
    if (done) break;

    lastDataTime = Date.now();
    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() ?? "";

    for (const chunk of chunks) {
      const lines = chunk.split("\n");
      let eventType = "message";
      let data = "";
      for (const line of lines) {
        if (line.startsWith("event: ")) eventType = line.slice(7).trim();
        if (line.startsWith("data: ")) data = line.slice(6);
      }
      if (data) {
        try {
          onEvent({ type: eventType, data: JSON.parse(data) });
        } catch {}
      }
    }
  }
}

export async function stopAgent(agentName) {
  return fetch(`${API_BASE}/agent-stop`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ agentName }),
  }).catch(() => {});
}

export async function fetchAgentTrajectory(agentName) {
  return fetch(`${API_BASE}/agent-trajectory/${encodeURIComponent(agentName)}`)
    .then((response) => response.json())
    .then((data) => data.trajectory || []);
}

export async function fetchAgentCamera(agentName) {
  return fetch(`${API_BASE}/agent-camera/${encodeURIComponent(agentName)}`).then((response) => response.json());
}

export async function fetchPieStatus() {
  return fetch(`${API_BASE}/pie-status`).then((response) => response.json());
}

export async function startPie() {
  return fetch(`${API_BASE}/pie-start`, { method: "POST" });
}

export async function postChatCommand(message, sessionId) {
  return fetch(`${API_BASE}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, sessionId }),
  });
}

export async function broadcastAgentMessage(text, target = "all") {
  return fetch(`${API_BASE}/agent-broadcast`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, target }),
  });
}

export async function stopAllAgents() {
  return fetch(`${API_BASE}/agent-stop-all`, { method: "POST" }).catch(() => {});
}

export async function captureContextSnapshot() {
  return fetch(`${API_BASE}/context-snapshot`, { method: "POST" }).catch(() => {});
}

export async function discoverAgents() {
  return fetch(`${API_BASE}/agent-discover`, { method: "POST" }).catch(() => {});
}

export async function refreshAgentContext() {
  return Promise.allSettled([captureContextSnapshot(), discoverAgents()]);
}

export async function runSceneCheck() {
  const response = await fetch(`${API_BASE}/scene-check`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  return response.json();
}

export async function fetchLatestScreenshotDataUrl() {
  const response = await fetch(`${API_BASE}/screenshot/latest?t=${Date.now()}`);
  if (!response.ok) throw new Error("No screenshot");
  const blob = await response.blob();
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.readAsDataURL(blob);
  });
}

export async function scoreSceneWithVlm(imageDataUrl, sessionId) {
  return (
    await fetch(`${API_BASE}/vlm-score`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ imageDataUrl, sessionId }),
    })
  ).json();
}

export async function scoreLatestScreenshot(sessionId) {
  const imageDataUrl = await fetchLatestScreenshotDataUrl();
  const result = await scoreSceneWithVlm(imageDataUrl, sessionId);
  return { ...result, imageDataUrl };
}

export async function runArena(prompt, skills, onEvent, signal) {
  const response = await fetch(`${API_BASE}/arena/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt, skills }),
    signal,
  });

  if (!response.ok || !response.body) {
    throw new Error(`Server error: ${response.status}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() ?? "";

    for (const chunk of chunks) {
      const lines = chunk.split("\n");
      let eventType = "message";
      let data = "";

      for (const line of lines) {
        if (line.startsWith("event: ")) eventType = line.slice(7).trim();
        if (line.startsWith("data: ")) data = line.slice(6);
      }

      if (data) {
        try {
          const parsed = JSON.parse(data);
          onEvent(eventType, parsed);
        } catch {}
      }
    }
  }
}

export async function fetchTools() {
  return (await fetch(`${API_BASE}/tools`)).json();
}

export async function fetchEvolutionConfig() {
  return (await fetch(`${API_BASE}/evolution/config`)).json();
}

export async function updateEvolutionConfig(enabled) {
  return (
    await fetch(`${API_BASE}/evolution/config`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: Boolean(enabled), source: "scene_agent_toggle" }),
    })
  ).json();
}

export async function updateToolProcedure(id, patch) {
  return (
    await fetch(`${API_BASE}/tools/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    })
  ).json();
}

export async function deleteToolProcedure(id) {
  return (
    await fetch(`${API_BASE}/tools/${id}`, {
      method: "DELETE",
    })
  ).json();
}

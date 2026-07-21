import { useEffect, useRef, useState } from "react";
import { API_BASE } from "../api/client.js";

const LEGACY_SESSION_STORAGE_KEY = "sw_session_token";
const HEARTBEAT_MS = 60_000;
const WARN_SECS = 5 * 60;

function removeLegacySessionToken() {
  try {
    sessionStorage.removeItem(LEGACY_SESSION_STORAGE_KEY);
  } catch {}
}

export async function clearSessionToken() {
  removeLegacySessionToken();
  try {
    await fetch(`${API_BASE}/session/release`, {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      keepalive: true,
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
  } catch {}
}

async function postSession(path) {
  const response = await fetch(`${API_BASE}/session/${path}`, {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch {}
  return { response, payload };
}

export function useSession() {
  const [session, setSession] = useState(null);
  const [poolFull, setPoolFull] = useState(null);
  const [secsLeft, setSecsLeft] = useState(null);
  const [expired, setExpired] = useState(false);
  const lastHeartbeatAt = useRef(null);
  const ttlMsRef = useRef(0);

  useEffect(() => {
    let cancelled = false;
    removeLegacySessionToken();

    async function init() {
      try {
        let result = await postSession("heartbeat");
        if (!result.response.ok) result = await postSession("acquire");
        if (cancelled) return;

        const { response, payload } = result;
        if (!response.ok || !payload || payload.error) {
          setPoolFull({
            message: payload?.error || "Studio session is unavailable",
            queueLength: payload?.queueLength,
          });
          return;
        }

        if (payload.dev) {
          setSession({ dev: true, slotId: payload.slotId ?? 0 });
          return;
        }
        if (payload.schema !== "studio-session/v2" && payload.schema !== "studio-session-heartbeat/v2") {
          throw new Error("Invalid Studio session response");
        }
        lastHeartbeatAt.current = Date.now();
        ttlMsRef.current = payload.sessionTtlMs || 30 * 60 * 1000;
        setSession({ ...payload, dev: false });
      } catch (error) {
        if (!cancelled) {
          setPoolFull({ message: error.message || "Studio session is unavailable" });
        }
      }
    }

    init();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!session || session.dev) return undefined;
    const interval = setInterval(async () => {
      try {
        const { response, payload } = await postSession("heartbeat");
        if (!response.ok || !payload?.ok) {
          setExpired(true);
          return;
        }
        lastHeartbeatAt.current = Date.now();
        if (payload.sessionTtlMs) ttlMsRef.current = payload.sessionTtlMs;
      } catch {}
    }, HEARTBEAT_MS);
    return () => clearInterval(interval);
  }, [session]);

  useEffect(() => {
    if (!session || session.dev || !lastHeartbeatAt.current || !ttlMsRef.current) return undefined;
    const interval = setInterval(() => {
      const left = Math.max(0, ttlMsRef.current - (Date.now() - lastHeartbeatAt.current));
      setSecsLeft(Math.floor(left / 1000));
      if (left === 0) setExpired(true);
    }, 1000);
    return () => clearInterval(interval);
  }, [session]);

  return {
    session,
    poolFull,
    secsLeft,
    expired,
    isLoading: session === null && !poolFull,
    warningSoon: secsLeft !== null && secsLeft < WARN_SECS,
  };
}

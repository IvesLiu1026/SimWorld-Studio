import { useEffect, useRef, useState } from "react";
import { API_BASE } from "../api/client.js";

const SESSION_STORAGE_KEY = "sw_session_token";
const HEARTBEAT_MS = 60_000;
const WARN_SECS = 5 * 60;

export function clearSessionToken() {
  try {
    sessionStorage.removeItem(SESSION_STORAGE_KEY);
  } catch {}
}

export function useSession() {
  const [session, setSession] = useState(null);
  const [poolFull, setPoolFull] = useState(null);
  const [secsLeft, setSecsLeft] = useState(null);
  const [expired, setExpired] = useState(false);
  const acquiredAt = useRef(null);
  const ttlMsRef = useRef(0);

  useEffect(() => {
    let cancelled = false;

    async function init() {
      const saved = sessionStorage.getItem(SESSION_STORAGE_KEY);
      if (saved && saved !== "_dev") {
        try {
          const response = await fetch(`${API_BASE}/session/heartbeat`, {
            method: "POST",
            headers: { "Content-Type": "application/json", "x-session-token": saved },
            body: "{}",
          });
          const payload = await response.json();
          if (payload.ok && !cancelled) {
            acquiredAt.current = Date.now() - (payload.idleMs || 0);
            setSession({ token: saved, dev: false });
            return;
          }
        } catch {}
        clearSessionToken();
      }

      try {
        const response = await fetch(`${API_BASE}/session/acquire`, { method: "POST" });
        const payload = await response.json();
        if (cancelled) return;

        if (payload.code === "POOL_FULL" || (payload.error && !payload.token)) {
          setPoolFull({
            message: payload.error || "Server at capacity",
            queueLength: payload.queueLength,
          });
          return;
        }

        if (payload.dev) {
          setSession({ token: "_dev", dev: true });
          return;
        }

        sessionStorage.setItem(SESSION_STORAGE_KEY, payload.token);
        acquiredAt.current = Date.now();
        ttlMsRef.current = payload.sessionTtlMs || 30 * 60 * 1000;
        setSession(payload);
      } catch {
        if (!cancelled) setSession({ token: "_dev", dev: true });
      }
    }

    init();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!session || session.dev) return;
    const interval = setInterval(async () => {
      try {
        const response = await fetch(`${API_BASE}/session/heartbeat`, {
          method: "POST",
          headers: { "Content-Type": "application/json", "x-session-token": session.token },
          body: "{}",
        });
        const payload = await response.json();
        if (!payload.ok) setExpired(true);
      } catch {}
    }, HEARTBEAT_MS);
    return () => clearInterval(interval);
  }, [session]);

  useEffect(() => {
    if (!session || session.dev || !acquiredAt.current || !ttlMsRef.current) return;
    const interval = setInterval(() => {
      const left = Math.max(0, ttlMsRef.current - (Date.now() - acquiredAt.current));
      setSecsLeft(Math.floor(left / 1000));
      if (left === 0) {
        setExpired(true);
        clearInterval(interval);
      }
    }, 1000);
    return () => clearInterval(interval);
  }, [session]);

  useEffect(() => {
    if (!session || session.dev) return;
    const handler = () => {
      navigator.sendBeacon?.(`${API_BASE}/session/release`, JSON.stringify({ token: session.token }));
      clearSessionToken();
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
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

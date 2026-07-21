import React, { useCallback, useEffect, useRef, useState } from "react";
import { API_BASE } from "../../api/client.js";
import PixelStreamPlayer from "../../PixelStreamPlayer.jsx";
import { useLatestRun } from "../training/datahub.js";

// Live agent-camera view: the real RGB frame from the SPEAR cluster's render client
// (the UE instance currently running the agent), polled straight from the datahub so it is
// ALWAYS connected to the latest running UE — independent of how many instances exist or of
// any SSE store. This is the viewport "接入" the running agent camera.
function AgentCameraView() {
  const data = useLatestRun(1200);   // run status + per-step metadata
  const [cirrusPort, setCirrusPort] = useState(null);
  useEffect(() => {
    let on = true;
    fetch(`${API_BASE}/pixel-streaming-url`).then((r) => r.json())
      .then((j) => { if (on && j && j.detectedPort) setCirrusPort(j.detectedPort); }).catch(() => {});
    return () => { on = false; };
  }, []);
  const run = data && data.run;
  const status = (data && data.status) || "idle";
  const live = status === "running" || status === "starting";
  const eps = (run && run.episodes) || [];
  let ep = eps.find((e) => !e.done) || eps[eps.length - 1];
  const steps = (ep && ep.steps) || [];
  const st = steps[steps.length - 1];

  // Auto-connect fix: the iframe mounts as soon as the run is `live`, but the cluster's render
  // client (and thus the "DefaultStreamer") only registers ~3 min later once it finishes booting.
  // The first connect attempt finds no streamer and gives up — which is why a MANUAL reconnect
  // worked but auto-connect didn't. So we REMOUNT the player (bump `streamNonce` → new React key)
  // the moment the stream becomes available (first captured frame = render client up = streamer
  // registered), plus a couple of safety retries. Each remount is a fresh connect, exactly like the
  // manual reconnect the user did by hand.
  const hasFrame = !!st;
  const [streamNonce, setStreamNonce] = useState(0);
  const sawFrame = useRef(false);
  useEffect(() => {
    if (!live) { sawFrame.current = false; return; }
    if (hasFrame && !sawFrame.current) {
      sawFrame.current = true;
      setStreamNonce((n) => n + 1);
      const t1 = setTimeout(() => setStreamNonce((n) => n + 1), 6000);
      const t2 = setTimeout(() => setStreamNonce((n) => n + 1), 16000);
      return () => { clearTimeout(t1); clearTimeout(t2); };
    }
  }, [live, hasFrame]);

  if (!run) {
    return <div className="viewport-screenshot-empty"><span>No training run yet. Start one — the agent's live video shows here.</span></div>;
  }
  const epName = ep && (ep.runName || `${run.runId}_e${ep.idx}`);
  const stepUrl = st ? `${API_BASE}/training/${run.runId}/frame?ep=${encodeURIComponent(epName)}&step=${st.step}` : null;
  // Live = real WebRTC PixelStream of the agent (continuous video → smooth walking). NOTE: this UE
  // build ignores -PixelStreamingStreamerId and ALWAYS registers the render client as
  // "DefaultStreamer" (confirmed in client log: "PixelStreaming streamer ID: DefaultStreamer"), so
  // the viewport selects DefaultStreamer on the training cirrus. Falls back to the last captured
  // frame when finished.
  const agentPlayerUrl = cirrusPort ? `/ue-player.html?cirrus=${cirrusPort}&StreamerId=DefaultStreamer` : null;
  // Only mount the WebRTC player once the stream is actually READY (first captured frame = render
  // client booted = DefaultStreamer registered on cirrus). Before that, mounting it just spins for
  // the whole ~3 min cluster boot with no streamer to connect to — which read as "一直转圈连不上".
  // While booting we show a clear status instead; the player appears (and connects) the moment the
  // stream exists, then streamNonce remounts it twice more as a safety net.
  const streamReady = live && hasFrame && agentPlayerUrl;
  return (
    <div className="viewport-screenshot-frame" style={{ position: "relative" }}>
      {streamReady
        ? <PixelStreamPlayer key={`ps-${run.runId}-${streamNonce}`} playerUrl={agentPlayerUrl} />
        : (stepUrl
            ? <img src={stepUrl} alt="agent camera" className="loaded"
                style={{ width: "100%", height: "100%", objectFit: "contain" }}
                onError={(e) => { e.currentTarget.style.opacity = 0.2; }} />
            : <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
                {live && <span style={{ width: 12, height: 12, borderRadius: 12,
                  border: "2px solid var(--ink-3)", borderTopColor: "transparent", display: "inline-block",
                  animation: "spin 0.8s linear infinite" }} />}
                {live ? "SPEAR cluster booting (~3 min) — auto-connects to the agent camera when ready…" : "run finished"}
              </span>)}
      <div style={{ position: "absolute", left: 8, top: 8, padding: "2px 8px", borderRadius: 6,
        background: live ? "rgba(61,220,132,0.85)" : "rgba(0,0,0,0.6)", color: live ? "#000" : "#fff",
        fontSize: 11, fontWeight: 700 }}>
        {live ? "● LIVE" : status} · {run.runId}
      </div>
      {st && (
        <div style={{ position: "absolute", left: 8, bottom: 8, padding: "3px 8px", borderRadius: 6,
          background: "rgba(0,0,0,0.6)", color: "#fff", fontSize: 12, fontFamily: "monospace" }}>
          ep {ep.idx} · step {st.step} · {st.action}
          {st.distanceCm != null && ` · ${(st.distanceCm / 100).toFixed(1)}m to goal`}
        </div>
      )}
    </div>
  );
}

const CAMERA_PRESETS = [
  { label: "Top", title: "Bird's-eye view", args: [0, 0, 5000, -90, 0, 0] },
  { label: "Iso", title: "Isometric overview", args: [3000, -3000, 3000, -35, 45, 0] },
  { label: "Front", title: "Front view (Y-axis)", args: [0, -4000, 1000, 0, 0, 0] },
  { label: "Side", title: "Side view (X-axis)", args: [-4000, 0, 1000, 0, 90, 0] },
];

async function sendCameraCommand(cmd, args = []) {
  await fetch(`${API_BASE}/camera`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cmd, args }),
  });
}

function ScreenshotView({ imgKey, onRefresh, src }) {
  const [loaded, setLoaded] = useState(false);
  const [errored, setErrored] = useState(false);

  useEffect(() => {
    setLoaded(false);
    setErrored(false);
  }, [src, imgKey]);

  if (!src) {
    return (
      <div className="viewport-screenshot-empty">
        <span>No screenshot yet</span>
      </div>
    );
  }

  return (
    <div className="viewport-screenshot-frame">
      <img
        key={imgKey}
        src={src}
        alt="UE viewport"
        className={loaded ? "loaded" : ""}
        onLoad={() => setLoaded(true)}
        onError={() => {
          setErrored(true);
          setTimeout(() => onRefresh?.(), 1000);
        }}
      />
      {!loaded && !errored && <span>Loading screenshot...</span>}
      {errored && <span>Retrying screenshot...</span>}
    </div>
  );
}

function SaveAsButton({ icons }) {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);

  const onClick = async () => {
    if (busy) return;
    const raw = window.prompt("Save current scene as (alphanumeric, _, -):", "");
    if (raw == null) return;
    const name = raw.trim();
    if (!/^[A-Za-z0-9_-]+$/.test(name)) {
      setMsg({ kind: "err", text: "name must be [A-Za-z0-9_-]" });
      setTimeout(() => setMsg(null), 4000);
      return;
    }
    setBusy(true);
    setMsg(null);
    try {
      const response = await fetch(`${API_BASE}/scene/save-as`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, revert_to_original: false }),
      });
      const payload = await response.json();
      if (!response.ok || !payload.ok) {
        setMsg({ kind: "err", text: (payload.error || "save failed").slice(0, 80) });
      } else {
        setMsg({ kind: "ok", text: `Saved: ${payload.asset_path}` });
      }
    } catch (error) {
      setMsg({ kind: "err", text: error.message.slice(0, 80) });
    } finally {
      setBusy(false);
      setTimeout(() => setMsg(null), 6000);
    }
  };

  return (
    <>
      <button className="viewport-save-btn" title="Save current scene as a new .umap" disabled={busy} onClick={onClick}>
        {busy ? (
          "Saving..."
        ) : (
          <>
            {icons.document(12)} Save As
          </>
        )}
      </button>
      {msg && <span className={`viewport-save-msg ${msg.kind}`}>{msg.text}</span>}
    </>
  );
}

function responseError(payload, fallback) {
  if (payload && typeof payload === "object" && typeof payload.error === "string") {
    return payload.error.slice(0, 120);
  }
  return fallback;
}

async function readJsonResponse(response) {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

function retryDelayMs(payload) {
  const value = payload?.retry_after_ms ?? payload?.retryAfterMs;
  return Number.isSafeInteger(value) && value > 0 && value <= 60_000 ? value : null;
}

function hasExactKeys(value, expectedKeys) {
  return Boolean(
    value &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    Object.keys(value).length === expectedKeys.length &&
    expectedKeys.every((key) => Object.prototype.hasOwnProperty.call(value, key))
  );
}

function validRuntimeState(payload, expectedPawnClass) {
  const finiteTriple = (value) => Array.isArray(value) && value.length === 3 && value.every(Number.isFinite);
  return Boolean(
    hasExactKeys(payload, [
      "schema", "pie", "possessed", "pawn_class", "location", "rotation",
      "velocity", "on_ground", "engine_time",
    ]) &&
    payload?.schema === "vista-runtime-state/v1" &&
    payload?.pie === true &&
    payload?.possessed === true &&
    typeof expectedPawnClass === "string" &&
    payload?.pawn_class === expectedPawnClass &&
    finiteTriple(payload?.location) &&
    finiteTriple(payload?.rotation) &&
    finiteTriple(payload?.velocity) &&
    typeof payload?.on_ground === "boolean" &&
    Number.isFinite(payload?.engine_time) &&
    payload.engine_time >= 0
  );
}

function validStoppedState(payload) {
  return Boolean(
    hasExactKeys(payload, [
      "schema", "pie", "possessed", "pawn_class", "location", "rotation",
      "velocity", "on_ground", "engine_time",
    ]) &&
    payload?.schema === "vista-runtime-state/v1" &&
    payload?.pie === false &&
    payload?.possessed === false &&
    payload?.pawn_class === null &&
    payload?.location === null &&
    payload?.rotation === null &&
    payload?.velocity === null &&
    payload?.on_ground === null &&
    payload?.engine_time === null
  );
}

function validStopResponse(payload) {
  return Boolean(
    hasExactKeys(payload, [
      "schema", "phase", "stop_requested", "was_playing", "retry_after_ms",
    ]) &&
    payload?.schema === "vista-runtime-stop/v1" &&
    payload?.phase === "stop_requested" &&
    payload?.stop_requested === true &&
    typeof payload?.was_playing === "boolean" &&
    retryDelayMs(payload)
  );
}

async function fetchJsonWithTimeout(url, options, timeoutMs) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    return { response, payload: await readJsonResponse(response) };
  } catch (error) {
    if (error?.name === "AbortError") throw new Error("VISTA runtime request timed out");
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

export default function ViewportPanel({ health, icons, latestScreenshot }) {
  const [mode, setMode] = useState("pixelstream");
  const [imgKey, setImgKey] = useState(0);
  const [screenshotUrl, setScreenshotUrl] = useState(null);
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [refreshInterval, setRefreshInterval] = useState(5);
  const [cameraMoving, setCameraMoving] = useState(false);
  const [controlsOpen, setControlsOpen] = useState(true);
  const [playerUrl, setPlayerUrl] = useState(null);
  const intervalRef = useRef(null);
  const screenshotObjectUrlRef = useRef(null);
  const pixelPlayerRef = useRef(null);
  const vistaPollTimerRef = useRef(null);
  const vistaRunRef = useRef(0);
  const vistaExpectedPawnRef = useRef(null);
  const vistaGraceUntilRef = useRef(0);
  const vistaLiveRef = useRef(false);
  const vistaPlayDispatchedRef = useRef(false);
  const vistaPlayLeaseRef = useRef(false);
  const vistaReconciledRef = useRef(false);
  const [pixelStreamReady, setPixelStreamReady] = useState(false);
  const [vistaDemo, setVistaDemo] = useState({ phase: "idle", text: "Waiting for viewport stream" });
  const engineLabel = health?.engineLabel || (health?.engineVersion ? `UE ${health.engineVersion}` : "Unreal Engine");

  const clearVistaPoll = useCallback(() => {
    if (!vistaPollTimerRef.current) return;
    clearTimeout(vistaPollTimerRef.current);
    vistaPollTimerRef.current = null;
  }, []);

  useEffect(() => () => {
    vistaRunRef.current += 1;
    clearVistaPoll();
  }, [clearVistaPoll]);

  useEffect(() => {
    if (vistaDemo.phase !== "idle") return;
    setVistaDemo({
      phase: "idle",
      text: pixelStreamReady ? "Simulation ready" : "Waiting for viewport stream",
    });
  }, [pixelStreamReady, vistaDemo.phase]);

  useEffect(() => {
    if (!playerUrl || vistaReconciledRef.current || vistaDemo.phase !== "idle") return undefined;
    let cancelled = false;
    let retryTimer = null;
    let attempts = 0;
    setMode("pixelstream");
    setVistaDemo({ phase: "reconciling", text: "Checking existing UE Play state..." });

    const reconcile = async () => {
      if (cancelled) return;
      attempts += 1;
      try {
        const { response, payload } = await fetchJsonWithTimeout(
          `${API_BASE}/vista/get_vista_state`,
          {
            method: "GET",
            credentials: "same-origin",
            cache: "no-store",
            headers: { Accept: "application/json" },
          },
          20_000,
        );
        if (cancelled) return;
        const transient = response.status === 425 || response.status === 429 ||
          response.status === 503 ||
          (response.status === 502 && payload?.code === "VISTA_RUNTIME_PROTOCOL_ERROR");
        if (transient && attempts < 10) {
          retryTimer = setTimeout(reconcile, retryDelayMs(payload) || 1_000);
          return;
        }
        if (!response.ok) {
          throw new Error(responseError(payload, `State reconciliation failed (${response.status})`));
        }
        if (validStoppedState(payload)) {
          vistaReconciledRef.current = true;
          vistaExpectedPawnRef.current = null;
          vistaPlayDispatchedRef.current = false;
          vistaPlayLeaseRef.current = false;
          setVistaDemo({ phase: "idle", text: "Simulation ready" });
          return;
        }
        if (typeof payload?.pawn_class === "string" && validRuntimeState(payload, payload.pawn_class)) {
          vistaReconciledRef.current = true;
          vistaExpectedPawnRef.current = payload.pawn_class;
          vistaPlayDispatchedRef.current = true;
          vistaLiveRef.current = true;
          setMode("pixelstream");
          setVistaDemo({ phase: "live", text: "Simulation already running — controls recovered" });
          return;
        }
        throw new Error("UE Play state could not be reconciled");
      } catch (error) {
        if (!cancelled) {
          vistaPlayDispatchedRef.current = true;
          setMode("pixelstream");
          setVistaDemo({
            phase: "play_error",
            text: `${error.message || "State reconciliation failed"} — Stop is available`,
          });
        }
      }
    };

    reconcile();
    return () => {
      cancelled = true;
      if (retryTimer) clearTimeout(retryTimer);
    };
    // Reconciliation starts as soon as the local player endpoint is known; it
    // must not depend on a working WebRTC input channel because backend Stop is
    // the recovery path for a live PIE after stream loss. Depending on phase
    // would cancel the in-flight probe when it sets `reconciling`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playerUrl]);

  useEffect(() => {
    if (
      !pixelStreamReady &&
      vistaPlayDispatchedRef.current &&
      (vistaDemo.phase === "grace" || vistaDemo.phase === "live")
    ) {
      vistaLiveRef.current = false;
      setVistaDemo({
        phase: "play_error",
        text: "Live stream disconnected — Stop remains available",
      });
    }
  }, [pixelStreamReady, vistaDemo.phase]);

  const startVistaDemo = useCallback(async () => {
    if (!pixelStreamReady || !pixelPlayerRef.current) return;
    clearVistaPoll();
    const runId = vistaRunRef.current + 1;
    let expectedPawnClass = null;
    let stateProbeAttempts = 0;
    vistaRunRef.current = runId;
    vistaExpectedPawnRef.current = null;
    vistaGraceUntilRef.current = 0;
    vistaLiveRef.current = false;
    vistaPlayDispatchedRef.current = false;
    vistaPlayLeaseRef.current = false;
    setMode("pixelstream");
    setVistaDemo({ phase: "preparing", text: "Preparing simulation..." });

    const scheduleProbe = (delayMs) => {
      clearVistaPoll();
      setVistaDemo({
        phase: "grace",
        text: `Entering Play mode — verifying after ${Math.ceil(delayMs / 1000)}s`,
      });
      vistaPollTimerRef.current = setTimeout(probeState, delayMs);
    };

    const probeState = async () => {
      vistaPollTimerRef.current = null;
      if (vistaRunRef.current !== runId) return;
      try {
        stateProbeAttempts += 1;
        if (stateProbeAttempts > 10) throw new Error("VISTA Play verification timed out");
        const { response, payload } = await fetchJsonWithTimeout(
          `${API_BASE}/vista/get_vista_state`,
          {
            method: "GET",
            credentials: "same-origin",
            cache: "no-store",
            headers: { Accept: "application/json" },
          },
          20_000,
        );
        if (vistaRunRef.current !== runId) return;
        if (response.status === 425 || response.status === 429 || response.status === 503) {
          const retryMs = retryDelayMs(payload) || (response.status === 503 ? 1_000 : null);
          if (
            !retryMs ||
            (response.status === 425 && payload?.code !== "VISTA_PLAY_START_GRACE")
          ) {
            throw new Error("Invalid VISTA grace response");
          }
          scheduleProbe(retryMs);
          return;
        }
        if (
          response.status === 502 &&
          payload?.code === "VISTA_RUNTIME_PROTOCOL_ERROR"
        ) {
          scheduleProbe(retryDelayMs(payload) || 1_000);
          return;
        }
        if (!response.ok) throw new Error(responseError(payload, `State check failed (${response.status})`));
        if (validStoppedState(payload)) {
          vistaExpectedPawnRef.current = null;
          vistaPlayDispatchedRef.current = false;
          vistaPlayLeaseRef.current = false;
          setVistaDemo({ phase: "stopped", text: "Simulation remained stopped — Play was not toggled twice" });
          return;
        }
        const observedPawnClass = expectedPawnClass || payload?.pawn_class;
        if (!validRuntimeState(payload, observedPawnClass)) {
          throw new Error("VISTA runtime is not possessed");
        }
        expectedPawnClass = observedPawnClass;
        vistaExpectedPawnRef.current = observedPawnClass;
        vistaReconciledRef.current = true;
        vistaLiveRef.current = true;
        setVistaDemo({ phase: "live", text: "Simulation running — WASD / arrows / mouse / Space" });
      } catch (error) {
        if (vistaRunRef.current === runId) {
          setVistaDemo({
            phase: vistaPlayDispatchedRef.current || vistaPlayLeaseRef.current ? "play_error" : "error",
            text: error.message || "VISTA state check failed",
          });
        }
      }
    };

    try {
      const { response, payload } = await fetchJsonWithTimeout(
        `${API_BASE}/vista/setup_vista_play_mode`,
        {
          method: "POST",
          credentials: "same-origin",
        },
        35_000,
      );
      if (response.status === 409 && payload?.code === "VISTA_PLAY_LEASE_HELD") {
        vistaPlayDispatchedRef.current = true;
        scheduleProbe(retryDelayMs(payload) || 500);
        return;
      }
      if (!response.ok) throw new Error(responseError(payload, `VISTA setup failed (${response.status})`));
      const retryMs = retryDelayMs(payload);
      expectedPawnClass = payload?.pawn_class;
      if (
        !hasExactKeys(payload, [
          "schema", "phase", "prepared", "game_mode_class", "pawn_class",
          "player_start_present", "requires_operator_play", "retry_after_ms",
          "state_probe_grace_ms", "play_lease_granted",
        ]) ||
        payload?.schema !== "vista-runtime-setup/v1" ||
        payload?.phase !== "prepared" ||
        payload?.prepared !== true ||
        payload?.requires_operator_play !== true ||
        payload?.play_lease_granted !== true ||
        typeof expectedPawnClass !== "string" ||
        !retryMs ||
        payload.state_probe_grace_ms !== retryMs
      ) {
        throw new Error("Invalid VISTA setup response");
      }
      if (vistaRunRef.current !== runId) return;
      vistaExpectedPawnRef.current = expectedPawnClass;
      vistaGraceUntilRef.current = Date.now() + retryMs;
      vistaPlayLeaseRef.current = true;
      if (!pixelPlayerRef.current?.playVistaDemo()) throw new Error("Live stream is not ready for Play");
      vistaPlayDispatchedRef.current = true;
      scheduleProbe(retryMs);
    } catch (error) {
      if (vistaRunRef.current === runId) {
        setVistaDemo({
          phase: vistaPlayDispatchedRef.current || vistaPlayLeaseRef.current ? "play_error" : "error",
          text: error.message || "VISTA setup failed",
        });
      }
    }
  }, [clearVistaPoll, pixelStreamReady]);

  const stopVistaDemo = useCallback(async () => {
    vistaRunRef.current += 1;
    const runId = vistaRunRef.current;
    clearVistaPoll();
    // Escape is a low-latency best effort. The fixed same-origin backend Stop
    // remains authoritative and works even after the WebRTC input channel drops.
    pixelPlayerRef.current?.stopVistaDemo();
    const expectedPawnClass = vistaExpectedPawnRef.current;
    vistaLiveRef.current = false;
    const deadline = Date.now() + 45_000;
    let fixedStopAttempts = 0;
    setVistaDemo({ phase: "stopping", text: "Stop sent — verifying UE Play mode ended..." });

    const scheduleStopProbe = (delayMs) => {
      clearVistaPoll();
      vistaPollTimerRef.current = setTimeout(probeStopped, delayMs);
    };

    const requestFixedStop = async () => {
      fixedStopAttempts += 1;
      try {
        const { response, payload } = await fetchJsonWithTimeout(
          `${API_BASE}/vista/stop_vista_play_mode`,
          {
            method: "POST",
            credentials: "same-origin",
          },
          20_000,
        );
        if (response.ok) {
          if (!validStopResponse(payload)) throw new Error("Invalid VISTA Stop response");
          return retryDelayMs(payload);
        }
        if (
          response.status === 429 ||
          response.status === 502 ||
          response.status === 503
        ) {
          return retryDelayMs(payload) || 1_000;
        }
        throw new Error(responseError(payload, `VISTA Stop failed (${response.status})`));
      } catch (error) {
        if (fixedStopAttempts >= 3) throw error;
        return 1_000;
      }
    };

    const probeStopped = async () => {
      vistaPollTimerRef.current = null;
      if (vistaRunRef.current !== runId) return;
      if (Date.now() >= deadline) {
        setVistaDemo({ phase: "stop_error", text: "VISTA Stop was not confirmed — retry Stop" });
        return;
      }
      try {
        const { response, payload } = await fetchJsonWithTimeout(
          `${API_BASE}/vista/get_vista_state`,
          {
            method: "GET",
            credentials: "same-origin",
            cache: "no-store",
            headers: { Accept: "application/json" },
          },
          20_000,
        );
        if (vistaRunRef.current !== runId) return;
        if (
          response.status === 425 ||
          response.status === 429 ||
          response.status === 502 ||
          response.status === 503
        ) {
          scheduleStopProbe(retryDelayMs(payload) || 1_000);
          return;
        }
        if (!response.ok) throw new Error(responseError(payload, `Stop check failed (${response.status})`));
        if (validStoppedState(payload)) {
          vistaExpectedPawnRef.current = null;
          vistaGraceUntilRef.current = 0;
          vistaPlayDispatchedRef.current = false;
          vistaPlayLeaseRef.current = false;
          vistaReconciledRef.current = true;
          setVistaDemo({ phase: "stopped", text: "Simulation stopped — UE confirmed" });
          return;
        }
        if (validRuntimeState(payload, expectedPawnClass)) {
          const retryMs = fixedStopAttempts < 3 ? await requestFixedStop() : 600;
          scheduleStopProbe(retryMs || 600);
          return;
        }
        throw new Error("Invalid VISTA Stop state");
      } catch (error) {
        if (vistaRunRef.current === runId) {
          setVistaDemo({
            phase: "stop_error",
            text: `${error.message || "VISTA Stop verification failed"} — retry Stop`,
          });
        }
      }
    };

    try {
      const retryMs = await requestFixedStop();
      if (vistaRunRef.current === runId) scheduleStopProbe(retryMs || 600);
    } catch (error) {
      if (vistaRunRef.current === runId) {
        // The fixed command may have executed before its marker/transport was
        // lost, so still verify state once before exposing a retry.
        scheduleStopProbe(600);
      }
    }
  }, [clearVistaPoll]);

  const vistaOwnsViewport = [
    "preparing",
    "reconciling",
    "grace",
    "live",
    "play_error",
    "stopping",
    "stop_error",
  ].includes(vistaDemo.phase);
  const canStopVista = ["grace", "live", "play_error", "stop_error"].includes(vistaDemo.phase);
  const vistaDemoHasError = ["error", "play_error", "stop_error"].includes(vistaDemo.phase);

  // Auto-connect the agent camera to the LATEST RUNNING UE: poll the datahub and, while a run
  // is live, KEEP the viewport on the Agent view (re-assert every tick — not just on the
  // transition — so opening the page mid-run still lands on the agent stream, not the editor's
  // empty scene). This is why the viewport showed "Editor" before: it was never switched.
  useEffect(() => {
    let on = true;
    const tick = async () => {
      try {
        const r = await fetch(`${API_BASE}/training/datahub/latest`);
        const j = await r.json();
        const live = j && (j.status === "running" || j.status === "starting");
        if (on && live && !vistaOwnsViewport) setMode("agent");
      } catch (_e) {}
    };
    tick();
    const t = setInterval(tick, 2500);
    return () => { on = false; clearInterval(t); };
  }, [vistaOwnsViewport]);

  const clearScreenshotObjectUrl = useCallback(() => {
    if (!screenshotObjectUrlRef.current) return;
    URL.revokeObjectURL(screenshotObjectUrlRef.current);
    screenshotObjectUrlRef.current = null;
  }, []);

  const showScreenshotBlob = useCallback((blob) => {
    clearScreenshotObjectUrl();
    const objectUrl = URL.createObjectURL(blob);
    screenshotObjectUrlRef.current = objectUrl;
    setScreenshotUrl(objectUrl);
    setImgKey((value) => value + 1);
  }, [clearScreenshotObjectUrl]);

  const showScreenshotUrl = useCallback((url) => {
    clearScreenshotObjectUrl();
    setScreenshotUrl(url);
    setImgKey((value) => value + 1);
  }, [clearScreenshotObjectUrl]);

  const handleCameraPreset = useCallback(async (args) => {
    setCameraMoving(true);
    try {
      await sendCameraCommand("set_camera", args);
    } finally {
      setCameraMoving(false);
    }
  }, []);

  useEffect(() => {
    fetch(`${API_BASE}/pixel-streaming-url`)
      .then((response) => response.json())
      .then((data) => {
        if (!data.url) return;
        const webRtcFps = data.webRtcFps === 30 || data.webRtcFps === 60
          ? data.webRtcFps
          : null;
        if (!webRtcFps) return;
        const cirrusPort = data.detectedPort || (() => {
          try {
            return new URL(data.url).port || 8685;
          } catch {
            return 8685;
          }
        })();
        const params = new URLSearchParams({
          cirrus: String(cirrusPort),
          StreamerId: "Editor",
          StreamerAutoJoinInterval: "3",
          MaxReconnectAttempts: "0",
          AutoConnect: "true",
          AutoPlayVideo: "true",
          StartVideoMuted: "true",
          WaitForStreamer: "true",
          HoveringMouse: "true",
          KeyboardInput: "true",
          MouseInput: "true",
          GamepadInput: "true",
          TouchInput: "true",
          ControlsQuality: "true",
          MatchViewportRes: "true",
          TimeoutIfIdle: "false",
          WebRTCFPS: String(webRtcFps),
        });
        setPlayerUrl(`/ue-player.html?${params.toString()}`);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (!latestScreenshot) return;
    if (!vistaOwnsViewport) setMode("screenshot");
    let cancelled = false;
    const sep = latestScreenshot.includes("?") ? "&" : "?";
    const url = `${latestScreenshot}${sep}t=${Date.now()}`;
    fetch(url)
      .then((response) => (response.ok ? response.blob() : Promise.reject()))
      .then((blob) => {
        if (!cancelled) {
          showScreenshotBlob(blob);
        }
      })
      .catch(() => {
        if (!cancelled) {
          showScreenshotUrl(url);
        }
      });
    return () => {
      cancelled = true;
    };
  // `vistaOwnsViewport` is intentionally not a dependency: ending a VISTA run
  // must not replay an old screenshot event and immediately hide the Start UI.
  // A genuinely new screenshot still evaluates the current render's guard.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [latestScreenshot, showScreenshotBlob, showScreenshotUrl]);

  const fetchLatestScreenshot = useCallback(async () => {
    try {
      const response = await fetch(`${API_BASE}/screenshot/latest?t=${Date.now()}`);
      if (response.ok) {
        const blob = await response.blob();
        showScreenshotBlob(blob);
      }
    } catch {}
  }, [showScreenshotBlob]);

  useEffect(() => clearScreenshotObjectUrl, [clearScreenshotObjectUrl]);

  useEffect(() => {
    fetchLatestScreenshot();
  }, [fetchLatestScreenshot]);

  useEffect(() => {
    if (intervalRef.current) clearInterval(intervalRef.current);
    if (!autoRefresh || mode !== "screenshot") return undefined;

    const tick = () => {
      if (document.visibilityState === "visible") fetchLatestScreenshot();
    };
    intervalRef.current = setInterval(tick, refreshInterval * 1000);
    document.addEventListener("visibilitychange", tick);
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
      document.removeEventListener("visibilitychange", tick);
    };
  }, [autoRefresh, fetchLatestScreenshot, mode, refreshInterval]);

  return (
    <div className="viewport-panel">
      <div className={`viewport-toolbar${controlsOpen ? "" : " compact"}`}>
        <button
          className="viewport-tool-btn viewport-controls-toggle"
          onClick={() => setControlsOpen((value) => !value)}
          title={controlsOpen ? "Hide viewport controls" : "Show viewport controls"}
          type="button"
        >
          {controlsOpen ? "Hide" : "Controls"}
        </button>
        <div className="viewport-mode-tabs">
          {[
            { id: "pixelstream", label: "Editor" },
            { id: "agent", label: "Camera" },
            { id: "screenshot", label: "Capture" },
          ].map((tab) => (
            <button
              key={tab.id}
              className={`viewport-mode-btn${mode === tab.id ? " active" : ""}`}
              disabled={vistaOwnsViewport && tab.id !== "pixelstream"}
              onClick={() => setMode(tab.id)}
              title={vistaOwnsViewport && tab.id !== "pixelstream" ? "Stop the simulation before changing views" : undefined}
            >
              {tab.label}
            </button>
          ))}
        </div>

        {controlsOpen && mode === "screenshot" && (
          <div className="viewport-screenshot-controls">
            <button className="viewport-tool-btn" onClick={fetchLatestScreenshot}>
              Refresh
            </button>
            <label className="viewport-auto-label">
              <input type="checkbox" checked={autoRefresh} onChange={(event) => setAutoRefresh(event.target.checked)} />
              Auto
            </label>
            <select
              className="viewport-select"
              value={refreshInterval}
              onChange={(event) => setRefreshInterval(Number(event.target.value))}
            >
              {[2, 3, 5, 10].map((seconds) => (
                <option key={seconds} value={seconds}>
                  {seconds}s
                </option>
              ))}
            </select>
          </div>
        )}

        {controlsOpen ? (
          <div className="viewport-camera-controls">
            <span className="viewport-camera-label">CAM</span>
            {CAMERA_PRESETS.map((preset) => (
              <button
                key={preset.label}
                className="viewport-camera-btn"
                title={preset.title}
                disabled={cameraMoving}
                onClick={() => handleCameraPreset(preset.args)}
              >
                {preset.label}
              </button>
            ))}
            <button
              className="viewport-unlock-btn"
              title="Release camera lock"
              disabled={cameraMoving}
              onClick={() => {
                setCameraMoving(true);
                sendCameraCommand("unpilot_camera").finally(() => setCameraMoving(false));
              }}
            >
              Unlock
            </button>
            <SaveAsButton icons={icons} />
          </div>
        ) : (
          <div className="viewport-compact-label">
            {mode === "pixelstream" ? "Editor controls hidden" : "Capture controls hidden"}
          </div>
        )}

        <div className="viewport-toolbar-spacer" />

        {mode === "pixelstream" && (
          <>
            <button
              className="viewport-tool-btn"
              type="button"
              data-testid="vista-demo-toggle"
              disabled={
                vistaDemo.phase === "preparing" ||
                vistaDemo.phase === "reconciling" ||
                vistaDemo.phase === "stopping" ||
                (!canStopVista && !pixelStreamReady)
              }
              onClick={canStopVista ? stopVistaDemo : startVistaDemo}
              style={{
                color: vistaDemoHasError ? "var(--red)" : vistaDemo.phase === "live" ? "var(--green)" : "var(--blue-2)",
                borderColor: vistaDemoHasError ? "var(--red)" : vistaDemo.phase === "live" ? "var(--green)" : "var(--blue)",
              }}
            >
              {vistaDemo.phase === "stopping"
                ? "Stopping simulation..."
                : vistaDemo.phase === "reconciling"
                  ? "Checking simulation..."
                : canStopVista
                  ? "Stop Simulation"
                  : "Start Simulation"}
            </button>
            <span
              role="status"
              data-testid="vista-demo-status"
              title={vistaDemo.text}
              style={{
                maxWidth: 280,
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
                color: vistaDemoHasError ? "var(--red)" : "var(--ink-3)",
                fontSize: 11,
              }}
            >
              {vistaDemo.text}
            </span>
          </>
        )}

        {controlsOpen && playerUrl && (
          <a className="viewport-popout" href={playerUrl} target="_blank" rel="noreferrer">
            Pop out
          </a>
        )}
      </div>

      <div className="viewport-stage">
        <div className="viewport-pixelstream-mount" style={{ display: mode === "pixelstream" ? "block" : "none" }}>
          <PixelStreamPlayer
            ref={pixelPlayerRef}
            playerUrl={playerUrl}
            onStreamReadyChange={setPixelStreamReady}
          />
        </div>
        {mode === "screenshot" && (
          <ScreenshotView src={screenshotUrl} imgKey={imgKey} onRefresh={fetchLatestScreenshot} />
        )}
        {mode === "agent" && <AgentCameraView />}
      </div>

      <div className="viewport-statusbar">
        <span>{engineLabel} / SimWorld Studio</span>
        {playerUrl && (
          <span>
            {playerUrl.match(/cirrus=(\d+)/)?.[1]
              ? `Cirrus :${playerUrl.match(/cirrus=(\d+)/)[1]}`
              : playerUrl}
          </span>
        )}
        {mode === "screenshot" && screenshotUrl && (
          <span className="viewport-screenshot-ready">
            {icons.check(12)} Screenshot ready
          </span>
        )}
      </div>
    </div>
  );
}

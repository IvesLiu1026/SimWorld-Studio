import React, { useCallback, useEffect, useRef, useState } from "react";
import { API_BASE } from "../../api/client.js";
import PixelStreamPlayer from "../../PixelStreamPlayer.jsx";

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
  const engineLabel = health?.engineLabel || (health?.engineVersion ? `UE ${health.engineVersion}` : "Unreal Engine");

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
          WebRTCFPS: "60",
        });
        setPlayerUrl(`/ue-player.html?${params.toString()}`);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (!latestScreenshot) return;
    setMode("screenshot");
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
            { id: "pixelstream", label: "Live" },
            { id: "screenshot", label: "Shot" },
          ].map((tab) => (
            <button
              key={tab.id}
              className={`viewport-mode-btn${mode === tab.id ? " active" : ""}`}
              onClick={() => setMode(tab.id)}
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
              title="Unlock camera from agent"
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
            {mode === "pixelstream" ? "Live stream controls hidden" : "Screenshot controls hidden"}
          </div>
        )}

        <div className="viewport-toolbar-spacer" />

        {controlsOpen && playerUrl && (
          <a className="viewport-popout" href={playerUrl} target="_blank" rel="noreferrer">
            Pop out
          </a>
        )}
      </div>

      <div className="viewport-stage">
        <div className="viewport-pixelstream-mount" style={{ display: mode === "pixelstream" ? "block" : "none" }}>
          <PixelStreamPlayer playerUrl={playerUrl} />
        </div>
        {mode === "screenshot" && (
          <ScreenshotView src={screenshotUrl} imgKey={imgKey} onRefresh={fetchLatestScreenshot} />
        )}
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

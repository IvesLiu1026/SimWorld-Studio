import React, { useState, useEffect, useRef, useCallback } from "react";

const LS_PORT_KEY = "sw_cirrus_port_override";

/**
 * PixelStreamPlayer
 * - Auto-detect / manual Cirrus port override (persisted to localStorage)
 * - Status machine: idle → connecting → connected → disconnected → reconnecting
 * - Activation overlay (click to enable input)
 * - PIE mode hint after repeated failures
 * - Iframe stays mounted (preserves WebRTC state)
 */
export default function PixelStreamPlayer({ playerUrl }) {
  const iframeRef       = useRef(null);
  const reconnectTimer  = useRef(null);
  const pingTimer       = useRef(null);
  const lastConnected   = useRef(null);

  const [status,     setStatus]     = useState("idle");
  const [active,     setActive]     = useState(false);
  const [reconnects, setReconnects] = useState(0);
  const [showConfig, setShowConfig] = useState(false);

  // Manual port override — read from localStorage, fall back to auto-detected from playerUrl
  const [portOverride, setPortOverride] = useState(() => localStorage.getItem(LS_PORT_KEY) || "");

  // Effective player URL: use portOverride if set, else auto-detected playerUrl
  const effectiveUrl = useCallback(() => {
    if (!playerUrl) return null;
    if (!portOverride) return playerUrl;
    // Replace the cirrus= param with the override
    try {
      const u = new URL(playerUrl, window.location.href);
      u.searchParams.set("cirrus", portOverride);
      return u.pathname + "?" + u.searchParams.toString();
    } catch { return playerUrl; }
  }, [playerUrl, portOverride]);

  // Save port override to localStorage
  const applyPortOverride = useCallback((port) => {
    const cleaned = port.trim();
    if (cleaned) { localStorage.setItem(LS_PORT_KEY, cleaned); }
    else         { localStorage.removeItem(LS_PORT_KEY); }
    setPortOverride(cleaned);
    setShowConfig(false);
    // Reconnect with new URL
    setStatus("connecting");
    setActive(false);
    setReconnects(0);
  }, []);

  // Reset on playerUrl change
  useEffect(() => {
    if (!playerUrl) return;
    setStatus("connecting");
    setActive(false);
  }, [playerUrl]);

  // Listen for iframe postMessage
  useEffect(() => {
    function onMsg(e) {
      if (e.data?.type === "sw-stream-connected") {
        setStatus("connected");
        setReconnects(0);
        lastConnected.current = Date.now();
      }
    }
    window.addEventListener("message", onMsg);
    return () => window.removeEventListener("message", onMsg);
  }, []);

  // Heartbeat — detect silent drops after 45s no signal
  useEffect(() => {
    if (status !== "connected") return;
    pingTimer.current = setInterval(() => {
      if (lastConnected.current && Date.now() - lastConnected.current > 45_000) {
        setStatus("disconnected");
        clearInterval(pingTimer.current);
        scheduleReconnect();
      }
    }, 5000);
    return () => clearInterval(pingTimer.current);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status]);

  const scheduleReconnect = useCallback(() => {
    if (reconnectTimer.current) return;
    const delay = Math.min(1500 * Math.pow(1.5, reconnects), 20_000);
    setReconnects(n => n + 1);
    reconnectTimer.current = setTimeout(() => {
      reconnectTimer.current = null;
      doReconnect();
    }, delay);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reconnects]);

  const doReconnect = useCallback(() => {
    if (!iframeRef.current) return;
    setStatus("connecting");
    const url = effectiveUrl();
    if (!url) return;
    const sep = url.includes("?") ? "&" : "?";
    iframeRef.current.src = url + sep + "_t=" + Date.now();
    try { iframeRef.current.contentWindow?.postMessage({ type: "sw-reconnect" }, "*"); } catch {}
  }, [effectiveUrl]);

  const activate = useCallback(() => {
    setActive(true);
    setTimeout(() => {
      iframeRef.current?.focus();
      iframeRef.current?.contentWindow?.postMessage({ type: "sw-focus-stream" }, "*");
    }, 80);
  }, []);

  const statusColor = { idle:"#64748b", connecting:"#f59e0b", connected:"#22c55e", disconnected:"#f59e0b", error:"#dc2626" }[status] || "#64748b";
  const statusLabel = { idle:"Initialising", connecting:"Connecting…", connected:"Live", disconnected:"Reconnecting…", error:"Error" }[status] || "";
  const showPieHint = reconnects >= 2 && status !== "connected";
  const url = effectiveUrl();

  return (
    <div style={{ width:"100%", height:"100%", position:"relative", background:"#0b1220" }}>

      {/* iframe — always mounted */}
      {url && (
        <iframe
          ref={iframeRef}
          src={url}
          onLoad={() => { if (status === "idle" || status === "disconnected") setStatus("connecting"); }}
          style={{
            width:"100%", height:"100%", border:"none", display:"block",
            pointerEvents: (active && status === "connected") ? "auto" : "none",
          }}
          allow="pointer-lock *; fullscreen *; autoplay *; clipboard-read *; clipboard-write *"
          allowFullScreen
          tabIndex={0}
          title="UE Pixel Streaming"
        />
      )}

      {/* ── Status badge (always visible, top-left) ── */}
      <div style={{
        position:"absolute", top:8, left:8, zIndex:20,
        display:"inline-flex", alignItems:"center", gap:6,
        background:"rgba(11,18,32,0.75)", backdropFilter:"blur(6px)",
        border:"1px solid rgba(255,255,255,0.08)", borderRadius:8,
        padding:"4px 10px", fontSize:11, fontWeight:600, color:"#e2e8f0",
        userSelect:"none", pointerEvents:"none",
      }}>
        <span style={{
          width:7, height:7, borderRadius:"50%", background:statusColor, flexShrink:0,
          boxShadow: status==="connected" ? `0 0 6px ${statusColor}` : "none",
          animation: (status==="connecting"||status==="disconnected") ? "ps-pulse 1.2s infinite" : "none",
        }}/>
        {statusLabel}
        {reconnects > 0 && status !== "connected" && (
          <span style={{ color:"#64748b", marginLeft:2 }}>#{reconnects}</span>
        )}
      </div>

      {/* ── Config button (top-right) ── */}
      <button
        onClick={() => setShowConfig(s => !s)}
        title="Configure Cirrus port"
        style={{
          position:"absolute", top:8, right:8, zIndex:20,
          padding:"3px 8px", borderRadius:7, cursor:"pointer",
          border:"1px solid rgba(255,255,255,.12)", background:"rgba(11,18,32,.7)",
          color:"#64748b", fontSize:10, fontWeight:600,
          backdropFilter:"blur(4px)",
        }}
      >⚙ Port</button>

      {/* ── Port config panel ── */}
      {showConfig && (
        <PortConfigPanel
          currentUrl={url}
          portOverride={portOverride}
          onApply={applyPortOverride}
          onClose={() => setShowConfig(false)}
        />
      )}

      {/* ── Manual reconnect ── */}
      {(status === "error" || status === "disconnected") && !reconnectTimer.current && (
        <button
          onClick={() => { setReconnects(0); doReconnect(); }}
          style={{
            position:"absolute", top:36, left:8, zIndex:20,
            padding:"5px 12px", borderRadius:7, border:"1px solid #2563eb",
            background:"rgba(37,99,235,0.2)", color:"#93c5fd",
            fontSize:11, fontWeight:600, cursor:"pointer", backdropFilter:"blur(4px)",
          }}
        >↻ Reconnect</button>
      )}

      {/* ── PIE hint (after multiple failures) ── */}
      {showPieHint && (
        <div style={{
          position:"absolute", bottom:12, left:"50%", transform:"translateX(-50%)", zIndex:20,
          padding:"8px 14px", borderRadius:9,
          background:"rgba(245,158,11,.12)", border:"1px solid rgba(245,158,11,.3)",
          color:"#fcd34d", fontSize:11, textAlign:"center", lineHeight:1.5,
          backdropFilter:"blur(6px)", pointerEvents:"none",
        }}>
          💡 Make sure Pixel Streaming is active in UE<br/>
          <span style={{ color:"#94a3b8" }}>Go to UE → Editor Preferences → Plugins → Pixel Streaming → Enable Now</span>
        </div>
      )}

      {/* ── Activation overlay (stream connected but input locked) ── */}
      {status === "connected" && !active && (
        <div onClick={activate} style={{
          position:"absolute", inset:0, zIndex:10,
          display:"flex", flexDirection:"column", alignItems:"center", justifyContent:"center",
          background:"rgba(11,18,32,0.6)", backdropFilter:"blur(2px)",
          cursor:"pointer", gap:12,
        }}>
          <div style={{
            width:52, height:52, borderRadius:13,
            background:"rgba(37,99,235,0.2)", border:"2px solid rgba(37,99,235,0.5)",
            display:"flex", alignItems:"center", justifyContent:"center", fontSize:26,
          }}>🎮</div>
          <div style={{ color:"#e2e8f0", fontSize:15, fontWeight:700 }}>Click to Control</div>
          <div style={{ color:"#94a3b8", fontSize:11, textAlign:"center", lineHeight:1.6 }}>
            Enables keyboard &amp; mouse.<br/>
            <kbd style={{ background:"rgba(255,255,255,.1)", padding:"1px 5px", borderRadius:4 }}>Esc</kbd> to release.
          </div>
        </div>
      )}

      {/* ── Connecting overlay ── */}
      {(status === "idle" || status === "connecting") && (
        <div style={{
          position:"absolute", inset:0, zIndex:10, pointerEvents:"none",
          display:"flex", flexDirection:"column", alignItems:"center", justifyContent:"center",
          background:"rgba(11,18,32,0.85)", gap:14,
        }}>
          <div style={{
            width:38, height:38, borderRadius:"50%",
            border:"3px solid rgba(37,99,235,0.2)", borderTopColor:"#2563eb",
            animation:"ps-spin 0.9s linear infinite",
          }}/>
          <div style={{ color:"#e2e8f0", fontSize:13, fontWeight:600 }}>Connecting to UE Stream</div>
          {url && (
            <div style={{ color:"#475569", fontSize:11 }}>
              Cirrus :{url.match(/cirrus=(\d+)/)?.[1] || "?"}
              {portOverride && <span style={{ color:"#f59e0b", marginLeft:4 }}>(manual)</span>}
            </div>
          )}
        </div>
      )}

      {/* ── No URL ── */}
      {!url && (
        <div style={{
          position:"absolute", inset:0, zIndex:10, pointerEvents:"none",
          display:"flex", flexDirection:"column", alignItems:"center", justifyContent:"center",
          background:"rgba(11,18,32,0.85)", gap:10,
        }}>
          <div style={{ fontSize:28, opacity:0.3 }}>📡</div>
          <div style={{ color:"#64748b", fontSize:12 }}>No stream URL — is Cirrus running?</div>
        </div>
      )}

      <style>{`
        @keyframes ps-spin  { to{transform:rotate(360deg)} }
        @keyframes ps-pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
      `}</style>
    </div>
  );
}

// ── Port configuration panel ─────────────────────────────────────────────────
function PortConfigPanel({ currentUrl, portOverride, onApply, onClose }) {
  const [val, setVal] = useState(portOverride || "");

  // Parse current detected port from URL
  const detectedPort = currentUrl?.match(/cirrus=(\d+)/)?.[1] || "8685";

  return (
    <div style={{
      position:"absolute", top:36, right:8, zIndex:30,
      width:260, background:"#0f172a", border:"1px solid rgba(255,255,255,.12)",
      borderRadius:10, padding:14, boxShadow:"0 8px 24px rgba(0,0,0,.4)",
    }}>
      <div style={{ fontSize:12, fontWeight:700, color:"#e2e8f0", marginBottom:10 }}>
        Cirrus Port Configuration
      </div>

      <div style={{ fontSize:10, color:"#64748b", marginBottom:6 }}>
        Auto-detected: <strong style={{ color:"#94a3b8" }}>{detectedPort}</strong>
        {portOverride && <span style={{ color:"#f59e0b", marginLeft:4 }}>(overridden)</span>}
      </div>

      <input
        value={val}
        onChange={e => setVal(e.target.value.replace(/\D/g, ""))}
        placeholder={`Override port (default: ${detectedPort})`}
        onKeyDown={e => { if (e.key === "Enter") onApply(val); if (e.key === "Escape") onClose(); }}
        style={{
          width:"100%", padding:"6px 10px", borderRadius:7,
          background:"rgba(255,255,255,.07)", border:"1px solid rgba(255,255,255,.12)",
          color:"#e2e8f0", fontSize:12, outline:"none", boxSizing:"border-box",
        }}
        autoFocus
      />

      <div style={{ fontSize:10, color:"#475569", margin:"6px 0 10px" }}>
        Common Cirrus ports: 8685, 8585, 8080, 80<br/>
        Leave empty to use auto-detection.
      </div>

      <div style={{ display:"flex", gap:6 }}>
        <button onClick={() => onApply(val)} style={{
          flex:1, padding:"5px", borderRadius:6, border:"none",
          background:"#2563eb", color:"#fff", cursor:"pointer", fontSize:11, fontWeight:600,
        }}>Apply &amp; Reconnect</button>
        {portOverride && (
          <button onClick={() => onApply("")} style={{
            padding:"5px 8px", borderRadius:6, border:"1px solid rgba(255,255,255,.12)",
            background:"transparent", color:"#64748b", cursor:"pointer", fontSize:11,
          }}>Reset</button>
        )}
        <button onClick={onClose} style={{
          padding:"5px 8px", borderRadius:6, border:"1px solid rgba(255,255,255,.12)",
          background:"transparent", color:"#64748b", cursor:"pointer", fontSize:11,
        }}>✕</button>
      </div>
    </div>
  );
}

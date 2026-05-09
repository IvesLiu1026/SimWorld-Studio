import React, { useState, useEffect, useRef, useCallback } from "react";

/**
 * PixelStreamPlayer — wraps ue-player.html iframe with:
 *  - Auto-reconnect on disconnect (exponential backoff)
 *  - Connection status display
 *  - Keyboard/mouse input fix (focus management)
 *  - Parent ↔ iframe postMessage bridge
 */
export default function PixelStreamPlayer({ playerUrl }) {
  const iframeRef     = useRef(null);
  const [status, setStatus]     = useState("idle");     // idle | connecting | connected | disconnected | error
  const [active, setActive]     = useState(false);      // user clicked to enable input
  const [reconnects, setReconnects] = useState(0);
  const reconnectTimer = useRef(null);
  const pingTimer      = useRef(null);
  const lastConnected  = useRef(null);

  // Listen to messages from iframe
  useEffect(() => {
    function onMessage(e) {
      if (e.data?.type === "sw-stream-connected") {
        setStatus("connected");
        setReconnects(0);
        lastConnected.current = Date.now();
      }
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, []);

  // When playerUrl changes → reset to connecting
  useEffect(() => {
    if (!playerUrl) return;
    setStatus("connecting");
    setActive(false);
  }, [playerUrl]);

  // Reconnect by reloading iframe src
  const reconnect = useCallback(() => {
    if (!iframeRef.current || !playerUrl) return;
    setStatus("connecting");
    // Append cache-bust so iframe actually reloads
    const sep = playerUrl.includes("?") ? "&" : "?";
    iframeRef.current.src = playerUrl + sep + "_t=" + Date.now();
    // Tell iframe to reconnect
    try { iframeRef.current.contentWindow?.postMessage({ type: "sw-reconnect" }, "*"); } catch {}
  }, [playerUrl]);

  // Periodic health check — if connected but no ping for 30s, try reconnect
  useEffect(() => {
    if (status !== "connected") return;
    pingTimer.current = setInterval(() => {
      if (lastConnected.current && Date.now() - lastConnected.current > 45_000) {
        // No connected signal for 45s — may have silently dropped
        setStatus("disconnected");
        clearInterval(pingTimer.current);
        scheduleReconnect();
      }
    }, 5000);
    return () => clearInterval(pingTimer.current);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status]);

  function scheduleReconnect() {
    if (reconnectTimer.current) return;
    const delay = Math.min(1500 * Math.pow(1.5, reconnects), 20_000);
    setReconnects(n => n + 1);
    reconnectTimer.current = setTimeout(() => {
      reconnectTimer.current = null;
      reconnect();
    }, delay);
  }

  const activate = useCallback(() => {
    setActive(true);
    setTimeout(() => {
      iframeRef.current?.focus();
      iframeRef.current?.contentWindow?.postMessage({ type: "sw-focus-stream" }, "*");
    }, 80);
  }, []);

  // Colors per status
  const statusColor = { idle:"#64748b", connecting:"#f59e0b", connected:"#22c55e", disconnected:"#f59e0b", error:"#dc2626" }[status] || "#64748b";
  const statusLabel = { idle:"Initialising", connecting:"Connecting…", connected:"Live", disconnected:"Reconnecting…", error:"Error" }[status] || "";

  return (
    <div style={{ width:"100%", height:"100%", position:"relative", background:"#0b1220" }}>

      {/* iframe — kept always mounted to preserve WebRTC state */}
      {playerUrl && (
        <iframe
          ref={iframeRef}
          src={playerUrl}
          onLoad={() => { if (status === "idle" || status === "disconnected") setStatus("connecting"); }}
          style={{
            width:"100%", height:"100%", border:"none", display:"block",
            pointerEvents: (active && status === "connected") ? "auto" : "none",
          }}
          allow="pointer-lock *; fullscreen *; autoplay *; clipboard-read *; clipboard-write *; camera *; microphone *"
          allowFullScreen
          tabIndex={0}
          title="UE Pixel Streaming"
        />
      )}

      {/* Status badge — always visible, top-left */}
      <div style={{
        position:"absolute", top:8, left:8, zIndex:20,
        display:"inline-flex", alignItems:"center", gap:6,
        background:"rgba(11,18,32,0.75)", backdropFilter:"blur(6px)",
        border:"1px solid rgba(255,255,255,0.08)",
        borderRadius:8, padding:"4px 10px",
        fontSize:11, fontWeight:600, color:"#e2e8f0",
        userSelect:"none", pointerEvents:"none",
      }}>
        <span style={{
          width:7, height:7, borderRadius:"50%", background:statusColor, flexShrink:0,
          boxShadow: status === "connected" ? `0 0 6px ${statusColor}` : "none",
          animation: status === "connecting" || status === "disconnected" ? "ps-pulse 1.2s ease-in-out infinite" : "none",
        }}/>
        {statusLabel}
        {reconnects > 0 && status !== "connected" && (
          <span style={{ color:"#64748b", marginLeft:2 }}>#{reconnects}</span>
        )}
      </div>

      {/* Manual reconnect button (shown if not connected and not actively connecting) */}
      {(status === "error" || (status === "disconnected" && !reconnectTimer.current)) && (
        <button
          onClick={() => { setReconnects(0); reconnect(); }}
          style={{
            position:"absolute", top:36, left:8, zIndex:20,
            padding:"5px 12px", borderRadius:7, border:"1px solid #2563eb",
            background:"rgba(37,99,235,0.2)", color:"#93c5fd",
            fontSize:11, fontWeight:600, cursor:"pointer",
            backdropFilter:"blur(4px)",
          }}
        >
          ↻ Reconnect
        </button>
      )}

      {/* Activation overlay — shown when stream is connected but input not enabled */}
      {status === "connected" && !active && (
        <div
          onClick={activate}
          style={{
            position:"absolute", inset:0, zIndex:10,
            display:"flex", flexDirection:"column", alignItems:"center", justifyContent:"center",
            background:"rgba(11,18,32,0.6)", backdropFilter:"blur(2px)",
            cursor:"pointer", gap:12,
          }}
        >
          <div style={{
            width:56, height:56, borderRadius:14,
            background:"rgba(37,99,235,0.2)", border:"2px solid rgba(37,99,235,0.5)",
            display:"flex", alignItems:"center", justifyContent:"center", fontSize:28,
          }}>🎮</div>
          <div style={{ color:"#e2e8f0", fontSize:16, fontWeight:700 }}>Click to Control</div>
          <div style={{ color:"#94a3b8", fontSize:12, textAlign:"center", lineHeight:1.6 }}>
            Enables keyboard &amp; mouse control.<br/>
            <span style={{ color:"#64748b" }}>Esc</span> to release cursor.
          </div>
          <div style={{ display:"flex", gap:20, marginTop:4, color:"#64748b", fontSize:11 }}>
            <span>🖱 Click &amp; drag to look</span>
            <span>⌨ WASD to move</span>
          </div>
        </div>
      )}

      {/* Connecting/loading overlay */}
      {(status === "idle" || status === "connecting") && (
        <div style={{
          position:"absolute", inset:0, zIndex:10,
          display:"flex", flexDirection:"column", alignItems:"center", justifyContent:"center",
          background:"rgba(11,18,32,0.85)", gap:16,
          pointerEvents:"none",
        }}>
          <div style={{
            width:40, height:40, borderRadius:"50%",
            border:"3px solid rgba(37,99,235,0.2)", borderTopColor:"#2563eb",
            animation:"ps-spin 0.9s linear infinite",
          }}/>
          <div style={{ color:"#e2e8f0", fontSize:14, fontWeight:600 }}>Connecting to UE Stream</div>
          <div style={{ color:"#64748b", fontSize:12 }}>
            {playerUrl ? `Cirrus port: ${new URL(playerUrl, location.href).searchParams.get("cirrus") || "..."}` : "Waiting for stream URL…"}
          </div>
        </div>
      )}

      {/* No URL state */}
      {!playerUrl && (
        <div style={{
          position:"absolute", inset:0, zIndex:10,
          display:"flex", flexDirection:"column", alignItems:"center", justifyContent:"center",
          background:"rgba(11,18,32,0.85)", gap:12, pointerEvents:"none",
        }}>
          <div style={{ fontSize:32, opacity:0.4 }}>📡</div>
          <div style={{ color:"#64748b", fontSize:13 }}>No stream URL — is Cirrus running?</div>
        </div>
      )}

      <style>{`
        @keyframes ps-spin { to{transform:rotate(360deg)} }
        @keyframes ps-pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
      `}</style>
    </div>
  );
}

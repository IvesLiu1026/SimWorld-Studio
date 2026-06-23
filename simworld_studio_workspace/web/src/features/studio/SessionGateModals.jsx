import React from "react";

function SessionModal({ children, icon, title, variant = "solid" }) {
  return (
    <div className={`sw-session-overlay ${variant}`}>
      <div className="sw-session-card">
        <div className="sw-session-icon">{icon}</div>
        <div className="sw-session-title">{title}</div>
        {children}
      </div>
    </div>
  );
}

export default function SessionGateModals({ expired, icons, onRetry, onStartNewSession, poolFull }) {
  if (poolFull) {
    return (
      <SessionModal icon={icons.clock(48)} title="Server at capacity">
        <div className="sw-session-message">
          All simulation slots are currently in use.
          <br />
          {poolFull.queueLength > 0 && (
            <>
              Queue length: <strong>{poolFull.queueLength}</strong>
              <br />
            </>
          )}
          {poolFull.message}
        </div>
        <button onClick={onRetry} className="sw-btn-blue sw-session-action">
          Try again
        </button>
      </SessionModal>
    );
  }

  if (expired) {
    return (
      <SessionModal icon={icons.lock(48)} title="Session ended" variant="blurred">
        <div className="sw-session-message">
          Your 30-minute session has expired.
          <br />
          Refresh to start a new session.
        </div>
        <button onClick={onStartNewSession} className="sw-btn-blue sw-session-action">
          Start new session
        </button>
      </SessionModal>
    );
  }

  return null;
}

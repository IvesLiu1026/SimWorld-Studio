import React, { useCallback, useRef, useState } from "react";
import { Btn } from "../../components/ui/primitives.jsx";

export default function AnnotateOverlay({ onCancel, onSubmitFeedback, src }) {
  const [points, setPoints] = useState([]);
  const [feedbackText, setFeedbackText] = useState("");
  const imgRef = useRef(null);

  const handleImageClick = useCallback((event) => {
    const rect = event.currentTarget.getBoundingClientRect();
    const x = (event.clientX - rect.left) / rect.width;
    const y = (event.clientY - rect.top) / rect.height;
    const text = prompt("Describe what to change at this point:");
    if (text) setPoints((prev) => [...prev, { x, y, text }]);
  }, []);

  const removePoint = (index) => {
    setPoints((prev) => prev.filter((_, itemIndex) => itemIndex !== index));
  };

  const handleSubmit = () => {
    let result = feedbackText;
    if (points.length > 0) {
      result += "\n\nAnnotated points on the screenshot:";
      for (const point of points) {
        const pctX = Math.round(point.x * 100);
        const pctY = Math.round(point.y * 100);
        result += `\n- At position (${pctX}% from left, ${pctY}% from top): "${point.text}"`;
      }
    }
    onSubmitFeedback(result.trim(), points);
  };

  const submitDisabled = !feedbackText && points.length === 0;

  return (
    <div
      style={{
        position: "absolute",
        inset: 0,
        background: "var(--modal-backdrop)",
        display: "flex",
        flexDirection: "column",
        zIndex: 100,
      }}
    >
      <div
        style={{
          padding: "8px 14px",
          borderBottom: "1px solid var(--line)",
          display: "flex",
          alignItems: "center",
          gap: 8,
          background: "var(--panel)",
        }}
      >
        <span style={{ fontSize: 13, fontWeight: 600, color: "var(--orange)" }}>
          Annotate Screenshot
        </span>
        <span style={{ fontSize: 12, color: "var(--ink-3)" }}>
          Click on the image to add feedback points
        </span>
        <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          <Btn variant="cancel" size="xs" onClick={onCancel}>
            Cancel
          </Btn>
          <Btn variant="primary" size="xs" disabled={submitDisabled} onClick={handleSubmit}>
            Send Feedback
          </Btn>
        </div>
      </div>

      <div style={{ flex: 1, display: "flex", gap: 0, overflow: "hidden" }}>
        <div style={{ flex: 1, position: "relative", overflow: "hidden" }}>
          <div
            onClick={handleImageClick}
            style={{
              width: "100%",
              height: "100%",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              cursor: "crosshair",
              position: "relative",
            }}
          >
            <img
              ref={imgRef}
              src={src}
              alt="Annotate"
              style={{ maxWidth: "100%", maxHeight: "100%", objectFit: "contain" }}
            />
            {points.map((point, index) => (
              <div
                key={`${point.x}-${point.y}-${index}`}
                style={{
                  position: "absolute",
                  left: `${point.x * 100}%`,
                  top: `${point.y * 100}%`,
                  transform: "translate(-50%, -50%)",
                  pointerEvents: "auto",
                }}
              >
                <button
                  type="button"
                  style={{
                    width: 24,
                    height: 24,
                    borderRadius: "50%",
                    background: "var(--orange)",
                    border: "2px solid var(--panel)",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    fontSize: 12,
                    fontWeight: 700,
                    color: "var(--accent-ink)",
                    cursor: "pointer",
                    padding: 0,
                  }}
                  onClick={(event) => {
                    event.stopPropagation();
                    removePoint(index);
                  }}
                  title="Remove annotation"
                >
                  {index + 1}
                </button>
                <div
                  style={{
                    position: "absolute",
                    left: 16,
                    top: -4,
                    background: "var(--panel-2)",
                    border: "1px solid var(--line)",
                    borderRadius: 4,
                    padding: "2px 6px",
                    fontSize: 12,
                    color: "var(--ink)",
                    whiteSpace: "nowrap",
                    maxWidth: 200,
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                  }}
                >
                  {point.text}
                </div>
              </div>
            ))}
          </div>
        </div>

        <div
          style={{
            width: 260,
            borderLeft: "1px solid var(--line)",
            background: "var(--bg)",
            display: "flex",
            flexDirection: "column",
            padding: 12,
            gap: 8,
          }}
        >
          <span style={{ fontSize: 12, fontWeight: 600, color: "var(--ink)" }}>Feedback</span>
          <textarea
            value={feedbackText}
            onChange={(event) => setFeedbackText(event.target.value)}
            placeholder="Describe what to change overall..."
            style={{
              flex: 1,
              resize: "none",
              background: "var(--panel)",
              border: "1px solid var(--line)",
              borderRadius: 6,
              color: "var(--ink)",
              padding: 8,
              fontSize: 12,
              fontFamily: "inherit",
              cursor: "text",
            }}
          />
          {points.length > 0 && (
            <div style={{ fontSize: 12, color: "var(--ink-3)" }}>
              <div style={{ fontWeight: 600, marginBottom: 4 }}>Annotations:</div>
              {points.map((point, index) => (
                <div key={`${point.x}-${point.y}-${index}`} style={{ display: "flex", gap: 4, alignItems: "flex-start", marginBottom: 4 }}>
                  <span
                    style={{
                      width: 16,
                      height: 16,
                      borderRadius: "50%",
                      background: "var(--orange)",
                      fontSize: 12,
                      color: "var(--accent-ink)",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      flexShrink: 0,
                    }}
                  >
                    {index + 1}
                  </span>
                  <span style={{ fontSize: 12, color: "var(--ink)" }}>{point.text}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

import React from "react";

const BADGE_VARIANTS = {
  blue: {
    background: "var(--blue-soft)",
    color: "var(--blue)",
    border: "1px solid color-mix(in srgb, var(--blue) 30%, transparent)",
  },
  green: {
    background: "var(--green-soft)",
    color: "var(--green)",
    border: "1px solid color-mix(in srgb, var(--green) 30%, transparent)",
  },
  orange: {
    background: "var(--orange-soft)",
    color: "var(--orange)",
    border: "1px solid color-mix(in srgb, var(--orange) 30%, transparent)",
  },
  red: {
    background: "color-mix(in srgb, var(--red) 12%, transparent)",
    color: "var(--red)",
    border: "1px solid color-mix(in srgb, var(--red) 30%, transparent)",
  },
  muted: {
    background: "var(--panel-2)",
    color: "var(--ink-3)",
    border: "1px solid var(--line)",
  },
  violet: {
    background: "var(--violet-soft)",
    color: "var(--violet)",
    border: "1px solid color-mix(in srgb, var(--violet) 30%, transparent)",
  },
};

export function Badge({ variant = "muted", children, style, dot }) {
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        padding: "2px 8px",
        borderRadius: 6,
        fontSize: 11,
        fontWeight: 700,
        whiteSpace: "nowrap",
        ...BADGE_VARIANTS[variant],
        ...style,
      }}
    >
      {dot && (
        <span
          style={{ width: 6, height: 6, borderRadius: "50%", background: "currentColor", flexShrink: 0 }}
        />
      )}
      {children}
    </span>
  );
}

export function SourceBadge({ source, style }) {
  const map = {
    builtin: ["muted", "builtin"],
    custom: ["blue", "custom"],
    learned: ["blue", "learned"],
  };
  const [variant, label] = map[source] || ["muted", source];
  return (
    <Badge variant={variant} style={style}>
      {label}
    </Badge>
  );
}

export function StatusBadge({ enabled, readOnly, style }) {
  if (readOnly) {
    return (
      <Badge variant="muted" style={style}>
        Static MCP
      </Badge>
    );
  }
  return (
    <Badge variant={enabled ? "green" : "muted"} style={style}>
      {enabled ? "Enabled" : "Disabled"}
    </Badge>
  );
}

const TAG_COLORS = {
  city: "59 130 246",
  buildings: "185 28 28",
  props: "100 116 139",
  weather: "234 88 12",
  camera: "124 58 237",
  layout: "22 163 74",
  planning: "59 130 246",
  spacing: "245 158 11",
  trees: "22 163 74",
  vehicles: "234 88 12",
  lighting: "245 158 11",
  atmosphere: "124 58 237",
  screenshot: "124 58 237",
  decoration: "100 116 139",
  furniture: "100 116 139",
  architecture: "185 28 28",
  environment: "22 163 74",
  viewpoint: "124 58 237",
  placement: "245 158 11",
  roads: "100 116 139",
  capture: "124 58 237",
};

export function tagChipSx(tag, overrides = {}) {
  const color = TAG_COLORS[tag];
  return {
    fontSize: 11,
    padding: "2px 6px",
    borderRadius: 4,
    background: color ? `rgb(${color} / 0.18)` : "var(--panel-2)",
    color: color ? `rgb(${color})` : "var(--ink-3)",
    border: `1px solid ${color ? `rgb(${color} / 0.28)` : "var(--line)"}`,
    ...overrides,
  };
}

export function TagChip({ tag }) {
  return <span style={tagChipSx(tag)}>{tag}</span>;
}

const BTN_VARIANTS = {
  primary: { background: "var(--blue)", color: "var(--accent-ink)", border: "1px solid var(--blue)" },
  success: { background: "var(--green)", color: "var(--accent-ink)", border: "1px solid var(--green)" },
  danger: {
    background: "color-mix(in srgb, var(--red) 10%, transparent)",
    color: "var(--red)",
    border: "1px solid color-mix(in srgb, var(--red) 30%, transparent)",
  },
  enable: {
    background: "var(--blue-soft)",
    color: "var(--blue)",
    border: "1px solid color-mix(in srgb, var(--blue) 40%, transparent)",
  },
  disable: { background: "var(--panel-2)", color: "var(--ink-2)", border: "1px solid var(--line)" },
  cancel: { background: "var(--panel-2)", color: "var(--ink-2)", border: "1px solid var(--line)" },
  ghost: { background: "transparent", color: "var(--ink-2)", border: "1px solid var(--line)" },
};

const BTN_SIZES = {
  xs: { padding: "3px 8px", fontSize: 11 },
  sm: { padding: "5px 12px", fontSize: 12 },
  md: { padding: "7px 16px", fontSize: 13 },
};

export function Btn({ variant = "ghost", size = "sm", onClick, disabled, children, style, ...rest }) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 5,
        borderRadius: 6,
        fontFamily: "inherit",
        fontWeight: 600,
        cursor: disabled ? "wait" : "pointer",
        opacity: disabled ? 0.45 : 1,
        transition: "opacity 0.12s",
        ...BTN_SIZES[size],
        ...BTN_VARIANTS[variant],
        ...style,
      }}
      {...rest}
    >
      {children}
    </button>
  );
}

export function ToggleBtn({ enabled, busy, onClick, style }) {
  return (
    <Btn variant={enabled ? "disable" : "enable"} disabled={busy} onClick={onClick} style={style}>
      {busy ? "Working..." : enabled ? "Disable" : "Enable"}
    </Btn>
  );
}

export function ModalOverlay({ onClose, children, maxWidth = 750, maxHeight = "85vh" }) {
  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: "var(--modal-backdrop)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 9999,
        backdropFilter: "blur(3px)",
      }}
      onClick={onClose}
    >
      <div
        onClick={(event) => event.stopPropagation()}
        style={{
          width: "90%",
          maxWidth,
          maxHeight,
          background: "var(--panel)",
          border: "1px solid var(--line)",
          borderRadius: 10,
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
          boxShadow: "var(--shadow-pop)",
        }}
      >
        {children}
      </div>
    </div>
  );
}

function DefaultCloseIcon() {
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" aria-hidden="true">
      <path d="M6 6l12 12M18 6L6 18" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
  );
}

export function ModalHeader({ title, subtitle, onClose, closeIcon = <DefaultCloseIcon />, children }) {
  return (
    <div
      style={{
        padding: "14px 20px",
        borderBottom: "1px solid var(--line)",
        display: "flex",
        alignItems: "center",
        gap: 10,
        background: "var(--panel-3)",
        flexShrink: 0,
      }}
    >
      <div style={{ flex: 1, minWidth: 0 }}>
        {title && (
          <div
            style={{
              fontSize: 16,
              fontWeight: 700,
              color: "var(--ink)",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {title}
          </div>
        )}
        {subtitle && <div style={{ fontSize: 12, color: "var(--ink-3)", marginTop: 3 }}>{subtitle}</div>}
      </div>
      {children}
      {onClose && (
        <button
          onClick={onClose}
          style={{
            width: 28,
            height: 28,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            borderRadius: 6,
            border: "none",
            background: "transparent",
            color: "var(--ink-3)",
            cursor: "pointer",
            flexShrink: 0,
          }}
          onMouseEnter={(event) => {
            event.currentTarget.style.background = "var(--bg-hover)";
          }}
          onMouseLeave={(event) => {
            event.currentTarget.style.background = "transparent";
          }}
          title="Close"
        >
          {closeIcon}
        </button>
      )}
    </div>
  );
}

export function ModalFooter({ children }) {
  return (
    <div
      style={{
        padding: "12px 20px",
        borderTop: "1px solid var(--line)",
        display: "flex",
        gap: 8,
        justifyContent: "flex-end",
        background: "var(--panel-3)",
        flexShrink: 0,
      }}
    >
      {children}
    </div>
  );
}

export function PageHeader({ icon, title, subtitle, action }) {
  return (
    <div
      style={{
        padding: "14px 24px",
        borderBottom: "1px solid var(--line)",
        display: "flex",
        alignItems: "center",
        gap: 12,
        flexShrink: 0,
        background: "var(--panel)",
      }}
    >
      {icon && (
        <span style={{ display: "inline-flex", alignItems: "center", color: "var(--ink-2)", flexShrink: 0 }}>
          {icon}
        </span>
      )}
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 16, fontWeight: 700, color: "var(--ink)" }}>{title}</div>
        {subtitle && <div style={{ fontSize: 12, color: "var(--ink-3)", marginTop: 2 }}>{subtitle}</div>}
      </div>
      {action}
    </div>
  );
}

export function Eyebrow({ children, style }) {
  return (
    <div
      style={{
        fontSize: 11,
        fontWeight: 700,
        letterSpacing: "0.07em",
        textTransform: "uppercase",
        color: "var(--ink-2)",
        ...style,
      }}
    >
      {children}
    </div>
  );
}

export function Field({ label, children }) {
  return (
    <div>
      {label && (
        <label style={{ fontSize: 12, color: "var(--ink-3)", display: "block", marginBottom: 4 }}>{label}</label>
      )}
      {children}
    </div>
  );
}

export const inputSx = {
  width: "100%",
  padding: "7px 12px",
  fontSize: 13,
  fontFamily: "inherit",
  background: "var(--bg-tertiary)",
  border: "1px solid var(--line)",
  borderRadius: 6,
  color: "var(--ink)",
  outline: "none",
  cursor: "text",
  boxSizing: "border-box",
};

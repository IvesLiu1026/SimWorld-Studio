import React, { useEffect, useMemo, useRef, useState } from "react";
import ClaudeIcon from "@lobehub/icons/es/Claude/components/Color.js";
import CursorIcon from "@lobehub/icons/es/Cursor/components/Mono.js";
import GeminiIcon from "@lobehub/icons/es/Gemini/components/Color.js";
import GrokIcon from "@lobehub/icons/es/Grok/components/Mono.js";
import OpenAIIcon from "@lobehub/icons/es/OpenAI/components/Mono.js";
import XAIIcon from "@lobehub/icons/es/XAI/components/Mono.js";

const AGENT_PROVIDERS = {
  claude: "claude",
  codex: "openai",
  cursor: "cursor",
  gemini: "gemini",
  grok: "grok",
  opencode: "opencode",
};

const PROVIDER_ICONS = {
  claude: ClaudeIcon,
  cursor: CursorIcon,
  gemini: GeminiIcon,
  grok: GrokIcon,
  openai: OpenAIIcon,
  xai: XAIIcon,
};

function getModelProvider(modelId, fallbackProvider) {
  const normalized = String(modelId || "").toLowerCase();
  if (normalized.includes("anthropic/") || normalized.includes("claude")) return "claude";
  if (normalized.includes("google/") || normalized.includes("gemini")) return "gemini";
  if (normalized.includes("openai/") || normalized.includes("gpt") || normalized === "o3" || normalized.startsWith("o")) {
    return "openai";
  }
  if (normalized.includes("xai/") || normalized.includes("grok")) return "grok";
  if (normalized.includes("composer")) return "cursor";
  return fallbackProvider;
}

function BrandIcon({ provider }) {
  const Icon = PROVIDER_ICONS[provider];
  if (!Icon) {
    return (
      <span className={`sw-menu-select-brand ${provider || "generic"}`} aria-hidden="true">
        {provider === "opencode" ? "OC" : "AI"}
      </span>
    );
  }
  return (
    <span className={`sw-menu-select-brand ${provider || "default"}`} aria-hidden="true">
      <Icon size={14} />
    </span>
  );
}

function MenuSelect({ ariaLabel, className = "", options, value, onChange }) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef(null);
  const selected = options.find((option) => option.value === value) || options[0];

  useEffect(() => {
    if (!open) return undefined;

    const closeOnOutside = (event) => {
      if (!rootRef.current?.contains(event.target)) setOpen(false);
    };
    const closeOnEscape = (event) => {
      if (event.key === "Escape") setOpen(false);
    };

    document.addEventListener("pointerdown", closeOnOutside);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutside);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  return (
    <div ref={rootRef} className={`sw-menu-select ${className}${open ? " open" : ""}`}>
      <button
        type="button"
        className="sw-menu-select-trigger"
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <BrandIcon provider={selected?.provider} />
        <span className="sw-menu-select-label">{selected?.label || "Select"}</span>
        <span className="sw-menu-select-caret" aria-hidden="true" />
      </button>
      {open ? (
        <div className="sw-menu-select-menu" role="listbox" aria-label={ariaLabel}>
          {options.map((option) => (
            <button
              key={option.value}
              type="button"
              role="option"
              aria-selected={option.value === value}
              className={`sw-menu-select-option${option.value === value ? " selected" : ""}`}
              onClick={() => {
                onChange(option.value);
                setOpen(false);
              }}
            >
              <BrandIcon provider={option.provider} />
              <span className="sw-menu-select-option-label">{option.label}</span>
              {option.detail ? <span className="sw-menu-select-option-detail">{option.detail}</span> : null}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

export default function CodingAgentSelector({ agents, agent, locked = false, setAgent, model, setModel, icon }) {
  const cfg = agents[agent] || {};
  const models = cfg.models || [];
  const derivedCustom = !!model && !models.includes(model);
  const [forceCustom, setForceCustom] = useState(false);
  const showCustom = forceCustom || derivedCustom;
  const activeAgentProvider = AGENT_PROVIDERS[agent] || "openai";
  const agentOptions = useMemo(
    () =>
      Object.entries(agents).map(([id, agentDef]) => ({
        value: id,
        label: agentDef.label || id,
        detail: agentDef.models?.length ? `${agentDef.models.length} profiles` : "Default",
        provider: AGENT_PROVIDERS[id] || "openai",
      })),
    [agents],
  );
  const modelOptions = useMemo(
    () => [
      { value: "", label: "Default", detail: "Runtime default", provider: activeAgentProvider },
      ...models.map((modelId) => ({
        value: modelId,
        label: modelId,
        detail: "Configured",
        provider: getModelProvider(modelId, activeAgentProvider),
      })),
      {
        value: "__custom__",
        label: "Custom profile",
        detail: "Manual id",
        provider: getModelProvider(model, activeAgentProvider),
      },
    ],
    [activeAgentProvider, model, models],
  );

  useEffect(() => {
    if (!derivedCustom) setForceCustom(false);
  }, [derivedCustom, agent]);

  const onModelChange = (value) => {
    if (value === "__custom__") setForceCustom(true);
    else {
      setForceCustom(false);
      setModel(value);
    }
  };

  if (locked) {
    return (
      <div
        className="coding-agent-selector locked"
        title="This runtime is managed by the production deployment policy"
      >
        {icon ? <span className="coding-agent-selector-icon">{icon}</span> : null}
        <div className="coding-agent-managed" aria-label="Managed scene build runtime">
          <span className="coding-agent-managed-label">Managed runtime</span>
          <strong>{cfg.label || agent} · {model || cfg.defaultModel || "Default"}</strong>
        </div>
      </div>
    );
  }

  return (
    <div
      className="coding-agent-selector"
      title="Scene build runtime and execution profile"
    >
      {icon ? <span className="coding-agent-selector-icon">{icon}</span> : null}
      <MenuSelect
        ariaLabel="Execution backend"
        className="agent-menu"
        options={agentOptions}
        value={agent}
        onChange={setAgent}
      />
      <MenuSelect
        ariaLabel="Runtime profile"
        className="model-menu"
        options={modelOptions}
        value={showCustom ? "__custom__" : model || ""}
        onChange={onModelChange}
      />
      {showCustom && (
        <input
          className="coding-agent-custom-input"
          value={model}
          onChange={(event) => setModel(event.target.value)}
          placeholder="profile id"
          aria-label="Custom runtime profile id"
        />
      )}
    </div>
  );
}

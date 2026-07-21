import React, { memo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import ToolCallBlock from "./ToolCallBlock.jsx";

function MarkdownBlock({ children }) {
  if (!children) return null;
  return (
    <div className="markdown chat-markdown">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{children}</ReactMarkdown>
    </div>
  );
}

function errorMessage(value, fallback = "") {
  if (typeof value === "string") return value.trim();
  if (value && typeof value === "object") {
    return String(value.message || value.error || value.code || "").trim();
  }
  return value === true ? fallback : "";
}

const USD_FORMAT = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 6,
});

function ReviewAccounting({ accounting }) {
  if (!accounting || !Number.isFinite(accounting.spentUsd)) return null;
  const summary = [
    `Review cost ${USD_FORMAT.format(accounting.spentUsd)}`,
    Number.isFinite(accounting.limitUsd) ? `/ ${USD_FORMAT.format(accounting.limitUsd)}` : "",
    Number.isFinite(accounting.remainingUsd)
      ? `· ${USD_FORMAT.format(accounting.remainingUsd)} remaining`
      : "",
  ].filter(Boolean).join(" ");
  const detail = [
    Number.isFinite(accounting.builderCostUsd)
      ? `Builder ${USD_FORMAT.format(accounting.builderCostUsd)}`
      : "",
    Number.isFinite(accounting.criticCostUsd)
      ? `Critic ${USD_FORMAT.format(accounting.criticCostUsd)}`
      : "",
  ].filter(Boolean).join(" · ");
  return (
    <div
      aria-label="Review budget"
      className={`review-accounting${accounting.exhausted ? " review-accounting-exhausted" : ""}`}
    >
      <div>{summary}</div>
      {detail && <div className="review-accounting-detail">{detail}</div>}
    </div>
  );
}

// Structured execution blocks: asset provenance and scene-review lifecycle.
function LoopBlock({ block }) {
  if (block.type === "asset_retrieval") {
    const status = String(block.status || "unknown").toLowerCase();
    const tone = status === "ready" ? "pass" : status === "blocked" ? "fail" : "warn";
    const reasonCode = block.reason?.code || block.code;
    const reasonMessage = block.reason?.message || block.message;
    const details = [
      block.mode ? `mode ${block.mode}` : "",
      block.snapshotRevision ? `snapshot ${block.snapshotRevision}` : "",
      block.degradedMode && block.degradedMode !== "disabled" ? `fallback ${block.degradedMode}` : "",
    ].filter(Boolean).join(" · ");
    return (
      <div className={`asset-retrieval-block asset-retrieval-${tone}`}>
        <div className="loop-critic-head">Asset source: {status.replaceAll("_", " ")}</div>
        {details && <div className="asset-retrieval-meta">{details}</div>}
        {(reasonCode || reasonMessage) && (
          <div className="asset-retrieval-reason">
            {[reasonCode, reasonMessage].filter(Boolean).join(" — ")}
          </div>
        )}
      </div>
    );
  }
  if (block.type === "round_header") {
    return (
      <div className="loop-round-header">
        Round {block.round}{block.total ? ` / ${block.total}` : ""} · Build
      </div>
    );
  }
  if (block.type === "critic") {
    const st = String(block.status || "").toUpperCase();
    const tone = st.includes("PASS") ? "pass" : (st.includes("FAIL") ? "fail" : "warn");
    const issues = Array.isArray(block.issues) ? block.issues : [];
    const sugg = Array.isArray(block.suggestions) ? block.suggestions : [];
    const runtime = [block.provider, block.model].filter(Boolean).join(" · ");
    const failure = errorMessage(block.error, issues.length === 0 ? "Review execution failed" : "");
    const txt = (x) => (typeof x === "string" ? x : (x && (x.text || x.message)) || JSON.stringify(x));
    return (
      <div className={`loop-critic loop-critic-${tone}`}>
        <div className="loop-critic-head">
          Review: {st || "PENDING"}{block.round ? ` · round ${block.round}` : ""}{runtime ? ` · ${runtime}` : ""}
        </div>
        {failure && <p><strong>Failure:</strong> {failure}</p>}
        {issues.length > 0 && <ul className="loop-critic-issues">{issues.map((x, i) => <li key={i}>{txt(x)}</li>)}</ul>}
        {sugg.length > 0 && <ul className="loop-critic-suggestions">{sugg.map((x, i) => <li key={i}>{txt(x)}</li>)}</ul>}
      </div>
    );
  }
  if (block.type === "loop_done") {
    const failure = errorMessage(block.error);
    return (
      <div className="loop-done">
        <div>
          Validation cycle complete — {block.reason || "done"}{block.rounds ? ` · ${block.rounds} round(s)` : ""}{block.finalStatus ? ` · ${block.finalStatus}` : ""}
        </div>
        {failure && <div><strong>Failure:</strong> {failure}</div>}
      </div>
    );
  }
  return null;
}

function AssistantContent({ fallbackToolIcon, message, toolIcons }) {
  const toolCalls = message.toolCalls || [];

  return (
    <>
      {message.waiting && (
        <div className="chat-waiting">
          <span />
          Processing command...
        </div>
      )}
      {message.blocks
        ? message.blocks.map((block, index) => (
            block.type === "text" ? (
              <MarkdownBlock key={`text-${index}`}>{block.content}</MarkdownBlock>
            ) : (block.type === "asset_retrieval" || block.type === "round_header" || block.type === "critic" || block.type === "loop_done") ? (
              <LoopBlock key={`loop-${index}`} block={block} />
            ) : (
              <ToolCallBlock
                key={block.toolId}
                fallbackIcon={fallbackToolIcon}
                tool={toolCalls.find((toolCall) => toolCall.id === block.toolId)}
                toolIcons={toolIcons}
              />
            )
          ))
        : (
          <>
            <MarkdownBlock>{message.content}</MarkdownBlock>
            {toolCalls.map((toolCall) => (
              <ToolCallBlock
                key={toolCall.id}
                fallbackIcon={fallbackToolIcon}
                tool={toolCall}
                toolIcons={toolIcons}
              />
            ))}
          </>
        )}
    </>
  );
}

const ChatMessage = memo(function ChatMessage({ fallbackToolIcon, message, toolIcons }) {
  const isUser = message.role === "user";
  const time = new Date(message.timestamp).toLocaleTimeString();
  const bubble = (
    <div className={isUser ? "sw-bubble-user chat-bubble user" : "sw-bubble-assistant chat-bubble assistant"}>
      {isUser ? (
        <div className="chat-user-text">{message.content}</div>
      ) : (
        <>
          <AssistantContent
            fallbackToolIcon={fallbackToolIcon}
            message={message}
            toolIcons={toolIcons}
          />
          <ReviewAccounting accounting={message.reviewAccounting} />
        </>
      )}
    </div>
  );

  return (
    <div className={`chat-message ${isUser ? "user" : "assistant"}`}>
      <div className="chat-record-head">
        <span className="chat-record-kind">{isUser ? "REQUEST" : "RESULT"}</span>
        <time>{time}</time>
      </div>
      <div className={`chat-message-row ${isUser ? "user" : "assistant"}`}>
        <div className="chat-message-bubble-wrap">{bubble}</div>
      </div>
    </div>
  );
});

export function TypingIndicator() {
  return (
    <div className="chat-typing">
      <span />
      Processing command...
    </div>
  );
}

export default ChatMessage;

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

// Scene-loop (build-critic) blocks: round headers, critic verdicts, loop-done.
function LoopBlock({ block }) {
  if (block.type === "round_header") {
    return (
      <div className="loop-round-header">
        Round {block.round}{block.total ? ` / ${block.total}` : ""} · Developer
      </div>
    );
  }
  if (block.type === "critic") {
    const st = String(block.status || "").toUpperCase();
    const tone = st.includes("PASS") ? "pass" : (st.includes("FAIL") ? "fail" : "warn");
    const issues = Array.isArray(block.issues) ? block.issues : [];
    const sugg = Array.isArray(block.suggestions) ? block.suggestions : [];
    const txt = (x) => (typeof x === "string" ? x : (x && (x.text || x.message)) || JSON.stringify(x));
    return (
      <div className={`loop-critic loop-critic-${tone}`}>
        <div className="loop-critic-head">Critic: {st || "REVIEW"}{block.round ? ` · round ${block.round}` : ""}</div>
        {issues.length > 0 && <ul className="loop-critic-issues">{issues.map((x, i) => <li key={i}>{txt(x)}</li>)}</ul>}
        {sugg.length > 0 && <ul className="loop-critic-suggestions">{sugg.map((x, i) => <li key={i}>{txt(x)}</li>)}</ul>}
      </div>
    );
  }
  if (block.type === "loop_done") {
    return (
      <div className="loop-done">
        Loop finished — {block.reason || "done"}{block.rounds ? ` · ${block.rounds} round(s)` : ""}{block.finalStatus ? ` · ${block.finalStatus}` : ""}
      </div>
    );
  }
  return null;
}

function AssistantContent({ agentLabel, fallbackToolIcon, message, toolIcons }) {
  const toolCalls = message.toolCalls || [];

  return (
    <>
      {message.waiting && (
        <div className="chat-waiting">
          <span />
          Waiting for {agentLabel || "agent"}...
        </div>
      )}
      {message.blocks
        ? message.blocks.map((block, index) => (
            block.type === "text" ? (
              <MarkdownBlock key={`text-${index}`}>{block.content}</MarkdownBlock>
            ) : (block.type === "round_header" || block.type === "critic" || block.type === "loop_done") ? (
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

const ChatMessage = memo(function ChatMessage({ agentLabel, fallbackToolIcon, message, toolIcons }) {
  const isUser = message.role === "user";
  const time = new Date(message.timestamp).toLocaleTimeString();
  const assistantName = agentLabel || "SimCoder";
  const bubble = (
    <div className={isUser ? "sw-bubble-user chat-bubble user" : "sw-bubble-assistant chat-bubble assistant"}>
      {isUser ? (
        <div className="chat-user-text">{message.content}</div>
      ) : (
        <AssistantContent
          agentLabel={agentLabel}
          fallbackToolIcon={fallbackToolIcon}
          message={message}
          toolIcons={toolIcons}
        />
      )}
    </div>
  );

  if (isUser) {
    return (
      <div className="chat-message user">
        <div className="chat-message-row user">
          <div className="chat-message-bubble-wrap">{bubble}</div>
          <div className="chat-avatar user">
            <svg viewBox="0 0 24 24" fill="none" width="14" height="14">
              <circle cx="12" cy="8" r="4" fill="currentColor" />
              <path d="M4 20c0-4 3.6-7 8-7s8 3 8 7" fill="currentColor" />
            </svg>
          </div>
        </div>
        <div className="chat-meta user">You - {time}</div>
      </div>
    );
  }

  return (
    <div className="chat-message assistant">
      <div className="chat-message-row assistant">
        <div className="chat-avatar assistant">
          <img src="/SimCoder.png" alt="SimCoder" />
        </div>
        <div className="chat-message-bubble-wrap">{bubble}</div>
      </div>
      <div className="chat-meta assistant">{assistantName} - {time}</div>
    </div>
  );
});

export function TypingIndicator() {
  return (
    <div className="chat-typing">
      {[0, 1, 2].map((index) => (
        <span key={index} style={{ animationDelay: `${index * 0.2}s` }} />
      ))}
    </div>
  );
}

export default ChatMessage;

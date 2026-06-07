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

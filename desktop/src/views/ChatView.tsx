/**
 * ChatView.tsx — Chat interface. Persists history across navigation, shows Telegram messages.
 */

import React, { useState, useRef, useEffect, useCallback, type FormEvent } from "react";
import { useChatStream, type ChatMessage } from "../hooks/useChatStream";
import logoSrc from "../assets/cato-logo.png";

interface ChatViewProps {
  wsBase?: string;
  httpPort?: number;
}

interface BadgeProps {
  source?: string;
  model?: string;
}

const SourceBadge: React.FC<BadgeProps> = ({ source, model }) => {
  const badges = [];

  if (source && source !== "web") {
    const label = source === "telegram" ? "Telegram" : source;
    const color = source === "telegram" ? "#229ED9" : "#94a3b8";
    badges.push(
      <span key={`source-${source}`} style={{
        fontSize: 10, fontWeight: 700, padding: "1px 6px", borderRadius: 8,
        background: `${color}22`, color, border: `1px solid ${color}55`,
        marginLeft: 6, lineHeight: 1.4,
      }}>
        {label}
      </span>
    );
  }

  if (model) {
    const modelLabel = model.toUpperCase();
    const modelColors: Record<string, string> = {
      "CLAUDE": "#9B5DE5",
      "CODEX": "#00D9FF",
      "GEMINI": "#F77F00",
      "CURSOR": "#06FFA5",
      "SWARMSYNC": "#FF006E",
    };
    const modelColor = Object.entries(modelColors).find(([key]) => modelLabel.includes(key))?.[1] || "#64748B";
    badges.push(
      <span key={`model-${model}`} style={{
        fontSize: 10, fontWeight: 700, padding: "1px 6px", borderRadius: 8,
        background: `${modelColor}22`, color: modelColor, border: `1px solid ${modelColor}55`,
        marginLeft: 6, lineHeight: 1.4,
      }}>
        {modelLabel}
      </span>
    );
  }

  return badges.length > 0 ? <>{badges}</> : null;
};

const ChatBubble: React.FC<{ message: ChatMessage }> = ({ message }) => {
  const isUser = message.role === "user";
  return (
    <div className={`chat-bubble ${isUser ? "chat-bubble-user" : "chat-bubble-assistant"}`}>
      <div className="chat-bubble-header">
        <span className="chat-bubble-role">
          {isUser ? "You" : "Cato"}
          <SourceBadge source={message.source} model={message.model} />
        </span>
        <time className="chat-bubble-time">
          {new Date(message.timestamp).toLocaleTimeString([], {
            hour: "2-digit",
            minute: "2-digit",
          })}
        </time>
      </div>
      <div className="chat-bubble-text">{message.text}</div>
    </div>
  );
};

export const ChatView: React.FC<ChatViewProps> = ({ wsBase, httpPort }) => {
  const { messages, connectionStatus, sendMessage, isStreaming, clearHistory } =
    useChatStream(wsBase, httpPort);
  const [input, setInput] = useState("");
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const handleSubmit = useCallback(
    (e: FormEvent) => {
      e.preventDefault();
      const text = input.trim();
      if (!text) return;
      sendMessage(text);
      setInput("");
      inputRef.current?.focus();
    },
    [input, sendMessage],
  );

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        handleSubmit(e as unknown as FormEvent);
      }
    },
    [handleSubmit],
  );

  return (
    <div className="chat-view">
      <header className="chat-header">
        <h1 className="chat-title">Cato Chat</h1>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span className={`chat-status chat-status-${connectionStatus}`}>
            {connectionStatus === "connected"    ? "Connected"      :
             connectionStatus === "connecting"   ? "Connecting..."  :
             connectionStatus === "reconnecting" ? "Reconnecting..."
                                                 : "Disconnected"}
          </span>
          {messages.length > 0 && (
            <button
              className="btn-cancel-sm"
              onClick={clearHistory}
              title="Clear conversation history"
              style={{ fontSize: 11 }}
            >
              Clear
            </button>
          )}
        </div>
      </header>

      <div className="chat-messages" role="log" aria-live="polite" aria-label="Chat messages">
        {messages.length === 0 && (
          <div className="chat-empty">
            <img
              src={logoSrc}
              alt="Cato"
              className="chat-empty-logo"
            />
            <p>Start a conversation with Cato</p>
            <p className="chat-empty-hint">
              Ask questions, get help with code, or explore ideas.
              Telegram messages appear here too.
            </p>
          </div>
        )}
        {messages.map((msg) => (
          <ChatBubble key={msg.id} message={msg} />
        ))}
        {isStreaming && (
          <div className="chat-bubble chat-bubble-assistant">
            <div className="chat-bubble-header">
              <span className="chat-bubble-role">Cato</span>
            </div>
            <div className="chat-typing">
              <span /><span /><span />
            </div>
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>

      <form className="chat-input-form" onSubmit={handleSubmit}>
        <textarea
          ref={inputRef}
          className="chat-input"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Type a message... (Enter to send, Shift+Enter for newline)"
          aria-label="Type a message"
          rows={2}
          disabled={connectionStatus !== "connected"}
          autoFocus
        />
        <button
          type="submit"
          className="chat-send-btn"
          disabled={!input.trim() || connectionStatus !== "connected" || isStreaming}
        >
          {isStreaming ? "Working..." : "Send"}
        </button>
      </form>
    </div>
  );
};

export default ChatView;

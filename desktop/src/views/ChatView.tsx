/**
 * ChatView.tsx — Simple single-model chat interface.
 *
 * Connects to the Cato gateway WebSocket for general conversation.
 */

import React, { useState, useRef, useEffect, useCallback, type FormEvent } from "react";
import { useChatStream, type ChatMessage } from "../hooks/useChatStream";

interface ChatViewProps {
  wsBase?: string;
  httpPort?: number;
}

const ChatBubble: React.FC<{ message: ChatMessage }> = ({ message }) => {
  const isUser = message.role === "user";
  return (
    <div className={`chat-bubble ${isUser ? "chat-bubble-user" : "chat-bubble-assistant"}`}>
      <div className="chat-bubble-header">
        <span className="chat-bubble-role">{isUser ? "You" : "Cato"}</span>
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

export const ChatView: React.FC<ChatViewProps> = ({ wsBase }) => {
  const { messages, connectionStatus, sendMessage, isStreaming } = useChatStream(wsBase);
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
        <span className={`chat-status chat-status-${connectionStatus}`}>
          {connectionStatus === "connected" ? "Connected" :
           connectionStatus === "connecting" ? "Connecting..." :
           connectionStatus === "reconnecting" ? "Reconnecting..." :
           "Disconnected"}
        </span>
      </header>

      <div className="chat-messages" role="log" aria-live="polite" aria-label="Chat messages">
        {messages.length === 0 && (
          <div className="chat-empty">
            <div className="chat-empty-icon">C</div>
            <p>Start a conversation with Cato</p>
            <p className="chat-empty-hint">
              Ask questions, get help with code, or explore ideas.
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
          disabled={!input.trim() || connectionStatus !== "connected"}
        >
          Send
        </button>
      </form>
    </div>
  );
};

export default ChatView;

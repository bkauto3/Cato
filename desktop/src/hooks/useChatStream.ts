/**
 * useChatStream.ts — WebSocket hook for the general chat view.
 *
 * Connects to the gateway WebSocket at ws://127.0.0.1:{port}/ws
 * and exchanges messages using the gateway protocol:
 *   Send:    {"type": "message", "text": "...", "session_id": "..."}
 *   Receive: {"type": "response", "text": "..."} or newline-delimited JSON events
 */

import { useState, useEffect, useRef, useCallback } from "react";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system";
  text: string;
  timestamp: number;
}

export type ChatConnectionStatus = "connecting" | "connected" | "disconnected" | "reconnecting";

export interface UseChatStreamResult {
  messages: ChatMessage[];
  connectionStatus: ChatConnectionStatus;
  sendMessage: (text: string) => void;
  isStreaming: boolean;
}

const MAX_RETRIES = 5;
const INITIAL_BACKOFF_MS = 500;

export function useChatStream(wsBase?: string): UseChatStreamResult {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [connectionStatus, setConnectionStatus] = useState<ChatConnectionStatus>("connecting");
  const [isStreaming, setIsStreaming] = useState(false);

  const wsRef = useRef<WebSocket | null>(null);
  const retriesRef = useRef(0);
  const sessionIdRef = useRef(crypto.randomUUID());

  const connect = useCallback(() => {
    // KRAK-4: validate wsBase is localhost-only
    const rawHost = wsBase ?? "127.0.0.1:19001";
    const host = /^127\.0\.0\.1:\d+$/.test(rawHost) ? rawHost : "127.0.0.1:19001";
    const url = `ws://${host}/ws`;

    setConnectionStatus("connecting");
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      setConnectionStatus("connected");
      retriesRef.current = 0;
    };

    ws.onmessage = (ev: MessageEvent<string>) => {
      try {
        const data = JSON.parse(ev.data.trimEnd());

        if (data.type === "health" || data.type === "heartbeat") return;

        // Handle response messages from the gateway
        if (data.type === "response" || data.text || data.reply) {
          const text = data.text ?? data.reply ?? data.message ?? JSON.stringify(data);
          const msg: ChatMessage = {
            id: crypto.randomUUID(),
            role: "assistant",
            text,
            timestamp: Date.now(),
          };
          setMessages((prev) => [...prev, msg]);
          setIsStreaming(false);
        }
      } catch {
        // Non-JSON message, treat as plain text response
        if (ev.data.trim()) {
          setMessages((prev) => [
            ...prev,
            {
              id: crypto.randomUUID(),
              role: "assistant",
              text: ev.data.trim(),
              timestamp: Date.now(),
            },
          ]);
          setIsStreaming(false);
        }
      }
    };

    ws.onerror = () => {
      console.error("[useChatStream] WebSocket error");
    };

    ws.onclose = () => {
      if (retriesRef.current < MAX_RETRIES) {
        const delay = Math.min(INITIAL_BACKOFF_MS * 2 ** retriesRef.current, 16_000);
        retriesRef.current += 1;
        setConnectionStatus("reconnecting");
        setTimeout(connect, delay);
      } else {
        setConnectionStatus("disconnected");
      }
    };
  }, [wsBase]);

  useEffect(() => {
    connect();
    return () => {
      if (wsRef.current) {
        wsRef.current.onclose = null;
        wsRef.current.close();
        wsRef.current = null;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const sendMessage = useCallback((text: string) => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;

    const userMsg: ChatMessage = {
      id: crypto.randomUUID(),
      role: "user",
      text,
      timestamp: Date.now(),
    };
    setMessages((prev) => [...prev, userMsg]);
    setIsStreaming(true);

    wsRef.current.send(
      JSON.stringify({
        type: "message",
        text,
        session_id: sessionIdRef.current,
      }) + "\n",
    );
  }, []);

  return { messages, connectionStatus, sendMessage, isStreaming };
}

export default useChatStream;

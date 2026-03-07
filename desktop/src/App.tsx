/**
 * App.tsx — Root component for Cato Desktop.
 *
 * Two-tab layout: Chat and Coding Agent views.
 * Polls the daemon health endpoint until ready.
 */

import { useState, useEffect } from "react";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { ChatView } from "./views/ChatView";
import { CodingAgentView } from "./views/CodingAgentView";
import "./styles/app.css";

type View = "chat" | "coding-agent";

interface DaemonInfo {
  httpPort: number;
  wsPort: number;
  status: "starting" | "ready" | "stopped" | "error";
}

// Cato gateway ports: webchat_port 8080 (HTTP), webchat_port+1 8081 (WebSocket)
const DAEMON_HTTP_PORT = 8080;
const DAEMON_WS_PORT   = 8081;

function useDaemonInfo(): DaemonInfo {
  const [info, setInfo] = useState<DaemonInfo>({
    httpPort: DAEMON_HTTP_PORT,
    wsPort: DAEMON_WS_PORT,
    status: "starting",
  });

  useEffect(() => {
    let cancelled = false;
    let attempts = 0;
    const maxAttempts = 30;

    const poll = async () => {
      while (!cancelled && attempts < maxAttempts) {
        try {
          // Use constant port — avoids stale closure on state read
          const res = await fetch(`http://127.0.0.1:${DAEMON_HTTP_PORT}/health`);
          if (res.ok) {
            setInfo((prev) => ({ ...prev, status: "ready" }));
            return;
          }
        } catch {
          // Daemon not yet ready
        }
        attempts++;
        await new Promise((r) => setTimeout(r, 1000));
      }
      if (!cancelled) {
        setInfo((prev) => ({ ...prev, status: "error" }));
      }
    };
    poll();
    return () => { cancelled = true; };
  }, []);

  return info;
}

function App() {
  const [view, setView] = useState<View>("chat");
  const daemon = useDaemonInfo();

  return (
    <div className="app-root">
      {/* Navigation */}
      <nav className="app-nav">
        <div className="app-nav-brand">
          <span className="app-nav-logo">C</span>
          <span className="app-nav-title">Cato</span>
        </div>
        <div className="app-nav-tabs">
          <button
            className={`app-nav-tab ${view === "chat" ? "active" : ""}`}
            onClick={() => setView("chat")}
          >
            Chat
          </button>
          <button
            className={`app-nav-tab ${view === "coding-agent" ? "active" : ""}`}
            onClick={() => setView("coding-agent")}
          >
            Coding Agent
          </button>
        </div>
        <div className="app-nav-status">
          <span className={`status-dot status-${daemon.status}`} />
          <span className="status-label">
            {daemon.status === "ready" ? "Connected" :
             daemon.status === "starting" ? "Starting..." :
             daemon.status === "error" ? "Error" : "Stopped"}
          </span>
        </div>
      </nav>

      {/* Daemon loading screen */}
      {daemon.status === "starting" && (
        <div className="app-loading">
          <div className="app-loading-spinner" />
          <p>Starting Cato daemon...</p>
        </div>
      )}

      {/* Main content */}
      {daemon.status === "ready" && (
        <main className="app-main">
          <ErrorBoundary>
            {view === "chat" && (
              <ChatView wsBase={`127.0.0.1:${daemon.wsPort}`} />
            )}
            {view === "coding-agent" && (
              <CodingAgentView
                wsBase={`127.0.0.1:${daemon.httpPort}`}
                apiBase={`http://127.0.0.1:${daemon.httpPort}`}
              />
            )}
          </ErrorBoundary>
        </main>
      )}

      {daemon.status === "error" && (
        <div className="app-error">
          <p>Failed to connect to Cato daemon.</p>
          <button className="retry-btn" onClick={() => window.location.reload()}>
            Retry
          </button>
        </div>
      )}
    </div>
  );
}

export default App;

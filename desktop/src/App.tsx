/**
 * App.tsx — Root component for Cato Desktop.
 *
 * Sidebar layout: left nav + main content area.
 * Polls the daemon health endpoint until ready.
 */

import { useState, useEffect } from "react";
import { invoke } from "@tauri-apps/api/core";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { Sidebar, type View } from "./components/Sidebar";
import { ChatView } from "./views/ChatView";
import { CodingAgentView } from "./views/CodingAgentView";
import { DashboardView } from "./views/DashboardView";
import { SessionsView } from "./views/SessionsView";
import { SkillsView } from "./views/SkillsView";
import { CronView } from "./views/CronView";
import { UsageView } from "./views/UsageView";
import { LogsView } from "./views/LogsView";
import { AuditLogView } from "./views/AuditLogView";
import { ConfigView } from "./views/ConfigView";
import { BudgetView } from "./views/BudgetView";
import { AlertsView } from "./views/AlertsView";
import { AuthKeysView } from "./views/AuthKeysView";
import "./styles/app.css";

type DaemonStatus = "starting" | "ready" | "stopped" | "error";

interface DaemonInfo {
  httpPort: number;
  wsPort: number;
  status: DaemonStatus;
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
          const status = await invoke<{ running: boolean }>("get_daemon_status");
          if (status.running) {
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

function renderView(view: View, daemon: DaemonInfo, onNavigate: (v: View) => void): React.ReactNode {
  const { httpPort, wsPort } = daemon;
  switch (view) {
    case "dashboard":
      return <DashboardView httpPort={httpPort} onNavigate={onNavigate} />;
    case "chat":
      return <ChatView wsBase={`127.0.0.1:${wsPort}`} httpPort={httpPort} />;
    case "coding-agent":
      return (
        <CodingAgentView
          wsBase={`127.0.0.1:${httpPort}`}
          apiBase={`http://127.0.0.1:${httpPort}`}
        />
      );
    case "skills":
      return <SkillsView httpPort={httpPort} />;
    case "cron":
      return <CronView httpPort={httpPort} />;
    case "sessions":
      return <SessionsView httpPort={httpPort} />;
    case "usage":
      return <UsageView httpPort={httpPort} />;
    case "logs":
      return <LogsView httpPort={httpPort} />;
    case "audit":
      return <AuditLogView httpPort={httpPort} />;
    case "config":
      return <ConfigView httpPort={httpPort} />;
    case "budget":
      return <BudgetView httpPort={httpPort} />;
    case "alerts":
      return <AlertsView httpPort={httpPort} />;
    case "auth-keys":
      return <AuthKeysView httpPort={httpPort} />;
    default:
      return null;
  }
}

function App() {
  const [view, setView] = useState<View>("dashboard");
  const daemon = useDaemonInfo();

  // Allow child views to trigger navigation (e.g. quick-launch buttons)
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent<string>).detail as View;
      if (detail) setView(detail);
    };
    window.addEventListener("cato-navigate", handler);
    return () => window.removeEventListener("cato-navigate", handler);
  }, []);

  return (
    <div className="app-root app-root-sidebar">
      <Sidebar
        activeView={view}
        onNavigate={setView}
        daemonStatus={daemon.status}
      />

      <div className="app-content">
        {daemon.status === "starting" && (
          <div className="app-loading">
            <div className="app-loading-spinner" />
            <p>Starting Cato daemon...</p>
          </div>
        )}

        {daemon.status === "ready" && (
          <main className="app-main">
            <ErrorBoundary>
              {renderView(view, daemon, setView)}
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
    </div>
  );
}

export default App;

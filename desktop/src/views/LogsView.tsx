/**
 * LogsView — Live daemon log stream with level filtering.
 */
import React, { useState, useEffect, useCallback, useRef } from "react";

interface LogsViewProps {
  httpPort: number;
}

interface LogEntry {
  ts: number;
  level: string;
  name: string;
  msg: string;
}

const LEVEL_COLORS: Record<string, string> = {
  DEBUG: "#64748b",
  INFO: "#94a3b8",
  WARNING: "#eab308",
  ERROR: "#ef4444",
  CRITICAL: "#dc2626",
};

export const LogsView: React.FC<LogsViewProps> = ({ httpPort }) => {
  const base = `http://127.0.0.1:${httpPort}`;
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [level, setLevel] = useState("");
  const [limit, setLimit] = useState(200);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const bottomRef = useRef<HTMLDivElement>(null);

  const fetchLogs = useCallback(async () => {
    try {
      const params = new URLSearchParams({ limit: String(limit) });
      if (level) params.set("level", level);
      const r = await fetch(`${base}/api/logs?${params}`);
      setLogs(await r.json());
      setError(null);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [base, level, limit]);

  useEffect(() => {
    fetchLogs();
  }, [fetchLogs]);

  useEffect(() => {
    if (!autoRefresh) return;
    const t = setInterval(fetchLogs, 3000);
    return () => clearInterval(t);
  }, [autoRefresh, fetchLogs]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [logs]);

  if (loading) return <div className="view-loading"><div className="app-loading-spinner" /></div>;

  return (
    <div className="page-view">
      <div className="page-header">
        <h1 className="page-title">Logs</h1>
        <div className="page-controls">
          <select
            className="settings-select"
            value={level}
            onChange={(e) => setLevel(e.target.value)}
          >
            <option value="">All Levels</option>
            <option value="DEBUG">DEBUG</option>
            <option value="INFO">INFO</option>
            <option value="WARNING">WARNING</option>
            <option value="ERROR">ERROR</option>
          </select>
          <select
            className="settings-select"
            value={limit}
            onChange={(e) => setLimit(Number(e.target.value))}
          >
            <option value={50}>50</option>
            <option value={100}>100</option>
            <option value={200}>200</option>
            <option value={500}>500</option>
          </select>
          <label className="toggle-label">
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(e) => setAutoRefresh(e.target.checked)}
            />
            Auto-refresh
          </label>
          <button className="btn-secondary" onClick={fetchLogs}>Refresh</button>
        </div>
      </div>
      {error && <div className="page-error">{error}</div>}

      <div className="log-container">
        {logs.length === 0 ? (
          <div className="empty-state">No log entries</div>
        ) : (
          logs.map((entry, i) => (
            <div key={i} className="log-row">
              <span className="log-ts">{new Date(entry.ts * 1000).toLocaleTimeString()}</span>
              <span className="log-level" style={{ color: LEVEL_COLORS[entry.level] ?? "#94a3b8" }}>
                {entry.level.padEnd(8)}
              </span>
              <span className="log-name">{entry.name}</span>
              <span className="log-msg">{entry.msg}</span>
            </div>
          ))
        )}
        <div ref={bottomRef} />
      </div>
    </div>
  );
};

export default LogsView;

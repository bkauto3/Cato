/**
 * SessionsView — Live session list with kill, queue depth, and transcript download.
 */
import React, { useState, useEffect, useCallback } from "react";

interface SessionsViewProps {
  httpPort: number;
}

interface SessionEntry {
  session_id: string;
  queue_depth: number;
  running: boolean;
}

export const SessionsView: React.FC<SessionsViewProps> = ({ httpPort }) => {
  const base = `http://127.0.0.1:${httpPort}`;
  const [sessions, setSessions] = useState<SessionEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [killing, setKilling] = useState<string | null>(null);

  const fetchSessions = useCallback(async () => {
    try {
      const r = await fetch(`${base}/api/sessions`);
      setSessions(await r.json());
      setError(null);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [base]);

  useEffect(() => {
    fetchSessions();
    const t = setInterval(fetchSessions, 5000);
    return () => clearInterval(t);
  }, [fetchSessions]);

  const killSession = async (sessionId: string) => {
    setKilling(sessionId);
    try {
      await fetch(`${base}/api/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
      await fetchSessions();
    } catch (e) {
      setError(String(e));
    } finally {
      setKilling(null);
    }
  };

  const downloadTranscript = async (sessionId: string) => {
    try {
      const r = await fetch(`${base}/api/audit/entries?session_id=${encodeURIComponent(sessionId)}&limit=1000`);
      const entries = await r.json();
      const jsonl = entries.map((e: Record<string, unknown>) => JSON.stringify(e)).join("\n");
      const blob = new Blob([jsonl], { type: "application/x-ndjson" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `cato-session-${sessionId.slice(0, 12)}.jsonl`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setError(String(e));
    }
  };

  if (loading) return <div className="view-loading"><div className="app-loading-spinner" /></div>;

  return (
    <div className="page-view">
      <div className="page-header">
        <h1 className="page-title">Sessions</h1>
        <button className="btn-secondary" onClick={fetchSessions}>Refresh</button>
      </div>
      {error && <div className="page-error">{error}</div>}

      {sessions.length === 0 ? (
        <div className="empty-state">No active sessions</div>
      ) : (
        <div className="table-container">
          <table className="data-table">
            <thead>
              <tr>
                <th>Session ID</th>
                <th>Status</th>
                <th>Queue Depth</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {sessions.map((s) => (
                <tr key={s.session_id}>
                  <td><code className="code-cell">{s.session_id}</code></td>
                  <td>
                    <span className={`status-badge ${s.running ? "status-badge-green" : "status-badge-gray"}`}>
                      {s.running ? "Running" : "Idle"}
                    </span>
                  </td>
                  <td>{s.queue_depth}</td>
                  <td className="action-cell">
                    <button
                      className="btn-danger-sm"
                      onClick={() => killSession(s.session_id)}
                      disabled={killing === s.session_id}
                    >
                      {killing === s.session_id ? "Killing…" : "Kill"}
                    </button>
                    <button
                      className="btn-secondary-sm"
                      onClick={() => downloadTranscript(s.session_id)}
                    >
                      Export
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
};

export default SessionsView;

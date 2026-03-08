/**
 * AuthKeysView — SwarmSync key, CLI OAuth status panel, Vault key management.
 * All data is live from /api/vault/keys, /api/config.
 */
import React, { useState, useEffect, useCallback } from "react";

interface AuthKeysViewProps {
  httpPort: number;
}

// Which vault keys go here and what they're for
const VAULT_KEY_META: Record<string, string> = {
  SWARMSYNC_API_KEY:       "Chat routing (sk-ss-…) — required for chat",
  brave_api_key:           "Brave web search (Conduit)",
  exa_api_key:             "Exa semantic search (Conduit)",
  tavily_api_key:          "Tavily web search (Conduit)",
  perplexity_api_key:      "Perplexity deep search (Conduit)",
  semantic_scholar_api_key:"Semantic Scholar academic search (Conduit)",
};

const CLI_BACKENDS = [
  { id: "claude",  label: "Claude",       loginCmd: "claude login",  logoutCmd: "claude auth logout" },
  { id: "codex",   label: "Codex",        loginCmd: "codex login",   logoutCmd: "codex logout" },
  { id: "gemini",  label: "Gemini",       loginCmd: "gemini login",  logoutCmd: "gemini auth logout" },
  { id: "cursor",  label: "Cursor Agent", loginCmd: "agent login",   logoutCmd: "agent logout" },
] as const;

export const AuthKeysView: React.FC<AuthKeysViewProps> = ({ httpPort }) => {
  const base = `http://127.0.0.1:${httpPort}`;
  const [vaultKeys, setVaultKeys] = useState<string[]>([]);
  const [config, setConfig] = useState<Record<string, unknown>>({});
  const [loading, setLoading] = useState(true);

  // SwarmSync key entry
  const [ssKey, setSsKey] = useState("");
  const [ssSaving, setSsSaving] = useState(false);
  const [ssMsg, setSsMsg] = useState("");

  // Add vault key form
  const [newKeyName, setNewKeyName] = useState("");
  const [newKeyValue, setNewKeyValue] = useState("");
  const [addingSaving, setAddingSaving] = useState(false);
  const [addMsg, setAddMsg] = useState("");

  const fetchData = useCallback(async () => {
    try {
      const [kr, cr] = await Promise.all([
        fetch(`${base}/api/vault/keys`).then((r) => r.json()),
        fetch(`${base}/api/config`).then((r) => r.json()),
      ]);
      setVaultKeys(kr as string[]);
      setConfig(cr as Record<string, unknown>);
    } catch {
      // silently ignore; show whatever we have
    } finally {
      setLoading(false);
    }
  }, [base]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const saveSwarmSyncKey = async () => {
    if (!ssKey.trim()) return;
    setSsSaving(true);
    try {
      const r = await fetch(`${base}/api/vault/set`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: "SWARMSYNC_API_KEY", value: ssKey.trim() }),
      });
      const d = await r.json();
      if (d.status === "ok") {
        setSsMsg("Saved");
        setSsKey("");
        await fetchData();
      } else {
        setSsMsg(`Error: ${d.message}`);
      }
    } catch (e) {
      setSsMsg(String(e));
    } finally {
      setSsSaving(false);
      setTimeout(() => setSsMsg(""), 3000);
    }
  };

  const addKey = async () => {
    if (!newKeyName.trim() || !newKeyValue.trim()) return;
    setAddingSaving(true);
    try {
      await fetch(`${base}/api/vault/set`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: newKeyName.trim(), value: newKeyValue.trim() }),
      });
      setNewKeyName(""); setNewKeyValue("");
      setAddMsg("Key added");
      await fetchData();
    } catch (e) {
      setAddMsg(String(e));
    } finally {
      setAddingSaving(false);
      setTimeout(() => setAddMsg(""), 3000);
    }
  };

  const deleteKey = async (key: string) => {
    await fetch(`${base}/api/vault/delete`, {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key }),
    });
    await fetchData();
  };

  const hasSwarmSync = vaultKeys.includes("SWARMSYNC_API_KEY");

  if (loading) return <div className="view-loading"><div className="app-loading-spinner" /></div>;

  return (
    <div className="page-view">
      <div className="page-header">
        <h1 className="page-title">Auth & Keys</h1>
        <button className="btn-secondary" onClick={fetchData}>Refresh</button>
      </div>

      <div className="info-note">
        You do <strong>NOT</strong> need Anthropic / OpenAI / Google API keys.
        Claude, Codex, and Gemini coding backends use CLI OAuth (login once, no key needed).
      </div>

      {/* SwarmSync */}
      <div className="section-block">
        <div className="section-title">
          SwarmSync Key
          {hasSwarmSync
            ? <span className="badge-green">Configured</span>
            : <span className="badge-red">Missing — chat will return 401</span>}
        </div>
        <div className="section-desc">
          Chat routing key (sk-ss-…). SwarmSync picks the best model automatically.
          One key covers all providers — no per-provider keys needed.
        </div>
        {!hasSwarmSync && (
          <div className="warn-banner">
            ⚠ Chat requires a SwarmSync key. Enter it below.
          </div>
        )}
        <div className="form-row">
          <input
            type="password"
            className="form-input form-input-wide"
            placeholder="sk-ss-..."
            value={ssKey}
            onChange={(e) => setSsKey(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && saveSwarmSyncKey()}
          />
          <button className="btn-primary" onClick={saveSwarmSyncKey} disabled={ssSaving || !ssKey.trim()}>
            {ssSaving ? "Saving…" : "Save"}
          </button>
          {ssMsg && <span className="save-msg">{ssMsg}</span>}
        </div>

        {/* SwarmSync config fields */}
        <div className="form-row" style={{ marginTop: 8 }}>
          <label>SwarmSync Enabled</label>
          <span className={`badge ${config.swarmsync_enabled ? "badge-green" : "badge-gray"}`}>
            {config.swarmsync_enabled ? "Yes" : "No"}
          </span>
        </div>
        <div className="form-row">
          <label>API URL</label>
          <code className="code-cell">{String(config.swarmsync_api_url ?? "https://api.swarmsync.ai/v1/chat/completions")}</code>
        </div>
      </div>

      {/* CLI OAuth status */}
      <div className="section-block">
        <div className="section-title">CLI OAuth Status</div>
        <div className="section-desc">
          Coding agent backends authenticate via CLI — no API keys needed.
          Run login commands in a terminal; Cato reuses the stored session.
        </div>
        <div className="cli-status-list">
          {CLI_BACKENDS.map((cli) => (
            <div key={cli.id} className="cli-status-row">
              <div className="cli-status-info">
                <span className="cli-label">{cli.label}</span>
                <code className="cli-cmd">{cli.loginCmd}</code>
              </div>
              <div className="cli-status-actions">
                <span className="cli-note">Run in terminal to authenticate</span>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Vault keys */}
      <div className="section-block">
        <div className="section-title">Vault Keys</div>
        <div className="section-desc">
          Stored keys (values are encrypted — only names are shown).
        </div>
        <div className="vault-key-list">
          {Object.entries(VAULT_KEY_META).map(([key, desc]) => {
            const present = vaultKeys.includes(key);
            return (
              <div key={key} className="vault-key-row">
                <span className={`status-dot ${present ? "status-ready" : "status-error"}`} />
                <div className="vault-key-info">
                  <code className="vault-key-name">{key}</code>
                  <span className="vault-key-desc">{desc}</span>
                </div>
                {present && (
                  <button className="btn-danger-sm" onClick={() => deleteKey(key)}>Delete</button>
                )}
              </div>
            );
          })}

          {/* Any other vault keys not in the predefined list */}
          {vaultKeys
            .filter((k) => !(k in VAULT_KEY_META))
            .map((key) => (
              <div key={key} className="vault-key-row">
                <span className="status-dot status-ready" />
                <div className="vault-key-info">
                  <code className="vault-key-name">{key}</code>
                  <span className="vault-key-desc">Custom key</span>
                </div>
                <button className="btn-danger-sm" onClick={() => deleteKey(key)}>Delete</button>
              </div>
            ))}
        </div>

        {/* Add key form */}
        <div className="add-key-form">
          <div className="form-row">
            <input
              className="form-input"
              placeholder="KEY_NAME"
              value={newKeyName}
              onChange={(e) => setNewKeyName(e.target.value)}
            />
            <input
              type="password"
              className="form-input form-input-wide"
              placeholder="value"
              value={newKeyValue}
              onChange={(e) => setNewKeyValue(e.target.value)}
            />
            <button
              className="btn-secondary"
              onClick={addKey}
              disabled={addingSaving || !newKeyName.trim() || !newKeyValue.trim()}
            >
              {addingSaving ? "Adding…" : "Add Key"}
            </button>
          </div>
          {addMsg && <span className="save-msg">{addMsg}</span>}
        </div>
      </div>
    </div>
  );
};

export default AuthKeysView;

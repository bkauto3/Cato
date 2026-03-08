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
  OPENROUTER_API_KEY: "OpenRouter API key — required for chat (sk-or-…)",
  TELEGRAM_BOT_TOKEN: "Telegram bot token — Cato's Telegram interface",
  brave_api_key:      "Brave web search",
  exa_api_key:        "Exa semantic search",
  tavily_api_key:     "Tavily web search",
};

const CLI_BACKENDS = [
  {
    id: "codex",
    label: "Codex",
    status: "working",
    note: "No login needed — runs locally via warm pool",
    loginCmd: null,
  },
  {
    id: "cursor",
    label: "Cursor Agent",
    status: "working",
    note: "Uses Cursor IDE session — log in via Cursor IDE",
    loginCmd: null,
  },
  {
    id: "gemini",
    label: "Gemini",
    status: "degraded",
    note: "Hangs in non-interactive mode on this machine — timeout/degraded",
    loginCmd: "gemini auth login",
  },
] as const;

export const AuthKeysView: React.FC<AuthKeysViewProps> = ({ httpPort }) => {
  const base = `http://127.0.0.1:${httpPort}`;
  const [vaultKeys, setVaultKeys] = useState<string[]>([]);
  const [config, setConfig] = useState<Record<string, unknown>>({});
  const [loading, setLoading] = useState(true);

  // OpenRouter key entry
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
        body: JSON.stringify({ key: "OPENROUTER_API_KEY", value: ssKey.trim() }),
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

  const hasOpenRouter = vaultKeys.includes("OPENROUTER_API_KEY");

  if (loading) return <div className="view-loading"><div className="app-loading-spinner" /></div>;

  return (
    <div className="page-view">
      <div className="page-header">
        <h1 className="page-title">Auth & Keys</h1>
        <button className="btn-secondary" onClick={fetchData}>Refresh</button>
      </div>

      <div className="info-note">
        Chat uses <strong>OpenRouter</strong> (one key, all models).
        Coding agents (Codex, Cursor) use local sessions — no API keys required.
      </div>

      {/* OpenRouter Key */}
      <div className="section-block">
        <div className="section-title">
          OpenRouter API Key
          {hasOpenRouter
            ? <span className="badge-green">Configured</span>
            : <span className="badge-red">Missing — chat will fail</span>}
        </div>
        <div className="section-desc">
          Routes chat to any LLM (MiniMax, GPT-4o, Claude, etc.) via OpenRouter.
          Get your key at <strong>openrouter.ai/keys</strong> (sk-or-…).
        </div>
        {!hasOpenRouter && (
          <div className="warn-banner">
            ⚠ Chat requires an OpenRouter API key. Enter it below.
          </div>
        )}
        <div className="form-row">
          <input
            type="password"
            className="form-input form-input-wide"
            placeholder="sk-or-..."
            value={ssKey}
            onChange={(e) => setSsKey(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && saveSwarmSyncKey()}
          />
          <button className="btn-primary" onClick={saveSwarmSyncKey} disabled={ssSaving || !ssKey.trim()}>
            {ssSaving ? "Saving…" : "Save"}
          </button>
          {ssMsg && <span className="save-msg">{ssMsg}</span>}
        </div>
        <div className="form-row" style={{ marginTop: 8 }}>
          <label>Current Model</label>
          <code className="code-cell">{String(config.default_model ?? "openrouter/minimax/minimax-m2.5")}</code>
        </div>
      </div>

      {/* CLI backend status */}
      <div className="section-block">
        <div className="section-title">Coding Agent Backends</div>
        <div className="section-desc">
          Coding tasks dispatch to these CLI backends. Status reflects configuration on this machine.
        </div>
        <div className="cli-status-list">
          {CLI_BACKENDS.map((cli) => (
            <div key={cli.id} className="cli-status-row">
              <div className="cli-status-info">
                <span className="cli-label">{cli.label}</span>
                <span
                  className={cli.status === "working" ? "badge-green" : "badge-yellow"}
                  style={{ fontSize: 10, padding: "1px 6px", borderRadius: 8, fontWeight: 700 }}
                >
                  {cli.status === "working" ? "Working" : "Degraded"}
                </span>
              </div>
              <div className="cli-status-actions">
                <span className="cli-note">{cli.note}</span>
                {cli.loginCmd && (
                  <code className="cli-cmd" style={{ marginTop: 4 }}>{cli.loginCmd}</code>
                )}
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

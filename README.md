# Cato — The AI agent daemon you can audit in a coffee break

**~3,000 lines of auditable Python. No mystery dependencies. No open ports by default. Budget-capped so it cannot bankrupt you overnight.**

- **No open ports by default** — WebChat is opt-in; Telegram and WhatsApp connectors use outbound polling only
- **Hard budget caps** — session cap ($1.00) and monthly cap ($20.00) enforced before every LLM call; raises `BudgetExceeded` before your card is charged
- **Auditable in an afternoon** — ~3,000 lines across 6 core modules, fully type-hinted, zero magic
- **One-command migration** — `cato migrate --from-openclaw` copies your workspaces and validates SKILL.md compatibility instantly

---

## Why Not OpenClaw?

OpenClaw has accumulated a pattern of undisclosed credential handling, silent telemetry, and a dependency tree that ships with known CVEs. Specific issues:

- **Credential exposure**: API keys stored in plaintext JSON under `~/.openclaw/keys/` with 644 permissions
- **Silent telemetry**: Usage data sent to `telemetry.openclaw.io` without opt-out in versions prior to 2.4.0
- **Supply-chain risk**: Transitive dependency `openclaw-native` bundles a pre-built C extension with no reproducible build

Cato stores all credentials in AES-256-GCM encrypted vault (`vault.enc`), emits zero telemetry, and has zero C extensions.

---

## Quick Start

### Install

```bash
# From PyPI (recommended)
pip install cato-daemon
patchright install chromium   # one-time browser download (~130 MB)

# Or install from source
git clone https://github.com/yourorg/cato
cd cato && pip install -e .
patchright install chromium
```

### First run (~60 seconds)

```bash
cato init
```

The wizard asks for:
- Monthly and session budget caps (defaults: $20 / $1)
- A vault master password (used to encrypt all API keys with AES-256-GCM)
- Whether to enable Telegram, WhatsApp, or SwarmSync routing

### Start the daemon

```bash
cato start                        # WebChat on localhost:8765
cato start --channel telegram     # Telegram polling only
cato start --channel all          # all channels
```

That's it. No Docker. No PostgreSQL. No Redis. SQLite for memory, a single YAML for config, one encrypted file for secrets.

---

## Powered by SwarmSync Routing

[SwarmSync](https://swarmsync.ai) is an intelligent model router that selects the cheapest model capable of handling each task — without you having to think about it.

### Enabling SwarmSync

```yaml
# ~/.cato/config.yaml
swarmsync_enabled: true
swarmsync_api_url: https://api.swarmsync.ai/v1/chat/completions
```

Or enable interactively during `cato init`.

### How it works

1. Before each LLM call, Cato sends the task description to the SwarmSync router
2. The router scores each of the 16 supported models against the task complexity
3. The cheapest capable model is selected automatically
4. The selected model's actual cost is tracked in your budget as normal
5. Routing itself costs $0.00

When SwarmSync is disabled (the default), Cato uses `default_model` from config.yaml for every call.

---

## Migrate from OpenClaw

```bash
# Preview what would be migrated (safe, no files written)
cato migrate --from-openclaw --dry-run

# Apply the migration
cato migrate --from-openclaw
```

This command:
1. Scans `~/.openclaw/agents/` for all agent workspaces
2. Copies workspace files: `SOUL.md`, `AGENTS.md`, `USER.md`, `IDENTITY.md`, `MEMORY.md`, `TOOLS.md`, `HEARTBEAT.md`, `CRONS.json`
3. Validates each `SKILL.md` — must have a `# Title` and `## Instructions` section
4. Validates each session `.jsonl` — every line must be valid JSON
5. Copies `sessions/*.jsonl` and `skills/*.md` per agent
6. Prints a summary: agents migrated, skills migrated, sessions migrated, files skipped

What is NOT copied:
- `config.json` — Cato uses YAML; re-run `cato init` to configure
- `node_modules/`, Node binaries — not applicable to Cato
- `.env` files — re-enter API keys via `cato init` and `cato vault set`

After migration, run `cato doctor` to audit token budgets and `cato init` to configure API keys.

---

## Model Support

All 16 models across 6 providers, with per-call cost tracking:

| Model | Provider | Input $/M | Output $/M |
|-------|----------|-----------|------------|
| claude-opus-4-6 | Anthropic | $15.00 | $75.00 |
| claude-sonnet-4-6 | Anthropic | $3.00 | $15.00 |
| claude-haiku-4-5 | Anthropic | $0.80 | $4.00 |
| gpt-4o | OpenAI | $2.50 | $10.00 |
| gpt-4o-mini | OpenAI | $0.15 | $0.60 |
| o3-mini | OpenAI | $1.10 | $4.40 |
| gemini-2.0-pro | Google | $1.25 | $5.00 |
| gemini-2.0-flash | Google | $0.10 | $0.40 |
| gemini-2.0-flash-lite | Google | $0.075 | $0.30 |
| deepseek-v3 | DeepSeek | $0.27 | $1.10 |
| deepseek-r1 | DeepSeek | $0.55 | $2.19 |
| groq-llama-3.3-70b | Groq | $0.59 | $0.79 |
| mistral-small | Mistral | $0.10 | $0.30 |
| minimax-2.5 | MiniMax | $0.20 | $1.00 |
| kimi-k2.5 | Moonshot | $0.15 | $0.60 |
| swarmync-router | SwarmSync | $0.00 | $0.00 |

---

## Built-in Skills

Cato ships with 5 ready-to-use skills in `cato/skills/`. They are loaded automatically by the agent loop.

| Skill file | Capabilities | What it does |
|------------|-------------|--------------|
| `web_search.md` | browser.search, browser.navigate | DuckDuckGo search with source citations |
| `summarize_url.md` | browser.navigate, browser.snapshot | Fetch any URL and return a 3-5 sentence summary |
| `send_email.md` | browser.navigate, browser.click, browser.type | Draft and send email via Gmail web UI (confirms before sending) |
| `add_notion.md` | shell | Add pages to a Notion database via the REST API |
| `daily_digest.md` | browser.search, memory.search, file.read | Personalized news digest from tracked topics + open tasks |

### Writing your own skill

A SKILL.md file requires exactly two structural elements:

```markdown
# My Skill Name
**Version:** 1.0.0
**Capabilities:** shell, browser.navigate

## Instructions
Tell the agent exactly what to do step by step.
Use numbered lists for sequential actions.
Reference tools by their canonical names: `shell`, `browser`, `file`, `memory`.
```

Drop the file into `~/.cato/agents/{your-agent}/skills/` and restart Cato. The context builder injects active skills into every turn.

---

## Architecture

Cato is intentionally flat. Every module does exactly one thing:

| File | Lines | Purpose |
|------|-------|---------|
| [`cato/vault.py`](cato/vault.py) | ~150 | AES-256-GCM credential store, Argon2id KDF |
| [`cato/budget.py`](cato/budget.py) | ~170 | Spend cap enforcement, call-level cost tracking |
| [`cato/config.py`](cato/config.py) | ~90 | YAML config with safe defaults, first-run detection |
| [`cato/core/context_builder.py`](cato/core/context_builder.py) | ~160 | 7,000-token context assembly with priority stack |
| [`cato/core/memory.py`](cato/core/memory.py) | ~210 | SQLite + BM25 + sentence-transformer hybrid memory |
| [`cato/cli.py`](cato/cli.py) | ~260 | `init`, `start`, `stop`, `migrate`, `doctor`, `status` |

No orchestration magic. No hidden event loops. Read it in a coffee break.

### ASCII Architecture Diagram

```
  User message
       |
       v
+------+--------+      +-----------+      +----------+
| Telegram /    |      |  Gateway  |      | SwarmSync|
| WhatsApp /    +----->|  (auth +  +----->|  Router  |
| WebChat       |      |  routing) |      | (opt-in) |
+---------------+      +-----+-----+      +----------+
                              |
                              v
                    +---------+--------+
                    |   ContextBuilder  |
                    | (7,000-tok budget)|
                    | SOUL + AGENTS +   |
                    | USER + MEMORY +   |
                    | skills + log      |
                    +---------+--------+
                              |
                              v
                    +---------+--------+
                    |    Agent Loop     |
                    |  plan → execute  |
                    |  → reflect → done|
                    +---------+--------+
                              |
               +--------------+-----------+
               |              |           |
               v              v           v
          +--------+    +---------+  +--------+
          |  Shell  |    | Browser |  |  File  |
          |  tool   |    |  tool   |  |  tool  |
          +--------+    +---------+  +--------+
               |              |           |
               +------+-------+-----------+
                      |
                      v
              +--------------+
              |    Memory     |
              | SQLite+BM25  |
              | +embeddings  |
              +--------------+
                      |
                      v
              +--------------+
              | Budget Guard  |
              | session+month |
              | hard caps     |
              +--------------+
```

---

## Known Limitations

- **Memory at scale**: The hybrid BM25+semantic search loads all chunks for each query.
  Works well up to ~5,000 memory chunks. For larger memory stores, an ANN index
  (faiss/hnswlib) will be added in v0.2.

---

## Contributing

Pull requests welcome. The bar is: does it fit in a coffee break?

### Principles
- Keep modules small and single-purpose (target < 250 lines each)
- No new required dependencies without strong justification
- Zero telemetry — every outbound connection must be user-initiated
- All credentials must pass through the vault, never environment variables

### Adding a new tool

1. Create `cato/tools/mytool.py` implementing the `BaseTool` interface from `cato/tools/base.py`
2. Register it in `cato/tools/__init__.py`
3. Add a row to the capabilities table in this README

### Adding a new adapter (messaging channel)

1. Create `cato/adapters/myadapter.py` subclassing `BaseAdapter`
2. Register it in `cato/adapters/__init__.py`
3. Add the enable flag to `CatoConfig` in `cato/config.py`

### Adding a built-in skill

1. Create a SKILL.md in `cato/skills/` with a `# Title` and `## Instructions` section
2. List the capabilities it requires in the frontmatter
3. Add a row to the Built-in Skills table in this README

---

```bash
cato init                    # first-run wizard
cato start                   # start daemon
cato start --channel telegram  # telegram only
cato stop                    # graceful shutdown
cato status                  # running state + budget summary
cato doctor                  # audit token budget per workspace
cato migrate --from-openclaw  # migrate OpenClaw agents
cato migrate --from-openclaw --dry-run  # preview migration
```

---

## Configuration

All config lives at `~/.cato/config.yaml`:

```yaml
agent_name: cato
default_model: claude-sonnet-4-6
monthly_cap: 20.0
session_cap: 1.0
swarmsync_enabled: true
swarmsync_api_url: https://api.swarmsync.ai/v1/chat/completions
telegram_enabled: false
whatsapp_enabled: false
webchat_port: 8765
max_planning_turns: 2
context_budget_tokens: 7000
log_level: INFO
```

---

## Security Model

- **Vault**: AES-256-GCM, Argon2id (64 MiB, 3 iterations, 4 threads), nonce-per-encryption
- **Key storage**: Derived key lives in process memory only — never written to disk
- **Credentials**: All API keys go through `cato vault set <KEY> <VALUE>`, not environment variables
- **No telemetry**: Zero external connections except to LLM APIs you configure

---

## License

MIT. Do whatever you want. Attribution appreciated.

---

*Powered by [SwarmSync](https://swarmsync.ai) intelligent model routing.*

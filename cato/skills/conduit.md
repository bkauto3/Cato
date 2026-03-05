# Conduit Browser
**Version:** 1.0.0
**Capabilities:** browser.navigate, browser.click, browser.type, browser.extract, browser.screenshot

## Overview
Conduit is Cato's built-in headless browser engine — enabled by default. Every browser action
is logged to a SHA-256 hash-chained audit trail, signed with the agent's Ed25519 identity key,
and enforced against the session budget cap before execution. All actions are free for local use.

## Activation
Conduit is enabled by default (`conduit_enabled: true` in config.yaml). No flags needed.

## Instructions

### Navigating to a URL
Use the `browser` tool with `action: navigate` and `url`:
- Action is logged to the SHA-256 hash-chained audit trail with timestamp and session ID
- VOIX `<tool>` and `<context>` tags are stripped from page content automatically
- URL is recorded in the billing ledger (free for local use)

### Clicking elements
Use `browser` with `action: click` and `selector` (CSS selector or XPath):
- Use specific selectors; avoid broad ones that might match multiple elements
- Action logged and signed with agent Ed25519 identity

### Typing text
Use `browser` with `action: type`, `selector`, and `text`:
- Sensitive values (passwords, API keys) are automatically redacted in the audit log
- Use for form inputs, search boxes, text areas

### Extracting page content
Use `browser` with `action: extract` (optional `selector`, defaults to `body`):
- Returns `text` (cleaned content) and `char_count`
- VOIX tags stripped automatically before content reaches the agent

### Taking screenshots
Use `browser` with `action: screenshot` (optional `path` to save file):
- Returns a base64 image or saved file path
- Logged to audit trail

## Audit Trail
Every action is written to the append-only SHA-256 hash-chained audit log in SQLite.
Each row's hash includes the previous row — tamper-evident across the full session history.

```bash
cato audit --session <id>   # full action-by-action replay
cato audit --verify         # tamper detection across all sessions
cato receipt --session <id> # signed receipt with line-item log
```

## Safety
- IRREVERSIBLE actions (form submissions that send data externally) require user confirmation
  when `safety_mode: strict` (default)
- Non-interactive daemon mode: HIGH_STAKES actions denied by default (fail-safe)
- Sensitive input keys (password, token, api_key, secret, bearer, etc.) auto-redacted in logs
- Budget cap enforced before each action — action never executes if it would exceed cap

## Example Task: Research a topic
1. `navigate` to a starting URL
2. `extract` the page content and summarize
3. `click` a relevant link if needed
4. `extract` again for more detail
5. `screenshot` to capture the final state
All steps logged, signed, and budget-checked automatically.

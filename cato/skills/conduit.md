# Conduit Browser
**Version:** 1.0.0
**Capabilities:** browser.navigate, browser.click, browser.type, browser.extract, browser.screenshot

## Trigger Phrases
"browse", "navigate to", "click", "extract page", "screenshot", "open url", "web page"

## Overview
Conduit is Cato's built-in headless browser engine (enabled by default). All actions are
hash-chained audit-logged, Ed25519-signed, and budget-checked before execution.

## Quick Reference

| Action | Tool call | Key params |
|--------|-----------|------------|
| Navigate | `browser navigate` | `url` |
| Click | `browser click` | `selector` |
| Type | `browser type` | `selector`, `text` |
| Extract content | `browser extract` | `selector` (optional, default: body) |
| Screenshot | `browser screenshot` | `path` (optional) |

## Safety
- IRREVERSIBLE actions require user confirmation (`safety_mode: strict` default)
- Sensitive inputs (password, api_key, token) auto-redacted in logs
- Budget cap enforced before every action

<!-- COLD -->

## Detailed Instructions

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

## Extended Safety Notes
- Non-interactive daemon mode: HIGH_STAKES actions denied by default (fail-safe)

## Example Task: Research a topic
1. `navigate` to a starting URL
2. `extract` the page content and summarize
3. `click` a relevant link if needed
4. `extract` again for more detail
5. `screenshot` to capture the final state
All steps logged, signed, and budget-checked automatically.

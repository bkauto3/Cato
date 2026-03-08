# AGENTS — Available CLI Backends

Cato can dispatch coding tasks to these agents. Each runs as a subprocess.

## Codex (PRIMARY - use by default)
- **CLI**: `codex mcp-server` (warm pool, MCP JSON-RPC over stdio)
- **Status**: Working — warm pool active on startup
- **Auth**: No API key needed (runs locally)
- **Flag**: `--dangerously-bypass-approvals-and-sandbox` for unrestricted mode
- **Best for**: Code generation, file edits, shell commands

## Cursor Agent (SECONDARY)
- **CLI**: `node.exe index.js --print --trust --yolo --model auto`
- **Binary**: `%LOCALAPPDATA%\cursor-agent\versions\2026.02.27-e7d2ef6\`
- **Status**: Working — tested and confirmed responding
- **Auth**: Uses Cursor IDE session (Cursor must be running or previously logged in)
- **Best for**: Complex multi-file tasks, Cursor's built-in model routing

## Gemini (TERTIARY - degraded on this VPS)
- **CLI**: `gemini -p <prompt>`
- **Status**: Hangs in non-interactive mode on this VPS — returns timeout/degraded response
- **Auth**: `AIzaSyAc5lGnaAGDLlYsG1EOfceobFVK9Ge_FeA` (authenticated)
- **Note**: Works in interactive terminal but not as subprocess here

## Claude CLI (BLOCKED - do not use)
- **Reason**: Running inside Claude Code session — nested invocation is blocked
- **Alternative**: Use OpenRouter via chat gateway instead

## Default Configuration
```yaml
subagent_enabled: true
subagent_coding_backend: codex
enabled_models: [codex, cursor, gemini]
```

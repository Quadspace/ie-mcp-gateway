# CLAUDE.md — IE.AI MCP Gateway Context

> This file is read by Claude Code on every task. Keep it updated.

Last updated: 2026-03-15 — v7.0.0 rebuild

## What This Is

A production MCP server that runs on a Mac Mini. It receives coding tasks from
Manus (AI agent platform) and dispatches them to Claude Code CLI, which is a
full coding agent with filesystem access. Manus saves ~97% of credits.

## Architecture

```
Manus → ngrok (dinoonemcp.ngrok.app) → FastMCP (port 8765) → claude CLI → OpenRouter → Anthropic
```

## Project Structure

```
ie_mcp_gateway/
├── server/
│   └── gateway.py          # Main MCP server (FastMCP v7.0.0)
├── dashboard/
│   └── index.html           # Live execution dashboard
├── config/
│   └── .env.template        # Environment template for new setups
├── ecosystem.config.js      # PM2 process configuration
├── requirements.txt         # Python dependencies
├── CLAUDE.md                # This file (project context)
├── README.md                # Setup guide and documentation
└── .gitignore
```

## Tech Stack

| Component | Technology |
|-----------|-----------|
| MCP Server | Official `mcp` Python SDK (FastMCP) |
| Execution Engine | Claude Code CLI (`claude -p`) |
| LLM Routing | OpenRouter API |
| Database | SQLite (tasks + memory) |
| Process Manager | PM2 |
| Tunnel | ngrok |

## Key Environment Variables

| Variable | Value |
|----------|-------|
| ANTHROPIC_BASE_URL | `https://openrouter.ai/api` |
| ANTHROPIC_AUTH_TOKEN | OpenRouter API key |
| ANTHROPIC_API_KEY | `""` (must be empty string) |
| CLAUDE_BIN | `/Users/ie.ai-dino1/.local/bin/claude` |
| PROJECT_PATH | `/Users/ie.ai-dino1/Documents/Dino_One_MCP` |
| GATEWAY_PORT | `8765` |

## Coding Conventions

1. MCP tools use `@mcp.tool()` decorator from FastMCP
2. HTTP routes use `@mcp.custom_route()` for non-MCP endpoints
3. Claude CLI is called via `asyncio.create_subprocess_exec` with `-p` flag
4. All task results logged to `tasks` SQLite table
5. Memory stored in `memory` SQLite table
6. Error handling: catch and return errors, never crash the server

## Active Tasks

- [x] Fix claude CLI routing through OpenRouter
- [x] Rebuild on official MCP Python SDK (FastMCP)
- [x] Add execute_code_task tool
- [x] Add persistent memory (remember/recall)
- [x] Create PM2 ecosystem config
- [ ] Deploy v7.0.0 to Mac Mini
- [ ] Set up GitHub private repo
- [ ] End-to-end test with Manus
- [ ] Build premium dashboard with IE.AI branding

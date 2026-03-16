# IE.AI MCP Gateway — Claude Code Briefing (v8.1)

This document provides the standing brief for Claude Code when working on the MCP gateway itself.

## 1. Project Overview

- **Name**: IE.AI MCP Gateway
- **Repo**: `Quadspace/ie-mcp-gateway`
- **Description**: A fast, reliable MCP tool server for the Mac Mini. Manus is the AI orchestrator. This gateway is the hands.

## 2. Tech Stack

| Area | Technology | Notes |
|---|---|---|
| **Framework** | FastAPI (via FastMCP) | Lightweight, high-performance Python web framework |
| **Language** | Python 3.11+ | Type hints are mandatory |
| **Database** | SQLite | Simple, file-based, no external dependencies |
| **Dependencies** | `httpx`, `mcp.server` | Minimal dependencies |

## 3. Project Structure

- `server/gateway.py`: The entire application. All logic lives in this one file.
- `dashboard/index.html`: The HTML for the web dashboard.
- `ecosystem.config.js`: PM2 process manager configuration.
- `~/.config/ie-mcp/.env`: Environment variables (API keys, etc.)

## 4. Golden Rules & Conventions

1.  **Simplicity is paramount.** The gateway should be a thin, fast, reliable tool server. Avoid adding unnecessary complexity.
2.  **Backwards compatibility matters.** When adding or changing tools, consider the impact on Manus. Avoid breaking changes to tool signatures.
3.  **Security is critical.** All endpoints that perform actions (`/api/shell`, `/api/deploy`) must be protected by the `GATEWAY_TOKEN`.
4.  **Configuration** is loaded from `~/.config/ie-mcp/.env` on startup. Do not hardcode secrets.
5.  **The dashboard** should be a simple, static HTML file with vanilla JavaScript. No complex frontend frameworks.
6.  **`execute_code_task`** is the primary tool. It must use the `ANTHROPIC_API_KEY` directly and skip slow MCP server initialization via `--mcp-config`.

## 5. Current Sprint Priorities (as of Mar 16, 2026)

- **P0: Stability and Reliability**: Ensure `execute_code_task` completes in under 90 seconds.
- **P1: Cost & Performance Dashboard**: Enhance the dashboard to show real-time cost per task, token counts, and duration.
- **P2: Client Installer**: Package the gateway as a simple `.dmg` installer for new clients.

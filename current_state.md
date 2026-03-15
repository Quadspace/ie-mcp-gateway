# Current Gateway State (2026-03-15)

## Health Check Response
- Status: ok
- Version: 5.0.0
- OpenRouter configured: true
- ngrok gateway configured: true
- ngrok domain: dinoonemcp.ngrok.app
- ngrok gateway URL: https://dino-one-ai.ngrok.app
- Tiers:
  - free: llama3
  - openrouter: anthropic/claude-haiku-4.5
  - standard: anthropic/claude-sonnet-4.6
  - power: anthropic/claude-opus-4.6
- Active jobs: 0
- Max workers: 3
- Project path: /Users/ie.ai-dino1/Documents/Dino_One_MCP

## Core Bug
The execute_code_task function calls claude CLI which defaults to local Ollama model (qwen2.5-coder:14b) instead of using OpenRouter.
The fix needs to either:
1. Pass --model flag and correct env vars to claude CLI subprocess
2. OR bypass claude CLI entirely and call OpenRouter API directly via httpx

## Key Files on Mac Mini
- /Users/ie.ai-dino1/Downloads/ie_mcp_gateway/server/gateway.py - Main server
- ~/.config/ie-mcp/gateway.db - SQLite database
- /Users/ie.ai-dino1/.local/bin/claude - Claude CLI v2.1.42

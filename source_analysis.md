# Source Code Analysis of gateway.py on Mac Mini

## Current State (v6.0)
- FastAPI server on port 8765
- Auth: Bearer token = GATEWAY_TOKEN env var, default "ie-gateway-mike-2026"
- OpenRouter API key loaded from env or .env file
- Model tiers: Fast (gpt-4o-mini), Standard (claude-sonnet-4.6), Max (claude-opus-4.6)
- The model names use "openrouter:" prefix: e.g., "openrouter:anthropic/claude-sonnet-4.6"
- NGROK_AI_GATEWAY_URL defaults to "https://openrouter.ai/api/v1"
- run_ai_task() calls OpenRouter directly via httpx - NO claude CLI!
- CreditOptimizer.ai analysis engine for smart routing
- SQLite persistent memory with search
- Dashboard is inline HTML in the root endpoint

## The REAL Bug
The model names have "openrouter:" prefix which OpenRouter doesn't understand.
The model should be "anthropic/claude-sonnet-4-20250514" not "openrouter:anthropic/claude-sonnet-4.6"
Also "claude-sonnet-4.6" and "claude-opus-4.6" are not valid OpenRouter model IDs.

## What needs fixing
1. Fix model names to valid OpenRouter IDs
2. Add execute_code_task tool (alias for execute_task)
3. Improve the dashboard HTML
4. Add CORS middleware (already there)
5. Add OAuth endpoints for Manus registration
6. Add more API endpoints for dashboard data
7. Better error handling in execute_task

## Files to create/update
- server/gateway.py - Fix model names, add execute_code_task, improve dashboard
- dashboard/index.html - Already created in previous session
- ecosystem.config.js - PM2 config
- .gitignore - Git ignore file
- README.md - Comprehensive documentation
- CLAUDE.md - Context file for claude
- config/.env.template - Environment template

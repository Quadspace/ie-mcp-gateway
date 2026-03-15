# IE.AI MCP Gateway — Changelog

This file tracks every change to the system. When you ask Manus to make an
update, it will add an entry here so you always know what changed and when.

---

## How to Request Updates

Tell Manus any of the following:
- "Add a new user to the gateway"
- "Add Ollama local model support"
- "Add voice/Whisper support"
- "Add a new project to the dashboard"
- "Change the rate limit to X requests per minute"
- "Add a spend alert when a user hits 80% of their cap"
- "Add a new panel to the dashboard showing X"

Manus will update the relevant files and add an entry to this changelog.

---

## Files You May Need to Edit Manually

| File | When to Edit |
|---|---|
| `~/.config/ie-mcp/.env` | Change ports, worker count, rate limits |
| `~/.config/ie-mcp/users.json` | Add/remove users, change API keys |
| `<project>/CLAUDE.md` | Update project memory after major changes |

---

## Version History

### v1.0.0 — Initial Release (2026-03-11)

**What was built:**
- `server/gateway.py` — FastAPI MCP server with JSON-RPC 2.0 protocol
- `dashboard/index.html` — IE.AI branded Command Center dashboard
- `dashboard/assets/ie-logo.png` — IE.AI logo asset
- `setup.sh` — One-command Mac Mini setup script
- `config/users.template.json` — Multi-tenant user config template
- `SKILL.md` — Manus skill for automatic delegation to Mac Mini

**Features:**
- Multi-tenant authentication (per-user gateway API keys)
- Per-user Anthropic API key isolation (no shared billing)
- Rate limiting (60 req/min per user, configurable)
- Persistent task context via `task_id` (conversation memory)
- 3 concurrent worker slots (configurable by Mac Mini spec)
- Real-time dashboard served on same port as MCP server (no CORS issues)
- 4 MCP tools: `execute_code_task`, `clear_task_context`, `get_server_config`
- Health endpoint at `/health` (public, no auth)
- Stats endpoint at `/api/stats` (feeds the dashboard)
- pm2 process management (auto-restart, boot persistence)
- macOS sleep prevention reminder in setup
- CLAUDE.md auto-generation prompt during setup

**Known limitations in v1.0.0:**
- Dashboard shows "Fetching tunnel URL..." until ngrok is running
- Claude Code must be authenticated separately after install (`claude` command)
- ngrok free tier URL changes on restart (upgrade to Personal $10/mo to fix)

---

_Future entries will be added here by Manus when updates are made._

---

### v2.0.0 — 2026-03-11 (Session 2)

**What was added:**
- Ollama integration for free local inference (`query_local_model` MCP tool)
- Whisper audio transcription (`transcribe_audio` MCP tool)
- Multi-tenant user management with per-user spend limits
- IE.AI branded dashboard with real-time monitoring (served from same port)
- SQLite audit log with full request history
- `clear_task_context` and `get_server_config` MCP tools
- pm2 process management support
- ngrok tunnel integration (free local inspector at localhost:4040)
- `setup.sh` one-command installation script
- Rate limiting reduced to 30 req/min (more conservative default)

---

### v3.0.0 — 2026-03-11 (Session 3)

**What was added:**

**Bidirectional Context Flow (Structured Insights)**

The gateway now parses Claude Code output into typed Insight objects that Manus
can act on directly, without reading raw text. Every `execute_code_task` response
now returns a JSON object with a structured `insights` array:

| Insight type | What it means | Manus action |
|---|---|---|
| `new_file` | Claude wrote a new file | `file write` with path + content |
| `code_delta` | Claude modified an existing file | `file edit` to apply the diff |
| `dependency_required` | A package install is needed | `shell exec` the command |
| `user_clarification_needed` | Claude has a question | `message ask` the user |
| `general_finding` | Summary / explanation | Read and continue |

**Model Tier Auto-Routing**

The gateway now automatically selects the right model based on prompt keywords:

| Tier | Model | Trigger keywords | Cost |
|---|---|---|---|
| `free` | Ollama (Llama 3) | "use ollama", "free tier", "summarize", "translate" | $0 |
| `standard` | Claude Sonnet | *(default)* | Low |
| `power` | Claude Opus | "use opus", "power tier", "architecture", "refactor entire" | Higher |

- Ollama failures automatically fall back to standard tier
- `tier` argument on `execute_code_task` overrides keyword detection
- ngrok AI Gateway proxy activated via `NGROK_AI_GATEWAY_URL` in `.env`

**Dashboard Updates**

- Tier Usage Stats row: 4 new cards (Free / Standard / Power counts + total insights)
- Recent Jobs table: Tier column (color-coded) + Insights button per row
- Insights Modal: click any job to see structured breakdown (files, deps, questions)

**Audit DB Updates**

- `audit_log` table gains `tier` and `insight_count` columns
- Automatic migration: existing databases gain new columns without data loss

**SKILL.md Updated (v6)**

- Documents structured insight response format
- Explains tier routing with override keywords
- Provides action table for each insight type
- Updated credit savings estimates

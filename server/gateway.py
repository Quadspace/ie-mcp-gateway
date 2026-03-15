#!/usr/bin/env python3
"""
IE.AI MCP Gateway v7.0.0
========================
Production MCP server that dispatches coding tasks to Claude Code CLI
on the Mac Mini, routed through OpenRouter for cost savings.

Built on the official MCP Python SDK (FastMCP).

Architecture:
  Manus -> ngrok -> this server -> claude CLI -> OpenRouter -> Anthropic
  
The claude CLI is a full coding agent with filesystem access.
It reads files, writes code, runs tests, and modifies the codebase.
"""

import os
import json
import time
import sqlite3
import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, HTMLResponse, Response

# ─── Configuration ────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ie-mcp-gateway")

VERSION = "7.2.0"

# Load .env from config directory
HOME = Path(os.environ.get("HOME", "/home/ubuntu"))
CONFIG_DIR = HOME / ".config" / "ie-mcp"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
ENV_FILE = CONFIG_DIR / ".env"

if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

# Core settings
OPENROUTER_API_KEY = os.environ.get(
    "OPENROUTER_API_KEY",
    "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",
)
GATEWAY_TOKEN = os.environ.get("GATEWAY_TOKEN", "ie-gateway-mike-2026")
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", str(HOME / ".local" / "bin" / "claude"))
PROJECT_PATH = os.environ.get("PROJECT_PATH", str(HOME / "Documents" / "Dino_One_MCP"))
NGROK_DOMAIN = os.environ.get("NGROK_DOMAIN", "dinoonemcp.ngrok.app")
GATEWAY_PORT = int(os.environ.get("GATEWAY_PORT", "8765"))
DB_PATH = CONFIG_DIR / "gateway.db"
DASHBOARD_DIR = Path(__file__).parent.parent / "dashboard"

# Model tiers — these map to claude CLI --model flag values
MODEL_TIERS = {
    "standard": {"model": "sonnet", "label": "Claude Sonnet", "credits_saved": 450},
    "power":    {"model": "opus",   "label": "Claude Opus",   "credits_saved": 900},
}

# ─── Database ─────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute("""CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        task_id TEXT,
        tier TEXT,
        model TEXT,
        prompt_preview TEXT,
        output TEXT,
        duration_ms INTEGER,
        status TEXT,
        error_message TEXT,
        credits_saved INTEGER DEFAULT 0
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT DEFAULT 'general',
        key TEXT,
        value TEXT NOT NULL,
        importance INTEGER DEFAULT 0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(category, key)
    )""")
    conn.commit()
    conn.close()

init_db()

def log_task(task_id, tier, model, prompt, output, duration_ms, status, error=None, credits_saved=0):
    try:
        conn = get_db()
        conn.execute(
            """INSERT INTO tasks (task_id, tier, model, prompt_preview, output, duration_ms, status, error_message, credits_saved)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (task_id, tier, model, prompt[:500], output[:10000] if output else None,
             duration_ms, status, error, credits_saved),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"DB log error: {e}")

def memory_store(value, category="general", key=None, importance=0):
    conn = get_db()
    conn.execute(
        """INSERT INTO memory (category, key, value, importance)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(category, key) DO UPDATE SET value=excluded.value""",
        (category, key or value[:50], value, importance),
    )
    conn.commit()
    conn.close()

def memory_search(query, limit=5):
    conn = get_db()
    words = [w for w in query.lower().split() if len(w) > 2][:5]
    if not words:
        conn.close()
        return []
    clauses = " OR ".join(["LOWER(value) LIKE ?" for _ in words])
    params = [f"%{w}%" for w in words] + [limit]
    rows = conn.execute(
        f"SELECT id, category, key, value, importance FROM memory WHERE ({clauses}) ORDER BY importance DESC LIMIT ?",
        params,
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def memory_list(category=None):
    conn = get_db()
    if category:
        rows = conn.execute("SELECT * FROM memory WHERE category=? ORDER BY importance DESC", (category,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM memory ORDER BY importance DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_task_history(limit=20):
    conn = get_db()
    rows = conn.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_stats():
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    success = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='success'").fetchone()[0]
    total_saved = conn.execute("SELECT SUM(credits_saved) FROM tasks WHERE status='success'").fetchone()[0] or 0
    conn.close()
    return {"total_tasks": total, "successful": success, "total_credits_saved": total_saved}


# ─── Claude Code Execution Engine ─────────────────────────────────────────────

async def run_claude_code(task: str, tier: str = "standard", working_dir: str = None, max_turns: int = 20) -> dict:
    """
    Execute a task using the Claude Code CLI.
    
    This spawns the actual `claude` binary which is a full coding agent:
    - It can read and write files on the Mac Mini filesystem
    - It can run shell commands
    - It can modify codebases, run tests, deploy code
    - It routes through OpenRouter for the LLM calls
    
    Returns dict with: output, duration_ms, model, status, error
    """
    tier_config = MODEL_TIERS.get(tier, MODEL_TIERS["standard"])
    model = tier_config["model"]
    
    # Environment: route claude CLI through OpenRouter
    # Ensure PATH includes the claude binary's directory and common macOS paths
    claude_dir = str(Path(CLAUDE_BIN).parent)
    extra_paths = [
        claude_dir,
        "/opt/homebrew/bin",
        "/usr/local/bin",
        str(HOME / ".local" / "bin"),
        str(HOME / ".nvm" / "versions" / "node"),  # nvm users
    ]
    current_path = os.environ.get("PATH", "/usr/bin:/bin")
    full_path = ":".join(extra_paths) + ":" + current_path
    
    env = {
        **os.environ,
        "PATH": full_path,
        "ANTHROPIC_BASE_URL": "https://openrouter.ai/api",
        "ANTHROPIC_AUTH_TOKEN": OPENROUTER_API_KEY,
        "ANTHROPIC_API_KEY": "",  # MUST be empty string to prevent Anthropic auth
    }
    
    # Build the command
    # NOTE: Do NOT pass --model flag when using OpenRouter — OpenRouter's Anthropic Skin
    # handles model routing automatically via ANTHROPIC_BASE_URL. Passing --model can
    # cause Claude Code to attempt Anthropic-specific model validation and hang.
    cmd = [
        CLAUDE_BIN,
        "-p", task,                         # Print mode (non-interactive)
        "--output-format", "text",          # Plain text output
        "--dangerously-skip-permissions",   # Skip permission prompts for automation
        "--max-turns", str(max_turns),      # Safety limit on agentic turns
    ]
    
    cwd = working_dir or PROJECT_PATH
    
    logger.info(f"Executing claude CLI: tier={tier}, model={model}, cwd={cwd}")
    logger.info(f"Task preview: {task[:200]}")
    
    start = time.time()
    
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
        )
        
        # Wait for completion with timeout
        # Standard tasks: 180s. Power tasks: 300s.
        timeout_secs = 300 if tier == "power" else 180
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_secs)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            duration_ms = int((time.time() - start) * 1000)
            return {
                "output": f"Task timed out after {timeout_secs} seconds. Consider using a simpler task or power tier for complex work.",
                "duration_ms": duration_ms,
                "model": model,
                "status": "timeout",
                "error": f"Task timed out after {timeout_secs} seconds",
            }
        
        duration_ms = int((time.time() - start) * 1000)
        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        
        # Combine stdout and stderr — Claude Code writes progress to stderr and result to stdout
        # If stdout is empty but stderr has content, use stderr as the output
        output = stdout_text if stdout_text else stderr_text
        if not output:
            output = f"Task completed (no output captured). Exit code: {proc.returncode}"
        
        if proc.returncode != 0:
            logger.error(f"Claude CLI failed (exit {proc.returncode}): {stderr_text[:500]}")
            return {
                "output": output,
                "duration_ms": duration_ms,
                "model": model,
                "status": "error",
                "error": f"Exit code {proc.returncode}: {stderr_text[:200]}",
            }
        
        logger.info(f"Claude CLI completed in {duration_ms}ms, output length: {len(output)}")
        return {
            "output": output,
            "duration_ms": duration_ms,
            "model": model,
            "status": "success",
            "error": None,
        }
        
    except FileNotFoundError:
        duration_ms = int((time.time() - start) * 1000)
        return {
            "output": None,
            "duration_ms": duration_ms,
            "model": model,
            "status": "error",
            "error": f"Claude CLI not found at {CLAUDE_BIN}. Install: npm install -g @anthropic-ai/claude-code",
        }
    except Exception as e:
        duration_ms = int((time.time() - start) * 1000)
        return {
            "output": None,
            "duration_ms": duration_ms,
            "model": model,
            "status": "error",
            "error": str(e),
        }


# ─── MCP Server ───────────────────────────────────────────────────────────────

mcp = FastMCP(
    "IE.AI MCP Gateway",
    host="0.0.0.0",
    port=GATEWAY_PORT,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)


@mcp.tool()
async def execute_code_task(task: str, tier: str = "standard", working_dir: str = "", max_turns: int = 10) -> str:
    """
    Execute a coding task using Claude Code on the Mac Mini.
    
    Claude Code is a full coding agent with filesystem access. It can:
    - Read and write files
    - Run shell commands and tests
    - Modify codebases
    - Deploy applications
    
    Args:
        task: The coding task to execute. Be specific about what you want done.
        tier: "standard" (Claude Sonnet, fast) or "power" (Claude Opus, complex tasks)
        working_dir: Optional working directory path. Leave empty for /tmp (faster for simple tasks).
        max_turns: Maximum agentic turns (default 10). Use 20-50 for complex multi-file tasks.
    
    Returns:
        The full output from Claude Code including any files created/modified.
    """
    import uuid
    task_id = str(uuid.uuid4())[:8]
    
    tier = tier.lower().strip()
    if tier not in MODEL_TIERS:
        tier = "standard"
    
    # Use /tmp for tasks that don't need project access (much faster — avoids reading codebase)
    effective_working_dir = working_dir if working_dir else "/tmp"
    
    result = await run_claude_code(
        task=task,
        tier=tier,
        working_dir=effective_working_dir,
        max_turns=max_turns,
    )
    
    credits_saved = MODEL_TIERS[tier]["credits_saved"] if result["status"] == "success" else 0
    
    # Log to database
    log_task(
        task_id=task_id,
        tier=tier,
        model=result["model"],
        prompt=task,
        output=result["output"],
        duration_ms=result["duration_ms"],
        status=result["status"],
        error=result["error"],
        credits_saved=credits_saved,
    )
    
    if result["status"] == "success":
        return json.dumps({
            "status": "success",
            "task_id": task_id,
            "tier": tier,
            "model": result["model"],
            "duration_ms": result["duration_ms"],
            "credits_saved": credits_saved,
            "output": result["output"],
        })
    else:
        return json.dumps({
            "status": result["status"],
            "task_id": task_id,
            "tier": tier,
            "model": result["model"],
            "duration_ms": result["duration_ms"],
            "error": result["error"],
            "output": result["output"],
        })


@mcp.tool()
async def execute_task(prompt: str, tier: str = "standard", session_id: str = "") -> str:
    """
    Alias for execute_code_task. Execute any task using Claude Code.
    
    Args:
        prompt: The task to execute.
        tier: "standard" (Sonnet) or "power" (Opus). Also accepts "free"/"openrouter" (maps to standard).
        session_id: Optional session grouping ID.
    """
    # Map legacy tier names
    tier_map = {"free": "standard", "openrouter": "standard"}
    tier = tier_map.get(tier.lower().strip(), tier.lower().strip())
    return await execute_code_task(task=prompt, tier=tier)


@mcp.tool()
async def remember(value: str, category: str = "general", key: str = "", importance: int = 0) -> str:
    """
    Store a memory for future context. Memories persist across sessions.
    
    Args:
        value: The information to remember.
        category: Category like 'preference', 'fact', 'decision', 'project'.
        key: Short key for this memory. Auto-generated if empty.
        importance: 0-10 importance score. Higher = recalled first.
    """
    memory_store(value, category, key if key else None, importance)
    return f"Stored: [{category}] {value[:80]}"


@mcp.tool()
async def recall(query: str) -> str:
    """
    Search stored memories by keyword.
    
    Args:
        query: Keywords to search for in memories.
    """
    memories = memory_search(query)
    if not memories:
        return "No relevant memories found."
    return "\n".join([f"[{m['category']}] {m['value']}" for m in memories])


@mcp.tool()
async def list_memory(category: str = "") -> str:
    """
    List all stored memories, optionally filtered by category.
    
    Args:
        category: Optional category filter.
    """
    memories = memory_list(category if category else None)
    if not memories:
        return "No memories stored."
    return "\n".join([f"[{m['id']}] [{m['category']}] {m['value'][:120]}" for m in memories])


@mcp.tool()
async def forget(memory_id: int) -> str:
    """
    Delete a specific memory by ID.
    
    Args:
        memory_id: The ID of the memory to delete (from list_memory).
    """
    conn = get_db()
    conn.execute("DELETE FROM memory WHERE id=?", (memory_id,))
    conn.commit()
    conn.close()
    return f"Deleted memory #{memory_id}"


# ─── HTTP Routes (Dashboard, Health, API) ─────────────────────────────────────

@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> Response:
    """Health check endpoint."""
    stats = get_stats()
    return JSONResponse({
        "status": "ok",
        "version": VERSION,
        "service": "IE.AI MCP Gateway",
        "claude_bin": CLAUDE_BIN,
        "claude_bin_exists": os.path.exists(CLAUDE_BIN),
        "openrouter_configured": bool(OPENROUTER_API_KEY),
        "ngrok_domain": NGROK_DOMAIN,
        "project_path": PROJECT_PATH,
        "models": {k: v["label"] for k, v in MODEL_TIERS.items()},
        "stats": stats,
    })


@mcp.custom_route("/", methods=["GET"])
async def dashboard(request: Request) -> Response:
    """Serve the dashboard HTML."""
    dashboard_file = DASHBOARD_DIR / "index.html"
    if dashboard_file.exists():
        return HTMLResponse(dashboard_file.read_text())
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><title>IE.AI MCP Gateway v{VERSION}</title></head>
<body style="font-family:system-ui;max-width:800px;margin:40px auto;padding:20px;">
<h1>IE.AI MCP Gateway v{VERSION}</h1>
<p>Dashboard HTML not found at {dashboard_file}</p>
<p><a href="/health">Health Check</a> | <a href="/api/tasks">Task History</a></p>
</body></html>""")


@mcp.custom_route("/api/tasks", methods=["GET"])
async def api_tasks(request: Request) -> Response:
    """Get task history."""
    limit = int(request.query_params.get("limit", "20"))
    tasks = get_task_history(limit)
    return JSONResponse(tasks)


@mcp.custom_route("/api/stats", methods=["GET"])
async def api_stats(request: Request) -> Response:
    """Get gateway statistics."""
    return JSONResponse(get_stats())


@mcp.custom_route("/api/memory", methods=["GET"])
async def api_memory(request: Request) -> Response:
    """Get all memories."""
    category = request.query_params.get("category")
    memories = memory_list(category)
    return JSONResponse(memories)


@mcp.custom_route("/api/config", methods=["GET"])
async def api_config(request: Request) -> Response:
    """Get gateway configuration (non-sensitive)."""
    return JSONResponse({
        "version": VERSION,
        "claude_bin": CLAUDE_BIN,
        "project_path": PROJECT_PATH,
        "ngrok_domain": NGROK_DOMAIN,
        "gateway_port": GATEWAY_PORT,
        "db_path": str(DB_PATH),
        "tiers": {k: {"model": v["model"], "label": v["label"], "credits_saved": v["credits_saved"]} for k, v in MODEL_TIERS.items()},
    })


# ─── OAuth endpoints for Manus MCP registration ──────────────────────────────

@mcp.custom_route("/oauth/authorize", methods=["GET"])
async def oauth_authorize(request: Request) -> Response:
    """OAuth authorize endpoint — returns the gateway token directly."""
    redirect_uri = request.query_params.get("redirect_uri", "")
    state = request.query_params.get("state", "")
    if redirect_uri:
        sep = "&" if "?" in redirect_uri else "?"
        return Response(
            status_code=302,
            headers={"Location": f"{redirect_uri}{sep}code={GATEWAY_TOKEN}&state={state}"},
        )
    return JSONResponse({"token": GATEWAY_TOKEN})


@mcp.custom_route("/oauth/token", methods=["POST"])
async def oauth_token(request: Request) -> Response:
    """OAuth token endpoint — returns the gateway token."""
    return JSONResponse({
        "access_token": GATEWAY_TOKEN,
        "token_type": "bearer",
        "expires_in": 86400 * 365,
    })


# ─── Self-Healing: Patch Claude Code settings ────────────────────────────────

def patch_claude_settings():
    """
    Remove any hardcoded 'model' from ~/.claude/settings.json.
    This prevents local Ollama models from overriding OpenRouter routing.
    Called once at startup.
    """
    settings_path = HOME / ".claude" / "settings.json"
    if not settings_path.exists():
        return
    try:
        with open(settings_path, "r") as f:
            settings = json.load(f)
        if "model" in settings:
            removed_model = settings.pop("model")
            with open(settings_path, "w") as f:
                json.dump(settings, f, indent=2)
            logger.info(f"Patched ~/.claude/settings.json: removed hardcoded model '{removed_model}'")
        else:
            logger.info("~/.claude/settings.json: no hardcoded model found (clean)")
    except Exception as e:
        logger.warning(f"Could not patch ~/.claude/settings.json: {e}")


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    patch_claude_settings()
    logger.info(f"Starting IE.AI MCP Gateway v{VERSION}")
    logger.info(f"Claude CLI: {CLAUDE_BIN}")
    logger.info(f"Project path: {PROJECT_PATH}")
    logger.info(f"OpenRouter: {'configured' if OPENROUTER_API_KEY else 'NOT configured'}")
    logger.info(f"Port: {GATEWAY_PORT}")
    logger.info(f"MCP endpoint: http://0.0.0.0:{GATEWAY_PORT}/mcp")
    logger.info(f"Dashboard: http://0.0.0.0:{GATEWAY_PORT}/")
    mcp.run(transport="streamable-http")

#!/usr/bin/env python3
"""
IE.AI MCP Gateway v8.5
======================
A fast, reliable MCP tool server for the Mac Mini.
Manus is the AI orchestrator. This gateway is the hands.

Architecture (simple and correct):
  Manus -> ngrok -> this gateway -> Mac Mini tools (shell, files, Anthropic API)

Key improvements over v7:
  - NO Claude CLI subprocess. Direct Anthropic API calls instead.
  - Tasks complete in <10 seconds instead of 3-5 minutes.
  - No OAuth sessions to expire. API key never changes.
  - Shell execution tool for any command Manus needs to run.
  - File read/write tools for direct filesystem access.
"""
import os
import json
import time
import uuid
import sqlite3
import asyncio
import logging
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, HTMLResponse, Response
from starlette.websockets import WebSocket, WebSocketDisconnect

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("ie-mcp-gateway")

VERSION = "8.7.0"

# ─── Configuration ────────────────────────────────────────────────────────────
HOME = Path(os.environ.get("HOME", "/Users/ie.ai-dino1"))
CONFIG_DIR = HOME / ".config" / "ie-mcp"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
ENV_FILE = CONFIG_DIR / ".env"

# Load .env file — force-override any empty strings set by ecosystem.config.js.
# PM2 sets ANTHROPIC_API_KEY="" intentionally for OpenRouter routing, but we
# need the real sk-ant-api03-... key from .env for Claude Code CLI subprocesses.
ENV_VARS = {}
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            ENV_VARS[k] = v
            # Force-set: override empty strings from ecosystem.config.js
            if not os.environ.get(k):  # only override if not set or empty
                os.environ[k] = v

ANTHROPIC_API_KEY  = ENV_VARS.get("ANTHROPIC_API_KEY", "") or os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "")
GATEWAY_TOKEN      = os.environ.get("GATEWAY_TOKEN", "ie-gateway-mike-2026")
PROJECT_PATH       = os.environ.get("PROJECT_PATH", str(HOME / "Documents" / "Dino_One_MCP"))
NGROK_DOMAIN       = os.environ.get("NGROK_DOMAIN", "dinoonemcp.ngrok.app")
GATEWAY_PORT       = int(os.environ.get("MCP_PORT", "8765"))
CLAUDE_BIN         = os.environ.get("CLAUDE_BIN", str(HOME / ".local" / "bin" / "claude"))
EMPTY_MCP_CFG      = CONFIG_DIR / "empty-mcp.json"
DB_PATH            = CONFIG_DIR / "gateway.db"
DASHBOARD_DIR      = Path(__file__).parent.parent / "dashboard"

# Anthropic model names
MODELS = {
    "standard": "claude-sonnet-4-5",
    "power":    "claude-opus-4-5",
}

# ─── Database ─────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp     TEXT    NOT NULL,
            task_id       TEXT    NOT NULL,
            tool          TEXT    NOT NULL DEFAULT 'unknown',
            tier          TEXT,
            model         TEXT,
            prompt_preview TEXT,
            output        TEXT,
            duration_ms   INTEGER,
            status        TEXT    NOT NULL DEFAULT 'pending',
            error_message TEXT
        )
    """)
    conn.commit()
    # Migrate: add columns that may be missing from older schema versions
    for col, definition in [
        ("tool",  "TEXT NOT NULL DEFAULT 'unknown'"),
        ("tier",  "TEXT"),
        ("model", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} {definition}")
            conn.commit()
            logger.info(f"Migration: added column '{col}' to tasks table")
        except Exception:
            pass  # Column already exists
    conn.close()
    logger.info(f"Database ready: {DB_PATH}")
    # Write empty MCP config so Claude Code skips slow MCP server initialization
    if not EMPTY_MCP_CFG.exists():
        EMPTY_MCP_CFG.write_text('{"mcpServers":{}}')
        logger.info(f"Created empty MCP config: {EMPTY_MCP_CFG}")

# ─── WebSocket Connection Manager ────────────────────────────────────────────
class _WSManager:
    """Tracks all open WebSocket connections and broadcasts task updates."""
    def __init__(self):
        self._connections: set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._connections.add(ws)
        logger.info(f"[WS] Client connected ({len(self._connections)} total)")

    def disconnect(self, ws: WebSocket):
        self._connections.discard(ws)
        logger.info(f"[WS] Client disconnected ({len(self._connections)} remaining)")

    async def broadcast(self, payload: dict):
        if not self._connections:
            return
        msg = json.dumps(payload)
        dead = set()
        for ws in self._connections:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.add(ws)
        for ws in dead:
            self._connections.discard(ws)

ws_manager = _WSManager()


def log_task(task_id, tool, tier, model, prompt, output, duration_ms, status, error=None):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    conn.execute(
        """INSERT INTO tasks
           (timestamp, task_id, tool, tier, model, prompt_preview, output, duration_ms, status, error_message)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            ts,
            task_id,
            tool,
            tier,
            model,
            (prompt or "")[:300],
            (output or "")[:10000] if output else None,
            duration_ms,
            status,
            error,
        ),
    )
    conn.commit()
    conn.close()
    # Broadcast the update to all connected WebSocket clients
    asyncio.get_event_loop().call_soon_threadsafe(
        lambda: asyncio.ensure_future(ws_manager.broadcast({
            "type": "task_update",
            "task_id": task_id,
            "tool": tool,
            "tier": tier,
            "status": status,
            "duration_ms": duration_ms,
            "timestamp": ts,
            "prompt_preview": (prompt or "")[:300],
            "output": (output or "")[:2000],
        }))
    )

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

# ─── Tool 1: run_shell_command ────────────────────────────────────────────────
@mcp.tool()
async def run_shell_command(
    command: str,
    cwd: Optional[str] = None,
    timeout: int = 60,
) -> str:
    """
    Run any shell command on the Mac Mini and return the output.
    Use this to: restart services, run scripts, check status, deploy code, etc.

    Args:
        command: The shell command to run (e.g. "pm2 restart ie-mcp-gateway")
        cwd: Working directory (defaults to project path)
        timeout: Max seconds to wait (default 60, max 300)

    Returns:
        Combined stdout + stderr output as a string.
    """
    task_id = uuid.uuid4().hex[:8]
    start = time.time()
    working_dir = cwd or PROJECT_PATH
    timeout = min(timeout, 300)

    logger.info(f"[{task_id}] run_shell_command: {command[:100]} (cwd={working_dir})")

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            duration_ms = int((time.time() - start) * 1000)
            msg = f"Command timed out after {timeout}s: {command[:100]}"
            log_task(task_id, "run_shell_command", None, None, command, msg, duration_ms, "timeout", msg)
            return msg

        duration_ms = int((time.time() - start) * 1000)
        out = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()
        combined = out if out else err
        if not combined:
            combined = f"(no output, exit code {proc.returncode})"

        status = "success" if proc.returncode == 0 else "error"
        log_task(task_id, "run_shell_command", None, None, command, combined, duration_ms, status)
        logger.info(f"[{task_id}] Completed in {duration_ms}ms, exit={proc.returncode}")
        return combined

    except Exception as e:
        duration_ms = int((time.time() - start) * 1000)
        msg = f"Error running command: {e}"
        log_task(task_id, "run_shell_command", None, None, command, msg, duration_ms, "error", str(e))
        return msg


# ─── Tool 2: read_file ────────────────────────────────────────────────────────
@mcp.tool()
async def read_file(path: str) -> str:
    """
    Read the contents of any file on the Mac Mini.

    Args:
        path: Absolute path to the file (e.g. "/Users/ie.ai-dino1/Downloads/ie_mcp_gateway/server/gateway.py")

    Returns:
        File contents as a string, or an error message.
    """
    task_id = uuid.uuid4().hex[:8]
    start = time.time()
    logger.info(f"[{task_id}] read_file: {path}")

    try:
        content = Path(path).read_text(encoding="utf-8", errors="replace")
        duration_ms = int((time.time() - start) * 1000)
        log_task(task_id, "read_file", None, None, path, content, duration_ms, "success")
        return content
    except FileNotFoundError:
        msg = f"File not found: {path}"
        log_task(task_id, "read_file", None, None, path, msg, 0, "error", msg)
        return msg
    except Exception as e:
        msg = f"Error reading file: {e}"
        log_task(task_id, "read_file", None, None, path, msg, 0, "error", str(e))
        return msg


# ─── Tool 3: write_file ───────────────────────────────────────────────────────
@mcp.tool()
async def write_file(path: str, content: str) -> str:
    """
    Write content to any file on the Mac Mini (creates or overwrites).

    Args:
        path: Absolute path to the file
        content: The full content to write

    Returns:
        Success message or error.
    """
    task_id = uuid.uuid4().hex[:8]
    start = time.time()
    logger.info(f"[{task_id}] write_file: {path} ({len(content)} chars)")

    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        duration_ms = int((time.time() - start) * 1000)
        msg = f"Successfully wrote {len(content)} characters to {path}"
        log_task(task_id, "write_file", None, None, path, msg, duration_ms, "success")
        return msg
    except Exception as e:
        msg = f"Error writing file: {e}"
        log_task(task_id, "write_file", None, None, path, msg, 0, "error", str(e))
        return msg


# ─── Tool 4: run_anthropic_completion ────────────────────────────────────────
@mcp.tool()
async def run_anthropic_completion(
    prompt: str,
    tier: str = "standard",
    system: Optional[str] = None,
    max_tokens: int = 4096,
) -> str:
    """
    Send a prompt directly to the Anthropic API and return the response.
    This is the fast, reliable replacement for the old execute_code_task tool.
    Tasks complete in seconds, not minutes.

    Args:
        prompt: The task or question to send to Claude
        tier: "standard" (Sonnet, fast) or "power" (Opus, best quality)
        system: Optional system prompt to set context
        max_tokens: Maximum tokens in the response (default 4096)

    Returns:
        Claude's response as a string.
    """
    if not ANTHROPIC_API_KEY:
        return "Error: ANTHROPIC_API_KEY is not configured. Please set it in ~/.config/ie-mcp/.env"

    task_id = uuid.uuid4().hex[:8]
    start = time.time()
    model = MODELS.get(tier, MODELS["standard"])

    logger.info(f"[{task_id}] run_anthropic_completion: tier={tier}, model={model}")
    logger.info(f"[{task_id}] Prompt preview: {prompt[:150]}")

    messages = [{"role": "user", "content": prompt}]
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system:
        payload["system"] = system

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=payload,
            )

        duration_ms = int((time.time() - start) * 1000)

        if resp.status_code != 200:
            error_body = resp.text[:500]
            msg = f"Anthropic API error {resp.status_code}: {error_body}"
            log_task(task_id, "run_anthropic_completion", tier, model, prompt, msg, duration_ms, "error", msg)
            logger.error(f"[{task_id}] {msg}")
            return msg

        data = resp.json()
        output = data["content"][0]["text"]
        input_tokens = data.get("usage", {}).get("input_tokens", 0)
        output_tokens = data.get("usage", {}).get("output_tokens", 0)

        log_task(task_id, "run_anthropic_completion", tier, model, prompt, output, duration_ms, "success")
        logger.info(f"[{task_id}] Completed in {duration_ms}ms | tokens in={input_tokens} out={output_tokens}")
        return output

    except httpx.TimeoutException:
        duration_ms = int((time.time() - start) * 1000)
        msg = "Request to Anthropic API timed out after 120 seconds."
        log_task(task_id, "run_anthropic_completion", tier, model, prompt, msg, duration_ms, "timeout", msg)
        return msg
    except Exception as e:
        duration_ms = int((time.time() - start) * 1000)
        msg = f"Unexpected error calling Anthropic API: {e}"
        log_task(task_id, "run_anthropic_completion", tier, model, prompt, msg, duration_ms, "error", str(e))
        return msg


# ─── Background runner for execute_code_task ─────────────────────────────────
async def _run_claude_code_background(task_id: str, cmd: list, cwd: str, env: dict,
                                       tier: str, task: str, timeout: int):
    """Runs Claude Code in the background and stores result in DB when done."""
    start = time.time()
    try:
        # Task 1.1: Auto git pull — always work on the latest committed code.
        # If the pull fails (e.g. merge conflict, no remote), the task errors
        # immediately with the git message so we never run Claude Code on stale code.
        git_proc = await asyncio.create_subprocess_exec(
            "git", "-C", cwd, "pull",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
        git_stdout, git_stderr = await asyncio.wait_for(git_proc.communicate(), timeout=30)
        git_out = git_stdout.decode("utf-8", errors="replace").strip()
        git_err = git_stderr.decode("utf-8", errors="replace").strip()
        git_summary = git_out or git_err or "git pull: no output"
        logger.info(f"[{task_id}] git pull: {git_summary[:120]}")
        if git_proc.returncode != 0:
            duration_ms = int((time.time() - start) * 1000)
            msg = f"git pull failed (exit {git_proc.returncode}): {git_summary}"
            log_task(task_id, "execute_code_task", tier, tier, task, msg, duration_ms, "error", msg)
            logger.error(f"[{task_id}] {msg}")
            return

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,  # Fix: prevent stdin pipe hang
            cwd=cwd,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            duration_ms = int((time.time() - start) * 1000)
            msg = f"Task timed out after {timeout}s."
            log_task(task_id, "execute_code_task", tier, tier, task, msg, duration_ms, "timeout", msg)
            logger.info(f"[{task_id}] Timed out after {timeout}s")
            return

        duration_ms = int((time.time() - start) * 1000)
        out = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()
        output = out or err or f"Task completed with exit code {proc.returncode} (no output captured)"
        status = "success" if proc.returncode == 0 else "error"
        log_task(task_id, "execute_code_task", tier, tier, task, output, duration_ms, status)
        logger.info(f"[{task_id}] Completed in {duration_ms}ms, exit={proc.returncode}")

    except Exception as e:
        duration_ms = int((time.time() - start) * 1000)
        msg = f"Error running Claude Code: {e}"
        log_task(task_id, "execute_code_task", tier, tier, task, msg, duration_ms, "error", str(e))
        logger.error(f"[{task_id}] Exception: {e}")


# ─── Tool 5: execute_code_task ───────────────────────────────────────────────
@mcp.tool()
async def execute_code_task(
    task: str,
    tier: str = "standard",
    working_dir: str = "",
    max_turns: int = 10,
) -> str:
    """
    Execute a coding task using Claude Code CLI on the Mac Mini.
    FIRE-AND-FORGET: returns a task_id immediately, runs Claude Code in background.
    Poll GET /api/tasks to check status and retrieve output when complete.

    Use this for tasks that need to read/write files in the project codebase.
    For simple questions or fast responses, use run_anthropic_completion instead.

    Args:
        task: The coding task description
        tier: "standard" (Sonnet) or "power" (Opus)
        working_dir: Project directory path (defaults to PROJECT_PATH)
        max_turns: Max Claude Code iterations, default 10

    Returns:
        JSON string with task_id and poll URL. Check /api/tasks for results.
    """
    if not Path(CLAUDE_BIN).exists():
        return f"Claude CLI not found at {CLAUDE_BIN}. Install: npm install -g @anthropic-ai/claude-code"

    if not ANTHROPIC_API_KEY:
        return "Error: ANTHROPIC_API_KEY is not configured. Set it in ~/.config/ie-mcp/.env"

    task_id = uuid.uuid4().hex[:8]
    cwd = working_dir or PROJECT_PATH
    timeout = 300 if tier == "power" else 180

    logger.info(f"[{task_id}] execute_code_task queued: tier={tier}, cwd={cwd}")
    logger.info(f"[{task_id}] Task preview: {task[:150]}")

    # Log as pending immediately so it shows up in the dashboard
    log_task(task_id, "execute_code_task", tier, tier, task, None, 0, "pending")

    # Build env for Claude Code subprocess.
    # Claude Code CLI requires a DIRECT Anthropic API key and CANNOT use OpenRouter.
    # PM2 ecosystem.config.js poisons os.environ with ANTHROPIC_API_KEY="" and
    # ANTHROPIC_BASE_URL=openrouter. We bypass os.environ entirely by reading
    # the real key directly from ENV_VARS (parsed from .env file at startup).
    env = os.environ.copy()
    real_key = ENV_VARS.get("ANTHROPIC_API_KEY", "") or ENV_VARS.get("ANTHROPIC_AUTH_TOKEN", "")
    env["ANTHROPIC_API_KEY"] = real_key
    env.pop("ANTHROPIC_BASE_URL", None)   # must hit api.anthropic.com directly
    env.pop("ANTHROPIC_AUTH_TOKEN", None) # avoid duplicate auth confusion
    env.pop("OPENROUTER_API_KEY", None)   # not used by Claude Code CLI

    # Fix: wrap with 'script -q /dev/null' to create a pseudo-TTY on macOS.
    # Claude Code CLI hangs without a TTY when spawned from a non-interactive
    # subprocess (e.g. a background service). This is a documented macOS bug.
    # See: https://github.com/anthropics/claude-code/issues/9026
    cmd = [
        "script", "-q", "/dev/null",
        CLAUDE_BIN,
        "-p", task,
        "--output-format", "text",
        "--dangerously-skip-permissions",
        "--max-turns", str(max_turns),
        "--mcp-config", str(EMPTY_MCP_CFG),
    ]

    # Fire and forget — run in background, return task_id immediately
    asyncio.create_task(_run_claude_code_background(task_id, cmd, cwd, env, tier, task, timeout))

    return json.dumps({
        "task_id": task_id,
        "status": "queued",
        "message": f"Claude Code task queued. Poll /api/tasks to check status.",
        "poll_url": f"https://{NGROK_DOMAIN}/api/tasks?limit=10",
        "tier": tier,
        "timeout_seconds": timeout,
    })


# ─── HTTP API Endpoints ───────────────────────────────────────────────────────

def _check_auth(request: Request) -> bool:
    auth = request.headers.get("Authorization", "")
    return auth.replace("Bearer ", "").strip() == GATEWAY_TOKEN

@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> Response:
    conn = get_db()
    row = conn.execute(
        "SELECT COUNT(*) as total, SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as successful FROM tasks"
    ).fetchone()
    conn.close()
    return JSONResponse({
        "status": "ok",
        "version": VERSION,
        "service": "IE.AI MCP Gateway",
        "anthropic_configured": bool(ANTHROPIC_API_KEY),
        "claude_bin": CLAUDE_BIN,
        "claude_exists": Path(CLAUDE_BIN).exists(),
        "ngrok_domain": NGROK_DOMAIN,
        "project_path": PROJECT_PATH,
        "stats": {
            "total_tasks": row["total"] or 0,
            "successful": row["successful"] or 0,
        },
    })

@mcp.custom_route("/api/tasks", methods=["GET"])
async def api_tasks(request: Request) -> Response:
    limit = int(request.query_params.get("limit", "50"))
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return JSONResponse([dict(r) for r in rows])

@mcp.custom_route("/api/shell", methods=["POST"])
async def api_shell(request: Request) -> Response:
    """Direct shell execution endpoint — protected by gateway token."""
    if not _check_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    body = await request.json()
    cmd = body.get("cmd", "")
    cwd = body.get("cwd", PROJECT_PATH)
    timeout = min(int(body.get("timeout", 30)), 300)
    if not cmd:
        return JSONResponse({"error": "cmd is required"}, status_code=400)
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return JSONResponse({
            "exit_code": proc.returncode,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
        })
    except asyncio.TimeoutError:
        return JSONResponse({"exit_code": None, "stdout": "", "stderr": f"Timed out after {timeout}s"}, status_code=408)
    except Exception as e:
        return JSONResponse({"exit_code": -1, "stdout": "", "stderr": str(e)}, status_code=500)

@mcp.custom_route("/api/deploy", methods=["POST"])
async def api_deploy(request: Request) -> Response:
    """Pull latest code from GitHub and restart PM2 — protected by gateway token."""
    if not _check_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    gateway_dir = str(Path(__file__).parent.parent)
    steps = []
    try:
        for step_cmd in ["git pull origin master", "pm2 restart ie-mcp-gateway"]:
            proc = await asyncio.create_subprocess_shell(
                step_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=gateway_dir,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            steps.append({
                "step": step_cmd,
                "exit_code": proc.returncode,
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
            })
            if proc.returncode != 0:
                return JSONResponse({"status": "error", "steps": steps})
        return JSONResponse({"status": "deployed", "steps": steps})
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e), "steps": steps}, status_code=500)

@mcp.custom_route("/api/config", methods=["GET"])
async def api_config(request: Request) -> Response:
    return JSONResponse({
        "version": VERSION,
        "project_path": PROJECT_PATH,
        "ngrok_domain": NGROK_DOMAIN,
        "gateway_port": GATEWAY_PORT,
        "models": MODELS,
        "anthropic_configured": bool(ANTHROPIC_API_KEY),
    })

# ─── OAuth (for Manus MCP registration) ──────────────────────────────────────
@mcp.custom_route("/oauth/authorize", methods=["GET"])
async def oauth_authorize(request: Request) -> Response:
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
    return JSONResponse({
        "access_token": GATEWAY_TOKEN,
        "token_type": "bearer",
        "expires_in": 86400 * 365,
    })

# ─── WebSocket endpoint ──────────────────────────────────────────────────────
@mcp.custom_route("/ws/tasks", methods=["GET", "WEBSOCKET"])
async def ws_tasks(websocket: WebSocket) -> None:
    """WebSocket endpoint — streams real-time task events to connected dashboards."""
    await ws_manager.connect(websocket)
    try:
        while True:
            # Keep connection alive; we only push, never pull
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception:
        ws_manager.disconnect(websocket)

# ─── Dashboard ────────────────────────────────────────────────────────────────
@mcp.custom_route("/", methods=["GET"])
async def dashboard(request: Request) -> Response:
    index = DASHBOARD_DIR / "index.html"
    if index.exists():
        return HTMLResponse(index.read_text())
    return HTMLResponse(f"<h1>IE.AI MCP Gateway v{VERSION}</h1><p>Dashboard not found.</p>")

# ─── Entry Point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    logger.info(f"Starting IE.AI MCP Gateway v{VERSION}")
    logger.info(f"Anthropic API: {'configured' if ANTHROPIC_API_KEY else 'NOT CONFIGURED — set ANTHROPIC_API_KEY in ~/.config/ie-mcp/.env'}")
    logger.info(f"Project path: {PROJECT_PATH}")
    logger.info(f"Port: {GATEWAY_PORT}")
    logger.info(f"Dashboard: http://0.0.0.0:{GATEWAY_PORT}/")
    logger.info(f"MCP endpoint: http://0.0.0.0:{GATEWAY_PORT}/mcp")
    mcp.run(transport="streamable-http")

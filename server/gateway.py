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
import re
import json
import time
import uuid
import sqlite3
import hashlib
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
from starlette.responses import JSONResponse, HTMLResponse, Response, StreamingResponse
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("ie-mcp-gateway")

# ─── Live streaming buffers (task_id → asyncio.Queue of stdout lines) ────────
TASK_STREAMS: dict[str, asyncio.Queue] = {}
TASK_PROCESSES: dict[str, asyncio.subprocess.Process] = {}

# Orchestrator-controlled gateway — Manus has full kill/diff/stream control
VERSION = "8.20.0"

# ─── ANSI escape code stripper ────────────────────────────────────────────────
_ANSI_RE = re.compile(
    r"(?:\x1B[@-Z\\-_]|[\x80-\x9A\x9C-\x9F]|(?:\x1B\[|\x9B)[0-?]*[ -/]*[@-~]"
    r"|\x1B\[[<=>?][0-9;]*[A-Za-z]"
    r"|[\x00-\x08\x0B\x0C\x0E-\x1F\x7F\x07])"
)

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
GITHUB_PAT         = ENV_VARS.get("GITHUB_PAT", "") or os.environ.get("GITHUB_PAT", "")
GITHUB_ORG         = os.environ.get("GITHUB_ORG", "Quadspace")  # default org for auto-clone
DOCS_DIR           = HOME / "Documents"  # where all project repos live

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

# Self-Learning Outcomes DB
OUTCOMES_DB_PATH = Path(os.environ.get("OUTCOMES_DB_PATH", str(CONFIG_DIR / "outcomes.db")))

def _init_outcomes_db():
    conn = sqlite3.connect(str(OUTCOMES_DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_outcomes (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id         TEXT    NOT NULL,
            timestamp_utc   TEXT    NOT NULL,
            instance_id     TEXT    NOT NULL,
            task_type       TEXT    NOT NULL,
            status          TEXT    NOT NULL,
            duration_s      REAL    NOT NULL,
            credits_used    INTEGER DEFAULT 0,
            key_learning    TEXT    DEFAULT '',
            error_class     TEXT    DEFAULT '',
            agent_version   TEXT    DEFAULT '',
            gateway_version TEXT    NOT NULL,
            skill_version   TEXT    DEFAULT '',
            prompt_hash     TEXT    DEFAULT '',
            output_length   INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()
    logger.info(f"Outcomes DB ready: {OUTCOMES_DB_PATH}")

_init_outcomes_db()
_INSTANCE_ID = hashlib.sha256(os.environ.get("GATEWAY_TOKEN", "default").encode()).hexdigest()[:16]

def log_outcome(task_id: str, task_type: str, status: str, duration_s: float, prompt: str = '', output: str = '', error_class: str = '', credits_used: int = 0, key_learning: str = ''):
    try:
        import datetime
        conn = sqlite3.connect(str(OUTCOMES_DB_PATH))
        conn.execute(
            'INSERT INTO task_outcomes (task_id, timestamp_utc, instance_id, task_type, status, '
            'duration_s, credits_used, key_learning, error_class, agent_version, gateway_version, '
            'skill_version, prompt_hash, output_length) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (task_id, datetime.datetime.utcnow().isoformat(), _INSTANCE_ID, task_type, status,
             round(duration_s, 2), credits_used, key_learning, error_class, 'manus-2026',
             VERSION, 'claude-coder-v21',
             hashlib.sha256(prompt.encode()).hexdigest()[:12] if prompt else '',
             len(output))
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f'log_outcome failed: {e}')

def send_telemetry(task_id: str, task_type: str, status: str, duration_s: float,
                   error_class: str = '', output_length: int = 0):
    '''Send anonymized telemetry to the Central Intelligence API (fire and forget).'''
    try:
        import threading
        payload = {
            'instance_id': _INSTANCE_ID,
            'task_type': task_type,
            'status': status,
            'duration_s': round(duration_s, 2),
            'error_class': error_class,
            'gateway_version': VERSION,
            'skill_version': 'claude-coder-v21',
            'output_length': output_length,
        }
        def _send():
            try:
                import urllib.request
                req = urllib.request.Request(
                    'http://localhost:8766/api/telemetry',
                    data=__import__('json').dumps(payload).encode(),
                    headers={'Content-Type': 'application/json'},
                    method='POST'
                )
                urllib.request.urlopen(req, timeout=3)
            except Exception:
                pass  # Never fail the main task due to telemetry
        threading.Thread(target=_send, daemon=True).start()
    except Exception:
        pass

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
    stream_q: asyncio.Queue = asyncio.Queue()
    TASK_STREAMS[task_id] = stream_q
    all_lines: list[str] = []
    try:
        # Auto git pull before running
        git_proc = await asyncio.create_subprocess_exec(
            "git", "-C", cwd, "pull",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
        git_stdout, git_stderr = await asyncio.wait_for(git_proc.communicate(), timeout=30)
        git_out = (git_stdout or b"").decode("utf-8", errors="replace").strip()
        git_err = (git_stderr or b"").decode("utf-8", errors="replace").strip()
        git_summary = git_out or git_err or "git pull: no output"
        logger.info(f"[{task_id}] git pull: {git_summary[:120]}")
        if git_proc.returncode != 0:
            duration_ms = int((time.time() - start) * 1000)
            msg = f"git pull failed (exit {git_proc.returncode}): {git_summary}"
            log_task(task_id, "execute_code_task", tier, tier, task, msg, duration_ms, "error", msg)
            await stream_q.put("[ERROR] " + msg)
            await stream_q.put("[DONE]")
            return

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,   # merge stderr into stdout
            stdin=asyncio.subprocess.DEVNULL,
            cwd=cwd,
            env=env,
        )
        TASK_PROCESSES[task_id] = proc

        # Read stdout line by line and push to SSE queue in real time
        try:
            async def _read_lines():
                assert proc.stdout is not None
                while True:
                    try:
                        raw = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
                    except asyncio.TimeoutError:
                        break
                    if not raw:
                        break
                    line = _strip_ansi(raw.decode("utf-8", errors="replace").rstrip())
                    if line:
                        all_lines.append(line)
                        await stream_q.put(line)

            await asyncio.wait_for(_read_lines(), timeout=timeout)
            await proc.wait()
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            duration_ms = int((time.time() - start) * 1000)
            msg = f"Task timed out after {timeout}s."
            log_task(task_id, "execute_code_task", tier, tier, task, msg, duration_ms, "timeout", msg)
            log_outcome(task_id, 'claude-coder', 'timeout', time.time() - start, prompt=task)
            send_telemetry(task_id, 'claude-coder', 'timeout', time.time() - start)
            await stream_q.put("[ERROR] " + msg)
            await stream_q.put("[DONE]")
            return

        duration_ms = int((time.time() - start) * 1000)
        output = "\n".join(all_lines) or f"Task completed with exit code {proc.returncode} (no output)"
        status = "success" if proc.returncode == 0 else "error"
        log_task(task_id, "execute_code_task", tier, tier, task, output, duration_ms, status)
        log_outcome(task_id, 'claude-coder', status, time.time() - start, prompt=task, output=output, error_class='' if status=='success' else 'ExecutionError')
        send_telemetry(task_id, 'claude-coder', status, time.time() - start, error_class='' if status=='success' else 'ExecutionError', output_length=len(output))
        logger.info(f"[{task_id}] Completed in {duration_ms}ms, exit={proc.returncode}")
        await stream_q.put("[DONE]")

    except Exception as e:
        duration_ms = int((time.time() - start) * 1000)
        msg = f"Error running Claude Code: {e}"
        log_task(task_id, "execute_code_task", tier, tier, task, msg, duration_ms, "error", str(e))
        log_outcome(task_id, 'claude-coder', 'error', time.time() - start, prompt=task, error_class=type(e).__name__)
        send_telemetry(task_id, 'claude-coder', 'error', time.time() - start, error_class=type(e).__name__)
        logger.error(f"[{task_id}] Exception: {e}")
        await stream_q.put("[ERROR] " + msg)
        await stream_q.put("[DONE]")
    finally:
        TASK_STREAMS.pop(task_id, None)
        TASK_PROCESSES.pop(task_id, None)


# ─── Context Injection Helper ───────────────────────────────────────────────
async def _auto_clone_if_missing(cwd: str) -> str:
    """
    If the working_dir doesn't exist on disk, auto-clone the repo from GitHub.
    Derives the repo name from the last path segment of cwd.
    Requires GITHUB_PAT and GITHUB_ORG to be set in .env.
    Returns a status string for logging.
    """
    path = Path(cwd)
    if path.exists():
        return "exists"  # already cloned, nothing to do

    if not GITHUB_PAT:
        return f"ERROR: {cwd} does not exist and GITHUB_PAT is not set — cannot auto-clone"

    repo_name = path.name  # e.g. 'brad-wolfe-cfo' from '/Users/.../Documents/brad-wolfe-cfo'
    clone_url = f"https://{GITHUB_PAT}@github.com/{GITHUB_ORG}/{repo_name}.git"
    parent_dir = str(path.parent)

    logger.info(f"[auto-clone] {cwd} not found — cloning {GITHUB_ORG}/{repo_name}...")
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "clone", clone_url, str(path),
            cwd=parent_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode == 0:
            logger.info(f"[auto-clone] Successfully cloned {repo_name}")
            return f"cloned:{repo_name}"
        else:
            err = stderr.decode().strip()
            logger.error(f"[auto-clone] Failed to clone {repo_name}: {err}")
            return f"ERROR: git clone failed for {repo_name}: {err[:200]}"
    except asyncio.TimeoutError:
        return f"ERROR: git clone timed out for {repo_name}"
    except Exception as e:
        return f"ERROR: auto-clone exception: {e}"


async def _build_context_prompt(cwd: str, task: str) -> str:
    """
    Task 2.1: Build an enriched prompt by prepending project context to the task.
    Gathers: last 10 git commits + CLAUDE.md contents.
    Fails silently — if anything goes wrong, returns the original task unchanged.
    """
    context_parts = []

    # 1. Recent git commits — what changed recently in this repo
    try:
        git_log_proc = await asyncio.create_subprocess_exec(
            "git", "-C", cwd, "log", "--oneline", "-10",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL,
        )
        git_log_out, _ = await asyncio.wait_for(git_log_proc.communicate(), timeout=10)
        git_log = git_log_out.decode("utf-8", errors="replace").strip()
        if git_log:
            context_parts.append(f"## Recent Commits (last 10)\n{git_log}")
    except Exception:
        pass  # Fail silently

    # 2. CLAUDE.md — standing project instructions
    try:
        claude_md_path = Path(cwd) / "CLAUDE.md"
        if claude_md_path.exists():
            claude_md = claude_md_path.read_text(encoding="utf-8", errors="replace").strip()
            if claude_md:
                # Truncate to 3000 chars to avoid blowing up the prompt
                if len(claude_md) > 3000:
                    claude_md = claude_md[:3000] + "\n...[truncated]"
                context_parts.append(f"## Project Instructions (CLAUDE.md)\n{claude_md}")
    except Exception:
        pass  # Fail silently

    # If no context gathered, return task unchanged
    if not context_parts:
        return task

    context_block = "\n\n".join(context_parts)
    enriched = (
        f"<project_context>\n"
        f"{context_block}\n"
        f"</project_context>\n\n"
        f"<task>\n"
        f"{task}\n"
        f"</task>"
    )
    return enriched


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape codes and TTY artifacts from Claude Code output."""
    # Remove ANSI/VT escape sequences
    cleaned = _ANSI_RE.sub("", text)
    # Remove control characters (backspace, bell, null, etc.)
    cleaned = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", cleaned)
    # Remove ^D (EOF marker from macOS script wrapper)
    cleaned = cleaned.replace("\x04", "").replace("^D", "").replace("\x08", "")
    # Remove script(1) session header/footer artifacts like "9;4;0;0;"
    cleaned = re.sub(r"^[\d;]+\s*", "", cleaned.strip())
    cleaned = re.sub(r"\s*[\d;]+$", "", cleaned.strip())
    # Normalize line endings
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    return cleaned.strip()


async def _run_claude_sync(task_id: str, cmd: list, cwd: str, env: dict,
                           tier: str, task: str, timeout: int) -> dict:
    """
    Run Claude Code synchronously and return a result dict.
    Mirrors _run_claude_code_background but returns instead of just logging.
    """
    start = time.time()
    try:
        # Auto git pull — same as async version
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
            log_task(task_id, "execute_code_task_sync", tier, tier, task, msg, duration_ms, "error", msg)
            return {"task_id": task_id, "status": "error", "output": msg,
                    "duration_ms": duration_ms, "exit_code": git_proc.returncode}

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
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
            log_task(task_id, "execute_code_task_sync", tier, tier, task, msg, duration_ms, "timeout", msg)
            return {"task_id": task_id, "status": "error", "output": msg,
                    "duration_ms": duration_ms, "exit_code": -1}

        duration_ms = int((time.time() - start) * 1000)
        out = _strip_ansi(stdout.decode("utf-8", errors="replace").strip())
        err = _strip_ansi(stderr.decode("utf-8", errors="replace").strip())
        output = out or err or f"Task completed with exit code {proc.returncode} (no output captured)"
        status = "success" if proc.returncode == 0 else "error"
        log_task(task_id, "execute_code_task_sync", tier, tier, task, output, duration_ms, status)
        logger.info(f"[{task_id}] Sync completed in {duration_ms}ms, exit={proc.returncode}")
        return {"task_id": task_id, "status": status, "output": output,
                "duration_ms": duration_ms, "exit_code": proc.returncode}

    except Exception as e:
        duration_ms = int((time.time() - start) * 1000)
        msg = f"Error running Claude Code: {e}"
        log_task(task_id, "execute_code_task_sync", tier, tier, task, msg, duration_ms, "error", str(e))
        logger.error(f"[{task_id}] Exception: {e}")
        return {"task_id": task_id, "status": "error", "output": msg,
                "duration_ms": duration_ms, "exit_code": -1}


# ─── Tool 5: execute_code_task ───────────────────────────────────────────────
@mcp.tool()
async def execute_code_task(
    task: str,
    tier: str = "standard",
    working_dir: str = "",
    max_turns: int = 50,
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

    # Auto-clone: if working_dir doesn't exist, clone it from GitHub automatically.
    # This means any new project works on first use — no manual setup required.
    clone_status = await _auto_clone_if_missing(cwd)
    if clone_status.startswith("ERROR"):
        log_task(task_id, "execute_code_task", tier, tier, task, clone_status, 0, "error")
        return json.dumps({"task_id": task_id, "status": "error", "message": clone_status})
    if clone_status.startswith("cloned:"):
        logger.info(f"[{task_id}] Auto-cloned: {clone_status}")

    # Task 2.1: Context injection — gather git log + CLAUDE.md and prepend to task
    # so Claude Code never starts cold. Runs async, fails silently if git/file unavailable.
    enriched_task = await _build_context_prompt(cwd, task)

    # Fix: wrap with 'script -q /dev/null' to create a pseudo-TTY on macOS.
    # Claude Code CLI hangs without a TTY when spawned from a non-interactive
    # subprocess (e.g. a background service). This is a documented macOS bug.
    # See: https://github.com/anthropics/claude-code/issues/9026
    cmd = [
        "script", "-q", "/dev/null",
        CLAUDE_BIN,
        "-p", enriched_task,
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


# ─── Tool 6: execute_code_task_sync ─────────────────────────────────────────
@mcp.tool()
async def execute_code_task_sync(
    task: str,
    working_dir: str = "",
    tier: str = "standard",
) -> str:
    """
    Execute a coding task using Claude Code CLI and wait for the result.
    SYNCHRONOUS: blocks until Claude Code finishes (up to 10 minutes) and returns
    the full output in one call. No polling required.

    Use this when you need the result immediately in the same call.
    For long-running tasks where you can poll later, use execute_code_task instead.

    Args:
        task: The coding task description
        working_dir: Project directory path (defaults to PROJECT_PATH)
        tier: "standard" (Sonnet) or "power" (Opus)

    Returns:
        JSON string with task_id, status, output, duration_ms, exit_code.
    """
    if not Path(CLAUDE_BIN).exists():
        return json.dumps({"task_id": None, "status": "error",
                           "output": f"Claude CLI not found at {CLAUDE_BIN}.",
                           "duration_ms": 0, "exit_code": -1})

    if not ANTHROPIC_API_KEY:
        return json.dumps({"task_id": None, "status": "error",
                           "output": "ANTHROPIC_API_KEY not configured.",
                           "duration_ms": 0, "exit_code": -1})

    task_id = uuid.uuid4().hex[:8]
    cwd = working_dir or PROJECT_PATH
    timeout = 600  # hard 10-minute limit

    logger.info(f"[{task_id}] execute_code_task_sync: tier={tier}, cwd={cwd}")
    logger.info(f"[{task_id}] Task preview: {task[:150]}")

    log_task(task_id, "execute_code_task_sync", tier, tier, task, None, 0, "pending")

    env = os.environ.copy()
    real_key = ENV_VARS.get("ANTHROPIC_API_KEY", "") or ENV_VARS.get("ANTHROPIC_AUTH_TOKEN", "")
    env["ANTHROPIC_API_KEY"] = real_key
    env.pop("ANTHROPIC_BASE_URL", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    env.pop("OPENROUTER_API_KEY", None)

    clone_status = await _auto_clone_if_missing(cwd)
    if clone_status.startswith("ERROR"):
        log_task(task_id, "execute_code_task_sync", tier, tier, task, clone_status, 0, "error")
        return json.dumps({"task_id": task_id, "status": "error", "output": clone_status,
                           "duration_ms": 0, "exit_code": -1})

    enriched_task = await _build_context_prompt(cwd, task)

    cmd = [
        "script", "-q", "/dev/null",
        CLAUDE_BIN,
        "-p", enriched_task,
        "--output-format", "text",
        "--dangerously-skip-permissions",
        "--max-turns", "50",
        "--mcp-config", str(EMPTY_MCP_CFG),
    ]

    result = await _run_claude_sync(task_id, cmd, cwd, env, tier, task, timeout)
    return json.dumps(result)


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

@mcp.custom_route("/api/projects", methods=["GET"])
async def api_projects(request: Request) -> Response:
    """List all git repos cloned under ~/Documents."""
    projects = []
    for entry in sorted(DOCS_DIR.iterdir()):
        if not entry.is_dir() or not (entry / ".git").is_dir():
            continue
        last_commit = ""
        try:
            result = subprocess.run(
                ["git", "-C", str(entry), "log", "-1", "--format=%h %s"],
                capture_output=True, text=True, timeout=5
            )
            last_commit = result.stdout.strip()
        except Exception:
            pass
        projects.append({
            "name": entry.name,
            "path": str(entry),
            "last_commit": last_commit,
            "has_claude_md": (entry / "CLAUDE.md").is_file(),
        })
    return JSONResponse({"projects": projects, "count": len(projects)})

@mcp.custom_route("/api/diff", methods=["GET"])
async def api_diff(request: Request) -> Response:
    """
    Returns the current git diff for a working directory.
    Query param: ?path=/absolute/path/to/repo
    Lets the orchestrator review changes before they are committed.
    """
    path = request.query_params.get("path", "")
    if not path:
        return JSONResponse({"error": "path query param required"}, status_code=400)
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", path, "diff", "--stat",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        stat = stdout.decode("utf-8", errors="replace").strip()

        proc2 = await asyncio.create_subprocess_exec(
            "git", "-C", path, "diff",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout2, _ = await asyncio.wait_for(proc2.communicate(), timeout=10)
        full_diff = stdout2.decode("utf-8", errors="replace").strip()

        return JSONResponse({
            "path": path,
            "stat": stat,
            "diff": full_diff,
            "has_changes": bool(stat),
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@mcp.custom_route("/api/tasks/{task_id}/kill", methods=["POST"])
async def api_kill_task(request: Request) -> Response:
    """Kill a running Claude Code task. Returns {killed: true/false}."""
    task_id = request.path_params.get("task_id", "")
    proc = TASK_PROCESSES.get(task_id)
    if proc is None:
        return JSONResponse({"killed": False, "reason": "task not found or already finished"})
    try:
        proc.terminate()
        await asyncio.sleep(0.5)
        if proc.returncode is None:
            proc.kill()
        TASK_PROCESSES.pop(task_id, None)
        logger.info(f"[{task_id}] Killed by orchestrator")
        return JSONResponse({"killed": True, "task_id": task_id})
    except Exception as e:
        return JSONResponse({"killed": False, "reason": str(e)})

@mcp.custom_route("/api/stream/{task_id}", methods=["GET"])
async def api_stream_task(request: Request) -> Response:
    """SSE stream of live stdout for a running task. Sends one line per event, then [DONE]."""
    task_id = request.path_params.get("task_id", "")
    async def event_generator():
        for _ in range(60):
            if task_id in TASK_STREAMS:
                break
            await asyncio.sleep(0.5)
        else:
            yield "data: [ERROR: task not found]\n\n"
            return
        q = TASK_STREAMS[task_id]
        while True:
            try:
                line = await asyncio.wait_for(q.get(), timeout=30.0)
            except asyncio.TimeoutError:
                yield "data: [HEARTBEAT]\n\n"
                continue
            if line is None:
                yield "data: [DONE]\n\n"
                return
            yield f"data: {line}\n\n"
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@mcp.custom_route("/api/execute_sync", methods=["POST"])
async def api_execute_sync(request: Request) -> Response:
    """
    Synchronous Claude Code execution endpoint — waits for result, returns full output.
    Protected by gateway token. Body: {task, working_dir?, tier?}
    """
    if not _check_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    body = await request.json()
    task = body.get("task", "").strip()
    working_dir = body.get("working_dir", "").strip()
    tier = body.get("tier", "standard")

    if not task:
        return JSONResponse({"error": "task is required"}, status_code=400)

    if not Path(CLAUDE_BIN).exists():
        return JSONResponse({"task_id": None, "status": "error",
                             "output": f"Claude CLI not found at {CLAUDE_BIN}.",
                             "duration_ms": 0, "exit_code": -1}, status_code=500)

    if not ANTHROPIC_API_KEY:
        return JSONResponse({"task_id": None, "status": "error",
                             "output": "ANTHROPIC_API_KEY not configured.",
                             "duration_ms": 0, "exit_code": -1}, status_code=500)

    task_id = uuid.uuid4().hex[:8]
    cwd = working_dir or PROJECT_PATH
    timeout = 600

    logger.info(f"[{task_id}] /api/execute_sync: tier={tier}, cwd={cwd}")
    log_task(task_id, "execute_code_task_sync", tier, tier, task, None, 0, "pending")

    env = os.environ.copy()
    real_key = ENV_VARS.get("ANTHROPIC_API_KEY", "") or ENV_VARS.get("ANTHROPIC_AUTH_TOKEN", "")
    env["ANTHROPIC_API_KEY"] = real_key
    env.pop("ANTHROPIC_BASE_URL", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    env.pop("OPENROUTER_API_KEY", None)

    clone_status = await _auto_clone_if_missing(cwd)
    if clone_status.startswith("ERROR"):
        log_task(task_id, "execute_code_task_sync", tier, tier, task, clone_status, 0, "error")
        return JSONResponse({"task_id": task_id, "status": "error", "output": clone_status,
                             "duration_ms": 0, "exit_code": -1}, status_code=500)

    enriched_task = await _build_context_prompt(cwd, task)

    cmd = [
        "script", "-q", "/dev/null",
        CLAUDE_BIN,
        "-p", enriched_task,
        "--output-format", "text",
        "--dangerously-skip-permissions",
        "--max-turns", "50",
        "--mcp-config", str(EMPTY_MCP_CFG),
    ]

    result = await _run_claude_sync(task_id, cmd, cwd, env, tier, task, timeout)
    status_code = 200 if result["status"] == "success" else 500
    return JSONResponse(result, status_code=status_code)


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

@mcp.custom_route('/api/outcomes', methods=['GET'])
async def get_outcomes(request: Request) -> Response:
    limit = int(request.query_params.get('limit', 100))
    try:
        conn = sqlite3.connect(str(OUTCOMES_DB_PATH))
        rows = conn.execute('SELECT task_id, timestamp_utc, task_type, status, duration_s, credits_used, key_learning, error_class, gateway_version FROM task_outcomes ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
        conn.close()
        cols = ['task_id','timestamp_utc','task_type','status','duration_s','credits_used','key_learning','error_class','gateway_version']
        return Response(json.dumps([dict(zip(cols, r)) for r in rows]), media_type='application/json')
    except Exception as e:
        return Response(json.dumps({'error': str(e)}), status_code=500, media_type='application/json')

@mcp.custom_route('/api/outcomes/summary', methods=['GET'])
async def get_outcomes_summary(request: Request) -> Response:
    try:
        conn = sqlite3.connect(str(OUTCOMES_DB_PATH))
        stats = conn.execute('SELECT COUNT(*), SUM(CASE WHEN status=\'success\' THEN 1 ELSE 0 END), SUM(CASE WHEN status=\'error\' THEN 1 ELSE 0 END), ROUND(AVG(duration_s),1), SUM(credits_used), ROUND(AVG(credits_used),1), COUNT(DISTINCT DATE(timestamp_utc)) FROM task_outcomes').fetchone()
        conn.close()
        cols = ['total_tasks','successful','failed','avg_duration_s','total_credits_used','avg_credits_per_task','active_days']
        return Response(json.dumps(dict(zip(cols, stats))), media_type='application/json')
    except Exception as e:
        return Response(json.dumps({'error': str(e)}), status_code=500, media_type='application/json')

# ─── CI Proxy ────────────────────────────────────────────────────────────────
@mcp.custom_route('/api/ci/{path:path}', methods=['GET'])
async def ci_proxy(request: Request) -> Response:
    path = request.path_params.get('path', '')
    ci_url = f"http://localhost:8766/{path}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(ci_url, params=dict(request.query_params))
        return Response(resp.text, status_code=resp.status_code, media_type=resp.headers.get('content-type', 'application/json'))
    except Exception as e:
        return Response(json.dumps({'error': str(e)}), status_code=502, media_type='application/json')

# ─── ROI Dashboard ────────────────────────────────────────────────────────────
@mcp.custom_route('/roi', methods=['GET'])
async def roi_dashboard(request: Request) -> Response:
    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>IE.AI // ROI DASHBOARD</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;600;700&display=swap');
  *{margin:0;padding:0;box-sizing:border-box;}
  body{background:#0a0a0a;color:#e0e0e0;font-family:'Fira Code','Courier New',monospace;padding:24px;min-height:100vh;}
  h1{color:#00ff88;font-size:1.4rem;letter-spacing:0.15em;margin-bottom:4px;}
  .subtitle{color:#00ccff;font-size:0.75rem;letter-spacing:0.2em;margin-bottom:32px;}
  .stats-row{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-bottom:32px;}
  .card{background:#111;border:1px solid #1a2a1a;border-radius:6px;padding:20px;text-align:center;}
  .card-label{color:#555;font-size:0.7rem;letter-spacing:0.15em;margin-bottom:8px;}
  .card-value{color:#00ff88;font-size:2rem;font-weight:700;}
  .section{background:#111;border:1px solid #1a2a1a;border-radius:6px;padding:20px;margin-bottom:24px;}
  .section-title{color:#00ccff;font-size:0.8rem;letter-spacing:0.2em;margin-bottom:16px;border-bottom:1px solid #1a3a3a;padding-bottom:8px;}
  .credits-cols{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px;}
  .credit-box{background:#0d0d0d;border:1px solid #1a2a1a;border-radius:4px;padding:16px;text-align:center;}
  .credit-box-label{color:#555;font-size:0.7rem;letter-spacing:0.1em;margin-bottom:6px;}
  .credit-box-value{color:#ffaa00;font-size:1.4rem;font-weight:600;}
  .savings-row{text-align:center;padding:12px;}
  .savings-label{color:#555;font-size:0.75rem;letter-spacing:0.15em;margin-bottom:4px;}
  .savings-value{color:#00ff88;font-size:1.8rem;font-weight:700;}
  .improvement-item{padding:8px 0;border-bottom:1px solid #1a2a1a;font-size:0.8rem;}
  .improvement-item:last-child{border-bottom:none;}
  .imp-id{color:#00ccff;margin-right:8px;}
  .imp-text{color:#ccc;}
  .telemetry-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;}
  .tele-box{background:#0d0d0d;border:1px solid #1a2a1a;border-radius:4px;padding:16px;text-align:center;}
  .tele-label{color:#555;font-size:0.7rem;letter-spacing:0.1em;margin-bottom:6px;}
  .tele-value{color:#00ccff;font-size:1.4rem;font-weight:600;}
  .refresh-note{color:#333;font-size:0.65rem;text-align:right;margin-top:16px;}
  .loading{color:#333;font-size:0.8rem;}
  .error{color:#ff4444;font-size:0.75rem;}
</style>
</head>
<body>
<h1>IE.AI // SELF-LEARNING ROI DASHBOARD</h1>
<div class="subtitle">AUTONOMOUS INTELLIGENCE — PERFORMANCE &amp; SAVINGS OVERVIEW</div>

<div class="stats-row">
  <div class="card"><div class="card-label">TASKS EXECUTED</div><div class="card-value" id="total-tasks">—</div></div>
  <div class="card"><div class="card-label">SUCCESS RATE</div><div class="card-value" id="success-rate">—</div></div>
  <div class="card"><div class="card-label">AVG DURATION</div><div class="card-value" id="avg-duration">—</div></div>
</div>

<div class="section">
  <div class="section-title">CREDIT SAVINGS</div>
  <div class="credits-cols">
    <div class="credit-box">
      <div class="credit-box-label">WITHOUT IE.AI</div>
      <div class="credit-box-value" id="cost-without">—</div>
      <div style="color:#555;font-size:0.65rem;margin-top:4px;">Manus @ $0.10/credit × 15 credits/task</div>
    </div>
    <div class="credit-box">
      <div class="credit-box-label">WITH IE.AI</div>
      <div class="credit-box-value" id="cost-with">—</div>
      <div style="color:#555;font-size:0.65rem;margin-top:4px;">Claude Code @ $0.01/task avg</div>
    </div>
  </div>
  <div class="savings-row">
    <div class="savings-label">SAVINGS</div>
    <div class="savings-value" id="savings">—</div>
  </div>
</div>

<div class="section">
  <div class="section-title">IMPROVEMENTS GENERATED</div>
  <div id="improvements-list"><span class="loading">loading...</span></div>
</div>

<div class="section">
  <div class="section-title">TELEMETRY NETWORK</div>
  <div class="telemetry-grid">
    <div class="tele-box"><div class="tele-label">UNIQUE INSTANCES</div><div class="tele-value" id="tele-instances">—</div></div>
    <div class="tele-box"><div class="tele-label">TOTAL EVENTS</div><div class="tele-value" id="tele-events">—</div></div>
  </div>
</div>

<div class="refresh-note">AUTO-REFRESH: 30s &nbsp;|&nbsp; LAST UPDATED: <span id="last-updated">—</span></div>

<script>
async function loadData() {
  // Outcomes summary
  try {
    const r = await fetch('/api/outcomes/summary');
    const d = await r.json();
    const total = d.total_tasks || 0;
    const successful = d.successful || 0;
    const avgDur = d.avg_duration_s || 0;
    document.getElementById('total-tasks').textContent = total.toLocaleString();
    const rate = total > 0 ? ((successful / total) * 100).toFixed(1) : '0.0';
    document.getElementById('success-rate').textContent = rate + '%';
    document.getElementById('avg-duration').textContent = avgDur + 's';
    // Credit savings
    const costWithout = (total * 15 * 0.10).toFixed(2);
    const costWith = (total * 0.01).toFixed(2);
    const savings = (parseFloat(costWithout) - parseFloat(costWith)).toFixed(2);
    document.getElementById('cost-without').textContent = '$' + costWithout;
    document.getElementById('cost-with').textContent = '$' + costWith;
    document.getElementById('savings').textContent = '$' + savings;
  } catch(e) {
    ['total-tasks','success-rate','avg-duration','cost-without','cost-with','savings'].forEach(id => {
      document.getElementById(id).innerHTML = '<span class="error">ERR</span>';
    });
  }

  // Improvements
  try {
    const r = await fetch('/api/ci/api/improvements');
    const items = await r.json();
    const list = document.getElementById('improvements-list');
    if (!items || items.length === 0) {
      list.innerHTML = '<span class="loading">no improvements yet</span>';
    } else {
      list.innerHTML = items.slice(0, 20).map(imp => {
        const id = imp.id || imp.improvement_id || '';
        const text = imp.description || imp.improvement || imp.text || JSON.stringify(imp);
        return '<div class="improvement-item"><span class="imp-id">#' + id + '</span><span class="imp-text">' + text.substring(0,120) + '</span></div>';
      }).join('');
    }
  } catch(e) {
    document.getElementById('improvements-list').innerHTML = '<span class="error">CI API unavailable</span>';
  }

  // Telemetry
  try {
    const r = await fetch('/api/ci/api/telemetry/summary');
    const d = await r.json();
    document.getElementById('tele-instances').textContent = (d.unique_instances || d.instances || 0).toLocaleString();
    document.getElementById('tele-events').textContent = (d.total_events || d.events || 0).toLocaleString();
  } catch(e) {
    document.getElementById('tele-instances').innerHTML = '<span class="error">—</span>';
    document.getElementById('tele-events').innerHTML = '<span class="error">—</span>';
  }

  document.getElementById('last-updated').textContent = new Date().toLocaleTimeString();
}

loadData();
setInterval(loadData, 30000);
</script>
</body>
</html>"""
    return HTMLResponse(html)

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

# ─── Dashboard ────────────────────────────────────────────────────────────────
@mcp.custom_route("/", methods=["GET"])
async def dashboard(request: Request) -> Response:
    index = DASHBOARD_DIR / "index.html"
    if index.exists():
        return HTMLResponse(index.read_text())
    return HTMLResponse(f"<h1>IE.AI MCP Gateway v{VERSION}</h1><p>Dashboard not found.</p>")

# ─── Entry Point ────────────────────────────────────────────────────────────────
async def _ws_tasks_handler(websocket: WebSocket) -> None:
    """WebSocket handler — streams real-time task events to connected dashboards."""
    await ws_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()  # keep alive; we only push
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception:
        ws_manager.disconnect(websocket)

# Register the WebSocket route directly on FastMCP's internal route list
# (custom_route only supports HTTP Route objects, not WebSocketRoute)
mcp._custom_starlette_routes.append(
    WebSocketRoute("/ws/tasks", endpoint=_ws_tasks_handler, name="ws_tasks")
)

if __name__ == "__main__":
    import signal

    def _handle_sigterm(signum, frame):
        """Graceful shutdown: wait up to 10 min for running tasks to finish."""
        import sys
        logger.info("SIGTERM received — waiting for running tasks to drain...")
        deadline = time.time() + 600  # 10 minute drain window
        while TASK_PROCESSES and time.time() < deadline:
            running = list(TASK_PROCESSES.keys())
            logger.info(f"Draining {len(running)} running task(s): {running}")
            time.sleep(5)
        if TASK_PROCESSES:
            logger.warning(f"Drain timeout — force-killing {len(TASK_PROCESSES)} task(s)")
            for proc in TASK_PROCESSES.values():
                try: proc.kill()
                except: pass
        logger.info("Graceful shutdown complete.")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_sigterm)

    init_db()
    logger.info(f"Starting IE.AI MCP Gateway v{VERSION}")
    logger.info(f"Anthropic API: {'configured' if ANTHROPIC_API_KEY else 'NOT CONFIGURED — set ANTHROPIC_API_KEY in ~/.config/ie-mcp/.env'}")
    logger.info(f"Project path: {PROJECT_PATH}")
    logger.info(f"Port: {GATEWAY_PORT}")
    logger.info(f"Dashboard: http://0.0.0.0:{GATEWAY_PORT}/")
    logger.info(f"MCP endpoint: http://0.0.0.0:{GATEWAY_PORT}/mcp")
    mcp.run(transport="streamable-http")

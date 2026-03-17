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

VERSION = "8.13.0"

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

        # Create streaming queue so /api/stream/{task_id} can watch live output
        stream_q: asyncio.Queue = asyncio.Queue()
        TASK_STREAMS[task_id] = stream_q

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            cwd=cwd,
            env=env,
        )
        TASK_PROCESSES[task_id] = proc

        lines: list[str] = []
        deadline = time.time() + timeout
        try:
            while True:
                if time.time() > deadline:
                    proc.kill()
                    await proc.wait()
                    duration_ms = int((time.time() - start) * 1000)
                    msg = f"Task timed out after {timeout}s."
                    log_task(task_id, "execute_code_task", tier, tier, task, msg, duration_ms, "timeout", msg)
                    logger.info(f"[{task_id}] Timed out after {timeout}s")
                    await stream_q.put(None)
                    TASK_STREAMS.pop(task_id, None)
                    TASK_PROCESSES.pop(task_id, None)
                    return
                try:
                    raw = await asyncio.wait_for(proc.stdout.readline(), timeout=1.0)
                except asyncio.TimeoutError:
                    if proc.returncode is not None:
                        break
                    continue
                if not raw:
                    break
                line = _strip_ansi(raw.decode("utf-8", errors="replace").rstrip())
                if line:
                    lines.append(line)
                    await stream_q.put(line)
        finally:
            await proc.wait()
            await stream_q.put(None)
            TASK_STREAMS.pop(task_id, None)
            TASK_PROCESSES.pop(task_id, None)

        duration_ms = int((time.time() - start) * 1000)
        output = "\n".join(lines) or f"Task completed with exit code {proc.returncode} (no output captured)"
        status = "success" if proc.returncode == 0 else "error"
        log_task(task_id, "execute_code_task", tier, tier, task, output, duration_ms, status)
        logger.info(f"[{task_id}] Completed in {duration_ms}ms, exit={proc.returncode}")

    except Exception as e:
        duration_ms = int((time.time() - start) * 1000)
        msg = f"Error running Claude Code: {e}"
        log_task(task_id, "execute_code_task", tier, tier, task, msg, duration_ms, "error", str(e))
        logger.error(f"[{task_id}] Exception: {e}")


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
        "--max-turns", "10",
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
        "--max-turns", "10",
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
    init_db()
    logger.info(f"Starting IE.AI MCP Gateway v{VERSION}")
    logger.info(f"Anthropic API: {'configured' if ANTHROPIC_API_KEY else 'NOT CONFIGURED — set ANTHROPIC_API_KEY in ~/.config/ie-mcp/.env'}")
    logger.info(f"Project path: {PROJECT_PATH}")
    logger.info(f"Port: {GATEWAY_PORT}")
    logger.info(f"Dashboard: http://0.0.0.0:{GATEWAY_PORT}/")
    logger.info(f"MCP endpoint: http://0.0.0.0:{GATEWAY_PORT}/mcp")
    mcp.run(transport="streamable-http")

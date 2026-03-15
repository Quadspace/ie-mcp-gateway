# MCP Python SDK Key Findings

## FastMCP is the way to build this

Instead of hand-rolling JSON-RPC on FastAPI, use the official `mcp` Python SDK:

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("IE.AI Gateway")

@mcp.tool()
async def execute_code_task(task: str, tier: str = "standard") -> str:
    """Execute a coding task via Claude Code on the Mac Mini."""
    # Spawn claude CLI subprocess
    ...
    return result
```

## Transport: Streamable HTTP

The SDK supports `streamable-http` transport which runs on port 8000 by default.
Can be mounted to an existing ASGI server (like FastAPI/Uvicorn).

```python
if __name__ == "__main__":
    mcp.run(transport="streamable-http")
```

## Key architecture decision:

The gateway should use FastMCP from the official SDK, with:
1. `execute_code_task` tool that spawns `claude` CLI as subprocess
2. `remember` / `recall` tools for persistent memory
3. Streamable HTTP transport exposed via ngrok
4. Dashboard as a separate FastAPI mount or static files

## Claude Code subprocess call (from OpenRouter docs):

```python
env = {
    **os.environ,
    "ANTHROPIC_BASE_URL": "https://openrouter.ai/api",
    "ANTHROPIC_AUTH_TOKEN": OPENROUTER_API_KEY,
    "ANTHROPIC_API_KEY": "",  # MUST be empty string
}

result = subprocess.run(
    [CLAUDE_BIN, "--print", "--model", model, task],
    env=env,
    capture_output=True,
    text=True,
    timeout=300,
    cwd=working_directory,
)
```

## Installation:
```bash
pip install mcp
# or
uv pip install mcp
```

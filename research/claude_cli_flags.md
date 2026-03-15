# Claude Code CLI Flags for Gateway Subprocess

## The exact command for non-interactive execution:

```bash
claude -p "task description" \
  --model sonnet \
  --output-format json \
  --dangerously-skip-permissions \
  --max-turns 25 \
  --max-budget-usd 5.00
```

## Key flags:

| Flag | Purpose |
|------|---------|
| `-p "query"` | Print mode — non-interactive, runs task, prints result, exits |
| `--model sonnet` | Use Claude Sonnet (alias). Or `opus` for power tier |
| `--model claude-sonnet-4-6` | Full model name alternative |
| `--output-format json` | Returns structured JSON with result |
| `--output-format text` | Returns plain text (default) |
| `--dangerously-skip-permissions` | Skip all permission prompts (needed for automation) |
| `--max-turns 25` | Limit agentic turns (safety) |
| `--max-budget-usd 5.00` | Cost cap per task |
| `--no-session-persistence` | Don't save session to disk |
| `-c` | Continue most recent conversation |
| `--append-system-prompt` | Add custom instructions |

## Environment variables for OpenRouter:

```bash
ANTHROPIC_BASE_URL="https://openrouter.ai/api"
ANTHROPIC_AUTH_TOKEN="sk-or-v1-..."
ANTHROPIC_API_KEY=""  # MUST be empty string
```

## The complete subprocess call for the gateway:

```python
import subprocess, os, json

def run_claude_task(task: str, tier: str = "standard", working_dir: str = None, max_turns: int = 25):
    model = "opus" if tier == "power" else "sonnet"
    
    env = {
        **os.environ,
        "ANTHROPIC_BASE_URL": "https://openrouter.ai/api",
        "ANTHROPIC_AUTH_TOKEN": OPENROUTER_API_KEY,
        "ANTHROPIC_API_KEY": "",
    }
    
    cmd = [
        CLAUDE_BIN,
        "-p", task,
        "--model", model,
        "--output-format", "json",
        "--dangerously-skip-permissions",
        "--max-turns", str(max_turns),
    ]
    
    result = subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        cwd=working_dir or PROJECT_PATH,
    )
    
    return result.stdout, result.stderr, result.returncode
```

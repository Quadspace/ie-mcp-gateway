# Claude Code + OpenRouter: Exact Configuration

Source: https://openrouter.ai/docs/guides/guides/coding-agents/claude-code-integration

## The Three Required Environment Variables

```bash
export ANTHROPIC_BASE_URL="https://openrouter.ai/api"
export ANTHROPIC_AUTH_TOKEN="sk-or-v1-your-openrouter-key"
export ANTHROPIC_API_KEY=""   # MUST be empty string, NOT unset
```

## Key Facts

1. ANTHROPIC_BASE_URL = "https://openrouter.ai/api" (NOT /v1)
2. ANTHROPIC_AUTH_TOKEN = your OpenRouter API key (this is the AUTH token)
3. ANTHROPIC_API_KEY = "" (MUST be explicitly blank to prevent conflicts)
4. If ANTHROPIC_API_KEY is unset (null), Claude Code falls back to default Anthropic auth
5. No --model flag needed — OpenRouter's "Anthropic Skin" handles model mapping
6. No local proxy needed — direct connection
7. Claude Code speaks native Anthropic protocol to OpenRouter
8. OpenRouter handles "Thinking" blocks and native tool use

## For subprocess call in gateway.py:

```python
env = {
    **os.environ,
    "ANTHROPIC_BASE_URL": "https://openrouter.ai/api",
    "ANTHROPIC_AUTH_TOKEN": "sk-or-v1-...",
    "ANTHROPIC_API_KEY": "",  # MUST be empty string
}

# Run claude CLI with --print flag for non-interactive output
subprocess.run(
    ["/Users/ie.ai-dino1/.local/bin/claude", "--print", prompt],
    env=env,
    capture_output=True,
    text=True,
    timeout=300,
)
```

## Previous bug explanation:
- The old code set ANTHROPIC_API_KEY to the OpenRouter key
- This caused Claude Code to try authenticating with Anthropic servers using an OpenRouter key
- The fix: set ANTHROPIC_AUTH_TOKEN to the OpenRouter key, and ANTHROPIC_API_KEY to ""

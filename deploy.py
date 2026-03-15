#!/usr/bin/env python3
"""
Deploy updated gateway files to the Mac Mini via the MCP endpoint.
This script uses the execute_task tool to write files on the Mac Mini.
"""
import httpx
import json
import sys
import os
import base64

GATEWAY_URL = "https://dinoonemcp.ngrok.app/mcp"
API_KEY = "ie-gateway-mike-2026"

def call_mcp(method, params=None):
    """Call the MCP endpoint."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params or {}
    }
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }
    resp = httpx.post(GATEWAY_URL, json=payload, headers=headers, timeout=300.0)
    return resp.json()

def deploy_file(local_path, remote_path):
    """Deploy a single file by sending its content via execute_task."""
    with open(local_path, 'r') as f:
        content = f.read()
    
    # Escape the content for embedding in a prompt
    escaped = content.replace('\\', '\\\\').replace('`', '\\`').replace('$', '\\$')
    
    prompt = f"""Write the following content to the file at {remote_path}. Create parent directories if needed. Output ONLY "DONE" when complete.

```
{content}
```

Use this exact command:
mkdir -p "$(dirname '{remote_path}')" && cat > '{remote_path}' << 'HEREDOC_EOF_MARKER'
{content}
HEREDOC_EOF_MARKER
"""
    
    print(f"  Deploying {local_path} -> {remote_path}...")
    result = call_mcp("tools/call", {
        "name": "execute_task",
        "arguments": {"prompt": prompt, "tier": "free"}
    })
    
    if "error" in result:
        print(f"  ERROR: {result['error']}")
        return False
    
    content_items = result.get("result", {}).get("content", [])
    response_text = content_items[0]["text"] if content_items else "No response"
    print(f"  Response: {response_text[:200]}")
    return True

def main():
    # Files to deploy
    files = {
        "server/gateway.py": "/Users/ie.ai-dino1/Downloads/ie_mcp_gateway/server/gateway.py",
        "dashboard/index.html": "/Users/ie.ai-dino1/Downloads/ie_mcp_gateway/dashboard/index.html",
        "ecosystem.config.js": "/Users/ie.ai-dino1/Downloads/ie_mcp_gateway/ecosystem.config.js",
        "CLAUDE.md": "/Users/ie.ai-dino1/Downloads/ie_mcp_gateway/CLAUDE.md",
        "README.md": "/Users/ie.ai-dino1/Downloads/ie_mcp_gateway/README.md",
        ".gitignore": "/Users/ie.ai-dino1/Downloads/ie_mcp_gateway/.gitignore",
        "config/.env.template": "/Users/ie.ai-dino1/Downloads/ie_mcp_gateway/config/.env.template",
        "requirements.txt": "/Users/ie.ai-dino1/Downloads/ie_mcp_gateway/requirements.txt",
    }
    
    print("IE.AI MCP Gateway Deployment")
    print("=" * 40)
    
    # First, verify the gateway is alive
    print("\n1. Checking gateway health...")
    try:
        resp = httpx.get("https://dinoonemcp.ngrok.app/health", timeout=10.0)
        health = resp.json()
        print(f"   Gateway is {health.get('status', 'unknown')} (v{health.get('version', '?')})")
    except Exception as e:
        print(f"   ERROR: Gateway unreachable: {e}")
        sys.exit(1)
    
    # Deploy each file
    print("\n2. Deploying files...")
    for local, remote in files.items():
        local_path = os.path.join(os.path.dirname(__file__), local)
        if os.path.exists(local_path):
            deploy_file(local_path, remote)
        else:
            print(f"  SKIP: {local} not found locally")
    
    # Restart PM2
    print("\n3. Restarting PM2...")
    result = call_mcp("tools/call", {
        "name": "execute_task",
        "arguments": {
            "prompt": "Run this shell command and report the output: pm2 restart ie-mcp-gateway && sleep 3 && pm2 status",
            "tier": "free"
        }
    })
    content_items = result.get("result", {}).get("content", [])
    print(f"   {content_items[0]['text'][:300] if content_items else 'No response'}")
    
    print("\nDeployment complete!")

if __name__ == "__main__":
    main()

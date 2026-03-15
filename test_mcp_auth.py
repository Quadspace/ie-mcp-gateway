#!/usr/bin/env python3
"""Test the MCP gateway with correct auth."""
import httpx
import json

GATEWAY = "https://dinoonemcp.ngrok.app"
AUTH = "Bearer ie-gateway-mike-2026"

# Test 1: List MCP tools
print("=== Test 1: List MCP tools ===")
resp = httpx.post(
    f"{GATEWAY}/mcp",
    json={
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {}
    },
    headers={"Content-Type": "application/json", "Authorization": AUTH},
    timeout=30
)
print(f"Status: {resp.status_code}")
try:
    data = resp.json()
    print(json.dumps(data, indent=2)[:5000])
except:
    print(resp.text[:3000])

# Test 2: Simple execute_code_task
print("\n=== Test 2: execute_code_task ===")
resp2 = httpx.post(
    f"{GATEWAY}/mcp",
    json={
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "execute_code_task",
            "arguments": {
                "task": "Create a file /tmp/proof.txt with the content 'IE.AI Gateway works!'",
                "tier": "standard"
            }
        }
    },
    headers={"Content-Type": "application/json", "Authorization": AUTH},
    timeout=120
)
print(f"Status: {resp2.status_code}")
try:
    data2 = resp2.json()
    print(json.dumps(data2, indent=2)[:8000])
except:
    print(resp2.text[:5000])

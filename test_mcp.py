#!/usr/bin/env python3
"""Test the MCP gateway execute_code_task endpoint."""
import httpx
import json
import sys

GATEWAY = "https://dinoonemcp.ngrok.app"

# Test 1: Call the MCP endpoint with tools/list to see available tools
print("=== Test 1: List MCP tools ===")
resp = httpx.post(
    f"{GATEWAY}/mcp",
    json={
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {}
    },
    headers={"Content-Type": "application/json"},
    timeout=30
)
print(f"Status: {resp.status_code}")
try:
    data = resp.json()
    print(json.dumps(data, indent=2)[:3000])
except:
    print(resp.text[:2000])

print("\n=== Test 2: Call execute_code_task with a simple test ===")
resp2 = httpx.post(
    f"{GATEWAY}/mcp",
    json={
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "execute_code_task",
            "arguments": {
                "task": "Write 'hello from gateway' to /tmp/proof.txt",
                "tier": "standard"
            }
        }
    },
    headers={"Content-Type": "application/json"},
    timeout=120
)
print(f"Status: {resp2.status_code}")
try:
    data2 = resp2.json()
    print(json.dumps(data2, indent=2)[:5000])
except:
    print(resp2.text[:5000])

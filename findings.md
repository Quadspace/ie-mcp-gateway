# Key Findings

## Previous MCP tool result shows the gateway DID work at some point
The file at /home/ubuntu/.mcp/tool-results/2026-03-14_18-35-56_macmini-dino-one-mcp_execute_code_task.json
shows a successful execution that returned a calculate_roi function. So the gateway was working at some point.

## Auth key for the gateway
The users.json file shows: gateway_api_key = "test-gateway-key-mike"
So the Bearer token for API calls is: test-gateway-key-mike

## MCP server not configured in this sandbox
The .mcp/servers.json is empty {}. Need to register the server.

## The core issue
The execute_code_task function in gateway.py calls claude CLI which defaults to local Ollama.
The fix should bypass claude CLI entirely and call OpenRouter API directly via httpx.
This is more reliable than trying to configure claude CLI's model routing.

## OpenRouter API Key
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

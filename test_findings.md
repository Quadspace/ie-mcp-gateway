# Test Findings

## Auth key works
The correct auth key is: ie-gateway-mike-2026

## Tools available (from tools/list)
The gateway exposes these MCP tools:
1. execute_task (NOT execute_code_task) - takes "prompt", "session_id", "tier"
2. remember - store persistent memory
3. recall - search persistent memory
4. list_memory - list all memories
5. forget - delete a memory
6. list_sessions - list recent sessions

## execute_task crashes the server
When calling execute_task (or execute_code_task), the server disconnects without response.
This confirms the core bug: the execute function crashes, likely when trying to call claude CLI.

## The tool name is "execute_task" not "execute_code_task"
The skill file references execute_code_task but the actual tool is named execute_task.

## Next step
Need to write a completely new gateway.py that:
1. Calls OpenRouter API directly via httpx (no claude CLI)
2. Properly handles streaming
3. Has all the existing endpoints
4. Fixes the execute_task function

# IE.AI MCP Gateway v6.1.0 — Deployment Instructions

Run this single command on your Mac Mini terminal to deploy:

```bash
cd /Users/ie.ai-dino1/Downloads/ie_mcp_gateway && \
curl -L "https://files.manuscdn.com/user_upload_by_module/session_file/310419663029835097/SqmjLIODxTlORkjV.gz" -o /tmp/gateway_v6.1.tar.gz && \
tar -xzf /tmp/gateway_v6.1.tar.gz --overwrite && \
pm2 restart ie-mcp-gateway && \
sleep 3 && \
curl http://localhost:8765/health
```

This will:
1. Download the v6.1.0 package
2. Extract and overwrite the updated files
3. Restart the PM2 process
4. Verify the health endpoint

After deployment, test with:
```bash
curl -X POST https://dinoonemcp.ngrok.app/mcp \
  -H "Authorization: Bearer ie-gateway-mike-2026" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"execute_code_task","arguments":{"task":"Write a Python one-liner that creates /tmp/proof.txt with the text: IE.AI Gateway v6.1.0 is alive","tier":"standard"}}}'
```

/**
 * IE.AI MCP Gateway v7.0.0 — PM2 Ecosystem Configuration
 * =======================================================
 * Usage:
 *   pm2 start ecosystem.config.js
 *   pm2 restart ie-mcp-gateway
 *   pm2 logs ie-mcp-gateway
 *   pm2 monit
 *
 * The gateway runs as a Python script (FastMCP handles its own HTTP server).
 * ngrok is managed as a separate PM2 process for the tunnel.
 */
module.exports = {
  apps: [
    {
      name: "ie-mcp-gateway",
      script: "python3",
      args: "server/gateway.py",
      cwd: "/Users/ie.ai-dino1/Downloads/ie_mcp_gateway",
      interpreter: "none",
      env: {
        // OpenRouter API (used by claude CLI)
        OPENROUTER_API_KEY: "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",

        // Claude Code CLI environment (routed through OpenRouter)
        ANTHROPIC_BASE_URL: "https://openrouter.ai/api",
        ANTHROPIC_AUTH_TOKEN: "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",
        ANTHROPIC_API_KEY: "",  // MUST be empty string

        // Gateway settings
        GATEWAY_TOKEN: "ie-gateway-mike-2026",
        GATEWAY_PORT: "8765",
        CLAUDE_BIN: "/Users/ie.ai-dino1/.local/bin/claude",
        PROJECT_PATH: "/Users/ie.ai-dino1/Documents/Dino_One_MCP",
        NGROK_DOMAIN: "dinoonemcp.ngrok.app",

        // Python
        PYTHONUNBUFFERED: "1",
        PYTHONPATH: "/Users/ie.ai-dino1/Downloads/ie_mcp_gateway",
      },

      // Logging
      log_file: "/Users/ie.ai-dino1/.config/ie-mcp/gateway.log",
      error_file: "/Users/ie.ai-dino1/.config/ie-mcp/gateway-error.log",
      out_file: "/Users/ie.ai-dino1/.config/ie-mcp/gateway-out.log",
      log_date_format: "YYYY-MM-DD HH:mm:ss Z",

      // Process management
      autorestart: true,
      max_restarts: 10,
      restart_delay: 3000,
      watch: false,
      max_memory_restart: "500M",
      kill_timeout: 600000,
      wait_ready: true,
      listen_timeout: 15000,
    },
    {
      name: "ie-mcp-ngrok",
      script: "ngrok",
      args: "http --url=dinoonemcp.ngrok.app 8765",
      interpreter: "none",
      autorestart: true,
      max_restarts: 10,
      restart_delay: 5000,
      watch: false,
      error_file: "/Users/ie.ai-dino1/.config/ie-mcp/ngrok-error.log",
      out_file: "/Users/ie.ai-dino1/.config/ie-mcp/ngrok-out.log",
    },
  ],
};

#!/bin/bash
# IE.AI MCP Gateway — macOS Auto-Start Installer
# Installs a LaunchAgent so the gateway runs permanently without Terminal

set -e

PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_FILE="$PLIST_DIR/ai.ie.mcp-gateway.plist"
GATEWAY_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$HOME/.config/ie-mcp/logs"
ENV_FILE="$HOME/.config/ie-mcp/.env"

# Load env to get ngrok domain
NGROK_DOMAIN=""
if [ -f "$ENV_FILE" ]; then
  NGROK_DOMAIN=$(grep "^NGROK_DOMAIN=" "$ENV_FILE" | cut -d= -f2 | tr -d '"')
fi
NGROK_DOMAIN="${NGROK_DOMAIN:-dinoonemcp.ngrok.app}"

mkdir -p "$PLIST_DIR" "$LOG_DIR"

echo ""
echo "IE.AI MCP Gateway — Auto-Start Setup"
echo "────────────────────────────────────"
echo ""

# Write the gateway LaunchAgent
cat > "$PLIST_FILE" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ai.ie.mcp-gateway</string>
    <key>ProgramArguments</key>
    <array>
        <string>$HOME/.config/ie-mcp/venv/bin/python3</string>
        <string>$GATEWAY_DIR/server/gateway.py</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
        <key>HOME</key>
        <string>$HOME</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/gateway.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/gateway-error.log</string>
    <key>WorkingDirectory</key>
    <string>$GATEWAY_DIR</string>
</dict>
</plist>
EOF

# Write the ngrok LaunchAgent
NGROK_PLIST="$PLIST_DIR/ai.ie.mcp-ngrok.plist"
NGROK_BIN=$(which ngrok 2>/dev/null || echo "/opt/homebrew/bin/ngrok")

cat > "$NGROK_PLIST" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ai.ie.mcp-ngrok</string>
    <key>ProgramArguments</key>
    <array>
        <string>$NGROK_BIN</string>
        <string>http</string>
        <string>8765</string>
        <string>--domain=$NGROK_DOMAIN</string>
        <string>--log=stdout</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>$HOME</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/ngrok.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/ngrok-error.log</string>
</dict>
</plist>
EOF

# Load both agents
launchctl unload "$PLIST_FILE" 2>/dev/null || true
launchctl unload "$NGROK_PLIST" 2>/dev/null || true
launchctl load "$PLIST_FILE"
launchctl load "$NGROK_PLIST"

echo "✓ Gateway LaunchAgent installed"
echo "✓ ngrok LaunchAgent installed"
echo ""
echo "Both services will now:"
echo "  • Start automatically when your Mac boots"
echo "  • Restart automatically if they crash"
echo "  • Run in the background — no Terminal needed"
echo ""
echo "Logs are at: $LOG_DIR"
echo "  gateway.log       — gateway output"
echo "  ngrok.log         — tunnel output"
echo ""
echo "To check status:   launchctl list | grep ai.ie"
echo "To stop:           launchctl unload ~/Library/LaunchAgents/ai.ie.mcp-gateway.plist"
echo "To start:          launchctl load ~/Library/LaunchAgents/ai.ie.mcp-gateway.plist"
echo ""
echo "Dashboard: http://localhost:8765"
echo ""

#!/bin/bash

# IE.AI MCP Gateway Setup Wizard — v5.0
# ========================================
# World-class easy setup. Asks 3 questions, configures everything.

set -e

CONFIG_DIR=~/.config/ie-mcp
ENV_FILE="$CONFIG_DIR/.env"
USERS_FILE="$CONFIG_DIR/users.json"

# --- Helper Functions ---

print_header() {
    echo -e "\033[1;35m
-- IE.AI MCP Gateway Setup Wizard ------------------------------------------\033[0m"
}

print_subheader() {
    echo -e "\n\033[1;34m$1\033[0m"
}

print_success() {
    echo -e "\033[32m✓ $1\033[0m"
}

print_warning() {
    echo -e "\033[33m! $1\033[0m"
}

print_error() {
    echo -e "\033[31m✗ ERROR: $1\033[0m" >&2
    exit 1
}

check_command() {
    if ! command -v $1 &> /dev/null; then
        print_error "$1 is not installed. Please install it and re-run the script."
    fi
    print_success "$1 is installed"
}

# --- Main Setup Logic ---

print_header
echo "This wizard will configure your self-hosted AI gateway in a few easy steps."
echo "It will create a configuration directory at: $CONFIG_DIR"

# 1. Check Dependencies
print_subheader "1. Checking dependencies..."
check_command "python3"
check_command "pip3"
check_command "pm2"

# 2. Gather User Input
print_subheader "2. Configuration (3 questions)"

# Q1: OpenRouter API Key
read -p "Enter your OpenRouter API Key (sk-or-...) : " OPENROUTER_API_KEY
if [[ -z "$OPENROUTER_API_KEY" ]]; then
    print_error "OpenRouter API Key cannot be empty."
fi

# Q2: ngrok AI Gateway URL (optional)
read -p "Enter your ngrok AI Gateway URL (optional, press Enter to skip): " NGROK_AI_GATEWAY_URL

# Q3: Port
read -p "Enter the port to run the gateway on [8765]: " MCP_PORT
MCP_PORT=${MCP_PORT:-8765}

# 3. Create Configuration
print_subheader "3. Generating configuration files..."

mkdir -p "$CONFIG_DIR"

# Create .env file
cat > "$ENV_FILE" << EOL
# IE.AI MCP Gateway Environment Variables
OPENROUTER_API_KEY=$OPENROUTER_API_KEY
NGROK_AI_GATEWAY_URL=${NGROK_AI_GATEWAY_URL:-""}
MCP_PORT=$MCP_PORT
MAX_WORKERS=3
RATE_LIMIT_RPM=30
OLLAMA_HOST=http://localhost:11434
PROJECT_PATH=~/projects
EOL
print_success "Created environment file: $ENV_FILE"

# Create users.json file
GATEWAY_API_KEY="mcp-$(uuidgen | tr '[:upper:]' '[:lower:]')"
cat > "$USERS_FILE" << EOL
{
  "users": [
    {
      "username": "default-user",
      "gateway_api_key": "$GATEWAY_API_KEY",
      "description": "Default user created by setup wizard"
    }
  ]
}
EOL
print_success "Created user file: $USERS_FILE"

# 4. Install Dependencies
print_subheader "4. Installing Python packages..."

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

if [ -f "$SCRIPT_DIR/requirements.txt" ]; then
    pip3 install --upgrade pip &> /dev/null
    pip3 install -r "$SCRIPT_DIR/requirements.txt"
    print_success "Dependencies installed from requirements.txt"
else
    print_warning "requirements.txt not found. Skipping dependency installation."
fi

# 5. Setup PM2 Service
print_subheader "5. Setting up background service with pm2..."

GATEWAY_SCRIPT="$SCRIPT_DIR/server/gateway.py"

if [ ! -f "$GATEWAY_SCRIPT" ]; then
    print_error "Gateway script not found at $GATEWAY_SCRIPT"
fi

pm2 delete ie-mcp-gateway &> /dev/null || true
pm2 start "$GATEWAY_SCRIPT" --name "ie-mcp-gateway" --interpreter python3
pm2 save

print_success "Gateway service 'ie-mcp-gateway' is running via pm2."
echo "To view logs, run: pm2 logs ie-mcp-gateway"

# 6. Final Summary
print_subheader "Setup Complete!"

echo -e "\nYour gateway is now running and ready to use."
echo -e "  \033[1mDashboard URL:\033[0m http://localhost:$MCP_PORT"

echo -e "\nTo connect Manus to your gateway, use this MCP Server configuration:"
echo -e "  \033[1mServer Name:\033[0m macmini-dino-one-mcp"
echo -e "  \033[1mServer URL:\033[0m  http://localhost:$MCP_PORT/mcp"

echo -e "\nYour Gateway API Key for Manus is:"
echo -e "  \033[1;32m$GATEWAY_API_KEY\033[0m"

echo -e "\nThank you for using the IE.AI MCP Gateway!"

